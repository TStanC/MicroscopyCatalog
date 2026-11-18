"""OME-XML parsing into compact, JSON-serialisable per-series summaries.

The same parser is used for XML produced by Bio-Formats from raw files and for the
OME/METADATA.ome.xml sidecar of bioformats2raw zarr stores, so both produce
comparable fingerprints.
"""

from __future__ import annotations

import math
import re
import statistics
import xml.etree.ElementTree as ET
from collections import defaultdict

# Bump when the summary content changes -> cached XML is re-parsed (no data access needed).
PARSER_VERSION = 4

LENGTH_UM = {
    "µm": 1.0, "μm": 1.0, "um": 1.0, "micron": 1.0, "micrometer": 1.0, "micrometre": 1.0,
    "nm": 1e-3, "nanometer": 1e-3, "pm": 1e-6, "Å": 1e-4, "angstrom": 1e-4,
    "mm": 1e3, "millimeter": 1e3, "cm": 1e4, "centimeter": 1e4, "m": 1e6, "meter": 1e6,
}
TIME_S = {
    "s": 1.0, "sec": 1.0, "second": 1.0, "ms": 1e-3, "millisecond": 1e-3,
    "µs": 1e-6, "μs": 1e-6, "us": 1e-6, "microsecond": 1e-6, "ns": 1e-9,
    "min": 60.0, "minute": 60.0, "h": 3600.0, "hour": 3600.0, "d": 86400.0, "day": 86400.0,
}
OBJ_FULL_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[xX×]\s*/\s*(\d*[.,]\d+)")
OBJ_MAG_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[xX×](?![a-zA-Z])")


def _f(x) -> float | None:
    try:
        v = float(str(x).replace(",", ".")) if x is not None else None
    except ValueError:
        return None
    return v if v is not None and math.isfinite(v) else None


def to_um(value, unit) -> float | None:
    v = _f(value)
    if v is None:
        return None
    if not unit:
        return v
    k = LENGTH_UM.get(unit.strip())
    return v * k if k is not None else None


def to_s(value, unit) -> float | None:
    v = _f(value)
    if v is None:
        return None
    if not unit:
        return v
    k = TIME_S.get(unit.strip())
    return v * k if k is not None else None


def norm_date(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip().replace("Z", "")
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d+)?", t)
    if not m:
        return None
    if int(m.group(1)[:4]) < 1995:
        return None
    frac = (m.group(3) or "")[:4]
    return f"{m.group(1)}T{m.group(2)}{frac}"


def parse_objective_model(model: str | None) -> tuple[float | None, float | None]:
    if not model:
        return None, None
    m = OBJ_FULL_RE.search(model)
    if m:
        return _f(m.group(1)), _f(m.group(2))
    m = OBJ_MAG_RE.search(model)
    if m:
        return _f(m.group(1)), None
    return None, None


def _rnd(v: float | None, nd: int = 6) -> float | None:
    return None if v is None else round(v, nd)


def _to_nm(value, unit) -> float | None:
    um = to_um(value, unit or "nm")
    return None if um is None else round(um * 1e3, 1)


def _color(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        v = int(raw) & 0xFFFFFFFF
    except ValueError:
        return None
    r, g, b = (v >> 24) & 255, (v >> 16) & 255, (v >> 8) & 255
    return f"#{r:02X}{g:02X}{b:02X}"


def parse_ome(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)
    ns = root.tag[1 : root.tag.index("}")] if root.tag.startswith("{") else ""

    def q(tag: str) -> str:
        return f"{{{ns}}}{tag}" if ns else tag

    objectives: dict[str, dict] = {}
    for ob in root.iter(q("Objective")):
        model = (ob.get("Model") or "").strip() or None
        mag = _f(ob.get("NominalMagnification"))
        na = _f(ob.get("LensNA"))
        pmag, pna = parse_objective_model(model)
        immersion = ob.get("Immersion")
        objectives[ob.get("ID")] = {
            "model": re.sub(r"\s+", " ", model) if model else None,
            "mag": mag if mag else pmag,
            "na": na if na else pna,
            "mag_src": "metadata" if mag else ("objective_model" if pmag else None),
            "na_src": "metadata" if na else ("objective_model" if pna else None),
            "immersion": immersion if immersion and immersion != "Other" else None,
        }

    series = []
    for idx, im in enumerate(root.findall(q("Image"))):
        px = im.find(q("Pixels"))
        if px is None:
            continue
        ad = im.find(q("AcquisitionDate"))
        osx = im.find(q("ObjectiveSettings"))
        obj = dict(objectives.get(osx.get("ID"), {})) if osx is not None else {}

        channels = []
        for ch in px.findall(q("Channel")):
            channels.append({
                "name": ch.get("Name") or None,
                "fluor": ch.get("Fluor") or None,
                "em_nm": _to_nm(ch.get("EmissionWavelength"), ch.get("EmissionWavelengthUnit")),
                "ex_nm": _to_nm(ch.get("ExcitationWavelength"), ch.get("ExcitationWavelengthUnit")),
                "color": _color(ch.get("Color")),
                "spp": int(ch.get("SamplesPerPixel") or 1),
                "mode": ch.get("AcquisitionMode") or None,
            })

        dts: dict[int, list[float]] = defaultdict(list)
        all_dt: list[float] = []
        zpos: dict[int, float] = {}
        expo: dict[int, set] = defaultdict(set)
        n_planes = 0
        for pl in px.findall(q("Plane")):
            n_planes += 1
            try:
                t = int(pl.get("TheT") or 0)
                c = int(pl.get("TheC") or 0)
                z = int(pl.get("TheZ") or 0)
            except ValueError:
                continue
            dt = to_s(pl.get("DeltaT"), pl.get("DeltaTUnit"))
            if dt is not None:
                dts[t].append(dt)
                all_dt.append(dt)
            if t == 0 and c == 0:
                pz = to_um(pl.get("PositionZ"), pl.get("PositionZUnit"))
                if pz is not None:
                    zpos[z] = pz
            e = to_s(pl.get("ExposureTime"), pl.get("ExposureTimeUnit"))
            if e is not None:
                expo[c].add(round(e, 6))

        size_t = int(px.get("SizeT") or 1)
        dt_med = dt_min = dt_max = None
        if size_t > 1 and len(dts) >= 2:
            times = [min(dts[t]) for t in sorted(dts)]
            diffs = [b - a for a, b in zip(times, times[1:])]
            dt_med, dt_min, dt_max = statistics.median(diffs), min(diffs), max(diffs)
        stage_z = None
        if len(zpos) >= 2:
            ordered = [zpos[k] for k in sorted(zpos)]
            steps = [abs(b - a) for a, b in zip(ordered, ordered[1:]) if abs(b - a) > 1e-6]
            if steps:
                stage_z = statistics.median(steps)

        series.append({
            "idx": idx,
            "name": im.get("Name") or str(idx),
            "date": norm_date(ad.text if ad is not None else None),
            "T": size_t,
            "C": int(px.get("SizeC") or 1),
            "Z": int(px.get("SizeZ") or 1),
            "Y": int(px.get("SizeY") or 1),
            "X": int(px.get("SizeX") or 1),
            "dtype": px.get("Type"),
            "px_x": _rnd(to_um(px.get("PhysicalSizeX"), px.get("PhysicalSizeXUnit"))),
            "px_y": _rnd(to_um(px.get("PhysicalSizeY"), px.get("PhysicalSizeYUnit"))),
            "px_z": _rnd(to_um(px.get("PhysicalSizeZ"), px.get("PhysicalSizeZUnit"))),
            "time_inc": _rnd(to_s(px.get("TimeIncrement"), px.get("TimeIncrementUnit"))),
            "objective": obj,
            "channels": channels,
            "n_planes": n_planes,
            "dt_med": _rnd(dt_med, 4),
            "dt_min": _rnd(dt_min, 4),
            "dt_max": _rnd(dt_max, 4),
            "span_s": _rnd(max(all_dt) - min(all_dt), 3) if all_dt else None,
            "stage_z": _rnd(stage_z, 4),
            "exposure_s": {str(c): sorted(v) for c, v in sorted(expo.items())},
        })
    return {"series": series}


def series_fingerprint(s: dict) -> str | None:
    """Identity of one series that survives conversion to zarr (needs an acquisition date)."""
    if not s.get("date"):
        return None
    return f"{s['date'][:19]}|{s['T']}x{s['C']}x{s['Z']}x{s['Y']}x{s['X']}"


def needs_objective_lookup(summary: dict) -> bool:
    for s in summary.get("series", []):
        if s["X"] <= 1 or s["Y"] <= 1:
            continue
        if re.match(r"(?i)^(macro|label|thumbnail)( image)?$", s["name"] or ""):
            continue
        ob = s.get("objective") or {}
        if not ob.get("mag") or not ob.get("na"):
            return True
    return False


_ORIG_FIELDS = {
    "objectivename": "model", "objective name": "model", "objective": "model", "objectivemodel": "model",
    "numericalaperture": "na", "numerical aperture": "na", "objective na": "na", "lensna": "na",
    "magnification": "mag", "objective magnification": "mag", "nominalmagnification": "mag",
    "immersion": "immersion",
}
ORIG_KEY_REGEX = r"(?i)(objective|magnification|numericalaperture|numerical aperture|immersion|lensna)"


def parse_orig_objectives(tsv_text: str, summary: dict) -> dict[str, dict]:
    """Map series idx (as str) -> objective info from Bio-Formats original metadata."""
    series = summary.get("series", [])
    by_name: dict[str, list[int]] = defaultdict(list)
    for s in series:
        by_name[s["name"]].append(s["idx"])
    found: dict[int, dict] = defaultdict(dict)
    global_vals: dict[str, str] = {}
    for line in tsv_text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        sidx, key, value = parts
        value = value.strip()
        if not value:
            continue
        last = re.sub(r"\s*#\d+$", "", key.split("|")[-1]).strip().lower()
        fld = _ORIG_FIELDS.get(last)
        if fld is None:
            continue
        targets: list[int] = []
        if int(sidx) >= 0:
            targets = [int(sidx)]
        elif "|" in key:
            prefix = re.sub(r"\s+Image #\d+$", "", key.split("|")[0]).strip()
            targets = by_name.get(prefix, [])
        if not targets:
            global_vals.setdefault(fld, value)
            continue
        for t in targets:
            found[t].setdefault(fld, value)

    out: dict[str, dict] = {}
    for s in series:
        vals = dict(global_vals)
        vals.update(found.get(s["idx"], {}))
        model = vals.get("model")
        pmag, pna = parse_objective_model(model)
        mag = _f(vals.get("mag")) or pmag
        na = _f(vals.get("na")) or pna
        if model or mag or na:
            out[str(s["idx"])] = {
                "model": re.sub(r"\s+", " ", model).strip() if model else None,
                "mag": mag,
                "na": na,
                "immersion": vals.get("immersion"),
            }
    return out
