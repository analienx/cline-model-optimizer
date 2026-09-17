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
