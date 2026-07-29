# Startup script: activate venv and start the server
$ErrorActionPreference = "Stop"

# Switch to the script directory (works regardless of where it is launched from)
Set-Location -Path $PSScriptRoot

# Self-heal PowerShell execution policy for the current process (avoids policy block)
$curPolicy = Get-ExecutionPolicy -Scope Process
if ($curPolicy -notin @("Bypass", "Unrestricted", "RemoteSigned")) {
    try {
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
        Write-Host "Execution policy set to Bypass for this process." -ForegroundColor DarkGray
    } catch {
        Write-Host "Unable to set execution policy. If the script is blocked, run as admin:" -ForegroundColor Yellow
        Write-Host "  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned" -ForegroundColor Yellow
    }
}

# Check if the virtual environment exists
$venvActivate = Join-Path $PSScriptRoot ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $venvActivate)) {
    Write-Host "Virtual environment not found: .venv\Scripts\Activate.ps1" -ForegroundColor Red
    Write-Host "Please run first: python -m venv .venv  ;  .\.venv\Scripts\Activate.ps1  ;  pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}

# Activate the virtual environment
Write-Host "Activating virtual environment..." -ForegroundColor Green
. $venvActivate

# Start the server
Write-Host "Starting server (python run.py)..." -ForegroundColor Green
python run.py
