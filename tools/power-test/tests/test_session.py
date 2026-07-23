# ============================================================
#  Testy zapisu/odczytu sesji i składania tierów
# ============================================================

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

import common  # noqa: F401

from autorun import downsample as ds
from autorun import session as sess


class DownsampleTest(unittest.TestCase):
    def test_base_tier_min_avg_max(self):
        tier = ds.BaseTier(samples_per_window=4)
        recs = tier.push(np.array([1, 2, 3, 4, 10, 20, 30, 40],
                                  np.float32))
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs["min"][0], 1)
        self.assertEqual(recs["max"][0], 4)
        self.assertEqual(recs["avg"][0], 2.5)
        self.assertEqual(recs["max"][1], 40)
        self.assertEqual(int(recs["n"][0]), 4)

    def test_base_tier_buffers_partial(self):
        tier = ds.BaseTier(4)
        self.assertEqual(len(tier.push(np.array([1, 2], np.float32))), 0)
        recs = tier.push(np.array([3, 4, 5], np.float32))
        self.assertEqual(len(recs), 1)          # 1,2,3,4 domknięte
        self.assertEqual(recs["avg"][0], 2.5)
        flushed = tier.flush()                  # zostało samo 5
        self.assertEqual(len(flushed), 1)
        self.assertEqual(flushed["avg"][0], 5)
        self.assertEqual(int(flushed["n"][0]), 1)

    def test_fold_weighted_average(self):
        base = np.zeros(10, ds.RECORD_DTYPE)
        base["avg"] = np.arange(10)
        base["min"] = np.arange(10)
        base["max"] = np.arange(10) + 100
        base["n"] = 5
        fold = ds.FoldTier(fanout=10)
        out = fold.push(base)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out["avg"][0], np.arange(10).mean(),
                               places=4)
        self.assertEqual(out["min"][0], 0)
        self.assertEqual(out["max"][0], 109)
        self.assertEqual(int(out["n"][0]), 50)

    def test_cascade_conserves_mean(self):
        # Stały sygnał: każdy tier ma tę samą średnią.
        casc = ds.Cascade(sample_rate=1000, base_window_ms=1)
        for tier_win, recs in casc.push(np.full(1000, 7.0, np.float32)):
            if len(recs):
                self.assertTrue(np.allclose(recs["avg"], 7.0))


class SessionWriterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "sess"
        self.dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _writer(self, mode="downsampled", rate=1000, window=1):
        return sess.SessionWriter(self.dir, meta={"scenario": "x"},
                                  sample_rate=rate, storage_mode=mode,
                                  window_ms=window)

    def test_roundtrip_summary(self):
        w = self._writer()
        w.write_samples(np.full(1000, 5.0, np.float32))
        w.write_samples(np.full(1000, 15.0, np.float32))
        summary = w.finalize("done")
        self.assertEqual(summary["samples"], 2000)
        self.assertAlmostEqual(summary["avg_uA"], 10.0, places=3)
        self.assertEqual(summary["min_uA"], 5.0)
        self.assertEqual(summary["max_uA"], 15.0)
        self.assertAlmostEqual(summary["duration_s"], 2.0, places=3)
        # ładunek = avg * czas = 10 µA * 2 s = 20 µC
        self.assertAlmostEqual(summary["charge_uC"], 20.0, places=2)

    def test_reader_tiers(self):
        w = self._writer(rate=1000, window=1)
        w.write_samples(np.arange(5000, dtype=np.float32) % 100)
        w.finalize("done")
        r = sess.SessionReader(self.dir)
        # 5000 próbek / (1000/s * 1ms) = 5000 rekordów w tier_1ms
        self.assertEqual(r.tier_len(1), 5000)
        recs = r.read_tier(1, 0, 10)
        self.assertEqual(len(recs), 10)
        # tier_for_span: dla 5 s przy oknie 1 ms trzeba grubszego tieru
        win = r.tier_for_span(5.0, max_records=4000)
        self.assertGreater(win, 1)

    def test_raw_mode(self):
        w = self._writer(mode="both", rate=1000)
        data = np.linspace(0, 99, 2000, dtype=np.float32)
        w.write_samples(data)
        w.finalize("done")
        r = sess.SessionReader(self.dir)
        self.assertTrue(r.has_raw())
        chunk, i0 = r.read_raw(0.0, 2.0)
        self.assertEqual(i0, 0)
        self.assertTrue(np.allclose(chunk, data))

    def test_annotations_merge(self):
        w = self._writer()
        w.write_samples(np.ones(500, np.float32))
        w.annotate(0.1, "Friend Poll", pattern="Poll", rtt_line="x Poll")
        w.finalize("done")
        sess.append_manual_annotation(self.dir, 0.3, "ręczna")
        r = sess.SessionReader(self.dir)
        anns = r.annotations()
        self.assertEqual(len(anns), 2)
        self.assertEqual(anns[0]["source"], "rtt")
        self.assertEqual(anns[1]["source"], "manual")
        self.assertLess(anns[0]["t_s"], anns[1]["t_s"])

    def test_status_atomic_json(self):
        w = self._writer()
        w.write_samples(np.ones(300, np.float32))
        w.update_status("measuring", avg_1s_uA=1.0)
        status = json.loads((self.dir / "status.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "measuring")
        self.assertEqual(status["samples"], 300)
        w.finalize("done")            # domknij pliki tierów

    def test_gap_recorded(self):
        w = self._writer()
        w.write_samples(np.ones(1000, np.float32))
        w.record_gap(500)
        summary = w.finalize("done")
        self.assertEqual(summary["lost_samples"], 500)
        self.assertEqual(len(w.meta["gaps"]), 1)


class ScanTest(unittest.TestCase):
    def test_scan_finds_nested(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        run = root / "20260101_000000_plan"
        s1 = run / "20260101_000001_x"
        s1.mkdir(parents=True)
        (s1 / "meta.json").write_text(
            json.dumps({"start": "2026-01-01T00:00:01", "scenario": "x"}))
        top = root / "20260102_000000_y"
        top.mkdir()
        (top / "meta.json").write_text(
            json.dumps({"start": "2026-01-02T00:00:00", "scenario": "y"}))
        found = sess.scan_sessions(root)
        self.assertEqual(len(found), 2)
        # najnowsza pierwsza
        self.assertEqual(found[0][1]["scenario"], "y")
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
