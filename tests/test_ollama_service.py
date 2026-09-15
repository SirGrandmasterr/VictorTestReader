"""Tests for model discovery and streaming cancellation."""

import threading
from types import SimpleNamespace

import pytest

from core.ollama_service import EditCancelled, OllamaService, OllamaUnavailable


class FakeClient:
    def __init__(self, chunks=None, models=None, counts=None):
        self.chunks = chunks or []
        self.models = models or []
        self.counts = counts  # (prompt_eval_count, eval_count) sent with the final message
        self.chat_arguments = None

    def list(self):
        return SimpleNamespace(
            models=[SimpleNamespace(model=name) for name in self.models]
        )

    def chat(self, **kwargs):
        self.chat_arguments = kwargs
        responses = [SimpleNamespace(message=SimpleNamespace(content=chunk)) for chunk in self.chunks]
        if self.counts is not None:
            responses.append(SimpleNamespace(
                message=SimpleNamespace(content=""), done=True, done_reason="stop",
                prompt_eval_count=self.counts[0], eval_count=self.counts[1],
            ))
        return iter(responses)


def test_list_models_returns_unique_sorted_names():
    service = OllamaService(FakeClient(models=["z-model", "a-model", "a-model"]))
    assert service.list_models() == ["a-model", "z-model"]


def test_stream_edit_combines_chunks_and_uses_deterministic_options():
    client = FakeClient(chunks=["Edited ", "text."])
    service = OllamaService(client)

    result = service.stream_edit(
        "local-model", "Fix grammar.", "Original text.", threading.Event()
    )

    assert result == "Edited text."
    assert client.chat_arguments["stream"] is True
    assert client.chat_arguments["options"]["temperature"] == 0.1


def test_stream_edit_text_first_puts_the_text_before_the_instruction():
    client = FakeClient(chunks=["Edited."])
    service = OllamaService(client)

    service.stream_edit("local-model", "Fix grammar.", "Original text.", threading.Event(), text_first=True)

    system, user = client.chat_arguments["messages"]
    assert user["content"] == "Text:\nOriginal text.\n\nInstruction:\nFix grammar."
    assert system["content"].endswith(" The instruction follows the text.")


def test_cancelled_before_request_does_not_call_client():
    client = FakeClient(chunks=["Unused"])
    service = OllamaService(client)
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(EditCancelled):
        service.stream_edit("model", "instruction", "text", cancelled)
    assert client.chat_arguments is None


def test_cancellation_during_stream_discards_partial_output():
    cancelled = threading.Event()

    class CancellingClient(FakeClient):
        def chat(self, **kwargs):
            def stream():
                yield {"message": {"content": "partial"}}
                cancelled.set()
                yield {"message": {"content": "ignored"}}

            return stream()

    service = OllamaService(CancellingClient())
    with pytest.raises(EditCancelled):
        service.stream_edit("model", "instruction", "text", cancelled)


def test_connection_failure_has_actionable_error():
    class BrokenClient(FakeClient):
        def list(self):
            raise ConnectionError("offline")

    with pytest.raises(OllamaUnavailable, match="Ensure the Ollama service is running"):
        OllamaService(BrokenClient()).list_models()


def test_usage_is_taken_from_the_final_message_when_present():
    client = FakeClient(chunks=["Edited ", "text."], counts=(40, 9))
    service = OllamaService(client)
    records = []

    result = service.stream_edit("local-model", "Fix grammar.", "Original text.", threading.Event(),
                                 on_usage=records.append)

    assert result == "Edited text."
    assert len(records) == 1
    assert (records[0].prompt_tokens, records[0].completion_tokens, records[0].model) == (40, 9, "local-model")
    assert service.last_usage is records[0]

    # dict-style responses (older client versions) work the same way
    class DictClient(FakeClient):
        def chat(self, **kwargs):
            return iter([{"message": {"content": "x"}}, {"message": {"content": ""}, "done": True,
                         "prompt_eval_count": 3, "eval_count": 1}])

    service = OllamaService(DictClient())
    assert service.stream_edit("m", "i", "t", threading.Event()) == "x"
    assert (service.last_usage.prompt_tokens, service.last_usage.completion_tokens) == (3, 1)


def test_missing_counts_report_no_usage():
    service = OllamaService(FakeClient(chunks=["Edited."]))
    records = []

    service.stream_edit("m", "i", "t", threading.Event(), on_usage=records.append)

    assert records == [] and service.last_usage is None


def test_on_stream_receives_the_thinking_field_and_the_answer_as_they_arrive():
    class ThinkingClient(FakeClient):
        def chat(self, **kwargs):
            return iter([
                SimpleNamespace(message=SimpleNamespace(content="", thinking="Let me ")),
                {"message": {"content": "", "thinking": "check."}},  # dict-style chunks work too
                SimpleNamespace(message=SimpleNamespace(content="Edited ")),
                SimpleNamespace(message=SimpleNamespace(content="text.")),
            ])

    pieces = []
    result = OllamaService(ThinkingClient()).stream_edit(
        "m", "i", "t", threading.Event(), on_stream=lambda kind, text: pieces.append((kind, text))
    )

    assert result == "Edited text."
    assert pieces == [("thinking", "Let me "), ("thinking", "check."), ("answer", "Edited "), ("answer", "text.")]


def test_on_stream_splits_inline_think_blocks_and_the_result_stays_clean():
    client = FakeClient(chunks=["<think>\nplan", "</thi", "nk>\n\nEdited ", "text."])
    pieces = []

    result = OllamaService(client).stream_edit(
        "m", "i", "t", threading.Event(), on_stream=lambda kind, text: pieces.append((kind, text))
    )

    assert result == "Edited text."
    assert pieces == [("thinking", "\nplan"), ("answer", "Edited "), ("answer", "text.")]
    # without a callback nothing changes
    assert OllamaService(FakeClient(chunks=["<think>x</think>y"])).stream_edit("m", "i", "t", threading.Event()) == "y"
