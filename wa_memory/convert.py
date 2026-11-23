"""Turn WhatsApp messages (the protobufs the live link receives) into rows of the memory.

Pure functions plus `record`, which writes one message, an edit or a deletion. Used for live messages
and for the history the phone sends after linking (live.py); tested on made-up messages.
"""
import re

from . import store

SKIP_SERVERS = ("broadcast", "newsletter")
MEDIA_FIELDS = (("imageMessage", "image"), ("videoMessage", "video"), ("audioMessage", "audio"),
                ("documentMessage", "document"), ("stickerMessage", "sticker"))
WRAPPERS = ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "viewOnceMessageV2Extension",
            "documentWithCaptionMessage", "editedMessage")
AD_JID = re.compile(r"^([^@:.]+)(?:\.\d+)?(?::\d+)?@(.+)$")


def norm_jid(jid):
    """A person or chat JID without device parts ("4070…:12@s.whatsapp.net" -> "4070…@s.whatsapp.net")."""
    if not jid:
        return None
    m = AD_JID.match(jid)
    if not m:
        return jid
    server = "s.whatsapp.net" if m.group(2) == "c.us" else m.group(2)
    return "%s@%s" % (m.group(1), server)


def skipped_chat(jid):
    """Status updates, broadcast lists and channels are not conversations."""
    return not jid or jid == "status@broadcast" or jid.split("@")[-1] in SKIP_SERVERS


def chat_kind(jid):
    return "group" if jid.endswith("@g.us") else "dm"


def _has(msg, field):
    try:
        return msg.HasField(field)
    except ValueError:  # a field this protobuf version does not have
        return False


def unwrap(msg):
    """The real content inside disappearing, view-once, captioned-document and edit wrappers."""
    for _ in range(6):
        for field in WRAPPERS:
            if _has(msg, field):
                inner = getattr(msg, field)
                if _has(inner, "message"):
                    msg = inner.message
                    break
        else:
            return msg
    return msg


def _quoted(part):
    ci = getattr(part, "contextInfo", None)
    return (ci.stanzaID or None) if ci is not None and _has(part, "contextInfo") else None


def describe(msg):
    """What a message means for the memory, as a dict with "op":

    - {"op": "message", text, media_type, filename, quoted, ref}  (ref: what fetch.py needs, or None)
    - {"op": "revoke", target}                                     (deleted for everyone)
    - {"op": "edit", target, text}
    - None for things that are not messages (reactions, receipts, key distribution, polls votes, ...)
    """
    if msg is None:
        return None
    msg = unwrap(msg)
    if _has(msg, "protocolMessage"):
        pm = msg.protocolMessage
        kind = pm.Type.Name(pm.type) if _has(pm, "type") else ""
        if kind == "REVOKE" and pm.key.ID:
            return {"op": "revoke", "target": pm.key.ID}
        if kind == "MESSAGE_EDIT" and pm.key.ID:
            inner = describe(pm.editedMessage) if _has(pm, "editedMessage") else None
            text = inner.get("text") if inner and inner["op"] == "message" else None
            return {"op": "edit", "target": pm.key.ID, "text": text} if text else None
        return None
    out = {"op": "message", "text": None, "media_type": None, "filename": None, "quoted": None, "ref": None}
    if msg.conversation:
        out["text"] = msg.conversation
    elif _has(msg, "extendedTextMessage"):
        out["text"] = msg.extendedTextMessage.text or None
        out["quoted"] = _quoted(msg.extendedTextMessage)
    for field, kind in MEDIA_FIELDS:
        if _has(msg, field):
            part = getattr(msg, field)
            out["media_type"] = kind
            out["text"] = getattr(part, "caption", "") or out["text"]
            out["quoted"] = _quoted(part) or out["quoted"]
            filename = getattr(part, "fileName", "") or None
            out["filename"] = filename
            out["ref"] = {"kind": kind, "mime": part.mimetype or None, "url": part.URL or None,
                          "direct_path": part.directPath or None, "media_key": bytes(part.mediaKey) or None,
                          "enc_sha256": bytes(part.fileEncSHA256) or None, "sha256": bytes(part.fileSHA256) or None,
                          "length": int(part.fileLength or 0) or None, "filename": filename}
            break
    if out["media_type"] is None:
        if _has(msg, "locationMessage") or _has(msg, "liveLocationMessage"):
            loc = msg.locationMessage if _has(msg, "locationMessage") else msg.liveLocationMessage
            label = ", ".join(x for x in (getattr(loc, "name", ""), getattr(loc, "address", "")) if x)
            out["media_type"] = "location"
            out["text"] = "Location: %s(%.5f, %.5f)" % (label + " " if label else "", loc.degreesLatitude, loc.degreesLongitude)
        elif _has(msg, "contactMessage"):
            out["text"] = "Contact card: %s" % (msg.contactMessage.displayName or "unnamed")
        elif _has(msg, "contactsArrayMessage"):
            names = [c.displayName for c in msg.contactsArrayMessage.contacts if c.displayName]
            out["text"] = "Contact cards: %s" % ", ".join(names or ["unnamed"])
        else:
            for field in ("pollCreationMessage", "pollCreationMessageV2", "pollCreationMessageV3"):
                if _has(msg, field):
                    poll = getattr(msg, field)
                    options = [o.optionName for o in poll.options if o.optionName]
                    out["text"] = "Poll: %s (%s)" % (poll.name, " / ".join(options))
                    break
    if not out["text"] and not out["media_type"]:
        return None
    return out


def _chat(mem, jid, name=None, ts=None):
    mem.execute(
        "INSERT INTO chats(jid,kind,name,last_ts,source) VALUES(?,?,?,?, 'live') ON CONFLICT(jid) DO UPDATE SET "
        "name=COALESCE(excluded.name, chats.name), last_ts=max(COALESCE(chats.last_ts,0), COALESCE(excluded.last_ts,0))",
        (jid, chat_kind(jid), name or None, ts))


def contact(mem, jid, name=None, phone=None, saved=False):
    """A contact's name. `saved` (from the phone's address book) wins over a name the person set on WhatsApp."""
    jid = norm_jid(jid)
    if not jid or skipped_chat(jid):
        return
    if phone is None and jid.endswith("@s.whatsapp.net"):
        phone = "+" + jid.split("@")[0]
    if saved:
        mem.execute("INSERT INTO contacts(jid,name,phone,source) VALUES(?,?,?,'live') ON CONFLICT(jid) DO UPDATE SET "
                    "name=COALESCE(excluded.name, contacts.name), phone=COALESCE(contacts.phone, excluded.phone)",
                    (jid, name or None, phone))
    else:
        mem.execute("INSERT INTO contacts(jid,name,phone,source) VALUES(?,?,?,'live') ON CONFLICT(jid) DO UPDATE SET "
                    "name=COALESCE(contacts.name, excluded.name), phone=COALESCE(contacts.phone, excluded.phone)",
                    (jid, name or None, phone))


def save_ref(mem, chat_jid, msg_id, ref):
    mem.execute(
        "INSERT INTO media_refs(chat_jid,msg_id,kind,mime,url,direct_path,media_key,enc_sha256,sha256,length,filename) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(msg_id, chat_jid) DO UPDATE SET kind=excluded.kind, mime=excluded.mime, "
        "url=excluded.url, direct_path=excluded.direct_path, media_key=excluded.media_key, enc_sha256=excluded.enc_sha256, "
        "sha256=excluded.sha256, length=excluded.length, filename=excluded.filename",
        (chat_jid, msg_id, ref["kind"], ref["mime"], ref["url"], ref["direct_path"], ref["media_key"], ref["enc_sha256"],
         ref["sha256"], ref["length"], ref["filename"]))


def seconds(ts):
    ts = int(ts or 0)
    return ts // 1000 if ts > 10 ** 11 else ts


def record(mem, chat, msg_id, sender, sender_name, from_me, ts, msg, excluded=frozenset(), chat_name=None,
           sender_alt=None):
    """Write one received or historical message. Returns 'inserted', 'updated', 'duplicate', 'tombstoned',
    'deleted', 'edited' or 'skipped'. A deletion or an edit applies only to a message of the same chat by the same
    author (`sender`, or its other identity `sender_alt`: phone number vs LID)."""
    chat = norm_jid(chat)
    if skipped_chat(chat) or chat in excluded or not msg_id:
        return "skipped"
    d = describe(msg)
    if d is None:
        return "skipped"
    ts = seconds(ts)
    senders = set() if from_me else {j for j in (norm_jid(sender), norm_jid(sender_alt)) if j}
    if d["op"] in ("revoke", "edit"):
        row = mem.execute("SELECT from_me, sender_jid FROM messages WHERE chat_jid=? AND msg_id=?",
                          (chat, d["target"])).fetchone()
        if row is not None and not store.same_author(chat, row["from_me"], row["sender_jid"], from_me, senders):
            return "skipped"
        if d["op"] == "revoke":
            author = row["sender_jid"] if row is not None else (None if from_me else norm_jid(sender))
            store.tombstone(mem, chat, d["target"], "revoked", from_me, author)
            return "deleted"
        if row is None:
            return "skipped"
        mem.execute("UPDATE messages SET text=? WHERE chat_jid=? AND msg_id=?", (d["text"], chat, d["target"]))
        return "edited"
    sender = norm_jid(sender) if not from_me else None
    _chat(mem, chat, chat_name, ts)
    if sender and sender_name:
        contact(mem, sender, sender_name)
    result = store.upsert_message(mem, {
        "chat_jid": chat, "msg_id": msg_id, "sender_jid": sender, "sender_name": None if from_me else (sender_name or None),
        "from_me": 1 if from_me else 0, "ts": ts, "text": d["text"], "media_type": d["media_type"],
        "filename": d["filename"], "quoted_msg_id": d["quoted"], "source": "live"})
    if d["ref"] is not None and result in ("inserted", "updated"):
        save_ref(mem, chat, msg_id, d["ref"])
    return result


def record_history(mem, data, excluded=frozenset()):
    """Write one chunk of the history the phone sends after linking. Returns counts."""
    counts = {}
    for pn in data.pushnames:
        if pn.ID and pn.pushname:
            contact(mem, pn.ID, pn.pushname)
    for conv in data.conversations:
        chat = norm_jid(conv.ID)
        if skipped_chat(chat) or chat in excluded:
            continue
        name = conv.name or conv.displayName or None
        _chat(mem, chat, name, seconds(conv.conversationTimestamp) or None)
        for hm in conv.messages:
            w = hm.message
            key = w.key
            from_me = bool(key.fromMe)
            sender = key.participant or w.participant or (None if from_me else chat)
            mem.execute("SAVEPOINT one")
            try:
                r = record(mem, chat, key.ID, sender, w.pushName or None, from_me, w.messageTimestamp,
                           w.message if w.HasField("message") else None, excluded, chat_name=name)
                mem.execute("RELEASE one")
            except Exception:  # noqa: BLE001 (one odd message must not lose the rest of the chunk)
                mem.execute("ROLLBACK TO one")
                mem.execute("RELEASE one")
                r = "failed"
            counts[r] = counts.get(r, 0) + 1
    return counts
