#!/bin/bash
# Scarlett Control — Uninstaller

echo ""
echo "  🗑  Removing Scarlett Control..."

rm -rf  "$HOME/.local/share/scarlett-control"
rm -f   "$HOME/.local/share/applications/scarlett-control.desktop"
rm -f   "$HOME/.local/bin/scarlett-control"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo "  ✅  Scarlett Control removed."
echo ""
