#!/usr/bin/env bash
# =============================================================================
# Quill — macOS Installer
# =============================================================================
#
# Creates a native Quill.app bundle in /Applications and a Dock-compatible
# application that starts the server and opens Quill in app mode.
#
# Usage:
#   bash install/install-macos.sh
#
# Requirements:
#   - Python 3.10+ (via brew or python.org)
#   - git
#   - Google Chrome or Chromium (for --app mode); Safari is used as fallback
#
# What it does:
#   1. Verifies Python 3.10+ and git
#   2. Creates a venv at ~/Library/Application Support/Quill/venv
#   3. Installs Python dependencies
#   4. Generates quill.icns icon set
#   5. Builds /Applications/Quill.app bundle
#   6. Creates a Dock-compatible .command launcher
#
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "${GREEN}✓${RESET} $*"; }
info() { echo -e "${CYAN}→${RESET} $*"; }
warn() { echo -e "${YELLOW}⚠${RESET} $*"; }
fail() { echo -e "${RED}✗${RESET} $*"; exit 1; }

echo -e "\n${BOLD}${CYAN}  Quill — macOS Installer${RESET}\n"

# ── Paths ────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SUPPORT_DIR="$HOME/Library/Application Support/Quill"
VENV_DIR="$SUPPORT_DIR/venv"
APP_BUNDLE="/Applications/Quill.app"
APP_MACOS="$APP_BUNDLE/Contents/MacOS"
APP_RES="$APP_BUNDLE/Contents/Resources"
SOURCE_ICON="$SCRIPT_DIR/icons/quill.png"
LOG_FILE="$SUPPORT_DIR/quill-server.log"

info "Repo:    $REPO_DIR"
info "Install: $SUPPORT_DIR"

# ── 1. Check Python ──────────────────────────────────────────────────────────

echo ""
info "Checking dependencies…"

PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &>/dev/null; then
        if "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done
[[ -z "$PYTHON" ]] && fail "Python 3.10+ required.\n  Install via Homebrew: brew install python3\n  Or: https://www.python.org/downloads/macos/"
ok "Python: $($PYTHON --version)"

command -v git &>/dev/null || fail "git not found. Install Xcode Command Line Tools: xcode-select --install"
ok "git: $(git --version)"

# ── 2. Create virtualenv ─────────────────────────────────────────────────────

echo ""
info "Setting up Python environment…"
mkdir -p "$SUPPORT_DIR"
if [[ ! -d "$VENV_DIR" ]]; then
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Created venv at $VENV_DIR"
else
    ok "Existing venv at $VENV_DIR"
fi

# ── 3. Install dependencies ──────────────────────────────────────────────────

info "Installing Python dependencies…"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"
ok "Dependencies installed"

# ── 4. Build ICNS icon ───────────────────────────────────────────────────────

echo ""
info "Building icon set…"

ICONSET_DIR="$SUPPORT_DIR/quill.iconset"
mkdir -p "$ICONSET_DIR"

ICNS_BUILT=false
# Try Pillow for resizing
if "$VENV_DIR/bin/python" -c "from PIL import Image" 2>/dev/null; then
    "$VENV_DIR/bin/python" - << PYICON
from PIL import Image
import pathlib, os
src = pathlib.Path('$SOURCE_ICON')
dst = pathlib.Path('$ICONSET_DIR')
sizes = {
    'icon_16x16.png':     16,  'icon_16x16@2x.png':   32,
    'icon_32x32.png':     32,  'icon_32x32@2x.png':   64,
    'icon_128x128.png':  128,  'icon_128x128@2x.png': 256,
    'icon_256x256.png':  256,  'icon_256x256@2x.png': 512,
    'icon_512x512.png':  512,  'icon_512x512@2x.png': 1024,
}
img = Image.open(src).convert('RGBA')
for name, size in sizes.items():
    img.resize((size, size), Image.LANCZOS).save(dst / name)
    print(f'  → {name}')
PYICON
    ICNS_BUILT=true
    ok "Icon resizing done (Pillow)"
elif command -v sips &>/dev/null; then
    for size in 16 32 64 128 256 512 1024; do
        sips -z "$size" "$size" "$SOURCE_ICON" --out "$ICONSET_DIR/icon_${size}x${size}.png" &>/dev/null
    done
    ICNS_BUILT=true
    ok "Icon resizing done (sips)"
else
    warn "No image processing tool found — icon may not render at all sizes"
fi

if $ICNS_BUILT && command -v iconutil &>/dev/null; then
    iconutil -c icns "$ICONSET_DIR" -o "$SUPPORT_DIR/quill.icns" 2>/dev/null
    ok "ICNS built: $SUPPORT_DIR/quill.icns"
    ICON_FOR_APP="$SUPPORT_DIR/quill.icns"
else
    # Fallback: use PNG (modern macOS accepts it for app bundles)
    cp "$SOURCE_ICON" "$SUPPORT_DIR/quill.png"
    ICON_FOR_APP="$SUPPORT_DIR/quill.png"
    warn "iconutil not available — using PNG icon"
fi

# ── 5. Build .app bundle ─────────────────────────────────────────────────────

echo ""
info "Building Quill.app bundle…"

# Remove stale bundle
[[ -d "$APP_BUNDLE" ]] && rm -rf "$APP_BUNDLE"
mkdir -p "$APP_MACOS" "$APP_RES"

# Info.plist
cat > "$APP_BUNDLE/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>      <string>quill</string>
    <key>CFBundleIdentifier</key>      <string>com.batteryphil.quill</string>
    <key>CFBundleName</key>            <string>Quill</string>
    <key>CFBundleDisplayName</key>     <string>Quill</string>
    <key>CFBundleVersion</key>         <string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleIconFile</key>        <string>quill</string>
    <key>NSHighResolutionCapable</key> <true/>
    <key>LSMinimumSystemVersion</key>  <string>12.0</string>
    <key>NSAppleEventsUsageDescription</key><string>Quill uses AppleEvents to open your browser.</string>
</dict>
</plist>
PLIST

# Copy icon into bundle
cp "$ICON_FOR_APP" "$APP_RES/"

# Main executable script
cat > "$APP_MACOS/quill" << LAUNCHER_SCRIPT
#!/usr/bin/env bash
# Quill macOS launcher — auto-generated by install-macos.sh

QUILL_REPO="$REPO_DIR"
QUILL_VENV="$VENV_DIR"
QUILL_PORT=8000
LOG_FILE="$LOG_FILE"
LOCK_FILE="/tmp/quill-server.pid"

# Start server if not running
if [[ -f "\$LOCK_FILE" ]]; then
    OLD_PID=\$(cat "\$LOCK_FILE")
    kill -0 "\$OLD_PID" 2>/dev/null || rm -f "\$LOCK_FILE"
fi

if [[ ! -f "\$LOCK_FILE" ]]; then
    cd "\$QUILL_REPO"
    "\$QUILL_VENV/bin/python" -m uvicorn backend.main:app \\
        --host 127.0.0.1 --port "\$QUILL_PORT" \\
        --log-level warning >> "\$LOG_FILE" 2>&1 &
    echo \$! > "\$LOCK_FILE"
fi

# Wait for server ready (up to 15s)
for i in \$(seq 1 30); do
    curl -sf "http://127.0.0.1:\$QUILL_PORT/api/projects" >/dev/null 2>&1 && break
    sleep 0.5
done

URL="http://127.0.0.1:\$QUILL_PORT"

# Open in Chrome app mode (best) → Safari fallback
CHROME=$(ls /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome 2>/dev/null | head -1)
CHROMIUM=$(ls /Applications/Chromium.app/Contents/MacOS/Chromium 2>/dev/null | head -1)

if [[ -x "\$CHROME" ]]; then
    "\$CHROME" --app="\$URL" --window-size=1400,900 &
elif [[ -x "\$CHROMIUM" ]]; then
    "\$CHROMIUM" --app="\$URL" --window-size=1400,900 &
else
    # Safari / default browser
    open "\$URL"
fi
LAUNCHER_SCRIPT

chmod +x "$APP_MACOS/quill"
ok "App bundle: $APP_BUNDLE"

# ── 6. Register with macOS ───────────────────────────────────────────────────

echo ""
info "Registering application…"
touch "$APP_BUNDLE"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$APP_BUNDLE" 2>/dev/null || true
ok "Application registered (should appear in Spotlight)"

# ── Done ─────────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}${GREEN}  ✓ Quill installed to /Applications/Quill.app${RESET}"
echo ""
echo -e "  ${CYAN}Launch options:${RESET}"
echo -e "   • Double-click Quill in /Applications or Launchpad"
echo -e "   • Spotlight search: Cmd+Space → 'Quill'"
echo -e "   • Drag Quill.app to your Dock"
echo ""
echo -e "  ${CYAN}Requires Google Chrome for best app-window experience.${RESET}"
echo -e "  ${CYAN}Falls back to Safari if Chrome is not installed.${RESET}"
echo ""
