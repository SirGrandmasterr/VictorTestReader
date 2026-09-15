"""Persisted application settings (backend choice, remote relay, last models, UI language).

Settings live in ``TextEnhanceAI-settings.json`` next to the application, the
same place the scratchpads go, and are ignored by Git. Environment variables
seed a value when the file has none, so ``TEAI_REMOTE_URL``, ``TEAI_LANG`` and
friends still work for scripted setups, but anything saved from the Connection dialog wins.

Note: the relay API key is stored in plain text in that file. Keep the file
private, or rely on the ``TEAI_REMOTE_API_KEY`` environment variable instead.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .prompts import validate_chains, validate_custom_modes

SETTINGS_FILENAME = "TextEnhanceAI-settings.json"
BACKEND_OLLAMA = "ollama"
BACKEND_REMOTE = "remote"
BACKENDS = (BACKEND_OLLAMA, BACKEND_REMOTE)
BACKEND_LABELS = {
    BACKEND_OLLAMA: "Local Ollama",
    BACKEND_REMOTE: "Remote GPU (relay)",
}
DEFAULT_OLLAMA_MODEL = "llama3.1:8b"
UI_LANGUAGE_AUTO = "auto"
UI_LANGUAGES = (UI_LANGUAGE_AUTO, "en", "de")  # "auto" follows the OS locale
RECENT_FILES_LIMIT = 10

_ENV_TRUE = {"1", "true", "yes", "on"}


def _env_bool(value, default):
    if value is None:
        return default
    return value.strip().lower() in _ENV_TRUE


def _env_int(value, default):
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


@dataclass
class AppSettings:
    """User-editable configuration with JSON persistence."""

    backend: str = BACKEND_OLLAMA
    models: dict = field(default_factory=dict)
    remote_url: str = ""
    remote_api_key: str = ""
    remote_max_tokens: int = 4096
    remote_enable_thinking: bool = False
    ui_language: str = UI_LANGUAGE_AUTO
    default_style_guide: str = ""  # pre-fills "Author's instructions" for new review projects
    default_glossary: list = field(default_factory=list)  # pre-fills "Protected terms"
    default_evaluation_mode: str = "combined"  # "combined" or "separate", see core.workflow
    custom_modes: list = field(default_factory=list)  # [{"name", "instruction"}], see core.prompts
    chains: list = field(default_factory=list)  # [{"name", "steps"}], steps are built-in or custom mode names
    recent_files: list = field(default_factory=list)  # quick-editor files, most recent first
    path: Path = field(default=None, repr=False, compare=False)
    load_error: str = field(default="", repr=False, compare=False)

    _PERSISTED = (
        "backend",
        "models",
        "remote_url",
        "remote_api_key",
        "remote_max_tokens",
        "remote_enable_thinking",
        "ui_language",
        "default_style_guide",
        "default_glossary",
        "default_evaluation_mode",
        "custom_modes",
        "chains",
        "recent_files",
    )

    # ------------------------------------------------------------ persistence
    @classmethod
    def load(cls, path, environ=None):
        """Read settings from ``path`` (missing/corrupt files fall back safely)."""
        environ = os.environ if environ is None else environ
        settings = cls(path=Path(path))
        data = {}
        if settings.path.exists():
            try:
                loaded = json.loads(settings.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
                else:
                    settings.load_error = "Settings file is not a JSON object; defaults used."
            except (OSError, ValueError) as exc:
                settings.load_error = "Settings file could not be read ({0}); defaults used.".format(exc)

        backend = data.get("backend") or environ.get("TEAI_BACKEND") or BACKEND_OLLAMA
        settings.backend = backend if backend in BACKENDS else BACKEND_OLLAMA
        models = data.get("models")
        settings.models = {
            str(key): str(value)
            for key, value in (models or {}).items()
            if key in BACKENDS and value
        }
        settings.remote_url = str(
            data.get("remote_url") or environ.get("TEAI_REMOTE_URL", "") or ""
        ).strip()
        settings.remote_api_key = str(
            data.get("remote_api_key") or environ.get("TEAI_REMOTE_API_KEY", "") or ""
        ).strip()
        if "remote_max_tokens" in data:
            settings.remote_max_tokens = _env_int(str(data["remote_max_tokens"]), 4096)
        else:
            settings.remote_max_tokens = _env_int(environ.get("TEAI_REMOTE_MAX_TOKENS"), 4096)
        if "remote_enable_thinking" in data:
            settings.remote_enable_thinking = bool(data["remote_enable_thinking"])
        else:
            settings.remote_enable_thinking = _env_bool(
                environ.get("TEAI_REMOTE_THINKING"), False
            )
        settings.remote_max_tokens = max(256, settings.remote_max_tokens)
        language = str(data.get("ui_language") or environ.get("TEAI_LANG", "") or "").strip().lower()
        settings.ui_language = language if language in UI_LANGUAGES else UI_LANGUAGE_AUTO
        settings.default_style_guide = str(data.get("default_style_guide") or "")
        glossary = data.get("default_glossary")
        if isinstance(glossary, str):
            glossary = glossary.splitlines()
        settings.default_glossary = [
            " ".join(str(term).split()) for term in (glossary or []) if str(term).strip()
        ] if isinstance(glossary, list) else []
        mode = str(data.get("default_evaluation_mode") or environ.get("TEAI_EVALUATION_MODE", "") or "").strip().lower()
        settings.default_evaluation_mode = mode if mode in ("combined", "separate") else "combined"
        settings.custom_modes, problems = validate_custom_modes(data.get("custom_modes"))
        settings.chains, chain_problems = validate_chains(data.get("chains"), settings.custom_modes)
        problems += chain_problems
        if problems:
            note = "Ignored {0} invalid custom mode/chain entr{1}: {2}.".format(
                len(problems), "y" if len(problems) == 1 else "ies", "; ".join(problems[:3])
            )
            settings.load_error = (settings.load_error + " " + note).strip()
        recent = data.get("recent_files")
        settings.recent_files = []
        for entry in (recent if isinstance(recent, list) else []):
            entry = str(entry or "").strip()
            if entry and entry not in settings.recent_files and Path(entry).is_file():
                settings.recent_files.append(entry)  # files that vanished are pruned
        settings.recent_files = settings.recent_files[:RECENT_FILES_LIMIT]

        env_model = environ.get("TEAI_MODEL", "").strip()
        if settings.backend not in settings.models and env_model:
            settings.models[settings.backend] = env_model
        settings.models.setdefault(BACKEND_OLLAMA, DEFAULT_OLLAMA_MODEL)
        return settings

    def to_dict(self):
        """Return only the persisted fields."""
        data = asdict(self)
        return {key: data[key] for key in self._PERSISTED}

    def save(self):
        """Write the settings file; failures are reported, never raised."""
        if self.path is None:
            return None
        try:
            self.path.write_text(
                json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return "Settings could not be saved: {0}".format(exc)
        return None

    # --------------------------------------------------------------- helpers
    def preferred_model(self, backend=None):
        """Return the last model chosen for a backend ("" when unknown)."""
        return self.models.get(backend or self.backend, "")

    def remember_model(self, backend, model):
        """Store the model chosen for a backend."""
        if model:
            self.models[backend] = model

    def remember_file(self, path):
        """Put ``path`` at the front of the recent-files list (at most RECENT_FILES_LIMIT entries)."""
        path = str(path)
        self.recent_files = [path] + [entry for entry in self.recent_files if entry != path]
        del self.recent_files[RECENT_FILES_LIMIT:]

    def forget_file(self, path):
        self.recent_files = [entry for entry in self.recent_files if entry != str(path)]

    def custom_mode_names(self):
        return [entry["name"] for entry in self.custom_modes]

    def chain_names(self):
        return [entry["name"] for entry in self.chains]

    def find_chain(self, name):
        """The step names of the chain called ``name`` (None when unknown)."""
        for entry in self.chains:
            if entry["name"] == name:
                return list(entry["steps"])
        return None

    @property
    def remote_configured(self):
        """Return whether the remote backend has an address."""
        return bool(self.remote_url)
