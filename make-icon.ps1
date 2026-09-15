# Icon branding: big teal 'C' glyph on navy tile + green status dot.
# Bold Segoe UI 'C' centered, thin teal edge on the tile. Distinct from RDC's amber chevron.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$out = Join-Path $PSScriptRoot 'src\cmo-icon.ico'

$card   = [System.Drawing.Color]::FromArgb(255, 26, 31, 46)       # #1A1F2E background
$teal   = [System.Drawing.Color]::FromArgb(255, 94, 234, 212)      # #5EEAD4 ring + symbol
$ringDim= [System.Drawing.Color]::FromArgb(255, 55, 65, 81)        # #374151 track
$green  = [System.Drawing.Color]::FromArgb(255, 74, 222, 128)      # #4ADE80 status dot

$pngs = @{}
foreach ($size in 16, 24, 32, 48, 64, 128, 256) {
    $bmp = New-Object System.Drawing.Bitmap $size, $size
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'
    $g.TextRenderingHint = 'AntiAlias'

    $r = [Math]::Max(2, [int]($size * 0.22))
    $rect = New-Object System.Drawing.Rectangle 0, 0, ($size - 1), ($size - 1)

    $bgBrush = New-Object System.Drawing.SolidBrush $card
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $path.AddArc($rect.X, $rect.Y, 2*$r, 2*$r, 180, 90)
    $path.AddArc($rect.Right - 2*$r, $rect.Y, 2*$r, 2*$r, 270, 90)
    $path.AddArc($rect.Right - 2*$r, $rect.Bottom - 2*$r, 2*$r, 2*$r, 0, 90)
    $path.AddArc($rect.X, $rect.Bottom - 2*$r, 2*$r, 2*$r, 90, 90)
    $path.CloseFigure()
    $g.FillPath($bgBrush, $path)

    if ($size -ge 24) {
        $penW = [Math]::Max(1, [int]($size * 0.04))
        $pen = New-Object System.Drawing.Pen $teal, $penW
        $g.DrawPath($pen, $path)
        $pen.Dispose()
    }

    $cx = $size / 2.0; $cy = $size / 2.0

    # big teal 'C' glyph centered (the product mark; reads at 16px)
    $fontPx = [Math]::Max(8, [int]($size * 0.62))
    $font = New-Object System.Drawing.Font ('Segoe UI'), $fontPx, ([System.Drawing.FontStyle]::Bold), ([System.Drawing.GraphicsUnit]::Pixel)
    $fgBrush = New-Object System.Drawing.SolidBrush $teal
    $sf = New-Object System.Drawing.StringFormat
    $sf.Alignment = 'Center'; $sf.LineAlignment = 'Center'
    $textRect = New-Object System.Drawing.RectangleF 0, (-($size * 0.05)), $size, $size
    $g.DrawString('C', $font, $fgBrush, $textRect, $sf)

    # status dot bottom-right
    $dotR = [Math]::Max(2, [int]($size * 0.10))
    $dotBrush = New-Object System.Drawing.SolidBrush $green
    $dotRect = New-Object System.Drawing.Rectangle ($rect.Right - 3*$dotR), ($rect.Bottom - 3*$dotR), (2*$dotR), (2*$dotR)
    $g.FillEllipse($dotBrush, $dotRect)
    $dotBrush.Dispose()

    $font.Dispose(); $fgBrush.Dispose(); $bgBrush.Dispose(); $path.Dispose(); $g.Dispose()

    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $pngs[$size] = $ms.ToArray()
    $ms.Dispose()
    $bmp.Dispose()
}

$entries = @($pngs.Keys | Sort-Object)
$count = $entries.Count
$ico = New-Object System.IO.MemoryStream
$bw = New-Object System.IO.BinaryWriter $ico
$bw.Write([uint16]0); $bw.Write([uint16]1); $bw.Write([uint16]$count)
$offset = 6 + 16 * $count
foreach ($s in $entries) {
    $d = $pngs[$s]
    $bw.Write([byte]($(if ($s -ge 256) { 0 } else { $s })))
    $bw.Write([byte]($(if ($s -ge 256) { 0 } else { $s })))
    $bw.Write([byte]0); $bw.Write([byte]0)
    $bw.Write([uint16]1); $bw.Write([uint16]32)
    $bw.Write([uint32]$d.Length); $bw.Write([uint32]$offset)
    $offset += $d.Length
}
foreach ($s in $entries) { $bw.Write($pngs[$s]) }
$bw.Flush()
[System.IO.File]::WriteAllBytes($out, $ico.ToArray())
$bw.Dispose(); $ico.Dispose()
Write-Output ('icon written: ' + $out + ' (' + (Get-Item $out).Length + ' bytes, ' + $count + ' sizes)')
