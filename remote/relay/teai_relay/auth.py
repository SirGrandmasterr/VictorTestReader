"""Bearer-token authentication with constant-time comparison, plus the key store.

Keys come from two places: the environment (``RELAY_AGENT_KEYS`` /
``RELAY_CLIENT_KEYS``, fixed until restart) and a JSON file
(``RELAY_KEYS_FILE``) that the admin API and the ``keys`` command edit. The
``KeyStore`` merges both; file entries win over environment entries with the
same name, and names listed under ``revoked`` are dropped from either source
so a key from the environment can be revoked without a restart or an edit of
``.env``. Lookups always go through the store, so changes apply immediately.
"""

import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

KEY_PREFIX = "teai_"
KEY_KINDS = ("client", "agent")
KEYS_FILE_FORMAT = 1
MIN_KEY_LENGTH = 16


def generate_key(nbytes=32):
    """Return a new random key suitable for RELAY_AGENT_KEYS / RELAY_CLIENT_KEYS."""
    return KEY_PREFIX + secrets.token_urlsafe(nbytes)


def bearer_token(request):
    """Extract the bearer token from an aiohttp request (or ``None``)."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def lookup(token, keys):
    """Return the key's name when ``token`` matches one of ``keys`` (key -> name)."""
    if not token:
        return None
    match = None
    for key, name in keys.items():
        # Compare every key so timing does not reveal which prefix matched.
        if hmac.compare_digest(key.encode("utf-8"), token.encode("utf-8")):
            match = name
    return match


def token_matches(token, expected):
    """Constant-time comparison of a bearer token with one expected key."""
    if not token or not expected:
        return False
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


class KeyStoreError(ValueError):
    """A key operation was refused (duplicate or unknown name, bad kind)."""


def normalise_name(name):
    """Key names: trimmed, at most 64 characters, no whitespace or slashes."""
    name = str(name or "").strip()
    if not name or len(name) > 64 or any(ch.isspace() for ch in name) or "/" in name:
        raise KeyStoreError("A key name must be 1-64 characters without spaces or slashes.")
    return name


class KeyStore:
    """Merged view of environment keys and the keys file; the file is edited atomically."""

    def __init__(self, env_agent_keys=None, env_client_keys=None, path=None):
        self.path = Path(path) if path else None
        self.env = {"agent": dict(env_agent_keys or {}), "client": dict(env_client_keys or {})}  # key -> name
        self.file = {"agent": {}, "client": {}}  # name -> {"key", "created"}
        self.revoked = set()
        self.load_error = ""
        self._lock = threading.Lock()
        self.load()

    # -------------------------------------------------------------- file
    def load(self):
        """Read the keys file (missing file: empty; damaged file: ignored with ``load_error``)."""
        self.file = {"agent": {}, "client": {}}
        self.revoked = set()
        self.load_error = ""
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.load_error = "keys file {0} could not be read: {1}".format(self.path, exc)
            return
        if not isinstance(data, dict):
            self.load_error = "keys file {0} is not a JSON object".format(self.path)
            return
        for kind in KEY_KINDS:
            for entry in data.get(kind) or []:
                if not isinstance(entry, dict):
                    continue
                key = str(entry.get("key") or "")
                try:
                    name = normalise_name(entry.get("name"))
                except KeyStoreError:
                    continue
                if len(key) < MIN_KEY_LENGTH:
                    continue
                self.file[kind][name] = {"key": key, "created": str(entry.get("created") or "")}
        self.revoked = {str(name) for name in data.get("revoked") or [] if str(name).strip()}

    def _to_dict(self):
        return {
            "format": KEYS_FILE_FORMAT,
            "agent": [dict(name=name, **entry) for name, entry in sorted(self.file["agent"].items())],
            "client": [dict(name=name, **entry) for name, entry in sorted(self.file["client"].items())],
            "revoked": sorted(self.revoked),
        }

    def save(self):
        """Write the keys file atomically (temporary file + rename); no file configured: no-op."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(self._to_dict(), indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(str(temporary), 0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass
        os.replace(str(temporary), str(self.path))

    # ------------------------------------------------------------ queries
    def keys(self, kind):
        """Return ``{key: name}`` for ``kind`` with revocations applied; file entries win over env."""
        merged = {}
        file_names = {name for name in self.file[kind] if name not in self.revoked}
        for key, name in self.env[kind].items():
            if name in self.revoked or name in file_names:
                continue
            merged[key] = name
        for name, entry in self.file[kind].items():
            if name not in self.revoked:
                merged[entry["key"]] = name
        return merged

    @property
    def agent_keys(self):
        return self.keys("agent")

    @property
    def client_keys(self):
        return self.keys("client")

    def find(self, name):
        """Return ``(kind, source)`` of an active key name, or None."""
        for kind in KEY_KINDS:
            if name in self.file[kind] and name not in self.revoked:
                return kind, "file"
        for kind in KEY_KINDS:
            if name in self.env[kind].values() and name not in self.revoked:
                return kind, "env"
        return None

    def list(self):
        """Describe every active key without revealing it: name, kind, source, created."""
        rows = []
        for kind in KEY_KINDS:
            for name, entry in self.file[kind].items():
                if name not in self.revoked:
                    rows.append({"name": name, "kind": kind, "source": "file", "created": entry["created"]})
            for name in self.env[kind].values():
                if name not in self.revoked and name not in self.file[kind]:
                    rows.append({"name": name, "kind": kind, "source": "env", "created": ""})
        rows.sort(key=lambda row: (row["kind"], row["name"].lower()))
        return rows

    # ------------------------------------------------------------ changes
    def add(self, name, kind="client"):
        """Create a key for ``name``; returns the key (shown once). Persists the file."""
        name = normalise_name(name)
        if kind not in KEY_KINDS:
            raise KeyStoreError("kind must be 'client' or 'agent'")
        with self._lock:
            if self.find(name) is not None:
                raise KeyStoreError("A key named '{0}' already exists.".format(name))
            key = generate_key()
            self.file[kind][name] = {"key": key, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            self.revoked.discard(name)
            self.save()
        return key

    def revoke(self, name):
        """Revoke a key by name (file or environment); returns its kind, or None when unknown."""
        name = normalise_name(name)
        with self._lock:
            found = self.find(name)
            if found is None:
                return None
            kind, source = found
            if source == "file":
                del self.file[kind][name]
            if name in self.env[kind].values():
                self.revoked.add(name)  # environment keys can only be blocked, not deleted
            self.save()
        return kind
