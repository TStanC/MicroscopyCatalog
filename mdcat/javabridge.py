"""Compile and run the read-only Bio-Formats metadata dumper (java/OmeDump.java)."""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass

from . import safety
from .config import Config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA_SRC = os.path.join(HERE, "java", "OmeDump.java")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # no console windows when run from pythonw (server)


@dataclass
class Job:
    mode: str  # "ome" | "orig"
    src: str  # absolute input path (read only)
    out: str  # absolute output path inside the cache dir
    extra: str = ""


@dataclass
class JobResult:
    ok: bool
    message: str
    seconds: float


def _exe(cfg: Config, name: str) -> str:
    p = os.path.join(cfg.java_bin, name + (".exe" if os.name == "nt" else ""))
    if not cfg.java_bin or not os.path.exists(p):
        raise FileNotFoundError(f"{name} not found at {p!r} - run install.ps1 again (it sets up Java in "
                                f".runtime\\runtime.json)")
    if not cfg.bioformats_lib or not os.path.isdir(cfg.bioformats_lib):
        raise FileNotFoundError(f"Bio-Formats folder not found ({cfg.bioformats_lib!r}) - run install.ps1 again")
    return p


def build_dir(cfg: Config) -> str:
    return os.path.join(cfg.cache_dir, "java_build")


def ensure_compiled(cfg: Config, log) -> None:
    out = build_dir(cfg)
    cls = os.path.join(out, "OmeDump.class")
    if os.path.exists(cls) and os.path.getmtime(cls) >= os.path.getmtime(JAVA_SRC):
        return
    safety.makedirs(out)
    log(f"Compiling {JAVA_SRC}")
    cp = os.path.join(cfg.bioformats_lib, "*")
    r = subprocess.run([_exe(cfg, "javac"), "-cp", cp, "-d", out, JAVA_SRC], capture_output=True, text=True,
                       creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"javac failed:\n{r.stdout}\n{r.stderr}")


def run_jobs(cfg: Config, jobs: list[Job], log, label: str = "") -> list[JobResult]:
    """Run jobs in as few JVMs as possible; a hung or crashed file is skipped and the rest continue."""
    results: list[JobResult | None] = [None] * len(jobs)
    if not jobs:
        return []
    ensure_compiled(cfg, log)
    tmp_dir = os.path.join(cfg.cache_dir, "tmp")
    safety.makedirs(tmp_dir)
    for j in jobs:
        safety.check_writable(j.out)
    java = _exe(cfg, "java")
    cp = build_dir(cfg) + os.pathsep + os.path.join(cfg.bioformats_lib, "*")
    stderr_path = safety.check_writable(os.path.join(cfg.cache_dir, "java_stderr.log"))

    start = 0
    while start < len(jobs):
        batch = list(range(start, len(jobs)))
        jobs_file = os.path.join(tmp_dir, "jobs.tsv")
        with safety.open_write(jobs_file, "w", encoding="utf-8", newline="\n") as fh:
            for i in batch:
                j = jobs[i]
                fh.write(f"{j.mode}\t{j.src}\t{j.out}\t{j.extra}\n")
        with safety.open_write(stderr_path, "a", encoding="utf-8") as err:
            proc = subprocess.Popen(
                [java, f"-Xmx{cfg.java_xmx}", "-Djava.awt.headless=true",
                 f"-Dmdcat.readBudget={cfg.preview_read_bytes or 3_000_000_000}", "-cp", cp, "OmeDump", jobs_file],
                stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
            )
            q: queue.Queue = queue.Queue()

            def reader():
                for raw in proc.stdout:
                    q.put(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
                q.put(None)

            threading.Thread(target=reader, daemon=True).start()
            current: int | None = None
            t_start = time.time()
            next_start = len(jobs)
            while True:
                try:
                    line = q.get(timeout=cfg.hang_timeout_s)
                except queue.Empty:
                    proc.kill()
                    if current is not None:
                        gi = batch[current]
                        results[gi] = JobResult(False, f"timeout after {cfg.hang_timeout_s:.0f}s", time.time() - t_start)
                        log(f"  TIMEOUT {jobs[gi].src}")
                        next_start = gi + 1
                    break
                if line is None:
                    proc.wait()
                    if current is not None and results[batch[current]] is None:
                        gi = batch[current]
                        results[gi] = JobResult(False, f"JVM exited (code {proc.returncode}) while reading", 0.0)
                        next_start = gi + 1
                    break
                parts = line.split(" ", 3)
                if parts[0] == "START" and len(parts) >= 2:
                    current = int(parts[1])
                    t_start = time.time()
                    gi = batch[current]
                    log(f"  {label}[{gi + 1}/{len(jobs)}] {os.path.basename(jobs[gi].src)}")
                elif parts[0] == "DONE" and len(parts) >= 3:
                    gi = batch[int(parts[1])]
                    ok = parts[2] == "OK"
                    msg = parts[3] if len(parts) > 3 else ""
                    results[gi] = JobResult(ok, msg if not ok else "", time.time() - t_start)
                    if not ok:
                        log(f"    ERROR: {msg[:300]}")
                    current = None
            if next_start == len(jobs):
                # Anything not reported (should not happen) is an error.
                for gi in batch:
                    if results[gi] is None:
                        results[gi] = JobResult(False, "no result reported", 0.0)
            start = next_start
    return [r if r is not None else JobResult(False, "not run", 0.0) for r in results]
