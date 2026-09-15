"""Environment-driven relay configuration."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

DEFAULT_KEYS_FILE = "/data/keys.json"


class ConfigError(ValueError):
    """Raised when the relay is misconfigured (refuses to start)."""


def parse_keys(raw, kind):
    """Parse ``name:key,name2:key2`` (or bare keys) into ``{key: name}``.

    Names only appear in logs and status output. Bare keys get generated
    names (``client-1``, ``client-2``...). Whitespace is ignored.
    """
    keys = {}
    counter = 0
    for entry in (raw or "").replace("\n", ",").split(","):
        entry = entry.strip()
        if not entry:
            continue
        counter += 1
        if ":" in entry:
            name, key = entry.split(":", 1)
            name, key = name.strip(), key.strip()
        else:
            name, key = "{0}-{1}".format(kind, counter), entry
        if len(key) < 16:
            raise ConfigError(
                "{0} key '{1}' is too short; use at least 16 characters "
                "(python -m teai_relay keygen)".format(kind, name)
            )
        if key in keys:
            raise ConfigError("{0} key for '{1}' duplicates '{2}'".format(kind, name, keys[key]))
        keys[key] = name or "{0}-{1}".format(kind, counter)
    return keys


def _env_int(environ, name, default, minimum=1):
    value = environ.get(name)
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except ValueError:
        raise ConfigError("{0} must be an integer, got {1!r}".format(name, value))
    if parsed < minimum:
        raise ConfigError("{0} must be >= {1}".format(name, minimum))
    return parsed


def _env_float(environ, name, default, minimum=0.0):
    value = environ.get(name)
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except ValueError:
        raise ConfigError("{0} must be a number, got {1!r}".format(name, value))
    if parsed < minimum:
        raise ConfigError("{0} must be >= {1}".format(name, minimum))
    return parsed


@dataclass
class RelayConfig:
    """All tunables; see remote/relay/.env.example for documentation."""

    host: str = "0.0.0.0"
    port: int = 8080
    agent_keys: Dict[str, str] = field(default_factory=dict)  # key -> name
    client_keys: Dict[str, str] = field(default_factory=dict)  # key -> name
    max_body_bytes: int = 16 * 1024 * 1024
    max_ws_message_bytes: int = 32 * 1024 * 1024
    queue_timeout: float = 300.0
    idle_timeout: float = 180.0
    keepalive_interval: float = 15.0
    ws_heartbeat: float = 20.0
    hello_timeout: float = 30.0
    log_level: str = "INFO"
    public_url: str = ""
    admin_key: str = ""  # empty: the /admin routes answer 404
    keys_file: str = DEFAULT_KEYS_FILE  # keys added through the admin API or the keys command

    @classmethod
    def from_environ(cls, environ=None):
        environ = os.environ if environ is None else environ
        admin_key = environ.get("RELAY_ADMIN_KEY", "").strip()
        if admin_key and len(admin_key) < 16:
            raise ConfigError("RELAY_ADMIN_KEY is too short; use at least 16 characters (python -m teai_relay keygen)")
        config = cls(
            host=environ.get("RELAY_HOST", "0.0.0.0"),
            port=_env_int(environ, "RELAY_PORT", 8080),
            agent_keys=parse_keys(environ.get("RELAY_AGENT_KEYS"), "agent"),
            client_keys=parse_keys(environ.get("RELAY_CLIENT_KEYS"), "client"),
            max_body_bytes=_env_int(environ, "RELAY_MAX_BODY_MB", 16) * 1024 * 1024,
            max_ws_message_bytes=_env_int(environ, "RELAY_MAX_WS_MESSAGE_MB", 32) * 1024 * 1024,
            queue_timeout=_env_float(environ, "RELAY_QUEUE_TIMEOUT", 300.0),
            idle_timeout=_env_float(environ, "RELAY_IDLE_TIMEOUT", 180.0, minimum=5.0),
            keepalive_interval=_env_float(environ, "RELAY_KEEPALIVE_INTERVAL", 15.0, minimum=1.0),
            ws_heartbeat=_env_float(environ, "RELAY_WS_HEARTBEAT", 20.0, minimum=5.0),
            hello_timeout=_env_float(environ, "RELAY_HELLO_TIMEOUT", 30.0, minimum=1.0),
            log_level=environ.get("RELAY_LOG_LEVEL", "INFO").upper(),
            public_url=environ.get("RELAY_PUBLIC_URL", "").strip(),
            admin_key=admin_key,
            keys_file=environ.get("RELAY_KEYS_FILE", "").strip() or DEFAULT_KEYS_FILE,
        )
        # Keys may also come from the keys file (or be created through the admin
        # API), so an empty environment is only an error when neither exists.
        can_add_later = bool(config.admin_key) or Path(config.keys_file).exists()
        if not config.agent_keys and not can_add_later:
            raise ConfigError(
                "RELAY_AGENT_KEYS is empty. Generate one with "
                "'python -m teai_relay keygen' and give it to the GPU agent."
            )
        if not config.client_keys and not can_add_later:
            raise ConfigError(
                "RELAY_CLIENT_KEYS is empty. Generate one with "
                "'python -m teai_relay keygen' for each TextEnhanceAI user."
            )
        shared = set(config.agent_keys) & set(config.client_keys)
        if shared:
            raise ConfigError("Agent and client keys must be different values.")
        if config.admin_key and (config.admin_key in config.agent_keys or config.admin_key in config.client_keys):
            raise ConfigError("RELAY_ADMIN_KEY must differ from every agent and client key.")
        return config
