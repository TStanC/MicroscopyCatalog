"""One build at a time: ``build.lock`` in the catalog folder, held only while a build runs.

Every catalog server is a peer: edits go into the inbox of the catalog folder (mdcat.inbox)
without any lock. Only a build - which merges the inbox, scans Labdata and publishes the
catalog - takes ``<catalog_dir>/build.lock``:

* created with an exclusive create (atomic on SMB: the file server decides who wins);
* contains {host, user, pid, token, heartbeat}; the build refreshes the heartbeat every
  minute from a background thread;
* a lock whose heartbeat is older than STALE_S (crashed build, machine off, network gone)
  is taken over;
* released (deleted) at the end of the build.

``server.lock`` is the permanent lock of catalog servers before the edit inbox. A fresh one held
by another machine means an old server still maintains the folder: then nothing is written
(mixing both versions would lose edits) until that machine is updated.
"""

from __future__ import annotations

import getpass
import json
import os
import secrets
import socket
import threading
import time
from datetime import datetime

from . import safety

NAME = "build.lock"
LEGACY = "server.lock"
STALE_S = 600
HEARTBEAT_S = 60


def me() -> dict:
    return {"host": socket.gethostname().lower(), "user": getpass.getuser().lower()}


def read(catalog_dir: str, name: str = NAME) -> dict | None:
    """The lock record; None if there is no lock. A lock that exists but cannot be parsed (being
    written right now) is returned as {"unreadable": True} with its file time as heartbeat."""
    path = os.path.join(catalog_dir, name)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        try:
            return {"unreadable": True, "heartbeat_epoch": os.path.getmtime(path)}
        except OSError:
            return None


def fresh(h: dict | None) -> bool:
    return bool(h) and time.time() - float(h.get("heartbeat_epoch", 0)) < STALE_S


def describe(h: dict) -> str:
    if h.get("unreadable"):
        return "another machine (lock being written)"
    return (f"{h.get('user', '?')} on {h.get('host', '?')} "
            f"(last sign of life {str(h.get('heartbeat', '?')).replace('T', ' ')})")


def legacy_holder(catalog_dir: str) -> dict | None:
    """A fresh server.lock of an old catalog server on another machine/user, else None."""
    h = read(catalog_dir, LEGACY)
    if not fresh(h):
        return None
    m = me()
    return None if (h.get("host"), h.get("user")) == (m["host"], m["user"]) else h


def current_holder(catalog_dir: str) -> dict | None:
    """Who is building right now (fresh build.lock), else None."""
    h = read(catalog_dir)
    return h if fresh(h) else None


PREVIEW_NAME = "preview.lock"   # held by make_previews.py: one machine renders previews at a time


class BuildLease:
    """``with``-less use: acquire() -> None (ours) or the holder; check() before publishing;
    release() at the end. ``name`` = PREVIEW_NAME for the preview lock (same mechanism)."""

    def __init__(self, catalog_dir: str, name: str = NAME):
        self.dir = catalog_dir
        self.name = name
        self.token = secrets.token_hex(8)
        self.started = datetime.now().isoformat(timespec="seconds")
        self.lost = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _record(self) -> bytes:
        rec = {**me(), "pid": os.getpid(), "token": self.token, "started": self.started,
               "heartbeat": datetime.now().isoformat(timespec="seconds"), "heartbeat_epoch": time.time()}
        return json.dumps(rec, indent=1).encode("utf-8")

    def _mine(self) -> bool:
        h = read(self.dir, self.name)
        return bool(h) and h.get("token") == self.token

    def acquire(self) -> dict | None:
        """Take the lock. Returns None on success, else the holder (fresh build.lock, or a
        fresh legacy server.lock with "legacy": True)."""
        old = legacy_holder(self.dir) if self.name == NAME else None
        if old:
            return {**old, "legacy": True}
        for _ in range(3):
            if safety.create_exclusive(self.dir, self.name, self._record()):
                time.sleep(1.0)  # a taker of a stale lock may have removed + recreated it meanwhile
                if not self._mine():
                    return read(self.dir, self.name) or {"unreadable": True}
                if self.name == NAME:
                    self._cleanup_legacy()
                self._thread = threading.Thread(target=self._beat, daemon=True, name=f"lease-{self.name}")
                self._thread.start()
                return None
            h = read(self.dir, self.name)
            if h is None:
                continue  # released between our create and read: try again
            if fresh(h):
                return h
            # stale: remove it - but only if it is still the same stale lock (two takers at once)
            again = read(self.dir, self.name)
            if again and again.get("token") == h.get("token") and not fresh(again):
                try:
                    safety.unpublish_file(self.dir, self.name)
                except FileNotFoundError:
                    pass
        return read(self.dir, self.name) or {"unreadable": True}

    def _cleanup_legacy(self) -> None:
        if read(self.dir, LEGACY) is not None and not legacy_holder(self.dir):
            try:
                safety.unpublish_file(self.dir, LEGACY)
            except OSError:
                pass

    def _beat(self) -> None:
        while not self._stop.wait(HEARTBEAT_S):
            try:
                if not self._mine():
                    h = read(self.dir, self.name)
                    self.lost = f"{self.name} was taken over by {describe(h) if h else 'nobody (removed)'}"
                    return
                safety.publish_file(self.dir, self.name, self._record())
            except OSError:
                pass  # share briefly unreachable: try again at the next beat (stale only after STALE_S)

    def check(self) -> None:
        """Raise if this build no longer holds the lock (call before writing the catalog folder)."""
        if self.lost:
            raise RuntimeError(self.lost)
        if not self._mine():
            h = read(self.dir, self.name)
            raise RuntimeError(f"{self.name} is no longer held by this build ({describe(h) if h else 'removed'})")

    def release(self) -> None:
        self._stop.set()
        try:
            if self._mine():
                safety.unpublish_file(self.dir, self.name)
        except OSError:
            pass
