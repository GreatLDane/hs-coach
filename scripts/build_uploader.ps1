$ErrorActionPreference = "Stop"

$PY = "C:\Users\felza\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

New-Item -ItemType Directory -Force -Path "build" | Out-Null

$env:PYTHONPATH = "src"
& $PY -c "from hs_coach.desktop import create_tray_icon_image; create_tray_icon_image(64).save('build/hs_coach.ico', format='ICO', sizes=[(64, 64), (32, 32), (16, 16)])"

& $PY -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --paths src `
    --icon build/hs_coach.ico `
    --name HS-Coach-Uploader `
    src/hs_coach/tray_entry.py

Write-Host "Built: $Root\dist\HS-Coach-Uploader.exe"
