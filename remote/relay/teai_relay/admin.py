"""The relay's HTML status page (``GET /admin``): inline CSS, no scripts, no secrets.

Everything shown is state the relay keeps anyway: agents with their load and
throughput, relay counters, key *names* and the outcomes of the last requests
(id, client name, status, duration, chunk count). Prompt and output text never
reach this module.
"""

import html
import time

STYLE = """
body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 2rem; color: #1f2933; background: #f7f8fa; }
h1 { font-size: 1.4rem; margin-bottom: 0.2rem; }
h2 { font-size: 1.05rem; margin-top: 1.6rem; border-bottom: 1px solid #d9dde3; padding-bottom: 0.2rem; }
p.muted, span.muted { color: #6b7280; font-size: 0.9rem; }
table { border-collapse: collapse; width: 100%; background: #fff; font-size: 0.9rem; }
th, td { text-align: left; padding: 0.35rem 0.6rem; border-bottom: 1px solid #e5e7eb; vertical-align: top; }
th { background: #eef1f5; font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.state-ready { color: #15803d; font-weight: 600; }
.state-loading { color: #b45309; font-weight: 600; }
.state-unavailable, .state-draining { color: #b91c1c; font-weight: 600; }
.ok { color: #15803d; }
.err { color: #b91c1c; }
code { background: #eef1f5; padding: 0 0.25rem; border-radius: 3px; }
"""


def _esc(value):
    return html.escape(str(value), quote=True)


def _iso(timestamp):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(float(timestamp)))
    except (TypeError, ValueError, OverflowError):
        return "-"


def _duration(seconds):
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return "-"
    if seconds < 60:
        return "{0} s".format(seconds)
    if seconds < 3600:
        return "{0} min".format(seconds // 60)
    if seconds < 86400:
        return "{0} h {1:02d} min".format(seconds // 3600, (seconds % 3600) // 60)
    return "{0} d {1} h".format(seconds // 86400, (seconds % 86400) // 3600)


def _agents_table(agents):
    if not agents:
        return "<p class=\"muted\">No GPU agent is connected.</p>"
    rows = []
    for agent in agents:
        state = str(agent.get("state", "?"))
        models = ", ".join(str(model.get("id", model)) if isinstance(model, dict) else str(model)
                           for model in agent.get("models") or []) or "-"
        metrics = agent.get("metrics") or {}
        rows.append(
            "<tr><td>{name}</td><td class=\"state-{state}\">{state}</td><td>{models}</td>"
            "<td class=\"num\">{in_flight} / {limit}</td><td class=\"num\">{queued}</td>"
            "<td class=\"num\">{rate}</td><td class=\"num\">{total}</td><td>{since}<br>"
            "<span class=\"muted\">{for_}</span></td><td>{version}</td><td>{key}</td><td>{gpu}</td></tr>".format(
                name=_esc(agent.get("name", "?")), state=_esc(state), models=_esc(models),
                in_flight=_esc(agent.get("in_flight", 0)), limit=_esc(agent.get("max_concurrency", "?")),
                queued=_esc(agent.get("queued", 0)), rate=_esc(agent.get("chunks_per_s", 0)),
                total=_esc(agent.get("requests_total", 0)), since=_iso(agent.get("connected_at")),
                for_=_duration(agent.get("connected_for")), version=_esc(agent.get("agent_version", "")),
                key=_esc(agent.get("key", "")), gpu=_esc(_gpu_summary(metrics)),
            )
        )
    return (
        "<table><tr><th>Agent</th><th>State</th><th>Models</th><th class=\"num\">In flight</th>"
        "<th class=\"num\">Queued</th><th class=\"num\">Chunks/s</th><th class=\"num\">Requests</th>"
        "<th>Connected since</th><th>Version</th><th>Key</th><th>GPU</th></tr>" + "".join(rows) + "</table>"
    )


def _gpu_summary(metrics):
    """One line from the agent's optional vLLM metrics (see P5.5); "-" when absent."""
    if not isinstance(metrics, dict) or not metrics:
        return "-"
    parts = []
    if metrics.get("tokens_per_s") is not None:
        parts.append("{0} tok/s".format(metrics["tokens_per_s"]))
    if metrics.get("num_requests_running") is not None:
        parts.append("{0} running".format(metrics["num_requests_running"]))
    if metrics.get("num_requests_waiting") is not None:
        parts.append("{0} waiting".format(metrics["num_requests_waiting"]))
    if metrics.get("gpu_cache_usage_perc") is not None:
        try:
            parts.append("KV cache {0:.0f}%".format(float(metrics["gpu_cache_usage_perc"]) * 100))
        except (TypeError, ValueError):
            pass
    return " · ".join(parts) or "-"


def _keys_table(keys):
    if not keys:
        return "<p class=\"muted\">No keys.</p>"
    rows = "".join(
        "<tr><td>{0}</td><td>{1}</td><td>{2}</td><td>{3}</td></tr>".format(
            _esc(row.get("name", "")), _esc(row.get("kind", "")), _esc(row.get("source", "")),
            _esc(row.get("created") or "-"),
        )
        for row in keys
    )
    return "<table><tr><th>Name</th><th>Kind</th><th>Source</th><th>Created</th></tr>" + rows + "</table>"


def _recent_table(recent):
    if not recent:
        return "<p class=\"muted\">No requests yet.</p>"
    rows = []
    for item in reversed(list(recent)):
        status = str(item.get("status", ""))
        css = "ok" if status == "ok" else "err"
        rows.append(
            "<tr><td>{when}</td><td><code>{id}</code></td><td>{client}</td><td>{agent}</td><td>{model}</td>"
            "<td class=\"{css}\">{status}</td><td class=\"num\">{duration:.1f} s</td><td class=\"num\">{chunks}</td></tr>".format(
                when=_iso(item.get("finished_at")), id=_esc(item.get("id", "")), client=_esc(item.get("client", "")),
                agent=_esc(item.get("agent") or "-"), model=_esc(item.get("model") or "-"), css=css,
                status=_esc(status), duration=float(item.get("duration") or 0), chunks=_esc(item.get("chunks", 0)),
            )
        )
    return (
        "<table><tr><th>Finished</th><th>Request</th><th>Client</th><th>Agent</th><th>Model</th><th>Outcome</th>"
        "<th class=\"num\">Duration</th><th class=\"num\">Chunks</th></tr>" + "".join(rows) + "</table>"
    )


def render_admin_page(snapshot, recent, keys):
    """Return the complete HTML document for ``GET /admin``."""
    relay = snapshot.get("relay") or {}
    counters = (
        "<p>Version <b>{version}</b> · protocol {protocol} · up for {uptime} · "
        "{total} requests ({failed} failed, {cancelled} cancelled) · public URL {url}</p>"
    ).format(
        version=_esc(relay.get("version", "?")), protocol=_esc(relay.get("protocol", "?")),
        uptime=_duration(relay.get("uptime")), total=_esc(relay.get("requests_total", 0)),
        failed=_esc(relay.get("requests_failed", 0)), cancelled=_esc(relay.get("requests_cancelled", 0)),
        url=_esc(relay.get("public_url") or "-"),
    )
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>TextEnhanceAI relay</title><style>{style}</style></head><body>"
        "<h1>TextEnhanceAI relay</h1><p class=\"muted\">Snapshot taken {now}; reload the page for fresh numbers.</p>"
        "{counters}"
        "<h2>GPU agents</h2>{agents}"
        "<h2>Keys</h2><p class=\"muted\">Names only; keys are shown once when created "
        "(<code>POST /admin/keys</code>). Revoke with <code>DELETE /admin/keys/&lt;name&gt;</code>.</p>{keys}"
        "<h2>Last {count} requests</h2>{recent}"
        "</body></html>"
    ).format(
        style=STYLE, now=_iso(time.time()), counters=counters, agents=_agents_table(snapshot.get("agents") or []),
        keys=_keys_table(keys), count=len(recent), recent=_recent_table(recent),
    )
