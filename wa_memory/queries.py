"""Read-only questions over the memory database. Every function returns JSON-ready dicts."""
import os
import re
import time

from . import store

MAX_LIMIT = 200


def _prep(conn):
    conn.create_function("fold", 1, store.fold, deterministic=True)


def _names(conn):
    names = {}
    for r in conn.execute("SELECT jid, name FROM contacts WHERE name IS NOT NULL AND name != ''"):
        names[r["jid"]] = r["name"]
    for r in conn.execute("SELECT jid, name FROM chats WHERE name IS NOT NULL AND name != ''"):
        names.setdefault(r["jid"], r["name"])
    return names


def _who(m, names):
    if m["from_me"]:
        return "me"
    return m["sender_name"] or names.get(m["sender_jid"]) or store.jid_user(m["sender_jid"]) or "?"


def _chat_name(jid, names):
    return names.get(jid) or store.jid_user(jid)


def _msg(m, names, max_chars=1500, with_chat=False):
    text = m["text"] or ""
    if len(text) > max_chars:
        text = text[:max_chars] + "… [cut]"
    out = {"time": store.iso(m["ts"]), "from": _who(m, names), "text": text}
    if m["media_type"]:
        out["media"] = m["media_type"] + ((": " + m["filename"]) if m["filename"] else "")
    if with_chat:
        out["chat"] = _chat_name(m["chat_jid"], names)
        out["chat_id"] = m["chat_jid"]
    out["msg_id"] = m["msg_id"]
    return out


def _limit(n, default):
    try:
        n = int(n) if n is not None else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_LIMIT))


def resolve_chats(conn, chat, dm_only=False):
    """A JID, a phone number, or part of a chat/contact name -> list of (jid, name)."""
    _prep(conn)
    chat = (chat or "").strip()
    if not chat:
        return []
    if "@" in chat:
        return [(chat, _names(conn).get(chat, store.jid_user(chat)))]
    digits = re.sub(r"\D", "", chat)
    names = _names(conn)
    if len(digits) >= 6 and len(digits) >= len(chat.replace(" ", "")) - 2:
        rows = conn.execute("SELECT jid FROM chats WHERE jid LIKE ? UNION SELECT jid FROM contacts WHERE phone LIKE ? OR jid LIKE ?",
                            ("%" + digits + "%@%", "%" + digits + "%", "%" + digits + "%@%")).fetchall()
        return [(r["jid"], _chat_name(r["jid"], names)) for r in rows]
    f = store.fold(chat)
    rows = conn.execute(
        "SELECT jid, name, kind FROM chats WHERE fold(name) LIKE ? "
        "UNION SELECT jid, name, 'dm' FROM contacts WHERE fold(name) LIKE ?", ("%" + f + "%", "%" + f + "%")).fetchall()
    seen, out = set(), []
    exact = [r for r in rows if store.fold(r["name"]) == f]
    for r in (exact or rows):
        if dm_only and r["jid"].endswith("@g.us"):
            continue
        if r["jid"] not in seen:
            seen.add(r["jid"])
            out.append((r["jid"], r["name"]))
    # Prefer chats that actually have messages.
    has = {r[0] for r in conn.execute("SELECT DISTINCT chat_jid FROM messages WHERE chat_jid IN (%s)" %
                                      ",".join("?" * len(out)), [j for j, _ in out])} if out else set()
    out.sort(key=lambda x: (x[0] not in has, x[1] or ""))
    return out


def fts_query(q):
    """Words become prefix terms joined by AND; "quoted phrases" stay phrases; OR between terms is kept."""
    q = q or ""
    parts = []
    for phrase, word in re.findall(r'"([^"]+)"|(\S+)', q):
        if phrase:
            words = re.findall(r"\w+", phrase, re.UNICODE)
            if words:
                parts.append('"%s"' % " ".join(words))
        elif word == "OR" and parts and parts[-1] != "OR":
            parts.append("OR")
        else:
            for w in re.findall(r"\w+", word, re.UNICODE):
                parts.append('"%s"*' % w)
    while parts and parts[-1] == "OR":
        parts.pop()
    return " ".join(parts)


def search(conn, query, chat=None, sender=None, after=None, before=None, sort="relevance", limit=20):
    expr = fts_query(query)
    if not expr:
        return {"error": "Give at least one word to search for."}
    names = _names(conn)
    where, args = ["messages_fts MATCH ?"], [expr]
    if chat:
        jids = [j for j, _ in resolve_chats(conn, chat)]
        if not jids:
            return {"results": [], "note": "No chat matches %r." % chat}
        where.append("m.chat_jid IN (%s)" % ",".join("?" * len(jids)))
        args += jids
    if sender:
        if store.fold(sender) in ("me", "eu"):
            where.append("m.from_me = 1")
        else:
            jids = [j for j, _ in resolve_chats(conn, sender, dm_only=True)]
            if not jids:
                return {"results": [], "note": "No contact matches %r." % sender}
            where.append("m.from_me = 0 AND (m.sender_jid IN (%s) OR fold(m.sender_name) LIKE ?)" % ",".join("?" * len(jids)))
            args += jids + ["%" + store.fold(sender) + "%"]
    if after:
        where.append("m.ts >= ?")
        args.append(store.parse_when(after))
    if before:
        where.append("m.ts < ?")
        args.append(store.parse_when(before))
    _prep(conn)
    n = _limit(limit, 20)
    rows = conn.execute(
        "SELECT m.*, snippet(messages_fts, 0, '«', '»', '…', 24) AS snip, bm25(messages_fts) AS score, NULL AS kind "
        "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid WHERE %s ORDER BY %s LIMIT ?"
        % (" AND ".join(where), "m.ts DESC" if sort == "recent" else "score"), args + [n]).fetchall()
    awhere = ["attachments_fts MATCH ?"] + where[1:]
    arows = conn.execute(
        "SELECT m.*, snippet(attachments_fts, 0, '«', '»', '…', 24) AS snip, bm25(attachments_fts) AS score, "
        "a.text_kind AS kind FROM attachments_fts JOIN attachments a ON a.id = attachments_fts.rowid "
        "JOIN messages m ON m.msg_id = a.msg_id AND m.chat_jid = a.chat_jid WHERE %s ORDER BY %s LIMIT ?"
        % (" AND ".join(awhere), "m.ts DESC" if sort == "recent" else "score"), args + [n]).fetchall()
    merged, seen = [], set()
    for r in sorted(list(rows) + list(arows), key=(lambda r: -r["ts"]) if sort == "recent" else (lambda r: r["score"])):
        if r["msg_id"] in seen:
            continue
        seen.add(r["msg_id"])
        item = _msg(r, names, max_chars=400, with_chat=True)
        item["match"] = r["snip"]
        if r["kind"]:
            item["found_in"] = "voice note transcript" if r["kind"] == "transcript" else "attached document"
        merged.append(item)
    results = merged[:n]
    return {"query": expr, "count": len(results), "results": results}


def list_chats(conn, query=None, kind=None, active_after=None, limit=30):
    names = _names(conn)
    stats = {r["chat_jid"]: r for r in conn.execute(
        "SELECT chat_jid, count(*) AS n, min(ts) AS first, max(ts) AS last FROM messages GROUP BY chat_jid")}
    if query:
        jids = [j for j, _ in resolve_chats(conn, query)]
    else:
        jids = list(stats.keys())
    kinds = {r["jid"]: r["kind"] for r in conn.execute("SELECT jid, kind FROM chats")}
    after = store.parse_when(active_after) if active_after else None
    out = []
    for jid in jids:
        s = stats.get(jid)
        k = kinds.get(jid) or ("group" if jid.endswith("@g.us") else "dm")
        if kind and k != kind:
            continue
        if after and (not s or s["last"] < after):
            continue
        out.append({"chat": _chat_name(jid, names), "chat_id": jid, "kind": k,
                    "messages": s["n"] if s else 0,
                    "first": store.iso(s["first"]) if s else None, "last": store.iso(s["last"]) if s else None,
                    "_last": s["last"] if s else 0})
    out.sort(key=lambda x: -x["_last"])
    for o in out:
        o.pop("_last")
    return {"count": len(out), "chats": out[:_limit(limit, 30)]}


def read_chat(conn, chat, before=None, after=None, around_msg_id=None, limit=50):
    cands = resolve_chats(conn, chat)
    if not cands:
        return {"error": "No chat matches %r. Use whatsapp_list_chats to find it." % chat}
    if len(cands) > 1 and "@" not in chat:
        exact = [c for c in cands if store.fold(c[1]) == store.fold(chat)]
        if len(exact) != 1:
            return {"ambiguous": [{"chat": n, "chat_id": j} for j, n in cands[:15]],
                    "note": "Several chats match; call again with chat_id."}
        cands = exact
    jid, name = cands[0]
    names = _names(conn)
    n = _limit(limit, 50)
    if around_msg_id:
        row = conn.execute("SELECT ts FROM messages WHERE chat_jid=? AND msg_id=?", (jid, around_msg_id)).fetchone()
        if not row:
            return {"error": "Message %s not found in this chat." % around_msg_id}
        half = max(1, n // 2)
        older = conn.execute("SELECT * FROM messages WHERE chat_jid=? AND ts < ? ORDER BY ts DESC, id DESC LIMIT ?",
                             (jid, row["ts"], half)).fetchall()[::-1]
        newer = conn.execute("SELECT * FROM messages WHERE chat_jid=? AND ts >= ? ORDER BY ts, id LIMIT ?",
                             (jid, row["ts"], n - len(older))).fetchall()
        rows = older + newer
    elif after:
        rows = conn.execute("SELECT * FROM messages WHERE chat_jid=? AND ts >= ? ORDER BY ts, id LIMIT ?",
                            (jid, store.parse_when(after), n)).fetchall()
    else:
        b = store.parse_when(before) if before else int(time.time()) + 86400
        rows = conn.execute("SELECT * FROM messages WHERE chat_jid=? AND ts < ? ORDER BY ts DESC, id DESC LIMIT ?",
                            (jid, b, n)).fetchall()[::-1]
    total = conn.execute("SELECT count(*), min(ts) FROM messages WHERE chat_jid=?", (jid,)).fetchone()
    return {"chat": name or _chat_name(jid, names), "chat_id": jid, "messages_in_chat": total[0],
            "chat_starts": store.iso(total[1]), "shown": len(rows),
            "messages": _with_attachments(conn, rows, [_msg(r, names) for r in rows]),
            "tip": "For older messages call again with before = the first shown time. "
                   "Open an attachment with whatsapp_get_attachment(msg_id)."}


def _with_attachments(conn, rows, items):
    ids = [r["msg_id"] for r in rows if r["media_type"]]
    if not ids:
        return items
    att = {a["msg_id"]: a for a in conn.execute(
        "SELECT msg_id, status, text, text_kind FROM attachments WHERE msg_id IN (%s)" % ",".join("?" * len(ids)), ids)}
    for item in items:
        a = att.get(item["msg_id"])
        if not a:
            continue
        if a["text_kind"] == "transcript" and a["text"]:
            item["transcript"] = a["text"][:1500]
        elif a["text_kind"] == "document" and a["text"]:
            item["document_preview"] = a["text"][:300]
        if a["status"] == "expired":
            item["attachment"] = "expired on WhatsApp's servers"
    return items


def list_attachments(conn, chat=None, media_type=None, after=None, before=None, limit=30):
    names = _names(conn)
    where, args = ["m.media_type IS NOT NULL"], []
    if chat:
        jids = [j for j, _ in resolve_chats(conn, chat)]
        if not jids:
            return {"error": "No chat matches %r." % chat}
        where.append("m.chat_jid IN (%s)" % ",".join("?" * len(jids)))
        args += jids
    if media_type:
        where.append("m.media_type = ?")
        args.append(media_type)
    if after:
        where.append("m.ts >= ?")
        args.append(store.parse_when(after))
    if before:
        where.append("m.ts < ?")
        args.append(store.parse_when(before))
    rows = conn.execute("SELECT m.*, a.status AS astatus FROM messages m LEFT JOIN attachments a "
                        "ON a.msg_id = m.msg_id AND a.chat_jid = m.chat_jid WHERE %s ORDER BY m.ts DESC LIMIT ?"
                        % " AND ".join(where), args + [_limit(limit, 30)]).fetchall()
    out = []
    for r in rows:
        item = _msg(r, names, max_chars=200, with_chat=True)
        item["downloaded"] = {"ok": "yes", "expired": "expired on WhatsApp's servers"}.get(r["astatus"], "not yet")
        out.append(item)
    return {"count": len(out), "attachments": _with_attachments(conn, rows, out),
            "tip": "Open one with whatsapp_get_attachment(msg_id): pictures are shown, documents read, voice notes transcribed."}


def person(conn, who, recent=10):
    cands = resolve_chats(conn, who, dm_only=True)
    if not cands:
        return {"error": "No contact matches %r." % who}
    names = _names(conn)
    people = []
    for jid, name in cands[:5]:
        dm = conn.execute("SELECT count(*) AS n, sum(from_me) AS mine, min(ts) AS first, max(ts) AS last "
                          "FROM messages WHERE chat_jid=?", (jid,)).fetchone()
        groups = conn.execute("SELECT chat_jid, count(*) AS n, max(ts) AS last FROM messages WHERE sender_jid=? "
                              "AND chat_jid != ? GROUP BY chat_jid ORDER BY n DESC LIMIT 10", (jid, jid)).fetchall()
        last = conn.execute("SELECT * FROM messages WHERE chat_jid=? ORDER BY ts DESC, id DESC LIMIT ?",
                            (jid, _limit(recent, 10))).fetchall()[::-1]
        phone = conn.execute("SELECT phone FROM contacts WHERE jid=?", (jid,)).fetchone()
        people.append({
            "name": name or _chat_name(jid, names), "chat_id": jid,
            "phone": (phone["phone"] if phone and phone["phone"] else (store.jid_user(jid) if jid.endswith("@s.whatsapp.net") else None)),
            "direct_messages": dm["n"], "sent_by_me": dm["mine"] or 0, "sent_by_them": dm["n"] - (dm["mine"] or 0),
            "first_contact": store.iso(dm["first"]), "last_contact": store.iso(dm["last"]),
            "groups_they_write_in": [{"chat": _chat_name(g["chat_jid"], names), "chat_id": g["chat_jid"],
                                      "their_messages": g["n"], "last": store.iso(g["last"])} for g in groups],
            "recent_direct_messages": [_msg(r, names, max_chars=500) for r in last]})
    return {"matches": len(cands), "people": people}


def waiting_on_me(conn, days=14, include_groups=False, limit=30):
    since = int(time.time()) - int(days or 14) * 86400
    names = _names(conn)
    rows = conn.execute(
        "SELECT m.* FROM messages m JOIN (SELECT chat_jid, max(ts) AS mts FROM messages WHERE ts >= ? GROUP BY chat_jid) l "
        "ON l.chat_jid = m.chat_jid AND l.mts = m.ts WHERE m.from_me = 0 ORDER BY m.ts DESC", (since,)).fetchall()
    out, seen = [], set()
    for r in rows:
        if r["chat_jid"] in seen or (r["chat_jid"].endswith("@g.us") and not include_groups):
            continue
        seen.add(r["chat_jid"])
        my_last = conn.execute("SELECT max(ts) FROM messages WHERE chat_jid=? AND from_me=1", (r["chat_jid"],)).fetchone()[0]
        unanswered = conn.execute("SELECT * FROM messages WHERE chat_jid=? AND from_me=0 AND ts > ? ORDER BY ts LIMIT 5",
                                  (r["chat_jid"], my_last or 0)).fetchall()
        count = conn.execute("SELECT count(*) FROM messages WHERE chat_jid=? AND from_me=0 AND ts > ?",
                             (r["chat_jid"], my_last or 0)).fetchone()[0]
        out.append({"chat": _chat_name(r["chat_jid"], names), "chat_id": r["chat_jid"],
                    "waiting_since": store.iso(unanswered[0]["ts"] if unanswered else r["ts"]),
                    "unanswered_messages": count, "my_last_reply": store.iso(my_last),
                    "their_messages": [_msg(u, names, max_chars=300) for u in unanswered]})
    return {"days": days, "count": len(out), "waiting": out[:_limit(limit, 30)]}


def digest(conn, since=None, hours=24, limit_chats=30):
    start = store.parse_when(since) if since else int(time.time()) - int(hours or 24) * 3600
    names = _names(conn)
    rows = conn.execute("SELECT chat_jid, count(*) AS n, sum(from_me) AS mine, max(ts) AS last FROM messages "
                        "WHERE ts >= ? GROUP BY chat_jid ORDER BY last DESC", (start,)).fetchall()
    chats = []
    for r in rows[:_limit(limit_chats, 30)]:
        sample = conn.execute("SELECT * FROM messages WHERE chat_jid=? AND ts >= ? AND from_me=0 ORDER BY ts DESC LIMIT 4",
                              (r["chat_jid"], start)).fetchall()[::-1]
        chats.append({"chat": _chat_name(r["chat_jid"], names), "chat_id": r["chat_jid"],
                      "kind": "group" if r["chat_jid"].endswith("@g.us") else "dm",
                      "new_messages": r["n"], "from_me": r["mine"] or 0, "last": store.iso(r["last"]),
                      "latest_from_others": [_msg(s, names, max_chars=250) for s in sample]})
    total = sum(r["n"] for r in rows)
    return {"since": store.iso(start), "active_chats": len(rows), "messages": total, "chats": chats,
            "waiting_on_me": waiting_on_me(conn, days=7, limit=15)["waiting"]}


def stats(conn):
    by_source = [{"source": r["source"], "messages": r["n"], "oldest": store.iso(r["first"]), "newest": store.iso(r["last"])}
                 for r in conn.execute("SELECT source, count(*) AS n, min(ts) AS first, max(ts) AS last FROM messages GROUP BY source")]
    tot = conn.execute("SELECT count(*), min(ts), max(ts), count(DISTINCT chat_jid) FROM messages").fetchone()
    seen = store.get_meta(conn, "live_seen_at")
    last_sync = store.get_meta(conn, "live_connected_at")
    ios = store.get_meta(conn, "ios_imported_at")
    return {"messages": tot[0], "chats_with_messages": tot[3], "oldest": store.iso(tot[1]), "newest": store.iso(tot[2]),
            "by_source": by_source,
            "contacts": conn.execute("SELECT count(*) FROM contacts").fetchone()[0],
            "deleted_messages_forgotten": conn.execute("SELECT count(*) FROM tombstones").fetchone()[0],
            "live_link_connected_since": store.iso(last_sync) if last_sync else None,
            "iphone_backup_imported": store.iso(ios) if ios else None,
            "live_link_last_seen": store.iso(seen) if seen else None,
            "live_link_state": store.get_meta(conn, "live_state"),
            "excluded_chats": len(store.exclusions(conn)),
            "attachments_in_messages": conn.execute("SELECT count(*) FROM messages WHERE media_type IS NOT NULL").fetchone()[0],
            "attachments_fetched": {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, count(*) AS n FROM attachments GROUP BY status")},
            "voice_notes_transcribed": conn.execute("SELECT count(*) FROM attachments WHERE text_kind='transcript'").fetchone()[0],
            "documents_read": conn.execute("SELECT count(*) FROM attachments WHERE text_kind='document'").fetchone()[0]}
