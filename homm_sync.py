#!/usr/bin/env python3
"""Sync HoMM saves with the cloud-save server, launch the game, keep syncing while it runs."""
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# frozen (pyinstaller) -> .env / last_sync.txt live next to the exe, not in the temp unpack dir
HERE = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
LAST_SYNC = HERE / "last_sync.txt"


def load_env(path):
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


cfg = load_env(HERE / ".env")
API = cfg["SERVER"].rstrip("/") + "/apis/call"
GAMES = Path(cfg["GAMES_DIR"]).expanduser().resolve()
BASE = {"scope": "homm_saves", "machine": cfg["MACHINE"]}

s = requests.Session()
s.verify = cfg.get("VERIFY_TLS", "true").lower() == "true"
if not s.verify:
    requests.packages.urllib3.disable_warnings()


def call(method, **kw):
    r = s.request(method, API, timeout=60, **kw)
    if not r.ok:  # include the server's explanation, not just the status line
        raise RuntimeError(f"{r.status_code} {r.reason} for {r.url}: {r.text[:500]}")
    return r


def sync():
    t0 = datetime.now(timezone.utc)
    last = datetime.fromisoformat(LAST_SYNC.read_text().strip()) if LAST_SYNC.exists() else None

    jwt = call("GET", params={"username": cfg["API_USER"], "password": cfg["API_PASSWORD"]}).json()["jwt"]
    s.headers["x-api-key"] = jwt

    # snapshot local changes before pulling, so pulled files aren't mistaken for local edits
    local = {
        p.relative_to(GAMES).as_posix(): p
        for p in GAMES.rglob("*")
        if p.is_file() and (last is None or p.stat().st_mtime > last.timestamp())
    }

    since = {"since": last.strftime("%Y-%m-%dT%H:%M:%SZ")} if last else {}
    changes = call("GET", params={**BASE, "route": "changes", **since}).json()
    for item in changes.get("data", []):
        path = item["path"]
        remote_ts = datetime.fromisoformat(item.get("updated_at", "1970-01-01T00:00:00Z").replace("Z", "+00:00")).timestamp()
        if path in local and local[path].stat().st_mtime >= remote_ts:
            print(f"conflict, local is newer, keeping local: {path}")
            continue
        local.pop(path, None)  # remote is newer: pull it, don't push the older local copy
        out = (GAMES / path).resolve()
        if not out.is_relative_to(GAMES):
            print(f"refusing path outside games dir: {path}")
            continue
        print(f"pull {path}")
        r = s.get(API, params={**BASE, "route": "file", "path": path}, timeout=60)
        if r.status_code == 404:  # listed in changes but gone on the server; skip so it can't block the sync
            print(f"  missing on server, skipped: {path}")
            continue
        if not r.ok:
            raise RuntimeError(f"{r.status_code} {r.reason} for {r.url}: {r.text[:500]}")
        data = r.content
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        os.utime(out, (t0.timestamp(), t0.timestamp()))  # mtime == t0 -> not re-pushed next sync

    for path, p in local.items():
        print(f"push {path}")
        with open(p, "rb") as f:
            call("PUT", params={**BASE, "route": "file"}, data={"path": path}, files={"file": f})

    LAST_SYNC.write_text(t0.isoformat())  # only after everything succeeded


def safe_sync():
    try:
        sync()
    except Exception as e:  # network down etc.; last_sync untouched so next sync retries
        print(f"sync failed: {e}")


def main():
    sys.stdout.reconfigure(line_buffering=True)  # show output immediately even when logged/piped
    safe_sync()
    game = subprocess.Popen(cfg["GAME_CMD"], shell=True)
    cadence = int(cfg.get("UPLOAD_CADENCE", 300))
    while True:
        try:
            game.wait(timeout=cadence)
            break
        except subprocess.TimeoutExpired:
            safe_sync()
    safe_sync()


if __name__ == "__main__":
    main()
