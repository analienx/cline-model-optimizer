# CmoAccounts.ps1 - per-account "free budget today" view for Cline Model Optimizer.
#
# WHY THIS FILE EXISTS: Cline exposes NO quota/usage API, and it only persists ONE
# signed-in login on disk (see Get-CmoExtraAccounts in CmoCore.ps1). So the answer
# to "has this account used up its free tier for the day?" cannot be read from
# Cline - it is OBSERVED plus USER-CONFIRMED:
#   * observed  - minutes the guardian attributed to the live login today
#   * confirmed - a per-account daily budget (minutes) and/or the manual
#                 'used up today' marker, both editable in the dashboard
# Everything here is local metadata under LOCALAPPDATA. No tokens, no network.

function Get-CmoAccountPrefsPath {
    return (Join-Path $env:LOCALAPPDATA 'ClineModelOptimizer\account-prefs.json')
}

function Get-CmoDefaultBudgetMin { return 120 }

function Get-CmoTodayKey { return (Get-Date -Format 'yyyy-MM-dd') }

function Get-CmoAccountPrefs {
    # shape: { budgets: { <email>: <minutes> }, depleted: { <email>: 'yyyy-MM-dd' } }
    $fp = Get-CmoAccountPrefsPath
    if (-not (Test-Path -LiteralPath $fp)) {
        return [pscustomobject]@{ budgets = [pscustomobject]@{}; depleted = [pscustomobject]@{} }
    }
    try {
        $j = Get-Content -LiteralPath $fp -Raw | ConvertFrom-Json
        if (-not $j) { throw 'empty' }
        if (-not $j.budgets)  { $j | Add-Member -NotePropertyName budgets  -NotePropertyValue ([pscustomobject]@{}) -Force }
        if (-not $j.depleted) { $j | Add-Member -NotePropertyName depleted -NotePropertyValue ([pscustomobject]@{}) -Force }
        return $j
    } catch {
        return [pscustomobject]@{ budgets = [pscustomobject]@{}; depleted = [pscustomobject]@{} }
    }
}

function Save-CmoAccountPrefs {
    param([object]$Prefs)
    $fp = Get-CmoAccountPrefsPath
    New-Item -ItemType Directory -Path (Split-Path $fp) -Force | Out-Null
    ($Prefs | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $fp -Encoding UTF8
    return $Prefs
}

function Get-CmoAccountBudgetMin {
    param([string]$Email)
    if (-not $Email) { return (Get-CmoDefaultBudgetMin) }
    $p = Get-CmoAccountPrefs
    $v = $null
    try { $v = $p.budgets.$Email } catch { $v = $null }
    if ($null -eq $v) { return (Get-CmoDefaultBudgetMin) }
    try { $n = [int]$v } catch { return (Get-CmoDefaultBudgetMin) }
    if ($n -lt 1) { return (Get-CmoDefaultBudgetMin) }
    return $n
}

function Set-CmoAccountBudgetMin {
    param([string]$Email, [int]$Minutes)
    if (-not $Email) { throw 'email required' }
    if ($Minutes -lt 1) { $Minutes = 1 }
    if ($Minutes -gt 1440) { $Minutes = 1440 }
    $p = Get-CmoAccountPrefs
    $p.budgets | Add-Member -NotePropertyName $Email -NotePropertyValue ([int]$Minutes) -Force
    Save-CmoAccountPrefs -Prefs $p | Out-Null
    return [int]$Minutes
}

function Test-CmoAccountDepleted {
    param([string]$Email)
    if (-not $Email) { return $false }
    $p = Get-CmoAccountPrefs
    try { return ([string]$p.depleted.$Email -eq (Get-CmoTodayKey)) } catch { return $false }
}

function Set-CmoAccountDepleted {
    # marks the account 'used up today' (dashboard), or clears the marker.
    param([string]$Email, [switch]$Off)
    if (-not $Email) { throw 'email required' }
    $p = Get-CmoAccountPrefs
    if ($Off) {
        # clear = drop the entry entirely (an empty string would be dead weight)
        try { $p.depleted.PSObject.Properties.Remove($Email) } catch { }
    } else {
        $p.depleted | Add-Member -NotePropertyName $Email -NotePropertyValue (Get-CmoTodayKey) -Force
    }
    Save-CmoAccountPrefs -Prefs $p | Out-Null
}
function Get-CmoDailyResetSpan {
    # time until the local midnight day-boundary (when daily counters/markers expire)
    $now = Get-Date
    return ($now.Date.AddDays(1) - $now)
}

function Get-CmoDailyResetText {
    $s = Get-CmoDailyResetSpan
    return ('{0}h {1:00}m' -f [int]$s.TotalHours, $s.Minutes)
}

function Get-CmoAccountHealth {
    # GREEN = budget left, AMBER = >=75% of budget used, RED = used up, OFF = not signed in
    param(
        [string]$Email,
        [double]$UsedMin = 0,
        [bool]$SignedIn = $true
    )
    $budget = Get-CmoAccountBudgetMin -Email $Email
    $dep = (Test-CmoAccountDepleted -Email $Email)
    $state = 'GREEN'; $label = 'free budget left'
    if (-not $SignedIn) { $state = 'OFF'; $label = 'not signed in' }
    elseif ($dep) { $state = 'RED'; $label = 'used up today (marked by you)' }
    elseif ($UsedMin -ge $budget) { $state = 'RED'; $label = 'daily budget reached' }
    elseif ($UsedMin -ge ($budget * 0.75)) { $state = 'AMBER'; $label = 'close to daily budget' }
    $pct = 0
    if ($budget -gt 0) {
        $pct = [int][math]::Round(100.0 * $UsedMin / $budget)
        if ($pct -gt 100) { $pct = 100 }
        if ($pct -lt 0) { $pct = 0 }
    }
    return [pscustomobject]@{
        State = $state; Label = $label; Budget = $budget
        Used = [double]$UsedMin; Percent = $pct; Depleted = $dep
    }
}

function Remove-CmoExtraAccount {
    # removes a manually tracked account (Cline's own provider entries are read-only)
    param([string]$Email)
    if (-not $Email) { throw 'email required' }
    $fp = Get-CmoExtraAccountsPath
    $keep = @()
    if (Test-Path -LiteralPath $fp) {
        $keep = @(@(Get-CmoExtraAccounts) | Where-Object {
            $_ -and (([string]$_.Email).ToLowerInvariant() -ne $Email.ToLowerInvariant()) })
        ($keep | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $fp -Encoding UTF8
    }
    # drop its saved budget / marker too
    $p = Get-CmoAccountPrefs
    foreach ($bag in @('budgets', 'depleted')) {
        try {
            $names = @($p.$bag.PSObject.Properties.Name)
            if ($names -contains $Email) { $p.$bag.PSObject.Properties.Remove($Email) }
        } catch { }
    }
    Save-CmoAccountPrefs -Prefs $p | Out-Null
    return $keep
}

function Get-CmoAccountCounts {
    # aggregate for the ACCOUNTS card header: green/amber/red tallies
    param([object]$Accounts)
    $green = 0; $amber = 0; $red = 0; $off = 0; $signedIn = 0
    foreach ($a in @($Accounts)) {
        if (-not $a) { continue }
        if (-not $a.SignedIn) { $off++; continue }
        $signedIn++
        switch ([string]$a.Health) {
            'GREEN' { $green++ }
            'AMBER' { $amber++ }
            'RED'   { $red++ }
            default { }
        }
    }
    return [pscustomobject]@{ Green = $green; Amber = $amber; Red = $red; Off = $off; SignedIn = $signedIn }
}
function Get-CmoAccountSummary {
    # ONE row per ACCOUNT (email) - not per provider slot. The cline / cline-pass
    # slots can hold the SAME login (real case here), which must not be counted as
    # two accounts; budgets and markers are per account anyway.
    param([object]$Accounts)
    $groups = @{}
    $order = @()
    foreach ($a in @($Accounts)) {
        if (-not $a -or -not $a.SignedIn) { continue }
        $key = ([string]$a.Email).ToLowerInvariant()
        if (-not $key) { $key = '(no email)' }
        if (-not $groups.ContainsKey($key)) {
            $groups[$key] = [pscustomobject]@{
                Email = [string]$a.Email; Providers = @(); Models = @(); Sources = @()
                Quota = 'TRACKED'; UsedMin = 0.0; LastUsed = $false
                Health = 'GREEN'; HealthLabel = ''; BudgetMin = (Get-CmoDefaultBudgetMin)
                Percent = 0; DepletedToday = $false; SignedIn = $true
            }
            $order += $key
        }
        $g = $groups[$key]
        $g.Providers = @(@($g.Providers + [string]$a.Provider) | Where-Object { $_ } | Select-Object -Unique)
        if ($a.Model) { $g.Models = @(@($g.Models + [string]$a.Model) | Where-Object { $_ } | Select-Object -Unique) }
        $g.Sources = @(@($g.Sources + [string]$a.Source) | Where-Object { $_ } | Select-Object -Unique)
        if ([string]$a.Quota -eq 'LIVE') { $g.Quota = 'LIVE' }
        if ($a.LastUsed) { $g.LastUsed = $true }
        if ([double]$a.UsedMin -gt [double]$g.UsedMin) { $g.UsedMin = [double]$a.UsedMin }
    }
    foreach ($k in $order) {
        $g = $groups[$k]
        $h = Get-CmoAccountHealth -Email $g.Email -UsedMin $g.UsedMin -SignedIn $true
        $g.Health = $h.State; $g.HealthLabel = $h.Label
        $g.BudgetMin = $h.Budget; $g.Percent = $h.Percent; $g.DepletedToday = $h.Depleted
    }
    $qRank = @{ 'LIVE' = 0; 'TRACKED' = 1; 'OFF' = 2 }
    $hRank = @{ 'GREEN' = 0; 'AMBER' = 1; 'RED' = 2; 'OFF' = 3 }
    return @($order | ForEach-Object { $groups[$_] } |
        Sort-Object { $qRank[[string]$_.Quota] }, { $hRank[[string]$_.Health] }, Email)
}

function Get-CmoUnscoredProviders {
    # providers with no login on file: not free, not subscription, not scorable
    # (e.g. a corporate proxy left over from an old setup) - shown as one footnote
    # instead of pretending they are accounts.
    param([object]$Accounts)
    $names = @(@($Accounts) | Where-Object { $_ -and -not $_.SignedIn } |
        ForEach-Object { [string]$_.Provider } | Where-Object { $_ })
    return @($names | Select-Object -Unique)
}