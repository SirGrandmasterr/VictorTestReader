"""Tests for the OpenAI-compatible remote backend against a fake relay."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.backend import EditCancelled, OutputTruncated, UsageRecord, strip_thinking
from core.remote_service import (
    RemoteService,
    RemoteUnavailable,
    build_ssl_context,
    normalise_api_key,
    relay_throughput,
)


def sse(event):
    return ("data: " + json.dumps(event) + "\n\n").encode("utf-8")


def chunk(content=None, finish_reason=None, **extra):
    delta = {"role": "assistant"}
    if content is not None:
        delta["content"] = content
    delta.update(extra)
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


class FakeRelay:
    """Minimal relay double: /v1/models, /status, streaming chat completions."""

    def __init__(self):
        self.requests = []
        self.mode = "normal"
        self.stream_started = threading.Event()
        self.handler_finished = threading.Event()
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep test output quiet
                pass

            def _json(self, status, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                relay.requests.append(("GET", self.path, dict(self.headers), None))
                if self.headers.get("Authorization") != "Bearer secret":
                    self._json(401, {"error": {"message": "invalid api key"}})
                    return
                if self.path == "/v1/models":
                    self._json(
                        200,
                        {
                            "object": "list",
                            "data": [
                                {"id": "qwen-27b", "object": "model", "owned_by": "gpu-1"},
                                {"id": "aux-model", "object": "model", "owned_by": "gpu-1"},
                            ],
                        },
                    )
                elif self.path == "/status":
                    self._json(
                        200,
                        {
                            "relay": {"version": "1.0", "requests_total": 3},
                            "agents": [
                                {
                                    "name": "gpu-1",
                                    "state": "ready",
                                    "in_flight": 1,
                                    "max_concurrency": 4,
                                    "models": [{"id": "qwen-27b"}, {"id": "aux-model"}],
                                }
                            ],
                        },
                    )
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                relay.requests.append(("POST", self.path, dict(self.headers), body))
                if self.headers.get("Authorization") != "Bearer secret":
                    self._json(401, {"error": {"message": "invalid api key"}})
                    return
                if relay.mode == "model_missing":
                    self._json(503, {"error": {"message": "No agent serves model x", "type": "model_unavailable"}})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    if relay.mode == "slow":
                        self.wfile.write(sse(chunk("First ")))
                        self.wfile.flush()
                        relay.stream_started.set()
                        for _ in range(50):  # ~5 s unless the client disconnects
                            time.sleep(0.1)
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                        self.wfile.write(sse(chunk("never")))
                    elif relay.mode == "truncated":
                        self.wfile.write(sse(chunk("Partial")))
                        self.wfile.write(sse(chunk(None, finish_reason="length")))
                    elif relay.mode == "stream_error":
                        self.wfile.write(sse({"error": {"message": "agent disconnected"}}))
                    elif relay.mode == "inline_think":  # no reasoning parser on the server
                        self.wfile.write(sse(chunk("<thi")))
                        self.wfile.write(sse(chunk("nk>plan</think>\n\nEdited ")))
                        self.wfile.write(sse(chunk("text.")))
                        self.wfile.write(sse(chunk(None, finish_reason="stop")))
                    else:
                        self.wfile.write(sse(chunk(None, reasoning_content="thinking...")))
                        self.wfile.write(sse(chunk("Edited ")))
                        self.wfile.write(sse(chunk("text.")))
                        self.wfile.write(sse(chunk(None, finish_reason="stop")))
                        if body.get("stream_options", {}).get("include_usage"):
                            # vLLM/OpenAI: a final chunk without choices carries the counts
                            self.wfile.write(sse({
                                "id": "chatcmpl-1", "object": "chat.completion.chunk", "choices": [],
                                "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
                            }))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionError, OSError):
                    pass
                finally:
                    relay.handler_finished.set()
                    self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:{0}".format(self.server.server_address[1])

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def relay():
    fake = FakeRelay()
    yield fake
    fake.close()


def test_list_models_reads_models_and_status(relay):
    service = RemoteService(relay.url, "secret")

    models = service.list_models()

    assert models == ["aux-model", "qwen-27b"]
    assert service.connection_summary() == "Connected · gpu-1 · 1 busy"
    assert service.last_status["agents"][0]["name"] == "gpu-1"


def test_stream_edit_assembles_content_and_sends_editor_options(relay):
    service = RemoteService(relay.url, "secret", max_tokens=1234)
    progress = []

    result = service.stream_edit(
        "qwen-27b", "Fix grammar.", "Original.", threading.Event(), on_progress=progress.append
    )

    assert result == "Edited text."
    assert progress == [7, 12]
    method, path, headers, body = relay.requests[-1]
    assert (method, path) == ("POST", "/v1/chat/completions")
    assert headers["Authorization"] == "Bearer secret"
    assert body["model"] == "qwen-27b"
    assert body["stream"] is True
    assert body["max_tokens"] == 1234
    assert body["temperature"] == 0.1
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"][0]["role"] == "system"
    assert "Instruction:\nFix grammar.\n\nText:\nOriginal." == body["messages"][1]["content"]


def test_stream_edit_text_first_orders_the_prompt_for_prefix_caching(relay):
    service = RemoteService(relay.url, "secret")

    service.stream_edit("qwen-27b", "Fix grammar.", "Original.", threading.Event(), text_first=True)

    body = relay.requests[-1][3]
    assert body["messages"][1]["content"] == "Text:\nOriginal.\n\nInstruction:\nFix grammar."
    assert body["messages"][0]["content"].endswith("The instruction follows the text.")


def test_rejected_api_key_has_actionable_error(relay):
    service = RemoteService(relay.url, "wrong")

    with pytest.raises(RemoteUnavailable, match="rejected the API key"):
        service.list_models()
    with pytest.raises(RemoteUnavailable, match="rejected the API key"):
        service.stream_edit("qwen-27b", "i", "t", threading.Event())


def test_relay_error_message_is_passed_through(relay):
    relay.mode = "model_missing"
    service = RemoteService(relay.url, "secret")

    with pytest.raises(RemoteUnavailable, match="No agent serves model x"):
        service.stream_edit("x", "i", "t", threading.Event())


def test_error_inside_stream_is_reported(relay):
    relay.mode = "stream_error"
    service = RemoteService(relay.url, "secret")

    with pytest.raises(RemoteUnavailable, match="agent disconnected"):
        service.stream_edit("qwen-27b", "i", "t", threading.Event())


def test_length_limited_response_raises_truncation(relay):
    relay.mode = "truncated"
    service = RemoteService(relay.url, "secret")

    with pytest.raises(OutputTruncated, match="output token limit"):
        service.stream_edit("qwen-27b", "i", "t", threading.Event())


def test_cancellation_interrupts_a_blocked_stream_promptly(relay):
    relay.mode = "slow"
    service = RemoteService(relay.url, "secret")
    cancel = threading.Event()

    def cancel_after_first_chunk(received):
        cancel.set()

    started = time.time()
    with pytest.raises(EditCancelled):
        service.stream_edit(
            "qwen-27b", "i", "t", cancel, on_progress=cancel_after_first_chunk
        )
    assert time.time() - started < 3
    assert relay.handler_finished.wait(5)


def test_cancelled_before_request_does_not_contact_relay(relay):
    service = RemoteService(relay.url, "secret")
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(EditCancelled):
        service.stream_edit("qwen-27b", "i", "t", cancel)
    assert not any(method == "POST" for method, _, _, _ in relay.requests)


def test_unreachable_relay_has_actionable_error():
    service = RemoteService("http://127.0.0.1:9", "secret", timeout=2)

    with pytest.raises(RemoteUnavailable, match="Cannot reach the relay|did not respond in time"):
        service.list_models()


def test_missing_url_is_reported_without_network():
    service = RemoteService("", "secret")

    assert service.configured is False
    with pytest.raises(RemoteUnavailable, match="No relay URL"):
        service.list_models()


def test_url_normalisation_defaults_to_https_and_strips_v1():
    service = RemoteService("relay.example.com/v1", "k")
    assert service._parsed == {
        "scheme": "https",
        "host": "relay.example.com",
        "port": 443,
        "prefix": "",
    }
    service = RemoteService("http://10.0.0.5:8080/relay/", "k")
    assert service._parsed["port"] == 8080
    assert service._path("/v1/models") == "/relay/v1/models"


def test_no_models_hint_reflects_agent_state():
    service = RemoteService("http://x", "k")
    assert "No GPU agent" in service.no_models_hint()
    service.last_status = {"agents": [{"name": "gpu-1", "state": "loading"}]}
    assert "gpu-1 is still loading" in service.no_models_hint()
    assert service.connection_summary() == "Connected · gpu-1 loading"


def test_on_stream_receives_reasoning_content_and_the_answer_as_they_arrive(relay):
    service = RemoteService(relay.url, "secret")
    pieces = []

    result = service.stream_edit("qwen-27b", "Fix grammar.", "Original.", threading.Event(),
                                 on_stream=lambda kind, text: pieces.append((kind, text)))

    assert result == "Edited text."
    assert pieces == [("thinking", "thinking..."), ("answer", "Edited "), ("answer", "text.")]

    relay.mode = "inline_think"
    pieces = []
    result = service.generate("qwen-27b", [{"role": "user", "content": "x"}], threading.Event(),
                              on_stream=lambda kind, text: pieces.append((kind, text)))
    assert result == "Edited text."
    assert pieces == [("thinking", "plan"), ("answer", "Edited "), ("answer", "text.")]


def test_strip_thinking_removes_reasoning_blocks():
    assert strip_thinking("<think>\nplan\n</think>\n\nEdited.") == "Edited."
    assert strip_thinking("Plain text") == "Plain text"
    assert strip_thinking("<think>unterminated") == ""


def test_ssl_context_verifies_hostnames_and_has_trust_anchors():
    import ssl

    context = build_ssl_context()
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert len(context.get_ca_certs()) > 0


def test_pasted_bearer_prefix_is_stripped_from_api_key(relay):
    assert normalise_api_key("  Bearer secret ") == "secret"
    assert normalise_api_key("bearer secret") == "secret"
    assert RemoteService(relay.url, "Bearer secret").list_models()


def test_internal_http_client_failure_after_cancel_is_reported_as_cancelled(relay, monkeypatch):
    """Regression: tearing the socket down mid-read used to surface http.client's
    "'NoneType' object has no attribute 'close'" instead of EditCancelled."""
    relay.mode = "slow"
    service = RemoteService(relay.url, "secret")
    cancel = threading.Event()
    original_iter = RemoteService._iter_sse_events

    def flaky_iter(response):
        for payload in original_iter(response):
            yield payload
            cancel.set()
            raise AttributeError("'NoneType' object has no attribute 'close'")

    monkeypatch.setattr(RemoteService, "_iter_sse_events", staticmethod(flaky_iter))
    with pytest.raises(EditCancelled):
        service.stream_edit("qwen-27b", "i", "t", cancel)


def test_generate_returns_raw_text_for_custom_messages(relay):
    service = RemoteService(relay.url, "secret")
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    result = service.generate("qwen-27b", messages, threading.Event(), max_tokens=99)

    assert result == "Edited text."
    body = relay.requests[-1][3]
    assert body["messages"] == messages
    assert body["max_tokens"] == 99


def test_usage_is_taken_from_the_final_chunk_and_reported(relay):
    service = RemoteService(relay.url, "secret")
    records = []

    result = service.stream_edit("qwen-27b", "Fix grammar.", "Original.", threading.Event(), on_usage=records.append)

    assert result == "Edited text."
    body = relay.requests[-1][3]
    assert body["stream_options"] == {"include_usage": True}
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, UsageRecord)
    assert (record.prompt_tokens, record.completion_tokens, record.model) == (12, 5, "qwen-27b")
    assert record.total_tokens == 17
    assert 0 <= record.seconds < 10
    assert service.last_usage is record
    assert record.to_dict()["completion_tokens"] == 5


def test_missing_usage_leaves_last_usage_untouched(relay):
    relay.mode = "truncated"  # this stream carries no usage chunk
    service = RemoteService(relay.url, "secret")
    records = []

    with pytest.raises(OutputTruncated):
        service.stream_edit("qwen-27b", "i", "t", threading.Event(), on_usage=records.append)

    assert records == []
    assert service.last_usage is None
    assert RemoteService._parse_usage({"prompt_tokens": "x"}) is None
    assert RemoteService._parse_usage({"prompt_tokens": 3}) == (3, 0)


def test_relay_throughput_sums_agents_and_tolerates_old_relays():
    assert relay_throughput(None) is None
    assert relay_throughput({"agents": []}) is None
    assert relay_throughput({"agents": [{"name": "old", "in_flight": 1}]}) is None  # relay without the fields
    status = {"agents": [
        {"name": "a", "chunks_per_s": 30.5, "queued": 2, "in_flight": 3},
        {"name": "b", "chunks_per_s": 7.5, "queued": 0, "in_flight": 1},
        {"name": "c", "chunks_per_s": "bad", "queued": 1},
    ]}
    assert relay_throughput(status) == {"chunks_per_s": 38.0, "queued": 2, "in_flight": 4}
