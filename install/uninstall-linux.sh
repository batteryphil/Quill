#!/usr/bin/env bash
# Quill — Linux Uninstaller
set -euo pipefail
GREEN='\033[0;32m'; CYAN='\033[0;36m'; RESET='\033[0m'
ok()   { echo -e "${GREEN}✓${RESET} $*"; }
info() { echo -e "${CYAN}→${RESET} $*"; }

echo -e "\nQuill — Uninstall\n"

pkill -f "quill.*uvicorn" 2>/dev/null && info "Stopped running server" || true

rm -f  "$HOME/.local/bin/quill"                          && ok "Removed launcher"          || true
rm -f  "$HOME/.local/share/applications/quill.desktop"  && ok "Removed .desktop entry"    || true
rm -f  "$HOME/Desktop/Quill.desktop"                    && ok "Removed Desktop icon"      || true
rm -rf "$HOME/.local/share/quill"                        && ok "Removed install directory" || true

for size in 16 32 48 64 128 256 512; do
    rm -f "$HOME/.local/share/icons/hicolor/${size}x${size}/apps/quill.png"
done
ok "Removed icon files"

update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo -e "\n${GREEN}✓ Quill uninstalled.${RESET}"
echo -e "  Your projects and writing data at ~/.quill/ were NOT removed."
echo -e "  Delete manually if desired: rm -rf ~/.quill\n"
