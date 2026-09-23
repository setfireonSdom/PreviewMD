#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(uname -s)" != "Darwin" ]]; then
    printf '%s\n' 'ERROR: macOS is required to build PreviewMD.' >&2
    exit 1
fi

if [[ -n "${PYTHON:-}" ]]; then
    PYTHON_BIN="$PYTHON"
elif [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
elif [[ -x "$SCRIPT_DIR/venv/bin/python" ]]; then
    PYTHON_BIN="$SCRIPT_DIR/venv/bin/python"
else
    printf '%s\n' 'ERROR: Create a project .venv or set PYTHON to an interpreter with the build dependencies installed.' >&2
    exit 1
fi

if ! "$PYTHON_BIN" -c 'import PyInstaller, webview, watchdog'; then
    printf '%s\n' 'ERROR: The selected Python needs requirements.txt and pyinstaller. No dependencies were installed.' >&2
    exit 1
fi

for tool in codesign hdiutil ditto; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'ERROR: Required macOS tool not found: %s\n' "$tool" >&2
        exit 1
    fi
done

if [[ ! -f "$SCRIPT_DIR/PreviewMD.spec" ]]; then
    printf '%s\n' 'ERROR: PreviewMD.spec is missing.' >&2
    exit 1
fi

BUILD_NAME="$("$PYTHON_BIN" -c 'import platform, runpy, sys; m = runpy.run_path(sys.argv[1]); print("{}-{}-{}".format(m["APP_NAME"], m["APP_VERSION"], platform.machine()))' "$SCRIPT_DIR/app_metadata.py")"
APP_NAME="$("$PYTHON_BIN" -c 'import runpy, sys; print(runpy.run_path(sys.argv[1])["APP_NAME"])' "$SCRIPT_DIR/app_metadata.py")"
OUTPUT_DIR="$SCRIPT_DIR/dist/$BUILD_NAME"
WORK_DIR="$SCRIPT_DIR/build/$BUILD_NAME"

if [[ -e "$OUTPUT_DIR" ]]; then
    printf '==> Removing previous output: %s\n' "$OUTPUT_DIR"
    rm -rf -- "$OUTPUT_DIR"
fi
mkdir -p "$SCRIPT_DIR/dist"
mkdir "$OUTPUT_DIR"

printf '==> Using Python: %s\n' "$PYTHON_BIN"
"$PYTHON_BIN" -m PyInstaller \
    --distpath "$OUTPUT_DIR" \
    --workpath "$WORK_DIR" \
    --clean \
    --noconfirm \
    "$SCRIPT_DIR/PreviewMD.spec"

APP_PATH="$OUTPUT_DIR/$APP_NAME.app"
if [[ ! -d "$APP_PATH" ]]; then
    printf 'ERROR: Build did not produce %s\n' "$APP_PATH" >&2
    exit 1
fi
rm -rf -- "$OUTPUT_DIR/$APP_NAME"
codesign --deep --force --sign - "$APP_PATH"
codesign --verify --deep --strict "$APP_PATH"

STAGING_DIR="$OUTPUT_DIR/dmg-staging"
mkdir "$STAGING_DIR"
ditto "$APP_PATH" "$STAGING_DIR/$APP_NAME.app"
ln -s /Applications "$STAGING_DIR/Applications"
DMG_PATH="$OUTPUT_DIR/$BUILD_NAME.dmg"
hdiutil create -volname "$APP_NAME" \
    -srcfolder "$STAGING_DIR" \
    -format UDZO "$DMG_PATH"
hdiutil verify "$DMG_PATH"
rm -rf -- "$STAGING_DIR"

printf '\nApp: %s\nDMG: %s\n' "$APP_PATH" "$DMG_PATH"
printf '%s\n' 'Ad-hoc signed only; not Developer ID signed or notarized.'
