#!/bin/sh
# DeepSeek Code — installer
# curl -fsSL https://raw.githubusercontent.com/tr1xx-tech/deepseek-code/main/install.sh | bash

RAW="https://raw.githubusercontent.com/tr1xx-tech/deepseek-code/main"

if [ -t 1 ]; then
    R="\033[0m" BOLD="\033[1m" DIM="\033[38;5;245m"
    GREEN="\033[38;5;35m" CYAN="\033[38;5;33m" RED="\033[38;5;196m"
else
    R="" BOLD="" DIM="" GREEN="" CYAN="" RED=""
fi

SPIN_PID=""
die() {
    [ -n "$SPIN_PID" ] && kill "$SPIN_PID" 2>/dev/null && printf "\r\033[K"
    printf "  ${RED}✗  %s${R}\n\n" "$1" >&2; exit 1
}

IS_TERMUX=0
{ [ -n "$TERMUX_VERSION" ] || [ -d "/data/data/com.termux" ]; } && IS_TERMUX=1

if [ "$IS_TERMUX" = "1" ]; then
    SHARE_DIR="$PREFIX/share/deepseek"
    BIN_DIR="$PREFIX/bin"
else
    SHARE_DIR="$HOME/.local/share/deepseek"
    BIN_DIR="$HOME/.local/bin"
fi

APP="$SHARE_DIR/deepseek.py"

printf "\n"

# ── spinner ───────────────────────────────────────────────────────────────────
( while true; do
    for f in '⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏'; do
        printf "\r  ${CYAN}%s${R}  ${DIM}installing...${R}" "$f"
        sleep 0.08
    done
done ) &
SPIN_PID=$!

# ── curl / wget ───────────────────────────────────────────────────────────────
if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL --max-time "${2:-30}" "$1"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -qO- "$1"; }
else
    [ "$IS_TERMUX" = "1" ] \
        && pkg install -y curl >/dev/null 2>&1 \
        || die "curl or wget required"
    fetch() { curl -fsSL --max-time "${2:-30}" "$1"; }
fi

# ── download deepseek.py ──────────────────────────────────────────────────────
mkdir -p "$SHARE_DIR"
fetch "$RAW/deepseek.py" > "$APP.tmp" 2>/dev/null \
    && mv "$APP.tmp" "$APP" \
    || { rm -f "$APP.tmp"; die "download failed — check your internet connection"; }
chmod +x "$APP"

# ── python ────────────────────────────────────────────────────────────────────
PYTHON=""
for cmd in python3 python python3.13 python3.12 python3.11 python3.10 python3.9; do
    if command -v "$cmd" >/dev/null 2>&1; then
        [ "$("$cmd" -c "import sys;print('y'if sys.version_info>=(3,9)else'n')" 2>/dev/null)" = "y" ] \
            && PYTHON="$cmd" && break
    fi
done
if [ -z "$PYTHON" ]; then
    [ "$IS_TERMUX" = "1" ] \
        && pkg install -y python >/dev/null 2>&1 && PYTHON=python \
        || die "Python 3.9+ required"
fi

# ── venv (desktop — PEP 668) ──────────────────────────────────────────────────
VENV_DIR="$SHARE_DIR/.venv"
if [ "$IS_TERMUX" = "0" ]; then
    if ! $PYTHON -m pip install --dry-run pip >/dev/null 2>&1; then
        if [ ! -d "$VENV_DIR" ]; then
            $PYTHON -m venv "$VENV_DIR" >/dev/null 2>&1 || {
                command -v apt-get >/dev/null 2>&1 \
                    && sudo apt-get install -y python3-venv >/dev/null 2>&1 \
                    && $PYTHON -m venv "$VENV_DIR" >/dev/null 2>&1
            }
            [ -d "$VENV_DIR" ] || die "could not create venv"
        fi
        PYTHON="$VENV_DIR/bin/python"
        PIP="$VENV_DIR/bin/pip"
    fi
fi

# ── pip ───────────────────────────────────────────────────────────────────────
if [ -z "$PIP" ]; then
    $PYTHON -m pip --version >/dev/null 2>&1 && PIP="$PYTHON -m pip"
    [ -z "$PIP" ] && command -v pip3 >/dev/null 2>&1 && PIP="pip3"
    [ -z "$PIP" ] && command -v pip  >/dev/null 2>&1 && PIP="pip"
    if [ -z "$PIP" ]; then
        [ "$IS_TERMUX" = "1" ] && pkg install -y python-pip >/dev/null 2>&1 || true
        $PYTHON -m pip --version >/dev/null 2>&1 && PIP="$PYTHON -m pip"
        [ -z "$PIP" ] && die "pip not found"
    fi
fi

has_pkg()     { $PYTHON -c "import $1" >/dev/null 2>&1; }
pip_install() { $PIP install --quiet --disable-pip-version-check "$@" >/dev/null 2>&1 || true; }

# ── deps ──────────────────────────────────────────────────────────────────────
has_pkg numpy || {
    [ "$IS_TERMUX" = "1" ] \
        && { pkg install -y python-numpy >/dev/null 2>&1 || pip_install numpy; } \
        || pip_install numpy
    has_pkg numpy || die "numpy install failed"
}

{ has_pkg wasmtime || command -v node >/dev/null 2>&1; } || {
    if [ "$IS_TERMUX" = "1" ]; then
        pkg install -y nodejs >/dev/null 2>&1 || pkg install -y nodejs-lts >/dev/null 2>&1 || true
        command -v node >/dev/null 2>&1 || die "could not install nodejs"
    else
        pip_install wasmtime
        has_pkg wasmtime || die "wasmtime install failed"
    fi
}

has_pkg curl_cffi || {
    [ "$IS_TERMUX" = "1" ] && {
        pkg list-installed 2>/dev/null | grep -q "^libcurl" \
            || pkg install -y libcurl >/dev/null 2>&1 || true
    }
    pip_install curl-cffi
    has_pkg curl_cffi || { pip_install requests; has_pkg requests || die "could not install curl_cffi or requests"; }
}

# ── write ~/.local/bin/deepseek & dsk ────────────────────────────────────────
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/deepseek" << EOF
#!/bin/sh
exec $PYTHON "$APP" "\$@"
EOF
chmod +x "$BIN_DIR/deepseek"
ln -sf "$BIN_DIR/deepseek" "$BIN_DIR/dsk"

# ── PATH (desktop) ────────────────────────────────────────────────────────────
if [ "$IS_TERMUX" = "0" ]; then
    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *)
            LINE="export PATH=\"\$HOME/.local/bin:\$PATH\""
            for prof in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.zshrc" "$HOME/.zprofile" "$HOME/.profile"; do
                [ -f "$prof" ] && grep -qF ".local/bin" "$prof" 2>/dev/null \
                    || printf '\n%s\n' "$LINE" >> "$prof"
            done ;;
    esac
fi

# ── done ──────────────────────────────────────────────────────────────────────
HL="\033[38;5;75m"
kill "$SPIN_PID" 2>/dev/null; wait "$SPIN_PID" 2>/dev/null
printf "\r\033[K\n"
printf "  ${GREEN}✓${R}  ${CYAN}DeepSeek Code installed${R}\n\n"

case ":$PATH:" in
    *":$BIN_DIR:"*)
        printf "  Run ${BOLD}${CYAN}dsk${R} or ${BOLD}${CYAN}deepseek${R} to start.\n\n"
        ;;
    *)
        printf "  Add to PATH first:\n\n"
        printf "    ${HL}export PATH=\"\$HOME/.local/bin:\$PATH\"${R}\n\n"
        printf "  Then run ${BOLD}${CYAN}dsk${R} or ${BOLD}${CYAN}deepseek${R} to start.\n\n"
        ;;
esac
