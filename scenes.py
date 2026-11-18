"""Show the exact series ("scene") numbers of a multi-image file, grouped into catalog images.

Answers from the catalog database (instant, no file access). Output looks like
SceneViewerTool's, plus the catalog image and role (tile / merged / preview / ...)
of every series. Files that are not in the catalog, or any file with --live, are
read with SceneViewerTool (Bio-Formats, header only) and grouped the same way.

    python scenes.py QUERY [QUERY ...] [--all] [--tsv] [--live]

QUERY is a file path (absolute, or relative to the Labdata root; any copy or the zarr
of a dataset works), an acq_id, or words that must all occur in the path, e.g.
    python scenes.py LMNB1_CHIR_001
    python scenes.py "Thunder 20260115 lif"

Numbering:
    Fiji  1-based "Series N" of Fiji's Bio-Formats importer (same as SceneViewerTool)
    #     0-based Bio-Formats series index (Python readers, bioformats2raw zarr group name)

Options:
    --all   one line per series (default: consecutive identical series, e.g. tiles, share a line)
    --tsv   tab-separated, one line per series
    --live  read the file(s) now with SceneViewerTool instead of using the catalog
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mdcat.config import load_config  # noqa: E402
from mdcat.derive import SCENE_COLUMNS, build_images  # noqa: E402
from mdcat.report import collapse_scenes, fmt_duration  # noqa: E402

MAX_TABLES = 5


def rel_of(cfg, q: str) -> str | None:
    """Labdata-relative path ('/'-separated) of an absolute path inside Labdata, else None."""
    if not os.path.isabs(q):
        return None
    a = os.path.normcase(os.path.abspath(q))
    root = os.path.normcase(cfg.labdata_root)
    if a.startswith(root + os.sep):
        return os.path.abspath(q)[len(cfg.labdata_root) + 1:].replace(os.sep, "/")
    return None


def find_acqs(db, cfg, q: str) -> list[tuple[str, str]]:
    """-> [(acq_id, matched rel path)]"""
    rel = rel_of(cfg, q)
    if rel is None and not os.path.isabs(q) and ("/" in q or "\\" in q):
        rel = q.replace("\\", "/").strip("/")
    locs = db.execute("SELECT acq_id, rel FROM locations WHERE role IN ('original','duplicate','zarr')").fetchall()
    if rel is not None:
        low = rel.lower().rstrip("/")
        hits = [(a, r) for a, r in locs if r.lower() == low]
        if hits:
            return hits[:1]
    hit = db.execute("SELECT acq_id, path FROM acquisitions WHERE acq_id = ? AND present = 1", (q,)).fetchall()
    if hit:
        return hit
    words = q.lower().replace("\\", "/").split()
    seen, out = set(), []
    for a, r in locs:
        if a not in seen and all(w in r.lower() for w in words):
            seen.add(a)
            out.append((a, r))
    return out


def catalog_scenes(db, acq_id: str) -> list[dict]:
    cur = db.execute(f"SELECT {', '.join(SCENE_COLUMNS)} FROM scenes WHERE acq_id = ? ORDER BY series", (acq_id,))
    return [dict(zip(SCENE_COLUMNS, r)) for r in cur.fetchall()]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def live_scenes(cfg, path: str) -> list[dict]:
    """Read a file with SceneViewerTool (--tsv) and group its series like the catalog does."""
    if not cfg.sceneviewer or not os.path.exists(cfg.sceneviewer):
        raise RuntimeError(f"SceneViewerTool not found: {cfg.sceneviewer!r} (set 'sceneviewer' in config.toml)")
    proc = subprocess.run([cfg.sceneviewer, "--tsv", path], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"SceneViewerTool exit code {proc.returncode}")
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    head = lines[0].split("\t")
    series = []
    for ln in lines[1:]:
        r = dict(zip(head, ln.split("\t")))
        vox = [_f(v.strip()) for v in r.get("Voxel XYZ [um]", "").split(" x ")] + [None] * 3
        series.append({
            "idx": int(r["#"]), "name": r["Name"], "date": None,
            "T": int(r["T"]), "C": int(r["C"]), "Z": int(r["Z"]), "Y": int(r["Y"]), "X": int(r["X"]),
            "dtype": r["Type"], "px_x": vox[0], "px_y": vox[1], "px_z": vox[2], "time_inc": None,
            "objective": {}, "channels": [], "span_s": None, "stage_z": None, "exposure_s": {},
        })
    fmt = os.path.splitext(path)[1].lower().lstrip(".")
    out = []
    for im in build_images({"series": series}, fmt):
        for sc in im["_scenes"]:
            out.append({**sc, "acq_id": "", "file": path, "fiji": sc["series"] + 1, "image_key": im["image_key"]})
    return sorted(out, key=lambda s: s["series"])


HEADER = ["Fiji", "#", "Name", "Image", "Role", "Start", "Duration", "X", "Y", "Z", "C", "T", "Type", "Voxel XYZ [um]"]
LEFT = {"Name", "Image", "Role", "Start", "Type", "Voxel XYZ [um]"}


def _vox(s) -> str:
    return " x ".join("-" if v is None else f"{float(v):.4g}" for v in (s["px_x_um"], s["px_y_um"], s["px_z_um"]))


def print_table(title: str, scenes: list[dict], all_rows: bool) -> None:
    rows = []
    runs = ([{**s, "first": s["series"], "last": s["series"]} for s in scenes] if all_rows
            else collapse_scenes(scenes))
    for s in runs:
        rg = (lambda o: f"{s['first'] + o}" if s["first"] == s["last"] else f"{s['first'] + o}-{s['last'] + o}")
        rows.append([rg(1), rg(0), s["name"] or "", s["image_key"] or "", s["role"] or "",
                     (s.get("acquired_start") or "")[:19].replace("T", " "), fmt_duration(s.get("duration_s")),
                     *(str(s[k]) for k in ("X", "Y", "Z", "C", "T")), s["dtype"] or "", _vox(s)])
    w = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(HEADER)]
    print(title)
    for r in [HEADER] + rows:
        print("  ".join(c.ljust(w[i]) if HEADER[i] in LEFT else c.rjust(w[i]) for i, c in enumerate(r)).rstrip())
    print()


def print_tsv(scenes: list[dict], header: bool) -> None:
    cols = ["file", "fiji", "series", "name", "image_key", "role", "acquired_start", "duration_s", "time_delta_s", "X", "Y", "Z", "C", "T", "dtype",
            "px_x_um", "px_y_um", "px_z_um", "acq_id"]
    if header:
        print("\t".join(cols))
    for s in scenes:
        print("\t".join("" if s.get(c) is None else str(s.get(c)) for c in cols))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="+")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--tsv", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default=None, help="catalog working directory (default: data_dir from the config)")
    args = ap.parse_args(argv)
    cfg = load_config(args.config, args.out)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(errors="replace")

    db = None
    if os.path.exists(cfg.db_path):
        db = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='scenes'").fetchone():
            print("note: catalog has no scene table yet - run build_catalog.py once", file=sys.stderr)
            db = None

    failed, first_tsv = 0, True
    for q in args.query:
        jobs: list[tuple[str, list[dict]]] = []  # (title, scenes)
        try:
            hits = [] if (db is None or args.live) else find_acqs(db, cfg, q)
            if hits:
                if len(hits) > MAX_TABLES and not args.tsv:
                    print(f"{q!r} matches {len(hits)} datasets - be more specific:")
                    for _, r in hits[:40]:
                        print(f"  {r}")
                    print()
                    continue
                for acq_id, matched in hits:
                    sc = catalog_scenes(db, acq_id)
                    src = sc[0]["file"] if sc else matched
                    title = f"{src}  ({len(sc)} series, from catalog)"
                    if matched != src:
                        title += f"\n  queried: {matched} (identical copy/zarr -> same numbering)"
                    jobs.append((title, sc))
            else:
                path = q if os.path.isabs(q) else os.path.join(cfg.labdata_root, *q.replace("\\", "/").split("/"))
                if not os.path.exists(path):
                    print(f"{q!r}: not found in the catalog and not an existing file", file=sys.stderr)
                    failed += 1
                    continue
                sc = live_scenes(cfg, path)
                jobs.append((f"{path}  ({len(sc)} series, read with SceneViewerTool)", sc))
        except Exception as exc:  # noqa: BLE001 - report and continue with the next query
            print(f"{q!r}: {exc}", file=sys.stderr)
            failed += 1
            continue
        for title, sc in jobs:
            if args.tsv:
                print_tsv(sc, first_tsv)
                first_tsv = False
            elif not sc:
                print(f"{title}\n  no readable series (see catalog.html for the error)\n")
            else:
                print_table(title, sc, args.all)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
