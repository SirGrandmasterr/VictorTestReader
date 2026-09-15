"""Agent tests against a fake relay (WebSocket server) and a fake vLLM (HTTP)."""

import asyncio
import json
import os
import socket
import sys

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))

from teai_agent import protocol  # noqa: E402
from teai_agent.agent import Agent  # noqa: E402
from teai_agent.config import AgentConfig, ConfigError, relay_websocket_url  # noqa: E402
from teai_agent.vllm_client import parse_metrics  # noqa: E402

AGENT_KEY = "teai_agent_key_0123456789"


def sse_line(payload):
    return ("data: " + json.dumps(payload) + "\n\n").encode("utf-8")


class FakeVLLM:
    """Serves /v1/models, /version and a streaming /v1/chat/completions."""

    def __init__(self):
        self.models = ["qwen-27b"]
        self.mode = "normal"
        self.requests = []
        self.disconnected = asyncio.Event()
        self.started = asyncio.Event()
        self.release = asyncio.Event()  # lets a "slow" stream finish early
        self.tokens_total = 1000  # generation_tokens_total reported by /metrics
        self.metrics_mode = "normal"  # or "missing" (404)
        app = web.Application()
        app.router.add_get("/v1/models", self.list_models)
        app.router.add_get("/version", self.version)
        app.router.add_get("/metrics", self.metrics)
        app.router.add_post("/v1/chat/completions", self.chat)
        self.server = TestServer(app)

    async def metrics(self, request):
        if self.metrics_mode == "missing":
            raise web.HTTPNotFound()
        text = (
            "# HELP vllm:num_requests_running Number of requests currently running on GPU.\n"
            "# TYPE vllm:num_requests_running gauge\n"
            'vllm:num_requests_running{model_name="qwen-27b"} 2.0\n'
            'vllm:num_requests_waiting{model_name="qwen-27b"} 1.0\n'
            'vllm:gpu_cache_usage_perc{model_name="qwen-27b"} 0.42\n'
            'vllm:generation_tokens_total{model_name="qwen-27b"} ' + str(self.tokens_total) + "\n"
        )
        return web.Response(text=text, content_type="text/plain")

    async def list_models(self, request):
        if self.mode == "down":
            raise web.HTTPServiceUnavailable(text="loading")
        return web.json_response(
            {"data": [{"id": m, "object": "model", "max_model_len": 4096} for m in self.models]}
        )

    async def version(self, request):
        return web.json_response({"version": "0.test"})

    async def chat(self, request):
        body = await request.json()
        self.requests.append((dict(request.headers), body))
        if self.mode == "error":
            return web.json_response({"error": {"message": "prompt too long", "type": "invalid"}}, status=400)
        if not body.get("stream"):
            return web.json_response({"choices": [{"message": {"content": "Edited text."}}]})
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        try:
            await response.write(sse_line({"choices": [{"delta": {"content": "Edited "}}]}))
            self.started.set()
            if self.mode == "slow":
                for _ in range(100):
                    if self.release.is_set():
                        break
                    await asyncio.sleep(0.1)
                    await response.write(b": keepalive\n\n")
            await response.write(sse_line({"choices": [{"delta": {"content": "text."}, "finish_reason": "stop"}]}))
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
        except (ConnectionResetError, asyncio.CancelledError):
            self.disconnected.set()
            raise
        return response

    @property
    def url(self):
        return str(self.server.make_url(""))


class FakeRelay:
    """Accepts one agent socket at a time and records its frames."""

    def __init__(self):
        self.frames = asyncio.Queue()
        self.ws = None
        self.connections = 0
        self.connected = asyncio.Event()
        app = web.Application()
        app.router.add_get("/agent/ws", self.handle)
        self.server = TestServer(app)

    async def handle(self, request):
        if request.headers.get("Authorization") != "Bearer " + AGENT_KEY:
            return web.json_response({"error": "bad key"}, status=401)
        ws = web.WebSocketResponse(heartbeat=5)
        await ws.prepare(request)
        hello = await ws.receive_json()
        assert hello["type"] == protocol.HELLO
        await self.frames.put(hello)
        await ws.send_json({"type": protocol.WELCOME, "protocol": 1, "relay_version": "test"})
        self.ws = ws
        self.connections += 1
        self.connected.set()
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            await self.frames.put(json.loads(msg.data))
        self.connected.clear()
        return ws

    async def send(self, frame):
        await self.ws.send_json(frame)

    async def next_frame(self, kind=None, timeout=5):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            frame = await asyncio.wait_for(self.frames.get(), timeout=max(remaining, 0.01))
            if kind is None or frame["type"] == kind:
                return frame

    async def wait_ready(self):
        """Wait until the agent reports state=ready (via hello or a models frame)."""
        while True:
            frame = await self.next_frame()
            if frame["type"] in (protocol.HELLO, protocol.MODELS) and frame.get("state") == protocol.STATE_READY:
                return frame

    @property
    def url(self):
        return str(self.server.make_url(""))


class Harness:
    def __init__(self, health_port=0, vllm_mode="normal", **overrides):
        self.vllm = FakeVLLM()
        self.vllm.mode = vllm_mode
        self.relay = FakeRelay()
        self.health_port = health_port
        self.overrides = overrides
        self.agent = None
        self.task = None
        self.session = None

    async def __aenter__(self):
        await self.vllm.server.start_server()
        await self.relay.server.start_server()
        config = AgentConfig(
            relay_ws_url=relay_websocket_url(self.relay.url),
            relay_key=AGENT_KEY,
            agent_name="gpu-test",
            vllm_base_url=self.vllm.url,
            max_concurrency=2,
            health_port=self.health_port,
            poll_interval_loading=0.1,
            poll_interval_ready=0.2,
            reconnect_max_delay=1.0,
            request_timeout=30.0,
            ws_heartbeat=5.0,
            status_interval=0.3,
        )
        for key, value in self.overrides.items():
            setattr(config, key, value)
        self.session = aiohttp.ClientSession()
        self.agent = Agent(config, session=self.session)
        self.task = asyncio.ensure_future(self.agent.run())
        return self

    async def __aexit__(self, *exc):
        self.agent.request_stop()
        try:
            await asyncio.wait_for(self.task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            self.task.cancel()
        await self.session.close()
        await self.relay.server.close()
        await self.vllm.server.close()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run(coro):
    return asyncio.run(coro)


async def request_frame(request_id="req-1", stream=True, **body):
    payload = {"model": "qwen-27b", "messages": [{"role": "user", "content": "hi"}], "stream": stream}
    payload.update(body)
    return {"type": protocol.REQUEST, "request_id": request_id, "path": "/v1/chat/completions", "body": payload, "client": "alice"}


# ------------------------------------------------------------------ tests
def test_agent_registers_and_reports_models_when_vllm_is_ready():
    async def scenario():
        async with Harness() as h:
            hello = await h.relay.next_frame(protocol.HELLO)
            assert hello["agent"] == "gpu-test"
            assert hello["protocol"] == protocol.PROTOCOL_VERSION
            assert hello["max_concurrency"] == 2
            assert hello["meta"]["hostname"]
            ready = hello if hello.get("state") == protocol.STATE_READY else await h.relay.wait_ready()
            assert ready["models"] == [{"id": "qwen-27b", "max_model_len": 4096}]
            snapshot = h.agent.snapshot()
            assert snapshot["relay"]["connected"] is True
            assert snapshot["vllm"]["state"] == "ready"
            assert snapshot["vllm"]["version"] == "0.test"

    run(scenario())


def test_streaming_request_is_forwarded_and_chunks_relayed():
    async def scenario():
        async with Harness() as h:
            await h.relay.wait_ready()
            await h.relay.send(await request_frame(temperature=0.1))
            chunk1 = await h.relay.next_frame(protocol.CHUNK)
            chunk2 = await h.relay.next_frame(protocol.CHUNK)
            done = await h.relay.next_frame(protocol.DONE)
            assert json.loads(chunk1["data"])["choices"][0]["delta"]["content"] == "Edited "
            assert json.loads(chunk2["data"])["choices"][0]["finish_reason"] == "stop"
            assert done["request_id"] == "req-1"
            headers, body = h.vllm.requests[-1]
            assert body["temperature"] == 0.1
            assert body["stream"] is True
            assert headers["Accept"] == "text/event-stream"
            assert h.agent.requests_total == 1
            assert h.agent.in_flight == {}

    run(scenario())


def test_non_streaming_request_returns_response_frame():
    async def scenario():
        async with Harness() as h:
            await h.relay.wait_ready()
            await h.relay.send(await request_frame(stream=False))
            response = await h.relay.next_frame(protocol.RESPONSE)
            assert response["status"] == 200
            assert response["body"]["choices"][0]["message"]["content"] == "Edited text."

    run(scenario())


def test_vllm_http_error_becomes_error_frame():
    async def scenario():
        async with Harness() as h:
            await h.relay.wait_ready()
            h.vllm.mode = "error"
            await h.relay.send(await request_frame())
            error = await h.relay.next_frame(protocol.ERROR)
            assert error["status"] == 400
            assert error["message"] == "prompt too long"
            assert h.agent.requests_failed == 1

    run(scenario())


def test_cancel_frame_aborts_the_vllm_request():
    async def scenario():
        async with Harness() as h:
            await h.relay.wait_ready()
            h.vllm.mode = "slow"
            await h.relay.send(await request_frame())
            await asyncio.wait_for(h.vllm.started.wait(), timeout=5)
            assert "req-1" in h.agent.in_flight
            await h.relay.send({"type": protocol.CANCEL, "request_id": "req-1"})
            ack = await h.relay.next_frame(protocol.CANCELLED)
            assert ack["request_id"] == "req-1"
            await asyncio.wait_for(h.vllm.disconnected.wait(), timeout=5)
            assert h.agent.in_flight == {}
            assert h.agent.requests_cancelled == 1

    run(scenario())


def test_requests_are_refused_while_vllm_is_not_ready():
    async def scenario():
        async with Harness(vllm_mode="down") as h:
            hello = await h.relay.next_frame(protocol.HELLO)
            assert hello["state"] == protocol.STATE_LOADING
            await h.relay.send(await request_frame())
            error = await h.relay.next_frame(protocol.ERROR)
            assert error["status"] == 503
            assert "not ready" in error["message"]

    run(scenario())


def test_unknown_path_is_rejected():
    async def scenario():
        async with Harness() as h:
            await h.relay.wait_ready()
            frame = await request_frame()
            frame["path"] = "/v1/embeddings"
            await h.relay.send(frame)
            error = await h.relay.next_frame(protocol.ERROR)
            assert error["status"] == 404

    run(scenario())


def test_agent_reconnects_after_relay_closes_the_socket():
    async def scenario():
        async with Harness() as h:
            await h.relay.wait_ready()
            assert h.relay.connections == 1
            await h.relay.ws.close()
            await asyncio.wait_for(_wait_for(lambda: h.relay.connections >= 2), timeout=10)
            hello = await h.relay.next_frame(protocol.HELLO)
            assert hello["agent"] == "gpu-test"
            assert h.agent.snapshot()["relay"]["connected"] is True

    run(scenario())


def test_vllm_going_away_is_reported_as_unavailable():
    async def scenario():
        async with Harness() as h:
            await h.relay.wait_ready()
            h.vllm.mode = "down"
            frame = await h.relay.next_frame(protocol.MODELS)
            assert frame["state"] == protocol.STATE_UNAVAILABLE
            assert frame["models"] == []
            h.vllm.mode = "normal"
            frame = await h.relay.next_frame(protocol.MODELS)
            assert frame["state"] == protocol.STATE_READY

    run(scenario())


def test_health_endpoint_reflects_relay_connection():
    port = free_port()

    async def scenario():
        async with Harness(health_port=port, health_host="127.0.0.1") as h:
            await h.relay.wait_ready()
            async with aiohttp.ClientSession() as session:
                response = await session.get("http://127.0.0.1:{0}/health".format(port))
                assert response.status == 200
                body = await response.json()
                assert body["agent"] == "gpu-test"
                assert body["relay"]["connected"] is True
                assert body["vllm"]["models"][0]["id"] == "qwen-27b"

    run(scenario())


async def _wait_for(predicate):
    while not predicate():
        await asyncio.sleep(0.05)


def test_relay_url_normalisation_and_config_validation():
    assert relay_websocket_url("https://relay.example.com") == "wss://relay.example.com/agent/ws"
    assert relay_websocket_url("relay.example.com/") == "wss://relay.example.com/agent/ws"
    assert relay_websocket_url("http://10.0.0.5:8080") == "ws://10.0.0.5:8080/agent/ws"
    assert relay_websocket_url("wss://relay.example.com/agent/ws") == "wss://relay.example.com/agent/ws"
    assert relay_websocket_url("https://relay.example.com/prefix") == "wss://relay.example.com/prefix/agent/ws"
    with pytest.raises(ConfigError):
        relay_websocket_url("ftp://nope")
    with pytest.raises(ConfigError, match="RELAY_AGENT_KEY"):
        AgentConfig.from_environ({"RELAY_URL": "https://relay.example.com"})
    config = AgentConfig.from_environ(
        {"RELAY_URL": "https://relay.example.com", "RELAY_AGENT_KEY": "k", "AGENT_NAME": "gpu-9", "AGENT_TLS_VERIFY": "false"}
    )
    assert config.agent_name == "gpu-9"
    assert config.tls_verify is False
    assert config.vllm_base_url == "http://vllm:8000"


# --------------------------------------------------------------- metrics
def test_parse_metrics_matches_suffixes_and_sums_labels():
    text = (
        "# HELP something\n"
        'vllm:num_requests_running{model_name="a"} 2\n'
        'vllm:num_requests_running{model_name="b"} 1\n'
        "vllm_num_requests_waiting 3\n"
        'other:gpu_cache_usage_perc{engine="0"} 0.5\n'
        "vllm:generation_tokens_total 1234.0\n"
        "vllm:prompt_tokens_total 99\n"
        "garbage line without value\n"
        "vllm:num_requests_running nan_is_bad\n"
    )
    assert parse_metrics(text) == {
        "num_requests_running": 3.0, "num_requests_waiting": 3.0, "gpu_cache_usage_perc": 0.5,
        "generation_tokens_total": 1234.0,
    }
    assert parse_metrics("") == {}
    assert parse_metrics("vllm:generation_tokens_total 5") == {"generation_tokens_total": 5.0}


def test_agent_reports_metrics_in_status_frames():
    async def scenario():
        async with Harness() as h:
            await h.relay.wait_ready()
            status = await h.relay.next_frame(protocol.STATUS)
            assert status["state"] == protocol.STATE_READY
            assert status["in_flight"] == 0
            assert status["metrics"]["num_requests_running"] == 2
            assert status["metrics"]["num_requests_waiting"] == 1
            assert status["metrics"]["gpu_cache_usage_perc"] == 0.42
            assert status["metrics"]["generation_tokens_total"] == 1000
            h.vllm.tokens_total += 300
            for _ in range(10):
                status = await h.relay.next_frame(protocol.STATUS)
                if status["metrics"].get("tokens_per_s"):
                    break
            assert status["metrics"]["tokens_per_s"] > 0
            snapshot = h.agent.snapshot()
            assert snapshot["vllm"]["metrics"]["generation_tokens_total"] == 1300
            assert snapshot["draining"] is False
            # a vLLM without /metrics leaves the metrics empty, the agent keeps running
            h.vllm.metrics_mode = "missing"
            for _ in range(10):
                status = await h.relay.next_frame(protocol.STATUS)
                if not status["metrics"]:
                    break
            assert status["metrics"] == {}

    run(scenario())


# ----------------------------------------------------------------- drain
def test_drain_finishes_in_flight_requests_refuses_new_ones_and_exits():
    async def scenario():
        async with Harness() as h:
            await h.relay.wait_ready()
            h.vllm.mode = "slow"
            await h.relay.send(await request_frame("req-1"))
            await asyncio.wait_for(h.vllm.started.wait(), timeout=5)
            h.agent.request_drain()
            h.agent.request_drain()  # idempotent
            assert h.agent.draining is True and h.agent.state == protocol.STATE_DRAINING
            status = await h.relay.next_frame(protocol.STATUS)
            assert status["state"] == protocol.STATE_DRAINING and status["in_flight"] == 1
            await h.relay.send(await request_frame("req-2"))
            error = await h.relay.next_frame(protocol.ERROR)
            assert error["request_id"] == "req-2" and error["status"] == 503 and "draining" in error["message"]
            assert "req-1" in h.agent.in_flight  # the running request is untouched
            assert not h.task.done()
            h.vllm.release.set()
            done = await h.relay.next_frame(protocol.DONE)
            assert done["request_id"] == "req-1"
            await asyncio.wait_for(h.task, timeout=5)  # run() returns once the drain completed
            assert h.agent.in_flight == {}
            assert h.agent.snapshot()["draining"] is True
            assert h.relay.ws.closed

    run(scenario())


def test_drain_timeout_aborts_in_flight_requests_with_502():
    async def scenario():
        async with Harness(drain_timeout=0.5) as h:
            await h.relay.wait_ready()
            h.vllm.mode = "slow"
            await h.relay.send(await request_frame("req-1"))
            await asyncio.wait_for(h.vllm.started.wait(), timeout=5)
            h.agent.request_drain()
            error = await h.relay.next_frame(protocol.ERROR)
            assert error["request_id"] == "req-1" and error["status"] == 502 and "drained" in error["message"]
            await asyncio.wait_for(h.task, timeout=5)
            assert h.agent.requests_failed == 1

    run(scenario())


def test_sigterm_handler_drains_or_stops_depending_on_the_flag():
    async def scenario():
        async with Harness(drain_on_term=True) as h:
            await h.relay.wait_ready()
            h.agent.handle_term()  # what the SIGTERM handler calls
            assert h.agent.draining is True
            await asyncio.wait_for(h.task, timeout=5)  # nothing in flight: exits right away
        async with Harness(drain_on_term=False) as h:
            await h.relay.wait_ready()
            h.agent.handle_term()
            assert h.agent.draining is False
            await asyncio.wait_for(h.task, timeout=5)

    run(scenario())


def test_health_server_drain_endpoint():
    port = free_port()

    async def scenario():
        async with Harness(health_port=port, health_host="127.0.0.1") as h:
            await h.relay.wait_ready()
            async with aiohttp.ClientSession() as session:
                response = await session.post("http://127.0.0.1:{0}/drain".format(port))
                assert response.status == 202
                body = await response.json()
                assert body["draining"] is True and body["state"] == protocol.STATE_DRAINING
            await asyncio.wait_for(h.task, timeout=5)

    run(scenario())


def test_drain_and_status_configuration():
    config = AgentConfig.from_environ({"RELAY_URL": "https://relay.example.com", "RELAY_AGENT_KEY": "k"})
    assert config.status_interval == 10.0 and config.drain_on_term is True and config.drain_timeout == 600.0
    config = AgentConfig.from_environ({
        "RELAY_URL": "https://relay.example.com", "RELAY_AGENT_KEY": "k",
        "AGENT_STATUS_INTERVAL": "5", "AGENT_DRAIN_ON_TERM": "no", "AGENT_DRAIN_TIMEOUT": "30",
    })
    assert config.status_interval == 5.0 and config.drain_on_term is False and config.drain_timeout == 30.0
