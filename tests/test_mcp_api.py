"""Tests of the read API (mdcat/query.py), the server's JSON endpoints and the MCP rules
(``X-MDCAT-Agent: mcp`` -> "<user>_mcp", comments only, delete only own comments).

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v

Runs a real catalog HTTP server on a free localhost port with a temporary working copy and
catalog folder (no builds, nothing outside the temporary folder is touched).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import mcp_server  # noqa: E402
import serve_catalog as sc  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mdcat import comments as cm  # noqa: E402
from mdcat import config as mcfg  # noqa: E402
from mdcat import inbox, safety  # noqa: E402
from mdcat import query as qy  # noqa: E402

ACQ_COLS = ["acq_id", "status", "category", "kind", "owner", "instrument", "session", "name", "format", "path",
            "acquired_start", "total_time_s", "dims_TCZYX", "max_T", "max_Z", "max_C", "objective", "magnification",
            "na", "pixel_size_um", "z_step_um", "time_delta_s", "channels", "size_bytes", "has_original", "has_zarr",
            "has_segmentation", "has_tables", "has_rois", "has_processed", "has_exports", "has_metadata_files",
            "segmentation_names", "table_names", "notes"]
DATASETS = [
    dict(acq_id="A1", status="ok", category="raw", kind="timelapse", owner="self", instrument="Mica_Confocal",
         session="20251204", name="LMNB_CHIR_day1", format="lif", path="Mica_Confocal/20251204/a.lif",
         acquired_start="2025-12-04T13:11:24", total_time_s=57707.0, dims_TCZYX="65x2x15x100x100", max_T=65,
         max_Z=15, max_C=2, objective="HC PL APO 20x/0.75", magnification="20", na="0.75", channels="C0, C1",
         size_bytes=4000, has_original=1, has_zarr=1, has_segmentation=1),
    dict(acq_id="A2", status="ok", category="raw", kind="static", owner="self", instrument="Olympus_SpinningDisk",
         session="20260101", name="patterns_dapi", format="vsi", path="Olympus/20260101/b.vsi",
         acquired_start="2026-01-01T10:00:00", dims_TCZYX="1x3x30x100x100", max_T=1, max_Z=30, max_C=3,
         objective="UPLSAPO 60x/1.30", magnification="60", size_bytes=9000, has_original=1, has_zarr=0),
    dict(acq_id="A3", status="missing", category="raw", kind="static", owner="other", instrument="SP8_Confocal",
         session="20250101", name="old_gone", format="lif", path="SP8/20250101/c.lif",
         acquired_start="2025-01-01T09:00:00", max_T=1, max_Z=1, max_C=1, size_bytes=10),
]


def make_db(path: str, me_mcp: str) -> None:
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE acquisitions ({', '.join(ACQ_COLS)})")
    for d in DATASETS:
        db.execute(f"INSERT INTO acquisitions ({', '.join(d)}) VALUES ({','.join('?' * len(d))})", list(d.values()))
    db.execute("CREATE TABLE images (acq_id, image_key, kind, T, C, Z, Y, X, fiji_series)")
    db.execute("INSERT INTO images VALUES ('A1', 'img0', 'image', 65, 2, 15, 100, 100, '1')")
    db.execute("CREATE TABLE locations (acq_id, rel, role, format, size_bytes, status, error)")
    db.execute("INSERT INTO locations VALUES ('A1', 'Mica_Confocal/20251204/a.lif', 'original', 'lif', 4000, 'ok', '')")
    db.execute("CREATE TABLE scenes (acq_id, file, series, fiji, name, image_key, role)")
    db.executemany("INSERT INTO scenes VALUES ('A1', 'a.lif', ?, ?, 's', 'img0', 'image')",
                   [(i, i + 1) for i in range(5)])
    db.execute("CREATE TABLE needs_input (acq_id, image_key, field, instrument, session, name, path, hint)")
    db.execute("CREATE TABLE runs (started, finished, tool_version)")
    db.execute("INSERT INTO runs VALUES ('2026-11-01T10:00:00', '2026-11-01T10:05:00', '2.0.0')")
    cm.ensure_table(db)
    cm.add_comment(db, "A1", "nuclei drift after frame 40", "alice", comment_id="aaaaaaaa")
    cm.add_comment(db, "A2", "deleted remark", "bob", comment_id="bbbbbbbb")
    cm.delete_comment(db, "bbbbbbbb", "bob")
    cm.add_comment(db, "A2", "auto check: focus ok", me_mcp, comment_id="cccccccc")
    db.commit()
    db.close()


class QueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.path = os.path.join(cls.tmp, "catalog.sqlite")
        make_db(cls.path, "me_mcp")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.db = qy.connect(self.path)

    def tearDown(self):
        self.db.close()

    def test_connection_is_read_only(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.db.execute("DELETE FROM comments")

    def test_overview(self):
        o = qy.overview(self.db)
        self.assertEqual(o["n_datasets"], 3)
        self.assertEqual(o["n_comments"], 2)  # bob's comment was deleted
        self.assertEqual(o["instrument"]["Mica_Confocal"], 1)
        self.assertIn("acq_id", o["tables"]["acquisitions"])

    def test_search_filters(self):
        s = lambda **p: [r["acq_id"] for r in qy.search_datasets(self.db, p)["rows"]]  # noqa: E731
        self.assertEqual(s(), ["A2", "A1"])                       # missing excluded, newest first
        self.assertEqual(s(include_missing=True), ["A2", "A1", "A3"])
        self.assertEqual(s(text="chir lmnb"), ["A1"])             # all words, any order
        self.assertEqual(s(text="drift"), ["A1"])                 # found via a comment
        self.assertEqual(s(text="deleted"), [])                   # deleted comment is not searched
        self.assertEqual(s(instrument="mica_confocal"), ["A1"])
        self.assertEqual(s(has_zarr="0"), ["A2"])
        self.assertEqual(s(min_Z=20), ["A2"])
        self.assertEqual(s(magnification=60), ["A2"])
        self.assertEqual(s(objective="apo 20x"), ["A1"])
        self.assertEqual(s(date_from="2025-12-01", date_to="2025-12-31"), ["A1"])
        self.assertEqual(s(has_comments="1"), ["A2", "A1"])
        self.assertEqual(s(sort="size_desc"), ["A2", "A1"])
        self.assertEqual(s(status="missing"), ["A3"])
        r = qy.search_datasets(self.db, {"limit": 1})
        self.assertEqual((r["total"], len(r["rows"])), (2, 1))

    def test_get_dataset(self):
        d = qy.get_dataset(self.db, "A1", max_scenes=2)
        self.assertEqual(d["dataset"]["name"], "LMNB_CHIR_day1")
        self.assertEqual((d["n_scenes"], len(d["scenes"])), (5, 2))
        self.assertEqual([c["comment_id"] for c in d["comments"]], ["aaaaaaaa"])
        self.assertIsNone(qy.get_dataset(self.db, "nope"))

    def test_search_comments(self):
        self.assertEqual(qy.search_comments(self.db, {})["total"], 2)
        r = qy.search_comments(self.db, {"user": "ALICE"})
        self.assertEqual(r["rows"][0]["name"], "LMNB_CHIR_day1")

    def test_sql_only_select(self):
        r = qy.run_sql(self.db, "SELECT acq_id FROM acquisitions ORDER BY acq_id", limit=2)
        self.assertEqual((r["rows"], r["truncated"]), ([["A1"], ["A2"]], True))
        for bad in ("DELETE FROM comments", "PRAGMA table_info(comments)", "ATTACH 'x.db' AS x",
                    "SELECT 1; SELECT 2", "CREATE TABLE t(x)", ""):
            with self.subTest(bad), self.assertRaises(ValueError):
                qy.run_sql(self.db, bad)
        self.assertEqual(qy.run_sql(self.db, "SELECT count(*) FROM acquisitions")["rows"], [[3]])  # authorizer reset

    def test_comment_author(self):
        pending = [{"action": "comment_add", "id": "dddddddd", "user": "x_mcp", "acq_id": "A1"},
                   {"action": "comment_delete", "comment_id": "aaaaaaaa", "user": "alice"}]
        self.assertEqual(qy.comment_author(self.db, "cccccccc", pending), {"user": "me_mcp", "acq_id": "A2"})
        self.assertEqual(qy.comment_author(self.db, "dddddddd", pending)["user"], "x_mcp")
        self.assertIsNone(qy.comment_author(self.db, "aaaaaaaa", pending))   # deletion pending
        self.assertIsNone(qy.comment_author(self.db, "bbbbbbbb", pending))   # deleted


class StubBuilder:
    def __init__(self):
        self.requests = []

    def request(self, reason):
        self.requests.append(reason)
        return 1

    def state(self):
        return {}


class ServerTests(unittest.TestCase):
    """The real HTTP handler; the Windows user is identified from the TCP connection."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        work, lab = os.path.join(cls.tmp, "work"), os.path.join(cls.tmp, "lab")
        cat = os.path.join(lab, "Catalog")
        os.makedirs(work)
        os.makedirs(lab)
        cfg = mcfg.Config(out_dir=work, work_dir=work)            # not configured: no builder/supervisor threads
        cls.app = sc.App(cfg, None)
        cfg.labdata_root, cfg.catalog_dir = lab, cat
        safety.configure(lab, work)
        safety.configure_publish(cat)
        cls.app.status, cls.app.builder = "ok", StubBuilder()
        cls.me = cls.app.me
        make_db(cfg.db_path, cls.me + "_mcp")
        cls.httpd = sc.Server(("127.0.0.1", 0), sc.BaseHTTPRequestHandler)
        cls.port = cls.httpd.server_address[1]
        cls.httpd.RequestHandlerClass = sc.make_handler(cls.app, cls.port)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        mcp_server.PORT = cls.port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _human_post(self, path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
                                     method="POST", headers={"X-MDCAT": "1", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _agent_post(self, path, body, agent="mcp"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
                                     method="POST", headers={"X-MDCAT": "1", "X-MDCAT-Agent": agent,
                                                             "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _pending(self):
        return inbox.pending(self.app.cfg.publish_dir)

    def test_read_tools(self):
        o = mcp_server.catalog_overview()
        self.assertEqual((o["n_datasets"], o["server_status"]), (3, "ok"))
        self.assertEqual([r["acq_id"] for r in mcp_server.search_datasets(text="drift")["rows"]], ["A1"])
        self.assertEqual([r["acq_id"] for r in mcp_server.search_datasets(has_zarr=False)["rows"]], ["A2"])
        d = mcp_server.get_dataset("A1", max_scenes=1)
        self.assertEqual((d["n_scenes"], len(d["scenes"])), (5, 1))
        self.assertEqual(mcp_server.sql_query("SELECT count(*) n FROM scenes")["rows"], [[5]])
        with self.assertRaisesRegex(ToolError, "only SELECT"):
            mcp_server.sql_query("DELETE FROM comments")
        with self.assertRaisesRegex(ToolError, "unknown dataset"):
            mcp_server.get_dataset("nope")

    def test_add_and_delete_own_comment(self):
        e = mcp_server.add_comment("A1", "  checked by agent  ")["entry"]
        self.assertEqual((e["user"], e["text"], e["action"]), (self.me + "_mcp", "checked by agent", "comment_add"))
        self.assertTrue(any(p["id"] == e["id"] for p in self._pending()))
        self.assertTrue(self.app.builder.requests)                  # edit starts a build
        d = mcp_server.get_dataset("A1")
        self.assertIn(e["id"], [p.get("id") for p in d["pending_comments"]])
        self.assertIn(e["id"], [p.get("id") for p in mcp_server.search_comments(acq_id="A1")["pending"]])
        # delete the pending comment, then the already applied one of the same _mcp user
        de = mcp_server.delete_own_comment(e["id"])["entry"]
        self.assertEqual((de["action"], de["acq_id"], de["user"]), ("comment_delete", "A1", self.me + "_mcp"))
        with self.assertRaisesRegex(ToolError, "not found"):
            mcp_server.delete_own_comment(e["id"])                  # deletion already pending
        self.assertEqual(mcp_server.delete_own_comment("cccccccc")["entry"]["acq_id"], "A2")

    def test_build_applies_mcp_edits(self):
        """The inbox entries of the MCP are applied by a build like the browser's (in time order)."""
        keep = mcp_server.add_comment("A2", "stays")["entry"]["id"]
        gone = mcp_server.add_comment("A2", "goes again")["entry"]["id"]
        mcp_server.delete_own_comment(gone)
        mine = [e for e in self._pending() if keep in (e.get("id"), e.get("comment_id"))
                or gone in (e.get("id"), e.get("comment_id"))]
        self.assertEqual(len(mine), 3)
        work = self.app.cfg.out_dir
        copy = os.path.join(work, "apply_test.sqlite")
        shutil.copy(self.app.cfg.db_path, copy)
        db = sqlite3.connect(copy)
        inbox.ensure_table(db)
        res = inbox.apply(db, os.path.join(work, "manual_overrides.csv"), mine, os.path.join(work, "audit.log"),
                          lambda m: None)
        self.assertEqual([r[1] for r in res], ["applied"] * 3, res)
        active = {c["comment_id"]: c for c in cm.active_comments(db)}
        db.close()
        self.assertEqual(active[keep]["user"], self.me + "_mcp")
        self.assertNotIn(gone, active)

    def test_agent_cannot_delete_others(self):
        with self.assertRaisesRegex(ToolError, "only delete its own"):
            mcp_server.delete_own_comment("aaaaaaaa")
        with self.assertRaisesRegex(ToolError, "not found"):
            mcp_server.delete_own_comment("bbbbbbbb")

    def test_agent_restricted_to_comments(self):
        for path, body in (("/api/value", {"acq_id": "A1", "field": "na", "value": "1.2"}),
                           ("/api/rebuild", {}), ("/api/setup", {}), ("/api/check", {})):
            with self.subTest(path):
                code, r = self._agent_post(path, body)
                self.assertEqual(code, 403)
        code, _ = self._agent_post("/api/comment", {"acq_id": "A1", "text": "x"}, agent="other")
        self.assertEqual(code, 403)
        with self.assertRaisesRegex(ToolError, "unknown dataset"):
            mcp_server.add_comment("nope", "text")
        with self.assertRaisesRegex(ToolError, "empty comment"):
            mcp_server.add_comment("A1", "   ")

    def test_human_unchanged(self):
        code, r = self._human_post("/api/comment", {"acq_id": "A2", "text": "human note"})
        self.assertEqual((code, r["entry"]["user"]), (200, self.me))
        code, r = self._human_post("/api/sql", {"sql": "SELECT 1"})
        self.assertEqual((code, r["rows"]), (200, [[1]]))

    def test_get_requires_local_host_header(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/overview", headers={"Host": "evil.com"})
        with self.assertRaises(urllib.error.HTTPError) as cm_:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(cm_.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
