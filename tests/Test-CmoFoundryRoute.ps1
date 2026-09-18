# Test-CmoFoundryRoute.ps1 - deterministic tests for the canonical Foundry v3 route policy.
# No network. No Cline state. No tokens/secrets. Pure policy + queue logic.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$src = Join-Path $repo 'src'
. (Join-Path $src 'CmoFoundryRoute.ps1')

$script:passed = 0
function Assert-Cmo {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
    $script:passed++
}

$policyPath = Join-Path $src 'foundry-route-policy.json'
Assert-Cmo (Test-Path -LiteralPath $policyPath) 'foundry-route-policy.json must exist'
$policy = Get-CmoFoundryRoutePolicy -PolicyPath $policyPath

# 1. Policy validates (pin target for Pi launchers).
$check = Test-CmoFoundryRoutePolicy -Policy $policy
Assert-Cmo ($check.Valid) ('canonical policy must validate: ' + ($check.Errors -join '; '))
Assert-Cmo ([string]$policy.policyVersion -ne '') 'policyVersion must be set for pinning'
Assert-Cmo ([string]$policy.owner -eq 'cline-model-optimizer') 'owner must be cline-model-optimizer'

# 2. Canonical free order: Muse Spark 1.3 -> GLM-5.3 Flash -> DeepSeek V4.1 Flash.
$freeModels = @($policy.freeRoute | ForEach-Object { [string]$_.model })
Assert-Cmo ($freeModels.Count -eq 3) 'freeRoute must hold exactly 3 models'
Assert-Cmo ($freeModels[0] -eq 'cline-free/muse-spark-1.3-contributor') 'rank 1 must be Muse Spark 1.3 free'
Assert-Cmo ($freeModels[1] -eq 'z-ai/glm-5.3-flash') 'rank 2 must be GLM-5.3 Flash free'
Assert-Cmo ($freeModels[2] -eq 'cline-free/deepseek-v4.1-flash') 'rank 3 must be DeepSeek V4.1 Flash free'

# 3. Model-first (model-major) account order across account-1,account-2,account-3.
$queue = @(Get-CmoFoundryRouteQueue -Policy $policy)
$freeLegs = @($queue | Where-Object { [string]$_.tier -eq 'FREE' })
Assert-Cmo ($freeLegs.Count -eq 9) 'queue must hold exactly 9 free legs (3 models x 3 accounts)'
$expected = @()
foreach ($m in $freeModels) { foreach ($a in @('account-1', 'account-2', 'account-3')) { $expected += ($m + '|' + $a) } }
$actual = @($freeLegs | ForEach-Object { ([string]$_.model + '|' + [string]$_.piAccount) })
Assert-Cmo ($actual.Count -eq $expected.Count) 'free leg count mismatch'
for ($i = 0; $i -lt $expected.Count; $i++) {
    Assert-Cmo ($actual[$i] -eq $expected[$i]) ("model-major order violated at free leg ${i}: want $($expected[$i]), got $($actual[$i])")
}

# 4. Exhaustion: spent legs are skipped model-first, never jumping models early.
$next = Resolve-CmoFoundryNextStep -Policy $policy -ExhaustedLegs @()
Assert-Cmo (($next.model -eq $freeModels[0]) -and ($next.piAccount -eq 'account-1')) 'fresh queue must start at Muse Spark account-1'
$next = Resolve-CmoFoundryNextStep -Policy $policy -ExhaustedLegs @('cline-free/muse-spark-1.3-contributor|account-1')
Assert-Cmo (($next.model -eq $freeModels[0]) -and ($next.piAccount -eq 'account-2')) 'must rotate to account-2 before leaving Muse Spark'
$spentMuse = @(
    'cline-free/muse-spark-1.3-contributor|account-1',
    'cline-free/muse-spark-1.3-contributor|account-2',
    'cline-free/muse-spark-1.3-contributor|account-3'
)
$next = Resolve-CmoFoundryNextStep -Policy $policy -ExhaustedLegs $spentMuse
Assert-Cmo (($next.model -eq 'z-ai/glm-5.3-flash') -and ($next.piAccount -eq 'account-1')) 'after Muse Spark exhaustion must advance to GLM account-1'
$spentAllFree = @()
foreach ($m in $freeModels) { foreach ($a in @('account-1', 'account-2', 'account-3')) { $spentAllFree += ($m + '|' + $a) } }
$next = Resolve-CmoFoundryNextStep -Policy $policy -ExhaustedLegs $spentAllFree
Assert-Cmo ($next.legKind -eq 'subscription') 'all-free-exhausted must fall back to subscription'
Assert-Cmo ([string]$next.tier -eq 'SUBSCRIPTION') 'fallback leg must be SUBSCRIPTION tier'

# 5. Subscription fallback only; full exhaustion yields $null (stop, never paid).
$allKeys = @()
foreach ($leg in $queue) { $allKeys += ([string]$leg.model + '|' + [string]$leg.piAccount) }
$next = Resolve-CmoFoundryNextStep -Policy $policy -ExhaustedLegs $allKeys
Assert-Cmo ($null -eq $next) 'fully exhausted queue must resolve to $null, not paid'
$next = Resolve-CmoFoundryNextStep -Policy $policy -ExhaustedLegs $spentAllFree -SubscriptionExhausted
Assert-Cmo ($null -eq $next) 'subscription-exhausted must resolve to $null, not paid'

# 6. No-PAYG invariant: no PAID tier or paid legKind anywhere in policy.
foreach ($leg in $queue) {
    Assert-Cmo ([string]$leg.tier -ne 'PAID') 'routeQueue must never contain a PAID leg'
    Assert-Cmo ([string]$leg.legKind -ne 'paid') 'routeQueue must never contain a paid legKind'
}
Assert-Cmo ($policy.neverPayg -eq $true) 'neverPayg must be true'
Assert-Cmo (-not (@($policy.allowedTiers) -contains 'PAID')) 'allowedTiers must never contain PAID'
foreach ($s in @($policy.subscriptionFallback)) {
    Assert-Cmo (([string]$s.model) -like 'cline-pass/*') 'subscriptionFallback models must be cline-pass/*'
}

# 7. Stale aliases resolve to canonical ids.
Assert-Cmo ((Resolve-CmoFoundryModelId -Id 'cline-pass/deepseek-v4-flash' -Policy $policy) -eq 'cline-pass/deepseek-v4.1-flash') 'stale sub v4 must resolve to v4.1'
Assert-Cmo ((Resolve-CmoFoundryModelId -Id 'deepseek/deepseek-v4-flash' -Policy $policy) -eq 'cline-free/deepseek-v4.1-flash') 'stale free v4 must resolve to v4.1'
Assert-Cmo ((Resolve-CmoFoundryModelId -Id 'cline-free/glm-5.3-flash' -Policy $policy) -eq 'z-ai/glm-5.3-flash') 'assumed cline-free GLM prefix must resolve to z-ai/'
Assert-Cmo ((Resolve-CmoFoundryModelId -Id 'muse-spark-1.3' -Policy $policy) -eq 'cline-free/muse-spark-1.3-contributor') 'bare Muse Spark name must resolve to canonical id'
Assert-Cmo ((Resolve-CmoFoundryModelId -Id 'z-ai/glm-5.3-flash' -Policy $policy) -eq 'z-ai/glm-5.3-flash') 'canonical GLM id must be stable'

# 8. Failure taxonomy: quota vs auth vs transient.
Assert-Cmo ((Get-CmoFailureKind -ErrorText 'INFERENCE_CAP_ERROR quota exceeded') -eq 'quota') 'cap text must be quota'
Assert-Cmo ((Get-CmoFailureKind -HttpStatus 429) -eq 'quota') 'HTTP 429 must be quota'
Assert-Cmo ((Get-CmoFailureKind -HttpStatus 401) -eq 'auth') 'HTTP 401 must be auth'
Assert-Cmo ((Get-CmoFailureKind -ErrorText 'token expired, re-sign in') -eq 'auth') 'expiry text must be auth'
Assert-Cmo ((Get-CmoFailureKind -HttpStatus 503) -eq 'transient') 'HTTP 503 must be transient'
Assert-Cmo ((Get-CmoFailureKind -ErrorText 'connection timeout fetching free list') -eq 'transient') 'timeout text must be transient'
Assert-Cmo ((Get-CmoFailureKind -ErrorText 'something unexpected') -eq 'unknown') 'unmatched text must be unknown'

# 9. Bundled model-routing.json agrees with the canonical policy (no drift).
$routing = Get-Content -LiteralPath (Join-Path $src 'model-routing.json') -Raw | ConvertFrom-Json
$pref = @($routing.freeSelection.preferredFreeModels | ForEach-Object { [string]$_ })
Assert-Cmo ($pref.Count -eq 3) 'model-routing preferredFreeModels must hold 3 entries'
for ($i = 0; $i -lt 3; $i++) {
    $canon = Resolve-CmoFoundryModelId -Id $pref[$i] -Policy $policy
    Assert-Cmo ($canon -eq $freeModels[$i]) ("model-routing preferred rank $($i+1) must match canonical free order")
}
$subs = @($routing.freeSelection.subscriptionSteps)
Assert-Cmo ($subs.Count -ge 1) 'model-routing must keep a subscription tail'
foreach ($s in $subs) {
    Assert-Cmo (([string]$s.model) -like 'cline-pass/*') 'model-routing subscriptionSteps must be cline-pass/* only'
    Assert-Cmo (([string]$s.model) -ne 'cline-pass/deepseek-v4-flash') 'stale sub v4 alias must not remain in model-routing'
}
$freeList = @($routing.tierRules.freeModels | ForEach-Object { [string]$_ })
Assert-Cmo (-not ($freeList -contains 'deepseek/deepseek-v4-flash')) 'stale free v4 alias must not remain in tierRules.freeModels'
Assert-Cmo ($freeList -contains 'deepseek/deepseek-v4.1-flash') 'tierRules.freeModels must carry the corrected v4.1 alias entry'
if ($routing.PSObject.Properties.Name -contains 'paygAllowed') {
    Assert-Cmo ($routing.paygAllowed -eq $false) 'model-routing paygAllowed must be false'
}

Write-Host ("PASS: {0} Foundry route assertions" -f $script:passed)
