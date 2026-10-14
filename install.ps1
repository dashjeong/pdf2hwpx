param(
    [string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'ScanEdit'),
    [string]$ShortcutDirectory = [Environment]::GetFolderPath('Desktop')
)
$ErrorActionPreference = 'Stop'
$packageRoot = $PSScriptRoot
$scanEditInstall = [IO.Path]::GetFullPath($InstallDirectory)
if (-not (Test-Path -LiteralPath (Join-Path $packageRoot 'ScanEdit.exe'))) { throw '압축을 모두 풀고 설치해 주세요.' }
New-Item -ItemType Directory -Path $scanEditInstall -Force | Out-Null
Get-ChildItem -LiteralPath $packageRoot | Where-Object { $_.Name -ne 'data' } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $scanEditInstall -Recurse -Force
}
New-Item -ItemType Directory -Path $ShortcutDirectory -Force | Out-Null
$shortcutPath = Join-Path $ShortcutDirectory '스캔노트.lnk'
$shortcutObject = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
$shortcutObject.TargetPath = Join-Path $scanEditInstall 'ScanEdit.exe'
$shortcutObject.WorkingDirectory = $scanEditInstall
$shortcutObject.Save()
Write-Host '설치 완료. 바탕화면의 스캔노트를 실행해 주세요.'
