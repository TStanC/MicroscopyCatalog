"""Read-only queries on catalog.sqlite for the server's JSON API (used by mcp_server.py).

Every function takes an open connection (``connect``: read-only, short-lived - opened per
request and closed right after, so a build can always replace the file) and returns plain
JSON-serialisable dicts/lists. Results are capped (``MAX_LIMIT``) so that a large catalog
never produces huge answers.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

MAX_LIMIT = 500
SQL_TIMEOUT_S = 10

# compact columns of a dataset in search results
SEARCH_COLUMNS = ("acq_id", "name", "instrument", "session", "status", "kind", "format", "acquired_start",
                  "total_time_s", "dims_TCZYX", "objective", "magnification", "na", "pixel_size_um", "z_step_um",
                  "time_delta_s", "channels", "size_bytes", "has_zarr", "has_segmentation", "has_tables", "path")
# columns searched by free text (all words must occur in one of them or in a comment of the dataset)
TEXT_COLUMNS = ("acq_id", "name", "path", "instrument", "session", "objective", "channels", "notes",
                "segmentation_names", "table_names", "owner", "kind", "format")
FLAG_COLUMNS = ("has_original", "has_zarr", "has_segmentation", "has_tables", "has_rois", "has_processed",
                "has_exports", "has_metadata_files")
EQ_FILTERS = ("instrument", "session", "owner", "status", "kind", "format", "category")
SORTS = {"date_desc": "a.acquired_start DESC", "date_asc": "a.acquired_start ASC",
         "size_desc": "a.size_bytes DESC", "name": "a.name COLLATE NOCASE ASC"}

# active comments: added and not deleted (table comments is an append-only log)
ACTIVE_COMMENTS = ("SELECT * FROM comments WHERE action='add' AND comment_id NOT IN "
                   "(SELECT comment_id FROM comments WHERE action='delete')")


def connect(path: str, immutable: bool = False) -> sqlite3.Connection:
    """Read-only connection. ``immutable`` for the copy in the catalog folder (network share:
    no locking; it is replaced atomically, never written live)."""
    uri = Path(path).absolute().as_uri() + ("?mode=ro&immutable=1" if immutable else "?mode=ro")
    db = sqlite3.connect(uri, uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _limit(n, default: int = 50) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, MAX_LIMIT))


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _clean(row: sqlite3.Row | dict) -> dict:
    """Row as dict without empty values (keeps answers short)."""
    return {k: row[k] for k in row.keys() if row[k] not in (None, "")}


def _active_comments_sql(db: sqlite3.Connection) -> str:
    return ACTIVE_COMMENTS if _has_table(db, "comments") else \
        "SELECT '' ts, '' user, '' action, '' comment_id, '' acq_id, '' image_key, '' text WHERE 0"


# ----------------------------------------------------------------------------- overview

def overview(db: sqlite3.Connection) -> dict:
    """Counts and the distinct values to filter on."""
    def counts(col: str, n: int = 100) -> dict:
        return {(r[0] if r[0] is not None else ""): r[1] for r in db.execute(
            f"SELECT {col}, count(*) FROM acquisitions GROUP BY {col} ORDER BY count(*) DESC LIMIT ?", (n,))}

    ac = _active_comments_sql(db)
    out = {
        "n_datasets": db.execute("SELECT count(*) FROM acquisitions").fetchone()[0],
        "n_images": db.execute("SELECT count(*) FROM images").fetchone()[0],
        "n_comments": db.execute(f"SELECT count(*) FROM ({ac})").fetchone()[0],
        "acquired": dict(zip(("first", "last"), db.execute(
            "SELECT min(acquired_start), max(acquired_start) FROM acquisitions WHERE acquired_start > ''").fetchone())),
        "status": counts("status"), "instrument": counts("instrument"), "owner": counts("owner"),
        "kind": counts("kind"), "format": counts("format"), "category": counts("category"),
        "flags": {f: db.execute(f"SELECT count(*) FROM acquisitions WHERE {f}=1").fetchone()[0] for f in FLAG_COLUMNS},
        "n_sessions": db.execute("SELECT count(DISTINCT instrument || '/' || session) FROM acquisitions").fetchone()[0],
        "tables": {r[0]: [c[1] for c in db.execute(f"PRAGMA table_info({r[0]})")]
                   for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")},
    }
    if _has_table(db, "runs"):
        r = db.execute("SELECT * FROM runs ORDER BY started DESC LIMIT 1").fetchone()
        out["last_build"] = _clean(r) if r else None
    return out


# ----------------------------------------------------------------------------- datasets

def search_datasets(db: sqlite3.Connection, p: dict) -> dict:
    """Datasets matching all given filters (``p``: see mcp_server.search_datasets for the keys).
    Returns ``{"total": N, "rows": [...]}`` with at most ``limit`` compact rows."""
    where, args = [], []
    for word in str(p.get("text") or "").lower().split():
        hay = " || ' ' || ".join(f"coalesce(a.{c}, '')" for c in TEXT_COLUMNS)
        where.append(f"(instr(lower({hay}), ?) > 0 OR a.acq_id IN "
                     f"(SELECT acq_id FROM ({_active_comments_sql(db)}) WHERE instr(lower(text), ?) > 0))")
        args += [word, word]
    for col in EQ_FILTERS:
        v = p.get(col)
        if v not in (None, ""):
            where.append(f"lower(a.{col}) = lower(?)")
            args.append(str(v))
    for col in FLAG_COLUMNS:
        v = p.get(col)
        if v is not None and v != "":
            where.append(f"coalesce(a.{col}, 0) {'=' if _truthy(v) else '!='} 1")
    for key, col, op in (("min_T", "max_T", ">="), ("min_Z", "max_Z", ">="), ("min_C", "max_C", ">="),
                         ("min_total_time_s", "total_time_s", ">="), ("max_total_time_s", "total_time_s", "<=")):
        v = p.get(key)
        if v not in (None, ""):
            where.append(f"CAST(a.{col} AS REAL) {op} ?")
            args.append(float(v))
    if p.get("magnification") not in (None, ""):
        where.append("CAST(a.magnification AS REAL) = ?")
        args.append(float(p["magnification"]))
    if p.get("objective"):
        where.append("instr(lower(coalesce(a.objective, '')), lower(?)) > 0")
        args.append(str(p["objective"]))
    if p.get("date_from"):
        where.append("substr(a.acquired_start, 1, 10) >= ?")
        args.append(str(p["date_from"])[:10])
    if p.get("date_to"):
        where.append("a.acquired_start > '' AND substr(a.acquired_start, 1, 10) <= ?")
        args.append(str(p["date_to"])[:10])
    if not _truthy(p.get("include_missing", False)) and not p.get("status"):
        where.append("coalesce(a.status, '') != 'missing'")
    ncom = f"(SELECT count(*) FROM ({_active_comments_sql(db)}) c WHERE c.acq_id = a.acq_id)"
    if p.get("has_comments") not in (None, ""):
        where.append(f"{ncom} {'>' if _truthy(p['has_comments']) else '='} 0")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.execute(f"SELECT count(*) FROM acquisitions a{w}", args).fetchone()[0]
    order = SORTS.get(str(p.get("sort") or "date_desc"), SORTS["date_desc"])
    limit, offset = _limit(p.get("limit")), max(0, int(p.get("offset") or 0))
    cols = ", ".join(f"a.{c}" for c in SEARCH_COLUMNS)
    rows = db.execute(f"SELECT {cols}, {ncom} AS n_comments FROM acquisitions a{w} ORDER BY {order} "
                      f"LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    return {"total": total, "offset": offset, "rows": [_clean(r) for r in rows]}


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(v)


def get_dataset(db: sqlite3.Connection, acq_id: str, max_scenes: int = 50) -> dict | None:
    """Everything about one dataset: record, images, files, scenes (first ``max_scenes``), comments."""
    a = db.execute("SELECT * FROM acquisitions WHERE acq_id=?", (acq_id,)).fetchone()
    if a is None:
        return None
    out = {"dataset": _clean(a)}
    out["images"] = [_clean(r) for r in db.execute("SELECT * FROM images WHERE acq_id=? ORDER BY rowid", (acq_id,))]
    out["files"] = [_clean(r) for r in db.execute("SELECT * FROM locations WHERE acq_id=? ORDER BY role, rel",
                                                   (acq_id,))]
    if _has_table(db, "scenes"):
        n = db.execute("SELECT count(*) FROM scenes WHERE acq_id=?", (acq_id,)).fetchone()[0]
        out["n_scenes"] = n
        out["scenes"] = [_clean(r) for r in db.execute(
            "SELECT * FROM scenes WHERE acq_id=? ORDER BY file, CAST(series AS INTEGER) LIMIT ?",
            (acq_id, max(0, min(int(max_scenes), MAX_LIMIT))))]
    if _has_table(db, "needs_input"):
        out["needs_input"] = [_clean(r) for r in db.execute("SELECT field, image_key, hint FROM needs_input "
                                                             "WHERE acq_id=?", (acq_id,))]
    out["comments"] = [_clean(r) for r in db.execute(
        f"SELECT comment_id, ts, user, image_key, text FROM ({_active_comments_sql(db)}) "
        f"WHERE acq_id=? ORDER BY ts", (acq_id,))]
    return out


def known_acq(db: sqlite3.Connection, acq_id: str) -> bool:
    return db.execute("SELECT 1 FROM acquisitions WHERE acq_id=?", (acq_id,)).fetchone() is not None


# ----------------------------------------------------------------------------- comments

def search_comments(db: sqlite3.Connection, p: dict) -> dict:
    """Active comments, newest first; filters ``text`` (all words), ``user``, ``acq_id``."""
    where, args = [], []
    for word in str(p.get("text") or "").lower().split():
        where.append("instr(lower(c.text), ?) > 0")
        args.append(word)
    if p.get("user"):
        where.append("lower(c.user) = lower(?)")
        args.append(str(p["user"]))
    if p.get("acq_id"):
        where.append("c.acq_id = ?")
        args.append(str(p["acq_id"]))
    w = (" WHERE " + " AND ".join(where)) if where else ""
    base = f"FROM ({_active_comments_sql(db)}) c LEFT JOIN acquisitions a ON a.acq_id = c.acq_id{w}"
    total = db.execute(f"SELECT count(*) {base}", args).fetchone()[0]
    rows = db.execute(f"SELECT c.comment_id, c.ts, c.user, c.acq_id, c.image_key, c.text, a.name, a.path {base} "
                      f"ORDER BY c.ts DESC LIMIT ?", args + [_limit(p.get("limit"))]).fetchall()
    return {"total": total, "rows": [_clean(r) for r in rows]}


def filter_pending_comments(entries: list[dict], p: dict) -> list[dict]:
    """Inbox entries (not yet applied) that add or delete a comment, with the same filters as
    ``search_comments``."""
    words = str(p.get("text") or "").lower().split()
    out = []
    for e in entries:
        if e.get("action") not in ("comment_add", "comment_delete"):
            continue
        if p.get("acq_id") and e.get("acq_id") != p["acq_id"]:
            continue
        if p.get("user") and str(e.get("user", "")).lower() != str(p["user"]).lower():
            continue
        if words and not all(w in str(e.get("text", "")).lower() for w in words):
            continue
        out.append({k: v for k, v in e.items() if not k.startswith("_") and k != "host"})
    return out


def comment_author(db: sqlite3.Connection | None, comment_id: str, pending: list[dict]) -> dict | None:
    """``{"user", "acq_id"}`` of an active comment - applied, or added in the inbox and not yet
    applied - or None (unknown / deleted / deletion pending)."""
    if any(e.get("action") == "comment_delete" and e.get("comment_id") == comment_id for e in pending):
        return None
    if db is not None and _has_table(db, "comments"):
        r = db.execute(f"SELECT user, acq_id FROM ({ACTIVE_COMMENTS}) WHERE comment_id=?", (comment_id,)).fetchone()
        if r:
            return {"user": r["user"], "acq_id": r["acq_id"]}
    for e in pending:
        if e.get("action") == "comment_add" and e.get("id") == comment_id:
            return {"user": str(e.get("user", "")), "acq_id": str(e.get("acq_id", ""))}
    return None


# ----------------------------------------------------------------------------- SQL

_ALLOWED = {getattr(sqlite3, n, v) for n, v in
            (("SQLITE_SELECT", 21), ("SQLITE_READ", 20), ("SQLITE_FUNCTION", 31), ("SQLITE_RECURSIVE", 33))}


def _authorizer(action, *_):
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def run_sql(db: sqlite3.Connection, sql: str, limit=200) -> dict:
    """One SELECT statement (anything else is refused by the authorizer; the connection is
    read-only anyway). At most ``limit`` rows; aborted after SQL_TIMEOUT_S seconds."""
    sql = str(sql or "").strip().rstrip(";")
    if not sql:
        raise ValueError("empty SQL")
    limit = _limit(limit, 200)
    deadline = time.monotonic() + SQL_TIMEOUT_S
    db.set_authorizer(_authorizer)
    db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cur = db.execute(sql)
        rows = cur.fetchmany(limit + 1)
    except sqlite3.DatabaseError as exc:
        msg = str(exc)
        if "interrupted" in msg:
            msg = f"query took longer than {SQL_TIMEOUT_S} s"
        raise ValueError(f"SQL error: {msg} (only SELECT is allowed)") from None
    finally:
        db.set_authorizer(None)
        db.set_progress_handler(None, 0)
    cols = [d[0] for d in cur.description or ()]
    return {"columns": cols, "rows": [[_jsonable(v) for v in r] for r in rows[:limit]],
            "truncated": len(rows) > limit}


def _jsonable(v):
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    return v
