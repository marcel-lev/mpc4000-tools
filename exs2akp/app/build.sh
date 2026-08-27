#!/bin/zsh
# Builds "MPC EXS to MPC.app" and installs it into /Applications.
set -euo pipefail
cd "$(dirname "$0")"

APP="/Applications/MPC EXS to MPC.app"

echo "compiling…"
swiftc -O -parse-as-library Exs2AkpApp.swift -o exs2akp-gui

echo "assembling bundle…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
mv exs2akp-gui "$APP/Contents/MacOS/"
cp Info.plist "$APP/Contents/"
cp ../exs2akp.py "$APP/Contents/Resources/"

echo "rendering icon…"
swift MakeIcon.swift icon-1024.png >/dev/null
rm -rf AppIcon.iconset && mkdir AppIcon.iconset
for s in 16 32 128 256 512; do
  sips -z $s $s icon-1024.png --out "AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  sips -z $d $d icon-1024.png --out "AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns AppIcon.iconset -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf AppIcon.iconset icon-1024.png

codesign --force -s - "$APP"
echo "built: $APP"
