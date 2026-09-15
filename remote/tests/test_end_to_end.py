"""End-to-end: desktop RemoteService -> relay -> agent -> (fake) vLLM.

Only vLLM is faked; the relay and the agent are the real packages that ship in
the Docker images, and the client is the same class the Tk app uses.
"""

import asyncio
import json
import threading
import time

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from core.backend import EditCancelled
from core.remote_service import RemoteService, RemoteUnavailable
from teai_agent.agent import Agent
from teai_agent.config import AgentConfig, relay_websocket_url
from teai_relay.config import RelayConfig
from teai_relay.server import create_app

AGENT_KEY = "teai_agent_key_0123456789"
CLIENT_KEY = "teai_client_key_0123456789"


class FakeVLLM:
    def __init__(self):
        self.mode = "normal"
        self.chunk_delay = 0.0  # pause before the last chunk (long-run test: keeps requests in flight)
        self.bodies = []
        self.disconnected = asyncio.Event()
        self.started = asyncio.Event()
        self.release = asyncio.Event()  # lets a "slow" stream finish early
        app = web.Application()
        app.router.add_get("/v1/models", self.models)
        app.router.add_get("/metrics", self.metrics)
        app.router.add_post("/v1/chat/completions", self.chat)
        self.server = TestServer(app)

    async def metrics(self, request):
        return web.Response(
            text='vllm:num_requests_running{model_name="qwen-27b"} 1\nvllm:generation_tokens_total 500\n',
            content_type="text/plain",
        )

    async def models(self, request):
        return web.json_response({"data": [{"id": "qwen-27b", "object": "model", "max_model_len": 32768}]})

    async def chat(self, request):
        body = await request.json()
        self.bodies.append(body)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)

        async def emit(content=None, finish=None, **extra):
            delta = {"role": "assistant"}
            if content is not None:
                delta["content"] = content
            delta.update(extra)
            payload = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            await response.write(("data: " + json.dumps(payload) + "\n\n").encode("utf-8"))

        try:
            await emit(reasoning_content="thinking")
            await emit("Edited ")
            self.started.set()
            if self.mode == "slow":
                for _ in range(100):
                    if self.release.is_set():
                        break
                    await asyncio.sleep(0.1)
                    await response.write(b": keepalive\n\n")
            if self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)
            await emit("text.")
            await emit(None, "stop")
            if (body.get("stream_options") or {}).get("include_usage"):
                usage = {"choices": [], "usage": {"prompt_tokens": 21, "completion_tokens": 4, "total_tokens": 25}}
                await response.write(("data: " + json.dumps(usage) + "\n\n").encode("utf-8"))
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
        except (ConnectionResetError, asyncio.CancelledError):
            self.disconnected.set()
            raise
        return response


class Stack:
    """Relay + agent + fake vLLM running inside one event loop."""

    def __init__(self):
        self.vllm = FakeVLLM()
        self.relay = TestServer(
            create_app(
                RelayConfig(
                    agent_keys={AGENT_KEY: "gpu-e2e"},
                    client_keys={CLIENT_KEY: "desktop"},
                    keepalive_interval=0.5,
                    idle_timeout=10,
                    queue_timeout=10,
                    ws_heartbeat=5,
                )
            )
        )
        self.session = None
        self.agent = None
        self.agent_task = None

    async def __aenter__(self):
        await self.vllm.server.start_server()
        await self.relay.start_server()
        self.session = aiohttp.ClientSession()
        config = AgentConfig(
            relay_ws_url=relay_websocket_url(str(self.relay.make_url(""))),
            relay_key=AGENT_KEY,
            agent_name="gpu-e2e",
            vllm_base_url=str(self.vllm.server.make_url("")),
            health_port=0,
            poll_interval_loading=0.1,
            poll_interval_ready=0.5,
            reconnect_max_delay=1,
            ws_heartbeat=5,
            status_interval=0.3,
        )
        self.agent = Agent(config, session=self.session)
        self.agent_task = asyncio.ensure_future(self.agent.run())
        for _ in range(100):
            if self.agent.relay_connected and self.agent.state == "ready":
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)  # let the ready models frame reach the relay
        return self

    async def __aexit__(self, *exc):
        self.agent.request_stop()
        try:
            await asyncio.wait_for(self.agent_task, timeout=5)
        except Exception:
            self.agent_task.cancel()
        await self.session.close()
        await self.relay.close()
        await self.vllm.server.close()

    def client(self, **kwargs):
        return RemoteService(str(self.relay.make_url("")), CLIENT_KEY, **kwargs)

    async def in_thread(self, func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)


def test_desktop_client_edits_text_through_relay_and_agent():
    async def scenario():
        async with Stack() as stack:
            service = stack.client(max_tokens=2048)
            models = await stack.in_thread(service.list_models)
            assert models == ["qwen-27b"]
            assert service.connection_summary() == "Connected · gpu-e2e"
            assert service.last_status["agents"][0]["agent_version"]

            progress = []
            records = []
            result = await stack.in_thread(
                lambda: service.stream_edit(
                    "qwen-27b", "Fix grammar.", "Orignal text.", threading.Event(), progress.append,
                    on_usage=records.append,
                )
            )
            assert result == "Edited text."
            assert progress == [7, 12]
            assert [(r.prompt_tokens, r.completion_tokens) for r in records] == [(21, 4)]
            body = stack.vllm.bodies[-1]
            assert body["model"] == "qwen-27b"
            assert body["max_tokens"] == 2048
            assert body["stream_options"] == {"include_usage": True}
            assert body["chat_template_kwargs"] == {"enable_thinking": False}
            assert body["messages"][1]["content"].endswith("Text:\nOrignal text.")

            status = await stack.in_thread(service.fetch_status)
            assert status["relay"]["requests_total"] == 1
            assert status["agents"][0]["requests_total"] == 1
            assert status["agents"][0]["in_flight"] == 0
            assert status["agents"][0]["queued"] == 0
            assert status["agents"][0]["chunks_per_s"] > 0

    asyncio.run(scenario())


def test_cancel_in_desktop_reaches_vllm():
    async def scenario():
        async with Stack() as stack:
            stack.vllm.mode = "slow"
            service = stack.client()
            cancel = threading.Event()

            def cancel_after_first_chunk(received):
                cancel.set()

            started = time.time()
            try:
                await stack.in_thread(
                    service.stream_edit, "qwen-27b", "i", "t", cancel, cancel_after_first_chunk
                )
            except EditCancelled:
                pass
            else:
                raise AssertionError("expected EditCancelled")
            assert time.time() - started < 5
            await asyncio.wait_for(stack.vllm.disconnected.wait(), timeout=5)
            for _ in range(50):
                if stack.agent.requests_cancelled == 1 and not stack.agent.in_flight:
                    break
                await asyncio.sleep(0.05)
            assert stack.agent.requests_cancelled == 1
            assert stack.agent.in_flight == {}
            status = await stack.in_thread(service.fetch_status)
            assert status["relay"]["requests_cancelled"] == 1
            assert status["agents"][0]["in_flight"] == 0

    asyncio.run(scenario())


def test_wrong_key_and_unknown_model_surface_clear_errors():
    async def scenario():
        async with Stack() as stack:
            bad = RemoteService(str(stack.relay.make_url("")), "wrong-key")
            try:
                await stack.in_thread(bad.list_models)
            except RemoteUnavailable as exc:
                assert "rejected the API key" in str(exc)
            else:
                raise AssertionError("expected RemoteUnavailable")
            service = stack.client()
            try:
                await stack.in_thread(service.stream_edit, "other-model", "i", "t", threading.Event())
            except RemoteUnavailable as exc:
                assert "No ready GPU agent serves model 'other-model'" in str(exc)
                assert "gpu-e2e (ready: qwen-27b)" in str(exc)
            else:
                raise AssertionError("expected RemoteUnavailable")

    asyncio.run(scenario())


def test_draining_agent_finishes_the_running_request_and_refuses_new_ones():
    async def scenario():
        async with Stack() as stack:
            stack.vllm.mode = "slow"
            service = stack.client()
            first = asyncio.ensure_future(
                stack.in_thread(service.stream_edit, "qwen-27b", "i", "t", threading.Event())
            )
            await asyncio.wait_for(stack.vllm.started.wait(), timeout=5)
            stack.agent.request_drain()
            for _ in range(50):  # the status frame reaches the relay
                status = await stack.in_thread(service.fetch_status)
                if status["agents"] and status["agents"][0]["state"] == "draining":
                    break
                await asyncio.sleep(0.05)
            assert status["agents"][0]["state"] == "draining"
            assert status["agents"][0]["in_flight"] == 1
            assert status["models"] == []  # a draining agent offers nothing
            assert await stack.in_thread(service.list_models) == []
            try:
                await stack.in_thread(service.stream_edit, "qwen-27b", "i", "t", threading.Event())
            except RemoteUnavailable as exc:
                assert "No ready GPU agent serves model 'qwen-27b'" in str(exc)
                assert "draining" in str(exc)
            else:
                raise AssertionError("expected RemoteUnavailable")
            stack.vllm.release.set()
            assert await asyncio.wait_for(first, timeout=10) == "Edited text."
            await asyncio.wait_for(stack.agent_task, timeout=5)  # the agent exited after the drain
            for _ in range(50):
                status = await stack.in_thread(service.fetch_status)
                if not status["agents"]:
                    break
                await asyncio.sleep(0.05)
            assert status["agents"] == []

    asyncio.run(scenario())


def test_agent_metrics_reach_the_relay_status():
    async def scenario():
        async with Stack() as stack:
            service = stack.client()
            for _ in range(50):
                status = await stack.in_thread(service.fetch_status)
                if status["agents"] and status["agents"][0].get("metrics"):
                    break
                await asyncio.sleep(0.05)
            metrics = status["agents"][0]["metrics"]
            assert metrics["num_requests_running"] == 1
            assert metrics["generation_tokens_total"] == 500

    asyncio.run(scenario())
