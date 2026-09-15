"""Thread-friendly Ollama access with streaming cancellation."""

import time

from .backend import (
    DEFAULT_MAX_TOKENS,
    SYSTEM_PROMPT,
    TEMPERATURE,
    TOP_P,
    BackendUnavailable,
    EditCancelled,
    OutputTruncated,
    UsageRecord,
    build_messages,
    strip_thinking,
    truncated_message,
)

__all__ = [
    "EditCancelled",
    "OllamaService",
    "OllamaUnavailable",
    "SYSTEM_PROMPT",
]


class OllamaUnavailable(BackendUnavailable):
    """Raised when the Ollama package or service is unavailable."""


try:
    from ollama import Client
except ImportError:  # pragma: no cover - environment-dependent
    Client = None


class OllamaService:
    """Provide model discovery and cancellable text editing via local Ollama."""

    display_name = "Ollama"
    backend_id = "ollama"

    def __init__(self, client=None, max_tokens=DEFAULT_MAX_TOKENS):
        self.client = client
        self.max_tokens = max_tokens
        self.last_usage = None  # UsageRecord of the last completed request, when reported
        if self.client is None and Client is not None:
            self.client = Client()

    @property
    def package_available(self):
        """Return whether the Python client is installed."""
        return Client is not None or self.client is not None

    def connection_summary(self):
        """Return a short label describing the last successful connection."""
        return "Connected"

    def no_models_hint(self):
        """Return guidance shown when the model list is empty."""
        return "Install a local Ollama model, then refresh the list."

    def list_models(self):
        """Return sorted local model names or raise OllamaUnavailable."""
        if self.client is None:
            raise OllamaUnavailable(
                "The Ollama Python package is not installed. Run: pip install ollama"
            )
        try:
            response = self.client.list()
        except Exception as exc:
            raise OllamaUnavailable(
                "Cannot connect to Ollama. Ensure the Ollama service is running."
            ) from exc

        items = getattr(response, "models", None)
        if items is None and isinstance(response, dict):
            items = response.get("models", [])

        names = []
        for item in items or []:
            if isinstance(item, dict):
                name = item.get("model") or item.get("name")
            else:
                name = getattr(item, "model", None) or getattr(item, "name", None)
            if name:
                names.append(str(name))
        return sorted(set(names))

    @staticmethod
    def _field(response, name):
        """Read ``name`` from an Ollama response object or dict (None when absent)."""
        value = getattr(response, name, None)
        if value is None and isinstance(response, dict):
            value = response.get(name)
        return value

    def generate(self, model, messages, cancel_event, on_progress=None, max_tokens=None, response_format=None,
                 on_usage=None):
        """Stream one chat completion and return its text, honoring cancellation.

        ``response_format`` (a JSON schema dict) is passed as Ollama's ``format``
        so the answer is constrained to that schema. ``on_usage`` receives a
        ``UsageRecord`` built from ``prompt_eval_count``/``eval_count`` of the
        final message when Ollama reports them.
        """
        if self.client is None:
            raise OllamaUnavailable("The Ollama Python package is not installed.")
        if cancel_event.is_set():
            raise EditCancelled("Editing was cancelled.")

        request = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {
                "num_predict": max_tokens or self.max_tokens,
                "temperature": TEMPERATURE,
                "top_p": TOP_P,
            },
        }
        if response_format is not None:
            request["format"] = response_format
        done_reason = None
        counts = None  # (prompt_eval_count, eval_count) from the final message
        started = time.time()
        try:
            stream = self.client.chat(**request)
            chunks = []
            received = 0
            for response in stream:
                if cancel_event.is_set():
                    close = getattr(stream, "close", None)
                    if close:
                        close()
                    raise EditCancelled("Editing was cancelled.")
                message = self._field(response, "message")
                if message is None:
                    message = {}
                content = getattr(message, "content", None)
                if content is None and isinstance(message, dict):
                    content = message.get("content")
                if content:
                    chunks.append(content)
                    received += len(content)
                    if on_progress:
                        on_progress(received)
                reason = self._field(response, "done_reason")
                if reason:
                    done_reason = reason
                prompt_count = self._field(response, "prompt_eval_count")
                eval_count = self._field(response, "eval_count")
                if prompt_count is not None or eval_count is not None:
                    try:
                        counts = (int(prompt_count or 0), int(eval_count or 0))
                    except (TypeError, ValueError):
                        pass
        except EditCancelled:
            raise
        except Exception as exc:
            if cancel_event.is_set():
                raise EditCancelled("Editing was cancelled.")
            raise OllamaUnavailable("Ollama could not complete the edit: {0}".format(exc)) from exc

        if counts is not None:
            self.last_usage = UsageRecord(counts[0], counts[1], time.time() - started, model)
            if on_usage:
                on_usage(self.last_usage)
        if done_reason == "length":
            raise OutputTruncated(truncated_message(model))
        return strip_thinking("".join(chunks))

    def stream_edit(self, model, instruction, text, cancel_event, on_progress=None, text_first=False,
                    on_usage=None):
        """Return an edited document while honoring a cancellation event.

        ``on_progress`` (optional) receives the number of characters received
        so far and is called from the worker thread. ``text_first`` is passed
        to ``build_messages`` (prefix-cache friendly ordering).
        """
        result = self.generate(
            model, build_messages(instruction, text, text_first), cancel_event, on_progress=on_progress,
            on_usage=on_usage,
        )
        if not result.strip():
            raise OllamaUnavailable("Ollama returned an empty response.")
        return result
