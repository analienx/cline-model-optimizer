# CmoFoundryRoute.ps1 - canonical Foundry v3 route policy API (sole owner: cline-model-optimizer).
#
# Pi launchers pin or validate src/foundry-route-policy.json through this module:
#   Get-CmoFoundryRoutePolicy   - load the machine-readable canonical policy
#   Test-CmoFoundryRoutePolicy  - validate order, subscription-only tail, no PAYG
#   Get-CmoFoundryRouteQueue    - enumerate the model-major leg queue (9 free + subscription tail)
#   Resolve-CmoFoundryNextStep  - first non-exhausted leg; subscription fallback; never paid
#   Get-CmoFailureKind          - quota | auth | transient | unknown
#   Resolve-CmoFoundryModelId   - correct stale aliases to canonical ids
#
# CAPABILITY (precise): Pi launchers MAY rotate isolated account profiles
# (account-1/account-2/account-3). The Cline VS Code extension exposes only the ACTIVE login on
# disk, so this module never claims automatic switching of unavailable Cline
# credentials - legs for other Cline logins are advisory until signed in.
# Secrets/tokens are never read, logged, or written here (metadata only).

function Get-CmoFoundryRoutePolicyPath {
    return (Join-Path $PSScriptRoot 'foundry-route-policy.json')
}

function Get-CmoFoundryRoutePolicy {
    param([string]$PolicyPath)
    if (-not $PolicyPath) { $PolicyPath = Get-CmoFoundryRoutePolicyPath }
    $raw = Get-Content -LiteralPath $PolicyPath -Raw -ErrorAction Stop
    return ($raw | ConvertFrom-Json)
}

function Resolve-CmoFoundryModelId {
    # Correct stale aliases to canonical policy ids (case-insensitive).
    param([string]$Id, [object]$Policy)
    $s = ([string]$Id).Trim()
    if (-not $s) { return '' }
    $low = $s.ToLowerInvariant()
    # stale: deepseek v4 without minor version (free + subscription)
    if ($low -eq 'deepseek/deepseek-v4-flash') { return 'cline-free/deepseek-v4.1-flash' }
    if ($low -eq 'cline-pass/deepseek-v4-flash') { return 'cline-pass/deepseek-v4.1-flash' }
    # stale: assumed cline-free/ prefix rewrite for GLM flash
    if ($low -eq 'cline-free/glm-5.3-flash') { return 'z-ai/glm-5.3-flash' }
    # stale: bare short name for Muse Spark
    if ($low -eq 'muse-spark-1.3') { return 'cline-free/muse-spark-1.3-contributor' }
    if ($Policy) {
        try {
            foreach ($f in @($Policy.freeRoute)) {
                if (([string]$f.model).ToLowerInvariant() -eq $low) { return [string]$f.model }
                foreach ($a in @($f.aliases)) {
                    if (([string]$a).ToLowerInvariant() -eq $low) { return [string]$f.model }
                }
            }
            foreach ($q in @($Policy.routeQueue)) {
                if (([string]$q.model).ToLowerInvariant() -eq $low) { return [string]$q.model }
            }
        } catch { }
    }
    return $s
}

function Test-CmoFoundryRoutePolicy {
    # Validate a policy object. Returns @{ Valid; Errors[] }.
    param([object]$Policy)
    $errors = @()
    if (-not $Policy) { return @{ Valid = $false; Errors = @('policy missing') } }
    if ([string]$Policy.schema -ne 'foundry-route-policy/v1') { $errors += 'schema must be foundry-route-policy/v1' }
    if ([string]$Policy.owner -ne 'cline-model-optimizer') { $errors += 'owner must be cline-model-optimizer' }
    if ($Policy.neverPayg -ne $true) { $errors += 'neverPayg must be true' }
    $accts = @($Policy.piAccounts)
    foreach ($want in @('account-1', 'account-2', 'account-3')) {
        if (-not ($accts -contains $want)) { $errors += ("piAccounts missing " + $want) }
    }
    $freeModels = @($Policy.freeRoute | ForEach-Object { [string]$_.model })
    $wantFree = @('cline-free/muse-spark-1.3-contributor', 'z-ai/glm-5.3-flash', 'cline-free/deepseek-v4.1-flash')
    for ($i = 0; $i -lt $wantFree.Count; $i++) {
        if ($i -ge $freeModels.Count -or $freeModels[$i] -ne $wantFree[$i]) {
            $errors += ("freeRoute rank " + ($i + 1) + " must be " + $wantFree[$i])
        }
    }
    $allowed = @($Policy.allowedTiers)
    if (-not ($allowed -contains 'FREE') -or -not ($allowed -contains 'SUBSCRIPTION')) {
        $errors += 'allowedTiers must contain FREE and SUBSCRIPTION'
    }
    if ($allowed -contains 'PAID') { $errors += 'allowedTiers must never contain PAID' }
    # queue must be model-major: 3 accounts per free model, then subscription tail, never paid
    $queue = @($Policy.routeQueue)
    if ($queue.Count -lt 11) { $errors += 'routeQueue must hold 9 free legs + subscription tail' }
    else {
        $freeLegs = @($queue | Where-Object { [string]$_.tier -eq 'FREE' })
        if ($freeLegs.Count -ne 9) { $errors += 'routeQueue must hold exactly 9 FREE legs' }
        $mi = 0
        foreach ($m in $wantFree) {
            $block = @($freeLegs | Where-Object { [string]$_.model -eq $m })
            if ($block.Count -ne 3) { $errors += ("model " + $m + " must span 3 pi accounts") }
            else {
                for ($a = 0; $a -lt 3; $a++) {
                    if ([string]$block[$a].piAccount -ne ('account-' + ($a + 1))) {
                        $errors += ("model " + $m + " account order must be account-1,account-2,account-3")
                    }
                }
            }
            $mi++
        }
        # model-major: all legs of rank N precede rank N+1
        $seqModels = @($freeLegs | ForEach-Object { [string]$_.model })
        $expected = @()
        foreach ($m in $wantFree) { for ($a = 0; $a -lt 3; $a++) { $expected += $m } }
        for ($i = 0; $i -lt $expected.Count; $i++) {
            if ($seqModels[$i] -ne $expected[$i]) { $errors += 'routeQueue must be model-major (all accounts of model N before model N+1)'; break }
        }
        foreach ($q in $queue) {
            if ([string]$q.tier -eq 'PAID') { $errors += 'routeQueue must never contain a PAID leg' }
            if ([string]$q.legKind -eq 'paid') { $errors += 'routeQueue must never contain a paid legKind' }
        }
        $tail = @($queue | Where-Object { [string]$_.legKind -eq 'subscription' })
        if ($tail.Count -lt 1) { $errors += 'routeQueue must end with a subscription tail' }
        foreach ($t in $tail) {
            if ([string]$t.tier -ne 'SUBSCRIPTION') { $errors += 'subscription tail legs must be SUBSCRIPTION tier' }
            if (([string]$t.model) -notlike 'cline-pass/*') { $errors += 'subscription tail models must be cline-pass/*' }
        }
    }
    foreach ($s in @($Policy.subscriptionFallback)) {
        if (([string]$s.model) -notlike 'cline-pass/*') { $errors += 'subscriptionFallback models must be cline-pass/*' }
    }
    return @{ Valid = ($errors.Count -eq 0); Errors = @($errors) }
}

function Get-CmoFoundryRouteQueue {
    # Enumerate legs from the canonical policy (model-major order).
    param([object]$Policy)
    if (-not $Policy) { $Policy = Get-CmoFoundryRoutePolicy }
    return @($Policy.routeQueue)
}

function Resolve-CmoFoundryNextStep {
    # First non-exhausted leg. $ExhaustedLegs holds 'model|piAccount' keys for
    # spent free legs (subscription legs keyed 'model|'). Returns the leg object
    # or $null when everything (including subscription) is exhausted. Never paid.
    param(
        [object]$Policy,
        [string[]]$ExhaustedLegs = @(),
        [switch]$SubscriptionExhausted
    )
    if (-not $Policy) { $Policy = Get-CmoFoundryRoutePolicy }
    $spent = @{}
    foreach ($k in @($ExhaustedLegs)) { if ($k) { $spent[[string]$k] = $true } }
    foreach ($leg in @($Policy.routeQueue)) {
        $tier = [string]$leg.tier
        if ($tier -eq 'PAID') { continue }  # never PAYG, even if a foreign policy injects one
        if ([string]$leg.legKind -eq 'paid') { continue }
        if ($tier -eq 'SUBSCRIPTION' -and $SubscriptionExhausted) { continue }
        $key = ([string]$leg.model + '|' + [string]$leg.piAccount)
        if ($spent.ContainsKey($key)) { continue }
        return $leg
    }
    return $null
}

function Get-CmoFailureKind {
    # Distinguish quota (advance queue) vs auth (re-sign-in, do not advance as
    # spent) vs transient (retry same leg) vs unknown.
    param([string]$ErrorText = '', [int]$HttpStatus = 0)
    $t = ([string]$ErrorText).ToLowerInvariant()
    if ($HttpStatus -eq 401 -or $HttpStatus -eq 403) { return 'auth' }
    if ($HttpStatus -eq 429) { return 'quota' }
    if ($HttpStatus -ge 500 -and $HttpStatus -le 599) { return 'transient' }
    $authSignals = @('401', '403', 'unauthorized', 'forbidden', 'token expired', 'auth expired', 're-sign in', 'invalid_token', 'invalid token', 'auth metadata')
    foreach ($s in $authSignals) { if ($t -and $t.Contains($s)) { return 'auth' } }
    $quotaSignals = @('inference_cap_error', 'inference cap', 'status code 429', 'quota exceeded', 'quota_exceeded', 'resource exhausted', 'resource_exhausted', 'cline_free_promotion_ended')
    foreach ($s in $quotaSignals) { if ($t -and $t.Contains($s)) { return 'quota' } }
    $transientSignals = @('timeout', 'timed out', 'connection', 'reset by peer', 'econnreset', 'enotfound', '502', '503', '504', 'temporarily unavailable', 'fetch failed', 'unreachable', 'network')
    foreach ($s in $transientSignals) { if ($t -and $t.Contains($s)) { return 'transient' } }
    return 'unknown'
}
