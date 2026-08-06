#!/bin/sh
# Build porchlight_2.1_all.deb
set -e
cd "$(dirname "$0")"

rm -rf build
mkdir -p build/DEBIAN build/usr/bin build/usr/lib/porchlight \
         build/usr/share/applications \
         build/usr/share/porchlight/server build/usr/share/porchlight/web \
         build/usr/share/porchlight/models \
         build/usr/share/doc/porchlight \
         build/usr/share/polkit-1/actions \
         build/usr/share/icons/hicolor/256x256/apps

cp pkg/DEBIAN/control build/DEBIAN/
install -m 755 pkg/DEBIAN/postinst build/DEBIAN/postinst
install -m 755 launcher.sh build/usr/bin/porchlight
install -m 644 server/zmapi.py server/porchlight_server.py server/detect.py \
        build/usr/share/porchlight/server/
install -m 644 models/nanodet-plus-m-416.onnx build/usr/share/porchlight/models/
install -m 644 pkg/copyright build/usr/share/doc/porchlight/copyright
install -m 644 models/LICENSE build/usr/share/doc/porchlight/NANODET-LICENSE
install -m 644 web/index.html web/app.css web/app.js web/manifest.json build/usr/share/porchlight/web/
install -m 644 logo48.png build/usr/share/porchlight/web/logo.png
install -m 755 push.sh build/usr/share/porchlight/push.sh
install -m 755 admin/porchlight-admin build/usr/lib/porchlight/porchlight-admin
install -m 644 admin/com.porchlight.policy build/usr/share/polkit-1/actions/com.porchlight.policy
install -m 644 porchlight.desktop build/usr/share/applications/porchlight.desktop
install -m 644 porchlight.png build/usr/share/icons/hicolor/256x256/apps/porchlight.png

dpkg-deb --root-owner-group --build build porchlight_2.1_all.deb
echo "built porchlight_2.1_all.deb"
