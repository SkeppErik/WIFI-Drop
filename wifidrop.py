#!/usr/bin/env python3
"""WiFiDrop - AirDrop-style file sharing between devices on the same Wi-Fi.

Run this on every computer. Phones/tablets just open the printed URL in a browser.
Only the Python standard library is used, so it runs anywhere Python 3.8+ does.
All network traffic is encrypted with TLS (see certs.py); only a window on this computer itself
talks plain HTTP, over 127.0.0.1.
"""
import argparse
import hashlib
import hmac
import http.client
import json
import os
import platform
import queue
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from html import escape
from html.parser import HTMLParser
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlparse

import certs

APP = "wifidrop"
DISCOVERY_PORT = 8766
BEACON_INTERVAL = 3      # seconds between discovery broadcasts
PEER_TTL = 10            # forget a computer after this many silent seconds
INBOX_TTL = 30 * 60      # files waiting for a browser to download are kept this long
CHUNK = 1024 * 1024
NOTE_MAX_CHARS = 50_000  # longest text a single note may hold
NOTE_MAX_LIVE = 200      # older notes are removed beyond this many
TOMBSTONE_TTL = 30 * 86400  # how long deletions are remembered (so a stale computer can't revive them)
NOTE_SYNC_GAP = 5        # min seconds between catch-up syncs with the same computer
PAD_MAX_HTML = 1_000_000            # the shared text page, as sanitized HTML
PAD_IMAGE_MAX = 15 * 1024 * 1024    # one image on the shared text page
PAD_IMAGE_KEEP = 7 * 86400          # unused images are deleted after this long
PAD_IMAGE_RE = re.compile(r"[0-9a-f]{64}\.(?:png|jpg|gif|webp)")
# When packaged with PyInstaller, bundled files live in sys._MEIPASS.
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")
KEY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes (0/o, 1/l/i)
_UMASK = os.umask(0)
os.umask(_UMASK)


# ----------------------------------------------------------------------------- config

def config_dir():
    if os.environ.get("WIFIDROP_CONFIG_DIR"):
        return os.environ["WIFIDROP_CONFIG_DIR"]
    if os.name == "nt":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, APP)


def new_key():
    return "-".join("".join(secrets.choice(KEY_ALPHABET) for _ in range(4)) for _ in range(3))


def norm_key(k):
    """Keys are compared ignoring case, dashes and spaces - easier to type on a phone."""
    return re.sub(r"[^a-z0-9]", "", (k or "").lower())


def load_config(args):
    path = os.path.join(config_dir(), "config.json")
    cfg = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    defaults = {
        "id": uuid.uuid4().hex[:12],
        "name": socket.gethostname(),
        "key": new_key(),
        "port": 8765,
        "download_dir": os.path.join(os.path.expanduser("~"), "Downloads", "WiFiDrop"),
    }
    changed = False
    for k, v in defaults.items():
        if k not in cfg:
            cfg[k], changed = v, True
    for k in ("name", "key", "port", "download_dir"):
        v = getattr(args, k, None)
        if v is not None and cfg.get(k) != v:
            cfg[k], changed = v, True
    if len(norm_key(cfg["key"])) < 8:
        raise ValueError("The network key must contain at least 8 letters/digits.")
    if changed:
        save_config(cfg, path)
    return cfg, path


def save_config(cfg, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def platform_name():
    s = platform.system()
    return {"Darwin": "mac", "Windows": "windows"}.get(s, s.lower() or "unknown")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ----------------------------------------------------------------------------- files

_BAD_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_name(name):
    name = name.replace("\\", "/").split("/")[-1]
    name = _BAD_CHARS.sub("_", name).strip(" .")[:200]
    if not name:
        name = "file"
    if name.split(".")[0].lower() in _WIN_RESERVED:
        name = "_" + name
    return name


def unique_path(directory, name):
    stem, ext = os.path.splitext(name)
    path, n = os.path.join(directory, name), 1
    while os.path.exists(path):
        path = os.path.join(directory, f"{stem} ({n}){ext}")
        n += 1
    return path


def copy_exact(src, dst, length):
    remaining = length
    while remaining > 0:
        chunk = src.read(min(CHUNK, remaining))
        if not chunk:
            raise IOError("transfer was interrupted")
        dst.write(chunk)
        remaining -= len(chunk)


def save_upload(src, length, directory, name):
    """Stream an upload to a .part file and only give it its real name once complete."""
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".wifidrop-", suffix=".part", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            copy_exact(src, f, length)
        os.chmod(tmp, 0o666 & ~_UMASK)  # mkstemp creates 0600; use normal file permissions
        final = unique_path(directory, name)
        os.replace(tmp, final)
        return final
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def notify(title, body):
    try:
        if sys.platform.startswith("linux") and shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "WiFiDrop", title, body])
        elif sys.platform == "darwin":
            script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
            subprocess.Popen(["osascript", "-e", script])
    except Exception:
        pass


def open_folder(path):
    os.makedirs(path, exist_ok=True)
    if os.name == "nt":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def qr_svg(text):
    """QR code as SVG, or None if the optional qrcode package is missing."""
    try:
        import io
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return None
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ----------------------------------------------------------------------------- state

# ----------------------------------------------------------------------------- noteboard

class NoteBoard:
    """Shared text notes. Every computer keeps a full copy and they sync with each other.

    Notes never change after they're posted; deleting one leaves a "tombstone" so the
    deletion spreads too. Merging is a plain union (deleted wins), so copies always converge.
    """

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.notes = {}  # id -> {id, text, from, ts, deleted}
        try:
            with open(path, encoding="utf-8") as f:
                self._merge(json.load(f).get("notes", []))
        except (OSError, ValueError, AttributeError):
            pass

    @staticmethod
    def _clean(n):
        """Validate a note that came from elsewhere; None if it's malformed."""
        try:
            nid = str(n["id"])
            if not re.fullmatch(r"[\w-]{1,40}", nid):
                return None
            deleted = bool(n.get("deleted"))
            return {"id": nid, "ts": float(n["ts"]), "deleted": deleted,
                    "from": "" if deleted else str(n.get("from", ""))[:40],
                    "text": "" if deleted else str(n.get("text", ""))[:NOTE_MAX_CHARS]}
        except (KeyError, TypeError, ValueError):
            return None

    def _merge(self, incoming):
        changed = False
        for n in incoming if isinstance(incoming, list) else []:
            n = self._clean(n) if isinstance(n, dict) else None
            if not n:
                continue
            old = self.notes.get(n["id"])
            if old is None or (n["deleted"] and not old["deleted"]):
                self.notes[n["id"]] = n
                changed = True
        return self._prune() or changed

    def _prune(self):
        changed = False
        live = sorted((n for n in self.notes.values() if not n["deleted"]),
                      key=lambda n: (n["ts"], n["id"]), reverse=True)
        for n in live[NOTE_MAX_LIVE:]:
            n.update(deleted=True, text="", **{"from": ""})
            changed = True
        cutoff = time.time() - TOMBSTONE_TTL
        for nid in [n["id"] for n in self.notes.values() if n["deleted"] and n["ts"] < cutoff]:
            del self.notes[nid]
            changed = True
        return changed

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"notes": list(self.notes.values())}, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log(f"Warning: couldn't save notes ({e})")

    # --- public API (all thread-safe)
    def add(self, text, sender):
        text = text.replace("\r\n", "\n")[:NOTE_MAX_CHARS]
        if not text.strip():
            raise ValueError("The note is empty.")
        n = {"id": secrets.token_urlsafe(9), "ts": time.time(), "deleted": False,
             "from": sender[:40], "text": text}
        with self.lock:
            self.notes[n["id"]] = n
            self._prune()
            self._save()
        return n

    def delete(self, ids):
        with self.lock:
            hit = [self.notes[i] for i in ids if i in self.notes and not self.notes[i]["deleted"]]
            for n in hit:
                n.update(deleted=True, text="", **{"from": ""})
            if hit:
                self._save()
        return bool(hit)

    def merge(self, incoming):
        with self.lock:
            changed = self._merge(incoming)
            if changed:
                self._save()
        return changed

    def all(self):
        """Everything, including tombstones (for syncing)."""
        with self.lock:
            return [dict(n) for n in self.notes.values()]

    def visible(self):
        with self.lock:
            live = [dict(n) for n in self.notes.values() if not n["deleted"]]
        live.sort(key=lambda n: (n["ts"], n["id"]), reverse=True)
        for n in live:
            del n["deleted"]
        return live

    def digest(self):
        """Short fingerprint of the board, sent in discovery beacons to spot computers that differ."""
        with self.lock:
            items = sorted(f"{i}{'x' if n['deleted'] else ''}" for i, n in self.notes.items())
        return hashlib.sha256(",".join(items).encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------- shared text page

class _PadCleaner(HTMLParser):
    """Keeps only simple formatting and WiFiDrop's own images: every device renders this HTML."""
    KEEP = {"p", "div", "br", "b", "strong", "i", "em", "u", "s", "strike", "h1", "h2", "h3",
            "ul", "ol", "li", "blockquote", "pre", "code", "a", "img"}
    VOID = {"br", "img"}
    DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "template", "svg", "math",
                         "head", "title", "noscript", "textarea", "select", "button"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.stack, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.DROP_WITH_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in self.KEEP:
            return
        a = dict(attrs)
        if tag == "img":
            src = a.get("src") or ""
            m = re.fullmatch(r"/api/pad/img/(" + PAD_IMAGE_RE.pattern + ")", src)
            if m:
                self.out.append(f'<img src="{escape(src)}" alt="">')
            return
        if tag == "br":
            self.out.append("<br>")
            return
        if tag == "a":
            href = (a.get("href") or "").strip()
            if re.match(r"(?i)(https?:|mailto:)", href):
                self.out.append(f'<a href="{escape(href)}">')
            else:
                self.out.append("<a>")
        else:
            self.out.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID and not self.skip and tag in self.KEEP and self.stack and self.stack[-1] == tag:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.DROP_WITH_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag in self.VOID or tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(escape(data, quote=False))

    def result(self):
        self.close()
        return "".join(self.out) + "".join(f"</{t}>" for t in reversed(self.stack))


def clean_pad_html(html):
    c = _PadCleaner()
    c.feed(str(html)[:PAD_MAX_HTML * 2])
    out = c.result()
    if len(out) > PAD_MAX_HTML:
        raise ValueError("The text is too long.")
    return out


def image_ext(data):
    """File type from the image's own bytes (not from what the sender claims)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


class Pad:
    """One shared page of text and images that anyone can edit. Every computer keeps a copy.

    The newest save wins (ordered by time, then by who saved it). Images are separate files named
    after their SHA-256, so any computer can fetch a missing one from another and check it.
    """

    def __init__(self, directory):
        self.path = os.path.join(directory, "pad.json")
        self.img_dir = os.path.join(directory, "pad-images")
        self.lock = threading.Lock()
        self.doc = {"html": "", "ts": 0.0, "by": ""}
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.doc = {"html": clean_pad_html(d["html"]), "ts": float(d["ts"]), "by": str(d["by"])[:64]}
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def get(self):
        with self.lock:
            return dict(self.doc)

    def digest(self):
        with self.lock:
            return hashlib.sha256(f"{self.doc['ts']!r}|{self.doc['by']}".encode()).hexdigest()[:16]

    def edit(self, html, by):
        html = clean_pad_html(html)
        with self.lock:
            ts = max(time.time(), self.doc["ts"] + 0.001)  # always newer than what we have
            self.doc = {"html": html, "ts": ts, "by": str(by)[:64]}
            self._save()
            return dict(self.doc)

    def merge(self, d):
        """Take another computer's copy if it is newer. True if ours changed."""
        try:
            new = {"html": clean_pad_html(d["html"]), "ts": float(d["ts"]), "by": str(d["by"])[:64]}
        except (ValueError, KeyError, TypeError):
            return False
        with self.lock:
            if (new["ts"], new["by"]) <= (self.doc["ts"], self.doc["by"]):
                return False
            self.doc = new
            self._save()
            return True

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.doc, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log(f"Warning: couldn't save the shared text ({e})")
        self._clean_images()

    # --- images
    def referenced(self):
        return set(re.findall(r"/api/pad/img/(" + PAD_IMAGE_RE.pattern + ")", self.doc["html"]))

    def image_path(self, name):
        return os.path.join(self.img_dir, name) if PAD_IMAGE_RE.fullmatch(name) else None

    def add_image(self, data):
        ext = image_ext(data)
        if not ext:
            raise ValueError("Only PNG, JPEG, GIF and WebP images can be added.")
        name = f"{hashlib.sha256(data).hexdigest()}.{ext}"
        path = self.image_path(name)
        if not os.path.exists(path):
            os.makedirs(self.img_dir, exist_ok=True)
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        return name

    def _clean_images(self):
        """Delete images no longer on the page (after a grace period, so other devices and undo still work)."""
        try:
            names = os.listdir(self.img_dir)
        except OSError:
            return
        used, cutoff = self.referenced(), time.time() - PAD_IMAGE_KEEP
        for n in names:
            p = os.path.join(self.img_dir, n)
            try:
                if n not in used and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass


class State:
    def __init__(self, cfg, cfg_path):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self._set_key(cfg["key"])
        self.lock = threading.Lock()
        self.peers = {}    # daemon id -> {id, name, ip, port, platform, seen}
        self.clients = {}  # browser id -> {id, name, device, q, local}
        self.inbox = {}    # offer id -> {id, path, name, size, from, to, sender_client, created, ack}
        self.inbox_dir = tempfile.mkdtemp(prefix="wifidrop-inbox-")
        self.board = NoteBoard(os.path.join(os.path.dirname(cfg_path), "notes.json"))
        self.note_sync_at = {}  # peer id -> last catch-up sync time
        self.pad = Pad(os.path.dirname(cfg_path))
        self.pad_sync_at = {}
        self.tls = certs.TLS(os.path.join(os.path.dirname(cfg_path), "tls"), cfg["name"])
        self.tls.refresh(lan_ip())

    # --- auth / settings
    def _set_key(self, key):
        self.key = norm_key(key)
        self.session_token = hmac.new(self.key.encode(), b"wifidrop-session", hashlib.sha256).hexdigest()

    def key_ok(self, k):
        return hmac.compare_digest(norm_key(k).encode(), self.key.encode())

    def phone_url(self):
        return f"https://{lan_ip()}:{self.cfg['port']}/?k={self.cfg['key']}"

    def update_settings(self, name=None, key=None):
        if name is not None:
            name = name.strip()[:40]
            if not name:
                raise ValueError("The name can't be empty.")
        if key is not None:
            key = key.strip()
            if len(norm_key(key)) < 8:
                raise ValueError("The network key must contain at least 8 letters/digits.")
        key_changed = key is not None and norm_key(key) != self.key
        with self.lock:
            if name is not None:
                self.cfg["name"] = name
            if key is not None:
                self.cfg["key"] = key
            if key_changed:
                self._set_key(key)
                self.peers.clear()  # computers using the old key are no longer ours
            clients = list(self.clients.values())
            save_config(self.cfg, self.cfg_path)
        if key_changed:
            for c in clients:  # every browser must log in again with the new key
                c["q"].put(None)
        else:
            self.push_peers_all()
        return key_changed

    # --- computers (other daemons)
    def upsert_peer(self, msg, ip):
        with self.lock:
            old = self.peers.get(msg["id"])
            info = {"id": msg["id"], "name": str(msg.get("name", "?"))[:40], "ip": ip,
                    "port": int(msg["port"]), "platform": str(msg.get("platform", ""))[:16],
                    "fp": str(msg["tls"])}
            changed = old is None or any(old[k] != info[k] for k in ("name", "ip", "port"))
            info["seen"] = time.time()
            self.peers[msg["id"]] = info
        if changed:
            self.push_peers_all()
        return old is None

    def get_peer(self, pid):
        with self.lock:
            return dict(self.peers[pid]) if pid in self.peers else None

    def expire(self):
        now = time.time()
        with self.lock:
            gone = [p for p, v in self.peers.items() if now - v["seen"] > PEER_TTL]
            for p in gone:
                del self.peers[p]
            old_offers = [o for o in self.inbox.values() if now - o["created"] > INBOX_TTL]
            for o in old_offers:
                del self.inbox[o["id"]]
        for o in old_offers:
            _rm(o["path"])
        if gone:
            self.push_peers_all()

    # --- browsers
    def add_client(self, c):
        with self.lock:
            old = self.clients.get(c["id"])
            self.clients[c["id"]] = c
            pending = [o for o in self.inbox.values() if o["to"] == c["id"] and not o["ack"]]
        if old:
            old["q"].put(None)  # same tab reconnected: close the stale stream
        self.push_peers_all()
        c["q"].put(("notes", self.board.visible()))
        c["q"].put(("pad", self.pad.get()))
        for o in pending:
            c["q"].put(("offer", offer_public(o)))

    def remove_client(self, c):
        with self.lock:
            if self.clients.get(c["id"]) is not c:
                return
            del self.clients[c["id"]]
        self.push_peers_all()

    def get_client(self, cid):
        with self.lock:
            return self.clients.get(cid)

    def peers_for(self, c):
        with self.lock:
            out = []
            if not c["local"]:
                out.append({"id": "self", "name": self.cfg["name"], "kind": "computer",
                            "device": "computer", "platform": platform_name()})
            for p in sorted(self.peers.values(), key=lambda p: p["name"].lower()):
                out.append({"id": "d:" + p["id"], "name": p["name"], "kind": "computer",
                            "device": "computer", "platform": p["platform"]})
            for o in sorted(self.clients.values(), key=lambda o: o["name"].lower()):
                # A window on a computer is already represented by that computer itself.
                if o is not c and not o["local"]:
                    out.append({"id": "b:" + o["id"], "name": o["name"], "kind": "browser",
                                "device": o["device"]})
            return out

    def push_peers_all(self):
        with self.lock:
            clients = list(self.clients.values())
        for c in clients:
            c["q"].put(("peers", self.peers_for(c)))

    def push(self, cid, event, data):
        c = self.get_client(cid) if cid else None
        if c:
            c["q"].put((event, data))

    def push_local(self, event, data):
        with self.lock:
            clients = [c for c in self.clients.values() if c["local"]]
        for c in clients:
            c["q"].put((event, data))

    # --- noteboard
    def push_all(self, event, data):
        with self.lock:
            clients = list(self.clients.values())
        for c in clients:
            c["q"].put((event, data))

    def notes_changed(self, to_peers=True):
        self.push_all("notes", self.board.visible())
        if to_peers:  # tell the other computers right away (beacons catch up anything missed)
            with self.lock:
                peers = list(self.peers.values())
            for p in peers:
                threading.Thread(target=self.sync_notes, args=(p,), daemon=True).start()

    def sync_notes(self, peer):
        """Exchange boards with another computer: send ours, merge what it sends back."""
        self.note_sync_at[peer["id"]] = time.time()
        body = json.dumps({"notes": self.board.all()}).encode()
        conn = None
        try:
            conn = peer_connection(peer, timeout=10)
            conn.request("POST", "/api/notes/sync", body, {
                "Content-Type": "application/json", "X-WiFiDrop-Key": self.cfg["key"]})
            resp = conn.getresponse()
            data = resp.read()
            if resp.status == 200 and self.board.merge(json.loads(data).get("notes")):
                self.notes_changed(to_peers=False)
        except (OSError, ValueError, AttributeError) as e:
            log(f"Note sync with {peer['name']} failed: {e}")
        finally:
            if conn:
                conn.close()

    def maybe_sync_notes(self, peer_id, their_digest):
        if not their_digest or their_digest == self.board.digest():
            return
        if time.time() - self.note_sync_at.get(peer_id, 0) < NOTE_SYNC_GAP:
            return
        peer = self.get_peer(peer_id)
        if peer:
            self.note_sync_at[peer_id] = time.time()
            threading.Thread(target=self.sync_notes, args=(peer,), daemon=True).start()

    # --- shared text page
    def pad_changed(self, to_peers=True):
        self.push_all("pad", self.pad.get())
        if to_peers:
            with self.lock:
                peers = list(self.peers.values())
            for p in peers:
                threading.Thread(target=self.sync_pad, args=(p,), daemon=True).start()

    def sync_pad(self, peer):
        """Exchange shared-text copies with another computer; the newer one wins on both sides."""
        self.pad_sync_at[peer["id"]] = time.time()
        body = json.dumps({"pad": self.pad.get()}).encode()
        conn = None
        try:
            conn = peer_connection(peer, timeout=15)
            conn.request("POST", "/api/pad/sync", body, {
                "Content-Type": "application/json", "X-WiFiDrop-Key": self.cfg["key"]})
            resp = conn.getresponse()
            data = resp.read()
            if resp.status == 200 and self.pad.merge(json.loads(data).get("pad") or {}):
                self.fetch_missing_images()
                self.pad_changed(to_peers=False)
        except (OSError, ValueError, AttributeError) as e:
            log(f"Shared text sync with {peer['name']} failed: {e}")
        finally:
            if conn:
                conn.close()

    def maybe_sync_pad(self, peer_id, their_digest):
        if not their_digest or their_digest == self.pad.digest():
            return
        if time.time() - self.pad_sync_at.get(peer_id, 0) < NOTE_SYNC_GAP:
            return
        peer = self.get_peer(peer_id)
        if peer:
            self.pad_sync_at[peer_id] = time.time()
            threading.Thread(target=self.sync_pad, args=(peer,), daemon=True).start()

    def fetch_missing_images(self):
        for name in self.pad.referenced():
            if not os.path.exists(self.pad.image_path(name)):
                self.fetch_pad_image(name)

    def fetch_pad_image(self, name):
        """Get an image of the shared page from another computer. True if we have it now."""
        with self.lock:
            peers = list(self.peers.values())
        for peer in peers:
            conn = None
            try:
                conn = peer_connection(peer, timeout=20)
                conn.request("GET", f"/api/pad/img/{name}", headers={
                    "X-WiFiDrop-Key": self.cfg["key"], "X-WiFiDrop-No-Relay": "1"})
                resp = conn.getresponse()
                data = resp.read(PAD_IMAGE_MAX + 1)
                if resp.status == 200 and len(data) <= PAD_IMAGE_MAX \
                        and f"{hashlib.sha256(data).hexdigest()}.{image_ext(data)}" == name:
                    self.pad.add_image(data)
                    return True
            except (OSError, ValueError) as e:
                log(f"Couldn't get image from {peer['name']}: {e}")
            finally:
                if conn:
                    conn.close()
        return False

    # --- transfers
    def on_received(self, path, sender, size):
        name = os.path.basename(path)
        notify(f"Received {name}", f"From {sender} · {human_size(size)} · saved to {os.path.dirname(path)}")
        self.push_local("received", {"name": name, "from": sender, "size": size, "path": path})
        log(f"  ↓ {name} ({human_size(size)}) from {sender}")

    def add_offer(self, src, length, name, sender, to_cid, sender_cid):
        oid = secrets.token_urlsafe(12)
        path = os.path.join(self.inbox_dir, oid)
        try:
            with open(path, "wb") as f:
                copy_exact(src, f, length)
        except BaseException:
            _rm(path)
            raise
        offer = {"id": oid, "path": path, "name": name, "size": length, "from": sender,
                 "to": to_cid, "sender_client": sender_cid, "created": time.time(), "ack": False}
        with self.lock:
            self.inbox[oid] = offer
        self.push(to_cid, "offer", offer_public(offer))
        return offer

    def resolve_offer(self, oid, accepted):
        with self.lock:
            o = self.inbox.get(oid)
            if not o:
                return False
            if accepted:
                o["ack"] = True  # keep the file until it expires, in case the download is retried
            else:
                del self.inbox[oid]
        if not accepted:
            _rm(o["path"])
        receiver = self.get_client(o["to"])
        self.push(o["sender_client"], "offer-result", {
            "name": o["name"], "to": receiver["name"] if receiver else "They",
            "status": "accepted" if accepted else "declined"})
        return True


def log(msg):
    try:
        print(msg, flush=True)
    except Exception:
        pass  # no console (e.g. packaged Windows app)


def offer_public(o):
    return {"id": o["id"], "name": o["name"], "size": o["size"], "from": o["from"]}


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


_CLIENT_TLS = certs.client_context()


def peer_connection(peer, timeout):
    """HTTPS connection to another WiFiDrop computer, checked against the certificate fingerprint
    from its signed beacon (so nothing, including the key header, goes to an impostor)."""
    conn = http.client.HTTPSConnection(peer["ip"], peer["port"], timeout=timeout, context=_CLIENT_TLS)
    conn.connect()
    if not hmac.compare_digest(certs.peer_fingerprint(conn.sock) or "", peer["fp"]):
        conn.close()
        raise IOError(f"{peer['name']} presented the wrong certificate; not sending anything")
    return conn


def forward(peer, src, length, name, sender, key):
    """Stream an upload straight through to another computer running WiFiDrop."""
    conn = peer_connection(peer, timeout=30)
    try:
        conn.putrequest("POST", "/api/receive?" + urlencode({"name": name, "from": sender}))
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(length))
        conn.putheader("X-WiFiDrop-Key", key)
        conn.endheaders()
        remaining = length
        while remaining > 0:
            chunk = src.read(min(CHUNK, remaining))
            if not chunk:
                raise IOError("transfer was interrupted")
            conn.send(chunk)
            remaining -= len(chunk)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            try:
                msg = json.loads(body).get("error")
            except Exception:
                msg = None
            raise IOError(msg or f"{peer['name']} refused the file (HTTP {resp.status})")
    finally:
        conn.close()


# ----------------------------------------------------------------------------- discovery

def discovery_loop(state):
    """Every computer broadcasts a small signed beacon; only those with the same key accept it."""
    cfg = state.cfg
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind(("", DISCOVERY_PORT))
    except OSError as e:
        log(f"Warning: discovery disabled, UDP port {DISCOVERY_PORT} unavailable ({e}). "
            "Phones still work; other computers won't be found.")
        return
    sock.settimeout(1.0)

    def beacon(addr):
        payload = json.dumps({"app": APP, "id": cfg["id"], "name": cfg["name"], "port": cfg["port"],
                              "platform": platform_name(), "notes": state.board.digest(), "pad": state.pad.digest(),
                              "tls": state.tls.fingerprint, "ts": time.time()})
        sig = hmac.new(state.key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        try:
            sock.sendto(json.dumps({"m": payload, "s": sig}).encode(), addr)
        except OSError:
            pass  # e.g. network down; try again next round

    last = 0.0
    old_versions = set()
    while True:
        if time.time() - last >= BEACON_INTERVAL:
            try:
                state.tls.refresh(lan_ip())  # new certificate if the IP changed (e.g. another Wi-Fi)
            except Exception as e:
                log(f"Warning: couldn't renew the TLS certificate ({e})")
            beacon(("255.255.255.255", DISCOVERY_PORT))
            state.expire()
            last = time.time()
        try:
            data, addr = sock.recvfrom(4096)
            outer = json.loads(data)
            expected = hmac.new(state.key.encode(), outer["m"].encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, str(outer["s"])):
                continue
            msg = json.loads(outer["m"])
        except socket.timeout:
            continue
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        if msg.get("app") != APP or msg.get("id") == cfg["id"]:
            continue
        if not msg.get("tls"):
            if msg.get("id") not in old_versions:
                old_versions.add(msg.get("id"))
                log(f"Ignoring {msg.get('name')} ({addr[0]}): it runs an older WiFiDrop without encryption. Update it.")
            continue
        if state.upsert_peer(msg, addr[0]):
            beacon((addr[0], DISCOVERY_PORT))  # answer newcomers right away
        state.maybe_sync_notes(msg["id"], msg.get("notes"))
        state.maybe_sync_pad(msg["id"], msg.get("pad"))


# ----------------------------------------------------------------------------- http

def q1(qs, k, default=""):
    return qs.get(k, [default])[0]


class Handler(BaseHTTPRequestHandler):
    server_version = "WiFiDrop/1"

    def log_message(self, fmt, *args):
        pass

    @property
    def st(self):
        return self.server.state

    # --- helpers
    def is_local(self):
        """True for a window on this computer itself (gets settings + QR code)."""
        ip = self.client_address[0]
        return ip in ("127.0.0.1", "::1") or ip == lan_ip()

    @property
    def tls(self):
        return isinstance(self.connection, ssl.SSLSocket)

    def refuse_plain(self):
        """Plain HTTP is only for a window on this computer; send everyone else to HTTPS."""
        if self.tls or self.is_local():
            return False
        host = self.headers.get("Host") or f"{lan_ip()}:{self.st.cfg['port']}"
        if self.command == "GET":
            self.redirect(f"https://{host}{self.path}", code=301)
        else:
            self.send_json(403, {"error": "Use HTTPS"})
        return True

    def read_json(self, limit=65536):
        n = self.content_length()
        if not n or n > limit:
            return {}
        try:
            d = json.loads(self.rfile.read(n))
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}

    def authed(self):
        k = self.headers.get("X-WiFiDrop-Key")
        if k:
            return self.st.key_ok(k)
        c = cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return False
        m = c.get("wd_session")
        return m is not None and hmac.compare_digest(m.value.encode(), self.st.session_token.encode())

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location, cookie=None, code=303):
        self.send_response(code)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def login_redirect(self):
        secure = "; Secure" if self.tls else ""
        self.redirect("/", f"wd_session={self.st.session_token}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax{secure}")

    def serve_page(self, name):
        with open(os.path.join(WEB_DIR, name), "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def content_length(self):
        try:
            n = int(self.headers.get("Content-Length", ""))
            return n if n >= 0 else None
        except ValueError:
            return None

    # --- routes
    def do_GET(self):
        if self.refuse_plain():
            return
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/":
            if "k" in qs:  # link/QR with the key embedded: log in, then drop the key from the URL
                return self.login_redirect() if self.st.key_ok(q1(qs, "k")) else self.redirect("/?bad=1")
            return self.serve_page("index.html" if self.authed() else "login.html")
        if u.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if not self.authed():
            return self.send_json(401, {"error": "Not authorized"})
        if u.path == "/api/ping":
            return self.send_json(200, {"ok": True})
        if u.path == "/wifidrop.mobileconfig":  # iPhone profile that makes Safari trust this computer
            body = self.st.tls.mobileconfig(self.st.cfg["name"])
            self.send_response(200)
            self.send_header("Content-Type", "application/x-apple-aspen-config")
            self.send_header("Content-Disposition", 'attachment; filename="WiFiDrop.mobileconfig"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/notes":
            return self.send_json(200, {"notes": self.st.board.visible()})
        if u.path == "/api/events":
            return self.events(qs)
        if u.path == "/api/settings" and self.is_local():
            cfg = self.st.cfg
            return self.send_json(200, {"name": cfg["name"], "key": cfg["key"], "phone_url": self.st.phone_url(),
                                        "download_dir": cfg["download_dir"], "port": cfg["port"]})
        if u.path == "/api/qr.svg" and self.is_local():
            svg = qr_svg(self.st.phone_url())
            if not svg:
                return self.send_json(404, {"error": "qrcode package not installed"})
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(svg)
            return
        if u.path == "/api/pad":
            return self.send_json(200, self.st.pad.get())
        m = re.fullmatch(r"/api/pad/img/([^/]+)", u.path)
        if m:
            return self.pad_image(m.group(1))
        m = re.fullmatch(r"/api/inbox/([\w-]+)", u.path)
        if m:
            return self.download(m.group(1))
        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.refuse_plain():
            return
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/login":
            n = min(self.content_length() or 0, 4096)
            form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            return self.login_redirect() if self.st.key_ok(q1(form, "key")) else self.redirect("/?bad=1")
        if not self.authed():
            return self.send_json(401, {"error": "Not authorized"})
        if u.path == "/api/send":
            return self.send_file(qs)
        if u.path == "/api/receive":
            return self.receive(qs)
        if u.path == "/api/notes":
            d = self.read_json(limit=4 * NOTE_MAX_CHARS + 4096)
            sender = self.st.cfg["name"] if self.is_local() else (str(d.get("from") or "").strip()[:40] or "Someone")
            try:
                self.st.board.add(str(d.get("text") or ""), sender)
            except ValueError as e:
                return self.send_json(400, {"error": str(e)})
            self.st.notes_changed()
            return self.send_json(200, {"ok": True})
        if u.path == "/api/notes/delete":
            ids = self.read_json().get("ids")
            if isinstance(ids, list) and self.st.board.delete([str(i) for i in ids]):
                self.st.notes_changed()
            return self.send_json(200, {"ok": True})
        if u.path == "/api/pad":
            d = self.read_json(limit=6 * PAD_MAX_HTML)
            try:
                doc = self.st.pad.edit(d.get("html") or "", str(d.get("client") or "")[:64])
            except ValueError as e:
                return self.send_json(400, {"error": str(e)})
            self.st.pad_changed()
            return self.send_json(200, {"ts": doc["ts"]})
        if u.path == "/api/pad/image":
            n = self.content_length()
            if n is None or n > PAD_IMAGE_MAX:
                return self.send_json(413, {"error": "That image is too big (max 15 MB)."})
            data = self.rfile.read(n)
            try:
                name = self.st.pad.add_image(data)
            except ValueError as e:
                return self.send_json(400, {"error": str(e)})
            except OSError as e:
                return self.send_json(500, {"error": str(e)})
            return self.send_json(200, {"name": name, "src": f"/api/pad/img/{name}"})
        if u.path == "/api/pad/sync":  # another computer exchanging the shared text with us
            d = self.read_json(limit=6 * PAD_MAX_HTML + 65536)
            if self.st.pad.merge(d.get("pad") or {}):
                threading.Thread(target=self.st.fetch_missing_images, daemon=True).start()
                self.st.pad_changed(to_peers=False)
            return self.send_json(200, {"pad": self.st.pad.get()})
        if u.path == "/api/notes/sync":  # another computer exchanging boards with us
            d = self.read_json(limit=(NOTE_MAX_LIVE + 50) * (4 * NOTE_MAX_CHARS + 512) + 65536)
            if self.st.board.merge(d.get("notes")):
                self.st.notes_changed(to_peers=False)
            return self.send_json(200, {"notes": self.st.board.all()})
        if u.path == "/api/settings" and self.is_local():
            d = self.read_json()
            try:
                changed = self.st.update_settings(name=d.get("name"), key=d.get("key"))
            except ValueError as e:
                return self.send_json(400, {"error": str(e)})
            return self.send_json(200, {"ok": True, "relogin": f"/?k={quote(self.st.cfg['key'])}" if changed else None})
        if u.path == "/api/open-folder" and self.is_local():
            try:
                open_folder(self.st.cfg["download_dir"])
            except Exception as e:
                return self.send_json(500, {"error": str(e)})
            return self.send_json(200, {"ok": True})
        m = re.fullmatch(r"/api/inbox/([\w-]+)/(accept|decline)", u.path)
        if m:
            ok = self.st.resolve_offer(m.group(1), m.group(2) == "accept")
            return self.send_json(200 if ok else 404, {"ok": ok})
        self.send_json(404, {"error": "Not found"})

    # --- server-sent events: live device list and incoming offers for one browser tab
    def events(self, qs):
        c = {
            "id": q1(qs, "client") or uuid.uuid4().hex,
            "name": (q1(qs, "name") or "Browser").strip()[:40] or "Browser",
            "device": q1(qs, "device") if q1(qs, "device") in ("phone", "tablet", "computer") else "computer",
            "q": queue.Queue(),
            "local": self.is_local(),
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.st.add_client(c)
        try:
            self._event("hello", {"id": c["id"], "host": self.st.cfg["name"], "local": c["local"]})
            while True:
                try:
                    item = c["q"].get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if item is None:
                    break
                self._event(*item)
        except OSError:
            pass
        finally:
            self.st.remove_client(c)

    def _event(self, name, data):
        self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()

    # --- transfers
    def send_file(self, qs):
        to = q1(qs, "to")
        name = safe_name(q1(qs, "name") or "file")
        sender = (q1(qs, "from") or "Someone").strip()[:40] or "Someone"
        if self.is_local():
            sender = self.st.cfg["name"]  # the window on this computer sends as the computer
        length = self.content_length()
        if length is None:
            return self.send_json(411, {"error": "Missing Content-Length"})
        try:
            if to == "self":
                path = save_upload(self.rfile, length, self.st.cfg["download_dir"], name)
                self.st.on_received(path, sender, length)
            elif to.startswith("d:"):
                peer = self.st.get_peer(to[2:])
                if not peer:
                    return self.send_json(404, {"error": "That computer is no longer available"})
                forward(peer, self.rfile, length, name, sender, self.st.cfg["key"])
            elif to.startswith("b:"):
                if not self.st.get_client(to[2:]):
                    return self.send_json(404, {"error": "That device is no longer connected"})
                self.st.add_offer(self.rfile, length, name, sender, to[2:], q1(qs, "client"))
            else:
                return self.send_json(400, {"error": "Unknown destination"})
        except Exception as e:
            return self.send_json(502, {"error": str(e)})
        self.send_json(200, {"ok": True})

    def receive(self, qs):
        length = self.content_length()
        if length is None:
            return self.send_json(411, {"error": "Missing Content-Length"})
        name = safe_name(q1(qs, "name") or "file")
        sender = (q1(qs, "from") or "Someone").strip()[:40] or "Someone"
        try:
            path = save_upload(self.rfile, length, self.st.cfg["download_dir"], name)
        except Exception as e:
            return self.send_json(500, {"error": str(e)})
        self.st.on_received(path, sender, length)
        self.send_json(200, {"ok": True})

    def pad_image(self, name):
        path = self.st.pad.image_path(name)
        if not path:
            return self.send_json(404, {"error": "Not found"})
        if not os.path.exists(path) and not self.headers.get("X-WiFiDrop-No-Relay"):
            self.st.fetch_pad_image(name)  # it may only exist on another computer so far
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self.send_json(404, {"error": "Image not found"})
        self.send_response(200)
        self.send_header("Content-Type", IMAGE_TYPES[name.rsplit(".", 1)[1]])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")  # name = hash of content
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def download(self, oid):
        with self.st.lock:
            o = self.st.inbox.get(oid)
        if not o or not os.path.exists(o["path"]):
            return self.send_json(404, {"error": "This file has expired"})
        ascii_name = o["name"].encode("ascii", "replace").decode().replace('"', "_").replace("?", "_")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(o["size"]))
        self.send_header("Content-Disposition",
                         f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(o['name'])}")
        self.end_headers()
        try:
            with open(o["path"], "rb") as f:
                shutil.copyfileobj(f, self.wfile, CHUNK)
        except OSError:
            pass


class Server(ThreadingHTTPServer):
    """HTTPS and HTTP on one port: a TLS connection starts with a handshake record (byte 0x16)."""
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, ssl.SSLError, TimeoutError)):
            return  # a browser hung up mid-request: normal, not worth a traceback
        super().handle_error(request, client_address)

    def finish_request(self, request, client_address):  # runs in the connection's own thread
        try:
            request.settimeout(15)
            first = request.recv(1, socket.MSG_PEEK)
            if not first:
                return
            if first[0] == 0x16:
                request = self.state.tls.context.wrap_socket(request, server_side=True)
            request.settimeout(None)
        except (OSError, ssl.SSLError):
            return  # e.g. Safari closing the connection after the certificate warning
        try:
            self.RequestHandlerClass(request, client_address, self)
        finally:
            if isinstance(request, ssl.SSLSocket):
                try:
                    request.close()
                except OSError:
                    pass


# ----------------------------------------------------------------------------- main

def print_qr(url):
    try:
        import qrcode  # optional: pip install qrcode
    except ImportError:
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.print_ascii(invert=True)


def build_parser(description="AirDrop-style file sharing on your local Wi-Fi."):
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--name", help="how this computer appears to others (default: hostname)")
    ap.add_argument("--port", type=int, help="web/transfer port (default 8765)")
    ap.add_argument("--key", help="shared network key - use the same one on all your computers")
    ap.add_argument("--download-dir", dest="download_dir", help="where received files are saved")
    return ap


def start(args):
    """Start the web server and discovery in background threads. Raises OSError if the port is taken."""
    cfg, cfg_path = load_config(args)
    state = State(cfg, cfg_path)
    server = Server(("", cfg["port"]), Handler)
    server.state = state
    threading.Thread(target=discovery_loop, args=(state,), daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


def stop(server, state):
    server.shutdown()
    server.server_close()
    shutil.rmtree(state.inbox_dir, ignore_errors=True)


def main():
    """Headless mode: run in a terminal / as a service, use it from any browser."""
    args = build_parser().parse_args()
    try:
        server, state = start(args)
    except ValueError as e:
        sys.exit(str(e))
    except OSError as e:
        sys.exit(f"Could not listen: {e}. Is WiFiDrop already running? Try --port.")
    cfg = state.cfg
    phone_url = state.phone_url()
    log(f'\nWiFiDrop is running as "{cfg["name"]}"\n')
    log(f"  On this computer:   http://localhost:{cfg['port']}/?k={cfg['key']}")
    log(f"  On your phone:      {phone_url}")
    log(f"  Network key:        {cfg['key']}")
    log(f"  Other computers:    use the same key (--key {cfg['key']})")
    log(f"  Received files go:  {cfg['download_dir']}")
    log(f"  Config:             {state.cfg_path}\n")
    print_qr(phone_url)
    log("Press Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("\nStopped.")
    finally:
        stop(server, state)


if __name__ == "__main__":
    main()
