# PyInstaller spec for the WiFiDrop desktop app. Build with:  python build.py
import os
import sys

IS_LINUX = sys.platform.startswith("linux")
IS_WIN = os.name == "nt"

hidden = ["webview.platforms.gtk"] if IS_LINUX else ["webview.platforms.edgechromium", "webview.platforms.winforms"]
excludes = [
    "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "webview.platforms.qt", "webview.platforms.cef",
    "webview.platforms.android", "webview.platforms.cocoa", "lxml", "cryptography", "tornado",
    "PIL", "numpy", "tkinter", "gi.repository.Gst", "gi.repository.GstBase",
]
if not IS_LINUX:
    excludes += ["gi", "webview.platforms.gtk"]

a = Analysis(
    ["app.py"],
    datas=[("web", "web"), ("assets/icon.png", "assets")],
    hiddenimports=hidden,
    excludes=excludes,
)

if IS_LINUX:
    # Use the system's GTK 3 + WebKitGTK 4.1 instead of bundling this machine's copies:
    # bundled copies clash with the system WebKit helper processes and add ~250 MB.
    KEEP = ("libpython", "libffi", "libssl", "libcrypto", "libz.", "libbz2", "liblzma",
            "libexpat", "libmpdec", "libsqlite3", "libuuid")
    a.binaries = [b for b in a.binaries
                  if not ("/" not in b[0] and b[0].startswith("lib") and not b[0].startswith(KEEP))]
    a.datas = [d for d in a.datas
               if not d[0].startswith(("share/", "gi_typelibs/", "gio_modules/", "lib/"))]

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="WiFiDrop",
    console=False,
    icon="assets/icon.ico" if IS_WIN else None,
    upx=False,
)
