<#
.SYNOPSIS
  Gate-F deterministic installer for CMO v2.

.DESCRIPTION
  Verifies the artifact digest BEFORE any mutation, writes a deterministic
  (sorted-keys) JSON plan plus its sha256 into the backup dir first, backs up
  any existing files that would be overwritten plus read-only
  scheduled-task/shortcut metadata, stages v2/cmo.pyz and the compat exports
  (via `python <artifact> exports write --out-dir <InstallRoot>` against a
  TEMP scratch DB), verifies staged manifest digests against on-disk bytes,
  and emits a sorted-keys JSON receipt (schema cmo.install-receipt/v1).

  Drill mode: any InstallRoot that is not exactly the real
  %LOCALAPPDATA%\ClineModelOptimizer root is a drill. Drill mode performs NO
  host mutation outside InstallRoot/BackupDir/ScratchDb: it never registers,
  stops, disables, or touches scheduled tasks, services, shortcuts, or the
  real tree. Task/shortcut metadata is exported read-only in both modes.

.PARAMETER ArtifactPath
  Path to the cmo.pyz artifact. Defaults to dist/cmo.pyz in this worktree.

.PARAMETER ExpectedDigest
  Required lowercase sha256 the artifact must match before any mutation.

.PARAMETER InstallRoot
  Install target. Defaults to $env:LOCALAPPDATA\ClineModelOptimizer.
  Any other value is drill mode (override for drills).

.PARAMETER ServicePort
  Loopback service port recorded in the receipt. Default 4311.

.PARAMETER ScratchDb
  TEMP scratch SQLite DB used ONLY to generate the compat exports. The
  install never writes state into InstallRoot. Defaults to a unique TEMP file
  and is retained (never deleted) for audit.

.PARAMETER BackupDir
  Backup/audit dir. Defaults to <InstallRoot>\install-logs\backup-<utc stamp>.
#>
[CmdletBinding()]
param(
  [string]$ArtifactPath = (Join-Path $PSScriptRoot '..' '..' 'dist' 'cmo.pyz'),
  [Parameter(Mandatory = $true)][string]$ExpectedDigest,
  [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'ClineModelOptimizer'),
  [int]$ServicePort = 4311,
  [string]$ScratchDb = (Join-Path ([System.IO.Path]::GetTempPath()) ('cmo-scratch-' + [System.Guid]::NewGuid().ToString('N') + '.sqlite3')),
  [string]$BackupDir = ''
)
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

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

function Get-FileSha256([string]$Path) {
  return (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLowerInvariant()
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

# The 7 compat exports plus the staged artifact, in sorted order.
$STAGED_FILES = @(
  'cmo-compat-manifest.json',
  'foundry-route-policy.json',
  'free-models.json',
  'guardian-state.json',
  'guardian-status.json',
  'model-routing.json',
  'quota-state.json',
  'v2/cmo.pyz'
)

# ---------------------------------------------------------------------------
# resolve + verify BEFORE any mutation
# ---------------------------------------------------------------------------

$ArtifactFull = Resolve-FullPath $ArtifactPath
if (-not (Test-Path -LiteralPath $ArtifactFull)) { throw "artifact not found: $ArtifactFull" }
$artifactDigest = Get-FileSha256 $ArtifactFull
if ($artifactDigest -ne $ExpectedDigest.ToLowerInvariant()) {
  throw "artifact digest mismatch: expected $($ExpectedDigest.ToLowerInvariant()) got $artifactDigest (no changes made)"
}

$InstallFull = Resolve-FullPath $InstallRoot
$realRoot = Join-Path $env:LOCALAPPDATA 'ClineModelOptimizer'
$realFull = Resolve-FullPath $realRoot
$mode = 'drill'
if ((Get-NormalizedPath $InstallFull) -eq (Get-NormalizedPath $realFull)) { $mode = 'live' }

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
if ([string]::IsNullOrWhiteSpace($BackupDir)) {
  $BackupFull = Join-Path $InstallFull ("install-logs/backup-" + $stamp)
} else {
  $BackupFull = Resolve-FullPath $BackupDir
}
$ScratchFull = Resolve-FullPath $ScratchDb

# Drill guard: backup/scratch targets must never resolve into the real tree.
if ($mode -eq 'drill') {
  foreach ($target in @($BackupFull, $ScratchFull)) {
    if ((Test-IsUnder $target $realFull) -and -not (Test-IsUnder $target $InstallFull)) {
      throw "drill-safety: target $target resolves into the real tree; refusing"
    }
  }
}

# First mutation happens only after the digest check above: create backup dir.
New-Item -ItemType Directory -Path $BackupFull -Force | Out-Null

# Deterministic dashboard shortcut target (created only in live mode).
$dashboardShortcutPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Cline Model Optimizer Dashboard.url'
$dashboardUrl = "http://127.0.0.1:$ServicePort/"

# ---------------------------------------------------------------------------
# deterministic plan (sorted keys) + plan hash, written first
# ---------------------------------------------------------------------------

$actions = @('verify-artifact', 'write-plan', 'backup-existing', 'stage-artifact', 'exports-write', 'verify-manifest')
if ($mode -eq 'live') { $actions += 'live-host-tasks', 'live-dashboard-shortcut' } else { $actions += 'skip-host-mutation' }

$plan = @{
  actions           = $actions
  artifactDigest    = $artifactDigest
  artifactPath      = $ArtifactFull
  backupDir         = $BackupFull
  dashboardShortcut = $dashboardShortcutPath
  dashboardUrl      = $dashboardUrl
  expectedDigest = $ExpectedDigest.ToLowerInvariant()
  files          = $STAGED_FILES
  generatedAt    = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
  installRoot    = $InstallFull
  mode           = $mode
  scratchDb      = $ScratchFull
  servicePort    = $ServicePort
}
$planPath = Join-Path $BackupFull 'install-plan.json'
Write-DeterministicJson $planPath $plan
$planHash = Get-FileSha256 $planPath
$planHash | Out-File -LiteralPath (Join-Path $BackupFull 'install-plan.sha256') -Encoding ascii -NoNewline
Write-Host "plan $planPath planHash=$planHash mode=$mode"

# ---------------------------------------------------------------------------
# backup existing files + read-only task/shortcut metadata (export only)
# ---------------------------------------------------------------------------

$backupFilesDir = Join-Path $BackupFull 'files'
$backedUp = @()
$created = @()
foreach ($rel in $STAGED_FILES) {
  $target = Join-Path $InstallFull $rel
  if (Test-Path -LiteralPath $target) {
    $dest = Join-Path $backupFilesDir $rel
    $destDir = [System.IO.Path]::GetDirectoryName($dest)
    if (-not (Test-Path -LiteralPath $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
    Copy-Item -LiteralPath $target -Destination $dest -Force
    $backedUp += $rel
  } else {
    $created += $rel
  }
}
Write-DeterministicJson (Join-Path $BackupFull 'backup-index.json') @{
  backedUp = @($backedUp | Sort-Object)
  created  = @($created | Sort-Object)
  schema   = 'cmo.backup-index/v1'
}

# Metadata export is read-only in BOTH modes: never mutates host state here.
$taskMeta = @()
foreach ($pattern in @('ClineModelOptimizer*', 'CmoGuardian*', '*Cmo*')) {
  try {
    foreach ($task in (Get-ScheduledTask -TaskName $pattern -ErrorAction Stop)) {
      $taskMeta += @{ name = $task.TaskName; path = $task.TaskPath; state = $task.State.ToString() }
    }
  } catch { }
}
Write-DeterministicJson (Join-Path $BackupFull 'scheduled-tasks.json') @{ tasks = @($taskMeta | Sort-Object { $_.name }) }

$shortcutMeta = @()
foreach ($candidate in @(
    $dashboardShortcutPath,
    (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Cline Model Optimizer.lnk'),
    (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\Cline Model Optimizer.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Cline Model Optimizer.lnk'))) {
  $shortcutMeta += @{ path = $candidate; exists = (Test-Path -LiteralPath $candidate) }
}
Write-DeterministicJson (Join-Path $BackupFull 'shortcuts.json') @{ shortcuts = $shortcutMeta }

# Preserve a pre-existing dashboard shortcut (read-only to the host; the copy
# lands inside BackupDir). Done in both modes so rollback can restore it.
$shortcutPreexisting = Test-Path -LiteralPath $dashboardShortcutPath
$shortcutBackupRel = $null
if ($shortcutPreexisting) {
  $shortcutBackupRel = 'shortcut-backup/Cline Model Optimizer Dashboard.url'
  New-Item -ItemType Directory -Path (Join-Path $BackupFull 'shortcut-backup') -Force | Out-Null
  Copy-Item -LiteralPath $dashboardShortcutPath `
    -Destination (Join-Path $BackupFull $shortcutBackupRel) -Force
}
Write-Host ("backup: {0} existing file(s) preserved, {1} new staged file(s)" -f $backedUp.Count, $created.Count)

# ---------------------------------------------------------------------------
# stage artifact + compat exports (scratch DB only; no state in InstallRoot)
# ---------------------------------------------------------------------------

try {
  $v2Dir = Join-Path $InstallFull 'v2'
  if (-not (Test-Path -LiteralPath $v2Dir)) { New-Item -ItemType Directory -Path $v2Dir -Force | Out-Null }
  Copy-Item -LiteralPath $ArtifactFull -Destination (Join-Path $v2Dir 'cmo.pyz') -Force

  $exportsOut = & python $ArtifactFull exports write --db $ScratchFull --out-dir $InstallFull 2>&1
  $exportsOut | ForEach-Object { Write-Host $_ }
  if ($LASTEXITCODE -ne 0) { throw "exports write failed with exit $LASTEXITCODE" }

  # Verify staged manifest digests match on-disk bytes.
  $manifestPath = Join-Path $InstallFull 'cmo-compat-manifest.json'
  $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
  if ($manifest.schema -ne 'cmo.compat-manifest/v1') { throw "unexpected manifest schema: $($manifest.schema)" }
  foreach ($prop in $manifest.files.PSObject.Properties) {
    $diskPath = Join-Path $InstallFull $prop.Name
    if (-not (Test-Path -LiteralPath $diskPath)) { throw "manifest lists missing file: $($prop.Name)" }
    $actual = Get-FileSha256 $diskPath
    if ($actual -ne $prop.Value.ToLowerInvariant()) { throw "digest mismatch for $($prop.Name): manifest $($prop.Value) disk $actual" }
  }
  Write-Host "manifest verified: $(@($manifest.files.PSObject.Properties).Count) file digest(s) match"
} catch {
  # Best-effort restore of pre-existing files before failing.
  foreach ($rel in $backedUp) {
    Copy-Item -LiteralPath (Join-Path $backupFilesDir $rel) -Destination (Join-Path $InstallFull $rel) -Force
  }
  throw
}

# ---------------------------------------------------------------------------
# host tasks: LIVE ONLY (never in drill)
# ---------------------------------------------------------------------------

if ($mode -eq 'live') {
  $serveTarget = Join-Path $v2Dir 'cmo.pyz'
  $action = New-ScheduledTaskAction -Execute 'python' -Argument "`"$serveTarget`" serve --port $ServicePort --artifact-digest $artifactDigest"
  $trigger = New-ScheduledTaskTrigger -AtLogOn
  Register-ScheduledTask -TaskName 'ClineModelOptimizer-Service' -Action $action -Trigger $trigger -Force | Out-Null
  # Retire the legacy guardian writer. The observed legacy task name is
  # ClineModelOptimizer-Guardian; CmoGuardian* covers the v2-era variant.
  foreach ($guardianPattern in @('ClineModelOptimizer-Guardian', 'CmoGuardian*')) {
    try { Disable-ScheduledTask -TaskName $guardianPattern -ErrorAction Stop | Out-Null } catch { }
  }
  # Start Menu dashboard shortcut: a plain .url internet shortcut so the bytes
  # are deterministic and human-auditable.
  $shortcutText = "[InternetShortcut]`r`nURL=$dashboardUrl`r`n"
  [System.IO.File]::WriteAllText($dashboardShortcutPath, $shortcutText,
    [System.Text.ASCIIEncoding]::new())
  $shortcutWritten = $true
  Write-Host 'live host tasks registered; legacy guardian writer retired; dashboard shortcut written'
} else {
  $shortcutWritten = $false
  Write-Host 'drill mode: no tasks/services/shortcuts registered; real tree untouched'
}

# ---------------------------------------------------------------------------
# receipt (sorted keys)
# ---------------------------------------------------------------------------

$receipt = @{
  artifactDigest               = $artifactDigest
  backupDir                    = $BackupFull
  dashboardShortcut            = $dashboardShortcutPath
  dashboardShortcutBackup      = $shortcutBackupRel
  dashboardShortcutPreexisting = $shortcutPreexisting
  dashboardShortcutWritten     = $shortcutWritten
  files          = $STAGED_FILES
  installRoot    = $InstallFull
  mode           = $mode
  planHash       = $planHash
  schema         = 'cmo.install-receipt/v1'
  servicePort    = $ServicePort
}
$receiptPath = Join-Path $BackupFull 'install-receipt.json'
Write-DeterministicJson $receiptPath $receipt
Write-Host "receipt $receiptPath"
$receipt | ConvertTo-Json -Depth 10
