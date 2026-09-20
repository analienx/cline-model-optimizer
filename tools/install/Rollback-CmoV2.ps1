<#
.SYNOPSIS
  Gate-F deterministic rollback for CMO v2.

.DESCRIPTION
  Reverses an Install-CmoV2 run: restores the files preserved in BackupDir,
  removes exactly the staged v2-only files listed in the install receipt, and
  emits a sorted-keys rollback receipt (schema cmo.rollback-receipt/v1).

  State under <InstallRoot>\state (the v2 DB) is never touched: user evidence
  is retained, not deleted. Backup copies are kept (copied back, not moved)
  for audit. Idempotent: re-running removes nothing new and restores again.

  Drill-safe: when InstallRoot is not exactly the real
  %LOCALAPPDATA%\ClineModelOptimizer root, no scheduled task, service, or
  shortcut outside InstallRoot/BackupDir is stopped, removed, or restored.

.PARAMETER InstallRoot
  The install target the drill/live install wrote to.

.PARAMETER BackupDir
  The backup dir recorded in the install receipt.

.PARAMETER ReceiptPath
  Path to the install receipt (cmo.install-receipt/v1).
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$InstallRoot,
  [Parameter(Mandatory = $true)][string]$BackupDir,
  [Parameter(Mandatory = $true)][string]$ReceiptPath
)
$ErrorActionPreference = 'Stop'

function Resolve-FullPath([string]$Path) {
  # NOTE: [IO.Path]::GetFullPath uses the process CWD, which can differ from
  # the PowerShell location in hosted runs; anchor relative paths explicitly.
  if (-not [System.IO.Path]::IsPathRooted($Path)) { $Path = Join-Path (Get-Location) $Path }
  return [System.IO.Path]::GetFullPath($Path)
}

function Get-NormalizedPath([string]$Path) {
  return (Resolve-FullPath $Path).TrimEnd([System.IO.Path]::DirectorySeparatorChar).ToLowerInvariant()
}

function Test-IsUnder([string]$Child, [string]$Parent) {
  $c = Get-NormalizedPath $Child
  $p = Get-NormalizedPath $Parent
  return ($c -eq $p) -or $c.StartsWith($p + [System.IO.Path]::DirectorySeparatorChar)
}

function ConvertTo-Sorted([object]$Value) {
  if ($null -eq $Value) { return $null }
  # Cmdlet outputs (e.g. Join-Path) arrive PSObject-wrapped, which would
  # otherwise match the PSCustomObject branch and serialize as {Length: N}.
  $Value = $Value.PSObject.BaseObject
  if ($Value -is [string]) { return $Value }
  if ($Value -is [System.Collections.IDictionary]) {
    $ordered = [ordered]@{}
    foreach ($key in ($Value.Keys | Sort-Object)) { $ordered[$key] = ConvertTo-Sorted $Value[$key] }
    return $ordered
  }
  if ($Value -is [pscustomobject]) {
    $ordered = [ordered]@{}
    foreach ($prop in ($Value.PSObject.Properties.Name | Sort-Object)) { $ordered[$prop] = ConvertTo-Sorted $Value.$prop }
    return $ordered
  }
  if ($Value -is [System.Collections.IEnumerable] -and -not ($Value -is [string])) {
    $items = @()
    foreach ($item in $Value) { $items += ConvertTo-Sorted $item }
    return $items
  }
  return $Value
}

function Write-DeterministicJson([string]$Path, [object]$Value) {
  $sorted = ConvertTo-Sorted $Value
  $text = ($sorted | ConvertTo-Json -Depth 20 -Compress:$false) + "`n"
  $dir = [System.IO.Path]::GetDirectoryName($Path)
  if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
  [System.IO.File]::WriteAllText($Path, $text, [System.Text.UTF8Encoding]::new($false))
}

$InstallFull = Resolve-FullPath $InstallRoot
$BackupFull = Resolve-FullPath $BackupDir
$ReceiptFull = Resolve-FullPath $ReceiptPath
$realFull = Resolve-FullPath (Join-Path $env:LOCALAPPDATA 'ClineModelOptimizer')
$mode = 'drill'
if ((Get-NormalizedPath $InstallFull) -eq (Get-NormalizedPath $realFull)) { $mode = 'live' }

if (-not (Test-Path -LiteralPath $ReceiptFull)) { throw "receipt not found: $ReceiptFull" }
$receipt = Get-Content -LiteralPath $ReceiptFull -Raw -Encoding utf8 | ConvertFrom-Json
if ($receipt.schema -ne 'cmo.install-receipt/v1') { throw "unexpected receipt schema: $($receipt.schema)" }
if ((Get-NormalizedPath $receipt.installRoot) -ne (Get-NormalizedPath $InstallFull)) {
  throw "receipt installRoot $($receipt.installRoot) does not match -InstallRoot $InstallFull; refusing"
}
if ((Get-NormalizedPath $receipt.backupDir) -ne (Get-NormalizedPath $BackupFull)) {
  throw "receipt backupDir $($receipt.backupDir) does not match -BackupDir $BackupFull; refusing"
}
if (-not (Test-Path -LiteralPath $BackupFull)) { throw "backup dir missing: $BackupFull" }

# ---------------------------------------------------------------------------
# host teardown: LIVE ONLY (never in drill)
# ---------------------------------------------------------------------------

if ($mode -eq 'live') {
  try { Unregister-ScheduledTask -TaskName 'ClineModelOptimizer-Service' -Confirm:$false -ErrorAction Stop } catch { }
  foreach ($guardianPattern in @('ClineModelOptimizer-Guardian', 'CmoGuardian*')) {
    try { Enable-ScheduledTask -TaskName $guardianPattern -ErrorAction Stop | Out-Null } catch { }
  }
  # Dashboard shortcut: restore the pre-existing file when the install
  # overwrote one, otherwise remove exactly the file the install created.
  $shortcutAction = 'none'
  $shortcutPath = $receipt.dashboardShortcut
  if ($shortcutPath) {
    $backupRel = $receipt.dashboardShortcutBackup
    if ($receipt.dashboardShortcutPreexisting -and $backupRel) {
      $backupFile = Join-Path $BackupFull $backupRel
      if (Test-Path -LiteralPath $backupFile) {
        Copy-Item -LiteralPath $backupFile -Destination $shortcutPath -Force
        $shortcutAction = 'restored-preexisting'
      }
    } elseif ($receipt.dashboardShortcutWritten) {
      if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath -Force
        $shortcutAction = 'removed-created'
      } else {
        $shortcutAction = 'already-absent'
      }
    }
  }
  Write-Host 'live: v2 task unregistered; legacy guardian re-enabled'
} else {
  $shortcutAction = 'drill-skipped'
  Write-Host 'drill mode: no host tasks/services touched'
}

# ---------------------------------------------------------------------------
# remove staged v2-only files (exactly those listed in the receipt), then
# restore the preserved originals. State (incl. the v2 DB) is never deleted.
# ---------------------------------------------------------------------------

$removed = @()
foreach ($rel in $receipt.files) {
  $target = Join-Path $InstallFull $rel
  if (-not (Test-IsUnder $target $InstallFull)) { throw "drill-safety: staged path escapes InstallRoot: $rel" }
  if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Force
    $removed += $rel
  }
}

$backupFilesDir = Join-Path $BackupFull 'files'
$restored = @()
if (Test-Path -LiteralPath $backupFilesDir) {
  foreach ($item in (Get-ChildItem -LiteralPath $backupFilesDir -Recurse -File)) {
    $rel = [System.IO.Path]::GetRelativePath($backupFilesDir, $item.FullName)
    $dest = Join-Path $InstallFull $rel
    if (-not (Test-IsUnder $dest $InstallFull)) { throw "drill-safety: backup path escapes InstallRoot: $rel" }
    $destDir = [System.IO.Path]::GetDirectoryName($dest)
    if (-not (Test-Path -LiteralPath $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
    Copy-Item -LiteralPath $item.FullName -Destination $dest -Force
    $restored += $rel
  }
}

# Drop the now-empty v2 stage dir if the install created it (audit logs stay).
$v2Dir = Join-Path $InstallFull 'v2'
if ((Test-Path -LiteralPath $v2Dir) -and -not (Get-ChildItem -LiteralPath $v2Dir -Force)) {
  Remove-Item -LiteralPath $v2Dir -Force
}
Write-Host ("rollback: {0} staged file(s) removed, {1} backed-up file(s) restored" -f $removed.Count, $restored.Count)

$rollback = @{
  backupDir            = $BackupFull
  dashboardShortcut    = @{ action = $shortcutAction; path = $receipt.dashboardShortcut }
  installRoot          = $InstallFull
  mode                 = $mode
  removed              = @($removed | Sort-Object)
  restored             = @($restored | Sort-Object)
  schema               = 'cmo.rollback-receipt/v1'
  sourceArtifactDigest = $receipt.artifactDigest
  sourcePlanHash       = $receipt.planHash
}
$rollbackPath = Join-Path $BackupFull 'rollback-receipt.json'
Write-DeterministicJson $rollbackPath $rollback
Write-Host "rollback receipt $rollbackPath"
$rollback | ConvertTo-Json -Depth 10
