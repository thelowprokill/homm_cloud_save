#!/usr/bin/env python3
"""Sync HoMM saves (and RMG templates on start) with the cloud-save server, launch the game, keep syncing saves while it runs."""
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# frozen (pyinstaller) -> config / last_sync*.txt / log live next to the exe, not in the temp unpack dir
HERE = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(HERE / "homm_sync.log", mode="w"), logging.StreamHandler()],
)
log = logging.getLogger("homm_sync")
sys.excepthook = lambda *exc: log.critical("crashed", exc_info=exc)  # e.g. missing config, so it lands in the log too


def load_env(path):
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


DEFAULT_ENV = """\
# homm_sync settings. Write values as-is, without quotes around them
# (GAME_CMD is the exception: it's a shell command, so quote a program name with spaces).
# GAME_DIR is relative to the folder this program is in; GAMES_DIR and TEMPLATES_DIR are relative to GAME_DIR.

# Server connection; fill these in. MACHINE must be different on each device.
SERVER=
MACHINE=
API_USER=
API_PASSWORD=
# false only for a self-signed certificate (e.g. a local test server)
VERIFY_TLS=true

# Game folder; GAME_CMD is run from inside it. Windows: quote names with spaces.
# Linux example: GAME_CMD=wine "h3hota HD.exe"
GAME_DIR=..
GAME_CMD="h3hota HD.exe"

# Saves folder, synced on start, every UPLOAD_CADENCE seconds, and when the game closes
GAMES_DIR=Games
# RMG templates folder, synced on start only; leave blank to skip
TEMPLATES_DIR=HotA_RMGTemplates
# seconds between save syncs while the game runs
UPLOAD_CADENCE=30
"""

ENV_FILE = HERE / "homm_sync.env"  # not ".env": wine/winlator hide dotfiles
if not ENV_FILE.exists():
    ENV_FILE.write_text(DEFAULT_ENV)
    log.error(f"created {ENV_FILE} with default settings; fill in SERVER, MACHINE, API_USER and API_PASSWORD, then run again")
    sys.exit(1)
cfg = load_env(ENV_FILE)
missing = [k for k in ("SERVER", "MACHINE", "API_USER", "API_PASSWORD") if not cfg.get(k)]
if missing:
    log.error(f"fill in {', '.join(missing)} in {ENV_FILE}, then run again")
    sys.exit(1)
API = cfg["SERVER"].rstrip("/") + "/apis/call"


def rel_dir(key, base):  # absolute paths are used as-is
    return (base / Path(cfg[key]).expanduser()).resolve()


GAME_DIR = rel_dir("GAME_DIR", HERE) if cfg.get("GAME_DIR") else HERE  # relative to the exe


# GAMES_DIR / TEMPLATES_DIR are relative to GAME_DIR.
# section -> (local folder, file holding that section's last sync time); each section is tracked separately
SECTIONS = {"saves": (rel_dir("GAMES_DIR", GAME_DIR), HERE / "last_sync.txt")}
if cfg.get("TEMPLATES_DIR"):  # optional: RMG templates, synced on start only
    SECTIONS["templates"] = (rel_dir("TEMPLATES_DIR", GAME_DIR), HERE / "last_sync_templates.txt")

s = requests.Session()
s.verify = cfg.get("VERIFY_TLS", "true").lower() == "true"
if not s.verify:
    requests.packages.urllib3.disable_warnings()


def call(method, **kw):
    r = s.request(method, API, timeout=60, **kw)
    if not r.ok:  # include the server's explanation, not just the status line
        raise RuntimeError(f"{r.status_code} {r.reason} for {r.url}: {r.text[:500]}")
    return r


def scan(d):
    # os.scandir, not rglob+stat: on Windows the type and mtime come with the directory listing,
    # so there is no per-file stat call. Under wine/winlator that is ~70x faster.
    with os.scandir(d) as it:
        for e in it:
            if e.is_dir():
                yield from scan(e.path)
            elif e.is_file():
                yield e


def sync(section, final=False):
    root, last_file = SECTIONS[section]
    base = {"scope": "homm_saves", "section": section, "machine": cfg["MACHINE"]}
    t0 = datetime.now(timezone.utc)
    last = datetime.fromisoformat(last_file.read_text().strip()) if last_file.exists() else None

    # snapshot local changes before pulling, so pulled files aren't mistaken for local edits
    n = 0
    local = {}
    for e in scan(root):
        n += 1
        if last is None or e.stat().st_mtime > last.timestamp():
            local[Path(e.path).relative_to(root).as_posix()] = Path(e.path)
    log.info(f"{section}: scanned {n} files, {len(local)} changed ({(datetime.now(timezone.utc) - t0).total_seconds():.1f}s)")

    # nothing to upload on exit: skip the server entirely.
    # last_sync stays untouched, so remote changes still get pulled on the next start.
    if final and not local:
        log.info(f"{section}: no local changes, skipping final sync")
        return

    if "x-api-key" not in s.headers:  # log in once; dropped again when a sync fails
        log.info("login")
        s.headers["x-api-key"] = call("GET", params={"username": cfg["API_USER"], "password": cfg["API_PASSWORD"]}).json()["jwt"]

    since = {"since": last.strftime("%Y-%m-%dT%H:%M:%SZ")} if last else {}
    changes = call("GET", params={**base, "route": "changes", **since}).json()
    failed = []  # one bad file (e.g. too large for the server) shouldn't stop the rest of the section
    for item in changes.get("data", []):
        path = item["path"]
        remote_ts = datetime.fromisoformat(item.get("updated_at", "1970-01-01T00:00:00Z").replace("Z", "+00:00")).timestamp()
        if path in local and local[path].stat().st_mtime >= remote_ts:
            log.info(f"{section}: conflict, local is newer, keeping local: {path}")
            continue
        local.pop(path, None)  # remote is newer: pull it, don't push the older local copy
        out = (root / path).resolve()
        if not out.is_relative_to(root):
            log.warning(f"{section}: refusing path outside {root}: {path}")
            continue
        log.info(f"{section}: pull {path}")
        r = s.get(API, params={**base, "route": "file", "path": path}, timeout=60)
        if r.status_code == 404:  # listed in changes but gone on the server; skip so it can't block the sync
            log.warning(f"{section}: missing on server, skipped: {path}")
            continue
        if not r.ok:
            log.error(f"{section}: pull failed, skipped: {path}: {r.status_code} {r.reason}: {r.text[:200]}")
            failed.append(path)
            continue
        data = r.content
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        os.utime(out, (t0.timestamp(), t0.timestamp()))  # mtime == t0 -> not re-pushed next sync

    for path, p in local.items():
        log.info(f"{section}: push {path}")
        try:
            with open(p, "rb") as f:
                call("PUT", params={**base, "route": "file"}, data={"path": path}, files={"file": f})
        except Exception as e:
            log.error(f"{section}: push failed, skipped: {path}: {str(e)[:300]}")
            failed.append(path)

    if failed:  # keep last_sync as-is so the failed files are retried next time
        log.error(f"{section}: {len(failed)} file(s) failed, will retry next sync")
        return
    last_file.write_text(t0.isoformat())  # only after everything succeeded
    log.info(f"{section}: sync ok ({(datetime.now(timezone.utc) - t0).total_seconds():.1f}s)")


def safe_sync(section="saves", final=False):
    for attempt in (1, 2):  # a failure may be an expired token: log in again and retry once
        try:
            sync(section, final)
            return
        except Exception as e:  # network down etc.; last_sync untouched so next sync retries
            s.headers.pop("x-api-key", None)
            log.error(f"{section}: sync failed (attempt {attempt}): {e}")


def main():
    log.info(f"start, machine={cfg['MACHINE']}, " + ", ".join(f"{k} dir={v[0]}" for k, v in SECTIONS.items()))
    log.info(f"launching: {cfg['GAME_CMD']} (in {GAME_DIR})")
    game = subprocess.Popen(cfg["GAME_CMD"], shell=True, cwd=GAME_DIR)
    safe_sync()  # runs while the game starts up
    if "templates" in SECTIONS:
        safe_sync("templates")  # start only
    cadence = int(cfg.get("UPLOAD_CADENCE", 300))
    while True:
        try:
            game.wait(timeout=cadence)
            log.info(f"game exited with code {game.returncode}")
            break
        except subprocess.TimeoutExpired:
            safe_sync()
    safe_sync(final=True)
    log.info("done")


if __name__ == "__main__":
    main()
