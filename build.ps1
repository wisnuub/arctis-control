# Builds dist\ArctisControl\ArctisControl.exe (PyInstaller, one-folder, no console)
# from ArctisControl.spec. Used locally and by .github/workflows/release.yml.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# DeepFilterNet noise-cancelling model (official release, ~48 MB, not kept in git)
$dll = "deep_filter_ladspa.dll"
$dllUrl = "https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/deep_filter_ladspa-0.5.6-x86_64-pc-windows-msvc.dll"
$dllSha = "F97E34275EB884B6076A96D261683D2A43C6B2D97DDF9E346BC06F7C8057EB04"
if (-not (Test-Path $dll)) {
    Write-Host "Downloading DeepFilterNet LADSPA plugin..."
    Invoke-WebRequest -Uri $dllUrl -OutFile $dll
}
if ((Get-FileHash $dll -Algorithm SHA256).Hash -ne $dllSha) {
    Remove-Item $dll
    throw "$dll checksum mismatch"
}

python -m pip install --upgrade pip | Out-Null
python -m pip install -r requirements.txt pyinstaller

python -m PyInstaller --noconfirm --clean ArctisControl.spec

if (-not (Test-Path dist\ArctisControl\ArctisControl.exe)) { throw "build failed" }
Write-Host "Built dist\ArctisControl\ArctisControl.exe"
