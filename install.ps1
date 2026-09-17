# install.ps1 - per-user installer for Cline Model Optimizer (no elevation).
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'ClineModelOptimizer'),
    [switch]$NoShortcut
)
$ErrorActionPreference = 'Stop'
$srcDir = Join-Path $PSScriptRoot 'src'

function Step([string]$m) { Write-Host ("==> " + $m) -ForegroundColor Cyan }
function Ok([string]$m)   { Write-Host ("    " + $m) -ForegroundColor Green }

Step 'Checking Cline extension data (~\.cline\data)'
$dataDir = Join-Path $env:USERPROFILE '.cline\data'
if (-not (Test-Path -LiteralPath $dataDir)) {
    Write-Host '    WARNING: Cline data not found yet - install/open Cline in VS Code first.' -ForegroundColor Yellow
    Write-Host '    The optimizer will report NO-CLINE-STATE until Cline has run once.' -ForegroundColor Yellow
} else { Ok ('Cline data found: ' + $dataDir) }

Step ('Copying tooling to ' + $InstallDir)
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
# ship only real sources: the dev UI-audit harness drops throwaway copies named
# _*-status.ps1 into src/ and those must never reach an install.
foreach ($f in @(Get-ChildItem -Path (Join-Path $srcDir '*') -File | Where-Object { $_.Name -notlike '_*' })) {
    Copy-Item -LiteralPath $f.FullName -Destination (Join-Path $InstallDir $f.Name) -Force
}
# a previous install may still hold a stale harness copy - remove it
foreach ($junk in @(Get-ChildItem -Path (Join-Path $InstallDir '_*-status.ps1') -File -ErrorAction SilentlyContinue)) {
    Remove-Item -LiteralPath $junk.FullName -Force
}
Ok ('installed: ' + ((Get-ChildItem $InstallDir -File | Select-Object -ExpandProperty Name) -join ', '))

Step 'Registering scheduled task (logon + every 5 min)'
$hiddenRunner = Join-Path $InstallDir 'RunHidden.vbs'
$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
$action = New-ScheduledTaskAction -Execute $wscript `
    -Argument ('//B //NoLogo "' + $hiddenRunner + '" "' + (Join-Path $InstallDir 'CmoGuardian.ps1') + '"')
$lt = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$rt = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'ClineModelOptimizer-Guardian' -Action $action `
    -Trigger @($lt, $rt) -Settings $settings -Principal $principal `
    -Description 'Cline Model Optimizer: tracks active Cline model tier (free/subscription/paid), advises when paid models are used while free ones are available, watches auth token expiry.' `
    -Force | Out-Null
Ok 'task registered: ClineModelOptimizer-Guardian'

if (-not $NoShortcut) {
    Step 'Creating desktop shortcut'
    $wshell = New-Object -ComObject WScript.Shell
    $lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Cline Model Optimizer.lnk'
    $sc = $wshell.CreateShortcut($lnk)
    $sc.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $sc.Arguments = ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $InstallDir 'CmoStatus.ps1') + '"')
    $icon = Join-Path $InstallDir 'cmo-icon.ico'
    if (Test-Path -LiteralPath $icon) { $sc.IconLocation = ($icon + ',0') }
    $sc.Description = 'Cline Model Optimizer dashboard'
    $sc.Save()
    Ok ('shortcut created: ' + $lnk)
}

Step 'Running guardian once (initial check)'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallDir 'CmoGuardian.ps1')
Ok ('guardian exit code: ' + $LASTEXITCODE + '  (0=optimal-free 1=free-suboptimal 2=paid-in-use 3=auth-warning 4=no-state)')

Write-Host ''
Write-Host 'Cline Model Optimizer installed.' -ForegroundColor Green
Write-Host 'Dashboard: "Cline Model Optimizer" on your desktop' -ForegroundColor White
Write-Host 'Strategy config: ' (Join-Path $InstallDir 'model-routing.json') -ForegroundColor White
