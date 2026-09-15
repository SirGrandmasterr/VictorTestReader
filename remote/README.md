# Remote GPU models for TextEnhanceAI

This directory adds a second backend to the desktop app: instead of a local
Ollama model, TextEnhanceAI can use a large model (for example a Qwen 27B
checkpoint served by vLLM) that runs on a GPU server **which cannot be reached
from the internet**. A small relay on a public server (your Strato VPS) bridges
the two: the GPU server dials *out* to the relay and keeps a WebSocket open;
clients talk to the relay over HTTPS with the standard OpenAI chat API.

```
 your laptop                       Strato VPS (public)                 GPU server (NAT, outbound only)
┌────────────────────┐   HTTPS    ┌────────────────────┐   WSS (dialed   ┌─────────────────────────────┐
│ TextEnhanceAI      │ ─────────▶ │ relay  :443 Caddy  │ ◀── by agent) ─ │ agent  ──HTTP──▶  vLLM      │
│ Backend: Remote    │ ◀── SSE ── │        :8080 relay │ ──requests────▶ │ (docker compose, 2 services)│
└────────────────────┘            └────────────────────┘                 └─────────────────────────────┘
```

| Directory      | Runs on         | What it is                                                                 |
|----------------|-----------------|----------------------------------------------------------------------------|
| `relay/`       | Strato server   | `teai_relay` (Python/aiohttp) + Caddy for automatic HTTPS. Docker Compose.  |
| `gpu-agent/`   | GPU server      | vLLM (official image) + `teai_agent` that connects to the relay. Docker Compose. |
| `../core/remote_service.py` | client | The desktop backend (standard library only) that talks to the relay. |
| `PROTOCOL.md`  | —               | The relay ⇄ agent wire protocol and the client HTTP API.                    |

## What you get

- **Works through NAT/firewalls:** the GPU server never accepts inbound
  connections; only outbound HTTPS/WSS to the relay is required.
- **Standard API:** the relay is OpenAI-compatible (`/v1/models`,
  `/v1/chat/completions`, streaming SSE). Any OpenAI-style client can use it
  with `base_url=https://relay.example.com/v1` and a client key.
- **Separate keys for agents and clients**, per-person client keys, constant-
  time comparison, TLS via Let's Encrypt. Prompts and texts are **never logged**
  by the relay or the agent — only request ids, model names, timings and sizes.
- **Live status everywhere:** `GET /status` on the relay, `GET /health` on the
  agent, and the *Connection...* dialog in TextEnhanceAI show which agents are
  online, whether the model is still loading, and how busy the GPU is.
- **Cancellation end-to-end:** pressing *Cancel* in the editor closes the HTTP
  stream, the relay tells the agent, the agent drops its request, and vLLM
  aborts the generation immediately.
- **Robust connections:** the agent reconnects with exponential backoff, keeps
  WebSocket pings flowing, refreshes its model list if vLLM restarts, and
  registers with the relay *before* the model has finished loading so you can
  see progress from the client side.
- **Fair sharing:** the agent announces how many concurrent requests vLLM should
  take; the relay queues the rest (with keepalives so nothing times out) and
  balances across several GPU agents if you add more.
- **No new client dependencies:** the desktop app still needs only Python and
  (optionally) `ollama`; the relay and agent only need `aiohttp`.

## Setup in three steps

### 1. Relay on the public server

```bash
# on the Strato server (Docker + Docker Compose installed, ports 80/443 open)
git clone <this repo> && cd TextEnhanceAI/remote/relay
cp .env.example .env
python3 -m teai_relay keygen 3        # or: docker compose run --rm relay python -m teai_relay keygen 3
nano .env                              # RELAY_DOMAIN, RELAY_AGENT_KEYS, RELAY_CLIENT_KEYS
docker compose up -d --build
curl https://relay.example.com/health  # {"ok": true, "agents": 0, ...}
```

`RELAY_DOMAIN` must be a DNS name pointing at the server; Caddy fetches the
certificate automatically. Put one key per GPU server in `RELAY_AGENT_KEYS` and
one key per user in `RELAY_CLIENT_KEYS` (`name:key,name:key`). If the server
already runs nginx, use `docker-compose.nginx.yml` + `nginx-site.example.conf`
instead of Caddy. Details: [relay/README.md](relay/README.md).

### 2. vLLM + agent on the GPU server

```bash
# on the GPU server (NVIDIA driver, Docker with the NVIDIA container toolkit)
cd TextEnhanceAI/remote/gpu-agent
cp .env.example .env
nano .env        # RELAY_URL, RELAY_AGENT_KEY (one agent key from step 1), VLLM_MODEL, GPU settings
docker compose up -d --build
docker compose logs -f agent         # "connected to relay", later "vLLM ready ... models ['qwen-27b']"
curl -s http://127.0.0.1:8090/health # agent view: relay connection + vLLM state
```

The first start downloads the model weights into a Docker volume (or
`HF_CACHE_DIR`); loading a 27B model takes several minutes. Details and sizing
notes: [gpu-agent/README.md](gpu-agent/README.md).

### 3. TextEnhanceAI on the client

1. Start the app and click **Connection...**.
2. Choose **Remote GPU (relay)**, enter `https://relay.example.com` and your
   client key, press **Test connection**. The dialog lists the agents and models.
3. **Save**. The Backend box switches to *Remote GPU (relay)*, the model list
   shows the vLLM model, and the status bar reports the connection.
4. Edit as usual. The status bar shows elapsed time and characters received;
   **Cancel** stops the GPU immediately.

Settings persist in `TextEnhanceAI-settings.json` next to the app (Git-ignored).
Environment variables (`TEAI_BACKEND=remote`, `TEAI_REMOTE_URL`,
`TEAI_REMOTE_API_KEY`, `TEAI_REMOTE_MAX_TOKENS`, `TEAI_REMOTE_THINKING`) seed
the settings for scripted installs; values saved from the dialog take
precedence.

## Day-to-day operation

| Task                          | How                                                                 |
|-------------------------------|---------------------------------------------------------------------|
| See who is connected          | `curl -H "Authorization: Bearer <client key>" https://relay/status` |
| Add / revoke a user           | Edit `RELAY_CLIENT_KEYS` in `relay/.env`, `docker compose up -d`. The agent reconnects by itself. |
| Rotate the agent key          | Change it in both `.env` files, restart both stacks.                |
| Change the model              | Edit `VLLM_MODEL` / `VLLM_SERVED_MODEL_NAME`, `docker compose up -d`. Clients see the new name after *Refresh models*. |
| Add a second GPU server       | Another agent key + another `gpu-agent` stack with a different `AGENT_NAME`. The relay balances by load. |
| Watch a request               | Relay and agent logs share the request id (`request 3f2a…`).         |
| Try the API from anything     | `curl -N https://relay/v1/chat/completions -H "Authorization: Bearer <key>" -d '{"model":"qwen-27b","stream":true,"messages":[{"role":"user","content":"Hi"}]}'` |

## Troubleshooting

- **Dialog says "rejected the API key"** – the client key is not in
  `RELAY_CLIENT_KEYS` (or the agent key was used by mistake).
- **"No GPU agent is connected"** – check `docker compose logs agent` on the GPU
  server: wrong `RELAY_URL`/`RELAY_AGENT_KEY` shows as *HTTP 401*, a firewall
  as repeated *reconnecting to relay*.
- **"gpu-1 is still loading its model"** – vLLM is downloading or loading
  weights; `docker compose logs -f vllm` shows progress.
- **vLLM exits immediately** – usually out of GPU memory
  (`VLLM_MAX_MODEL_LEN`, `VLLM_GPU_MEMORY_UTILIZATION`, quantized checkpoint,
  `VLLM_TENSOR_PARALLEL_SIZE`) or an unsupported flag in `VLLM_EXTRA_ARGS`.
- **Response truncated** – raise *Max output tokens* in the Connection dialog
  (and make sure `VLLM_MAX_MODEL_LEN` leaves room for input + output).
- **Thinking text in the output** – keep *Allow the model to think* off, or run
  vLLM with `--reasoning-parser qwen3` (default in `.env.example`) so reasoning
  goes to `reasoning_content`; the client also strips `<think>` blocks. Either
  way the desktop app shows the reasoning live under *View → Model thinking*.

## Tests

```powershell
uv pip install -r requirements-dev.txt
uv run pytest -q                       # desktop, relay, agent and end-to-end suites
```

`remote/tests/test_end_to_end.py` runs the real relay and the real agent
in-process against a fake vLLM and drives them with the desktop
`RemoteService`, including cancellation reaching vLLM.
