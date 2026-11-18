"""Manual input: manual_overrides.csv, needs_input.csv harvesting and interactive prompting."""

from __future__ import annotations

import csv
import os
import re
from datetime import datetime

from . import safety

OVERRIDE_COLUMNS = ["acq_id", "image_key", "field", "value", "note"]
NEEDS_COLUMNS = ["acq_id", "image_key", "field", "value", "instrument", "session", "name", "path", "hint"]

# field -> parser; image-level fields may use image_key "*" for all images of the dataset.
IMAGE_FIELDS = {
    "acquired_start", "objective", "magnification", "na", "pixel_size_um", "z_step_um",
    "time_delta_s", "total_time_s", "channels", "dims",
}
DATASET_FIELDS = {"owner", "notes", "exclude", "name", "link_attachment"}
ALL_FIELDS = IMAGE_FIELDS | DATASET_FIELDS


def parse_duration_s(text: str) -> float:
    t = text.strip().lower().replace(",", ".")
    m = re.fullmatch(r"(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)", t)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.fullmatch(r"([\d.]+)\s*(ms|s|sec|min|m|h|hr|d)?", t)
    if not m:
        raise ValueError(f"cannot parse duration {text!r} (use e.g. 90, 90s, 1.5min, 2h, 01:30:00)")
    v = float(m.group(1))
    return v * {"ms": 1e-3, "s": 1, "sec": 1, None: 1, "min": 60, "m": 60, "h": 3600, "hr": 3600, "d": 86400}[m.group(2)]


def parse_datetime(text: str) -> str:
    t = text.strip().replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d-%m-%Y", "%d-%m-%Y %H:%M"):
        try:
            return datetime.strptime(t, fmt).isoformat(timespec="seconds")
        except ValueError:
            pass
    raise ValueError(f"cannot parse date {text!r} (use YYYY-MM-DD or YYYY-MM-DD HH:MM)")


def parse_value(field: str, value: str):
    v = value.strip()
    if field == "acquired_start":
        return parse_datetime(v)
    if field in ("time_delta_s", "total_time_s"):
        return parse_duration_s(v)
    if field in ("magnification", "na", "pixel_size_um", "z_step_um"):
        try:
            return float(v.lower().rstrip("x").replace(",", ".").replace("µm", "").replace("um", "").strip())
        except ValueError:
            raise ValueError(f"{field} must be a number, got {v!r}") from None
    if field == "dims":
        parts = [int(p) for p in re.split(r"[,x ]+", v) if p]
        if len(parts) != 5:
            raise ValueError("dims must be 5 integers T,C,Z,Y,X")
        return parts
    if field == "exclude":
        return v.lower() in ("1", "true", "yes", "y", "x")
    return v


def _read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [{k: (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(fh)]


def _write_csv(path: str, columns: list[str], rows: list[dict]) -> None:
    with safety.open_write(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})


def ensure_overrides_file(path: str) -> None:
    if not os.path.exists(path):
        _write_csv(path, OVERRIDE_COLUMNS, [])


def harvest_needs_input(needs_path: str, overrides_path: str, log) -> int:
    """Move values the user filled into needs_input.csv over to manual_overrides.csv."""
    filled = [r for r in _read_csv(needs_path) if r.get("value")]
    if not filled:
        return 0
    existing = _read_csv(overrides_path)
    keys = {(r["acq_id"], r.get("image_key", ""), r["field"]) for r in existing}
    added = 0
    for r in filled:
        try:
            parse_value(r["field"], r["value"])
        except ValueError as exc:
            log(f"  needs_input.csv: ignoring {r['acq_id']} {r['field']}={r['value']!r}: {exc}")
            continue
        key = (r["acq_id"], r.get("image_key", ""), r["field"])
        if key in keys:
            existing = [e for e in existing if (e["acq_id"], e.get("image_key", ""), e["field"]) != key]
        existing.append({"acq_id": r["acq_id"], "image_key": r.get("image_key", ""), "field": r["field"],
                         "value": r["value"], "note": "from needs_input.csv"})
        keys.add(key)
        added += 1
    _write_csv(overrides_path, OVERRIDE_COLUMNS, existing)
    return added


FIELD_HINTS = {
    "acquired_start": "YYYY-MM-DD or YYYY-MM-DD HH:MM",
    "objective": "objective name, e.g. HC PL APO 40x/1.30 OIL",
    "magnification": "e.g. 40",
    "na": "e.g. 1.3",
    "pixel_size_um": "µm, e.g. 0.325",
    "z_step_um": "µm, e.g. 2",
    "time_delta_s": "e.g. 90, 90s, 1.5min, 2h, 01:30:00",
    "total_time_s": "e.g. 2h, 01:30:00",
    "channels": "comma separated names",
    "dims": "T,C,Z,Y,X",
    "owner": "self / other / a name",
    "notes": "free text shown in the notes column",
    "exclude": "yes / no",
    "name": "display name",
    "link_attachment": "path of an unassigned file (relative to the Labdata root)",
}


def set_override(path: str, acq_id: str, image_key: str, field: str, value: str, note: str) -> str:
    """Insert/replace one manual value (``value`` empty -> remove it). Returns 'set' or 'removed'.

    The caller holds the edit lock. Raises ValueError for unknown fields or unparsable values.
    """
    if field not in ALL_FIELDS:
        raise ValueError(f"unknown field {field!r}")
    image_key = (image_key or "").strip() if field in IMAGE_FIELDS else ""
    if field in IMAGE_FIELDS and not image_key:
        image_key = "*"
    value = (value or "").strip()
    if value:
        parse_value(field, value)  # raises ValueError with a helpful message
    rows = _read_csv(path)
    key = (acq_id, image_key, field)
    same = lambda r: (r["acq_id"], r.get("image_key", ""), r["field"]) == key  # noqa: E731
    if field == "link_attachment":  # several links per dataset are allowed
        same = lambda r: (r["acq_id"], r["field"], r.get("value", "")) == (acq_id, field, value)  # noqa: E731
    rows = [r for r in rows if not same(r)]
    if value:
        rows.append({"acq_id": acq_id, "image_key": image_key, "field": field, "value": value, "note": note})
    _write_csv(path, OVERRIDE_COLUMNS, rows)
    return "set" if value else "removed"


def remove_override(path: str, acq_id: str, image_key: str, field: str, value: str = "") -> bool:
    rows = _read_csv(path)
    keep = [r for r in rows if not (r["acq_id"] == acq_id and r.get("image_key", "") == (image_key or "")
                                    and r["field"] == field and (not value or r.get("value", "") == value))]
    if len(keep) == len(rows):
        return False
    _write_csv(path, OVERRIDE_COLUMNS, keep)
    return True


def read_overrides_raw(path: str) -> list[dict]:
    return _read_csv(path)


def load_overrides(path: str, log) -> list[dict]:
    out = []
    for i, r in enumerate(_read_csv(path), start=2):
        f = r.get("field", "")
        if not r.get("acq_id") or f not in ALL_FIELDS:
            if any(r.values()):
                log(f"  manual_overrides.csv line {i}: unknown field {f!r} or missing acq_id - ignored")
            continue
        try:
            r["parsed"] = parse_value(f, r.get("value", ""))
        except ValueError as exc:
            log(f"  manual_overrides.csv line {i}: {exc} - ignored")
            continue
        out.append(r)
    return out


def write_needs_input(path: str, rows: list[dict]) -> None:
    _write_csv(path, NEEDS_COLUMNS, rows)


def interactive(needs: list[dict], overrides_path: str) -> int:
    """Prompt for missing values in the terminal; answers are appended to manual_overrides.csv."""
    if not needs:
        print("Nothing needs manual input.")
        return 0
    existing = _read_csv(overrides_path)
    added = 0
    print(f"{len(needs)} value(s) missing. Enter a value, press Enter to skip, 'q' to stop.")
    for r in needs:
        print(f"\n[{r['acq_id']}] {r['instrument']}/{r['session']}  {r['name']}  image={r['image_key']}")
        print(f"  {r['path']}")
        while True:
            ans = input(f"  {r['field']} ({r['hint']}): ").strip()
            if ans.lower() == "q":
                _write_csv(overrides_path, OVERRIDE_COLUMNS, existing)
                return added
            if not ans:
                break
            try:
                parse_value(r["field"], ans)
            except ValueError as exc:
                print(f"  -> {exc}")
                continue
            existing.append({"acq_id": r["acq_id"], "image_key": r["image_key"], "field": r["field"],
                             "value": ans, "note": "interactive"})
            added += 1
            break
    _write_csv(overrides_path, OVERRIDE_COLUMNS, existing)
    return added
