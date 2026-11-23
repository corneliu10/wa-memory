"""Attachments: download them (straight from WhatsApp's media servers, see fetch.py), read the text out of
documents, transcribe voice notes on this computer, and shrink pictures for Claude.

Files go to ~/.wa-memory/media/<chat hash>/<message hash>/. WhatsApp keeps files on its servers only for a
limited time; older ones answer 403/404 and are recorded as 'expired'.
"""
import base64
import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

from . import fetch, store

TEXT_EXT = {".pdf", ".docx", ".doc", ".rtf", ".odt", ".txt", ".md", ".csv", ".html", ".htm", ".xlsx", ".pptx",
            ".json", ".xml", ".py", ".js", ".ts", ".sql", ".log", ".yaml", ".yml"}
PLAIN_EXT = {".txt", ".md", ".csv", ".json", ".xml", ".py", ".js", ".ts", ".sql", ".log", ".yaml", ".yml"}
AUTO_MAX_BYTES = 25 * 1024 * 1024
MAX_TEXT = 200000
ZIP_MAX_UNPACKED = 50 * 1024 * 1024   # an xlsx/pptx that unpacks to more is not read (zip bombs)
# The container formats WhatsApp voice notes and videos come in; ffmpeg reads nothing else (no playlists).
FFMPEG_FORMATS = "ogg,mp3,mov,mp4,m4a,3gp,aac,wav,amr,matroska,webm,flac"
EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "video/mp4": ".mp4",
       "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "application/pdf": ".pdf"}
AUTO_TYPES = ("audio", "document")

PDF_SCRIPT = r"""
import sys
from pypdf import PdfReader
r = PdfReader(sys.argv[1])
out = []
for i, page in enumerate(r.pages):
    if i >= 300:
        break
    try:
        out.append(page.extract_text() or "")
    except Exception:
        pass
sys.stdout.write("\n".join(out))
"""


def _bin(env, name):
    """A program: $ENV if set, else `name` on PATH (Homebrew's prefix too), else the bare name."""
    if os.environ.get(env):
        return os.environ[env]
    found = shutil.which(name) or shutil.which(name, path="/opt/homebrew/bin:/usr/local/bin:/usr/bin")
    return found or name


def whisper_model():
    return os.environ.get("WHISPER_MODEL", os.path.join(store.home(), "models", "ggml-large-v3-turbo-q5_0.bin"))


def media_dir():
    d = os.path.join(store.home(), "media")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _msg_dir(chat_jid, msg_id):
    c = hashlib.sha256(chat_jid.encode("utf-8")).hexdigest()[:12]
    m = hashlib.sha256(msg_id.encode("utf-8")).hexdigest()[:24]
    return os.path.join(media_dir(), c, m)


def _files(d):
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if not f.startswith("."))


def _filename(ref):
    """A safe file name: the document's own name when it has one, else "<kind><extension from the type>"."""
    name = os.path.basename((ref.get("filename") or "").replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f\x7f/:]", "_", name).lstrip(".")
    name = name.encode("utf-8")[:200].decode("utf-8", "ignore")   # file systems limit names to 255 bytes
    if name:
        return name
    mime = (ref.get("mime") or "").split(";")[0].strip().lower()
    return (ref.get("kind") or "file") + (EXT.get(mime) or mimetypes.guess_extension(mime) or ".bin")


def download(conn, chat_jid, msg_id, max_bytes=fetch.MAX_BYTES):
    """Fetch one attachment from WhatsApp's media servers. Returns a dict with status (ok, expired, too_big, failed)."""
    d = _msg_dir(chat_jid, msg_id)
    have = _files(d)
    if have:
        p = os.path.join(d, have[0])
        return {"status": "ok", "path": p, "bytes": os.path.getsize(p), "source": "local"}
    ref = conn.execute("SELECT * FROM media_refs WHERE msg_id=? AND chat_jid=?", (msg_id, chat_jid)).fetchone()
    if ref is None:
        return {"status": "failed", "error": "No download details for this message (it came from a backup, or before "
                                              "wa-memory was linked)"}
    ref = dict(ref)
    try:
        data = fetch.download(ref, max_bytes=max_bytes)
    except fetch.FetchError as exc:
        return {"status": exc.status, "error": str(exc)}
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, _filename(ref))
    tmp = os.path.join(d, ".partial")   # a dot file: never listed as the attachment if a crash leaves it
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return {"status": "ok", "path": path, "bytes": len(data), "mime": ref.get("mime"), "source": "server"}


def _xml_text(zf, names):
    """Text runs of the given XML parts, reading at most MAX_TEXT*4 bytes of each (never a whole huge entry)."""
    parts = []
    for n in names:
        try:
            with zf.open(n) as fh:
                xml = fh.read(MAX_TEXT * 4).decode("utf-8", "replace")
        except KeyError:
            continue
        parts.append(" ".join(re.findall(r"<(?:a:)?t(?:\s[^<>]*)?>([^<]*)</(?:a:)?t>", xml)))
    return "\n".join(p for p in parts if p.strip())


def extract_text(path):
    """Text of a document, or None when the type is not supported."""
    ext = os.path.splitext(path)[1].lower()
    text = None
    if ext == ".pdf":
        py = os.environ.get("PDF_PYTHON", sys.executable)
        try:
            import pypdf  # noqa: F401  (the optional `pdf` extra)
        except ImportError:
            if "PDF_PYTHON" not in os.environ:
                return None
        p = subprocess.run([py, "-I", "-c", PDF_SCRIPT, path], capture_output=True, text=True, timeout=180)
        text = p.stdout if p.returncode == 0 else None
    elif ext in (".docx", ".doc", ".rtf", ".odt", ".html", ".htm"):
        p = subprocess.run([_bin("TEXTUTIL_BIN", "textutil"), "-noload", "-convert", "txt", "-stdout", path],
                           capture_output=True, text=True, timeout=120)
        text = p.stdout if p.returncode == 0 else None
    elif ext in (".xlsx", ".pptx"):
        with zipfile.ZipFile(path) as zf:
            if sum(i.file_size for i in zf.infolist()) > ZIP_MAX_UNPACKED:
                return None
            names = zf.namelist()
            if ext == ".xlsx":
                pick = [n for n in names if n == "xl/sharedStrings.xml" or n.startswith("xl/worksheets/sheet")]
            else:
                pick = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                              key=lambda n: int(re.findall(r"\d+", n)[-1]))
            text = _xml_text(zf, pick)
    elif ext in PLAIN_EXT:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(MAX_TEXT + 1)
    if text is None:
        return None
    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    return text[:MAX_TEXT]


def transcribe(path):
    """Voice note -> text with whisper.cpp on this Mac. Raises when the model is not there yet."""
    model = whisper_model()
    if not os.path.exists(model):
        raise FileNotFoundError("speech model not downloaded yet")
    work = tempfile.mkdtemp(prefix=".tr-", dir=media_dir())
    try:
        wav = os.path.join(work, "a.wav")
        subprocess.run([_bin("FFMPEG_BIN", "ffmpeg"), "-nostdin", "-loglevel", "error", "-y",
                        "-protocol_whitelist", "file", "-format_whitelist", FFMPEG_FORMATS, "-i", path,
                        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav], check=True, capture_output=True, timeout=300)
        base = os.path.join(work, "out")
        subprocess.run([_bin("WHISPER_BIN", "whisper-cli"), "-m", model, "-f", wav, "-l", "auto",
                        "-nt", "-np", "-otxt", "-of", base], check=True, capture_output=True, timeout=900)
        with open(base + ".txt", encoding="utf-8", errors="replace") as fh:
            return re.sub(r"\s+", " ", fh.read()).strip()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def image_jpeg(path, max_px=1568, video=False):
    """A JPEG of a picture (or of a video's first second) sized for Claude, as base64, or None."""
    work = tempfile.mkdtemp(prefix=".img-", dir=media_dir())
    try:
        out = os.path.join(work, "x.jpg")
        if video:
            for ss in ("1", "0"):
                r = subprocess.run([_bin("FFMPEG_BIN", "ffmpeg"), "-nostdin", "-loglevel", "error", "-y",
                                    "-protocol_whitelist", "file", "-format_whitelist", FFMPEG_FORMATS,
                                    "-ss", ss, "-i", path, "-frames:v", "1", "-vf", "scale='min(%d,iw)':-2" % max_px, out],
                                   capture_output=True, timeout=120)
                if r.returncode == 0 and os.path.exists(out):
                    break
        else:
            subprocess.run([_bin("SIPS_BIN", "sips"), "-s", "format", "jpeg", "-Z", str(max_px), path, "--out", out],
                           capture_output=True, timeout=60)
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            return None
        with open(out, "rb") as fh:
            data = fh.read()
        return base64.b64encode(data).decode("ascii") if len(data) <= 4 * 1024 * 1024 else None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _save(conn, chat_jid, msg_id, media_type, **kw):
    now = int(time.time())
    row = conn.execute("SELECT id, attempts FROM attachments WHERE msg_id=? AND chat_jid=?", (msg_id, chat_jid)).fetchone()
    attempts = (row["attempts"] if row else 0) + (1 if kw.get("count_attempt") else 0)
    vals = (kw.get("path"), kw.get("mime"), kw.get("bytes"), kw["status"], kw.get("error"), kw.get("text"),
            kw.get("text_kind"), attempts, now)
    if row:
        conn.execute("UPDATE attachments SET path=?, mime=?, bytes=?, status=?, error=?, text=?, text_kind=?, attempts=?, "
                     "updated_at=? WHERE id=?", vals + (row["id"],))
    else:
        conn.execute("INSERT INTO attachments(path, mime, bytes, status, error, text, text_kind, attempts, updated_at, "
                     "chat_jid, msg_id, media_type) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", vals + (chat_jid, msg_id, media_type))
    conn.commit()


def process(conn, chat_jid, msg_id, media_type, filename=None, max_bytes=fetch.MAX_BYTES):
    """Make sure an attachment is downloaded and its text/transcript is known. Returns the attachments row as dict."""
    row = conn.execute("SELECT * FROM attachments WHERE msg_id=? AND chat_jid=?", (msg_id, chat_jid)).fetchone()
    if row and row["status"] == "ok" and row["path"] and os.path.exists(row["path"]):
        need_text = row["text"] is None and (media_type == "audio" or
                                             os.path.splitext(row["path"])[1].lower() in TEXT_EXT)
        if not need_text or (media_type == "audio" and not os.path.exists(whisper_model())):
            return dict(row)
        dl = {"status": "ok", "path": row["path"], "bytes": row["bytes"], "mime": row["mime"]}
    elif row and row["status"] == "expired":
        return dict(row)
    else:
        dl = download(conn, chat_jid, msg_id, max_bytes=max_bytes)
        if dl["status"] != "ok":
            _save(conn, chat_jid, msg_id, media_type, status=dl["status"], error=dl.get("error"), count_attempt=True)
            return dict(conn.execute("SELECT * FROM attachments WHERE msg_id=? AND chat_jid=?", (msg_id, chat_jid)).fetchone())
    text, kind, err = None, None, None
    try:
        if media_type == "audio":
            if os.path.exists(whisper_model()):  # without the model: no error, so it is transcribed once it is installed
                text, kind = transcribe(dl["path"]), "transcript"
        elif media_type == "document":
            text = extract_text(dl["path"])
            kind = "document" if text is not None else None
    except Exception as exc:  # keep the file; the text can be retried
        err = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    _save(conn, chat_jid, msg_id, media_type, status="ok", path=dl["path"], mime=dl.get("mime"), bytes=dl.get("bytes"),
          text=text, text_kind=kind, error=err)
    return dict(conn.execute("SELECT * FROM attachments WHERE msg_id=? AND chat_jid=?", (msg_id, chat_jid)).fetchone())


def auto_fetch(conn, budget_s=150):
    """Download and read new documents and voice notes in the background (newest first)."""
    t0 = time.time()
    counts = {"downloaded": 0, "expired": 0, "failed": 0, "skipped": 0, "texts": 0, "transcripts": 0}
    have_model = os.path.exists(whisper_model())
    rows = conn.execute(
        "SELECT m.chat_jid, m.msg_id, m.media_type, m.filename, r.length FROM messages m "
        "JOIN media_refs r ON r.msg_id = m.msg_id AND r.chat_jid = m.chat_jid "
        "LEFT JOIN attachments a ON a.msg_id = m.msg_id AND a.chat_jid = m.chat_jid "
        "WHERE m.media_type IN ('audio','document') AND ("
        "  a.id IS NULL"
        "  OR (a.status = 'failed' AND a.attempts < 3 AND a.updated_at < ?)"
        "  OR (a.status = 'ok' AND a.text IS NULL AND a.error IS NULL AND m.media_type = 'audio' AND ?)"
        ") ORDER BY m.ts DESC LIMIT 400", (int(time.time()) - 6 * 3600, 1 if have_model else 0)).fetchall()
    for r in rows:
        if time.time() - t0 > budget_s:
            counts["stopped_for_time"] = True
            break
        if r["media_type"] == "document":
            ext = os.path.splitext(r["filename"] or "")[1].lower()
            if ext not in TEXT_EXT or (r["length"] or 0) > AUTO_MAX_BYTES:
                _save(conn, r["chat_jid"], r["msg_id"], r["media_type"], status="skipped",
                      error="not downloaded automatically (type or size); ask for it to fetch it")
                counts["skipped"] += 1
                continue
        try:
            res = process(conn, r["chat_jid"], r["msg_id"], r["media_type"], r["filename"], max_bytes=AUTO_MAX_BYTES)
        except Exception as exc:  # noqa: BLE001 (one odd attachment must not block the older ones)
            conn.rollback()
            _save(conn, r["chat_jid"], r["msg_id"], r["media_type"], status="failed",
                  error="%s: %s" % (type(exc).__name__, str(exc)[:200]), count_attempt=True)
            counts["failed"] += 1
            continue
        if res["status"] == "ok":
            counts["downloaded"] += 1
            if res.get("text_kind") == "transcript":
                counts["transcripts"] += 1
            elif res.get("text_kind") == "document":
                counts["texts"] += 1
        else:
            counts[res["status"] if res["status"] in counts else "failed"] += 1
    return counts


def remove_files(conn, where, args):
    for r in conn.execute("SELECT path FROM attachments WHERE " + where, args).fetchall():
        if r["path"]:
            shutil.rmtree(os.path.dirname(r["path"]), ignore_errors=True)
    conn.execute("DELETE FROM attachments WHERE " + where, args)
