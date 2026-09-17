# CmoSwitchAccount.ps1 - rotate Cline to a captured OAuth login.
#
# Account switching writes a captured auth object back into providers.json.
# It is refused while VS Code is running unless -AllowRunning is explicitly
# supplied. providers.json and globalState.json are backed up first.
#
# Usage:
#   powershell -File CmoSwitchAccount.ps1 -Email user@example.com
# Exit codes: 0=switched 1=vscode-running 2=no-credentials 3=write-failed
param(
    [Parameter(Mandatory = $true)][string]$Email,
    [switch]$AllowRunning
)
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'CmoCore.ps1')

$vsOpen = @(Get-Process -Name 'Code' -ErrorAction SilentlyContinue).Count -gt 0
if ($vsOpen -and -not $AllowRunning) {
    Write-Host 'VS Code is running - close it first (the extension would overwrite the switch).'
    exit 1
}

$cred = Get-CmoCredentialsFor -Email $Email
if (-not $cred -or -not $cred.Token) {
    Write-Host ('no captured credentials for ' + $Email + ' - sign into this account in Cline once so it can be captured')
    exit 2
}

$dataDir = Get-CmoDataDir
$gsPath = Join-Path $dataDir 'globalState.json'
$ppPath = Join-Path $dataDir 'settings\providers.json'
if (-not (Test-Path -LiteralPath $ppPath)) {
    Write-Host 'providers.json not found'
    exit 3
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$bk = Join-Path $env:LOCALAPPDATA ('ClineModelOptimizer\backup-' + $stamp)
New-Item -ItemType Directory -Path $bk -Force | Out-Null
Copy-Item -LiteralPath $ppPath -Destination $bk -Force
if (Test-Path -LiteralPath $gsPath) {
    Copy-Item -LiteralPath $gsPath -Destination $bk -Force
}
Write-Host ('backup: ' + $bk)

try {
    $pp = Get-Content -LiteralPath $ppPath -Raw | ConvertFrom-Json
    $provider = $pp.providers.cline
    $s = $provider.settings
    if (-not $s -or -not $s.auth) { throw 'Cline auth object not found in providers.json' }

    $s.auth.accessToken = [string]$cred.Token
    $s.auth.refreshToken = [string]$cred.RefreshToken
    $s.auth.accountId = [string]$cred.AccountId
    $s.auth.expiresAt = [long]$cred.ExpiresAtMs

    # Cline 4.1.x stores metadata as an object. Restore the captured account's
    # metadata rather than leaving the prior account's userInfo behind. Start a
    # fresh local session clock for this restored login.
    $sessionStartedAtMs = [long]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
    $metadata = $cred.Metadata
    if (-not $metadata) { $metadata = [pscustomobject]@{} }
    if ($metadata.PSObject.Properties['sessionStartedAtMs']) {
        $metadata.sessionStartedAtMs = $sessionStartedAtMs
    } else {
        $metadata | Add-Member -NotePropertyName sessionStartedAtMs -NotePropertyValue $sessionStartedAtMs
    }
    $s.auth.metadata = $metadata

    $provider.settings = $s
    $provider.updatedAt = [DateTimeOffset]::UtcNow.ToString(
        'yyyy-MM-ddTHH:mm:ss.fffZ', [Globalization.CultureInfo]::InvariantCulture)
    if ($provider.PSObject.Properties['tokenSource']) {
        $provider.tokenSource = 'oauth'
    } else {
        $provider | Add-Member -NotePropertyName tokenSource -NotePropertyValue 'oauth'
    }
    $pp.providers.cline = $provider

    $pp | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $ppPath -Force -Encoding UTF8
    Write-Host ('switched Cline login to ' + $Email + ' (model kept as-is)')
    exit 0
} catch {
    Write-Host ('switch failed: ' + $_.Exception.Message)
    exit 3
}
