#!/bin/bash
# Build PreviewMD.app using PyInstaller
# Usage: bash build.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DIST_DIR="$SCRIPT_DIR/dist"
APP_NAME="PreviewMD"

# Auto-detect the right Python (must already have pywebview + watchdog installed)
detect_python() {
    for candidate in "$SCRIPT_DIR/../../miniconda3/bin/python3" \
                     "/Users/$USER/miniconda3/bin/python3" \
                     "$(which python3)"; do
        if [ -x "$candidate" ] && "$candidate" -c "import webview, watchdog" 2>/dev/null; then
            echo "$candidate"
            return
        fi
    done
    echo ""
}

PYTHON=$(detect_python)

if [ -z "$PYTHON" ]; then
    echo "ERROR: Could not find a Python with pywebview and watchdog installed."
    echo "       Run: pip3 install pywebview watchdog"
    echo "       Then retry: bash build.sh"
    exit 1
fi

echo "==> Using Python: $PYTHON"

# Install pyinstaller if not present
if ! "$PYTHON" -c "import PyInstaller" 2>/dev/null; then
    echo "==> Installing pyinstaller..."
    "$PYTHON" -m pip install pyinstaller
fi

echo "==> Building $APP_NAME.app..."

"$PYTHON" -m PyInstaller \
    --onedir \
    --windowed \
    --name "$APP_NAME" \
    --add-data "resources:resources" \
    --osx-bundle-identifier com.previewmd.app \
    --clean \
    --noconfirm \
    "$SCRIPT_DIR/preview.py"

echo ""
echo "==> Done! App is at: $DIST_DIR/$APP_NAME.app"
echo ""

# ── Ad-hoc code signing ──
echo "==> Ad-hoc signing $APP_NAME.app..."
codesign --deep --force --sign - "$DIST_DIR/$APP_NAME.app" 2>/dev/null && \
    echo "    Signed OK." || echo "    Signing skipped (may fail on some systems, app still usable)."

# ── Create DMG for distribution ──
echo ""
echo "==> Creating DMG..."
DMG_PATH="$DIST_DIR/$APP_NAME.dmg"
rm -f "$DMG_PATH"
hdiutil create -volname "$APP_NAME" \
    -srcfolder "$DIST_DIR/$APP_NAME.app" \
    -ov -format UDZO \
    "$DMG_PATH" 2>&1 | tail -1

echo ""
echo "==> Build complete!"
echo ""
echo "    App:  $DIST_DIR/$APP_NAME.app"
echo "    DMG:  $DMG_PATH"
echo ""
echo "    Give the DMG to others. They open it and drag the app to /Applications."
echo "    First run: right-click the app → Open (to bypass Gatekeeper)."
