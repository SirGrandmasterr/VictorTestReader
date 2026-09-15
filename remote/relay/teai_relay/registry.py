"""Connected GPU agents and the routing of requests to them."""

import asyncio
import logging
import time
from collections import deque

from . import protocol

log = logging.getLogger("teai_relay.registry")

RATE_WINDOW = 60.0  # seconds over which chunks/s is averaged
RECENT_REQUESTS = 50  # request outcomes kept for the admin page


class AgentGone(Exception):
    """Raised when a frame cannot be delivered because the socket is closed."""


class RollingRate:
    """Events per second over a sliding window, counted in one-second buckets."""

    def __init__(self, window=RATE_WINDOW, clock=time.monotonic):
        self.window = float(window)
        self.clock = clock
        self._buckets = deque()  # (second, count), oldest first

    def _prune(self, now):
        cutoff = now - self.window
        while self._buckets and self._buckets[0][0] < cutoff:
            self._buckets.popleft()

    def hit(self, count=1):
        now = self.clock()
        second = int(now)
        if self._buckets and self._buckets[-1][0] == second:
            self._buckets[-1] = (second, self._buckets[-1][1] + count)
        else:
            self._buckets.append((second, count))
        self._prune(now)

    def per_second(self):
        """Average rate over the window (the full window, so a burst decays over time)."""
        now = self.clock()
        self._prune(now)
        total = sum(count for _, count in self._buckets)
        return round(total / self.window, 1)


class AgentConnection:
    """One live WebSocket to a GPU agent plus its in-flight request queues."""

    def __init__(self, name, ws, key_name, remote_addr=""):
        self.name = name
        self.ws = ws
        self.key_name = key_name
        self.remote_addr = remote_addr
        self.models = []
        self.state = protocol.STATE_LOADING
        self.max_concurrency = 4
        self.agent_version = ""
        self.protocol_version = None
        self.meta = {}
        self.metrics = {}  # optional GPU metrics from the agent's status frames (tokens/s, KV cache, ...)
        self.connected_at = time.time()
        self.last_seen = self.connected_at
        self.requests_total = 0
        self.in_flight = {}  # request_id -> asyncio.Queue of frames
        self.queued = 0  # requests waiting for a free slot on this agent
        self.chunk_rate = RollingRate()
        self._send_lock = asyncio.Lock()
        self.closed = False

    # -------------------------------------------------------------- metadata
    def apply_hello(self, frame):
        self.protocol_version = frame.get("protocol")
        self.agent_version = str(frame.get("agent_version", ""))
        self.meta = frame.get("meta") if isinstance(frame.get("meta"), dict) else {}
        concurrency = frame.get("max_concurrency")
        if isinstance(concurrency, int) and concurrency >= 1:
            self.max_concurrency = concurrency
        if isinstance(self.meta.get("metrics"), dict):
            self.metrics = dict(self.meta.pop("metrics"))
        self.apply_models(frame)

    def apply_status(self, frame):
        """A periodic ``status`` frame: state (``draining`` takes the agent out of routing) and metrics."""
        state = frame.get("state")
        if state in protocol.AGENT_STATES:
            self.state = state
        if isinstance(frame.get("metrics"), dict):
            self.metrics = dict(frame["metrics"])
        self.last_seen = time.time()

    def apply_models(self, frame):
        state = frame.get("state")
        self.state = state if state in protocol.AGENT_STATES else self.state
        models = frame.get("models")
        if isinstance(models, list):
            normalised = []
            for item in models:
                if isinstance(item, str):
                    normalised.append({"id": item})
                elif isinstance(item, dict) and item.get("id"):
                    normalised.append(dict(item))
            self.models = normalised
        self.last_seen = time.time()

    @property
    def ready(self):
        """Routable: only ``ready`` agents get requests (a draining agent keeps its socket but no new work)."""
        return self.state == protocol.STATE_READY and not self.closed

    @property
    def draining(self):
        return self.state == protocol.STATE_DRAINING

    @property
    def model_ids(self):
        return [model["id"] for model in self.models]

    def serves(self, model_id):
        return model_id in self.model_ids

    @property
    def load(self):
        return len(self.in_flight)

    @property
    def has_capacity(self):
        return self.load < self.max_concurrency

    # ------------------------------------------------------------ messaging
    async def send(self, frame):
        if self.closed:
            raise AgentGone("agent {0} is disconnected".format(self.name))
        try:
            async with self._send_lock:
                await self.ws.send_json(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.closed = True
            raise AgentGone("agent {0}: {1}".format(self.name, exc)) from exc

    async def send_cancel(self, request_id):
        if self.closed:
            return
        try:
            await self.send({"type": protocol.CANCEL, "request_id": request_id})
        except AgentGone as exc:
            log.debug("cancel for %s not delivered: %s", request_id, exc)

    def begin(self, request_id):
        queue = asyncio.Queue()
        self.in_flight[request_id] = queue
        self.requests_total += 1
        return queue

    def end(self, request_id):
        self.in_flight.pop(request_id, None)

    def deliver(self, frame):
        """Route a per-request frame to its queue; drop frames for unknown ids."""
        queue = self.in_flight.get(frame.get("request_id"))
        if queue is None:
            return False
        if frame.get("type") == protocol.CHUNK:
            self.chunk_rate.hit()
        queue.put_nowait(frame)
        return True

    @property
    def chunks_per_s(self):
        """Streamed chunks per second over the last minute (roughly tokens/s for one stream)."""
        return self.chunk_rate.per_second()

    def fail_all(self, message, status=502):
        """Terminate every in-flight request with an error frame."""
        for request_id, queue in list(self.in_flight.items()):
            queue.put_nowait(
                {
                    "type": protocol.ERROR,
                    "request_id": request_id,
                    "status": status,
                    "message": message,
                }
            )

    def snapshot(self):
        return {
            "name": self.name,
            "state": self.state,
            "models": self.models,
            "in_flight": self.load,
            "queued": self.queued,
            "chunks_per_s": self.chunks_per_s,
            "max_concurrency": self.max_concurrency,
            "requests_total": self.requests_total,
            "connected_at": self.connected_at,
            "connected_for": round(time.time() - self.connected_at),
            "last_seen": self.last_seen,
            "agent_version": self.agent_version,
            "key": self.key_name,
            "meta": self.meta,
            "metrics": self.metrics,
        }


class AgentRegistry:
    """Track connected agents and choose one for each request."""

    def __init__(self):
        self.agents = {}
        self.started_at = time.time()
        self.requests_total = 0
        self.requests_failed = 0
        self.requests_cancelled = 0
        self.recent = deque(maxlen=RECENT_REQUESTS)  # outcomes of the last requests, oldest first

    def record_outcome(self, request_id, client, agent, model, status, duration, chunks):
        """Remember how a request ended (never its content) for the admin page."""
        self.recent.append({
            "id": request_id, "client": client, "agent": agent, "model": model, "status": status,
            "duration": round(duration, 2), "chunks": chunks, "finished_at": time.time(),
        })

    def agents_with_key(self, key_name):
        return [agent for agent in self.agents.values() if agent.key_name == key_name]

    def register(self, agent):
        previous = self.agents.get(agent.name)
        self.agents[agent.name] = agent
        return previous

    def unregister(self, agent):
        if self.agents.get(agent.name) is agent:
            del self.agents[agent.name]
            return True
        return False

    def ready_agents(self):
        return [agent for agent in self.agents.values() if agent.ready]

    def models(self):
        """Return the OpenAI ``/v1/models`` list across ready agents."""
        seen = {}
        for agent in sorted(self.ready_agents(), key=lambda item: item.name):
            for model in agent.models:
                entry = seen.setdefault(
                    model["id"],
                    {
                        "id": model["id"],
                        "object": "model",
                        "created": int(agent.connected_at),
                        "owned_by": agent.name,
                        "agents": [],
                    },
                )
                entry["agents"].append(agent.name)
                for key, value in model.items():
                    if key != "id":
                        entry.setdefault(key, value)
        return list(seen.values())

    def pick(self, model_id):
        """Return the least-loaded ready agent serving ``model_id`` (or None)."""
        candidates = [agent for agent in self.ready_agents() if agent.serves(model_id)]
        if not candidates:
            return None
        return min(candidates, key=lambda agent: (agent.load / agent.max_concurrency, agent.name))

    def describe_unavailable(self, model_id):
        """Explain why a model cannot be served right now."""
        if not self.agents:
            return "No GPU agent is connected to the relay."
        parts = []
        for agent in self.agents.values():
            models = ", ".join(agent.model_ids) or "no models"
            parts.append("{0} ({1}: {2})".format(agent.name, agent.state, models))
        return "No ready GPU agent serves model '{0}'. Connected agents: {1}.".format(
            model_id, "; ".join(parts)
        )

    def snapshot(self, relay_version, public_url=""):
        now = time.time()
        return {
            "relay": {
                "version": relay_version,
                "protocol": protocol.PROTOCOL_VERSION,
                "uptime": round(now - self.started_at),
                "requests_total": self.requests_total,
                "requests_failed": self.requests_failed,
                "requests_cancelled": self.requests_cancelled,
                "public_url": public_url,
            },
            "agents": [agent.snapshot() for agent in sorted(self.agents.values(), key=lambda a: a.name)],
            "models": [model["id"] for model in self.models()],
        }
