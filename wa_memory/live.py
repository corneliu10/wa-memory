"""wa-memory's own WhatsApp linked device (whatsmeow, through the neonize Python bindings).

`link()` pairs this computer as a linked device (QR code in the terminal, or a pairing code typed on the
phone) and asks the phone for as much history as it will send. `run()` keeps the device connected and
writes every message, edit and deletion into the memory as it arrives (convert.py).

What it does not do: it has no code path that sends messages or marks chats as read, so read receipts
(blue ticks) are never sent; after connecting it sets its presence to unavailable, so the phone keeps its
notifications. Like every linked device it acknowledges delivery (the second grey tick), which is part of
the protocol.

The session (the device's keys) is in ~/.wa-memory/session.db, owner-only. Anyone with a copy can act as
this linked device until it is removed on the phone (WhatsApp > Settings > Linked devices).
"""
import os
import pathlib
import sqlite3
import sys
import threading
import time

from . import convert, store

DEVICE_NAME = "wa-memory"
HEARTBEAT_S = 60
CONTACTS_EVERY_S = 30 * 60
EXCLUSIONS_EVERY_S = 60


def session_path():
    return os.path.join(store.home(), "session.db")


def linked():
    """True when a paired device is stored in the session database."""
    path = session_path()
    if not os.path.exists(path):
        return False
    try:
        con = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            return con.execute("SELECT count(*) FROM whatsmeow_device").fetchone()[0] > 0
        finally:
            con.close()
    except sqlite3.Error:
        return False


def device_props():
    """How the device shows on the phone, and how much history it asks for (up to 10 years, 2 GB)."""
    from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps
    props = DeviceProps(os=os.environ.get("WA_MEMORY_DEVICE_NAME", DEVICE_NAME),
                        platformType=DeviceProps.PlatformType.DESKTOP, requireFullSync=True)
    props.historySyncConfig.fullSyncDaysLimit = 3650
    props.historySyncConfig.recentSyncDaysLimit = 3650
    props.historySyncConfig.fullSyncSizeMbLimit = 2048
    props.historySyncConfig.storageQuotaMb = 10240
    return props


def _client():
    store.private_home()
    from neonize.client import NewClient
    client = NewClient(session_path(), props=device_props())
    for p in (session_path(), session_path() + "-wal", session_path() + "-shm"):
        if os.path.exists(p):
            os.chmod(p, 0o600)
    return client


def _say(msg):
    sys.stdout.write("%s %s\n" % (time.strftime("%F %T"), msg))
    sys.stdout.flush()



# ---- Writing what arrives -------------------------------------------------------------------------------


class _Writer:
    """One memory connection shared by the link's callback threads, behind a lock."""

    def __init__(self):
        self.mem = store.open_memory(threads=True)
        self.lock = threading.Lock()
        self.excluded = frozenset()
        self.excluded_at = 0.0
        self.last_event = time.time()
        self.refresh_exclusions()

    def refresh_exclusions(self):
        """Re-read exclude.txt, and remove what is already stored for newly listed chats (caller may hold no lock)."""
        with self.lock:
            ex, _ = store.apply_exclusions(self.mem)
            self.mem.commit()
            self.excluded = frozenset(ex)
            self.excluded_at = time.time()

    def meta(self, **kv):
        with self.lock:
            for k, v in kv.items():
                store.set_meta(self.mem, k, v)
            self.mem.commit()


def _jid(j):
    from neonize.utils.jid import Jid2String
    return Jid2String(j) if j is not None and j.User else None


def _attach_writers(client, w):
    """Store every message, edit, deletion and history chunk the client receives."""
    from neonize.events import HistorySyncEv, MessageEv

    @client.event(MessageEv)
    def on_message(_, ev):
        info, src = ev.Info, ev.Info.MessageSource
        w.last_event = time.time()
        with w.lock:
            try:
                convert.record(w.mem, _jid(src.Chat), info.ID, _jid(src.Sender), info.Pushname or None,
                               src.IsFromMe, info.Timestamp, ev.Message, w.excluded, sender_alt=_jid(src.SenderAlt))
                w.mem.commit()
            except Exception as exc:  # noqa: BLE001 (one bad message must not stop the link)
                w.mem.rollback()
                _say("could not store a message: %s" % type(exc).__name__)

    @client.event(HistorySyncEv)
    def on_history(_, ev):
        w.last_event = time.time()
        with w.lock:
            try:
                counts = convert.record_history(w.mem, ev.Data, w.excluded)
                store.set_meta(w.mem, "history_received_at", int(time.time()))
                w.mem.commit()
                _say("history: %s" % ", ".join("%s %d" % kv for kv in sorted(counts.items())))
            except Exception as exc:  # noqa: BLE001
                w.mem.rollback()
                _say("could not store a history chunk: %s" % type(exc).__name__)


def _go_offline(client):
    """Presence "unavailable", so the phone keeps its notifications. False until WhatsApp accepts it (it refuses
    before the account's name has synced, typically for a short while after linking)."""
    try:
        from neonize.utils.enum import Presence
        client.send_presence(Presence.UNAVAILABLE)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---- Linking --------------------------------------------------------------------------------------------


def link(phone=None, timeout_s=600, settle_s=30):
    """Pair this computer as a linked device, storing the first history it receives. Returns 0 when linked."""
    if linked():
        print("Already linked. To link again, unlink this device on your phone, then delete %s." % session_path())
        return 0
    import segno
    from neonize.events import ConnectedEv, PairStatusEv
    from neonize.proto.Neonize_pb2 import PairStatus

    w = _Writer()
    client = _client()
    _attach_writers(client, w)
    state = {"paired": False, "error": None, "asked_code": False}
    done = threading.Event()

    def show_qr(_, data):
        if phone:
            if not state["asked_code"]:
                state["asked_code"] = True
                threading.Thread(target=ask_code, daemon=True).start()
            return
        print("\nOn your phone: WhatsApp > Settings > Linked devices > Link a device, and scan this code:\n")
        segno.make_qr(data).terminal(compact=True)
        print("(It changes every 20 seconds or so; a new one appears here.)")

    def ask_code():
        try:
            code = client.PairPhone(phone, True)
        except Exception as exc:  # noqa: BLE001 (shown to the user)
            state["error"] = "Could not get a pairing code: %s" % exc
            done.set()
            return
        print("\nOn your phone: WhatsApp > Settings > Linked devices > Link a device > Link with phone number "
              "instead, and type this code:\n\n    %s\n" % code)

    client.event.qr(show_qr)

    @client.event(PairStatusEv)
    def on_pair(_, ev):
        if ev.Status == PairStatus.SUCCESS:
            state["paired"] = True
            print("Linked. Your phone now sends its history; wa-memory keeps it in %s." % store.home())
        else:
            state["error"] = "Linking failed: %s" % (ev.Error or "unknown error")
            done.set()

    @client.event(ConnectedEv)
    def on_connected(c, ev):
        if state["paired"]:
            _go_offline(c)
            done.set()

    worker = threading.Thread(target=client.connect, daemon=True)
    worker.start()
    done.wait(timeout_s)
    if state["paired"] and not state["error"]:
        # Keep storing what the phone sends until it has been quiet for a while; the background job takes over.
        w.last_event = time.time()
        while time.time() - w.last_event < settle_s:
            time.sleep(1)
    try:
        client.stop()  # ends connect(); the session stays stored
    except Exception:  # noqa: BLE001
        pass
    worker.join(30)
    if state["error"]:
        print(state["error"])
        return 1
    if not state["paired"] or not linked():
        print("Not linked (no scan within %d minutes). Run `wa-memory link` again." % (timeout_s // 60))
        return 1
    return 0


# ---- Staying connected ----------------------------------------------------------------------------------


def run():
    """Stay connected and write everything into the memory. Returns the exit code (0: stopped on purpose)."""
    if not linked():
        print("Not linked yet: run `wa-memory link` first.")
        return 0
    w = _Writer()
    banned_until = int(store.get_meta(w.mem, "banned_until") or 0)
    if banned_until > time.time():
        print("WhatsApp restricted this number until %s; wa-memory waits until then." % store.iso(banned_until))
        return 0
    from neonize.events import ConnectedEv, DisconnectedEv, LoggedOutEv, StreamReplacedEv, TemporaryBanEv

    client = _client()
    _attach_writers(client, w)
    stop = {"code": None}
    presence = {"offline": False}

    def finish(code, why):
        _say(why)
        w.meta(live_state=why)
        stop["code"] = code
        threading.Thread(target=client.stop, daemon=True).start()  # not from inside the callback

    @client.event(ConnectedEv)
    def on_connected(c, ev):
        _say("connected")
        presence["offline"] = _go_offline(c)
        w.meta(live_state="connected", live_connected_at=int(time.time()))

    @client.event(LoggedOutEv)
    def on_logged_out(_, ev):
        finish(0, "logged out: this device was removed on the phone; run `wa-memory link` to link again")

    @client.event(TemporaryBanEv)
    def on_ban(_, ev):
        seconds = int(getattr(ev, "Expire", 0) or 0)
        if seconds > 10 ** 9:  # a duration in nanoseconds
            seconds //= 10 ** 9
        seconds = min(seconds, 30 * 86400)
        if seconds > 0:
            w.meta(banned_until=int(time.time()) + seconds)
        finish(0, "WhatsApp temporarily restricted this number (code %s); wa-memory stopped and will not reconnect "
                  "before the restriction ends" % ev.Code)

    @client.event(StreamReplacedEv)
    def on_replaced(_, ev):
        finish(0, "another program connected with this device's session; wa-memory stopped")

    @client.event(DisconnectedEv)
    def on_disconnected(_, ev):
        presence["offline"] = False
        _say("disconnected (the link reconnects by itself)")

    def housekeeping():
        last_contacts = 0.0
        while stop["code"] is None:
            w.meta(live_seen_at=int(time.time()))
            if time.time() - w.excluded_at > EXCLUSIONS_EVERY_S:
                w.refresh_exclusions()
            if client.is_connected and not presence["offline"]:
                presence["offline"] = _go_offline(client)
            if time.time() - last_contacts > CONTACTS_EVERY_S and client.is_connected:
                last_contacts = time.time()
                try:
                    people = client.contact.get_all_contacts()
                    with w.lock:
                        for c in people:
                            i = c.Info
                            saved = i.FullName or i.FirstName or i.BusinessName or None
                            convert.contact(w.mem, _jid(c.JID), saved or i.PushName or None, saved=bool(saved))
                        w.mem.commit()
                except Exception as exc:  # noqa: BLE001
                    _say("could not read contacts: %s" % type(exc).__name__)
            time.sleep(HEARTBEAT_S)

    threading.Thread(target=housekeeping, daemon=True).start()
    client.connect()
    return stop["code"] if stop["code"] is not None else 1
