#!/usr/bin/env bash
# Build CHILI desktop app for macOS and create a .dmg for distribution.
# Run from repo root: ./chili_mobile/scripts/build_macos.sh
# Requires: macOS, Flutter SDK, Xcode.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

APP_NAME="CHILI"
BUILD_DIR="build/macos/Build/Products/Release"
DMG_NAME="${APP_NAME}.dmg"
DMG_DIR="build/macos/dmg"

echo "Building Flutter macOS release..."
flutter build macos --release

APP_PATH="$BUILD_DIR/chili_mobile.app"
if [[ ! -d "$APP_PATH" ]]; then
  echo "Error: $APP_PATH not found."
  exit 1
fi

echo "Creating DMG..."
mkdir -p "$DMG_DIR"
rm -f "$DMG_DIR/$DMG_NAME"

# Create a temporary read-only DMG; use a writable copy to add the app then convert.
hdiutil create -volname "$APP_NAME" -srcfolder "$APP_PATH" -ov -format UDZO "$DMG_DIR/$DMG_NAME"

echo "Done: $DMG_DIR/$DMG_NAME"
echo "Share this file with housemates. On first open they may need to right-click > Open to bypass Gatekeeper if not signed."
