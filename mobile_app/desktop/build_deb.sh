#!/usr/bin/env bash
set -euo pipefail
desktop_source="$(cd "$(dirname "$0")" && pwd)"
output_dir="${1:?Usage: build_deb.sh OUTPUT_DIRECTORY}"
mkdir -p "$output_dir"
stage="$(mktemp -d)"
trap 'rm -rf -- "$stage"' EXIT
install -d "$stage/DEBIAN" "$stage/usr/bin" "$stage/usr/lib/paperscan" \
  "$stage/usr/share/applications" "$stage/usr/share/icons/hicolor/scalable/apps"
cat > "$stage/DEBIAN/control" <<'CONTROL'
Package: cix-paperscan-desktop
Version: 1.2.0
Section: office
Priority: optional
Architecture: all
Depends: python3, python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0
Maintainer: PaperScan
Description: Desktop client for the CIX PaperScan workbench
 Opens a board-hosted workbench for capture, progress and PDF downloads.
CONTROL
install -m 644 "$desktop_source/app.py" "$stage/usr/lib/paperscan/app.py"
install -m 644 "$desktop_source/../web/icon.svg" "$stage/usr/lib/paperscan/icon.svg"
install -m 644 "$desktop_source/com.cix.PaperScan.desktop" "$stage/usr/share/applications/"
install -m 644 "$desktop_source/../web/icon.svg" "$stage/usr/share/icons/hicolor/scalable/apps/com.cix.PaperScan.svg"
cat > "$stage/usr/bin/paperscan" <<'LAUNCHER'
#!/bin/sh
exec /usr/bin/python3 /usr/lib/paperscan/app.py "$@"
LAUNCHER
chmod 755 "$stage/usr/bin/paperscan"
dpkg-deb --root-owner-group --build "$stage" "$output_dir/cix-paperscan-desktop_1.2.0_all.deb"
