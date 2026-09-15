# CmoApply.ps1 - applies the resolved (dynamic) strategy to Cline's active model config.
#
# The target model is resolved from the DYNAMIC ladder (live free list from
# Cline's API, preferred entries first, up to freeSelection.maxFreeModels),
# not from the static fallback ladder.
#
# SAFETY RULES:
#   - Refuses to run while VS Code is open (the extension would overwrite our
#     changes from memory). Close VS Code first, or pass -AllowRunning at your
#     own risk (change may be reverted by the extension).
#   - Backs up globalState.json and providers.json before touching anything.
#   - Never reads or writes secrets (secrets.json) - only model/provider fields.
#
# Usage:
#   powershell -File CmoApply.ps1                          # apply ladder step 1 (top free), both modes
#   powershell -File CmoApply.ps1 -Mode act                # act mode only
#   powershell -File CmoApply.ps1 -List                    # just print the resolved ladder, change nothing
#   powershell -File CmoApply.ps1 -MaxFreeModels 3         # keep 3 free models on the ladder, then apply step 1
#   powershell -File CmoApply.ps1 -Rank 3                  # apply ladder step 3 (e.g. first subscription model)
#   powershell -File CmoApply.ps1 -RefreshFreeList         # force a live re-fetch of the free list first
#   powershell -File CmoApply.ps1 -Model z-ai/glm-5.3-flash -Provider cline   # explicit override
# Exit codes: 0=applied 1=vscode-running 2=state-missing 3=write-failed

param(
    [string]$Mode = 'both',          # act | plan | both
    [string]$Model,                  # optional explicit model override
    [string]$Provider,               # optional explicit provider override
    [string]$Thinking,               # optional reasoning effort override
    [int]$Rank = 1,                  # ladder step to apply (1 = top preferred free model)
    [int]$MaxFreeModels = 0,         # 1|2|3... persist freeSelection.maxFreeModels to the config first
    [switch]$RefreshFreeList,        # force a live re-fetch of the free list before resolving
    [switch]$List,                   # print the resolved ladder and exit (no changes)
    [switch]$AllowRunning
)
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'CmoCore.ps1')

$dataDir = Get-CmoDataDir
$gsPath  = Join-Path $dataDir 'globalState.json'
$ppPath  = Join-Path $dataDir 'settings\providers.json'

if (-not (Test-Path -LiteralPath $gsPath)) {
    Write-Error 'Cline globalState.json not found'
    exit 2
}

$routing = Get-CmoRoutingConfig

# ---- optionally persist maxFreeModels (number of free models on the ladder) ----
if ($MaxFreeModels -ge 1) {
    $cfgPath = Join-Path $PSScriptRoot 'model-routing.json'
    try {
        $cfg = Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json
        $cfg.freeSelection.maxFreeModels = [int]$MaxFreeModels
        $cfg | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $cfgPath -Force -Encoding UTF8
        $routing = $cfg
        Write-Host ("config: freeSelection.maxFreeModels = " + $MaxFreeModels + "  (" + $cfgPath + ")") -ForegroundColor DarkCyan
    } catch {
        Write-Warning ("could not persist maxFreeModels: " + $_.Exception.Message)
    }
}

# ---- resolve the ladder dynamically (live free list) ----
$eff = Get-CmoEffectiveStrategy -Routing $routing -Mode 'act' -RefreshFreeList:$RefreshFreeList
$ladder = @($eff.Steps)
$dyn = $eff.Dynamic
if ($Rank -lt 1 -or $Rank -gt $ladder.Count) { $Rank = 1 }

Write-Host ("free list: {0} models - source={1}{2}{3}" -f @($dyn.Models).Count, $dyn.Source, `
    $(if ($null -ne $dyn.FetchedAt) { " fetched=" + $dyn.FetchedAt } else { '' }), `
    $(if ($dyn.Fresh) { ' [fresh]' } else { ' [not fresh]' }))

if ($List) {
    Write-Host ''
    Write-Host ("resolved ladder (maxFreeModels=" + $eff.MaxFreeModels + ", mode=act):")
    $i = 0
    foreach ($s in $ladder) {
        $i++
        $note = ''
        if ($s.tier -eq 'SUBSCRIPTION' -and -not $s.live) { $note = '  [not in current pass list]' }
        Write-Host ("  #{0} {1}  [{2}] provider={3} thinking={4} source={5}{6}" -f $i, $s.model, $s.tier, $s.provider, $s.thinking, $s.source, $note)
    }
    exit 0
}

$target = $ladder[$Rank - 1]
if ($Model) {
    $target = [pscustomobject]@{ provider = $Provider; model = $Model
                                 thinking = $(if ($Thinking) { $Thinking } else { 'off' })
                                 tier = 'unknown'; source = 'explicit'; live = $true }
}
if (-not $target -or -not $target.model) { Write-Error 'resolved ladder has no steps'; exit 2 }

# ---- VS Code running check ----
$codeProcs = @(Get-Process -Name Code -ErrorAction SilentlyContinue)
if ($codeProcs.Count -gt 0 -and -not $AllowRunning) {
    Write-Host 'VS Code is running - Cline would overwrite this change from memory.' -ForegroundColor Yellow
    Write-Host 'Close VS Code and re-run, or use -AllowRunning (change may be reverted).' -ForegroundColor Yellow
    exit 1
}

# ---- backup ----
$backupDir = Join-Path $env:LOCALAPPDATA 'ClineModelOptimizer\backups'
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Copy-Item -LiteralPath $gsPath -Destination (Join-Path $backupDir ("globalState-" + $stamp + ".json")) -Force
if (Test-Path -LiteralPath $ppPath) {
    Copy-Item -LiteralPath $ppPath -Destination (Join-Path $backupDir ("providers-" + $stamp + ".json")) -Force
}
Write-Host ("backup: " + $backupDir + " (*-" + $stamp + ".json)")

# ---- mutate globalState.json (dynamic ladder target) ----
$gs = Get-Content -LiteralPath $gsPath -Raw | ConvertFrom-Json
$modePrefixes = if ($Mode -eq 'plan') { @('planMode') } elseif ($Mode -eq 'act') { @('actMode') } else { @('planMode','actMode') }
$isPass = ($target.model -like 'cline-pass/*' -or $target.tier -eq 'SUBSCRIPTION')

foreach ($mp in $modePrefixes) {
    if ($isPass) {
        # Cline 4.x stores pass selections as provider 'cline-pass' + ClinePassModelId
        $gs.($mp + 'ApiProvider') = 'cline-pass'
        $gs.($mp + 'ClinePassModelId') = $target.model
        $gs.($mp + 'ClineModelId') = $null
    } else {
        # free models ride the cline provider
        $gs.($mp + 'ApiProvider') = if ($target.provider) { $target.provider } else { 'cline' }
        $gs.($mp + 'ClineModelId') = $target.model
        $gs.($mp + 'ClinePassModelId') = $null
    }
    if ($target.thinking) { $gs.($mp + 'ReasoningEffort') = $target.thinking }
}

# model info blocks: pull from Cline's own cache when available
$cache = Join-Path $dataDir 'cache\cline_recommended_models.json'
$allCached = @()
if (Test-Path -LiteralPath $cache) {
    try {
        $j = Get-Content -LiteralPath $cache -Raw | ConvertFrom-Json
        $allCached = @($j.recommended) + @($j.free) + @($j.clinePass) | Where-Object { $_.id }
    } catch { }
}
$hit = $allCached | Where-Object { $_.id -eq $target.model } | Select-Object -First 1
if ($hit) {
    foreach ($mp in $modePrefixes) {
        $infoKey = if ($isPass) { $mp + 'ClinePassModelInfo' } else { $mp + 'ClineModelInfo' }
        $gs.($infoKey) = [pscustomobject]@{
            maxTokens = if ($hit.maxTokens) { $hit.maxTokens } else { -1 }
            contextWindow = if ($hit.contextWindow) { $hit.contextWindow } else { 128000 }
            supportsImages = [bool]$hit.supportsImages
            supportsPromptCache = [bool]$hit.supportsPromptCache
            inputPrice = 0; outputPrice = 0; description = $hit.description
        }
    }
}

try {
    $gs | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $gsPath -Force -Encoding UTF8
} catch {
    Write-Error ("failed to write globalState.json: " + $_.Exception.Message)
    exit 3
}

# ---- mutate providers.json (model field for the matching provider) ----
if (Test-Path -LiteralPath $ppPath) {
    try {
        $pp = Get-Content -LiteralPath $ppPath -Raw | ConvertFrom-Json
        $provName = if ($isPass) { 'cline-pass' } elseif ($target.provider) { $target.provider } else { 'cline' }
        if ($pp.providers.$provName) {
            $pp.providers.$provName.settings.model = $target.model
            if ($target.thinking -and $pp.providers.$provName.settings.reasoning) {
                $pp.providers.$provName.settings.reasoning.effort = $target.thinking
            }
            $pp.providers.$provName.updatedAt = (Get-Date -AsUTC -Format 'yyyy-MM-ddTHH:mm:ss.fffZ')
            $pp | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ppPath -Force -Encoding UTF8
        }
    } catch {
        Write-Warning ("providers.json update skipped: " + $_.Exception.Message)
    }
}

Write-Host ''
Write-Host ("APPLIED: " + $Mode + " mode -> provider=" + $(if ($isPass) { 'cline-pass' } else { $target.provider }) + " model=" + $target.model + " thinking=" + $target.thinking + " [" + $target.tier + ", source=" + $target.source + "]") -ForegroundColor Green
Write-Host 'Start VS Code and open Cline - the new model is active.' -ForegroundColor White
exit 0
