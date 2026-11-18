# Microscopy dataset catalog

A read-only, incremental catalog of all microscopy data in a Labdata folder, with
comments, manual values and small preview images, editable in the browser.
Installation: see [INSTALL.md](INSTALL.md).

## Where things are

| What | Where |
|---|---|
| Program (this git repository) incl. Python, Java, Bio-Formats | a local folder, e.g. `%LOCALAPPDATA%\Programs\microscopy-catalog` |
| Settings of this machine | `settings.toml` in the program folder (written by the setup page) |
| **Catalog folder** - the master copy: database (incl. comments), manual values, previews, raw-metadata cache, edit log, published page | chosen on the setup page, e.g. `X:\Titus\Labdata\MicroscopyCatalog\` |
| Catalog for everybody (read-only) | `<catalog folder>\index.html` |
| Catalog with editing | `http://localhost:8766` on the machine running the server |
| Local working copy + logs | `.work\` in the program folder |

SQLite must not be written live over a network share (a dropped connection can corrupt it), so
the server works on the local copy in `.work\` and copies every change into the catalog folder
within seconds (each build pushes a consistent snapshot; `sync.json` there carries a generation
number). When the catalog folder is newer than the local copy (another machine published, fresh
install), the next build pulls it first. If the catalog folder is not reachable, the server shows
the local copy read-only and retries every minute.

## Server

Started by the autostart shortcut or the **Microscopy Catalog** Start-menu shortcut
(`.venv\Scripts\pythonw.exe serve_catalog.py`, no console window). It
* shows an icon in the notification area: left click opens the catalog; right click gives Open
  catalog, Update now, Settings, Stop; the colour shows the status (`mdcat/tray.py`);
* serves the catalog on `http://localhost:8766` (this machine only). Only the Windows user who
  runs the server can edit; other users of the machine (e.g. via RDP) see it read-only;
* shows the setup page (`/setup`, **⚙ Settings** in the page header) on first start;
* after an edit rebuilds the catalog (a few seconds; previews skipped) and the page reloads;
* runs an incremental update every `auto_update_min` (60) minutes and makes new previews in a
  separate process;
* holds `server.lock` in the catalog folder (heartbeat every 2 min). A second server (another
  machine/user) stays read-only and takes over when the lock is older than 10 minutes. Builds
  started by hand on another machine refuse to write (exit code 5);
* logs to `.work\logs\server.log`; every edit is also recorded in `logs\web_edits.log` (copied to
  the catalog folder).

### Editing in the browser

Click a dataset row:
* **Comments**: free text per dataset (optionally about one image), with author and time.
  Stored in the `comments` table of `catalog.sqlite` as an append-only log (deleting adds a
  `delete` row; nothing is lost). Searchable; 💬 marks datasets with comments.
* **Manual values**: missing values (status `needs_input`) get input fields; any value can be
  set or corrected ("Set / correct a value"), and manual values can be removed again. Values
  are checked immediately (e.g. NA must be a number). Stored in `manual_overrides.csv`.
* **Unassigned files** (bottom of the page): link a file to a dataset with a dropdown.

## Preview images

Per image one small JPEG ("contact sheet"): one row per channel plus a colour composite
(fluorescence only - brightfield/transmitted channels with a bright background are left
out of it); columns = overview of the middle Z plane (heavily downsampled), its max
projection over Z (at most 10 evenly spaced Z planes), and a 1:1 cutout at full resolution
(max over Z). The cutout is placed where the overview shows the most signal, so even mostly
empty images show what is there.
First timepoint; tile scans use the merged mosaic or the middle tile; the subtitle names the
series (Fiji number) and the cutout position. Pixels are read read-only with Bio-Formats
(pyramid levels where available, strip-wise otherwise). Previews are cached per file
(size + modification time); a moved/renamed file gets a new preview. They are made by
`make_previews.py`, a separate process with its own lock, so catalog builds (and edits) are never
blocked by it: the server runs it 90 s after start and then every `auto_update_min`, spending at
most `preview_budget_min` (20) minutes per run (newest datasets first); afterwards a quick build
links and publishes the new previews. Roughly 10-30 s per file (deep z-stacks and large mosaics
take longest). How much is read per image is set by `previews` (setup page):
`network` (default) reads at most ~300 MB of pixel data per image - suited for VPN; `full` up to
~3 GB (more Z planes for huge stacks); `off` makes none. By hand: `.venv\Scripts\python.exe make_previews.py [--budget MIN]`, or
`build_catalog.py --preview-budget MIN` (inline). Shown in the image table of a dataset (click to enlarge).

## Running a build by hand

```powershell
cd <program folder>
.venv\Scripts\python.exe build_catalog.py
```

| Option | Effect |
|---|---|
| `--interactive` | ask in the terminal for values that could not be read |
| `--retry-errors` | re-read files that failed before although they did not change |
| `--force` | re-read every file (normally only new/changed files are read) |
| `--verify-readonly` | list the data folder again at the end and report any difference |
| `--no-previews` / `--preview-budget MIN` | no new previews / time for new previews in this run |
| `--no-publish` | do not copy the result into the catalog folder |
| `--only TEXT --out DIR` | test on paths containing TEXT, in a separate working folder (never pulls or publishes) |

Works on the same working copy as the server (`.work`); if the server is building at the same
moment, the second build exits with "Another catalog build is running" (code 3).

A run first lists the whole tree (seconds) and computes a change signature per
dataset (size + modification time of the file and its companion folder; for zarr
stores the metadata files and `labels/`, `tables/`, `rois/` listings). Only new or
changed datasets are read. Files modified within the last `recent_minutes` (config)
are treated as still being written and are read in a later run.

## Outputs

In the catalog folder (and the working copy `.work\`):

| File | Content |
|---|---|
| `catalog.html` (published as `index.html`) | interactive overview: search, filters, sort; click a row for comments, values, images (with previews), scenes and files |
| `CATALOG.md` | static overview grouped by instrument and session, ☑/☐ for what exists |
| `catalog.sqlite` | full database (`acquisitions`, `images`, `scenes`, `locations`, `attachments`, `needs_input`, `runs`, `sources`, `comments`) |
| `catalog_datasets.csv` / `catalog_images.csv` / `catalog_files.csv` / `catalog_scenes.csv` | flat tables (Excel-compatible, UTF-8) |
| `catalog.json` | nested export (dataset -> images, files, scenes, comments) |
| `comments.csv` | full comment log (export of the `comments` table) |
| `previews/` | preview images |
| `needs_input.csv` | values that could not be determined automatically (alternative to the browser) |
| `manual_overrides.csv` | manual values (browser, `needs_input.csv` or edited by hand) |
| `CHANGELOG.md` | new / changed / removed datasets per run |
| `metadata/` (`.work\cache\xml`, `orig`) | cached raw metadata (OME-XML), so changes of the parsing logic never need the data again |
| `sync.json`, `server.lock` | generation of the catalog folder; which server maintains it |
| `.work\logs\` | build, preview and server logs (only `web_edits.log` is copied to the catalog folder) |

`scenes.py` (command line) looks up exact series numbers, see below.

## Data model

* **Dataset** (`acquisitions`, one row each): a raw file (`.vsi`, `.lif`, `.xlef`, `.czi`, `.ims`, `.tif`, ...)
  with all identical copies merged, a zarr store without an original (`zarr_only`), or a
  low-metadata `snapshot` (`.jpg`, `.avi`). `acq_id` is derived from the acquisition
  timestamps + dimensions, so it stays the same when files are moved or copied.
* **Image** (`images`): logical images inside a dataset. A `.lif` gets one row per image;
  all tiles of a tile scan, its `_Merged` mosaic and its previews form one image
  (`n_tiles`, `has_merged`, `n_previews`). Olympus macro/label images are kind `aux`.
  `fiji_series` / `fiji_series_detail` give the exact series numbers of the image
  (e.g. `tiles 7-26; variant ICC 27-38`), `series` the same 0-based.
* **Scene** (`scenes`, `catalog_scenes.csv`): one row per Bio-Formats series of the file the
  metadata was read from, with its name, dimensions, catalog image and role
  (`tile`, `merged`, `image`, `variant ICC`, `preview`, `aux`, `other`).
* **Files** (`locations`): every file belonging to a dataset with its role:
  `original`, `duplicate`, `zarr`, `segmentation`, `processed`, `export`, `metadata`, `experiment_file`.
  Originals and zarr stores are matched by acquisition timestamp + dimensions
  (name rule of `convert*.ps1` as fallback). Other files are matched by name;
  files that cannot be matched unambiguously are listed as *unassigned*.

Presence flags: `has_original`, `has_zarr` (+ `n_zarr`), `has_segmentation` (zarr
`labels/` or segmentation csv/zip), `has_tables` (microio tables other than
`axes_trajectory`), `has_rois`, `has_processed`, `has_exports`, `has_metadata_files`.

`status`: `ok`, `needs_input`, `partial` (only an embedded preview is readable, e.g. a
`.vsi` whose `_name_` folder is missing), `incomplete` (still being written),
`unreadable`, `excluded`, `missing` (no longer on disk; kept for history).

### Where values come from (`*_source` columns)

| Value | Order of sources |
|---|---|
| acquisition start | OME `AcquisitionDate` -> date in file/folder name (`filename`) -> file time (snapshots only) -> manual |
| total time | first timestamp to last plane timestamp (`AcquisitionDate` + max `DeltaT`) |
| Δt | median step of per-timepoint `Plane.DeltaT` -> `Pixels.TimeIncrement` -> microio repair in zarr -> manual |
| z step | `PhysicalSizeZ` -> median stage `PositionZ` step -> microio repair in zarr -> manual |
| objective / magnification / NA | OME `Objective` -> parsed from objective model (`40x/1.10`) -> vendor original metadata -> `NNx` in file name (magnification only) -> manual |

## Series numbers in multi-image files (`scenes.py`)

Numbering is identical to `X:\Titus\Analysis\SceneViewerTool` (checked series by series):
**Fiji** = 1-based "Series N" in Fiji's Bio-Formats importer, **#** = 0-based Bio-Formats
index (Python readers; group name inside a bioformats2raw zarr). Identical copies of a file
have the same numbering. In `catalog.html` click a dataset: the image table has a
"Fiji series no." column, and "Scenes / series" lists every series with its start,
duration (first to last plane timestamp), Δt and dimensions. Both tables sort by clicking
a column header (again to reverse); "one row per series" un-collapses runs of identical tiles.

```powershell
$py = ".venv\Scripts\python.exe"     # in the program folder
& $py scenes.py 251111_Caax_40x.lif          # words that must occur in the path, or a path / acq_id
& $py scenes.py "SP8 251202" --all           # one line per series instead of collapsed runs
& $py scenes.py X:\some\other\file.lif       # not in the catalog -> read with SceneViewerTool
& $py scenes.py 251111_Caax_40x.lif --live   # force a fresh read with SceneViewerTool
```

`--tsv` gives tab-separated output. The catalog lookup needs no file access; the
SceneViewerTool path is set by `sceneviewer` (add it to `settings.toml`).

## Manual input without the browser

1. Open `.work\needs_input.csv`, fill the `value` column (dates `YYYY-MM-DD [HH:MM]`,
   durations `300`, `5min`, `01:30:00`, magnification `60`, NA `1.3`, dims `T,C,Z,Y,X`).
2. Run the script again: the values move to `manual_overrides.csv` and are applied.

Or use `--interactive`. `manual_overrides.csv` rows: `acq_id, image_key, field, value, note`.
`image_key` `*` = all images of the dataset. Extra fields: `exclude` (true), `owner`,
`notes`, `name`, `link_attachment` (value = path of an unassigned file, relative to the data root).

## Read-only guarantee

* Every write goes through `mdcat/safety.py`, which refuses any path inside the Labdata root.
* Single exception: the catalog folder. Inside the Labdata root it must be a folder directly in
  it; it is created by the tool (marker file `.mdcat-publish`; an existing non-empty folder
  without it is refused), only whitelisted file names are written (atomically, `.tmp` +
  rename), only previews and `server.lock` are ever deleted, and the scan skips every top-level
  folder carrying the marker. Nothing is written there with `--no-publish`, `--only` or `--out`.
* Pixel data for previews is only read (Bio-Formats `openBytes`).
* Metadata is read with Bio-Formats (`java/OmeDump.java`, `bioformats_package.jar`) without
  the Memoizer, so no `.bfmemo` files are created; zarr stores are only read (directly, or with
  microio in mode `"r"` if installed).
* `--verify-readonly` lists the tree again after the run and reports differences.

## Configuration

`config.toml` holds the defaults of every option (do not edit). `settings.toml` (setup page;
any option can be added by hand) overrides them key by key; `.runtime\runtime.json`
(install.ps1) sets Java and Bio-Formats. Options: Labdata and catalog folder, update interval,
previews, server port, excluded folders (`HERA_Crashmonitor`, `Overviews_Comparisons`,
`_temp`, hidden folders), other-owner folders (`otherPeopleData`), file types, folder names for
processed/metadata/converted data.
