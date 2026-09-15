"""aiohttp application: agent WebSocket endpoint and OpenAI-compatible HTTP API."""

import asyncio
import json
import logging
import secrets
import time

from aiohttp import WSMsgType, web

from . import __version__, protocol
from .auth import bearer_token, lookup
from .config import RelayConfig
from .registry import AgentConnection, AgentGone, AgentRegistry

log = logging.getLogger("teai_relay")

CONFIG_KEY = web.AppKey("config", RelayConfig) if hasattr(web, "AppKey") else "config"
REGISTRY_KEY = web.AppKey("registry", AgentRegistry) if hasattr(web, "AppKey") else "registry"
CLIENT_NAME_KEY = web.RequestKey("client_name", str) if hasattr(web, "RequestKey") else "client_name"


# ----------------------------------------------------------------- helpers
def error_body(message, error_type, status):
    return {"error": {"message": message, "type": error_type, "code": status}}


def json_error(status, message, error_type="relay_error", headers=None):
    return web.json_response(error_body(message, error_type, status), status=status, headers=headers)


def _client_name(request):
    return request.get(CLIENT_NAME_KEY, "?")


@web.middleware
async def auth_middleware(request, handler):
    """Require a client key for everything except /health, / and the agent socket."""
    if request.path in ("/health", "/") or request.path.startswith("/agent/"):
        return await handler(request)
    config = request.app[CONFIG_KEY]
    name = lookup(bearer_token(request), config.client_keys)
    if name is None:
        return json_error(
            401,
            "Missing or invalid API key. Send 'Authorization: Bearer <client key>'.",
            "authentication_error",
            headers={"WWW-Authenticate": 'Bearer realm="teai-relay"'},
        )
    request[CLIENT_NAME_KEY] = name
    return await handler(request)


class SSEStream:
    """Lazily-prepared text/event-stream response with keepalive comments."""

    def __init__(self, request, request_id):
        self.request = request
        self.request_id = request_id
        self.response = None
        self.chunks = 0

    @property
    def prepared(self):
        return self.response is not None

    async def ensure_prepared(self):
        if self.response is None:
            self.response = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "text/event-stream; charset=utf-8",
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "X-Request-ID": self.request_id,
                },
            )
            await self.response.prepare(self.request)
        return self.response

    async def comment(self, text):
        await self.ensure_prepared()
        await self.response.write(": {0}\n\n".format(text).encode("utf-8"))

    async def data(self, payload):
        await self.ensure_prepared()
        self.chunks += 1
        await self.response.write("data: {0}\n\n".format(payload).encode("utf-8"))

    async def finish(self):
        await self.ensure_prepared()
        await self.response.write(b"data: [DONE]\n\n")
        await self.response.write_eof()
        return self.response


class RequestOutcome(Exception):
    """Internal control flow: abort the proxy loop with an HTTP error."""

    def __init__(self, status, message, error_type="relay_error"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.error_type = error_type


# ------------------------------------------------------------- HTTP routes
async def index(request):
    return web.Response(
        text=(
            "TextEnhanceAI relay {0}\n"
            "GET /health, GET /v1/models, POST /v1/chat/completions, GET /status\n"
        ).format(__version__),
        content_type="text/plain",
    )


async def health(request):
    registry = request.app[REGISTRY_KEY]
    return web.json_response(
        {
            "ok": True,
            "version": __version__,
            "agents": len(registry.agents),
            "ready_agents": len(registry.ready_agents()),
        }
    )


async def status(request):
    config = request.app[CONFIG_KEY]
    registry = request.app[REGISTRY_KEY]
    return web.json_response(registry.snapshot(__version__, config.public_url))


async def list_models(request):
    registry = request.app[REGISTRY_KEY]
    return web.json_response({"object": "list", "data": registry.models()})


async def chat_completions(request):
    return await proxy_request(request, "/v1/chat/completions")


async def completions(request):
    return await proxy_request(request, "/v1/completions")


async def _read_json_body(request, config):
    if request.content_length and request.content_length > config.max_body_bytes:
        raise RequestOutcome(413, "Request body is too large.", "invalid_request_error")
    try:
        raw = await request.read()
    except web.HTTPRequestEntityTooLarge:
        raise RequestOutcome(413, "Request body is too large.", "invalid_request_error")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise RequestOutcome(400, "Request body must be valid JSON.", "invalid_request_error")
    if not isinstance(body, dict):
        raise RequestOutcome(400, "Request body must be a JSON object.", "invalid_request_error")
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise RequestOutcome(400, "The 'model' field is required.", "invalid_request_error")
    body["model"] = model.strip()
    return body


async def _wait_for_slot(agent, sse, config, request_id):
    """Poll until the agent has a free slot, sending keepalives while queued.

    The wait is counted in ``agent.queued`` so ``/status`` shows the queue depth.
    """
    if agent.has_capacity:
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.queue_timeout
    next_keepalive = loop.time() + config.keepalive_interval
    log.info("request %s queued behind %d on agent %s", request_id, agent.load, agent.name)
    agent.queued += 1
    try:
        while not agent.has_capacity:
            if agent.closed:
                raise RequestOutcome(502, "The GPU agent disconnected while the request was queued.", "agent_error")
            now = loop.time()
            if now >= deadline:
                raise RequestOutcome(
                    503,
                    "The GPU is busy and the request timed out in the queue after {0:.0f}s.".format(
                        config.queue_timeout
                    ),
                    "server_busy",
                )
            if sse is not None and now >= next_keepalive:
                await sse.comment("queued")
                next_keepalive = now + config.keepalive_interval
            await asyncio.sleep(0.25)
    finally:
        agent.queued -= 1


async def proxy_request(request, path):
    """Forward one OpenAI-style request to a GPU agent and relay the answer."""
    config = request.app[CONFIG_KEY]
    registry = request.app[REGISTRY_KEY]
    request_id = secrets.token_hex(8)
    started = time.time()
    client = _client_name(request)
    registry.requests_total += 1

    try:
        body = await _read_json_body(request, config)
    except RequestOutcome as outcome:
        registry.requests_failed += 1
        return json_error(outcome.status, outcome.message, outcome.error_type, {"X-Request-ID": request_id})

    model = body["model"]
    stream = body.get("stream") is True
    sse = SSEStream(request, request_id) if stream else None
    agent = registry.pick(model)
    if agent is None:
        registry.requests_failed += 1
        message = registry.describe_unavailable(model)
        log.info("request %s client=%s model=%s rejected: %s", request_id, client, model, message)
        return json_error(503, message, "model_unavailable", {"X-Request-ID": request_id})

    queue = None
    outcome_status = "ok"
    try:
        await _wait_for_slot(agent, sse, config, request_id)
        queue = agent.begin(request_id)
        log.info(
            "request %s client=%s agent=%s model=%s stream=%s path=%s",
            request_id, client, agent.name, model, stream, path,
        )
        try:
            await agent.send(
                {
                    "type": protocol.REQUEST,
                    "request_id": request_id,
                    "path": path,
                    "body": body,
                    "client": client,
                }
            )
        except AgentGone:
            raise RequestOutcome(502, "The GPU agent disconnected before the request was sent.", "agent_error")
        idle_since = time.time()
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=config.keepalive_interval)
            except asyncio.TimeoutError:
                if time.time() - idle_since >= config.idle_timeout:
                    raise RequestOutcome(
                        504,
                        "The GPU agent stopped responding for {0:.0f}s.".format(config.idle_timeout),
                        "agent_timeout",
                    )
                if sse is not None:
                    await sse.comment("keepalive")
                continue
            idle_since = time.time()
            kind = frame.get("type")
            if kind == protocol.CHUNK:
                if sse is None:
                    continue  # agent streamed although the client asked for JSON; ignore
                data = frame.get("data", "")
                await sse.data(data if isinstance(data, str) else json.dumps(data))
            elif kind == protocol.DONE:
                if sse is None:
                    raise RequestOutcome(502, "The GPU agent sent a stream for a non-streaming request.", "agent_error")
                return await sse.finish()
            elif kind == protocol.RESPONSE:
                status_code = int(frame.get("status", 200))
                if status_code != 200:
                    outcome_status = "error {0}".format(status_code)
                return web.json_response(
                    frame.get("body"), status=status_code, headers={"X-Request-ID": request_id}
                )
            elif kind == protocol.ERROR:
                raise RequestOutcome(
                    int(frame.get("status", 502)),
                    str(frame.get("message", "The GPU agent reported an error.")),
                    "agent_error",
                )
            elif kind == protocol.CANCELLED:
                raise RequestOutcome(499, "The request was cancelled.", "cancelled")
    except RequestOutcome as outcome:
        registry.requests_failed += 1
        outcome_status = "error {0}".format(outcome.status)
        if sse is not None and sse.prepared:
            # Headers already went out: report the error inside the stream.
            try:
                await sse.data(json.dumps(error_body(outcome.message, outcome.error_type, outcome.status)))
                return await sse.finish()
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            return sse.response
        return json_error(outcome.status, outcome.message, outcome.error_type, {"X-Request-ID": request_id})
    except asyncio.CancelledError:
        # aiohttp cancels the handler when the client disconnects.
        registry.requests_cancelled += 1
        outcome_status = "cancelled by client"
        if queue is not None:
            await agent.send_cancel(request_id)
        raise
    except ConnectionResetError:
        # A write to the client failed: it went away mid-stream.
        registry.requests_cancelled += 1
        outcome_status = "cancelled by client"
        if queue is not None:
            await agent.send_cancel(request_id)
        return sse.response if sse is not None and sse.prepared else web.Response(status=499)
    finally:
        if queue is not None:
            agent.end(request_id)
        log.info(
            "request %s finished: %s in %.1fs (%d chunks)",
            request_id, outcome_status, time.time() - started, sse.chunks if sse else 0,
        )


# ------------------------------------------------------------ agent socket
async def agent_websocket(request):
    config = request.app[CONFIG_KEY]
    registry = request.app[REGISTRY_KEY]
    key_name = lookup(bearer_token(request), config.agent_keys)
    if key_name is None:
        log.warning("agent connection from %s rejected: bad key", request.remote)
        return json_error(401, "Invalid agent key.", "authentication_error")

    ws = web.WebSocketResponse(
        heartbeat=config.ws_heartbeat, max_msg_size=config.max_ws_message_bytes
    )
    await ws.prepare(request)

    try:
        hello = await asyncio.wait_for(ws.receive(), timeout=config.hello_timeout)
    except asyncio.TimeoutError:
        await ws.close(code=4000, message=b"hello timeout")
        return ws
    frame = _parse_frame(hello)
    if not frame or frame.get("type") != protocol.HELLO or not isinstance(frame.get("agent"), str):
        await ws.close(code=4001, message=b"expected hello frame")
        return ws
    if frame.get("protocol") != protocol.PROTOCOL_VERSION:
        await ws.close(code=4002, message=b"unsupported protocol version")
        log.warning("agent %s uses protocol %s, relay speaks %s", frame.get("agent"), frame.get("protocol"), protocol.PROTOCOL_VERSION)
        return ws

    agent = AgentConnection(frame["agent"].strip()[:64] or "agent", ws, key_name, str(request.remote))
    agent.apply_hello(frame)
    previous = registry.register(agent)
    if previous is not None:
        log.info("agent %s reconnected; replacing the previous connection", agent.name)
        previous.closed = True
        previous.fail_all("The GPU agent reconnected; the request was interrupted.")
        try:
            await asyncio.wait_for(
                previous.ws.close(code=4003, message=b"replaced by a new connection"), timeout=5
            )
        except Exception:
            pass
    log.info(
        "agent %s connected (key=%s, state=%s, models=%s, concurrency=%d)",
        agent.name, key_name, agent.state, agent.model_ids, agent.max_concurrency,
    )
    try:
        await agent.send(
            {
                "type": protocol.WELCOME,
                "protocol": protocol.PROTOCOL_VERSION,
                "relay_version": __version__,
                "server_time": time.time(),
                "keepalive_interval": config.keepalive_interval,
            }
        )
        async for msg in ws:
            frame = _parse_frame(msg)
            if frame is None:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
                continue
            agent.last_seen = time.time()
            kind = frame.get("type")
            if kind in (protocol.MODELS, protocol.HELLO):
                agent.apply_models(frame)
                log.info("agent %s: state=%s models=%s", agent.name, agent.state, agent.model_ids)
            elif kind == protocol.STATUS:
                state = frame.get("state")
                if state in protocol.AGENT_STATES:
                    agent.state = state
            elif kind in (protocol.CHUNK, protocol.DONE, protocol.RESPONSE, protocol.ERROR, protocol.CANCELLED):
                agent.deliver(frame)
            else:
                log.debug("agent %s sent unknown frame type %r", agent.name, kind)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("agent %s socket error: %s", agent.name, exc)
    finally:
        agent.closed = True
        agent.fail_all("The GPU agent disconnected.")
        if registry.unregister(agent):
            log.info("agent %s disconnected", agent.name)
        if not ws.closed:
            try:
                await ws.close()
            except Exception:
                pass
    return ws


def _parse_frame(msg):
    if msg.type != WSMsgType.TEXT:
        return None
    try:
        frame = json.loads(msg.data)
    except ValueError:
        return None
    return frame if isinstance(frame, dict) else None


# ------------------------------------------------------------- application
def create_app(config):
    app = web.Application(client_max_size=config.max_body_bytes, middlewares=[auth_middleware])
    app[CONFIG_KEY] = config
    app[REGISTRY_KEY] = AgentRegistry()
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    app.router.add_get("/v1/models", list_models)
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_post("/v1/completions", completions)
    app.router.add_get("/agent/ws", agent_websocket)
    return app


def run(config):
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info(
        "TextEnhanceAI relay %s listening on %s:%d (%d agent keys, %d client keys)",
        __version__, config.host, config.port, len(config.agent_keys), len(config.client_keys),
    )
    web.run_app(
        create_app(config),
        host=config.host,
        port=config.port,
        access_log=None,
        handler_cancellation=True,
        print=None,
    )
