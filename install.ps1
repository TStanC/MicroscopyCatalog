# Installs / updates the microscopy catalog in THIS folder (the git clone). No admin rights needed.
#
# Everything is put inside this folder (nothing in AppData, no PATH changes):
#   .tools\uv.exe       uv (Python package manager) - unless uv is already on the PATH
#   .python\            Python 3.12, installed by uv
#   .venv\              numpy, Pillow (exact versions from uv.lock)
#   .runtime\jdk\       Java (Temurin JDK 21) - only if no JDK >= 11 is found on this machine
#   .runtime\bioformats\bioformats_package.jar
#   .runtime\runtime.json   which Java / Bio-Formats the catalog uses
#   .cache\             downloads
# Settings (settings.toml) and the local working copy (.work\) are created by the server.
# The catalog data itself lives in the catalog folder chosen on the setup page.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1                 install / update, start, open the page
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Autostart      ... and start it at every login
#   powershell -ExecutionPolicy Bypass -File install.ps1 -JavaHome "C:\path\to\jdk"   use this JDK
#   powershell -ExecutionPolicy Bypass -File install.ps1 -DownloadJdk    ignore installed JDKs, download one
#   powershell -ExecutionPolicy Bypass -File install.ps1 -WithMicroio <path or git URL>   optional zarr reader
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Port 8780    use another port than 8766
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Stop           stop the server
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall      stop + remove the shortcuts

param(
    [switch]$Autostart,
    [switch]$NoStart,
    [switch]$Stop,
    [switch]$Uninstall,
    [string]$JavaHome = "",
    [switch]$DownloadJdk,
    [string]$WithMicroio = "",
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# --- pinned versions ---------------------------------------------------------------------------
$UvVersion = "0.13.6"
$UvSha256 = "a2a8820d5eea32f08ef467ef3b7f66883a9b076d5737a2fc2fbb5f458d1bb50f"
$BfVersion = "8.5.0"
$BfSha256 = "c6e60665d53a334b66e4d635340151f403dfe57a64704c573dd4c03b873befb9"
$JdkMajor = 21          # downloaded if no suitable JDK is found
$MinJava = 11           # Bio-Formats 8 needs Java 11+

$root = $PSScriptRoot
$tools = Join-Path $root ".tools"
$rt = Join-Path $root ".runtime"
$dl = Join-Path $root ".cache\downloads"
$lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "Microscopy Catalog Server.lnk"
$startMenuLnk = Join-Path ([Environment]::GetFolderPath("Programs")) "Microscopy Catalog.lnk"
$folderLnk = Join-Path $root "Microscopy Catalog.lnk"
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
$venvPyw = Join-Path $root ".venv\Scripts\pythonw.exe"
$serve = Join-Path $root "serve_catalog.py"

# uv: keep Python, cache and venv inside this folder
$env:UV_PYTHON_INSTALL_DIR = Join-Path $root ".python"
$env:UV_CACHE_DIR = Join-Path $root ".cache\uv"
$env:UV_PYTHON_PREFERENCE = "only-managed"
$env:UV_PROJECT_ENVIRONMENT = Join-Path $root ".venv"
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue   # an activated conda/venv must not be used

function Say($msg) { Write-Host $msg }
function Step($msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Cyan }

function Get-ServerProcesses {
    $rx = [regex]::Escape($root)
    Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe' or Name='java.exe'" |
        Where-Object { $_.CommandLine -and (
            $_.CommandLine -match "$rx.*(serve_catalog|build_catalog|make_previews)\.py" -or
            ($_.Name -eq 'java.exe' -and $_.CommandLine -match [regex]::Escape((Join-Path $root ".work\cache\java_build")))) }
}

function Stop-Server {
    $procs = @(Get-ServerProcesses)
    if (-not $procs) { return }
    Say "Stopping the running catalog server (and its jobs) ..."
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 10 -and (Get-ServerProcesses); $i++) { Start-Sleep -Seconds 1 }
}

function Download($url, $out, $sha256) {
    New-Item -ItemType Directory -Force (Split-Path $out) | Out-Null
    if ((Test-Path $out) -and $sha256 -and ((Get-FileHash $out -Algorithm SHA256).Hash -eq $sha256.ToUpper())) { return }
    Say "  downloading $url"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile "$out.part"
    if ($sha256) {
        $h = (Get-FileHash "$out.part" -Algorithm SHA256).Hash
        if ($h -ne $sha256.ToUpper()) { Remove-Item "$out.part"; throw "Checksum mismatch for $url (got $h)" }
    }
    Move-Item -Force "$out.part" $out
}

function Expand-Zip($zip, $dest) {
    New-Item -ItemType Directory -Force $dest | Out-Null
    $tar = Join-Path $env:SystemRoot "System32\tar.exe"
    if (Test-Path $tar) { & $tar -xf $zip -C $dest; if ($LASTEXITCODE -ne 0) { throw "tar failed on $zip" } }
    else { Expand-Archive -Path $zip -DestinationPath $dest -Force }
}

function Get-JavaMajor($javaHome) {
    # -> major version of a JDK folder (needs bin\java.exe AND bin\javac.exe), else 0
    $java = Join-Path $javaHome "bin\java.exe"
    if (-not (Test-Path $java) -or -not (Test-Path (Join-Path $javaHome "bin\javac.exe"))) { return 0 }
    $out = (cmd /c "`"$java`" -version 2>&1") -join " "
    if ($out -match 'version "1\.(\d+)') { return [int]$Matches[1] }
    if ($out -match 'version "(\d+)') { return [int]$Matches[1] }
    return 0
}

# ===============================================================================================
if ($Stop -or $Uninstall) {
    Stop-Server
    if ($Uninstall) {
        foreach ($l in @($lnk, $startMenuLnk, $folderLnk)) { if (Test-Path $l) { Remove-Item $l -Confirm:$false; Say "Removed shortcut $l" } }
        Say "Stopped. To remove the program completely, delete the folder $root"
        Say "(the catalog data stays in the catalog folder, e.g. in Labdata)."
    } else { Say "Stopped." }
    return
}

Step "Microscopy catalog: installing into $root"
Stop-Server

# --- uv ----------------------------------------------------------------------------------------
Step "uv"
$uv = Join-Path $tools "uv.exe"
if (-not (Test-Path $uv)) {
    $onPath = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($onPath) {
        $uv = $onPath.Source
        Say "  using uv from the PATH: $uv"
    } else {
        $zip = Join-Path $dl "uv-$UvVersion.zip"
        Download "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" $zip $UvSha256
        Expand-Zip $zip $tools
        $uv = Join-Path $tools "uv.exe"
    }
}
Say "  $(& $uv --version)"

# --- Python + packages -------------------------------------------------------------------------
Step "Python environment (.venv)"
Push-Location $root
try {
    & $uv sync --locked --inexact
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
    if ($WithMicroio) {
        Say "  installing microio from $WithMicroio"
        & $uv pip install --python $venvPy $WithMicroio
        if ($LASTEXITCODE -ne 0) { throw "installing microio failed" }
    }
} finally { Pop-Location }

# --- Java ----------------------------------------------------------------------------------------
Step "Java (JDK >= $MinJava)"
$runtimeJson = Join-Path $rt "runtime.json"
$jdk = $null; $jdkSource = ""
$candidates = New-Object System.Collections.Generic.List[object]
if ($JavaHome) { $candidates.Add(@($JavaHome, "-JavaHome")) }
if (-not $DownloadJdk) {
    if (Test-Path $runtimeJson) {
        $prev = Get-Content $runtimeJson -Raw | ConvertFrom-Json
        if ($prev.java_bin) { $candidates.Add(@((Split-Path $prev.java_bin), "previous install")) }
    }
    if ($env:JAVA_HOME) { $candidates.Add(@($env:JAVA_HOME, "JAVA_HOME")) }
    $jc = Get-Command javac.exe -ErrorAction SilentlyContinue
    if ($jc) { $candidates.Add(@((Split-Path (Split-Path $jc.Source)), "PATH")) }
    # system-wide JDK installations, then a JDK downloaded by an earlier run of this script, then JDKs
    # inside conda environments (last: they disappear when the environment is removed)
    $globs = @("$env:ProgramFiles\Eclipse Adoptium\jdk-*", "$env:ProgramFiles\Java\jdk*", "$env:ProgramFiles\Microsoft\jdk-*",
               "$env:ProgramFiles\Zulu\zulu*", "$env:ProgramFiles\Amazon Corretto\jdk*", "$env:ProgramFiles\BellSoft\*",
               "$env:LOCALAPPDATA\Programs\Eclipse Adoptium\jdk-*", (Join-Path $rt "jdk"),
               "$env:LOCALAPPDATA\miniconda3\envs\*\Library", "$env:USERPROFILE\miniconda3\envs\*\Library",
               "$env:USERPROFILE\anaconda3\envs\*\Library", "$env:LOCALAPPDATA\miniconda3\Library",
               "$env:USERPROFILE\miniconda3\Library", "$env:USERPROFILE\anaconda3\Library")
    foreach ($g in $globs) {
        Get-Item $g -ErrorAction SilentlyContinue | Sort-Object Name -Descending | ForEach-Object { $candidates.Add(@($_.FullName, "found")) }
    }
} else {
    $candidates.Add(@((Join-Path $rt "jdk"), "downloaded earlier"))
}
foreach ($c in $candidates) {
    $v = Get-JavaMajor $c[0]
    if ($v -ge $MinJava) { $jdk = $c[0]; $jdkSource = "$($c[1]), Java $v"; break }
    elseif ($c[1] -eq "-JavaHome") { throw "-JavaHome $($c[0]) is not a JDK >= $MinJava (needs bin\java.exe and bin\javac.exe)" }
}
if ($jdk -and $DownloadJdk -and $jdkSource -notlike "downloaded*") { $jdk = $null }
if (-not $jdk) {
    if ($DownloadJdk) { Say "  -DownloadJdk: downloading Temurin JDK $JdkMajor (~200 MB)" }
    else { Say "  no JDK >= $MinJava found on this machine - downloading Temurin JDK $JdkMajor (~200 MB)" }
    $api = "https://api.adoptium.net/v3/assets/latest/$JdkMajor/hotspot?architecture=x64&image_type=jdk&os=windows&vendor=eclipse"
    $rel = @(Invoke-RestMethod -UseBasicParsing $api)[0]
    $pkg = $rel.binary.package
    $zip = Join-Path $dl $pkg.name
    Download $pkg.link $zip $pkg.checksum
    $tmp = Join-Path $rt "jdk_unpack"
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Zip $zip $tmp
    $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
    $target = Join-Path $rt "jdk"
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    Move-Item $inner.FullName $target
    Remove-Item $tmp -Recurse -Force
    Remove-Item $zip -Force
    $jdk = $target; $jdkSource = "downloaded $($rel.release_name)"
}
Say "  using $jdk ($jdkSource)"
$own = Join-Path $rt "jdk"
if ((Test-Path $own) -and ((Resolve-Path $jdk).Path -ne (Resolve-Path $own).Path)) {
    Remove-Item $own -Recurse -Force   # not needed: another JDK is used - don't keep two
    Say "  removed the no longer needed downloaded JDK in $own"
}

# --- Bio-Formats -------------------------------------------------------------------------------
Step "Bio-Formats $BfVersion"
$bfDir = Join-Path $rt "bioformats"
$jar = Join-Path $bfDir "bioformats_package.jar"
Download "https://downloads.openmicroscopy.org/bio-formats/$BfVersion/artifacts/bioformats_package.jar" $jar $BfSha256
Say "  $jar"

$info = [ordered]@{ java_bin = (Join-Path $jdk "bin"); java_source = $jdkSource; bioformats_lib = $bfDir;
                    bioformats_version = $BfVersion; installed = (Get-Date -Format s) }
New-Item -ItemType Directory -Force $rt | Out-Null
[IO.File]::WriteAllText($runtimeJson, ($info | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))

# --- check: compile the metadata reader once ---------------------------------------------------
Step "Check"
$chk = Join-Path $root ".cache\javacheck"
New-Item -ItemType Directory -Force $chk | Out-Null
& (Join-Path $jdk "bin\javac.exe") -nowarn -cp "$bfDir\*" -d $chk (Join-Path $root "java\OmeDump.java")
if ($LASTEXITCODE -ne 0) { throw "Compiling java\OmeDump.java failed - Java/Bio-Formats setup is broken" }
& $venvPy -c "import numpy, PIL, sqlite3, tomllib; print('  Python', __import__('sys').version.split()[0], '| numpy', numpy.__version__, '| Pillow', PIL.__version__)"
if ($LASTEXITCODE -ne 0) { throw "Python environment check failed" }
Say "  Java + Bio-Formats + Python ok"

# --- port ----------------------------------------------------------------------------------------
Step "Port"
$settingsFile = Join-Path $root "settings.toml"
function Get-SettingsPort {
    foreach ($f in @($settingsFile, (Join-Path $root "config.toml"))) {
        if (Test-Path $f) {
            $m = Select-String -Path $f -Pattern '^\s*server_port\s*=\s*(\d+)' | Select-Object -First 1
            if ($m) { return [int]$m.Matches[0].Groups[1].Value }
        }
    }
    return 8766
}
function Set-SettingsPort($p) {
    $lines = @()
    if (Test-Path $settingsFile) { $lines = @(Get-Content $settingsFile | Where-Object { $_ -notmatch '^\s*server_port\s*=' }) }
    $lines += "server_port = $p"
    [IO.File]::WriteAllText($settingsFile, (($lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
}
function Get-PortOwner($p) {
    # our own server was stopped above, so anything listening here is another program
    $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $c) { return $null }
    $pr = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)" -ErrorAction SilentlyContinue
    if ($pr) { return "$($pr.Name) (PID $($pr.ProcessId))" } else { return "process $($c.OwningProcess)" }
}
if ($Port) { Set-SettingsPort $Port }
$usePort = Get-SettingsPort
$owner = Get-PortOwner $usePort
if ($owner) {
    Say "  port $usePort is already used by another program: $owner"
    if ($Port) { throw "Port $Port is in use - choose another one with -Port <number>" }
    for ($pp = $usePort + 1; $pp -le $usePort + 50; $pp++) {
        if (-not (Get-PortOwner $pp)) { $usePort = $pp; break }
    }
    Set-SettingsPort $usePort
    Say "  using port $usePort instead (saved in settings.toml)"
}
$url = "http://localhost:$usePort"
Say "  $url"

# --- windowless launcher ------------------------------------------------------------------------
function Test-GuiExe($path) {
    # PE header subsystem 2 = Windows GUI (no console window), 3 = console
    if (-not (Test-Path $path)) { return $false }
    $b = [IO.File]::ReadAllBytes($path)
    $pe = [BitConverter]::ToInt32($b, 0x3C)
    return ([BitConverter]::ToUInt16($b, $pe + 0x5C) -eq 2)
}
$launcher = $venvPyw
if (-not (Test-GuiExe $venvPyw)) {
    $home_ = ((Get-Content (Join-Path $root ".venv\pyvenv.cfg")) | Where-Object { $_ -match '^\s*home\s*=' }) -replace '^\s*home\s*=\s*', ''
    $basePyw = Join-Path $home_.Trim() "pythonw.exe"
    if (Test-GuiExe $basePyw) {
        $launcher = $basePyw
        Say "  note: .venv\Scripts\pythonw.exe would open a console window on this machine - using $basePyw"
    } else {
        Say "  WARNING: no windowless pythonw.exe found - the server may show a console window"
    }
}

# --- shortcuts (no console window) + start ---------------------------------------------------------
Step "Shortcuts"
$ico = Join-Path $rt "catalog.ico"
& $venvPy -c "import sys; sys.path.insert(0, r'$root'); from mdcat.tray import make_icon; make_icon('ok').save(r'$ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64)])"
function New-Shortcut($path, $arguments, $description) {
    $sc = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
    $sc.TargetPath = $launcher
    $sc.Arguments = $arguments
    $sc.WorkingDirectory = $root
    $sc.Description = $description
    if (Test-Path $ico) { $sc.IconLocation = "$ico,0" }
    $sc.Save()
}
New-Shortcut $startMenuLnk "`"$serve`" --open" "Microscopy catalog: start the server (tray icon) and open the page"
New-Shortcut $folderLnk "`"$serve`" --open" "Microscopy catalog: start the server (tray icon) and open the page"
Say "  Start menu: Microscopy Catalog   (also: $folderLnk)"
if ($Autostart) {
    New-Shortcut $lnk "`"$serve`"" "Microscopy catalog server ($url)"
    Say "  Autostart at login: $lnk"
}
$oldCmd = Join-Path $root "Start Catalog.cmd"
if (Test-Path $oldCmd) { Remove-Item $oldCmd -Force }   # replaced by the shortcuts (no console window)

if (-not $NoStart) {
    Step "Starting the server"
    Start-Process -FilePath $launcher -ArgumentList "`"$serve`"", "--open", "--wait-port" -WorkingDirectory $root
    $ok = $false
    for ($i = 0; $i -lt 20 -and -not $ok; $i++) {
        Start-Sleep -Seconds 1
        try { $s = Invoke-RestMethod -UseBasicParsing "http://127.0.0.1:$usePort/api/state" -TimeoutSec 5; if ($s.status) { $ok = $true } } catch { }  # localhost may try IPv6 first (slow)
    }
    if ($ok) { Say "Server running: $url  (status: $($s.status))" }
    else {
        Say "The server did not answer yet - see $root\.work\logs\server.log"
        $crash = Join-Path $root ".work\logs\server_crash.log"
        if (Test-Path $crash) { Say "  and $crash :"; Get-Content $crash -Tail 15 | ForEach-Object { Say "    $_" } }
        Say "  To see errors directly: `"$venvPy`" `"$serve`" --console --no-tray"
    }
}
Say ""
Say "Done."
