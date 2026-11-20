"""Notification-area (tray) icon of the catalog server.

Left click (default item): open the catalog. Right click: menu with Open catalog, Update now,
Settings, Stop. The icon colour and the tooltip show the server status.
"""

from __future__ import annotations

import threading
import time
import webbrowser

from PIL import Image, ImageDraw

COLORS = {"ok": (31, 138, 76), "starting": (120, 128, 140), "setup": (47, 111, 219),
          "offline": (183, 121, 31), "readonly": (183, 121, 31)}
LABEL = {"ok": "running", "starting": "starting", "setup": "not set up yet - open Settings",
         "offline": "offline (Labdata / catalog folder not reachable)", "readonly": "read-only (an older catalog server maintains the catalog folder)"}


def make_icon(status: str, busy: bool = False) -> Image.Image:
    """64x64 icon: rounded square in the status colour with a white lens (objective) symbol."""
    s = 64
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, s - 3, s - 3), radius=14, fill=COLORS.get(status, COLORS["starting"]) + (255,))
    d.ellipse((14, 14, 50, 50), outline=(255, 255, 255, 255), width=6)     # lens
    d.ellipse((26, 26, 38, 38), fill=(255, 255, 255, 255))                   # centre
    if busy:  # small white dot top right while a build / previews run
        d.ellipse((44, 6, 58, 20), fill=(255, 255, 255, 255))
    return img


class Tray:
    def __init__(self, app, url: str, on_stop):
        import pystray  # optional at import time of the server (console runs work without it)

        self.app, self.url, self.on_stop = app, url, on_stop
        self._key = None
        M = pystray.MenuItem
        self.icon = pystray.Icon(
            "microscopy-catalog", make_icon(app.status), "Microscopy catalog",
            menu=pystray.Menu(
                M("Open catalog", self.open, default=True),
                M("Update now", self.update, enabled=lambda _: self.app.status == "ok"),
                M("Settings", self.settings),
                pystray.Menu.SEPARATOR,
                M("Stop catalog server", self.stop),
            ))

    # --- menu actions ---------------------------------------------------------------------------
    def open(self, *_):
        webbrowser.open(self.url)

    def settings(self, *_):
        webbrowser.open(self.url.rstrip("/") + "/setup")

    def update(self, *_):
        if self.app.builder:
            self.app.builder.request("update requested from the tray icon")
            if self.app.previewer:
                threading.Thread(target=self.app.previewer.run_once, daemon=True).start()
            self.app.log("tray: update requested")

    def stop(self, *_):
        self.app.log("tray: stop requested")
        self.on_stop()

    # --- status -----------------------------------------------------------------------------------
    def _refresh_loop(self):
        while self.icon is not None:
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - icon already gone
                pass
            time.sleep(3)

    def refresh(self):
        b, p = self.app.builder, self.app.previewer
        busy = bool(b and (b.running or b.pending)) or bool(p and p.running)
        key = (self.app.status, busy)
        if key == self._key:
            return
        self._key = key
        tip = f"Microscopy catalog - {LABEL.get(self.app.status, self.app.status)}"
        if b and b.running:
            tip += " - updating"
        elif p and p.running:
            tip += " - making previews"
        self.icon.icon = make_icon(self.app.status, busy)
        self.icon.title = tip[:120]
        self.icon.update_menu()

    def run(self):
        """Blocks (main thread) until close() is called."""
        threading.Thread(target=self._refresh_loop, daemon=True, name="tray").start()
        self.icon.run()

    def close(self):
        icon, self.icon = self.icon, None
        if icon is not None:
            icon.stop()
