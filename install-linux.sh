#!/usr/bin/env bash
# Installs the built WiFiDrop program for the current user and adds it to the app menu.
#   ./install-linux.sh            install dist/WiFiDrop
#   ./install-linux.sh --remove   uninstall
set -euo pipefail
cd "$(dirname "$0")"

BIN="$HOME/.local/bin/WiFiDrop"
ICON="$HOME/.local/share/icons/hicolor/256x256/apps/wifidrop.png"
DESKTOP="$HOME/.local/share/applications/wifidrop.desktop"

if [[ "${1:-}" == "--remove" ]]; then
  rm -f "$BIN" "$ICON" "$DESKTOP"
  echo "WiFiDrop removed (your settings in ~/.config/wifidrop were kept)."
  exit 0
fi

[[ -x dist/WiFiDrop ]] || { echo "dist/WiFiDrop not found - build it first: python3 build.py"; exit 1; }

# The program uses the system's WebKitGTK for its window.
if ! python3 -c "import gi; gi.require_version('WebKit2','4.1')" 2>/dev/null; then
  echo "Note: WebKitGTK 4.1 seems to be missing. On Ubuntu/Debian: sudo apt install gir1.2-webkit2-4.1"
fi

mkdir -p "$(dirname "$BIN")" "$(dirname "$ICON")" "$(dirname "$DESKTOP")"
install -m 755 dist/WiFiDrop "$BIN"
python3 - "$ICON" <<'EOF' 2>/dev/null || cp assets/icon.png "$ICON"
import sys
from PIL import Image
Image.open("assets/icon.png").resize((256, 256), Image.LANCZOS).save(sys.argv[1])
EOF

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=WiFiDrop
GenericName=File sharing
Comment=Send files between your devices on the same Wi-Fi
Exec=$BIN
Icon=wifidrop
Terminal=false
Categories=Network;FileTransfer;
StartupWMClass=WiFiDrop
EOF
update-desktop-database "$(dirname "$DESKTOP")" 2>/dev/null || true

echo "Installed. Find WiFiDrop in your app menu, or run: $BIN"
if command -v ufw >/dev/null && systemctl is-active --quiet ufw; then
  echo "Firewall: if phones can't connect, run:  sudo ufw allow 8765/tcp && sudo ufw allow 8766/udp"
fi
