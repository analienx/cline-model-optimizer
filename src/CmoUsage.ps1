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

function Get-CmoEmptyUsageState {
    return [pscustomobject]@{
        day = (Get-CmoTodayKey)
        fetchedAt = $null          # ISO when the API was last asked
        email = ''                  # email the current token belongs to
        perModel = @{}              # '<model>' -> { requests, totalTokens, creditsUsed, lastAt }
        autoAccounts = @{}          # '<email>'  -> epoch-ms when the cap hit was seen
        autoModels = @{}            # '<model>'  -> epoch-ms when the cap hit was seen
        lastScanMs = 0              # transcript messages older than this are ignored
        lastScanAt = $null
        lastError = ''
    }
}

function ConvertTo-CmoBag {
    # JSON round-trips turn hashtables into PSCustomObject; give callers a
    # consistent OrderedDictionary (with .Contains/.Keys/.Remove).
    param([object]$Bag, [string]$Kind)
    $out = New-Object System.Collections.Specialized.OrderedDictionary
    if ($null -eq $Bag) { return $out }
    if ($Bag -is [System.Collections.IDictionary]) {
        foreach ($k in @($Bag.Keys)) { $out[$k] = $Bag[$k] }
        return $out
    }
    foreach ($p in @($Bag.PSObject.Properties)) {
        if (-not $p.Name) { continue }
        if ($Kind -eq 'perModel') {
            # each model entry: normalize its counters too
            $v = $p.Value
            $m = New-Object System.Collections.Specialized.OrderedDictionary
            if ($v) {
                foreach ($q in @($v.PSObject.Properties)) {
                    $m[$q.Name] = $q.Value
                }
            }
            $out[$p.Name] = $m
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
            perModel = (ConvertTo-CmoBag -Bag $j.perModel -Kind 'perModel')
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
function Invoke-CmoUsageApiFetch {
    # live fetch: today's usage per model + the email the token belongs to.
    # returns $null on failure (caller keeps the cache).
    $auth = Get-CmoClineAuth
    if (-not $auth) { return $null }
    $hdr = @{ Authorization = ('Bearer ' + $auth.Token); 'Content-Type' = 'application/json' }
    $base = 'https://api.cline.bot/api/v1'

    $email = $auth.Email
    try {
        $me = Invoke-RestMethod -Uri ($base + '/users/me') -Headers $hdr -Method Get -TimeoutSec 15
        if ($me.data -and $me.data.email) { $email = [string]$me.data.email }
    } catch { }

    $dayStart = (Get-Date).Date
    $perModel = @{}
    $next = $null
    $pages = 0
    while ($pages -lt 20) {
        $pages++
        $u = $null
        try {
            $uri = $base + '/users/' + $auth.AccountId + '/usages'
            if ($next) { $uri = $uri + '?nextToken=' + [uri]::EscapeDataString($next) }
            $u = Invoke-RestMethod -Uri $uri -Headers $hdr -Method Get -TimeoutSec 20
        } catch { return $null }
        $items = @($u.data.items)
        $sawFresh = $false
        foreach ($it in $items) {
            $when = [datetime]::MinValue
            try { $when = [datetime]::Parse(([string]$it.createdAt), [System.Globalization.CultureInfo]::InvariantCulture, 'RoundtripKind') } catch { }
            if ($when -ne [datetime]::MinValue) {
                $local = $when.ToLocalTime()
                if ($local -lt $dayStart) { continue }
                $sawFresh = $true
            }
            $model = [string]$it.aiModelName
            if (-not $model) { $model = [string]$it.aiModelTypeName }
            if (-not $model) { $model = '(unknown model)' }
            if (-not $perModel.ContainsKey($model)) {
                $perModel[$model] = @{ requests = 0; totalTokens = 0; creditsUsed = 0.0; lastAt = '' }
            }
            $m = $perModel[$model]
            $m.requests = [int]$m.requests + 1
            $tok = 0
            try { $tok = [int]$it.totalTokens } catch { }
            $m.totalTokens = [int]$m.totalTokens + $tok
            $cr = 0.0
            try { $cr = [double]$it.creditsUsed } catch { }
            $m.creditsUsed = [math]::Round([double]$m.creditsUsed + $cr, 2)
            if ($it.createdAt) { $m.lastAt = [string]$it.createdAt }
        }
        $next = $u.data.nextToken
        # stop when no token, or the page held nothing from today (newest-first list)
        if (-not $next) { break }
        if (-not $sawFresh -and $items.Count -gt 0) { break }
        if ($items.Count -eq 0) { break }
    }
    return @{ Email = $email; PerModel = $perModel }
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
            $st.fetchedAt = (Get-Date).ToString('o')
            $st.lastError = ''
        } else {
            $st.lastError = 'usage API unreachable or token expired'
        }
    } catch { $st.lastError = $_.Exception.Message }
    Save-CmoUsageState -State $st | Out-Null
    return $st
}

function Get-CmoUsageTodayForModel {
    # cached usage line for one model id, e.g. '12 req / 1.4M tok / 3.2 cr today'.
    # The API's model names can differ slightly from config names
    # ('cline-pass/glm-5.3' vs 'cline-pass/glm-5.3-flash'), so exact match first,
    # then a suffix-tolerant match.
    param([string]$Model)
    if (-not $Model) { return '' }
    $st = Get-CmoUsageState
    try {
        $want = $Model.ToLowerInvariant()
        $match = $null
        foreach ($k in @($st.perModel.Keys)) {
            if (-not $k) { continue }
            $have = ([string]$k).ToLowerInvariant()
            if ($have -eq $want) { $match = $st.perModel[$k]; break }
        }
        if (-not $match) {
            foreach ($k in @($st.perModel.Keys)) {
                if (-not $k) { continue }
                $have = ([string]$k).ToLowerInvariant()
                if ($have.StartsWith($want) -or $want.StartsWith($have)) {
                    # require a '-' or end boundary so 'glm-5.3' can't match 'glm-5.30'
                    if ($have.StartsWith($want) -and ($have.Length -eq $want.Length -or $have[$want.Length] -eq '-')) { $match = $st.perModel[$k]; break }
                    if ($want.StartsWith($have) -and ($want.Length -eq $have.Length -or $want[$have.Length] -eq '-')) { $match = $st.perModel[$k]; break }
                }
            }
        }
        if ($match) {
            $tok = [double]$match.totalTokens
            $tokTxt = ('{0:N0}' -f $tok)
            if ($tok -ge 1000000) { $tokTxt = ('{0:N1}M' -f ($tok / 1000000.0)) }
            elseif ($tok -ge 1000) { $tokTxt = ('{0:N1}k' -f ($tok / 1000.0)) }
            return ('{0} req / {1} tok / {2} cr today' -f [int]$match.requests, $tokTxt, [double]$match.creditsUsed)
        }
    } catch { }
    return ''
}

function Get-CmoUsageTodayForEmail {
    # TOTAL usage today for the account that owns the current token (the LIVE
    # login) - every usage record the API returns belongs to that one account.
    param([string]$Email)
    if (-not $Email) { return '' }
    $st = Get-CmoUsageState
    if (-not $st.email -or (([string]$st.email).ToLowerInvariant() -ne ([string]$Email).ToLowerInvariant())) { return '' }
    try {
        $req = 0; $tok = 0.0; $cr = 0.0
        foreach ($k in @($st.perModel.Keys)) {
            $m = $st.perModel[$k]
            $req += [int]$m.requests
            $tok += [double]$m.totalTokens
            $cr += [double]$m.creditsUsed
        }
        if ($req -eq 0) { return '' }
        $tokTxt = ('{0:N0}' -f $tok)
        if ($tok -ge 1000000) { $tokTxt = ('{0:N1}M' -f ($tok / 1000000.0)) }
        elseif ($tok -ge 1000) { $tokTxt = ('{0:N1}k' -f ($tok / 1000.0)) }
        return ('{0} req / {1} tok / {2} cr today (API)' -f $req, $tokTxt, [math]::Round($cr, 2))
    } catch { }
    return ''
}

function Get-CmoUsageSummaryLine {
    # one compact line for the accounts card footnote / system card
    $st = Get-CmoUsageState
    $parts = @()
    if ($st.email) { $parts += ('live: ' + [string]$st.email) }
    if ($st.fetchedAt) { $parts += ('fetched ' + ([datetime]$st.fetchedAt).ToString('HH:mm')) }
    if ($st.lastError) { $parts += ('err: ' + [string]$st.lastError) }
    if ($parts.Count -eq 0) { return 'usage API: no data yet' }
    return ('usage API: ' + ($parts -join '  |  '))
}