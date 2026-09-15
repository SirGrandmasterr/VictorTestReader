"""Environment-driven agent configuration."""

import os
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


class ConfigError(ValueError):
    """Raised when the agent is misconfigured (refuses to start)."""


_TRUE = {"1", "true", "yes", "on"}


def relay_websocket_url(url):
    """Turn the relay's public URL into its agent WebSocket URL.

    ``https://relay.example.com`` -> ``wss://relay.example.com/agent/ws``.
    Explicit ``ws(s)://`` URLs with a path are used unchanged.
    """
    url = (url or "").strip()
    if not url:
        raise ConfigError("RELAY_URL is required, e.g. https://relay.example.com")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(parts.scheme)
    if scheme is None or not parts.netloc:
        raise ConfigError("RELAY_URL must be an http(s):// or ws(s):// URL, got {0!r}".format(url))
    path = parts.path.rstrip("/")
    if not path:
        path = "/agent/ws"
    elif not path.endswith("/agent/ws"):
        path = path + "/agent/ws"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


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
class AgentConfig:
    """All tunables; see remote/gpu-agent/.env.example for documentation."""

    relay_ws_url: str
    relay_key: str
    agent_name: str = ""
    vllm_base_url: str = "http://vllm:8000"
    vllm_api_key: str = ""
    max_concurrency: int = 4
    health_host: str = "0.0.0.0"
    health_port: int = 8090
    poll_interval_loading: float = 5.0
    poll_interval_ready: float = 30.0
    reconnect_max_delay: float = 30.0
    request_timeout: float = 900.0
    ws_heartbeat: float = 20.0
    max_ws_message_bytes: int = 32 * 1024 * 1024
    tls_verify: bool = True
    log_level: str = "INFO"
    status_interval: float = 10.0  # seconds between metrics scrapes / status frames
    drain_on_term: bool = True  # SIGTERM drains (finish in-flight requests) instead of stopping at once
    drain_timeout: float = 600.0  # seconds after which a drain aborts what is still in flight

    @classmethod
    def from_environ(cls, environ=None):
        environ = os.environ if environ is None else environ
        key = environ.get("RELAY_AGENT_KEY", "").strip()
        if not key:
            raise ConfigError("RELAY_AGENT_KEY is required (one of the relay's RELAY_AGENT_KEYS).")
        name = environ.get("AGENT_NAME", "").strip() or socket.gethostname()
        return cls(
            relay_ws_url=relay_websocket_url(environ.get("RELAY_URL")),
            relay_key=key,
            agent_name=name[:64],
            vllm_base_url=environ.get("VLLM_BASE_URL", "http://vllm:8000").strip().rstrip("/"),
            vllm_api_key=environ.get("VLLM_API_KEY", "").strip(),
            max_concurrency=_env_int(environ, "AGENT_MAX_CONCURRENCY", 4),
            health_host=environ.get("AGENT_HEALTH_HOST", "0.0.0.0"),
            health_port=_env_int(environ, "AGENT_HEALTH_PORT", 8090, minimum=0),
            poll_interval_loading=_env_float(environ, "AGENT_POLL_INTERVAL_LOADING", 5.0, minimum=1.0),
            poll_interval_ready=_env_float(environ, "AGENT_POLL_INTERVAL_READY", 30.0, minimum=1.0),
            reconnect_max_delay=_env_float(environ, "AGENT_RECONNECT_MAX_DELAY", 30.0, minimum=1.0),
            request_timeout=_env_float(environ, "AGENT_REQUEST_TIMEOUT", 900.0, minimum=10.0),
            ws_heartbeat=_env_float(environ, "AGENT_WS_HEARTBEAT", 20.0, minimum=5.0),
            max_ws_message_bytes=_env_int(environ, "AGENT_MAX_WS_MESSAGE_MB", 32) * 1024 * 1024,
            tls_verify=environ.get("AGENT_TLS_VERIFY", "true").strip().lower() in _TRUE,
            log_level=environ.get("AGENT_LOG_LEVEL", "INFO").upper(),
            status_interval=_env_float(environ, "AGENT_STATUS_INTERVAL", 10.0, minimum=1.0),
            drain_on_term=environ.get("AGENT_DRAIN_ON_TERM", "true").strip().lower() in _TRUE,
            drain_timeout=_env_float(environ, "AGENT_DRAIN_TIMEOUT", 600.0, minimum=1.0),
        )
