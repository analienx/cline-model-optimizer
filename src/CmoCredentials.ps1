# CmoCredentials.ps1 - local, encrypted storage of captured Cline logins.
#
# Cline persists one signed-in login in providers.json. Account rotation needs
# the OAuth material for each account, so the optimizer captures the login only
# after that account has been authenticated by Cline itself. Tokens and auth
# metadata are protected with Windows DPAPI (CurrentUser), kept under
# LOCALAPPDATA, and never logged or committed.

function Get-CmoCredStorePath {
    return (Join-Path $env:LOCALAPPDATA 'ClineModelOptimizer\account-credentials.json')
}

function Protect-CmoSecret([string]$Plain) {
    if (-not $Plain) { return '' }
    Add-Type -AssemblyName System.Security
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Plain)
    return [Convert]::ToBase64String(
        [System.Security.Cryptography.ProtectedData]::Protect(
            $bytes, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser))
}

function Unprotect-CmoSecret([string]$B64) {
    if (-not $B64) { return '' }
    try {
        Add-Type -AssemblyName System.Security
        $bytes = [System.Security.Cryptography.ProtectedData]::Unprotect(
            [Convert]::FromBase64String($B64), $null,
            [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
        return [System.Text.Encoding]::UTF8.GetString($bytes)
    } catch { return '' }
}

function Protect-CmoAuthMetadata([object]$Metadata) {
    if ($null -eq $Metadata) { return '' }
    try {
        return Protect-CmoSecret -Plain ($Metadata | ConvertTo-Json -Depth 20 -Compress)
    } catch { return '' }
}

function Unprotect-CmoAuthMetadata([string]$Encrypted) {
    if (-not $Encrypted) { return $null }
    try {
        $raw = Unprotect-CmoSecret -B64 $Encrypted
        if (-not $raw) { return $null }
        return ($raw | ConvertFrom-Json)
    } catch { return $null }
}

function Get-CmoCredStore {
    $fp = Get-CmoCredStorePath
    if (-not (Test-Path -LiteralPath $fp)) {
        return (New-Object System.Collections.Specialized.OrderedDictionary)
    }
    try {
        $j = ConvertFrom-CmoJsonTolerant -Text (Get-Content -LiteralPath $fp -Raw)
        if (-not $j) { return (New-Object System.Collections.Specialized.OrderedDictionary) }
        $out = New-Object System.Collections.Specialized.OrderedDictionary
        foreach ($p in @($j.PSObject.Properties)) {
            if ($p.Name) { $out[$p.Name] = $p.Value }
        }
        return $out
    } catch { return (New-Object System.Collections.Specialized.OrderedDictionary) }
}

function Save-CmoCredStore([object]$Store) {
    $fp = Get-CmoCredStorePath
    New-Item -ItemType Directory -Path (Split-Path $fp) -Force | Out-Null
    ($Store | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $fp -Encoding UTF8
    return $Store
}

function Find-CmoCredStoreKey {
    param([object]$Store, [string]$Email, [string]$AccountId)
    foreach ($k in @($Store.Keys)) {
        $entry = $Store[$k]
        if ($Email -and [string]::Equals([string]$k, $Email, [StringComparison]::OrdinalIgnoreCase)) {
            return [string]$k
        }
        if ($AccountId -and $entry -and [string]$entry.accountId -eq $AccountId) {
            return [string]$k
        }
    }
    return ''
}

function Get-CmoProvidersAuth {
    # Current login straight from providers.json. Values are never logged.
    try {
        $pp = Get-Content -LiteralPath (Join-Path (Get-CmoDataDir) 'settings\providers.json') -Raw | ConvertFrom-Json
        $a = $pp.providers.cline.settings.auth
        if ($a -and $a.accessToken) { return $a }
    } catch { }
    return $null
}

function ConvertTo-CmoExpiryMs {
    # Cline has returned both ISO timestamps and epoch milliseconds across
    # extension/API versions. Keep parsing here so a malformed refresh response
    # can never replace a known-good credential.
    param([object]$Value)
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) { return 0 }
    try {
        if ($Value -is [string] -and $Value -notmatch '^\d+$') {
            return [long]([DateTimeOffset]::Parse([string]$Value).ToUnixTimeMilliseconds())
        }
        $n = [long]$Value
        if ($n -gt 0 -and $n -lt 100000000000) { return ($n * 1000) }
        return $n
    } catch { return 0 }
}

function Invoke-CmoRefreshCapturedCredential {
    # Refresh one encrypted Cline OAuth record. This is deliberately independent
    # of providers.json: saved rotation accounts must stay fresh even while a
    # different account is the active Cline login. No token value is returned or
    # logged by this function.
    param(
        [Parameter(Mandatory = $true)][string]$Email,
        [int]$RefreshBeforeMinutes = 15,
        [switch]$Force
    )

    $cred = Get-CmoCredentialsFor -Email $Email
    if (-not $cred -or -not $cred.RefreshToken) {
        return [pscustomobject]@{ Email = $Email; Refreshed = $false; Reason = 'no-refresh-token' }
    }
    $now = [long]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
    $threshold = $now + ([math]::Max(0, $RefreshBeforeMinutes) * 60 * 1000)
    if (-not $Force -and $cred.ExpiresAtMs -gt $threshold) {
        return [pscustomobject]@{ Email = $Email; Refreshed = $false; Reason = 'not-due' }
    }

    try {
        # Match the Cline extension refresh contract exactly. The endpoint is
        # case-sensitive for these JSON property names.
        $body = @{ refreshToken = [string]$cred.RefreshToken; grantType = 'refresh_token' } |
            ConvertTo-Json -Compress
        $response = Invoke-RestMethod -Uri 'https://api.cline.bot/api/v1/auth/refresh' `
            -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 15
        $data = if ($response -and $response.data) { $response.data } else { $response }
        $access = [string]$data.accessToken
        $refresh = [string]$data.refreshToken
        $expires = ConvertTo-CmoExpiryMs -Value $data.expiresAt
        if (-not $access -or -not $refresh -or $expires -le $now) {
            return [pscustomobject]@{ Email = $Email; Refreshed = $false; Reason = 'invalid-refresh-response' }
        }

        $store = Get-CmoCredStore
        $key = Find-CmoCredStoreKey -Store $store -Email $Email
        if (-not $key -or -not $store[$key]) {
            return [pscustomobject]@{ Email = $Email; Refreshed = $false; Reason = 'credential-not-found' }
        }
        $entry = $store[$key]
        $entry | Add-Member -NotePropertyName accessToken -NotePropertyValue (Protect-CmoSecret -Plain $access) -Force
        $entry | Add-Member -NotePropertyName refreshToken -NotePropertyValue (Protect-CmoSecret -Plain $refresh) -Force
        $entry | Add-Member -NotePropertyName expiresAt -NotePropertyValue $expires -Force
        # Legacy credential records predate refreshedAt; Add-Member -Force both
        # migrates those records and updates newer ones safely.
        $entry | Add-Member -NotePropertyName refreshedAt -NotePropertyValue ((Get-Date).ToString('o')) -Force
        Save-CmoCredStore -Store $store | Out-Null
        return [pscustomobject]@{ Email = $key; Refreshed = $true; Reason = 'refreshed'; ExpiresAtMs = $expires }
    } catch {
        # Do not surface provider exception text: it can contain request context.
        $statusCode = 0
        $failureType = $_.Exception.GetType().FullName
        try {
            $response = $_.Exception.Response
            if ($response -and $response.StatusCode) { $statusCode = [int]$response.StatusCode }
        } catch { }
        $reason = if ($statusCode -in @(400, 401, 403)) { 'refresh-token-rejected' } else { 'refresh-request-failed' }
        return [pscustomobject]@{ Email = $Email; Refreshed = $false; Reason = $reason; StatusCode = $statusCode; FailureType = $failureType }
    }
}

function Update-CmoCapturedCredentialTokens {
    # Refresh every due saved login. OAuth refresh tokens rotate, so each result
    # is persisted immediately before the next account is attempted. A login that
    # Cline currently owns while VS Code is running must be excluded: refreshing
    # it here would rotate the refresh token underneath Cline's in-memory session.
    param(
        [int]$RefreshBeforeMinutes = 15,
        [string[]]$ExcludeEmail = @()
    )
    $results = @()
    foreach ($c in @(Get-CmoCapturedAccounts)) {
        if (-not $c.Email) { continue }
        $email = [string]$c.Email
        if (@($ExcludeEmail) -contains $email) {
            $results += [pscustomobject]@{ Email = $email; Refreshed = $false; Reason = 'active-owned-by-cline' }
            continue
        }
        $results += Invoke-CmoRefreshCapturedCredential -Email $email `
            -RefreshBeforeMinutes $RefreshBeforeMinutes
    }
    return @($results)
}

function Sync-CmoActiveClineCredential {
    # Make a freshly renewed active login available to Cline on its next start.
    # Never write while VS Code is running: its in-memory settings would race and
    # can overwrite the renewed token on shutdown.
    param([string]$Email)
    if (@(Get-Process -Name 'Code' -ErrorAction SilentlyContinue).Count -gt 0) { return $false }
    $cred = Get-CmoCredentialsFor -Email $Email
    if (-not $cred -or -not $cred.Token) { return $false }
    $ppPath = Join-Path (Get-CmoDataDir) 'settings\providers.json'
    if (-not (Test-Path -LiteralPath $ppPath)) { return $false }
    try {
        $pp = Get-Content -LiteralPath $ppPath -Raw | ConvertFrom-Json
        $auth = $pp.providers.cline.settings.auth
        if (-not $auth -or [string]$auth.accountId -ne [string]$cred.AccountId) { return $false }
        if ([string]$auth.accessToken -eq [string]$cred.Token -and [long]$auth.expiresAt -eq [long]$cred.ExpiresAtMs) { return $false }
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $bk = Join-Path $env:LOCALAPPDATA ('ClineModelOptimizer\backup-' + $stamp)
        New-Item -ItemType Directory -Path $bk -Force | Out-Null
        Copy-Item -LiteralPath $ppPath -Destination $bk -Force
        $auth.accessToken = [string]$cred.Token
        $auth.refreshToken = [string]$cred.RefreshToken
        $auth.expiresAt = [long]$cred.ExpiresAtMs
        $pp.providers.cline.settings.auth = $auth
        $pp.providers.cline.updatedAt = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffZ', [Globalization.CultureInfo]::InvariantCulture)
        $pp | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $ppPath -Force -Encoding UTF8
        return $true
    } catch { return $false }
}

function Update-CmoCapturedCredentials {
    # Capture/refresh the login Cline currently owns. If an earlier /users/me
    # lookup failed and produced unknown:<accountId>, retry identity resolution
    # later and re-key the same encrypted record to the real email.
    $a = Get-CmoProvidersAuth
    if (-not $a) { return '' }
    $acct = [string]$a.accountId
    if (-not $acct) { return '' }

    $store = Get-CmoCredStore
    $existingKey = Find-CmoCredStoreKey -Store $store -AccountId $acct
    $email = $existingKey
    $needsIdentity = (-not $existingKey -or $existingKey.StartsWith('unknown:', [StringComparison]::OrdinalIgnoreCase))
    if ($needsIdentity) {
        try {
            $hdr = @{ Authorization = ('Bearer ' + [string]$a.accessToken) }
            $me = Invoke-RestMethod -Uri 'https://api.cline.bot/api/v1/users/me' -Headers $hdr -Method Get -TimeoutSec 15
            if ($me.data -and $me.data.email) { $email = [string]$me.data.email }
        } catch { }
    }
    if (-not $email) { $email = ('unknown:' + $acct) }

    $existing = $null
    if ($existingKey) { $existing = $store[$existingKey] }
    $newExpiry = [long]$a.expiresAt
    $oldExpiry = 0
    if ($existing) { try { $oldExpiry = [long]$existing.expiresAt } catch { } }
    $identityChanged = ($existingKey -and -not [string]::Equals(
        $existingKey, $email, [StringComparison]::OrdinalIgnoreCase))
    $mustWrite = (-not $existing) -or $identityChanged -or ($newExpiry -gt $oldExpiry)

    # Backfill auth metadata for stores created by older optimizer builds even
    # when the token expiry itself has not changed.
    if ($existing -and -not $existing.PSObject.Properties['metadata'] -and $a.metadata) {
        $mustWrite = $true
    }
    if (-not $mustWrite) { return '' }

    if ($identityChanged) { $store.Remove($existingKey) }
    $store[$email] = [pscustomobject]@{
        accountId = $acct
        accessToken = (Protect-CmoSecret ([string]$a.accessToken))
        refreshToken = (Protect-CmoSecret ([string]$a.refreshToken))
        expiresAt = $newExpiry
        metadata = (Protect-CmoAuthMetadata $a.metadata)
        capturedAt = $(if ($existing -and $existing.capturedAt) {
            [string]$existing.capturedAt
        } else {
            (Get-Date).ToString('o')
        })
        refreshedAt = (Get-Date).ToString('o')
    }
    Save-CmoCredStore -Store $store | Out-Null
    return $email
}

function Get-CmoCapturedAccounts {
    # Public view: no token material leaves this function.
    $store = Get-CmoCredStore
    $nowMs = [long]([DateTimeOffset]::Now.ToUnixTimeMilliseconds())
    $out = @()
    foreach ($k in @($store.Keys)) {
        $c = $store[$k]
        $out += [pscustomobject]@{
            Email = [string]$k
            AccountId = [string]$c.accountId
            ExpiresAtMs = [long]$c.expiresAt
            Expired = ([long]$c.expiresAt -lt $nowMs)
            CapturedAt = [string]$c.capturedAt
            RefreshedAt = [string]$c.refreshedAt
        }
    }
    return @($out | Sort-Object Email)
}

function Get-CmoAccountRotationOrder {
    param([string]$LiveEmail)
    $live = [string]$LiveEmail
    $out = @()
    if ($live) { $out += $live }
    $captured = @()
    try { $captured = @(Get-CmoCapturedAccounts) } catch { }
    $sorted = @($captured | Sort-Object CapturedAt, Email)
    foreach ($c in @($sorted | Where-Object { -not $_.Expired })) {
        $e = [string]$c.Email
        if ($e -and ($out -notcontains $e)) { $out += $e }
    }
    foreach ($c in @($sorted | Where-Object { $_.Expired })) {
        $e = [string]$c.Email
        if ($e -and ($out -notcontains $e)) { $out += $e }
    }
    try {
        foreach ($a in @(Get-CmoAccounts)) {
            if (-not $a -or -not $a.Email) { continue }
            $e = [string]$a.Email
            if ($out -notcontains $e) { $out += $e }
        }
    } catch { }
    return @($out)
}

function Get-CmoCredentialsFor {
    # Internal apply path only. Decrypt exactly one account in memory.
    param([string]$Email)
    if (-not $Email) { return $null }
    $store = Get-CmoCredStore
    try {
        $key = Find-CmoCredStoreKey -Store $store -Email $Email
        if (-not $key) { return $null }
        $c = $store[$key]
        $metadata = $null
        if ($c.PSObject.Properties['metadata']) {
            $metadata = Unprotect-CmoAuthMetadata -Encrypted ([string]$c.metadata)
        }
        return @{
            Email = $key
            AccountId = [string]$c.accountId
            Token = (Unprotect-CmoSecret ([string]$c.accessToken))
            RefreshToken = (Unprotect-CmoSecret ([string]$c.refreshToken))
            ExpiresAtMs = [long]$c.expiresAt
            Metadata = $metadata
        }
    } catch { return $null }
}

function Remove-CmoCapturedCredentials {
    param([string]$Email)
    if (-not $Email) { return }
    $store = Get-CmoCredStore
    try {
        $key = Find-CmoCredStoreKey -Store $store -Email $Email
        if ($key) {
            $store.Remove($key)
            Save-CmoCredStore -Store $store | Out-Null
        }
    } catch { }
}
