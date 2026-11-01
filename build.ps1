# Builds dist\ArctisControl\ArctisControl.exe (PyInstaller, one-folder, no console)
# from ArctisControl.spec. Used locally and by .github/workflows/release.yml.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install --upgrade pip | Out-Null
python -m pip install -r requirements.txt pyinstaller

python -m PyInstaller --noconfirm --clean ArctisControl.spec

if (-not (Test-Path dist\ArctisControl\ArctisControl.exe)) { throw "build failed" }
Write-Host "Built dist\ArctisControl\ArctisControl.exe"
