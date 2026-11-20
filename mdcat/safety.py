"""Write guard: every file written by the tool goes through this module.

The catalog must never modify anything below ``labdata_root``. Local outputs live in the
working directory (``check_writable``). The single exception is the catalog folder
(``catalog_dir``): a folder created by the tool (marker file), into which only whitelisted
file names are written via ``publish_file()``.
"""

from __future__ import annotations

import os
import re
import time

_ROOTS: dict[str, str] = {}


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _inside(path_n: str, root_n: str) -> bool:
    return path_n == root_n or path_n.startswith(root_n.rstrip(os.sep) + os.sep)


def configure(labdata_root: str, out_dir: str) -> None:
    labdata = _norm(labdata_root)
    out = _norm(out_dir)
    if _inside(out, labdata):
        raise RuntimeError(f"Working directory {out_dir} lies inside the read-only data root {labdata_root}")
    _ROOTS["labdata"] = labdata
    _ROOTS["out"] = out


def check_writable(path: str) -> str:
    """Return ``path`` if it is inside the working directory, else raise."""
    if not _ROOTS:
        raise RuntimeError("safety.configure() was not called")
    real = _norm(path)
    if _inside(real, _ROOTS["labdata"]):
        raise PermissionError(f"Refusing to write inside the data root: {path}")
    if not _inside(real, _ROOTS["out"]):
        raise PermissionError(f"Refusing to write outside the working directory: {path}")
    return path


PUBLISH_MARKER = b'<meta name="generator" content="mdcat-microscopy-catalog">'
PUBLISH_DIR_MARKER = ".mdcat-publish"   # marks a folder created by this tool
# Only these names may be written into the catalog folder.
PUBLISH_FILES = {
    "index.html", "catalog.sqlite", "catalog.json", "CATALOG.md", "CHANGELOG.md", "README.txt",
    "catalog_datasets.csv", "catalog_images.csv", "catalog_files.csv", "catalog_scenes.csv",
    "comments.csv", "manual_overrides.csv", "sync.json", "server.lock", "build.lock", "preview.lock",
    PUBLISH_DIR_MARKER,
}
# edit inbox: inbox/<YYYYMMDDTHHMMSSmmm>_<user>_<id>.json, processed ones in done/conflicts/failed
INBOX_PATTERN = re.compile(r"^inbox/(?:(?:done|conflicts|failed)/)?\d{8}T\d{9}_[A-Za-z0-9-]{1,40}_[0-9a-f]{8}\.json$")
PUBLISH_PATTERNS = [
    re.compile(r"^previews/[0-9a-f]{16}\.jpg$"),
    re.compile(r"^metadata/xml/[0-9a-f]{16}\.ome\.xml\.gz$"),
    re.compile(r"^metadata/orig/[0-9a-f]{16}\.tsv\.gz$"),
    re.compile(r"^logs/web_edits\.log$"),
    INBOX_PATTERN,
]
REMOVABLE = [PUBLISH_PATTERNS[0], re.compile(r"^(server|build|preview)\.lock$"), INBOX_PATTERN]


def check_catalog_dir(catalog_dir: str, labdata_root: str) -> str:
    """Validate a catalog folder choice (setup page) without creating anything.
    Returns "" if fine, else the reason."""
    if not catalog_dir:
        return "no catalog folder chosen"
    real, lab = _norm(catalog_dir), _norm(labdata_root) if labdata_root else ""
    if lab and real == lab:
        return "the catalog folder cannot be the Labdata root itself - use a new folder in it"
    if lab and _inside(real, lab) and os.path.dirname(real) != lab:
        return ("inside the Labdata folder, the catalog folder must be directly in it "
                f"(e.g. {os.path.join(labdata_root, 'MicroscopyCatalog')}), not inside a dataset folder")
    if os.path.exists(catalog_dir):
        if not os.path.isdir(catalog_dir):
            return "exists and is not a folder"
        if not os.path.exists(os.path.join(catalog_dir, PUBLISH_DIR_MARKER)) and os.listdir(catalog_dir):
            return "the folder exists, is not empty and was not created by the catalog - choose an empty or new folder"
    elif not os.path.isdir(os.path.dirname(real)):
        return f"parent folder {os.path.dirname(catalog_dir)} does not exist"
    return ""


def configure_publish(publish_dir: str) -> str | None:
    """Allow one extra writable folder: the catalog folder (created by the tool, marker file).
    An existing folder without the marker is only accepted if it is empty, so a user folder
    of the same name is never touched. Returns the normalised path (None = disabled)."""
    if not publish_dir:
        return None
    if not _ROOTS:
        raise RuntimeError("safety.configure() was not called")
    lab = _ROOTS["labdata"]
    problem = check_catalog_dir(publish_dir, lab)
    if problem:
        raise RuntimeError(f"catalog folder {publish_dir}: {problem}")
    if not os.path.exists(publish_dir):
        os.mkdir(publish_dir)
    marker = os.path.join(publish_dir, PUBLISH_DIR_MARKER)
    if not os.path.exists(marker):
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("Folder written by the microscopy catalog tool. Do not put other files here.\n")
    _ROOTS["publish"] = _norm(publish_dir)
    return _ROOTS["publish"]


def _check_publish_name(publish_dir: str, name: str) -> None:
    if not _ROOTS.get("publish") or _norm(publish_dir) != _ROOTS["publish"]:
        raise PermissionError(f"Refusing to write to an unconfigured folder: {publish_dir}")
    if not ((name in PUBLISH_FILES and os.path.basename(name) == name) or any(p.match(name) for p in PUBLISH_PATTERNS)):
        raise PermissionError(f"Refusing to write an unexpected file name into the catalog folder: {name}")


def create_exclusive(publish_dir: str, name: str, data: bytes) -> bool:
    """Create a whitelisted file in the catalog folder only if it does not exist yet (atomic on
    SMB: the file server decides). Returns False if it already exists."""
    _check_publish_name(publish_dir, name)
    path = os.path.join(publish_dir, *name.split("/"))
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return True


def unpublish_file(publish_dir: str, name: str) -> None:
    """Remove one file from the catalog folder (only preview images, lock files and inbox entries)."""
    _check_publish_name(publish_dir, name)
    if not any(p.match(name) for p in REMOVABLE):
        raise PermissionError(f"Refusing to remove {name}")
    path = os.path.join(publish_dir, *name.split("/"))
    if os.path.exists(path):
        os.remove(path)


def publish_file(publish_dir: str, name: str, data: bytes) -> None:
    """Atomically write one whitelisted file into the configured catalog folder."""
    _check_publish_name(publish_dir, name)
    if "/" in name:
        os.makedirs(os.path.join(publish_dir, *name.split("/")[:-1]), exist_ok=True)
    if name == "index.html" and PUBLISH_MARKER not in data[:4096]:
        raise PermissionError("Refusing to publish: content is not a catalog page")
    path = os.path.join(publish_dir, *name.split("/"))
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # a viewer has the file open on SMB - retry briefly
            if attempt == 4:
                try:
                    os.remove(tmp)
                finally:
                    raise
            time.sleep(0.5)


def open_write(path: str, mode: str = "w", **kwargs):
    check_writable(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return open(path, mode, **kwargs)


def makedirs(path: str) -> None:
    check_writable(path)
    os.makedirs(path, exist_ok=True)


def remove(path: str) -> None:
    check_writable(path)
    if os.path.exists(path):
        os.remove(path)


def replace_local(path: str, data: bytes) -> None:
    """Atomically replace a file in the working directory (pull from the catalog folder)."""
    check_writable(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # briefly opened by the server
            if attempt == 9:
                raise
            time.sleep(0.5)
