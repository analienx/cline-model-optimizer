# ROUTING-OWNERSHIP.md - sole routing-policy owner for Agent Foundry v3.

`cline-model-optimizer` is the **sole model/account routing-policy owner** for
Agent Foundry v3 (implements `cline-model-optimizer` issue #1, the routing-owner
portion of `analienx/config#36`). Nothing else defines a competing
model/account order.

## Canonical route (pinned)

Machine-readable policy: [`../src/foundry-route-policy.json`](../src/foundry-route-policy.json)
(`schema: foundry-route-policy/v1`, `policyVersion: 1.0.1`).
PowerShell API: [`../src/CmoFoundryRoute.ps1`](../src/CmoFoundryRoute.ps1).

Order rule is **model-major**: each free model runs across Pi accounts
1,2,3 before the ladder advances:

1. `cline-free/muse-spark-1.3-contributor` (FREE) x `account-1,account-2,account-3`
2. `z-ai/glm-5.3-flash` (FREE) x `account-1,account-2,account-3`
3. `cline-free/deepseek-v4.1-flash` (FREE) x `account-1,account-2,account-3`
4. ClinePass subscription tail only:
   `cline-pass/glm-5.3-flash`, `cline-pass/deepseek-v4.1-flash`
5. **Never pay-as-you-go.** `neverPayg: true`; `allowedTiers` is
   `FREE, SUBSCRIPTION`. A fully exhausted queue resolves to `$null`
   (stop), never to a paid leg.

## Ownership boundary

| Role | Owns | Must NOT |
|---|---|---|
| `cline-model-optimizer` (this repo) | Canonical route order, alias corrections, failure taxonomy, `Test-CmoFoundryRoutePolicy` validation | Read/commit/expose tokens or secrets |
| Agent Foundry v3 launchers | Pin `policyVersion` (+ optional sha256), call `Test-CmoFoundryRoutePolicy` at startup, enumerate legs via `Get-CmoFoundryRouteQueue`, resolve via `Resolve-CmoFoundryNextStep` | Define their own model/account order; route to paid legs |
| Interop gateway | Forward route decisions unchanged | Reorder, inject, or skip legs |

## Capability precision (Pi vs Cline)

- **Pi CAN rotate isolated account profiles** (`account-1/account-2/account-3`): each
  profile carries its own credentials and budget, so the 9-leg free queue
  is executable on the Pi side.
- **The Cline VS Code extension exposes only the ACTIVE login** on disk
  (`settings/providers.json` holds one signed-in login). This tool therefore
  **never claims automatic switching of unavailable Cline credentials**:
  Cline-side legs for other logins are advisory (`needs-signin` /
  "sign in to unlock") until that login authenticates on the machine with
  VS Code closed. Local Cline writes (model apply) require VS Code closed
  with backup, and `secrets.json` plus auth tokens are never read, logged,
  committed, or displayed by the routing policy layer.

## Stale aliases corrected

- `deepseek/deepseek-v4-flash` -> `cline-free/deepseek-v4.1-flash`
  (missing minor version; old string still matches via core id).
- `cline-pass/deepseek-v4-flash` -> `cline-pass/deepseek-v4.1-flash`.
- `cline-free/glm-5.3-flash` (assumed rewrite) -> `z-ai/glm-5.3-flash`
  (GLM Flash free id rides the `cline` provider with the `z-ai/` prefix).
- `muse-spark-1.3` (bare) -> `cline-free/muse-spark-1.3-contributor`.
- See `Resolve-CmoFoundryModelId` and `staleAliasesCorrected` in the policy.

## Failure taxonomy

- **quota** (`INFERENCE_CAP_ERROR`, `status code 429`, `quota exceeded`,
  `resource exhausted`, `cline_free_promotion_ended`): budget spent -
  advance the queue (next account, then next model). See `Get-CmoFailureKind`.
- **auth** (`401/403`, `unauthorized`, `token expired`, `re-sign in`):
  credential problem - do NOT mark the leg spent; surface re-sign-in.
- **transient** (`timeout`, `connection`, `502/503/504`, `fetch failed`):
  retry the SAME leg with backoff; do not advance the queue.
- Unknown errors resolve to `unknown` (retry once, then surface).

## Pinning / validation (for launchers)

```powershell
. ./src/CmoFoundryRoute.ps1
$policy = Get-CmoFoundryRoutePolicy
$check  = Test-CmoFoundryRoutePolicy -Policy $policy
if (-not $check.Valid) { throw ($check.Errors -join '; ') }
$queue = Get-CmoFoundryRouteQueue -Policy $policy
$next  = Resolve-CmoFoundryNextStep -Policy $policy -ExhaustedLegs @()
```

Pin `policyVersion` (and sha256 of `foundry-route-policy.json`) in launcher
config; reject any local config whose free order differs, that contains a
paid leg, or with `neverPayg -ne $true`.

## Tests

`../tests/Test-CmoFoundryRoute.ps1` covers: model-first account order,
exhaustion (account rotation before model advance), subscription fallback,
no-PAYG invariant, alias correction, failure taxonomy, and
`model-routing.json` drift. No network, no Cline state, no secrets.

## Remaining external limitation

Cline publishes no quota-remaining API and persists only the active login,
so Cline-side per-account budget for non-active logins stays `unknown`
until that login signs in; Pi-side rotation across `account-1/account-2/account-3` is the
executable path for the full 9-leg queue.
