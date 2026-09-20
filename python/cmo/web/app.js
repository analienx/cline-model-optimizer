"use strict";
/* CMO dashboard: renders only recorded evidence. No optimistic state. */

const S = {
  snapshot: null,
  goalId: "",
  strategy: "free-first",
  stream: "connecting",
  lastRevision: null,
  eventFilter: { event_type: "", source: "" },
  serverSkewMs: 0,
  error: null,
  timers: [],
};

const $ = (id) => document.getElementById(id);
const stateSelect = $("strategy");
const goalSelect = $("goal-select");

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, String(v));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function fmtAge(ms) {
  if (ms === null || ms === undefined) return "no evidence";
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 90) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m}m ago`;
  const h = (m / 60).toFixed(1);
  if (h < 48) return `${h}h ago`;
  return `${(h / 24).toFixed(1)}d ago`;
}

function fmtClock(ms) {
  if (!ms) return "—";
  return new Date(ms).toLocaleString();
}

function shortDigest(d) { return d ? String(d).slice(0, 12) : "—"; }

function liveAge(cell) {
  const snap = S.snapshot;
  if (!snap || cell.age_ms === null || cell.age_ms === undefined) return null;
  const elapsed = Date.now() - snap.generated_at - S.serverSkewMs;
  return cell.age_ms + Math.max(0, elapsed);
}

/* Age spans carry their own base so the clock can tick without re-rendering
   (a full re-render would steal focus from the bounded action buttons). */
function ageSpan(offsetMs, baseMs) {
  if (offsetMs === null || offsetMs === undefined) {
    return el("span", { class: "meta", text: " no evidence" });
  }
  return el("span", {
    class: "meta",
    "data-age-offset": String(offsetMs),
    "data-age-base": String(baseMs),
    text: " " + fmtAge(offsetMs),
  });
}

function tickAges() {
  if (!S.snapshot) return;
  for (const node of document.querySelectorAll("[data-age-base]")) {
    const base = Number(node.dataset.ageBase);
    const offset = Number(node.dataset.ageOffset || 0);
    const elapsed = Math.max(0, Date.now() - base - S.serverSkewMs);
    node.textContent = " " + fmtAge(offset + elapsed);
  }
}

function showBanner(message, kind) {
  const b = $("banner");
  if (!message) { b.hidden = true; b.textContent = ""; return; }
  b.hidden = false;
  b.textContent = message;
  b.style.borderColor = kind === "warn" ? "var(--warn)" : "var(--bad)";
}

async function api(path, options) {
  const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, options));
  let body = null;
  try { body = await res.json(); } catch (e) { body = null; }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${body ? (body.error || JSON.stringify(body)) : ""}`);
  return body;
}

async function loadSnapshot() {
  const params = new URLSearchParams();
  if (S.goalId) params.set("goal_id", S.goalId);
  params.set("strategy", S.strategy);
  const t0 = Date.now();
  try {
    const snap = await api(`/api/snapshot?${params.toString()}`);
    const rtt = Date.now() - t0;
    S.serverSkewMs = Math.round(rtt / 2);
    S.snapshot = snap;
    S.lastRevision = snap.state_revision;
    S.error = null;
    render();
  } catch (err) {
    S.error = String(err.message || err);
    renderError();
  }
}

/* ---------------------------------------------------------------- render */

function render() {
  const snap = S.snapshot;
  if (!snap) return;
  showBanner(S.error, "bad");
  renderStream();
  renderVitals(snap);
  renderGoalSelect(snap);
  renderDecision(snap);
  renderMatrix(snap);
  renderSubscription(snap);
  renderCatalog(snap);
  renderAuth(snap);
  renderQuota(snap);
  renderGoals(snap);
  renderEvents(snap);
  renderOverrides(snap);
  $("raw").textContent = JSON.stringify(snap, null, 2);
  window.__cmoSnapshot = snap;
  window.__cmoState = S;
  $("footer-state").textContent =
    `snapshot schema ${snap.schema} · state revision ${snap.state_revision} · evaluated ${fmtClock(snap.generated_at)} · service ${snap.service.status}`;
}

function renderError() {
  renderStream();
  showBanner(`Cannot load status: ${S.error}`, "bad");
  $("footer-state").textContent = "status unavailable";
}

function renderStream() {
  const pill = $("stream");
  const map = {
    live: ["pill-ok", "live · SSE"],
    connecting: ["pill-unknown", "connecting…"],
    reconnecting: ["pill-warn", "reconnecting…"],
    polling: ["pill-warn", "polling fallback"],
  };
  const [cls, label] = map[S.stream] || map.connecting;
  pill.className = `pill ${cls}`;
  pill.textContent = label;
}

function renderVitals(snap) {
  const box = $("vitals");
  clear(box);
  const fresh = snap.freshness;
  const evaluatedAge = Date.now() - snap.generated_at - S.serverSkewMs;
  const staleWindow = fresh.browser_freshness_ms * 2;
  const catalogAge = fresh.catalog_seen_at ? Date.now() - fresh.catalog_seen_at - S.serverSkewMs : null;

  const servicePill = { connected: "pill-ok", degraded: "pill-warn", offline: "pill-bad" }[snap.service.status] || "pill-unknown";
  const catalogPill = { fresh: "pill-ok", stale: "pill-warn", error: "pill-bad" }[fresh.catalog_status] || "pill-unknown";
  const artifactOk = !snap.service.artifact_digest || !snap.installed_artifact_digest
    || snap.service.artifact_digest === snap.installed_artifact_digest;

  const items = [
    ["Service", snap.service.status, `pid ${snap.service.pid ?? "—"} · up ${snap.service.uptime_ms ? Math.round(snap.service.uptime_ms / 1000) + "s" : "—"} · db ${snap.service.db}`,
      servicePill],
    ["State revision", String(snap.state_revision), `evaluated ${fmtAge(evaluatedAge)}`, evaluatedAge > staleWindow ? "pill-warn" : "pill-ok"],    ["Catalog", fresh.catalog_status, `seen ${fmtAge(catalogAge)} · ${snap.catalog.length} model(s)`, catalogPill],
    ["Policy", `v${snap.policy.policyVersion}`, `owner ${snap.policy.owner} · PAYG ${snap.policy.neverPayg ? "impossible" : "POSSIBLE"}`, snap.policy.neverPayg ? "pill-ok" : "pill-bad"],
    ["Artifact", shortDigest(snap.service.artifact_digest), `installed ${shortDigest(snap.installed_artifact_digest)} · ${artifactOk ? "matches" : "MISMATCH"}`, artifactOk ? "pill-ok" : "pill-bad"],
    ["Evidence TTL", `${Math.round(fresh.evidence_ttl_ms / 1000)}s`, `browser freshness ${Math.round(fresh.browser_freshness_ms / 1000)}s`, "pill-unknown"],
  ];
  for (const [k, v, d, pill] of items) {
    const value = el("div", { class: "v" }, [String(v)]);
    if (pill) value.append(el("span", { class: `pill ${pill}` }, [" "]));
    if (pill && pill.includes("warn") && k === "State revision") value.append("");
    box.append(el("div", { class: "vital" }, [
      el("div", { class: "k", text: k }), value, el("div", { class: "d", text: d }),
    ]));
  }
  if (snap.service.issues && snap.service.issues.length) {
    box.append(el("div", { class: "vital" }, [
      el("div", { class: "k", text: "Issues" }),
      el("div", { class: "v", text: snap.service.issues.join("; ") }),
    ]));
  }
  if (evaluatedAge > staleWindow) {
    showBanner(`Displayed status is ${fmtAge(evaluatedAge)} old — outside the ${Math.round(staleWindow / 1000)}s freshness window.`, "warn");
  } else if (!S.error) {
    showBanner(null);
  }
}

function renderGoalSelect(snap) {
  const current = goalSelect.value;
  clear(goalSelect);
  goalSelect.append(el("option", { value: "" }, ["auto (most recent active)"]));
  for (const g of snap.goals) {
    goalSelect.append(el("option", { value: g.goal_id },
      [`${g.status}: ${g.goal_id.slice(0, 22)}${g.goal_id.length > 22 ? "…" : ""}`]));
  }
  goalSelect.value = current || S.goalId || "";
}

function cellNode(cell) {
  const parts = [
    el("span", { class: "st", text: cell.state_label }),
    ageSpan(cell.age_ms, S.snapshot.generated_at),
    el("span", { class: "meta", text: ` · ${cell.freshness}` }),
  ];
  if (cell.in_use) parts.push(el("span", { class: "badge badge-inuse", text: cell.in_use_liveness === "live" ? "IN USE" : "IN USE (stale)" }));
  if (cell.manual_override) parts.push(el("span", { class: "badge badge-override", text: "FORCE SKIP" }));
  if (cell.state === "STALE") parts.push(el("span", { class: "badge badge-stale", text: "STALE" }));
  const detail = [
    `route ${cell.route_key}`,
    `state ${cell.state}${cell.stored_state !== cell.state ? ` (stored ${cell.stored_state})` : ""}`,
    `observed ${cell.observed_at_iso || "never"}`,
    cell.reset_after_ms ? `reset_after ${Math.round(cell.reset_after_ms / 1000)}s (${cell.reset_known ? "provider reported" : "reset unknown"})` : null,
    cell.reset_at_iso ? `reset at ${cell.reset_at_iso}` : null,
    cell.next_check_at_iso ? `next check ${cell.next_check_at_iso}` : null,
    cell.last_reason_code ? `reason ${cell.last_reason_code}` : null,
    cell.evidence_source ? `evidence source ${cell.evidence_source}` : null,
    cell.manual_override_reason ? `override: ${cell.manual_override_reason}` : null,
  ].filter(Boolean).join("\n");
  const wrap = el("span", { class: `cell cell-${cell.state}`, title: detail }, parts);
  const recheck = el("button", {
    type: "button", class: "btn btn-mini", title: "Request one bounded recheck of this leg",
    onclick: () => requestRecheck(cell),
  }, ["recheck"]);
  wrap.append(document.createElement("br"), recheck);
  return wrap;
}

function renderMatrix(snap) {
  const table = $("free-matrix");
  clear(table);
  const accounts = [];
  for (const row of snap.free_matrix) {
    for (const a of Object.keys(row.accounts)) if (!accounts.includes(a)) accounts.push(a);
  }
  const thead = el("thead", {}, [el("tr", {}, [
    el("th", { scope: "col", text: "Free model (canonical order)" }),
    ...accounts.map(a => el("th", { scope: "col", text: a })),
  ])]);
  const tbody = el("tbody");
  for (const row of snap.free_matrix) {
    tbody.append(el("tr", {}, [
      el("th", { scope: "row", text: `${row.model}\n${row.provider}/${row.tier}` }),
      ...accounts.map(a => el("td", {}, [row.accounts[a] ? cellNode(row.accounts[a]) : el("span", { class: "meta", text: "not a configured leg" })])),
    ]));
  }
  table.append(el("caption", { class: "sr-only", text: "Free route status per model and account" }), thead, tbody);
  const legend = $("legend");
  clear(legend);
  for (const [cls, label] of [["AVAILABLE", "Recently verified available"], ["QUOTA", "Quota exhausted"], ["QUOTA_EXPIRED", "Reset passed, not re-verified"], ["STALE", "Evidence older than TTL"], ["AUTH_BLOCKED", "Sign-in required"], ["TRANSIENT", "Temporary failure"], ["PROBING", "Check in flight"], ["UNKNOWN", "Unknown"], ["CAPABILITY_UNAVAILABLE", "Not offered by catalog"], ["FORCE_SKIP", "Manual override"]]) {
    legend.append(el("span", {}, [
      el("span", { class: "swatch", style: `background: var(--${({ AVAILABLE: "ok", QUOTA: "bad", QUOTA_EXPIRED: "warn", STALE: "warn", AUTH_BLOCKED: "bad", TRANSIENT: "warn", PROBING: "info", UNKNOWN: "unknown", CAPABILITY_UNAVAILABLE: "unknown", FORCE_SKIP: "skip" })[cls]})` }),
      label,
    ]));
  }
}

function renderSubscription(snap) {
  const table = $("subscription");
  clear(table);
  const thead = el("thead", {}, [el("tr", {}, [
    el("th", { scope: "col", text: "Subscription model" }),
    el("th", { scope: "col", text: "Provider" }),
    el("th", { scope: "col", text: "Account" }),
    el("th", { scope: "col", text: "Status" }),
  ])]);
  const tbody = el("tbody");
  for (const cell of snap.subscription) {
    tbody.append(el("tr", {}, [
      el("th", { scope: "row", text: cell.model }),
      el("td", { text: cell.provider }),
      el("td", { text: cell.account_alias }),
      el("td", {}, [cellNode(cell)]),
    ]));
  }
  table.append(thead, tbody);
}

function renderDecision(snap) {
  const box = $("decision");
  clear(box);
  const d = snap.decision;
  const selected = d.selected || {};
  box.append(el("div", { class: `action action-${d.action}`, text: d.action || "UNKNOWN" }));
  box.append(el("div", { class: "route-chip", text: selected.route_key || d.blocked_reason || "no eligible route" }));
  if (selected.reason_code) box.append(el("div", { class: "meta", text: `reason ${selected.reason_code}` }));
  if (d.blocked_reason && d.action !== "LAUNCH") box.append(el("div", { class: "meta", text: d.blocked_reason }));
  box.append(el("div", { class: "meta", text: `strategy ${d.strategy || S.strategy}${d.free_only ? " · free_only" : ""} · goal ${snap.selected_goal_id || "none"}` }));
  if (selected.account_alias) box.append(el("div", { class: "meta", text: `account ${selected.account_alias}` }));

  const skipped = $("skipped");
  clear(skipped);
  const list = d.skipped || [];
  if (!list.length) { skipped.append(el("p", { class: "hint", text: "No legs were skipped." })); return; }
  skipped.append(el("h3", { text: `Skipped legs (${list.length})` }));
  skipped.append(el("ul", { class: "plain" }, list.map(s => el("li", {}, [
    el("code", { text: s.route_key || "—" }),
    el("div", { class: "meta", text: `${s.reason_code || "unknown"}${s.detail ? " · " + s.detail : ""}` }),
  ]))));
}

function renderCatalog(snap) {
  const box = $("catalog");
  clear(box);
  const f = snap.freshness;
  const dl = el("dl", { class: "kv" }, [
    el("dt", { text: "Status" }), el("dd", { text: f.catalog_status }),
    el("dt", { text: "Seen" }), el("dd", { text: fmtAge(f.catalog_seen_at ? Date.now() - f.catalog_seen_at - S.serverSkewMs : null) }),
    el("dt", { text: "Last error" }), el("dd", { text: f.catalog_last_error_at ? fmtClock(f.catalog_last_error_at) : "none recorded" }),
    el("dt", { text: "Published" }), el("dd", { text: `${snap.catalog.length} model(s)` }),
  ]);
  box.append(dl);
  if (snap.catalog.length) {
    box.append(el("ul", { class: "plain" }, snap.catalog.map(r => el("li", {}, [
      el("code", { text: r.model }),
      el("span", { class: "meta", text: ` · ${r.capability}` }),
    ]))));
  } else {
    box.append(el("p", { class: "hint", text: "No catalog evidence recorded yet. Capability is unknown, not unavailable." }));
  }
}

function renderAuth(snap) {
  const box = $("auth");
  clear(box);
  const authCells = snap.cells.filter(c => c.state === "AUTH_BLOCKED" || c.last_reason_code === "auth.required");
  const events = (snap.recent_events || []).filter(e => e.reason_code === "auth.required" || e.event_type === "auth.required");
  if (!authCells.length && !events.length) {
    box.append(el("p", { class: "hint", text: "No sign-in failure recorded. Unreported sign-in state stays unknown for non-active accounts." }));
    return;
  }
  box.append(el("ul", { class: "plain" }, authCells.map(c => el("li", {}, [
    el("code", { text: c.route_key }),
    el("div", { class: "meta", text: `${c.state_label} · observed ${c.observed_at_iso || "never"} · ${c.last_reason_code || ""}` }),
  ]))));
}

function renderQuota(snap) {
  const box = $("quota");
  clear(box);
  const rows = snap.cells.filter(c => c.reset_after_ms || ["QUOTA", "QUOTA_EXPIRED"].includes(c.state));
  if (!rows.length) {
    box.append(el("p", { class: "hint", text: "No quota exhaustion evidence recorded." }));
    return;
  }
  const table = el("table", {}, [
    el("caption", { class: "sr-only", text: "Recorded quota evidence per route" }),
    el("thead", {}, [el("tr", {}, ["Route", "State", "Observed", "Reset", "Next check", "Reason"].map(h => el("th", { scope: "col", text: h })))]),
    el("tbody", {}, rows.map(c => el("tr", {}, [
      el("th", { scope: "row", text: c.route_key }),
      el("td", { text: c.state_label }),
      el("td", { text: c.observed_at_iso || "never" }),
      el("td", { text: c.reset_after_ms ? `${Math.round(c.reset_after_ms / 1000)}s (${c.reset_known ? "provider reported" : "reset unknown"})` : "unknown" }),
      el("td", { text: c.next_check_at_iso || "—" }),
      el("td", { text: c.last_reason_code || "—" }),
    ]))),
  ]);
  box.append(el("div", { class: "tablewrap" }, [table]));
}

function renderGoals(snap) {
  const box = $("goals");
  clear(box);
  if (!snap.goals.length) {
    box.append(el("p", { class: "hint", text: "No Goal has reported telemetry yet." }));
    return;
  }
  box.append(el("ul", { class: "plain" }, snap.goals.map(g => {
    const items = [
      el("div", {}, [el("strong", { text: g.goal_id }), el("span", { class: `status-${g.status}`, text: ` · ${g.status}` })]),
      el("div", { class: "kv" }, [
        el("dt", { text: "repo" }), el("dd", { text: g.repo || "—" }),
        el("dt", { text: "strategy" }), el("dd", { text: `${g.strategy || "—"}${g.effective_strategy ? " → " + g.effective_strategy : ""}` }),
        el("dt", { text: "free_only" }), el("dd", { text: g.free_only ? "yes" : "no" }),
        el("dt", { text: "work_state" }), el("dd", { text: g.work_state || "—" }),
        el("dt", { text: "last event" }), el("dd", { text: fmtAge(g.last_event_at ? Date.now() - g.last_event_at - S.serverSkewMs : null) }),
        el("dt", { text: "context" }), el("dd", { text: g.context_escalated ? "escalated to subscription" : "normal" }),
      ]),
    ];
    if (g.objective) items.push(el("div", { class: "meta", text: g.objective.slice(0, 220) }));
    return el("li", {}, items);
  })));
}

function renderEvents(snap) {
  const table = $("events");
  clear(table);
  const rows = (snap.recent_events || []).filter(e =>
    (!S.eventFilter.event_type || e.event_type === S.eventFilter.event_type) &&
    (!S.eventFilter.source || (e.source_component || "").includes(S.eventFilter.source)));
  const head = ["#", "occurred", "received", "type", "source", "route", "outcome / reason", "detail"];
  table.append(
    el("caption", { class: "sr-only", text: "Recent ingested events" }),
    el("thead", {}, [el("tr", {}, head.map(h => el("th", { scope: "col", text: h })))]),
    el("tbody", {}, rows.map(e => el("tr", {}, [
      el("td", { text: e.seq }),
      el("td", { text: e.occurred_at_iso || e.occurred_at }),
      el("td", { text: e.received_at_iso || e.received_at }),
      el("td", {}, [el("code", { text: e.event_type })]),
      el("td", { text: e.source_component || "—" }),
      el("td", { text: [e.account_alias, e.provider, e.model, e.tier].filter(Boolean).join(" / ") || "—" }),
      el("td", { text: e.reason_code || e.outcome || "—" }),
      el("td", { text: e.safe_detail || "" }),
    ]))),
  );
}

function renderOverrides(snap) {
  const box = $("overrides");
  clear(box);
  const now = Date.now() - S.serverSkewMs;
  const active = (snap.overrides || []).filter(o => !o.expires_at || o.expires_at > now);
  if (!active.length) { box.append(el("p", { class: "hint", text: "No active overrides." })); return; }
  box.append(el("ul", { class: "plain" }, active.map(o => el("li", {}, [
    el("code", { text: o.route_key }),
    el("div", { class: "meta", text: `reason: ${o.reason || "—"} · expires ${o.expires_at ? fmtClock(o.expires_at) : "never"}` }),
    el("button", { type: "button", class: "btn btn-mini", onclick: () => removeOverride(o) }, ["Remove"]),
  ]))));
}

/* --------------------------------------------------------------- actions */

async function requestRecheck(cell) {
  try {
    const res = await api("/api/route/recheck", {
      method: "POST",
      body: JSON.stringify({ route_key: cell.route_key, account_alias: cell.account_alias, provider: cell.provider, model: cell.model, tier: cell.tier, reason: "bounded recheck from dashboard" }),
    });
    showBanner(`Recheck requested for ${cell.route_key} (recorded as a pending bounded request).`, "warn");
    setTimeout(() => loadSnapshot(), 400);
    return res;
  } catch (err) { showBanner(`Recheck request failed: ${err.message}`, "bad"); }
}

async function refreshCatalog() {
  const btn = $("refresh-catalog");
  btn.disabled = true;
  btn.textContent = "Refreshing…";
  try {
    const res = await api("/api/catalog/refresh", { method: "POST", body: "{}" });
    showBanner(res.ok ? `Catalog refreshed: ${res.models} model(s).` : `Catalog refresh failed honestly: ${res.detail}`, res.ok ? "warn" : "bad");
    setTimeout(() => loadSnapshot(), 300);
  } catch (err) { showBanner(`Catalog refresh failed: ${err.message}`, "bad"); }
  finally { btn.disabled = false; btn.textContent = "Refresh catalog"; }
}

async function removeOverride(o) {
  try {
    await api(`/api/override/${encodeURIComponent(o.override_id || o.route_key)}`, { method: "DELETE" });
    await loadSnapshot();
  } catch (err) { showBanner(`Remove failed: ${err.message}`, "bad"); }
}

$("override-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = new FormData(ev.target);
  const payload = Object.fromEntries(form.entries());
  payload.reason = (payload.reason || "").trim();
  if (!payload.reason) { showBanner("A force-skip requires an explicit reason.", "bad"); return; }
  payload.expires_in_ms = Number(payload.expires_in_minutes || 30) * 60000;
  try {
    await api("/api/override", { method: "POST", body: JSON.stringify(payload) });
    ev.target.reset();
    await loadSnapshot();
  } catch (err) { showBanner(`Override failed: ${err.message}`, "bad"); }
});

$("refresh").addEventListener("click", loadSnapshot);
stateSelect.addEventListener("change", () => { S.strategy = stateSelect.value; loadSnapshot(); });
goalSelect.addEventListener("change", () => { S.goalId = goalSelect.value; loadSnapshot(); });
$("evt-apply").addEventListener("click", () => {
  S.eventFilter = { event_type: $("evt-type").value.trim(), source: $("evt-source").value.trim() };
  render();
});
$("evt-clear").addEventListener("click", () => {
  $("evt-type").value = ""; $("evt-source").value = "";
  S.eventFilter = { event_type: "", source: "" }; render();
});

/* ------------------------------------------------------------------ SSE */

function connectStream() {
  if (!window.EventSource) { S.stream = "polling"; renderStream(); return; }
  const es = new EventSource("/api/stream");
  es.addEventListener("open", () => { S.stream = "live"; renderStream(); });
  es.addEventListener("revision", (evt) => {
    S.stream = "live"; renderStream();
    try {
      const data = JSON.parse(evt.data);
      if (data.revision !== S.lastRevision) loadSnapshot();
    } catch (err) { loadSnapshot(); }
  });
  es.addEventListener("error", () => {
    S.stream = es.readyState === 2 ? "reconnecting" : "connecting";
    renderStream();
  });
  return es;
}

/* the ticking clock keeps "true freshness" honest between snapshots */
S.timers.push(setInterval(tickAges, 1000));
S.timers.push(setInterval(() => { if (S.stream !== "live") loadSnapshot(); }, 5000));

loadSnapshot();
connectStream();
