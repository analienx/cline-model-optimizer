/* Single-screen CMO enhancements. /advanced.js stays as the existing bundled asset URL. */
(() => {
  'use strict';
  if (typeof renderRouter !== 'function' || !document.getElementById('accounts')) return;
  const openAccounts = new Set();
  let showAllWork = false;
  const node = (tag, className = '', content) => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (content !== undefined) item.textContent = String(content);
    return item;
  };
  const displayTime = value => Number(value) > 0 ? new Date(Number(value)).toLocaleString() : 'No observation recorded';
  const routeFor = (snap, account, model) => (snap.policy?.routes || []).find(route =>
    route.model === model && route.tier === 'free' && route.enabled !== false && route.accounts?.includes(account.id));
  const evidenceFor = (snap, account, model) =>
    (snap.free_matrix || []).find(row => row.model === model)?.accounts?.[account.id];
  const requestFor = (account, route, cell) => ({
    route_key: cell?.route_key || `${account.id}|${route.provider}|${route.model}|free`,
    account_alias: account.id, provider: route.provider, model: route.model, tier: 'free'
  });
  function freeStatus(cell, ready) {
    if (!ready?.cline_saved) return ['Connect Cline Free to check this model', 'setup'];
    if (!cell) return ['Not checked for this account', 'check'];
    if (cell.state === 'AVAILABLE' && cell.freshness === 'fresh')
      return ['Recently worked · remaining free quota not reported', 'working'];
    if (cell.state === 'QUOTA' && (!cell.reset_at || Number(cell.reset_at) > Date.now()))
      return [cell.reset_at ? `Exhausted · reported reset ${displayTime(cell.reset_at)}` : 'Exhausted · reset time not reported', 'exhausted'];
    if (cell.state === 'AUTH_BLOCKED')
      return ['The last model check failed sign-in; Cline login is saved now. A new model check is needed.', 'check'];
    if (cell.state === 'CAPABILITY_UNAVAILABLE') return ['Provider did not offer this model', 'unavailable'];
    if (cell.state === 'QUOTA_EXPIRED' || cell.state === 'QUOTA')
      return ['Previous quota cooldown elapsed · recheck required', 'check'];
    if (cell.state === 'TRANSIENT') return ['Provider check failed temporarily · retry available', 'check'];
    if (cell.state === 'AVAILABLE' || cell.state === 'STALE')
      return ['Worked previously · availability must be checked again', 'check'];
    return ['No current availability evidence · check this free model', 'check'];
  }
  function enhanceRouter(snap) {
    const area = document.getElementById('router-evidence');
    if (area) {
      area.replaceChildren();
      const d = snap.decision || {}, route = d.route || {};
      const heading = node('strong', '', d.action === 'LAUNCH' && route.tier === 'free' ? 'A free route was recently verified' :
        d.action === 'PROBE' && route.tier === 'free' ? 'The next free route needs a fresh check' : 'No free route confirmed now');
      area.append(heading);
      area.append(node('p', 'router-evidence-explanation', route.tier === 'free' && route.model ?
        `${names(route.model)} · ${route.account_alias || 'account not reported'} · ${d.reason || 'No routing reason recorded'}` :
        (d.reason || 'A supervised free-model check is required before work can start.')));
      area.append(node('small', 'muted', 'Evidence is time-limited. An expired check does not prove a model is unavailable. No paid route is checked here.'));
    }
    const rows = [...document.querySelectorAll('#route-status .route-item')];
    const models = (snap.policy?.routes || []).filter(route => route.tier === 'free' && route.enabled !== false).slice(0, 4);
    const accounts = [...(snap.policy?.accounts || [])].filter(a => a.enabled !== false).sort((a,b) => (a.priority ?? 0) - (b.priority ?? 0));
    rows.forEach((row, index) => {
      const model = models[index]?.model;
      if (!model) return;
      const chips = row.querySelectorAll('.route-cell');
      accounts.filter(a => routeFor(snap, a, model)).forEach((account, j) => {
        const chip = chips[j], cell = evidenceFor(snap, account, model), ready = state.readiness[account.id];
        if (!chip || !cell) return;
        const [description, category] = freeStatus(cell, ready);
        chip.replaceChildren(node('span', '', `${account.id.replace('account-', 'Account ')} · ${description}`));
        chip.append(node('small', 'evidence-time', `Last model check: ${displayTime(cell.observed_at)}`));
        if (category === 'check' && ready?.cline_saved) {
          const route = routeFor(snap, account, model);
          if (route) chip.append(makeButton('Request free check', () => recheckRoute(requestFor(account, route, cell))));
        }
      });
    });
  }
  function enhanceAccounts(snap) {
    const accounts = [...(snap.policy?.accounts || [])].sort((a,b) => (a.priority ?? 0) - (b.priority ?? 0));
    [...document.querySelectorAll('#accounts .account-item')].forEach((row, index) => {
      const account = accounts[index], main = row.querySelector('.item-main');
      if (!account || !main) return;
      const detail = node('details', 'account-disclosure'), toggle = node('summary', '', 'Free models & plan usage');
      const body = node('div', 'account-expanded');
      detail.open = openAccounts.has(account.id) || state.onboardingHelp?.id === account.id && state.readiness[account.id]?.setup_running;
      detail.addEventListener('toggle', () => detail.open ? openAccounts.add(account.id) : openAccounts.delete(account.id));
      detail.append(toggle, body);
      const free = node('section', 'account-free-models');
      free.append(node('h3', '', 'Free model availability'));
      free.append(node('p', 'muted', 'Model checks show whether a free request worked or reached a quota limit. The provider does not report a remaining percentage per free model.'));
      const routes = (snap.policy?.routes || []).filter(r => r.tier === 'free' && r.enabled !== false && r.accounts?.includes(account.id));
      for (const route of routes) {
        const cell = evidenceFor(snap, account, route.model);
        const [description, category] = freeStatus(cell, state.readiness[account.id]);
        const model = node('div', `free-model-row ${category}`);
        model.append(node('strong', '', names(route.model)), node('p', '', description));
        if (category === 'exhausted') {
          const progress = node('progress'); progress.max = 100; progress.value = 100;
          progress.setAttribute('aria-label', `${names(route.model)}: reported quota exhausted`);
          model.append(progress);
        }
        if (cell?.observed_at) model.append(node('small', 'muted', `Checked ${displayTime(cell.observed_at)}`));
        if (category === 'check' && state.readiness[account.id]?.cline_saved)
          model.append(makeButton('Request free check', () => recheckRoute(requestFor(account, route, cell))));
        free.append(model);
      }
      if (!routes.length) free.append(node('p', 'muted', 'No free models assigned to this account.'));
      body.append(free);
      const usage = main.querySelector('.account-usage');
      if (usage) {
        usage.prepend(node('h3', '', 'Plan usage · account-wide'));
        const windows = state.usage[account.id]?.windows || {};
        if (state.usage[account.id]?.plan_status === 'active') {
          for (const [key, title] of [['5h','5-hour'], ['weekly','Weekly'], ['monthly','Monthly']]) {
            const percent = windows[key]?.percent_used;
            const rowForWindow = [...usage.querySelectorAll('.account-usage-window')].find(row => row.querySelector('strong')?.textContent === title);
            if (rowForWindow && typeof percent === 'number' && Number.isFinite(percent) && percent >= 0) {
              const progress = node('progress'); progress.max = 100; progress.value = Math.min(100, percent);
              progress.setAttribute('aria-label', `${title} plan usage: ${percent}% used`);
              rowForWindow.append(progress);
            }
          }
        }
      }
      while (main.childNodes.length > 2) body.append(main.childNodes[2]);
      for (const control of [...row.children]) if (control !== main && !control.classList.contains('account-avatar')) body.append(control);
      body.append(node('p', 'muted', 'A failed or stale model check is not a failed account. A requested free check runs when a supervised Pi worker is available; CMO does not continuously retry provider requests in the background.'));
      main.append(detail);
    });
  }
  function enhanceWork() {
    const host = document.getElementById('work');
    const rows = [...host.querySelectorAll('.work-item')];
    if (rows.length <= 5) return;
    rows.forEach((row, i) => {row.hidden = !showAllWork && i >= 5;});
    const action = makeButton(showAllWork ? 'Show first five' : `Show all ${rows.length} projects & sessions`, () => {
      showAllWork = !showAllWork; renderWork(state.snap);
    });
    action.classList.add('show-more-work'); action.setAttribute('aria-expanded', String(showAllWork));
    host.append(action);
  }
  const originalRouter = renderRouter, originalAccounts = renderAccounts, originalWork = renderWork;
  renderRouter = snap => { originalRouter(snap); enhanceRouter(snap); };
  renderAccounts = snap => { originalAccounts(snap); enhanceAccounts(snap); };
  renderWork = snap => { originalWork(snap); enhanceWork(); };
  if (state.snap) {renderRouter(state.snap); renderAccounts(state.snap); renderWork(state.snap);}
})();
