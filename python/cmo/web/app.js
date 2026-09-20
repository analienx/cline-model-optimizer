"use strict";
/* CMO dashboard: renders only recorded evidence. No optimistic state.
   Mutations always go through the loopback API and re-render from a fresh
   snapshot; the UI never invents provider data. */

const S = {
  snapshot: null,
  goalId: "",
  previewStrategy: "",
  stream: "connecting",
  lastRevision: null,
  eventFilter: { event_type: "", source: "" },
  serverSkewMs: 0,
  error: null,
  timers: [],
  draft: null,      // routing-policy draft (deep copy of the saved doc)
  draftDirty: false,
  versions: [],
  toastTimer: null,
};

const $ = (id) => document.getElementById(id);
const goalSelect = $("goal-select");

/* Goal/strategy selection persists across reloads (D04). View persists via
   the URL hash in showView/initViews. Storage failures (private mode,
   disabled storage) degrade to per-session selection, never to an error. */
function readStored(key) {
  try { return localStorage.getItem(key) || ""; } catch (e) { return ""; }
}
function writeStored(key, value) {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch (e) { /* selection simply does not persist */ }
}
S.goalId = readStored("cmo.goalId");
S.previewStrategy = readStored("cmo.previewStrategy");

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
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "no evidence";
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 90) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m}m ago`;
  const h = (m / 60).toFixed(1);
  if (Number(h) < 48) return `${h}h ago`;
  return `${(Number(h) / 24).toFixed(1)}d ago`;
}

function fmtClock(ms) {
  if (!ms) return "—";
  return new Date(ms).toLocaleString();
}

function shortDigest(d) { return d ? String(d).slice(0, 12) : "—"; }

/* Local masking: full identity is only ever shown after an explicit reveal. */
function maskIdentity(value) {
  if (!value) return "—";
  const s = String(value);
  const at = s.indexOf("@");
  if (at > 0) {
    const local = s.slice(0, at);
    return `${local.slice(0, 1)}***${s.slice(at)}`;
  }
  if (s.length > 12) return `${s.slice(0, 4)}…${s.slice(-4)}`;
  return s;
}

const FRIENDLY_MODELS = {
  "cline-free/muse-spark-1.3-contributor": "Muse Spark (free)",
  "z-ai/glm-5.3-flash": "GLM Flash (free)",
  "cline-free/deepseek-v4.1-flash": "DeepSeek Flash (free)",
};
function friendlyModel(id) { return FRIENDLY_MODELS[id] || id; }

function redactSnapshotText(text) {
  return String(text)
    .replace(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, (m) => maskIdentity(m))
    .replace(/(token|secret|password|api[_-]?key|bearer)\s*[:=]\s*\S+/gi, "$1: [redacted]");
}

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

function toast(message, kind) {
  const t = $("toast");
  t.hidden = false;
  t.textContent = message;
  t.className = `toast toast-${kind || "info"} toast-show`;
  if (S.toastTimer) clearTimeout(S.toastTimer);
  S.toastTimer = setTimeout(() => { t.hidden = true; t.className = "toast"; }, 6000);
}

async function api(path, options) {
  const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, options));
  let body = null;
  try { body = await res.json(); } catch (e) { body = null; }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${body ? (body.error || JSON.stringify(body)) : ""}`);
  return body;
}

/* ---------------------------------------------------------------- views */

const VIEWS = ["overview", "accounts", "routing", "activity", "diagnostics"];

function showView(name, push) {
  if (!VIEWS.includes(name)) name = "overview";
  for (const v of VIEWS) {
    const panel = $(`view-${v}`);
    const tab = $(`tab-${v}`);
    const active = v === name;
    panel.hidden = !active;
    tab.classList.toggle("viewtab-active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
    tab.tabIndex = active ? 0 : -1;
  }
  if (push !== false) {
    try { history.replaceState(null, "", `#${name}`); } catch (e) { /* ignore */ }
  }
}

function initViews() {
  for (const v of VIEWS) {
    $(`tab-${v}`).addEventListener("click", () => showView(v));
  }
  document.querySelector(".viewnav-inner").addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") return;
    const current = VIEWS.findIndex((v) => !$(`view-${v}`).hidden);
    const next = (current + (ev.key === "ArrowRight" ? 1 : VIEWS.length - 1)) % VIEWS.length;
    showView(VIEWS[next]);
    $(`tab-${VIEWS[next]}`).focus();
    ev.preventDefault();
  });
  const initial = (location.hash || "").replace("#", "");
  showView(VIEWS.includes(initial) ? initial : "overview", false);
}

/* --------------------------------------------------------------- loading */

async function loadSnapshot() {
  const params = new URLSearchParams();
  if (S.goalId) params.set("goal_id", S.goalId);
  params.set("strategy", S.previewStrategy || "free-first");
  const t0 = Date.now();
  try {
    const snap = await api(`/api/snapshot?${params.toString()}`);
    const rtt = Date.now() - t0;
    S.serverSkewMs = Math.round(rtt / 2);
    S.snapshot = snap;
    S.lastRevision = snap.state_revision;
    S.error = null;
    if (!S.draftDirty) S.draft = null;
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
  renderCurrentWork(snap);
  renderDecision(snap);
  renderMatrix(snap);
  renderSubscription(snap);
  renderCatalog(snap);
  renderAuth(snap);
  renderQuota(snap);
  renderGoals(snap);
  renderEvents(snap);
  renderOverrides(snap);
  renderAccounts(snap);
  renderRouting(snap);
  renderRechecks(snap);
  $("raw").textContent = redactSnapshotText(JSON.stringify(snap, null, 2));
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
    live: ["pill-ok", "stream live · SSE"],
    connecting: ["pill-unknown", "stream connecting…"],
    reconnecting: ["pill-warn", "stream reconnecting…"],
    polling: ["pill-warn", "stream polling fallback"],
  };
  const [cls, label] = map[S.stream] || map.connecting;
  pill.className = `pill ${cls}`;
  pill.textContent = label;
}

function latestProviderObservation(snap) {
  let best = null;
  for (const c of snap.cells || []) {
    if (!c.observed_at || (c.evidence_source || "").includes("fixture")) continue;
    if (!best || c.observed_at > best.observed_at) best = c;
  }
  return best;
}

function renderVitals(snap) {
  const box = $("vitals");
  clear(box);
  const fresh = snap.freshness;
  const freshnessMs = fresh.browser_freshness_ms || fresh.browser_clock_ms;
  const evaluatedAge = Date.now() - snap.generated_at - S.serverSkewMs;
  const staleWindow = (freshnessMs || 0) * 2;
  const catalogAge = fresh.catalog_seen_at ? Date.now() - fresh.catalog_seen_at - S.serverSkewMs : null;
  const lastObs = latestProviderObservation(snap);

  const servicePill = { connected: "pill-ok", degraded: "pill-warn", offline: "pill-bad" }[snap.service.status] || "pill-unknown";
  const catalogPill = { fresh: "pill-ok", stale: "pill-warn", error: "pill-bad" }[fresh.catalog_status] || "pill-unknown";
  const artifactOk = !snap.service.artifact_digest || !snap.installed_artifact_digest
    || snap.service.artifact_digest === snap.installed_artifact_digest;
  const streamPill = { live: "pill-ok", polling: "pill-warn", reconnecting: "pill-warn" }[S.stream] || "pill-unknown";

  const items = [
    ["CMO service", snap.service.status, `pid ${snap.service.pid ?? "—"} · revision ${snap.state_revision}`, servicePill],
    ["Browser stream", S.stream === "live" ? "live" : S.stream, "browser transport only — not provider data", streamPill],
    ["Catalog", fresh.catalog_status || "unknown", `seen ${fmtAge(catalogAge)} · ${snap.catalog.length} model(s)`, catalogPill],
    ["Provider evidence", lastObs ? friendlyModel(lastObs.model) : "none current",
      lastObs ? `${lastObs.account_alias} · ${fmtAge(Date.now() - lastObs.observed_at - S.serverSkewMs)} · ${lastObs.state_label}` : "no current provider observation",
      lastObs ? "pill-ok" : "pill-warn"],
    ["Policy", `v${snap.policy.policyVersion}${snap.policy.saved ? ` (saved r${snap.policy.version})` : " (canonical)"}`, `owner ${snap.policy.owner} · PAYG ${snap.policy.neverPayg ? "impossible" : "POSSIBLE"}`, snap.policy.neverPayg ? "pill-ok" : "pill-bad"],
    ["Artifact", shortDigest(snap.service.artifact_digest), `installed ${shortDigest(snap.installed_artifact_digest)} · ${artifactOk ? "matches" : "MISMATCH"}`, artifactOk ? "pill-ok" : "pill-bad"],
  ];
  for (const [k, v, d, pill] of items) {
    const value = el("div", { class: "v" }, [String(v)]);
    if (pill) value.append(el("span", { class: `pill ${pill}` }, [" "]));
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
  } else if (!lastObs) {
    showBanner("No current provider evidence. Route states below are Unknown until a real probe or attempt records fresh evidence.", "warn");
  } else if (snap.data_quality && snap.data_quality.banner) {
    showBanner(snap.data_quality.banner, "warn");
  } else if (!S.error) {
    showBanner(null);
  }
}

function renderGoalSelect(snap) {
  const current = goalSelect.value;
  clear(goalSelect);
  goalSelect.append(el("option", { value: "" }, ["auto (most recent active)"]));
  for (const g of snap.goals) {
    const live = g.liveness === "disconnected" ? " (stale, no heartbeat)" : "";
    goalSelect.append(el("option", { value: g.goal_id },
      [`${g.display_status || g.status}: ${g.goal_id.slice(0, 22)}${g.goal_id.length > 22 ? "…" : ""}${live}`]));
  }
  // A stored selection for a Goal that no longer reports is dropped (and
  // forgotten) instead of lingering as a phantom selection.
  const wanted = current || S.goalId || "";
  const known = !wanted || snap.goals.some((g) => g.goal_id === wanted);
  goalSelect.value = known ? wanted : "";
  if (S.goalId !== goalSelect.value) {
    S.goalId = goalSelect.value;
    writeStored("cmo.goalId", S.goalId);
  }
}

function isLiveGoal(g) {
  if (!g) return false;
  if (g.liveness === "disconnected") return false;
  return !["completed", "failed", "cancelled"].includes(String(g.status || "").toLowerCase());
}

function renderCurrentWork(snap) {
  const box = $("current-work");
  clear(box);
  const live = (snap.goals || []).filter(isLiveGoal);
  if (!live.length) {
    box.append(el("p", { class: "hint", text: "No Pi Goal currently active." }));
    return;
  }
  box.append(el("ul", { class: "plain" }, live.map((g) => {
    const lastBeat = g.last_activity_at || g.updated_at || g.started_at;
    return el("li", {}, [
      el("div", {}, [
        el("strong", { text: friendlyModel(g.model || "") !== (g.model || "") ? `${friendlyModel(g.model)}` : (g.title || g.goal_id.slice(0, 18)) }),
        el("span", { class: `status-${g.status}`, text: ` · ${g.display_status || g.status}` }),
      ]),
      el("div", { class: "kv" }, [
        el("dt", { text: "project" }), el("dd", { text: g.repo || "—" }),
        el("dt", { text: "profile" }), el("dd", { text: `${g.account_alias || g.profile || "—"} · ${maskIdentity(g.session_ref || g.identity || "")}` }),
        el("dt", { text: "route" }), el("dd", { text: `${g.provider || "—"} / ${g.model || "—"} · ${g.free_only === false ? "subscription" : "free"}` }),
        el("dt", { text: "heartbeat" }), el("dd", { text: lastBeat ? `${fmtClock(lastBeat)} (${fmtAge(Date.now() - lastBeat - S.serverSkewMs)})` : "never" }),
        el("dt", { text: "goal" }), el("dd", { text: g.goal_id }),
      ]),
      g.objective ? el("div", { class: "meta", text: String(g.objective).slice(0, 220) }) : null,
    ]);
  })));
}

function accountOrder(snap) {
  const accounts = (snap.policy && snap.policy.accounts) || [];
  return [...accounts]
    .sort((a, b) => (a.priority || 0) - (b.priority || 0) || String(a.id).localeCompare(String(b.id)))
    .map((a) => a.id);
}

function cellFacts(cell) {
  return [
    `route ${cell.route_key}`,
    `state ${cell.state}${cell.stored_state !== cell.state ? ` (stored ${cell.stored_state})` : ""}`,
    `observed ${cell.observed_at_iso || "never"}`,
    cell.reset_after_ms ? `reset_after ${Math.round(cell.reset_after_ms / 1000)}s (${cell.reset_known ? "provider reported" : "reset unknown"})` : null,
    cell.reset_at_iso ? `reset at ${cell.reset_at_iso}` : null,
    cell.next_check_at_iso ? `next check ${cell.next_check_at_iso}` : null,
    cell.last_reason_code ? `reason ${cell.last_reason_code}` : null,
    cell.unknown_reason ? `why unknown: ${cell.unknown_reason}` : null,
    cell.recheck && cell.recheck.status ? `recheck ${cell.recheck.status}${cell.recheck.reason ? `: ${cell.recheck.reason}` : ""}` : null,
    cell.evidence_source ? `evidence source ${cell.evidence_source}` : null,
    cell.source_event_id ? `evidence event ${cell.source_event_id}` : null,
    cell.manual_override_reason ? `override: ${cell.manual_override_reason}` : null,
  ].filter(Boolean);
}

function cellNode(cell) {
  const summary = el("summary", { class: `cell cell-${cell.state}` }, [
    el("span", { class: "st", text: cell.state_label }),
    ageSpan(cell.age_ms, S.snapshot.generated_at),
    el("span", { class: "meta", text: ` · ${cell.freshness}` }),
  ]);
  if (cell.in_use) summary.append(el("span", { class: "badge badge-inuse", text: cell.in_use_liveness === "live" ? "IN USE" : "IN USE (stale)" }));
  if (cell.manual_override) summary.append(el("span", { class: "badge badge-override", text: "FORCE SKIP" }));
  if (cell.state === "STALE") summary.append(el("span", { class: "badge badge-stale", text: "STALE" }));
  if (cell.recheck && cell.recheck.status) {
    const label = cell.recheck.status === "running" ? "RECHECK RUNNING" : `RECHECK ${String(cell.recheck.status).toUpperCase()}`;
    summary.append(el("span", { class: "badge badge-recheck", text: label }));
  }
  const body = el("div", { class: "cell-detail" }, [
    el("div", { class: "meta", text: `canonical ${cell.route_key}` }),
    el("ul", { class: "plain" }, cellFacts(cell).slice(1).map((f) => el("li", { text: f }))),
    el("button", {
      type: "button", class: "btn btn-mini", title: "Request one bounded recheck of this leg",
      onclick: () => requestRecheck(cell),
    }, ["recheck"]),
  ]);
  if (cell.unknown_reason) body.prepend(el("div", { class: "unknown-why", text: cell.unknown_reason }));
  return el("details", { class: "cellbox" }, [summary, body]);
}

function renderMatrix(snap) {
  const table = $("free-matrix");
  clear(table);
  const order = accountOrder(snap);
  const accounts = [];
  for (const row of snap.free_matrix) {
    for (const a of order) {
      if (Object.prototype.hasOwnProperty.call(row.accounts, a) && !accounts.includes(a)) accounts.push(a);
    }
    for (const a of Object.keys(row.accounts)) if (!accounts.includes(a)) accounts.push(a);
  }
  const thead = el("thead", {}, [el("tr", {}, [
    el("th", { scope: "col", text: "Free model (saved order)" }),
    ...accounts.map((a) => el("th", { scope: "col", text: a })),
  ])]);
  const tbody = el("tbody");
  for (const row of snap.free_matrix) {
    tbody.append(el("tr", {}, [
      el("th", { scope: "row", text: `${friendlyModel(row.model)}\n${row.model}` }),
      ...accounts.map((a) => el("td", { "data-label": a }, [row.accounts[a] ? cellNode(row.accounts[a]) : el("span", { class: "meta", text: "not a configured leg" })])),
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
      el("td", { "data-label": "Provider", text: cell.provider }),
      el("td", { "data-label": "Account", text: cell.account_alias }),
      el("td", { "data-label": "Status" }, [cellNode(cell)]),
    ]));
  }
  table.append(thead, tbody);
}

function renderDecision(snap) {
  const box = $("decision");
  clear(box);
  const d = snap.decision;
  const selected = d.route || {};
  box.append(el("div", { class: `action action-${d.action}`, text: d.action || "UNKNOWN" }));
  box.append(el("div", { class: "route-chip", text: selected.route_key || (d.action === "BLOCKED" ? (d.reason || d.reason_code) : null) || "no eligible route" }));
  if (d.reason_code) box.append(el("div", { class: "meta", text: `reason ${d.reason_code}` }));
  if (d.reason && d.action !== "LAUNCH") box.append(el("div", { class: "meta", text: d.reason }));
  box.append(el("div", { class: "meta", text: `strategy ${d.strategy || "free-first"}${d.free_only ? " · free_only" : ""} · goal ${snap.selected_goal_id || "none"}` }));
  if (selected.account_alias) box.append(el("div", { class: "meta", text: `account ${selected.account_alias}` }));

  const kind = $("decision-kind");
  if (S.previewStrategy && S.previewStrategy !== (d.strategy || "free-first")) {
    kind.textContent = `Hypothetical preview for strategy “${S.previewStrategy}” — not the live route. Live routing follows the Goal strategy and saved policy.`;
  } else if (snap.selected_goal_id) {
    kind.textContent = "Live recommendation for the selected Goal.";
  } else {
    kind.textContent = "No Goal selected — showing the default free-first order. This is not a live recommendation.";
  }

  const skipped = $("skipped");
  clear(skipped);
  const list = d.skipped || [];
  if (!list.length) { skipped.append(el("p", { class: "hint", text: "No legs were skipped." })); return; }
  skipped.append(el("h3", { text: `Skipped legs (${list.length})` }));
  skipped.append(el("details", {}, [
    el("summary", { text: "Show skipped alternatives with evidence" }),
    el("ul", { class: "plain" }, list.map((s) => el("li", {}, [
      el("code", { text: s.route_key || "—" }),
      el("div", { class: "meta", text: `${s.reason_code || "unknown"}${s.detail ? " · " + s.detail : ""}` }),
    ]))),
  ]));
}

function renderCatalog(snap) {
  const box = $("catalog");
  clear(box);
  const f = snap.freshness;
  const dl = el("dl", { class: "kv" }, [
    el("dt", { text: "Status" }), el("dd", { text: f.catalog_status || "unknown" }),
    el("dt", { text: "Seen" }), el("dd", { text: fmtAge(f.catalog_seen_at ? Date.now() - f.catalog_seen_at - S.serverSkewMs : null) }),
    el("dt", { text: "Last error" }), el("dd", { text: f.catalog_last_error_at ? fmtClock(f.catalog_last_error_at) : "none recorded" }),
    el("dt", { text: "Published" }), el("dd", { text: `${snap.catalog.length} model(s)` }),
  ]);
  box.append(dl);
  if (snap.catalog.length) {
    box.append(el("ul", { class: "plain" }, snap.catalog.map((r) => el("li", {}, [
      el("code", { text: r.model }),
      el("span", { class: "meta", text: ` · ${r.capability || "unknown"}` }),
    ]))));
  } else {
    box.append(el("p", { class: "hint", text: "No catalog evidence recorded yet. Capability is unknown, not unavailable." }));
  }
}

function renderAuth(snap) {
  const box = $("auth");
  clear(box);
  const authCells = snap.cells.filter((c) => c.state === "AUTH_BLOCKED" || c.last_reason_code === "auth.required");
  const events = (snap.recent_events || []).filter((e) => e.reason_code === "auth.required" || e.event_type === "auth.required");
  if (!authCells.length && !events.length) {
    box.append(el("p", { class: "hint", text: "No sign-in failure recorded. Unreported sign-in state stays unknown for non-active accounts." }));
    return;
  }
  box.append(el("ul", { class: "plain" }, authCells.map((c) => el("li", {}, [
    el("code", { text: c.route_key }),
    el("div", { class: "meta", text: `${c.state_label} · observed ${c.observed_at_iso || "never"} · ${c.last_reason_code || ""}` }),
  ]))));
}

function renderQuota(snap) {
  const box = $("quota");
  clear(box);
  const rows = snap.cells.filter((c) => c.reset_after_ms || ["QUOTA", "QUOTA_EXPIRED"].includes(c.state));
  if (!rows.length) {
    box.append(el("p", { class: "hint", text: "No quota exhaustion evidence recorded." }));
    return;
  }
  const table = el("table", { class: "cards-table" }, [
    el("caption", { class: "sr-only", text: "Recorded quota evidence per route" }),
    el("thead", {}, [el("tr", {}, ["Route", "State", "Observed", "Reset", "Next check", "Reason"].map((h) => el("th", { scope: "col", text: h })))]),
    el("tbody", {}, rows.map((c) => el("tr", {}, [
      el("th", { scope: "row", text: c.route_key }),
      el("td", { "data-label": "State", text: c.state_label }),
      el("td", { "data-label": "Observed", text: c.observed_at_iso || "never" }),
      el("td", { "data-label": "Reset", text: c.reset_after_ms ? `${Math.round(c.reset_after_ms / 1000)}s (${c.reset_known ? "provider reported" : "reset unknown"})` : "unknown" }),
      el("td", { "data-label": "Next check", text: c.next_check_at_iso || "—" }),
      el("td", { "data-label": "Reason", text: c.last_reason_code || "—" }),
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
  box.append(el("ul", { class: "plain" }, snap.goals.map((g) => {
    const shown = g.display_status || g.status;
    const lastActivity = g.last_activity_at || g.updated_at || g.started_at;
    const items = [
      el("div", {}, [el("strong", { text: g.goal_id }), el("span", { class: `status-${g.status}`, text: ` · ${shown}` }),
        g.liveness === "disconnected" ? el("span", { class: "badge badge-stale", text: "NO HEARTBEAT" }) : null]),
      el("div", { class: "kv" }, [
        el("dt", { text: "repo" }), el("dd", { text: g.repo || "—" }),
        el("dt", { text: "strategy" }), el("dd", { text: `${g.strategy || "—"}${g.effective_strategy ? " → " + g.effective_strategy : ""}` }),
        el("dt", { text: "free_only" }), el("dd", { text: g.free_only ? "yes" : "no" }),
        el("dt", { text: "session" }), el("dd", { text: g.session_ref ? maskIdentity(g.session_ref) : "none recorded" }),
        el("dt", { text: "attempts" }), el("dd", { text: String(g.attempt_count || 0) }),
        el("dt", { text: "last activity" }), el("dd", { text: lastActivity ? `${fmtClock(lastActivity)} (${fmtAge(Date.now() - lastActivity - S.serverSkewMs)})` : "never" }),
        el("dt", { text: "reason" }), el("dd", { text: g.reason_code || "—" }),
      ]),
    ];
    if (g.objective) items.push(el("div", { class: "meta", text: String(g.objective).slice(0, 220) }));
    return el("li", {}, items);
  })));
}

function renderEvents(snap) {
  const table = $("events");
  clear(table);
  const rows = (snap.recent_events || []).filter((e) =>
    (!S.eventFilter.event_type || e.event_type === S.eventFilter.event_type) &&
    (!S.eventFilter.source || (e.source_component || "").includes(S.eventFilter.source)));
  const head = ["#", "occurred", "received", "type", "source", "route", "outcome / reason", "detail"];
  table.append(
    el("caption", { class: "sr-only", text: "Recent ingested events" }),
    el("thead", {}, [el("tr", {}, head.map((h) => el("th", { scope: "col", text: h })))]),
    el("tbody", {}, rows.map((e) => el("tr", {}, [
      el("td", { "data-label": "#", text: e.seq }),
      el("td", { "data-label": "occurred", text: e.occurred_iso || e.occurred_at }),
      el("td", { "data-label": "received", text: e.received_iso || e.received_at }),
      el("td", { "data-label": "type" }, [el("code", { text: e.event_type })]),
      el("td", { "data-label": "source", text: e.source_component || "—" }),
      el("td", { "data-label": "route", text: [e.account_alias, e.provider, e.model, e.tier].filter(Boolean).join(" / ") || "—" }),
      el("td", { "data-label": "outcome", text: e.reason_code || e.outcome || "—" }),
      el("td", { "data-label": "detail", text: e.safe_detail || "" }),
    ]))),
  );
}

function renderOverrides(snap) {
  const box = $("overrides");
  clear(box);
  const now = Date.now() - S.serverSkewMs;
  const active = (snap.overrides || []).filter((o) => !o.expires_at || o.expires_at > now);
  if (!active.length) { box.append(el("p", { class: "hint", text: "No active overrides." })); return; }
  box.append(el("ul", { class: "plain" }, active.map((o) => el("li", {}, [
    el("code", { text: o.route_key }),
    el("div", { class: "meta", text: `reason: ${o.reason || "—"} · expires ${o.expires_at ? fmtClock(o.expires_at) : "never"}` }),
    el("button", { type: "button", class: "btn btn-mini", onclick: () => removeOverride(o) }, ["Remove"]),
  ]))));
}

/* --------------------------------------------------------------- accounts */

const ACCOUNT_STATUS_LABEL = {
  tracked: "Tracked only — not route-ready",
  profile: "Profile created — sign-in not done",
  "signin-needed": "Sign-in needed",
  verified: "Verified",
  "auth-problem": "Auth problem",
  disabled: "Disabled",
};

function accountCells(snap, alias) {
  return (snap.cells || []).filter((c) => c.account_alias === alias);
}

function renderAccounts(snap) {
  const box = $("account-list");
  clear(box);
  const accounts = [...(snap.policy.accounts || [])]
    .sort((a, b) => (a.priority || 0) - (b.priority || 0) || String(a.id).localeCompare(String(b.id)));
  const attach = $("account-attach");
  if (attach && !attach.options.length) {
    for (const r of (snap.policy.routes || []).filter((r) => r.tier === "free")) {
      attach.append(el("option", { value: r.model }, [`${friendlyModel(r.model)} (${r.model})`]));
    }
  }
  if (!accounts.length) {
    box.append(el("p", { class: "hint", text: "No accounts registered." }));
    return;
  }
  const attempts = snap.attempts || [];
  box.append(el("ul", { class: "plain" }, accounts.map((a, idx) => {
    const cells = accountCells(snap, a.id);
    const routes = [...new Set(cells.map((c) => `${c.provider}/${c.model}`))];
    const quota = cells.filter((c) => ["QUOTA", "QUOTA_EXPIRED"].includes(c.state)).length;
    const authBad = cells.filter((c) => c.state === "AUTH_BLOCKED").length;
    const proven = cells
      .filter((c) => c.observed_at)
      .sort((x, y) => y.observed_at - x.observed_at)[0];
    const lastGoal = attempts.filter((t) => (t.account_alias || t.account) === a.id)[0];
    const statusLabel = a.enabled === false ? ACCOUNT_STATUS_LABEL.disabled
      : (ACCOUNT_STATUS_LABEL[a.status] || a.status || "Tracked only — not route-ready");
    const routeReady = a.enabled !== false && a.status === "verified";
    const items = [
      el("div", {}, [
        el("strong", { text: `${a.id}${a.label && a.label !== a.id ? ` · ${a.label}` : ""}` }),
        el("span", { class: a.enabled === false ? "status-blocked" : "status-active", text: ` · ${statusLabel}` }),
        routeReady ? null : el("span", { class: "badge badge-stale", text: "NOT ROUTE-READY" }),
      ]),
      el("div", { class: "kv" }, [
        el("dt", { text: "priority" }), el("dd", { text: String(a.priority ?? 0) }),
        el("dt", { text: "profile" }), el("dd", { text: a.profile || a.id }),
        el("dt", { text: "routes" }), el("dd", { text: routes.length ? routes.join(", ") : "none" }),
        el("dt", { text: "quota legs" }), el("dd", { text: String(quota) }),
        el("dt", { text: "auth-blocked" }), el("dd", { text: String(authBad) }),
        el("dt", { text: "last proof" }), el("dd", { text: proven ? `${friendlyModel(proven.model)} · ${fmtAge(Date.now() - proven.observed_at - S.serverSkewMs)}` : "never" }),
        el("dt", { text: "last goal" }), el("dd", { text: (lastGoal && (lastGoal.goal_id || lastGoal.goal)) || "none recorded" }),
      ]),
      el("div", { class: "formrow" }, [
        el("button", { type: "button", class: "btn btn-mini", disabled: idx === 0 ? "disabled" : null, title: "Move up in account priority", onclick: () => moveAccount(a.id, -1) }, ["↑ Up"]),
        el("button", { type: "button", class: "btn btn-mini", disabled: idx === accounts.length - 1 ? "disabled" : null, title: "Move down in account priority", onclick: () => moveAccount(a.id, 1) }, ["↓ Down"]),
        el("button", { type: "button", class: "btn btn-mini", onclick: () => toggleAccount(a) }, [a.enabled === false ? "Enable" : "Disable"]),
        a.status !== "verified" ? el("button", { type: "button", class: "btn btn-mini", title: "Verify profile without exposing credentials", onclick: () => verifyAccount(a.id) }, ["Verify"]) : null,
        el("button", { type: "button", class: "btn btn-mini btn-warn", onclick: () => removeAccount(a.id, false) }, ["Remove…"]),
      ]),
    ];
    return el("li", {}, items);
  })));
}

async function moveAccount(alias, dir) {
  const snap = S.snapshot;
  if (!snap) return;
  const ordered = [...(snap.policy.accounts || [])]
    .sort((a, b) => (a.priority || 0) - (b.priority || 0) || String(a.id).localeCompare(String(b.id)));
  const idx = ordered.findIndex((a) => a.id === alias);
  const other = ordered[idx + dir];
  if (!other) return;
  const myPriority = ordered[idx].priority ?? 0;
  const otherPriority = other.priority ?? 0;
  try {
    // Swap both priorities so the order visibly changes (server move sets
    // an absolute priority; a single write would only create a tie).
    await api("/api/accounts", { method: "POST", body: JSON.stringify({ op: "move", id: alias, priority: otherPriority, note: `dashboard reorder ${alias}` }) });
    await api("/api/accounts", { method: "POST", body: JSON.stringify({ op: "move", id: other.id, priority: myPriority, note: `dashboard reorder ${other.id}` }) });
    toast(`Account order updated: ${alias} swapped with ${other.id}. Applies to later decisions.`, "ok");
    await loadSnapshot();
  } catch (err) { toast(`Reorder failed: ${err.message}`, "bad"); }
}

async function toggleAccount(a) {
  try {
    await api("/api/accounts", { method: "POST", body: JSON.stringify({ op: "update", id: a.id, enabled: a.enabled === false, note: `dashboard ${a.enabled === false ? "enable" : "disable"} ${a.id}` }) });
    toast(`Account ${a.id} ${a.enabled === false ? "enabled" : "disabled"}. Running Goals keep their pinned route.`, "ok");
    await loadSnapshot();
  } catch (err) { toast(`Update failed: ${err.message}`, "bad"); }
}

async function verifyAccount(alias) {
  try {
    const res = await api("/api/accounts", { method: "POST", body: JSON.stringify({ op: "verify", id: alias }) });
    const check = res.check || res.result || {};
    toast(`Verify ${alias}: ${check.status || res.ok ? JSON.stringify(check) : "done"}.`, "ok");
    await loadSnapshot();
  } catch (err) { toast(`Verify failed: ${err.message}`, "bad"); }
}

async function removeAccount(alias, confirmedDetach) {
  const box = $("account-result");
  try {
    const res = await api("/api/accounts", {
      method: "POST",
      body: JSON.stringify({ op: "remove", id: alias, preview: !confirmedDetach, detach: !!confirmedDetach }),
    });
    if (res.preview) {
      clear(box);
      box.append(el("div", { class: "preview" }, [
        el("strong", { text: `Remove ${alias} affects ${res.attached_routes.length} route(s): ${res.attached_routes.join(", ") || "none"}.` }),
        res.requires_detach ? el("p", { class: "hint", text: "Detaching removes the alias from those routes. Running Goals keep their pinned route." }) : null,
        el("div", { class: "formrow" }, [
          el("button", { type: "button", class: "btn btn-warn", onclick: () => removeAccount(alias, true) }, ["Confirm remove with detach"]),
        ]),
      ]));
      return;
    }
    toast(`Account ${alias} removed. Receipt kept; rollback via policy versions.`, "ok");
    await loadSnapshot();
  } catch (err) { toast(`Remove failed: ${err.message}`, "bad"); }
}

/* ---------------------------------------------------------------- routing */

function draftDoc() {
  if (!S.draft && S.snapshot) {
    S.draft = JSON.parse(JSON.stringify({
      schema: S.snapshot.policy.schema,
      policyVersion: S.snapshot.policy.policyVersion,
      owner: S.snapshot.policy.owner,
      neverPayg: S.snapshot.policy.neverPayg,
      accounts: S.snapshot.policy.accounts,
      routes: S.snapshot.policy.routes,
      strategies: S.snapshot.policy.strategies,
      defaults: S.snapshot.policy.defaults,
      failureTaxonomy: S.snapshot.policy.failureTaxonomy,
    }));
  }
  return S.draft;
}

function markDirty() {
  S.draftDirty = true;
  $("draftbar").hidden = false;
}

function renderRouting(snap) {
  const doc = S.draftDirty ? S.draft : draftDoc();
  $("draftbar").hidden = !S.draftDirty;
  $("policy-meta").textContent = "";
  $("policy-meta").append(
    el("span", { class: "meta", text: `policy v${snap.policy.policyVersion} · saved r${snap.policy.version}${snap.policy.saved ? "" : " (canonical, never saved)"} · digest ${shortDigest(snap.policy.digest)}` }),
  );
  const free = (doc.routes || []).filter((r) => r.tier === "free");
  const sub = (doc.routes || []).filter((r) => r.tier !== "free");
  $("ladder-free").textContent = "";
  $("ladder-free").append(renderLadder(free, doc, "free"));
  $("ladder-sub").textContent = "";
  $("ladder-sub").append(renderLadder(sub, doc, "subscription"));
  renderAccountOrder(doc);
  const maxFree = $("max-free");
  if (document.activeElement !== maxFree) maxFree.value = (doc.defaults || {}).max_enabled_free_models ?? "";
  const sel = $("model-add-select");
  if (!sel.options.length) refreshModelOptions(snap);
  renderVersions();
}

function catalogCapability(snap, model) {
  const row = (snap.catalog || []).find((r) => r.model === model);
  return row ? (row.capability || "unknown") : "unknown";
}

function renderLadder(routes, doc, tier) {
  if (!routes.length) return el("p", { class: "hint", text: `No ${tier} routes in the draft.` });
  return el("ol", { class: "ladder" }, routes.map((r, idx) => el("li", {}, [
    el("div", {}, [
      el("strong", { text: friendlyModel(r.model) }),
      el("div", { class: "meta", text: `${r.model} · ${r.provider}/${r.tier}${r.enabled === false ? " · disabled" : ""}${r.pinned ? " · pinned" : ""} · capability ${catalogCapability(S.snapshot, r.model)}` }),
      el("div", { class: "meta", text: `accounts: ${(r.accounts || []).join(", ") || "none"}` }),
    ]),
    el("div", { class: "formrow" }, [
      el("button", { type: "button", class: "btn btn-mini", disabled: idx === 0 ? "disabled" : null, onclick: () => moveRoute(doc, r.model, -1) }, ["↑"]),
      el("button", { type: "button", class: "btn btn-mini", disabled: idx === routes.length - 1 ? "disabled" : null, onclick: () => moveRoute(doc, r.model, 1) }, ["↓"]),
      el("button", { type: "button", class: "btn btn-mini", onclick: () => toggleRoute(doc, r.model) }, [r.enabled === false ? "Enable" : "Disable"]),
      el("button", { type: "button", class: "btn btn-mini", onclick: () => pinRoute(doc, r.model) }, [r.pinned ? "Unpin" : "Pin"]),
      el("button", { type: "button", class: "btn btn-mini btn-warn", onclick: () => removeRoute(doc, r.model) }, ["Remove"]),
    ]),
  ])));
}

function moveRoute(doc, model, dir) {
  const tier = doc.routes.find((r) => r.model === model)?.tier;
  const idxs = doc.routes.map((r, i) => ({ r, i })).filter(({ r }) => r.tier === tier).map(({ i }) => i);
  const pos = idxs.findIndex((i) => doc.routes[i].model === model);
  const other = idxs[pos + dir];
  if (other === undefined) return;
  const cur = idxs[pos];
  const tmp = doc.routes[cur];
  doc.routes[cur] = doc.routes[other];
  doc.routes[other] = tmp;
  markDirty();
  renderRouting(S.snapshot);
}

function toggleRoute(doc, model) {
  const r = doc.routes.find((x) => x.model === model);
  if (r) { r.enabled = r.enabled === false; markDirty(); renderRouting(S.snapshot); }
}

function pinRoute(doc, model) {
  const r = doc.routes.find((x) => x.model === model);
  if (r) { r.pinned = !r.pinned; markDirty(); renderRouting(S.snapshot); }
}

function removeRoute(doc, model) {
  doc.routes = doc.routes.filter((x) => x.model !== model);
  markDirty();
  renderRouting(S.snapshot);
}

function renderAccountOrder(doc) {
  const box = $("account-order");
  clear(box);
  const ordered = [...(doc.accounts || [])]
    .sort((a, b) => (a.priority || 0) - (b.priority || 0) || String(a.id).localeCompare(String(b.id)));
  box.append(el("ol", { class: "ladder" }, ordered.map((a, idx) => el("li", {}, [
    el("div", {}, [
      el("strong", { text: a.id }),
      el("span", { class: "meta", text: ` · priority ${a.priority ?? 0}${a.enabled === false ? " · disabled" : ""}` }),
    ]),
    el("div", { class: "formrow" }, [
      el("button", { type: "button", class: "btn btn-mini", disabled: idx === 0 ? "disabled" : null, onclick: () => moveDraftAccount(doc, a.id, -1) }, ["↑"]),
      el("button", { type: "button", class: "btn btn-mini", disabled: idx === ordered.length - 1 ? "disabled" : null, onclick: () => moveDraftAccount(doc, a.id, 1) }, ["↓"]),
    ]),
  ]))));
}

function moveDraftAccount(doc, alias, dir) {
  const ordered = [...doc.accounts]
    .sort((a, b) => (a.priority || 0) - (b.priority || 0) || String(a.id).localeCompare(String(b.id)));
  const idx = ordered.findIndex((a) => a.id === alias);
  const other = ordered[idx + dir];
  if (!other) return;
  const a = doc.accounts.find((x) => x.id === alias);
  const b = doc.accounts.find((x) => x.id === other.id);
  const t = a.priority ?? 0;
  a.priority = b.priority ?? 0;
  b.priority = t;
  markDirty();
  renderRouting(S.snapshot);
}

function refreshModelOptions(snap) {
  const sel = $("model-add-select");
  clear(sel);
  const known = new Set((snap.policy.routes || []).map((r) => r.model));
  const age = snap.freshness.catalog_seen_at ? fmtAge(Date.now() - snap.freshness.catalog_seen_at - S.serverSkewMs) : "never";
  for (const row of snap.catalog || []) {
    if (known.has(row.model)) continue;
    sel.append(el("option", { value: row.model }, [`${friendlyModel(row.model)} · ${row.capability || "unknown"} · catalog ${age}`]));
  }
  if (!sel.options.length) sel.append(el("option", { value: "" }, ["No new catalog models available"]));
}

function expandDraft(doc) {
  const orderedAccounts = [...(doc.accounts || [])]
    .filter((a) => a.enabled !== false)
    .sort((a, b) => (a.priority || 0) - (b.priority || 0) || String(a.id).localeCompare(String(b.id)))
    .map((a) => a.id);
  const cap = (doc.defaults || {}).max_enabled_free_models;
  const free = (doc.routes || []).filter((r) => r.tier === "free" && r.enabled !== false);
  const limited = (typeof cap === "number" && cap > 0) ? free.slice(0, cap) : free;
  const seq = [];
  for (const r of limited) {
    for (const a of (r.accounts || []).filter((x) => orderedAccounts.includes(x))) {
      seq.push(`${a} / ${r.provider} / ${r.model}`);
    }
  }
  for (const r of (doc.routes || []).filter((r) => r.tier !== "free" && r.enabled !== false)) {
    for (const a of (r.accounts || []).filter((x) => orderedAccounts.includes(x))) {
      seq.push(`${a} / ${r.provider} / ${r.model} (subscription tail)`);
    }
  }
  return seq;
}

async function previewDraft() {
  const doc = draftDoc();
  const box = $("route-preview");
  clear(box);
  try {
    const res = await api("/api/policy", { method: "POST", body: JSON.stringify({ doc, dry_run: true }) });
    const seq = expandDraft(doc);
    box.append(el("div", { class: "preview" }, [
      el("strong", { text: `Draft route order (${seq.length} legs). Availability still applies from live evidence at decision time.` }),
      el("ol", { class: "plain" }, seq.map((s) => el("li", { text: s }))),
      res.catalog_unverified && res.catalog_unverified.length
        ? el("p", { class: "hint", text: `Catalog has not verified: ${res.catalog_unverified.join(", ")}` })
        : el("p", { class: "hint", text: "Draft validates. Save to version it." }),
    ]));
  } catch (err) { toast(`Draft invalid: ${err.message}`, "bad"); }
}

async function saveDraft() {
  const doc = draftDoc();
  const snap = S.snapshot;
  try {
    const res = await api("/api/policy", {
      method: "POST",
      body: JSON.stringify({ doc, note: "dashboard save", expected_digest: snap.policy.digest }),
    });
    S.draftDirty = false;
    S.draft = null;
    toast(`Policy saved as r${res.result.version} (${shortDigest(res.result.digest)}). Applies to later decisions; running Goals stay pinned.`, "ok");
    await loadSnapshot();
  } catch (err) {
    if (String(err.message).startsWith("409")) {
      toast("Policy changed elsewhere (conflict). Reloaded the current version — review and save again.", "bad");
    } else {
      toast(`Save failed: ${err.message}`, "bad");
    }
    await loadSnapshot();
  }
}

async function renderVersions() {
  try {
    const res = await api("/api/policy/versions");
    S.versions = res.versions || [];
  } catch (e) { S.versions = []; }
  const sel = $("rollback-select");
  const current = sel.value;
  clear(sel);
  for (const v of S.versions) {
    sel.append(el("option", { value: String(v.version) },
      [`r${v.version} · ${shortDigest(v.digest)}${v.active ? " (active)" : ""} · ${v.note || ""}`]));
  }
  if (current) sel.value = current;
}

/* --------------------------------------------------------------- rechecks */

function renderRechecks(snap) {
  const box = $("rechecks");
  clear(box);
  const rows = snap.recheck_requests || [];
  if (!rows.length) {
    box.append(el("p", { class: "hint", text: "No recheck requests recorded." }));
    return;
  }
  box.append(el("ul", { class: "plain" }, rows.map((r) => el("li", {}, [
    el("div", {}, [
      el("code", { text: r.route_key || "—" }),
      el("span", { class: r.status === "running" ? "badge badge-recheck" : (String(r.status || "").startsWith("finished") ? "status-completed" : "status-active"), text: ` · ${r.status || "pending"}` }),
    ]),
    el("div", { class: "meta", text: `requested ${r.requested_at ? fmtClock(r.requested_at) : "—"} by ${r.requested_by || "—"}${r.reason || r.last_error ? ` · ${r.reason || r.last_error}` : ""}${r.started_at ? ` · started ${fmtClock(r.started_at)}` : ""}${r.finished_at ? ` · settled ${fmtClock(r.finished_at)}` : ""}` }),
  ]))));
}

/* --------------------------------------------------------------- actions */

async function requestRecheck(cell) {
  try {
    const res = await api("/api/route/recheck", {
      method: "POST",
      body: JSON.stringify({ route_key: cell.route_key, account_alias: cell.account_alias, provider: cell.provider, model: cell.model, tier: cell.tier, reason: "bounded recheck from dashboard" }),
    });
    const id = (res.result && (res.result.request_id || res.result.event_id)) || res.request_id || "";
    toast(`Recheck queued for ${cell.route_key}${id ? ` (request ${id})` : ""}. Duplicate clicks are deduplicated.`, "ok");
    setTimeout(() => loadSnapshot(), 400);
    return res;
  } catch (err) { toast(`Recheck request failed: ${err.message}`, "bad"); }
}

async function refreshCatalog() {
  const btn = $("refresh-catalog");
  btn.disabled = true;
  btn.textContent = "Refreshing…";
  try {
    const res = await api("/api/catalog/refresh", { method: "POST", body: "{}" });
    toast(res.ok ? `Catalog refreshed: ${res.models} model(s).` : `Catalog refresh failed honestly: ${res.detail}`, res.ok ? "ok" : "bad");
    setTimeout(() => loadSnapshot(), 300);
  } catch (err) { toast(`Catalog refresh failed: ${err.message}`, "bad"); }
  finally { btn.disabled = false; btn.textContent = "Refresh catalog"; }
}

async function removeOverride(o) {
  try {
    await api(`/api/override/${encodeURIComponent(o.override_id || o.route_key)}`, { method: "DELETE" });
    await loadSnapshot();
  } catch (err) { toast(`Remove failed: ${err.message}`, "bad"); }
}

$("override-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = new FormData(ev.target);
  const payload = Object.fromEntries(form.entries());
  payload.reason = (payload.reason || "").trim();
  if (!payload.reason) { toast("A force-skip requires an explicit reason.", "bad"); return; }
  const parts = String(payload.route_key || "").split("|");
  if (parts.length !== 4) { toast("Route key must look like account|provider|model|tier.", "bad"); return; }
  payload.account_alias = payload.account_alias || parts[0];
  payload.provider = parts[1];
  payload.model = parts[2];
  payload.tier = parts[3];
  payload.expires_in_ms = Number(payload.expires_in_minutes || 30) * 60000;
  try {
    await api("/api/override", { method: "POST", body: JSON.stringify(payload) });
    ev.target.reset();
    await loadSnapshot();
  } catch (err) { toast(`Override failed: ${err.message}`, "bad"); }
});

$("account-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const submitter = ev.submitter && ev.submitter.name === "preview" ? "preview" : "add";
  const form = new FormData(ev.target);
  const attachSel = $("account-attach");
  const attach = [...attachSel.selectedOptions].map((o) => o.value).filter(Boolean);
  const payload = {
    op: "add",
    id: String(form.get("id") || "").trim(),
    label: String(form.get("label") || "").trim(),
    profile: String(form.get("profile") || "").trim() || String(form.get("id") || "").trim(),
    attach_routes: attach,
    note: "dashboard add-account",
  };
  const box = $("account-result");
  clear(box);
  try {
    if (submitter === "preview") {
      const res = await api("/api/accounts", { method: "POST", body: JSON.stringify({ ...payload, preview: true }) });
      box.append(el("div", { class: "preview" }, [
        el("strong", { text: `Preview: ${payload.id} would be added as tracked only.` }),
        el("ul", { class: "plain" }, (res.audits || []).map((a) => el("li", { text: `${a.event_type}: ${a.safe_detail || ""}` }))),
        el("p", { class: "hint", text: `Next: create the isolated Pi profile named “${payload.profile}” with your Pi profile helper, complete sign-in yourself, then Verify. CMO never accepts passwords or tokens.` }),
      ]));
      return;
    }
    const res = await api("/api/accounts", { method: "POST", body: JSON.stringify(payload) });
    void res;
    ev.target.reset();
    toast(`Account ${payload.id} added as tracked only — not route-ready until verified.`, "ok");
    await loadSnapshot();
  } catch (err) { toast(`Add account failed: ${err.message}`, "bad"); }
});

$("model-add-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const model = $("model-add-select").value;
  if (!model) { toast("No catalog model available to add.", "bad"); return; }
  const doc = draftDoc();
  const row = (S.snapshot.catalog || []).find((r) => r.model === model);
  doc.routes.push({ model, provider: "cline", tier: "free", accounts: [], enabled: true, pinned: false });
  void row;
  markDirty();
  renderRouting(S.snapshot);
  toast(`Draft: ${friendlyModel(model)} added (disabled until attached to accounts). Attach accounts via the policy JSON or account attach.`, "ok");
});

$("max-free").addEventListener("change", () => {
  const doc = draftDoc();
  const v = Number($("max-free").value);
  if (!Number.isInteger(v) || v < 1) { toast("Max enabled free models must be a positive integer.", "bad"); return; }
  doc.defaults = doc.defaults || {};
  doc.defaults.max_enabled_free_models = v;
  markDirty();
  renderRouting(S.snapshot);
});

$("draft-preview").addEventListener("click", previewDraft);
$("draft-save").addEventListener("click", saveDraft);
$("draft-discard").addEventListener("click", async () => {
  S.draft = null;
  S.draftDirty = false;
  await loadSnapshot();
  toast("Draft discarded.", "ok");
});

$("policy-reset").addEventListener("click", async () => {
  try {
    const canonical = await api("/api/policy/canonical");
    S.draft = canonical.doc || canonical.policy || canonical;
    markDirty();
    renderRouting(S.snapshot);
    toast("Draft reset to canonical defaults. Save to apply.", "ok");
  } catch (err) { toast(`Reset failed: ${err.message}`, "bad"); }
});

$("policy-rollback").addEventListener("click", async () => {
  const version = $("rollback-select").value;
  if (!version) { toast("No saved version to roll back to.", "bad"); return; }
  try {
    const res = await api("/api/policy/rollback", { method: "POST", body: JSON.stringify({ version: Number(version) }) });
    S.draft = null;
    S.draftDirty = false;
    toast(`Rolled back to r${version} as new r${res.result.version}.`, "ok");
    await loadSnapshot();
  } catch (err) { toast(`Rollback failed: ${err.message}`, "bad"); }
});

$("refresh").addEventListener("click", loadSnapshot);
goalSelect.addEventListener("change", () => { S.goalId = goalSelect.value; writeStored("cmo.goalId", S.goalId); loadSnapshot(); });
$("preview-strategy").addEventListener("change", () => { S.previewStrategy = $("preview-strategy").value; writeStored("cmo.previewStrategy", S.previewStrategy); loadSnapshot(); });
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

initViews();
if (S.previewStrategy) {
  const sel = $("preview-strategy");
  const known = [...sel.options].some((o) => o.value === S.previewStrategy);
  sel.value = known ? S.previewStrategy : "";
  if (!known) { S.previewStrategy = ""; writeStored("cmo.previewStrategy", ""); }
}
loadSnapshot();
connectStream();
