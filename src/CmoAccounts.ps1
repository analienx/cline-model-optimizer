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

function ConvertFrom-CmoJsonTolerant {
    # Windows PowerShell 5.1's ConvertFrom-Json HARD-FAILS on a JSON member whose
    # name is an empty string ("": 1) - it says 'use -AsHashTable', which 5.1 does
    # not have. One such member would otherwise make us lose the WHOLE prefs file,
    # so on failure we strip empty-name members and retry. (This tool never writes
    # them; they can only come from hand-editing or an older buggy version.)
    param([string]$Text)
    if (-not $Text -or -not $Text.Trim()) { return $null }
    try { return ($Text | ConvertFrom-Json) } catch { }
    try {
        $val = '("(?:[^"\\]|\\.)*"|null|true|false|-?\d+(?:\.\d+)?)'
        $t = [regex]::Replace($Text, (',\s*""\s*:\s*' + $val), '')
        $t = [regex]::Replace($t, ('""\s*:\s*' + $val + '\s*,'), '')
        $t = [regex]::Replace($t, ('""\s*:\s*' + $val), '')
        return ($t | ConvertFrom-Json)
    } catch { return $null }
}

function New-CmoPrefsBag { return (New-Object System.Collections.Specialized.OrderedDictionary) }

function Normalize-CmoPrefsBag {
    # rebuild a bag keeping only real string keys with a usable value. This also
    # erases phantom keys (a $null/empty name) left by older versions.
    param([object]$Bag, [string]$Kind)
    $out = New-Object System.Collections.Specialized.OrderedDictionary
    if ($null -eq $Bag) { return $out }
    foreach ($prop in @($Bag.PSObject.Properties)) {
        $k = [string]$prop.Name
        if (-not $k -or -not $k.Trim()) { continue }
        $v = $prop.Value
        if ($Kind -eq 'budgets') {
            $n = 0
            if (-not [int]::TryParse([string]$v, [ref]$n)) { continue }
            if ($n -lt 1) { continue }
            $out[$k] = $n
        } else {
            $sv = [string]$v
            if (-not $sv -or -not $sv.Trim()) { continue }
            $out[$k] = $sv
        }
    }
    return $out
}

function Get-CmoAccountPrefs {
    # shape: { budgets: { <email>: <minutes> }, depleted: { <email>: 'yyyy-MM-dd' } }
    $empty = [pscustomobject]@{ budgets = (New-CmoPrefsBag); depleted = (New-CmoPrefsBag) }
    $fp = Get-CmoAccountPrefsPath
    if (-not (Test-Path -LiteralPath $fp)) { return $empty }
    try {
        $j = ConvertFrom-CmoJsonTolerant -Text (Get-Content -LiteralPath $fp -Raw)
        if (-not $j) { return $empty }
        return [pscustomobject]@{
            budgets  = (Normalize-CmoPrefsBag -Bag $j.budgets -Kind 'budgets')
            depleted = (Normalize-CmoPrefsBag -Bag $j.depleted -Kind 'depleted')
        }
    } catch {
        return $empty
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
    try { if ($p.budgets.Contains($Email)) { $v = $p.budgets[$Email] } } catch { $v = $null }
    if ($null -eq $v) { return (Get-CmoDefaultBudgetMin) }
    try { $n = [int]$v } catch { return (Get-CmoDefaultBudgetMin) }
    if ($n -lt 1) { return (Get-CmoDefaultBudgetMin) }
    return $n
}

function Set-CmoAccountBudgetMin {
    param([string]$Email, [int]$Minutes)
    if (-not $Email -or -not $Email.Trim()) { throw 'email required' }
    if ($Minutes -lt 1) { $Minutes = 1 }
    if ($Minutes -gt 1440) { $Minutes = 1440 }
    $p = Get-CmoAccountPrefs
    $p.budgets[$Email] = [int]$Minutes
    Save-CmoAccountPrefs -Prefs $p | Out-Null
    return [int]$Minutes
}

function Test-CmoAccountDepleted {
    param([string]$Email)
    if (-not $Email) { return $false }
    $p = Get-CmoAccountPrefs
    try {
        if (-not $p.depleted.Contains($Email)) { return $false }
        return ([string]$p.depleted[$Email] -eq (Get-CmoTodayKey))
    } catch { return $false }
}

function Set-CmoAccountDepleted {
    # marks the account 'used up today' (dashboard), or clears the marker.
    # clearing REMOVES the key - never leaves an empty value behind.
    param([string]$Email, [switch]$Off)
    if (-not $Email -or -not $Email.Trim()) { throw 'email required' }
    $p = Get-CmoAccountPrefs
    if ($Off) { $p.depleted.Remove($Email) }
    else { $p.depleted[$Email] = (Get-CmoTodayKey) }
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
    foreach ($bag in @($p.budgets, $p.depleted)) {
        try { if ($bag.Contains($Email)) { $bag.Remove($Email) } } catch { }
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
        # automatic detection outranks the estimated health
        $autoAt = Get-CmoAutoDepletedInfo -Email $g.Email
        $g | Add-Member -NotePropertyName AutoDepletedMs -NotePropertyValue $autoAt -Force
        if ($autoAt) {
            $g.Health = 'RED'
            $g.HealthLabel = ('free cap hit (auto-detected ' + (ConvertTo-CmoEpochLocal -Ms ([long]$autoAt)).ToString('HH:mm') + ')')
        }
        # inferred cap: the usage feed shows a free model that stopped while work
        # continued (Cline's cap error itself is UI-only and never hits disk).
        # Only the token owner (LIVE account) has usage data.
        # IMPORTANT: Cline's free tier is PER MODEL, so one capped free model must
        # NOT paint the whole account red - that contradicted the ROTATION card,
        # which still offered a free model on this very account. The account lamp
        # answers 'has this account got any free model left?':
        #   every pinned free model capped -> RED   (nothing left here)
        #   some capped, some left         -> AMBER (partly used up)
        if ($g.Health -ne 'RED') {
            $uSt = Get-CmoUsageState
            if ($uSt.email -and (([string]$uSt.email).ToLowerInvariant() -eq ([string]$g.Email).ToLowerInvariant())) {
                $caps = Get-CmoInferredCaps -State $uSt
                if ($caps.Count -gt 0) {
                    $pinned = @()
                    try { $pinned = @($(Get-CmoRoutingConfig).freeSelection.preferredFreeModels | Where-Object { $_ }) } catch { }
                    $total = $pinned.Count
                    $cappedN = 0
                    foreach ($p in $pinned) {
                        $pc = Get-CmoModelCoreId -Id ([string]$p)
                        if ($pc -and $caps.Contains($pc)) { $cappedN++ }
                    }
                    $capKeys = @($caps.Keys)
                    $last = [long]$caps[$capKeys[0]]
                    $when = (ConvertTo-CmoEpochLocal -Ms $last).ToString('HH:mm')
                    if ($total -gt 0 -and $cappedN -ge $total) {
                        $g.Health = 'RED'
                        $g.HealthLabel = ('all ' + $total + ' free models used up today (last stopped ' + $when + ')')
                    } else {
                        $g.Health = 'AMBER'
                        if ($total -gt 0) {
                            $g.HealthLabel = ('' + $cappedN + ' of ' + $total + ' free models used up today (' + $when + ')')
                        } else {
                            $g.HealthLabel = ('some free models used up today (' + $when + ')')
                        }
                    }
                }
            }
        }
        $g | Add-Member -NotePropertyName ApiUsage -NotePropertyValue '' -Force
        # LIVE account (token owner): show the account TOTAL from the usage API;
        # other accounts: fall back to a per-model line if their model was seen.
        $tot = Get-CmoUsageTodayForEmail -Email $g.Email
        if ($tot) { $g.ApiUsage = $tot }
        else {
            foreach ($mid in @($g.Models)) {
                $u = Get-CmoUsageTodayForModel -Model ([string]$mid)
                if ($u) { $g.ApiUsage = $u; break }
            }
        }
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