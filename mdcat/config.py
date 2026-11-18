"""Configuration loading.

Three layers, later ones override earlier ones key by key:

1. ``config.toml`` next to the code - defaults for every option (in git).
2. ``.runtime/runtime.json`` - Java + Bio-Formats locations, written by install.ps1.
3. ``settings.toml`` next to the code - this machine's settings, written by the setup page
   of the server (``--config`` replaces it). Not in git.

Everything the program needs lives in the program folder (the git clone); all catalog
data (database, comments, manual values, previews) lives in the catalog folder
(``catalog_dir``, e.g. in Labdata). ``.work/`` in the program folder is only a local
working copy of it.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field

PROGRAM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULTS_PATH = os.path.join(PROGRAM_DIR, "config.toml")
SETTINGS_PATH = os.path.join(PROGRAM_DIR, "settings.toml")
RUNTIME_PATH = os.path.join(PROGRAM_DIR, ".runtime", "runtime.json")
DEFAULT_WORK_DIR = os.path.join(PROGRAM_DIR, ".work")

# settings the setup page writes (everything else can be added to settings.toml by hand)
SETUP_KEYS = ("labdata_root", "catalog_dir", "auto_update_min", "previews", "preview_budget_min")
PREVIEW_MODES = {"network": 300, "full": 3000, "off": 0}   # max MB read per preview image


@dataclass
class Config:
    out_dir: str = ""
    labdata_root: str = ""
    catalog_dir: str = ""
    java_bin: str = ""
    bioformats_lib: str = ""
    work_dir: str = ""
    java_xmx: str = "4g"
    recent_minutes: float = 60
    hang_timeout_s: float = 1800
    exclude_top: list[str] = field(default_factory=list)
    exclude_names: list[str] = field(default_factory=list)
    other_owner_names: list[str] = field(default_factory=list)
    raw_exts: list[str] = field(default_factory=list)
    snapshot_exts: list[str] = field(default_factory=list)
    processed_dirs: list[str] = field(default_factory=list)
    metadata_dirs: list[str] = field(default_factory=list)
    converted_dirs: list[str] = field(default_factory=list)
    segmentation_patterns: list[str] = field(default_factory=list)
    sceneviewer: str = ""
    server_port: int = 8766
    auto_update_min: float = 60
    previews: str = "network"
    preview_budget_min: float = 20
    config_path: str = ""

    # the build code calls the catalog folder the "publish" folder
    @property
    def publish_dir(self) -> str:
        return self.catalog_dir

    @publish_dir.setter
    def publish_dir(self, value: str) -> None:
        self.catalog_dir = value

    @property
    def data_dir(self) -> str:   # the default working directory (local copy of the catalog folder)
        return self.work_dir

    @property
    def configured(self) -> bool:
        return bool(self.labdata_root and self.catalog_dir)

    @property
    def cache_dir(self) -> str:
        return os.path.join(self.out_dir, "cache")

    @property
    def db_path(self) -> str:
        return os.path.join(self.out_dir, "catalog.sqlite")

    @property
    def publish_name(self) -> str:
        return os.path.basename(self.catalog_dir.rstrip("\\/")) if self.catalog_dir else ""

    @property
    def preview_read_bytes(self) -> int:
        return PREVIEW_MODES.get(self.previews, 300) * 1_000_000

    @property
    def previews_on(self) -> bool:
        return self.previews != "off" and self.preview_budget_min > 0


def _load(path: str) -> dict:
    with open(path, "rb") as fh:  # utf-8-sig: tolerate a BOM (Notepad / PowerShell 5 write one)
        return tomllib.loads(fh.read().decode("utf-8-sig"))


def read_settings(path: str | None = None) -> dict:
    p = path or SETTINGS_PATH
    return _load(p) if os.path.exists(p) else {}


def write_settings(values: dict, path: str | None = None) -> None:
    """Update settings.toml (keeps keys it does not know, e.g. ones added by hand)."""
    p = path or SETTINGS_PATH
    cur = read_settings(p)
    cur.update(values)
    lines = ["# Settings of this machine for the microscopy catalog (written by the setup page,",
             "# http://localhost:8766/setup). Any option of config.toml can be added here.", ""]
    for k, v in cur.items():
        lines.append(f"{k} = {_toml(v)}")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, p)


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)   # JSON string escaping is valid TOML


def load_config(path: str | None = None, out_dir: str | None = None) -> Config:
    """``path`` = settings file (default settings.toml in the program folder)."""
    raw = _load(DEFAULTS_PATH)
    if os.path.exists(RUNTIME_PATH):
        with open(RUNTIME_PATH, encoding="utf-8-sig") as fh:
            raw.update({k: v for k, v in json.load(fh).items() if k in ("java_bin", "bioformats_lib")})
    settings_path = path or SETTINGS_PATH
    raw.update(read_settings(settings_path))
    known = set(Config.__dataclass_fields__) - {"out_dir", "config_path"}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Unknown config keys: {sorted(unknown)} (in {settings_path} or config.toml)")
    cfg = Config(**raw)
    cfg.config_path = os.path.abspath(settings_path)
    cfg.work_dir = os.path.abspath(os.path.join(PROGRAM_DIR, os.path.expandvars(cfg.work_dir or ".work")))
    cfg.out_dir = os.path.abspath(os.path.expandvars(out_dir)) if out_dir else cfg.work_dir
    if cfg.labdata_root:
        cfg.labdata_root = os.path.abspath(os.path.expandvars(cfg.labdata_root))
    if cfg.catalog_dir:
        cfg.catalog_dir = os.path.abspath(os.path.expandvars(cfg.catalog_dir))
    if cfg.previews not in PREVIEW_MODES:
        raise ValueError(f"previews must be one of {sorted(PREVIEW_MODES)}, got {cfg.previews!r}")
    cfg.raw_exts = [e.lower() for e in cfg.raw_exts]
    cfg.snapshot_exts = [e.lower() for e in cfg.snapshot_exts]
    return cfg


def java_status(cfg: Config) -> dict:
    """What install.ps1 set up (for the setup page / error messages)."""
    jexe = os.path.join(cfg.java_bin, "java.exe") if cfg.java_bin else ""
    jar_ok = bool(cfg.bioformats_lib) and os.path.isdir(cfg.bioformats_lib) and \
        any(f.endswith(".jar") for f in os.listdir(cfg.bioformats_lib))
    return {"java_bin": cfg.java_bin, "java_ok": bool(jexe) and os.path.exists(jexe),
            "bioformats_lib": cfg.bioformats_lib, "bioformats_ok": jar_ok}
