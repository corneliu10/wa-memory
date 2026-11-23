"""The memory database: one local SQLite file holding every message from every source.

Sources: 'live' (wa-memory's own linked device, see live.py) and 'ios' (WhatsApp's ChatStorage.sqlite
from an iPhone backup). A message is the same message in both when its WhatsApp message ID matches.
Standard library only.
"""
import os
import re
import sqlite3
import time
import unicodedata
from datetime import datetime

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS chats(
  jid TEXT PRIMARY KEY, kind TEXT, name TEXT, last_ts INTEGER, source TEXT);
CREATE TABLE IF NOT EXISTS contacts(jid TEXT PRIMARY KEY, name TEXT, phone TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY,
  chat_jid TEXT NOT NULL,
  msg_id TEXT NOT NULL,
  sender_jid TEXT,
  sender_name TEXT,
  from_me INTEGER NOT NULL DEFAULT 0,
  ts INTEGER NOT NULL,
  text TEXT,
  media_type TEXT,
  filename TEXT,
  quoted_msg_id TEXT,
  source TEXT NOT NULL,
  gen INTEGER NOT NULL DEFAULT 0,
  UNIQUE(msg_id, chat_jid));
CREATE INDEX IF NOT EXISTS msg_chat_ts ON messages(chat_jid, ts);
CREATE INDEX IF NOT EXISTS msg_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS msg_sender ON messages(sender_jid);
CREATE INDEX IF NOT EXISTS msg_source ON messages(source, gen);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text, sender_name, filename,
  content='messages', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text, sender_name, filename)
  VALUES (new.id, COALESCE(new.text,''), COALESCE(new.sender_name,''), COALESCE(new.filename,''));
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text, sender_name, filename)
  VALUES ('delete', old.id, COALESCE(old.text,''), COALESCE(old.sender_name,''), COALESCE(old.filename,''));
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text, sender_name, filename ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text, sender_name, filename)
  VALUES ('delete', old.id, COALESCE(old.text,''), COALESCE(old.sender_name,''), COALESCE(old.filename,''));
  INSERT INTO messages_fts(rowid, text, sender_name, filename)
  VALUES (new.id, COALESCE(new.text,''), COALESCE(new.sender_name,''), COALESCE(new.filename,''));
END;
-- Messages deleted for everyone, per chat, with who deleted them: a later copy (history, a backup) of the same
-- message from the same author is not stored again.
CREATE TABLE IF NOT EXISTS tombstones(
  chat_jid TEXT NOT NULL, msg_id TEXT NOT NULL, from_me INTEGER, sender_jid TEXT, at INTEGER NOT NULL, reason TEXT,
  PRIMARY KEY(chat_jid, msg_id));
-- What is needed to download an attachment later, straight from WhatsApp's media servers (fetch.py).
CREATE TABLE IF NOT EXISTS media_refs(
  chat_jid TEXT NOT NULL,
  msg_id TEXT NOT NULL,
  kind TEXT NOT NULL,              -- image | video | audio | document | sticker
  mime TEXT, url TEXT, direct_path TEXT,
  media_key BLOB, enc_sha256 BLOB, sha256 BLOB,
  length INTEGER, filename TEXT,
  PRIMARY KEY(msg_id, chat_jid));
CREATE TABLE IF NOT EXISTS attachments(
  id INTEGER PRIMARY KEY,
  chat_jid TEXT NOT NULL,
  msg_id TEXT NOT NULL,
  media_type TEXT,
  path TEXT, mime TEXT, bytes INTEGER,
  status TEXT NOT NULL,            -- ok | expired | failed | too_big | skipped
  error TEXT,
  text TEXT,                       -- document text or voice-note transcript
  text_kind TEXT,                  -- document | transcript
  attempts INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  UNIQUE(msg_id, chat_jid));
CREATE VIRTUAL TABLE IF NOT EXISTS attachments_fts USING fts5(
  text, content='attachments', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS attachments_ai AFTER INSERT ON attachments BEGIN
  INSERT INTO attachments_fts(rowid, text) VALUES (new.id, COALESCE(new.text,''));
END;
CREATE TRIGGER IF NOT EXISTS attachments_ad AFTER DELETE ON attachments BEGIN
  INSERT INTO attachments_fts(attachments_fts, rowid, text) VALUES ('delete', old.id, COALESCE(old.text,''));
END;
CREATE TRIGGER IF NOT EXISTS attachments_au AFTER UPDATE OF text ON attachments BEGIN
  INSERT INTO attachments_fts(attachments_fts, rowid, text) VALUES ('delete', old.id, COALESCE(old.text,''));
  INSERT INTO attachments_fts(rowid, text) VALUES (new.id, COALESCE(new.text,''));
END;
"""

SKIP_KINDS = {"broadcast", "newsletter"}  # status, channels and broadcast lists are not conversations


def home():
    return os.path.expanduser(os.environ.get("WA_MEMORY_HOME", "~/.wa-memory"))


def private_home():
    """The data folder, created owner-only (0700), and owner-only files from here on (umask 077)."""
    os.umask(0o077)
    h = home()
    os.makedirs(h, mode=0o700, exist_ok=True)
    try:
        os.chmod(h, 0o700)
    except OSError:
        pass
    return h


def open_memory(path=None, threads=False):
    """The memory database. `threads`: one connection shared by the live link's callback threads (callers lock)."""
    private_home()
    path = path or os.path.join(home(), "memory.db")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=not threads)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    for p in (path, path + "-wal", path + "-shm"):
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    return conn


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, None if value is None else str(value)))


def fold(s):
    """Lower-case and strip diacritics, so 'Ședință' matches 'sedinta'."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()


def jid_user(jid):
    return (jid or "").split("@")[0]


def iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts)).astimezone().isoformat(timespec="minutes")


def parse_when(value):
    """'2024-05-01', '2024-05-01T10:00', '2024-05-01 10:00' or unix seconds -> unix seconds (local time)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    v = str(value).strip()
    if re.fullmatch(r"\d{9,11}", v):
        return int(v)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m"):
        try:
            return int(time.mktime(datetime.strptime(v, fmt).timetuple()))
        except ValueError:
            pass
    raise ValueError("Unrecognised date %r; use YYYY-MM-DD or YYYY-MM-DDTHH:MM" % value)


def exclusions(conn):
    """JIDs to keep out of memory: lines of exclude.txt (a JID, or an exact chat or contact name)."""
    path = os.path.join(home(), "exclude.txt")
    if not os.path.exists(path):
        return set()
    wanted = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                wanted.append(line)
    jids = set()
    for w in wanted:
        if "@" in w:
            user, _, server = w.partition("@")
            user = user.split(":")[0].split(".")[0]
            jids.add("%s@%s" % (user, "s.whatsapp.net" if server == "c.us" else server))
            continue
        f = fold(w)
        for row in conn.execute("SELECT jid, name FROM chats UNION ALL SELECT jid, name FROM contacts"):
            if row["name"] and fold(row["name"]) == f:
                jids.add(row["jid"])
    return jids


def apply_exclusions(conn):
    from . import media
    ex = exclusions(conn)
    n = 0
    for jid in ex:
        n += conn.execute("DELETE FROM messages WHERE chat_jid=?", (jid,)).rowcount
        conn.execute("DELETE FROM media_refs WHERE chat_jid=?", (jid,))
        media.remove_files(conn, "chat_jid=?", (jid,))
    return ex, n


def same_author(chat_jid, from_me, sender_jid, other_from_me, other_senders):
    """Whether a message by (from_me, sender_jid) and one by (other_from_me, any of other_senders) have the same
    author. In a group the sender must match; in a direct chat, "not from me" can only be the other person."""
    if bool(from_me) != bool(other_from_me):
        return False
    if from_me:
        return True
    if sender_jid and other_senders:
        return sender_jid in other_senders
    return not chat_jid.endswith("@g.us")


def upsert_message(conn, m, gen=0):
    """Insert or update one message dict. Returns 'inserted', 'updated', 'duplicate' or 'tombstoned'.
    A message with the ID of a stored one by another author is ignored ('duplicate'): IDs are not secrets."""
    senders = {m["sender_jid"]} if m.get("sender_jid") else set()
    t = conn.execute("SELECT from_me, sender_jid FROM tombstones WHERE chat_jid=? AND msg_id=?",
                     (m["chat_jid"], m["msg_id"])).fetchone()
    if t is not None and same_author(m["chat_jid"], t["from_me"], t["sender_jid"], m.get("from_me"), senders):
        return "tombstoned"
    row = conn.execute("SELECT id, source, from_me, sender_jid FROM messages WHERE msg_id=? AND chat_jid=?",
                       (m["msg_id"], m["chat_jid"])).fetchone()
    if row is not None and not same_author(m["chat_jid"], row["from_me"], row["sender_jid"], m.get("from_me"), senders):
        return "duplicate"
    if row is None:
        # Same message already stored under another chat identity (phone JID vs LID)?
        other = conn.execute("SELECT id, source FROM messages WHERE msg_id=? AND from_me=? AND abs(ts-?)<=120",
                             (m["msg_id"], int(m.get("from_me") or 0), int(m["ts"]))).fetchone()
        if other is not None:
            if m["source"] == "live":
                conn.execute("UPDATE messages SET gen=? WHERE id=?", (gen, other["id"]))
            return "duplicate"
        conn.execute(
            "INSERT INTO messages(chat_jid,msg_id,sender_jid,sender_name,from_me,ts,text,media_type,filename,"
            "quoted_msg_id,source,gen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (m["chat_jid"], m["msg_id"], m.get("sender_jid"), m.get("sender_name"), int(m.get("from_me") or 0),
             int(m["ts"]), m.get("text"), m.get("media_type"), m.get("filename"), m.get("quoted_msg_id"),
             m["source"], gen))
        return "inserted"
    if m["source"] == "live" or row["source"] == m["source"]:
        # The live copy is newest (edits); a backup never overwrites it.
        conn.execute(
            "UPDATE messages SET sender_jid=COALESCE(?,sender_jid), sender_name=COALESCE(?,sender_name), ts=?, "
            "text=?, media_type=?, filename=?, quoted_msg_id=?, source=?, gen=? WHERE id=?",
            (m.get("sender_jid"), m.get("sender_name"), int(m["ts"]), m.get("text"), m.get("media_type"),
             m.get("filename"), m.get("quoted_msg_id"), m["source"], gen, row["id"]))
        return "updated"
    return "duplicate"


def tombstone(conn, chat_jid, msg_id, reason, from_me=None, sender_jid=None):
    """Forget one message of one chat (deleted for everyone), with its attachment, and remember who deleted it."""
    from . import media
    conn.execute("DELETE FROM messages WHERE chat_jid=? AND msg_id=?", (chat_jid, msg_id))
    conn.execute("DELETE FROM media_refs WHERE chat_jid=? AND msg_id=?", (chat_jid, msg_id))
    media.remove_files(conn, "chat_jid=? AND msg_id=?", (chat_jid, msg_id))
    conn.execute("INSERT OR REPLACE INTO tombstones(chat_jid, msg_id, from_me, sender_jid, at, reason) VALUES(?,?,?,?,?,?)",
                 (chat_jid, msg_id, None if from_me is None else int(bool(from_me)), sender_jid, int(time.time()), reason))
