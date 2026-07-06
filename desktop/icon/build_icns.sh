#!/bin/bash
# icon-1024.png → icon.icns(macOS 自带 sips/iconutil,零依赖)
set -euo pipefail
cd "$(dirname "$0")"

SRC="icon-1024.png"
SET="icon.iconset"
rm -rf "$SET"; mkdir "$SET"

for sz in 16 32 64 128 256 512; do
  sips -z $sz $sz "$SRC" --out "$SET/icon_${sz}x${sz}.png" >/dev/null
  sips -z $((sz*2)) $((sz*2)) "$SRC" --out "$SET/icon_${sz}x${sz}@2x.png" >/dev/null
done
# 1024 作为 512@2x
sips -z 1024 1024 "$SRC" --out "$SET/icon_512x512@2x.png" >/dev/null

iconutil -c icns "$SET" -o icon.icns
rm -rf "$SET"
echo "已生成 icon.icns:$(ls -la icon.icns | awk '{print $5}') bytes"
