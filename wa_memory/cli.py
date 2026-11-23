"""Command line for wa-memory.

  wa-memory link [--phone +NUMBER]      link this computer as a WhatsApp device (QR code, or a pairing code)
  wa-memory run                         stay connected and keep the memory up to date (what the background job runs)
  wa-memory service [ACTION]            install | uninstall | status of the background jobs (macOS)
  wa-memory import-ios PATH [--business] import all history from an iPhone backup or ChatStorage.sqlite
  wa-memory fetch-media [--budget S]    download and read new documents and voice notes
  wa-memory stats                       what the memory holds
  wa-memory search WORDS...             quick search (prints JSON)
  wa-memory waiting [DAYS]              chats waiting on a reply
"""
import argparse
import json
import sys

from . import __version__, ingest_ios, media, queries, service, store


def main(argv=None):
    p = argparse.ArgumentParser(prog="wa-memory", description="Your WhatsApp history in a local, searchable memory.")
    p.add_argument("--version", action="version", version="wa-memory %s" % __version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    lk = sub.add_parser("link", help="link this computer as a WhatsApp device")
    lk.add_argument("--phone", help="link with a pairing code for this number (international format, e.g. +40700000001)")
    lk.add_argument("--no-service", action="store_true", help="do not start the background jobs afterwards")
    sub.add_parser("run", help="stay connected and keep the memory up to date")
    v = sub.add_parser("service", help="background jobs (macOS)")
    v.add_argument("action", nargs="?", default="status", choices=["install", "uninstall", "status"])
    f = sub.add_parser("fetch-media", help="download and read new documents and voice notes")
    f.add_argument("--budget", type=float, default=150)
    i = sub.add_parser("import-ios", help="import an iPhone backup")
    i.add_argument("path")
    i.add_argument("--business", action="store_true", help="WhatsApp Business instead of WhatsApp")
    sub.add_parser("stats", help="what the memory holds")
    q = sub.add_parser("search", help="search messages")
    q.add_argument("words", nargs="+")
    q.add_argument("--limit", type=int, default=10)
    w = sub.add_parser("waiting", help="chats waiting on your reply")
    w.add_argument("days", nargs="?", type=int, default=14)
    a = p.parse_args(argv)
    store.private_home()

    if a.cmd == "link":
        from . import live
        code = live.link(phone=a.phone)
        if code == 0 and not a.no_service:
            if sys.platform == "darwin":
                print(service.run("install"))
                print("wa-memory now runs in the background. Check it with `wa-memory service status`.")
            else:
                print("Now keep `wa-memory run` running (for example as a systemd user service).")
        return code
    if a.cmd == "run":
        from . import live
        return live.run()
    if a.cmd == "service":
        print(service.run(a.action))
        return 0

    conn = store.open_memory()
    store.apply_exclusions(conn)
    conn.commit()
    if a.cmd == "fetch-media":
        out = media.auto_fetch(conn, budget_s=a.budget)
    elif a.cmd == "import-ios":
        out = ingest_ios.import_chatstorage(conn, a.path, "business" if a.business else "whatsapp")
    elif a.cmd == "stats":
        out = queries.stats(conn)
    elif a.cmd == "search":
        out = queries.search(conn, " ".join(a.words), limit=a.limit)
    else:
        out = queries.waiting_on_me(conn, a.days)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
