# Generates src/cmo-icon.ico - dark rounded square, ">" prompt, mauve accent, green dot.
# Same generator design as the RDC icon, different palette.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$out = Join-Path $PSScriptRoot 'src\cmo-icon.ico'

$card   = [System.Drawing.Color]::FromArgb(255, 40, 40, 56)     # #282838
$accent = [System.Drawing.Color]::FromArgb(255, 203, 166, 247)  # #CBA6F7 (mauve)
$green  = [System.Drawing.Color]::FromArgb(255, 166, 227, 161)  # #A6E3A1

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
        $pen = New-Object System.Drawing.Pen $accent, $penW
        $g.DrawPath($pen, $path)
        $pen.Dispose()
    }

    $fontPx = [Math]::Max(6, [int]($size * 0.62))
    $font = New-Object System.Drawing.Font ('Segoe UI'), $fontPx, ([System.Drawing.FontStyle]::Bold), ([System.Drawing.GraphicsUnit]::Pixel)
    $fgBrush = New-Object System.Drawing.SolidBrush $accent
    $sf = New-Object System.Drawing.StringFormat
    $sf.Alignment = 'Center'; $sf.LineAlignment = 'Center'
    $textRect = New-Object System.Drawing.RectangleF 0, (-($size * 0.04)), $size, $size
    $g.DrawString('>', $font, $fgBrush, $textRect, $sf)

    if ($size -ge 32) {
        $dotR = [Math]::Max(2, [int]($size * 0.09))
        $dotBrush = New-Object System.Drawing.SolidBrush $green
        $dotRect = New-Object System.Drawing.Rectangle ($rect.Right - 3*$dotR), ($rect.Bottom - 3*$dotR), (2*$dotR), (2*$dotR)
        $g.FillEllipse($dotBrush, $dotRect)
        $dotBrush.Dispose()
    }

    if ($size -ge 48) {
        $stripBrush = New-Object System.Drawing.SolidBrush $green
        $stripH = [Math]::Max(2, [int]($size * 0.06))
        $g.FillRectangle($stripBrush, 0, ($rect.Bottom - 2*$stripH), $size, $stripH)
        $stripBrush.Dispose()
    }

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
