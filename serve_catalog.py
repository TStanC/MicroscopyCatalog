"""Catalog server: the catalog page with editing (missing values, comments, links) + setup.

    .venv\\Scripts\\pythonw.exe serve_catalog.py [--open]     (the "Microscopy Catalog" shortcut does this)

* Listens on http://localhost:<server_port> - reachable on this machine only. Every Windows
  user of the machine (also via RDP) can add comments and values under their own name
  (identified from the connection, mdcat/winuser.py); only the user who runs the server may
  change its settings.
* First start (no settings.toml): the page /setup asks for the Labdata folder and the catalog
  folder; saving restarts the server with the new settings.
* The catalog folder is the master copy, shared by any number of servers (machines/users).
  Edits are written into its inbox at once (mdcat/inbox.py, no lock) and shown on the page
  as "pending"; the next build - on whichever machine - applies them one after the other.
* A build holds build.lock in the catalog folder while it runs (one build at a time across
  machines); a server whose build finds the lock taken retries while edits are waiting.
* The page shown is the newest one: the catalog folder's if another machine published since
  this server's last build, else the local copy (.work).
* Network drive / VPN not available: shows the last local copy read-only and retries every
  minute.
* Updates the catalog when the last published update is older than ``auto_update_min``
  minutes (whoever notices first) and, after its own builds, makes preview images in a
  separate process (make_previews.py).
* Shows an icon in the notification area (bottom right): left click opens the catalog; right click:
  Open catalog, Update now, Settings, Stop. Colour = status (green ok, orange offline/read-only).
* Never writes below the Labdata root except into the catalog folder.
"""

from __future__ import annotations

import argparse
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
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_VENV_SP = os.path.join(HERE, ".venv", "Lib", "site-packages")
if os.path.isdir(_VENV_SP) and os.path.normcase(sys.prefix) != os.path.normcase(os.path.join(HERE, ".venv")):
    import site  # started with the base interpreter's pythonw.exe (see pythonw()): use the venv's packages
    site.addsitedir(_VENV_SP)

from mdcat import comments as cm  # noqa: E402
from mdcat import config as mcfg  # noqa: E402
from mdcat import inbox, lease, safety, setup_page, sync, winuser  # noqa: E402
from mdcat import overrides as ov  # noqa: E402

INJECT_BEFORE = b'<script type="application/json" id="data">'
CREATE_NO_WINDOW = 0x08000000
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
STARTUP_LNK = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup",
                           "Microscopy Catalog Server.lnk")
EXIT_MSG = {2: "not set up", 4: "Labdata folder not reachable", 5: "a build is running on another machine"}
BUSY_RETRY_S = 60          # build.lock taken by another machine and edits waiting: try again
CHECK_EVERY_S = 300        # how often the server checks whether an update is due


class Server(ThreadingHTTPServer):
    """Binds the port exclusively. The stdlib default (SO_REUSEADDR) lets a second program bind
    the same port on Windows - then two servers run and the browser reaches either of them."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def is_gui_exe(path: str) -> bool:
    """True if the .exe is a Windows GUI program (gets no console window), from its PE header."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
        pe = int.from_bytes(head[0x3C:0x40], "little")
        return head[pe:pe + 4] == b"PE\0\0" and int.from_bytes(head[pe + 0x5C:pe + 0x5E], "little") == 2
    except (OSError, ValueError):
        return False


def base_pythonw() -> str | None:
    """pythonw.exe of the Python the venv is based on (.python\\...), from .venv\\pyvenv.cfg."""
    try:
        with open(os.path.join(HERE, ".venv", "pyvenv.cfg"), encoding="utf-8") as fh:
            for line in fh:
                k, _, v = line.partition("=")
                if k.strip() == "home":
                    p = os.path.join(v.strip(), "pythonw.exe")
                    return p if os.path.exists(p) else None
    except OSError:
        pass
    return None


def pythonw() -> str:
    """A windowless Python for (re)starting the server. The venv's pythonw.exe normally is one,
    but on some machines it opens a console window; then the base interpreter's pythonw.exe is
    used (the server adds the venv's packages itself, see top of this file)."""
    venv_w = os.path.join(HERE, ".venv", "Scripts", "pythonw.exe")
    for p in (venv_w, base_pythonw(), os.path.join(os.path.dirname(sys.executable), "pythonw.exe")):
        if p and os.path.exists(p) and is_gui_exe(p):
            return p
    return venv_w if os.path.exists(venv_w) else sys.executable


def venv_python() -> str:
    """Interpreter for the build / preview subprocesses (started without a window)."""
    p = os.path.join(HERE, ".venv", "Scripts", "python.exe")
    return p if os.path.exists(p) else sys.executable


def detach_console() -> None:
    """Run in the background: never keep or write to a console window. Depending on how the
    server was started (PowerShell window, console python.exe), Windows hands it a console:
    the log lines then appeared there and the window stayed open. The log goes to
    .work/logs/server.log anyway."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        has_window = bool(k32.GetConsoleWindow())
    except Exception:  # noqa: BLE001
        k32, has_window = None, False
    if has_window and os.environ.get("MDCAT_BACKGROUND") != "1":
        # Started with a console window: start once more without one and quit, so the window
        # (and a venv launcher waiting for this process) closes. MDCAT_BACKGROUND marks the
        # second start - it never restarts again, whatever happens (no loop).
        env = dict(os.environ, MDCAT_BACKGROUND="1")
        subprocess.Popen([pythonw(), os.path.abspath(__file__), *sys.argv[1:]], cwd=HERE, env=env,
                         creationflags=CREATE_NO_WINDOW | 0x00000200, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sys.exit(0)
    if has_window and k32 is not None:
        try:
            k32.FreeConsole()
        except Exception:  # noqa: BLE001
            pass
    devnull = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - for the process lifetime
    sys.stdout = sys.stderr = devnull


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
        self.last_ok_epoch = 0.0
        self.last_reason = ""
        self.tail: list[str] = []
        threading.Thread(target=self._loop, daemon=True, name="builder").start()
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
        """Every few minutes: start a build if one is due (old catalog, waiting edits). With
        several servers, the first one that notices builds; the others then see a fresh catalog."""
        while True:
            time.sleep(CHECK_EVERY_S)
            if self.app.status == "ok" and not self.running and not self.pending:
                reason = self.app.build_due(edits_older_than_s=120)
                if reason:
                    self.request(reason)

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
            cmd = [venv_python(), os.path.join(HERE, "build_catalog.py"), "--no-previews"]
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
            if code == 5:  # build.lock held by another machine: it does the update
                # it may have read the inbox before our edit, or not have our newest previews: try again
                if self.app.edits_waiting() or self.app.previews_unlinked:
                    with self.lock:
                        self.pending = True
                    threading.Timer(BUSY_RETRY_S, self.wake.set).start()
                    self.app.log(f"build skipped ({lines[-1] if lines else 'catalog folder busy'}) - "
                                 f"edits waiting, retrying in {BUSY_RETRY_S} s")
                else:
                    self.app.log(f"build skipped: {lines[-1] if lines else 'catalog folder busy'}")
                self.app.supervise_now()
                continue
            with self.lock:
                self.last_ok = code == 0
                self.last_error = "" if code == 0 else (lines[-1] if lines else EXIT_MSG.get(code, f"exit code {code}"))
                self.last_finished = datetime.now().isoformat(timespec="seconds")
                if code == 0:
                    self.last_ok_epoch = time.time()
                    self.app.previews_unlinked = False
                self.generation += 1
            self.app.log(f"build finished: {'ok' if code == 0 else 'FAILED: ' + self.last_error}")
            if code == 4:
                self.app.supervise_now()


class Previewer:
    """Runs make_previews.py after every successful build of this server (and from the tray), until
    all previews are made or preview_budget_min (default 23 h) is used up. It never holds
    build.lock, so edits keep being applied meanwhile; preview.lock in the catalog folder lets one
    machine render at a time (the others skip). Every new preview is copied to the catalog folder
    at once; during a long run a build links the new ones into the page every LINK_EVERY_S, and
    once more at the end."""

    LINK_EVERY_S = 3600

    def __init__(self, app, config_arg):
        self.app, self.config_arg = app, config_arg
        self.running = False
        self.last = ""
        self.last_run_epoch = 0.0
        self._lock = threading.Lock()
        if app.cfg.previews_on:
            threading.Thread(target=self._loop, daemon=True, name="previews").start()

    def _loop(self):
        while True:
            time.sleep(60)
            b = self.app.builder
            if self.app.status == "ok" and not self.running and b.last_ok_epoch > self.last_run_epoch:
                self.run_once()

    def _newest_preview_mtime(self) -> float:
        try:
            return max((e.stat().st_mtime for e in os.scandir(os.path.join(self.app.cfg.out_dir, "previews"))
                        if e.name.endswith(".jpg")), default=0.0)
        except OSError:
            return 0.0

    def run_once(self):
        with self._lock:
            if self.running:
                return
            self.running = True
        cmd = [venv_python(), os.path.join(HERE, "make_previews.py")]
        if self.config_arg:
            cmd += ["--config", self.config_arg]
        self.last_run_epoch = time.time()
        self.app.log("previews started")
        last_line = []
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW, cwd=HERE)

            def read():  # drain the output (a long run prints a line per file)
                for ln in proc.stdout:
                    if ln.startswith("PREVIEWS "):
                        last_line[:] = [ln.strip()]

            reader = threading.Thread(target=read, daemon=True, name="previews-out")
            reader.start()
            linked = self._newest_preview_mtime()
            next_link = time.time() + self.LINK_EVERY_S
            while proc.poll() is None:
                time.sleep(10)
                if time.time() >= next_link:  # long run: show what is there so far
                    next_link = time.time() + self.LINK_EVERY_S
                    newest = self._newest_preview_mtime()
                    if newest > linked:
                        linked = newest
                        self.app.previews_unlinked = True
                        self.app.builder.request("link new previews")
            reader.join(30)
            self.last = last_line[0] if last_line else f"exit code {proc.returncode}"
        except OSError as exc:
            self.last = str(exc)
        finally:
            self.running = False
        self.app.log(f"previews finished: {self.last}")
        counts = {k: int(v) for k, v in re.findall(r"\b(made|fetched)=(\d+)", self.last)}
        if counts.get("made") or counts.get("fetched"):
            self.app.previews_unlinked = True
            self.app.builder.request("new previews")


class App:
    def __init__(self, cfg, config_arg):
        self.cfg, self.config_arg = cfg, config_arg
        self.me = getpass.getuser()
        self.server_log = os.path.join(cfg.out_dir, "logs", "server.log")
        self.actions = {"/api/value": self.set_value, "/api/comment": self.add_comment,
                        "/api/comment/delete": self.delete_comment}
        self.status = "ok" if cfg.configured else "setup"   # setup | starting | offline | readonly | ok
        self.status_msg = "" if cfg.configured else "not set up yet"
        self.previews_unlinked = False   # new previews made here that no build has linked yet
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
            return self._set("offline", f"Labdata folder {cfg.labdata_root} not reachable (network drive / VPN?) - "
                                        f"showing the last local copy, retrying every minute")
        try:
            safety.configure_publish(cfg.publish_dir)
            os.listdir(cfg.publish_dir)
            old = lease.legacy_holder(cfg.publish_dir)
        except (OSError, RuntimeError) as exc:
            return self._set("offline", f"catalog folder not usable ({exc}) - retrying every minute")
        if old:
            return self._set("readonly", f"read-only: an older version of the catalog server ({lease.describe(old)}) "
                                         f"still maintains the catalog folder - update the program there "
                                         f"(git pull + install.ps1), then edits are possible here")
        was = self.status
        self._set("ok", f"catalog folder {cfg.publish_dir}")
        if was != "ok":
            reason = self.build_due()
            if reason:
                self.builder.request(reason)

    # --- catalog folder state ----------------------------------------------------------------
    def share_generation(self) -> int:
        try:
            return int(sync.share_state(self.cfg).get("generation", 0))
        except (OSError, ValueError):
            return 0

    def local_generation(self) -> int:
        try:
            return int(sync.local_state(self.cfg).get("generation", 0))
        except (OSError, ValueError):
            return 0

    def published_age_s(self) -> float | None:
        """Seconds since the catalog folder was last published (None: never / unknown)."""
        try:
            t = sync.share_state(self.cfg).get("time")
            return (datetime.now() - datetime.fromisoformat(t)).total_seconds() if t else None
        except (OSError, ValueError, TypeError):
            return None

    def pending_edits(self) -> list[dict]:
        try:
            return inbox.pending(self.cfg.publish_dir)
        except OSError:
            return []

    def edits_waiting(self, older_than_s: float = 0) -> bool:
        if older_than_s <= 0:
            return bool(self.pending_edits())
        cutoff = time.time() - older_than_s
        for e in self.pending_edits():
            try:
                if datetime.fromisoformat(str(e.get("ts", ""))).timestamp() <= cutoff:
                    return True
            except ValueError:
                return True
        return False

    def build_due(self, edits_older_than_s: float = 0) -> str | None:
        """Reason to start a build now, else None. Edits are normally applied by the build the
        submitting server starts; ``edits_older_than_s`` lets the periodic check pick up ones
        whose server stopped before that build."""
        age = self.published_age_s()
        if age is None:
            return "first build"
        if self.cfg.auto_update_min and age > self.cfg.auto_update_min * 60:
            return "scheduled update"
        if self.edits_waiting(edits_older_than_s):
            return "edits waiting in the inbox"
        return None

    # --- logging ---------------------------------------------------------------------------
    def log(self, msg: str):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
        try:
            print(line, flush=True)
        except (OSError, ValueError, AttributeError):  # console gone / none
            pass
        try:
            with safety.open_write(self.server_log, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def is_owner(self, user: str | None) -> bool:
        return bool(user) and user.lower() == self.me.lower()

    def can_edit(self, user: str | None) -> bool:
        """Any identified Windows user of this machine may comment / enter values (as themselves)."""
        return self.status == "ok" and bool(user)

    def edit_refusal(self, user: str | None) -> str:
        if not user:
            return "your Windows user could not be identified - editing is not possible"
        return f"editing is not possible now: {self.status_msg}"

    def known_acq(self, acq_id: str) -> bool:
        """Dataset in the local copy or - if another machine published since - the catalog folder."""
        cands = [(self.cfg.db_path, "?mode=ro")]
        if self.cfg.publish_dir:  # immutable: no locking on the share (replaced atomically, never written live)
            cands.append((os.path.join(self.cfg.publish_dir, "catalog.sqlite"), "?mode=ro&immutable=1"))
        for path, opts in cands:
            if not os.path.exists(path):
                continue
            try:
                db = sqlite3.connect(Path(path).absolute().as_uri() + opts, uri=True, timeout=30)
                try:
                    if db.execute("SELECT 1 FROM acquisitions WHERE acq_id=?", (acq_id,)).fetchone():
                        return True
                finally:
                    db.close()
            except sqlite3.Error:
                continue
        return False

    # --- edit actions: validated here, written to the inbox, applied by the next build ---
    def _submit(self, user: str, action: str, **fields) -> dict:
        e = inbox.new_entry(action, user, **fields)
        name = inbox.submit(self.cfg.publish_dir, e)
        self.log(f"{user}: {action} -> inbox/{name}")
        return inbox.public(e)

    def set_value(self, user: str, b: dict) -> dict:
        acq, field, value = str(b.get("acq_id", "")), str(b.get("field", "")), str(b.get("value", "")).strip()
        image_key, old = str(b.get("image_key", "")), str(b.get("old_value", "")).strip()
        if field not in ov.ALL_FIELDS:
            raise ValueError(f"unknown field {field!r}")
        if not self.known_acq(acq):
            raise ValueError(f"unknown dataset {acq!r}")
        if value:
            ov.parse_value(field, value)  # raises ValueError with a helpful message
            return self._submit(user, "value_set", acq_id=acq, image_key=ov.norm_key(field, image_key),
                                field=field, value=value, old_value=old)
        if not old:
            raise ValueError("no value to remove")
        return self._submit(user, "value_remove", acq_id=acq, image_key=ov.norm_key(field, image_key),
                            field=field, old_value=old)

    def add_comment(self, user: str, b: dict) -> dict:
        acq = str(b.get("acq_id", ""))
        text = cm.check_text(str(b.get("text", "")))
        if not self.known_acq(acq):
            raise ValueError(f"unknown dataset {acq!r}")
        return self._submit(user, "comment_add", acq_id=acq, image_key=str(b.get("image_key", "")), text=text)

    def delete_comment(self, user: str, b: dict) -> dict:
        cid = str(b.get("comment_id", ""))
        if not re.fullmatch(r"[0-9a-f]{8,10}", cid):
            raise ValueError("invalid comment id")
        return self._submit(user, "comment_delete", acq_id=str(b.get("acq_id", "")), comment_id=cid)

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
            when = (f' (last written by {esc(st["host"])} {esc(str(st.get("time", "")).replace("T", " "))})'
                    if st.get("host") else "")
            ci = f'<span class="ok">existing catalog found</span>{when} – it will be used (shared with the ' \
                 f'other catalog servers that use it)'
            old = lease.legacy_holder(cat)
            if old:
                ci += f'<br><span class="warn">still maintained by an older catalog server ({esc(lease.describe(old))}) ' \
                      f'– this one stays read-only until that one is updated</span>'
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
            upd = max(0.0, float(b.get("auto_update_min", 1440)))
            budget = max(1.0, float(b.get("preview_budget_min", 1380)))
        except (TypeError, ValueError):
            raise ValueError("minutes must be numbers") from None
        mcfg.write_settings({"labdata_root": lab, "catalog_dir": cat, "auto_update_min": upd,
                             "previews": prev, "preview_budget_min": budget}, self.config_arg)
        autostart(bool(b.get("autostart")))
        return f"settings saved (labdata_root={lab}, catalog_dir={cat}, autostart={bool(b.get('autostart'))})"

    def restart(self):
        """Start a fresh server process (new settings) and stop this one."""
        self.log("restarting with new settings")
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
          "$s.WorkingDirectory=$env:MDCAT_DIR;$s.Description='Microscopy catalog server';"
          "if(Test-Path $env:MDCAT_ICO){$s.IconLocation=$env:MDCAT_ICO+',0'};$s.Save()")
    env = dict(os.environ, MDCAT_LNK=STARTUP_LNK, MDCAT_EXE=pythonw(),
               MDCAT_SCRIPT=os.path.join(HERE, "serve_catalog.py"), MDCAT_DIR=HERE,
               MDCAT_ICO=os.path.join(HERE, ".runtime", "catalog.ico"))
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

        def _page(self) -> tuple[bytes, int] | tuple[None, int]:
            """The newest catalog page and its generation: the catalog folder's when another
            machine published after this server's last build, else the local copy."""
            sh, lo = app.share_generation(), app.local_generation()
            local = (os.path.join(app.cfg.out_dir, "catalog.html"), lo)
            share = (os.path.join(app.cfg.publish_dir, "index.html"), sh) if app.cfg.publish_dir else None
            cands = [share, local] if share and sh > lo else [local, share]
            for c in cands:
                if not c:
                    continue
                try:
                    with open(c[0], "rb") as fh:
                        return fh.read(), c[1]
                except OSError:
                    continue
            return None, 0

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            path = self.path.split("?", 1)[0]
            if path == "/setup" or (path in ("/", "/index.html") and app.status == "setup"):
                if path != "/setup":
                    return self._send(302, b"", "text/plain", {"Location": "/setup"})
                return self._send(200, setup_page.render(app.setup_state()), "text/html; charset=utf-8")
            if path in ("/", "/index.html"):
                page, page_gen = self._page()
                if page is None:
                    msg = (f"The catalog is being built for the first time ({app.status_msg}). "
                           f"Reload in a minute - or open http://localhost:{port}/setup")
                    return self._send(503, msg.encode("utf-8"), "text/plain; charset=utf-8")
                user = self._user()
                info = {"user": user or "", "can_edit": app.can_edit(user), "status": app.status,
                        "status_msg": app.status_msg if app.can_edit(user) or app.status != "ok"
                        else app.edit_refusal(user), "owner": app.me, "page_generation": page_gen}
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
                holder = None
                if app.cfg.publish_dir and app.status == "ok":
                    try:
                        h = lease.current_holder(app.cfg.publish_dir)
                        holder = lease.describe(h) if h else None
                    except OSError:
                        pass
                return self._json(200, {"user": user or "", "can_edit": app.can_edit(user), "status": app.status,
                                        "status_msg": app.status_msg,
                                        "generation": b.generation if b else 0, "build": b.state() if b else {},
                                        "catalog_generation": max(app.share_generation(), app.local_generation()),
                                        "building": holder,
                                        "previews": {"running": app.previewer.running, "last": app.previewer.last}
                                        if app.previewer else {}})
            if path == "/api/pending":
                if not app.cfg.publish_dir or app.status not in ("ok", "readonly"):
                    return self._json(200, {"pending": [], "conflicts": [], "failed": []})
                try:
                    pdir = app.cfg.publish_dir
                    return self._json(200, {"pending": [inbox.public(e) for e in inbox.pending(pdir)],
                                            "conflicts": [inbox.public(e) for e in inbox.recent(pdir, "conflict")],
                                            "failed": [inbox.public(e) for e in inbox.recent(pdir, "failed")]})
                except OSError as exc:
                    return self._json(200, {"pending": [], "conflicts": [], "failed": [], "error": str(exc)})
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            # custom header + JSON content type -> browsers send a CORS preflight for cross-site
            # requests, which this server never answers, so other web pages cannot post here
            if (not self._host_ok() or self.headers.get("X-MDCAT") != "1"
                    or not self.headers.get("Content-Type", "").startswith("application/json")):
                return self._json(403, {"ok": False, "error": "forbidden"})
            user = self._user()
            owner_only = path in ("/api/check", "/api/pickfolder", "/api/setup", "/api/rebuild")
            if owner_only and not app.is_owner(user):
                return self._json(403, {"ok": False, "error": f"only {app.me} (who runs this server) may change "
                                                              f"its settings - you are {user or '?'}"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(min(n, 100_000)) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
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
                if path not in actions and path != "/api/rebuild":
                    return self._json(404, {"ok": False, "error": "unknown action"})
                if not app.can_edit(user):
                    return self._json(403, {"ok": False, "error": app.edit_refusal(user)})
                if path == "/api/rebuild":
                    app.log(f"{user}: rebuild requested")
                    return self._json(200, {"ok": True, "result": "rebuild requested",
                                            "generation": app.builder.request(f"requested by {user}")})
                entry = actions[path](user, body)
            except (ValueError, TimeoutError, subprocess.TimeoutExpired, OSError) as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            app.builder.request(f"edit by {user}")
            return self._json(200, {"ok": True, "entry": entry})

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
    ap.add_argument("--no-tray", action="store_true", help="no notification-area icon")
    ap.add_argument("--console", action="store_true", help="stay in the console and print the log (debugging)")
    args = ap.parse_args(argv)
    if not args.console:
        detach_console()
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
    url = f"http://localhost:{port}" + ("" if cfg.configured else "/setup")
    app.log(f"catalog server on {url} (program {HERE}, working copy {cfg.out_dir}, "
            f"catalog folder {cfg.publish_dir or '-'}, user {app.me})")
    if args.open:
        webbrowser.open(url)
    srv = threading.Thread(target=httpd.serve_forever, daemon=True, name="http")
    srv.start()
    tray = None
    if not args.no_tray:
        try:
            from mdcat.tray import Tray
            tray = Tray(app, f"http://localhost:{port}",
                        on_stop=lambda: threading.Thread(target=httpd.shutdown, daemon=True).start())
        except Exception as exc:  # noqa: BLE001 - no tray (e.g. pystray missing): server still works
            app.log(f"no tray icon: {exc}")
    if tray:
        # the tray loop blocks the main thread; when the web server stops (Stop / restart), close it
        threading.Thread(target=lambda: (srv.join(), tray.close()), daemon=True).start()
        tray.run()
    try:
        while srv.is_alive():
            srv.join(0.5)
    except KeyboardInterrupt:
        httpd.shutdown()
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
