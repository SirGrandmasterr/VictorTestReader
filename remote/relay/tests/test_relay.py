"""Relay tests: a fake GPU agent over WebSocket plus HTTP clients."""

import asyncio
import io
import json
import os
import sys

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from teai_relay import protocol  # noqa: E402
from teai_relay.__main__ import keys_command  # noqa: E402
from teai_relay.auth import KeyStore, KeyStoreError  # noqa: E402
from teai_relay.config import ConfigError, RelayConfig, parse_keys  # noqa: E402
from teai_relay.registry import RollingRate  # noqa: E402
from teai_relay.server import create_app  # noqa: E402

AGENT_KEY = "teai_agent_key_0123456789"
CLIENT_KEY = "teai_client_key_0123456789"
ADMIN_KEY = "teai_admin_key_0123456789"


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


# ------------------------------------------------------------ key store
def test_keystore_merges_file_over_environment_and_persists(tmp_path):
    path = tmp_path / "keys.json"
    store = KeyStore({AGENT_KEY: "gpu-1"}, {CLIENT_KEY: "alice"}, path)
    assert store.client_keys == {CLIENT_KEY: "alice"}
    assert [row["name"] for row in store.list()] == ["gpu-1", "alice"]
    assert store.list()[1]["source"] == "env"

    bob = store.add("bob")
    gpu2 = store.add("gpu-2", "agent")
    assert bob.startswith("teai_") and bob != gpu2
    with pytest.raises(KeyStoreError, match="already exists"):
        store.add("alice")  # taken by the environment
    with pytest.raises(KeyStoreError):
        store.add("bad name")
    with pytest.raises(KeyStoreError):
        store.add("x", "owner")
    assert store.client_keys == {CLIENT_KEY: "alice", bob: "bob"}
    assert store.agent_keys == {AGENT_KEY: "gpu-1", gpu2: "gpu-2"}

    data = json.loads(path.read_text(encoding="utf-8"))
    assert [e["name"] for e in data["client"]] == ["bob"] and data["client"][0]["key"] == bob
    assert data["client"][0]["created"]
    assert store.revoke("alice") == "client"  # an environment key: blocked, not deleted
    assert store.revoke("bob") == "client"
    assert store.revoke("nobody") is None
    assert store.client_keys == {}
    assert json.loads(path.read_text(encoding="utf-8"))["revoked"] == ["alice"]

    reloaded = KeyStore({AGENT_KEY: "gpu-1"}, {CLIENT_KEY: "alice"}, path)
    assert reloaded.client_keys == {}  # the block survives a restart while alice stays in .env
    assert reloaded.agent_keys == {AGENT_KEY: "gpu-1", gpu2: "gpu-2"}
    assert reloaded.add("alice")  # a new file key under the same name lifts the block and shadows the env key
    assert CLIENT_KEY not in reloaded.client_keys and "alice" in reloaded.client_keys.values()

    path.write_text("{oops", encoding="utf-8")
    damaged = KeyStore({AGENT_KEY: "gpu-1"}, {}, path)
    assert "could not be read" in damaged.load_error and damaged.agent_keys == {AGENT_KEY: "gpu-1"}
    path.write_text(json.dumps({"client": [{"name": "short", "key": "tiny"}, "junk", {"name": "ok", "key": "k" * 20}]}))
    assert list(KeyStore({}, {}, path).client_keys.values()) == ["ok"]


def test_config_allows_empty_environment_keys_when_they_can_come_from_elsewhere(tmp_path):
    with pytest.raises(ConfigError, match="RELAY_CLIENT_KEYS"):
        RelayConfig.from_environ({"RELAY_AGENT_KEYS": "gpu:abcdefghijklmnop"})
    config = RelayConfig.from_environ({"RELAY_AGENT_KEYS": "gpu:abcdefghijklmnop", "RELAY_ADMIN_KEY": ADMIN_KEY})
    assert config.client_keys == {} and config.admin_key == ADMIN_KEY
    keys_file = tmp_path / "keys.json"
    keys_file.write_text("{}", encoding="utf-8")
    config = RelayConfig.from_environ({"RELAY_KEYS_FILE": str(keys_file)})
    assert config.keys_file == str(keys_file)
    with pytest.raises(ConfigError, match="too short"):
        RelayConfig.from_environ({"RELAY_AGENT_KEYS": "gpu:abcdefghijklmnop", "RELAY_ADMIN_KEY": "short"})
    with pytest.raises(ConfigError, match="must differ"):
        RelayConfig.from_environ({
            "RELAY_AGENT_KEYS": "gpu:abcdefghijklmnop", "RELAY_CLIENT_KEYS": "a:0123456789abcdefg",
            "RELAY_ADMIN_KEY": "abcdefghijklmnop",
        })
    # a file with no keys and no admin key cannot serve anything
    with pytest.raises(ConfigError, match="holds no agent keys"):
        create_app(RelayConfig(keys_file=str(keys_file)))


def test_keys_command_edits_the_file(tmp_path):
    path = tmp_path / "keys.json"
    environ = {"RELAY_KEYS_FILE": str(path), "RELAY_CLIENT_KEYS": "alice:" + CLIENT_KEY}
    out = io.StringIO()
    assert keys_command(["add", "bob"], environ, out) == 0
    key = out.getvalue().strip()
    assert key.startswith("teai_")
    out = io.StringIO()
    assert keys_command(["add", "gpu-2", "--agent"], environ, out) == 0
    out = io.StringIO()
    assert keys_command(["list"], environ, out) == 0
    listing = out.getvalue()
    assert "bob" in listing and "gpu-2" in listing and "alice" in listing and key not in listing
    assert keys_command(["add", "bob"], environ, io.StringIO()) == 1
    assert keys_command(["revoke", "bob"], environ, io.StringIO()) == 0
    assert keys_command(["revoke", "bob"], environ, io.StringIO()) == 1
    assert keys_command(["add"], environ, io.StringIO()) == 2
    assert keys_command(["frobnicate"], environ, io.StringIO()) == 2
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [e["name"] for e in data["client"]] == [] and [e["name"] for e in data["agent"]] == ["gpu-2"]


# ---------------------------------------------------------- admin routes
def admin_fixture(tmp_path, admin_key=ADMIN_KEY):
    return RelayFixture(make_config(admin_key=admin_key, keys_file=str(tmp_path / "keys.json")))


def test_admin_routes_need_the_admin_key_and_vanish_without_one(tmp_path):
    async def scenario():
        async with admin_fixture(tmp_path, admin_key="") as relay:
            async with aiohttp.ClientSession(headers={"Authorization": "Bearer " + ADMIN_KEY}) as admin:
                assert (await admin.get(relay.url("/admin"))).status == 404
                assert (await admin.get(relay.url("/admin/keys"))).status == 404
            assert (await relay.session.get(relay.url("/admin/keys"))).status == 404
        async with admin_fixture(tmp_path) as relay:
            assert (await relay.session.get(relay.url("/admin/keys"))).status == 401  # a client key is not enough
            async with aiohttp.ClientSession() as anonymous:
                assert (await anonymous.get(relay.url("/admin"))).status == 401
                wrong = await anonymous.get(relay.url("/admin/keys"), headers={"Authorization": "Bearer nope"})
                assert wrong.status == 401
            async with aiohttp.ClientSession(headers={"Authorization": "Bearer " + ADMIN_KEY}) as admin:
                listing = await admin.get(relay.url("/admin/keys"))
                assert listing.status == 200
                assert listing.headers["Cache-Control"] == "no-store"
                assert (await listing.json())["keys"] == [
                    {"name": "gpu-1", "kind": "agent", "source": "env", "created": ""},
                    {"name": "alice", "kind": "client", "source": "env", "created": ""},
                ]
                assert (await admin.get(relay.url("/v1/models"))).status == 401  # and not a client key either

    run(scenario())


def test_admin_adds_and_revokes_keys_with_immediate_effect(tmp_path):
    async def scenario():
        async with admin_fixture(tmp_path) as relay:
            async with aiohttp.ClientSession(headers={"Authorization": "Bearer " + ADMIN_KEY}) as admin:
                created = await admin.post(relay.url("/admin/keys"), json={"name": "carol"})
                assert created.status == 201
                body = await created.json()
                assert body["name"] == "carol" and body["kind"] == "client"
                carol = body["key"]
                assert carol.startswith("teai_")
                duplicate = await admin.post(relay.url("/admin/keys"), json={"name": "carol"})
                assert duplicate.status == 409
                assert (await admin.post(relay.url("/admin/keys"), json={"name": "x y"})).status == 400
                assert (await admin.post(relay.url("/admin/keys"), data=b"{nope")).status == 400
                assert (await admin.post(relay.url("/admin/keys"), json={"name": "z", "kind": "root"})).status == 400

                async with aiohttp.ClientSession(headers={"Authorization": "Bearer " + carol}) as carol_session:
                    assert (await carol_session.get(relay.url("/v1/models"))).status == 200
                    names = [k["name"] for k in (await (await admin.get(relay.url("/admin/keys"))).json())["keys"]]
                    assert names == ["gpu-1", "alice", "carol"]
                    keys_file = json.loads((tmp_path / "keys.json").read_text(encoding="utf-8"))
                    assert keys_file["client"][0]["key"] == carol
                    listing_text = await (await admin.get(relay.url("/admin/keys"))).text()
                    assert carol not in listing_text

                    revoked = await admin.delete(relay.url("/admin/keys/carol"))
                    assert revoked.status == 200
                    assert (await revoked.json()) == {"name": "carol", "kind": "client", "agents_closed": 0}
                    assert (await carol_session.get(relay.url("/v1/models"))).status == 401
                assert (await admin.delete(relay.url("/admin/keys/carol"))).status == 404
                # an environment key can be blocked too
                assert (await admin.delete(relay.url("/admin/keys/alice"))).status == 200
                assert (await relay.session.get(relay.url("/v1/models"))).status == 401
                assert json.loads((tmp_path / "keys.json").read_text(encoding="utf-8"))["revoked"] == ["alice"]

    run(scenario())


def test_revoking_an_agent_key_closes_its_socket(tmp_path):
    async def scenario():
        async with admin_fixture(tmp_path) as relay:
            async with aiohttp.ClientSession(headers={"Authorization": "Bearer " + ADMIN_KEY}) as admin:
                created = await admin.post(relay.url("/admin/keys"), json={"name": "gpu-2", "kind": "agent"})
                gpu2 = (await created.json())["key"]
                async with relay.agent(name="gpu-2", key=gpu2, models=("other",)) as agent:
                    status = await (await relay.session.get(relay.url("/status"))).json()
                    assert [a["name"] for a in status["agents"]] == ["gpu-2"]
                    page = await admin.get(relay.url("/admin"))
                    assert page.status == 200
                    assert page.headers["Content-Type"].startswith("text/html")
                    assert page.headers["Cache-Control"] == "no-store"
                    html = await page.text()
                    assert "gpu-2" in html and "other" in html and "alice" in html
                    assert gpu2 not in html and AGENT_KEY not in html and CLIENT_KEY not in html and ADMIN_KEY not in html

                    revoked = await admin.delete(relay.url("/admin/keys/gpu-2"))
                    assert (await revoked.json())["agents_closed"] == 1
                    for _ in range(40):
                        if agent.ws.closed:
                            break
                        await asyncio.sleep(0.05)
                    assert agent.ws.closed
                    status = await (await relay.session.get(relay.url("/status"))).json()
                    assert status["agents"] == []
                    # the revoked key no longer opens a socket
                    async with aiohttp.ClientSession() as session:
                        with pytest.raises(aiohttp.WSServerHandshakeError):
                            await session.ws_connect(relay.url("/agent/ws"), headers={"Authorization": "Bearer " + gpu2})

    run(scenario())


def test_admin_page_lists_recent_request_outcomes_without_content(tmp_path):
    async def scenario():
        async with admin_fixture(tmp_path) as relay:
            rejected = await relay.chat(model="missing-model")
            assert rejected.status == 503
            async with relay.agent():
                response = await relay.chat()
                await read_sse(response)
            async with aiohttp.ClientSession(headers={"Authorization": "Bearer " + ADMIN_KEY}) as admin:
                html = await (await admin.get(relay.url("/admin"))).text()
                assert "Last 2 requests" in html
                assert "error 503" in html and "missing-model" in html
                assert "alice" in html and "gpu-1" in html
                assert "Edited text" not in html  # never the model output
                assert "<script" not in html
                status = await (await relay.session.get(relay.url("/status"))).json()
                assert status["relay"]["requests_total"] == 2

    run(scenario())
