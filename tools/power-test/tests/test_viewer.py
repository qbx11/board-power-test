# ============================================================
#  Testy viewera: statystyki (bez Qt) + smoke test okna (z Qt)
# ============================================================
# Część statystyczna jest czysta i biegnie zawsze. Smoke test tworzy
# okno w trybie offscreen – pomijany, gdy PyQt6/pyqtgraph nie ma
# (viewer to zależność opcjonalna, doinstalowywana leniwie).

import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

import common  # noqa: F401

from autorun import session as sess
from viewer import stats as statmod


class StatsTest(unittest.TestCase):
    def test_region_stats_weighted(self):
        recs = np.zeros(3, sess.RECORD_DTYPE)
        recs["min"] = [1, 2, 3]
        recs["max"] = [10, 20, 30]
        recs["avg"] = [5, 10, 15]
        recs["n"] = [100, 100, 100]
        st = statmod.region_stats(recs)
        self.assertAlmostEqual(st["avg_uA"], 10.0)
        self.assertEqual(st["min_uA"], 1)
        self.assertEqual(st["max_uA"], 30)
        self.assertEqual(st["samples"], 300)

    def test_region_stats_empty(self):
        self.assertIsNone(statmod.region_stats(
            np.empty(0, sess.RECORD_DTYPE)))

    def test_charge_and_battery(self):
        ce = statmod.charge_and_energy(1000.0, 3600, voltage_V=3.0)
        # 1 mA * 1 h = 1 mAh; ładunek = 1000 µA * 3600 s = 3.6e6 µC
        self.assertAlmostEqual(ce["charge_uC"], 3.6e6)
        self.assertAlmostEqual(ce["charge_mAh"], 1.0, places=6)
        self.assertAlmostEqual(ce["energy_mWh"], 3.0, places=6)
        # 220 mAh przy 1 mA -> 220 h
        self.assertAlmostEqual(statmod.battery_life(1000.0, 220), 220.0)

    def test_formatters(self):
        self.assertIn("µA", statmod.format_current(5))
        self.assertIn("mA", statmod.format_current(5000))
        self.assertIn("h", statmod.format_duration(7200))


def _make_session():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    d = sess.new_session_dir(root, "x")
    w = sess.SessionWriter(d, meta={"scenario": "x", "label": "X",
                                    "voltage_V": "3.0"},
                           sample_rate=1000, storage_mode="both",
                           window_ms=1)
    for sec in range(3):
        chunk = np.full(1000, 5.0, np.float32)
        chunk[:50] = 200.0
        w.write_samples(chunk)
        w.annotate(sec + 0.01, f"pik {sec}", pattern="pik")
    w.finalize("done")
    return tmp, d


@unittest.skipUnless(
    __import__("importlib").util.find_spec("PyQt6") is not None
    and __import__("importlib").util.find_spec("pyqtgraph") is not None,
    "viewer wymaga PyQt6 + pyqtgraph (zależność opcjonalna)")
class ViewerSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6 import QtWidgets
        cls.app = (QtWidgets.QApplication.instance()
                   or QtWidgets.QApplication([]))

    def setUp(self):
        self.tmp, self.dir = _make_session()

    def tearDown(self):
        self.tmp.cleanup()

    def test_session_view_loads(self):
        from viewer.plot import SessionView
        view = SessionView(str(self.dir))
        view._refresh_detail()
        view._refresh_overview()
        view._refresh_annotations()
        self.assertEqual(len(view.annotations._lines), 3)
        view.stat_region.setRegion([0.5, 1.5])
        view._update_stats()
        self.assertIn("zaznaczenie", view.stats_label.text())

    def test_compare_view(self):
        from viewer.plot import CompareView
        view = CompareView([str(self.dir), str(self.dir)])
        self.assertEqual(len(view.readers), 2)

    def test_add_manual_annotation(self):
        from viewer.plot import SessionView
        view = SessionView(str(self.dir))
        sess.append_manual_annotation(self.dir, 2.0, "ręczna")
        view._refresh_annotations()
        self.assertEqual(len(view.annotations._lines), 4)

    def test_csv_export(self):
        from viewer import export
        out = Path(self.tmp.name) / "e.csv"
        n = export.export_csv(out, __import__("autorun.session",
                                             fromlist=["SessionReader"])
                              .SessionReader(self.dir), 1, 0.0, 2.0)
        self.assertGreater(n, 0)
        self.assertTrue(out.is_file())


if __name__ == "__main__":
    unittest.main()
