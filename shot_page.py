#!/usr/bin/env python3
"""Render one page of the Porchlight UI to a PNG.

    DISPLAY=:99 python3 shot_page.py <url> <out.png> [seconds]

Epiphany can't start in a container (its dbus proxy dies), so drive WebKit
directly instead.
"""

import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

url, out = sys.argv[1], sys.argv[2]
settle = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0

win = Gtk.OffscreenWindow()
win.set_default_size(1440, 900)
view = WebKit2.WebView()
view.set_size_request(1440, 900)
win.add(view)
win.show_all()
view.load_uri(url)


def snap():
    view.get_snapshot(WebKit2.SnapshotRegion.FULL_DOCUMENT, WebKit2.SnapshotOptions.NONE,
                      None, done)
    return False


def done(v, res):
    surface = v.get_snapshot_finish(res)
    surface.write_to_png(out)
    Gtk.main_quit()


GLib.timeout_add_seconds(int(settle), snap)
Gtk.main()
