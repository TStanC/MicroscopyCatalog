"""Catalog folder (master copy, e.g. in Labdata) <-> local working copy (.work).

SQLite must not be written live over SMB/VPN (a dropped connection can corrupt it), so the
server works on a local copy and every build pushes a consistent snapshot to the catalog
folder (build_catalog.publish). ``sync.json`` in both places carries a generation number:

* catalog folder newer than the local copy (other machine published, or a fresh install)
  -> pull: database, manual values, page, previews, raw-metadata cache, edit log.
* local copy has changes that could not be pushed (share offline) -> pushed with the next
  build; if the catalog folder moved on in the meantime, the local copy is backed up to
  .work/backup first and then replaced. Edits from the browser are not affected: they stay
  in the inbox of the catalog folder until a published build has applied them.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import socket
from datetime import datetime

from . import safety

STATE = "sync.json"


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def share_state(cfg) -> dict:
    return _read_json(os.path.join(cfg.publish_dir, STATE))


def local_state(cfg) -> dict:
    return _read_json(os.path.join(cfg.out_dir, STATE))


def _save_local(cfg, st: dict) -> None:
    safety.replace_local(os.path.join(cfg.out_dir, STATE), json.dumps(st, indent=1).encode("utf-8"))


def mark_pending(cfg) -> None:
    st = local_state(cfg)
    st["pending_push"] = True
    _save_local(cfg, st)


def check_not_behind(cfg) -> None:
    """Refuse to push over a newer catalog folder (another machine published after our pull)."""
    sh, lo = share_state(cfg), local_state(cfg)
    if int(sh.get("generation", 0)) > int(lo.get("generation", 0)):
        raise RuntimeError(f"the catalog folder is newer (generation {sh.get('generation')} from "
                           f"{sh.get('host', '?')}) than this working copy ({lo.get('generation', 0)}) - "
                           f"it is pulled at the start of the next build")


def commit_push(cfg) -> int:
    """After a complete publish: bump the generation in the catalog folder and locally."""
    gen = max(int(share_state(cfg).get("generation", 0)), int(local_state(cfg).get("generation", 0))) + 1
    rec = {"generation": gen, "host": socket.gethostname().lower(), "user": getpass.getuser().lower(),
           "time": datetime.now().isoformat(timespec="seconds")}
    safety.publish_file(cfg.publish_dir, STATE, json.dumps(rec, indent=1).encode("utf-8"))
    _save_local(cfg, {"generation": gen, "pending_push": False, "pushed": rec["time"]})
    return gen


def _copy_in(src: str, dst: str) -> None:
    with open(src, "rb") as fh:
        safety.replace_local(dst, fh.read())


def pull_if_newer(cfg, log) -> bool:
    """Bring the working copy up to the catalog folder's state. Returns True if pulled."""
    pdir = cfg.publish_dir
    share_db = os.path.join(pdir, "catalog.sqlite")
    if not os.path.exists(share_db):
        return False  # empty catalog folder: nothing to pull, the first build fills it
    sh, lo = share_state(cfg), local_state(cfg)
    have_local = os.path.exists(cfg.db_path)
    newer = int(sh.get("generation", 0)) > int(lo.get("generation", 0))
    if have_local and not newer:
        return False
    if have_local and lo.get("pending_push"):
        bdir = os.path.join(cfg.out_dir, "backup", f"{datetime.now():%Y%m%d_%H%M%S}")
        safety.makedirs(bdir)
        for name in ("catalog.sqlite", "manual_overrides.csv"):
            if os.path.exists(os.path.join(cfg.out_dir, name)):
                shutil.copy2(os.path.join(cfg.out_dir, name), safety.check_writable(os.path.join(bdir, name)))
        log(f"WARNING: local changes that were never copied to the catalog folder are replaced by its newer "
            f"state; the old local copy is kept in {bdir}")
    log(f"Pulling the catalog from {pdir} (generation {sh.get('generation', 0)}, "
        f"written by {sh.get('host', '?')} {sh.get('time', '')})")
    _copy_in(share_db, cfg.db_path)
    for src, dst in (("manual_overrides.csv", "manual_overrides.csv"), ("index.html", "catalog.html"),
                     (os.path.join("logs", "web_edits.log"), os.path.join("logs", "web_edits.log"))):
        if os.path.exists(os.path.join(pdir, src)):
            _copy_in(os.path.join(pdir, src), os.path.join(cfg.out_dir, dst))
    n = 0
    for sub, local in (("previews", os.path.join(cfg.out_dir, "previews")),
                       (os.path.join("metadata", "xml"), os.path.join(cfg.cache_dir, "xml")),
                       (os.path.join("metadata", "orig"), os.path.join(cfg.cache_dir, "orig"))):
        sdir = os.path.join(pdir, sub)
        if not os.path.isdir(sdir):
            continue
        safety.makedirs(local)
        have = set(os.listdir(local))
        for e in os.scandir(sdir):
            if e.is_file() and not e.name.endswith(".tmp") and e.name not in have:
                _copy_in(e.path, os.path.join(local, e.name))
                n += 1
    _save_local(cfg, {"generation": int(sh.get("generation", 0)), "pending_push": False,
                      "pulled": datetime.now().isoformat(timespec="seconds")})
    log(f"  pulled database + {n} preview/metadata file(s)")
    return True


def pull_previews(cfg, log) -> int:
    """Copy previews that are in the catalog folder but not in the working copy (made on another
    machine by make_previews.py, which copies them there one by one). Every build does this, so it
    links them into the page and never treats them as missing."""
    from . import previews  # noqa: PLC0415 - avoid an import cycle at module load
    sdir, local = os.path.join(cfg.publish_dir, "previews"), previews.previews_dir(cfg)
    if not os.path.isdir(sdir):
        return 0
    safety.makedirs(local)
    have = set(os.listdir(local))
    n = 0
    for e in os.scandir(sdir):
        if e.is_file() and e.name.endswith(".jpg") and e.name not in have:
            _copy_in(e.path, os.path.join(local, e.name))
            n += 1
    if n:
        log(f"  took {n} preview(s) made on other machines from the catalog folder")
    return n


def push_dir(pdir: str, sub: str, local: str, pattern_prefix: str, remove_stale: bool,
             keep: set[str] | None = None) -> int:
    """Copy new/changed files of ``local`` into <pdir>/<sub> (one listing per side). With
    ``remove_stale``, files there that are not in ``local`` are removed - unless named in ``keep``
    (e.g. previews another machine copied there while this build ran)."""
    if not os.path.isdir(local):
        return 0
    sdir = os.path.join(pdir, *sub.split("/"))
    remote = {e.name: e.stat().st_size for e in os.scandir(sdir) if e.is_file()} if os.path.isdir(sdir) else {}
    n, local_names = 0, set()
    for e in os.scandir(local):
        if not e.is_file() or e.name.endswith(".tmp"):
            continue
        local_names.add(e.name)
        if remote.get(e.name) != e.stat().st_size:
            with open(e.path, "rb") as fh:
                safety.publish_file(pdir, f"{pattern_prefix}{e.name}", fh.read())
            n += 1
    if remove_stale:
        for name in remote:
            if name not in local_names and name not in (keep or ()) and not name.endswith(".tmp"):
                safety.unpublish_file(pdir, f"{pattern_prefix}{name}")
    return n
