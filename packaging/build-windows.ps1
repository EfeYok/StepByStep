# sbs.exe'yi üretir (Windows'ta, proje kökünden çalıştırın):
#   powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
# Çıktı: dist\sbs.exe
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

python -m pip install --upgrade pip
python -m pip install . "pyinstaller>=6.10"

# Textual widget'ları tembel yüklendiği için alt modüller ve CSS/veri dosyaları açıkça eklenir.
python -m PyInstaller `
    --noconfirm --clean `
    --onefile --console `
    --name sbs `
    --collect-submodules sbs `
    --collect-submodules textual `
    --collect-data textual `
    --collect-submodules rich `
    packaging\sbs_entry.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller başarısız ($LASTEXITCODE)" }

Write-Host "Hazır: dist\sbs.exe" -ForegroundColor Green
