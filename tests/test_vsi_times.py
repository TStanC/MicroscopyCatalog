"""Start/end times of Olympus .vsi acquisitions (mdcat/derive.py: series_times, build_images).

cellSens stores only "Creation time (UTC)" = the moment the document is saved = the END of the
acquisition; Bio-Formats passes it on as AcquisitionDate. The values below are the real ones of
datasets where this was checked against the acquisition order and the lab book.

    .venv\\Scripts\\python.exe -m unittest tests.test_vsi_times -v
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from mdcat.derive import build_images, series_times  # noqa: E402


def _series(date: str, span: float, T: int = 100, Z: int = 25, name: str = "") -> dict:
    dt = round(span / (T - 1), 4) if T > 1 else None
    return {"idx": 0, "name": name or "image", "date": date, "T": T, "C": 1, "Z": Z, "Y": 2769, "X": 2735,
            "dtype": "uint16", "px_x": 0.26, "px_y": 0.26, "px_z": 2.0, "time_inc": None, "objective": {},
            "channels": [{"name": "C555"}], "n_planes": T * Z, "dt_med": dt,
            "dt_min": dt, "dt_max": dt, "span_s": span, "stage_z": None, "exposure_s": {}}


def _utc(*a) -> float:
    return datetime(*a, tzinfo=timezone.utc).timestamp()


def _iso(ts):
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


class SeriesTimes(unittest.TestCase):
    def test_vsi_creation_time_is_the_end(self):
        # 20261107_500um_ibidiPatterns_24hCHIR_25x_010: 121 x 10 min, saved 2026-11-07 12:41:28
        s = _series("2026-11-07T12:41:28", 72018.42)
        start, end = series_times(s, "vsi", src_mtime=_utc(2026, 11, 7, 12, 41, 56))
        self.assertEqual(_iso(end), "2026-11-07T12:41:28")
        self.assertEqual(_iso(start), "2026-11-06T16:41:09")

    def test_vsi_without_mtime_is_shifted_too(self):
        start, end = series_times(_series("2026-11-07T12:41:28", 72018.42), "vsi")
        self.assertEqual(_iso(start), "2026-11-06T16:41:09")

    def test_vsi_streamed_file_keeps_date_as_start(self):
        # 20260607_LMNB1_chir_60x_d0_001: created 2026-06-05 19:00:42, last written 2026-06-07 11:04:33 (UTC)
        s = _series("2026-06-05T19:00:42", 144009.35, T=81)
        start, end = series_times(s, "vsi", src_mtime=_utc(2026, 6, 7, 11, 4, 33))
        self.assertEqual(_iso(start), "2026-06-05T19:00:42")
        self.assertEqual(_iso(end), "2026-06-07T11:00:51")

    def test_vsi_copied_file_with_unrelated_mtime_is_shifted(self):
        # LMNB1_CHIR_d1_001: saved 03:35:38, the folder was copied later (mtime 08:56:30) -> still the end
        s = _series("2026-02-24T03:35:38", 30027.0, T=300)
        start, end = series_times(s, "vsi", src_mtime=_utc(2026, 2, 24, 8, 56, 30))
        self.assertEqual(_iso(end), "2026-02-24T03:35:38")
        self.assertEqual(_iso(start), "2026-02-23T19:15:11")

    def test_short_vsi_stack_mtime_ambiguous_defaults_to_end(self):
        # 51 s z-stack: mtime 1 s after the creation time matches both readings -> end
        s = _series("2026-11-06T16:12:16", 51.0, T=1, Z=59)
        start, end = series_times(s, "vsi", src_mtime=_utc(2026, 11, 6, 16, 12, 17))
        self.assertEqual(_iso(end), "2026-11-06T16:12:16")
        self.assertEqual(end - start, 51.0)

    def test_other_formats_unchanged(self):
        s = _series("2025-11-11T13:44:47", 91046.0)
        for fmt in ("lif", "czi", "zarr"):
            start, end = series_times(s, fmt, src_mtime=_utc(2025, 11, 12, 15, 6, 58))
            self.assertEqual(_iso(start), "2025-11-11T13:44:47", fmt)
            self.assertEqual(end - start, 91046.0, fmt)

    def test_missing_date_or_span(self):
        self.assertEqual(series_times({"date": None, "span_s": 10.0}, "vsi"), (None, None))
        start, end = series_times({"date": "2026-01-01T10:00:00", "span_s": None}, "vsi")
        self.assertEqual(start, end)


class BuildImages(unittest.TestCase):
    def test_image_and_scene_times_vsi(self):
        summary = {"series": [_series("2026-11-07T12:41:28", 72018.42, T=121)]}
        im = [i for i in build_images(summary, "vsi", _utc(2026, 11, 7, 12, 41, 56)) if i["kind"] == "main"][0]
        self.assertEqual(im["acquired_start"], "2026-11-06T16:41:09")
        self.assertEqual(im["acquired_end"], "2026-11-07T12:41:28")
        self.assertAlmostEqual(im["total_time_s"], 72018.0, delta=1)
        self.assertEqual(im["_scenes"][0]["acquired_start"], "2026-11-06T16:41:09")
        # the fingerprint (acq_id, raw <-> zarr matching) still uses the stored creation time
        self.assertTrue(im["_fps"][0].startswith("2026-11-07T12:41:28|"))

    def test_image_times_lif(self):
        summary = {"series": [_series("2025-11-11T13:44:47", 3600.0)]}
        im = [i for i in build_images(summary, "lif") if i["kind"] == "main"][0]
        self.assertEqual(im["acquired_start"], "2025-11-11T13:44:47")
        self.assertEqual(im["acquired_end"], "2025-11-11T14:44:47")
        self.assertEqual(im["_scenes"][0]["acquired_start"], "2025-11-11T13:44:47")


if __name__ == "__main__":
    unittest.main()
