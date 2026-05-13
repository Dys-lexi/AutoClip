# One-shot setup: create venv, install deps, copy vendored DLLs into Scripts/.
# Run from this folder:  .\setup.ps1

$ErrorActionPreference = "Stop"

python -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt

if (Test-Path .\vendor) {
    Copy-Item .\vendor\* .\venv\Scripts\ -Force
    Write-Host "Copied vendored DLLs into venv\Scripts\"
}

Write-Host "Done. Activate with:  .\venv\Scripts\Activate.ps1"
