"""Catalog server: the catalog page with editing (missing values, comments, links) + setup.

    .venv\\Scripts\\pythonw.exe serve_catalog.py [--open]     (Start Catalog.cmd does this)

* Listens on http://localhost:<server_port> - reachable on this machine only. Only the Windows
  user who runs the server may edit; other users of the machine (RDP) can only view.
* First start (no settings.toml): the page /setup asks for the Labdata folder and the catalog
  folder; saving restarts the server with the new settings.
* The catalog folder is the master copy. The server works on a local copy (.work), pulls the
  catalog folder when it is newer and copies every change back within seconds (each build).
* Only one server at a time maintains a catalog folder (server.lock there, heartbeat every
  2 min). A second server shows the catalog read-only; it takes over when the lock is
  older than 10 minutes (other machine off / server stopped).
* Network drive / VPN not available: shows the last local copy read-only and retries every
  minute.
* Runs an incremental update every ``auto_update_min`` minutes and makes preview images in a
  separate process (make_previews.py).
* Never writes below the Labdata root except into the catalog folder.
"""

from __future__ import annotations

import argparse
import atexit
import getpass
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mdcat import comments as cm  # noqa: E402
from mdcat import config as mcfg  # noqa: E402
from mdcat import lease, locks, safety, setup_page, sync, winuser  # noqa: E402
from mdcat import overrides as ov  # noqa: E402

INJECT_BEFORE = b'<script type="application/json" id="data">'
CREATE_NO_WINDOW = 0x08000000
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
STARTUP_LNK = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup",
                           "Microscopy Catalog Server.lnk")
EXIT_MSG = {2: "not set up", 4: "Labdata folder not reachable", 5: "catalog folder is maintained by another server"}


class Server(ThreadingHTTPServer):
    """Binds the port exclusively. The stdlib default (SO_REUSEADDR) lets a second program bind
    the same port on Windows - then two servers run and the browser reaches either of them."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def pythonw() -> str:
    p = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return p if os.path.exists(p) else sys.executable


class Builder:
    """Runs build_catalog.py in a subprocess; requests that arrive while it runs are coalesced."""

    def __init__(self, app, config_arg: str | None):
        self.app, self.config_arg = app, config_arg
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.pending = False
        self.running = False
        self.generation = 0
        self.last_ok: bool | None = None
        self.last_error = ""
        self.last_finished = ""
        self.last_reason = ""
        self.tail: list[str] = []
        threading.Thread(target=self._loop, daemon=True, name="builder").start()
        if app.cfg.auto_update_min and app.cfg.auto_update_min > 0:
            threading.Thread(target=self._timer, daemon=True, name="auto-update").start()

    def request(self, reason: str) -> int:
        with self.lock:
            self.pending = True
            self.last_reason = reason
            gen = self.generation
        self.wake.set()
        return gen

    def state(self) -> dict:
        with self.lock:
            return {"running": self.running, "pending": self.pending, "last_ok": self.last_ok,
                    "last_error": self.last_error, "last_finished": self.last_finished, "tail": self.tail[-15:]}

    def _timer(self):
        while True:
            time.sleep(self.app.cfg.auto_update_min * 60)
            if self.app.status == "ok":
                self.request("scheduled update")

    def _loop(self):
        while True:
            self.wake.wait()
            time.sleep(1.0)  # collect edits that arrive together
            with self.lock:
                self.wake.clear()
                if not self.pending or self.app.status != "ok":
                    continue  # offline / read-only: the request is repeated when the server is back online
                self.pending = False
                self.running = True
                reason = self.last_reason
            self.app.log(f"build started ({reason})")
            cmd = [sys.executable, os.path.join(HERE, "build_catalog.py"), "--no-previews"]
            if self.config_arg:
                cmd += ["--config", self.config_arg]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                      creationflags=CREATE_NO_WINDOW, cwd=HERE)
                lines = (proc.stdout + proc.stderr).splitlines()
                code = proc.returncode
            except OSError as exc:
                lines, code = [str(exc)], -1
            with self.lock:
                self.running = False
                self.tail = lines[-40:]
                if code == 3:  # another build (e.g. started by hand) is running - try again shortly
                    self.pending = True
                    threading.Timer(15, self.wake.set).start()
                    self.app.log("build busy (another build running) - retrying in 15 s")
                    continue
                self.last_ok = code == 0
                self.last_error = "" if code == 0 else (lines[-1] if lines else EXIT_MSG.get(code, f"exit code {code}"))
                self.last_finished = datetime.now().isoformat(timespec="seconds")
                self.generation += 1
            self.app.log(f"build finished: {'ok' if code == 0 else 'FAILED: ' + self.last_error}")
            if code in (4, 5):
                self.app.supervise_now()


class Previewer:
    """Runs make_previews.py (own lock, never blocks builds) after start and every auto_update_min;
    when new previews were made, a quick build links + publishes them."""

    def __init__(self, app, config_arg):
        self.app, self.config_arg = app, config_arg
        self.running = False
        self.last = ""
        if app.cfg.previews_on:
            threading.Thread(target=self._loop, daemon=True, name="previews").start()

    def _loop(self):
        time.sleep(90)  # let the start-up build finish first
        while True:
            if self.app.status == "ok":
                self.run_once()
            time.sleep(max(60.0, (self.app.cfg.auto_update_min or 60) * 60))

    def run_once(self):
        cmd = [sys.executable, os.path.join(HERE, "make_previews.py")]
        if self.config_arg:
            cmd += ["--config", self.config_arg]
        self.running = True
        self.app.log("previews started")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  creationflags=CREATE_NO_WINDOW, cwd=HERE)
            lines = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("PREVIEWS ")]
            self.last = lines[-1] if lines else f"exit code {proc.returncode}"
        except OSError as exc:
            self.last = str(exc)
        finally:
            self.running = False
        self.app.log(f"previews finished: {self.last}")
        if "made=" in self.last and "made=0 " not in self.last:
            self.app.builder.request("new previews")


class App:
    def __init__(self, cfg, config_arg):
        self.cfg, self.config_arg = cfg, config_arg
        self.me = getpass.getuser()
        self.p_over = os.path.join(cfg.out_dir, "manual_overrides.csv")
        self.audit_path = os.path.join(cfg.out_dir, "logs", "web_edits.log")
        self.server_log = os.path.join(cfg.out_dir, "logs", "server.log")
        self.actions = {"/api/value": self.set_value, "/api/comment": self.add_comment,
                        "/api/comment/delete": self.delete_comment}
        self.status = "ok" if cfg.configured else "setup"   # setup | offline | readonly | ok
        self.status_msg = "" if cfg.configured else "not set up yet"
        self.holder: dict | None = None
        self.owns_lock = False
        self.last_beat = 0.0
        self.httpd: Server | None = None
        self._wake = threading.Event()
        if cfg.configured:
            self.status, self.status_msg = "starting", "starting - checking the Labdata and catalog folders"
            self.builder = Builder(self, config_arg)
            self.previewer = Previewer(self, config_arg)
            threading.Thread(target=self._supervise_loop, daemon=True, name="supervisor").start()
        else:
            self.builder = self.previewer = None

    # --- online / offline / read-only ------------------------------------------------------
    def supervise_now(self):
        self._wake.set()

    def _supervise_loop(self):
        while True:
            try:
                self.supervise()
            except Exception as exc:  # noqa: BLE001 - never let the supervisor die
                self.log(f"supervisor error: {exc}")
            self._wake.wait(60)
            self._wake.clear()

    def _set(self, status: str, msg: str):
        if (status, msg) != (self.status, self.status_msg):
            self.log(f"status: {status} - {msg}")
        self.status, self.status_msg = status, msg

    def supervise(self):
        cfg = self.cfg
        if not os.path.isdir(cfg.labdata_root):
            self.owns_lock = False
            return self._set("offline", f"Labdata folder {cfg.labdata_root} not reachable (network drive / VPN?) - "
                                        f"showing the last local copy, retrying every minute")
        try:
            safety.configure_publish(cfg.publish_dir)
            if self.owns_lock:
                if time.time() - self.last_beat < lease.HEARTBEAT_S:
                    return
                holder = lease.heartbeat(cfg.publish_dir)
            else:
                holder = lease.acquire(cfg.publish_dir)
        except (OSError, RuntimeError) as exc:
            self.owns_lock = False
            return self._set("offline", f"catalog folder not usable ({exc}) - retrying every minute")
        if holder:
            self.owns_lock, self.holder = False, holder
            return self._set("readonly", f"read-only: the catalog folder is maintained by the server of "
                                         f"{lease.describe(holder)}. This server takes over when that one has "
                                         f"been stopped for 10 minutes.")
        self.last_beat = time.time()
        if not self.owns_lock:
            self.owns_lock, self.holder = True, None
            self._set("ok", f"catalog folder {cfg.publish_dir}")
            self.builder.request("server start" if self.builder.generation == 0 else "back online")

    def release(self):
        if self.owns_lock:
            try:
                lease.release(self.cfg.publish_dir)
            except (OSError, RuntimeError):
                pass
            self.owns_lock = False

    # --- logging ---------------------------------------------------------------------------
    def log(self, msg: str):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
        print(line, flush=True)
        try:
            with safety.open_write(self.server_log, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def audit(self, user: str, action: str, payload: dict):
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "user": user, "action": action, **payload}
        with safety.open_write(self.audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def is_owner(self, user: str | None) -> bool:
        return bool(user) and user.lower() == self.me.lower()

    def can_edit(self, user: str | None) -> bool:
        return self.status == "ok" and self.is_owner(user)

    def known_acq(self, acq_id: str) -> bool:
        db = sqlite3.connect(f"file:{self.cfg.db_path}?mode=ro", uri=True, timeout=30)
        try:
            return db.execute("SELECT 1 FROM acquisitions WHERE acq_id=?", (acq_id,)).fetchone() is not None
        finally:
            db.close()

    # --- edit actions (each returns a short description for the audit log) ---
    def set_value(self, user: str, b: dict) -> str:
        acq, field, value = str(b.get("acq_id", "")), str(b.get("field", "")), str(b.get("value", ""))
        image_key = str(b.get("image_key", ""))
        if not self.known_acq(acq):
            raise ValueError(f"unknown dataset {acq!r}")
        with locks.edit_lock(self.cfg):
            if value.strip():
                ov.set_override(self.p_over, acq, image_key, field, value,
                                note=f"web {user} {datetime.now():%Y-%m-%d %H:%M}")
                return f"set {field}={value!r}"
            removed = ov.remove_override(self.p_over, acq, image_key, field, str(b.get("old_value", "")))
            if not removed:
                raise ValueError("value not found (already removed?)")
            return f"removed {field}"

    def add_comment(self, user: str, b: dict) -> str:
        acq = str(b.get("acq_id", ""))
        if not self.known_acq(acq):
            raise ValueError(f"unknown dataset {acq!r}")
        db = cm.connect(self.cfg.db_path)
        try:
            row = cm.add_comment(db, acq, str(b.get("text", "")), user, str(b.get("image_key", "")))
        finally:
            db.close()
        return f"comment {row['comment_id']}"

    def delete_comment(self, user: str, b: dict) -> str:
        db = cm.connect(self.cfg.db_path)
        try:
            cm.delete_comment(db, str(b.get("comment_id", "")), user)
        finally:
            db.close()
        return f"deleted comment {b.get('comment_id')}"

    # --- setup -----------------------------------------------------------------------------
    def setup_state(self) -> dict:
        cur = {k: getattr(self.cfg, k) for k in mcfg.SETUP_KEYS}
        return {"settings": cur, "configured": self.cfg.configured, "status": self.status,
                "status_msg": self.status_msg, "autostart": os.path.exists(STARTUP_LNK),
                "program_dir": HERE, "settings_path": self.cfg.config_path, "me": self.me,
                "java": mcfg.java_status(self.cfg)}

    def check_folders(self, b: dict) -> dict:
        lab = str(b.get("labdata_root", "")).strip()
        cat = str(b.get("catalog_dir", "")).strip()
        esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;")  # noqa: E731
        if not lab:
            li = ""
        elif os.path.isdir(lab):
            li = '<span class="ok">reachable</span>'
        else:
            li = '<span class="bad">not reachable (network drive connected? VPN?)</span>'
        if not cat:
            return {"labdata": li, "catalog": ""}
        problem = safety.check_catalog_dir(cat, lab)
        if problem:
            ci = f'<span class="bad">{esc(problem)}</span>'
        elif os.path.exists(os.path.join(cat, "catalog.sqlite")):
            st = sync.share_state(type("C", (), {"publish_dir": cat})())
            h = lease.read(cat)
            when = (f' (last written by {esc(st["host"])} {esc(str(st.get("time", "")).replace("T", " "))})'
                    if st.get("host") else "")
            ci = f'<span class="ok">existing catalog found</span>{when} – it will be used'
            if h and lease.other_holder(cat):
                ci += f'<br><span class="warn">currently maintained by the server of {esc(lease.describe(h))} – ' \
                      f'this one will be read-only until that one stops</span>'
        elif os.path.isdir(cat):
            ci = ('<span class="warn">empty folder – a new catalog is built: the first run reads the metadata '
                  'of all files (hours for a large Labdata folder)</span>')
        else:
            ci = ('<span class="warn">new folder (will be created) – the first run reads the metadata of all files '
                  '(hours for a large Labdata folder)</span>')
        return {"labdata": li, "catalog": ci}

    def save_setup(self, b: dict) -> str:
        lab = str(b.get("labdata_root", "")).strip().rstrip("\\/") or ""
        cat = str(b.get("catalog_dir", "")).strip().rstrip("\\/") or ""
        if len(lab) == 2 and lab[1] == ":":
            lab += "\\"
        if not lab or not os.path.isdir(lab):
            raise ValueError(f"Labdata folder not reachable: {lab!r}")
        problem = safety.check_catalog_dir(cat, lab)
        if problem:
            raise ValueError(f"catalog folder: {problem}")
        prev = str(b.get("previews", "network"))
        if prev not in mcfg.PREVIEW_MODES:
            raise ValueError("unknown preview mode")
        try:
            upd = max(0.0, float(b.get("auto_update_min", 60)))
            budget = max(1.0, float(b.get("preview_budget_min", 20)))
        except (TypeError, ValueError):
            raise ValueError("minutes must be numbers") from None
        mcfg.write_settings({"labdata_root": lab, "catalog_dir": cat, "auto_update_min": upd,
                             "previews": prev, "preview_budget_min": budget}, self.config_arg)
        autostart(bool(b.get("autostart")))
        return f"settings saved (labdata_root={lab}, catalog_dir={cat}, autostart={bool(b.get('autostart'))})"

    def restart(self):
        """Start a fresh server process (new settings) and stop this one."""
        self.log("restarting with new settings")
        self.release()
        cmd = [pythonw(), os.path.join(HERE, "serve_catalog.py"), "--wait-port"]
        if self.config_arg:
            cmd += ["--config", self.config_arg]
        subprocess.Popen(cmd, cwd=HERE, creationflags=DETACHED, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self.httpd:
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()


def autostart(on: bool):
    """Create / remove the shortcut in the Startup folder (runs pythonw serve_catalog.py)."""
    if not on:
        if os.path.exists(STARTUP_LNK):
            os.remove(STARTUP_LNK)
        return
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:MDCAT_LNK);"
          "$s.TargetPath=$env:MDCAT_EXE;$s.Arguments='\"'+$env:MDCAT_SCRIPT+'\"';"
          "$s.WorkingDirectory=$env:MDCAT_DIR;$s.Description='Microscopy catalog server';$s.Save()")
    env = dict(os.environ, MDCAT_LNK=STARTUP_LNK, MDCAT_EXE=pythonw(),
               MDCAT_SCRIPT=os.path.join(HERE, "serve_catalog.py"), MDCAT_DIR=HERE)
    r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], env=env,
                       capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=60)
    if r.returncode != 0:
        raise ValueError(f"could not create the autostart shortcut: {r.stderr.strip()[:300]}")


PICK_PS = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object System.Windows.Forms.Form
$f.TopMost = $true; $f.ShowInTaskbar = $false; $f.StartPosition = 'CenterScreen'; $f.Size = '1,1'
$f.Show(); $f.Activate()
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = 'Microscopy catalog: choose a folder'
$d.ShowNewFolderButton = $true
if ($env:MDCAT_INITIAL -and (Test-Path -LiteralPath $env:MDCAT_INITIAL)) { $d.SelectedPath = $env:MDCAT_INITIAL }
if ($d.ShowDialog($f) -eq 'OK') { [Console]::Out.Write($d.SelectedPath) }
$f.Close()
"""


def pick_folder(initial: str) -> str:
    env = dict(os.environ, MDCAT_INITIAL=initial or "")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", PICK_PS],
                       env=env, capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=900)
    return r.stdout.decode("utf-8", errors="replace").strip()


def make_handler(app: App, port: int):
    allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    actions = app.actions

    class Handler(BaseHTTPRequestHandler):
        server_version = "mdcat"

        def log_message(self, fmt, *args):  # quiet console
            pass

        def _user(self) -> str | None:
            return winuser.connection_user(self.client_address[1], port)

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict):
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:  # blocks DNS-rebinding style access from other origins
            return self.headers.get("Host", "") in allowed_hosts

        def _page(self) -> bytes | None:
            cands = [os.path.join(app.cfg.out_dir, "catalog.html")]
            if app.status == "readonly":  # the owning server keeps the catalog folder current
                cands.insert(0, os.path.join(app.cfg.publish_dir, "index.html"))
            for p in cands:
                try:
                    with open(p, "rb") as fh:
                        return fh.read()
                except OSError:
                    continue
            return None

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            path = self.path.split("?", 1)[0]
            if path == "/setup" or (path in ("/", "/index.html") and app.status == "setup"):
                if path != "/setup":
                    return self._send(302, b"", "text/plain", {"Location": "/setup"})
                return self._send(200, setup_page.render(app.setup_state()), "text/html; charset=utf-8")
            if path in ("/", "/index.html"):
                page = self._page()
                if page is None:
                    msg = (f"The catalog is being built for the first time ({app.status_msg}). "
                           f"Reload in a minute - or open http://localhost:{port}/setup")
                    return self._send(503, msg.encode("utf-8"), "text/plain; charset=utf-8")
                user = self._user()
                info = {"user": user or "", "can_edit": app.can_edit(user), "status": app.status,
                        "status_msg": app.status_msg, "owner": app.me}
                inject = b"<script>window.MDCAT_SERVER=" + json.dumps(info).encode("utf-8") + b";</script>\n"
                page = page.replace(INJECT_BEFORE, inject + INJECT_BEFORE, 1)
                return self._send(200, page, "text/html; charset=utf-8")
            m = re.fullmatch(r"/previews/([0-9a-f]{16}\.jpg)", path)
            if m:
                dirs = [os.path.join(app.cfg.out_dir, "previews")]
                if app.cfg.publish_dir:
                    dirs.append(os.path.join(app.cfg.publish_dir, "previews"))
                for d in dirs:
                    try:
                        with open(os.path.join(d, m.group(1)), "rb") as fh:
                            return self._send(200, fh.read(), "image/jpeg")
                    except OSError:
                        continue
                return self._send(404, b"not found", "text/plain")
            if path == "/api/state":
                user = self._user()
                b = app.builder
                return self._json(200, {"user": user or "", "can_edit": app.can_edit(user), "status": app.status,
                                        "status_msg": app.status_msg,
                                        "generation": b.generation if b else 0, "build": b.state() if b else {},
                                        "previews": {"running": app.previewer.running, "last": app.previewer.last}
                                        if app.previewer else {}})
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            # custom header + JSON content type -> browsers send a CORS preflight for cross-site
            # requests, which this server never answers, so other web pages cannot post here
            if (not self._host_ok() or self.headers.get("X-MDCAT") != "1"
                    or not self.headers.get("Content-Type", "").startswith("application/json")):
                return self._json(403, {"ok": False, "error": "forbidden"})
            user = self._user()
            if not app.is_owner(user):
                return self._json(403, {"ok": False, "error": f"only {app.me} (who runs this server) may change "
                                                              f"things here - you are {user or '?'}"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(min(n, 100_000)) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._json(400, {"ok": False, "error": "invalid JSON"})
            try:
                if path == "/api/check":
                    return self._json(200, {"ok": True, **app.check_folders(body)})
                if path == "/api/pickfolder":
                    return self._json(200, {"ok": True, "path": pick_folder(str(body.get("initial", "")))})
                if path == "/api/setup":
                    what = app.save_setup(body)
                    app.log(f"{user}: {what}")
                    self._json(200, {"ok": True, "result": what})
                    threading.Timer(0.5, app.restart).start()
                    return None
                if not app.can_edit(user):
                    return self._json(403, {"ok": False, "error": f"editing is not possible now: {app.status_msg}"})
                if path == "/api/rebuild":
                    what = "rebuild requested"
                elif path in actions:
                    what = actions[path](user, body)
                else:
                    return self._json(404, {"ok": False, "error": "unknown action"})
            except (ValueError, TimeoutError, subprocess.TimeoutExpired, OSError) as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            app.audit(user, path, {"result": what, **{k: v for k, v in body.items() if k != "text"},
                                   **({"text": body["text"]} if "text" in body else {})})
            app.log(f"{user}: {path} {what}")
            gen = app.builder.request(f"edit by {user}")
            return self._json(200, {"ok": True, "result": what, "generation": gen})

        def do_OPTIONS(self):  # no CORS - refuse preflights
            self._send(403, b"", "text/plain")

    return Handler


def catalog_answers(port: int) -> bool:
    """Is the program listening on ``port`` this catalog server?"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=3) as r:
            return r.headers.get("Server", "").startswith("mdcat") and "status" in json.load(r)
    except Exception:  # noqa: BLE001
        return False


def show_error(msg: str) -> None:
    """Message box (the server runs without a console under pythonw)."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "Microscopy catalog", 0x10)
    except Exception:  # noqa: BLE001
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="settings file (default: settings.toml in the program folder)")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    ap.add_argument("--wait-port", action="store_true", help="retry binding the port for 30 s (restart)")
    args = ap.parse_args(argv)
    cfg = mcfg.load_config(args.config)
    port = args.port or cfg.server_port
    if cfg.configured:
        safety.configure(cfg.labdata_root, cfg.out_dir)
    else:  # setup mode: only the working directory (logs) is written
        safety.configure(os.path.join(cfg.out_dir, "_no_labdata_yet_"), cfg.out_dir)
    safety.makedirs(os.path.join(cfg.out_dir, "logs"))
    app = App(cfg, args.config)
    httpd = None
    for attempt in range(30 if args.wait_port else 1):
        try:
            httpd = Server(("127.0.0.1", port), make_handler(app, port))
            break
        except OSError as exc:
            if attempt == (29 if args.wait_port else 0):
                if catalog_answers(port):  # "Start Catalog" while it already runs: just show the page
                    app.log(f"port {port}: the catalog server is already running")
                    if args.open:
                        webbrowser.open(f"http://localhost:{port}")
                    return 3
                app.log(f"Cannot listen on port {port} ({exc}): another program uses it. Choose another port: "
                        f"install.ps1 -Port <number> (or server_port in settings.toml)")
                if args.open:
                    show_error(f"The microscopy catalog cannot start: port {port} is used by another program.\n\n"
                               f"Run  install.ps1 -Port <number>  (e.g. 8780) to use another port.")
                return 3
            time.sleep(1)
    app.httpd = httpd
    atexit.register(app.release)
    url = f"http://localhost:{port}" + ("" if cfg.configured else "/setup")
    app.log(f"catalog server on {url} (program {HERE}, working copy {cfg.out_dir}, "
            f"catalog folder {cfg.publish_dir or '-'}, user {app.me})")
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    app.release()
    app.log("server stopped")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - under pythonw nobody sees a traceback: write it to a file
        import traceback
        d = os.path.join(mcfg.DEFAULT_WORK_DIR, "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "server_crash.log"), "a", encoding="utf-8") as fh:
            fh.write(f"--- {datetime.now().isoformat(timespec='seconds')}\n{traceback.format_exc()}\n")
        raise
