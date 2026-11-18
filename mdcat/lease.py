"""One writer at a time: ``server.lock`` in the catalog folder.

The catalog server that owns the catalog folder writes {host, user, pid, heartbeat} into
``<catalog_dir>/server.lock`` and refreshes the heartbeat every couple of minutes. Another
server (other machine or other Windows user) sees a fresh lock and stays read-only; a lock
whose heartbeat is older than STALE_S (crashed server, machine off) can be taken over.
Builds started by hand on the owning machine (same host + user) may publish.
"""

from __future__ import annotations

import getpass
import json
import os
import socket
import time
from datetime import datetime

from . import safety

NAME = "server.lock"
STALE_S = 600
HEARTBEAT_S = 120


def me() -> dict:
    return {"host": socket.gethostname().lower(), "user": getpass.getuser().lower()}


def read(catalog_dir: str) -> dict | None:
    try:
        with open(os.path.join(catalog_dir, NAME), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _fresh(h: dict) -> bool:
    return time.time() - float(h.get("heartbeat_epoch", 0)) < STALE_S


def _mine(h: dict) -> bool:
    m = me()
    return h.get("host") == m["host"] and h.get("user") == m["user"]


def other_holder(catalog_dir: str) -> dict | None:
    """The holder of a fresh lock that is not this host+user, else None."""
    h = read(catalog_dir)
    return h if h and _fresh(h) and not _mine(h) else None


def describe(h: dict) -> str:
    return (f"{h.get('user', '?')} on {h.get('host', '?')} "
            f"(last sign of life {h.get('heartbeat', '?').replace('T', ' ')})")


def _write(catalog_dir: str) -> None:
    rec = {**me(), "pid": os.getpid(), "heartbeat": datetime.now().isoformat(timespec="seconds"),
           "heartbeat_epoch": time.time()}
    safety.publish_file(catalog_dir, NAME, json.dumps(rec, indent=1).encode("utf-8"))


def acquire(catalog_dir: str) -> dict | None:
    """Take the lock. Returns None on success, else the other holder."""
    h = other_holder(catalog_dir)
    if h:
        return h
    _write(catalog_dir)
    time.sleep(1.5)  # two servers starting at the same moment: the later write wins, the other backs off
    h = read(catalog_dir)
    if h and not _mine(h):
        return h
    return None


def heartbeat(catalog_dir: str) -> dict | None:
    """Refresh the lock. Returns None while it is ours, else the holder that took it over."""
    h = read(catalog_dir)
    if h and not _mine(h):
        return h
    _write(catalog_dir)
    return None


def release(catalog_dir: str) -> None:
    h = read(catalog_dir)
    if h and _mine(h) and h.get("pid") == os.getpid():
        safety.unpublish_file(catalog_dir, NAME)
