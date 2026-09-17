#!/bin/sh
# Build porchlight_2.3.2_all.deb
set -e
cd "$(dirname "$0")"
build_dir=${BUILD_DIR:-build}

rm -rf "$build_dir"
mkdir -p "$build_dir/DEBIAN" "$build_dir/usr/bin" "$build_dir/usr/lib/porchlight" \
         "$build_dir/usr/share/applications" \
         "$build_dir/usr/share/porchlight/server" "$build_dir/usr/share/porchlight/web" \
         "$build_dir/usr/share/porchlight/models" \
         "$build_dir/usr/share/doc/porchlight" \
         "$build_dir/usr/share/polkit-1/actions" \
         "$build_dir/usr/share/icons/hicolor/256x256/apps"

cp pkg/DEBIAN/control "$build_dir/DEBIAN/"
install -m 755 pkg/DEBIAN/postinst "$build_dir/DEBIAN/postinst"
install -m 755 launcher.sh "$build_dir/usr/bin/porchlight"
install -m 644 server/zmapi.py server/porchlight_server.py server/detect.py \
        server/find.py server/mqtt.py server/motion.py \
        "$build_dir/usr/share/porchlight/server/"
install -m 644 models/nanodet-plus-m-416.onnx "$build_dir/usr/share/porchlight/models/"
install -m 644 pkg/copyright "$build_dir/usr/share/doc/porchlight/copyright"
install -m 644 models/LICENSE "$build_dir/usr/share/doc/porchlight/NANODET-LICENSE"
install -m 644 web/index.html web/app.css web/app.js web/mark.svg web/manifest.json "$build_dir/usr/share/porchlight/web/"
install -m 644 logo48.png "$build_dir/usr/share/porchlight/web/logo.png"
install -m 755 push.sh "$build_dir/usr/share/porchlight/push.sh"
install -m 755 admin/porchlight-admin "$build_dir/usr/lib/porchlight/porchlight-admin"
install -m 644 admin/com.porchlight.policy "$build_dir/usr/share/polkit-1/actions/com.porchlight.policy"
install -m 644 porchlight.desktop "$build_dir/usr/share/applications/porchlight.desktop"
install -m 644 porchlight.png "$build_dir/usr/share/icons/hicolor/256x256/apps/porchlight.png"

dpkg-deb --root-owner-group --build "$build_dir" porchlight_2.3.2_all.deb
echo "built porchlight_2.3.2_all.deb"
