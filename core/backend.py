"""Shared contract for text-editing backends (local Ollama or a remote relay).

Every backend exposes the same small surface so the UI does not need to know
where the model runs:

- ``display_name``: short human label used in status messages.
- ``list_models()``: sorted model names, or raise ``BackendUnavailable``.
- ``connection_summary()``: one-line connection state after ``list_models``.
- ``no_models_hint()``: what the user should do when no model is listed.
- ``stream_edit(model, instruction, text, cancel_event, on_progress=None,
  text_first=False)``: return the edited document or raise
  ``EditCancelled``/``BackendUnavailable``. ``text_first`` puts the text before
  the instruction so consecutive requests on the same segment share a prompt
  prefix (see ``build_messages``).
- ``generate(model, messages, cancel_event, on_progress=None, max_tokens=None,
  response_format=None, on_usage=None)``: one raw chat completion.
  ``response_format`` is a JSON schema dict the answer must follow; a server
  that cannot honour it raises ``StructuredOutputUnsupported``. ``on_usage``
  (also accepted by ``stream_edit``) receives one ``UsageRecord`` per request
  when the server reports token counts; it is called from the worker thread.
  A backend also keeps the last record as ``last_usage``.
"""

import re
from dataclasses import dataclass


class BackendUnavailable(RuntimeError):
    """Raised when a backend cannot be reached or cannot complete a request."""


class EditCancelled(RuntimeError):
    """Raised when the user cancels an active generation."""


class OutputTruncated(BackendUnavailable):
    """Raised when the model stopped because the output token limit was hit."""


class StructuredOutputUnsupported(BackendUnavailable):
    """Raised when the server rejects a ``response_format`` JSON schema request."""


SYSTEM_PROMPT = (
    "You are a careful text editor. Apply only the requested edits. Preserve "
    "the original language, meaning, paragraphs, line breaks, quotations, and "
    "formatting unless the instruction explicitly requires changing them. Make "
    "the smallest necessary changes. Return only the complete edited text, "
    "without commentary, labels, or Markdown fences."
)

# Appended to the system prompt when the user message starts with the text.
TEXT_FIRST_NOTE = "The instruction follows the text."

# Appended to the system prompt when the user message starts with the text.
TEXT_FIRST_NOTE = "The instruction follows the text."

# Appended to the system prompt when the user message starts with the text.
TEXT_FIRST_NOTE = "The instruction follows the text."

# Deterministic, conservative sampling shared by every backend.
DEFAULT_MAX_TOKENS = 4096
TEMPERATURE = 0.1
TOP_P = 0.9

_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_OPEN_THINK = re.compile(r"^\s*<think>.*", re.DOTALL)


def build_messages(instruction, text, text_first=False):
    """Return the chat messages used for one editing request.

    The default order (instruction, then text) is what the quick editor sends.
    With ``text_first`` the user message starts with the text and the system
    prompt says so: the automatic review runs several checks on the same
    segment back to back, and with the text in front every request shares the
    same token prefix, which vLLM's prefix cache can reuse instead of
    re-encoding the segment for each check.
    """
    if text_first:
        system = SYSTEM_PROMPT + " " + TEXT_FIRST_NOTE
        content = "Text:\n{0}\n\nInstruction:\n{1}".format(text, instruction)
    else:
        system = SYSTEM_PROMPT
        content = "Instruction:\n{0}\n\nText:\n{1}".format(instruction, text)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]


def strip_thinking(text):
    """Remove ``<think>...</think>`` reasoning blocks some models emit."""
    if "<think>" not in text:
        return text
    cleaned = _THINK_BLOCK.sub("", text)
    if "</think>" not in cleaned and "<think>" in cleaned:
        # Unterminated block: the whole remainder is reasoning, keep nothing.
        cleaned = _OPEN_THINK.sub("", cleaned)
    return cleaned.lstrip("\n")


def truncated_message(model):
    """Return the user-facing explanation for a length-limited response."""
    return (
        "{0} stopped before finishing because the output token limit was "
        "reached. Edit a shorter passage or raise the output token limit."
    ).format(model)


# ------------------------------------------------------------------ usage
@dataclass
class UsageRecord:
    """Token counts of one request as reported by the server.

    ``seconds`` is the wall-clock time of the request measured by the client
    (from sending the request to the end of the stream).
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    model: str = ""

    @property
    def total_tokens(self):
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self):
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "seconds": self.seconds,
            "model": self.model,
        }


USAGE_KEYS = ("prompt_tokens", "completion_tokens", "requests", "seconds")


def empty_usage():
    """Return a fresh usage total: ``{"prompt_tokens", "completion_tokens", "requests", "seconds"}``."""
    return {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0, "seconds": 0.0}


def normalise_usage(data):
    """Return a usage total with every key present and numeric (tolerates old or damaged files)."""
    totals = empty_usage()
    if not isinstance(data, dict):
        return totals
    for key in USAGE_KEYS:
        try:
            value = data.get(key, 0) or 0
            totals[key] = float(value) if key == "seconds" else int(value)
        except (TypeError, ValueError):
            pass
    return totals


def add_usage(totals, record):
    """Add one ``UsageRecord`` to a usage total in place and return the total."""
    totals["prompt_tokens"] = totals.get("prompt_tokens", 0) + int(record.prompt_tokens or 0)
    totals["completion_tokens"] = totals.get("completion_tokens", 0) + int(record.completion_tokens or 0)
    totals["requests"] = totals.get("requests", 0) + 1
    totals["seconds"] = totals.get("seconds", 0.0) + float(record.seconds or 0.0)
    return totals


def _compact_number(value):
    value = int(value)
    if value >= 1_000_000:
        return "{0:.1f}M".format(value / 1_000_000)
    if value >= 10_000:
        return "{0:.0f}k".format(value / 1000)
    if value >= 1_000:
        return "{0:.1f}k".format(value / 1000)
    return str(value)


def format_usage(totals, compact=True):
    """One-line summary of a usage total, e.g. ``12.3k tokens (10.1k in, 2.2k out) · 7 requests``.

    With ``compact=False`` the numbers are written out in full (report, tooltip).
    """
    totals = normalise_usage(totals)
    number = _compact_number if compact else (lambda value: "{0:,}".format(int(value)))
    total = totals["prompt_tokens"] + totals["completion_tokens"]
    parts = [
        "{0} tokens ({1} in, {2} out)".format(
            number(total), number(totals["prompt_tokens"]), number(totals["completion_tokens"])
        ),
        "{0} request{1}".format(totals["requests"], "" if totals["requests"] == 1 else "s"),
    ]
    if totals["seconds"] >= 1 and totals["completion_tokens"]:
        parts.append("{0:.0f} tok/s".format(totals["completion_tokens"] / totals["seconds"]))
    return " · ".join(parts)
