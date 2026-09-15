# CmoStatus.ps1 - dashboard (WPF, on-demand only).
# UX: status hero on top, separated cards, page scrolls. See PART 2/3, 3/3 below.
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase

. (Join-Path $PSScriptRoot 'CmoCore.ps1')
$routing = Get-CmoRoutingConfig
$cmoDir = Join-Path $env:LOCALAPPDATA 'ClineModelOptimizer'
$gLog = Join-Path $cmoDir 'guardian.log'

# palette: deep-navy bg, soft borders, high-contrast text, teal accent
$bg = '#1A1F2E'; $bgCard = '#232B3D'; $bgBadge = '#111827'
$border = '#374151'; $fg = '#F3F4F6'; $dim = '#9CA3AF'
$green = '#4ADE80'; $amber = '#FBBF24'; $red = '#F87171'; $teal = '#5EEAD4'

$brush = { param($hex) [System.Windows.Media.ColorConverter]::ConvertFromString($hex) | ForEach-Object { New-Object System.Windows.Media.SolidColorBrush $_ } }

$w = New-Object System.Windows.Window
$w.Title = 'Cline Model Optimizer'
$w.Width = 880; $w.Height = 760
$w.MinWidth = 760; $w.MinHeight = 560
$w.WindowStartupLocation = 'CenterScreen'
$w.Background = (& $brush $bg)
$w.FontFamily = 'Segoe UI'
$iconPath = Join-Path $PSScriptRoot 'cmo-icon.ico'
if (Test-Path -LiteralPath $iconPath) {
    try {
        $uri = New-Object System.Uri ($iconPath, [System.UriKind]::Absolute)
        $dec = [System.Windows.Media.Imaging.BitmapDecoder]::Create(
            $uri, [System.Windows.Media.Imaging.BitmapCreateOptions]::PreservePixelFormat,
            [System.Windows.Media.Imaging.BitmapCacheOption]::OnLoad)
        $w.Icon = $dec.Frames[0]
    } catch { }
}

$tierColor = { param($t) switch ($t) { 'FREE' { $green } 'SUBSCRIPTION' { $amber } 'PAID' { $red } default { $dim } } }

$scroll = New-Object System.Windows.Controls.ScrollViewer -Property @{
    VerticalScrollBarVisibility = 'Auto'; Padding = '20,16,20,20' }
$root = New-Object System.Windows.Controls.StackPanel
$scroll.Content = $root
$w.Content = $scroll

$hdr = New-Object System.Windows.Controls.TextBlock -Property @{
    Text = 'Cline Model Optimizer'; FontSize = 24; FontWeight = 'Bold'
    Foreground = (& $brush $teal) }
[void]$root.Children.Add($hdr)
$sub2 = New-Object System.Windows.Controls.TextBlock -Property @{
    FontSize = 12; Margin = '0,2,0,14'; Foreground = (& $brush $dim) }
$sub2.Text = ('free tier first - subscription / paid only when needed   |   strategy: ' + $routing.strategy)
[void]$root.Children.Add($sub2)

$hero = New-Object System.Windows.Controls.Border -Property @{
    Background = (& $brush $bgCard); CornerRadius = '10'; Padding = '16,12'; Margin = '0,0,0,12'
    BorderBrush = (& $brush $border); BorderThickness = '1' }
$heroStack = New-Object System.Windows.Controls.StackPanel
$hero.Child = $heroStack
$heroText = New-Object System.Windows.Controls.TextBlock -Property @{
    FontSize = 15; FontWeight = 'SemiBold'; Foreground = (& $brush $fg); TextWrapping = 'Wrap' }
$heroSub = New-Object System.Windows.Controls.TextBlock -Property @{
    FontSize = 12; Margin = '0,4,0,0'; Foreground = (& $brush $dim); TextWrapping = 'Wrap' }
[void]$heroStack.Children.Add($heroText)
[void]$heroStack.Children.Add($heroSub)
[void]$root.Children.Add($hero)


function New-Card([string]$title) {
    $c = New-Object System.Windows.Controls.Border -Property @{
        Background = (& $brush $bgCard); CornerRadius = '10'; Padding = '16,12'; Margin = '0,0,0,12'
        BorderBrush = (& $brush $border); BorderThickness = '1' }
    $p = New-Object System.Windows.Controls.StackPanel
    $t = New-Object System.Windows.Controls.TextBlock -Property @{
        Text = $title; FontSize = 12; FontWeight = 'Bold'; Margin = '0,0,0,4'
        Foreground = (& $brush $teal) }
    [void]$p.Children.Add($t)
    $c.Child = $p
    [void]$root.Children.Add($c)
    return $p
}

function Add-Row($panel, [string]$label, [string]$value, [string]$valueColor, [string]$badge) {
    $grid = New-Object System.Windows.Controls.Grid
    $grid.Margin = '0,3'
    $c1 = New-Object System.Windows.Controls.ColumnDefinition
    $c1.Width = 150
    $c2 = New-Object System.Windows.Controls.ColumnDefinition
    $c2.Width = '*'
    [void]$grid.ColumnDefinitions.Add($c1)
    [void]$grid.ColumnDefinitions.Add($c2)
    $lab = New-Object System.Windows.Controls.TextBlock -Property @{
        Text = $label; Foreground = (& $brush $dim); FontSize = 12.5
        VerticalAlignment = 'Top'; TextWrapping = 'Wrap' }
    [void]$grid.Children.Add($lab)
    [System.Windows.Controls.Grid]::SetColumn($lab, 0)
    $valStack = New-Object System.Windows.Controls.WrapPanel -Property @{ Orientation = 'Horizontal' }
    if ($badge) {
        $bd = New-Object System.Windows.Controls.Border -Property @{
            Background = (& $brush $bgBadge); CornerRadius = '4'; Padding = '6,1'; Margin = '0,0,6,2'
            BorderBrush = (& $brush $valueColor); BorderThickness = '1' }
        $bt = New-Object System.Windows.Controls.TextBlock -Property @{
            Text = $badge; FontSize = 11; FontWeight = 'Bold'; Foreground = (& $brush $valueColor) }
        $bd.Child = $bt
        [void]$valStack.Children.Add($bd)
    }
    $val = New-Object System.Windows.Controls.TextBlock -Property @{
        Text = $value; FontWeight = 'SemiBold'; FontSize = 12.5
        Foreground = (& $brush $valueColor); TextWrapping = 'Wrap'; MaxWidth = 620 }
    $val.ToolTip = $value
    [void]$valStack.Children.Add($val)
    [void]$grid.Children.Add($valStack)
    [System.Windows.Controls.Grid]::SetColumn($valStack, 1)
    [void]$panel.Children.Add($grid)
}

function Add-Note($panel, [string]$text) {
    $n = New-Object System.Windows.Controls.TextBlock -Property @{
        Text = $text; FontSize = 11.5; Margin = '0,3'; Foreground = (& $brush $dim)
        TextWrapping = 'Wrap' }
    [void]$panel.Children.Add($n)
}

function New-SmallButton($panel, [string]$text, [scriptblock]$onClick) {
    # small inline action button (e.g. per-ladder-rank 'Use #N').
    # Returns the button so callers can set .Tag (handlers read $this.Tag -
    # loop variables must NOT be captured: all handlers would see the last value).
    $b = New-Object System.Windows.Controls.Button -Property @{
        Content = $text; Padding = '10,2'; Margin = '0,2,0,6'; Cursor = 'Hand'
        HorizontalAlignment = 'Left'; FontSize = 11.5
        Background = (& $brush $bgBadge); Foreground = (& $brush $teal); BorderBrush = (& $brush $border) }
    $b.Add_Click($onClick)
    [void]$panel.Children.Add($b)
    return $b
}

function Short-Id([string]$id) {
    if (-not $id) { return '(none)' }
    if ($id.Length -gt 48) { return ($id.Substring(0, 45) + '...') }
    return $id
}

function Get-LogTail([string]$path, [int]$n) {
    if (Test-Path -LiteralPath $path) { return (Get-Content -LiteralPath $path -Tail $n) -join "`n" }
    return '(guardian log not created yet)'
}

$activePanel  = New-Card 'ACTIVE MODEL'
$accountPanel = New-Card 'ACCOUNTS (signed-in providers)'
$ladderPanel  = New-Card 'PREFERRED LADDER'
$sessionPanel = New-Card 'RECENT SESSIONS'
$logPanel     = New-Card 'GUARDIAN LOG'

$logBox = New-Object System.Windows.Controls.TextBox -Property @{
    IsReadOnly = $true; FontFamily = 'Consolas'; FontSize = 11
    Background = 'Transparent'; BorderThickness = 0; Foreground = (& $brush $fg)
    Height = 120; TextWrapping = 'Wrap'; VerticalScrollBarVisibility = 'Auto'
    IsReadOnlyCaretVisible = $false }
[void]$logPanel.Children.Add($logBox)


function Invoke-Refresh {
    foreach ($p in @($activePanel, $accountPanel, $ladderPanel, $sessionPanel)) {
        for ($i = $p.Children.Count - 1; $i -ge 1; $i--) { $p.Children.RemoveAt($i) }
    }
    $snap = Get-CmoSnapshot -Routing $routing
    if (-not $snap.StateExists) {
        $heroText.Text = 'Cline state not found'
        $heroSub.Text = ('looked in ' + (Join-Path $env:USERPROFILE '.cline\data'))
        Add-Note $activePanel 'Install/open Cline in VS Code first.'
        return
    }

    $st = $null
    $repPath = Join-Path $cmoDir 'guardian-status.json'
    if (Test-Path -LiteralPath $repPath) { try { $st = Get-Content -LiteralPath $repPath -Raw | ConvertFrom-Json } catch { } }

    $act = $snap.Act
    if ($act.Recommendation.Optimal) {
        $heroText.Text = ('ON TRACK - using {0} [{1}]' -f (Short-Id $act.Model), $act.Tier)
        $heroText.Foreground = (& $brush $green)
    } elseif ($act.Tier -eq 'FREE') {
        $heroText.Text = ('CLOSE - free model, not the preferred one: {0}' -f (Short-Id $act.Model))
        $heroText.Foreground = (& $brush $amber)
    } else {
        $heroText.Text = ('OFF TRACK - {0} model in use: {1}' -f $act.Tier.ToLower(), (Short-Id $act.Model))
        $heroText.Foreground = (& $brush $amber)
    }
    $heroBits = @($act.Recommendation.Message)
    $tok = $snap.TokenRemainingHours
    if ($null -ne $tok) {
        if ($tok -le 0) { $heroBits += 'Cline auth EXPIRED - re-sign in' }
        elseif ($tok -lt 12) { $heroBits += ('Cline auth expires in ' + $tok + 'h') }
        else { $heroBits += ('Cline auth ok (' + $tok + 'h left)') }
    }
    if ($st -and $st.TierMinutes) {
        $tm = $st.TierMinutes
        $fz = [math]::Round([double]$tm.free, 0)
        $sz = [math]::Round([double]$tm.subscription, 0)
        $pz = [math]::Round([double]$tm.paid, 0)
        $heroBits += ('today: free {0}m / subscription {1}m / paid {2}m' -f $fz, $sz, $pz)
    }
    $heroSub.Text = ($heroBits -join '   |   ')

    $actM = $snap.Act; $planM = $snap.Plan
    $sameMode = ($actM.Provider -eq $planM.Provider -and $actM.Model -eq $planM.Model -and $actM.Reasoning -eq $planM.Reasoning)
    if ($sameMode) {
        Add-Note $activePanel 'PLAN = ACT (same)'
        $m = $actM
        Add-Row $activePanel 'model' (Short-Id $m.Model) (& $tierColor $m.Tier) $m.Tier
        Add-Row $activePanel 'provider' ([string]$m.Provider) $fg $null
        if ($m.Reasoning) { Add-Row $activePanel 'reasoning' ('effort: ' + $m.Reasoning) $fg $null }
        if ($m.Recommendation.Optimal) { Add-Row $activePanel 'verdict' $m.Recommendation.Message $green $null }
        else { Add-Row $activePanel 'verdict' $m.Recommendation.Message $amber $null }
    } else {
        foreach ($mode in 'Act','Plan') {
            $m = $snap.$mode
            Add-Note $activePanel ($mode.ToUpper() + ' MODE')
            Add-Row $activePanel 'model' (Short-Id $m.Model) (& $tierColor $m.Tier) $m.Tier
            Add-Row $activePanel 'provider' ([string]$m.Provider) $fg $null
            if ($m.Reasoning) { Add-Row $activePanel 'reasoning' ('effort: ' + $m.Reasoning) $fg $null }
            if ($m.Recommendation.Optimal) { Add-Row $activePanel 'verdict' $m.Recommendation.Message $green $null }
            else { Add-Row $activePanel 'verdict' $m.Recommendation.Message $amber $null }
        }
    }
    if ($null -ne $tok) {
        if ($tok -le 0) { Add-Row $activePanel 'auth token' 'EXPIRED - re-sign in' $red $null }
        elseif ($tok -lt 12) { Add-Row $activePanel 'auth token' ('expires in ' + $tok + 'h') $red $null }
        elseif ($tok -lt 48) { Add-Row $activePanel 'auth token' ('expires in ' + $tok + 'h') $amber $null }
        else { Add-Row $activePanel 'auth token' ('expires in ' + $tok + 'h') $green $null }
    }

    foreach ($a in @($snap.Accounts | Where-Object { $_.SignedIn })) {
        $tag = $a.Provider
        if ($a.LastUsed) { $tag += '  -  LAST USED' }
        $who = if ($a.Email) { $a.Email } else { '(no email on file)' }
        $rowColor = if ($a.LastUsed) { $green } else { $fg }
        Add-Row $accountPanel $tag $who $rowColor $null
        if ($a.Model) { Add-Row $accountPanel 'model' (Short-Id $a.Model) $dim $null }
    }
    $hidden = @(@($snap.Accounts) | Where-Object { -not $_.SignedIn })
    if ($hidden.Count -gt 0) {
        Add-Note $accountPanel ('hidden, not signed in: ' + (($hidden | ForEach-Object { $_.Provider }) -join ', '))
    }
    if (@($snap.Accounts | Where-Object { $_.SignedIn }).Count -eq 0) { Add-Note $accountPanel 'No signed-in accounts found.' }

    $rec = $snap.Act.Recommendation
    $freeWord = 'not fresh'
    if ($snap.DynamicFree.Fresh) { $freeWord = 'fresh' }
    Add-Note $ladderPanel ('free list: ' + $snap.DynamicFree.Source + ', ' + @($snap.DynamicFree.Models).Count + ' models (' + $freeWord + ')')
    if ($rec -and $rec.Ladder) {
        $rank = 0
        foreach ($s in @($rec.Ladder)) {
            $rank++
            $mark = '     '
            if ($rank -eq 1) { $mark = '>> ' }
            $detail = ($s.model + '  (' + $s.source + ', thinking=' + $s.thinking + ')')
            if ($s.tier -eq 'SUBSCRIPTION' -and -not $s.live) { $detail += '  [not in current pass list]' }
            Add-Row $ladderPanel ($mark + '#' + $rank) $detail (& $tierColor $s.tier) $s.tier
            $useBtn = New-SmallButton $ladderPanel ('Use #' + $rank) {
                $rk = [int]$this.Tag
                $ap = Join-Path $PSScriptRoot 'CmoApply.ps1'
                $aa = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $ap + '"'), '-Rank', $rk)
                $r = Invoke-CmoHidden -File 'powershell.exe' -Arguments $aa
                [System.Windows.MessageBox]::Show((($r.Output + "`n" + $r.Error).Trim()), ('Apply rank ' + $rk), 'OK', 'Information') | Out-Null
                Invoke-Refresh
            }
            $useBtn.Tag = $rank
        }
    }

    $shown = 0
    foreach ($s in @($snap.RecentSessions)) {
        if ($shown -ge 8) { break }
        $shown++
        $tier = Get-CmoModelTier -Model $s.Model -Provider $s.Provider -Routing $Routing
        $when = '?'
        if ($s.StartedAt) { $when = ([datetime]$s.StartedAt).ToLocalTime().ToString('MM-dd HH:mm') }
        $title = [string]$s.Title
        if ($title -and $title.Length -gt 60) { $title = $title.Substring(0, 60) + '...' }
        if (-not $title) { $title = '(untitled)' }
        $cost = ''
        if ($null -ne $s.Cost) { $cost = ('  -  cost $' + $s.Cost) }
        Add-Row $sessionPanel $when ($title + '  -  ' + (Short-Id $s.Model) + $cost) (& $tierColor $tier) $tier
    }
    if ($shown -eq 0) { Add-Note $sessionPanel 'No recent sessions.' }

    $logBox.Text = (Get-LogTail $gLog 12)
}

$buttons = New-Object System.Windows.Controls.WrapPanel -Property @{ Orientation = 'Horizontal'; Margin = '0,2,0,0' }
function New-Button([string]$text, [scriptblock]$onClick) {
    $b = New-Object System.Windows.Controls.Button -Property @{
        Content = $text; Padding = '14,7'; Margin = '0,0,10,8'; Cursor = 'Hand'
        Background = (& $brush $bgCard); Foreground = (& $brush $fg); BorderBrush = (& $brush $border) }
    $b.Add_Click($onClick)
    [void]$buttons.Children.Add($b)
}
function Show-AddAccountDialog {
    $d = New-Object System.Windows.Window
    $d.Title = 'Add account'; $d.Width = 420; $d.Height = 200
    $d.WindowStartupLocation = 'CenterOwner'; $d.Owner = $w
    $d.Background = (& $brush $bg)
    $sp = New-Object System.Windows.Controls.StackPanel -Property @{ Margin = '16' }
    $d.Content = $sp
    $mkLabel = { param($s) New-Object System.Windows.Controls.TextBlock -Property @{ Text = $s; Foreground = (& $brush $dim); FontSize = 12; Margin = '0,6,0,2' } }
    $mkBox = { New-Object System.Windows.Controls.TextBox -Property @{ FontSize = 13; Padding = '6,4'; Background = (& $brush $bgBadge); Foreground = (& $brush $fg); BorderBrush = (& $brush $border) } }
    [void]$sp.Children.Add((& $mkLabel 'email'))
    $emailBox = (& $mkBox); [void]$sp.Children.Add($emailBox)
    $row = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; Margin = '0,14,0,0' }
    [void]$sp.Children.Add($row)
    $ok = New-Object System.Windows.Controls.Button -Property @{ Content = 'Save'; Padding = '18,6'; Margin = '0,0,10,0' }
    $cancel = New-Object System.Windows.Controls.Button -Property @{ Content = 'Cancel'; Padding = '18,6' }
    [void]$row.Children.Add($ok); [void]$row.Children.Add($cancel)
    $ok.Add_Click({ $d.Tag = @{ Email = $emailBox.Text }; $d.DialogResult = $true; $d.Close() })
    $cancel.Add_Click({ $d.DialogResult = $false; $d.Close() })
    if ($d.ShowDialog() -eq $true) { return $d.Tag }
    return $null
}
function Show-PreferredDialog {
    # ordered preferred free-model list + ladder size. Saved to user-preferred.json
    # in LOCALAPPDATA so reinstalls never wipe it.
    $cur = Get-CmoRoutingConfig
    $d = New-Object System.Windows.Window
    $d.Title = 'Preferred models'; $d.Width = 560; $d.Height = 480
    $d.WindowStartupLocation = 'CenterOwner'; $d.Owner = $w
    $d.Background = (& $brush $bg)
    $sp = New-Object System.Windows.Controls.StackPanel -Property @{ Margin = '16' }
    $d.Content = $sp
    $mkLabel = { param($s) New-Object System.Windows.Controls.TextBlock -Property @{ Text = $s; Foreground = (& $brush $dim); FontSize = 12; Margin = '0,8,0,4'; TextWrapping = 'Wrap' } }
    [void]$sp.Children.Add((& $mkLabel 'preferred free models, in order (one per line):'))
    $box = New-Object System.Windows.Controls.TextBox -Property @{
        FontSize = 12.5; Padding = '6,4'; MinHeight = 200; MaxHeight = 240
        Background = (& $brush $bgBadge); Foreground = (& $brush $fg); BorderBrush = (& $brush $border)
        AcceptsReturn = $true; VerticalScrollBarVisibility = 'Auto'; TextWrapping = 'NoWrap' }
    $box.Text = ((@($cur.freeSelection.preferredFreeModels) | Where-Object { $_ }) -join "`r`n")
    [void]$sp.Children.Add($box)
    $cntRow = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; Margin = '0,8,0,0' }
    [void]$sp.Children.Add($cntRow)
    [void]$cntRow.Children.Add((& $mkLabel 'free models on ladder: '))
    $cntBox = New-Object System.Windows.Controls.TextBox -Property @{ Text = ([string]$cur.freeSelection.maxFreeModels); Width = 40; FontSize = 13; Padding = '4,2'; Background = (& $brush $bgBadge); Foreground = (& $brush $fg); BorderBrush = (& $brush $border) }
    [void]$cntRow.Children.Add($cntBox)
    $btnRow = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; Margin = '0,14,0,0' }
    [void]$sp.Children.Add($btnRow)
    $ok = New-Object System.Windows.Controls.Button -Property @{ Content = 'Save'; Padding = '18,6'; Margin = '0,0,10,0' }
    $reset = New-Object System.Windows.Controls.Button -Property @{ Content = 'Reset defaults'; Padding = '18,6'; Margin = '0,0,10,0' }
    $cancel = New-Object System.Windows.Controls.Button -Property @{ Content = 'Cancel'; Padding = '18,6' }
    [void]$btnRow.Children.Add($ok); [void]$btnRow.Children.Add($reset); [void]$btnRow.Children.Add($cancel)
    $ok.Add_Click({ $d.Tag = @{ Models = ($box.Text -split "`r?`n"); Count = $cntBox.Text }; $d.DialogResult = $true; $d.Close() })
    $reset.Add_Click({ $d.Tag = @{ Reset = $true }; $d.DialogResult = $true; $d.Close() })
    $cancel.Add_Click({ $d.DialogResult = $false; $d.Close() })
    if ($d.ShowDialog() -eq $true) { return $d.Tag }
    return $null
}
New-Button 'Refresh' { Invoke-Refresh }
New-Button 'Preferred models' {
    $r = Show-PreferredDialog
    if ($r -and $r.Reset) {
        $fp = Get-CmoUserPreferredPath
        if (Test-Path -LiteralPath $fp) { Remove-Item -LiteralPath $fp -Force }
        $routing = Get-CmoRoutingConfig
        Invoke-Refresh
    } elseif ($r -and $r.Models) {
        $n = 2
        try { $n = [int]$r.Count } catch { }
        if ($n -lt 1) { $n = 1 }; if ($n -gt 5) { $n = 5 }
        try {
            Set-CmoUserPreferred -PreferredFreeModels @($r.Models) -MaxFreeModels $n | Out-Null
            $routing = Get-CmoRoutingConfig
        } catch {
            [System.Windows.MessageBox]::Show($_.Exception.Message, 'Preferred models', 'OK', 'Warning') | Out-Null
        }
        Invoke-Refresh
    }
}
New-Button 'Add account' {
    $r = Show-AddAccountDialog
    if ($r -and $r.Email -and $r.Email.Trim()) {
        try {
            Add-CmoExtraAccount -Email $r.Email.Trim() | Out-Null
        } catch {
            [System.Windows.MessageBox]::Show($_.Exception.Message, 'Add account', 'OK', 'Warning') | Out-Null
        }
        Invoke-Refresh
    }
}
New-Button 'Refresh free list' {
    Get-CmoDynamicFreeModels -Routing $routing -Refresh | Out-Null
    Invoke-Refresh
}
New-Button 'Run guardian now' {
    $gp = Join-Path $PSScriptRoot 'CmoGuardian.ps1'
    $ga = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $gp + '"'))
    Invoke-CmoHidden -File 'powershell.exe' -Arguments $ga | Out-Null
    Invoke-Refresh
}
New-Button 'Apply preferred (needs VS Code closed)' {
    $ap = Join-Path $PSScriptRoot 'CmoApply.ps1'
    $aa = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $ap + '"'))
    $r = Invoke-CmoHidden -File 'powershell.exe' -Arguments $aa
    [System.Windows.MessageBox]::Show((($r.Output + "`n" + $r.Error).Trim()), 'Apply result', 'OK', 'Information') | Out-Null
    Invoke-Refresh
}
[void]$root.Children.Add($buttons)

Invoke-Refresh
[void]$w.ShowDialog()

