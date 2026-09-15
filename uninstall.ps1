# uninstall.ps1 - removes Cline Model Optimizer (task, shortcut, files).
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'ClineModelOptimizer'),
    [switch]$PurgeData
)
$ErrorActionPreference = 'SilentlyContinue'

Unregister-ScheduledTask -TaskName 'ClineModelOptimizer-Guardian' -Confirm:$false
Write-Host 'task removed: ClineModelOptimizer-Guardian'

$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Cline Model Optimizer.lnk'
if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk -Force; Write-Host 'shortcut removed' }

if (Test-Path -LiteralPath $InstallDir) { Remove-Item -LiteralPath $InstallDir -Recurse -Force; Write-Host ('removed: ' + $InstallDir) }

if ($PurgeData) {
    # NOTE: never touches ~\.cline\data (that belongs to the Cline extension)
    Write-Host 'backups and logs are inside the install dir and were removed with it'
}
Write-Host ''
Write-Host 'Uninstalled. Cline itself was not modified.' -ForegroundColor Green
