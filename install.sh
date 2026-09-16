#!/usr/bin/env bash
# Installer for the Expense Program.
#
# Checks for every prerequisite this app needs and installs whatever's
# missing, then sets up and (optionally) verifies the app itself.
#
# Usage:
#   ./install.sh              interactive: asks Docker vs native Python
#   ./install.sh --docker     force the Docker path
#   ./install.sh --native     force the native Python (venv) path
#   ./install.sh --skip-tests skip the post-install self-check (pytest)
#   ./install.sh --yes        don't prompt before installing missing
#                             system packages (for unattended/CI use)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
  C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_BOLD='\033[1m'; C_RESET='\033[0m'
else
  C_GREEN=''; C_YELLOW=''; C_RED=''; C_BOLD=''; C_RESET=''
fi
info()  { printf "%b\n" "${C_GREEN}==>${C_RESET} $*"; }
warn()  { printf "%b\n" "${C_YELLOW}==>${C_RESET} $*"; }
err()   { printf "%b\n" "${C_RED}==> ERROR:${C_RESET} $*" >&2; }
step()  { printf "\n%b\n" "${C_BOLD}$*${C_RESET}"; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
MODE=""            # "docker" | "native" | ""(ask)
SKIP_TESTS=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --docker) MODE="docker" ;;
    --native) MODE="native" ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --help|-h)
      sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) err "Unknown option: $arg (see --help)"; exit 1 ;;
  esac
done

confirm() {
  # confirm "question" -> 0 (yes) or 1 (no). Auto-yes if --yes was passed or
  # there's no interactive terminal (e.g. piped into bash).
  if [ "$ASSUME_YES" = 1 ] || [ ! -t 0 ]; then
    return 0
  fi
  read -r -p "$1 [Y/n] " reply
  case "$reply" in
    [nN]*) return 1 ;;
    *) return 0 ;;
  esac
}

# ---------------------------------------------------------------------------
# OS / package-manager detection
# ---------------------------------------------------------------------------
OS="unknown"
PKG_MANAGER="unknown"
case "$(uname -s)" in
  Linux*)
    OS="linux"
    if command -v apt-get >/dev/null 2>&1; then PKG_MANAGER="apt"
    elif command -v dnf >/dev/null 2>&1; then PKG_MANAGER="dnf"
    elif command -v pacman >/dev/null 2>&1; then PKG_MANAGER="pacman"
    fi
    ;;
  Darwin*) OS="macos" ;;
esac
info "Detected platform: $OS${PKG_MANAGER:+ ($PKG_MANAGER)}"

# ---------------------------------------------------------------------------
# Prerequisite: Docker + Compose (only checked/installed if the Docker path
# is actually chosen below)
# ---------------------------------------------------------------------------
has_docker() {
  command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1
}

install_docker() {
  step "Docker is not installed."
  if ! confirm "Install Docker now?"; then
    err "Docker is required for the Docker path. Re-run with --native for a plain-Python install instead."
    exit 1
  fi
  case "$OS" in
    linux)
      info "Installing Docker via the official convenience script (get.docker.com)..."
      curl -fsSL https://get.docker.com | sh
      if ! command -v docker >/dev/null 2>&1; then
        err "Docker install script finished but 'docker' still isn't on PATH. Install manually: https://docs.docker.com/engine/install/"
        exit 1
      fi
      if ! groups "$USER" | grep -q docker; then
        warn "Adding $USER to the 'docker' group so you don't need sudo for docker commands."
        sudo usermod -aG docker "$USER" || true
        warn "You'll need to log out and back in (or run 'newgrp docker') for that to take effect."
      fi
      ;;
    macos)
      if command -v brew >/dev/null 2>&1; then
        info "Installing Docker Desktop via Homebrew..."
        brew install --cask docker
        warn "Open the Docker Desktop app once from Applications to finish setup, then re-run this script."
        exit 0
      else
        err "Homebrew not found. Install Docker Desktop manually: https://www.docker.com/products/docker-desktop/"
        exit 1
      fi
      ;;
    *)
      err "Don't know how to install Docker on this platform. Install manually: https://docs.docker.com/get-docker/"
      exit 1
      ;;
  esac
}

# ---------------------------------------------------------------------------
# Prerequisite: Python 3.10+ and the venv module
# ---------------------------------------------------------------------------
PYTHON_BIN=""

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      local ver
      ver="$("$candidate" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo "0.0")"
      local major="${ver%%.*}" minor="${ver##*.}"
      if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then
        PYTHON_BIN="$candidate"
        return 0
      fi
    fi
  done
  return 1
}

install_python() {
  step "Python 3.10+ was not found."
  if ! confirm "Install Python 3 now?"; then
    err "Python 3.10+ is required. Install it manually and re-run this script."
    exit 1
  fi
  case "$PKG_MANAGER" in
    apt)
      info "Installing python3, python3-venv, python3-pip via apt..."
      sudo apt-get update
      sudo apt-get install -y python3 python3-venv python3-pip
      ;;
    dnf)
      info "Installing python3, python3-pip via dnf..."
      sudo dnf install -y python3 python3-pip
      ;;
    pacman)
      info "Installing python via pacman..."
      sudo pacman -Sy --noconfirm python python-pip
      ;;
    *)
      if [ "$OS" = "macos" ]; then
        if command -v brew >/dev/null 2>&1; then
          info "Installing python@3.12 via Homebrew..."
          brew install python@3.12
        else
          err "Homebrew not found. Install it from https://brew.sh, then re-run this script,"
          err "or install Python manually from https://www.python.org/downloads/"
          exit 1
        fi
      else
        err "Don't know how to install Python on this platform automatically."
        err "Install Python 3.10+ manually from https://www.python.org/downloads/ and re-run."
        exit 1
      fi
      ;;
  esac
}

ensure_venv_module() {
  # On Debian/Ubuntu, python3 can exist without the venv module (it's a
  # separate apt package) - this is a common, confusing failure mode, so
  # it's checked and fixed independently of "does python3 exist at all".
  if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
    step "Python's venv module isn't available."
    if [ "$PKG_MANAGER" = "apt" ]; then
      if confirm "Install python3-venv via apt?"; then
        sudo apt-get update
        sudo apt-get install -y python3-venv
      else
        err "The venv module is required. Install python3-venv and re-run."
        exit 1
      fi
    else
      err "Re-install Python with the venv/pip components included, then re-run."
      exit 1
    fi
  fi
}

# ---------------------------------------------------------------------------
# .env setup - copies the example file and generates a real SECRET_KEY
# rather than shipping with the insecure placeholder
# ---------------------------------------------------------------------------
setup_env_file() {
  if [ -f ".env" ]; then
    info ".env already exists - leaving it as-is."
    return
  fi
  info "Creating .env from .env.example with a freshly generated SECRET_KEY..."
  cp .env.example .env
  # Deliberately not using Python here (e.g. "$PYTHON_BIN" -c '...') even
  # though it's available in the native path - this function is also
  # called from the Docker path, which never resolves a Python binary at
  # all (the whole point of choosing Docker is not needing one on the
  # host). /dev/urandom + od are present on every Linux/Mac system by
  # default, so this works the same regardless of which path got here.
  local secret
  secret="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  # portable in-place sed (GNU and BSD/macOS sed differ on -i syntax)
  if sed --version >/dev/null 2>&1; then
    sed -i "s/^SECRET_KEY=.*/SECRET_KEY=${secret}/" .env
  else
    sed -i '' "s/^SECRET_KEY=.*/SECRET_KEY=${secret}/" .env
  fi
  warn "Default login will be admin / admin123 - change it in Users immediately after first login."
}

# ---------------------------------------------------------------------------
# Docker path
# ---------------------------------------------------------------------------
run_docker_path() {
  step "Setting up with Docker"
  if ! has_docker; then
    install_docker
  else
    info "Docker and Docker Compose already installed."
  fi
  setup_env_file
  info "Building and starting the app (docker compose up -d --build)..."
  docker compose up -d --build
  step "Done."
  info "Open http://localhost:8000 in a browser."
  info "Default login: admin / admin123 - change the password immediately."
}

# ---------------------------------------------------------------------------
# Native Python path
# ---------------------------------------------------------------------------
run_native_path() {
  step "Setting up with a native Python virtual environment"
  if ! find_python; then
    install_python
    if ! find_python; then
      err "Still couldn't find a working Python 3.10+ after installing. Please check manually."
      exit 1
    fi
  fi
  info "Using $PYTHON_BIN ($($PYTHON_BIN --version))."
  ensure_venv_module

  if [ ! -d venv ]; then
    info "Creating virtual environment in ./venv ..."
    "$PYTHON_BIN" -m venv venv
  else
    info "./venv already exists - reusing it."
  fi

  info "Installing dependencies..."
  ./venv/bin/pip install --quiet --upgrade pip
  ./venv/bin/pip install --quiet -r requirements.txt

  setup_env_file

  info "Initializing the database..."
  ./venv/bin/python init_db.py

  if [ "$SKIP_TESTS" != 1 ]; then
    step "Verifying the install (running the test suite)"
    ./venv/bin/pip install --quiet -r requirements-dev.txt
    if PETTY_CASH_DATA_DIR="$(mktemp -d)" ./venv/bin/pytest -q; then
      info "All checks passed."
    else
      warn "Some tests failed - the app may still work, but something isn't right. See output above."
    fi
  fi

  step "Done."
  info "Start the app with:"
  echo "    source venv/bin/activate"
  echo "    uvicorn app.main:app --host 0.0.0.0 --port 8000"
  info "Then open http://localhost:8000 in a browser."
  info "Default login: admin / admin123 - change the password immediately."
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if [ ! -f "requirements.txt" ] || [ ! -f "app/main.py" ]; then
  err "This doesn't look like the project folder (requirements.txt / app/main.py not found)."
  err "Run this script from inside the extracted project folder."
  exit 1
fi

if [ -z "$MODE" ]; then
  if has_docker; then
    info "Docker is already installed."
    if confirm "Use Docker for setup? (choose 'n' for a plain-Python install instead)"; then
      MODE="docker"
    else
      MODE="native"
    fi
  else
    if [ -t 0 ] && confirm "Docker isn't installed. Set it up and use it? (choose 'n' for a plain-Python install instead)"; then
      MODE="docker"
    else
      MODE="native"
    fi
  fi
fi

case "$MODE" in
  docker) run_docker_path ;;
  native) run_native_path ;;
esac
