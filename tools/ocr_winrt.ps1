# ocr_winrt.ps1 -- Windows built-in OCR (WinRT), zero install.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools/ocr_winrt.ps1 -Image "C:\a.png" -Out "C:\a.txt"
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools/ocr_winrt.ps1 -SelfTest -Out "C:\a.txt"
#
# Contract (keep this file ASCII-only, it is parsed by PowerShell 5.1):
#   - On success : writes UTF-8 text to -Out file, stdout = "OK"
#   - On failure : stderr = "OCR_ERROR: <ascii reason>", exit code 1
#   - Languages  : zh-Hans-CN (default) / en-US / zh-Hant
param(
    [string]$Image = "",
    [string]$Out = "",
    [string]$Lang = "zh-Hans-CN",
    [string]$SelfTestImage = "",
    [switch]$SelfTest,
    [int]$TimeoutSec = 60
)

$ErrorActionPreference = "Stop"

function Write-Err([string]$msg) {
    [Console]::Error.WriteLine("OCR_ERROR: $msg")
    exit 1
}

# ---------- self test image ----------
if ($SelfTest) {
    try {
        Add-Type -AssemblyName System.Drawing
        $bmp = New-Object System.Drawing.Bitmap 560, 180
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.Clear([System.Drawing.Color]::White)
        $f1 = New-Object System.Drawing.Font("Microsoft YaHei", 26)
        $f2 = New-Object System.Drawing.Font("Arial", 18)
        $g.DrawString("Contract Party: ACME Corp", $f1, [System.Drawing.Brushes]::Black, 20, 25)
        # Chinese text is built from code points so this script stays pure ASCII
        # (PowerShell 5.1 would garble a literal CJK string in a non-BOM file).
        $zh = -join ([char[]](0x5408, 0x540C, 0x7532, 0x65B9, 0xFF1A, 0x67D0, 0x67D0, 0x516C, 0x53F8))
        $g.DrawString($zh, $f1, [System.Drawing.Brushes]::Black, 20, 70)
        $g.DrawString("MAU 1 billion 2026-10-23", $f2, [System.Drawing.Brushes]::DarkBlue, 20, 130)
        $g.Dispose()
        # NOTE: do NOT use $out here - PowerShell variable names are case-insensitive,
        # so $out would overwrite the -Out parameter.
        if ($SelfTestImage) {
            $selfImage = $SelfTestImage
        } else {
            $selfImage = Join-Path $env:TEMP "ocr_selftest.png"
        }
        $bmp.Save($selfImage, [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
        $Image = $selfImage
    } catch {
        Write-Err "selftest-image-failed: $($_.Exception.Message)"
    }
}

if (-not $Image -or -not (Test-Path -LiteralPath $Image)) {
    Write-Err "image-not-found"
}
if (-not $Out) { Write-Err "missing-out-param" }

# ---------- WinRT async bridge ----------
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
} catch {
    Write-Err "winrt-assembly-unavailable"
}

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
if (-not $asTaskGeneric) { Write-Err "astask-bridge-not-found" }

function Await($op, $type) {
    $m = $asTaskGeneric.MakeGenericMethod($type)
    $t = $m.Invoke($null, @($op))
    if (-not $t.Wait($TimeoutSec * 1000)) { throw "winrt-timeout" }
    $t.Result
}

try {
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
} catch {
    Write-Err "winrt-ocr-types-unavailable"
}

try {
    # PowerShell 5.1 cannot marshal a WinRT Language object reliably, so prefer the
    # profile-based engine (follows the OS display language) and only fall back to a
    # language-specific engine when the profile one is unavailable.
    $engine = $null
    try {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    } catch { $engine = $null }
    if (-not $engine) {
        $langType = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
        $lang = $langType::new($Lang)
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    }
    if (-not $engine) { Write-Err "no-recognizer-available" }

    $full = (Resolve-Path -LiteralPath $Image).Path
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($full)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync(
        [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
        [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied)) ([Windows.Graphics.Imaging.SoftwareBitmap])

    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $text = $result.Text
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    if (Test-Path -LiteralPath $Out) { Remove-Item -LiteralPath $Out -Force -ErrorAction SilentlyContinue }
    $fs = [System.IO.File]::Open($Out, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
    try {
        $sw = New-Object System.IO.StreamWriter($fs, $utf8)
        $sw.Write($text)
        $sw.Flush()
        $sw.Dispose()
    } finally {
        if ($fs) { $fs.Dispose() }
    }
    [Console]::Out.Write("OK")
    exit 0
} catch {
    Write-Err "ocr-failed: $($_.Exception.Message)"
}
