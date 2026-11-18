"""Build / update the microscopy dataset catalog of the Labdata directory.

Read-only with respect to the data: files below ``labdata_root`` are only listed
and opened for reading. The build works in the local working copy (.work) and then copies
the result into the catalog folder (``catalog_dir``, settings.toml) - the only place it ever
writes to on the share. At the start it pulls the catalog folder if that is newer.

Run (PowerShell, from the program folder):
    .venv\\Scripts\\python.exe build_catalog.py

Options:
    --interactive      prompt in the terminal for values that could not be read
    --retry-errors     re-read files that failed in an earlier run (even if unchanged)
    --force            re-read every file (ignores the change signatures)
    --verify-readonly  re-list the data directory at the end and report any change
    --only TEXT        restrict to paths containing TEXT (testing; use with --out)
    --out DIR          separate working directory for tests (never pulls or publishes)
    --no-publish       do not copy the result into the catalog folder
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mdcat import TOOL_VERSION, lease, locks, safety, sync  # noqa: E402
from mdcat import comments as cm  # noqa: E402
from mdcat import previews  # noqa: E402
from mdcat.config import load_config  # noqa: E402
from mdcat.derive import ACQ_COLUMNS, IMG_COLUMNS, SCENE_COLUMNS, derive  # noqa: E402
from mdcat.extract import update_sources, load_sources  # noqa: E402
from mdcat import overrides as ov  # noqa: E402
from mdcat import report  # noqa: E402
from mdcat.scan import abs_path, classify, walk  # noqa: E402


EXIT_BUSY = 3
EXIT_NOT_SET_UP = 2
EXIT_OFFLINE = 4
EXIT_NOT_OWNER = 5


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="settings file (default: settings.toml in the program folder)")
    ap.add_argument("--out", default=None, help="separate working directory for tests (no pull/publish)")
    ap.add_argument("--interactive", action="store_true")
    ap.add_argument("--retry-errors", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--verify-readonly", action="store_true")
    ap.add_argument("--only", default=None)
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--no-previews", action="store_true", help="do not create new preview images in this run")
    ap.add_argument("--preview-budget", type=float, default=None,
                    help="minutes for new previews in this run (default: preview_budget_min)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config, args.out)
    if not cfg.labdata_root:
        print("Not set up yet: start the catalog server and fill in the setup page "
              "(or set labdata_root / catalog_dir in settings.toml).", file=sys.stderr)
        return EXIT_NOT_SET_UP
    if not os.path.isdir(cfg.labdata_root):
        print(f"Labdata folder not reachable: {cfg.labdata_root} (network drive / VPN?)", file=sys.stderr)
        return EXIT_OFFLINE
    safety.configure(cfg.labdata_root, cfg.out_dir)
    # Test runs (--only / another --out) never pull or publish.
    is_test = bool(args.only) or os.path.normcase(cfg.out_dir) != os.path.normcase(cfg.data_dir)
    publish_dir = None if (args.no_publish or is_test) else cfg.publish_dir or None
    if publish_dir:
        try:
            safety.configure_publish(publish_dir)
            holder = lease.other_holder(publish_dir)
        except (OSError, RuntimeError):
            holder = None  # catalog folder offline: build locally, push later (see _run)
        if holder:
            print(f"The catalog folder {publish_dir} is maintained by the catalog server of "
                  f"{lease.describe(holder)}; this machine must not write it. "
                  f"Use --out <folder> for a separate test catalog.", file=sys.stderr)
            return EXIT_NOT_OWNER
    safety.makedirs(cfg.cache_dir)
    safety.makedirs(os.path.join(cfg.out_dir, "logs"))
    lock = locks.build_lock(cfg)
    if not lock.acquire(timeout=0):
        print("Another catalog build is running (cache\\build.lock) - try again later.", file=sys.stderr)
        return EXIT_BUSY
    try:
        return _run(cfg, args, publish_dir)
    finally:
        lock.release()


def _run(cfg, args, publish_dir) -> int:
    started = datetime.now()
    run_ts = started.isoformat(timespec="seconds")
    log_path = os.path.join(cfg.out_dir, "logs", f"run_{started:%Y%m%d_%H%M%S}.log")
    log_fh = safety.open_write(log_path, "w", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        print(line, flush=True)
        log_fh.write(line + "\n")
        log_fh.flush()

    log(f"Working directory {cfg.out_dir} (config {cfg.config_path})")
    p_over = os.path.join(cfg.out_dir, "manual_overrides.csv")
    p_needs = os.path.join(cfg.out_dir, "needs_input.csv")
    share_ok = bool(publish_dir)
    if publish_dir:
        # the catalog folder is the master copy: take it if it is newer (other machine, new install)
        try:
            with locks.edit_lock(cfg):
                sync.pull_if_newer(cfg, log)
        except (OSError, RuntimeError) as exc:
            share_ok = False
            log(f"WARNING: catalog folder {publish_dir} not reachable ({exc}) - working on the local copy; "
                f"the changes are copied there by a later build")
    db = sqlite3.connect(safety.check_writable(cfg.db_path), timeout=30)
    cm.ensure_table(db)

    with locks.edit_lock(cfg):
        ov.ensure_overrides_file(p_over)
        n = ov.harvest_needs_input(p_needs, p_over, log)
    if n:
        log(f"Took {n} value(s) from needs_input.csv into manual_overrides.csv")

    log(f"Scanning {cfg.labdata_root} (read-only) ...")
    scan = walk(cfg)
    for rel, err in scan.errors:
        log(f"  cannot access {rel}: {err}")
    log(f"  {len(scan.files)} files, {len(scan.zarrs)} zarr stores in {scan.seconds:.1f}s")
    classified = classify(scan, cfg)
    if args.only:
        needle = args.only.lower()
        classified.sources = [s for s in classified.sources if needle in s.rel.lower()]
        classified.attachments = [a for a in classified.attachments if needle in a.lower()]
    counts = {}
    for s in classified.sources:
        counts[s.type] = counts.get(s.type, 0) + 1
    log(f"  sources: {counts}, other files: {len(classified.attachments)}")

    stats = update_sources(cfg, db, classified.sources, retry_errors=args.retry_errors, force=args.force, log=log)
    log(f"Extraction: {stats}")

    session_notes = read_session_notes(cfg, classified.attachments)

    def build_and_write(prev_acq, prev_img):
        with locks.edit_lock(cfg):
            overrides = ov.load_overrides(p_over, log)
        d = derive(cfg, classified, load_sources(db), scan.files, overrides, prev_acq, prev_img, run_ts)
        d["overrides"] = [{k: o.get(k, "") for k in ov.OVERRIDE_COLUMNS} for o in overrides]
        d["comments"] = cm.active_comments(db)
        return d

    prev_acq, prev_img = report.load_previous(db)
    data = build_and_write(prev_acq, prev_img)
    if args.interactive:
        with locks.edit_lock(cfg):
            added = ov.interactive(data["needs"], p_over)
        if added:
            log(f"{added} value(s) entered; rebuilding catalog")
            data = build_and_write(prev_acq, prev_img)

    budget = 0 if (args.no_previews or args.only) else \
        (args.preview_budget if args.preview_budget is not None else
         (cfg.preview_budget_min if cfg.previews_on else 0)) * 60
    pstats = previews.update(cfg, data, scan.files, log, budget)
    log(f"Previews: {pstats}")
    if not args.only:  # drop previews of files that changed or disappeared
        keep = {it["key"] + ".jpg" for it in previews.plan(data, scan.files)}
        for f in os.listdir(previews.previews_dir(cfg)):
            if f.endswith(".jpg") and f not in keep:
                safety.remove(os.path.join(previews.previews_dir(cfg), f))

    readonly = "not checked"
    if args.verify_readonly:
        log("Verifying that the data directory was not modified ...")
        after = walk(cfg)
        changed = [r for r in scan.files if r in after.files and after.files[r] != scan.files[r]]
        added = [r for r in after.files if r not in scan.files]
        removed = [r for r in scan.files if r not in after.files]
        readonly = f"changed={len(changed)} added={len(added)} removed={len(removed)}"
        for r in (changed + added + removed)[:50]:
            log(f"  differs: {r}")
        if changed or added or removed:
            log("  NOTE: differences can also come from acquisitions/copies running at the same time.")
        log(f"  read-only check: {readonly}")

    finished = datetime.now()
    changes = [a for a in data["acquisitions"] if a.get("change")]
    run = {
        "started": run_ts, "finished": finished.isoformat(timespec="seconds"), "tool_version": TOOL_VERSION,
        "scan_seconds": round(scan.seconds, 1), "n_sources": len(classified.sources),
        "read": stats["read"], "reparsed": stats["reparsed"], "cached": stats["cached"], "errors": stats["errors"],
        "incomplete": stats["incomplete"],
        "new": sum(a["change"] == "new" for a in changes), "changed": sum(a["change"] == "changed" for a in changes),
        "removed": sum(a["change"] == "removed" for a in changes), "readonly_check": readonly,
    }
    report.write_sqlite(db, data, run)
    out = cfg.out_dir
    acq_sorted = sorted(data["acquisitions"], key=lambda r: (r["instrument"] or "", r["acquired_start"] or "9999", r["path"] or ""))
    report.write_csv(os.path.join(out, "catalog_datasets.csv"),
                     ACQ_COLUMNS + ["first_seen", "last_changed", "present"], acq_sorted)
    report.write_csv(os.path.join(out, "catalog_images.csv"), IMG_COLUMNS,
                     sorted(data["images"], key=lambda r: (r["acq_id"], r["image_key"] or "")))
    report.write_csv(os.path.join(out, "catalog_scenes.csv"), SCENE_COLUMNS,
                     sorted(data["scenes"], key=lambda r: (r["acq_id"], r["series"])))
    report.write_csv(os.path.join(out, "catalog_files.csv"), report.LOC_COLUMNS,
                     sorted(data["locations"], key=lambda r: (r["acq_id"], r["role"], r["rel"])))
    report.write_json(os.path.join(out, "catalog.json"), data, run_ts)
    report.write_markdown(os.path.join(out, "CATALOG.md"), data, run_ts, session_notes)
    report.write_html(os.path.join(out, "catalog.html"), data, run_ts, session_notes, cfg.labdata_root,
                      server_port=cfg.server_port)
    report.write_changelog(os.path.join(out, "CHANGELOG.md"), data, run_ts, stats)
    ov.write_needs_input(p_needs, data["needs"])
    with safety.open_write(os.path.join(out, "comments.csv"), "wb") as fh:
        fh.write(cm.export_csv(db))

    if publish_dir:
        try:
            if not share_ok:
                raise RuntimeError("not reachable at the start of this build")
            publish(cfg, db, log)
        except (OSError, RuntimeError) as exc:  # share offline: the local catalog is complete, push later
            sync.mark_pending(cfg)
            log(f"  WARNING: could not copy the catalog to {cfg.publish_dir}: {exc}")
    db.close()

    present = [a for a in data["acquisitions"] if a.get("present")]
    by_status = {}
    for a in present:
        by_status[a["status"]] = by_status.get(a["status"], 0) + 1
    log(f"Catalog: {len(present)} datasets {by_status}; new {run['new']}, changed {run['changed']}, "
        f"removed {run['removed']}; {len(data['needs'])} value(s) need manual input (needs_input.csv)")
    log(f"Done in {(finished - started).total_seconds():.0f}s. Open {os.path.join(out, 'catalog.html')}")
    log_fh.close()
    return 0


PUBLISH_README = """Microscopy dataset catalog (written automatically - do not edit or add files here)

index.html            catalog page (open in Edge/Chrome/Firefox; read-only)
catalog.sqlite        the catalog database (incl. table `comments`) - master copy
catalog_*.csv         flat tables (datasets, images, scenes/series, files)
catalog.json          nested export
CATALOG.md            overview by instrument and session
CHANGELOG.md          changes per run
comments.csv          full comment log (also in catalog.sqlite)
manual_overrides.csv  manually entered values
previews/             small preview images (overview, max projection, cutout per channel)
metadata/             cached raw metadata (OME-XML) of the image files
logs/web_edits.log    who edited what
sync.json             version of this folder's content
server.lock           which catalog server currently maintains this folder ({owner})

To add comments or missing values, run the catalog server (git repository
"microscopy-catalog", see its INSTALL.md) on your machine and open http://localhost:{port}.
Only one server at a time maintains this folder; others show it read-only.
"""


def publish(cfg, db, log) -> None:
    """Copy the catalog into the catalog folder (whitelisted names, atomic writes) and bump its
    generation in sync.json (written last, so an interrupted publish is simply repeated)."""
    pdir = cfg.publish_dir
    sync.check_not_behind(cfg)
    holder = lease.other_holder(pdir)
    if holder:
        raise RuntimeError(f"catalog folder is maintained by {lease.describe(holder)}")
    out = cfg.out_dir
    for name in ("catalog.html", "catalog_datasets.csv", "catalog_images.csv", "catalog_scenes.csv",
                 "catalog_files.csv", "catalog.json", "CATALOG.md", "CHANGELOG.md", "comments.csv",
                 "manual_overrides.csv"):
        src = os.path.join(out, name)
        if os.path.exists(src):
            with open(src, "rb") as fh:
                safety.publish_file(pdir, "index.html" if name == "catalog.html" else name, fh.read())
    # consistent snapshot of the live database (sqlite backup API) -> bytes -> atomic replace
    snap = os.path.join(cfg.cache_dir, "publish_snapshot.sqlite")
    safety.remove(snap)
    dst = sqlite3.connect(safety.check_writable(snap))
    db.backup(dst)
    dst.close()
    with open(snap, "rb") as fh:
        safety.publish_file(pdir, "catalog.sqlite", fh.read())
    safety.remove(snap)
    audit = os.path.join(out, "logs", "web_edits.log")
    if os.path.exists(audit):
        with open(audit, "rb") as fh:
            safety.publish_file(pdir, "logs/web_edits.log", fh.read())
    n_prev = sync.push_dir(pdir, "previews", previews.previews_dir(cfg), "previews/", remove_stale=True)
    n_meta = sum(sync.push_dir(pdir, f"metadata/{k}", os.path.join(cfg.cache_dir, k), f"metadata/{k}/",
                               remove_stale=False) for k in ("xml", "orig"))
    owner = lease.read(pdir)
    safety.publish_file(pdir, "README.txt", PUBLISH_README.format(
        port=cfg.server_port, owner=lease.describe(owner) if owner else "none").encode("utf-8"))
    gen = sync.commit_push(cfg)
    log(f"Copied catalog -> {pdir} (generation {gen}; {n_prev} new preview(s), {n_meta} metadata file(s))")


def read_session_notes(cfg, attachments: list[str]) -> dict:
    """Read readme*.txt files (read-only) so their content shows up in the reports."""
    from mdcat.derive import attachment_category, location

    notes = {}
    for rel in attachments:
        if attachment_category(rel, cfg) != "notes" or not rel.lower().endswith((".txt", ".md")):
            continue
        try:
            with open(abs_path(cfg, rel), "rb") as fh:
                raw = fh.read(4000)
        except OSError:
            continue
        for enc in ("utf-8", "cp1252"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        key = location(rel, cfg)
        notes[key] = (notes.get(key, "") + f"[{rel.rsplit('/', 1)[-1]}]\n{text.strip()}\n").strip()
    return notes


if __name__ == "__main__":
    sys.exit(main())
