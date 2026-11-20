"""Free-text comments per dataset (optionally per image), stored in catalog.sqlite.

Table ``comments`` is a persistent, append-only log (never rewritten by the build): every
addition and deletion is one row (``action`` = add / delete), so the history of who did
what is kept. After every build the full log is exported to comments.csv and published
with the catalog as a backup; ``restore_if_empty`` re-imports such a copy into a new
(empty) database.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import sqlite3
from datetime import datetime

COMMENT_COLUMNS = ["ts", "user", "action", "comment_id", "acq_id", "image_key", "text"]
MAX_LEN = 4000


def connect(db_path: str) -> sqlite3.Connection:
    db = sqlite3.connect(db_path, timeout=30)
    ensure_table(db)
    return db


def ensure_table(db: sqlite3.Connection) -> None:
    db.execute(f"CREATE TABLE IF NOT EXISTS comments ({', '.join(c + ' TEXT' for c in COMMENT_COLUMNS)})")
    db.commit()


def _rows(db: sqlite3.Connection) -> list[dict]:
    cur = db.execute(f"SELECT {', '.join(COMMENT_COLUMNS)} FROM comments ORDER BY rowid")
    return [dict(zip(COMMENT_COLUMNS, r)) for r in cur.fetchall()]


def active_comments(db: sqlite3.Connection) -> list[dict]:
    """Comments that were added and not deleted, oldest first."""
    rows = _rows(db)
    deleted = {r["comment_id"] for r in rows if r["action"] == "delete"}
    return [{k: r[k] or "" for k in ("comment_id", "ts", "user", "acq_id", "image_key", "text")}
            for r in rows if r["action"] == "add" and r["comment_id"] not in deleted]


def _insert(db: sqlite3.Connection, row: dict) -> None:
    db.execute(f"INSERT INTO comments VALUES ({','.join('?' * len(COMMENT_COLUMNS))})",
               tuple(row.get(c, "") for c in COMMENT_COLUMNS))
    db.commit()


def check_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty comment")
    if len(text) > MAX_LEN:
        raise ValueError(f"comment longer than {MAX_LEN} characters")
    return text


def exists(db: sqlite3.Connection, comment_id: str, action: str = "add") -> bool:
    return db.execute("SELECT 1 FROM comments WHERE comment_id=? AND action=?",
                      (comment_id, action)).fetchone() is not None


def add_comment(db: sqlite3.Connection, acq_id: str, text: str, user: str, image_key: str = "",
                ts: str | None = None, comment_id: str | None = None) -> dict:
    """``ts`` / ``comment_id`` are given when the comment comes from the edit inbox."""
    text = check_text(text)
    ts = ts or datetime.now().isoformat(timespec="seconds")
    cid = comment_id or hashlib.sha1(f"{ts}|{user}|{acq_id}|{text}".encode("utf-8")).hexdigest()[:10]
    row = {"ts": ts, "user": user, "action": "add", "comment_id": cid, "acq_id": acq_id,
           "image_key": image_key or "", "text": text}
    _insert(db, row)
    return row


def delete_comment(db: sqlite3.Connection, comment_id: str, user: str, ts: str | None = None) -> dict:
    match = [c for c in active_comments(db) if c["comment_id"] == comment_id]
    if not match:
        raise ValueError("comment not found (already deleted?)")
    c = match[0]
    row = {"ts": ts or datetime.now().isoformat(timespec="seconds"), "user": user, "action": "delete",
           "comment_id": comment_id, "acq_id": c["acq_id"], "image_key": c["image_key"], "text": ""}
    _insert(db, row)
    return row


def export_csv(db: sqlite3.Connection) -> bytes:
    """The complete log as CSV (UTF-8 with BOM, opens in Excel)."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COMMENT_COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in _rows(db):
        w.writerow({c: r[c] or "" for c in COMMENT_COLUMNS})
    return ("﻿" + buf.getvalue()).encode("utf-8")


def restore_if_empty(db: sqlite3.Connection, csv_path: str) -> int:
    """Import a comments.csv log into an empty table (recovery after losing the local DB)."""
    if db.execute("SELECT count(*) FROM comments").fetchone()[0] or not os.path.exists(csv_path):
        return 0
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("acq_id") and r.get("action") in ("add", "delete")]
    db.executemany(f"INSERT INTO comments VALUES ({','.join('?' * len(COMMENT_COLUMNS))})",
                   [tuple(r.get(c, "") or "" for c in COMMENT_COLUMNS) for r in rows])
    db.commit()
    return len(rows)
