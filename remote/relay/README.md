# TextEnhanceAI relay (public server)

The relay is a single Python process (`teai_relay`, aiohttp) fronted by Caddy
for HTTPS. GPU agents connect to it over an outbound WebSocket; TextEnhanceAI
and any other OpenAI-compatible client call its HTTP API. See
[../PROTOCOL.md](../PROTOCOL.md) for the endpoints and frames.

## Deploy with Docker Compose

```bash
cp .env.example .env
python3 -m teai_relay keygen 3          # prints three random keys
#   -> one for RELAY_AGENT_KEYS  (name:key)
#   -> the others for RELAY_CLIENT_KEYS (name:key,name:key)
nano .env                                # also set RELAY_DOMAIN
docker compose up -d --build
docker compose logs -f relay
```

Requirements on the host: Docker with Compose v2, inbound TCP 80 and 443, and
a DNS record for `RELAY_DOMAIN`. Caddy stores certificates in the `caddy-data`
volume and renews them automatically. The relay container itself is not
published; Caddy reaches it on the internal network.

Without a domain (LAN or testing only): set `RELAY_DOMAIN=:80` so Caddy serves
plain HTTP, then use `http://<ip>` on the clients and `AGENT_TLS_VERIFY=true`
stays irrelevant because no TLS is involved.

## Behind an existing nginx (or another reverse proxy)

If the host already runs nginx on ports 80/443, skip Caddy: publish the relay
on localhost and add a site to nginx.

```bash
cp docker-compose.nginx.yml docker-compose.override.yml   # auto-loaded by compose
echo "RELAY_BIND_PORT=8084" >> .env                        # any free localhost port
docker compose up -d --build
curl http://127.0.0.1:8084/health

cp nginx-site.example.conf /etc/nginx/sites-available/relay.conf   # edit server_name + port
ln -s /etc/nginx/sites-available/relay.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d relay.example.com --key-type rsa
```

`nginx-site.example.conf` keeps buffering off for SSE and uses long timeouts for
the agent WebSocket; a generic proxy snippet with `proxy_buffering on` or a
60 s read timeout would break streaming. `--key-type rsa` keeps the certificate
chain on ISRG Root X1, which every Windows trust store has; Python clients on
Windows frequently fail to verify Let's Encrypt's ECDSA chain because of a stale
cross-signed intermediate in the local store.

## Run without Docker

```bash
pip install -r requirements.txt
export RELAY_AGENT_KEYS="gpu-1:$(python -m teai_relay keygen)"
export RELAY_CLIENT_KEYS="alice:$(python -m teai_relay keygen)"
python -m teai_relay                     # listens on 0.0.0.0:8080
```

Put your own TLS terminator in front (Caddy, nginx, Traefik). It must pass
WebSocket upgrades to `/agent/ws` and must not buffer `text/event-stream`
responses.

## Configuration

All settings are environment variables (see `.env.example`):

| Variable                    | Default | Meaning                                                    |
|-----------------------------|---------|------------------------------------------------------------|
| `RELAY_AGENT_KEYS`          | —       | `name:key,…` accepted from GPU agents (required)            |
| `RELAY_CLIENT_KEYS`         | —       | `name:key,…` accepted from clients (required)               |
| `RELAY_HOST` / `RELAY_PORT` | 0.0.0.0 / 8080 | Bind address inside the container                     |
| `RELAY_QUEUE_TIMEOUT`       | 300 s   | Max wait for a free slot on a busy agent                    |
| `RELAY_IDLE_TIMEOUT`        | 180 s   | Max silence from the agent during a request                 |
| `RELAY_KEEPALIVE_INTERVAL`  | 15 s    | SSE comment interval while a client waits                   |
| `RELAY_WS_HEARTBEAT`        | 20 s    | WebSocket ping interval towards agents                      |
| `RELAY_MAX_BODY_MB`         | 16      | Largest accepted request body                               |
| `RELAY_LOG_LEVEL`           | INFO    | `DEBUG` prints every frame type                             |
| `RELAY_PUBLIC_URL`          | —       | Shown in `/status` (informational)                          |
| `RELAY_ADMIN_KEY`           | —       | Enables `/admin` (status page and key management)           |
| `RELAY_KEYS_FILE`           | `/data/keys.json` | Keys created at runtime (Docker volume `relay-data`) |

Keys must be at least 16 characters; agent, client and admin keys must differ.
With `RELAY_ADMIN_KEY` set, `RELAY_AGENT_KEYS`/`RELAY_CLIENT_KEYS` may be
empty and every key can be created through the admin API instead.

## Administration: status page and key management

Set `RELAY_ADMIN_KEY` in `.env` (generate it with `keygen`) and restart. The
admin routes use that key and nothing else; they answer 404 while it is unset.

```bash
ADMIN="Authorization: Bearer teai_admin..."
curl -H "$ADMIN" https://relay.example.com/admin/keys            # names, kinds, sources (never the keys)
curl -H "$ADMIN" -X POST https://relay.example.com/admin/keys \
     -H "Content-Type: application/json" -d '{"name":"carol","kind":"client"}'   # the key is returned once
curl -H "$ADMIN" -X DELETE https://relay.example.com/admin/keys/carol    # revoked immediately
```

`GET /admin` (open it in a browser with the key in the `Authorization`
header, e.g. through a browser extension, or `curl -H "$ADMIN" .../admin`)
renders a status page: agents with state, models, in-flight and queued
requests, chunks/s and GPU metrics, relay counters, key names and the outcomes
of the last 50 requests (request id, client name, status, duration, chunk
count — never any text). The page is served with `Cache-Control: no-store`.

Keys created this way live in `RELAY_KEYS_FILE` (the `relay-data` volume) and
are merged with the environment keys; a file entry shadows an environment
entry with the same name. Revoking an environment key records it as blocked
in the file, so the block survives restarts until the entry leaves `.env`.
Revoking an agent key also closes that agent's socket; its in-flight
requests fail with 502. Requests of a revoked client key that are already
streaming finish; the next request is rejected with 401. Admin actions are
logged with the key *name* only.

When the relay is down, the same file can be edited from the command line:

```bash
docker compose run --rm relay python -m teai_relay keys list
docker compose run --rm relay python -m teai_relay keys add carol           # client key
docker compose run --rm relay python -m teai_relay keys add gpu-2 --agent   # agent key
docker compose run --rm relay python -m teai_relay keys revoke carol
```

A running relay does not re-read the file, so use the API while it runs.

## Endpoints

| Method | Path                    | Auth        | Purpose                                |
|--------|-------------------------|-------------|----------------------------------------|
| GET    | `/health`               | none        | Liveness + agent counts                |
| GET    | `/v1/models`            | client key  | Models offered by ready agents         |
| POST   | `/v1/chat/completions`  | client key  | Chat completion (streaming or JSON)    |
| POST   | `/v1/completions`       | client key  | Legacy completion                      |
| GET    | `/status`               | client key  | Agents, load, queue depth, chunks/s, counters |
| GET    | `/agent/ws`             | agent key   | WebSocket for GPU agents               |
| GET    | `/admin`                | admin key   | HTML status page (404 without `RELAY_ADMIN_KEY`) |
| GET    | `/admin/keys`           | admin key   | Key names and kinds                    |
| POST   | `/admin/keys`           | admin key   | `{"name", "kind"}` → new key (shown once) |
| DELETE | `/admin/keys/{name}`    | admin key   | Revoke a key immediately               |

Example:

```bash
curl -N https://relay.example.com/v1/chat/completions \
  -H "Authorization: Bearer teai_..." -H "Content-Type: application/json" \
  -d '{"model":"qwen-27b","stream":true,"messages":[{"role":"user","content":"Say hi"}]}'
```

## Logging and privacy

The relay logs one line when a request starts (request id, client name, agent,
model, streaming flag) and one when it ends (outcome, duration, chunk count).
Prompt text and model output are never written to the log. Docker keeps the
last 50 MB of logs per container.

## Tests

```bash
pip install pytest aiohttp
pytest -q tests
```
