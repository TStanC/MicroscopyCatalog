"""MCP server for the microscopy catalog: lets an AI agent (Claude Code, ...) search the catalog
and add comments.

    .venv\\Scripts\\python.exe mcp_server.py [--port N]       (started by the MCP client over stdio)

A thin client of the catalog server (serve_catalog.py) on http://127.0.0.1:<server_port>: it
reads nothing from disk itself. The catalog server must be running (autostart / Start menu).

* Searches use the server's read-only JSON API (/api/overview, /api/search, /api/dataset,
  /api/comments, /api/sql).
* Comments are sent with the header ``X-MDCAT-Agent: mcp``: the server stores them under the
  Windows user who runs this process plus ``_mcp`` (e.g. ``go24meg_mcp``), and lets this agent
  delete only comments of that ``_mcp`` user. Values, settings and rebuilds stay with humans.

Register once per Windows user (in the program folder):

    claude mcp add microscopy-catalog --scope user -- "<program folder>\\.venv\\Scripts\\python.exe" "<program folder>\\mcp_server.py"

Needs the ``mcp`` dependency group: ``install.ps1 -Mcp`` (or ``uv sync --group mcp``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from mdcat import config as mcfg  # noqa: E402

TIMEOUT_S = 60
PORT = 8766

INSTRUCTIONS = """\
Catalog of the microscopy datasets in the lab's Labdata folder (read from the files' metadata).
A dataset (table `acquisitions`, key `acq_id`) is one raw acquisition file with all identical
copies, its zarr conversions, segmentations, exports and other related files. It contains one or more
images (table `images`), each with T/C/Z/Y/X dimensions, objective, pixel size, z step,
time step and channels; `scenes` lists every Bio-Formats series (Fiji series number =
`fiji`). Paths are relative to the Labdata root.

Start with `catalog_overview` (instruments, statuses, counts, table columns), find datasets with
`search_datasets`, then `get_dataset` for the details. `sql_query` runs any SELECT on the
catalog database when the filters are not enough.

Comments: `add_comment` stores a comment under your user's name plus "_mcp". New comments
and deletions are *pending* until the next catalog build applies them (usually a few minutes);
`get_dataset` and `search_comments` list them under `pending...`. You may only delete comments
that were written through this MCP by the same user. Write comments for people: short, factual,
and only when the user asked for it.
"""

mcp = MCPServer("microscopy-catalog", instructions=INSTRUCTIONS)
READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)


# ----------------------------------------------------------------------------- HTTP

def _request(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    url = f"http://127.0.0.1:{PORT}{path}"   # IPv4: the server identifies the Windows user by the TCP connection
    if params:
        q = {k: _q(v) for k, v in params.items() if v is not None and v != ""}
        if q:
            url += "?" + urllib.parse.urlencode(q)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "X-MDCAT": "1", "X-MDCAT-Agent": "mcp", "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read().decode("utf-8")).get("error") or exc.reason
        except (ValueError, AttributeError):
            msg = exc.reason
        raise ToolError(f"catalog server: {msg} (HTTP {exc.code})") from None
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        raise ToolError(f"the microscopy catalog server is not reachable on http://localhost:{PORT} ({exc}). "
                        f"Start it: Start menu -> 'Microscopy Catalog' (or tray icon).") from None


def _q(v: Any) -> str:
    return ("1" if v else "0") if isinstance(v, bool) else str(v)


def _strip(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "ok"}


# ----------------------------------------------------------------------------- tools

@mcp.tool(annotations=READ)
def catalog_overview() -> dict[str, Any]:
    """Overview of the catalog: number of datasets/images/comments, acquisition date range, counts per
    status / instrument / owner / kind / format, presence flags (has_zarr, has_segmentation, ...),
    the last build, pending edits, and the columns of every database table (for sql_query)."""
    return _strip(_request("GET", "/api/overview"))


@mcp.tool(annotations=READ)
def search_datasets(text: str = "", instrument: str = "", session: str = "", owner: str = "", status: str = "",
                    kind: str = "", format: str = "", date_from: str = "", date_to: str = "",
                    has_zarr: bool | None = None, has_segmentation: bool | None = None,
                    has_tables: bool | None = None, has_rois: bool | None = None,
                    has_processed: bool | None = None, has_comments: bool | None = None,
                    min_T: int | None = None, min_Z: int | None = None, min_C: int | None = None,
                    min_total_time_s: float | None = None, magnification: float | None = None,
                    objective: str = "", include_missing: bool = False, sort: str = "date_desc",
                    limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Find datasets. All given filters must match.

    text: words that must all occur (case-insensitive) in name, path, instrument, session, objective,
        channels, notes, segmentation/table names - or in a comment of the dataset.
    instrument / session / owner / status / kind / format: exact value (case-insensitive), see
        catalog_overview. status: ok, needs_input, partial, incomplete, unreadable, excluded, missing.
        kind: static, timelapse, overview. owner: self or other. format: file extension, e.g. lif, vsi.
    date_from / date_to: acquisition date YYYY-MM-DD (inclusive).
    has_*: presence flags (True/False). min_T / min_Z / min_C: minimum number of timepoints / z planes /
        channels. min_total_time_s: minimum time span. magnification: e.g. 20. objective: substring.
    include_missing: also datasets no longer on disk (status missing).
    sort: date_desc, date_asc, size_desc or name. limit (max 500) / offset: paging.
    Returns {"total": N, "rows": [...]} with compact rows (acq_id, name, path, dims_TCZYX, ...)."""
    p = {k: v for k, v in locals().items()}
    return _strip(_request("GET", "/api/search", p))


@mcp.tool(annotations=READ)
def get_dataset(acq_id: str, max_scenes: int = 50) -> dict[str, Any]:
    """All details of one dataset: the dataset record, its images (dimensions, pixel size, z step, Δt,
    objective, channels, Fiji series numbers), all files (original, duplicates, zarr, segmentation, ...),
    the first max_scenes Bio-Formats series (n_scenes = total), values still missing, active comments
    and comments still pending in the inbox."""
    return _strip(_request("GET", "/api/dataset", {"acq_id": acq_id, "max_scenes": max_scenes}))


@mcp.tool(annotations=READ)
def search_comments(text: str = "", user: str = "", acq_id: str = "", limit: int = 50) -> dict[str, Any]:
    """Active comments, newest first, with the dataset's name and path. text: words that must all occur;
    user: author (e.g. "go24meg" or "go24meg_mcp"); acq_id: one dataset. "pending" lists comment
    additions/deletions not applied yet."""
    return _strip(_request("GET", "/api/comments", {"text": text, "user": user, "acq_id": acq_id, "limit": limit}))


@mcp.tool(annotations=READ)
def sql_query(sql: str, limit: int = 200) -> dict[str, Any]:
    """Run one read-only SELECT on the catalog database (SQLite; tables acquisitions, images, scenes,
    locations, attachments, comments, needs_input, runs, sources - columns: catalog_overview). Comments are
    an append-only log: active = action 'add' whose comment_id has no 'delete' row. At most limit
    (<= 500) rows; queries are stopped after 10 s."""
    return _strip(_request("POST", "/api/sql", body={"sql": sql, "limit": limit}))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                      open_world_hint=False))
def add_comment(acq_id: str, text: str, image_key: str = "") -> dict[str, Any]:
    """Add a comment to a dataset (optionally about one image: image_key from get_dataset's images).
    Stored under "<Windows user>_mcp". It is pending until the next catalog build (minutes) and then
    visible to everybody on the catalog page. Returns the inbox entry; its "id" is the comment_id."""
    return _strip(_request("POST", "/api/comment", body={"acq_id": acq_id, "text": text, "image_key": image_key}))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True,
                                      open_world_hint=False))
def delete_own_comment(comment_id: str) -> dict[str, Any]:
    """Delete a comment that this MCP user ("<Windows user>_mcp") wrote - applied or still pending.
    Comments of other users cannot be deleted. Nothing is lost: the log keeps a 'delete' row."""
    return _strip(_request("POST", "/api/comment/delete", body={"comment_id": comment_id}))


def main(argv=None) -> int:
    global PORT
    ap = argparse.ArgumentParser(description="MCP server (stdio) for the microscopy catalog")
    ap.add_argument("--port", type=int, help="catalog server port (default: server_port of settings.toml)")
    args = ap.parse_args(argv)
    PORT = args.port or mcfg.load_config().server_port
    mcp.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
