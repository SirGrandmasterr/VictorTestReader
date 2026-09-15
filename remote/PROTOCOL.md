# TextEnhanceAI relay protocol (version 1)

Three parties take part in a remote edit:

```
┌──────────────────┐  HTTPS (OpenAI-style)  ┌──────────────────┐  WSS (outbound only)  ┌──────────────────┐
│ TextEnhanceAI    │ ─────────────────────▶ │ Relay            │ ◀──────────────────── │ GPU agent        │
│ desktop client   │ ◀───── SSE stream ──── │ (Strato server)  │ ── request frames ──▶ │ (next to vLLM)   │
└──────────────────┘                        └──────────────────┘                       └───────┬──────────┘
                                                                                               │ HTTP (docker network)
                                                                                       ┌───────▼──────────┐
                                                                                       │ vLLM             │
                                                                                       └──────────────────┘
```

The GPU server can only open outbound connections, so the agent dials the
relay and keeps a WebSocket open. Clients never see the GPU server; they talk
to the relay with the standard OpenAI chat-completions API, which means any
OpenAI-compatible tool can use the relay, not just TextEnhanceAI.

## Client side (HTTP)

All client endpoints require `Authorization: Bearer <client key>`.

| Method | Path                   | Purpose                                                        |
|--------|------------------------|----------------------------------------------------------------|
| GET    | `/health`              | No auth. `{"ok": true, "agents": n, "ready_agents": m}`.        |
| GET    | `/v1/models`           | OpenAI list of models served by *ready* agents (`owned_by` = agent). |
| POST   | `/v1/chat/completions` | Forwarded verbatim to vLLM; streaming (SSE) or JSON.            |
| POST   | `/v1/completions`      | Same, legacy completion endpoint.                              |
| GET    | `/status`              | Agents, their state/models/load, relay counters.               |

Errors use the OpenAI shape: `{"error": {"message": "...", "type": "...", "code": 503}}`.
Relevant statuses: `401` bad key, `400` invalid body, `413` body too large,
`503` no ready agent serves the model / queue timeout, `504` agent idle timeout,
`502` agent failed or disconnected. The relay adds an `X-Request-ID` header.

While a streaming request waits for the GPU, the relay writes SSE comments
(`: keepalive`, `: queued`) every `RELAY_KEEPALIVE_INTERVAL` seconds so
proxies and clients keep the connection open. Comments are ignored by every
SSE parser. When an error occurs *after* streaming started, the relay emits
`data: {"error": {...}}` followed by `data: [DONE]`.

Closing the HTTP connection cancels the request: the relay sends a `cancel`
frame to the agent, which drops its HTTP connection to vLLM, and vLLM aborts
the generation.

### Admin endpoints

Available only when the relay runs with `RELAY_ADMIN_KEY`; they require
`Authorization: Bearer <admin key>` (checked in constant time, separate from
client and agent keys) and answer `404` while the key is unset, `401` on a
wrong key. Responses carry `Cache-Control: no-store`.

| Method | Path                  | Purpose                                                                 |
|--------|-----------------------|-------------------------------------------------------------------------|
| GET    | `/admin`              | HTML status page: agents, counters, key names, last 50 request outcomes. |
| GET    | `/admin/keys`         | `{"keys": [{"name", "kind", "source": "env"\|"file", "created"}]}` — never the keys. |
| POST   | `/admin/keys`         | Body `{"name": "...", "kind": "client"\|"agent"}`; `201 {"name", "kind", "key"}` — the key is returned exactly once. `409` when the name exists. |
| DELETE | `/admin/keys/{name}`  | Revokes the key at once: `{"name", "kind", "agents_closed"}`; `404` for unknown names. |

Keys created here are stored in `RELAY_KEYS_FILE` and merged with the
environment keys (`RELAY_AGENT_KEYS`/`RELAY_CLIENT_KEYS`); revoking an
environment key blocks it persistently. Revoking an agent key closes the
agent's socket (close code `4004`).

### Throughput fields in `/status`

Every entry of `agents` carries, besides `state`, `models`, `in_flight` and
`max_concurrency`:

| Field          | Meaning                                                                  |
|----------------|--------------------------------------------------------------------------|
| `queued`       | Requests waiting for a free slot on this agent right now.                |
| `chunks_per_s` | Streamed SSE chunks per second over the last 60 s (≈ tokens/s summed over all streams). |

The desktop app polls `/status` every 10 s while an automatic review runs and
shows `GPU ≈ N chunks/s · Q queued` in the progress line. Clients that ask
for `stream_options: {"include_usage": true}` receive vLLM's final chunk with
the `usage` object (`prompt_tokens`, `completion_tokens`) and an empty
`choices` list; the relay forwards it untouched.

## Agent side (WebSocket)

Endpoint: `GET /agent/ws` with `Authorization: Bearer <agent key>`. Every frame
is one JSON text message with a `type` field. The relay never inspects prompt
content beyond the fields it needs for routing (`model`, `stream`).

### Agent → relay

| type        | fields                                                                                   |
|-------------|------------------------------------------------------------------------------------------|
| `hello`     | `protocol` (1), `agent` (name), `agent_version`, `state`, `models`, `max_concurrency`, `meta` (may carry `metrics`) |
| `models`    | `state` (`loading`\|`ready`\|`unavailable`\|`draining`), `models` (list of `{id, max_model_len?}`) |
| `status`    | periodic (every `AGENT_STATUS_INTERVAL`, default 10 s) `{state, in_flight, metrics?}`      |
| `chunk`     | `request_id`, `data` — the raw JSON payload of one SSE `data:` line from vLLM              |
| `done`      | `request_id` — streaming finished                                                         |
| `response`  | `request_id`, `status`, `body` — full JSON answer for non-streaming requests               |
| `error`     | `request_id`, `status` (HTTP-like), `message`                                             |
| `cancelled` | `request_id` — acknowledgement of a cancel                                                |

The first frame must be `hello` (sent within 30 s of connecting). A second
connection with the same agent name replaces the first.

`metrics` (optional, protocol still version 1) is what the agent scraped from
vLLM's Prometheus `/metrics`: `num_requests_running`, `num_requests_waiting`,
`gpu_cache_usage_perc` (0–1), `generation_tokens_total` and `tokens_per_s`
(derived from the counter delta between two scrapes), plus `sampled_at`.
Missing metrics are simply absent; the relay stores the latest set and shows
it in `/status` (`agents[].metrics`) and on the admin page.

**Draining.** `POST /drain` on the agent's health server (or `SIGTERM` with
`AGENT_DRAIN_ON_TERM=1`, the default) switches the agent to state
`draining`: it sends a `status` frame, the relay stops routing to it and
drops its models from `/v1/models` while keeping the socket, new `request`
frames are answered with `error` 503, in-flight requests finish normally,
and once nothing is in flight the agent closes the socket (code 1001) and
exits 0. After `AGENT_DRAIN_TIMEOUT` (default 600 s) whatever is still in
flight is aborted with `error` 502.

### Relay → agent

| type      | fields                                                        |
|-----------|---------------------------------------------------------------|
| `welcome` | `protocol`, `relay_version`, `server_time`, `keepalive_interval` |
| `request` | `request_id`, `path` (`/v1/chat/completions` or `/v1/completions`), `body`, `client` (key name) |
| `cancel`  | `request_id`                                                  |

Keepalive uses WebSocket ping/pong (`RELAY_WS_HEARTBEAT`, default 20 s). If the
socket drops, every in-flight request on that agent fails with `502` and the
agent reconnects with exponential backoff (1 s → 30 s, with jitter).

## Prefix caching

The relay forwards requests verbatim, so whether the GPU can reuse work
between requests is decided on the client side. TextEnhanceAI's automatic
review evaluates each segment with several independent checks; it dispatches
those checks consecutively (queue order is chapter, segment, check) and builds
their edit requests with the segment text *before* the instruction (system
prompt "... The instruction follows the text.", user message
`Text:\n<segment>\n\nInstruction:\n<check instruction>`). Consecutive requests
therefore share one token prefix — system prompt plus segment — and vLLM
(`--enable-prefix-caching`, on in the GPU stack) serves it from cache; only
the short instruction and the answer differ per check. The quick editor keeps
the traditional instruction-first order, as does any other OpenAI-compatible
client; nothing in the protocol depends on the ordering. If you change the
review scheduling, keep the checks of one segment together and keep the
segment in front, or the cache hit rate drops to zero.

## Concurrency

Each agent announces `max_concurrency`. The relay picks the ready agent with
the fewest in-flight requests that serves the requested model, waits up to
`RELAY_QUEUE_TIMEOUT` for a free slot (sending `: queued` keepalives), then
dispatches. vLLM batches concurrent requests, so 4–8 is a good default on a
single GPU.
