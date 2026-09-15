"""Relay tests: a fake GPU agent over WebSocket plus HTTP clients."""

import asyncio
import json
import os
import sys

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from teai_relay import protocol  # noqa: E402
from teai_relay.config import ConfigError, RelayConfig, parse_keys  # noqa: E402
from teai_relay.registry import RollingRate  # noqa: E402
from teai_relay.server import create_app  # noqa: E402

AGENT_KEY = "teai_agent_key_0123456789"
CLIENT_KEY = "teai_client_key_0123456789"


def make_config(**overrides):
    values = dict(
        agent_keys={AGENT_KEY: "gpu-1"},
        client_keys={CLIENT_KEY: "alice"},
        keepalive_interval=0.2,
        idle_timeout=5.0,
        queue_timeout=5.0,
        hello_timeout=1.0,
        ws_heartbeat=5.0,
    )
    values.update(overrides)
    return RelayConfig(**values)


def sse_chunk(text, finish=None):
    return json.dumps(
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}]}
    )


class FakeAgent:
    """Connects like the real agent and answers request frames via ``behaviour``."""

    def __init__(self, server, name="gpu-1", key=AGENT_KEY, models=("qwen-27b",),
                 state=protocol.STATE_READY, max_concurrency=4, behaviour=None):
        self.server = server
        self.name = name
        self.key = key
        self.models = list(models)
        self.state = state
        self.max_concurrency = max_concurrency
        self.behaviour = behaviour or self.default_behaviour
        self.received = []
        self.cancelled = []
        self.welcome = None
        self.session = None
        self.ws = None
        self.task = None
        self.tasks = []

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(
            self.server.make_url("/agent/ws"),
            headers={"Authorization": "Bearer " + self.key},
        )
        await self.ws.send_json(
            {
                "type": protocol.HELLO,
                "protocol": protocol.PROTOCOL_VERSION,
                "agent": self.name,
                "agent_version": "test",
                "state": self.state,
                "models": [{"id": model} for model in self.models],
                "max_concurrency": self.max_concurrency,
            }
        )
        self.welcome = await self.ws.receive_json()
        self.task = asyncio.ensure_future(self._loop())
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        for task in self.tasks:
            task.cancel()
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        if self.ws and not self.ws.closed:
            await self.ws.close()
        if self.session:
            await self.session.close()

    async def _loop(self):
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            frame = json.loads(msg.data)
            self.received.append(frame)
            if frame["type"] == protocol.REQUEST:
                self.tasks.append(asyncio.ensure_future(self.behaviour(self, frame)))
            elif frame["type"] == protocol.CANCEL:
                self.cancelled.append(frame["request_id"])

    async def send_models(self, models, state=protocol.STATE_READY):
        await self.ws.send_json(
            {"type": protocol.MODELS, "state": state, "models": [{"id": m} for m in models]}
        )

    @staticmethod
    async def default_behaviour(agent, frame):
        request_id = frame["request_id"]
        if frame["body"].get("stream"):
            for text in ("Edited ", "text."):
                await agent.ws.send_json({"type": protocol.CHUNK, "request_id": request_id, "data": sse_chunk(text)})
            await agent.ws.send_json({"type": protocol.CHUNK, "request_id": request_id, "data": sse_chunk("", "stop")})
            await agent.ws.send_json({"type": protocol.DONE, "request_id": request_id})
        else:
            await agent.ws.send_json(
                {
                    "type": protocol.RESPONSE,
                    "request_id": request_id,
                    "status": 200,
                    "body": {"choices": [{"message": {"content": "Edited text."}}]},
                }
            )


class RelayFixture:
    def __init__(self, config=None):
        self.config = config or make_config()
        self.server = TestServer(create_app(self.config))
        self.session = None

    async def __aenter__(self):
        await self.server.start_server()
        self.session = aiohttp.ClientSession(headers={"Authorization": "Bearer " + CLIENT_KEY})
        return self

    async def __aexit__(self, *exc):
        await self.session.close()
        await self.server.close()

    def url(self, path):
        return self.server.make_url(path)

    def agent(self, **kwargs):
        return FakeAgent(self.server, **kwargs)

    async def chat(self, model="qwen-27b", stream=True, **extra):
        body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": stream}
        body.update(extra)
        return await self.session.post(self.url("/v1/chat/completions"), json=body)


async def read_sse(response):
    """Return (data payloads, comments) from an SSE response."""
    payloads, comments = [], []
    async for raw in response.content:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith(":"):
            comments.append(line[1:].strip())
        elif line.startswith("data:"):
            payloads.append(line[5:].strip())
    return payloads, comments


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ tests
def test_health_is_public_but_api_needs_a_client_key():
    async def scenario():
        async with RelayFixture() as relay:
            async with aiohttp.ClientSession() as anonymous:
                health = await anonymous.get(relay.url("/health"))
                assert health.status == 200
                assert (await health.json())["agents"] == 0
                models = await anonymous.get(relay.url("/v1/models"))
                assert models.status == 401
                assert (await models.json())["error"]["type"] == "authentication_error"
                wrong = await anonymous.get(relay.url("/status"), headers={"Authorization": "Bearer nope"})
                assert wrong.status == 401
            ok = await relay.session.get(relay.url("/status"))
            assert ok.status == 200
            assert (await ok.json())["agents"] == []

    run(scenario())


def test_agent_registration_and_model_updates_are_visible():
    async def scenario():
        async with RelayFixture() as relay:
            async with relay.agent(state=protocol.STATE_LOADING, models=()) as agent:
                assert agent.welcome["type"] == protocol.WELCOME
                assert agent.welcome["protocol"] == protocol.PROTOCOL_VERSION
                models = await (await relay.session.get(relay.url("/v1/models"))).json()
                assert models["data"] == []
                status = await (await relay.session.get(relay.url("/status"))).json()
                assert status["agents"][0]["name"] == "gpu-1"
                assert status["agents"][0]["state"] == "loading"

                await agent.send_models(["qwen-27b", "small"])
                await asyncio.sleep(0.05)
                models = await (await relay.session.get(relay.url("/v1/models"))).json()
                assert sorted(m["id"] for m in models["data"]) == ["qwen-27b", "small"]
                assert models["data"][0]["owned_by"] == "gpu-1"
                status = await (await relay.session.get(relay.url("/status"))).json()
                assert status["agents"][0]["state"] == "ready"
                assert status["models"] == ["qwen-27b", "small"]
            await asyncio.sleep(0.05)
            health = await (await relay.session.get(relay.url("/health"))).json()
            assert health["agents"] == 0

    run(scenario())


def test_streaming_completion_is_relayed_from_the_agent():
    async def scenario():
        async with RelayFixture() as relay:
            async with relay.agent() as agent:
                response = await relay.chat(temperature=0.1)
                assert response.status == 200
                assert response.headers["Content-Type"].startswith("text/event-stream")
                assert response.headers["X-Request-ID"]
                payloads, _ = await read_sse(response)
                assert payloads[-1] == "[DONE]"
                texts = [json.loads(p)["choices"][0]["delta"]["content"] for p in payloads[:-1]]
                assert texts == ["Edited ", "text.", ""]
                request_frame = next(f for f in agent.received if f["type"] == protocol.REQUEST)
                assert request_frame["path"] == "/v1/chat/completions"
                assert request_frame["body"]["temperature"] == 0.1
                assert request_frame["client"] == "alice"
            status = await (await relay.session.get(relay.url("/status"))).json()
            assert status["relay"]["requests_total"] == 1
            assert status["relay"]["requests_failed"] == 0

    run(scenario())


def test_non_streaming_completion_returns_json():
    async def scenario():
        async with RelayFixture() as relay:
            async with relay.agent():
                response = await relay.chat(stream=False)
                assert response.status == 200
                body = await response.json()
                assert body["choices"][0]["message"]["content"] == "Edited text."

    run(scenario())


def test_unknown_model_or_no_agent_yields_503_with_explanation():
    async def scenario():
        async with RelayFixture() as relay:
            response = await relay.chat()
            assert response.status == 503
            assert "No GPU agent is connected" in (await response.json())["error"]["message"]
            async with relay.agent(state=protocol.STATE_LOADING):
                response = await relay.chat()
                assert response.status == 503
                message = (await response.json())["error"]["message"]
                assert "gpu-1 (loading: qwen-27b)" in message
            missing = await relay.session.post(
                relay.url("/v1/chat/completions"), json={"messages": []}
            )
            assert missing.status == 400
            not_json = await relay.session.post(relay.url("/v1/chat/completions"), data=b"{oops")
            assert not_json.status == 400

    run(scenario())


def test_agent_error_before_streaming_becomes_http_status():
    async def failing(agent, frame):
        await agent.ws.send_json(
            {"type": protocol.ERROR, "request_id": frame["request_id"], "status": 400, "message": "bad prompt"}
        )

    async def scenario():
        async with RelayFixture() as relay:
            async with relay.agent(behaviour=failing):
                response = await relay.chat()
                assert response.status == 400
                assert (await response.json())["error"]["message"] == "bad prompt"

    run(scenario())


def test_agent_disconnect_mid_stream_is_reported_inside_the_stream():
    async def half_then_drop(agent, frame):
        await agent.ws.send_json({"type": protocol.CHUNK, "request_id": frame["request_id"], "data": sse_chunk("Part")})
        await asyncio.sleep(0.05)
        await agent.ws.close()

    async def scenario():
        async with RelayFixture() as relay:
            async with relay.agent(behaviour=half_then_drop):
                response = await relay.chat()
                assert response.status == 200
                payloads, _ = await read_sse(response)
                assert json.loads(payloads[0])["choices"][0]["delta"]["content"] == "Part"
                assert "disconnected" in json.loads(payloads[1])["error"]["message"]
                assert payloads[-1] == "[DONE]"

    run(scenario())


def test_client_disconnect_sends_cancel_to_agent():
    started = asyncio.Event()

    async def slow(agent, frame):
        await agent.ws.send_json({"type": protocol.CHUNK, "request_id": frame["request_id"], "data": sse_chunk("First")})
        started.set()
        await asyncio.sleep(30)

    async def scenario():
        async with RelayFixture() as relay:
            async with relay.agent(behaviour=slow) as agent:
                response = await relay.chat()
                first = await response.content.readline()
                assert first.startswith(b"data:")
                response.close()
                for _ in range(40):
                    if agent.cancelled:
                        break
                    await asyncio.sleep(0.05)
                request_id = next(f["request_id"] for f in agent.received if f["type"] == protocol.REQUEST)
                assert agent.cancelled == [request_id]
            status = await (await relay.session.get(relay.url("/status"))).json()
            assert status["relay"]["requests_cancelled"] == 1

    run(scenario())


def test_requests_queue_when_the_agent_is_at_capacity():
    release = asyncio.Event()

    async def gated(agent, frame):
        await release.wait()
        await FakeAgent.default_behaviour(agent, frame)

    async def scenario():
        async with RelayFixture() as relay:
            async with relay.agent(max_concurrency=1, behaviour=gated) as agent:
                first = await relay.chat()
                second_task = asyncio.ensure_future(relay.chat())
                await asyncio.sleep(0.6)
                assert len([f for f in agent.received if f["type"] == protocol.REQUEST]) == 1
                status = await (await relay.session.get(relay.url("/status"))).json()
                assert status["agents"][0]["in_flight"] == 1
                assert status["agents"][0]["queued"] == 1
                assert status["agents"][0]["chunks_per_s"] == 0
                release.set()
                second = await second_task
                first_payloads, _ = await read_sse(first)
                second_payloads, second_comments = await read_sse(second)
                assert first_payloads[-1] == "[DONE]"
                assert second_payloads[-1] == "[DONE]"
                assert "queued" in second_comments
                assert len([f for f in agent.received if f["type"] == protocol.REQUEST]) == 2
                status = await (await relay.session.get(relay.url("/status"))).json()
                assert status["agents"][0]["queued"] == 0
                assert status["agents"][0]["in_flight"] == 0
                assert status["agents"][0]["chunks_per_s"] == round(6 / 60, 1)  # 3 chunks per stream

    run(scenario())


def test_bad_agent_key_and_missing_hello_are_rejected():
    async def scenario():
        async with RelayFixture() as relay:
            async with aiohttp.ClientSession() as session:
                with pytest.raises(aiohttp.WSServerHandshakeError):
                    await session.ws_connect(
                        relay.url("/agent/ws"), headers={"Authorization": "Bearer wrong-key-0123456789"}
                    )
                ws = await session.ws_connect(
                    relay.url("/agent/ws"), headers={"Authorization": "Bearer " + AGENT_KEY}
                )
                msg = await ws.receive(timeout=3)
                assert msg.type == aiohttp.WSMsgType.CLOSE
                assert msg.data == 4000

    run(scenario())


def test_reconnecting_agent_with_same_name_replaces_the_old_socket():
    async def scenario():
        async with RelayFixture() as relay:
            async with relay.agent(models=("old",)) as old:
                async with relay.agent(models=("new",)) as new:
                    await asyncio.sleep(0.1)
                    status = await (await relay.session.get(relay.url("/status"))).json()
                    assert len(status["agents"]) == 1
                    assert status["models"] == ["new"]
                    assert old.ws.closed
                    assert new.ws.closed is False

    run(scenario())


def test_key_parsing_and_config_validation():
    assert parse_keys("gpu:abcdefghijklmnop,0123456789abcdefg", "agent") == {
        "abcdefghijklmnop": "gpu",
        "0123456789abcdefg": "agent-2",
    }
    with pytest.raises(ConfigError, match="too short"):
        parse_keys("short", "client")
    with pytest.raises(ConfigError, match="RELAY_AGENT_KEYS"):
        RelayConfig.from_environ({"RELAY_CLIENT_KEYS": "x:abcdefghijklmnop"})
    with pytest.raises(ConfigError, match="different"):
        RelayConfig.from_environ(
            {"RELAY_AGENT_KEYS": "abcdefghijklmnop", "RELAY_CLIENT_KEYS": "abcdefghijklmnop"}
        )
    config = RelayConfig.from_environ(
        {
            "RELAY_AGENT_KEYS": "gpu:abcdefghijklmnop",
            "RELAY_CLIENT_KEYS": "a:0123456789abcdefg",
            "RELAY_QUEUE_TIMEOUT": "42",
        }
    )
    assert config.queue_timeout == 42.0
    assert config.agent_keys == {"abcdefghijklmnop": "gpu"}


def test_rolling_rate_averages_over_its_window():
    now = [1000.0]
    rate = RollingRate(window=60, clock=lambda: now[0])
    assert rate.per_second() == 0
    for _ in range(120):
        rate.hit()
    assert rate.per_second() == 2.0
    now[0] += 30
    rate.hit(60)
    assert rate.per_second() == 3.0
    now[0] += 31  # the first burst falls out of the window
    assert rate.per_second() == 1.0
    now[0] += 60
    assert rate.per_second() == 0
