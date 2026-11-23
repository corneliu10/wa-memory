"""Tests on made-up chats only. Run from the repository root: python3 -m unittest discover -s tests -v"""
import hashlib
import hmac
import importlib.util
import io
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from cryptography.hazmat.primitives import padding  # noqa: E402
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message  # noqa: E402
from neonize.proto.waHistorySync.WAWebProtobufsHistorySync_pb2 import HistorySync  # noqa: E402

from wa_memory import convert, fetch, ingest_ios, queries, store  # noqa: E402

NOW = int(time.time())
ANA = "40700000001@s.whatsapp.net"      # made-up people
BOGDAN = "40700000002@s.whatsapp.net"
GROUP = "120363000000000001@g.us"
NEWS = "120363000000000009@newsletter"


def text(t):
    return Message(conversation=t)


def reply(t, quoted):
    m = Message()
    m.extendedTextMessage.text = t
    m.extendedTextMessage.contextInfo.stanzaID = quoted
    return m


def reaction(target):
    m = Message()
    m.reactionMessage.key.ID = target
    m.reactionMessage.text = "+1"
    return m


def revoke(target):
    m = Message()
    m.protocolMessage.type = m.protocolMessage.Type.Value("REVOKE")
    m.protocolMessage.key.ID = target
    return m


def edit(target, new_text):
    m = Message()
    m.protocolMessage.type = m.protocolMessage.Type.Value("MESSAGE_EDIT")
    m.protocolMessage.key.ID = target
    m.protocolMessage.editedMessage.conversation = new_text
    return m


def encrypt(plain, kind):
    """What WhatsApp's media servers hold for `plain`: (media key, encrypted file, its sha256, plain sha256)."""
    key = os.urandom(32)
    iv, ck, mk = fetch.expand_key(key, kind)
    padder = padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(ck), modes.CBC(iv)).encryptor()
    body = enc.update(padder.update(plain) + padder.finalize()) + enc.finalize()
    blob = body + hmac.new(mk, iv + body, hashlib.sha256).digest()[:10]
    return key, blob, hashlib.sha256(blob).digest(), hashlib.sha256(plain).digest()


def media_msg(kind, plain, caption=None, filename=None, mime="application/octet-stream", url_id=None, remote=None, ptt=False):
    """A message with an attachment; its encrypted file goes into `remote` (url -> bytes)."""
    key, blob, enc_sha, sha = encrypt(plain, kind)
    url = "https://mmg.whatsapp.net/v/t62/%s.enc?oh=x" % (url_id or os.urandom(4).hex())
    if remote is not None:
        remote[url] = blob
    m = Message()
    part = getattr(m, {"image": "imageMessage", "video": "videoMessage", "audio": "audioMessage",
                       "document": "documentMessage", "sticker": "stickerMessage"}[kind])
    part.URL = url
    part.directPath = "/v/t62/x.enc"
    part.mediaKey = key
    part.fileEncSHA256 = enc_sha
    part.fileSHA256 = sha
    part.fileLength = len(plain)
    part.mimetype = mime
    if caption:
        part.caption = caption
    if filename:
        part.fileName = filename
    if ptt:
        part.PTT = True
    return m


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


class FakeServer:
    """Stands in for WhatsApp's media servers (fetch._open): known URLs answer their bytes, others 403.
    Tests never reach the network."""

    def __init__(self):
        self.files = {}
        self.calls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        if url not in self.files:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        return io.BytesIO(self.files[url])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wam-test-")
        os.environ["WA_MEMORY_HOME"] = os.path.join(self.tmp, "home")
        self.mem = store.open_memory()
        hs = HistorySync()
        hs.pushnames.add(ID=ANA, pushname="Ana")
        hs.pushnames.add(ID=BOGDAN, pushname="Bogdan I.")

        def conv(jid, name, msgs):
            c = hs.conversations.add(ID=jid)
            if name:
                c.name = name
            for mid, ts, msg, from_me, participant, push in msgs:
                h = c.messages.add()
                w = h.message
                w.key.remoteJID, w.key.ID, w.key.fromMe = jid, mid, from_me
                if participant:
                    w.key.participant = participant
                if push:
                    w.pushName = push
                w.messageTimestamp = ts
                w.message.CopyFrom(msg)

        conv(ANA, "Ana Popescu", [
            ("A1", NOW - 86400 * 3, text("Ședința de mâine e la 10, aduci contractul?"), False, None, "Ana"),
            ("A2", NOW - 86400 * 3 + 60, text("Da, îl aduc."), True, None, None),
            ("A3", NOW - 3600, text("Ai semnat contractul pentru apartament?"), False, None, "Ana")])
        conv(BOGDAN, None, [("B1", NOW - 7200, text("Factura e gata"), True, None, None)])
        conv(GROUP, "Vecini Bloc 4", [
            ("G1", NOW - 600, text("Apa caldă se oprește joi"), False, BOGDAN, "Bogdan I."),
            ("G2", NOW - 500, reaction("G1"), False, ANA, "Ana")])
        conv(NEWS, "Știri", [("N1", NOW - 100, text("Breaking news"), False, None, None)])
        self.counts = convert.record_history(self.mem, hs)
        convert.contact(self.mem, ANA, "Ana Popescu", saved=True)
        self.mem.commit()

    def tearDown(self):
        self.mem.close()
        shutil.rmtree(self.tmp)
        os.environ.pop("WA_MEMORY_HOME", None)

    def ids(self):
        return {r[0] for r in self.mem.execute("SELECT msg_id FROM messages")}


class History(Base):
    def test_history_chunk(self):
        self.assertEqual(self.counts, {"inserted": 5, "skipped": 1})   # the reaction; the channel is never read
        self.assertEqual(self.ids(), {"A1", "A2", "A3", "B1", "G1"})
        g1 = self.mem.execute("SELECT sender_jid, sender_name, from_me FROM messages WHERE msg_id='G1'").fetchone()
        self.assertEqual(tuple(g1), (BOGDAN, "Bogdan I.", 0))
        a2 = self.mem.execute("SELECT sender_jid, from_me FROM messages WHERE msg_id='A2'").fetchone()
        self.assertEqual(tuple(a2), (None, 1))
        names = dict(self.mem.execute("SELECT jid, name FROM contacts").fetchall())
        self.assertEqual(names[ANA], "Ana Popescu")      # the name saved on the phone wins over "Ana"
        self.assertEqual(names[BOGDAN], "Bogdan I.")
        self.assertEqual(self.mem.execute("SELECT name FROM chats WHERE jid=?", (GROUP,)).fetchone()[0], "Vecini Bloc 4")
        self.assertIsNone(self.mem.execute("SELECT 1 FROM chats WHERE jid=?", (NEWS,)).fetchone())

    def test_same_message_twice_is_one(self):
        r = convert.record(self.mem, ANA, "A3", ANA, "Ana", False, NOW - 3600, text("Ai semnat contractul pentru apartament?"))
        self.assertEqual(r, "updated")
        self.assertEqual(self.mem.execute("SELECT count(*) FROM messages WHERE msg_id='A3'").fetchone()[0], 1)


class Live(Base):
    def test_reply_edit_and_delete(self):
        r = convert.record(self.mem, ANA + "", "A4", "40700000001:7@s.whatsapp.net", "Ana", False, NOW * 1000,
                           reply("Mâine la 9 e ok?", "A3"))
        self.assertEqual(r, "inserted")
        a4 = self.mem.execute("SELECT sender_jid, ts, quoted_msg_id FROM messages WHERE msg_id='A4'").fetchone()
        self.assertEqual(tuple(a4), (ANA, NOW, "A3"))          # device part dropped, milliseconds to seconds
        self.assertEqual(convert.record(self.mem, ANA, "E1", ANA, "Ana", False, NOW, edit("A4", "Mâine la 10 e ok?")), "edited")
        self.assertEqual(queries.search(self.mem, "10")["results"][0]["msg_id"], "A4")
        self.assertEqual(convert.record(self.mem, ANA, "R1", ANA, "Ana", False, NOW, revoke("A4")), "deleted")
        self.assertNotIn("A4", self.ids())
        again = convert.record(self.mem, ANA, "A4", ANA, "Ana", False, NOW, text("Mâine la 9 e ok?"))
        self.assertEqual(again, "tombstoned", "a deleted message never comes back (e.g. from a later history chunk)")

    def test_only_the_author_deletes_or_edits_and_ids_do_not_collide(self):
        # Bogdan's group message G1: Ana cannot delete or edit it; a message from Ana reusing its ID is ignored.
        self.assertEqual(convert.record(self.mem, GROUP, "X1", ANA, "Ana", False, NOW, revoke("G1")), "skipped")
        self.assertEqual(convert.record(self.mem, GROUP, "X2", ANA, "Ana", False, NOW, edit("G1", "fals")), "skipped")
        self.assertEqual(convert.record(self.mem, GROUP, "G1", ANA, "Ana", False, NOW, text("overwrite")), "duplicate")
        self.assertEqual(queries.read_chat(self.mem, "Vecini")["messages"][0]["text"], "Apa caldă se oprește joi")
        # Nobody else can rewrite or delete my messages, in a direct chat either.
        self.assertEqual(convert.record(self.mem, ANA, "X3", ANA, "Ana", False, NOW, edit("A2", "Nu.")), "skipped")
        self.assertEqual(convert.record(self.mem, ANA, "X4", ANA, "Ana", False, NOW, revoke("A2")), "skipped")
        # A deletion applies to its own chat only: the same ID in another chat stays.
        convert.record(self.mem, BOGDAN, "B2", BOGDAN, "Bogdan I.", False, NOW, text("alt chat"))
        self.assertEqual(convert.record(self.mem, ANA, "X5", ANA, "Ana", False, NOW, revoke("B2")), "deleted")
        self.assertIn("B2", self.ids())
        # Bogdan deletes his own message, also when he writes from his other identity (a LID).
        lid = "11111111111111@lid"
        self.assertEqual(convert.record(self.mem, GROUP, "X6", lid, "Bogdan I.", False, NOW, revoke("G1"), sender_alt=BOGDAN),
                         "deleted")
        self.assertNotIn("G1", self.ids())
        self.assertEqual(convert.record(self.mem, GROUP, "G1", BOGDAN, "Bogdan I.", False, NOW, text("again")), "tombstoned")

    def test_not_conversations_and_excluded_chats(self):
        for chat in ("status@broadcast", NEWS, "123@broadcast"):
            self.assertEqual(convert.record(self.mem, chat, "X%d" % len(chat), ANA, "Ana", False, NOW, text("x")), "skipped")
        self.assertEqual(convert.record(self.mem, ANA, "X9", ANA, "Ana", False, NOW, reaction("A3")), "skipped")
        with open(os.path.join(store.home(), "exclude.txt"), "w", encoding="utf-8") as fh:
            fh.write("Ana Popescu  # private\n40700000002:3@c.us\n")
        ex, removed = store.apply_exclusions(self.mem)
        self.assertEqual((ex, removed), ({ANA, BOGDAN}, 4))
        self.assertEqual(convert.record(self.mem, ANA, "A5", ANA, "Ana", False, NOW, text("x"), excluded=ex), "skipped")
        self.assertEqual(self.ids(), {"G1"})

    def test_wrappers_and_other_kinds(self):
        m = Message()
        m.ephemeralMessage.message.viewOnceMessage.message.imageMessage.caption = "Poza cu cheia"
        d = convert.describe(m)
        self.assertEqual((d["media_type"], d["text"]), ("image", "Poza cu cheia"))
        m = Message()
        m.documentWithCaptionMessage.message.documentMessage.fileName = "Plan.pdf"
        m.documentWithCaptionMessage.message.documentMessage.caption = "Planul"
        d = convert.describe(m)
        self.assertEqual((d["media_type"], d["filename"], d["text"], d["ref"]["kind"]), ("document", "Plan.pdf", "Planul", "document"))
        m = Message()
        m.locationMessage.degreesLatitude, m.locationMessage.degreesLongitude = 44.43, 26.1
        m.locationMessage.name = "Notariat"
        self.assertEqual(convert.describe(m)["text"], "Location: Notariat (44.43000, 26.10000)")
        m = Message()
        m.pollCreationMessageV3.name = "Când?"
        m.pollCreationMessageV3.options.add(optionName="Luni")
        m.pollCreationMessageV3.options.add(optionName="Marți")
        self.assertEqual(convert.describe(m)["text"], "Poll: Când? (Luni / Marți)")
        self.assertIsNone(convert.describe(Message()))
        self.assertEqual(convert.norm_jid("40700000001.0:12@c.us"), ANA)

    def test_media_reference_is_kept_for_later(self):
        remote = {}
        m = media_msg("document", b"%PDF-1.4 x", filename="Contract.pdf", mime="application/pdf", remote=remote)
        self.assertEqual(convert.record(self.mem, ANA, "D9", ANA, "Ana", False, NOW, m), "inserted")
        ref = self.mem.execute("SELECT kind, filename, length, url FROM media_refs WHERE msg_id='D9'").fetchone()
        self.assertEqual(tuple(ref), ("document", "Contract.pdf", 10, list(remote)[0]))


class OddPaths(unittest.TestCase):
    def test_paths_with_hash_and_question_marks(self):
        tmp = tempfile.mkdtemp(prefix="wam #?x-")
        try:
            os.environ["WA_MEMORY_HOME"] = os.path.join(tmp, "home #1?")
            from wa_memory import live
            store.private_home()
            con = sqlite3.connect(live.session_path())
            con.execute("CREATE TABLE whatsmeow_device(jid TEXT)")
            con.execute("INSERT INTO whatsmeow_device VALUES('x')")
            con.commit()
            con.close()
            self.assertTrue(live.linked())
            cs = os.path.join(tmp, "Chat #Storage?.sqlite")
            make_chatstorage(cs)
            mem = store.open_memory()
            self.assertEqual(ingest_ios.import_chatstorage(mem, cs)["inserted"], 5)
            mem.close()
        finally:
            os.environ.pop("WA_MEMORY_HOME", None)
            shutil.rmtree(tmp)


class Queries(Base):
    def test_search_ignores_diacritics_and_matches_prefixes(self):
        r = queries.search(self.mem, "sedinta")
        self.assertEqual([x["msg_id"] for x in r["results"]], ["A1"])
        r = queries.search(self.mem, "contract", sort="recent")
        self.assertEqual([x["msg_id"] for x in r["results"]], ["A3", "A1"])
        self.assertEqual(r["results"][0]["chat"], "Ana Popescu")
        r = queries.search(self.mem, "factura", sender="me")
        self.assertEqual([x["msg_id"] for x in r["results"]], ["B1"])
        r = queries.search(self.mem, "contract", sender="Ana")
        self.assertEqual(len(r["results"]), 2)
        r = queries.search(self.mem, '"apa calda" OR factura')
        self.assertEqual({x["msg_id"] for x in r["results"]}, {"G1", "B1"})
        self.assertIn("error", queries.search(self.mem, "  ...  "))

    def test_read_chat_person_waiting_digest_stats(self):
        r = queries.read_chat(self.mem, "ana")
        self.assertEqual([m["msg_id"] for m in r["messages"]], ["A1", "A2", "A3"])
        self.assertEqual(r["messages"][1]["from"], "me")
        r = queries.read_chat(self.mem, "Ana Popescu", around_msg_id="A2", limit=2)
        self.assertEqual([m["msg_id"] for m in r["messages"]], ["A1", "A2"])
        p = queries.person(self.mem, "0700000001")
        self.assertEqual(p["people"][0]["direct_messages"], 3)
        w = queries.waiting_on_me(self.mem, days=7)
        self.assertEqual([x["chat"] for x in w["waiting"]], ["Ana Popescu"])
        self.assertEqual(w["waiting"][0]["unanswered_messages"], 1)
        d = queries.digest(self.mem, hours=24)
        self.assertEqual(d["messages"], 3)  # A3, B1, G1
        s = queries.stats(self.mem)
        self.assertEqual(s["messages"], 5)
        lc = queries.list_chats(self.mem, kind="group")
        self.assertEqual(lc["chats"][0]["chat"], "Vecini Bloc 4")


def make_chatstorage(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE ZWACHATSESSION(Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZSESSIONTYPE INTEGER);
    CREATE TABLE ZWAGROUPMEMBER(Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT, ZCONTACTNAME TEXT);
    CREATE TABLE ZWAPROFILEPUSHNAME(Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
    CREATE TABLE ZWAMESSAGE(Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGEDATE REAL,
      ZTEXT TEXT, ZMESSAGETYPE INTEGER, ZGROUPMEMBER INTEGER, ZSTANZAID TEXT, ZSENTDATE REAL);
    CREATE TABLE ZWAMEDIAITEM(Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZTITLE TEXT, ZMEDIALOCALPATH TEXT);
    """)
    a = 978307200
    c.executemany("INSERT INTO ZWACHATSESSION VALUES(?,?,?,?)", [(1, ANA, "Ana Popescu", 0), (2, GROUP, "Vecini Bloc 4", 1),
                                                                 (3, "status@broadcast", None, 3)])
    c.execute("INSERT INTO ZWAGROUPMEMBER VALUES(1, ?, 'Bogdan Ionescu')", (BOGDAN,))
    old = NOW - 86400 * 365 * 4 - a  # four years ago, Apple epoch
    c.executemany("INSERT INTO ZWAMESSAGE VALUES(?,?,?,?,?,?,?,?,?)", [
        (1, 1, 0, old, "Bună! Ne vedem la notar luni?", 0, None, "OLD1", None),
        (2, 1, 1, old + 60, "Da, la 9.", 0, None, "OLD2", None),
        (3, 1, 0, NOW - 86400 * 3 - a, "Ședința de mâine e la 10, aduci contractul?", 0, None, "A1", None),  # also live
        (4, 2, 0, old + 120, None, 1, 1, "OLD3", None),             # image with caption
        (5, 1, 0, old + 180, None, 14, None, "OLD4", None),         # deleted placeholder
        (6, 1, 1, NOW - 86400 * 3 + 60 - a, "Da, îl aduc.", 0, None, "A2", None),  # deleted live -> tombstone
        (7, 3, 0, old, "status text", 0, None, "ST1", None),
    ])
    c.execute("INSERT INTO ZWAMEDIAITEM VALUES(1, 4, 'Planul apartamentului', 'Media/120363/a/b/plan.jpg')")
    c.commit()
    c.close()


class IosImport(Base):
    def test_merge_with_live_history(self):
        convert.record(self.mem, ANA, "R2", None, None, True, NOW, revoke("A2"))   # deleted on the phone
        cs = os.path.join(self.tmp, "ChatStorage.sqlite")
        make_chatstorage(cs)
        c = ingest_ios.import_chatstorage(self.mem, cs)
        self.assertEqual(c["inserted"], 3)      # OLD1, OLD2, OLD3
        self.assertEqual(c["duplicate"], 1)     # A1 already from the live link
        self.assertEqual(c["tombstoned"], 1)    # A2 was deleted on the phone
        self.assertEqual(c["skipped"], 2)       # deleted placeholder, status
        r = queries.read_chat(self.mem, ANA)
        self.assertEqual([m["msg_id"] for m in r["messages"]], ["OLD1", "OLD2", "A1", "A3"])
        img = queries.search(self.mem, "planul")["results"][0]
        self.assertEqual((img["from"], img["media"]), ("Bogdan Ionescu", "image: plan.jpg"))
        again = ingest_ios.import_chatstorage(self.mem, cs)
        self.assertEqual(again["inserted"], 0)

    def test_finder_backup_folder(self):
        bk = os.path.join(self.tmp, "backup")
        fid = ingest_ios.FILE_IDS["whatsapp"]
        os.makedirs(os.path.join(bk, fid[:2]))
        make_chatstorage(os.path.join(bk, fid[:2], fid))
        with open(os.path.join(bk, "Manifest.plist"), "wb") as fh:
            plistlib.dump({"IsEncrypted": False}, fh)
        self.assertTrue(ingest_ios.find_chatstorage(bk).endswith(fid))
        with open(os.path.join(bk, "Manifest.plist"), "wb") as fh:
            plistlib.dump({"IsEncrypted": True}, fh)
        with self.assertRaises(ingest_ios.EncryptedBackup):
            ingest_ios.find_chatstorage(bk)


class McpProtocol(Base):
    def test_stdio_session(self):
        reqs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "whatsapp_search", "arguments": {"query": "sedinta"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "whatsapp_read_chat", "arguments": {"chat": "nobody here"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
            [], "just a string", {"jsonrpc": "2.0", "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 6, "method": "initialize", "params": [1, 2]},
        ]
        p = subprocess.run([sys.executable, "-m", "wa_memory.mcp_server"],
                           input="\n".join(json.dumps(r) for r in reqs) + "\n{not json\n",
                           capture_output=True, text=True, timeout=60, env=os.environ.copy(), cwd=ROOT)
        out = [json.loads(line) for line in p.stdout.splitlines()]
        self.assertEqual([o["id"] for o in out], [1, 2, 3, 4, 5, None, None, 6, None])
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")
        tools = out[1]["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertEqual(len(names), 9)
        self.assertFalse([n for n in names if any(w in n for w in ("send", "delete", "edit", "write"))])
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))
        hit = json.loads(out[2]["result"]["content"][0]["text"])
        self.assertEqual(hit["results"][0]["msg_id"], "A1")
        self.assertTrue(out[3]["result"]["isError"])
        self.assertEqual(out[4]["error"]["code"], -32601)
        self.assertEqual([o["error"]["code"] for o in out[5:]], [-32600, -32600, -32602, -32700])


FAKE_FFMPEG = r'''#!/usr/bin/env python3
import shutil, sys
a = sys.argv[1:]
src = a[a.index("-i") + 1]
if "-frames:v" in a:
    open(a[-1], "wb").write(open(src + ".frame.jpg", "rb").read())
else:
    shutil.copy(src, a[-1])
'''
FAKE_WHISPER = r'''#!/usr/bin/env python3
import sys
a = sys.argv[1:]
open(a[a.index("-of") + 1] + ".txt", "w").write("Salut, ne vedem mâine la notar la ora nouă.\n")
'''


def make_pdf(path, text):
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"]
    stream = b"BT /F1 14 Tf 20 100 Td (" + text.encode("latin-1") + b") Tj ET"
    objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    with open(path, "wb") as fh:
        fh.write(out)


def make_png(path):
    import struct
    import zlib
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 8 for _ in range(8))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Fetch(unittest.TestCase):
    def test_decrypt_round_trip_and_tampering(self):
        for kind in ("image", "video", "audio", "document", "sticker"):
            key, blob, enc_sha, sha = encrypt(b"made-up file " * 50, kind)
            self.assertEqual(fetch.decrypt(blob, key, kind, enc_sha, sha), b"made-up file " * 50)
        key, blob, enc_sha, sha = encrypt(b"x" * 100, "document")
        bad = blob[:-11] + bytes([blob[-11] ^ 1]) + blob[-10:]
        with self.assertRaises(fetch.FetchError):
            fetch.decrypt(bad, key, "document")
        with self.assertRaises(fetch.FetchError):
            fetch.decrypt(blob, os.urandom(32), "document")
        with self.assertRaises(fetch.FetchError):
            fetch.decrypt(blob, key, "image")                 # another kind's keys
        with self.assertRaises(fetch.FetchError):
            fetch.decrypt(blob, key, "document", enc_sha256=b"0" * 32)

    def test_only_whatsapp_media_hosts(self):
        self.assertEqual(fetch.media_url("https://mmg.whatsapp.net/v/a.enc?x=1", None), "https://mmg.whatsapp.net/v/a.enc?x=1")
        self.assertEqual(fetch.media_url("https://evil.example/a", "/v/t62/a.enc"), "https://mmg.whatsapp.net/v/t62/a.enc")
        for url, path in (("http://mmg.whatsapp.net/a", None), ("https://whatsapp.net.evil.example/a", None),
                          ("https://u:p@mmg.whatsapp.net/a", None), (None, "//evil.example/a"), (None, "v/a"), (None, None)):
            with self.assertRaises(fetch.FetchError):
                fetch.media_url(url, path)

    def test_odd_addresses_fail_cleanly_and_redirects_stay_on_whatsapp(self):
        for url in ("https://mmg.whatsapp.net:abc/x", "https://mmg.whatsapp.net/a b", "https://mmg.whatsapp.net/ă"):
            with self.assertRaises(fetch.FetchError):
                fetch.media_url(url, None)
        handler = fetch._SameHostsRedirect()
        req = urllib.request.Request("https://mmg.whatsapp.net/a")
        for target in ("http://127.0.0.1/x", "https://evil.example/x", "ftp://mmg.whatsapp.net/x"):
            with self.assertRaises(fetch.FetchError):
                handler.redirect_request(req, None, 302, "Found", {}, target)
        self.assertIsNotNone(handler.redirect_request(req, None, 302, "Found", {}, "https://mmg2.whatsapp.net/b"))

    def test_size_limits(self):
        with self.assertRaises(fetch.FetchError) as cm:
            fetch.download({"kind": "document", "length": 10 ** 9, "url": "https://mmg.whatsapp.net/a"})
        self.assertEqual(cm.exception.status, "too_big")
        key, blob, enc_sha, sha = encrypt(b"y" * 5000, "document")
        ref = {"kind": "document", "url": "https://mmg.whatsapp.net/a", "media_key": key, "enc_sha256": enc_sha, "sha256": sha}
        with self.assertRaises(fetch.FetchError) as cm:
            fetch.download(ref, max_bytes=1000, opener=lambda req, timeout=None: io.BytesIO(blob))
        self.assertEqual(cm.exception.status, "too_big")
        self.assertEqual(fetch.download(ref, opener=lambda req, timeout=None: io.BytesIO(blob)), b"y" * 5000)


class Media(Base):
    def setUp(self):
        super().setUp()
        bins = os.path.join(self.tmp, "bin")
        os.makedirs(bins)
        for name, body in (("ffmpeg", FAKE_FFMPEG), ("whisper", FAKE_WHISPER)):
            p = os.path.join(bins, name)
            with open(p, "w") as fh:
                fh.write(body)
            os.chmod(p, 0o755)
        self.model = os.path.join(self.tmp, "model.bin")
        open(self.model, "w").close()
        os.environ.update(FFMPEG_BIN=os.path.join(bins, "ffmpeg"), WHISPER_BIN=os.path.join(bins, "whisper"),
                          WHISPER_MODEL=self.model)
        self.server = FakeServer()
        pdf = os.path.join(self.tmp, "c.pdf")
        make_pdf(pdf, "Contract de inchiriere apartament Floreasca")
        png = os.path.join(self.tmp, "p.png")
        make_png(png)
        f = self.server.files
        msgs = [
            (ANA, "D1", NOW - 5000, ANA, media_msg("document", read(pdf), filename="Contract.pdf", mime="application/pdf", remote=f)),
            (ANA, "V1", NOW - 4000, ANA, media_msg("audio", b"OggS fake voice", mime="audio/ogg; codecs=opus", remote=f, ptt=True)),
            (GROUP, "P1", NOW - 3000, BOGDAN, media_msg("image", read(png), caption="Uite planul", mime="image/png", remote=f)),
            (ANA, "Z1", NOW - 2000, ANA, media_msg("document", b"PK zip", filename="export.zip", remote=f)),
            (ANA, "OLDPIC", NOW - 86400 * 300, ANA, media_msg("image", b"gone", mime="image/jpeg")),   # not on the server any more
        ]
        for chat, mid, ts, sender, msg in msgs:
            self.assertEqual(convert.record(self.mem, chat, mid, sender, "Bogdan I." if sender == BOGDAN else "Ana", False, ts, msg), "inserted")
        self.mem.commit()
        self.patch = mock.patch("wa_memory.fetch._open", self.server)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        for k in ("FFMPEG_BIN", "WHISPER_BIN", "WHISPER_MODEL"):
            os.environ.pop(k, None)
        super().tearDown()

    def test_auto_fetch_reads_documents_and_transcribes_voice_notes(self):
        from wa_memory import media
        c = media.auto_fetch(self.mem, budget_s=60)
        self.assertEqual((c["downloaded"], c["transcripts"], c["skipped"]), (2, 1, 1))
        has_pdf = importlib.util.find_spec("pypdf") is not None   # the optional `pdf` extra
        self.assertEqual(c["texts"], 1 if has_pdf else 0)
        st = {r["msg_id"]: r["status"] for r in self.mem.execute("SELECT msg_id, status FROM attachments")}
        self.assertEqual(st, {"D1": "ok", "V1": "ok", "Z1": "skipped"})  # pictures wait until asked for
        if has_pdf:
            r = queries.search(self.mem, "floreasca")
            self.assertEqual(r["results"][0]["msg_id"], "D1")
            self.assertEqual(r["results"][0]["found_in"], "attached document")
        r = queries.search(self.mem, "notar")
        self.assertEqual(r["results"][0]["found_in"], "voice note transcript")
        chat = queries.read_chat(self.mem, "Ana Popescu")
        voice = [m for m in chat["messages"] if m["msg_id"] == "V1"][0]
        self.assertIn("notar", voice["transcript"])
        self.assertEqual(media.auto_fetch(self.mem, budget_s=60)["downloaded"], 0)   # nothing twice
        path = self.mem.execute("SELECT path FROM attachments WHERE msg_id='D1'").fetchone()[0]
        self.assertEqual(os.path.basename(path), "Contract.pdf")
        self.assertEqual(oct(os.stat(store.home()).st_mode & 0o777), "0o700")

    def test_voice_notes_wait_for_the_speech_model(self):
        from wa_memory import media
        os.remove(self.model)
        media.auto_fetch(self.mem, budget_s=60)
        v1 = self.mem.execute("SELECT status, text, error FROM attachments WHERE msg_id='V1'").fetchone()
        self.assertEqual(tuple(v1), ("ok", None, None))
        open(self.model, "w").close()
        self.assertEqual(media.auto_fetch(self.mem, budget_s=60)["transcripts"], 1)

    @unittest.skipUnless(sys.platform == "darwin", "picture resizing uses macOS's sips")
    def test_on_demand_picture_expired_and_deletion(self):
        from wa_memory import mcp_server
        res = mcp_server.get_attachment(self.mem, "P1")
        kinds = [c["type"] for c in res["content"]]
        self.assertEqual(kinds, ["text", "image"])
        self.assertTrue(res["content"][1]["data"])
        info = json.loads(res["content"][0]["text"])
        self.assertEqual((info["from"], info["status"]), ("Bogdan I.", "ok"))
        old = json.loads(mcp_server.get_attachment(self.mem, "OLDPIC")["content"][0]["text"])
        self.assertEqual(old["status"], "expired")
        lst = queries.list_attachments(self.mem, chat="Vecini", media_type="image")
        self.assertEqual([(a["msg_id"], a["downloaded"]) for a in lst["attachments"]], [("P1", "yes")])
        path = self.mem.execute("SELECT path FROM attachments WHERE msg_id='P1'").fetchone()[0]
        self.assertTrue(os.path.exists(path))
        convert.record(self.mem, GROUP, "R3", BOGDAN, "Bogdan I.", False, NOW, revoke("P1"))
        self.assertFalse(os.path.exists(path), "a deleted message's file is removed too")
        self.assertIsNone(self.mem.execute("SELECT 1 FROM media_refs WHERE msg_id='P1'").fetchone())

    def test_one_bad_attachment_does_not_block_the_others(self):
        from wa_memory import media
        self.mem.execute("UPDATE media_refs SET url='https://mmg.whatsapp.net:abc/x', direct_path=NULL WHERE msg_id='V1'")
        self.mem.commit()
        c = media.auto_fetch(self.mem, budget_s=60)
        st = {r["msg_id"]: r["status"] for r in self.mem.execute("SELECT msg_id, status FROM attachments")}
        self.assertEqual(st["V1"], "failed")
        self.assertEqual(st["D1"], "ok")
        self.assertEqual(c["downloaded"], 1)

    def test_long_names_and_slow_patterns(self):
        from wa_memory import media
        name = media._filename({"filename": "ș" * 300 + ".pdf", "kind": "document"})
        self.assertLessEqual(len(name.encode("utf-8")), 200)
        self.assertEqual(media._filename({"filename": "../../etc/passwd", "kind": "document"}), "passwd")
        self.assertEqual(media._filename({"filename": "", "kind": "image", "mime": "image/jpeg"}), "image.jpg")
        p = os.path.join(self.tmp, "slow.xlsx")
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("xl/sharedStrings.xml", "<t " + "a" * 300000)
        t0 = time.time()
        media.extract_text(p)
        self.assertLess(time.time() - t0, 2)

    def test_zip_bomb_is_not_read(self):
        from wa_memory import media
        p = os.path.join(self.tmp, "bomb.xlsx")
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("xl/sharedStrings.xml", b"\0" * (media.ZIP_MAX_UNPACKED + 1))
        self.assertIsNone(media.extract_text(p))


if __name__ == "__main__":
    unittest.main()
