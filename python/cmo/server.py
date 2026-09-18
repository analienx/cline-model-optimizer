"""CMO v2 loopback dashboard + SSE server. Binds 127.0.0.1 only by default."""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .decision import DecisionEngine
from .policy import load_canonical_policy, policy_digest
from .projections import PROJECTION_SQL

DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>CMO v2 — Route Control</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { color-scheme: dark; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #12141a; color: #e6e8ee; margin: 0; padding: 1.5rem; }
h1 { font-size: 1.2rem; } h2 { font-size: 1rem; color: #9aa3b2; }
table { border-collapse: collapse; margin: 0.75rem 0; width: 100%; }
th, td { border: 1px solid #2a2e39; padding: 0.4rem 0.7rem; text-align: left; font-size: 0.85rem; }
th { background: #1a1d26; color: #9aa3b2; }
.state-AVAILABLE { color: #6fe38f; } .state-QUOTA { color: #f0b45c; }
.state-AUTH_BLOCKED { color: #f06a6a; } .state-UNKNOWN { color: #8b93a3; }
.badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 4px; background: #232735; font-size: 0.75rem; }
#events { max-height: 16rem; overflow-y: auto; font-size: 0.8rem; }
li { margin: 0.15rem 0; }
</style></head>
<body>
<h1>CMO v2 — Route Control <span class="badge" id="freshness">loading…</span></h1>
<h2>Active goal / next decision</h2>
<div id="decision">…</div>
<h2>Free-route matrix (3 models × 3 accounts — evidence-based, no invented percentages)</h2>
<table id="matrix"><thead><tr><th>Model</th><th>account-1</th><th>account-2</th><th>account-3</th></tr></thead><tbody></tbody></table>
<h2>Subscription routes</h2>
<table id="subroutes"><thead><tr><th>Model</th><th>Account</th><th>State</th><th>Evidence</th></tr></thead><tbody></tbody></table>
<h2>Catalog capability</h2>
<div id="catalog">…</div>
<h2>Event timeline</h2>
<ul id="events"></ul>
<script>
async function tick() {
  const r = await fetch('/api/snapshot'); const s = await r.json();
  document.getElementById('freshness').textContent = 'updated ' + new Date().toLocaleTimeString();
  const d = s.decision || {};
  document.getElementById('decision').textContent =
    (d.action || '—') + ' — ' + (d.reason || '') +
    (d.route ? ' → ' + d.route.model + ' @ ' + d.route.account_alias : '');
  const byCell = {}; (s.route_state || []).forEach(c => byCell[c.model + '|' + c.account_alias] = c);
  const tb = document.querySelector('#matrix tbody'); tb.innerHTML = '';
  const freeModels = [...new Set((s.policy_routes || []).filter(r => r.tier === 'free').map(r => r.model))];
  for (const m of freeModels) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td>' + m + '</td>' + ['account-1','account-2','account-3'].map(a => {
      const c = byCell[m + '|' + a] || {};
      const ev = c.last_probe_at ? new Date(c.last_probe_at).toLocaleTimeString() : 'no evidence';
      return '<td class="state-' + (c.state || 'UNKNOWN') + '">' + (c.state || 'UNKNOWN') +
             '<br><small>src: ' + (c.evidence_source || 'none') + ' @ ' + ev + '</small></td>';
    }).join('');
    tb.appendChild(tr);
  }
  const st = document.querySelector('#subroutes tbody'); st.innerHTML = '';
  (s.policy_routes || []).filter(r => r.tier === 'subscription').forEach(rr => {
    const c = byCell[rr.model + '|' + rr.accounts[0]] || {};
    st.insertAdjacentHTML('beforeend', '<tr><td>' + rr.model + '</td><td>' + rr.accounts[0] +
      '</td><td class="state-' + (c.state || 'UNKNOWN') + '">' + (c.state || 'UNKNOWN') + '</td><td>' +
      (c.last_reason_code || '—') + '</td></tr>');
  });
  document.getElementById('catalog').textContent = (s.catalog_models || [])
    .map(m => m.model + ': ' + m.capability).join(' · ') || 'no catalog evidence';
  const ul = document.getElementById('events'); ul.innerHTML = '';
  (s.recent_events || []).slice(0, 25).forEach(e => {
    ul.insertAdjacentHTML('beforeend', '<li><b>' + e.event_type + '</b> ' + (e.model || '') +
      ' ' + (e.account_alias || '') + ' <i>' + (e.reason_code || '') + '</i> ' + e.occurred_at + '</li>');
  });
}
tick(); setInterval(tick, 15000);
const es = new EventSource('/api/stream'); es.onmessage = () => tick();
</script>
</body></html>"""


class CmoHandler(BaseHTTPRequestHandler):
    server_version = "cmo/2.0"

    @property
    def store(self):
        return self.server.store  # type: ignore[attr-defined]

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Loopback-only dashboard: never cache evidence.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._send(200, DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/snapshot":
            self._send(200, json.dumps(self._snapshot(), sort_keys=True).encode(), "application/json")
        elif self.path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(b"retry: 5000\n\n")
                self.wfile.flush()
                # Keep the SSE channel open; the client polls a 15s freshness tick.
                for _ in range(3600):
                    time.sleep(5)
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (ConnectionAbortedError, BrokenPipeError):
                pass
        elif self.path == "/api/health":
            self._send(200, b'{"ok": true, "service": "cmo-v2"}', "application/json")
        else:
            self._send(404, b'{"error": "not found"}', "application/json")

    def _snapshot(self) -> dict[str, Any]:
        conn = self.store._conn
        conn.executescript(PROJECTION_SQL)
        rows = [dict(r) for r in conn.execute("SELECT * FROM route_state ORDER BY model, account_alias")]
        overrides = [dict(r) for r in conn.execute(
            "SELECT * FROM overrides WHERE expires_at IS NULL OR expires_at > ?",
            (int(time.time() * 1000),))]
        engine = DecisionEngine()
        decision = engine.decide(rows, overrides)
        policy = load_canonical_policy()
        return {
            "schema": "cmo.snapshot/v1",
            "policy_digest": policy_digest(policy),
            "policy_routes": policy["routes"],
            "route_state": rows,
            "catalog_models": [dict(r) for r in conn.execute("SELECT * FROM catalog_models")],
            "overrides": overrides,
            "decision": decision,
            "recent_events": self.store.events(limit=50),
        }

    def log_message(self, fmt: str, *args) -> None:  # silence default stderr noise
        pass


def serve(bind: str = "127.0.0.1", port: int = 4311, db_path: str | Path | None = None) -> None:
    if bind != "127.0.0.1":
        raise SystemExit("CMO v2 dashboard binds 127.0.0.1 only (spec 7.1)")
    from .events import EventStore
    store = EventStore(db_path or (Path.home() / ".cmo" / "cmo-v2.sqlite3"))
    httpd = ThreadingHTTPServer((bind, port), CmoHandler)
    httpd.store = store  # type: ignore[attr-defined]
    httpd.serve_forever()
