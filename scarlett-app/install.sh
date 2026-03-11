#!/bin/bash
# ============================================================
#  Scarlett Control — Linux Install Script
# ============================================================
# Installs the app + all dependencies so it appears in your
# application launcher.  Run with:  bash install.sh
# ============================================================

set -e

INSTALL_DIR="$HOME/.local/share/scarlett-control"
DESKTOP_DIR="$HOME/.local/share/applications"
BIN_DIR="$HOME/.local/bin"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Detect package manager
if   command -v apt-get  &>/dev/null; then PKG=apt
elif command -v dnf      &>/dev/null; then PKG=dnf
elif command -v pacman   &>/dev/null; then PKG=pacman
elif command -v zypper   &>/dev/null; then PKG=zypper
else                                       PKG=unknown
fi

# ── Helpers ───────────────────────────────────────────────────────────────────

green()  { echo "  ✅  $*"; }
warn()   { echo "  ⚠️   $*"; }
fail()   { echo "  ❌  $*"; }
info()   { echo "  ℹ️   $*"; }
header() { echo ""; echo "  $*"; }

try_pip() {
  # Try pip install, suppressing noise, return 0 on success
  pip3 install --user --quiet "$@" 2>/dev/null && return 0
  pip3 install --user "$@" 2>&1 | tail -3
  return 1
}

sys_install() {
  # Install a system package with sudo, if available
  local pkg="$1"
  if ! command -v sudo &>/dev/null; then
    warn "sudo not available — cannot install system package $pkg"
    return 1
  fi
  case "$PKG" in
    apt)    sudo apt-get install -y "$pkg" ;;
    dnf)    sudo dnf install -y "$pkg" ;;
    pacman) sudo pacman -S --noconfirm "$pkg" ;;
    zypper) sudo zypper install -y "$pkg" ;;
    *)      warn "Unknown package manager — install $pkg manually"; return 1 ;;
  esac
}

# ── Banner ────────────────────────────────────────────────────────────────────

echo ""
echo "  🎛  Scarlett Control — Linux Installer"
echo "  ======================================="
echo ""
info "Detected package manager: ${PKG}"

# ── 1. Python 3 ───────────────────────────────────────────────────────────────

header "Checking Python 3…"
if ! command -v python3 &>/dev/null; then
  fail "Python 3 is required but not installed."
  case "$PKG" in
    apt)    echo "      Run: sudo apt install python3 python3-pip" ;;
    dnf)    echo "      Run: sudo dnf install python3 python3-pip" ;;
    pacman) echo "      Run: sudo pacman -S python python-pip" ;;
    *)      echo "      Install python3 and pip3 for your distribution." ;;
  esac
  exit 1
fi
green "Python $(python3 --version) found"

# Ensure pip is available
if ! command -v pip3 &>/dev/null; then
  warn "pip3 not found — attempting to install…"
  case "$PKG" in
    apt)    sys_install python3-pip ;;
    dnf)    sys_install python3-pip ;;
    pacman) sys_install python-pip ;;
    *)      fail "Install pip3 manually, then re-run this script."; exit 1 ;;
  esac
fi

# ── 2. PyQt6 + WebEngine ──────────────────────────────────────────────────────

header "Installing PyQt6 (the app window toolkit)…"
echo "      This may take 2–3 minutes on first install."
echo ""

# Try pip first — works on most distros
if try_pip PyQt6 PyQt6-WebEngine; then
  green "PyQt6 installed via pip"
else
  warn "pip install failed — trying system packages…"
  case "$PKG" in
    apt)
      sys_install python3-pyqt6
      sys_install python3-pyqt6.qtwebengine || sys_install python3-pyqt6-webengine || true
      ;;
    dnf)
      sys_install python3-qt6
      sys_install python3-qt6-webengine || true
      ;;
    pacman)
      sys_install python-pyqt6
      sys_install python-pyqt6-webengine || true
      ;;
    zypper)
      sys_install python3-PyQt6
      sys_install python3-PyQt6-WebEngine || true
      ;;
    *)
      fail "Could not install PyQt6 automatically."
      echo "      Try: pip3 install --user PyQt6 PyQt6-WebEngine"
      exit 1
      ;;
  esac
fi

# Verify the critical import works
if ! python3 -c "from PyQt6.QtWebEngineWidgets import QWebEngineView" 2>/dev/null; then
  fail "PyQt6-WebEngine is not working after install."
  echo ""
  echo "      Common fixes:"
  case "$PKG" in
    apt)    echo "        sudo apt install python3-pyqt6.qtwebengine" ;;
    dnf)    echo "        sudo dnf install python3-qt6-webengine" ;;
    pacman) echo "        sudo pacman -S python-pyqt6-webengine" ;;
    *)      echo "        pip3 install --user PyQt6 PyQt6-WebEngine" ;;
  esac
  exit 1
fi
green "PyQt6-WebEngine working"

# ── 3. parecord (PulseAudio utils — for live signal meters) ──────────────────

header "Checking parecord (required for live signal meters)…"

if command -v parecord &>/dev/null; then
  green "parecord already installed"
else
  warn "parecord not found — installing pulseaudio-utils…"
  case "$PKG" in
    apt)    sys_install pulseaudio-utils ;;
    dnf)    sys_install pulseaudio-utils ;;
    pacman) sys_install libpulse ;;          # parecord is part of libpulse on Arch
    zypper) sys_install pulseaudio-utils ;;
    *)
      warn "Unknown package manager."
      echo "      Install 'pulseaudio-utils' or 'parecord' for your distro."
      echo "      Signal meters will not work without it."
      ;;
  esac

  if command -v parecord &>/dev/null; then
    green "parecord installed"
  else
    warn "parecord still not found after install attempt."
    echo "      Signal meters will be inactive."
    echo "      On Ubuntu/Debian: sudo apt install pulseaudio-utils"
    echo "      On Fedora:        sudo dnf install pulseaudio-utils"
    echo "      On Arch:          sudo pacman -S libpulse"
    # Non-fatal — app works without meters
  fi
fi

# ── 4. Copy app files ─────────────────────────────────────────────────────────

header "Installing app files to $INSTALL_DIR …"
mkdir -p "$INSTALL_DIR/ui"

cp "$SCRIPT_DIR/main.py"       "$INSTALL_DIR/"
cp "$SCRIPT_DIR/backend.py"    "$INSTALL_DIR/"
cp "$SCRIPT_DIR/ui/index.html" "$INSTALL_DIR/ui/"

green "App files installed"

# ── 5. Launcher script ────────────────────────────────────────────────────────

mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/scarlett-control" << EOF
#!/bin/bash
cd "$INSTALL_DIR"
exec python3 "$INSTALL_DIR/main.py" "\$@"
EOF
chmod +x "$BIN_DIR/scarlett-control"
green "Launcher created at $BIN_DIR/scarlett-control"

# ── 6. Desktop entry ──────────────────────────────────────────────────────────

mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/scarlett-control.desktop" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Scarlett Control
GenericName=Audio Interface Control
Comment=Control panel for Focusrite Scarlett interfaces on Linux
Exec=$BIN_DIR/scarlett-control
Icon=audio-card
Terminal=false
Categories=AudioVideo;Audio;Settings;
Keywords=focusrite;scarlett;audio;interface;microphone;recording;
StartupNotify=true
EOF
chmod +x "$DESKTOP_DIR/scarlett-control.desktop"
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
green "App launcher entry created"

# ── 7. PATH check ─────────────────────────────────────────────────────────────

if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
  echo ""
  warn "~/.local/bin is not in your PATH."
  echo "      Add this line to your ~/.bashrc or ~/.zshrc, then restart your terminal:"
  echo ""
  echo "          export PATH=\"\$HOME/.local/bin:\$PATH\""
  echo ""
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "  ✅  Installation complete!"
echo ""
echo "  ┌─────────────────────────────────────────────────────┐"
echo "  │  To launch Scarlett Control:                        │"
echo "  │                                                     │"
echo "  │  • Search 'Scarlett Control' in your app launcher   │"
echo "  │  • Or run in terminal:  scarlett-control            │"
echo "  └─────────────────────────────────────────────────────┘"
echo ""
