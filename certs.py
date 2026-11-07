"""TLS certificates for WiFiDrop, made with the Python standard library only.

Each computer has its own small certificate authority (CA) and uses it to issue the certificate
its server presents (for its current LAN IP). The CA is what an iPhone can install once to stop
Safari's "not private" warning. It carries name constraints limiting it to private-network
addresses and *.local names, so even a stolen CA key can't impersonate real websites.

Computers don't use the CA to check each other: every signed discovery beacon carries the
fingerprint of the sender's certificate, and connections are checked against that.

Python's ssl module can serve TLS but can't create certificates, hence the small RSA + DER
implementation below (keys come from the `secrets` module).
"""
import base64
import datetime
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import threading
import uuid

LEAF_DAYS = 397          # Apple rejects server certificates valid for longer than 398 days
LEAF_RENEW_DAYS = 30     # issue a new one when less than this is left
CA_YEARS = 10
PRIVATE_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10"]

# ----------------------------------------------------------------------------- RSA

_SMALL_PRIMES = [p for p in range(3, 2000) if all(p % d for d in range(2, int(p ** 0.5) + 1))]


def _probable_prime(n, rounds=40):
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits, e):
    while True:
        n = secrets.randbits(bits) | (0b11 << (bits - 2)) | 1  # top two bits set: p*q has exactly 2*bits
        if (n - 1) % e and _probable_prime(n):
            return n


def rsa_generate(bits=2048, e=65537):
    while True:
        p, q = _prime(bits // 2, e), _prime(bits // 2, e)
        if p == q:
            continue
        n = p * q
        lam = (p - 1) * (q - 1) // _gcd(p - 1, q - 1)
        d = pow(e, -1, lam)
        return {"n": n, "e": e, "d": d, "p": p, "q": q}


def _gcd(a, b):
    while b:
        a, b = b, a % b
    return a


def rsa_sign_sha256(key, data):
    """RSASSA-PKCS1-v1_5 with SHA-256."""
    k = (key["n"].bit_length() + 7) // 8
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(data).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(digest_info) - 3) + b"\x00" + digest_info
    p, q, d = key["p"], key["q"], key["d"]
    m = int.from_bytes(em, "big")
    # Chinese remainder theorem: ~4x faster than pow(m, d, n)
    m1, m2 = pow(m, d % (p - 1), p), pow(m, d % (q - 1), q)
    h = (pow(q, -1, p) * (m1 - m2)) % p
    s = m2 + h * q
    assert pow(s, key["e"], key["n"]) == m
    return s.to_bytes(k, "big")


# ----------------------------------------------------------------------------- DER

def _len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag, content):
    return bytes([tag]) + _len(len(content)) + content


def seq(*items):
    return tlv(0x30, b"".join(items))


def der_set(*items):
    return tlv(0x31, b"".join(items))


def der_int(v):
    return tlv(0x02, v.to_bytes(v.bit_length() // 8 + 1, "big"))  # +1 keeps the sign bit clear


def der_bool(v):
    return tlv(0x01, b"\xff" if v else b"\x00")


def oid(dotted):
    parts = [int(x) for x in dotted.split(".")]
    out = bytes([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(reversed(chunk))
    return tlv(0x06, out)


def octets(b):
    return tlv(0x04, b)


def bits(b, unused=0):
    return tlv(0x03, bytes([unused]) + b)


def utf8(s):
    return tlv(0x0C, s.encode())


def der_time(dt):
    if dt.year < 2050:
        return tlv(0x17, dt.strftime("%y%m%d%H%M%SZ").encode())
    return tlv(0x18, dt.strftime("%Y%m%d%H%M%SZ").encode())


def explicit(n, content):
    return tlv(0xA0 | n, content)


NULL = b"\x05\x00"
SHA256_RSA = seq(oid("1.2.840.113549.1.1.11"), NULL)


def name(*rdns):
    """X.509 Name from (oid, text) pairs."""
    return seq(*(der_set(seq(oid(o), utf8(v))) for o, v in rdns))


def rsa_public_der(key):
    return seq(der_int(key["n"]), der_int(key["e"]))


def spki(key):
    return seq(seq(oid("1.2.840.113549.1.1.1"), NULL), bits(rsa_public_der(key)))


def ext(o, value, critical=False):
    return seq(oid(o), *( [der_bool(True)] if critical else []), octets(value))


def make_cert(subject_key, subject, issuer_key, issuer, days, extensions):
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    serial = secrets.randbits(127) | (1 << 126)
    tbs = seq(
        explicit(0, der_int(2)),  # v3
        der_int(serial),
        SHA256_RSA,
        issuer,
        seq(der_time(now - datetime.timedelta(days=1)), der_time(now + datetime.timedelta(days=days))),
        subject,
        spki(subject_key),
        explicit(3, seq(*extensions)),
    )
    sig = rsa_sign_sha256(issuer_key, tbs)
    return seq(tbs, SHA256_RSA, bits(sig))


def key_id(key):
    return hashlib.sha1(rsa_public_der(key)).digest()


def pem(der, label):
    b = base64.b64encode(der).decode()
    return f"-----BEGIN {label}-----\n" + "\n".join(b[i:i + 64] for i in range(0, len(b), 64)) + f"\n-----END {label}-----\n"


def unpem(text):
    return base64.b64decode("".join(l for l in text.splitlines() if l and not l.startswith("-----")))


def key_pem(key):
    p, q, d = key["p"], key["q"], key["d"]
    der = seq(der_int(0), der_int(key["n"]), der_int(key["e"]), der_int(d), der_int(p), der_int(q),
              der_int(d % (p - 1)), der_int(d % (q - 1)), der_int(pow(q, -1, p)))
    return pem(der, "RSA PRIVATE KEY")


CN, ORG = "2.5.4.3", "2.5.4.10"


def make_ca(key, label):
    subject = name((ORG, "WiFiDrop"), (CN, label))
    permitted = [seq(tlv(0x87, ipaddress.ip_network(n).network_address.packed + ipaddress.ip_network(n).netmask.packed))
                 for n in PRIVATE_NETS]
    permitted += [seq(tlv(0x82, b"local")), seq(tlv(0x82, b"localhost"))]
    exts = [
        ext("2.5.29.19", seq(der_bool(True), der_int(0)), critical=True),  # CA, may only sign end certificates
        ext("2.5.29.15", bits(b"\x86", 1), critical=True),  # digitalSignature, keyCertSign, cRLSign
        ext("2.5.29.14", octets(key_id(key))),
        # Name constraints: only private IPs and .local/localhost names. Marked non-critical so that
        # software which doesn't understand them still accepts the CA (those that do still enforce them).
        ext("2.5.29.30", seq(tlv(0xA0, b"".join(permitted)))),
    ]
    return make_cert(key, subject, key, subject, CA_YEARS * 365, exts), subject


def make_leaf(key, ca_key, ca_subject, ips, dns_names):
    san = b"".join(tlv(0x87, ipaddress.ip_address(i).packed) for i in ips)
    san += b"".join(tlv(0x82, d.encode()) for d in dns_names)
    exts = [
        ext("2.5.29.19", seq(), critical=True),
        ext("2.5.29.15", bits(b"\xa0", 5), critical=True),  # digitalSignature, keyEncipherment
        ext("2.5.29.37", seq(oid("1.3.6.1.5.5.7.3.1"))),    # serverAuth
        ext("2.5.29.17", seq(san)),
        ext("2.5.29.14", octets(key_id(key))),
        ext("2.5.29.35", seq(tlv(0x80, key_id(ca_key)))),
    ]
    subject = name((ORG, "WiFiDrop"), (CN, "wifidrop.local"))
    return make_cert(key, subject, ca_key, ca_subject, LEAF_DAYS, exts)


# ----------------------------------------------------------------------------- key files

def _write_private(path, text):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _key_to_json(key):
    return json.dumps({k: hex(v) for k, v in key.items()})


def _key_from_json(text):
    return {k: int(v, 16) for k, v in json.loads(text).items()}


def _mdns_name():
    host = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().split(".")[0].lower()).strip("-")[:63]
    return f"{host}.local" if host else None


class TLS:
    """Owns this computer's CA and server certificate and hands out an up-to-date SSLContext."""

    def __init__(self, directory, label):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        self.lock = threading.Lock()
        ca_key_path = os.path.join(directory, "ca-key.json")
        ca_path = os.path.join(directory, "ca.pem")
        if os.path.exists(ca_key_path) and os.path.exists(ca_path):
            with open(ca_key_path) as f:
                self.ca_key = _key_from_json(f.read())
            with open(ca_path) as f:
                self.ca_der = unpem(f.read())
        else:
            self.ca_key = rsa_generate()
            stamp = datetime.date.today().isoformat()
            self.ca_der, _ = make_ca(self.ca_key, f"WiFiDrop {label} {stamp} {uuid.uuid4().hex[:6]}")
            _write_private(ca_key_path, _key_to_json(self.ca_key))
            with open(ca_path, "w") as f:
                f.write(pem(self.ca_der, "CERTIFICATE"))
        self.ca_subject = _subject_of(self.ca_der)
        leaf_key_path = os.path.join(directory, "server-key.pem")
        leaf_key_json = os.path.join(directory, "server-key.json")
        if os.path.exists(leaf_key_json) and os.path.exists(leaf_key_path):
            with open(leaf_key_json) as f:
                self.leaf_key = _key_from_json(f.read())
        else:
            self.leaf_key = rsa_generate()
            _write_private(leaf_key_json, _key_to_json(self.leaf_key))
            _write_private(leaf_key_path, key_pem(self.leaf_key))
        self.leaf_key_path = leaf_key_path
        self.leaf_path = os.path.join(directory, "server.pem")
        self.meta_path = os.path.join(directory, "server.json")
        self.context = None
        self.fingerprint = None  # sha256 of the server certificate, shared in discovery beacons
        self.ip = None

    @property
    def ca_fingerprint(self):
        return hashlib.sha256(self.ca_der).hexdigest()

    def refresh(self, ip):
        """Make sure the server certificate covers `ip` and isn't close to expiry. Cheap when nothing changed."""
        with self.lock:
            if self.context is not None and ip == self.ip and not self._expiring():
                return
            meta = self._load_meta()
            if not (meta and meta.get("ip") == ip and meta.get("ca") == self.ca_fingerprint
                    and meta.get("expires", 0) - _now_ts() > LEAF_RENEW_DAYS * 86400
                    and os.path.exists(self.leaf_path)):
                ips = sorted({ip, "127.0.0.1"}, key=lambda a: a == "127.0.0.1")
                if not any(ipaddress.ip_address(ip) in ipaddress.ip_network(n) for n in PRIVATE_NETS):
                    ips = ["127.0.0.1"]  # the CA isn't allowed to vouch for public addresses
                dns = ["localhost"] + ([_mdns_name()] if _mdns_name() else [])
                der = make_leaf(self.leaf_key, self.ca_key, self.ca_subject, ips, dns)
                with open(self.leaf_path, "w") as f:
                    f.write(pem(der, "CERTIFICATE") + pem(self.ca_der, "CERTIFICATE"))
                meta = {"ip": ip, "ca": self.ca_fingerprint, "expires": _now_ts() + LEAF_DAYS * 86400,
                        "fingerprint": hashlib.sha256(der).hexdigest()}
                with open(self.meta_path, "w") as f:
                    json.dump(meta, f)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(self.leaf_path, self.leaf_key_path)
            self.context, self.ip, self.fingerprint = ctx, ip, meta["fingerprint"]
            self._expires = meta["expires"]

    def _expiring(self):
        return self._expires - _now_ts() < LEAF_RENEW_DAYS * 86400

    def _load_meta(self):
        try:
            with open(self.meta_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def mobileconfig(self, label):
        """An iOS configuration profile that installs the CA."""
        b64 = base64.b64encode(self.ca_der).decode()
        uid = uuid.UUID(self.ca_fingerprint[:32])
        payload_uid = uuid.UUID(hashlib.sha256(b"payload" + self.ca_der).hexdigest()[:32])
        esc = (label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>PayloadContent</key>
  <array>
    <dict>
      <key>PayloadCertificateFileName</key><string>wifidrop.cer</string>
      <key>PayloadContent</key><data>{b64}</data>
      <key>PayloadDescription</key><string>Lets Safari trust WiFiDrop on {esc}. Only valid for devices on your own network.</string>
      <key>PayloadDisplayName</key><string>WiFiDrop ({esc})</string>
      <key>PayloadIdentifier</key><string>wifidrop.ca.{payload_uid}</string>
      <key>PayloadType</key><string>com.apple.security.root</string>
      <key>PayloadUUID</key><string>{str(payload_uid).upper()}</string>
      <key>PayloadVersion</key><integer>1</integer>
    </dict>
  </array>
  <key>PayloadDescription</key><string>Removes Safari's "not private" warning for WiFiDrop on {esc}.</string>
  <key>PayloadDisplayName</key><string>WiFiDrop ({esc})</string>
  <key>PayloadIdentifier</key><string>wifidrop.profile.{uid}</string>
  <key>PayloadRemovalDisallowed</key><false/>
  <key>PayloadType</key><string>Configuration</string>
  <key>PayloadUUID</key><string>{str(uid).upper()}</string>
  <key>PayloadVersion</key><integer>1</integer>
</dict>
</plist>
""".encode()


def _now_ts():
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


def _subject_of(cert_der):
    """Extract the raw subject Name from a certificate (to use as the issuer of certificates it signs)."""
    def read(buf, i):
        tag = buf[i]
        n = buf[i + 1]
        j = i + 2
        if n & 0x80:
            k = n & 0x7F
            n = int.from_bytes(buf[j:j + k], "big")
            j += k
        return tag, j, j + n  # tag, content start, end

    _, s, _ = read(cert_der, 0)          # Certificate
    _, s, _ = read(cert_der, s)          # TBSCertificate
    fields = []
    i = s
    for _ in range(6):                   # version, serial, sigAlg, issuer, validity, subject
        tag, cs, end = read(cert_der, i)
        fields.append(cert_der[i:end])
        i = end
    return fields[5]


def peer_fingerprint(sock):
    """sha256 of the certificate presented on an SSL socket."""
    der = sock.getpeercert(binary_form=True)
    return hashlib.sha256(der).hexdigest() if der else None


def client_context():
    """For talking to other WiFiDrop computers: no CA check, the certificate is pinned by fingerprint instead."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx
