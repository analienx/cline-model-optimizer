# CmoGuardian.ps1 - periodic check of Cline model usage vs the configured strategy.
#
# Behavior (advisory-first, never destructive):
#   - FREE model in use and optimal  -> log only (silent OK)
#   - FREE in use but not preferred  -> log
#   - SUBSCRIPTION/PAID in use while free preferred exists -> toast (rate-limited 2h)
#   - cline auth token expires < 12h or expired -> toast (rate-limited 6h)
# Tracks cumulative tier-usage minutes per day in guardian-state.json.
# Writes %LOCALAPPDATA%\ClineModelOptimizer\guardian-status.json for the dashboard.
# Exit codes: 0=optimal 1=free-in-use-suboptimal 2=paid-in-use 3=auth-warning 4=state-missing

$ErrorActionPreference = 'SilentlyContinue'

. (Join-Path $PSScriptRoot 'CmoCore.ps1')

$cmoDir   = Join-Path $env:LOCALAPPDATA 'ClineModelOptimizer'
$gLog     = Join-Path $cmoDir 'guardian.log'
$gState   = Join-Path $cmoDir 'guardian-state.json'
$gReport  = Join-Path $cmoDir 'guardian-status.json'
$notify   = Join-Path $PSScriptRoot 'CmoNotify.ps1'

if (-not (Test-Path -LiteralPath $cmoDir)) { New-Item -ItemType Directory -Path $cmoDir -Force | Out-Null }

function Write-Log([string]$msg) {
    Add-Content -LiteralPath $gLog -Value ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
    if ((Get-Item -LiteralPath $gLog -ErrorAction SilentlyContinue).Length -gt 256KB) {
        Set-Content -LiteralPath $gLog -Value ('{0} log rotated' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    }
}

# Rate-limited toast: returns $true if the toast was actually sent.
function Invoke-CmoToast {
    param([string]$Key, [double]$RateLimitHours, [string]$Title, [string]$Message)
    $stampKey = 'LastToast' + $Key
    $last = $state[$stampKey]
    if ($last -and (((Get-Date) - [datetime]$last).TotalHours -lt $RateLimitHours)) { return $false }
    # headless child: must never flash a console window (see Invoke-CmoHidden)
    Invoke-CmoHidden -File 'powershell.exe' -Arguments @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $notify + '"'),
        '-Title', ('"' + $Title + '"'), '-Message', ('"' + $Message + '"')) | Out-Null
    $state[$stampKey] = (Get-Date).ToString('o')
    return $true
}

$state = @{ LastCheck = (Get-Date).ToString('o'); LastStatus = 'init'
            LastToastPaid = $null; LastToastAuth = $null; LastToastAutoSwitch = $null
            TierMinutes = @{ free = 0.0; subscription = 0.0; paid = 0.0 }; TierDate = (Get-Date -Format 'yyyy-MM-dd')
            AccountMinutes = @{} }
if (Test-Path -LiteralPath $gState) {
    try {
        $s = Get-Content -LiteralPath $gState -Raw | ConvertFrom-Json
        $state.LastCheck = $s.LastCheck; $state.LastStatus = $s.LastStatus
        $state.LastToastPaid = $s.LastToastPaid; $state.LastToastAuth = $s.LastToastAuth
        $state.LastToastAutoSwitch = $s.LastToastAutoSwitch
        $state.TierDate = $s.TierDate
        foreach ($k in 'free','subscription','paid') { $state.TierMinutes[$k] = [double]$s.TierMinutes.$k }
        if ($s.AccountMinutes) { foreach ($k in @($s.AccountMinutes.PSObject.Properties.Name)) { $state.AccountMinutes[$k] = [double]$s.AccountMinutes.$k } }
    } catch { }
}
function Save-State([string]$status) {
    $state.LastStatus = $status
    $state.LastCheck = (Get-Date).ToString('o')
    @{ LastCheck = $state.LastCheck; LastStatus = $status
       LastToastPaid = $state.LastToastPaid; LastToastAuth = $state.LastToastAuth
       LastToastAutoSwitch = $state.LastToastAutoSwitch
       TierMinutes = $state.TierMinutes; TierDate = $state.TierDate; AccountMinutes = $state.AccountMinutes } |
        ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $gState -Force
}

$routing = Get-CmoRoutingConfig
$snap = Get-CmoSnapshot -Routing $routing
if (-not $snap.StateExists) {
    Write-Log 'Cline state not found (~\.cline\data\globalState.json missing) - is Cline installed?'
    Save-State 'NO-CLINE-STATE'; exit 4
}

# ---- accumulate tier usage minutes since last check ----
$today = (Get-Date -Format 'yyyy-MM-dd')
if ($state.TierDate -ne $today) { $state.TierMinutes = @{ free = 0.0; subscription = 0.0; paid = 0.0 }; $state.TierDate = $today }
$elapsedMin = ((Get-Date) - [datetime]$state.LastCheck).TotalMinutes
if ($elapsedMin -gt 0 -and $elapsedMin -lt 30) {
    $lastTier = switch -Wildcard ($state.LastStatus) {
        'OK-FREE*' { 'free' }
        'PAID*'    { 'paid' }
        'SUB*'     { 'subscription' }
        default    { $null }
    }
    if ($lastTier) { $state.TierMinutes[$lastTier] = [math]::Round($state.TierMinutes[$lastTier] + $elapsedMin, 1) }
    # per-login usage: attribute elapsed minutes to the live login's email so the
    # dashboard can show which account did the work today (observed, not quota).
    try {
        $liveKey = $null
        $ap = $snap.Act.Provider
        $hit = @($snap.Accounts | Where-Object { $_.Provider -eq $ap -and $_.Email })
        if ($hit.Count -gt 0) { $liveKey = $hit[0].Email }
        if ($liveKey) {
            if (-not $state.AccountMinutes.ContainsKey($liveKey)) { $state.AccountMinutes[$liveKey] = 0.0 }
            $state.AccountMinutes[$liveKey] = [math]::Round($state.AccountMinutes[$liveKey] + $elapsedMin, 1)
        }
    } catch { }
}
if ($state.TierDate -ne $today) { $state.AccountMinutes = @{} }

# ---- automatic usage + cap-hit detection (Cline usage API + transcripts) ----
# never fatal: on any failure the dashboard keeps the last good cache.
try {
    Invoke-CmoUsageRefresh | Out-Null
    $u = Get-CmoUsageState
    $models = 0
    try { $models = @($u.perModel.Keys).Count } catch { }
    Write-Log ("usage API: {0} models today, fetched={1}{2}" -f $models, [bool]$u.fetchedAt,
        $(if ($u.lastError) { ' err=' + $u.lastError } else { '' }))
    try {
        $caps = Get-CmoInferredCaps -State $u
        foreach ($ck in @($caps.Keys)) {
            Write-Log ('cap likely (inferred): ' + $ck + ' last used ' + (ConvertTo-CmoEpochLocal -Ms ([long]$caps[$ck])).ToString('HH:mm'))
        }
    } catch { }
    try {
        foreach ($ae in @($u.autoAccounts.Keys)) {
            Write-Log ('cap hit detected (auto): ' + $ae)
        }
    } catch { }
} catch { Write-Log ('usage refresh failed: ' + $_.Exception.Message) }

# ---- evaluate ----
$act = $snap.Act
$dynCount = @($snap.DynamicFree.Models).Count
Write-Log ("free list: {0} models - source={1} fresh={2}{3}" -f $dynCount, $snap.DynamicFree.Source, $snap.DynamicFree.Fresh, $(if ($snap.DynamicFree.Error) { ' error=' + $snap.DynamicFree.Error } else { '' }))
Write-Log ("ACT: provider=" + $act.Provider + " model=" + $act.Model + " tier=" + $act.Tier + " | optimal=" + $act.Recommendation.Optimal)
Write-Log ("PLAN: provider=" + $snap.Plan.Provider + " model=" + $snap.Plan.Model + " tier=" + $snap.Plan.Tier)
# ---- auto-switch: account-first rotation (see Get-CmoAutoSwitchPlan) ----
# Capture the current login (so rotation tokens accumulate as the user signs
# into different accounts), refresh captured accounts' usage, then apply the
# plan. VS Code CLOSED -> apply; VS Code OPEN -> advise via toast.
try {
    $captured = Update-CmoCapturedCredentials
    if ($captured) { Write-Log ('captured login for rotation: ' + $captured) }
    $u = Update-CmoAccountUsages
    $rot = Get-CmoRotationContext
    $plan = Get-CmoAutoSwitchPlan -Routing $routing -Act $act -UsageState $rot.State -AccountCaps $rot.Caps -LiveEmail $rot.LiveEmail
    if ($plan) {
        $vsOpen = @(Get-Process -Name 'Code' -ErrorAction SilentlyContinue).Count -gt 0
        $kind = [string]$plan.Kind
        if ($kind -eq 'needs-signin') {
            # informational only: a tracked account may still have budget; one
            # sign-in (while it is the active login) captures its token.
            Write-Log ('rotation wants sign-in: ' + $plan.Account + ' (' + $plan.Reason + ') - captured logins: ' + (@($rot.Captured | ForEach-Object { $_.Email + $(if ($_.Expired) { ' (expired)' }) }) -join ', '))
        } elseif ($vsOpen) {
            Write-Log ("auto-switch plan (" + $kind + "): " + $plan.From + " -> " + $plan.To + $(if ($plan.Account) { ' @ ' + $plan.Account }) + " - VS Code running, advising only")
            $msg = switch ($kind) {
                'account-switch' { ($plan.Account + " still has free budget for " + $plan.To + ". Close VS Code to rotate, or use the dashboard") }
                'both'           { ($plan.Account + " has free budget for " + $plan.To + ". Close VS Code to switch (account + model)") }
                default          { ($plan.To + " is available free - close VS Code to let me switch, or pick it on the dashboard") }
            }
            if (Invoke-CmoToast -Key 'AutoSwitch' -RateLimitHours 1 -Title 'Cline Model Optimizer' -Message $msg) {
                Write-Log 'toast: switch advised'
            }
        } else {
            $ok = $false; $detail = ''
            if ($kind -eq 'model-switch') {
                $r = Invoke-CmoHidden -File 'powershell.exe' -Arguments @(
                    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + (Join-Path $PSScriptRoot 'CmoApply.ps1') + '"'),
                    '-Rank', [string]$plan.Rank)
                $ok = ($r.ExitCode -eq 0); $detail = (($r.Output + ' ' + $r.Error).Trim())
            } else {
                # account-switch or both: rotate login first (VS Code is closed)
                $r = Invoke-CmoHidden -File 'powershell.exe' -Arguments @(
                    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + (Join-Path $PSScriptRoot 'CmoSwitchAccount.ps1') + '"'),
                    '-Email', ('"' + $plan.Account + '"'))
                if ($r.ExitCode -eq 0) {
                    Write-Log ('ROTATED ACCOUNT -> ' + $plan.Account)
                    if ($kind -eq 'both') {
                        $r2 = Invoke-CmoHidden -File 'powershell.exe' -Arguments @(
                            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + (Join-Path $PSScriptRoot 'CmoApply.ps1') + '"'),
                            '-Rank', [string]$plan.Rank)
                        $ok = ($r2.ExitCode -eq 0); $detail = (($r2.Output + ' ' + $r2.Error).Trim())
                    } else { $ok = $true }
                } else {
                    $detail = 'switch-account failed (exit ' + $r.ExitCode + '): ' + (($r.Output + ' ' + $r.Error).Trim())
                }
            }
            if ($ok) {
                Write-Log ("AUTO-SWITCH (" + $kind + ") " + $plan.From + " -> " + $plan.To + $(if ($plan.Account -and $kind -ne 'model-switch') { ' @ ' + $plan.Account }))
                Invoke-CmoToast -Key 'AutoSwitch' -RateLimitHours 1 -Title 'Cline Model Optimizer' `
                    -Message ("Auto-switched" + $(if ($plan.Account -and $kind -ne 'model-switch') { ' to ' + $plan.Account }) + ": " + $plan.To + " (" + $plan.Reason + ")") | Out-Null
            } else {
                Write-Log ('auto-switch apply failed: ' + $detail)
            }
        }
    }
} catch { Write-Log ('auto-switch error: ' + $_.Exception.Message) }


$status = 'OK-FREE-OPTIMAL'
$exit = 0

if ($act.Tier -eq 'FREE' -and $act.Recommendation.Optimal) {
    $status = 'OK-FREE-OPTIMAL'; $exit = 0
} elseif ($act.Tier -eq 'FREE') {
    $status = 'OK-FREE-SUBOPTIMAL'; $exit = 1
} elseif ($act.Tier -eq 'SUBSCRIPTION') {
    $status = 'SUB-IN-USE'; $exit = 2
    if (Invoke-CmoToast -Key 'Paid' -RateLimitHours 2 -Title 'Cline Model Optimizer' `
        -Message ("Subscription model in use: " + $act.Model + " - free alternatives available")) {
        Write-Log 'toast: subscription in use'
    }
} else {
    $status = 'PAID-IN-USE'; $exit = 2
    if (Invoke-CmoToast -Key 'Paid' -RateLimitHours 2 -Title 'Cline Model Optimizer' `
        -Message ("Paid model in use: " + $act.Model + " - free alternatives available")) {
        Write-Log 'toast: paid in use'
    }
}

# ---- auth token check ----
$tok = $snap.TokenRemainingHours
if ($null -ne $tok) {
    if ($tok -le 0) {
        if (Invoke-CmoToast -Key 'Auth' -RateLimitHours 6 -Title 'Cline Model Optimizer' `
            -Message 'Cline authentication EXPIRED - re-sign in to Cline in VS Code') { Write-Log 'toast: token expired' }
        if ($exit -eq 0) { $status = 'AUTH-EXPIRED'; $exit = 3 }
    } elseif ($tok -lt 12) {
        if (Invoke-CmoToast -Key 'Auth' -RateLimitHours 6 -Title 'Cline Model Optimizer' `
            -Message ("Cline token expires in " + $tok + "h - will need re-sign in")) { Write-Log 'toast: token expiring' }
        if ($exit -eq 0) { $status = 'AUTH-EXPIRING'; $exit = 3 }
    }
}

# ---- report for dashboard ----
@{ Timestamp = (Get-Date -Format 'o'); Status = $status
   TierMinutes = $state.TierMinutes; TierDate = $state.TierDate
   DynamicFree = @{ Source = $snap.DynamicFree.Source; Count = $dynCount
                    Fresh = $snap.DynamicFree.Fresh; FetchedAt = $snap.DynamicFree.FetchedAt
                    Error = $snap.DynamicFree.Error }
   Act = @{ Provider = $act.Provider; Model = $act.Model; Tier = $act.Tier
            Optimal = $act.Recommendation.Optimal; Message = $act.Recommendation.Message }
   Plan = @{ Provider = $snap.Plan.Provider; Model = $snap.Plan.Model; Tier = $snap.Plan.Tier
             Optimal = $snap.Plan.Recommendation.Optimal }
   TokenRemainingHours = $tok
   Strategy = $routing.strategy } |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $gReport -Force

Save-State $status
exit $exit
