"""Incremental metadata extraction.

Only sources whose change signature differs from the last run are read from the
data directory. Raw OME-XML / original-metadata dumps are cached (gzip) in the
catalog folder, so changing the derivation logic never requires re-reading data.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime

from . import safety
from .config import Config
from .javabridge import Job, run_jobs
from .omexml import ORIG_KEY_REGEX, PARSER_VERSION, needs_objective_lookup, parse_ome, parse_orig_objectives
from .scan import Source, abs_path
from .zarrinfo import inspect_zarr

SOURCES_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
    rel TEXT PRIMARY KEY,
    type TEXT, fmt TEXT,
    signature TEXT,
    status TEXT,            -- ok | error | incomplete
    error TEXT,
    parser_version INTEGER,
    summary TEXT,           -- JSON
    used_files TEXT,        -- JSON list
    size INTEGER,
    members INTEGER,
    max_mtime REAL,
    extracted_at TEXT,
    read_seconds REAL,
    present INTEGER,
    first_seen TEXT,
    last_seen TEXT
);
"""


def _key(rel: str) -> str:
    return hashlib.sha1(rel.lower().encode("utf-8")).hexdigest()[:16]


def _cache_path(cfg: Config, kind: str, rel: str, suffix: str) -> str:
    return os.path.join(cfg.cache_dir, kind, _key(rel) + suffix)


def _write_gz(path: str, data: bytes) -> None:
    with safety.open_write(path, "wb") as fh:
        fh.write(gzip.compress(data, compresslevel=6, mtime=0))


def _read_gz(path: str) -> bytes | None:
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return gzip.decompress(fh.read())


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_sources(db: sqlite3.Connection) -> dict[str, dict]:
    db.execute(SOURCES_SCHEMA)
    cur = db.execute("SELECT * FROM sources")
    cols = [c[0] for c in cur.description]
    return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


def _summarise_raw(cfg: Config, rel: str) -> dict:
    xml = _read_gz(_cache_path(cfg, "xml", rel, ".ome.xml.gz"))
    if xml is None:
        raise FileNotFoundError("cached OME-XML missing")
    summary = parse_ome(xml)
    orig = _read_gz(_cache_path(cfg, "orig", rel, ".tsv.gz"))
    if orig is not None:
        summary["orig_objectives"] = parse_orig_objectives(orig.decode("utf-8", errors="replace"), summary)
    return summary


def update_sources(cfg: Config, db: sqlite3.Connection, sources: list[Source], *, retry_errors: bool,
                   force: bool, log) -> dict:
    prev = load_sources(db)
    now = time.time()
    now_s = _now()
    stats = {"read": 0, "reparsed": 0, "cached": 0, "incomplete": 0, "errors": 0, "removed": 0}
    rows: dict[str, dict] = {}

    to_java: list[Source] = []
    to_zarr: list[Source] = []
    to_reparse: list[Source] = []
    for s in sources:
        p = prev.get(s.rel)
        base = {
            "rel": s.rel, "type": s.type, "fmt": s.fmt, "size": s.size, "members": len(s.members),
            "max_mtime": s.max_mtime, "present": 1, "last_seen": now_s,
            "first_seen": p["first_seen"] if p else now_s,
        }
        if now - s.max_mtime < cfg.recent_minutes * 60:
            row = dict(p) if p else {}
            row.update(base)
            row.update({"status": "incomplete", "signature": "",
                        "error": f"modified within the last {cfg.recent_minutes:g} min - read in a later run"})
            rows[s.rel] = row
            stats["incomplete"] += 1
            continue
        unchanged = p is not None and p["signature"] == s.signature and not force
        if unchanged and p["status"] == "ok":
            row = dict(p)
            row.update(base)
            rows[s.rel] = row
            if p["parser_version"] != PARSER_VERSION and s.type != "zarr":
                to_reparse.append(s)
            elif p["parser_version"] != PARSER_VERSION:
                to_zarr.append(s)
            else:
                stats["cached"] += 1
            continue
        if unchanged and p["status"] == "error" and not retry_errors:
            row = dict(p)
            row.update(base)
            rows[s.rel] = row
            stats["cached"] += 1
            continue
        rows[s.rel] = dict(base, signature=s.signature, status="pending", error=None, summary=None,
                           used_files=None, parser_version=PARSER_VERSION)
        (to_zarr if s.type == "zarr" else to_java).append(s)

    # --- re-parse cached XML after a parser change (no data access) ---
    for s in to_reparse:
        try:
            rows[s.rel]["summary"] = json.dumps(_summarise_raw(cfg, s.rel), sort_keys=True)
            rows[s.rel]["parser_version"] = PARSER_VERSION
            stats["reparsed"] += 1
        except Exception:
            to_java.append(s)  # cache gone -> read again

    # --- raw files and snapshots via Bio-Formats ---
    if to_java:
        log(f"Reading metadata of {len(to_java)} new/changed file(s) with Bio-Formats ...")
        jobs = [Job("ome", abs_path(cfg, s.rel), os.path.join(cfg.cache_dir, "tmp", _key(s.rel) + ".ome.xml"))
                for s in to_java]
        results = run_jobs(cfg, jobs, log, label="ome ")
        need_orig: list[Source] = []
        for s, job, res in zip(to_java, jobs, results):
            row = rows[s.rel]
            row["extracted_at"] = now_s
            row["read_seconds"] = round(res.seconds, 2)
            row["signature"] = s.signature
            if not res.ok:
                row.update(status="error", error=res.message, summary=None)
                stats["errors"] += 1
                continue
            with open(job.out, "rb") as fh:
                xml = fh.read()
            used = []
            if os.path.exists(job.out + ".used.txt"):
                with open(job.out + ".used.txt", encoding="utf-8") as fh:
                    used = [u for u in fh.read().splitlines() if u]
            _write_gz(_cache_path(cfg, "xml", s.rel, ".ome.xml.gz"), xml)
            safety.remove(job.out)
            safety.remove(job.out + ".used.txt")
            safety.remove(_cache_path(cfg, "orig", s.rel, ".tsv.gz"))
            try:
                summary = parse_ome(xml)
            except Exception as exc:
                row.update(status="error", error=f"OME-XML parse error: {exc}", summary=None)
                stats["errors"] += 1
                continue
            row.update(status="ok", error=None, summary=json.dumps(summary, sort_keys=True),
                       used_files=json.dumps(sorted(os.path.relpath(u, cfg.labdata_root).replace(os.sep, "/")
                                                    for u in used)),
                       parser_version=PARSER_VERSION)
            stats["read"] += 1
            if s.type == "raw" and needs_objective_lookup(summary):
                need_orig.append(s)

        if need_orig:
            log(f"Objective missing in standard metadata for {len(need_orig)} file(s); reading original metadata ...")
            ojobs = [Job("orig", abs_path(cfg, s.rel), os.path.join(cfg.cache_dir, "tmp", _key(s.rel) + ".orig.tsv"),
                         ORIG_KEY_REGEX) for s in need_orig]
            for s, job, res in zip(need_orig, ojobs, run_jobs(cfg, ojobs, log, label="orig ")):
                if not res.ok:
                    continue  # objective stays unknown -> needs_input later
                with open(job.out, "rb") as fh:
                    data = fh.read()
                _write_gz(_cache_path(cfg, "orig", s.rel, ".tsv.gz"), data)
                safety.remove(job.out)
                rows[s.rel]["summary"] = json.dumps(_summarise_raw(cfg, s.rel), sort_keys=True)

    # --- zarr stores via microio (read-only) ---
    if to_zarr:
        log(f"Inspecting {len(to_zarr)} new/changed zarr store(s) ...")
    for i, s in enumerate(to_zarr, 1):
        row = rows[s.rel]
        t0 = time.time()
        log(f"  zarr [{i}/{len(to_zarr)}] {os.path.basename(s.rel)}")
        try:
            summary, xml = inspect_zarr(abs_path(cfg, s.rel))
            row.update(status="ok", error=None, summary=json.dumps(summary, sort_keys=True),
                       parser_version=PARSER_VERSION, signature=s.signature)
            stats["read"] += 1
        except Exception as exc:
            row.update(status="error", error=f"{type(exc).__name__}: {exc}", summary=None, signature=s.signature)
            stats["errors"] += 1
        row["extracted_at"] = now_s
        row["read_seconds"] = round(time.time() - t0, 2)

    # --- sources that disappeared: keep, mark as not present ---
    for rel, p in prev.items():
        if rel not in rows:
            row = dict(p)
            if row.get("present"):
                stats["removed"] += 1
            row["present"] = 0
            rows[rel] = row

    cols = ["rel", "type", "fmt", "signature", "status", "error", "parser_version", "summary", "used_files", "size",
            "members", "max_mtime", "extracted_at", "read_seconds", "present", "first_seen", "last_seen"]
    with db:
        db.execute("DELETE FROM sources")
        db.executemany(
            f"INSERT INTO sources({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            [tuple(r.get(c) for c in cols) for _, r in sorted(rows.items())],
        )
    return stats
