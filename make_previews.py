r"""Create missing preview images for the catalog (separately from the build).

    .venv\Scripts\python.exe make_previews.py [--budget MIN]

Reads the list of images from catalog.sqlite (no scan), reads pixel data read-only with
Bio-Formats and writes previews/<id>.jpg into the working directory. Runs under its own locks
(cache\\preview.lock locally, preview.lock in the catalog folder: one machine renders at a time),
never under build.lock, so catalog builds - e.g. after edits in the browser - are never blocked
by it. Each new preview is copied into the catalog folder at once; previews another machine
already made are copied from there instead of rendered. The next build (any machine) links them
into the page; the catalog server starts one automatically. Exit code 0 = ok, 3 = already
running (here or on another machine).
Prints a last line "PREVIEWS made=<n> fetched=<n> deferred=<n> failed=<n>".
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mdcat import lease, previews, safety  # noqa: E402
from mdcat.config import load_config  # noqa: E402
from mdcat.locks import FileLock  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--budget", type=float, default=None, help="minutes (default: preview_budget_min, 1380 = 23 h)")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if not cfg.labdata_root or not os.path.isdir(cfg.labdata_root):
        print("PREVIEWS skipped: not set up or Labdata not reachable", flush=True)
        return 4
    if args.budget is None and not cfg.previews_on:
        print("PREVIEWS made=0 deferred=0 failed=0 (previews = off)", flush=True)
        return 0
    safety.configure(cfg.labdata_root, cfg.out_dir)
    safety.makedirs(os.path.join(cfg.out_dir, "logs"))
    lock = FileLock(os.path.join(cfg.cache_dir, "preview.lock"))
    if not lock.acquire(timeout=0):
        print("make_previews is already running", file=sys.stderr)
        print("PREVIEWS skipped: already running on this machine", flush=True)
        return 3
    share_dir, share_lease = None, None
    if cfg.publish_dir:
        try:
            safety.configure_publish(cfg.publish_dir)
            share_lease = lease.BuildLease(cfg.publish_dir, lease.PREVIEW_NAME)
            holder = share_lease.acquire()
            share_dir = cfg.publish_dir
        except (OSError, RuntimeError):
            share_lease = holder = None  # catalog folder offline: render locally, a later build copies them
        if holder:
            lock.release()
            print(f"PREVIEWS skipped: previews are being made on another machine ({lease.describe(holder)})",
                  flush=True)
            return 3
    started = datetime.now()
    log_fh = safety.open_write(os.path.join(cfg.out_dir, "logs", f"previews_{started:%Y%m%d_%H%M%S}.log"),
                               "w", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        print(line, flush=True)
        log_fh.write(line + "\n")
        log_fh.flush()

    try:
        if not os.path.exists(cfg.db_path):
            log("no catalog database yet - run build_catalog.py first")
            return 1
        db = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True, timeout=30)
        try:
            items = previews.plan_from_db(cfg, db)
        finally:
            db.close()
        budget = (args.budget if args.budget is not None else cfg.preview_budget_min) * 60
        stats = previews.make(cfg, items, log, budget, share_dir)
        stats.pop("have", None)
        log(f"Previews: {stats}")
        print(f"PREVIEWS made={stats['made']} fetched={stats['fetched']} deferred={stats['deferred']} "
              f"failed={stats['failed']}", flush=True)
        return 0
    finally:
        log_fh.close()
        if share_lease:
            share_lease.release()
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
