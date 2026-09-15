"""Record and replay model answers for the automatic review pipeline.

A *cassette* is one JSON file per sample text and model in
``tests/recorded/<sample>.<model-slug>.json`` (the three separate checks) or
``tests/recorded/<sample>.<model-slug>.combined.json`` (the single combined
request of ``run_segment_combined``)::

    {"format": 1, "model": "...", "backend": "remote", "sample": "de_bahnhof",
     "recorded_at": "...", "requests": [{"key": "<sha256>", "check": "spelling",
     "max_tokens": null, "messages": [...], "response": "..."}, ...]}

``key`` is the SHA-256 of the canonical JSON of ``[model, messages,
max_tokens]`` exactly as ``run_check`` hands them to the backend (``max_tokens``
is ``None`` for edits, ``2048`` for explanations). Because ``stream_edit``
builds its messages with ``core.backend.build_messages`` (honouring
``text_first``, which the review checks use), the key matches what
``RemoteService`` and ``OllamaService`` send, so any prompt change makes the
lookup miss and the failure says "re-record" instead of silently passing.

``RecordingService`` wraps a live backend and captures every pair;
``scripts/record_responses.py`` drives it. ``RecordedService`` replays a
cassette offline and satisfies the backend contract from ``core/backend.py``.
"""

import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path

from core.backend import BackendUnavailable, EditCancelled, build_messages
from core.workflow import CHECK_INSTRUCTIONS, CHECK_LABELS

RECORDED_DIR = Path(__file__).resolve().parent / "recorded"
SAMPLES_DIR = RECORDED_DIR / "samples"
CASSETTE_FORMAT = 1
RERECORD_HINT = "run scripts/record_responses.py to re-record"

_EXPLANATION_HEAD = re.compile(r"^A (\w+) check proposed")


# ------------------------------------------------------------------ keys
def request_key(model, messages, max_tokens=None):
    """Return the cassette key for one request (sha256 of canonical JSON)."""
    canonical = json.dumps(
        [model, messages, max_tokens], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def model_slug(model):
    """Return a file-name-safe model id (``Qwen/Qwen3-32B`` -> ``qwen-qwen3-32b``)."""
    slug = re.sub(r"[^A-Za-z0-9.]+", "-", model or "").strip("-.").lower()
    return slug or "model"


COMBINED_SUFFIX = ".combined.json"


def cassette_path(sample, model, directory=RECORDED_DIR, combined=False):
    suffix = COMBINED_SUFFIX if combined else ".json"
    return Path(directory) / "{0}.{1}{2}".format(sample, model_slug(model), suffix)


def cassettes_for(sample, directory=RECORDED_DIR, combined=False):
    """Return the cassette files recorded for a sample name (separate or combined mode), sorted."""
    paths = Path(directory).glob("{0}.*.json".format(sample))
    return sorted(path for path in paths if path.name.endswith(COMBINED_SUFFIX) == combined)


def sample_names(directory=SAMPLES_DIR):
    return sorted(path.stem for path in Path(directory).glob("*.txt"))


def load_sample(sample, directory=SAMPLES_DIR):
    return (Path(directory) / (sample + ".txt")).read_text(encoding="utf-8")


def _user_content(messages):
    for message in messages:
        if message.get("role") == "user":
            return message.get("content", "")
    return ""


def split_edit_request(user_content):
    """Return ``(instruction, text)`` from an edit request in either order, or ``None``."""
    if user_content.startswith("Instruction:\n"):
        instruction, _, text = user_content[len("Instruction:\n"):].partition("\n\nText:\n")
        return instruction, text
    if user_content.startswith("Text:\n"):
        text, _, instruction = user_content[len("Text:\n"):].rpartition("\n\nInstruction:\n")
        return instruction, text
    return None


def check_name(messages):
    """Best-effort name of the check behind a request (for error messages and summaries)."""
    user = _user_content(messages)
    edit = split_edit_request(user)
    if edit is not None:
        for check, text in CHECK_INSTRUCTIONS.items():
            if text == edit[0]:
                return check
        return "edit"
    match = _EXPLANATION_HEAD.match(user)
    if match:
        for check, label in CHECK_LABELS.items():
            if label.lower() == match.group(1).lower():
                return check + "-explanation"
    if user.startswith("Review the text below"):
        return "combined"
    return "unknown"


# -------------------------------------------------------------- cassettes
def write_cassette(path, model, backend, entries, sample=""):
    """Write a cassette; ``entries`` are the dicts collected by ``RecordingService``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "format": CASSETTE_FORMAT,
        "model": model,
        "backend": backend,
        "sample": sample,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "requests": list(entries),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def load_cassette(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "requests" not in data:
        raise ValueError("{0} is not a cassette (no 'requests' list).".format(path))
    if data.get("format", CASSETTE_FORMAT) > CASSETTE_FORMAT:
        raise ValueError(
            "{0} was recorded by a newer harness; update tests/recorded_service.py.".format(path)
        )
    return data


class UnrecordedRequest(KeyError):
    """A request has no answer in the cassette; the prompt probably changed."""

    def __str__(self):
        return self.args[0] if self.args else "unrecorded request"


# ------------------------------------------------------------------ replay
class RecordedService:
    """Answer edit and explanation requests from a cassette (backend contract)."""

    display_name = "recorded model"
    backend_id = "recorded"

    def __init__(self, cassette):
        if isinstance(cassette, (str, Path)):
            self.path = Path(cassette)
            data = load_cassette(self.path)
        else:
            self.path = None
            data = cassette
        self.model = data.get("model", "")
        self.backend = data.get("backend", "")
        self.sample = data.get("sample", "")
        self.requests = {entry["key"]: entry for entry in data.get("requests", [])}
        self.calls = []  # (check, key) in call order; a repeated key means a retry
        self._lock = threading.Lock()

    # -- discovery
    def list_models(self):
        return [self.model] if self.model else []

    def connection_summary(self):
        return "Replaying {0} recorded request(s)".format(len(self.requests))

    def no_models_hint(self):
        return "The cassette names no model; {0}.".format(RERECORD_HINT)

    # -- generation
    def _miss_message(self, model, messages):
        preview = _user_content(messages).replace("\n", " ")[:80]
        where = self.path.name if self.path else "cassette"
        return 'No recorded answer for the {0} request "{1}" (model {2!r}, {3}); {4}.'.format(
            check_name(messages), preview, model, where, RERECORD_HINT
        )

    def generate(self, model, messages, cancel_event, on_progress=None, max_tokens=None, response_format=None, on_usage=None, on_stream=None):
        if cancel_event.is_set():
            raise EditCancelled("Editing was cancelled.")
        key = request_key(model, messages, max_tokens)
        entry = self.requests.get(key)
        if entry is None:
            raise UnrecordedRequest(self._miss_message(model, messages))
        with self._lock:
            self.calls.append((check_name(messages), key))
        if entry.get("error"):
            raise BackendUnavailable(entry["error"])
        response = entry.get("response", "")
        if on_progress:
            on_progress(len(response))
        return response

    def stream_edit(self, model, instruction, text, cancel_event, on_progress=None, text_first=False, on_usage=None, on_stream=None):
        result = self.generate(
            model, build_messages(instruction, text, text_first), cancel_event, on_progress=on_progress,
            on_usage=on_usage,
        )
        if not result.strip():
            raise BackendUnavailable("The recorded model returned an empty response.")
        return result


# ------------------------------------------------------------------ record
class RecordingService:
    """Pass-through wrapper that captures every request/response pair of a live backend.

    A failed request is kept as an ``error`` entry until the same request
    succeeds (``run_check`` retries transient failures), so a cassette replays
    the outcome the pipeline actually saw.
    """

    def __init__(self, service):
        self.service = service
        self.display_name = getattr(service, "display_name", "recorded model")
        self.backend_id = getattr(service, "backend_id", "")
        self.entries = {}  # key -> entry, insertion ordered
        self._lock = threading.Lock()

    def list_models(self):
        return self.service.list_models()

    def connection_summary(self):
        return self.service.connection_summary()

    def no_models_hint(self):
        return self.service.no_models_hint()

    @staticmethod
    def _entry(model, messages, max_tokens, key, response_format=None):
        entry = {
            "key": key,
            "check": check_name(messages),
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if response_format is not None:
            entry["structured"] = True
        return entry

    def generate(self, model, messages, cancel_event, on_progress=None, max_tokens=None, response_format=None, on_usage=None, on_stream=None):
        key = request_key(model, messages, max_tokens)
        try:
            response = self.service.generate(
                model, messages, cancel_event, on_progress=on_progress, max_tokens=max_tokens,
                response_format=response_format, on_usage=on_usage, on_stream=on_stream,
            )
        except EditCancelled:
            raise
        except BackendUnavailable as exc:
            with self._lock:
                if key not in self.entries:
                    self.entries[key] = dict(
                        self._entry(model, messages, max_tokens, key, response_format), error=str(exc)
                    )
            raise
        with self._lock:
            self.entries[key] = dict(self._entry(model, messages, max_tokens, key, response_format), response=response)
        return response

    def stream_edit(self, model, instruction, text, cancel_event, on_progress=None, text_first=False, on_usage=None, on_stream=None):
        # Mirror the real backends: build the messages here so the pair is captured.
        result = self.generate(
            model, build_messages(instruction, text, text_first), cancel_event, on_progress=on_progress,
            on_usage=on_usage, on_stream=on_stream,
        )
        if not result.strip():
            raise BackendUnavailable("{0} returned an empty response.".format(self.display_name))
        return result
