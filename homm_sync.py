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
    r.raise_for_status()
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
        if path in local:
            print(f"conflict, keeping local: {path}")
            continue
        out = (GAMES / path).resolve()
        if not out.is_relative_to(GAMES):
            print(f"refusing path outside games dir: {path}")
            continue
        print(f"pull {path}")
        data = call("GET", params={**BASE, "route": "file", "path": path}).content
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
