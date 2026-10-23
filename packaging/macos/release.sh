#!/usr/bin/env bash
# Turn dist/Degreaser.app into a signed, notarised dist/Degreaser-VERSION-macOS.dmg with
# Sparkle embedded (docs/decisions.md D22, D41). Run by .github/workflows/release.yml after
# PyInstaller (which already signed the app's own code when DEGREASER_CODESIGN_IDENTITY was set).
#
# Inputs (environment):
#   VERSION                      e.g. 0.1.0
#   SPARKLE_DIR                  extracted Sparkle release (contains Sparkle.framework)
#   DEGREASER_CODESIGN_IDENTITY  "Developer ID Application: …"; unset → unsigned test build
#   NOTARY_KEY_PATH, NOTARY_KEY_ID, NOTARY_ISSUER   App Store Connect API key; unset → skip
set -euo pipefail

APP=dist/Degreaser.app
DMG="dist/Degreaser-${VERSION}-macOS.dmg"
ENTITLEMENTS=packaging/macos/entitlements.plist
IDENTITY="${DEGREASER_CODESIGN_IDENTITY:-}"

sign() {  # sign one item with the hardened runtime and a secure timestamp
  codesign --force --timestamp --options runtime --sign "$IDENTITY" "$@"
}

echo "Embedding Sparkle"
mkdir -p "$APP/Contents/Frameworks"
rm -rf "$APP/Contents/Frameworks/Sparkle.framework"
ditto "$SPARKLE_DIR/Sparkle.framework" "$APP/Contents/Frameworks/Sparkle.framework"
cp "$SPARKLE_DIR/LICENSE" "$APP/Contents/Resources/Sparkle-LICENSE.txt"

if [[ -n "$IDENTITY" ]]; then
  echo "Signing Sparkle (inside-out, as Sparkle's documentation describes) and the app"
  FW="$APP/Contents/Frameworks/Sparkle.framework/Versions/B"
  sign "$FW/XPCServices/Installer.xpc"
  sign --preserve-metadata=entitlements "$FW/XPCServices/Downloader.xpc"
  sign "$FW/Autoupdate"
  sign "$FW/Updater.app"
  sign "$APP/Contents/Frameworks/Sparkle.framework"
  # Re-seal the bundle around the new framework; its own code is already signed.
  sign --entitlements "$ENTITLEMENTS" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
else
  echo "No signing identity: building an unsigned DMG (for testing only)"
  codesign --force --deep --sign - "$APP"  # ad-hoc, so it still launches on Apple silicon
fi

echo "Building $DMG"
STAGING=$(mktemp -d)
ditto "$APP" "$STAGING/Degreaser.app"
ln -s /Applications "$STAGING/Applications"
rm -f "$DMG"
hdiutil create -volname "Degreaser" -srcfolder "$STAGING" -fs HFS+ -format UDZO -ov "$DMG"
rm -rf "$STAGING"

if [[ -n "$IDENTITY" ]]; then
  codesign --force --timestamp --sign "$IDENTITY" "$DMG"
  if [[ -n "${NOTARY_KEY_PATH:-}" ]]; then
    echo "Notarising (this usually takes a few minutes)"
    xcrun notarytool submit "$DMG" --key "$NOTARY_KEY_PATH" --key-id "$NOTARY_KEY_ID" \
      --issuer "$NOTARY_ISSUER" --wait --timeout 30m
    xcrun stapler staple "$DMG"
    spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG"
  else
    echo "No notarisation key: the DMG is signed but not notarised"
  fi
fi
echo "Done: $DMG"
