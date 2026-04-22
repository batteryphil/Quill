#!/usr/bin/env bash
# Quill — macOS Uninstaller
set -euo pipefail
GREEN='\033[0;32m'; CYAN='\033[0;36m'; RESET='\033[0m'
ok()   { echo -e "${GREEN}✓${RESET} $*"; }
info() { echo -e "${CYAN}→${RESET} $*"; }

echo -e "\nQuill — Uninstall\n"

pkill -f "quill.*uvicorn" 2>/dev/null && info "Stopped running server" || true

APP="/Applications/Quill.app"
SUPPORT="$HOME/Library/Application Support/Quill"

[[ -d "$APP"     ]] && rm -rf "$APP"     && ok "Removed $APP"
[[ -d "$SUPPORT" ]] && rm -rf "$SUPPORT" && ok "Removed $SUPPORT"

/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -u "$APP" 2>/dev/null || true

echo -e "\n${GREEN}✓ Quill uninstalled.${RESET}"
echo -e "  Your projects at ~/.quill/ were NOT removed.\n"
