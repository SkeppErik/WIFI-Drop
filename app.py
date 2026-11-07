#!/usr/bin/env python3
"""WiFiDrop desktop app: runs the WiFiDrop server and shows its interface in a native window.

Closing the window hides WiFiDrop to the tray icon (when the desktop has a tray); Quit is in the tray
menu. Phones keep using the browser (scan the QR code in the window).

    WiFiDrop --send FILE...   hand files to WiFiDrop (starting it if needed) and choose a device for them
    WiFiDrop --hidden         start in the tray without a window (used when starting at login)

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
import json
import re
import subprocess
import threading
from urllib.parse import quote

import desktop
import wifidrop

GUI = "gtk" if sys.platform.startswith("linux") else "edgechromium" if IS_WIN else None
ICON_PNG = os.path.join(wifidrop.BASE_DIR, "assets", "icon.png")
ICON_ICO = os.path.join(wifidrop.BASE_DIR, "assets", "icon.ico")


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


def local_call(cfg, action, body):
    """POST to the WiFiDrop already running on this computer. Returns its JSON reply, or None."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", cfg["port"], timeout=5)
        conn.request("POST", "/api/local/" + action, json.dumps(body),
                     {"Content-Type": "application/json", "X-WiFiDrop-Key": cfg["key"]})
        resp = conn.getresponse()
        data = resp.read()
        return json.loads(data) if resp.status == 200 else None
    except (OSError, ValueError):
        return None


def hand_over(cfg, files):
    """If WiFiDrop is already running, give it our files (or just ask it to show its window). True if done."""
    if not already_running(cfg["port"]):
        return False
    if IS_WIN:
        desktop.win_allow_focus()  # we were started by the user, so we may pass on the right to the front
    reply = local_call(cfg, "stage" if files else "show", {"paths": files})
    if reply and reply.get("window"):
        if IS_WIN:
            time.sleep(0.3)
            desktop.win_focus_window()
        wifidrop.log("Handed over to the running WiFiDrop.")
        return True
    # A terminal-mode WiFiDrop (or an older version) has no window of its own: use the browser.
    url = f"http://127.0.0.1:{cfg['port']}/?k={quote(cfg['key'])}"
    wifidrop.log(f"WiFiDrop is already running; opening it in the browser: {url}")
    import webbrowser
    webbrowser.open(url)
    return True


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
    """Show the interface in a native window; returns when it is closed (Windows child process)."""
    import webview
    webview.create_window("WiFiDrop", url, width=1000, height=720, min_size=(420, 520))
    webview.start(gui=GUI, icon=ICON_PNG if os.path.exists(ICON_PNG) else None)


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


def run_in_browser(url, owns_server, wait=True):
    import webbrowser
    wifidrop.log(f"Opening in the browser: {url}")
    webbrowser.open(url)
    if wait:
        wait_until_quit(owns_server)


def find_edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            p = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.exists(p):
                return p
    return None


def run_edge_app(url, owns_server, wait=True):
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
    if wait and time.time() - t0 < 5:
        # Edge handed the window to an already-running Edge and returned at once:
        # we can't tell when it closes, so fall back to the "click OK to quit" box.
        wait_until_quit(owns_server)
    return True


class Desktop:
    """Hooks the server calls into: show the window, and the Settings switches."""

    def __init__(self, tray):
        self.tray = tray
        self.show_window = lambda: None

    def settings(self):
        return {"tray": self.tray, "autostart": desktop.autostart_enabled(),
                "context_menu": desktop.context_menu_enabled()}

    def update_settings(self, d):
        if "autostart" in d:
            desktop.set_autostart(bool(d["autostart"]))
        if "context_menu" in d:
            desktop.set_context_menu(bool(d["context_menu"]))


# ----------------------------------------------------------------------------- Linux

def linux_ui(url, state, hidden):
    """Native window (GTK) plus a tray icon. Closing the window hides it while the tray is there."""
    import webview
    tray_ok = desktop.linux_tray_available()
    if not tray_ok:
        wifidrop.log("No tray on this desktop: closing the window quits WiFiDrop.")
    dk = state.desktop = Desktop(tray_ok)
    win = webview.create_window("WiFiDrop", url, width=1000, height=720, min_size=(420, 520), hidden=hidden)
    quitting, told = [False], [False]

    # Window.show()/hide()/destroy() wait until the window has been shown once (never, if started hidden)
    # and would block when called on the GTK thread, so use the backend's non-waiting versions.
    def gtk():
        return win.gui

    def on_closing():
        if quitting[0] or not tray_ok:
            return True
        gtk().hide(win.uid)
        if not told[0]:
            told[0] = True
            wifidrop.notify("WiFiDrop is still running", "It keeps receiving files. Quit it from the tray icon.")
        return False

    def show():
        if win.gui:
            gtk().show(win.uid)
            gtk().restore(win.uid)

    def quit_app():
        quitting[0] = True
        gtk().destroy_window(win.uid)

    win.events.closing += on_closing
    dk.show_window = show

    def start_tray():
        from gi.repository import GLib
        tray = desktop.LinuxTray("WiFiDrop", ICON_PNG, show, [
            ("Open WiFiDrop", show),
            ("Received files", lambda: wifidrop.open_folder(state.cfg["download_dir"])),
            None,
            ("Quit WiFiDrop", quit_app),
        ])
        GLib.idle_add(lambda: tray.start() and False)

    webview.start(start_tray if tray_ok else None, gui=GUI, icon=ICON_PNG if os.path.exists(ICON_PNG) else None)


# ----------------------------------------------------------------------------- Windows

class WindowsUI:
    """The window runs in a child process, so a crash in the window engine can't take WiFiDrop down.
    If it fails, Edge in app mode is used, then the default browser."""

    def __init__(self, url, browser_only=False):
        self.url, self.browser_only = url, browser_only
        self.lock = threading.Lock()
        self.thread = None
        self.proc = None
        self.on_closed = None

    def open(self):
        if desktop.win_focus_window():
            return
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def _run(self):
        if self.browser_only:
            return run_in_browser(self.url, True, wait=False)
        cmd = [sys.executable] + ([] if FROZEN else [os.path.abspath(__file__)]) + ["--window-only", self.url]
        try:
            self.proc = subprocess.Popen(cmd)
            rc = self.proc.wait()
        except OSError as e:
            rc = f"not started: {e}"
        finally:
            self.proc = None
        if rc == 0:
            if self.on_closed:
                self.on_closed()
            return
        wifidrop.log(f"App window failed (exit code {rc}); trying Edge instead.")
        if not run_edge_app(self.url, True, wait=False):
            run_in_browser(self.url, True, wait=False)

    def close(self):
        p = self.proc
        if p:
            try:
                p.terminate()
            except OSError:
                pass


def windows_main(url, state, hidden, browser_only):
    """Tray icon on the main thread; the window comes and goes. Returns when Quit is chosen."""
    ui = WindowsUI(url, browser_only)
    dk = state.desktop = Desktop(True)
    dk.show_window = ui.open
    tray = desktop.WinTray("WiFiDrop", ICON_ICO, ui.open, [
        ("Open WiFiDrop", ui.open),
        ("Received files", lambda: wifidrop.open_folder(state.cfg["download_dir"])),
        None,
        ("Quit WiFiDrop", lambda: (ui.close(), tray.stop())),
    ])
    told = [False]

    def closed():
        if not told[0]:
            told[0] = True
            tray.notify("WiFiDrop is still running", "It keeps receiving files. Quit it from the tray icon.")

    ui.on_closed = closed
    wifidrop.NOTIFY_HOOK = tray.notify
    if not hidden:  # only once the tray works, so a failing tray can't leave two windows behind
        threading.Thread(target=lambda: tray.ready.wait(10) and ui.open(), daemon=True).start()
    tray.run()
    ui.close()


def windows_fallback(url, browser_only):
    """No tray icon (shouldn't happen): the old behaviour, closing the window quits WiFiDrop."""
    if browser_only:
        return run_in_browser(url, True)
    cmd = [sys.executable] + ([] if FROZEN else [os.path.abspath(__file__)]) + ["--window-only", url]
    try:
        rc = subprocess.run(cmd).returncode
    except OSError as e:
        rc = f"not started: {e}"
    if rc == 0:
        return
    wifidrop.log(f"App window failed (exit code {rc}); trying Edge instead.")
    if not run_edge_app(url, True):
        run_in_browser(url, True)


# ----------------------------------------------------------------------------- main

def main():
    ap = wifidrop.build_parser("WiFiDrop desktop app")
    ap.add_argument("--no-window", action="store_true", help="don't open an app window; use the browser")
    ap.add_argument("--hidden", action="store_true", help="start in the tray without opening the window")
    ap.add_argument("--send", nargs="+", metavar="FILE", help="send these files: choose a device in the window")
    ap.add_argument("--window-only", metavar="URL", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.window_only:  # child process on Windows: just the window
        try:
            run_window(args.window_only)
        except Exception as e:
            wifidrop.log(f"Window engine error: {e!r}")
            sys.exit(3)
        return

    files = [os.path.abspath(f) for f in (args.send or [])]
    try:
        cfg, _ = wifidrop.load_config(args)
    except ValueError as e:
        show_error(str(e))
        return
    if hand_over(cfg, files):
        return

    server = state = None
    try:
        server, state = wifidrop.start(args)
        cfg = state.cfg
    except OSError as e:
        # Several copies may start at once (one per file from Explorer): wait for the winner to answer.
        for _ in range(20):
            if hand_over(cfg, files):
                return
            time.sleep(0.5)
        show_error(f"Port {cfg['port']} is used by another program ({e}). "
                   "Start WiFiDrop with a different <code>--port</code>.")
        return

    url = f"http://127.0.0.1:{cfg['port']}/?k={quote(cfg['key'])}"
    wifidrop.log(f"Server on port {cfg['port']}")
    desktop.refresh()
    if files:
        state.stage_files(files)
    hidden = args.hidden and not files
    try:
        if IS_WIN:
            try:
                windows_main(url, state, hidden, args.no_window)
            except Exception as e:
                wifidrop.log(f"Tray icon unavailable ({e!r}); closing the window will quit WiFiDrop.")
                state.desktop = None
                wifidrop.NOTIFY_HOOK = None
                windows_fallback(url, args.no_window)
        elif args.no_window:
            run_in_browser(url, True)
        else:
            try:
                linux_ui(url, state, hidden)
            except Exception as e:  # e.g. no WebKitGTK available
                wifidrop.log(f"App window unavailable ({e}); using the browser instead.")
                state.desktop = None
                run_in_browser(url, True)
    except Exception:
        import traceback
        traceback.print_exc()
        raise
    finally:
        if server:
            wifidrop.stop(server, state)


if __name__ == "__main__":
    main()
