# Installer for the Expense Program (Windows).
#
# Checks for every prerequisite this app needs and installs whatever's
# missing, then sets up and (optionally) verifies the app itself.
#
# Usage (run from an ordinary PowerShell prompt, or just double-click
# install.bat which launches this correctly):
#   .\install.ps1                interactive: asks Docker vs native Python
#   .\install.ps1 -Docker        force the Docker path
#   .\install.ps1 -Native        force the native Python (venv) path
#   .\install.ps1 -SkipTests     skip the post-install self-check (pytest)
#   .\install.ps1 -Yes           don't prompt before installing missing
#                                 prerequisites (for unattended use)

param(
    [switch]$Docker,
    [switch]$Native,
    [switch]$SkipTests,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Info($msg)  { Write-Host "==> $msg" -ForegroundColor Green }
function Warn($msg)  { Write-Host "==> $msg" -ForegroundColor Yellow }
function ErrMsg($msg) { Write-Host "==> ERROR: $msg" -ForegroundColor Red }
function Step($msg)  { Write-Host ""; Write-Host $msg -ForegroundColor White }

function Confirm($question) {
    if ($Yes) { return $true }
    $reply = Read-Host "$question [Y/n]"
    return -not ($reply -match '^[nN]')
}

function New-SecretKey {
    # Deliberately not shelling out to Python here, even though it's
    # available by the time the native path calls this - this function is
    # also called from the Docker path, which never resolves a Python
    # binary at all (the whole point of choosing Docker is not needing one
    # on the host). .NET's crypto RNG is built into PowerShell itself, so
    # this works identically regardless of which path got here - this is
    # exactly the bug that broke the Docker path before this fix.
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    return ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
}

function Split-Command($commandString) {
    # "python3" -> exe="python3", args=@()
    # "py -3"   -> exe="py", args=@("-3")
    # $parts[1..($parts.Length-1)] looks like it should give "everything but
    # the first element", but for a 1-element array that's a descending
    # range (1..0) which PowerShell doesn't treat as empty - it silently
    # re-includes element 0, doubling the executable up as its own first
    # argument. Select-Object -Skip 1 doesn't have that edge case.
    $parts = $commandString.Split(" ")
    return [PSCustomObject]@{
        Exe  = $parts[0]
        Args = @($parts | Select-Object -Skip 1)
    }
}

# ---------------------------------------------------------------------------
# Sanity check: run from the project folder
# ---------------------------------------------------------------------------
if (-not (Test-Path "requirements.txt") -or -not (Test-Path "app\main.py")) {
    ErrMsg "This doesn't look like the project folder (requirements.txt / app\main.py not found)."
    ErrMsg "Run this script from inside the extracted project folder."
    exit 1
}

# ---------------------------------------------------------------------------
# Prerequisite: winget (used to install Docker/Python automatically if
# they're missing - if winget isn't available, the script falls back to
# printing manual install instructions instead of failing silently)
# ---------------------------------------------------------------------------
function Has-Winget { return [bool](Get-Command winget -ErrorAction SilentlyContinue) }

# ---------------------------------------------------------------------------
# Prerequisite: Docker Desktop + Compose
# ---------------------------------------------------------------------------
function Has-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }
    docker compose version *> $null
    return $LASTEXITCODE -eq 0
}

function Install-Docker {
    Step "Docker Desktop is not installed."
    if (-not (Confirm "Install Docker Desktop now?")) {
        ErrMsg "Docker is required for the Docker path. Re-run with -Native for a plain-Python install instead."
        exit 1
    }
    if (Has-Winget) {
        Info "Installing Docker Desktop via winget..."
        winget install --id Docker.DockerDesktop -e --accept-source-agreements --accept-package-agreements
        Warn "Docker Desktop needs a restart and a manual first launch (it may prompt for WSL2 setup)."
        Warn "Start Docker Desktop from the Start menu, wait for it to say 'running', then re-run this script."
        exit 0
    } else {
        ErrMsg "winget isn't available on this system."
        ErrMsg "Download Docker Desktop manually: https://www.docker.com/products/docker-desktop/"
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Prerequisite: Python 3.10+
# ---------------------------------------------------------------------------
$script:PythonBin = $null

function Find-Python {
    foreach ($candidate in @("py -3", "python", "python3")) {
        $cmd = Split-Command $candidate
        if (-not (Get-Command $cmd.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $verOutput = & $cmd.Exe @($cmd.Args + "--version") 2>&1
            if ($verOutput -match "Python (\d+)\.(\d+)") {
                $verMajor = [int]$Matches[1]
                $verMinor = [int]$Matches[2]
                if ($verMajor -eq 3 -and $verMinor -ge 10) {
                    $script:PythonBin = $candidate
                    return $true
                }
            }
        } catch { continue }
    }
    return $false
}

function Install-Python {
    Step "Python 3.10+ was not found."
    if (-not (Confirm "Install Python now?")) {
        ErrMsg "Python 3.10+ is required. Install it manually and re-run this script."
        exit 1
    }
    if (Has-Winget) {
        Info "Installing Python via winget..."
        winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
        Warn "Close and reopen PowerShell so the updated PATH takes effect, then re-run this script."
        exit 0
    } else {
        ErrMsg "winget isn't available on this system."
        ErrMsg "Download Python manually from https://www.python.org/downloads/"
        ErrMsg "IMPORTANT: on the first install screen, check 'Add Python to PATH' before installing."
        exit 1
    }
}

# ---------------------------------------------------------------------------
# .env setup
# ---------------------------------------------------------------------------
function Setup-EnvFile {
    if (Test-Path ".env") {
        Info ".env already exists - leaving it as-is."
        return
    }
    Info "Creating .env from .env.example with a freshly generated SECRET_KEY..."
    Copy-Item ".env.example" ".env"
    $secret = New-SecretKey
    (Get-Content ".env") -replace '^SECRET_KEY=.*', "SECRET_KEY=$secret" | Set-Content ".env"
    Warn "Default login will be admin / admin123 - change it in Users immediately after first login."
}

# ---------------------------------------------------------------------------
# Docker path
# ---------------------------------------------------------------------------
function Run-DockerPath {
    Step "Setting up with Docker"
    if (-not (Has-Docker)) {
        Install-Docker
    } else {
        Info "Docker and Docker Compose already installed."
    }
    Setup-EnvFile
    Info "Building and starting the app (docker compose up -d --build)..."
    docker compose up -d --build
    Step "Done."
    Info "Open http://localhost:8000 in a browser."
    Info "Default login: admin / admin123 - change the password immediately."
}

# ---------------------------------------------------------------------------
# Native Python path
# ---------------------------------------------------------------------------
function Run-NativePath {
    Step "Setting up with a native Python virtual environment"
    if (-not (Find-Python)) {
        Install-Python
        if (-not (Find-Python)) {
            ErrMsg "Still couldn't find a working Python 3.10+ after installing. Please check manually."
            exit 1
        }
    }
    $cmd = Split-Command $script:PythonBin
    $verString = & $cmd.Exe @($cmd.Args + "--version")
    Info "Using '$script:PythonBin' ($verString)."

    if (-not (Test-Path "venv")) {
        Info "Creating virtual environment in .\venv ..."
        & $cmd.Exe @($cmd.Args + "-m" + "venv" + "venv")
    } else {
        Info "./venv already exists - reusing it."
    }

    Info "Installing dependencies..."
    & ".\venv\Scripts\pip.exe" install --quiet --upgrade pip
    & ".\venv\Scripts\pip.exe" install --quiet -r requirements.txt

    Setup-EnvFile

    Info "Initializing the database..."
    & ".\venv\Scripts\python.exe" init_db.py

    if (-not $SkipTests) {
        Step "Verifying the install (running the test suite)"
        & ".\venv\Scripts\pip.exe" install --quiet -r requirements-dev.txt
        $tempData = Join-Path $env:TEMP ([System.Guid]::NewGuid().ToString())
        New-Item -ItemType Directory -Path $tempData | Out-Null
        $env:PETTY_CASH_DATA_DIR = $tempData
        & ".\venv\Scripts\pytest.exe" -q
        $testsOk = $LASTEXITCODE -eq 0
        Remove-Item Env:\PETTY_CASH_DATA_DIR
        if ($testsOk) {
            Info "All checks passed."
        } else {
            Warn "Some tests failed - the app may still work, but something isn't right. See output above."
        }
    }

    Step "Done."
    Info "Start the app with:"
    Write-Host "    venv\Scripts\activate"
    Write-Host "    uvicorn app.main:app --host 0.0.0.0 --port 8000"
    Info "Then open http://localhost:8000 in a browser."
    Info "Default login: admin / admin123 - change the password immediately."
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
$mode = ""
if ($Docker) { $mode = "docker" }
elseif ($Native) { $mode = "native" }

if ($mode -eq "") {
    if (Has-Docker) {
        Info "Docker is already installed."
        if (Confirm "Use Docker for setup? (choose 'n' for a plain-Python install instead)") {
            $mode = "docker"
        } else {
            $mode = "native"
        }
    } else {
        if (Confirm "Docker isn't installed. Set it up and use it? (choose 'n' for a plain-Python install instead)") {
            $mode = "docker"
        } else {
            $mode = "native"
        }
    }
}

if ($mode -eq "docker") { Run-DockerPath } else { Run-NativePath }
