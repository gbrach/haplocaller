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
    # Pinned uv release (static musl build, so it runs on old clusters); the download is checked before use.
    UV_VERSION=0.13.7
    fetch_uv() {
        case "$(uname -m)" in
            x86_64|amd64) arch=x86_64; sum=287491da03f2a785c70fa75b13e1f849918c2f2b5415fe84876b8214eeb792f9 ;;
            aarch64|arm64) arch=aarch64; sum=2d3ec076a0c4c0e8250bb6418c44080828b544f1a3be0b6e46adf9fb6923de45 ;;
            *) return 1 ;;
        esac
        mkdir -p .tools
        tmp=$(mktemp -d .tools/dl.XXXXXX)
        file="$tmp/uv.tar.gz"
        if curl -fsSL -o "$file" "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${arch}-unknown-linux-musl.tar.gz" \
            && printf '%s  %s\n' "$sum" "$file" | { sha256sum -c --status 2>/dev/null || shasum -a 256 -c --status 2>/dev/null; } \
            && tar -xz -C .tools --strip-components=1 -f "$file"; then
            rm -rf "$tmp"
        else
            rm -rf "$tmp"; return 1
        fi
    }
    # Keep uv's cache and any Python it downloads inside this folder, not in the quota-limited home directory.
    export UV_CACHE_DIR="$PWD/.tools/cache" UV_PYTHON_INSTALL_DIR="$PWD/.tools/python"
    # uv builds the environment without ensurepip (missing on Debian) and can fetch Python itself.
    if [ -x .tools/uv ] || fetch_uv; then
        say "Creating the Python environment with uv (about a minute)..."
        # Python 3.11 before 3.11.4 (Debian 12) cannot safely unpack the Dorado download, so use 3.12 or newer.
        if [ -n "$PY" ] && "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
            .tools/uv venv -q --python "$PY" .venv
        else
            .tools/uv venv -q --python 3.12 .venv
        fi
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
