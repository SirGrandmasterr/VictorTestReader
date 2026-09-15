"""Thin async client for the local vLLM OpenAI-compatible server."""

import asyncio
import json
import logging
import math
import re

import aiohttp

log = logging.getLogger("teai_agent.vllm")

# Prometheus metrics read from vLLM's /metrics; matched by suffix so a renamed
# prefix (vllm:, vllm_, ...) or a future family name still works.
METRIC_SUFFIXES = (
    "num_requests_running",
    "num_requests_waiting",
    "gpu_cache_usage_perc",
    "generation_tokens_total",
)
_METRIC_LINE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eEinfNa]+)")


def parse_metrics(text, suffixes=METRIC_SUFFIXES):
    """Extract the wanted gauges/counters from a Prometheus text exposition.

    Returns ``{suffix: float}`` for every metric found; samples of the same
    family (several ``model_name`` labels) are summed. Missing metrics are
    simply absent, unparsable lines are skipped.
    """
    found = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _METRIC_LINE.match(line)
        if not match:
            continue
        name, _, value = match.groups()
        for suffix in suffixes:
            if name == suffix or name.endswith(":" + suffix) or name.endswith("_" + suffix):
                try:
                    number = float(value)
                except ValueError:
                    break
                if math.isfinite(number):  # Prometheus may emit NaN/+Inf; those are no data
                    found[suffix] = found.get(suffix, 0.0) + number
                break
    return found


class VLLMError(Exception):
    """A request to vLLM failed; carries an HTTP-like status for the relay."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _extract_error(status, text):
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error") or payload.get("detail") or payload.get("message")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str):
            return error
    text = (text or "").strip()
    return text[:500] if text else "vLLM returned HTTP {0}".format(status)


class VLLMClient:
    """Discover models and forward requests to vLLM."""

    def __init__(self, session, base_url, api_key="", request_timeout=900.0):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.request_timeout = request_timeout

    def _headers(self):
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    async def probe(self):
        """Return ``(reachable, models, meta)`` describing the server right now."""
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with self.session.get(
                self.base_url + "/v1/models", headers=self._headers(), timeout=timeout
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    return (
                        False,
                        [],
                        {"vllm_error": "HTTP {0}: {1}".format(response.status, _extract_error(response.status, text))},
                    )
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
            return False, [], {"vllm_error": str(exc) or exc.__class__.__name__}
        except Exception as exc:  # unexpected but never fatal for the monitor
            return False, [], {"vllm_error": repr(exc)}

        models = []
        for item in (payload or {}).get("data", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            entry = {"id": str(item["id"])}
            if isinstance(item.get("max_model_len"), int):
                entry["max_model_len"] = item["max_model_len"]
            models.append(entry)
        meta = {}
        try:
            async with self.session.get(self.base_url + "/version", timeout=timeout) as response:
                if response.status == 200:
                    version = await response.json(content_type=None)
                    if isinstance(version, dict) and version.get("version"):
                        meta["vllm_version"] = str(version["version"])
        except Exception:
            pass
        meta["vllm_error"] = "" if models else "vLLM lists no models yet"
        return True, models, meta

    async def metrics(self):
        """Return the parsed ``GET /metrics`` gauges (see ``parse_metrics``), or None when unreachable."""
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with self.session.get(self.base_url + "/metrics", headers=self._headers(), timeout=timeout) as response:
                if response.status != 200:
                    return None
                return parse_metrics(await response.text())
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            return None
        except Exception:  # never fatal for the monitor
            return None

    async def forward(self, path, body, on_chunk=None):
        """Forward one request. Streams SSE payloads to ``on_chunk`` or returns JSON.

        Returns ``(status, json_body)`` for non-streaming requests and
        ``(200, None)`` after a completed stream. Raises ``VLLMError`` on
        HTTP errors; ``asyncio.CancelledError`` propagates (and closes the
        connection, which makes vLLM abort the generation).
        """
        streaming = body.get("stream") is True and on_chunk is not None
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        if streaming:
            headers["Accept"] = "text/event-stream"
        timeout = aiohttp.ClientTimeout(total=self.request_timeout, sock_connect=10)
        try:
            async with self.session.post(
                self.base_url + path, json=body, headers=headers, timeout=timeout
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    raise VLLMError(response.status, _extract_error(response.status, text))
                if not streaming:
                    return response.status, await response.json(content_type=None)
                data_lines = []
                async for raw in response.content:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line:
                        if data_lines:
                            payload = "\n".join(data_lines)
                            data_lines = []
                            if payload.strip() == "[DONE]":
                                return 200, None
                            await on_chunk(payload)
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                if data_lines:
                    payload = "\n".join(data_lines)
                    if payload.strip() != "[DONE]":
                        await on_chunk(payload)
                return 200, None
        except aiohttp.ClientError as exc:
            raise VLLMError(502, "vLLM connection failed: {0}".format(exc or exc.__class__.__name__))
        except (OSError, TimeoutError, asyncio.TimeoutError) as exc:
            raise VLLMError(504, "vLLM request timed out or was interrupted: {0}".format(exc or exc.__class__.__name__))
