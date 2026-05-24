#!/usr/bin/env bash
set -euo pipefail

root="$(pwd)"
dist="$root/dist"
stage="$dist/yc-package"
archive="$dist/max-bot-yc.zip"

mkdir -p "$dist"
rm -rf "$stage"
mkdir -p "$stage"

cp -R "$root/app" "$stage/app"
cp -R "$root/seed" "$stage/seed"
cp "$root/index.py" "$stage/index.py"
cp "$root/requirements.txt" "$stage/requirements.txt"
rm -rf "$stage/app/migration"
find "$stage" -type d -name "__pycache__" -prune -exec rm -rf {} +

rm -f "$archive"
(cd "$stage" && zip -qr "$archive" .)
echo "Пакет Cloud Functions создан: $archive"
