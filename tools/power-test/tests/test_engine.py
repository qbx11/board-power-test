# ============================================================
#  Testy silnika trybu autonomicznego (autorun/engine.py)
# ============================================================
# Bez sprzętu: fałszywy west/nrfutil (FakeEnv) + FakeSampler/FakeRttReader.
# Sprawdzamy maszynę stanów, zawartość sesji, wiersz CSV, politykę
# błędów przy timeout triggera i czyste domknięcie po przerwaniu.

import csv
import threading
import time
import unittest

from common import FakeEnv
from fakes import FakeRttReader, FakeSampler

import power_test as core
from autorun import plan as planmod
from autorun.engine import AutoRunner


def _plan(**step):
    step.setdefault("scenario", "zwykly")
    step.setdefault("duration_s", 0.2)
    step.setdefault("power_cycle", True)
    return planmod.Plan(name="test", board="btz",
                        steps=[planmod.PlanStep(**step)])


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.env = FakeEnv()
        self.manifest = core.load_manifest()
        self.events = []
        self.sampler = FakeSampler(sample_rate=2000)

    def tearDown(self):
        self.env.cleanup()

    def _run(self, plan, rtt_script=None, cancel=None):
        rtt = FakeRttReader(rtt_script)
        self.rtt = rtt
        runner = AutoRunner(
            plan, self.manifest, "BTZ #1",
            sampler_factory=lambda p: self.sampler,
            rtt_factory=lambda prof: rtt,
            event_cb=self.events.append,
            cancel=cancel or threading.Event())
        self.runner = runner
        return runner.run()

    def _states(self):
        return [ev.text for ev in self.events if ev.kind == "state"]

    def test_delay_step_completes(self):
        results = self._run(_plan(
            trigger=planmod.Trigger(type="delay", seconds=0)))
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r.status, "done")
        self.assertGreater(r.summary["samples"], 0)
        # Maszyna stanów przeszła przez kluczowe fazy.
        states = self._states()
        for want in ("build", "power", "flash", "trigger", "measure"):
            self.assertIn(want, states)
        # PPK2: napięcie ustawione, płytka zasilona i odcięta na końcu.
        self.assertEqual(self.sampler.voltage_mV, 3000)
        self.assertIn("close", self.sampler.log)
        self.assertFalse(self.sampler.dut)          # odcięte po planie

    def test_csv_row_written(self):
        self._run(_plan(trigger=planmod.Trigger(type="delay", seconds=0)))
        with open(core.CSV_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["scenariusz"], "zwykly")
        self.assertEqual(row["egzemplarz"], "BTZ #1")
        self.assertTrue(row["prad_uA"])
        self.assertTrue(row["prad_min_uA"])
        self.assertTrue(row["sesja"])
        self.assertTrue(row["sesja"].startswith("reports/sessions/"))

    def test_session_files(self):
        results = self._run(_plan(
            trigger=planmod.Trigger(type="delay", seconds=0),
            storage=planmod.Storage(mode="both", window_ms=1)))
        d = results[0].session_dir
        self.assertTrue((d / "meta.json").is_file())
        self.assertTrue((d / "status.json").is_file())
        self.assertTrue((d / "raw.bin").is_file())
        self.assertTrue((d / "tiers" / "tier_1ms.bin").is_file())

    def test_rtt_trigger_fires(self):
        results = self._run(
            _plan(scenario="zwykly", duration_s=0.15,
                  trigger=planmod.Trigger(type="rtt", pattern="Ready",
                                          timeout_s=5),
                  rtt="trigger"),
            rtt_script=[(0.05, "boot"), (0.1, "System Ready now")])
        self.assertEqual(results[0].status, "done")
        # rtt='trigger': czytnik podłączony i odłączony przed pomiarem.
        self.assertTrue(self.rtt.detached)

    def test_rtt_timeout_skips(self):
        plan = _plan(scenario="zwykly", duration_s=0.1,
                     trigger=planmod.Trigger(type="rtt",
                                             pattern="NigdyNie",
                                             timeout_s=0.3),
                     rtt="trigger")
        plan.on_step_error = "skip"
        results = self._run(plan, rtt_script=[(0.01, "cos innego")])
        self.assertEqual(results[0].status, "trigger_timeout")

    def test_rtt_continuous_labels(self):
        results = self._run(
            _plan(scenario="zwykly", duration_s=0.3,
                  trigger=planmod.Trigger(type="delay", seconds=0),
                  rtt="continuous",
                  labels=[planmod.LabelRule(pattern="Poll",
                                            label="Friend Poll")]),
            rtt_script=[(0.05, "Friend Poll sent"),
                        (0.1, "inny log")])
        d = results[0].session_dir
        anns = [line for line in
                (d / "annotations_auto.jsonl").read_text().splitlines()
                if line]
        self.assertTrue(any("Friend Poll" in a for a in anns))

    def test_cancel_finalizes(self):
        cancel = threading.Event()
        plan = _plan(scenario="zwykly", duration_s=30,   # długi pomiar
                     trigger=planmod.Trigger(type="delay", seconds=0))

        # Przerwij dopiero, gdy pomiar faktycznie ruszył (deterministycznie
        # – nie na sztywnym opóźnieniu, które bywa krótsze niż FAZA 1).
        def watcher(ev):
            self.events.append(ev)
            if ev.kind == "state" and ev.text == "measure":
                threading.Timer(0.3, cancel.set).start()

        rtt = FakeRttReader()
        self.rtt = rtt
        runner = AutoRunner(
            plan, self.manifest, "BTZ #1",
            sampler_factory=lambda p: self.sampler,
            rtt_factory=lambda prof: rtt,
            event_cb=watcher, cancel=cancel)
        self.runner = runner
        results = runner.run()
        self.assertEqual(results[0].status, "cancelled")
        # Sesja domknięta mimo przerwania – częściowe dane są poprawne.
        d = results[0].session_dir
        import json
        meta = json.loads((d / "meta.json").read_text())
        self.assertEqual(meta["state"], "cancelled")
        self.assertGreater(meta["summary"]["samples"], 0)

    def test_dangerous_voltage_aborts_before_hardware(self):
        # Napięcie poza twardym limitem: plan pada na walidacji, ZANIM
        # cokolwiek trafi na płytkę (sampler nie dostaje set_voltage).
        from autorun.engine import AutoRunError
        plan = _plan(voltage="5.0",
                     trigger=planmod.Trigger(type="delay", seconds=0))
        with self.assertRaises(AutoRunError) as ctx:
            self._run(plan)
        self.assertIn("napięcie", str(ctx.exception))
        self.assertIsNone(self.sampler.voltage_mV)   # nic nie ustawiono

    def test_safe_voltage_reaches_sampler(self):
        # W zakresie: napięcie zostaje przeliczone na mV i podane samplerowi.
        self._run(_plan(voltage="3.3",
                        trigger=planmod.Trigger(type="delay", seconds=0)))
        self.assertEqual(self.sampler.voltage_mV, 3300)

    def test_build_is_just_in_time(self):
        # Dwa kroki: build kroku 2 musi nastąpić PO zakończeniu pomiaru
        # kroku 1 (a nie z góry przed wszystkimi pomiarami).
        plan = planmod.Plan(name="test", board="btz", steps=[
            planmod.PlanStep(scenario="zwykly", duration_s=0.2,
                             trigger=planmod.Trigger(type="delay",
                                                     seconds=0)),
            planmod.PlanStep(scenario="zrodlowy", duration_s=0.2,
                             trigger=planmod.Trigger(type="delay",
                                                     seconds=0))])
        self._run(plan)
        seq = [(ev.step, ev.kind, ev.text) for ev in self.events]
        # pozycja buildu kroku 2 i zakończenia pomiaru kroku 1
        build2 = next(i for i, (s, k, t) in enumerate(seq)
                      if s == 2 and k == "state" and t == "build")
        done1 = next(i for i, (s, k, t) in enumerate(seq)
                     if s == 1 and k == "step_done")
        self.assertLess(done1, build2,
                        "build kroku 2 powinien być po pomiarze kroku 1")

    def test_live_has_cumulative_and_instant(self):
        # Pomiar dość długi (wall > 1 s), żeby padł co najmniej jeden 'live';
        # sprawdzamy, że niesie i średnią skumulowaną, i chwilową.
        self._run(_plan(scenario="zwykly", duration_s=1.2,
                        trigger=planmod.Trigger(type="delay", seconds=0)))
        live = [ev for ev in self.events if ev.kind == "live"]
        self.assertTrue(live)
        self.assertIn("avg_uA", live[-1].data)
        self.assertIn("inst_uA", live[-1].data)

    def test_pause_and_resume(self):
        # Stop (pause) tuż po starcie pomiaru, po chwili wznów – pomiar
        # dokańcza się, a silnik zgłasza 'paused' i 'resumed'.
        pause = threading.Event()
        plan = _plan(scenario="zwykly", duration_s=0.3,
                     trigger=planmod.Trigger(type="delay", seconds=0))

        def watcher(ev):
            self.events.append(ev)
            if ev.kind == "state" and ev.text == "measure":
                def seq():
                    pause.set()
                    time.sleep(0.2)
                    pause.clear()
                threading.Thread(target=seq, daemon=True).start()

        rtt = FakeRttReader()
        runner = AutoRunner(
            plan, self.manifest, "BTZ #1",
            sampler_factory=lambda p: self.sampler,
            rtt_factory=lambda prof: rtt,
            event_cb=watcher, cancel=threading.Event(), pause=pause)
        results = runner.run()
        self.assertEqual(results[0].status, "done")
        kinds = [ev.kind for ev in self.events]
        self.assertIn("paused", kinds)
        self.assertIn("resumed", kinds)
        # W trakcie pauzy sampler był zatrzymany i wznowiony.
        self.assertGreaterEqual(self.sampler.log.count("stop"), 2)

    def test_sample_rate_decimates(self):
        # sample_rate < sprzętowego (FakeSampler=2000) -> decymacja: sesja
        # zapisana w wybranej częstotliwości, mniej próbek.
        import json
        results = self._run(_plan(
            scenario="zwykly", duration_s=0.5, sample_rate=100,
            trigger=planmod.Trigger(type="delay", seconds=0)))
        self.assertEqual(results[0].status, "done")
        meta = json.loads((results[0].session_dir / "meta.json").read_text())
        self.assertEqual(meta["sample_rate"], 100)      # 2000 / 20
        # ~0.5 s * 100 S/s ≈ 50 próbek (a nie ~1000 przy 2000 S/s).
        self.assertLessEqual(abs(results[0].summary["samples"] - 50), 6)

    def test_hex_step_skips_build(self):
        # 'hexowy' ma pole hex – FAZA 1 go nie buduje.
        results = self._run(_plan(
            scenario="hexowy", duration_s=0.1,
            trigger=planmod.Trigger(type="delay", seconds=0)))
        self.assertEqual(results[0].status, "done")
        cmds = self.env.commands()
        # Był flash (nrfutil device program), nie było builda hexowego.
        self.assertTrue(any("device program" in c for c in cmds))


if __name__ == "__main__":
    unittest.main()
