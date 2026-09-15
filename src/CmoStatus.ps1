# CmoStatus.ps1 - Cline Model Optimizer dashboard (WPF, on-demand only).
#
# UX RULES (why this file looks like this):
#   * ONE screen, NO modal dialogs: every card carries its own actions and is
#     edited IN PLACE, next to the data it changes.
#   * dense ledger rows instead of label/value paragraphs - only what changes a
#     decision is shown (status, usage, model, tier). No filler, no notes.
#   * "free budget left today" is per ACCOUNT: observed (guardian minutes) plus
#     user-confirmed (daily budget, 'used up' marker). Cline has NO quota API and
#     persists only one signed-in login, so nothing here is invented.
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase

. (Join-Path $PSScriptRoot 'CmoCore.ps1')
$script:routing = Get-CmoRoutingConfig
$cmoDir = Join-Path $env:LOCALAPPDATA 'ClineModelOptimizer'
$gLog = Join-Path $cmoDir 'guardian.log'

# palette: deep-navy surfaces, teal accent, traffic-light status colours
$bg = '#1A1F2E'; $bgCard = '#232B3D'; $bgBadge = '#111827'
$bgRow = '#1E2637'; $bgHot = '#243149'
$border = '#374151'; $fg = '#F3F4F6'; $dim = '#9CA3AF'
$green = '#4ADE80'; $amber = '#FBBF24'; $red = '#F87171'; $teal = '#5EEAD4'

$brush = { param($hex) New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($hex)) }
$tierColor = { param($t) switch ($t) { 'FREE' { $green } 'SUBSCRIPTION' { $amber } 'PAID' { $red } default { $dim } } }
$healthColor = { param($h) switch ($h) { 'GREEN' { $green } 'AMBER' { $amber } 'RED' { $red } default { $dim } } }

# ---------- tiny UI builders ----------
function New-Text([string]$text, [double]$size, [string]$color, [string]$weight = 'Normal') {
    return New-Object System.Windows.Controls.TextBlock -Property @{
        Text = $text; FontSize = $size; Foreground = (& $brush $color)
        FontWeight = $weight; TextWrapping = 'Wrap'; VerticalAlignment = 'Center' }
}

function New-Pill([string]$text, [string]$color) {
    $bd = New-Object System.Windows.Controls.Border -Property @{
        Background = (& $brush $bgBadge); CornerRadius = '9'; Padding = '7,1'; Margin = '0,0,5,0'
        BorderBrush = (& $brush $color); BorderThickness = '1'; VerticalAlignment = 'Center' }
    $bd.Child = (New-Text $text 10.5 $color 'Bold')
    return $bd
}

function New-TinyButton([string]$text, [scriptblock]$onClick, [string]$color = '') {
    if (-not $color) { $color = $teal }
    $b = New-Object System.Windows.Controls.Button -Property @{
        Content = $text; Padding = '8,1'; Margin = '0,0,4,0'; Cursor = 'Hand'; FontSize = 11
        Background = (& $brush $bgBadge); Foreground = (& $brush $color)
        BorderBrush = (& $brush $border); BorderThickness = '1'; VerticalAlignment = 'Center' }
    $b.Add_Click($onClick)
    return $b
}

function New-RowGrid([string[]]$widths) {
    # ledger row: fixed / star columns, so rows line up like a table
    $g = New-Object System.Windows.Controls.Grid
    foreach ($wd in $widths) {
        $cd = New-Object System.Windows.Controls.ColumnDefinition
        $cd.Width = $wd
        [void]$g.ColumnDefinitions.Add($cd)
    }
    return $g
}

function New-RowSurface([string]$background = '') {
    if (-not $background) { $background = $bgRow }
    return New-Object System.Windows.Controls.Border -Property @{
        Background = (& $brush $background); CornerRadius = '5'; Padding = '8,5'; Margin = '0,0,0,4' }
}

function New-Card([string]$title, [string]$hint = '') {
    $outer = New-Object System.Windows.Controls.StackPanel
    $head = New-RowGrid @('*', 'Auto')
    $tl = New-Text $title 12.5 $teal 'Bold'
    [void]$head.Children.Add($tl)
    $acts = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; HorizontalAlignment = 'Right' }
    [void]$head.Children.Add($acts)
    [System.Windows.Controls.Grid]::SetColumn($acts, 1)
    [void]$outer.Children.Add($head)
    $sub = New-Text $hint 11.5 $dim
    $sub.Margin = '0,3,0,6'
    [void]$outer.Children.Add($sub)
    $body = New-Object System.Windows.Controls.StackPanel
    [void]$outer.Children.Add($body)
    $b = New-Object System.Windows.Controls.Border -Property @{
        Background = (& $brush $bgCard); CornerRadius = '8'; Padding = '12,10'; Margin = '0,0,0,8'
        BorderBrush = (& $brush $border); BorderThickness = '1' }
    $b.Child = $outer
    return [pscustomobject]@{ Card = $b; Body = $body; Actions = $acts; Sub = $sub }
}

function Clear-Body($card) {
    for ($i = $card.Body.Children.Count - 1; $i -ge 0; $i--) { $card.Body.Children.RemoveAt($i) }
}

# ---------- window ----------
$w = New-Object System.Windows.Window
$w.Title = 'Cline Model Optimizer'
$w.Width = 1020; $w.Height = 780
$w.MinWidth = 880; $w.MinHeight = 560
$w.WindowStartupLocation = 'CenterScreen'
$w.Background = (& $brush $bg)
$w.FontFamily = 'Segoe UI'
$iconPath = Join-Path $PSScriptRoot 'cmo-icon.ico'
if (Test-Path -LiteralPath $iconPath) {
    try {
        # a .ico holds several sizes; take the LARGEST frame so the taskbar and
        # alt-tab get a crisp icon instead of an upscaled 16px one.
        $uri = New-Object System.Uri ($iconPath, [System.UriKind]::Absolute)
        $dec = [System.Windows.Media.Imaging.IconBitmapDecoder]::new(
            $uri, [System.Windows.Media.Imaging.BitmapCreateOptions]::PreservePixelFormat,
            [System.Windows.Media.Imaging.BitmapCacheOption]::OnLoad)
        $best = $dec.Frames[0]
        foreach ($f in $dec.Frames) { if ($f.PixelWidth -gt $best.PixelWidth) { $best = $f } }
        $w.Icon = $best
    } catch { }
}

$scroll = New-Object System.Windows.Controls.ScrollViewer -Property @{
    VerticalScrollBarVisibility = 'Auto'; Padding = '16,12,16,14' }
$root = New-Object System.Windows.Controls.StackPanel
$scroll.Content = $root
$w.Content = $scroll

# ---------- child-process actions (headless; never flash a console) ----------
function Invoke-CmoScript([string]$Name, [string[]]$Extra = @()) {
    $p = Join-Path $PSScriptRoot $Name
    $a = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $p + '"')) + $Extra
    return Invoke-CmoHidden -File 'powershell.exe' -Arguments $a
}

function Start-CmoApply([int]$Rank = 1, [string]$Mode = 'both') {
    # Cline's extension rewrites its in-memory config from globalState.json, so a
    # live VS Code would fight us - CmoApply handles that check itself.
    $r = Invoke-CmoScript 'CmoApply.ps1' @('-Rank', [string]$Rank, '-Mode', $Mode)
    $msg = (($r.Output + "`n" + $r.Error).Trim())
    if (-not $msg) { $msg = 'no output' }
    [System.Windows.MessageBox]::Show($msg, ('Apply ladder #' + $Rank + ' (' + $Mode + ')'), 'OK', 'Information') | Out-Null
    Invoke-Refresh
}

function Start-CmoGuardian {
    Invoke-CmoScript 'CmoGuardian.ps1' | Out-Null
    Invoke-Refresh
}

function Open-Cline {
    $cands = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Microsoft VS Code\Code.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft VS Code\Code.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft VS Code\Code.exe'))
    foreach ($c in $cands) {
        if ($c -and (Test-Path -LiteralPath $c)) { Start-Process -FilePath $c | Out-Null; return }
    }
    Start-Process 'code' | Out-Null
}

# ---------- header strip ----------
$headRow = New-RowGrid @('Auto', '*', 'Auto')
[void]$headRow.Children.Add((New-Text 'Cline Model Optimizer' 18 $teal 'Bold'))
$mid = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; Margin = '14,0,0,0' }
$pillBorder = New-Object System.Windows.Controls.Border -Property @{
    Background = (& $brush $bgBadge); CornerRadius = '10'; Padding = '9,2'
    BorderBrush = (& $brush $dim); BorderThickness = '1'; VerticalAlignment = 'Center' }
$pillText = New-Text 'checking...' 12 $dim 'Bold'
$pillBorder.Child = $pillText
[void]$mid.Children.Add($pillBorder)
[void]$headRow.Children.Add($mid)
[System.Windows.Controls.Grid]::SetColumn($mid, 1)
$headActs = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; HorizontalAlignment = 'Right' }
[void]$headRow.Children.Add($headActs)
[System.Windows.Controls.Grid]::SetColumn($headActs, 2)
[void]$root.Children.Add($headRow)

$statusLine = New-Text 'reading Cline state...' 12 $dim
$statusLine.Margin = '0,7,0,2'
[void]$root.Children.Add($statusLine)
$updatedLine = New-Text '' 10.5 $dim
$updatedLine.Margin = '0,0,0,10'
[void]$root.Children.Add($updatedLine)

# ---------- layout: accounts full width, then ladder | active+recent+system ----------
$acctCard = New-Card 'ACCOUNTS - free budget left today'
[void]$root.Children.Add($acctCard.Card)

$cols = New-RowGrid @('*', '384')
$cols.Margin = '0,0,0,0'
$leftCol = New-Object System.Windows.Controls.StackPanel
$rightCol = New-Object System.Windows.Controls.StackPanel
[void]$cols.Children.Add($leftCol)
[void]$cols.Children.Add($rightCol)
[System.Windows.Controls.Grid]::SetColumn($rightCol, 1)
$rightCol.Margin = '10,0,0,0'
[void]$root.Children.Add($cols)

$ladderCard = New-Card 'PREFERRED LADDER - free models first'
$activeCard = New-Card 'WHAT CLINE IS USING'
$recentCard = New-Card 'RECENT ACTIVITY'
$systemCard = New-Card 'GUARDIAN & FREE LIST'
[void]$leftCol.Children.Add($ladderCard.Card)
[void]$rightCol.Children.Add($activeCard.Card)
[void]$rightCol.Children.Add($recentCard.Card)
[void]$rightCol.Children.Add($systemCard.Card)


# ---------- ACCOUNTS ----------
function Render-Accounts {
    Clear-Body $acctCard
    foreach ($c in @($acctCard.Actions.Children)) { $acctCard.Actions.Children.Remove($c) }

    $acc = @(Get-CmoAccounts)
    $rows = @(Get-CmoAccountSummary -Accounts $acc)
    $counts = Get-CmoAccountCounts -Accounts $rows

    $tally = ($counts.Green.ToString() + ' green')
    if ($counts.Amber -gt 0) { $tally += (' | ' + $counts.Amber + ' close to limit') }
    if ($counts.Red -gt 0) { $tally += (' | ' + $counts.Red + ' used up today') }
    if ($counts.SignedIn -eq 0) { $tally = 'no accounts tracked yet' }
    $acctCard.Sub.Text = ($tally + '   |   resets in ' + (Get-CmoDailyResetText) +
        '   |   minutes the guardian saw today')

    $addBtn = New-TinyButton '+ Add account' { Show-CmoAddAccount } $green
    [void]$acctCard.Actions.Children.Add($addBtn)

    foreach ($r in $rows) { [void]$acctCard.Body.Children.Add((New-AccountRow $r)) }

    $unscored = Get-CmoUnscoredProviders -Accounts $acc
    if ($unscored.Count -gt 0) {
        $n = New-Text ('not signed in (unscored, hidden): ' + ($unscored -join ', ')) 10.5 $dim
        $n.Margin = '2,2,0,0'
        [void]$acctCard.Body.Children.Add($n)
    }
    $foot = 'Cline has no quota API - the counter is what the guardian observed today. Mark an account used up when Cline reports its free tier is done.'
    [void]$acctCard.Body.Children.Add((New-Text $foot 10.5 $dim))
}

function Show-CmoAddAccount {
    $tb = New-Object System.Windows.Controls.TextBox -Property @{ Width = 260; FontSize = 12; Padding = '4,2' }
    $tb.Text = '@'
    $win = New-Object System.Windows.Window -Property @{
        Title = 'Add account'; SizeToContent = 'WidthAndHeight'; Owner = $w
        WindowStartupLocation = 'CenterOwner'; Background = (& $brush $bgCard); ResizeMode = 'NoResize' }
    $p = New-Object System.Windows.Controls.StackPanel -Property @{ Margin = '14' }
    [void]$p.Children.Add((New-Text 'Sign-in email to track (budget + used-up marker key)' 12 $dim))
    $tb.Margin = '0,6,0,10'
    [void]$p.Children.Add($tb)
    $btns = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; HorizontalAlignment = 'Right' }
    $okBtn = New-TinyButton 'Add' {
        $mail = ($tb.Text).Trim()
        if ($mail -and $mail -ne '@' -and $mail -match '@') {
            Add-CmoExtraAccount -Email $mail -Provider 'cline' | Out-Null
            $win.Close()
            Render-Accounts
        }
    } $green
    [void]$btns.Children.Add((New-TinyButton 'Cancel' { $win.Close() } $dim))
    [void]$btns.Children.Add($okBtn)
    [void]$p.Children.Add($btns)
    $win.Content = $p
    $tb.Add_KeyDown({ param($s, $e)
            if ($e.Key -eq 'Return') { $okBtn.RaiseEvent((New-Object System.Windows.RoutedEventArgs ([System.Windows.Controls.Button]::ClickEvent))) } })
    $tb.CaretIndex = 0
    $win.Add_ContentRendered({ $tb.Focus() | Out-Null })
    $win.ShowDialog() | Out-Null
}

function Show-CmoBudgetEditor([object]$Row) {
    $tb = New-Object System.Windows.Controls.TextBox -Property @{ Width = 70; FontSize = 12; Text = [string][int]$Row.BudgetMin }
    $win = New-Object System.Windows.Window -Property @{
        Title = 'Daily free budget'; SizeToContent = 'WidthAndHeight'; Owner = $w
        WindowStartupLocation = 'CenterOwner'; Background = (& $brush $bgCard); ResizeMode = 'NoResize' }
    $p = New-Object System.Windows.Controls.StackPanel -Property @{ Margin = '14' }
    [void]$p.Children.Add((New-Text ('Minutes of free usage per day for ' + $Row.Email) 12 $dim))
    $tb.Margin = '0,6,0,10'
    [void]$p.Children.Add($tb)
    [void]$p.Children.Add((New-TinyButton 'Save' {
                try { Set-CmoAccountBudgetMin -Email $Row.Email -Minutes ([int]$tb.Text) | Out-Null } catch { }
                $win.Close(); Render-Accounts } $green))
    $win.Content = $p
    $tb.Add_KeyDown({ param($s, $e) if ($e.Key -eq 'Return') { $win.Close(); Render-Accounts } })
    $win.Add_ContentRendered({ $tb.Focus() | Out-Null; $tb.SelectAll() })
    $win.ShowDialog() | Out-Null
}




function New-AccountRow([object]$r) {
    $row = New-RowSurface
    $g = New-RowGrid @('18', '*', '52', '90', 'Auto')
    $lamp = New-Object System.Windows.Shapes.Ellipse -Property @{
        Width = 11; Height = 11; Fill = (& $brush (& $healthColor $r.Health)); VerticalAlignment = 'Center' }
    $lamp.ToolTip = ($r.Health + ': ' + $r.HealthLabel)
    [void]$g.Children.Add($lamp)
    # one line per account: email + provenance pill (no second line = no wasted space)
    $nameStack = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; VerticalAlignment = 'Center' }
    $emailT = New-Text $r.Email 13 $fg 'SemiBold'
    $emailT.Margin = '0,0,7,0'
    [void]$nameStack.Children.Add($emailT)
    if ($r.Sources -contains 'cline-file') { [void]$nameStack.Children.Add((New-Pill 'cline login' $teal)) }
    else { [void]$nameStack.Children.Add((New-Pill 'added' $dim)) }
    [void]$g.Children.Add($nameStack)
    [System.Windows.Controls.Grid]::SetColumn($nameStack, 1)
    $live = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; VerticalAlignment = 'Center' }
    if ($r.Quota -eq 'LIVE') { [void]$live.Children.Add((New-Pill 'LIVE' $green)) }
    if ($r.DepletedToday) { [void]$live.Children.Add((New-Pill 'USED UP' $red)) }
    [void]$g.Children.Add($live)
    [System.Windows.Controls.Grid]::SetColumn($live, 2)
    $usage = New-Object System.Windows.Controls.StackPanel -Property @{ VerticalAlignment = 'Center'; ToolTip = $r.HealthLabel }
    [void]$usage.Children.Add((New-Text ('{0:0}m / {1}m' -f $r.UsedMin, $r.BudgetMin) 12 $fg))
    $bar = New-Object System.Windows.Controls.ProgressBar -Property @{
        Minimum = 0; Maximum = 100; Value = $r.Percent; Height = 4; Width = 84
        Background = (& $brush $bgBadge); Foreground = (& $brush (& $healthColor $r.Health));
        BorderThickness = '0'; Margin = '0,3,0,0' }
    [void]$usage.Children.Add($bar)
    [void]$g.Children.Add($usage)
    [System.Windows.Controls.Grid]::SetColumn($usage, 3)
    $cell = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; HorizontalAlignment = 'Right'; VerticalAlignment = 'Center' }
    $email = $r.Email
    [void]$cell.Children.Add((New-TinyButton ('budget ' + [int]$r.BudgetMin + 'm') { Show-CmoBudgetEditor $r } $teal))
    if ($r.DepletedToday) {
        [void]$cell.Children.Add((New-TinyButton 'clear used-up' { Set-CmoAccountDepleted -Email $email -Off; Render-Accounts } $amber))
    } else {
        [void]$cell.Children.Add((New-TinyButton 'mark used up' { Set-CmoAccountDepleted -Email $email; Render-Accounts } $dim))
    }
    if ($r.Sources -contains 'added') {
        [void]$cell.Children.Add((New-TinyButton 'remove' { Remove-CmoExtraAccount -Email $email | Out-Null; Render-Accounts } $red))
    }
    [void]$g.Children.Add($cell)
    [System.Windows.Controls.Grid]::SetColumn($cell, 4)
    $row.Child = $g
    return $row
}

# ---------- PREFERRED LADDER ----------
function Get-CmoPreferredIds {
    # the user's PINNED free models, in order (this is what the editor mutates)
    $ids = @()
    try { $ids = @($script:routing.freeSelection.preferredFreeModels | Where-Object { $_ }) } catch { }
    return $ids
}

function Save-CmoPreferredIds {
    param([string[]]$Ids, [int]$MaxFree = 0)
    if ($MaxFree -lt 1) { try { $MaxFree = [int]$script:routing.freeSelection.maxFreeModels } catch { $MaxFree = 2 } }
    if ($MaxFree -lt 1) { $MaxFree = 1 }
    Set-CmoUserPreferred -PreferredFreeModels @($Ids) -MaxFreeModels $MaxFree | Out-Null
    $script:routing = Get-CmoRoutingConfig
    Render-Ladder
}

function Move-CmoPreferredId([string]$Id, [int]$Delta) {
    $ids = @(Get-CmoPreferredIds)
    $i = [array]::IndexOf($ids, $Id)
    if ($i -lt 0) { return }
    $j = $i + $Delta
    if ($j -lt 0 -or $j -ge $ids.Count) { return }
    $tmp = $ids[$i]; $ids[$i] = $ids[$j]; $ids[$j] = $tmp
    Save-CmoPreferredIds -Ids $ids
}

function Remove-CmoPreferredId([string]$Id) {
    $ids = @(Get-CmoPreferredIds | Where-Object { $_ -ne $Id })
    Save-CmoPreferredIds -Ids $ids
}

function Add-CmoPreferredId([string]$Id) {
    if (-not $Id) { return }
    $ids = @(Get-CmoPreferredIds)
    if (@($ids | Where-Object { $_.ToLowerInvariant() -eq $Id.ToLowerInvariant() }).Count -gt 0) { return }
    $ids += $Id
    Save-CmoPreferredIds -Ids $ids
}


function Render-Ladder {
    Clear-Body $ladderCard
    foreach ($c in @($ladderCard.Actions.Children)) { $ladderCard.Actions.Children.Remove($c) }

    $pinned = @(Get-CmoPreferredIds)
    try { $maxFree = [int]$script:routing.freeSelection.maxFreeModels } catch { $maxFree = 2 }
    if ($maxFree -lt 1) { $maxFree = 1 }

    # inline ladder-size picker: how many free models stay on the ladder
    $sizeLab = New-Text 'free on ladder' 10.5 $dim
    $sizeLab.Margin = '0,0,6,0'
    [void]$ladderCard.Actions.Children.Add($sizeLab)
    for ($n = 1; $n -le 5; $n++) {
        $col = $dim
        if ($n -eq $maxFree) { $col = $teal }
        $nn = $n
        [void]$ladderCard.Actions.Children.Add((New-TinyButton ([string]$n) {
                    Save-CmoPreferredIds -Ids (Get-CmoPreferredIds) -MaxFree $nn } $col))
    }
    [void]$ladderCard.Actions.Children.Add((New-TinyButton 'reset defaults' {
                Remove-Item -LiteralPath (Get-CmoUserPreferredPath) -ErrorAction SilentlyContinue
                $script:routing = Get-CmoRoutingConfig
                Render-Ladder } $amber))

    $eff = Get-CmoEffectiveStrategy -Routing $script:routing -Mode 'act'
    $steps = @($eff.Steps)
    $dyn = $eff.Dynamic

    $ladderCard.Sub.Text = ('1 = best free. pin = keep, arrows = order, Use = write into Cline   |   free list: ' + [string]$dyn.Source + $(if ($dyn.Fresh) { ' (live)' } else { ' (cached)' }))

    $rank = 0
    foreach ($s in $steps) {
        $rank++
        $isPinned = (@($pinned | Where-Object { $_.ToLowerInvariant() -eq ([string]$s.model).ToLowerInvariant() }).Count -gt 0)
        $rowBg = $bgRow
        if ($s.tier -eq 'FREE') { $rowBg = $bgHot }
        $row = New-RowSurface $rowBg
        $g = New-RowGrid @('30', '*', 'Auto')
        $rankT = New-Text ([string]$rank) 16 $teal 'Bold'
        if ($s.tier -eq 'FREE') { $rankT.Foreground = (& $brush $green) }
        [void]$g.Children.Add($rankT)
        $mid = New-Object System.Windows.Controls.StackPanel -Property @{ VerticalAlignment = 'Center' }
        $line1 = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal' }
        [void]$line1.Children.Add((New-Text ([string]$s.model) 13.5 $fg 'SemiBold'))
        [void]$line1.Children.Add((New-Pill ([string]$s.tier) (& $tierColor $s.tier)))
        if ($s.thinking -and $s.thinking -ne 'off') { [void]$line1.Children.Add((New-Pill ('think:' + $s.thinking) $dim)) }
        if ($isPinned) { [void]$line1.Children.Add((New-Pill 'PINNED' $teal)) }
        if (-not $s.live) { [void]$line1.Children.Add((New-Pill 'not live' $red)) }
        [void]$mid.Children.Add($line1)
        [void]$mid.Children.Add((New-Text (([string]$s.provider) + '  |  ' + [string]$s.source) 10.5 $dim))
        [void]$g.Children.Add($mid)
        [System.Windows.Controls.Grid]::SetColumn($mid, 1)
        $acts = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; HorizontalAlignment = 'Right'; VerticalAlignment = 'Center' }
        $mv = [string]$s.model
        if ($isPinned) {
            [void]$acts.Children.Add((New-TinyButton ([char]0x25B2) { Move-CmoPreferredId $mv -1 } $teal))
            [void]$acts.Children.Add((New-TinyButton ([char]0x25BC) { Move-CmoPreferredId $mv 1 } $teal))
            [void]$acts.Children.Add((New-TinyButton ([char]0x2715) { Remove-CmoPreferredId $mv } $red))
        } else {
            [void]$acts.Children.Add((New-TinyButton '+ pin' { Add-CmoPreferredId $mv } $green))
        }
        $rn = $rank
        [void]$acts.Children.Add((New-TinyButton ('Use #' + $rn) { Start-CmoApply -Rank $rn } $teal))
        [void]$g.Children.Add($acts)
        [System.Windows.Controls.Grid]::SetColumn($acts, 2)
        $row.Child = $g
        [void]$ladderCard.Body.Children.Add($row)
    }

    # inline add: pick a model straight from the live free list
    $freeIds = @($dyn.Models | Where-Object { $_ } | Select-Object -Unique)
    $candidates = @($freeIds | Where-Object { $l = $_.ToLowerInvariant(); @($pinned | Where-Object { $_.ToLowerInvariant() -eq $l }).Count -eq 0 })
    if ($candidates.Count -gt 0) {
        $addRow = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; Margin = '0,6,0,0' }
        $cb = New-Object System.Windows.Controls.ComboBox -Property @{ Width = 300; FontSize = 11.5; VerticalAlignment = 'Center' }
        foreach ($c in $candidates) { [void]$cb.Items.Add($c) }
        $cb.SelectedIndex = 0
        [void]$addRow.Children.Add($cb)
        [void]$addRow.Children.Add((New-TinyButton '+ add to top' {
                    if ($cb.SelectedItem) { Add-CmoPreferredId ([string]$cb.SelectedItem) } } $green))
        [void]$ladderCard.Body.Children.Add($addRow)
    } elseif ($pinned.Count -eq 0) {
        [void]$ladderCard.Body.Children.Add((New-Text 'no free models resolved yet - press Refresh to fetch the live list' 11 $amber))
    }
}



# ---------- WHAT CLINE IS USING ----------
function Render-Active([object]$snap) {
    Clear-Body $activeCard
    if (-not $snap.StateExists -or -not $snap.Act) {
        [void]$activeCard.Body.Children.Add((New-Text 'no Cline state yet - open Cline in VS Code once' 11.5 $amber))
        return
    }
    $plan = $snap.Plan; $act = $snap.Act

    # When plan and act resolve to the same model, print ONE block instead of two
    # identical ones (that was pure wasted space).
    $same = ($plan.Model -eq $act.Model -and $plan.Provider -eq $act.Provider -and $plan.Reasoning -eq $act.Reasoning)
    if ($same) {
        [void]$activeCard.Body.Children.Add((New-ModeBlock 'plan = act (same)' $act))
    } else {
        [void]$activeCard.Body.Children.Add((New-ModeBlock 'ACT' $act))
        [void]$activeCard.Body.Children.Add((New-ModeBlock 'PLAN' $plan))
    }

    if ($snap.LiveAccount) {
        [void]$activeCard.Body.Children.Add((New-Text ('driven by ' + $snap.LiveAccount.Email) 11 $dim))
    }

    # the verdict, in plain words
    $rec = $act.Recommendation
    $verdict = ''
    $vCol = $dim
    if ($rec) {
        if ($rec.Optimal) { $verdict = 'already on the best free model'; $vCol = $green }
        else { $verdict = [string]$rec.Message; $vCol = $amber }
    }
    if ($verdict) {
        $vt = New-Text $verdict 11.5 $vCol
        $vt.Margin = '0,4,0,0'
        [void]$activeCard.Body.Children.Add($vt)
    }
}

function New-ModeBlock([string]$label, [object]$m) {
    $b = New-RowSurface $bgBadge
    $sp = New-Object System.Windows.Controls.StackPanel
    $l1 = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal' }
    [void]$l1.Children.Add((New-Text $label 10.5 $dim 'Bold'))
    [void]$l1.Children.Add((New-Pill ([string]$m.Tier) (& $tierColor $m.Tier)))
    [void]$sp.Children.Add($l1)
    [void]$sp.Children.Add((New-Text ([string]$m.Model) 13 $fg 'SemiBold'))
    $sub = [string]$m.Provider
    if ($m.Reasoning) { $sub += '   |   reasoning: ' + [string]$m.Reasoning }
    [void]$sp.Children.Add((New-Text $sub 10.5 $dim))
    $b.Child = $sp
    return $b
}

# ---------- RECENT ACTIVITY ----------
function Render-Recent([object]$snap) {
    Clear-Body $recentCard
    $shown = 0
    foreach ($s in @($snap.RecentSessions)) {
        if ($shown -ge 6) { break }
        $shown++
        $tier = Get-CmoModelTier -Model $s.Model -Provider $s.Provider -Routing $script:routing
        $when = '?'
        if ($s.StartedAt) { $when = ([datetime]$s.StartedAt).ToLocalTime().ToString('MM-dd HH:mm') }
        $title = [string]$s.Title
        if ($title -and $title.Length -gt 46) { $title = $title.Substring(0, 46) + '...' }
        if (-not $title) { $title = '(untitled)' }
        $row = New-RowSurface
        $g = New-RowGrid @('84', '*', 'Auto')
        [void]$g.Children.Add((New-Text $when 11 $dim))
        $mid = New-Object System.Windows.Controls.StackPanel -Property @{ VerticalAlignment = 'Center' }
        [void]$mid.Children.Add((New-Text $title 11.5 $fg))
        [void]$mid.Children.Add((New-Text ([string]$s.Model) 10.5 $dim))
        [void]$g.Children.Add($mid)
        [System.Windows.Controls.Grid]::SetColumn($mid, 1)
        $pills = New-Object System.Windows.Controls.StackPanel -Property @{ Orientation = 'Horizontal'; VerticalAlignment = 'Center' }
        [void]$pills.Children.Add((New-Pill ([string]$tier) (& $tierColor $tier)))
        [void]$g.Children.Add($pills)
        [System.Windows.Controls.Grid]::SetColumn($pills, 2)
        $row.Child = $g
        [void]$recentCard.Body.Children.Add($row)
    }
    if ($shown -eq 0) { [void]$recentCard.Body.Children.Add((New-Text 'no recent sessions' 11 $dim)) }
}


# ---------- GUARDIAN & FREE LIST ----------
function Render-System([object]$snap) {
    Clear-Body $systemCard
    foreach ($c in @($systemCard.Actions.Children)) { $systemCard.Actions.Children.Remove($c) }
    [void]$systemCard.Actions.Children.Add((New-TinyButton 'run guardian' { Start-CmoGuardian } $teal))
    [void]$systemCard.Actions.Children.Add((New-TinyButton 'refresh free list' {
                Get-CmoDynamicFreeModels -Routing $script:routing -Refresh | Out-Null
                Invoke-Refresh } $teal))
    [void]$systemCard.Actions.Children.Add((New-TinyButton 'open VS Code' { Open-Cline } $dim))

    $dyn = $snap.DynamicFree
    if (-not $dyn) { $dyn = Get-CmoDynamicFreeModels -Routing $script:routing }
    $fresh = 'stale'
    if ($dyn.Fresh) { $fresh = 'live' }
    $lines = @()
    $lines += ('free models now: ' + @($dyn.Models).Count + '  (' + [string]$dyn.Source + ', ' + $fresh + ')')
    if ($null -ne $dyn.AgeHours) { $lines += ('list age: ' + [string]$dyn.AgeHours + 'h') }
    $lines += ('cline-pass models: ' + @($dyn.ClinePass).Count)
    $lines += ('daily counters reset in: ' + (Get-CmoDailyResetText))
    if ($dyn.Error) { $lines += ('last fetch error: ' + [string]$dyn.Error) }
    foreach ($l in $lines) { [void]$systemCard.Body.Children.Add((New-Text $l 11 $dim)) }

    $log = ''
    if (Test-Path -LiteralPath $gLog) { $log = ((Get-Content -LiteralPath $gLog -Tail 6) -join "`n") }
    if ($log) {
        $lt = New-Text $log 10 '#6B7280'
        $lt.FontFamily = 'Consolas'
        $lt.Margin = '0,6,0,0'
        [void]$systemCard.Body.Children.Add($lt)
    }
}

# ---------- header actions + refresh ----------
[void]$headActs.Children.Add((New-TinyButton 'Refresh' { Invoke-Refresh } $teal))
[void]$headActs.Children.Add((New-TinyButton 'Use #1 best free' { Start-CmoApply -Rank 1 } $green))

function Invoke-Refresh {
    $snap = Get-CmoSnapshot -Routing $script:routing
    Render-Accounts
    Render-Ladder
    Render-Active $snap
    Render-Recent $snap
    Render-System $snap

    # one-line verdict: optimal (green) / suboptimal (amber) / paid (red)
    $v = 'no Cline state found - open Cline in VS Code once'
    $vCol = $red
    if ($snap.StateExists -and $snap.Act) {
        $tier = [string]$snap.Act.Tier
        if ($snap.Act.Recommendation -and $snap.Act.Recommendation.Optimal) {
            $v = 'on the best free model'
            $vCol = $green
        } elseif ($tier -eq 'FREE') {
            $v = 'free model in use, but not the top of the ladder'
            $vCol = $amber
        } elseif ($tier -eq 'SUBSCRIPTION') {
            $v = 'subscription model in use - a free model is available'
            $vCol = $amber
        } elseif ($tier -eq 'PAID') {
            $v = 'PAID model in use'
            $vCol = $red
        } else {
            $v = 'unknown tier in use'
            $vCol = $amber
        }
        $msg = [string]$snap.Act.Recommendation.Message
        if ($msg) { $v += '  -  ' + $msg }
    }
    $statusLine.Text = $v
    $statusLine.Foreground = (& $brush $vCol)
    $pillWord = 'CHECK'
    if ($vCol -eq $green) { $pillWord = 'OPTIMAL' } elseif ($vCol -eq $amber) { $pillWord = 'SUBOPTIMAL' }
    $pillText.Text = $pillWord
    $pillText.Foreground = (& $brush $vCol)
    $pillBorder.BorderBrush = (& $brush $vCol)
    $live = 'n/a'
    if ($snap.Act) { $live = [string]$snap.Act.Model }
    $srcName = 'n/a'
    if ($snap.DynamicFree) { $srcName = [string]$snap.DynamicFree.Source }
    $gWhen = 'never'
    if (Test-Path -LiteralPath $gLog) { $gWhen = (Get-Item $gLog).LastWriteTime.ToString('HH:mm') }
    $updatedLine.Text = ('updated ' + (Get-Date -Format 'HH:mm:ss') + '   |   in use: ' + $live +
        '   |   free list: ' + $srcName + '   |   guardian last ran: ' + $gWhen)
}

Invoke-Refresh
[void]$w.ShowDialog()