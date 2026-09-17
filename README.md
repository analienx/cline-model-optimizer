# Cline Model Optimizer

**Free-tier-first model routing, usage visibility and a dark control dashboard for the
[Cline](https://github.com/cline/cline) VS Code extension on Windows.**

Cline gives you free-tier models (GLM Flash, DeepSeek Flash, Cline Free models...), a
Cline Pass subscription tier, and pay-as-you-go providers. In practice it is easy to end
up burning subscription credits or paid tokens on tasks that a free model could handle -
and hard to see which model, tier and account was actually used.

This tool watches Cline's own data store and gives you:

- **Dynamic free-first strategy** - reads Cline's live free-model catalog each cycle and
  picks your preferred free models automatically (top 1, 2, 3... - configurable), then
  Cline Pass subscription as the only explicit fallback - never pay-as-you-go
- **Full visibility** - which model is active in Plan/Act mode, its tier
  (FREE / SUBSCRIPTION / PAID), which account, reasoning effort, auth-token expiry
- **Usage accounting** - minutes spent per tier per day, per-session model/cost history
- **Advisory guardian** - toast notifications (rate-limited) when a paid or subscription
  model is in use while free alternatives exist, and when Cline auth is about to expire
- **One-click model apply** - safely rewrites Cline's active model config (with backup,
  only while VS Code is closed)

Sister project of
[remote-desktop-commander-agent-control](https://github.com/analienx/remote-desktop-commander-agent-control)
- same design language, same guardian/dashboard pattern.

## Requirements

- Windows 10/11, PowerShell 5.1+ (no admin needed, everything per-user)
- [Cline](https://marketplace.visualstudio.com/items?itemName=saoudrizwan.claude-dev) 4.x
  installed in VS Code and opened at least once (creates `~\.cline\data`)

## Quick start

```powershell
irm https://raw.githubusercontent.com/analienx/cline-model-optimizer/main/install.ps1 | iex
```

or

```powershell
git clone https://github.com/analienx/cline-model-optimizer.git
cd cline-model-optimizer
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer registers a per-user scheduled task (**ClineModelOptimizer-Guardian**,
logon + every 5 min), creates a **Cline Model Optimizer** desktop shortcut, and runs the
first check.

## How the dynamic free-first ladder works

`src/model-routing.json` controls everything:

```json
"freeSelection": {
  "maxFreeModels": 2,
  "preferredFreeModels": [
    "cline-free/muse-spark-1.3-contributor",
    "z-ai/glm-5.3-flash",
    "cline-free/deepseek-v4.1-flash"
  ],
  "dynamicSource": "cline-cache",
  "fillFromDynamic": true,
  "subscriptionSteps": [...]
}
```

At runtime the ladder is resolved from Cline's own cached catalog
(`~\.cline\data\cache\cline_recommended_models.json`, which the Cline extension
refreshes itself):

1. Your **preferred free models** that are currently in the dynamic free list -
   up to **`maxFreeModels`** (1, 2, 3...) in your preferred order
2. If `fillFromDynamic` is on: remaining **dynamic free models** fill the rest
3. If the dynamic list is unavailable: static `preferredFreeModels` fallback
4. **subscriptionSteps** (Cline Pass) as explicit fallback
5. Everything else (non-Cline providers) counts as PAID

Change `maxFreeModels` to 1/2/3 to decide how many free models participate before the
subscription tier kicks in. Reorder `preferredFreeModels` to rank them. The dashboard
shows the resolved ladder live, including each step's source.

## Canonical routing (Agent Foundry v3)

This repo is the **sole routing-policy owner** for Agent Foundry v3
([issue #1](https://github.com/analienx/cline-model-optimizer/issues/1),
see [`docs/ROUTING-OWNERSHIP.md`](docs/ROUTING-OWNERSHIP.md)).

Canonical free order (model-major across Pi accounts 1,2,3):
**Muse Spark 1.3 free** (`cline-free/muse-spark-1.3-contributor`) x pi-1,2,3,
then **GLM-5.3 Flash free** (`z-ai/glm-5.3-flash`) x pi-1,2,3,
then **DeepSeek V4.1 Flash free** (`cline-free/deepseek-v4.1-flash`) x pi-1,2,3,
then **Cline Pass subscription only** - never pay-as-you-go.

Machine-readable policy: [`src/foundry-route-policy.json`](src/foundry-route-policy.json)
(pin `policyVersion`, validate with `Test-CmoFoundryRoutePolicy` in
[`src/CmoFoundryRoute.ps1`](src/CmoFoundryRoute.ps1)).

Capability precision: **Pi launchers may rotate isolated account profiles**
(pi-1/pi-2/pi-3), while **the Cline VS Code extension exposes only the active
login** - this tool never claims automatic switching of unavailable Cline
credentials (other Cline logins are advisory until signed in).

## Daily use

| Want to... | Do this |
|---|---|
| See what's running / which tier / which account | Double-click **Cline Model Optimizer** |
| Force a check | Dashboard → **Run guardian now** |
| Switch Plan+Act to the top free model | Dashboard → **Apply preferred** (close VS Code first) |
| Tune the strategy | Edit `%LOCALAPPDATA%\ClineModelOptimizer\model-routing.json` |
| See history | Dashboard recent-sessions; logs in `%LOCALAPPDATA%\ClineModelOptimizer` |
| Remove everything | `powershell -File .\uninstall.ps1` |

## Safety model

- Cline's state is read **read-only** by the guardian - it never modifies anything
- **Apply** is the only writer: refuses to run while VS Code is open (the extension
  would overwrite from memory), backs up `globalState.json` + `providers.json` first
- **Secrets are never touched**: `secrets.json` and auth tokens are never read, logged,
  or displayed - only account email/id and token *expiry* are surfaced
- Toasts are rate-limited (2 h paid-in-use, 6 h auth) - no nagging
- Exit codes: `0` optimal-free · `1` free-but-suboptimal · `2` subscription/paid-observed (never routed to) ·
  `3` auth-warning · `4` no Cline state

## Dashboard

Dark themed (Catppuccin-inspired, matching RDC Agent Control):

- **ACTIVE MODEL** - Plan/Act provider, model, tier badge, reasoning effort, verdict,
  token expiry countdown, today's tier-usage minutes
- **ACCOUNTS** - signed-in provider accounts (email, last-used marker)
- **PREFERRED LADDER** - resolved dynamic ladder with per-step tier + source
- **RECENT SESSIONS** - per-session model, tier and title history

## Troubleshooting

**Dashboard says `CLINE STATE NOT FOUND`** - open Cline in VS Code once so it creates
`~\.cline\data`, then hit Refresh.

**`Apply preferred` refuses** - VS Code is running. Close it and retry (by design; the
Cline extension rewrites its config from memory on exit).

**Toasts about a paid model right after you deliberately chose one** - expected; either
ignore (rate-limited) or add that model to the ladder so it counts as preferred.

## License

[MIT](LICENSE) © 2026 analienx
