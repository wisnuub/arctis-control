# Builds dist\ArctisControl\ArctisControl.exe (PyInstaller, one-folder, no console).
# Used locally and by .github/workflows/release.yml.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install --upgrade pip | Out-Null
python -m pip install -r requirements.txt pyinstaller

python -m PyInstaller --noconfirm --clean --windowed `
    --name ArctisControl `
    --icon arctis_control.ico `
    --add-data "arctis_control.ico;." `
    --add-data "arctis_control.png;." `
    --collect-submodules comtypes `
    arctis_battery.py

if (-not (Test-Path dist\ArctisControl\ArctisControl.exe)) { throw "build failed" }
Write-Host "Built dist\ArctisControl\ArctisControl.exe"
