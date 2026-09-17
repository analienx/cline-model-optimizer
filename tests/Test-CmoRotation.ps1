$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$src = Join-Path $repo 'src'
. (Join-Path $src 'CmoCore.ps1')

$script:passed = 0
function Assert-Cmo {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
    $script:passed++
}

# Never depend on Cline's network/cache in regression tests.
$routing = Get-Content -LiteralPath (Join-Path $src 'model-routing.json') -Raw | ConvertFrom-Json
$preferred = @($routing.freeSelection.preferredFreeModels)
function Get-CmoDynamicFreeModels {
    param([object]$Routing, [switch]$Refresh)
    return [pscustomobject]@{
        Models = @($preferred)
        ClinePass = @($Routing.freeSelection.subscriptionSteps | ForEach-Object { $_.model })
        Source = 'test'
        Fresh = $true
    }
}

$live = 'alpha@example.com'
$other = 'beta@example.com'
$script:CapturedForTest = @(
    [pscustomobject]@{
        Email = $other
        Expired = $false
        CapturedAt = '2026-09-17T10:00:00Z'
    }
)
function Get-CmoCapturedAccounts { return @($script:CapturedForTest) }
function Get-CmoAccountRotationOrder {
    param([string]$LiveEmail)
    return @($LiveEmail, $other)
}

function New-TestUsageState {
    $perAccount = New-Object System.Collections.Specialized.OrderedDictionary
    foreach ($email in @($live, $other)) {
        $perAccount[$email] = [pscustomobject]@{
            fetchedAt = '2026-09-17T10:00:00Z'
            perModel = (New-Object System.Collections.Specialized.OrderedDictionary)
        }
    }
    return [pscustomobject]@{ email = $live; perAccount = $perAccount }
}

function New-TestCaps {
    param([string[]]$LiveModels = @(), [string[]]$OtherModels = @())
    $out = New-Object System.Collections.Specialized.OrderedDictionary
    foreach ($entry in @(@($live, $LiveModels), @($other, $OtherModels))) {
        $bag = New-Object System.Collections.Specialized.OrderedDictionary
        foreach ($m in @($entry[1])) {
            if ($m) { $bag[$m] = 1 }
        }
        $out[[string]$entry[0]] = $bag
    }
    return $out
}

$usage = New-TestUsageState
$m1 = Get-CmoModelCoreId -Id ([string]$preferred[0])
$m2 = Get-CmoModelCoreId -Id ([string]$preferred[1])
$act = [pscustomobject]@{ Model = [string]$preferred[0]; Tier = 'FREE' }

# Current account still has the current preferred model -> no switch.
$plan = Get-CmoAutoSwitchPlan -Routing $routing -Act $act -UsageState $usage `
    -AccountCaps (New-TestCaps) -LiveEmail $live
Assert-Cmo ($null -eq $plan) 'green live account should not switch'

# Current model depleted only on live account -> same model on next captured account.
$plan = Get-CmoAutoSwitchPlan -Routing $routing -Act $act -UsageState $usage `
    -AccountCaps (New-TestCaps -LiveModels @($m1)) -LiveEmail $live
Assert-Cmo ($plan.Kind -eq 'account-switch') 'current-model cap should rotate account first'
Assert-Cmo ($plan.To -eq [string]$preferred[0]) 'account switch must keep current model'
Assert-Cmo ($plan.Account -eq $other) 'account switch must choose next usable account'

# Current model depleted everywhere, next preferred model only free on other account.
$plan = Get-CmoAutoSwitchPlan -Routing $routing -Act $act -UsageState $usage `
    -AccountCaps (New-TestCaps -LiveModels @($m1, $m2) -OtherModels @($m1)) -LiveEmail $live
Assert-Cmo ($plan.Kind -eq 'both') 'planner should combine account + model switch'
Assert-Cmo ($plan.To -eq [string]$preferred[1]) 'combined switch should advance one preferred model'
Assert-Cmo ($plan.Account -eq $other) 'combined switch should target account with budget'

# Same model is free elsewhere but that login has never been captured -> ask for sign-in.
$script:CapturedForTest = @()
$plan = Get-CmoAutoSwitchPlan -Routing $routing -Act $act -UsageState $usage `
    -AccountCaps (New-TestCaps -LiveModels @($m1)) -LiveEmail $live
Assert-Cmo ($plan.Kind -eq 'needs-signin') 'uncaptured account must require sign-in, not mutate'
Assert-Cmo ($plan.Account -eq $other) 'sign-in request should identify next tracked account'
$script:CapturedForTest = @([pscustomobject]@{ Email=$other; Expired=$false; CapturedAt='2026-09-17T10:00:00Z' })

# JSON round-trip: nested per-account/per-model data must stay dictionary-shaped.
$sample = '{"alpha@example.com":{"fetchedAt":"2026-09-17T10:00:00Z","perModel":{"model-a":{"requests":2,"lastAtMs":10}}}}' | ConvertFrom-Json
$bag = ConvertTo-CmoBag -Bag $sample -Kind 'perAccount'
Assert-Cmo ($bag -is [System.Collections.IDictionary]) 'perAccount must normalize to a dictionary'
Assert-Cmo ($bag[$live].perModel -is [System.Collections.IDictionary]) 'perModel must normalize to a dictionary'
Assert-Cmo ($bag[$live].perModel['model-a'].Contains('requests')) 'counter bag must expose dictionary keys'

# DPAPI round-trip + auth metadata shape.
$secret = 'rotation-regression-secret'
$enc = Protect-CmoSecret -Plain $secret
Assert-Cmo ($enc -and $enc -ne $secret) 'DPAPI ciphertext must not equal plaintext'
Assert-Cmo ((Unprotect-CmoSecret -B64 $enc) -eq $secret) 'DPAPI secret round-trip failed'
$meta = [pscustomobject]@{
    sessionStartedAtMs = 123
    tokenType = 'Bearer'
    userInfo = [pscustomobject]@{ email = 'shape@example.com' }
}
$metaEnc = Protect-CmoAuthMetadata -Metadata $meta
$metaOut = Unprotect-CmoAuthMetadata -Encrypted $metaEnc
Assert-Cmo ($metaOut.userInfo.email -eq 'shape@example.com') 'auth metadata object round-trip failed'

# Regression: an earlier unresolved account must re-key to its real email later.
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('cmo-rotation-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
try {
    $script:TestCredPath = Join-Path $tmp 'account-credentials.json'
    function Get-CmoCredStorePath { return $script:TestCredPath }
    $old = New-Object System.Collections.Specialized.OrderedDictionary
    $old['unknown:acct-1'] = [pscustomobject]@{
        accountId = 'acct-1'
        accessToken = (Protect-CmoSecret 'old-access')
        refreshToken = (Protect-CmoSecret 'old-refresh')
        expiresAt = 2000000000000
        capturedAt = '2026-09-17T09:00:00Z'
    }
    Save-CmoCredStore -Store $old | Out-Null
    function Get-CmoProvidersAuth {
        return [pscustomobject]@{
            accountId = 'acct-1'
            accessToken = 'new-access'
            refreshToken = 'new-refresh'
            expiresAt = [long]2000000000000
            metadata = [pscustomobject]@{
                tokenType = 'Bearer'
                userInfo = [pscustomobject]@{ email = 'resolved@example.com' }
            }
        }
    }
    function Invoke-RestMethod {
        param($Uri, $Headers, $Method, $TimeoutSec)
        return [pscustomobject]@{
            data = [pscustomobject]@{ email = 'resolved@example.com' }
        }
    }
    $captured = Update-CmoCapturedCredentials
    $store = Get-CmoCredStore
    Assert-Cmo ($captured -eq 'resolved@example.com') 'unknown account should resolve to real email'
    Assert-Cmo ($store.Contains('resolved@example.com')) 'resolved email key missing'
    Assert-Cmo (-not $store.Contains('unknown:acct-1')) 'stale unknown account key must be removed'
    $restored = Get-CmoCredentialsFor -Email 'RESOLVED@example.com'
    Assert-Cmo ($restored.Token -eq 'new-access') 'case-insensitive credential lookup failed'
    Assert-Cmo ($restored.Metadata.userInfo.email -eq 'resolved@example.com') 'captured metadata was not restored'

    # Token renewal must use Cline's exact refresh contract and rotate both
    # encrypted token values without leaking either into the credential file.
    $script:RefreshRequest = $null
    function Invoke-RestMethod {
        param($Uri, $Method, $ContentType, $Body, $TimeoutSec)
        $script:RefreshRequest = [pscustomobject]@{
            Uri = $Uri; Method = $Method; ContentType = $ContentType
            Body = ($Body | ConvertFrom-Json)
        }
        return [pscustomobject]@{
            data = [pscustomobject]@{
                accessToken = 'rotated-access'
                refreshToken = 'rotated-refresh'
                expiresAt = '2033-05-18T03:33:20.000Z'
            }
        }
    }
    # Simulate credentials captured by an older CMO build that had no refreshedAt field.
    $legacyStore = Get-CmoCredStore
    $legacyStore['resolved@example.com'].PSObject.Properties.Remove('refreshedAt')
    Save-CmoCredStore -Store $legacyStore | Out-Null
    $renewed = Invoke-CmoRefreshCapturedCredential -Email 'resolved@example.com' -Force
    Assert-Cmo $renewed.Refreshed 'expired captured token should renew'
    Assert-Cmo ($script:RefreshRequest.Uri -eq 'https://api.cline.bot/api/v1/auth/refresh') 'refresh endpoint changed'
    Assert-Cmo ($script:RefreshRequest.Method -eq 'Post') 'refresh must be POST'
    Assert-Cmo ($script:RefreshRequest.ContentType -eq 'application/json') 'refresh must send JSON'
    Assert-Cmo ($script:RefreshRequest.Body.grantType -eq 'refresh_token') 'refresh grant type changed'
    Assert-Cmo ($script:RefreshRequest.Body.refreshToken -eq 'new-refresh') 'refresh must use saved refresh token'
    $renewedCred = Get-CmoCredentialsFor -Email 'resolved@example.com'
    Assert-Cmo ($renewedCred.Token -eq 'rotated-access') 'renewed access token was not stored'
    Assert-Cmo ($renewedCred.RefreshToken -eq 'rotated-refresh') 'rotated refresh token was not stored'
    Assert-Cmo ($renewedCred.ExpiresAtMs -eq 2000000000000) 'ISO expiry was not normalized to milliseconds'
    $postRenewStore = Get-CmoCredStore
    Assert-Cmo ([bool]$postRenewStore['resolved@example.com'].refreshedAt) 'legacy credential record must gain refreshedAt during renewal'
    $rawCred = Get-Content -LiteralPath $script:TestCredPath -Raw
    Assert-Cmo (-not $rawCred.Contains('rotated-access') -and -not $rawCred.Contains('rotated-refresh')) 'renewed tokens must remain DPAPI encrypted'

    # A running Cline owns the active refresh token. Excluding it must avoid any refresh request.
    $script:RefreshRequest = $null
    $skipped = @(Update-CmoCapturedCredentialTokens -RefreshBeforeMinutes 999999 -ExcludeEmail @($other))
    Assert-Cmo (@($skipped | Where-Object { $_.Reason -eq 'active-owned-by-cline' }).Count -ge 1) 'active Cline login should be excluded from external refresh'
    Assert-Cmo ($null -eq $script:RefreshRequest) 'excluded active login must not call the refresh endpoint'
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

# Writer/UI regressions are cheap to guard statically without touching live Cline state.
$switchSource = Get-Content -LiteralPath (Join-Path $src 'CmoSwitchAccount.ps1') -Raw
Assert-Cmo ($switchSource.Contains('yyyy-MM-ddTHH:mm:ss.fffZ')) 'provider updatedAt must stay ISO-8601'
Assert-Cmo (-not $switchSource.Contains("metadata.userInfo = ''")) 'userInfo must never be replaced by a string'
$statusSource = Get-Content -LiteralPath (Join-Path $src 'CmoStatus.ps1') -Raw
Assert-Cmo ($statusSource.Contains('$kind -eq ''both''')) 'Apply next must handle combined switch'
Assert-Cmo ($statusSource.Contains('$kind -eq ''needs-signin''')) 'Apply next must handle unlock-required plan'
Assert-Cmo ($statusSource.Contains('Start-CmoManualCredentialRenewal')) 'dashboard must expose manual credential renewal'
Assert-Cmo ($statusSource.Contains("New-TinyButton 'renew login'")) 'expired login must have a renew button'
$guardianSource = Get-Content -LiteralPath (Join-Path $src 'CmoGuardian.ps1') -Raw
Assert-Cmo ($guardianSource.Contains('-ExcludeEmail $excludeRenewal')) 'guardian must not rotate a running Cline login'

Write-Host ("PASS: {0} Cline rotation assertions" -f $script:passed)
