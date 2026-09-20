#!/usr/bin/env node
/*
 * Browser review: render the live dashboard in headless Chrome and assert the
 * required panels, evidence labels and accessibility affordances exist in the
 * real DOM after JavaScript runs. Writes the rendered HTML for audit.
 *
 * Usage: node review_ui.mjs --base http://127.0.0.1:4320 --out <dir>
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
const CDP_PORT = Number(args["cdp-port"] ?? 9334);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const REQUIRED_TEXT = {
  "service freshness panel": /Service & evidence freshness/,
  "route decision panel": /Next route/,
  "free matrix panel": /Free route status/,
  "subscription panel": /ClinePass subscription tail/,
  "catalog panel": /Catalog evidence/,
  "auth panel": /Auth evidence/,
  "quota panel": /Quota evidence/,
  "goals panel": /Goals/,
  "events panel": /Live event stream/,
  "overrides panel": /Bounded manual overrides/,
  "skip link (a11y)": /Skip to status content/,
  "table captions (a11y)": /Free route status per model and account/,
  "live region for stream": /aria-live="polite"/,
  "never-payg statement": /pay-as-you-go|PAYG/,
  "reset unknown honesty": /reset unknown/,
  "unknown legs stay unknown": /Unknown/,
  "verified evidence shown": /Recently verified/,
  "quota evidence shown": /Quota exhausted/,
  "in-use marker": /IN USE/,
  "manual override marker present": /Force skip leg/,
  "catalog capability caveat": /capability<\/em>? evidence only|capability evidence only/,
  "artifact identity shown": /artifact|Artifact/,
  "policy owner shown": /cline-model-optimizer/,
};

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "cmo-review-"));
  const chrome = spawn(CHROME, [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--disable-extensions", `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${profile}`, "--window-size=1500,2600", "about:blank",
  ], { stdio: "ignore" });

  let ws = null;
  const failures = [];
  try {
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      try { if ((await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`)).ok) break; } catch {}
      await sleep(200);
    }
    const target = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/new?${encodeURIComponent(BASE + "/")}`, { method: "PUT" })).json();
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener("open", res, { once: true });
      ws.addEventListener("error", rej, { once: true });
    });
    let id = 0;
    const pending = new Map();
    const handlers = new Map();
    ws.addEventListener("message", (evt) => {
      const msg = JSON.parse(evt.data);
      if (msg.id !== undefined && pending.has(msg.id)) {
        const { resolve, reject } = pending.get(msg.id);
        pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      } else if (msg.method) {
        for (const h of handlers.get(msg.method) ?? []) h(msg.params);
      }
    });
    const send = (method, params = {}) => {
      const mid = ++id;
      ws.send(JSON.stringify({ id: mid, method, params }));
      return new Promise((resolve, reject) => pending.set(mid, { resolve, reject }));
    };
    const on = (method, handler) => {
      if (!handlers.has(method)) handlers.set(method, []);
      handlers.get(method).push(handler);
      return () => {
        const list = handlers.get(method) ?? [];
        const i = list.indexOf(handler);
        if (i >= 0) list.splice(i, 1);
      };
    };
    const evaluate = async (expression) => {
      const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
      return r.result.value;
    };

    await send("Page.enable");
    await send("Runtime.enable");
    const loaded = new Promise((resolve) => { const off = on("Page.loadEventFired", () => { off(); resolve(); }); });
    await send("Page.navigate", { url: `${BASE}/` });
    await loaded;
    for (let i = 0; i < 60; i += 1) {
      const ok = await evaluate("!!(window.__cmoSnapshot && window.__cmoSnapshot.cells)");
      if (ok) break;
      await sleep(250);
    }
    await sleep(1200);

    const html = await evaluate("document.documentElement.outerHTML");
    fs.writeFileSync(path.join(OUT, "dom-review.html"), html);
    const text = await evaluate("document.body.innerText");
    const missing = [];
    for (const [name, pattern] of Object.entries(REQUIRED_TEXT)) {
      if (!pattern.test(html) && !pattern.test(text)) missing.push(name);
    }
    if (missing.length) failures.push(...missing.map(m => `missing in rendered DOM: ${m}`));

    // Accessibility/structure assertions on real nodes.
    const structure = await evaluate(`(() => {
      const q = (s) => document.querySelectorAll(s).length;
      const controls = [...document.querySelectorAll('button, select, input')];
      const unlabelled = controls.filter(c => !c.getAttribute('aria-label') && !c.id && !c.closest('label') && !c.textContent.trim());
      return {
        headings: [...document.querySelectorAll('h1,h2,h3')].map(h => h.textContent.trim()),
        tables: q('table'), captions: q('caption'), thScopes: q('th[scope]'),
        buttons: q('button'), selects: q('select'), unlabelledControls: unlabelled.length,
        liveRegions: q('[aria-live]'), skipLinks: q('a.skip'),
        title: document.title, lang: document.documentElement.lang,
        stream: document.getElementById('stream')?.textContent,
        stateRevision: window.__cmoSnapshot?.state_revision,
        catalogModels: window.__cmoSnapshot?.catalog?.length ?? 0,
        modelOptions: document.getElementById("model-add-select")?.options.length ?? 0,
      };
    })()`);
    if (structure.catalogModels < 1) failures.push("provider catalog is empty in live browser");
    if (structure.catalogModels > 0 && structure.modelOptions < 1) failures.push("catalog populated but Add Model selector is empty");
    if (structure.unlabelledControls > 0) failures.push(`${structure.unlabelledControls} control(s) lack an accessible name`);
    if (structure.captions < 3) failures.push("expected at least 3 table captions");
    if (structure.skipLinks < 1) failures.push("missing skip link");
    if (!structure.lang) failures.push("html lang attribute missing");

    const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
    fs.writeFileSync(path.join(OUT, "dashboard-review.png"), Buffer.from(shot.data, "base64"));

    const report = { schema: "cmo.ui-review/v1", ok: failures.length === 0, base: BASE, structure, missing, failures };
    fs.writeFileSync(path.join(OUT, "ui-review.json"), JSON.stringify(report, null, 2) + "\n");
    console.log(JSON.stringify({ verdict: failures.length === 0 ? "PASS" : "FAIL", failures, structure }, null, 2));
    process.exitCode = failures.length === 0 ? 0 : 1;
  } catch (err) {
    console.log(JSON.stringify({ verdict: "ERROR", error: String(err && err.stack || err) }));
    process.exitCode = 2;
  } finally {
    try { ws && ws.close(); } catch {}
    chrome.kill();
  }
}

main();
