#!/usr/bin/env node
/*
 * Independent browser acceptance proof: a real event ingested over HTTP must
 * appear as the exact route projection in a live headless Chrome page within
 * the 2s SSE latency budget, and the page must keep its SSE stream open.
 *
 * Usage: node prove_live_update.mjs --base http://127.0.0.1:4319 \
 *          --chrome "<path to chrome.exe>" --out <artifacts dir>
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, value, i, all) => {
  if (value.startsWith("--")) acc.push([value.slice(2), all[i + 1]]);
  return acc;
}, []));

const BASE = args.base ?? "http://127.0.0.1:4311";
const CHROME = args.chrome ?? "C:/Program Files/Google/Chrome/Application/chrome.exe";
const OUT = args.out ?? path.join(process.cwd(), "artifacts", "browser");
const CDP_PORT = Number(args["cdp-port"] ?? 9333);
const ROUTE_KEY = args["route-key"]
  ?? "account-2|cline|cline-free/muse-spark-1.3-contributor|free";
const BUDGET_MS = Number(args["budget-ms"] ?? 2000);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (msg, extra = {}) => console.log(JSON.stringify({ step: msg, ...extra }));

async function waitFor(fn, { timeoutMs = 20000, intervalMs = 100, label = "condition" } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const value = await fn();
      if (value) return value;
    } catch (err) { lastError = err; }
    await sleep(intervalMs);
  }
  throw new Error(`timed out waiting for ${label}${lastError ? `: ${lastError.message}` : ""}`);
}

class Cdp {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.handlers = new Map(); }
  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", (e) => reject(new Error(`websocket error: ${e.message ?? "unknown"}`)), { once: true });
    });
    const cdp = new Cdp(ws);
    ws.addEventListener("message", (evt) => {
      const msg = JSON.parse(evt.data);
      if (msg.id !== undefined && cdp.pending.has(msg.id)) {
        const { resolve, reject } = cdp.pending.get(msg.id);
        cdp.pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
      } else if (msg.method) {
        for (const handler of cdp.handlers.get(msg.method) ?? []) handler(msg.params);
      }
    });
    return cdp;
  }
  on(method, handler) {
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(handler);
    return () => {
      const list = this.handlers.get(method) ?? [];
      const i = list.indexOf(handler);
      if (i >= 0) list.splice(i, 1);
    };
  }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }
  async eval(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression, returnByValue: true, awaitPromise: true,
    });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
    return result.result.value;
  }
  close() { try { this.ws.close(); } catch {} }
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "cmo-chrome-"));
  const chrome = spawn(CHROME, [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--disable-extensions", "--hide-scrollbars", `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${profile}`, "--window-size=1500,2400", "about:blank",
  ], { stdio: ["ignore", "pipe", "pipe"] });
  let chromeLog = "";
  chrome.stdout.on("data", (d) => { chromeLog += d.toString(); });
  chrome.stderr.on("data", (d) => { chromeLog += d.toString(); });

  const failures = [];
  let cdp = null;
  try {
    await waitFor(async () => (await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`)).ok,
      { label: "chrome devtools endpoint", timeoutMs: 30000 });
    log("chrome-ready");

    const target = await (await fetch(
      `http://127.0.0.1:${CDP_PORT}/json/new?${encodeURIComponent(`${BASE}/`)}`,
      { method: "PUT" })).json();
    cdp = await Cdp.connect(target.webSocketDebuggerUrl);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Network.enable");

    const sseRequests = [];
    cdp.on("Network.responseReceived", (p) => {
      if (p.type === "Eventsource" || (p.response?.url ?? "").includes("/api/stream")) {
        sseRequests.push({ url: p.response.url, status: p.response.status, type: p.type });
      }
    });
    const consoleErrors = [];
    cdp.on("Runtime.consoleAPICalled", (p) => {
      if (p.type === "error") consoleErrors.push(p.args.map(a => a.value ?? a.description).join(" "));
    });
    cdp.on("Runtime.exceptionThrown", (p) => consoleErrors.push(p.exceptionDetails?.text ?? "exception"));

    const loaded = new Promise((resolve) => {
      const off = cdp.on("Page.loadEventFired", () => { off(); resolve(); });
    });
    await cdp.send("Page.navigate", { url: `${BASE}/` });
    await loaded;
    log("page-loaded", { url: `${BASE}/` });

    const ready = await waitFor(async () => {
      const value = await cdp.eval(`(() => {
        const s = window.__cmoSnapshot;
        return s && s.cells ? { revision: s.state_revision, stream: window.__cmoState ? window.__cmoState.stream : null } : null;
      })()`);
      return value;
    }, { label: "dashboard snapshot in browser", timeoutMs: 20000 });
    log("snapshot-in-browser", ready);

    const before = await cdp.eval(`(() => {
      const s = window.__cmoSnapshot;
      const c = s.cells.find(x => x.route_key === ${JSON.stringify(ROUTE_KEY)});
      return { state: c ? c.state : null, stored: c ? c.stored_state : null, revision: s.state_revision };
    })()`);
    log("before-event", before);
    if (!before || before.state === null) failures.push(`route ${ROUTE_KEY} missing from browser projection`);
    if (before && before.state === "AVAILABLE") failures.push(`route ${ROUTE_KEY} was already AVAILABLE before the event`);

    const occurredAt = Date.now();
    const event = {
      event_type: "route.probe.succeeded",
      occurred_at: occurredAt,
      source_component: "browser-acceptance",
      reason_code: "probe.ok",
      safe_detail: "independent browser acceptance probe (synthesized evidence)",
      account_alias: "account-2",
      provider: "cline",
      model: "cline-free/muse-spark-1.3-contributor",
      tier: "free",
      event_id: `browser-acceptance-${occurredAt}`,
    };
    const post = await fetch(`${BASE}/api/events`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(event),
    });
    if (!post.ok) failures.push(`event ingest failed: ${post.status} ${await post.text()}`);
    const postBody = await post.json();
    log("event-ingested", { revision: postBody.state_revision, result: postBody.result });

    const t1 = Date.now();
    let after = null;
    try {
      after = await waitFor(async () => {
        const value = await cdp.eval(`(() => {
          const s = window.__cmoSnapshot;
          const c = s.cells.find(x => x.route_key === ${JSON.stringify(ROUTE_KEY)});
          return c && c.state === "AVAILABLE" ? { state: c.state, revision: s.state_revision,
            age_ms: c.age_ms, observed: c.observed_at_iso, label: c.state_label } : null;
        })()`);
        return value;
      }, { label: "route projection update in browser", timeoutMs: BUDGET_MS + 3000, intervalMs: 100 });
    } catch (err) {
      failures.push(err.message);
    }
    const latencyMs = Date.now() - t1;
    log("after-event", { latencyMs, after });

    if (after) {
      if (after.revision !== postBody.state_revision) {
        failures.push(`revision mismatch: browser ${after.revision} vs api ${postBody.state_revision}`);
      }
      if (latencyMs > BUDGET_MS) failures.push(`browser update latency ${latencyMs}ms exceeded ${BUDGET_MS}ms budget`);
    }

    const streamState = await cdp.eval("window.__cmoState ? window.__cmoState.stream : null");
    if (streamState !== "live") failures.push(`SSE stream state was ${streamState}, expected live`);
    if (!sseRequests.length) failures.push("no /api/stream event-source request observed by CDP");
    log("sse", { streamState, sseRequests });

    // Prove the rendered table also shows the same evidence (not just the JS state).
    const rendered = await cdp.eval(`(() => {
      const t = document.getElementById('free-matrix');
      return t ? t.innerText.replace(/\\s+/g, ' ').slice(0, 900) : null;
    })()`);
    if (!rendered || !/Recently verified/i.test(rendered)) {
      failures.push("rendered free matrix does not show a verified leg");
    }

    // Prove the decision panel renders the leg the engine actually chose
    // (regression: it used to read phantom decision.selected/blocked_reason
    // keys and always showed "no eligible route").
    const decisionPanel = await cdp.eval(`(() => {
      const box = document.getElementById('decision');
      const s = window.__cmoSnapshot;
      return { text: box ? box.innerText.replace(/\\s+/g, ' ').slice(0, 600) : null,
               action: s && s.decision ? s.decision.action : null,
               route: s && s.decision && s.decision.route ? s.decision.route.route_key : null };
    })()`);
    log("decision-panel", decisionPanel);
    if (decisionPanel && decisionPanel.route) {
      if (!decisionPanel.text || !decisionPanel.text.includes(decisionPanel.route)) {
        failures.push(`rendered decision panel does not show the ${decisionPanel.action} route ${decisionPanel.route}`);
      }
    } else if (decisionPanel && decisionPanel.action === "BLOCKED" &&
               (!decisionPanel.text || !/no eligible route/i.test(decisionPanel.text))) {
      failures.push("rendered decision panel on BLOCKED does not explain the block");
    }

    const shot = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
    const shotPath = path.join(OUT, "dashboard-live.png");
    fs.writeFileSync(shotPath, Buffer.from(shot.data, "base64"));
    log("screenshot", { path: shotPath });

    const evidence = {
      schema: "cmo.browser-acceptance/v1",
      ok: failures.length === 0,
      base: BASE,
      route_key: ROUTE_KEY,
      budget_ms: BUDGET_MS,
      latency_ms: latencyMs,
      before, after, api_revision: postBody.state_revision,
      stream_state: streamState,
      sse_requests: sseRequests,
      console_errors: consoleErrors,
      rendered_matrix_excerpt: rendered,
      decision_panel: decisionPanel,
      screenshot: shotPath,
      failures,
    };
    fs.writeFileSync(path.join(OUT, "browser-acceptance.json"),
      JSON.stringify(evidence, null, 2) + "\n");
    console.log(JSON.stringify({ verdict: failures.length === 0 ? "PASS" : "FAIL", failures, latency_ms: latencyMs }));
    process.exitCode = failures.length === 0 ? 0 : 1;
  } catch (err) {
    console.log(JSON.stringify({ verdict: "ERROR", error: String(err && err.stack || err), chrome_log_tail: chromeLog.slice(-1500) }));
    process.exitCode = 2;
  } finally {
    if (cdp) cdp.close();
    chrome.kill();
  }
}

main();
