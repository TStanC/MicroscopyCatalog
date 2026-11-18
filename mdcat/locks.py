"""Inter-process file locks (Windows, msvcrt) for the catalog directory.

* ``build.lock``     held for a whole build_catalog.py run (one build at a time)
* ``overrides.lock`` held briefly around read-modify-write of manual_overrides.csv / comments.csv
"""

from __future__ import annotations

import msvcrt
import os
import time

from . import safety


class FileLock:
    def __init__(self, path: str):
        self.path = safety.check_writable(path)
        self._fh = None

    def acquire(self, timeout: float = 0.0) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.monotonic() + timeout
        while True:
            fh = open(self.path, "a+b")
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                self._fh = fh
                return True
            except OSError:
                fh.close()
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.2)

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self._fh.close()
                self._fh = None

    def __enter__(self):
        if not self.acquire(timeout=30):
            raise TimeoutError(f"could not lock {self.path} within 30 s")
        return self

    def __exit__(self, *exc):
        self.release()


def build_lock(cfg) -> FileLock:
    return FileLock(os.path.join(cfg.cache_dir, "build.lock"))


def edit_lock(cfg) -> FileLock:
    return FileLock(os.path.join(cfg.cache_dir, "overrides.lock"))
