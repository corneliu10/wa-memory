"""Import the whole WhatsApp history from an iPhone backup (WhatsApp's own ChatStorage.sqlite).

Accepts a ChatStorage.sqlite file, or an unencrypted Finder backup folder
(~/Library/Application Support/MobileSync/Backup/<device id>). Column names follow WhatsApp's
Core Data schema as read by WhatsApp-Chat-Exporter (MIT). Reads the backup read-only.
"""
import os
import pathlib
import plistlib
import sqlite3

from . import store

APPLE_EPOCH = 978307200  # 2001-01-01 in unix seconds
FILE_IDS = {
    "whatsapp": "7c7fba66680ef796b916b067077cc246adacf01d",   # AppDomainGroup-group.net.whatsapp.WhatsApp.shared-ChatStorage.sqlite
    "business": "724bd3b98b18518b455a87c1f3ac3a0d189c4466",   # ...WhatsAppSMB.shared-ChatStorage.sqlite
}
MEDIA_TYPES = {1: "image", 2: "video", 3: "audio", 4: "contact", 5: "location", 8: "document", 11: "gif", 15: "sticker"}
TEXT_TYPES = {0, 7}            # 7 = text with a link preview
SKIP_TYPES = {6, 10, 14}       # group/system events, calls and "message deleted" placeholders


class EncryptedBackup(Exception):
    pass


def find_chatstorage(path, app="whatsapp"):
    path = os.path.expanduser(path)
    if os.path.isfile(path):
        return path
    manifest = os.path.join(path, "Manifest.plist")
    if os.path.exists(manifest):
        with open(manifest, "rb") as fh:
            info = plistlib.load(fh)
        if info.get("IsEncrypted"):
            raise EncryptedBackup(
                "This Finder backup is encrypted, which wa-memory cannot read yet. Make an unencrypted backup, or "
                "decrypt ChatStorage.sqlite with another tool and pass that file.")
    fid = FILE_IDS[app]
    candidate = os.path.join(path, fid[:2], fid)
    if os.path.isfile(candidate):
        return candidate
    raise FileNotFoundError("No WhatsApp ChatStorage.sqlite in %s" % path)


def _kind(jid):
    jid = jid or ""
    if jid.endswith("@g.us"):
        return "group"
    if jid.endswith("@newsletter"):
        return "newsletter"
    if jid.endswith("@broadcast") or jid.startswith("status"):
        return "broadcast"
    return "dm"


def _cols(src, table):
    return {r[1] for r in src.execute("PRAGMA table_info(%s)" % table)}


def import_chatstorage(mem, path, app="whatsapp"):
    db = find_chatstorage(path, app)
    src = sqlite3.connect(pathlib.Path(db).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    src.row_factory = sqlite3.Row
    counts = {"inserted": 0, "updated": 0, "duplicate": 0, "tombstoned": 0, "skipped": 0,
              "oldest": None, "newest": None, "source_file": os.path.basename(db)}
    try:
        excluded = store.exclusions(mem)
        sessions = {}
        for r in src.execute("SELECT Z_PK, ZCONTACTJID, ZPARTNERNAME FROM ZWACHATSESSION"):
            jid = r["ZCONTACTJID"]
            if not jid:
                continue
            sessions[r["Z_PK"]] = (jid, r["ZPARTNERNAME"])
            mem.execute("INSERT INTO chats(jid,kind,name,last_ts,source) VALUES(?,?,?,NULL,'ios') "
                        "ON CONFLICT(jid) DO UPDATE SET name=COALESCE(chats.name, excluded.name)",
                        (jid, _kind(jid), r["ZPARTNERNAME"]))
            if _kind(jid) == "dm" and r["ZPARTNERNAME"]:
                mem.execute("INSERT INTO contacts(jid,name,phone,source) VALUES(?,?,?,'ios') "
                            "ON CONFLICT(jid) DO UPDATE SET name=COALESCE(contacts.name, excluded.name)",
                            (jid, r["ZPARTNERNAME"], store.jid_user(jid)))
        push = {}
        if "ZWAPROFILEPUSHNAME" in {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            for r in src.execute("SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME"):
                push[r["ZJID"]] = r["ZPUSHNAME"]
        gm_cols = _cols(src, "ZWAGROUPMEMBER")
        name_col = "ZCONTACTNAME" if "ZCONTACTNAME" in gm_cols else "NULL"
        members = {r["Z_PK"]: (r["ZMEMBERJID"], r["NAME"])
                   for r in src.execute("SELECT Z_PK, ZMEMBERJID, %s AS NAME FROM ZWAGROUPMEMBER" % name_col)}
        contact_names = {r["jid"]: r["name"] for r in mem.execute("SELECT jid, name FROM contacts WHERE name IS NOT NULL")}

        q = ("SELECT m.Z_PK, m.ZCHATSESSION, m.ZISFROMME, m.ZMESSAGEDATE, m.ZTEXT, m.ZMESSAGETYPE, "
             "m.ZGROUPMEMBER, m.ZSTANZAID, mi.ZTITLE AS CAPTION, mi.ZMEDIALOCALPATH AS MEDIAPATH "
             "FROM ZWAMESSAGE m LEFT JOIN ZWAMEDIAITEM mi ON mi.ZMESSAGE = m.Z_PK ORDER BY m.ZMESSAGEDATE")
        n = 0
        for r in src.execute(q):
            sess = sessions.get(r["ZCHATSESSION"])
            mtype = r["ZMESSAGETYPE"]
            if not sess or not r["ZSTANZAID"] or r["ZMESSAGEDATE"] is None or mtype in SKIP_TYPES:
                counts["skipped"] += 1
                continue
            chat_jid, partner = sess
            if chat_jid in excluded or _kind(chat_jid) in store.SKIP_KINDS:
                counts["skipped"] += 1
                continue
            media = MEDIA_TYPES.get(mtype)
            text = r["ZTEXT"] if (mtype in TEXT_TYPES or media is None) else (r["CAPTION"] or r["ZTEXT"])
            if not text and not media:
                counts["skipped"] += 1
                continue
            from_me = 1 if r["ZISFROMME"] else 0
            if from_me:
                sender_jid, sender_name = None, None
            elif _kind(chat_jid) == "group":
                sender_jid, sender_name = members.get(r["ZGROUPMEMBER"], (None, None))
                sender_name = sender_name or contact_names.get(sender_jid) or push.get(sender_jid)
            else:
                sender_jid, sender_name = chat_jid, partner or push.get(chat_jid)
            ts = int(APPLE_EPOCH + r["ZMESSAGEDATE"])
            filename = os.path.basename(r["MEDIAPATH"]) if (media and r["MEDIAPATH"]) else None
            res = store.upsert_message(mem, {
                "chat_jid": chat_jid, "msg_id": r["ZSTANZAID"], "sender_jid": sender_jid,
                "sender_name": sender_name, "from_me": from_me, "ts": ts, "text": text,
                "media_type": media, "filename": filename, "quoted_msg_id": None, "source": "ios"})
            counts[res] += 1
            counts["oldest"] = ts if counts["oldest"] is None else min(counts["oldest"], ts)
            counts["newest"] = ts if counts["newest"] is None else max(counts["newest"], ts)
            n += 1
            if n % 20000 == 0:
                mem.commit()
        mem.execute("UPDATE chats SET last_ts=(SELECT max(ts) FROM messages WHERE chat_jid=chats.jid) "
                    "WHERE last_ts IS NULL OR last_ts < COALESCE((SELECT max(ts) FROM messages WHERE chat_jid=chats.jid),0)")
        store.set_meta(mem, "ios_imported_at", int(__import__("time").time()))
        mem.commit()
        counts["oldest"] = store.iso(counts["oldest"])
        counts["newest"] = store.iso(counts["newest"])
        return counts
    finally:
        src.close()
