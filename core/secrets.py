"""Relay API keys in the operating system's keyring (optional ``keyring`` package).

Every relay profile's key is stored under the service ``TextEnhanceAI`` with
the username ``relay:<profile name>``; the settings file then only holds the
placeholder ``@keyring``. Nothing here ever raises: when the package is
missing, the backend is a no-op one, or the OS store refuses, the functions
return ``None``/``False`` and the settings fall back to plain text.
"""

SERVICE_NAME = "TextEnhanceAI"
KEYRING_PLACEHOLDER = "@keyring"
USERNAME_PREFIX = "relay:"
INSTALL_HINT = "pip install keyring"

try:
    import keyring as _keyring
except ImportError:  # pragma: no cover - environment-dependent
    _keyring = None


def _username(profile):
    return USERNAME_PREFIX + str(profile)


def _backend_name(backend):
    return "{0}.{1}".format(type(backend).__module__, type(backend).__name__).lower()


def keyring_available():
    """Return whether a usable keyring backend is installed (never raises)."""
    if _keyring is None:
        return False
    try:
        backend = _keyring.get_keyring()
    except Exception:
        return False
    name = _backend_name(backend)
    # keyring falls back to a backend that raises on every call, or to a
    # chainer with nothing inside, when no OS store is usable.
    if "fail" in name or "null" in name:
        return False
    if "chainer" in name and not getattr(backend, "backends", None):
        return False
    return True


def store_key(profile, key):
    """Store ``key`` for ``profile``; returns True on success."""
    if _keyring is None or not key:
        return False
    try:
        _keyring.set_password(SERVICE_NAME, _username(profile), key)
    except Exception:
        return False
    return True


def load_key(profile):
    """Return the stored key for ``profile`` (None when missing or unavailable)."""
    if _keyring is None:
        return None
    try:
        return _keyring.get_password(SERVICE_NAME, _username(profile)) or None
    except Exception:
        return None


def delete_key(profile):
    """Forget the stored key for ``profile``; returns True when one was removed."""
    if _keyring is None:
        return False
    try:
        _keyring.delete_password(SERVICE_NAME, _username(profile))
    except Exception:  # includes PasswordDeleteError when nothing was stored
        return False
    return True
