"""Desktop integration for the WiFiDrop app: tray icon, start at login, and "Send with WiFiDrop"
in the file manager's right-click menu. Standard library only (plus GTK's own GObject bindings on Linux).

    Linux:   tray icon over D-Bus (StatusNotifierItem + dbusmenu: KDE Plasma, GNOME with the AppIndicator
             extension, most other desktops); autostart in ~/.config/autostart; right-click entries for
             Dolphin, Nautilus (Files) and Nemo.
    Windows: tray icon through the Win32 API (ctypes); autostart in the registry Run key; right-click
             entry under HKEY_CURRENT_USER (Windows 11: "Show more options").
"""
import os
import shutil
import sys
import threading

IS_WIN = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")
FROZEN = getattr(sys, "frozen", False)
APP_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")


def launch_command(*extra):
    """How to start this WiFiDrop again (the built program, or Python + app.py when run from source)."""
    if FROZEN:
        cmd = [sys.executable]
    else:
        exe = sys.executable
        if IS_WIN:  # no console window flashing up at login
            w = os.path.join(os.path.dirname(exe), "pythonw.exe")
            exe = w if os.path.exists(w) else exe
        cmd = [exe, APP_SCRIPT]
    return cmd + list(extra)


# ----------------------------------------------------------------------------- Linux files

def _xdg(var, default):
    return os.environ.get(var) or os.path.expanduser(default)


def _desktop_quote(arg):
    """Quote one argument for an Exec= line (Desktop Entry spec)."""
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`='):
        return arg
    return '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'


def _exec_line(*extra):
    return " ".join(_desktop_quote(a) for a in launch_command()) + (" " + " ".join(extra) if extra else "")


def _write(path, text, executable=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if executable:
        os.chmod(path, 0o755)


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _linux_autostart_path():
    return os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), "autostart", "wifidrop.desktop")


def _linux_menu_paths():
    data = _xdg("XDG_DATA_HOME", "~/.local/share")
    return {
        "dolphin": os.path.join(data, "kio", "servicemenus", "wifidrop-send.desktop"),          # Plasma 6
        "dolphin5": os.path.join(data, "kservices5", "ServiceMenus", "wifidrop-send.desktop"),  # Plasma 5
        "nautilus": os.path.join(data, "nautilus", "scripts", "Send with WiFiDrop"),
        "nemo": os.path.join(data, "nemo", "actions", "wifidrop-send.nemo_action"),
    }


# ----------------------------------------------------------------------------- Windows registry

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MENU_KEY = r"Software\Classes\*\shell\WiFiDrop"


def _win_cmdline(*extra):
    import subprocess
    return subprocess.list2cmdline(launch_command()) + (" " + " ".join(extra) if extra else "")


def _reg_get(path, name=""):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _reg_delete_tree(path):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _reg_delete_tree(path + "\\" + sub)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        pass


# ----------------------------------------------------------------------------- public: autostart / menu

def autostart_enabled():
    if IS_WIN:
        return _reg_get(RUN_KEY, "WiFiDrop") is not None
    if IS_LINUX:
        return os.path.exists(_linux_autostart_path())
    return False


def set_autostart(on):
    """Start WiFiDrop (hidden, in the tray) when the user logs in."""
    if IS_WIN:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if on:
                winreg.SetValueEx(k, "WiFiDrop", 0, winreg.REG_SZ, _win_cmdline("--hidden"))
            else:
                try:
                    winreg.DeleteValue(k, "WiFiDrop")
                except OSError:
                    pass
    elif IS_LINUX:
        path = _linux_autostart_path()
        if on:
            _write(path, "[Desktop Entry]\nType=Application\nName=WiFiDrop\n"
                         "Comment=Send files between your devices on the same Wi-Fi\n"
                         f"Exec={_exec_line('--hidden')}\nIcon=wifidrop\nTerminal=false\n"
                         "X-GNOME-Autostart-enabled=true\n")
        else:
            _rm(path)


def context_menu_enabled():
    if IS_WIN:
        return _reg_get(MENU_KEY + r"\command") is not None
    if IS_LINUX:
        return any(os.path.exists(p) for p in _linux_menu_paths().values())
    return False


def set_context_menu(on):
    """Add or remove "Send with WiFiDrop" in the file manager's right-click menu."""
    if IS_WIN:
        import winreg
        if not on:
            _reg_delete_tree(MENU_KEY)
            return
        exe = launch_command()[0] if FROZEN else sys.executable
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, MENU_KEY) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "Send with WiFiDrop")
            winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, f"{exe},0")
            winreg.SetValueEx(k, "MultiSelectModel", 0, winreg.REG_SZ, "Player")  # also for 15+ files
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, MENU_KEY + r"\command") as k:
            # Explorer starts one process per selected file; they all hand their file to one window.
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, _win_cmdline("--send", '"%1"'))
    elif IS_LINUX:
        paths = _linux_menu_paths()
        if not on:
            for p in paths.values():
                _rm(p)
            return
        dolphin = ("[Desktop Entry]\nType=Service\nMimeType=all/allfiles;\nActions=wifidrop;\n"
                   "X-KDE-ServiceTypes=KonqPopupMenu/Plugin\nX-KDE-Priority=TopLevel\n\n"
                   "[Desktop Action wifidrop]\nName=Send with WiFiDrop\nIcon=wifidrop\n"
                   f"Exec={_exec_line('--send', '%F')}\n")
        nemo = ("[Nemo Action]\nName=Send with WiFiDrop\nComment=Send the selected files with WiFiDrop\n"
                f"Exec={_exec_line('--send', '%F')}\nIcon-Name=wifidrop\nSelection=notnone\nExtensions=nodirs;\n")
        import shlex
        nautilus = ("#!/bin/sh\n# Nautilus script: right-click > Scripts > Send with WiFiDrop\n"
                    f"exec {shlex.join(launch_command('--send'))} \"$@\"\n")
        wrote = False
        if shutil.which("dolphin"):
            _write(paths["dolphin"], dolphin, executable=True)  # Plasma 6 only runs executable service menus
            _write(paths["dolphin5"], dolphin)
            wrote = True
        if shutil.which("nautilus"):
            _write(paths["nautilus"], nautilus, executable=True)
            wrote = True
        if shutil.which("nemo"):
            _write(paths["nemo"], nemo)
            wrote = True
        if not wrote:
            raise OSError("No supported file manager found (Dolphin, Files/Nautilus or Nemo).")


def refresh():
    """Point already-enabled integrations at this copy of WiFiDrop (in case the program was moved)."""
    for enabled, setter in ((autostart_enabled, set_autostart), (context_menu_enabled, set_context_menu)):
        try:
            if enabled():
                setter(True)
        except OSError:
            pass


# ----------------------------------------------------------------------------- Linux tray (D-Bus)

SNI_XML = """<node>
 <interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg name="status" type="s"/></signal>
 </interface>
</node>"""

MENU_XML = """<node>
 <interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
   <arg name="parentId" type="i" direction="in"/><arg name="recursionDepth" type="i" direction="in"/>
   <arg name="propertyNames" type="as" direction="in"/>
   <arg name="revision" type="u" direction="out"/><arg name="layout" type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
   <arg name="ids" type="ai" direction="in"/><arg name="propertyNames" type="as" direction="in"/>
   <arg name="properties" type="a(ia{sv})" direction="out"/>
  </method>
  <method name="GetProperty">
   <arg name="id" type="i" direction="in"/><arg name="name" type="s" direction="in"/>
   <arg name="value" type="v" direction="out"/>
  </method>
  <method name="Event">
   <arg name="id" type="i" direction="in"/><arg name="eventId" type="s" direction="in"/>
   <arg name="data" type="v" direction="in"/><arg name="timestamp" type="u" direction="in"/>
  </method>
  <method name="EventGroup">
   <arg name="events" type="a(isvu)" direction="in"/><arg name="idErrors" type="ai" direction="out"/>
  </method>
  <method name="AboutToShow">
   <arg name="id" type="i" direction="in"/><arg name="needUpdate" type="b" direction="out"/>
  </method>
  <method name="AboutToShowGroup">
   <arg name="ids" type="ai" direction="in"/>
   <arg name="updatesNeeded" type="ai" direction="out"/><arg name="idErrors" type="ai" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated">
   <arg name="updatedProps" type="a(ia{sv})"/><arg name="removedProps" type="a(ias)"/>
  </signal>
  <signal name="LayoutUpdated"><arg name="revision" type="u"/><arg name="parent" type="i"/></signal>
 </interface>
</node>"""


def linux_tray_available():
    """True if the desktop shows tray icons (a StatusNotifierWatcher is on the session bus)."""
    try:
        import gi
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        r = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                          "NameHasOwner", GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
                          GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 2000, None)
        return bool(r.unpack()[0])
    except Exception:
        return False


class LinuxTray:
    """A tray icon with a menu. items: list of (label, callback) or None for a separator.
    Must be started on the GTK main thread (callbacks run there too)."""

    def __init__(self, title, icon_png, on_activate, items):
        self.title, self.icon_png, self.on_activate = title, icon_png, on_activate
        self.items = items
        self.bus = None
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.revision = 1

    def start(self):
        from gi.repository import Gio, GLib
        self.GLib = GLib
        self.pixmaps = self._pixmaps()
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.bus.register_object("/StatusNotifierItem", sni, self._sni_call, self._sni_prop, None)
        self.bus.register_object("/MenuBar", menu, self._menu_call, self._menu_prop, None)
        Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        self._register()
        # Re-register if the panel restarts (e.g. plasmashell crash).
        Gio.bus_watch_name_on_connection(self.bus, "org.kde.StatusNotifierWatcher",
                                         Gio.BusNameWatcherFlags.NONE, lambda *a: self._register(), None)

    def _register(self):
        try:
            self.bus.call_sync("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                               "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                               self.GLib.Variant("(s)", (self.name,)), None, 0, 3000, None)
        except Exception as e:
            print(f"Tray icon not registered: {e}", flush=True)

    def _pixmaps(self):
        """Icon as ARGB32 (network byte order) pixmaps, which every tray host understands."""
        out = []
        try:
            import gi
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf
            for size in (22, 32, 64):
                pb = GdkPixbuf.Pixbuf.new_from_file_at_size(self.icon_png, size, size)
                if not pb.get_has_alpha():
                    pb = pb.add_alpha(False, 0, 0, 0)
                w, h, stride, px = pb.get_width(), pb.get_height(), pb.get_rowstride(), pb.get_pixels()
                argb = bytearray(w * h * 4)
                i = 0
                for y in range(h):
                    row = y * stride
                    for x in range(w):
                        r, g, b, a = px[row + x * 4: row + x * 4 + 4]
                        argb[i:i + 4] = bytes((a, r, g, b))
                        i += 4
                out.append((w, h, bytes(argb)))
        except Exception as e:
            print(f"Tray icon image not loaded: {e}", flush=True)
        return out

    # --- org.kde.StatusNotifierItem
    def _sni_prop(self, conn, sender, path, iface, prop):
        V = self.GLib.Variant
        return {
            "Category": lambda: V("s", "ApplicationStatus"),
            "Id": lambda: V("s", "wifidrop"),
            "Title": lambda: V("s", self.title),
            "Status": lambda: V("s", "Active"),
            "WindowId": lambda: V("i", 0),
            "IconName": lambda: V("s", ""),
            "IconPixmap": lambda: V("a(iiay)", self.pixmaps),
            "ToolTip": lambda: V("(sa(iiay)ss)", ("", [], self.title, "")),
            "ItemIsMenu": lambda: V("b", False),
            "Menu": lambda: V("o", "/MenuBar"),
        }[prop]()

    def _sni_call(self, conn, sender, path, iface, method, params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            self.on_activate()
        invocation.return_value(None)

    # --- com.canonical.dbusmenu
    def _menu_prop(self, conn, sender, path, iface, prop):
        V = self.GLib.Variant
        return {"Version": lambda: V("u", 3), "TextDirection": lambda: V("s", "ltr"),
                "Status": lambda: V("s", "normal"), "IconThemePath": lambda: V("as", [])}[prop]()

    def _item_props(self, i):
        V = self.GLib.Variant
        if i == 0:
            return {"children-display": V("s", "submenu")}
        item = self.items[i - 1]
        if item is None:
            return {"type": V("s", "separator")}
        return {"label": V("s", item[0]), "enabled": V("b", True), "visible": V("b", True)}

    def _layout(self):
        V = self.GLib.Variant
        children = [V("(ia{sv}av)", (i, self._item_props(i), [])) for i in range(1, len(self.items) + 1)]
        return (0, self._item_props(0), children)

    def _menu_call(self, conn, sender, path, iface, method, params, invocation):
        V = self.GLib.Variant
        args = params.unpack()
        if method == "GetLayout":
            invocation.return_value(V("(u(ia{sv}av))", (self.revision, self._layout())))
        elif method == "GetGroupProperties":
            ids = args[0] or list(range(len(self.items) + 1))
            props = [(i, self._item_props(i)) for i in ids if 0 <= i <= len(self.items)]
            invocation.return_value(V("(a(ia{sv}))", (props,)))
        elif method == "GetProperty":
            invocation.return_value(V("(v)", (self._item_props(args[0]).get(args[1], V("s", "")),)))
        elif method == "Event":
            invocation.return_value(None)
            if args[1] == "clicked":
                self._clicked(args[0])
        elif method == "EventGroup":
            for i, event, _data, _ts in args[0]:
                if event == "clicked":
                    self._clicked(i)
            invocation.return_value(V("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(V("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(V("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _clicked(self, i):
        if 1 <= i <= len(self.items) and self.items[i - 1]:
            self.GLib.idle_add(lambda: self.items[i - 1][1]() and False)


# ----------------------------------------------------------------------------- Windows tray (Win32)

class WinTray:
    """Notification-area icon with a right-click menu, on its own hidden window and message loop.
    run() blocks until stop() is called (from any thread)."""

    WM_TRAY = 0x8000 + 1  # WM_APP + 1
    WM_STOP = 0x8000 + 2
    WM_BALLOON = 0x8000 + 3

    def __init__(self, title, icon_ico, on_activate, items):
        self.title, self.icon_ico, self.on_activate, self.items = title, icon_ico, on_activate, items
        self.hwnd = None
        self.ready = threading.Event()
        self._balloon = None

    def _api(self):
        import ctypes
        from ctypes import wintypes as w
        self.ct, self.w = ctypes, w
        u32, s32, k32 = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
        LRESULT = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", w.UINT), ("style", w.UINT), ("lpfnWndProc", self.WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE),
                        ("hIcon", w.HICON), ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                        ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR), ("hIconSm", w.HICON)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                        ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                        ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                        ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", w.HICON)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", w.LONG), ("y", w.LONG)]

        self.WNDCLASSEXW, self.NOTIFYICONDATAW, self.POINT = WNDCLASSEXW, NOTIFYICONDATAW, POINT
        u32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        u32.DefWindowProcW.restype = LRESULT
        u32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
        u32.CreateWindowExW.restype = w.HWND
        u32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        u32.RegisterClassExW.restype = w.ATOM
        u32.RegisterWindowMessageW.argtypes = [w.LPCWSTR]
        u32.RegisterWindowMessageW.restype = w.UINT
        u32.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
        u32.LoadImageW.restype = w.HANDLE
        u32.LoadIconW.argtypes = [w.HINSTANCE, w.LPVOID]
        u32.LoadIconW.restype = w.HICON
        u32.CreatePopupMenu.restype = w.HMENU
        u32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
        u32.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.LPVOID]
        u32.DestroyMenu.argtypes = [w.HMENU]
        u32.SetForegroundWindow.argtypes = [w.HWND]
        u32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        u32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
        u32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
        u32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
        u32.DestroyWindow.argtypes = [w.HWND]
        u32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
        s32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        k32.GetModuleHandleW.argtypes = [w.LPCWSTR]
        k32.GetModuleHandleW.restype = w.HMODULE
        self.u32, self.s32, self.k32 = u32, s32, k32

    def _nid(self, flags):
        nid = self.NOTIFYICONDATAW()
        nid.cbSize = self.ct.sizeof(self.NOTIFYICONDATAW)
        nid.hWnd, nid.uID, nid.uFlags = self.hwnd, 1, flags
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = self.title[:127]
        return nid

    def _add_icon(self):
        NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
        self.s32.Shell_NotifyIconW(0, self.ct.byref(self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)))  # NIM_ADD

    def _show_menu(self):
        u32 = self.u32
        menu = u32.CreatePopupMenu()
        for i, item in enumerate(self.items, 1):
            if item is None:
                u32.AppendMenuW(menu, 0x800, 0, None)  # MF_SEPARATOR
            else:
                u32.AppendMenuW(menu, 0, i, item[0])
        pt = self.POINT()
        u32.GetCursorPos(self.ct.byref(pt))
        u32.SetForegroundWindow(self.hwnd)  # so the menu closes when clicking elsewhere
        cmd = u32.TrackPopupMenu(menu, 0x0100 | 0x0002, pt.x, pt.y, 0, self.hwnd, None)  # RETURNCMD|RIGHTBUTTON
        u32.PostMessageW(self.hwnd, 0, 0, 0)
        u32.DestroyMenu(menu)
        if 1 <= cmd <= len(self.items) and self.items[cmd - 1]:
            threading.Thread(target=self.items[cmd - 1][1], daemon=True).start()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        WM_DESTROY, WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0002, 0x0202, 0x0205, 0x007B
        if msg == self.WM_TRAY:
            if lparam in (WM_LBUTTONUP, 0x0203):  # click or double-click
                threading.Thread(target=self.on_activate, daemon=True).start()
            elif lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._show_menu()
            return 0
        if msg == self.WM_BALLOON and self._balloon:
            title, text = self._balloon
            nid = self._nid(0x10)  # NIF_INFO
            nid.szInfoTitle, nid.szInfo, nid.dwInfoFlags = title[:63], text[:255], 1  # NIIF_INFO
            self.s32.Shell_NotifyIconW(1, self.ct.byref(nid))  # NIM_MODIFY
            return 0
        if msg == self.taskbar_created:  # Explorer restarted: put the icon back
            self._add_icon()
            return 0
        if msg == self.WM_STOP:
            self.u32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            self.s32.Shell_NotifyIconW(2, self.ct.byref(self._nid(0)))  # NIM_DELETE
            self.u32.PostQuitMessage(0)
            return 0
        return self.u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self):
        self._api()
        u32, ct = self.u32, self.ct
        hinst = self.k32.GetModuleHandleW(None)
        self._proc = self.WNDPROC(self._wndproc)  # keep a reference: Windows calls it
        wc = self.WNDCLASSEXW()
        wc.cbSize = ct.sizeof(self.WNDCLASSEXW)
        wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self._proc, hinst, "WiFiDropTray"
        u32.RegisterClassExW(ct.byref(wc))
        self.taskbar_created = u32.RegisterWindowMessageW("TaskbarCreated")
        self.hwnd = u32.CreateWindowExW(0, "WiFiDropTray", "WiFiDrop tray", 0, 0, 0, 0, 0, None, None, hinst, None)
        self.hicon = None
        if self.icon_ico and os.path.exists(self.icon_ico):
            self.hicon = u32.LoadImageW(None, self.icon_ico, 1, 0, 0, 0x10 | 0x40)  # ICON, LOADFROMFILE|DEFAULTSIZE
        if not self.hicon:
            self.hicon = u32.LoadIconW(None, ct.c_void_p(32512))  # IDI_APPLICATION
        self._add_icon()
        self.ready.set()
        msg = self.w.MSG()
        while u32.GetMessageW(ct.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ct.byref(msg))
            u32.DispatchMessageW(ct.byref(msg))

    def notify(self, title, text):
        if self.hwnd:
            self._balloon = (title, text)
            self.u32.PostMessageW(self.hwnd, self.WM_BALLOON, 0, 0)

    def stop(self):
        if self.hwnd:
            self.u32.PostMessageW(self.hwnd, self.WM_STOP, 0, 0)


def win_focus_window(title="WiFiDrop"):
    """Bring a top-level window with this exact title to the front. False if there is none."""
    import ctypes
    from ctypes import wintypes as w
    u32 = ctypes.windll.user32
    u32.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]
    u32.FindWindowW.restype = w.HWND
    u32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
    u32.SetForegroundWindow.argtypes = [w.HWND]
    u32.IsIconic.argtypes = [w.HWND]
    hwnd = u32.FindWindowW(None, title)
    if not hwnd:
        return False
    u32.ShowWindow(hwnd, 9 if u32.IsIconic(hwnd) else 5)  # SW_RESTORE / SW_SHOW
    u32.SetForegroundWindow(hwnd)
    return True


def win_allow_focus():
    """Let another process (the running WiFiDrop) bring its window to the front."""
    try:
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    except Exception:
        pass
