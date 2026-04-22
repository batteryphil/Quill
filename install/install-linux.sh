#!/usr/bin/env bash
# =============================================================================
# Quill — Linux Installer
# =============================================================================
#
# Installs Quill as a native desktop application for the current user.
# No root/sudo required.
#
# What it does:
#   1. Verifies Python 3.10+ and git
#   2. Creates a Python venv at ~/.local/share/quill/venv
#   3. Installs Python dependencies
#   4. Generates multi-size icons (128px, 256px, 512px)
#   5. Creates a launcher script at ~/.local/bin/quill
#   6. Writes a .desktop entry → Applications menu
#   7. Copies a desktop shortcut to ~/Desktop
#   8. Updates the desktop database
#
# Usage:
#   bash install/install-linux.sh
#
# =============================================================================

set -euo pipefail

# ── Colours ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "${GREEN}✓${RESET} $*"; }
info() { echo -e "${CYAN}→${RESET} $*"; }
warn() { echo -e "${YELLOW}⚠${RESET} $*"; }
fail() { echo -e "${RED}✗${RESET} $*"; exit 1; }

echo -e "\n${BOLD}${CYAN}  Quill — Linux Installer${RESET}\n"

# ── Paths ────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_DIR="$HOME/.local/share/quill"
VENV_DIR="$INSTALL_DIR/venv"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICON_BASE="$HOME/.local/share/icons/hicolor"
DESKTOP_FILE="$APP_DIR/quill.desktop"
LAUNCHER="$BIN_DIR/quill"
SOURCE_ICON="$SCRIPT_DIR/icons/quill.png"

info "Repo:    $REPO_DIR"
info "Install: $INSTALL_DIR"

# ── 1. Check Python ──────────────────────────────────────────────────────────

echo ""
info "Checking dependencies…"

PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c 'import sys; print(sys.version_info[:2])')
        if "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done
[[ -z "$PYTHON" ]] && fail "Python 3.10+ required. Install: sudo apt install python3.12"
ok "Python: $($PYTHON --version)"

command -v git &>/dev/null || fail "git required. Install: sudo apt install git"
ok "git: $(git --version)"

# ── 2. Create virtualenv ─────────────────────────────────────────────────────

echo ""
info "Setting up Python environment…"
mkdir -p "$INSTALL_DIR"
if [[ ! -d "$VENV_DIR" ]]; then
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Created venv at $VENV_DIR"
else
    ok "Existing venv at $VENV_DIR"
fi

# ── 3. Install dependencies ──────────────────────────────────────────────────

info "Installing Python dependencies (this may take a minute)…"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"
ok "Dependencies installed"

# ── 4. Install icons ─────────────────────────────────────────────────────────

echo ""
info "Installing icons…"
mkdir -p "$INSTALL_DIR"

# Copy master icon
cp "$SOURCE_ICON" "$INSTALL_DIR/quill.png"

# Install into hicolor icon theme at multiple sizes (requires python3-pil or imagemagick)
ICON_INSTALLED=false
if "$VENV_DIR/bin/python" -c "from PIL import Image" 2>/dev/null; then
    "$VENV_DIR/bin/python" - << 'PYICON'
from PIL import Image
import os, pathlib

src  = pathlib.Path(os.environ.get("INSTALL_DIR", os.path.expanduser("~/.local/share/quill"))) / "quill.png"
base = pathlib.Path(os.path.expanduser("~/.local/share/icons/hicolor"))

for size in [16, 32, 48, 64, 128, 256, 512]:
    dest = base / f"{size}x{size}" / "apps"
    dest.mkdir(parents=True, exist_ok=True)
    img  = Image.open(src).resize((size, size), Image.LANCZOS)
    img.save(dest / "quill.png")
    print(f"  → {size}x{size}")
PYICON
    ICON_INSTALLED=true
    ok "Multi-size icons installed (hicolor theme)"
elif command -v convert &>/dev/null; then
    for size in 16 32 48 64 128 256 512; do
        d="$ICON_BASE/${size}x${size}/apps"
        mkdir -p "$d"
        convert -resize "${size}x${size}" "$INSTALL_DIR/quill.png" "$d/quill.png"
    done
    ICON_INSTALLED=true
    ok "Multi-size icons installed (ImageMagick)"
else
    warn "Pillow/ImageMagick not found — using single-size icon (install python3-pil for better results)"
fi

# ── 5. Create launcher script ────────────────────────────────────────────────

echo ""
info "Creating launcher…"
mkdir -p "$BIN_DIR"

# Detect best browser for app mode
BROWSER_CMD=""
for b in google-chrome chromium-browser chromium google-chrome-stable brave-browser microsoft-edge; do
    if command -v "$b" &>/dev/null; then
        BROWSER_CMD="$b"
        break
    fi
done

cat > "$LAUNCHER" << LAUNCHER_SCRIPT
#!/usr/bin/env bash
# Quill launcher — starts the server and opens the app window.
# Auto-generated by install-linux.sh — do not edit.

QUILL_REPO="$REPO_DIR"
QUILL_VENV="$VENV_DIR"
QUILL_PORT=8000
LOCK_FILE="/tmp/quill-server.pid"
LOG_FILE="/tmp/quill-server.log"

# ── Start server (if not already running) ─────────────────────────
if [[ -f "\$LOCK_FILE" ]]; then
    OLD_PID=\$(cat "\$LOCK_FILE")
    if kill -0 "\$OLD_PID" 2>/dev/null; then
        echo "Quill server already running (PID \$OLD_PID)"
    else
        rm -f "\$LOCK_FILE"
    fi
fi

if [[ ! -f "\$LOCK_FILE" ]]; then
    cd "\$QUILL_REPO"
    "\$QUILL_VENV/bin/python" -m uvicorn backend.main:app \\
        --host 127.0.0.1 --port "\$QUILL_PORT" \\
        --log-level warning >> "\$LOG_FILE" 2>&1 &
    echo \$! > "\$LOCK_FILE"
    echo "Starting Quill server (PID \$(cat \$LOCK_FILE))…"
fi

# ── Wait for server ready ─────────────────────────────────────────
for i in \$(seq 1 30); do
    if curl -sf "http://127.0.0.1:\$QUILL_PORT/api/projects" >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

# ── Open in app mode ──────────────────────────────────────────────
BROWSER="${BROWSER_CMD}"
URL="http://127.0.0.1:\$QUILL_PORT"

open_browser() {
    if   [[ -n "\$BROWSER" ]]      && command -v "\$BROWSER" &>/dev/null; then
        "\$BROWSER" --app="\$URL" --window-size=1400,900 --class=Quill \\
            --user-data-dir="\$HOME/.local/share/quill/browser-profile" &
    elif command -v google-chrome  &>/dev/null; then
        google-chrome  --app="\$URL" --window-size=1400,900 --class=Quill &
    elif command -v chromium-browser &>/dev/null; then
        chromium-browser --app="\$URL" --window-size=1400,900 --class=Quill &
    elif command -v chromium       &>/dev/null; then
        chromium       --app="\$URL" --window-size=1400,900 --class=Quill &
    elif command -v firefox        &>/dev/null; then
        firefox "\$URL" &
    else
        xdg-open "\$URL"
    fi
}

open_browser
LAUNCHER_SCRIPT

chmod +x "$LAUNCHER"
ok "Launcher: $LAUNCHER"

# ── 6. Write .desktop file ───────────────────────────────────────────────────

echo ""
info "Creating .desktop entry…"
mkdir -p "$APP_DIR"

cat > "$DESKTOP_FILE" << DESKTOP
[Desktop Entry]
Version=1.0
Type=Application
Name=Quill
GenericName=Writing Environment
Comment=AI-first local book writing platform
Exec=$LAUNCHER
Icon=$INSTALL_DIR/quill.png
Terminal=false
StartupNotify=true
StartupWMClass=Quill
Categories=Office;WordProcessor;Education;
Keywords=writing;novel;book;AI;story;author;
MimeType=text/markdown;
Actions=NewWindow;

[Desktop Action NewWindow]
Name=Open Quill
Exec=$LAUNCHER
DESKTOP

chmod +x "$DESKTOP_FILE"
ok "Application entry: $DESKTOP_FILE"

# ── 7. Desktop shortcut ──────────────────────────────────────────────────────

DESKTOP_DIR="$HOME/Desktop"
[[ -d "$DESKTOP_DIR" ]] || DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME")"

if [[ -d "$DESKTOP_DIR" ]]; then
    cp "$DESKTOP_FILE" "$DESKTOP_DIR/Quill.desktop"
    chmod +x "$DESKTOP_DIR/Quill.desktop"
    gio set "$DESKTOP_DIR/Quill.desktop" metadata::trusted true 2>/dev/null || true
    ok "Desktop icon: $DESKTOP_DIR/Quill.desktop"
fi

# ── 8. Update database ───────────────────────────────────────────────────────

echo ""
info "Refreshing desktop database…"
update-desktop-database "$APP_DIR" 2>/dev/null && ok "Desktop database updated" || warn "update-desktop-database not available (icon may appear after logout/login)"
gtk-update-icon-cache -f -t "$ICON_BASE" 2>/dev/null || true
xdg-desktop-menu forceupdate 2>/dev/null || true

# ── Done ─────────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}${GREEN}  ✓ Quill installed successfully!${RESET}"
echo ""
echo -e "  ${CYAN}Launch options:${RESET}"
echo -e "   • Double-click the Quill icon on your Desktop"
echo -e "   • Search 'Quill' in your application launcher"
echo -e "   • Run from terminal: quill"
echo ""
echo -e "  ${CYAN}Installed to:${RESET} $INSTALL_DIR"
echo -e "  ${CYAN}Server logs:${RESET}  /tmp/quill-server.log"
echo ""
