#!/usr/bin/env python3
"""Drive the real Porchlight UI through a short, repeatable hero sequence."""

import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8321/#cameras"

win = Gtk.Window()
win.set_default_size(1440, 900)
win.set_resizable(False)
view = WebKit2.WebView()
win.add(view)
win.show_all()
view.load_uri(url)


def js(script):
    view.run_javascript(script, None, None, None)
    return False


GLib.timeout_add(4500, js, "location.hash='live'")
GLib.timeout_add(6000, js, "liveLayout=document.querySelector('#live-layout'); liveLayout.value='1'; liveLayout.dispatchEvent(new Event('change'))")
GLib.timeout_add(9500, js, "document.querySelector('#live-grid .pane img')?.click()")
GLib.timeout_add(13000, js, "document.querySelector('#viewer button')?.click()")
GLib.timeout_add(14500, js, "location.hash='cameras'")
GLib.timeout_add(17000, Gtk.main_quit)

Gtk.main()
