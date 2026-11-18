"""Small preview images ("contact sheets") per catalog image.

One JPEG per image: one row per channel (grey, contrast-stretched) plus a colour
composite row; columns = downsampled overview (middle Z), its max projection over Z, and
a full-resolution cutout from the image centre (max projection), all at the first timepoint.
Pixels are read read-only with Bio-Formats (OmeDump "thumb" mode). Previews are cached by
the file's size/mtime, so only new or changed files are read; a time budget per run keeps
updates short (the rest follows in the next run).
"""

from __future__ import annotations

import hashlib
import json
import os
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import safety
from .javabridge import Job, run_jobs
from .scan import abs_path

PREVIEW_VERSION = 5
TILE = 192          # panel size in the contact sheet
QUALITY = 82
ROLE_ORDER = {"merged": 0, "image": 1, "tile": 2, "other": 3, "preview": 4}
PALETTE = [(0, 255, 0), (255, 0, 255), (0, 255, 255), (255, 255, 0), (255, 64, 64), (64, 128, 255), (255, 160, 0),
           (255, 255, 255)]


def previews_dir(cfg) -> str:
    return os.path.join(cfg.out_dir, "previews")


def _key(rel: str, sig, series: int) -> str:
    return hashlib.sha1(f"{rel}|{sig}|{series}|{PREVIEW_VERSION}".encode("utf-8")).hexdigest()[:16]


def choose_series(scenes: list[dict]) -> int | None:
    """Representative series of one image: merged mosaic > single image > middle tile > ..."""
    cands = [s for s in scenes if not str(s.get("role", "")).startswith("variant") and s.get("role") != "aux"]
    if not cands:
        cands = [s for s in scenes if s.get("role") != "aux"]
    if not cands:
        return None
    best = min(ROLE_ORDER.get(s["role"], 5) for s in cands)
    group = sorted((s for s in cands if ROLE_ORDER.get(s["role"], 5) == best), key=lambda s: s["series"])
    return group[len(group) // 2]["series"] if group[0]["role"] == "tile" else group[0]["series"]


def plan(data: dict, files: dict) -> list[dict]:
    """-> [{acq_id, image_key, file, series, key, path}] for every previewable image."""
    acq = {a["acq_id"]: a for a in data["acquisitions"]}
    by_img: dict[tuple, list] = {}
    for s in data.get("scenes", []):
        by_img.setdefault((s["acq_id"], s["image_key"]), []).append(s)
    out = []
    for (aid, ikey), scenes in by_img.items():
        a = acq.get(aid)
        if not a or not a.get("present") or a.get("category") == "snapshot":
            continue
        rel = scenes[0].get("file")
        if not rel or rel.lower().endswith(".zarr") or rel not in files:
            continue  # metadata came from a zarr (original unreadable): not supported yet
        if all(s.get("role") == "aux" for s in scenes):
            continue
        series = choose_series(scenes)
        if series is None:
            continue
        key = _key(rel, files[rel], series)
        title = a.get("name") or aid
        if ikey and ikey != "*" and ikey != title:
            title += f" – {ikey}"
        out.append({"acq_id": aid, "image_key": ikey, "file": rel, "series": series, "key": key,
                    "acq_date": a.get("acquired_start") or "", "title": title})
    return out


def plan_from_db(cfg, db) -> list[dict]:
    """Same as plan(), from the catalog database (for make_previews.py, which runs without a scan)."""
    from .scan import abs_path as _abs
    acq = {r[0]: {"acq_id": r[0], "present": r[1], "category": r[2], "name": r[3], "acquired_start": r[4]}
           for r in db.execute("SELECT acq_id, present, category, name, acquired_start FROM acquisitions")}
    cols = ["acq_id", "file", "series", "image_key", "role"]
    scenes = [dict(zip(cols, r)) for r in db.execute(f"SELECT {', '.join(cols)} FROM scenes")]
    files = {}
    for rel in {s["file"] for s in scenes if s["file"]}:
        try:
            st = os.stat(_abs(cfg, rel))
            files[rel] = (st.st_size, st.st_mtime)
        except OSError:
            pass
    return plan({"acquisitions": list(acq.values()), "scenes": scenes}, files)


def make(cfg, items: list[dict], log, budget_s: float) -> dict:
    """Create the missing previews of ``items`` (newest datasets first) within the time budget."""
    pdir = previews_dir(cfg)
    safety.makedirs(pdir)
    have = {f[:-4] for f in os.listdir(pdir) if f.endswith(".jpg")}
    todo = [it for it in items if it["key"] not in have]
    stats = {"images": len(items), "existing": len(items) - len(todo), "made": 0, "failed": 0, "deferred": 0}
    if todo and budget_s > 0:
        tmp = os.path.join(cfg.cache_dir, "thumbs")
        safety.makedirs(tmp)
        by_file: dict[str, list] = {}
        for it in sorted(todo, key=lambda it: it["acq_date"], reverse=True):
            by_file.setdefault(it["file"], []).append(it)
        t0 = time.time()
        files_order = list(by_file)
        log(f"Previews: {len(todo)} to make from {len(files_order)} file(s) (budget {budget_s / 60:.0f} min)")
        for n, rel in enumerate(files_order):
            if time.time() - t0 > budget_s:
                stats["deferred"] += sum(len(by_file[r]) for r in files_order[n:])
                break
            its = by_file[rel]
            prefix = os.path.join(tmp, hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16])
            series = sorted({it["series"] for it in its})
            res = run_jobs(cfg, [Job("thumb", abs_path(cfg, rel), prefix, ",".join(map(str, series)))], log,
                           label="preview ")[0]
            for it in its:
                base = f"{prefix}_s{it['series']}"
                try:
                    if not res.ok:
                        raise RuntimeError(res.message)
                    compose(base, os.path.join(pdir, it["key"] + ".jpg"), it["title"])
                    stats["made"] += 1
                    have.add(it["key"])
                except Exception as exc:  # noqa: BLE001 - one bad image must not stop the run
                    stats["failed"] += 1
                    log(f"    preview failed for {rel} series {it['series']}: {str(exc)[:200]}")
                for ext in (".bin", ".json"):
                    if os.path.exists(base + ext):
                        safety.remove(base + ext)
    else:
        stats["deferred"] = len(todo)
    stats["have"] = have
    return stats


def update(cfg, data: dict, files: dict, log, budget_s: float) -> dict:
    """Create missing previews within the budget (0 = none) and link the existing ones:
    sets image["preview"] = 'previews/<key>.jpg'. Returns stats."""
    items = plan(data, files)
    stats = make(cfg, items, log, budget_s)
    have = stats.pop("have")
    for im in data["images"]:
        im["preview"] = None
    idx = {(it["acq_id"], it["image_key"]): it for it in items if it["key"] in have}
    for im in data["images"]:
        it = idx.get((im["acq_id"], im["image_key"]))
        if it:
            im["preview"] = f"previews/{it['key']}.jpg"
            im["preview_series"] = it["series"]
    stats["available"] = len(idx)
    return stats


def referenced(data: dict) -> set[str]:
    return {os.path.basename(im["preview"]) for im in data["images"] if im.get("preview")}


# ----------------------------------------------------------------------------- drawing

def _font(size: int):
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _stretch(a: np.ndarray, lo: float | None = None, hi: float | None = None) -> np.ndarray:
    """Contrast stretch; without lo/hi each panel uses its own 0.5-99.8 percentiles."""
    if lo is None or hi is None:
        v = a[np.isfinite(a)]
        lo, hi = (float(np.percentile(v, 0.5)), float(np.percentile(v, 99.8))) if v.size else (0.0, 1.0)
    a = np.nan_to_num(a, nan=lo, neginf=lo, posinf=hi)
    return np.clip((a - lo) / (hi - lo if hi > lo else 1.0), 0, 1)


def _fit(a: np.ndarray, size: int) -> Image.Image:
    """float 0..1 array -> greyscale image fitted into size x size (aspect kept)."""
    img = Image.fromarray((a * 255).astype(np.uint8))
    img.thumbnail((size, size), Image.LANCZOS) if max(img.size) > size else None
    if max(img.size) < size and min(img.size) > 0:
        f = size / max(img.size)
        img = img.resize((max(1, round(img.size[0] * f)), max(1, round(img.size[1] * f))), Image.NEAREST
                         if f >= 2 else Image.LANCZOS)
    return img


def _rgb_color(v) -> tuple[int, int, int] | None:
    if v is None:
        return None
    v = int(v) & 0xFFFFFFFF
    c = ((v >> 24) & 255, (v >> 16) & 255, (v >> 8) & 255)
    return None if c in ((255, 255, 255), (0, 0, 0)) else c


def compose(base: str, out_path: str, title: str) -> None:
    with open(base + ".json", encoding="utf-8") as fh:
        meta = json.load(fh)
    raw = np.fromfile(base + ".bin", dtype="<f4")
    nch, ow, oh, cw, chh = meta["nch"], meta["ov_w"], meta["ov_h"], meta["cut_w"], meta["cut_h"]
    per = 2 * ow * oh + 2 * cw * chh
    if raw.size != nch * per:
        raise ValueError(f"preview data size mismatch ({raw.size} != {nch * per})")
    z = meta["Z"]
    rgb = meta.get("rgb", 1)
    names = meta.get("names") or []
    chans = []
    for c in range(nch):
        o = c * per
        ov_mid = raw[o:o + ow * oh].reshape(oh, ow)
        ov_max = raw[o + ow * oh:o + 2 * ow * oh].reshape(oh, ow)
        o2 = o + 2 * ow * oh
        cu_max = raw[o2 + cw * chh:o2 + 2 * cw * chh].reshape(chh, cw)
        cu_mid = raw[o2:o2 + cw * chh].reshape(chh, cw)
        lo = hi = None
        if rgb > 1:  # true RGB data (e.g. brightfield): fixed scaling, same for all three samples
            lo, hi = 0.0, max(255.0, float(np.nanmax(ov_max)) if ov_max.size else 255.0)
        cols = [_stretch(ov_mid, lo, hi)]
        if z > 1:
            cols.append(_stretch(ov_max, lo, hi))
        cols.append(_stretch(cu_max if z > 1 else cu_mid, lo, hi))
        color = (PALETTE[c] if rgb == 1 else ((255, 0, 0), (0, 255, 0), (0, 0, 255))[c % 3])
        mc = _rgb_color((meta.get("colors") or [None] * nch)[c]) if rgb == 1 else None
        # brightfield / transmitted light: bright background -> left out of the composite
        bright = rgb == 1 and float(np.median(cols[0])) > 0.35
        chans.append({"cols": cols, "name": names[c] if c < len(names) and names[c] else f"C{c}",
                      "color": mc or (color if nch > 1 else (255, 255, 255)), "bright": bright})
    headers = ["overview" + (" (mid Z)" if z > 1 else "")] + (["max projection"] if z > 1 else []) + \
              ["cutout 1:1" + (" (max Z)" if z > 1 else "")]
    ncol = len(headers)
    rows = ([] if rgb > 1 else chans) + ([{"composite": True}] if nch > 1 else [])
    if not rows:
        rows = chans
    gap, head = 4, 50
    W = ncol * TILE + (ncol + 1) * gap
    H = head + len(rows) * (TILE + gap) + gap
    sheet = Image.new("RGB", (W, H), (24, 24, 26))
    d = ImageDraw.Draw(sheet)
    f_small, f_title = _font(12), _font(13)
    sub = f"{meta['X']}x{meta['Y']} px, Z={z}, T={meta['T']}, series {meta['series']} (Fiji {meta['series'] + 1})"
    if "cut_x" in meta:
        sub += f"; cutout at x={meta['cut_x']}, y={meta['cut_y']}"
    while d.textlength(title, font=f_title) > W - 2 * gap and len(title) > 10:
        title = title[:-2]
    d.text((gap, 2), title, fill=(235, 235, 235), font=f_title)
    d.text((gap, 18), sub, fill=(160, 160, 165), font=f_small)
    for ci, h in enumerate(headers):
        tw = d.textlength(h, font=f_small)
        d.text((gap + ci * (TILE + gap) + (TILE - tw) / 2, 34), h, fill=(160, 160, 165), font=f_small)
    for ri, row in enumerate(rows):
        y = head + ri * (TILE + gap)
        for ci in range(ncol):
            x = gap + ci * (TILE + gap)
            if row.get("composite"):
                acc = None
                fl = [ch for ch in chans if not ch["bright"]] or chans
                for ch in fl:
                    g = np.asarray(_fit(ch["cols"][ci], TILE), dtype=np.float32) / 255.0
                    bg = float(np.median(g))  # background subtraction keeps the composite from washing out
                    g = np.clip((g - bg) / max(1e-3, 1.0 - bg), 0, 1)
                    c = np.array(ch["color"], dtype=np.float32)[None, None, :] / 255.0
                    acc = g[..., None] * c if acc is None else acc + g[..., None] * c
                img = Image.fromarray((np.clip(acc, 0, 1) * 255).astype(np.uint8), "RGB")
            else:
                img = _fit(row["cols"][ci], TILE).convert("RGB")
            ox, oy = x + (TILE - img.size[0]) // 2, y + (TILE - img.size[1]) // 2
            sheet.paste(img, (ox, oy))
        label = row["name"] if not row.get("composite") else \
            ("composite" + (" (fluorescence)" if any(ch["bright"] for ch in chans) and
                             not all(ch["bright"] for ch in chans) else ""))
        col = (235, 235, 235) if row.get("composite") else tuple(row["color"])
        d.rectangle([gap, y, gap + d.textlength(label, font=f_small) + 6, y + 15], fill=(0, 0, 0))
        d.text((gap + 3, y + 1), label, fill=col, font=f_small)
    tmp = out_path + ".part"
    with safety.open_write(tmp, "wb") as fh:
        sheet.save(fh, "JPEG", quality=QUALITY, optimize=True)
    os.replace(tmp, out_path)
