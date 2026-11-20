"""Edit inbox in the catalog folder: every edit made in the browser is one small file.

    <catalog_dir>/inbox/<YYYYMMDDTHHMMSSmmm>_<user>_<id>.json

* Any catalog server writes edits there (atomic write + rename, unique names) - no lock, so
  several people on several machines can comment / enter values at the same time.
* The next build (whichever machine holds build.lock) processes the inbox like a queue:
  sorted by file name (= time), one entry after the other, into catalog.sqlite (comments)
  and manual_overrides.csv (values).
* Value edits are compare-and-set: an entry carries the value its author saw (``old_value``).
  If the stored value has changed since, the entry is not applied but moved to
  ``inbox/conflicts`` with the reason, and the page shows it.
* Every processed entry is recorded in table ``applied_edits`` (with its outcome), so
  processing it again - after a build that crashed before publishing - changes nothing.
  Entries leave the inbox only after the catalog was published successfully:
  ``done/`` (applied), ``conflicts/``, ``failed/`` (invalid); kept KEEP_DAYS days.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import sqlite3
from datetime import datetime

from . import comments as cm
from . import overrides as ov
from . import safety

DIR = "inbox"
OUTCOME_DIR = {"applied": "done", "conflict": "conflicts", "failed": "failed"}
KEEP_DAYS = 30
NAME_RE = re.compile(r"^\d{8}T\d{9}_[A-Za-z0-9-]{1,40}_[0-9a-f]{8}\.json$")
ACTIONS = ("comment_add", "comment_delete", "value_set", "value_remove")
FIELDS = ("acq_id", "image_key", "text", "comment_id", "field", "value", "old_value")
MAX_ENTRY_BYTES = 20_000


# ----------------------------------------------------------------------------- writing (servers)

def new_entry(action: str, user: str, **fields) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    return {"id": secrets.token_hex(4), "ts": datetime.now().isoformat(timespec="milliseconds"),
            "user": user, "host": socket.gethostname().lower(), "action": action,
            **{k: str(v) for k, v in fields.items() if k in FIELDS}}


def file_name(e: dict) -> str:
    t = datetime.fromisoformat(e["ts"])
    user = re.sub(r"[^A-Za-z0-9-]", "-", e.get("user") or "")[:40] or "unknown"
    return f"{t:%Y%m%dT%H%M%S}{t.microsecond // 1000:03d}_{user}_{e['id']}.json"


def submit(catalog_dir: str, e: dict) -> str:
    """Write one entry into the inbox. Returns its file name."""
    name = file_name(e)
    data = json.dumps(e, ensure_ascii=False, indent=1).encode("utf-8")
    if len(data) > MAX_ENTRY_BYTES:
        raise ValueError("edit too large")
    safety.publish_file(catalog_dir, f"{DIR}/{name}", data)
    return name


# ----------------------------------------------------------------------------- reading

def _list(catalog_dir: str, sub: str = "") -> list[dict]:
    d = os.path.join(catalog_dir, DIR, sub) if sub else os.path.join(catalog_dir, DIR)
    try:
        names = sorted(n for n in os.listdir(d) if NAME_RE.match(n))
    except FileNotFoundError:
        return []
    out = []
    for n in names:
        try:
            with open(os.path.join(d, n), encoding="utf-8") as fh:
                e = json.load(fh)
            if not isinstance(e, dict):
                raise ValueError("not a JSON object")
        except FileNotFoundError:
            continue  # processed by a build in the meantime
        except (OSError, ValueError) as exc:
            e = {"_error": f"unreadable inbox file: {exc}"}
        e["_file"] = n
        out.append(e)
    return out


def pending(catalog_dir: str) -> list[dict]:
    """Entries waiting to be processed, oldest first."""
    return _list(catalog_dir)


def recent(catalog_dir: str, outcome: str, days: float = 14) -> list[dict]:
    """Processed entries with this outcome ("conflict" / "failed") of the last ``days`` days."""
    cutoff = datetime.now().timestamp() - days * 86400
    out = []
    for e in _list(catalog_dir, OUTCOME_DIR[outcome]):
        try:
            if datetime.fromisoformat(str(e.get("processed", ""))).timestamp() >= cutoff:
                out.append(e)
        except ValueError:
            continue
    return out


def public(e: dict) -> dict:
    """An entry as sent to the page."""
    return {k: v for k, v in e.items() if not k.startswith("_")}


# ----------------------------------------------------------------------------- processing (builds)

def ensure_table(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS applied_edits(edit_id TEXT PRIMARY KEY, file TEXT, ts TEXT,
                  user TEXT, action TEXT, outcome TEXT, reason TEXT, processed TEXT)""")
    db.commit()


def _known_acq(db: sqlite3.Connection, acq_id: str) -> bool:
    try:
        return db.execute("SELECT 1 FROM acquisitions WHERE acq_id=?", (acq_id,)).fetchone() is not None
    except sqlite3.OperationalError:  # brand-new catalog without tables yet
        return False


def _label(field: str, image_key: str) -> str:
    ik = ov.norm_key(field, image_key)
    return field + ("" if not ik else " (all images)" if ik == "*" else f" (image {ik})")


def _apply_one(db: sqlite3.Connection, p_over: str, e: dict) -> tuple[str, str]:
    """Apply one entry. Returns (outcome, reason). Every action is idempotent: applying an
    entry a second time (crash between applying and recording) gives "applied" again."""
    if e.get("_error"):
        return "failed", e["_error"]
    action, user, acq = e.get("action"), str(e.get("user", "")), str(e.get("acq_id", ""))
    ts = str(e.get("ts", ""))[:19]
    if action not in ACTIONS or not e.get("id"):
        return "failed", f"unknown action {action!r} or missing id"

    if action == "comment_add":
        if cm.exists(db, e["id"]):
            return "applied", "already added"
        if not _known_acq(db, acq):
            return "failed", f"unknown dataset {acq!r}"
        try:
            cm.add_comment(db, acq, str(e.get("text", "")), user, str(e.get("image_key", "")),
                           ts=ts, comment_id=e["id"])
        except ValueError as exc:
            return "failed", str(exc)
        return "applied", ""

    if action == "comment_delete":
        cid = str(e.get("comment_id", ""))
        if not cm.exists(db, cid):
            return "failed", f"comment {cid!r} not found"
        if cm.exists(db, cid, "delete"):
            return "applied", "already deleted"
        cm.delete_comment(db, cid, user, ts=ts)
        return "applied", ""

    # value_set / value_remove: compare-and-set against the value the author saw
    field, value = str(e.get("field", "")), str(e.get("value", "")).strip()
    old = str(e.get("old_value", "")).strip()
    ik = str(e.get("image_key", ""))
    if field not in ov.ALL_FIELDS:
        return "failed", f"unknown field {field!r}"
    if not _known_acq(db, acq):
        return "failed", f"unknown dataset {acq!r}"
    note = f"web {user} {ts.replace('T', ' ')[:16]}"

    if field == "link_attachment":  # several links per dataset: no conflict possible
        if action == "value_set":
            ov.set_override(p_over, acq, "", field, value, note=note)
        else:
            ov.remove_override(p_over, acq, "", field, old)
        return "applied", ""

    row = ov.current_value(p_over, acq, ik, field)
    cur = (row or {}).get("value", "").strip()
    by = f" ({row.get('note')})" if row and row.get("note") else ""
    if action == "value_set":
        if not value:
            return "failed", "empty value"
        try:
            ov.parse_value(field, value)
        except ValueError as exc:
            return "failed", str(exc)
        if cur == value:
            return "applied", "already set"
        if cur != old:
            return "conflict", (f"{_label(field, ik)} is now {cur or 'not set'!r}{by}; "
                                f"{user} saw {old or 'not set'!r} and wanted {value!r}")
        ov.set_override(p_over, acq, ik, field, value, note=note)
        return "applied", ""

    # value_remove
    if not cur:
        return "applied", "already removed"
    if cur != old:
        return "conflict", (f"{_label(field, ik)} is now {cur!r}{by}; "
                            f"{user} wanted to remove {old or 'not set'!r}")
    ov.remove_override(p_over, acq, ik, field, cur)
    return "applied", ""


def apply(db: sqlite3.Connection, p_over: str, entries: list[dict], audit_path: str, log) -> list[tuple]:
    """Process ``entries`` (from pending(), oldest first) one after the other. The caller holds
    build.lock and the local edit lock. Returns [(entry, outcome, reason)] for archive()."""
    ensure_table(db)
    ov.ensure_overrides_file(p_over)
    seen = {r[0]: (r[1], r[2]) for r in db.execute("SELECT edit_id, outcome, reason FROM applied_edits")}
    results = []
    with safety.open_write(audit_path, "a", encoding="utf-8") as audit:
        for e in entries:
            eid = str(e.get("id", ""))
            if eid and eid in seen:
                outcome, reason = seen[eid]
                results.append((e, outcome, reason))
                continue
            try:
                outcome, reason = _apply_one(db, p_over, e)
            except (ValueError, sqlite3.Error) as exc:
                outcome, reason = "failed", str(exc)
            processed = datetime.now().isoformat(timespec="seconds")
            if eid:
                db.execute("INSERT OR REPLACE INTO applied_edits VALUES (?,?,?,?,?,?,?,?)",
                           (eid, e["_file"], str(e.get("ts", "")), str(e.get("user", "")),
                            str(e.get("action", "")), outcome, reason, processed))
                db.commit()
                seen[eid] = (outcome, reason)
            audit.write(json.dumps({"ts": processed, "edit_ts": e.get("ts"), "user": e.get("user"),
                                    "host": e.get("host"), "action": e.get("action"), "outcome": outcome,
                                    "reason": reason, **{k: e[k] for k in FIELDS if k in e}},
                                   ensure_ascii=False) + "\n")
            log(f"  inbox {e['_file']}: {e.get('action')} by {e.get('user')} -> {outcome}"
                + (f" ({reason})" if reason else ""))
            results.append((e, outcome, reason))
    return results


def archive(catalog_dir: str, results: list[tuple], log) -> None:
    """Move processed entries out of the inbox (call only after a successful publish)."""
    now = datetime.now().isoformat(timespec="seconds")
    for e, outcome, reason in results:
        name = e["_file"]
        rec = {**public(e), "outcome": outcome, "reason": reason, "processed": now}
        try:
            safety.publish_file(catalog_dir, f"{DIR}/{OUTCOME_DIR.get(outcome, 'failed')}/{name}",
                                json.dumps(rec, ensure_ascii=False, indent=1).encode("utf-8"))
            safety.unpublish_file(catalog_dir, f"{DIR}/{name}")
        except OSError as exc:  # stays in the inbox; applied_edits makes the next build skip it
            log(f"  WARNING: could not archive inbox/{name}: {exc}")


def prune(catalog_dir: str, days: float = KEEP_DAYS) -> int:
    """Delete processed entries older than ``days`` (the history stays in catalog.sqlite,
    manual_overrides.csv and logs/web_edits.log)."""
    cutoff = datetime.now().timestamp() - days * 86400
    n = 0
    for sub in OUTCOME_DIR.values():
        d = os.path.join(catalog_dir, DIR, sub)
        try:
            entries = list(os.scandir(d))
        except FileNotFoundError:
            continue
        for de in entries:
            if NAME_RE.match(de.name) and de.stat().st_mtime < cutoff:
                try:
                    safety.unpublish_file(catalog_dir, f"{DIR}/{sub}/{de.name}")
                    n += 1
                except OSError:
                    pass
    return n
