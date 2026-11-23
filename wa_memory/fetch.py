"""Download and decrypt one WhatsApp attachment, straight from WhatsApp's media servers.

No WhatsApp session is needed: an attachment is an encrypted file at a URL on WhatsApp's media hosts, and
the message carries everything needed to fetch and open it (the URL or direct path, the media key and
the hashes). Nothing is sent to anyone else and no read receipt is involved.

Format (the same for every WhatsApp client): HKDF-SHA256 expands the 32-byte media key, with an
"info" string per media kind, into 112 bytes: IV (16), AES-256 key (32), HMAC key (32), rest unused.
The file is AES-256-CBC ciphertext (PKCS#7 padding) followed by the first 10 bytes of
HMAC-SHA256(IV + ciphertext). The message's fileEncSHA256 is the SHA-256 of the whole downloaded
file, and fileSHA256 that of the decrypted file.
"""
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MEDIA_HOST = "https://mmg.whatsapp.net"
INFO = {
    "image": b"WhatsApp Image Keys",
    "sticker": b"WhatsApp Image Keys",
    "video": b"WhatsApp Video Keys",
    "audio": b"WhatsApp Audio Keys",
    "document": b"WhatsApp Document Keys",
}
MAX_BYTES = 100 * 1024 * 1024
USER_AGENT = "wa-memory"


class FetchError(Exception):
    """`status` is 'expired' (gone from WhatsApp's servers), 'too_big' or 'failed'."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def expand_key(media_key, kind):
    """(iv, cipher_key, mac_key) for a media key and kind."""
    if kind not in INFO:
        raise FetchError("failed", "unknown media kind %r" % kind)
    if not media_key or len(media_key) != 32:
        raise FetchError("failed", "missing or malformed media key")
    out = HKDF(algorithm=hashes.SHA256(), length=112, salt=None, info=INFO[kind]).derive(bytes(media_key))
    return out[:16], out[16:48], out[48:80]


def decrypt(data, media_key, kind, enc_sha256=None, sha256=None):
    """The plain bytes of a downloaded attachment, after checking both hashes (when known) and the MAC."""
    if enc_sha256 and hashlib.sha256(data).digest() != bytes(enc_sha256):
        raise FetchError("failed", "the downloaded file does not match its hash")
    if len(data) < 10 + 16:
        raise FetchError("failed", "the downloaded file is too short")
    iv, cipher_key, mac_key = expand_key(media_key, kind)
    body, mac = data[:-10], data[-10:]
    if not hmac.compare_digest(hmac.new(mac_key, iv + body, hashlib.sha256).digest()[:10], mac):
        raise FetchError("failed", "the file's MAC does not match (wrong key or damaged file)")
    if len(body) % 16:
        raise FetchError("failed", "the file is not whole AES blocks")
    dec = Cipher(algorithms.AES(cipher_key), modes.CBC(iv)).decryptor()
    padded = dec.update(body) + dec.finalize()
    try:
        unpad = padding.PKCS7(128).unpadder()
        plain = unpad.update(padded) + unpad.finalize()
    except ValueError as exc:
        raise FetchError("failed", "bad padding") from exc
    if sha256 and hashlib.sha256(plain).digest() != bytes(sha256):
        raise FetchError("failed", "the decrypted file does not match its hash")
    return plain


def _whatsapp_https(url):
    """Whether `url` is plain https on a whatsapp.net host (no credentials, no port, printable ASCII)."""
    try:
        u = urllib.parse.urlsplit(url)
        ok = (u.scheme == "https" and bool(u.hostname)
              and (u.hostname == "whatsapp.net" or u.hostname.endswith(".whatsapp.net"))
              and not u.username and not u.password and u.port is None)
    except ValueError:
        return False
    return ok and all(33 <= ord(c) < 127 for c in url)


class _SameHostsRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to another https whatsapp.net address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _whatsapp_https(newurl):
            raise FetchError("failed", "redirected away from WhatsApp's media servers")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameHostsRedirect)


def _open(req, timeout):
    return _OPENER.open(req, timeout=timeout)


def media_url(url, direct_path):
    """The https URL to fetch: the message's own URL when it is on a whatsapp.net host, else MEDIA_HOST + path."""
    if url and _whatsapp_https(url):
        return url
    if direct_path and direct_path.startswith("/") and "//" not in direct_path and "\\" not in direct_path \
            and _whatsapp_https(MEDIA_HOST + direct_path):
        return MEDIA_HOST + direct_path
    raise FetchError("failed", "the message has no usable download address")


def download(ref, max_bytes=MAX_BYTES, timeout=60, opener=None):
    """The decrypted bytes of the attachment described by `ref` (a media_refs row as a dict)."""
    declared = ref.get("length") or 0
    if declared > max_bytes:
        raise FetchError("too_big", "the file is %d bytes; the limit is %d" % (declared, max_bytes))
    url = media_url(ref.get("url"), ref.get("direct_path"))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        resp = (opener or _open)(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in (403, 404, 410):
            raise FetchError("expired", "No longer on WhatsApp's servers (HTTP %d)" % exc.code) from exc
        raise FetchError("failed", "HTTP %d" % exc.code) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FetchError("failed", "download failed: %s" % getattr(exc, "reason", exc)) from exc
    try:
        chunks, size = [], 0
        while True:
            try:
                chunk = resp.read(1 << 16)
            except (OSError, ValueError) as exc:
                raise FetchError("failed", "download broke off: %s" % exc) from exc
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes + 10:
                raise FetchError("too_big", "the file is over %d bytes" % max_bytes)
            chunks.append(chunk)
    finally:
        resp.close()
    return decrypt(b"".join(chunks), ref.get("media_key"), ref.get("kind"), ref.get("enc_sha256"), ref.get("sha256"))
