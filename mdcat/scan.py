"""Fast read-only walk of the data root and classification of files.

Only ``os.scandir``/``os.stat``/``os.listdir`` are used here; no file content is read.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field

from .config import Config


@dataclass
class Scan:
    files: dict[str, tuple[int, float]]  # rel path ("/" separated) -> (size, mtime)
    dirs: set[str]
    zarrs: list[str]
    errors: list[tuple[str, str]]
    seconds: float


@dataclass
class Source:
    """One thing that is read for metadata: a raw file, a snapshot, or a zarr store."""

    rel: str
    type: str  # "raw" | "snapshot" | "zarr"
    fmt: str  # extension without dot, e.g. "vsi", "lif", "zarr"
    members: list[str] = field(default_factory=list)  # rel paths of all files belonging to it
    size: int = 0
    max_mtime: float = 0.0
    signature: str = ""


@dataclass
class Classified:
    sources: list[Source]
    attachments: list[str]  # rel paths of all other files (processed, metadata, docs, ...)
    member_of: dict[str, str]  # file rel -> source rel


def parent(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def basename(rel: str) -> str:
    return rel.rsplit("/", 1)[-1]


def ext_of(rel: str) -> str:
    name = basename(rel).lower()
    if name.endswith((".ome.tif", ".ome.tiff")):
        return ".tif"
    return os.path.splitext(name)[1]


def stem_of(rel: str) -> str:
    name = basename(rel)
    low = name.lower()
    for suffix in (".ome.tiff", ".ome.tif", ".ome.zarr"):
        if low.endswith(suffix):
            return name[: -len(suffix)]
    return os.path.splitext(name)[0]


def norm_name(text: str) -> str:
    """Normalise a file stem for name matching: lower case, non-alphanumerics -> '_'."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def abs_path(cfg: Config, rel: str) -> str:
    return os.path.join(cfg.labdata_root, *rel.split("/"))


def walk(cfg: Config) -> Scan:
    t0 = time.time()
    files: dict[str, tuple[int, float]] = {}
    dirs: set[str] = set()
    zarrs: list[str] = []
    errors: list[tuple[str, str]] = []
    excl_top = {n.lower() for n in cfg.exclude_top}
    excl_any = {n.lower() for n in cfg.exclude_names}
    # the tool's own publish folder (and the older single-file publish) are not data
    excl_root = {"microscopycatalog.html", "microscopycatalog.html.tmp"}
    if cfg.publish_name and os.path.normcase(os.path.dirname(cfg.publish_dir.rstrip("\\/"))) == \
            os.path.normcase(cfg.labdata_root.rstrip("\\/")):
        excl_root.add(cfg.publish_name.lower())
    stack = [""]
    while stack:
        rel = stack.pop()
        absd = abs_path(cfg, rel) if rel else cfg.labdata_root
        try:
            entries = list(os.scandir(absd))
        except OSError as exc:
            errors.append((rel, str(exc)))
            continue
        for entry in entries:
            name = entry.name
            low = name.lower()
            r = f"{rel}/{name}" if rel else name
            if name.startswith(".") or low in excl_any or (not rel and low in excl_root):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not rel and low in excl_top:
                        continue
                    if not rel and os.path.exists(os.path.join(entry.path, ".mdcat-publish")):
                        continue  # a catalog folder of this tool (also one used by another setup)
                    if low.endswith(".zarr"):
                        zarrs.append(r)
                        continue
                    dirs.add(r)
                    stack.append(r)
                else:
                    if not rel:
                        continue  # loose files directly in the data root are not datasets
                    st = entry.stat()
                    files[r] = (st.st_size, st.st_mtime)
            except OSError as exc:
                errors.append((r, str(exc)))
    zarrs.sort()
    return Scan(files=files, dirs=dirs, zarrs=zarrs, errors=errors, seconds=time.time() - t0)


def _in_named_dir(rel: str, names: set[str]) -> bool:
    return any(part.lower() in names for part in rel.split("/")[:-1])


def _token_contains(haystack: str, needle: str) -> bool:
    """True if normalised ``needle`` occurs in ``haystack`` at token boundaries."""
    if not needle or len(needle) >= len(haystack):
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![0-9])", haystack) is not None


def _signature(entries: list[tuple[str, int, float]]) -> str:
    h = hashlib.sha1()
    for rel, size, mtime in sorted(entries):
        h.update(f"{rel}|{size}|{int(mtime)}\n".encode("utf-8"))
    return h.hexdigest()


def classify(scan: Scan, cfg: Config) -> Classified:
    raw_exts = set(cfg.raw_exts)
    snap_exts = set(cfg.snapshot_exts)
    proc_dirs = {d.lower() for d in cfg.processed_dirs}
    meta_dirs = {d.lower() for d in cfg.metadata_dirs}
    conv_dirs = {d.lower() for d in cfg.converted_dirs}
    files = scan.files

    member_of: dict[str, str] = {}
    by_dir: dict[str, list[str]] = defaultdict(list)
    for rel in files:
        by_dir[parent(rel)].append(rel)

    # Companion folders of .vsi files ("_stem_") and Leica .xlef project folders.
    vsi_companion: dict[str, str] = {}
    xlef_dirs: dict[str, str] = {}
    for rel in sorted(files):
        e = ext_of(rel)
        if e == ".vsi":
            comp = f"{parent(rel)}/_{stem_of(rel)}_" if parent(rel) else f"_{stem_of(rel)}_"
            if comp in scan.dirs:
                vsi_companion[comp] = rel
        elif e == ".xlef":
            xlef_dirs.setdefault(parent(rel), rel)

    def owner_folder(rel: str) -> str | None:
        parts = rel.split("/")
        for i in range(len(parts) - 1, 0, -1):
            anc = "/".join(parts[:i])
            if anc in vsi_companion:
                return vsi_companion[anc]
            if anc in xlef_dirs and not rel.lower().endswith(".xlef"):
                return xlef_dirs[anc]
        return None

    # 1) Raw primaries (excluding derived/processed locations and folder members).
    raw: list[str] = []
    for rel in sorted(files):
        e = ext_of(rel)
        if e not in raw_exts:
            continue
        if _in_named_dir(rel, proc_dirs | meta_dirs | conv_dirs):
            continue
        if owner_folder(rel):
            continue
        raw.append(rel)

    # tif/tiff that are exports or derivatives of another raw file in the same folder.
    raw_set = set(raw)
    derived: set[str] = set()
    for rel in raw:
        if ext_of(rel) not in (".tif", ".tiff"):
            continue
        me = norm_name(stem_of(rel))
        for other in by_dir[parent(rel)]:
            if other == rel or other not in raw_set:
                continue
            ot = norm_name(stem_of(other))
            if ext_of(other) not in (".tif", ".tiff") and ot == me:
                derived.add(rel)  # e.g. Snap_553.tif exported from Snap_553.czi
            elif _token_contains(me, ot):
                derived.add(rel)  # e.g. Cropped_FFT_Snap_068.tif derived from Snap_068.tif
    raw = [r for r in raw if r not in derived]
    raw_set = set(raw)
    raw_stems_by_dir: dict[str, set[str]] = defaultdict(set)
    for rel in raw:
        raw_stems_by_dir[parent(rel)].add(norm_name(stem_of(rel)))

    sources: list[Source] = []
    for rel in raw:
        e = ext_of(rel)
        members = [rel]
        if e == ".vsi":
            comp = f"{parent(rel)}/_{stem_of(rel)}_" if parent(rel) else f"_{stem_of(rel)}_"
            if comp in vsi_companion:
                members += [f for f in files if f.startswith(comp + "/")]
        elif e == ".lif":
            lifext = f"{parent(rel)}/{stem_of(rel)}.lifext" if parent(rel) else f"{stem_of(rel)}.lifext"
            if lifext in files:
                members.append(lifext)
        elif e == ".xlef":
            d = parent(rel)
            members += [f for f in files if f.startswith(d + "/") and f != rel and owner_folder(f) == rel]
        for m in members:
            member_of[m] = rel
        sources.append(_make_source(rel, "raw", e.lstrip("."), members, files))

    # 2) Snapshots (jpg/avi) that are not exports of a raw file.
    for rel in sorted(files):
        e = ext_of(rel)
        if e not in snap_exts or rel in member_of:
            continue
        if _in_named_dir(rel, proc_dirs | meta_dirs | conv_dirs) or owner_folder(rel):
            continue
        if norm_name(stem_of(rel)) in raw_stems_by_dir[parent(rel)]:
            continue  # export/preview of a raw file -> attachment
        member_of[rel] = rel
        sources.append(_make_source(rel, "snapshot", e.lstrip("."), [rel], files))

    # Members of folders that belong to a raw file but were not listed yet (e.g. .ets).
    for rel in files:
        if rel not in member_of:
            own = owner_folder(rel)
            if own and own in raw_set:
                member_of[rel] = own
                for s in sources:
                    if s.rel == own:
                        s.members.append(rel)
                        s.size += files[rel][0]
                        s.max_mtime = max(s.max_mtime, files[rel][1])
                        break
    for s in sources:
        s.signature = _signature([(m, *files[m]) for m in s.members])

    # 3) Zarr stores.
    for rel in scan.zarrs:
        sig, mtime = zarr_signature(abs_path(cfg, rel))
        sources.append(Source(rel=rel, type="zarr", fmt="zarr", members=[], size=0, max_mtime=mtime, signature=sig))

    attachments = sorted(f for f in files if f not in member_of)
    sources.sort(key=lambda s: s.rel.lower())
    return Classified(sources=sources, attachments=attachments, member_of=member_of)


def _make_source(rel: str, typ: str, fmt: str, members: list[str], files) -> Source:
    size = sum(files[m][0] for m in members)
    mtime = max(files[m][1] for m in members)
    return Source(rel=rel, type=typ, fmt=fmt, members=sorted(members), size=size, max_mtime=mtime)


def zarr_signature(path: str) -> tuple[str, float]:
    """Cheap change signature of a zarr store from metadata files and group listings."""
    parts: list[str] = []
    mtimes: list[float] = []

    def st(p: str) -> str:
        try:
            s = os.stat(p)
        except OSError:
            return "-"
        mtimes.append(s.st_mtime)
        return f"{s.st_size}|{int(s.st_mtime)}"

    try:
        names = sorted(os.listdir(path))
    except OSError as exc:
        return f"ERR:{exc}", 0.0
    parts.append("root:" + ",".join(names))
    for n in (".zattrs", ".zgroup", "zarr.json"):
        if n in names:
            parts.append(n + st(os.path.join(path, n)))
    parts.append("ome" + st(os.path.join(path, "OME", "METADATA.ome.xml")))
    for n in names:
        if n.startswith(".") or n in ("OME", "zarr.json"):
            continue
        sd = os.path.join(path, n)
        try:
            sub = sorted(os.listdir(sd))
        except OSError:
            continue
        parts.append(f"{n}:" + ",".join(x for x in sub if not x.isdigit()))
        for m in (".zattrs", "zarr.json"):
            if m in sub:
                parts.append(st(os.path.join(sd, m)))
        for g in ("labels", "tables", "rois"):
            if g in sub:
                try:
                    listing = ",".join(sorted(os.listdir(os.path.join(sd, g))))
                except OSError:
                    listing = "?"
                parts.append(f"{g}:{listing}|{st(os.path.join(sd, g))}")
    sig = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
    return sig, max(mtimes) if mtimes else 0.0
