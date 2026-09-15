"""Persisted application settings (backend choice, relay profiles, last models, UI language).

Settings live in ``TextEnhanceAI-settings.json`` in the per-user data
directory (``core.paths.user_data_dir``: ``%APPDATA%\\TextEnhanceAI``,
``~/Library/Application Support/TextEnhanceAI`` or
``~/.config/TextEnhanceAI``; ``TEAI_DATA_DIR`` overrides), the same place
the scratchpads go. Environment variables
seed a value when the file has none, so ``TEAI_REMOTE_URL``, ``TEAI_LANG`` and
friends still work for scripted setups, but anything saved from the Connection dialog wins.

Relay connections are *profiles* (``remote_profiles``: name, url, api_key,
max_tokens, enable_thinking) with one of them ``active_profile``. The flat
``remote_url`` / ``remote_api_key`` / ``remote_max_tokens`` /
``remote_enable_thinking`` attributes read and write the active profile, so
callers written for a single relay keep working; the file also carries them
for the active profile so an older app version still finds its settings. A
file with only the flat fields (or only the environment variables) becomes a
single profile called "Default".

API keys: with ``use_keyring`` on and the optional ``keyring`` package
installed, ``save()`` puts every profile's key into the OS keyring (see
``core.secrets``) and writes the placeholder ``@keyring`` instead; ``load()``
resolves the placeholder again. Otherwise the keys are stored in plain text
in the file - keep it private, or rely on the ``TEAI_REMOTE_API_KEY``
environment variable, which also fills in the active profile's key when the
file only holds a placeholder (scripted setups without a keyring).
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import secrets
from .paths import SETTINGS_FILENAME, user_data_dir
from .prompts import validate_chains, validate_custom_modes

BACKEND_OLLAMA = "ollama"
BACKEND_REMOTE = "remote"
BACKENDS = (BACKEND_OLLAMA, BACKEND_REMOTE)
BACKEND_LABELS = {
    BACKEND_OLLAMA: "Local Ollama",
    BACKEND_REMOTE: "Remote GPU (relay)",
}
DEFAULT_OLLAMA_MODEL = "llama3.1:8b"
DEFAULT_PROFILE_NAME = "Default"
DEFAULT_REMOTE_MAX_TOKENS = 4096
PROFILE_NAME_MAX = 40
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


def normalise_profile_name(name):
    """Collapse whitespace and cap the length; "" when nothing is left."""
    return " ".join(str(name or "").split())[:PROFILE_NAME_MAX]


def make_profile(name, url="", api_key="", max_tokens=DEFAULT_REMOTE_MAX_TOKENS, enable_thinking=False):
    """Return a normalised profile dict (the shape stored in ``remote_profiles``)."""
    return {
        "name": normalise_profile_name(name) or DEFAULT_PROFILE_NAME,
        "url": str(url or "").strip(),
        "api_key": str(api_key or "").strip(),
        "max_tokens": max(256, _env_int(str(max_tokens), DEFAULT_REMOTE_MAX_TOKENS)),
        "enable_thinking": bool(enable_thinking),
    }


def _profile_from_dict(data):
    if not isinstance(data, dict):
        return None
    name = normalise_profile_name(data.get("name"))
    if not name:
        return None
    return make_profile(
        name, data.get("url"), data.get("api_key"),
        data.get("max_tokens", DEFAULT_REMOTE_MAX_TOKENS), data.get("enable_thinking", False),
    )


def remote_model_key(profile_name):
    """The ``models`` key under which the last model of a relay profile is remembered."""
    return "{0}:{1}".format(BACKEND_REMOTE, profile_name)


@dataclass
class AppSettings:
    """User-editable configuration with JSON persistence."""

    backend: str = BACKEND_OLLAMA
    models: dict = field(default_factory=dict)  # backend id or "remote:<profile>" -> last model
    remote_profiles: list = field(default_factory=list)  # [make_profile(...)], never empty after load()
    active_profile: str = DEFAULT_PROFILE_NAME
    ui_language: str = UI_LANGUAGE_AUTO
    default_style_guide: str = ""  # pre-fills "Author's instructions" for new review projects
    default_glossary: list = field(default_factory=list)  # pre-fills "Protected terms"
    default_evaluation_mode: str = "combined"  # "combined" or "separate", see core.workflow
    custom_modes: list = field(default_factory=list)  # [{"name", "instruction"}], see core.prompts
    chains: list = field(default_factory=list)  # [{"name", "steps"}], steps are built-in or custom mode names
    recent_files: list = field(default_factory=list)  # quick-editor files, most recent first
    quick_explanations: bool = False  # ask the model to explain each change in the quick editor (one extra request)
    use_keyring: bool = False  # keep relay keys in the OS keyring instead of the settings file
    path: Path = field(default=None, repr=False, compare=False)
    load_error: str = field(default="", repr=False, compare=False)
    # profile name -> key known to be in the keyring (so save() only writes what changed)
    _stored_keys: dict = field(default_factory=dict, repr=False, compare=False)
    # profiles whose "@keyring" placeholder could not be resolved (package missing): save() keeps it
    _unresolved_keys: set = field(default_factory=set, repr=False, compare=False)
    # profiles whose key came from TEAI_REMOTE_API_KEY instead of the keyring: save() keeps the placeholder
    _env_keys: set = field(default_factory=set, repr=False, compare=False)

    _PERSISTED = (
        "backend",
        "models",
        "remote_profiles",
        "active_profile",
        "use_keyring",
        "ui_language",
        "default_style_guide",
        "default_glossary",
        "default_evaluation_mode",
        "custom_modes",
        "chains",
        "recent_files",
        "quick_explanations",
    )
    # written for the active profile so older versions (one relay) keep working
    _FLAT_REMOTE = ("remote_url", "remote_api_key", "remote_max_tokens", "remote_enable_thinking")

    def __post_init__(self):
        if not self.remote_profiles:
            self.remote_profiles = [make_profile(DEFAULT_PROFILE_NAME)]
        if self.find_profile(self.active_profile) is None:
            self.active_profile = self.remote_profiles[0]["name"]

    # ------------------------------------------------------------ persistence
    @classmethod
    def default_path(cls, environ=None):
        """The settings file in the per-user data directory (``TEAI_DATA_DIR`` overrides)."""
        return user_data_dir(environ) / SETTINGS_FILENAME

    @classmethod
    def load(cls, path=None, environ=None):
        """Read settings from ``path`` (default: ``default_path()``); missing/corrupt files fall back safely."""
        environ = os.environ if environ is None else environ
        settings = cls(path=Path(path) if path is not None else cls.default_path(environ))
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
        settings.remote_profiles, settings.active_profile = cls._load_profiles(data, environ)
        settings._resolve_keyring(data, environ)
        models = data.get("models")
        settings.models = {
            str(key): str(value)
            for key, value in (models or {}).items()
            if value and (key in BACKENDS or str(key).startswith(BACKEND_REMOTE + ":"))
        }
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
        if "quick_explanations" in data:
            settings.quick_explanations = bool(data["quick_explanations"])
        else:
            settings.quick_explanations = _env_bool(environ.get("TEAI_QUICK_EXPLAIN"), False)
        recent = data.get("recent_files")
        settings.recent_files = []
        for entry in (recent if isinstance(recent, list) else []):
            entry = str(entry or "").strip()
            if entry and entry not in settings.recent_files and Path(entry).is_file():
                settings.recent_files.append(entry)  # files that vanished are pruned
        settings.recent_files = settings.recent_files[:RECENT_FILES_LIMIT]

        env_model = environ.get("TEAI_MODEL", "").strip()
        if env_model and not settings.preferred_model(settings.backend):
            settings.remember_model(settings.backend, env_model)
        settings.models.setdefault(BACKEND_OLLAMA, DEFAULT_OLLAMA_MODEL)
        return settings

    @classmethod
    def _load_profiles(cls, data, environ):
        """Return ``(profiles, active name)`` from the file, migrating flat fields or the environment."""
        profiles = []
        seen = set()
        raw = data.get("remote_profiles")
        for item in (raw if isinstance(raw, list) else []):
            profile = _profile_from_dict(item)
            if profile is None or profile["name"].lower() in seen:
                continue  # damaged entries and duplicate names are dropped
            seen.add(profile["name"].lower())
            profiles.append(profile)
        if not profiles:
            # a file written by a single-relay version, or a fresh start seeded by the environment
            url = str(data.get("remote_url") or environ.get("TEAI_REMOTE_URL", "") or "").strip()
            key = str(data.get("remote_api_key") or environ.get("TEAI_REMOTE_API_KEY", "") or "").strip()
            if "remote_max_tokens" in data:
                max_tokens = _env_int(str(data["remote_max_tokens"]), DEFAULT_REMOTE_MAX_TOKENS)
            else:
                max_tokens = _env_int(environ.get("TEAI_REMOTE_MAX_TOKENS"), DEFAULT_REMOTE_MAX_TOKENS)
            if "remote_enable_thinking" in data:
                thinking = bool(data["remote_enable_thinking"])
            else:
                thinking = _env_bool(environ.get("TEAI_REMOTE_THINKING"), False)
            profiles = [make_profile(DEFAULT_PROFILE_NAME, url, key, max_tokens, thinking)]
        active = normalise_profile_name(data.get("active_profile"))
        if not any(profile["name"] == active for profile in profiles):
            active = profiles[0]["name"]
        return profiles, active

    def _resolve_keyring(self, data, environ):
        """Replace "@keyring" placeholders by the stored keys and decide ``use_keyring``."""
        placeholders = [p for p in self.remote_profiles if p["api_key"] == secrets.KEYRING_PLACEHOLDER]
        if "use_keyring" in data:
            self.use_keyring = bool(data["use_keyring"])
        else:
            self.use_keyring = bool(placeholders) or _env_bool(environ.get("TEAI_USE_KEYRING"), False)
        if not placeholders:
            return
        available = secrets.keyring_available()
        env_key = str(environ.get("TEAI_REMOTE_API_KEY", "") or "").strip()
        missing = []
        for profile in placeholders:
            name = profile["name"]
            if env_key and name == self.active_profile:
                profile["api_key"] = env_key  # scripted setups: the environment wins over the keyring
                self._env_keys.add(name)  # ...but the stored key is left alone
                continue
            key = secrets.load_key(name) if available else None
            if key:
                profile["api_key"] = key
                self._stored_keys[name] = key
            else:
                profile["api_key"] = ""
                self._unresolved_keys.add(name)
                missing.append(name)
        if missing:
            if available:
                note = "No relay key is stored in the system keyring for profile{0} {1}; enter it again.".format(
                    "" if len(missing) == 1 else "s", ", ".join(missing)
                )
            else:
                note = (
                    "The relay key of profile{0} {1} is stored in the system keyring, but the keyring package "
                    "is not installed ({2})."
                ).format("" if len(missing) == 1 else "s", ", ".join(missing), secrets.INSTALL_HINT)
            self.load_error = (self.load_error + " " + note).strip()

    @property
    def keyring_in_use(self):
        """Whether save() will put the keys into the OS keyring rather than the file."""
        return self.use_keyring and secrets.keyring_available()

    def _file_view(self):
        """The persisted dict with keys moved to or removed from the keyring as configured."""
        data = self.to_dict()
        profiles = data["remote_profiles"]
        if self.keyring_in_use:
            for profile in profiles:
                name, key = profile["name"], profile["api_key"]
                if name in self._env_keys:
                    profile["api_key"] = secrets.KEYRING_PLACEHOLDER  # the environment's key is never stored
                elif key:
                    if self._stored_keys.get(name) != key and not secrets.store_key(name, key):
                        continue  # the store refused: this key stays in the file
                    self._stored_keys[name] = key
                    self._unresolved_keys.discard(name)
                    profile["api_key"] = secrets.KEYRING_PLACEHOLDER
                elif name in self._unresolved_keys:
                    profile["api_key"] = secrets.KEYRING_PLACEHOLDER  # never overwrite an unread key
                elif name in self._stored_keys:
                    secrets.delete_key(name)
                    del self._stored_keys[name]
            for name in list(self._stored_keys):  # renamed or deleted profiles
                if self.find_profile(name) is None:
                    secrets.delete_key(name)
                    del self._stored_keys[name]
        else:
            if self._stored_keys and secrets.keyring_available():
                for name in list(self._stored_keys):  # keyring -> file: drop the other copy
                    secrets.delete_key(name)
                self._stored_keys = {}
            for profile in profiles:
                if profile["name"] in self._env_keys or (
                    not profile["api_key"] and profile["name"] in self._unresolved_keys
                ):
                    profile["api_key"] = secrets.KEYRING_PLACEHOLDER
        for profile in profiles:
            if profile["name"] == self.active_profile:
                data["remote_api_key"] = profile["api_key"]  # the flat mirror never leaks a keyring key
        return data

    def to_dict(self):
        """Return only the persisted fields plus the active profile's flat fields (keys in clear)."""
        data = {}
        for key in self._PERSISTED:
            value = getattr(self, key)
            if key == "remote_profiles":
                value = [dict(profile) for profile in value]
            elif isinstance(value, dict):
                value = dict(value)
            elif isinstance(value, list):
                value = [dict(item) if isinstance(item, dict) else item for item in value]
            data[key] = value
        for key in self._FLAT_REMOTE:
            data[key] = getattr(self, key)
        return data

    def save(self):
        """Write the settings file (keys go to the keyring when configured); failures are reported, never raised."""
        if self.path is None:
            return None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._file_view(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return "Settings could not be saved: {0}".format(exc)
        return None

    # -------------------------------------------------------------- profiles
    def profile_names(self):
        return [profile["name"] for profile in self.remote_profiles]

    def find_profile(self, name):
        """The profile dict called ``name`` (case-insensitive), or None."""
        wanted = normalise_profile_name(name).lower()
        for profile in self.remote_profiles:
            if profile["name"].lower() == wanted:
                return profile
        return None

    @property
    def profile(self):
        """The active profile dict (always exists)."""
        found = self.find_profile(self.active_profile)
        if found is None:
            if not self.remote_profiles:
                self.remote_profiles = [make_profile(DEFAULT_PROFILE_NAME)]
            found = self.remote_profiles[0]
            self.active_profile = found["name"]
        return found

    def set_active_profile(self, name):
        """Make ``name`` the active profile; returns False when no such profile exists."""
        found = self.find_profile(name)
        if found is None:
            return False
        self.active_profile = found["name"]
        return True

    def add_profile(self, name, **fields):
        """Append a new profile; returns it, or None when the name is empty or taken."""
        name = normalise_profile_name(name)
        if not name or self.find_profile(name) is not None:
            return None
        profile = make_profile(name, **fields)
        self.remote_profiles.append(profile)
        return profile

    def rename_profile(self, old, new):
        """Rename a profile (model memory follows); returns False when ``new`` is empty or taken."""
        profile = self.find_profile(old)
        new = normalise_profile_name(new)
        if profile is None or not new:
            return False
        clash = self.find_profile(new)
        if clash is not None and clash is not profile:
            return False
        previous = profile["name"]
        profile["name"] = new
        if self.active_profile == previous:
            self.active_profile = new
        remembered = self.models.pop(remote_model_key(previous), None)
        if remembered:
            self.models[remote_model_key(new)] = remembered
        return True

    def delete_profile(self, name):
        """Remove a profile; the active one falls back to the first. The last profile cannot be deleted."""
        profile = self.find_profile(name)
        if profile is None or len(self.remote_profiles) <= 1:
            return False
        self.remote_profiles.remove(profile)
        self.models.pop(remote_model_key(profile["name"]), None)
        if self.active_profile == profile["name"]:
            self.active_profile = self.remote_profiles[0]["name"]
        return True

    # --------------------------------------------- flat view of the active profile
    @property
    def remote_url(self):
        return self.profile["url"]

    @remote_url.setter
    def remote_url(self, value):
        self.profile["url"] = str(value or "").strip()

    @property
    def remote_api_key(self):
        return self.profile["api_key"]

    @remote_api_key.setter
    def remote_api_key(self, value):
        self.profile["api_key"] = str(value or "").strip()

    @property
    def remote_max_tokens(self):
        return self.profile["max_tokens"]

    @remote_max_tokens.setter
    def remote_max_tokens(self, value):
        self.profile["max_tokens"] = max(256, _env_int(str(value), DEFAULT_REMOTE_MAX_TOKENS))

    @property
    def remote_enable_thinking(self):
        return self.profile["enable_thinking"]

    @remote_enable_thinking.setter
    def remote_enable_thinking(self, value):
        self.profile["enable_thinking"] = bool(value)

    @property
    def remote_configured(self):
        """Return whether the active relay profile has an address."""
        return bool(self.remote_url)

    # --------------------------------------------------------------- helpers
    def _model_key(self, backend):
        backend = backend or self.backend
        return remote_model_key(self.active_profile) if backend == BACKEND_REMOTE else backend

    def preferred_model(self, backend=None):
        """Return the last model chosen for a backend ("" when unknown).

        For the remote backend the memory is per profile; a file from before
        profiles existed still has its choice under the plain ``remote`` key.
        """
        backend = backend or self.backend
        model = self.models.get(self._model_key(backend), "")
        if not model and backend == BACKEND_REMOTE:
            model = self.models.get(BACKEND_REMOTE, "")
        return model

    def remember_model(self, backend, model):
        """Store the model chosen for a backend (per profile for the remote backend)."""
        if model:
            self.models[self._model_key(backend)] = model

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
