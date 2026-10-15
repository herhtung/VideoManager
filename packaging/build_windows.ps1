param(
    [string]$Python = "python",
    [string]$Ffmpeg = "",
    [string]$Ffprobe = "",
    [string]$FfmpegDir = "",
    [string]$DistPath = "dist"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot
foreach ($relative in @("build", $DistPath)) {
    $candidate = Join-Path $projectRoot $relative
    $resolved = if (Test-Path -LiteralPath $candidate) { (Resolve-Path -LiteralPath $candidate).Path } else { [IO.Path]::GetFullPath($candidate) }
    if (-not $resolved.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Build output escapes the project workspace: $resolved"
    }
}
$ffmpegBin = $null
if ($FfmpegDir) {
    $ffmpegRoot = (Resolve-Path -LiteralPath $FfmpegDir -ErrorAction Stop).Path
    $ffmpegBin = if (Test-Path -LiteralPath (Join-Path $ffmpegRoot "bin\ffmpeg.exe")) {
        Join-Path $ffmpegRoot "bin"
    } else { $ffmpegRoot }
    $Ffmpeg = Join-Path $ffmpegBin "ffmpeg.exe"
    $Ffprobe = Join-Path $ffmpegBin "ffprobe.exe"
}
if (-not $Ffmpeg) { $Ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source }
if (-not $Ffprobe) { $Ffprobe = (Get-Command ffprobe -ErrorAction Stop).Source }
if (-not (Test-Path -LiteralPath $Ffmpeg -PathType Leaf)) { throw "FFmpeg executable not found" }
if (-not (Test-Path -LiteralPath $Ffprobe -PathType Leaf)) { throw "FFprobe executable not found" }
$sourcePath = Join-Path $projectRoot "src"
# The host toolchain may put Poppler's ICU DLL ahead of Windows' ICU on PATH.
# Qt uses the Windows ICU exports, so do not let PyInstaller collect that
# unrelated DLL into the application directory.
$env:PATH = (($env:PATH -split ';') | Where-Object { $_ -and ($_ -notmatch '[\\/]native[\\/]poppler[\\/]Library[\\/]bin[\\/]?$') }) -join ';'
if ($env:PYTHONPATH) { $env:PYTHONPATH = "$sourcePath;$env:PYTHONPATH" }
else { $env:PYTHONPATH = $sourcePath }
if (Test-Path -LiteralPath (Join-Path $projectRoot ".deps")) {
    $env:PYTHONPATH = "$(Join-Path $projectRoot '.deps');$env:PYTHONPATH"
    $env:PYTHONPATH = "$(Join-Path $projectRoot '.deps\win32');$(Join-Path $projectRoot '.deps\win32\lib');$(Join-Path $projectRoot '.deps\Pythonwin');$env:PYTHONPATH"
    $env:PATH = "$(Join-Path $projectRoot '.deps\pywin32_system32');$env:PATH"
}
$migration = Join-Path $projectRoot "src\videomanager\db\migrations\001_initial.sql"
$migration2 = Join-Path $projectRoot "src\videomanager\db\migrations\002_unique_categories.sql"
$migration3 = Join-Path $projectRoot "src\videomanager\db\migrations\003_video_groups.sql"
$migration4 = Join-Path $projectRoot "src\videomanager\db\migrations\004_tags_only.sql"
$migration5 = Join-Path $projectRoot "src\videomanager\db\migrations\005_required_tag_groups.sql"
$brandPng = Join-Path $projectRoot "assets\videomanager.png"
$brandIco = Join-Path $projectRoot "assets\videomanager.ico"
if (-not (Test-Path -LiteralPath $brandPng -PathType Leaf) -or -not (Test-Path -LiteralPath $brandIco -PathType Leaf)) {
    & $Python scripts/make_brand_assets.py
    if ($LASTEXITCODE -ne 0) { throw "Brand asset generation failed" }
}
$arguments = @(
    "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed", "--name", "VideoManager",
    "--workpath", "build", "--distpath", $DistPath, "--specpath", "build",
    "--icon", $brandIco,
    "--add-data", "$brandPng;assets",
    "--add-data", "$migration;videomanager/db/migrations",
    "--add-data", "$migration2;videomanager/db/migrations",
    "--add-data", "$migration3;videomanager/db/migrations",
    "--add-data", "$migration4;videomanager/db/migrations",
    "--add-data", "$migration5;videomanager/db/migrations",
    "--add-data", "$(Join-Path $projectRoot 'packaging\THIRD_PARTY.md');third_party",
    "--add-data", "$(Join-Path $projectRoot 'packaging\third_party_licenses');third_party/licenses",
    "--add-binary", "$Ffmpeg;bin", "--add-binary", "$Ffprobe;bin"
)
if ($ffmpegBin) {
    foreach ($dll in Get-ChildItem -LiteralPath $ffmpegBin -Filter "*.dll" -File) {
        $arguments += @("--add-binary", "$($dll.FullName);bin")
    }
    $ffmpegLicense = Join-Path (Split-Path $ffmpegBin -Parent) "LICENSE.txt"
    if (Test-Path -LiteralPath $ffmpegLicense -PathType Leaf) {
        $arguments += @("--add-data", "$ffmpegLicense;third_party")
    }
}
if (Test-Path -LiteralPath (Join-Path $projectRoot "LICENSE.md")) {
    $arguments += @("--add-data", "$(Join-Path $projectRoot 'LICENSE.md');.")
}
$arguments += @(
    "--hidden-import", "send2trash.win.modern",
    "--hidden-import", "pythoncom", "--hidden-import", "pywintypes",
    "scripts/launch.py"
)
& $Python @arguments
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
Write-Host "Built: $projectRoot\$DistPath\VideoManager\VideoManager.exe"
