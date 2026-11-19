# Installing the microscopy catalog

Windows 10/11, no admin rights, nothing needs to be installed beforehand (not even Python or
conda). You need the network drive with the Labdata folder (e.g. `X:`), via VPN if you are not
in the institute network.

## 1. Get the program

Choose a local folder for the program, for example `%LOCALAPPDATA%\Programs\microscopy-catalog`
(not on the network drive).

With git:

```powershell
git clone <repository URL> "$env:LOCALAPPDATA\Programs\microscopy-catalog"
```

Without git: on GitHub, **Code → Download ZIP**, then unzip it into that folder.

## 2. Install

```powershell
cd "$env:LOCALAPPDATA\Programs\microscopy-catalog"
powershell -ExecutionPolicy Bypass -File install.ps1 -Autostart
```

`-Autostart` starts the server automatically when you log in, silently: only its icon appears in
the notification area. Leave it out if you would rather start it yourself with the
**Microscopy Catalog** shortcut, which the installer puts into the Start menu and the program folder.

The script puts everything into the program folder, about 1 minute and 100–400 MB:

| Folder | Content |
|---|---|
| `.tools\` | uv, a Python package manager (skipped if `uv` is already on the PATH) |
| `.python\`, `.venv\` | Python 3.12 with numpy, Pillow and pystray (tray icon), in the exact versions from `uv.lock` |
| `.runtime\jdk\` | Java (Temurin JDK 21). Only downloaded if no JDK 11 or newer is found on the machine. |
| `.runtime\bioformats\` | `bioformats_package.jar` (Bio-Formats 8.5.0, verified by checksum) |
| `.runtime\runtime.json` | which Java and Bio-Formats are used |
| `.work\` | local working copy of the catalog (created by the server) |
| `settings.toml` | your settings (created by the setup page) |

**Java:** the script uses a JDK that is already installed if it finds one (JDK 11 or newer, with
`javac`). It looks in this order:

1. `-JavaHome <folder>`
2. the JDK used by the previous install
3. `JAVA_HOME`
4. the PATH
5. the usual folders under `C:\Program Files`
6. JDKs inside conda environments

Use `-DownloadJdk` to always use the script's own JDK instead.

## 3. Set up

At the end the installer opens `http://localhost:8766/setup` (or another port if 8766 is taken, see Troubleshooting):

1. **Labdata folder**: e.g. `X:\Titus\Labdata`. **Browse…** opens a folder dialog, which may
   appear behind the browser window.
2. **Catalog folder**: e.g. `X:\Titus\Labdata\MicroscopyCatalog`.
   - If the folder already contains a catalog, that catalog is used.
   - If it is empty or new, a new catalog is built. The first build reads the metadata of every
     file, which takes hours for a large Labdata folder.
3. **Save and start**. The server restarts and opens the catalog.

You can change the settings later with **⚙ Settings** in the header of the catalog page, or at
`http://localhost:8766/setup`.

## Using it

- **Start:** the autostart shortcut, or **Microscopy Catalog** in the Start menu. No console
  window opens. If the server is already running, the shortcut just opens the page.
- **Tray icon** (bottom right of the taskbar; Windows may put it under the **^** arrow at first,
  and you can drag it next to the clock):
  - **Left click:** open the catalog.
  - **Right click:** a menu with **Open catalog**, **Update now** (look for new data and make
    missing previews), **Settings** and **Stop catalog server**.
  - **Colour:** green = running, orange = offline or read-only, grey = starting, blue = not set
    up yet. A white dot in the corner means an update or previews are running.
  - **Hover:** the tooltip says what is going on.
- **Page:** `http://localhost:8766`. Click a dataset to add comments or missing values.
- **Stop:** tray icon → **Stop catalog server**, or `powershell -ExecutionPolicy Bypass -File install.ps1 -Stop`

Anyone can open the catalog page read-only, without a server, at
`<catalog folder>\index.html`.

## Updating

```powershell
cd "$env:LOCALAPPDATA\Programs\microscopy-catalog"
git pull                    # or download the ZIP again and unzip it over the folder
powershell -ExecutionPolicy Bypass -File install.ps1
```

The script only downloads what changed, and your settings are kept.

## Uninstalling

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall   # stops the server, removes the autostart shortcut
```

Then delete the program folder. The catalog itself (database, comments, previews) stays in the
catalog folder.

## Several machines

Only one server at a time maintains a catalog folder. It holds a lock file, `server.lock` in the
catalog folder, and refreshes it every 2 minutes.

- **A second server** (another machine, or another user on the same machine) shows the catalog
  read-only. The header of its page says which server owns the folder.
- **Taking over:** the second server takes over automatically once the first one has been stopped
  for 10 minutes, for example because that machine was switched off.
- **Moving the server:** stop the server on the old machine (`install.ps1 -Stop` or
  `-Uninstall`), then install it on the new one. The new machine pulls the whole catalog from the
  catalog folder on its first start.

## Troubleshooting

| Problem | What to do |
|---|---|
| The page says "Labdata folder not reachable" or "catalog folder not usable" | Connect the network drive or VPN. The server shows the last local copy and retries every minute. |
| The page says "read-only: maintained by the server of …" | Another server owns the catalog folder (see *Several machines*). |
| The setup page shows Java or Bio-Formats as missing | Run `install.ps1` again. |
| The download fails (proxy, firewall) | Run `install.ps1` again (finished downloads are reused). If it keeps failing, download the JDK yourself and pass `-JavaHome`. |
| Another program already uses port 8766 | The installer then picks the next free port (it tells you which one, e.g. `http://localhost:8767`) and saves it in `settings.toml`. To choose one yourself: `install.ps1 -Port 8780`. |
| The server does not start | See `.work\logs\server.log` and `.work\logs\server_crash.log`. To watch it live in a console: stop it, then run `.venv\Scripts\python.exe serve_catalog.py --console` in the program folder (Ctrl+C stops it). |
| You moved or renamed the program folder | Run `install.ps1` again (the Python environment contains absolute paths). |
| Optional: better zarr inspection with microio | `install.ps1 -WithMicroio <path or git URL of microio>` |
