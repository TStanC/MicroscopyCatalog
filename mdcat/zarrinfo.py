"""Read-only inspection of bioformats2raw / microio OME-Zarr stores.

Primary path: microio (``open_dataset(mode="r")``). If microio cannot open a store
(non-standard layout, missing root metadata, ...), a plain directory/JSON fallback
collects scene names and their labels/tables/rois so the store still shows up.
"""

from __future__ import annotations

import json
import logging
import os
import re

from .omexml import parse_ome

logging.getLogger("microio").setLevel(logging.ERROR)

META_FILES = {".zattrs", ".zgroup", ".zarray", "zarr.json"}


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _group_attrs(path: str) -> dict:
    v3 = _read_json(os.path.join(path, "zarr.json")).get("attributes", {})
    if v3:
        return {**v3, **(v3.get("ome") or {})}
    return _read_json(os.path.join(path, ".zattrs"))


def _subgroups(path: str) -> list[str]:
    try:
        return sorted(n for n in os.listdir(path) if n not in META_FILES and not n.startswith(".")
                      and os.path.isdir(os.path.join(path, n)))
    except OSError:
        return []


def inspect_zarr(path: str) -> tuple[dict, bytes | None]:
    """Return (summary, sidecar xml bytes). Opens the store strictly read-only."""
    try:
        entries = os.listdir(path)
    except OSError as exc:
        raise RuntimeError(f"cannot list zarr store: {exc}")
    if not entries:
        raise RuntimeError("empty zarr store (no files) - conversion probably failed or was aborted")

    xml_path = os.path.join(path, "OME", "METADATA.ome.xml")
    xml_bytes = None
    summary: dict = {"series": [], "scenes": [], "warnings": []}
    if os.path.exists(xml_path):
        with open(xml_path, "rb") as fh:
            xml_bytes = fh.read()
        try:
            summary.update(parse_ome(xml_bytes))
        except Exception as exc:  # malformed sidecar
            summary["warnings"].append(f"OME sidecar unreadable: {exc}")
    else:
        summary["warnings"].append("no OME/METADATA.ome.xml sidecar")

    try:
        summary["scenes"] = _scenes_microio(path)
        summary["reader"] = "microio"
    except ImportError:  # microio is optional (install.ps1 -WithMicroio)
        summary["scenes"] = _scenes_fallback(path, summary["series"])
        summary["reader"] = "fallback (microio not installed)"
    except Exception as exc:
        summary["warnings"].append(f"microio could not open the store ({type(exc).__name__}: {exc}); "
                                   "used directory fallback")
        summary["scenes"] = _scenes_fallback(path, summary["series"])
        summary["reader"] = "fallback"
    return summary, xml_bytes


def _scenes_microio(path: str) -> list[dict]:
    from microio import open_dataset

    ds = open_dataset(path, mode="r")
    scenes = []
    for ref in ds.list_scene_refs():
        attrs = _safe(lambda: ds.read_scene_metadata(ref.id, corrected=False), {})
        repaired = (attrs.get("microio") or {}).get("repair", {}).get("repaired_axes", {}) if isinstance(attrs, dict) else {}
        levels = _safe(lambda: ds.list_levels(ref.id), [])
        scenes.append({
            "id": ref.id,
            "name": ref.name,
            "ome_index": ref.ome_index,
            "shape": list(levels[0].shape) if levels else None,
            "scale": list(levels[0].scale) if levels else None,
            "n_levels": len(levels),
            "repaired": {k: {"value": v.get("value"), "unit": v.get("unit"), "source": v.get("source")}
                         for k, v in sorted(repaired.items()) if isinstance(v, dict)},
            "labels": sorted(_safe(lambda: ds.list_labels(ref.id), [])),
            "tables": sorted(_safe(lambda: ds.list_tables(ref.id), [])),
            "rois": sorted(_safe(lambda: ds.list_rois(ref.id), [])),
        })
    return scenes


def _scenes_fallback(path: str, series: list[dict]) -> list[dict]:
    ome_attrs = _group_attrs(os.path.join(path, "OME"))
    order = ome_attrs.get("series")
    scene_ids = [str(s) for s in order] if isinstance(order, list) and order else \
        [d for d in _subgroups(path) if d != "OME"]
    by_norm: dict[str, int] = {}
    for s in series:
        by_norm.setdefault(_norm(s["name"]), s["idx"])
    scenes = []
    for pos, sid in enumerate(scene_ids):
        sdir = os.path.join(path, sid)
        if not os.path.isdir(sdir):
            continue
        if isinstance(order, list) and order:
            ome_index = pos  # bioformats2raw layout: OME "series" order == OME Image order
        else:
            key = _norm(sid)
            ome_index = by_norm.get(key, by_norm.get(re.sub(r"_\d+$", "", key)))
        attrs = _group_attrs(sdir)
        repaired = (attrs.get("microio") or {}).get("repair", {}).get("repaired_axes", {})
        name = ((attrs.get("multiscales") or [{}])[0] or {}).get("name") or sid
        scenes.append({
            "id": sid, "name": str(name).lstrip("/"), "ome_index": ome_index, "shape": None, "scale": None,
            "n_levels": None,
            "repaired": {k: {"value": v.get("value"), "unit": v.get("unit"), "source": v.get("source")}
                         for k, v in sorted(repaired.items()) if isinstance(v, dict)},
            "labels": _subgroups(os.path.join(sdir, "labels")),
            "tables": _subgroups(os.path.join(sdir, "tables")),
            "rois": _subgroups(os.path.join(sdir, "rois")),
        })
    return scenes
