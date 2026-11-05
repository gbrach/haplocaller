#!/usr/bin/env bash
# One-step installer: ./install.sh   (safe to run again; no administrator rights needed)
set -euo pipefail
cd "$(dirname "$0")"
say() { printf '\033[36m==> %s\033[0m\n' "$*"; }

if [ -x .venv/bin/python ] && .venv/bin/python -c 'import pod5, pysam' 2>/dev/null; then
    say "Python environment already present."
else
    PY=""
    for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys, venv; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
            PY=$(command -v "$candidate"); break
        fi
    done
    rm -rf .venv
    fetch_uv() {
        case "$(uname -m)" in x86_64|amd64) arch=x86_64 ;; aarch64|arm64) arch=aarch64 ;; *) return 1 ;; esac
        mkdir -p .tools
        curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-${arch}-unknown-linux-gnu.tar.gz" | tar -xz -C .tools --strip-components=1
    }
    # uv builds the environment without ensurepip (missing on Debian) and can fetch Python itself.
    if [ -x .tools/uv ] || fetch_uv; then
        say "Creating the Python environment with uv (about a minute)..."
        if [ -n "$PY" ]; then .tools/uv venv -q --python "$PY" .venv; else .tools/uv venv -q --python 3.12 .venv; fi
        .tools/uv pip install -q --python .venv/bin/python pod5 pysam
    elif [ -n "$PY" ]; then
        say "Could not download uv; using $PY with pip instead..."
        "$PY" -m venv .venv
        .venv/bin/python -m pip install -q pod5 pysam
    else
        echo "No usable Python (3.9 or newer) and uv could not be downloaded from GitHub." >&2; exit 1
    fi
fi
exec .venv/bin/python install.py "$@"
