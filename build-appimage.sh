#!/bin/sh
set -eu
cd "$(dirname "$0")"

version=2.3.2
tool_url=https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage
tool_sha=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0
tmp=$(mktemp -d)
container=porchlight-appimage-$$
trap 'docker rm -f "$container" >/dev/null 2>&1 || true; rm -rf "$tmp"' EXIT

mkdir -p dist "$tmp/AppDir/usr/bin" "$tmp/AppDir/usr/lib/x86_64-linux-gnu" \
    "$tmp/AppDir/usr/share/applications" "$tmp/AppDir/usr/share/icons/hicolor/256x256/apps" \
    "$tmp/AppDir/usr/share/porchlight/server" "$tmp/AppDir/usr/share/porchlight/web" \
    "$tmp/AppDir/usr/share/porchlight/models"

docker image inspect porchlight-motion:1.36.33 >/dev/null 2>&1 || \
    docker build -t porchlight-motion:1.36.33 tools/motion
docker create --name "$container" --entrypoint sleep porchlight-motion:1.36.33 infinity >/dev/null
docker start "$container" >/dev/null
docker cp -L "$container:/usr/bin/python3" "$tmp/AppDir/usr/bin/python3"
docker cp "$container:/usr/bin/python3.12" "$tmp/AppDir/usr/bin/python3.12"
docker cp "$container:/usr/lib/python3.12" "$tmp/AppDir/usr/lib/python3.12"
docker run --rm --entrypoint sh porchlight-motion:1.36.33 -c \
    "{ ldd /usr/bin/python3.12; find /usr/lib/python3.12 -type f -name '*.so' -exec ldd {} \; 2>/dev/null; } | awk '/=> \\// {print \$3}' | sort -u" \
    > "$tmp/libs"
while IFS= read -r lib; do
    case "$lib" in
        */libc.so.*|*/libm.so.*|*/libpthread.so.*|*/libdl.so.*|*/librt.so.*) continue ;;
    esac
    docker cp -L "$container:$lib" "$tmp/AppDir/usr/lib/x86_64-linux-gnu/${lib##*/}"
done < "$tmp/libs"

install -m 755 pkg/AppImage/AppRun "$tmp/AppDir/AppRun"
install -m 644 porchlight.desktop "$tmp/AppDir/porchlight.desktop"
install -m 644 porchlight.png "$tmp/AppDir/porchlight.png"
install -m 644 porchlight.png "$tmp/AppDir/usr/share/icons/hicolor/256x256/apps/porchlight.png"
install -m 644 server/*.py "$tmp/AppDir/usr/share/porchlight/server/"
install -m 644 web/index.html web/app.css web/app.js web/mark.svg web/manifest.json "$tmp/AppDir/usr/share/porchlight/web/"
install -m 644 logo48.png "$tmp/AppDir/usr/share/porchlight/web/logo.png"
install -m 644 models/nanodet-plus-m-416.onnx "$tmp/AppDir/usr/share/porchlight/models/"

tool="$tmp/appimagetool"
curl -L --fail --silent --show-error "$tool_url" -o "$tool"
echo "$tool_sha  $tool" | sha256sum -c -
chmod +x "$tool"
ARCH=x86_64 "$tool" "$tmp/AppDir" "dist/Porchlight-$version-x86_64.AppImage"
echo "built dist/Porchlight-$version-x86_64.AppImage"
