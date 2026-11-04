#!/usr/bin/env bash
# Builds dist/OCR Studio.app  (SwiftUI front end + bundled Python engine).
set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG=${CONFIG:-release}
swift build -c "$CONFIG" --package-path app
BIN="$(swift build -c "$CONFIG" --package-path app --show-bin-path)/OCRStudio"

APP="dist/OCR Studio.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/OCRStudio"

# Engine + its requirements travel inside the bundle; the venv is created on first launch.
rsync -a --exclude '__pycache__' --exclude '*.pyc' engine/ "$APP/Contents/Resources/engine/"
cp requirements.txt "$APP/Contents/Resources/requirements.txt"

python3 scripts/make_icon.py "$APP/Contents/Resources"
rm -rf "$APP/Contents/Resources/AppIcon.iconset"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>OCR Studio</string>
  <key>CFBundleDisplayName</key><string>OCR Studio</string>
  <key>CFBundleIdentifier</key><string>dev.festomanolo.ocrstudio</string>
  <key>CFBundleExecutable</key><string>OCRStudio</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>CFBundleDocumentTypes</key><array>
    <dict>
      <key>CFBundleTypeName</key><string>Documents</string>
      <key>CFBundleTypeRole</key><string>Viewer</string>
      <key>LSItemContentTypes</key><array>
        <string>com.adobe.pdf</string><string>public.image</string>
        <string>org.openxmlformats.wordprocessingml.document</string>
        <string>org.openxmlformats.presentationml.presentation</string>
        <string>public.html</string>
      </array>
    </dict>
  </array>
</dict></plist>
PLIST

codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
echo "Built: $APP"
