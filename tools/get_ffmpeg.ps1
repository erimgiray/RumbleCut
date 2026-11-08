# Downloads a static Windows FFmpeg build (includes NVIDIA NVENC) into vendor\ffmpeg
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$dest = Join-Path $root "vendor\ffmpeg"
New-Item -ItemType Directory -Force -Path $dest | Out-Null

$url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
$zip = Join-Path $env:TEMP "rumblecut-ffmpeg.zip"
$tmp = Join-Path $env:TEMP "rumblecut-ffmpeg"

Write-Host "Downloading FFmpeg from $url ..."
Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Expand-Archive -Path $zip -DestinationPath $tmp -Force

$ff = Get-ChildItem -Path $tmp -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
if (-not $ff) { throw "ffmpeg.exe not found in the downloaded archive" }
Copy-Item (Join-Path $ff.DirectoryName "ffmpeg.exe")  $dest -Force
Copy-Item (Join-Path $ff.DirectoryName "ffprobe.exe") $dest -Force
Remove-Item -Recurse -Force $tmp, $zip
Write-Host "FFmpeg ready in $dest"
