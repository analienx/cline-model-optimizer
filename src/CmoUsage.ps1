# CmoUsage.ps1 - REAL usage + automatic 'free tier depleted' detection.
#
# WHAT THIS SOLVES: 'which account/model has burned its free tier today?'
#   1. Cline's own usage API answers HOW MUCH was used today, per model, per
#      account:  GET /api/v1/users/{id}/usages  (per-request records with model,
#      tokens, credits, timestamps). The Bearer token is in providers.json
#      (settings.auth.accessToken) - we already read that file; secrets.json is
#      never touched. GET /api/v1/users/me confirms which email the token is for.
#   2. WHEN a free cap is actually hit, Cline's extension classifies the API
#      error itself (verified in the extension source): INFERENCE_CAP_ERROR,
#      'status code 429', 'quota exceeded', 'resource exhausted',
#      'cline_free_promotion_ended'. We scan ONLY NEW transcript messages for
#      those markers and auto-mark the account + model depleted for today.
# HONEST LIMITS: there is no public 'remaining quota' number (so GREEN/RED is
#      detection, not prediction), and only the SIGNED-IN account has a token -
#      tracked-but-not-signed-in accounts become queryable once Cline logs in.

$script:CmoUsageStatePath = Join-Path $env:LOCALAPPDATA 'ClineModelOptimizer\cline-usage.json'

# high-specificity patterns (from the Cline extension's own error classifier);
# deliberately excludes generic words like 'rate limit'/'quota' alone so that
# ordinary conversation text (including chats ABOUT quotas) cannot false-positive.
$script:CmoCapPatterns = @(
    'INFERENCE_CAP_ERROR',
    'inference cap',
    'inference_cap',
    'status code 429',
    'quota exceeded',
    'quota_exceeded',
    'resource exhausted',
    'resource_exhausted',
    'cline_free_promotion_ended'
)

function Get-CmoUsageStatePath { return $script:CmoUsageStatePath }

function ConvertTo-CmoEpochLocal {
    # epoch milliseconds -> local DateTime (PS 5.1 safe, no ticks confusion)
    param([long]$Ms)
    try { return ([DateTimeOffset]::FromUnixTimeMilliseconds($Ms)).LocalDateTime } catch { return (Get-Date) }
}

function Get-CmoModelCoreId {
    # canonical core id so config ids, API raw_models and display names all
    # match: 'cline-free/muse-spark-1.3-contributor', 'zai/muse-spark-1.3' and
    # 'Muse Spark 1.3 Contributor' all reduce to a comparable key.
    param([string]$Id)
    $s = [string]$Id
    if (-not $s) { return '' }
    $s = $s.ToLowerInvariant().Trim()
    $s = $s -replace '\s+', '-'
    if ($s.Contains('/')) { $s = $s.Substring($s.LastIndexOf('/') + 1) }
    foreach ($p in @('cline-free-', 'cline-pass-')) {
        if ($s.StartsWith($p)) { $s = $s.Substring($p.Length) }
    }
    return $s
}

function Get-CmoEmptyUsageState {
    return [pscustomobject]@{
        day = (Get-CmoTodayKey)
        fetchedAt = $null          # ISO when the API was last asked
        email = ''                  # email the current token belongs to
        balanceCents = $null        # account credit balance (paid credits)
        perModel = @{}              # core id -> { display, providerType, requests, totalTokens, creditsUsed, firstAtMs, lastAtMs }
        perAccount = @{}            # email -> { fetchedAt, perModel } (rotation view: live + captured)
        autoAccounts = @{}          # '<email>'  -> epoch-ms when a pasted cap error was seen
        autoModels = @{}            # '<model>'  -> epoch-ms (secondary, from pasted errors)
        lastScanMs = 0              # transcript messages older than this are ignored
        lastScanAt = $null
        lastError = ''
    }
}

function ConvertTo-CmoCounterBag {
    # ONE model's counters -> OrderedDictionary (callers use .Contains/.Keys and
    # property-style access; a JSON PSCustomObject has no .Keys).
    param([object]$Entry)
    $m = New-Object System.Collections.Specialized.OrderedDictionary
    if ($null -eq $Entry) { return $m }
    if ($Entry -is [System.Collections.IDictionary]) {
        foreach ($k in @($Entry.Keys)) { if ("$k") { $m[[string]$k] = $Entry[$k] } }
        return $m
    }
    foreach ($q in @($Entry.PSObject.Properties)) { if ($q.Name) { $m[$q.Name] = $q.Value } }
    return $m
}

function ConvertTo-CmoPerModelBag {
    # core id -> counters. MUST be a real dictionary: this is what the inferred-
    # cap detector and the rotation planner walk, so a broken bag silently turns
    # 'this account is used up' into 'no cap detected' (and rotation never fires).
    param([object]$Bag)
    $out = New-Object System.Collections.Specialized.OrderedDictionary
    if ($null -eq $Bag) { return $out }
    if ($Bag -is [System.Collections.IDictionary]) {
        foreach ($k in @($Bag.Keys)) { if ("$k") { $out[[string]$k] = (ConvertTo-CmoCounterBag $Bag[$k]) } }
        return $out
    }
    foreach ($p in @($Bag.PSObject.Properties)) {
        if (-not $p.Name) { continue }
        $out[$p.Name] = (ConvertTo-CmoCounterBag $p.Value)
    }
    return $out
}

function ConvertTo-CmoAccountBag {
    # ONE account's usage entry -> { fetchedAt; perModel } with perModel as a REAL
    # dictionary. THIS FUNCTION WAS REFERENCED BUT MISSING for a while, and the
    # effect was brutal: loading any state that contained a perAccount entry threw
    # CommandNotFoundException, Get-CmoUsageState swallowed it and returned an EMPTY
    # state - so the whole account-rotation feature silently no-op'd forever.
    # Keep it defined; tests/Test-CmoRotation.ps1 round-trips this exact path.
    param([object]$Entry)
    if ($null -eq $Entry) {
        return [pscustomobject]@{ fetchedAt = $null; perModel = (New-Object System.Collections.Specialized.OrderedDictionary) }
    }
    $fa = $null; $pm = $null
    if ($Entry -is [System.Collections.IDictionary]) {
        if ($Entry.Contains('fetchedAt')) { $fa = $Entry['fetchedAt'] }
        if ($Entry.Contains('perModel')) { $pm = $Entry['perModel'] }
    } else {
        foreach ($p in @($Entry.PSObject.Properties)) {
            if ($p.Name -eq 'fetchedAt') { $fa = $p.Value }
            elseif ($p.Name -eq 'perModel') { $pm = $p.Value }
        }
    }
    return [pscustomobject]@{
        fetchedAt = $fa
        perModel = (ConvertTo-CmoPerModelBag -Bag $pm)
    }
}

function ConvertTo-CmoBag {
    # JSON round-trips turn hashtables into PSCustomObject; give callers a
    # consistent OrderedDictionary (with .Contains/.Keys/.Remove).
    param([object]$Bag, [string]$Kind)
    $out = New-Object System.Collections.Specialized.OrderedDictionary
    if ($null -eq $Bag) { return $out }
    if ($Bag -is [System.Collections.IDictionary]) {
        foreach ($k in @($Bag.Keys)) {
            if (-not "$k") { continue }
            if ($Kind -eq 'perModel')        { $out[[string]$k] = (ConvertTo-CmoCounterBag $Bag[$k]) }
            elseif ($Kind -eq 'perAccount')  { $out[[string]$k] = (ConvertTo-CmoAccountBag $Bag[$k]) }
            else                             { $out[[string]$k] = $Bag[$k] }
        }
        return $out
    }
    foreach ($p in @($Bag.PSObject.Properties)) {
        if (-not $p.Name) { continue }
        if ($Kind -eq 'perModel') {
            $out[$p.Name] = (ConvertTo-CmoCounterBag $p.Value)
        } elseif ($Kind -eq 'perAccount') {
            $out[$p.Name] = (ConvertTo-CmoAccountBag $p.Value)
        } else {
            $out[$p.Name] = $p.Value
        }
    }
    return $out
}

function Get-CmoUsageState {
    $fp = Get-CmoUsageStatePath
    if (-not (Test-Path -LiteralPath $fp)) { return (Get-CmoEmptyUsageState) }
    try {
        $j = ConvertFrom-CmoJsonTolerant -Text (Get-Content -LiteralPath $fp -Raw)
        if (-not $j) { return (Get-CmoEmptyUsageState) }
        $st = [pscustomobject]@{
            day = [string]$j.day
            fetchedAt = $j.fetchedAt
            email = [string]$j.email
            balanceCents = $j.balanceCents
            perModel = (ConvertTo-CmoBag -Bag $j.perModel -Kind 'perModel')
            perAccount = (ConvertTo-CmoBag -Bag $j.perAccount -Kind 'perAccount')
            autoAccounts = (ConvertTo-CmoBag -Bag $j.autoAccounts -Kind 'plain')
            autoModels = (ConvertTo-CmoBag -Bag $j.autoModels -Kind 'plain')
            lastScanMs = [long]$j.lastScanMs
            lastScanAt = $j.lastScanAt
            lastError = [string]$j.lastError
        }
        # day rollover: everything except lastScanMs dies at midnight
        if ($st.day -ne (Get-CmoTodayKey)) {
            $keepScan = $st.lastScanMs
            $st = Get-CmoEmptyUsageState
            $st.lastScanMs = $keepScan
        }
        return $st
    } catch { return (Get-CmoEmptyUsageState) }
}

function Save-CmoUsageState {
    param([object]$State)
    $fp = Get-CmoUsageStatePath
    New-Item -ItemType Directory -Path (Split-Path $fp) -Force | Out-Null
    ($State | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $fp -Encoding UTF8
    return $State
}
# ---- auth from providers.json (NEVER logged, never written anywhere) ----
function Get-CmoClineAuth {
    # returns @{ Token; AccountId; Email; ExpiresAtMs } or $null. Only 'cline'
    # provider rows carry a real Cline login.
    try {
        $pp = Get-Content -LiteralPath (Join-Path (Get-CmoDataDir) 'settings\providers.json') -Raw | ConvertFrom-Json
        $s = $pp.providers.cline.settings
        if (-not $s -or -not $s.auth -or -not $s.auth.accessToken) { return $null }
        $email = ''
        $acct = [string]$s.auth.accountId
        foreach ($a in @(Get-CmoAccounts)) {
            if ($a.AccountId -and ([string]$a.AccountId) -eq $acct -and $a.Email) { $email = [string]$a.Email; break }
        }
        return @{ Token = [string]$s.auth.accessToken; AccountId = $acct; Email = $email; ExpiresAtMs = [long]$s.auth.expiresAt }
    } catch { return $null }
}

# ---- usage API ----
function Invoke-CmoUsageForAuth {
    # today's usage per model for ANY account (token from providers.json OR a
    # captured login). PAGINATION: '?limit=50' + '?cursor=' (verified live; the
    # extension itself only reads page 1). Items deduped by id.
    param([string]$Token, [string]$AccountId, [string]$Email = '', [switch]$WithBalance)
    if (-not $Token -or -not $AccountId) { return $null }
    $hdr = @{ Authorization = ('Bearer ' + $Token); 'Content-Type' = 'application/json' }
    $base = 'https://api.cline.bot/api/v1'

    if (-not $Email) {
        try {
            $me = Invoke-RestMethod -Uri ($base + '/users/me') -Headers $hdr -Method Get -TimeoutSec 15
            if ($me.data -and $me.data.email) { $Email = [string]$me.data.email }
        } catch { }
    }

    $dayStart = (Get-Date).Date
    $perModel = @{}
    $seenIds = @{}
    $cursor = ''
    $pages = 0
    while ($pages -lt 30) {
        $pages++
        $u = $null
        try {
            $uri = $base + '/users/' + $AccountId + '/usages?limit=50'
            if ($cursor) { $uri = $uri + '&cursor=' + [uri]::EscapeDataString($cursor) }
            $u = Invoke-RestMethod -Uri $uri -Headers $hdr -Method Get -TimeoutSec 20
        } catch { return $null }
        $items = @($u.data.items)
        $fresh = 0
        foreach ($it in $items) {
            if ($seenIds.ContainsKey([string]$it.id)) { continue }
            $seenIds[[string]$it.id] = $true
            $when = [datetime]::MinValue
            try { $when = ([datetime]::Parse(([string]$it.createdAt))).ToLocalTime() } catch { }
            if ($when -ne [datetime]::MinValue) {
                if ($when -lt $dayStart) { continue }
                $fresh++
            }
            # canonical key: raw_model beats the display name
            $raw = ''
            try { $raw = [string]$it.metadata.raw_model } catch { }
            $key = Get-CmoModelCoreId -Id $raw
            if (-not $key) { $key = Get-CmoModelCoreId -Id ([string]$it.aiModelName) }
            if (-not $key) { $key = 'unknown' }
            if (-not $perModel.ContainsKey($key)) {
                $perModel[$key] = @{
                    display = [string]$it.aiModelName; providerType = [string]$it.aiModelTypeName
                    requests = 0; totalTokens = 0; creditsUsed = 0.0; firstAtMs = 0; lastAtMs = 0 }
            }
            $m = $perModel[$key]
            if (-not $m.display) { $m.display = [string]$it.aiModelName }
            $m.requests = [int]$m.requests + 1
            $tok = 0
            try { $tok = [int]$it.totalTokens } catch { }
            $m.totalTokens = [int]$m.totalTokens + $tok
            $cr = 0.0
            try { $cr = [double]$it.creditsUsed } catch { }
            $m.creditsUsed = [math]::Round([double]$m.creditsUsed + $cr, 2)
            $ts = 0
            try { $ts = ([DateTimeOffset]::Parse(([string]$it.createdAt))).ToUnixTimeMilliseconds() } catch { }
            if ($ts -gt 0) {
                if ($m.firstAtMs -eq 0 -or $ts -lt $m.firstAtMs) { $m.firstAtMs = $ts }
                if ($ts -gt $m.lastAtMs) { $m.lastAtMs = $ts }
            }
        }
        $cursor = [string]$u.data.nextToken
        # stop when no cursor, or the page held nothing new from today (newest-first)
        if (-not $cursor) { break }
        if ($fresh -eq 0) { break }
        if ($items.Count -eq 0) { break }
    }
    $balanceCents = $null
    if ($WithBalance) {
        try {
            $b = Invoke-RestMethod -Uri ($base + '/users/' + $AccountId + '/balance') -Headers $hdr -Method Get -TimeoutSec 15
            if ($b.data) { $balanceCents = [long]$b.data.balance }
        } catch { }
    }
    return @{ Email = $Email; BalanceCents = $balanceCents; PerModel = $perModel }
}

function Invoke-CmoUsageApiFetch {
    # live (signed-in) account fetch - thin wrapper over Invoke-CmoUsageForAuth
    $auth = Get-CmoClineAuth
    if (-not $auth) { return $null }
    return Invoke-CmoUsageForAuth -Token $auth.Token -AccountId $auth.AccountId -Email $auth.Email -WithBalance
}
# ---- transcript cap-hit scan ----
function ConvertTo-CmoTextBlocksText {
    # ONLY the 'text' blocks of a message. The other block types (thinking,
    # tool_result, tool_use) are where agent reasoning and tool output live -
    # a project that DISCUSSES error codes would false-positive on those.
    # A relayed API error is a plain user-visible text message.
    param([object]$Content)
    if ($null -eq $Content) { return '' }
    if ($Content -is [string]) { return $Content }
    if ($Content -is [System.Array]) {
        $sb = New-Object System.Text.StringBuilder
        foreach ($b in $Content) {
            if ($b -is [pscustomobject]) {
                $t = [string]$b.type
                if ($t -eq 'text' -and $b.text) { [void]$sb.Append([string]$b.text); [void]$sb.Append(' ') }
            } elseif ($b -is [string]) { [void]$sb.Append($b); [void]$sb.Append(' ') }
        }
        return $sb.ToString()
    }
    if ($Content -is [pscustomobject]) {
        if (([string]$Content.type) -eq 'text' -and $Content.text) { return [string]$Content.text }
        return ''
    }
    return [string]$Content
}

function Test-CmoCapMetaNoise([string]$Text) {
    # a pasted discussion ABOUT the detector (this tool's own docs/chats) must
    # never be read as a cap error. Long or meta-referencing text is skipped.
    if ($Text.Length -gt 800) { return $true }
    foreach ($w in @('CmoUsage', 'ClineModelOptimizer', 'cline-model-optimizer', 'cap pattern', 'pattern list', 'scanner', 'classifier')) {
        if ($Text.IndexOf($w, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
    }
    return $false
}

function Find-CmoCapPattern([string]$Text) {
    foreach ($p in $script:CmoCapPatterns) {
        if ($Text -and $Text.IndexOf($p, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { return $p }
    }
    return $null
}

function Invoke-CmoTranscriptScan {
    # scan NEW transcript messages (ts > lastScanMs) for cap-hit markers.
    # -SessionsDir lets tests point at a sandbox; production uses ~/.cline/data.
    param([string]$SessionsDir = '')
    if (-not $SessionsDir) { $SessionsDir = Join-Path (Get-CmoDataDir) 'sessions' }
    $st = Get-CmoUsageState
    $since = [long]$st.lastScanMs
    $maxMs = $since
    $hits = @()

    if (Test-Path -LiteralPath $SessionsDir) {
        # only transcripts touched in the last 6 h can hold unscanned messages
        $cutoff = (Get-Date).AddHours(-6)
        $files = @(Get-ChildItem -LiteralPath $SessionsDir -Recurse -Filter '*.messages.json' -File -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -gt $cutoff })
        foreach ($f in $files) {
            try {
                $j = ConvertFrom-CmoJsonTolerant -Text ([System.IO.File]::ReadAllText($f.FullName))
                if (-not $j -or -not $j.messages) { continue }
                foreach ($m in @($j.messages)) {
                    $ts = 0
                    try { $ts = [long]$m.ts } catch { }
                    if ($ts -le $since) { continue }
                    if ($ts -gt $maxMs) { $maxMs = $ts }
                    # STRUCTURAL GUARDS: a real relayed cap error is a short,
                    # user-role, text-block message. Thinking blocks, tool
                    # output and assistant chatter about error codes are noise.
                    if (([string]$m.role) -ne 'user') { continue }
                    $txt = ConvertTo-CmoTextBlocksText -Content $m.content
                    if (-not $txt) { continue }
                    if (Test-CmoCapMetaNoise -Text $txt) { continue }
                    $pat = Find-CmoCapPattern -Text $txt
                    if ($pat) { $hits += [pscustomobject]@{ Ts = $ts; Pattern = $pat; File = $f.Name } }
                }
            } catch { }
        }
    }

    $st.lastScanMs = $maxMs
    $st.lastScanAt = (Get-Date).ToString('o')
    if ($hits.Count -gt 0) {
        $auth = Get-CmoClineAuth
        $email = [string]$st.email
        if (-not $email -and $auth) { $email = [string]$auth.Email }
        if ($email) {
            $latest = ($hits | Sort-Object Ts | Select-Object -Last 1).Ts
            if (-not $st.autoAccounts.Contains($email)) { $st.autoAccounts[$email] = $latest }
            elseif ([long]$st.autoAccounts[$email] -lt $latest) { $st.autoAccounts[$email] = $latest }
        }
    }
    Save-CmoUsageState -State $st | Out-Null
    return $st
}
# ---- auto-depleted queries + refresh entry ----
function Get-CmoAutoDepletedInfo {
    # per email: the epoch-ms a cap hit was seen (or $null)
    param([string]$Email)
    if (-not $Email) { return $null }
    $st = Get-CmoUsageState
    try { if ($st.autoAccounts.Contains($Email)) { return [long]$st.autoAccounts[$Email] } } catch { }
    return $null
}

function Get-CmoAutoDepletedEmails {
    $st = Get-CmoUsageState
    try { return @($st.autoAccounts.Keys) } catch { return @() }
}

function Get-CmoCapHitModels {
    # models flagged as cap-hit today (from the scan; account-level today)
    $st = Get-CmoUsageState
    try { return @($st.autoModels.Keys) } catch { return @() }
}

function Clear-CmoAutoDepleted {
    # manual 'clear' on the dashboard: the flag stays gone until a genuinely NEW
    # error message appears (lastScanMs has already moved past the old one).
    param([string]$Email)
    if (-not $Email) { return }
    $st = Get-CmoUsageState
    try { if ($st.autoAccounts.Contains($Email)) { $st.autoAccounts.Remove($Email) } } catch { }
    Save-CmoUsageState -State $st | Out-Null
}

function Invoke-CmoUsageRefresh {
    # guardian entry: transcript scan + API fetch (cache-friendly, never throws)
    try { $st = Invoke-CmoTranscriptScan } catch { $st = Get-CmoUsageState }
    try {
        $r = Invoke-CmoUsageApiFetch
        if ($r) {
            $bag = @{}
            foreach ($k in @($r.PerModel.Keys)) { $bag[$k] = $r.PerModel[$k] }
            $st.perModel = $bag
            $st.email = [string]$r.Email
            $st.balanceCents = $r.BalanceCents
            $st.fetchedAt = (Get-Date).ToString('o')
            $st.lastError = ''
        } else {
            $st.lastError = 'usage API unreachable or token expired'
        }
    } catch { $st.lastError = $_.Exception.Message }
    Save-CmoUsageState -State $st | Out-Null
    return $st
}

function Get-CmoInferredCaps {
    # THE automatic depleted signal. Cline's cap error is UI-ONLY (never written
    # to transcripts, logs or state - verified), so the cap must be inferred from
    # the usage feed itself: a FREE model that was used heavily today, then STOPPED
    # while the account kept working, is capped. Returns core id -> lastAtMs.
    param([object]$State)
    $now = [long](([DateTimeOffset]::Now).ToUnixTimeMilliseconds())
    $out = New-Object System.Collections.Specialized.OrderedDictionary
    try {
        $latestAny = 0
        foreach ($k in @($State.perModel.Keys)) { $m = $State.perModel[$k]; if ([long]$m.lastAtMs -gt $latestAny) { $latestAny = [long]$m.lastAtMs } }
        foreach ($k in @($State.perModel.Keys)) {
            $m = $State.perModel[$k]
            if (([string]$m.providerType) -ne 'cline-free') { continue }
            if ([int]$m.requests -lt 5) { continue }
            $last = [long]$m.lastAtMs
            if ($last -le 0) { continue }
            # stopped >= 45 min ago, and the account kept working afterwards
            if (($now - $last) -lt 2700000) { continue }
            if ($latestAny -le $last) { continue }
            $out[$k] = $last
        }
    } catch { }
    return $out
}

function Get-CmoUsageTodayForModel {
    # cached usage line for one model id, e.g. '45 req / 2.1M tok today (last 09:41)'.
    # Matching is by canonical core id, so config ids ('cline-free/muse-spark-1.3-
    # contributor') match API display names ('Muse Spark 1.3 Contributor').
    param([string]$Model)
    if (-not $Model) { return '' }
    $st = Get-CmoUsageState
    try {
        $want = Get-CmoModelCoreId -Id $Model
        if (-not $want) { return '' }
        foreach ($k in @($st.perModel.Keys)) {
            if ($want -eq ([string]$k)) {
                $m = $st.perModel[$k]
                $tok = [double]$m.totalTokens
                $tokTxt = ('{0:N0}' -f $tok)
                if ($tok -ge 1000000) { $tokTxt = ('{0:N1}M' -f ($tok / 1000000.0)) }
                elseif ($tok -ge 1000) { $tokTxt = ('{0:N1}k' -f ($tok / 1000.0)) }
                $line = ('{0} req / {1} tok today' -f [int]$m.requests, $tokTxt)
                if ([long]$m.lastAtMs -gt 0) { $line += (' (last ' + (ConvertTo-CmoEpochLocal -Ms ([long]$m.lastAtMs)).ToString('HH:mm') + ')') }
                return $line
            }
        }
    } catch { }
    return ''
}

function Get-CmoUsageTodayForEmail {
    # TOTAL usage today for the account that owns the current token (the LIVE
    # login), split free vs subscription - every usage record the API returns
    # belongs to that one account.
    param([string]$Email)
    if (-not $Email) { return '' }
    $st = Get-CmoUsageState
    if (-not $st.email -or (([string]$st.email).ToLowerInvariant() -ne ([string]$Email).ToLowerInvariant())) { return '' }
    try {
        $req = 0; $tok = 0.0; $cr = 0.0; $freeReq = 0; $freeTok = 0.0
        foreach ($k in @($st.perModel.Keys)) {
            $m = $st.perModel[$k]
            $req += [int]$m.requests
            $tok += [double]$m.totalTokens
            $cr += [double]$m.creditsUsed
            if (([string]$m.providerType) -eq 'cline-free') { $freeReq += [int]$m.requests; $freeTok += [double]$m.totalTokens }
        }
        if ($req -eq 0) { return '' }
        $tokTxt = ('{0:N0}' -f $tok)
        if ($tok -ge 1000000) { $tokTxt = ('{0:N1}M' -f ($tok / 1000000.0)) }
        elseif ($tok -ge 1000) { $tokTxt = ('{0:N1}k' -f ($tok / 1000.0)) }
        return ('{0} req / {1} tok today (free {2} req) - API' -f $req, $tokTxt, $freeReq)
    } catch { }
    return ''
}

function Get-CmoUsageSummaryLine {
    # one compact line for the system card. The live account's email is already
    # shown on the LIVE accounts row and the 'driven by' line - not repeated here.
    $st = Get-CmoUsageState
    $parts = @()
    if ($st.fetchedAt) { $parts += ('fetched ' + ([datetime]$st.fetchedAt).ToString('HH:mm')) }
    if ($null -ne $st.balanceCents) { $parts += ('credits $' + ('{0:N2}' -f ([long]$st.balanceCents / 100.0))) }
    if ($st.lastError) { $parts += ('err: ' + [string]$st.lastError) }
    if ($parts.Count -eq 0) { return 'usage API: no data yet' }
    return ('usage API: ' + ($parts -join '  ·  '))
}

# ---- per-account usage (rotation data layer) ----
function Update-CmoAccountUsages {
    # refresh cached usage for every CAPTURED account (5-min TTL each). The live
    # account's usage lives in the top-level perModel bag; every account (live
    # included) also gets an entry under perAccount so the rotation planner sees
    # one uniform map of who-is-depleted-for-what.
    $st = Get-CmoUsageState
    if (-not $st.perAccount) {
        $st | Add-Member -NotePropertyName perAccount -NotePropertyValue (New-Object System.Collections.Specialized.OrderedDictionary) -Force
    }
    $nowMs = [long](([DateTimeOffset]::Now).ToUnixTimeMilliseconds())
    $nowIso = (Get-Date).ToString('o')

    # mirror the live account
    if ($st.email) {
        $bag = New-Object System.Collections.Specialized.OrderedDictionary
        foreach ($k in @($st.perModel.Keys)) { $bag[$k] = $st.perModel[$k] }
        $st.perAccount[$st.email] = @{ fetchedAt = $st.fetchedAt; perModel = $bag }
    }

    foreach ($c in @(Get-CmoCapturedAccounts)) {
        if ($c.Email -eq $st.email) { continue }
        if ($c.Expired) { continue }
        $fresh = $false
        try {
            if ($st.perAccount.Contains($c.Email)) {
                $prev = $st.perAccount[$c.Email]
                $fa = 0
                try { $fa = [long](([DateTimeOffset]::Parse(([string]$prev.fetchedAt))).ToUnixTimeMilliseconds()) } catch { }
                if (($nowMs - $fa) -lt 300000) { $fresh = $true }   # 5 min TTL
            }
        } catch { }
        if ($fresh) { continue }
        $cred = Get-CmoCredentialsFor -Email $c.Email
        if (-not $cred -or -not $cred.Token) { continue }
        $r = Invoke-CmoUsageForAuth -Token $cred.Token -AccountId $cred.AccountId -Email $c.Email
        if ($r) {
            $st.perAccount[$c.Email] = @{ fetchedAt = $nowIso; perModel = $r.PerModel }
        }
    }
    Save-CmoUsageState -State $st | Out-Null
    return $st
}

function Get-CmoAccountModelCaps {
    # email -> { core -> lastAtMs } of inferred-capped free models, for every
    # account we have usage data for (live + captured).
    $st = Get-CmoUsageState
    $out = New-Object System.Collections.Specialized.OrderedDictionary
    try {
        if (-not $st.perAccount) { return $out }
        foreach ($e in @($st.perAccount.Keys)) {
            $pa = $st.perAccount[$e]
            $shim = [pscustomobject]@{ perModel = $pa.perModel }
            $out[$e] = Get-CmoInferredCaps -State $shim
        }
    } catch { }
    return $out
}

function Get-CmoRotationContext {
    # one call for planners + UI: caps per account, live email, captured tokens
    $st = Get-CmoUsageState
    return [pscustomobject]@{
        Caps = (Get-CmoAccountModelCaps)
        LiveEmail = [string]$st.email
        Captured = @(Get-CmoCapturedAccounts)
        State = $st
    }
}