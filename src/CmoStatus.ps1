# CmoStatus.ps1 - dark-themed model-usage dashboard (WPF), same family as RDC Agent Control.
# Open on demand; nothing here runs in background.
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase

. (Join-Path $PSScriptRoot 'CmoCore.ps1')
$routing = Get-CmoRoutingConfig
$cmoDir = Join-Path $env:LOCALAPPDATA 'ClineModelOptimizer'
$gLog = Join-Path $cmoDir 'guardian.log'

# ---- dark palette (Catppuccin-inspired, matches RDC Agent Control) ----
$bg = '#1E1E2E'; $bgCard = '#282838'; $fg = '#CDD6F4'; $dim = '#6C7086'
$green = '#A6E3A1'; $yellow = '#F9E2AF'; $red = '#F38BA8'; $accent = '#89B4FA'
$mauve = '#CBA6F7'

$brush = { param($hex) New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($hex)) }

$w = New-Object System.Windows.Window
$w.Title = 'Cline Model Optimizer'
$w.Width = 820; $w.Height = 640
$w.WindowStartupLocation = 'CenterScreen'
$w.Background = (& $brush $bg)
$w.FontFamily = 'Segoe UI'
$iconPath = Join-Path $PSScriptRoot 'cmo-icon.ico'
if (Test-Path -LiteralPath $iconPath) {
    try { Add-Type -AssemblyName System.Drawing; $w.Icon = New-Object System.Drawing.Icon ($iconPath) } catch { }
}

$root = New-Object System.Windows.Controls.StackPanel
$root.Margin = '20,16'
[void]$root.Children.Add((New-Object System.Windows.Controls.TextBlock -Property @{
    Text = 'CLINE MODEL OPTIMIZER'; FontSize = 22; FontWeight = 'Bold'
    Foreground = (& $brush $mauve) }))
[void]$root.Children.Add((New-Object System.Windows.Controls.TextBlock -Property @{
    Text = ('strategy: ' + $routing.strategy + '  -  free tier first, subscription/paid only when needed')
    FontSize = 12; Margin = '0,2,0,14'; Foreground = (& $brush $dim) }))

function New-Card {
    $c = New-Object System.Windows.Controls.Border -Property @{
        Background = (& $brush $bgCard); CornerRadius = '10'; Padding = '16,12'; Margin = '0,0,0,10' }
    $p = New-Object System.Windows.Controls.StackPanel
    $c.Child = $p
    [void]$root.Children.Add($c)
    return $p
}
$activePanel = New-Card
$accountPanel = New-Card
$ladderPanel = New-Card
$sessionsPanel = New-Card

$apiKey = Join-Path $cmoDir 'guardian-status.json'
$logBox = New-Object System.Windows.Controls.TextBox -Property @{
    IsReadOnly = $true; FontFamily = 'Consolas'; FontSize = 11.5
    Background = 'Transparent'; BorderThickness = 0; Foreground = (& $brush $fg)
    Height = 110; TextWrapping = 'NoWrap'; VerticalScrollBarVisibility = 'Auto' }
$logCard = New-Object System.Windows.Controls.Border -Property @{
    Background = (& $brush $bgCard); CornerRadius = '10'; Padding = '12,10'; Margin = '0,0,0,10' }
$logCard.Child = $logBox
[void]$root.Children.Add($logCard)

# ---- refresh ----
function Add-Row([System.Windows.Controls.StackPanel]$panel, [string]$label, [string]$value, [string]$valueColor) {
    $sp = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; Margin = '0,3' }
    [void]$sp.Children.Add((New-Object System.Windows.Controls.TextBlock -Property @{
        Text = ($label + '  '); Width = 170; Foreground = (& $brush $dim) }))
    [void]$sp.Children.Add((New-Object System.Windows.Controls.TextBlock -Property @{
        Text = $value; FontWeight = 'SemiBold'; Foreground = (& $brush $valueColor) }))
    [void]$panel.Children.Add($sp)
}

function Add-Section([System.Windows.Controls.StackPanel]$panel, [string]$title) {
    [void]$panel.Children.Add((New-Object System.Windows.Controls.TextBlock -Property @{
        Text = $title; FontSize = 13; FontWeight = 'Bold'; Margin = '0,0,0,6'
        Foreground = (& $brush $accent) }))
}

function Invoke-Refresh {
    foreach ($p in @($activePanel, $accountPanel, $ladderPanel, $sessionsPanel)) {
        foreach ($child in @($p.Children)) { $p.RemoveChild($child) }
    }
    $snap = Get-CmoSnapshot -Routing $routing
    if (-not $snap.StateExists) {
        Add-Section $activePanel 'CLINE STATE NOT FOUND'
        Add-Row $activePanel 'looked in' (Join-Path $env:USERPROFILE '.cline\data') $red
        return
    }

    # tier colors + usage minutes
    $st = $null
    if (Test-Path -LiteralPath $apiKey) { try { $st = Get-Content -LiteralPath $apiKey -Raw | ConvertFrom-Json } catch { } }
    $tierColor = { param($t) switch ($t) { 'FREE' { $green } 'SUBSCRIPTION' { $yellow } 'PAID' { $red } default { $dim } } }

    Add-Section $activePanel 'ACTIVE MODEL'
    foreach ($mode in 'Act','Plan') {
        $m = $snap.$mode
        $badge = ('{0}  {1}' -f $m.Tier, $m.Model)
        Add-Row $activePanel ($mode.ToUpper() + ' model') $badge (& $tierColor $m.Tier)
        Add-Row $activePanel ($mode.ToUpper() + ' reasoning') ("effort: " + $m.Reasoning) $fg
        Add-Row $activePanel ($mode.ToUpper() + ' verdict') $m.Recommendation.Message $(if ($m.Recommendation.Optimal) { $green } else { $yellow })
    }
    $tok = $snap.TokenRemainingHours
    if ($null -ne $tok) {
        Add-Row $activePanel 'cline auth token' ("expires in " + $tok + "h") $(if ($tok -lt 12) { $red } elseif ($tok -lt 48) { $yellow } else { $green })
    }
    if ($st) {
        $tm = $st.TierMinutes
        Add-Row $activePanel 'today usage' ("free: {0} min  -  subscription: {1} min  -  paid: {2} min" -f [math]::Round([double]$tm.free,0), [math]::Round([double]$tm.subscription,0), [math]::Round([double]$tm.paid,0)) $fg
    }

    Add-Section $accountPanel 'ACCOUNTS'
    foreach ($a in $snap.Accounts) {
        $who = if ($a.Email) { $a.Email } else { $a.Provider }
        Add-Row $accountPanel $a.Provider ($who + $(if ($a.LastUsed) { '  [last used]' } else { '' })) $(if ($a.LastUsed) { $green } else { $dim })
    }

    Add-Section $ladderPanel ('PREFERRED LADDER - free list: ' + $snap.DynamicFree.Source + ', ' + @($snap.DynamicFree.Models).Count + ' models' + $(if ($snap.DynamicFree.Fresh) { ' [fresh]' } else { ' [not fresh]' }))
    $rec = $snap.Act.Recommendation
    if ($rec -and $rec.Ladder) {
        $rank = 0
        foreach ($s in $rec.Ladder) {
            $rank++
            $note = ''
            if ($s.tier -eq 'SUBSCRIPTION' -and -not $s.live) { $note = '  [not in current pass list]' }
            Add-Row $ladderPanel ('#' + $rank) ($s.model + '  [' + $s.tier + ']  (' + $s.source + ', thinking=' + $s.thinking + ')' + $note) (& $tierColor $s.tier)
        }
        if ($snap.DynamicFree -and $snap.DynamicFree.Models) {
            $age = if ($null -ne $snap.DynamicFree.AgeHours) { (', age ' + $snap.DynamicFree.AgeHours + 'h') } else { '' }
            Add-Row $ladderPanel 'free list' (@($snap.DynamicFree.Models).Count.ToString() + ' models available' + $age) $dim
        }
    }

    Add-Section $sessionsPanel 'RECENT SESSIONS'
    foreach ($s in $snap.RecentSessions) {
        $tier = Get-CmoModelTier -Model $s.Model -Provider $s.Provider -Routing $routing
        $when = if ($s.StartedAt) { ([datetime]$s.StartedAt).ToLocalTime().ToString('MM-dd HH:mm') } else { '?' }
        $title = $s.Title
        if ($title -and $title.Length -gt 52) { $title = $title.Substring(0, 52) + '...' }
        Add-Row $sessionsPanel $when ($title + '  -  ' + $s.Model + '  [' + $tier + ']') (& $tierColor $tier)
    }

    $logBox.Text = ((Get-LogTail $gLog 10))
}

function Get-LogTail([string]$path, [int]$n) {
    if (Test-Path -LiteralPath $path) { return (Get-Content -LiteralPath $path -Tail $n) -join "`n" }
    return '(guardian log not created yet)'
}

# ---- buttons ----
$buttons = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal' }
function New-Button([string]$text, [scriptblock]$onClick) {
    $b = New-Object System.Windows.Controls.Button -Property @{
        Content = $text; Padding = '14,7'; Margin = '0,0,10,0'; Cursor = 'Hand'
        Background = (& $brush $bgCard); Foreground = (& $brush $fg); BorderBrush = (& $brush $accent) }
    $b.Add_Click($onClick)
    [void]$buttons.Children.Add($b)
}
New-Button 'Refresh' { Invoke-Refresh }
New-Button 'Refresh free list' {
    Get-CmoDynamicFreeModels -Routing $routing -Refresh | Out-Null
    Invoke-Refresh
}
New-Button 'Run guardian now' {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'CmoGuardian.ps1') | Out-Null
    Invoke-Refresh
}
New-Button 'Apply preferred (needs VS Code closed)' {
    $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'CmoApply.ps1') 2>&1
    [System.Windows.MessageBox]::Show(($out -join "`n"), 'Apply result', 'OK', 'Information') | Out-Null
    Invoke-Refresh
}
[void]$root.Children.Add($buttons)

$w.Content = $root
Invoke-Refresh
[void]$w.ShowDialog()
