#!/usr/bin/env python3
"""WiFiDrop desktop app: runs the WiFiDrop server and shows its interface in a native window.

Closing the window quits WiFiDrop. Phones keep using the browser (scan the QR code in the window).

On Windows the window runs in a child process: if the WebView2/.NET window engine crashes (which kills
the process outright, past any try/except), WiFiDrop survives and falls back to Microsoft Edge in app
mode, then to the default browser. Everything is logged to %APPDATA%\\wifidrop\\wifidrop.log.
"""
import os
import sys
import time

IS_WIN = os.name == "nt"
FROZEN = getattr(sys, "frozen", False)


def _log_path():
    base = os.environ.get("WIFIDROP_CONFIG_DIR") or os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")), "wifidrop")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "wifidrop.log")


# A packaged GUI app on Windows has no console: send output (and native crash traces) to a log file.
if IS_WIN and (sys.stdout is None or FROZEN):
    try:
        _path = _log_path()
        if os.path.exists(_path) and os.path.getsize(_path) > 1_000_000:
            os.replace(_path, _path + ".old")
        _logf = open(_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = _logf
        import faulthandler
        faulthandler.enable(_logf)
        print(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} pid {os.getpid()} {' '.join(sys.argv[1:])}")
    except OSError:
        pass
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

if FROZEN and sys.platform.startswith("linux"):
    # The Linux build uses the system's GTK/WebKitGTK. Undo the paths PyInstaller points
    # at the bundle so GTK and WebKit's helper processes find their own system files.
    for var in ("GI_TYPELIB_PATH", "GIO_MODULE_DIR", "GSETTINGS_SCHEMA_DIR", "GST_PLUGIN_PATH",
                "GST_PLUGIN_SYSTEM_PATH", "GST_REGISTRY", "GDK_PIXBUF_MODULE_FILE", "GTK_DATA_PREFIX",
                "GTK_EXE_PREFIX", "GTK_PATH", "GST_PLUGIN_SCANNER"):
        os.environ.pop(var, None)
    orig = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig:
        os.environ["LD_LIBRARY_PATH"] = orig
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)

import argparse
import http.client
import re
import subprocess
from urllib.parse import quote

import wifidrop

GUI = "gtk" if sys.platform.startswith("linux") else "edgechromium" if IS_WIN else None


def already_running(port):
    """True if another WiFiDrop (app or terminal version) is answering on this port."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/ping")
        resp = conn.getresponse()
        resp.read()
        return resp.getheader("Server", "").startswith("WiFiDrop")
    except OSError:
        return False


def message_box(text, title="WiFiDrop", error=False):
    import ctypes
    flags = (0x10 if error else 0x40) | 0x40000  # MB_ICONERROR / MB_ICONINFORMATION, MB_TOPMOST
    ctypes.windll.user32.MessageBoxW(None, text, title, flags)


def show_error(message):
    wifidrop.log(f"Error: {message}")
    if IS_WIN:
        message_box("WiFiDrop couldn't start.\n\n" + re.sub(r"<[^>]+>", "", message), error=True)
        return
    import webview
    page = f"""<!doctype html><meta charset="utf-8">
<body style="font:15px/1.5 -apple-system,'Segoe UI',sans-serif;padding:28px;background:#f4f5f8;color:#16181d">
<h2 style="margin:0 0 8px">WiFiDrop couldn't start</h2><p>{message}</p></body>"""
    webview.create_window("WiFiDrop", html=page, width=480, height=240)
    webview.start(gui=GUI)


def run_window(url):
    """Show the interface in a native window; returns when it is closed."""
    import webview
    webview.create_window("WiFiDrop", url, width=1000, height=720, min_size=(420, 520))
    icon = os.path.join(wifidrop.BASE_DIR, "assets", "icon.png")
    webview.start(gui=GUI, icon=icon if os.path.exists(icon) else None)


def wait_until_quit(owns_server):
    """Keep the server alive with no app window of our own."""
    if not owns_server:
        return
    if IS_WIN:
        # A message box is the "window": WiFiDrop keeps running until it's closed.
        message_box("WiFiDrop is running and opened in your browser.\n\nClick OK to quit WiFiDrop.")
        return
    wifidrop.log("WiFiDrop is running. Press Ctrl+C to quit.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def run_in_browser(url, owns_server):
    import webbrowser
    wifidrop.log(f"Opening in the browser: {url}")
    webbrowser.open(url)
    wait_until_quit(owns_server)


def find_edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            p = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.exists(p):
                return p
    return None


def run_edge_app(url, owns_server):
    """Edge in app mode: a plain window without browser UI, no .NET needed. False if Edge is missing."""
    edge = find_edge()
    if not edge:
        wifidrop.log("Microsoft Edge not found.")
        return False
    profile = os.path.join(os.environ.get("LOCALAPPDATA", wifidrop.config_dir()), "wifidrop", "edge-window")
    cmd = [edge, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
           "--no-default-browser-check", "--window-size=1000,720"]
    wifidrop.log(f"Using Edge app window: {edge}")
    t0 = time.time()
    try:
        subprocess.run(cmd)
    except OSError as e:
        wifidrop.log(f"Edge failed to start: {e}")
        return False
    if time.time() - t0 < 5:
        # Edge handed the window to an already-running Edge and returned at once:
        # we can't tell when it closes, so fall back to the "click OK to quit" box.
        wait_until_quit(owns_server)
    return True


def windows_ui(url, owns_server):
    """Native window in a child process, so a crash in the window engine can't take WiFiDrop down."""
    cmd = [sys.executable] + ([] if FROZEN else [os.path.abspath(__file__)]) + ["--window-only", url]
    try:
        rc = subprocess.run(cmd).returncode
    except OSError as e:
        rc = f"not started: {e}"
    if rc == 0:
        return
    wifidrop.log(f"App window failed (exit code {rc}); trying Edge instead.")
    if not run_edge_app(url, owns_server):
        run_in_browser(url, owns_server)


def main():
    ap = wifidrop.build_parser("WiFiDrop desktop app")
    ap.add_argument("--no-window", action="store_true", help="don't open an app window; use the browser")
    ap.add_argument("--window-only", metavar="URL", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.window_only:  # child process on Windows: just the window
        try:
            run_window(args.window_only)
        except Exception as e:
            wifidrop.log(f"Window engine error: {e!r}")
            sys.exit(3)
        return

    server = state = None
    try:
        server, state = wifidrop.start(args)
        cfg = state.cfg
    except ValueError as e:
        show_error(str(e))
        return
    except OSError as e:
        cfg, _ = wifidrop.load_config(args)
        if not already_running(cfg["port"]):
            show_error(f"Port {cfg['port']} is used by another program ({e}). "
                       "Start WiFiDrop with a different <code>--port</code>.")
            return
        wifidrop.log("WiFiDrop is already running: opening another window for it.")

    url = f"http://127.0.0.1:{cfg['port']}/?k={quote(cfg['key'])}"
    wifidrop.log(f"Server on port {cfg['port']}")
    owns = server is not None
    try:
        if args.no_window:
            run_in_browser(url, owns)
        elif IS_WIN:
            windows_ui(url, owns)
        else:
            try:
                run_window(url)
            except Exception as e:  # e.g. no WebKitGTK available
                wifidrop.log(f"App window unavailable ({e}); using the browser instead.")
                run_in_browser(url, owns)
    except Exception:
        import traceback
        traceback.print_exc()
        raise
    finally:
        if server:
            wifidrop.stop(server, state)


if __name__ == "__main__":
    main()
