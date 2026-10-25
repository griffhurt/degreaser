#!/usr/bin/env bash
# Package dist/Degreaser (PyInstaller onedir) as dist/Degreaser-VERSION-x86_64.AppImage, with
# update information for AppImageUpdate (and a .zsync next to it). Run by release.yml.
#
# Inputs (environment): VERSION, APPIMAGETOOL (path to appimagetool-x86_64.AppImage)
set -euo pipefail

APPDIR=$(mktemp -d)/Degreaser.AppDir
NAME="Degreaser-${VERSION}-x86_64.AppImage"
mkdir -p "$APPDIR/usr/lib"
cp -a dist/Degreaser "$APPDIR/usr/lib/degreaser"
cp packaging/icons/degreaser.png "$APPDIR/degreaser.png"
cp packaging/icons/degreaser.png "$APPDIR/.DirIcon"
cat > "$APPDIR/degreaser.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Degreaser
Comment=Triage large file trees one file at a time
Exec=degreaser %F
Icon=degreaser
Categories=Utility;FileTools;
Terminal=false
DESKTOP
cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/degreaser/degreaser" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

# CI runners have no FUSE, so appimagetool runs extracted. It writes the .zsync into the
# current directory, so run it from dist/ to keep both files together.
(
  cd dist
  APPIMAGE_EXTRACT_AND_RUN=1 ARCH=x86_64 "$APPIMAGETOOL" \
    --updateinformation "gh-releases-zsync|griffhurt|degreaser|latest|Degreaser-*-x86_64.AppImage.zsync" \
    "$APPDIR" "$NAME"
)
[[ -f "dist/$NAME.zsync" ]] || { echo "appimagetool didn't write dist/$NAME.zsync"; exit 1; }
echo "Done: dist/$NAME and dist/$NAME.zsync"
