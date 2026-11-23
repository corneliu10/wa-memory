"""Background jobs on macOS (launchd, started at login).

  dev.wa-memory.link   `wa-memory run`: stays connected and writes new messages into the memory as they arrive
  dev.wa-memory.media  `wa-memory fetch-media` every 5 minutes: downloads and reads new documents and voice notes

`wa-memory service install | uninstall | status`. On Linux, run `wa-memory run` as a systemd user service and
`wa-memory fetch-media` from a timer instead.
"""
import os
import plistlib
import subprocess
import sys

from . import store

LINK = "dev.wa-memory.link"
MEDIA = "dev.wa-memory.media"
MAX_LOG = 5_000_000
# Settings the jobs need to see as the user's shell does.
PASS_ENV = ("WA_MEMORY_HOME", "WA_MEMORY_DEVICE_NAME", "FFMPEG_BIN", "WHISPER_BIN", "WHISPER_MODEL", "TEXTUTIL_BIN",
            "SIPS_BIN", "PDF_PYTHON")


def _agents():
    d = os.path.expanduser("~/Library/LaunchAgents")
    os.makedirs(d, exist_ok=True)
    return d


def _logs():
    d = os.path.join(store.private_home(), "logs")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _q(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _job(log, args):
    """`args` run by /bin/sh, appending to `log`, which is emptied once it grows past MAX_LOG."""
    cmd = " ".join(_q(a) for a in args)
    script = ('umask 077; f=%s; [ "$(stat -f%%z "$f" 2>/dev/null || echo 0)" -gt %d ] && : > "$f"; '
              'date "+%%F %%T start" >> "$f"; exec %s >> "$f" 2>&1' % (_q(log), MAX_LOG, cmd))
    return ["/bin/sh", "-c", script]


def _env():
    env = {k: os.environ[k] for k in PASS_ENV if os.environ.get(k)}
    env["WA_MEMORY_HOME"] = store.home()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    return env


def _load(label, plist):
    path = os.path.join(_agents(), label + ".plist")
    with open(path, "wb") as fh:
        plistlib.dump(plist, fh)
    os.chmod(path, 0o600)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, label)], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", "gui/%d" % uid, path], capture_output=True, text=True)
    return "started %s" % label if r.returncode == 0 else "could not start %s: %s" % (label, r.stderr.strip())


def install():
    py = sys.executable
    link = _load(LINK, {
        "Label": LINK, "ProgramArguments": _job(os.path.join(_logs(), "link.log"), [py, "-m", "wa_memory.cli", "run"]),
        "EnvironmentVariables": _env(), "RunAtLoad": True,
        # Restarted after a crash, not after a clean stop (logged out, temporarily banned, replaced).
        "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 60})
    media = _load(MEDIA, {
        "Label": MEDIA, "ProgramArguments": _job(os.path.join(_logs(), "media.log"),
                                                 [py, "-m", "wa_memory.cli", "fetch-media", "--budget", "150"]),
        "EnvironmentVariables": _env(), "StartInterval": 300, "RunAtLoad": True})
    return "\n".join([link, media])


def uninstall():
    out = []
    for label in (LINK, MEDIA):
        subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), label)], capture_output=True)
        path = os.path.join(_agents(), label + ".plist")
        if os.path.exists(path):
            os.remove(path)
        out.append("removed %s" % label)
    return "\n".join(out)


def status():
    out = []
    for label in (LINK, MEDIA):
        r = subprocess.run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)], capture_output=True, text=True)
        if r.returncode != 0:
            out.append("%s: not installed" % label)
            continue
        keep = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith(("state", "pid", "last exit code"))][:3]
        out.append("%s: %s" % (label, "; ".join(keep)))
    conn = store.open_memory()
    out.append("link: %s (last seen %s)" % (store.get_meta(conn, "live_state") or "not started yet",
                                           store.iso(store.get_meta(conn, "live_seen_at")) or "never"))
    return "\n".join(out)


def run(action):
    if action == "status":
        return status() if sys.platform == "darwin" else "Background jobs are macOS-only for now (launchd)."
    if sys.platform != "darwin":
        return "Background jobs are macOS-only for now. On Linux, run `wa-memory run` as a systemd user service."
    return install() if action == "install" else uninstall()
