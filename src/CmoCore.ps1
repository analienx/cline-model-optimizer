# CmoCore.ps1 - shared library for Cline Model Optimizer.
# Reads Cline's own data store (~\.cline\data) READ-ONLY.
# SECURITY: tokens/secrets are never read, returned or logged - only metadata
# (email, account id, expiry). secrets.json is never opened.

function Get-CmoDataDir {
    return Join-Path $env:USERPROFILE '.cline\data'
}

function Get-CmoRoutingConfig {
    param([string]$ConfigPath)
    if (-not $ConfigPath) { $ConfigPath = Join-Path $PSScriptRoot 'model-routing.json' }
    return (Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json)
}

# ---- tier classification ----
function Get-CmoModelTier {
    param(
        [string]$Model,
        [string]$Provider,
        [object]$Routing
    )
    if (-not $Model) { return 'UNKNOWN' }
    $m = $Model.ToLowerInvariant()

    if ($m -like 'cline-pass/*' -or $Provider -eq 'cline-pass') { return 'SUBSCRIPTION' }
    foreach ($p in $Routing.tierRules.freePrefixes) { if ($m -like ($p.ToLowerInvariant() + '*')) { return 'FREE' } }
    foreach ($s in $Routing.tierRules.freeSuffixes) { if ($m -like ('*' + $s.ToLowerInvariant())) { return 'FREE' } }
    foreach ($f in $Routing.tierRules.freeModels)  { if ($m -eq $f.ToLowerInvariant()) { return 'FREE' } }

    # dynamic: our own cached free list (fetched from Cline's public API)
    $c = Read-CmoFreeListCache -Path (Get-CmoFreeListCachePath)
    if ($c) {
        foreach ($id in $c.Free)      { if ($id -and $id.ToLowerInvariant() -eq $m) { return 'FREE' } }
        foreach ($id in $c.ClinePass) { if ($id -and $id.ToLowerInvariant() -eq $m) { return 'SUBSCRIPTION' } }
    }

    # dynamic: Cline's own cache of free models (older extension versions)
    $cache = Join-Path (Get-CmoDataDir) 'cache\cline_recommended_models.json'
    if (Test-Path -LiteralPath $cache) {
        try {
            $j = Get-Content -LiteralPath $cache -Raw | ConvertFrom-Json
            foreach ($fm in $j.free) { if ($fm.id -and $fm.id.ToLowerInvariant() -eq $m) { return 'FREE' } }
            foreach ($pm in $j.clinePass) { if ($pm.id -and $pm.id.ToLowerInvariant() -eq $m) { return 'SUBSCRIPTION' } }
        } catch { }
    }
    return 'PAID'
}

# ---- active mode config ----
function Get-CmoActiveState {
    # Returns per-mode active provider/model/reasoning from globalState.json.
    # Cline 4.x stores Cline Pass selections as provider 'cline-pass' +
    # <mode>ClinePassModelId - resolve the REAL active model for each mode.
    $gs = Join-Path (Get-CmoDataDir) 'globalState.json'
    if (-not (Test-Path -LiteralPath $gs)) { return $null }
    $g = Get-Content -LiteralPath $gs -Raw | ConvertFrom-Json
    $planModel = $g.planModeClineModelId
    if ($g.planModeApiProvider -eq 'cline-pass' -and $g.planModeClinePassModelId) { $planModel = $g.planModeClinePassModelId }
    $actModel = $g.actModeClineModelId
    if ($g.actModeApiProvider -eq 'cline-pass' -and $g.actModeClinePassModelId) { $actModel = $g.actModeClinePassModelId }
    return [pscustomobject]@{
        PlanProvider = $g.planModeApiProvider
        PlanModel    = $planModel
        PlanReasoning= $g.planModeReasoningEffort
        ActProvider  = $g.actModeApiProvider
        ActModel     = $actModel
        ActReasoning = $g.actModeReasoningEffort
        Raw          = $g
    }
}

# ---- provider accounts (metadata only - no tokens) ----
function Get-CmoAccounts {
    $pp = Join-Path (Get-CmoDataDir) 'settings\providers.json'
    if (-not (Test-Path -LiteralPath $pp)) { return @() }
    $p = Get-Content -LiteralPath $pp -Raw | ConvertFrom-Json
    $out = @()
    foreach ($prop in $p.providers.PSObject.Properties) {
        $s = $prop.Value.settings
        $email = $null; $accountId = $null; $expiresAt = $null
        if ($s.auth) {
            $email = $s.auth.metadata.userInfo.email
            $accountId = $s.auth.accountId
            $expiresAt = $s.auth.expiresAt
        }
        $out += [pscustomobject]@{
            Provider  = $prop.Name
            Model     = $s.model
            Reasoning = if ($s.reasoning) { $s.reasoning.effort } else { $null }
            Email     = $email
            AccountId = $accountId
            ExpiresAtMs = $expiresAt
            UpdatedAt = $prop.Value.updatedAt
            LastUsed  = ($p.lastUsedProvider -eq $prop.Name)
        }
    }
    return $out
}

function Get-CmoTokenRemainingHours {
    param([object]$Accounts)
    $c = $Accounts | Where-Object { $_.Provider -eq 'cline' } | Select-Object -First 1
    if (-not $c -or -not $c.ExpiresAtMs) { return $null }
    return [math]::Round((($c.ExpiresAtMs / 1000) - [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) / 3600.0, 1)
}

# ---- session history (from per-session JSON files) ----
function Get-CmoRecentSessions {
    param([int]$Count = 8)
    $dir = Join-Path (Get-CmoDataDir) 'sessions'
    if (-not (Test-Path -LiteralPath $dir)) { return @() }
    $rows = @()
    foreach ($d in (Get-ChildItem -LiteralPath $dir -Directory)) {
        $f = Join-Path $d.FullName ($d.Name + '.json')
        if (-not (Test-Path -LiteralPath $f)) { continue }
        try {
            $j = Get-Content -LiteralPath $f -Raw | ConvertFrom-Json
            $rows += [pscustomobject]@{
                SessionId = $j.session_id
                StartedAt = $j.started_at
                EndedAt   = $j.ended_at
                Status    = $j.status
                Provider  = $j.provider
                Model     = $j.model
                Cost      = $j.metadata.totalCost
                TokensIn  = $j.metadata.usage.inputTokens
                TokensOut = $j.metadata.usage.outputTokens
                Title     = ($j.metadata.title -split "`n")[0]
            }
        } catch { }
    }
    return @($rows | Sort-Object StartedAt -Descending | Select-Object -First $Count)
}

# ---- dynamic free list ----
# The Cline extension resolves its free models from a public endpoint:
#   GET https://api.cline.bot/api/v1/ai/cline/recommended-models
#   -> { recommended:[{id,name,description,tags}], free:[...], clinePass:[...] }
# (its per-task 'cline_recommended_models.json' cache name exists only as dead
# config in 4.1.17 - never written - so we do our own caching.)
#
# Resolution order (first source that yields >= 1 free model id wins):
#   1. fresh (< ttlHours) copy of our cache file
#   2. live fetch of the API (result written to our cache)
#   3. stale copy of our cache (real list, just old - flagged not-fresh)
#   4. Cline's own cache file, if this Cline version writes one
#   5. built-in fallback list from model-routing.json (extension defaults)
function Get-CmoFreeListCachePath {
    return Join-Path (Join-Path $env:LOCALAPPDATA 'ClineModelOptimizer') 'free-models.json'
}

function Read-CmoFreeListCache {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $j = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        return [pscustomobject]@{
            FetchedAt = $j.fetchedAt
            Source    = $j.source
            Free      = @($j.free | Where-Object { $_ })
            ClinePass = @($j.clinePass | Where-Object { $_ })
        }
    } catch { return $null }
}

function Write-CmoFreeListCache {
    param([string]$Path, [string]$Source, [string[]]$Free, [string[]]$ClinePass)
    try {
        $dir = Split-Path -Parent $Path
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        @{ fetchedAt = (Get-Date).ToString('o'); source = $Source
           free = @($Free); clinePass = @($ClinePass) } |
            ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $Path -Force -Encoding UTF8
    } catch { }
}

function Invoke-CmoFreeListFetch {
    param([string]$Endpoint, [int]$TimeoutSec = 10)
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $r = Invoke-RestMethod -Uri $Endpoint -Method Get -TimeoutSec $TimeoutSec `
                -Headers @{ 'User-Agent' = 'ClineModelOptimizer/1.0' } -ErrorAction Stop
        $free = @($r.free | ForEach-Object { $_.id } | Where-Object { $_ } | Select-Object -Unique)
        $pass = @($r.clinePass | ForEach-Object { $_.id } | Where-Object { $_ } | Select-Object -Unique)
        if ($free.Count -gt 0) {
            return [pscustomobject]@{ Free = $free; ClinePass = $pass
                                      FetchedAt = (Get-Date).ToString('o'); Source = 'api' }
        }
    } catch { }
    return $null
}

function Get-CmoDynamicFreeModels {
    # Returns the currently-available free model ids plus metadata:
    #   Models / ClinePass - id arrays
    #   Source             - api | api-cache | cline-cache | extension-defaults | none
    #   FetchedAt          - ISO timestamp of the underlying list
    #   AgeHours           - age of that list
    #   Fresh              - true when verified live (or within TTL)
    #   Error              - set when a live fetch was attempted and failed
    param(
        [object]$Routing,
        [switch]$Refresh,   # bypass the TTL and hit the API now
        [switch]$NoFetch    # resolve offline only (never touch the network)
    )
    $res = [pscustomobject]@{
        Models = @(); ClinePass = @(); Source = 'none'; FetchedAt = $null
        AgeHours = $null; Fresh = $false; Error = $null
    }
    $ttl = 6.0; $endpoint = $null; $fallback = @(); $timeoutSec = 10
    if ($Routing) {
        $dynCfg = $Routing.dynamic
        if ($dynCfg) {
            if ($dynCfg.ttlHours) { $ttl = [double]$dynCfg.ttlHours }
            if ($dynCfg.timeoutSeconds) { $timeoutSec = [int]$dynCfg.timeoutSeconds }
            if ($dynCfg.endpoint) { $endpoint = $dynCfg.endpoint }
            if ($dynCfg.fallbackFreeModels) { $fallback = @($dynCfg.fallbackFreeModels | Where-Object { $_ }) }
        }
    }

    # 1. fresh local cache
    $cachePath = Get-CmoFreeListCachePath
    $stale = $null; $staleAge = $null
    if (-not $Refresh) {
        $cached = Read-CmoFreeListCache -Path $cachePath
        if ($cached -and $cached.FetchedAt -and $cached.Free.Count -gt 0) {
            $age = [double]::PositiveInfinity
            try { $age = ((Get-Date) - [datetime]$cached.FetchedAt).TotalHours } catch { }
            if ($age -ge 0 -and $age -lt $ttl) {
                $res.Models = $cached.Free; $res.ClinePass = $cached.ClinePass
                $res.Source = ($cached.Source + '-cache'); $res.FetchedAt = $cached.FetchedAt
                $res.AgeHours = [math]::Round($age, 1); $res.Fresh = $true
                return $res
            }
            $stale = $cached; $staleAge = $age
        }
    }

    # 2. live fetch
    if (-not $NoFetch -and $endpoint) {
        $live = Invoke-CmoFreeListFetch -Endpoint $endpoint -TimeoutSec $timeoutSec
        if ($live) {
            Write-CmoFreeListCache -Path $cachePath -Source $live.Source -Free $live.Free -ClinePass $live.ClinePass
            $res.Models = $live.Free; $res.ClinePass = $live.ClinePass
            $res.Source = 'api'; $res.FetchedAt = $live.FetchedAt
            $res.AgeHours = 0; $res.Fresh = $true
            return $res
        }
        $res.Error = 'live fetch failed'
    }

    # 3. stale local cache (still a real list - better than built-in guesses)
    if ($stale) {
        $res.Models = $stale.Free; $res.ClinePass = $stale.ClinePass
        $res.Source = ($stale.Source + '-cache'); $res.FetchedAt = $stale.FetchedAt
        $res.AgeHours = [math]::Round($staleAge, 1); $res.Fresh = $false
        return $res
    }

    # 4. Cline's own cache file (older extension versions)
    $clineCache = Join-Path (Get-CmoDataDir) 'cache\cline_recommended_models.json'
    if (Test-Path -LiteralPath $clineCache) {
        try {
            $j = Get-Content -LiteralPath $clineCache -Raw | ConvertFrom-Json
            $free = @($j.free | ForEach-Object { $_.id } | Where-Object { $_ } | Select-Object -Unique)
            $pass = @($j.clinePass | ForEach-Object { $_.id } | Where-Object { $_ } | Select-Object -Unique)
            if ($free.Count -gt 0) {
                $res.Models = $free; $res.ClinePass = $pass; $res.Source = 'cline-cache'
                $res.FetchedAt = (Get-Item -LiteralPath $clineCache).LastWriteTime.ToString('o')
                try { $res.AgeHours = [math]::Round(((Get-Date) - (Get-Item -LiteralPath $clineCache).LastWriteTime).TotalHours, 1) } catch { }
                return $res
            }
        } catch { }
    }

    # 5. built-in fallback (the Cline extension's own hard-coded defaults)
    if ($fallback.Count -gt 0) {
        $res.Models = $fallback; $res.Source = 'extension-defaults'; $res.Fresh = $false
    }
    return $res
}

# ---- effective (resolved) strategy ladder ----
function Get-CmoEffectiveStrategy {
    # Builds the runtime ladder from the LIVE free list (1,2,3,... free slots):
    #   1. preferred free models that are ALSO in the dynamic free list
    #      (preferred order, up to freeSelection.maxFreeModels)
    #   2. remaining slots filled from the rest of the dynamic free list
    #      (when freeSelection.fillFromDynamic)
    #   3. if the dynamic list is unavailable: configured static free models
    #   4. subscription steps (annotated with live presence in the pass list)
    #   5. last resort: the static strategy ladder
    # Each step: provider, model, thinking, tier, source, live.
    param(
        [object]$Routing,
        [string]$Mode = 'act',
        [switch]$RefreshFreeList
    )

    $sel = $Routing.freeSelection
    $static = $null
    $strat = $Routing.strategies.($Routing.strategy)
    if ($strat) { $static = if ($Mode -eq 'plan') { $strat.plan } else { $strat.act } }
    $maxFree = if ($sel.maxFreeModels) { [int]$sel.maxFreeModels } else { 2 }
    if ($maxFree -lt 1) { $maxFree = 1 }

    $dynParams = @{ Routing = $Routing }
    if ($RefreshFreeList) { $dynParams.Refresh = $true }
    $dyn = Get-CmoDynamicFreeModels @dynParams
    $dynIds = @($dyn.Models | ForEach-Object { $_.ToLowerInvariant() } | Select-Object -Unique)
    $passIds = @($dyn.ClinePass | ForEach-Object { $_.ToLowerInvariant() } | Select-Object -Unique)
    $dynAvailable = ($dynIds.Count -gt 0)

    $freeStepSource = switch ($dyn.Source) {
        'api'                { 'dynamic-free' }
        'api-cache'          { 'dynamic-free' }
        'cline-cache'        { 'dynamic-free' }
        'extension-defaults' { 'default-free' }
        default              { 'static-free' }
    }

    $thinkingFor = { param($m) if ($static) { $s = $static | Where-Object { $_.model -eq $m } | Select-Object -First 1; if ($s -and $s.thinking) { $s.thinking } else { 'off' } } else { 'off' } }

    $steps = @(); $chosen = @()

    # 1. preferred free models present in the live free list (preferred order)
    foreach ($p in $sel.preferredFreeModels) {
        if ($steps.Count -ge $maxFree) { break }
        $pl = $p.ToLowerInvariant()
        if ($chosen -contains $pl) { continue }
        if ($dynAvailable -and ($dynIds -contains $pl)) {
            $steps += [pscustomobject]@{ provider='cline'; model=$p; thinking=(& $thinkingFor $p); tier='FREE'; source='dynamic-free'; live=$true }
            $chosen += $pl
        }
    }

    # 2. fill remaining free slots from the rest of the dynamic free list
    if ($sel.fillFromDynamic -ne $false -and $dynAvailable) {
        foreach ($id in $dynIds) {
            if ($steps.Count -ge $maxFree) { break }
            if ($chosen -contains $id) { continue }
            $steps += [pscustomobject]@{ provider='cline'; model=$id; thinking=(& $thinkingFor $id); tier='FREE'; source=$freeStepSource; live=$true }
            $chosen += $id
        }
    }

    # 3. dynamic list unavailable -> configured static free models
    if (-not $dynAvailable) {
        foreach ($p in $sel.preferredFreeModels) {
            if ($steps.Count -ge $maxFree) { break }
            $pl = $p.ToLowerInvariant()
            if ($chosen -contains $pl) { continue }
            $steps += [pscustomobject]@{ provider='cline'; model=$p; thinking=(& $thinkingFor $p); tier='FREE'; source='static-free'; live=$false }
            $chosen += $pl
        }
    }

    # 4. subscription steps
    foreach ($s in $sel.subscriptionSteps) {
        $live = ($passIds.Count -eq 0) -or ($passIds -contains $s.model.ToLowerInvariant())
        $steps += [pscustomobject]@{ provider=$s.provider; model=$s.model; thinking=$s.thinking; tier='SUBSCRIPTION'; source='subscription'; live=$live }
    }

    # 5. if nothing resolved at all, fall back to the static strategy ladder
    if ($steps.Count -eq 0 -and $static) {
        foreach ($s in $static) {
            $tier = Get-CmoModelTier -Model $s.model -Provider $s.provider -Routing $Routing
            $steps += [pscustomobject]@{ provider=$s.provider; model=$s.model; thinking=$s.thinking; tier=$tier; source='static'; live=$true }
        }
    }
    return [pscustomobject]@{ Steps = @($steps); Dynamic = $dyn; MaxFreeModels = $maxFree }
}

# ---- recommendation engine (uses the dynamic resolved ladder) ----
function Get-CmoRecommendation {
    param(
        [object]$Routing,
        [object]$State,
        [string]$Mode = 'act'   # act | plan
    )
    $eff = Get-CmoEffectiveStrategy -Routing $Routing -Mode $Mode
    $list = @($eff.Steps)
    $activeModel = if ($Mode -eq 'plan') { $State.PlanModel } else { $State.ActModel }
    $activeProvider = if ($Mode -eq 'plan') { $State.PlanProvider } else { $State.ActProvider }

    $rank = 0
    foreach ($step in $list) {
        $rank++
        $same = ($step.model -eq $activeModel -and $step.provider -eq $activeProvider)
        if ($same) {
            # free-first rule: only the TOP step is optimal. Subscription/paid
            # steps are fallbacks - using them while a free step exists higher
            # in the ladder is suboptimal by definition.
            return [pscustomobject]@{
                Optimal = ($rank -eq 1); Rank = $rank; Step = $step; Ladder = $list; Dynamic = $eff.Dynamic
                Message = $(if ($rank -eq 1) {
                    ("already using preferred #1: {0} [{1}]" -f $step.model, $step.tier)
                } else {
                    ("using #{0} [{1}] - higher-priority free step available: {2}" -f $rank, $step.tier, $list[0].model)
                })
            }
        }
    }
    $top = $list | Select-Object -First 1
    return [pscustomobject]@{
        Optimal = $false; Rank = 0; Step = $top; Ladder = $list; Dynamic = $eff.Dynamic
        Message = ("recommended: {0} ({1}) - currently {2} [{3}]" -f $top.model, $top.tier, $activeModel, (Get-CmoModelTier -Model $activeModel -Provider $activeProvider -Routing $Routing))
    }
}

# ---- full snapshot for dashboard/guardian ----
function Get-CmoSnapshot {
    param([object]$Routing)
    $state = Get-CmoActiveState
    $accounts = Get-CmoAccounts
    $snap = [pscustomobject]@{
        Timestamp    = (Get-Date -Format 'o')
        StateExists  = ($null -ne $state)
        Plan = $null
        Act  = $null
        Accounts = $accounts
        TokenRemainingHours = (Get-CmoTokenRemainingHours -Accounts $accounts)
        RecentSessions = (Get-CmoRecentSessions -Count 8)
        DynamicFree = $null
    }
    if ($state) {
        $snap.DynamicFree = Get-CmoDynamicFreeModels -Routing $Routing
        foreach ($mode in 'Plan','Act') {
            $provider = $state.($mode + 'Provider')
            $model = $state.($mode + 'Model')
            $tier = Get-CmoModelTier -Model $model -Provider $provider -Routing $Routing
            $rec = Get-CmoRecommendation -Routing $Routing -State $state -Mode $mode.ToLower()
            $snap.$mode = [pscustomobject]@{
                Provider = $provider; Model = $model
                Reasoning = $state.($mode + 'Reasoning')
                Tier = $tier
                Recommendation = $rec
            }
        }
    }
    return $snap
}
