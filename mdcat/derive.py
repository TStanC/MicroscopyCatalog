"""Turn extracted source summaries into the catalog (deterministic, recomputed every run).

Entities
--------
acquisition  one row per dataset: a raw file (all identical copies merged), a zarr store
             without any original, or a low-metadata snapshot (jpg/avi).
image        one row per logical image inside an acquisition. For .lif files an image
             groups all tiles (+ merged mosaic + previews) that share one series name.
location     every file/zarr belonging to an acquisition (original, duplicate, zarr, ...).
attachment   any other file (processed, segmentation, metadata, docs); linked to an
             acquisition by name where possible, otherwise listed as unassigned.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timedelta

from .config import Config
from .omexml import series_fingerprint, to_um
from .scan import Classified, basename, ext_of, norm_name, parent, stem_of

AUX_RE = re.compile(r"(?i)^(macro|label|thumbnail)( image)?$")
VSI_AUX_RE = re.compile(r"(?i)^(macro|label|thumbnail|overview)( image)?$")
OVERVIEW_RE = re.compile(r"(?i)^(sample overview|overview( \d+)?)$")
ZARR_PREFIX_RE = re.compile(r"^(copy|old|test|tmp|backup)_", re.I)
FILENAME_MAG_RE = re.compile(r"(?<![0-9a-z])(\d{1,3})x(?![a-z0-9])")
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "mrz": 3, "maer": 3, "apr": 4, "may": 5, "mai": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "okt": 10, "nov": 11, "dec": 12, "dez": 12,
}

ACQ_COLUMNS = [
    "acq_id", "status", "category", "kind", "owner", "instrument", "session", "name", "format",
    "path", "acquired_start", "acquired_end", "total_time_s", "date_source",
    "n_images", "n_overview_images", "dims_TCZYX", "max_T", "max_Z", "max_C", "n_tiles",
    "objective", "magnification", "na", "pixel_size_um", "z_step_um", "time_delta_s", "channels",
    "size_bytes",
    "has_original", "n_original_copies", "has_zarr", "n_zarr", "has_segmentation", "has_tables",
    "has_rois", "has_processed", "has_exports", "has_metadata_files",
    "segmentation_names", "table_names", "needs_input", "notes", "error",
]
IMG_COLUMNS = [
    "acq_id", "image_key", "kind", "n_series", "n_tiles", "has_merged", "n_previews", "variants",
    "series", "fiji_series", "fiji_series_detail",
    "acquired_start", "acquired_end", "total_time_s", "total_time_source",
    "T", "C", "Z", "Y", "X", "dtype", "pixel_size_x_um", "pixel_size_y_um",
    "z_step_um", "z_step_source", "time_delta_s", "time_delta_min_s", "time_delta_max_s", "time_delta_source",
    "objective", "magnification", "na", "immersion", "objective_source",
    "channels", "channel_details", "exposure_s",
    "in_zarr", "segmentation", "tables", "rois", "preview", "preview_series",
]
# One row per Bio-Formats series ("scene") of the file the image metadata was read from.
# series = 0-based index (Bio-Formats / bioformats2raw zarr group / Python readers),
# fiji = 1-based number shown by Fiji's Bio-Formats importer (same as SceneViewerTool).
SCENE_COLUMNS = [
    "acq_id", "file", "series", "fiji", "name", "image_key", "role",
    "acquired_start", "duration_s", "time_delta_s",
    "T", "C", "Z", "Y", "X", "dtype", "px_x_um", "px_y_um", "px_z_um",
]


# ----------------------------------------------------------------------------- helpers

def _epoch(d: str | None) -> float | None:
    if not d:
        return None
    try:
        return datetime.fromisoformat(d).timestamp()
    except ValueError:
        return None


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def date_from_text(text: str) -> str | None:
    """Best-effort date from a file or folder name (YYYYMMDD, YYMMDD_, 26Okt_2025, ...)."""
    t = text.lower()
    for m in re.finditer(r"(?<!\d)(20\d{2})[-_]?(\d{2})[-_]?(\d{2})(?!\d)", t):
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date().isoformat()
        except ValueError:
            pass
    m = re.search(r"(?<!\d)(\d{1,2})[ _-]?([a-z]{3,4})[a-z]*[ _-]?(20\d{2})(?!\d)", t)
    if m and m.group(2)[:3] in MONTHS or (m and m.group(2) in MONTHS):
        mon = MONTHS.get(m.group(2)) or MONTHS.get(m.group(2)[:3])
        try:
            return datetime(int(m.group(3)), mon, int(m.group(1))).date().isoformat()
        except (ValueError, TypeError):
            pass
    m = re.match(r"^(\d{2})(\d{2})(\d{2})(?:_|$)", t)
    if m:
        try:
            return datetime(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))).date().isoformat()
        except ValueError:
            pass
    return None


def location(rel: str, cfg: Config) -> tuple[str, str]:
    role_dirs = {d.lower() for d in cfg.processed_dirs + cfg.metadata_dirs + cfg.converted_dirs}
    role_dirs.add("originaldata")
    parts = rel.split("/")
    instrument = parts[0]
    session = parts[1] if len(parts) > 2 and parts[1].lower() not in role_dirs else "(root)"
    return instrument, session


def series_kind(s: dict, fmt: str) -> str:
    name = (s.get("name") or "").strip()
    if s["X"] <= 1 or s["Y"] <= 1 or "climatedata" in name.lower().replace(" ", ""):
        return "aux"
    if (VSI_AUX_RE if fmt == "vsi" else AUX_RE).match(name):
        return "aux"
    return "main"


def image_key(name: str) -> tuple[str, bool]:
    n = re.sub(r"\s+#\d+$", "", (name or "").strip())
    n = re.sub(r"\s+Resolution Level \d+$", "", n, flags=re.I)  # Imaris pyramid levels
    m = re.match(r"^(.*?)/Preview \d+", n, re.I)
    if m:
        return m.group(1), True
    prev = None
    while prev != n:  # strip "_Merged" and Leica computational-clearing variants ("_ICC", "_LVCC", ...)
        prev = n
        n = re.sub(r"_(Merged|ICC|LVCC|SVCC)$", "", n)
    return n, False


VARIANT_RE = re.compile(r"_(ICC|LVCC|SVCC)$")


def variant_of(name: str) -> str | None:
    m = VARIANT_RE.search(re.sub(r"\s+#\d+$", "", (name or "").strip()))
    return m.group(1) if m else None


def _uniq(values) -> list:
    out = []
    for v in values:
        if v is not None and v != "" and v not in out:
            out.append(v)
    return out


def fmt_num(v: float | None, nd: int = 4) -> str:
    if v is None:
        return ""
    s = f"{v:.{nd}f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _summary_text(values: list, nd: int = 4) -> str:
    vals = _uniq(values)
    return "; ".join(fmt_num(v, nd) if isinstance(v, float) else str(v) for v in sorted(vals, key=str))


def idx_ranges(idxs, offset: int = 0) -> str:
    """[0,1,2,5] -> '0-2, 5' (offset=1 gives Fiji's 1-based numbering)."""
    out, run = [], []
    for i in sorted(set(idxs)):
        if run and i == run[-1] + 1:
            run.append(i)
            continue
        if run:
            out.append(run)
        run = [i]
    if run:
        out.append(run)
    return ", ".join(f"{r[0] + offset}" if len(r) == 1 else f"{r[0] + offset}-{r[-1] + offset}" for r in out)


ROLE_PLURAL = {"tile": "tiles", "preview": "previews", "aux": "aux", "other": "other"}


def role_ranges(roles, offset: int = 0) -> str:
    """[(series, role), ...] -> 'tiles 1-100; merged 101; previews 102-103'."""
    by_role: "OrderedDict[str, list[int]]" = OrderedDict()
    for s, role in roles:
        by_role.setdefault(role, []).append(s["idx"])
    if len(by_role) == 1 and next(iter(by_role)) in ("image", "aux"):
        return idx_ranges(next(iter(by_role.values())), offset)
    return "; ".join(f"{ROLE_PLURAL.get(r, r) if len(v) > 1 else r} {idx_ranges(v, offset)}" for r, v in by_role.items())


# ----------------------------------------------------------------------------- images

def build_images(summary: dict, fmt: str) -> list[dict]:
    """Group the series of one file into logical images."""
    series = summary.get("series", [])
    orig = summary.get("orig_objectives", {})
    groups: "OrderedDict[str, dict]" = OrderedDict()
    for s in series:
        kind = series_kind(s, fmt)
        key, is_preview = image_key(s["name"])
        if kind == "aux":
            key = f"[aux] {key}"
        g = groups.setdefault(key, {"members": [], "previews": [], "variants": [], "aux": kind == "aux"})
        if is_preview:
            g["previews"].append(s)
        elif variant_of(s["name"]):
            g["variants"].append(s)
        else:
            g["members"].append(s)

    images = []
    for key, g in groups.items():
        members, previews, variants = g["members"], g["previews"], g["variants"]
        if g["aux"]:
            kind = "aux"
        elif OVERVIEW_RE.match(key) or not (members or variants):
            kind = "overview"
        else:
            kind = "main"
        pool = members or variants or previews
        allser = pool + [s for s in variants if s not in pool]
        merged = [s for s in pool if (s["name"] or "").strip().endswith("_Merged")]
        if merged:
            rep = merged[0]
            tiles = [s for s in pool if s not in merged]
        else:
            # most frequent shape (tiles); ties -> largest plane (e.g. pyramid levels exposed as series)
            shapes = Counter((s["T"], s["C"], s["Z"], s["Y"], s["X"]) for s in pool)
            top = sorted(shapes.items(), key=lambda kv: (-kv[1], -kv[0][3] * kv[0][4], kv[0]))[0][0]
            rep = next(s for s in pool if (s["T"], s["C"], s["Z"], s["Y"], s["X"]) == top)
            tiles = [s for s in pool if (s["Y"], s["X"]) == (rep["Y"], rep["X"])]

        starts = [_epoch(s["date"]) for s in pool if s.get("date")]
        starts = [x for x in starts if x is not None]
        ends = [_epoch(s["date"]) + (s.get("span_s") or 0.0) for s in pool if _epoch(s.get("date")) is not None]
        start = min(starts) if starts else None
        end = max(ends) if ends else None

        # objective
        ob = rep.get("objective") or {}
        obj_src = ob.get("mag_src")
        if not ob.get("mag"):
            for s in pool:
                if (s.get("objective") or {}).get("mag"):
                    ob, obj_src = s["objective"], s["objective"].get("mag_src")
                    break
        if not ob.get("mag") or not ob.get("na"):
            for s in [rep] + allser:
                o2 = orig.get(str(s["idx"]))
                if o2 and (o2.get("mag") or o2.get("na")):
                    ob = {"model": ob.get("model") or o2.get("model"), "mag": ob.get("mag") or o2.get("mag"),
                          "na": ob.get("na") or o2.get("na"), "immersion": ob.get("immersion") or o2.get("immersion")}
                    obj_src = obj_src or "original_metadata"
                    break

        # z step
        z_step = z_src = None
        if rep["Z"] > 1:
            if rep.get("px_z"):
                z_step, z_src = rep["px_z"], "metadata"
            else:
                st = [s["stage_z"] for s in pool if s.get("stage_z")]
                if st:
                    z_step, z_src = statistics.median(st), "stage_positions"

        # time delta
        dt = dt_min = dt_max = dt_src = None
        if rep["T"] > 1:
            meds = [s["dt_med"] for s in pool if s.get("dt_med")]
            if meds:
                dt, dt_src = statistics.median(meds), "plane_timestamps"
                dt_min = min(s["dt_min"] for s in pool if s.get("dt_min") is not None)
                dt_max = max(s["dt_max"] for s in pool if s.get("dt_max") is not None)
            elif rep.get("time_inc"):
                dt, dt_src = rep["time_inc"], "time_increment"

        total = total_src = None
        if start is not None and end is not None:
            total, total_src = end - start, "timestamps"
        elif any(s.get("span_s") for s in pool):
            total, total_src = max(s.get("span_s") or 0 for s in pool), "plane_timestamps"
        elif dt and rep["T"] > 1:
            total, total_src = dt * (rep["T"] - 1), "derived_T_x_dt"

        # role of every series of this image (for the scene table / exact series numbers)
        merged_ids, tile_ids = {id(s) for s in merged}, {id(s) for s in tiles}
        roles = []
        preview_ids = {id(s) for s in previews}
        for s in pool:
            if g["aux"]:
                role = "aux"
            elif id(s) in preview_ids:  # image consisting only of previews (e.g. Leica "Overview")
                role = "preview"
            elif id(s) in merged_ids:
                role = "merged"
            elif id(s) in tile_ids:
                role = "tile" if len(tiles) > 1 else "image"
            else:
                role = "other"
            roles.append((s, role))
        roles += [(s, f"variant {variant_of(s['name'])}") for s in variants if s not in pool]
        roles += [(s, "preview") for s in previews if s not in pool]
        roles.sort(key=lambda sr: sr[0]["idx"])

        chans = rep.get("channels") or []
        ch_names =[c.get("name") or c.get("fluor") or f"C{i}" for i, c in enumerate(chans)]
        details = []
        for name, c in zip(ch_names, chans):
            extra = []
            if c.get("ex_nm"):
                extra.append(f"ex {fmt_num(c['ex_nm'], 0)}")
            if c.get("em_nm"):
                extra.append(f"em {fmt_num(c['em_nm'], 0)}")
            if c.get("spp", 1) > 1:
                extra.append(f"{c['spp']} samples (RGB)")
            details.append(f"{name} ({', '.join(extra)})" if extra else name)

        images.append({
            "image_key": key, "kind": kind, "n_series": len(pool), "n_tiles": len(tiles),
            "has_merged": bool(merged), "n_previews": len(previews) if (members or variants) else 0,
            "variants": ", ".join(f"{v} ({n})" for v, n in sorted(Counter(variant_of(s["name"]) for s in variants).items()))
            if members else "",
            "acquired_start": _iso(start), "acquired_end": _iso(end),
            "total_time_s": round(total, 3) if total is not None else None, "total_time_source": total_src,
            "T": rep["T"], "C": rep["C"], "Z": rep["Z"], "Y": rep["Y"], "X": rep["X"], "dtype": rep.get("dtype"),
            "pixel_size_x_um": rep.get("px_x"), "pixel_size_y_um": rep.get("px_y"),
            "z_step_um": z_step, "z_step_source": z_src,
            "time_delta_s": dt, "time_delta_min_s": dt_min, "time_delta_max_s": dt_max, "time_delta_source": dt_src,
            "objective": ob.get("model"), "magnification": ob.get("mag"), "na": ob.get("na"),
            "immersion": ob.get("immersion"), "objective_source": obj_src if ob.get("mag") or ob.get("na") else None,
            "channels": ", ".join(ch_names), "channel_details": "; ".join(details),
            "exposure_s": json.dumps(rep.get("exposure_s") or {}, sort_keys=True),
            "in_zarr": False, "segmentation": "", "tables": "", "rois": "",
            "series": idx_ranges([s["idx"] for s, _ in roles]),
            "fiji_series": idx_ranges([s["idx"] for s, _ in roles], 1),
            "fiji_series_detail": role_ranges(roles, 1),
            "_scenes": [{"series": s["idx"], "name": s["name"], "T": s["T"], "C": s["C"], "Z": s["Z"], "Y": s["Y"],
                         "X": s["X"], "dtype": s.get("dtype"), "px_x_um": s.get("px_x"), "px_y_um": s.get("px_y"),
                         "px_z_um": s.get("px_z"), "role": role, "acquired_start": s.get("date"),
                         "duration_s": s.get("span_s"), "time_delta_s": s.get("dt_med")} for s, role in roles],
            "_fps": sorted({fp for fp in (series_fingerprint(s) for s in allser + previews) if fp}),
            "_series_idx": [s["idx"] for s in allser],
        })
    if fmt == "vsi" and images and all(im["kind"] == "aux" for im in images):
        # Only Olympus' embedded preview is readable (pixel-data folder _name_ missing):
        # report its dimensions, flagged, instead of an empty entry.
        big = max(images, key=lambda im: (im["Y"] * im["X"], im["image_key"]))
        big["kind"] = "main"
        big["_embedded_preview"] = True
    return images


def fingerprint_set(summary: dict | None, fmt: str) -> frozenset:
    if not summary:
        return frozenset()
    fps = set()
    for s in summary.get("series", []):
        if series_kind(s, fmt) == "aux":
            continue
        fp = series_fingerprint(s)
        if fp:
            fps.add(fp)
    return frozenset(fps)


# ----------------------------------------------------------------------------- attachments

def attachment_category(rel: str, cfg: Config, raw_stems_by_dir: dict | None = None) -> str:
    low = rel.lower()
    name = basename(low)
    e = ext_of(rel)
    parts = low.split("/")[:-1]
    if name.startswith("readme"):
        return "notes"
    if e == ".ps1" or name.startswith(("conversion_log", "conversionnames", "repair_log", "convert")):
        return "conversion"
    if e in (".csv", ".zip", ".tif", ".tiff", ".npy", ".npz", ".h5", ".roi", ".json", ".pkl") and \
            any(p in name for p in cfg.segmentation_patterns):
        return "segmentation"
    if any(p in {d.lower() for d in cfg.metadata_dirs} for p in parts):
        return "metadata"
    if e == ".oex":
        return "experiment_file"
    if any(p in {d.lower() for d in cfg.processed_dirs} for p in parts):
        return "processed"
    if e in set(cfg.snapshot_exts) | {".tif", ".tiff"} and raw_stems_by_dir is not None and \
            norm_name(stem_of(rel)) in raw_stems_by_dir.get(parent(rel), set()):
        return "export"  # same name as a raw file next to it, e.g. Snap_553.tif next to Snap_553.czi
    return "processed"


# ----------------------------------------------------------------------------- main

def derive(cfg: Config, classified: Classified, source_rows: dict[str, dict], files: dict,
           overrides: list[dict], prev_acq: dict[str, dict], prev_img: dict[str, list[dict]], run_ts: str) -> dict:
    src = {}
    for rel, r in source_rows.items():
        if not r.get("present"):
            continue
        r = dict(r)
        r["summary_obj"] = json.loads(r["summary"]) if r.get("summary") else None
        src[rel] = r

    raw = [r for r in src.values() if r["type"] == "raw"]
    snaps = [r for r in src.values() if r["type"] == "snapshot"]
    zarrs = [r for r in src.values() if r["type"] == "zarr"]

    def canon_key(r):
        fmt_rank = 1 if r["fmt"] == "xlef" else 0
        return (0 if r["status"] == "ok" else 1, 0 if r.get("_fps") else 1,
                0 if "/originaldata/" in "/" + r["rel"].lower() else 1,
                fmt_rank, len(r["rel"]), r["rel"].lower())

    # --- group identical raw copies ---
    for r in raw:
        r["_fps"] = fingerprint_set(r["summary_obj"], r["fmt"])
    dated_sibling = {}
    for r in sorted(raw, key=lambda r: r["rel"].lower()):
        if r["_fps"]:
            dated_sibling.setdefault((parent(r["rel"]), norm_name(stem_of(r["rel"]))), r["_fps"])
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in sorted(raw, key=lambda r: r["rel"].lower()):
        fps = r["_fps"]
        main_size = files.get(r["rel"], (0, 0))[0]
        sibling = None
        if not fps:
            # e.g. an Imaris .ims converted from the .vsi next to it, or an unreadable Leica .xlef
            # project inside the folder of its .lif: same name, no acquisition date of its own.
            parts = parent(r["rel"]).split("/")
            for i in range(len(parts), 0, -1):
                sibling = dated_sibling.get(("/".join(parts[:i]), norm_name(stem_of(r["rel"]))))
                if sibling:
                    break
        if fps:
            key = ("fp", fps)
        elif sibling:
            key = ("fp", sibling)
        else:
            key = ("name", norm_name(stem_of(r["rel"]).replace(" - Copy", "")), main_size)
        groups[key].append(r)
    for r in snaps:
        groups[("snap", norm_name(stem_of(r["rel"])), files.get(r["rel"], (0, 0))[0])].append(r)

    acqs: list[dict] = []
    for key, members in groups.items():
        members.sort(key=canon_key)
        canon = members[0]
        acqs.append({"category": "snapshot" if key[0] == "snap" else "raw", "originals": members,
                     "canon": canon, "zarrs": [], "fps": canon.get("_fps") or frozenset()})

    # --- link zarr stores ---
    def zarr_stem(rel):
        return norm_name(ZARR_PREFIX_RE.sub("", stem_of(rel)))

    raw_acqs = [a for a in acqs if a["category"] == "raw"]
    zarr_only: dict[tuple, dict] = {}
    for z in sorted(zarrs, key=lambda r: r["rel"].lower()):
        zfps = fingerprint_set(z["summary_obj"], "zarr")
        z["_fps"] = zfps
        inst, sess = location(z["rel"], cfg)
        best, best_score = None, 0.0
        if zfps:
            for a in raw_acqs:
                if not a["fps"]:
                    continue
                score = len(zfps & a["fps"]) / len(zfps)
                if score > best_score + 1e-9:
                    best, best_score = a, score
            if best_score < 0.5:
                best = None
        if best is None:
            zs = zarr_stem(z["rel"])
            cands = []
            for a in raw_acqs:
                for o in a["originals"]:
                    if location(o["rel"], cfg)[0] != inst:
                        continue
                    os_ = norm_name(stem_of(o["rel"]))
                    if zs == os_ or zs.endswith("_" + os_):
                        same_sess = location(o["rel"], cfg)[1] == sess
                        cands.append((-len(os_), 0 if same_sess else 1, o["rel"].lower(), id(a), a))
            if cands:
                cands.sort(key=lambda c: c[:3])
                best = cands[0][4]
        if best is not None:
            best["zarrs"].append(z)
        else:
            key = ("zfp", zfps) if zfps else ("zrel", z["rel"])
            if key not in zarr_only:
                zarr_only[key] = {"category": "zarr_only", "originals": [], "canon": z, "zarrs": [], "fps": zfps}
                acqs.append(zarr_only[key])
            zarr_only[key]["zarrs"].append(z)

    # --- ids, names, locations ---
    for a in acqs:
        a["zarrs"].sort(key=lambda z: (1 if ZARR_PREFIX_RE.match(basename(z["rel"])) else 0, len(z["rel"]), z["rel"].lower()))
        if a["category"] == "zarr_only":
            a["canon"] = a["zarrs"][0]
        if a["category"] == "snapshot":
            a["id"] = "S" + _hash("snap:" + a["canon"]["rel"].lower())
        elif a["fps"]:
            a["id"] = "A" + _hash("|".join(sorted(a["fps"])))
        else:
            a["id"] = "A" + _hash("rel:" + a["canon"]["rel"].lower())
    seen_ids: Counter = Counter()
    for a in sorted(acqs, key=lambda a: a["canon"]["rel"].lower()):
        seen_ids[a["id"]] += 1
        if seen_ids[a["id"]] > 1:
            a["id"] = f"{a['id']}-{seen_ids[a['id']]}"

    # --- attachments ---
    name_index: list[tuple[str, str, str, dict, str]] = []  # (norm name, instrument, session, acq, folder)
    for a in acqs:
        names = {(norm_name(stem_of(o["rel"])), *location(o["rel"], cfg), parent(o["rel"])) for o in a["originals"]}
        for z in a["zarrs"]:
            names.add((zarr_stem(z["rel"]), *location(z["rel"], cfg), parent(z["rel"])))
            names.add((norm_name(stem_of(z["rel"])), *location(z["rel"], cfg), parent(z["rel"])))
        for n, inst, sess, folder in sorted(names):
            if len(n) >= 4:
                name_index.append((n, inst, sess, a, folder))
    by_inst: dict[str, list] = defaultdict(list)
    for entry in name_index:
        by_inst[entry[1]].append(entry)

    manual_links = {o["value"].replace("\\", "/"): o["acq_id"] for o in overrides if o["field"] == "link_attachment"}
    acq_by_id = {a["id"]: a for a in acqs}
    raw_stems_by_dir: dict[str, set] = defaultdict(set)
    for r in raw:
        raw_stems_by_dir[parent(r["rel"])].add(norm_name(stem_of(r["rel"])))
    attachments = []
    for rel in classified.attachments:
        cat = attachment_category(rel, cfg, raw_stems_by_dir)
        inst, sess = location(rel, cfg)
        target = None
        if rel in manual_links and manual_links[rel] in acq_by_id:
            target = acq_by_id[manual_links[rel]]
        elif cat not in ("notes", "conversion"):
            hay = norm_name(basename(rel).rsplit(".", 1)[0])
            best_len, hits = 0, []
            for n, i2, s2, a, folder in by_inst.get(inst, []):
                if hay == n or re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![0-9])", hay):
                    if len(n) > best_len:
                        best_len, hits = len(n), [(s2, a, folder)]
                    elif len(n) == best_len:
                        hits.append((s2, a, folder))
            # unique match wins; otherwise prefer the one in the same folder, then the same session
            for pick in (lambda h: True, lambda h: h[2] == parent(rel), lambda h: h[0] == sess):
                uniq = {id(h[1]): h[1] for h in hits if pick(h)}
                if len(uniq) == 1:
                    target = next(iter(uniq.values()))
                    break
        attachments.append({"rel": rel, "category": cat, "instrument": inst, "session": sess,
                            "acq_id": target["id"] if target else None,
                            "size": files.get(rel, (0, 0))[0]})
        if target is not None:
            target.setdefault("attachments", []).append(attachments[-1])

    # --- overrides by acquisition ---
    ov_by_acq: dict[str, list[dict]] = defaultdict(list)
    for o in overrides:
        if o["field"] != "link_attachment":
            ov_by_acq[o["acq_id"]].append(o)

    acq_rows, img_rows, loc_rows, needs, scene_rows = [], [], [], [], []
    for a in sorted(acqs, key=lambda a: a["canon"]["rel"].lower()):
        row, imgs, locs, need, scenes = _compose(cfg, a, files, ov_by_acq.get(a["id"], []))
        acq_rows.append(row)
        img_rows += imgs
        loc_rows += locs
        needs += need
        scene_rows += scenes

    # --- bookkeeping: first_seen / last_changed / missing ---
    now_ids = {r["acq_id"] for r in acq_rows}
    for r in acq_rows:
        h = _hash(json.dumps({k: r[k] for k in ACQ_COLUMNS}, sort_keys=True, default=str))
        p = prev_acq.get(r["acq_id"])
        r["content_hash"] = h
        r["first_seen"] = p["first_seen"] if p else run_ts
        r["last_changed"] = p["last_changed"] if p and p.get("content_hash") == h else run_ts
        r["present"] = 1
        r["change"] = "new" if not p else ("changed" if p.get("content_hash") != h or not p.get("present") else "")
    for aid, p in sorted(prev_acq.items()):
        if aid in now_ids:
            continue
        r = dict(p)
        r["change"] = "removed" if p.get("present") else ""
        if p.get("present"):
            r["last_changed"] = run_ts
        r["present"] = 0
        r["status"] = "missing"
        acq_rows.append(r)
        img_rows += prev_img.get(aid, [])

    return {"acquisitions": acq_rows, "images": img_rows, "locations": loc_rows,
            "attachments": attachments, "needs": needs, "scenes": scene_rows}


def _compose(cfg: Config, a: dict, files: dict, ovs: list[dict]):
    canon = a["canon"]
    cat = a["category"]
    inst, sess = location(canon["rel"], cfg)
    notes: list[str] = []
    fmt = canon["fmt"]

    # Which summary describes the images? Original if readable, else the first readable zarr.
    summary, summary_fmt, meta_from = None, fmt, None
    if canon["status"] == "ok" and canon.get("summary_obj"):
        summary, meta_from = canon["summary_obj"], canon["rel"]
    else:
        for o in a["originals"][1:]:
            if o["status"] == "ok" and o.get("summary_obj"):
                summary, summary_fmt, meta_from = o["summary_obj"], o["fmt"], o["rel"]
                notes.append("metadata read from a duplicate copy")
                break
        if summary is None:
            for z in a["zarrs"]:
                if z["status"] == "ok" and z.get("summary_obj") and z["summary_obj"].get("series"):
                    summary, summary_fmt, meta_from = z["summary_obj"], "zarr", z["rel"]
                    if cat != "zarr_only":
                        notes.append("original unreadable - metadata taken from zarr")
                    break
    images = build_images(summary, summary_fmt) if summary else []
    if any(im.get("_embedded_preview") for im in images):
        # The original only holds a preview; a zarr converted earlier may still hold the real series.
        for z in a["zarrs"]:
            zs = z.get("summary_obj") or {}
            if z["status"] == "ok" and any(series_kind(s, "vsi") == "main" for s in zs.get("series", [])):
                images = build_images(zs, "vsi")
                meta_from = z["rel"]
                notes.append("pixel-data folder of the original missing - metadata taken from zarr")
                break

    # --- zarr enrichment (labels/tables/rois/repaired axes) per image ---
    any_labels, any_tables, any_rois = set(), set(), set()
    for z in a["zarrs"]:
        zs = z.get("summary_obj") or {}
        series_key = {s["idx"]: image_key(s["name"])[0] for s in zs.get("series", [])}
        zfp_by_idx = {s["idx"]: series_fingerprint(s) for s in zs.get("series", [])}
        for sc in zs.get("scenes", []):
            any_labels.update(sc.get("labels", []))
            any_tables.update(sc.get("tables", []))
            any_rois.update(sc.get("rois", []))
            idx = sc.get("ome_index")
            key = series_key.get(idx) if idx is not None else image_key(sc.get("name") or "")[0]
            fp = zfp_by_idx.get(idx) if idx is not None else None
            for im in images:
                if (fp and fp in im["_fps"]) or (not im["_fps"] and key == im["image_key"]) or \
                        (fp is None and key == im["image_key"]):
                    im["in_zarr"] = True
                    im["_labels"] = sorted(set(im.get("_labels", [])) | set(sc.get("labels", [])))
                    im["_tables"] = sorted(set(im.get("_tables", [])) | set(sc.get("tables", [])))
                    im["_rois"] = sorted(set(im.get("_rois", [])) | set(sc.get("rois", [])))
                    rep = sc.get("repaired") or {}
                    if im["Z"] > 1 and im["z_step_um"] is None and rep.get("z", {}).get("value"):
                        v = to_um(rep["z"]["value"], {"micrometer": "µm"}.get(rep["z"].get("unit"), rep["z"].get("unit")))
                        if v:
                            im["z_step_um"], im["z_step_source"] = round(v, 4), "microio_repair"
                    if im["T"] > 1 and im["time_delta_s"] is None and rep.get("t", {}).get("value"):
                        unit = {"second": "s", "millisecond": "ms", "minute": "min"}.get(rep["t"].get("unit"), rep["t"].get("unit"))
                        from .omexml import to_s
                        v = to_s(rep["t"]["value"], unit)
                        if v:
                            im["time_delta_s"], im["time_delta_source"] = v, "microio_repair"
                    break
        if a["zarrs"] and len(images) == 1 and not images[0]["in_zarr"] and zs.get("scenes"):
            images[0]["in_zarr"] = True
    for im in images:
        im["segmentation"] = ", ".join(im.pop("_labels", []))
        im["tables"] = ", ".join(im.pop("_tables", []))
        im["rois"] = ", ".join(im.pop("_rois", []))

    # --- fallbacks: magnification from file name, date from names ---
    stem = stem_of(canon["rel"])
    fn_mag = FILENAME_MAG_RE.search(stem.lower())
    for im in images:
        if im["kind"] != "aux" and not im["magnification"] and fn_mag:
            im["magnification"], im["objective_source"] = float(fn_mag.group(1)), "filename"
    name_date = date_from_text(stem) or date_from_text(sess) or None
    date_source = "metadata"

    # --- manual overrides ---
    excluded = False
    owner = "other" if any(p.lower() in {n.lower() for n in cfg.other_owner_names} for p in canon["rel"].split("/")) else "self"
    name = stem
    manual_notes = []
    for o in ovs:
        f, v, key = o["field"], o["parsed"], o.get("image_key") or "*"
        if f == "exclude":
            excluded = bool(v)
        elif f == "owner":
            owner = v
        elif f == "notes":
            manual_notes.append(v)
        elif f == "name":
            name = v
        else:
            targets = [im for im in images if key in ("*", im["image_key"]) and (key != "*" or im["kind"] != "aux")]
            if not targets and key == "*" and not images:
                images.append(_empty_image())
                targets = images
            for im in targets:
                _apply_image_override(im, f, v)

    # --- needs input ---
    need = []
    status = "ok"
    error = None
    readable = bool(images)
    embedded_only = any(im.get("_embedded_preview") for im in images)
    if canon.get("status") == "incomplete":
        status, error = "incomplete", canon.get("error")
    elif embedded_only:
        status = "partial"
        notes.append("only the embedded preview image is readable - pixel-data folder (_name_) missing")
    elif not readable and canon.get("status") == "error" and cat == "snapshot":
        notes.append(f"dimensions not readable ({(canon.get('error') or '')[:120]})")
    elif not readable and canon.get("status") == "error":
        status, error = "unreadable", canon.get("error")
    elif not readable and cat == "zarr_only":
        status, error = "unreadable", canon.get("error") or "zarr has no readable scenes"

    main_imgs = [im for im in images if im["kind"] == "main"] or [im for im in images if im["kind"] == "overview"]
    has_meta_date = any(im["acquired_start"] for im in main_imgs)
    acquired_start = min((im["acquired_start"] for im in main_imgs if im["acquired_start"]), default=None)
    acquired_end = max((im["acquired_end"] for im in main_imgs if im["acquired_end"]), default=None)
    if any(im.get("_manual_start") for im in main_imgs):
        date_source = "manual"
    if not has_meta_date:
        if name_date:
            acquired_start, date_source = name_date, "filename"
        elif cat == "snapshot":
            mt = files.get(canon["rel"], (0, 0.0))[1]
            acquired_start, date_source = _iso(mt), "file_mtime"
        else:
            date_source = None

    def add_need(image_key_, field, hint):
        need.append({"acq_id": a["id"], "image_key": image_key_, "field": field, "value": "",
                     "instrument": inst, "session": sess, "name": stem, "path": canon["rel"], "hint": hint})

    ask = not excluded and cat != "snapshot" and owner == "self"  # do not ask for other people's data
    if ask and status in ("ok",):
        if not acquired_start:
            add_need("*", "acquired_start", "YYYY-MM-DD [HH:MM]")
        for im in main_imgs:
            if not im["magnification"]:
                add_need(im["image_key"], "magnification", "e.g. 60")
            if not im["na"]:
                add_need(im["image_key"], "na", "e.g. 1.3")
            if not im["pixel_size_x_um"]:
                add_need(im["image_key"], "pixel_size_um", "µm per pixel")
            if im["Z"] > 1 and not im["z_step_um"]:
                add_need(im["image_key"], "z_step_um", "µm between planes")
            if im["T"] > 1 and not im["time_delta_s"]:
                add_need(im["image_key"], "time_delta_s", "e.g. 300, 5min, 00:05:00")
    elif ask and status == "unreadable":
        if not acquired_start:
            add_need("*", "acquired_start", f"file unreadable ({(error or '')[:80]}); or set exclude=true")
        add_need("*", "dims", "T,C,Z,Y,X  (file unreadable)")
        add_need("*", "magnification", "e.g. 60  (file unreadable)")
    if need and status == "ok":
        status = "needs_input"
    if excluded:
        status = "excluded"

    # --- totals ---
    total = None
    if acquired_start and acquired_end and "T" in acquired_start:
        total = _epoch(acquired_end) - _epoch(acquired_start)
    elif len(main_imgs) == 1:
        total = main_imgs[0]["total_time_s"]

    # --- locations ---
    locs = []
    for i, o in enumerate(a["originals"]):
        locs.append({"acq_id": a["id"], "rel": o["rel"], "role": "original" if i == 0 else "duplicate",
                     "format": o["fmt"], "size_bytes": o.get("size"), "status": o["status"], "error": o.get("error")})
    for z in a["zarrs"]:
        locs.append({"acq_id": a["id"], "rel": z["rel"], "role": "zarr", "format": "zarr", "size_bytes": None,
                     "status": z["status"], "error": z.get("error")})
    att = a.get("attachments", [])
    for x in att:
        locs.append({"acq_id": a["id"], "rel": x["rel"], "role": x["category"], "format": ext_of(x["rel"]).lstrip("."),
                     "size_bytes": x["size"], "status": "", "error": None})

    # --- automatic notes ---
    if len(a["originals"]) > 1:
        notes.append(f"{len(a['originals']) - 1} duplicate copy/copies of the original")
    if fmt == "vsi" and canon.get("members", 1) == 1 and cat == "raw" and not embedded_only:
        notes.append("vsi without pixel-data folder (_name_)")
    for z in a["zarrs"]:
        if z["status"] == "error":
            notes.append(f"zarr unreadable: {basename(z['rel'])} ({(z.get('error') or '')[:100]})")
        elif any("microio could not open" in w for w in (z.get("summary_obj") or {}).get("warnings", [])):
            notes.append(f"zarr not readable with microio (non-standard/incomplete layout): {basename(z['rel'])}")
        elif z["status"] == "incomplete":
            notes.append(f"zarr being written: {basename(z['rel'])}")
    if owner != "self":
        notes.append(f"owner: {owner}")
    notes += manual_notes

    seg_att = [x for x in att if x["category"] == "segmentation"]
    seg_names = sorted(any_labels) + [basename(x["rel"]) for x in seg_att]
    real_tables = sorted(t for t in any_tables if t != "axes_trajectory")  # microio's repair table is always there
    multi = len(main_imgs) != 1

    def agg(field, nd=4):
        return _summary_text([im[field] for im in main_imgs], nd)

    row = {
        "acq_id": a["id"], "status": status, "category": cat,
        "kind": "overview" if "overview" in stem.lower() else ("timelapse" if any(im["T"] > 1 for im in main_imgs) else "static"),
        "owner": owner, "instrument": inst, "session": sess, "name": name, "format": fmt,
        "path": canon["rel"], "acquired_start": acquired_start, "acquired_end": acquired_end,
        "total_time_s": round(total, 3) if total is not None else None, "date_source": date_source,
        "n_images": len([im for im in images if im["kind"] == "main"]),
        "n_overview_images": len([im for im in images if im["kind"] == "overview"]),
        "dims_TCZYX": (f"{main_imgs[0]['T']}x{main_imgs[0]['C']}x{main_imgs[0]['Z']}x{main_imgs[0]['Y']}x{main_imgs[0]['X']}"
                       if not multi else (f"{len(main_imgs)} images" if main_imgs else "")),
        "max_T": max((im["T"] for im in main_imgs), default=None),
        "max_Z": max((im["Z"] for im in main_imgs), default=None),
        "max_C": max((im["C"] for im in main_imgs), default=None),
        "n_tiles": sum(im["n_tiles"] for im in main_imgs) if main_imgs else None,
        "objective": agg("objective"), "magnification": agg("magnification", 1), "na": agg("na", 3),
        "pixel_size_um": agg("pixel_size_x_um"), "z_step_um": agg("z_step_um"), "time_delta_s": agg("time_delta_s", 2),
        "channels": agg("channels"),
        "size_bytes": sum(o.get("size") or 0 for o in a["originals"][:1]) or None,
        "has_original": bool(a["originals"]) and cat != "snapshot",
        "n_original_copies": len(a["originals"]) if cat != "snapshot" else 0,
        "has_zarr": bool(a["zarrs"]), "n_zarr": len(a["zarrs"]),
        "has_segmentation": bool(seg_names), "has_tables": bool(real_tables),
        "has_rois": bool(any_rois) or any("roi" in basename(x["rel"]).lower() for x in seg_att),
        "has_processed": any(x["category"] == "processed" for x in att),
        "has_exports": any(x["category"] == "export" for x in att),
        "has_metadata_files": any(x["category"] in ("metadata", "experiment_file") for x in att),
        "segmentation_names": ", ".join(seg_names), "table_names": ", ".join(real_tables),
        "needs_input": ", ".join(sorted({n["field"] for n in need})),
        "notes": "; ".join(notes), "error": error,
    }
    img_rows = []
    for im in images:
        r = {k: im.get(k) for k in IMG_COLUMNS}
        r["acq_id"] = a["id"]
        img_rows.append(r)
    scene_rows = []
    for im in images:
        for sc in im.get("_scenes", []):
            scene_rows.append({**sc, "acq_id": a["id"], "file": meta_from, "fiji": sc["series"] + 1,
                               "image_key": im["image_key"]})
    scene_rows.sort(key=lambda s: s["series"])
    return row, img_rows, locs, need, scene_rows


def _empty_image() -> dict:
    im = {k: None for k in IMG_COLUMNS}
    im.update({"image_key": "*", "kind": "main", "n_series": 0, "n_tiles": 0, "has_merged": False, "n_previews": 0,
               "T": 1, "C": 1, "Z": 1, "Y": 0, "X": 0, "in_zarr": False, "segmentation": "", "tables": "", "rois": "",
               "_fps": []})
    return im


def _apply_image_override(im: dict, field: str, v) -> None:
    if field == "acquired_start":
        im["acquired_start"] = v
        im["_manual_start"] = True
    elif field == "objective":
        im["objective"], im["objective_source"] = v, "manual"
    elif field == "magnification":
        im["magnification"], im["objective_source"] = v, "manual"
    elif field == "na":
        im["na"], im["objective_source"] = v, "manual"
    elif field == "pixel_size_um":
        im["pixel_size_x_um"] = im["pixel_size_y_um"] = v
    elif field == "z_step_um":
        im["z_step_um"], im["z_step_source"] = v, "manual"
    elif field == "time_delta_s":
        im["time_delta_s"], im["time_delta_source"] = v, "manual"
    elif field == "total_time_s":
        im["total_time_s"], im["total_time_source"] = v, "manual"
    elif field == "channels":
        im["channels"] = v
    elif field == "dims":
        im["T"], im["C"], im["Z"], im["Y"], im["X"] = v
