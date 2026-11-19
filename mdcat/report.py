"""Write the catalog outputs: SQLite tables, CSV, JSON, Markdown, HTML and changelog."""

from __future__ import annotations

import csv
import html
import json
import os
import sqlite3
from collections import defaultdict

from . import safety
from .derive import ACQ_COLUMNS, IMG_COLUMNS, SCENE_COLUMNS, fmt_num
from .overrides import DATASET_FIELDS, FIELD_HINTS, IMAGE_FIELDS

BOOK_COLUMNS = ["content_hash", "first_seen", "last_changed", "present", "change"]
LOC_COLUMNS = ["acq_id", "rel", "role", "format", "size_bytes", "status", "error"]
ATT_COLUMNS = ["rel", "category", "instrument", "session", "acq_id", "size"]


def fmt_duration(s) -> str:
    if s is None or s == "":
        return ""
    s = float(s)
    if s < 120:
        return f"{fmt_num(s, 1)} s"
    if s < 7200:
        return f"{fmt_num(s / 60, 1)} min"
    if s < 172800:
        h, m = divmod(int(round(s / 60)), 60)
        return f"{h} h {m:02d} min"
    return f"{fmt_num(s / 86400, 1)} d"


def fmt_size(b) -> str:
    if not b:
        return ""
    b = float(b)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{b:.0f} {unit}" if unit in ("B", "KB") else f"{b:.1f} {unit}"
        b /= 1024
    return ""


# ----------------------------------------------------------------------------- sqlite

def load_previous(db: sqlite3.Connection) -> tuple[dict, dict]:
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    prev_acq, prev_img = {}, defaultdict(list)
    if "acquisitions" in tables:
        cur = db.execute("SELECT * FROM acquisitions")
        cols = [c[0] for c in cur.description]
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            prev_acq[d["acq_id"]] = d
    if "images" in tables:
        cur = db.execute("SELECT * FROM images")
        cols = [c[0] for c in cur.description]
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            prev_img[d["acq_id"]].append(d)
    return prev_acq, prev_img


def _table(db, name, columns, rows, pk=None):
    db.execute(f"DROP TABLE IF EXISTS {name}")
    cols = ", ".join(f'"{c}"' + (" PRIMARY KEY" if c == pk else "") for c in columns)
    db.execute(f"CREATE TABLE {name} ({cols})")

    def conv(v):
        if isinstance(v, bool):
            return int(v)
        return v

    db.executemany(f"INSERT INTO {name} VALUES ({','.join('?' * len(columns))})",
                   [tuple(conv(r.get(c)) for c in columns) for r in rows])


def write_sqlite(db: sqlite3.Connection, data: dict, run: dict) -> None:
    with db:
        _table(db, "acquisitions", ACQ_COLUMNS + BOOK_COLUMNS, data["acquisitions"], pk="acq_id")
        _table(db, "images", IMG_COLUMNS, data["images"])
        _table(db, "locations", LOC_COLUMNS, data["locations"])
        _table(db, "attachments", ATT_COLUMNS, data["attachments"])
        _table(db, "scenes", SCENE_COLUMNS, data.get("scenes", []))
        db.execute("CREATE INDEX IF NOT EXISTS idx_scenes_acq ON scenes(acq_id)")
        _table(db, "needs_input",["acq_id", "image_key", "field", "instrument", "session", "name", "path", "hint"],
               data["needs"])
        db.execute("CREATE INDEX IF NOT EXISTS idx_images_acq ON images(acq_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_locations_acq ON locations(acq_id)")
        db.execute("""CREATE TABLE IF NOT EXISTS runs(started TEXT, finished TEXT, tool_version TEXT,
                      scan_seconds REAL, n_sources INTEGER, read INTEGER, reparsed INTEGER, cached INTEGER,
                      errors INTEGER, incomplete INTEGER, new INTEGER, changed INTEGER, removed INTEGER,
                      readonly_check TEXT)""")
        db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   tuple(run.get(k) for k in ("started", "finished", "tool_version", "scan_seconds", "n_sources",
                                              "read", "reparsed", "cached", "errors", "incomplete", "new",
                                              "changed", "removed", "readonly_check")))


# ----------------------------------------------------------------------------- csv / json

def _sorted_acqs(acqs):
    return sorted(acqs, key=lambda r: (r["instrument"] or "", r["acquired_start"] or "9999", r["path"] or ""))


def write_csv(path: str, columns: list[str], rows: list[dict]) -> None:
    with safety.open_write(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(columns)
        for r in rows:
            w.writerow(["" if r.get(c) is None else (int(r[c]) if isinstance(r.get(c), bool) else r.get(c))
                        for c in columns])


SCENE_RUN_KEYS = ("name", "image_key", "role", "T", "C", "Z", "Y", "X", "dtype", "px_x_um", "px_y_um", "px_z_um")


# per-series fields sent to the HTML page (as compact arrays; collapsed/sorted in the browser)
HTML_SCENE_KEYS = ["series"] + [k for k in SCENE_COLUMNS if k not in ("acq_id", "file", "series", "fiji")]


def _scene_times(s: dict) -> tuple[float | None, float | None]:
    from datetime import datetime
    try:
        t0 = datetime.fromisoformat(s["acquired_start"]).timestamp() if s.get("acquired_start") else None
    except ValueError:
        t0 = None
    return t0, (t0 + (s.get("duration_s") or 0.0)) if t0 is not None else None


def collapse_scenes(scenes: list[dict]) -> list[dict]:
    """Merge consecutive series that differ only in their index (like SceneViewerTool without --all).

    A merged run starts at its earliest series start; its duration lasts until the latest series end.
    """
    out: list[dict] = []
    for s in sorted(scenes, key=lambda s: s["series"]):
        last = out[-1] if out else None
        t0, t1 = _scene_times(s)
        if last and last["last"] == s["series"] - 1 and all(last[k] == s.get(k) for k in SCENE_RUN_KEYS):
            last["last"] = s["series"]
            if t0 is not None:
                if last["_t0"] is None or t0 < last["_t0"]:
                    last["_t0"], last["acquired_start"] = t0, s["acquired_start"]
                last["_t1"] = max(last["_t1"] or t1, t1)
                last["duration_s"] = round(last["_t1"] - last["_t0"], 3)
            continue
        out.append({"first": s["series"], "last": s["series"], **{k: s.get(k) for k in SCENE_RUN_KEYS},
                    "acquired_start": s.get("acquired_start"), "duration_s": s.get("duration_s"),
                    "time_delta_s": s.get("time_delta_s"), "_t0": t0, "_t1": t1})
    for r in out:
        r.pop("_t0", None)
        r.pop("_t1", None)
    return out


def write_json(path: str, data: dict, generated: str) -> None:
    imgs, locs, scenes = defaultdict(list), defaultdict(list), defaultdict(list)
    for im in data["images"]:
        imgs[im["acq_id"]].append({k: im.get(k) for k in IMG_COLUMNS if k != "acq_id"})
    for loc in data["locations"]:
        locs[loc["acq_id"]].append({k: loc.get(k) for k in LOC_COLUMNS if k != "acq_id"})
    for sc in data.get("scenes", []):
        scenes[sc["acq_id"]].append({k: sc.get(k) for k in SCENE_COLUMNS if k != "acq_id"})
    out = {"generated": generated, "acquisitions": []}
    for a in _sorted_acqs(data["acquisitions"]):
        d = {k: a.get(k) for k in ACQ_COLUMNS + ["first_seen", "last_changed", "present"]}
        d["images"] = imgs.get(a["acq_id"], [])
        d["files"] = locs.get(a["acq_id"], [])
        d["scenes"] = scenes.get(a["acq_id"], [])
        d["comments"] = [c for c in data.get("comments", []) if c["acq_id"] == a["acq_id"]]
        out["acquisitions"].append(d)
    out["unassigned_attachments"] = [x for x in data["attachments"] if not x["acq_id"]]
    with safety.open_write(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, ensure_ascii=False, default=str)


# ----------------------------------------------------------------------------- markdown

def _tick(v) -> str:
    return "☑" if v else "☐"


def _mag(a) -> str:
    m, n = a.get("magnification") or "", a.get("na") or ""
    if not m and not n:
        return ""
    return f"{m}x/{n}" if m and n and ";" not in m + n else " ".join(x for x in (f"{m}x" if m else "", f"NA {n}" if n else "") if x)


def write_markdown(path: str, data: dict, generated: str, session_notes: dict) -> None:
    acqs = [a for a in data["acquisitions"] if a.get("present")]
    by = defaultdict(lambda: defaultdict(list))
    for a in acqs:
        by[a["instrument"]][a["session"]].append(a)
    lines = [
        "# Microscopy dataset catalog", "",
        f"Generated {generated} by `build_catalog.py`. Do not edit - regenerate instead. "
        "Interactive view: `catalog.html`; machine-readable: `catalog.sqlite`, `catalog_*.csv`, `catalog.json`.", "",
        "Columns O/Z/S/T/P/M = original / zarr / segmentation / microio tables / processed / metadata files present.", "",
        "| Instrument | Datasets | Images | Size (originals) | Needs input | Unreadable/incomplete |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for inst in sorted(by):
        al = [a for s in by[inst].values() for a in s]
        lines.append(f"| {inst} | {len(al)} | {sum(a['n_images'] or 0 for a in al)} | "
                     f"{fmt_size(sum(a['size_bytes'] or 0 for a in al))} | "
                     f"{sum(1 for a in al if a['status'] == 'needs_input')} | "
                     f"{sum(1 for a in al if a['status'] in ('unreadable', 'incomplete'))} |")
    for inst in sorted(by):
        lines += ["", f"## {inst}"]
        for sess in sorted(by[inst]):
            lines += ["", f"### {sess}"]
            note = session_notes.get((inst, sess))
            if note:
                lines += ["", "> " + note.strip().replace("\n", "\n> ")[:800]]
            lines += ["", "| Date | Name | Obj | TCZYX | px µm | dz µm | Δt | Total | Channels | O | Z | S | T | P | M | Status |",
                      "|---|---|---|---|---|---|---|---|---|:-:|:-:|:-:|:-:|:-:|:-:|---|"]
            for a in sorted(by[inst][sess], key=lambda r: (r["acquired_start"] or "9999", r["path"])):
                dt = a["time_delta_s"]
                dt_txt = fmt_duration(float(dt)) if dt and ";" not in dt else (dt or "")
                lines.append(
                    f"| {(a['acquired_start'] or '')[:16].replace('T', ' ')} | {a['name']} | {_mag(a)} | {a['dims_TCZYX']} | "
                    f"{a['pixel_size_um']} | {a['z_step_um']} | {dt_txt} | {fmt_duration(a['total_time_s'])} | "
                    f"{a['channels']} | {_tick(a['has_original'])} | {_tick(a['has_zarr'])} | {_tick(a['has_segmentation'])} | "
                    f"{_tick(a['has_tables'])} | {_tick(a['has_processed'])} | {_tick(a['has_metadata_files'])} | {a['status']} |")
    missing = [a for a in data["acquisitions"] if not a.get("present")]
    if missing:
        lines += ["", "## No longer found on disk", ""]
        lines += [f"- {a['path']} (last seen before {a['last_changed']})" for a in missing]
    with safety.open_write(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).replace("| None |", "|  |") + "\n")


def write_changelog(path: str, data: dict, run_ts: str, stats: dict) -> bool:
    changes = [a for a in data["acquisitions"] if a.get("change")]
    if not changes:
        return False
    sec = [f"## Run {run_ts}", "",
           f"new: {sum(a['change'] == 'new' for a in changes)}, changed: {sum(a['change'] == 'changed' for a in changes)}, "
           f"removed: {sum(a['change'] == 'removed' for a in changes)}", ""]
    for a in sorted(changes, key=lambda r: (r["change"], r["path"])):
        sec.append(f"- **{a['change']}** `{a['acq_id']}` {a['path']} ({a['status']})")
    old = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            old = fh.read()
        if old.startswith("# Catalog changelog"):
            old = old.split("\n", 2)[2] if old.count("\n") >= 2 else ""
    with safety.open_write(path, "w", encoding="utf-8") as fh:
        fh.write("# Catalog changelog\n\n" + "\n".join(sec) + "\n\n" + old)
    return True


# ----------------------------------------------------------------------------- html

def write_html(path: str, data: dict, generated: str, session_notes: dict, root: str,
               server_port: int = 8766) -> None:
    imgs, locs = defaultdict(list), defaultdict(list)
    for im in data["images"]:
        imgs[im["acq_id"]].append({k: im.get(k) for k in IMG_COLUMNS if k != "acq_id"})
    for loc in data["locations"]:
        locs[loc["acq_id"]].append({k: loc.get(k) for k in ("rel", "role", "format", "size_bytes", "status", "error")})
    comments, overrides, needs = defaultdict(list), defaultdict(list), defaultdict(list)
    for c in data.get("comments", []):
        comments[c["acq_id"]].append(c)
    for o in data.get("overrides", []):
        overrides[o["acq_id"]].append({k: o.get(k, "") for k in ("image_key", "field", "value", "note")})
    for n in data.get("needs", []):
        needs[n["acq_id"]].append({k: n.get(k, "") for k in ("image_key", "field", "hint")})
    scenes, scene_file = defaultdict(list), {}
    for sc in data.get("scenes", []):
        scenes[sc["acq_id"]].append(sc)
        scene_file[sc["acq_id"]] = sc.get("file")
    rows = []
    for a in _sorted_acqs(data["acquisitions"]):
        d = {k: a.get(k) for k in ACQ_COLUMNS + ["first_seen", "last_changed", "present"]}
        d["images"] = imgs.get(a["acq_id"], [])
        d["files"] = locs.get(a["acq_id"], [])
        d["scenes"] = [[s.get(k) for k in HTML_SCENE_KEYS] for s in sorted(scenes.get(a["acq_id"], []),
                                                                           key=lambda s: s["series"])]
        d["scene_file"] = scene_file.get(a["acq_id"])
        d["comments"] = comments.get(a["acq_id"], [])
        d["overrides"] = overrides.get(a["acq_id"], [])
        d["needs"] = needs.get(a["acq_id"], [])
        rows.append(d)
    payload = {
        "generated": generated, "root": root, "rows": rows, "scene_keys": HTML_SCENE_KEYS,
        "server_port": server_port, "field_hints": FIELD_HINTS,
        "image_fields": sorted(IMAGE_FIELDS), "dataset_fields": sorted(DATASET_FIELDS),
        "unassigned": [x for x in data["attachments"] if not x["acq_id"] and x["category"] not in ("notes", "conversion")],
        "sessions": [{"instrument": k[0], "session": k[1], "notes": v} for k, v in sorted(session_notes.items())],
    }
    blob = json.dumps(payload, ensure_ascii=False, default=str).replace("</", "<\\/")
    with safety.open_write(path, "w", encoding="utf-8") as fh:
        fh.write(HTML_TEMPLATE.replace("__DATA__", blob).replace("__GENERATED__", html.escape(generated)))


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="mdcat-microscopy-catalog">
<title>Microscopy Dataset Catalog</title>
<style>
:root{--bg:#f7f7f5;--panel:#fff;--text:#1d1d1f;--muted:#6b6b70;--line:#e2e2e0;--accent:#2f6fb3;--ok:#2e7d4f;--warn:#b26a00;--bad:#b3261e;--chip:#eef2f7;--hover:#f0f4fa}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--panel:#1d1d20;--text:#e8e8ea;--muted:#9a9aa2;--line:#2e2e33;--accent:#6aa6e8;--ok:#5cc28a;--warn:#e0a040;--bad:#ef6b62;--chip:#26303b;--hover:#23272e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:18px 20px 8px}h1{font-size:20px;margin:0 0 4px}.sub{color:var(--muted)}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 0}.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 12px}
.stat b{font-size:16px;display:block}
.controls{position:sticky;top:0;z-index:5;background:var(--bg);padding:10px 20px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:8px;align-items:center}
input[type=search],select{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:5px 8px;font:inherit}
input[type=search]{min-width:240px;flex:1;max-width:420px}label.cb{display:flex;gap:4px;align-items:center;color:var(--muted);white-space:nowrap}
main{padding:10px 20px 40px}.wrap{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%}th,td{padding:5px 7px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}
th{position:sticky;top:0;background:var(--panel);cursor:pointer;user-select:none;font-weight:600;color:var(--muted);font-size:12px}
th.sorted{color:var(--accent)}tr.r:hover{background:var(--hover);cursor:pointer}td.name{white-space:normal;min-width:200px;max-width:300px;word-break:break-word}
td.c{text-align:center}td.w{white-space:normal;max-width:130px;overflow-wrap:break-word}.y{color:var(--ok);font-weight:700}.n{color:var(--line)}
.st{padding:1px 7px;border-radius:10px;font-size:11px;background:var(--chip)}.st.ok{color:var(--ok)}.st.needs_input,.st.incomplete{color:var(--warn)}.st.unreadable,.st.missing{color:var(--bad)}
tr.d>td{background:var(--bg);white-space:normal;padding:10px 14px}.d table{background:var(--panel);margin:6px 0 10px;font-size:12px}.d h4{margin:8px 0 2px;font-size:12px;color:var(--muted)}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:11.5px}.muted{color:var(--muted)}.src{color:var(--muted);font-size:10.5px}
details{margin-top:18px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 12px}summary{cursor:pointer;font-weight:600}
pre{white-space:pre-wrap;margin:4px 0 10px}
.box{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:8px 0}
.cm{border-left:3px solid var(--accent);padding:2px 8px;margin:6px 0;white-space:pre-wrap}.cm .src{white-space:normal}
button{font:inherit;background:var(--chip);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:3px 10px;cursor:pointer}
button:hover{border-color:var(--accent)}button.x{padding:0 6px;margin-left:6px;color:var(--bad)}
textarea{font:inherit;width:100%;min-height:54px;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:5px 8px}
input.v{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:3px 6px;font:inherit;min-width:180px}
.lb{position:fixed;inset:0;z-index:30;background:rgba(0,0,0,.82);display:none;flex-direction:column;align-items:center;justify-content:center;cursor:zoom-out}.lb img{border-radius:4px;box-shadow:0 4px 30px rgba(0,0,0,.5);background:#000}.lb .cap{color:#ddd;font-size:12px;margin-top:8px}.lb .cap a{color:#9cf}
.toast{position:fixed;right:16px;bottom:16px;z-index:20;background:var(--panel);border:1px solid var(--accent);border-radius:8px;padding:8px 14px;box-shadow:0 2px 10px rgba(0,0,0,.2);display:none}
.toast.err{border-color:var(--bad)}.chipc{font-size:11px;color:var(--accent)}
table.sub th{position:static;cursor:pointer}table.sub th:hover{color:var(--accent)}
@media (max-width:640px){header,main,.controls{padding-left:16px;padding-right:16px}input[type=search]{min-width:0;width:100%}}
</style></head><body>
<header><h1>Microscopy dataset catalog</h1><div class="sub">Generated __GENERATED__ · read-only scan of <span id="root" class="mono"></span> · <span id="mode"></span></div>
<div class="stats" id="stats"></div></header>
<div class="controls">
<input type="search" id="q" placeholder="Search name, path, channel, objective…">
<select id="inst"><option value="">All instruments</option></select>
<select id="stat"><option value="">Any status</option></select>
<select id="cat"><option value="">Any category</option></select>
<select id="own"><option value="">Any owner</option></select>
<label class="cb"><input type="checkbox" id="fz">has zarr</label>
<label class="cb"><input type="checkbox" id="fs">segmented</label>
<label class="cb"><input type="checkbox" id="ft">time-lapse</label>
<label class="cb"><input type="checkbox" id="fzs">z-stack</label>
<label class="cb"><input type="checkbox" id="fm">show missing</label>
<span id="count" class="muted"></span>
</div>
<main><div class="wrap"><table id="t"><thead><tr id="hr"></tr></thead><tbody id="tb"></tbody></table></div>
<details id="unas"><summary>Unassigned files (processed/metadata files not matched to a dataset)</summary><div id="unasb"></div></details>
<details id="sess"><summary>Session notes (readme files)</summary><div id="sessb"></div></details>
<details><summary>How to read this page</summary><p>One row per dataset (raw file incl. all identical copies, a zarr without original, or a snapshot). Click a row for its images, files and sources.
Tick columns: <b>Orig</b> original raw file, <b>Zarr</b> converted OME-Zarr, <b>Seg</b> segmentation (zarr labels or segmentation csv/zip), <b>Tab</b> microio tables, <b>Proc</b> processed images, <b>Meta</b> metadata files/screenshots.
Values with several entries (multi-image files) are separated by “;”. Missing values are listed in <span class="mono">needs_input.csv</span>; fill the <i>value</i> column and run the script again.</p></details>
</main>
<div class="toast" id="toast"></div>
<div class="lb" id="lb"><img id="lbimg" alt="preview"><div class="cap" id="lbcap"></div></div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const $=id=>document.getElementById(id);
$('root').textContent=D.root;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function dur(s){if(s===null||s===undefined||s==='')return'';s=+s;if(isNaN(s))return'';if(s<120)return (Math.round(s*10)/10)+' s';if(s<7200)return (Math.round(s/6)/10)+' min';if(s<172800){const m=Math.round(s/60);return Math.floor(m/60)+' h '+String(m%60).padStart(2,'0')+' min'}return (Math.round(s/8640)/10)+' d'}
function durTxt(v){if(v===null||v===undefined||v==='')return'';const p=String(v).split(';').map(x=>x.trim());return p.map(x=>dur(x)||x).join('; ')}
function size(b){if(!b)return'';const u=['B','KB','MB','GB','TB'];let i=0;b=+b;while(b>=1024&&i<4){b/=1024;i++}return (i<2?Math.round(b):b.toFixed(1))+' '+u[i]}
function mag(r){const m=r.magnification||'',n=r.na||'';if(!m&&!n)return'';if(m&&n&&!(m+n).includes(';'))return m+'x/'+n;return [m?m+'x':'',n?'NA '+n:''].filter(Boolean).join(' ')}
const tick=v=>v?'<span class="y">✓</span>':'<span class="n">–</span>';
const COLS=[
 ['acquired_start','Date',r=>esc((r.acquired_start||'').slice(0,16).replace('T',' '))+(r.date_source&&r.date_source!=='metadata'?' <span class="src">('+esc(r.date_source)+')</span>':'')],
 ['instrument','Instrument / session',r=>esc(r.instrument.replace(/_/g,' '))+'<br><span class="muted">'+esc(r.session)+'</span>'],
 ['name','Name',r=>'<b>'+esc(r.name)+'</b><br><span class="muted">'+esc(r.format)+(r.n_images>1?' · '+r.n_images+' images':'')+(r.category!=='raw'?' · '+esc(r.category):'')+(r.owner!=='self'?' · owner: '+esc(r.owner):'')+(r.comments&&r.comments.length?' · <span class="chipc">💬 '+r.comments.length+'</span>':'')+'</span>','name'],
 ['magnification','Obj',r=>esc(mag(r)),'w'],['dims_TCZYX','T×C×Z×Y×X',r=>esc(r.dims_TCZYX)],
 ['pixel_size_um','px µm',r=>esc(r.pixel_size_um),'w'],['z_step_um','dz µm',r=>esc(r.z_step_um),'w'],
 ['time_delta_s','Δt',r=>esc(durTxt(r.time_delta_s)),'w'],['total_time_s','Total',r=>esc(dur(r.total_time_s))],
 ['channels','Channels',r=>esc(r.channels),'w'],['size_bytes','Size',r=>esc(size(r.size_bytes))],
 ['has_original','Orig',r=>tick(r.has_original)+(r.n_original_copies>1?'<sup>'+r.n_original_copies+'</sup>':''),'c'],
 ['has_zarr','Zarr',r=>tick(r.has_zarr)+(r.n_zarr>1?'<sup>'+r.n_zarr+'</sup>':''),'c'],
 ['has_segmentation','Seg',r=>tick(r.has_segmentation),'c'],['has_tables','Tab',r=>tick(r.has_tables),'c'],
 ['has_processed','Proc',r=>tick(r.has_processed||r.has_exports),'c'],['has_metadata_files','Meta',r=>tick(r.has_metadata_files),'c'],
 ['status','Status',r=>'<span class="st '+esc(r.status)+'">'+esc(r.status)+'</span>'],
];
let sortKey='acquired_start',sortDir=-1;const open=new Set();
$('hr').innerHTML=COLS.map(c=>'<th data-k="'+c[0]+'">'+c[1]+'</th>').join('');
document.querySelectorAll('#hr th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(sortKey===k)sortDir=-sortDir;else{sortKey=k;sortDir=1}render()});
function fill(id,key){const vals=[...new Set(D.rows.map(r=>r[key]).filter(Boolean))].sort();$(id).innerHTML+=vals.map(v=>'<option>'+esc(v)+'</option>').join('')}
fill('inst','instrument');fill('stat','status');fill('cat','category');fill('own','owner');
['q','inst','stat','cat','own','fz','fs','ft','fzs','fm'].forEach(id=>$(id).addEventListener('input',render));
function stats(){const p=D.rows.filter(r=>r.present);const n=k=>p.filter(r=>r[k]).length;
 const items=[['Datasets',p.length],['Images',p.reduce((a,r)=>a+(r.n_images||0),0)],['Originals size',size(p.reduce((a,r)=>a+(r.size_bytes||0),0))],['With zarr',n('has_zarr')],['Segmented',n('has_segmentation')],['Needs input',p.filter(r=>r.status==='needs_input').length],['Partial / unreadable / incomplete',p.filter(r=>['partial','unreadable','incomplete'].includes(r.status)).length]];
 $('stats').innerHTML=items.map(i=>'<div class="stat"><b>'+i[1]+'</b>'+i[0]+'</div>').join('')}
function match(r){const q=$('q').value.toLowerCase().trim();
 if(!$('fm').checked&&!r.present)return false;
 if($('inst').value&&r.instrument!==$('inst').value)return false;if($('stat').value&&r.status!==$('stat').value)return false;
 if($('cat').value&&r.category!==$('cat').value)return false;if($('own').value&&r.owner!==$('own').value)return false;
 if($('fz').checked&&!r.has_zarr)return false;if($('fs').checked&&!r.has_segmentation)return false;
 if($('ft').checked&&!(r.max_T>1))return false;if($('fzs').checked&&!(r.max_Z>1))return false;
 if(q){const hay=[r.name,r.path,r.session,r.instrument,r.channels,r.objective,r.notes,r.acq_id,r.acquired_start,...r.files.map(f=>f.rel),...r.images.map(i=>i.image_key),...(r.scenes||[]).map(s=>s.name),...(r.comments||[]).map(c=>c.text+' '+c.user)].join(' ').toLowerCase();return q.split(/\s+/).every(t=>hay.includes(t))}
 return true}
function cmp(a,b){let x=a[sortKey],y=b[sortKey];if(x===y)return 0;if(x===null||x===undefined||x==='')return 1;if(y===null||y===undefined||y==='')return -1;
 const nx=parseFloat(x),ny=parseFloat(y);if(!isNaN(nx)&&!isNaN(ny)&&typeof x!=='boolean'&&!/^\d{4}-/.test(String(x)))return (nx-ny)*sortDir;return String(x).localeCompare(String(y))*sortDir}
const subSort={},scOpen={},scAll={};
const nz=v=>v===null||v===undefined||v===''?null:v;
const firstNum=v=>{const m=String(v??'').match(/\d+/);return m?+m[0]:null};
function tsec(iso){if(!iso)return null;const t=Date.parse(iso.slice(0,19));return isNaN(t)?null:t/1000+(parseFloat('0'+iso.slice(19))||0)}
function sortRows(rows,cols,id){const st=subSort[id];if(!st)return rows;const c=cols.find(c=>c.k===st.k);if(!c)return rows;
 return rows.map((r,i)=>[nz(c.v(r)),i,r]).sort((a,b)=>{const x=a[0],y=b[0];if(x===null&&y===null)return a[1]-b[1];if(x===null)return 1;if(y===null)return -1;
  const d=(typeof x==='number'&&typeof y==='number')?x-y:String(x).localeCompare(String(y),undefined,{numeric:true});return d?d*st.dir:a[1]-b[1]}).map(x=>x[2])}
function subTable(id,cols,rows){const st=subSort[id]||{};
 let h='<table class="sub"><tr>'+cols.map(c=>'<th data-sub="'+esc(id)+'" data-k="'+c.k+'"'+(st.k===c.k?' class="sorted"':'')+' title="click to sort">'+c.t+(st.k===c.k?(st.dir>0?' ▲':' ▼'):'')+'</th>').join('')+'</tr>';
 for(const r of sortRows(rows,cols,id))h+='<tr>'+cols.map(c=>'<td'+(c.cls?' class="'+c.cls+'"':'')+'>'+c.h(r)+'</td>').join('')+'</tr>';return h+'</table>'}
function collapseScenes(sc){const K=['name','image_key','role','T','C','Z','Y','X','dtype','px_x_um','px_y_um','px_z_um'];const out=[];
 for(const s of [...sc].sort((a,b)=>a.series-b.series)){const l=out[out.length-1];const t0=tsec(s.acquired_start),t1=t0===null?null:t0+(s.duration_s||0);
  if(l&&l.last===s.series-1&&K.every(k=>l[k]===s[k])){l.last=s.series;if(t0!==null){if(l._t0===null||t0<l._t0){l._t0=t0;l.acquired_start=s.acquired_start}l._t1=Math.max(l._t1??t1,t1);l.duration_s=l._t1-l._t0}continue}
  out.push({...s,first:s.series,last:s.series,_t0:t0,_t1:t1})}return out}
const rg=(s,o)=>s.first===s.last?String(s.first+o):(s.first+o)+'–'+(s.last+o);
function when(iso,ref){if(!iso)return'';const t=iso.slice(0,19).replace('T',' ');return ref&&iso.slice(0,10)===String(ref).slice(0,10)?t.slice(11):t}
const objTxt=i=>esc(i.objective||'')+' '+esc(i.magnification?i.magnification+'x':'')+esc(i.na?'/'+i.na:'')+(i.objective_source?' <span class="src">'+esc(i.objective_source)+'</span>':'');
const IMG_COLS=[
 {k:'preview',t:'Preview',v:i=>i.preview?1:0,h:i=>i.preview?'<a href="'+esc(i.preview)+'" target="_blank" data-lb="1" title="click to enlarge (Ctrl+click: new tab)"><img src="'+esc(i.preview)+'" loading="lazy" style="height:64px;max-width:160px;border-radius:3px;display:block"></a>':''},
 {k:'image_key',t:'Image',v:i=>i.image_key,h:i=>esc(i.image_key)},
 {k:'kind',t:'Kind',v:i=>i.kind,h:i=>esc(i.kind)},
 {k:'n_series',t:'Series/tiles',v:i=>i.n_series,h:i=>i.n_series+(i.n_tiles>1?' ('+i.n_tiles+' tiles'+(i.has_merged?' + merged':'')+')':'')+(i.n_previews?' +'+i.n_previews+' previews':'')+(i.variants?'<br><span class="src">variants: '+esc(i.variants)+'</span>':'')},
 {k:'fiji',t:'Fiji series no.',cls:'w',v:i=>firstNum(i.series),h:i=>'<b>'+esc(i.fiji_series_detail||'')+'</b>'+(nz(i.series)!==null?'<br><span class="src">0-based: '+esc(i.series)+'</span>':'')},
 {k:'acquired_start',t:'Start',v:i=>i.acquired_start,h:i=>esc((i.acquired_start||'').replace('T',' '))},
 {k:'total_time_s',t:'Total',v:i=>i.total_time_s,h:i=>esc(dur(i.total_time_s))},
 {k:'dims',t:'T×C×Z×Y×X',v:i=>i.T*i.C*i.Z*i.Y*i.X,h:i=>[i.T,i.C,i.Z,i.Y,i.X].join('×')+' <span class="src">'+esc(i.dtype)+'</span>'},
 {k:'pixel_size_x_um',t:'px µm',v:i=>i.pixel_size_x_um,h:i=>esc(i.pixel_size_x_um??'')},
 {k:'z_step_um',t:'dz µm',v:i=>i.z_step_um,h:i=>esc(i.z_step_um??'')+(i.z_step_source?' <span class="src">'+esc(i.z_step_source)+'</span>':'')},
 {k:'time_delta_s',t:'Δt',v:i=>i.time_delta_s,h:i=>esc(dur(i.time_delta_s))+(i.time_delta_source?' <span class="src">'+esc(i.time_delta_source)+'</span>':'')},
 {k:'magnification',t:'Objective',v:i=>i.magnification,h:objTxt},
 {k:'channels',t:'Channels',v:i=>i.channels,h:i=>esc(i.channel_details||i.channels)},
 {k:'in_zarr',t:'Zarr',cls:'c',v:i=>i.in_zarr?1:0,h:i=>tick(i.in_zarr)},
 {k:'seg',t:'Seg / tables',v:i=>[i.segmentation,i.tables].filter(Boolean).join(' | '),h:i=>esc([i.segmentation,i.tables].filter(Boolean).join(' | '))},
];
function sceneCols(r){return [
 {k:'fiji',t:'Fiji',v:s=>s.first,h:s=>'<b>'+rg(s,1)+'</b>'},
 {k:'series',t:'#',v:s=>s.first,h:s=>'<span class="muted">'+rg(s,0)+'</span>'},
 {k:'name',t:'Series name',v:s=>s.name,h:s=>esc(s.name)},
 {k:'image_key',t:'Image',v:s=>s.image_key,h:s=>esc(s.image_key)},
 {k:'role',t:'Role',v:s=>s.role,h:s=>esc(s.role)},
 {k:'acquired_start',t:'Start',v:s=>tsec(s.acquired_start),h:s=>esc(when(s.acquired_start,r.acquired_start))},
 {k:'duration_s',t:'Duration',v:s=>s.duration_s,h:s=>esc(dur(s.duration_s))},
 {k:'time_delta_s',t:'Δt',v:s=>s.time_delta_s,h:s=>esc(dur(s.time_delta_s))},
 {k:'T',t:'T',v:s=>s.T,h:s=>s.T},{k:'C',t:'C',v:s=>s.C,h:s=>s.C},{k:'Z',t:'Z',v:s=>s.Z,h:s=>s.Z},
 {k:'XY',t:'X×Y',v:s=>s.X*s.Y,h:s=>s.X+'×'+s.Y},
 {k:'dtype',t:'Type',v:s=>s.dtype,h:s=>esc(s.dtype||'')},
 {k:'px',t:'Voxel XYZ µm',v:s=>s.px_x_um,h:s=>[s.px_x_um,s.px_y_um,s.px_z_um].map(v=>v===null||v===undefined?'-':esc(v)).join(' × ')},
]}
function imagesHtml(r){return '<h4>Images ('+r.images.length+')</h4>'+subTable('im:'+r.acq_id,IMG_COLS,r.images)}
function scenesHtml(r){const ns=r.scenes.length,all=!!scAll[r.acq_id],isOpen=scOpen[r.acq_id]??(ns<=40);
 const runs=all?r.scenes.map(s=>({...s,first:s.series,last:s.series})):collapseScenes(r.scenes);
 return '<details data-sc="'+esc(r.acq_id)+'"'+(isOpen?' open':'')+' style="margin-top:6px"><summary>Scenes / series ('+ns+') in <span class="mono">'+esc((r.scene_file||'').split('/').pop())+'</span></summary>'
 +'<label class="cb" style="margin:6px 0 0"><input type="checkbox" data-scall="'+esc(r.acq_id)+'"'+(all?' checked':'')+'>one row per series (otherwise consecutive identical series, e.g. tiles, share a row: start = earliest, duration = until the last one ends)</label>'
 +subTable('sc:'+r.acq_id,sceneCols(r),runs)
 +'<div class="src">Click a column header to sort (again to reverse). Fiji = number in Fiji’s Bio-Formats importer (Series N, 1-based); # = 0-based Bio-Formats series index (Python readers, bioformats2raw zarr group). Same numbering as SceneViewerTool. Duration = first to last plane timestamp of the series.</div></details>'}

// ------------------------------------------------------------ editing (only via serve_catalog.py)
const SRV=window.MDCAT_SERVER||null;let CAN_EDIT=!!(SRV&&SRV.can_edit);
function setMode(){const st=SRV?SRV.status:'';
 $('mode').innerHTML=SRV?((CAN_EDIT?'<b style="color:var(--ok)">editing enabled</b> as '+esc(SRV.user)
   :'<b style="color:var(--warn)">view only</b>'+(st!=='ok'?' – '+esc(SRV.status_msg):(SRV.user&&SRV.user!==SRV.owner?' ('+esc(SRV.user)+' is not the server owner '+esc(SRV.owner)+')':'')))
   +' · <a href="/setup">⚙ Settings</a>')
 :'static copy – to add comments or values, run the catalog server on your machine (see README.txt next to this page)'}
function toast(msg,err){const t=$('toast');t.innerHTML=msg;t.className='toast'+(err?' err':'');t.style.display='block';if(!err&&!/updating/.test(msg))setTimeout(()=>t.style.display='none',4000)}
async function api(path,body){const res=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-MDCAT':'1'},body:JSON.stringify(body)});
 let j={};try{j=await res.json()}catch(e){}if(!res.ok||!j.ok)throw new Error(j.error||('HTTP '+res.status));return j}
function saveUi(){try{sessionStorage.setItem('mdcat_ui',JSON.stringify({open:[...open],q:$('q').value,inst:$('inst').value,stat:$('stat').value,cat:$('cat').value,own:$('own').value,
 cb:['fz','fs','ft','fzs','fm'].filter(i=>$(i).checked),y:window.scrollY,sortKey,sortDir,subSort,scOpen,scAll}))}catch(e){}}
function restoreUi(){let s=null;try{s=JSON.parse(sessionStorage.getItem('mdcat_ui')||'null');sessionStorage.removeItem('mdcat_ui')}catch(e){}if(!s)return;
 s.open.forEach(x=>open.add(x));$('q').value=s.q;['inst','stat','cat','own'].forEach(i=>$(i).value=s[i]);s.cb.forEach(i=>$(i).checked=true);
 sortKey=s.sortKey;sortDir=s.sortDir;Object.assign(subSort,s.subSort);Object.assign(scOpen,s.scOpen);Object.assign(scAll,s.scAll);render();setTimeout(()=>window.scrollTo(0,s.y),0)}
async function waitAndReload(gen){toast('Saved. Updating catalog …');for(let i=0;i<600;i++){await new Promise(r=>setTimeout(r,1500));
  try{const s=await (await fetch('/api/state')).json();if(s.generation>gen&&!s.build.running&&!s.build.pending){if(s.build.last_ok===false){toast('Saved, but the catalog update failed: '+esc(s.build.last_error||''),true);return}saveUi();location.reload();return}}catch(e){}}
 toast('Saved – the catalog update takes long; reload the page later.',true)}
async function doEdit(path,body){try{const j=await api(path,body);await waitAndReload(j.generation)}catch(e){toast('Not saved: '+esc(e.message),true)}}
function commentsHtml(r){const cs=r.comments||[];if(!cs.length&&!CAN_EDIT)return'';let h='<div class="box"><b>Comments</b>';
 for(const c of cs)h+='<div class="cm">'+esc(c.text)+'<div class="src">'+esc(c.user)+' · '+esc((c.ts||'').replace('T',' '))+(c.image_key?' · image '+esc(c.image_key):'')
  +(CAN_EDIT?'<button class="x" title="delete comment" data-act="cdel" data-cid="'+esc(c.comment_id)+'">×</button>':'')+'</div></div>';
 if(!cs.length)h+='<div class="muted">No comments yet.</div>';
 if(CAN_EDIT){const imgs=r.images.filter(i=>i.image_key!=='*');h+='<div style="margin-top:6px"><textarea id="ct_'+r.acq_id+'" placeholder="Add a comment (what is in this dataset, problems, where results are, …)"></textarea>'
  +(imgs.length>1?'<label class="muted">about image <select id="ci_'+r.acq_id+'"><option value="">whole dataset</option>'+imgs.map(i=>'<option>'+esc(i.image_key)+'</option>').join('')+'</select></label> ':'')
  +'<button data-act="cadd" data-acq="'+esc(r.acq_id)+'">Add comment</button></div>'}
 return h+'</div>'}
function valuesHtml(r){const ns=r.needs||[],os=r.overrides||[];if(!CAN_EDIT&&!os.length)return'';let h='<div class="box"><b>Manual values</b>';
 if(CAN_EDIT&&ns.length){h+='<div style="color:var(--warn);margin:4px 0">Missing values – enter what you know:</div><table class="sub">';
  ns.forEach((n,k)=>{h+='<tr><td>'+esc(n.field)+'</td><td>'+(n.image_key==='*'?'all images':esc(n.image_key))+'</td><td><input class="v" id="nv_'+r.acq_id+'_'+k+'" placeholder="'+esc(n.hint)+'"></td><td><button data-act="need" data-acq="'+esc(r.acq_id)+'" data-k="'+k+'">Save</button></td></tr>'});h+='</table>'}
 if(os.length){h+='<table class="sub"><tr><th>Field</th><th>Image</th><th>Value</th><th>Note</th>'+(CAN_EDIT?'<th></th>':'')+'</tr>';
  for(const o of os)h+='<tr><td>'+esc(o.field)+'</td><td>'+esc(o.image_key==='*'?'all images':o.image_key)+'</td><td>'+esc(o.value)+'</td><td class="muted">'+esc(o.note)+'</td>'
   +(CAN_EDIT?'<td><button class="x" title="remove this value" data-act="odel" data-acq="'+esc(r.acq_id)+'" data-img="'+esc(o.image_key)+'" data-field="'+esc(o.field)+'" data-val="'+esc(o.value)+'">×</button></td>':'')+'</tr>';h+='</table>'}
 if(CAN_EDIT){const imgs=r.images.filter(i=>i.image_key!=='*');const fields=[...D.image_fields,...D.dataset_fields.filter(f=>f!=='link_attachment')];
  h+='<div style="margin-top:6px" class="muted">Set / correct a value: <select id="sf_'+r.acq_id+'">'+fields.map(f=>'<option>'+f+'</option>').join('')+'</select> '
   +'<select id="si_'+r.acq_id+'"><option value="*">all images</option>'+imgs.map(i=>'<option>'+esc(i.image_key)+'</option>').join('')+'</select> '
   +'<input class="v" id="sv_'+r.acq_id+'" placeholder="value"> <button data-act="oset" data-acq="'+esc(r.acq_id)+'">Save</button>'
   +'<div class="src">image fields ('+D.image_fields.join(', ')+') apply to the selected image; others to the dataset. Formats: '+Object.entries(D.field_hints).filter(([k])=>k!=='link_attachment').map(([k,v])=>esc(k)+' = '+esc(v)).join('; ')+'</div></div>'}
 return h+'</div>'}
$('tb').addEventListener('click',e=>{const b=e.target.closest('button[data-act]');if(!b)return;e.stopPropagation();const a=b.dataset.act,acq=b.dataset.acq,row=D.rows.find(r=>r.acq_id===acq);
 if(a==='cadd'){const txt=$('ct_'+acq).value.trim();if(!txt)return toast('Comment is empty',true);const s=$('ci_'+acq);doEdit('/api/comment',{acq_id:acq,image_key:s?s.value:'',text:txt})}
 else if(a==='cdel'){if(confirm('Delete this comment? (it stays in the history log)'))doEdit('/api/comment/delete',{comment_id:b.dataset.cid})}
 else if(a==='need'){const n=row.needs[+b.dataset.k],v=$('nv_'+acq+'_'+b.dataset.k).value.trim();if(!v)return toast('Enter a value first',true);doEdit('/api/value',{acq_id:acq,image_key:n.image_key,field:n.field,value:v})}
 else if(a==='oset'){const v=$('sv_'+acq).value.trim();if(!v)return toast('Enter a value first',true);doEdit('/api/value',{acq_id:acq,image_key:$('si_'+acq).value,field:$('sf_'+acq).value,value:v})}
 else if(a==='odel'){if(confirm('Remove this manual value?'))doEdit('/api/value',{acq_id:acq,image_key:b.dataset.img,field:b.dataset.field,value:'',old_value:b.dataset.val})}
 else if(a==='link'){const sel=$('lk_'+b.dataset.i);if(!sel.value)return toast('Choose a dataset',true);doEdit('/api/value',{acq_id:sel.value,image_key:'',field:'link_attachment',value:b.dataset.rel})}});
function detail(r){const im=r.images;let h='';
 h+='<div class="mono">'+esc(r.acq_id)+' · '+esc(r.path)+'</div>';
 if(r.notes)h+='<div><b>Notes:</b> '+esc(r.notes)+'</div>';if(r.error)h+='<div style="color:var(--bad)"><b>Error:</b> '+esc(r.error)+'</div>';
 if(r.needs_input&&!CAN_EDIT)h+='<div style="color:var(--warn)"><b>Needs input:</b> '+esc(r.needs_input)+(SRV?'':' (enter it via the catalog server)')+'</div>';
 h+=commentsHtml(r)+valuesHtml(r);
 if(im.length)h+=imagesHtml(r);
 if(r.scenes&&r.scenes.length)h+=scenesHtml(r);
 if(r.files.length){h+='<h4>Files ('+r.files.length+')</h4><table><tr><th>Role</th><th>Path (relative to root)</th><th>Size</th><th>Status</th></tr>';
  for(const f of r.files)h+='<tr><td>'+esc(f.role)+'</td><td class="mono">'+esc(f.rel)+'</td><td>'+esc(size(f.size_bytes))+'</td><td>'+esc(f.status||'')+(f.error?' <span style="color:var(--bad)">'+esc(f.error).slice(0,200)+'</span>':'')+'</td></tr>';h+='</table>'}
 h+='<div class="src">first seen '+esc(r.first_seen)+' · last changed '+esc(r.last_changed)+'</div>';return h}
function render(){const rows=D.rows.filter(match).sort(cmp);
 document.querySelectorAll('#hr th').forEach(th=>th.classList.toggle('sorted',th.dataset.k===sortKey));
 const out=[];for(const r of rows){out.push('<tr class="r" data-id="'+esc(r.acq_id)+'">'+COLS.map(c=>'<td'+(c[3]?' class="'+c[3]+'"':'')+'>'+c[2](r)+'</td>').join('')+'</tr>');
  if(open.has(r.acq_id))out.push('<tr class="d"><td colspan="'+COLS.length+'">'+detail(r)+'</td></tr>')}
 $('tb').innerHTML=out.join('');$('count').textContent=rows.length+' of '+D.rows.filter(r=>r.present).length+' datasets';
 document.querySelectorAll('tr.r').forEach(tr=>tr.onclick=()=>{const id=tr.dataset.id;open.has(id)?open.delete(id):open.add(id);render()})}
const dsOpts='<option value="">– link to dataset –</option>'+[...D.rows].filter(r=>r.present).sort((a,b)=>(a.instrument+a.session+a.name).localeCompare(b.instrument+b.session+b.name)).map(r=>'<option value="'+esc(r.acq_id)+'">'+esc(r.instrument+' / '+r.session+' / '+r.name)+'</option>').join('');
function renderUnas(){$('unasb').innerHTML=D.unassigned.length?'<table><tr><th>Category</th><th>Instrument</th><th>Session</th><th>Path</th><th>Size</th>'+(CAN_EDIT?'<th>Link</th>':'')+'</tr>'+D.unassigned.map((x,i)=>'<tr><td>'+esc(x.category)+'</td><td>'+esc(x.instrument)+'</td><td>'+esc(x.session)+'</td><td class="mono">'+esc(x.rel)+'</td><td>'+esc(size(x.size))+'</td>'+(CAN_EDIT?'<td><select id="lk_'+i+'" style="max-width:260px">'+dsOpts+'</select> <button data-act="link" data-i="'+i+'" data-rel="'+esc(x.rel)+'">Link</button></td>':'')+'</tr>').join('')+'</table><p class="muted">Link a file to a dataset with a manual_overrides.csv row: acq_id, *, link_attachment, &lt;path&gt;.</p>':'<p class="muted">None.</p>';}
$('sessb').innerHTML=D.sessions.length?D.sessions.map(s=>'<h4>'+esc(s.instrument)+' / '+esc(s.session)+'</h4><pre>'+esc(s.notes)+'</pre>').join(''):'<p class="muted">None.</p>';
$('tb').addEventListener('click',e=>{const th=e.target.closest('th[data-sub]');if(!th)return;e.stopPropagation();const id=th.dataset.sub,k=th.dataset.k,st=subSort[id];
 subSort[id]=st&&st.k===k?{k,dir:-st.dir}:{k,dir:1};render()});
$('tb').addEventListener('change',e=>{const id=e.target.dataset&&e.target.dataset.scall;if(id===undefined)return;scAll[id]=e.target.checked;render()});
$('tb').addEventListener('toggle',e=>{const id=e.target.dataset&&e.target.dataset.sc;if(id!==undefined)scOpen[id]=e.target.open},true);
for(const r of D.rows)r.scenes=(r.scenes||[]).map(a=>Object.fromEntries(D.scene_keys.map((k,i)=>[k,a[i]])));
$('unasb').addEventListener('click',e=>{const b=e.target.closest('button[data-act=link]');if(!b)return;const sel=$('lk_'+b.dataset.i);if(!sel.value)return toast('Choose a dataset',true);doEdit('/api/value',{acq_id:sel.value,image_key:'',field:'link_attachment',value:b.dataset.rel})});
// --- preview overlay: click a preview to enlarge it (Esc / click to close, arrow keys = previous/next)
let LB=-1;
function lbLinks(){return [...document.querySelectorAll('a[data-lb]')]}
function lbShow(k){const ls=lbLinks();if(!ls.length)return;LB=(k+ls.length)%ls.length;const a=ls[LB],im=$('lbimg');
 im.onload=()=>{const s=Math.min(2,innerWidth*.96/im.naturalWidth,innerHeight*.88/im.naturalHeight);im.style.width=Math.round(im.naturalWidth*s)+'px';im.style.height='auto'};
 im.src=a.getAttribute('href');$('lbcap').innerHTML=(LB+1)+' / '+ls.length+' · Esc or click to close · ← → previous / next · <a href="'+esc(a.getAttribute('href'))+'" target="_blank">open in new tab</a>';
 $('lb').style.display='flex'}
function lbHide(){$('lb').style.display='none';$('lbimg').removeAttribute('src');LB=-1}
document.addEventListener('click',e=>{const a=e.target.closest('a[data-lb]');
 if(a&&!e.ctrlKey&&!e.metaKey&&!e.shiftKey&&e.button===0){e.preventDefault();e.stopPropagation();lbShow(lbLinks().indexOf(a));return}
 if(LB>=0&&e.target.closest('#lb')&&!e.target.closest('#lbcap a')){e.preventDefault();lbHide()}},true);
document.addEventListener('keydown',e=>{if(LB<0)return;if(e.key==='Escape')lbHide();else if(e.key==='ArrowRight')lbShow(LB+1);else if(e.key==='ArrowLeft')lbShow(LB-1);else return;e.preventDefault()});
setMode();renderUnas();stats();render();restoreUi();
</script></body></html>
"""
