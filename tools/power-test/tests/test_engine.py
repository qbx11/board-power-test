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

import numpy as np

from common import FakeEnv
from fakes import FakeRttReader, FakeSampler, FakeSerialReader

import power_test as core
from autorun import plan as planmod
from autorun.engine import AutoRunner


class _LossySampler(FakeSampler):
    """PPK2, który gubi próbki (USB nie nadąża) – oddaje tylko `keep`
    część tego, co powinno przyjść. Oś liczona próbkami zostaje wtedy
    w tyle za zegarem."""

    def __init__(self, keep=0.5, **kw):
        super().__init__(**kw)
        self.keep = keep

    def read(self):
        chunk = super().read()
        return chunk[:int(len(chunk) * self.keep)]


class _StepSampler(FakeSampler):
    """Prąd skacze z `low` na `high` po `switch_s` od startu pomiaru.
    Średnia z całej sekundy wypada wtedy POŚRODKU obu poziomów, a średnia
    z krótkiego okna trafia w poziom bieżący – to rozróżnia oba okna."""

    def __init__(self, switch_s=0.5, low=10.0, high=1000.0, **kw):
        super().__init__(**kw)
        self.switch_s = switch_s
        self.low = low
        self.high = high

    def read(self):
        chunk = super().read()
        if not len(chunk):
            return chunk
        level = (self.high if time.monotonic() - self._t0 >= self.switch_s
                 else self.low)
        return np.full(len(chunk), level, np.float32)


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
        # Podłoga startu po flashu to sekundy realnego czekania – w
        # testach niepowiązanych z nią zerujemy ją, żeby suite nie
        # spowolnił. Klasy sprawdzające samą podłogę ustawiają ją same.
        self._set_min_delay(0.0)

    def _set_min_delay(self, seconds):
        import autorun.engine as eng
        old, eng.MIN_START_DELAY_S = eng.MIN_START_DELAY_S, seconds
        self.addCleanup(setattr, eng, "MIN_START_DELAY_S", old)

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

    def _closed_before_plan_done(self):
        """Indeks zdarzenia 'plan_done' i liczba zdarzeń w chwili zamknięcia
        samplera (do porównania kolejności)."""
        done_at = next(i for i, ev in enumerate(self.events)
                       if ev.kind == "plan_done")
        return self._closed_at, done_at

    def _watch_close(self):
        """Zapamiętaj, ile zdarzeń poleciało, zanim sampler się zamknął."""
        self._closed_at = None
        orig = self.sampler.close

        def close():
            orig()
            if self._closed_at is None:
                self._closed_at = len(self.events)

        self.sampler.close = close

    def test_ppk2_zwolnione_przed_ogloszeniem_konca(self):
        # REGRESJA (#23): 'plan_done' odsłania w UI wyjście z ekranu, a więc
        # i start kolejnego przebiegu. Gdy PPK2 zamykało się dopiero PO tym
        # zdarzeniu, następny pomiar trafiał na wciąż otwarte urządzenie –
        # i nie ruszał bez fizycznego restartu PPK2.
        self._watch_close()
        self._run(_plan(trigger=planmod.Trigger(type="delay", seconds=0)))
        closed_at, done_at = self._closed_before_plan_done()
        self.assertIsNotNone(closed_at, "sampler nie został zamknięty")
        self.assertLessEqual(closed_at, done_at)

    def test_ppk2_zwolnione_przed_ogloszeniem_przerwania(self):
        # Ta sama kolejność na ścieżce Esc – to ona zgłoszona w #23.
        cancel = threading.Event()
        self._watch_close()
        plan = _plan(duration_s=30,
                     trigger=planmod.Trigger(type="delay", seconds=0))

        def watcher(ev):
            self.events.append(ev)
            if ev.kind == "state" and ev.text == "measure":
                threading.Timer(0.3, cancel.set).start()

        rtt = FakeRttReader()
        AutoRunner(plan, self.manifest, "BTZ #1",
                   sampler_factory=lambda p: self.sampler,
                   rtt_factory=lambda prof: rtt,
                   event_cb=watcher, cancel=cancel).run()
        closed_at, done_at = self._closed_before_plan_done()
        self.assertIsNotNone(closed_at, "sampler nie został zamknięty")
        self.assertLessEqual(closed_at, done_at)
        self.assertFalse(self.sampler.dut)      # zasilanie płytki odcięte

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

    def test_teraz_pokazuje_krotkie_okno_a_nie_cala_sekunde(self):
        # REGRESJA: „teraz” liczone z całej sekundy to średnia ze 100 000
        # próbek – jej błąd standardowy jest tak mały, że na ekranie stała
        # ta sama liczba do końca pomiaru. Prąd skacze tu w połowie okna:
        # średnia sekundowa dałaby ~505 (pół na pół), krótkie okno musi
        # pokazać poziom bieżący.
        self.sampler = _StepSampler(sample_rate=2000, switch_s=0.5,
                                    low=10.0, high=1000.0)
        self._run(_plan(duration_s=1.5,
                        trigger=planmod.Trigger(type="delay", seconds=0)))
        live = [ev for ev in self.events if ev.kind == "live"]
        self.assertTrue(live)
        inst = live[-1].data["inst_uA"]
        self.assertGreater(inst, 900,
                           f"'teraz' = {inst} – wygląda na średnią z całej "
                           f"sekundy, nie z ostatnich 100 ms")
        # Kontrola: średnia SKUMULOWANA ma dalej mieszać oba poziomy,
        # inaczej test przechodziłby też przy zepsutej średniej.
        avg = live[-1].data["avg_uA"]
        self.assertLess(avg, 800, f"średnia skumulowana = {avg}")

    def test_status_json_zostaje_przy_sredniej_sekundowej(self):
        # Pole w status.json nazywa się avg_1s_uA i viewer czyta je jako
        # średnią SEKUNDOWĄ – skrócenie okna „teraz” w UI nie ma go po
        # cichu podmienić na wartość z krótkiego okna.
        from autorun import session as sessmod
        seen = []
        orig = sessmod.SessionWriter.update_status

        def spy(writer, state, avg_1s_uA=None):
            seen.append((state, avg_1s_uA))
            return orig(writer, state, avg_1s_uA)

        sessmod.SessionWriter.update_status = spy
        self.addCleanup(setattr, sessmod.SessionWriter, "update_status",
                        orig)
        self.sampler = _StepSampler(sample_rate=2000, switch_s=0.5,
                                    low=10.0, high=1000.0)
        self._run(_plan(duration_s=1.5,
                        trigger=planmod.Trigger(type="delay", seconds=0)))
        vals = [v for state, v in seen
                if state == "measuring" and v is not None]
        self.assertTrue(vals, "brak statusu z trwającego pomiaru")
        # Sekunda miesza oba poziomy (~505); poziom bieżący to 1000.
        self.assertLess(vals[-1], 900,
                        f"status.json dostał {vals[-1]} – to wygląda na "
                        "wartość z krótkiego okna, nie z sekundy")

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

    def test_serial_trigger_fires_and_streams(self):
        # Monitor dongla: linie lecą jako 'monitor', a pomiar startuje, gdy
        # linia ZAWIERA fragment triggera (podłańcuch, nie regex).
        serial = FakeSerialReader([
            (0.02, "[00:00:01.000] <inf> node_friend: boot"),
            (0.06, "[00:00:08.379] <inf> node_friend: "
                   "Friendship z LPN nawiazany")])
        plan = _plan(scenario="zwykly", duration_s=0.15,
                     monitor_port="/dev/ttyACM0",
                     trigger=planmod.Trigger(type="serial",
                                             pattern="Friendship z LPN "
                                             "nawiazany", timeout_s=5))
        runner = AutoRunner(
            plan, self.manifest, "BTZ #1",
            sampler_factory=lambda p: self.sampler,
            serial_factory=lambda port: serial,
            event_cb=self.events.append, cancel=threading.Event())
        results = runner.run()
        self.assertEqual(results[0].status, "done")
        mon = [ev.text for ev in self.events if ev.kind == "monitor"]
        self.assertTrue(any("Friendship z LPN nawiazany" in m for m in mon))
        # Fragment logu zapisany do dongle.log sesji.
        dlog = (results[0].session_dir / "dongle.log").read_text()
        self.assertIn("node_friend", dlog)

    def test_serial_trigger_timeout_skips(self):
        serial = FakeSerialReader([(0.01, "nic ciekawego")])
        plan = _plan(scenario="zwykly", duration_s=0.1,
                     monitor_port="/dev/ttyACM0",
                     trigger=planmod.Trigger(type="serial",
                                             pattern="NigdyNie",
                                             timeout_s=0.3))
        plan.on_step_error = "skip"
        runner = AutoRunner(
            plan, self.manifest, "BTZ #1",
            sampler_factory=lambda p: self.sampler,
            serial_factory=lambda port: serial,
            event_cb=self.events.append, cancel=threading.Event())
        results = runner.run()
        self.assertEqual(results[0].status, "trigger_timeout")
        # Monitor pokazuje się JUŻ podczas czekania na trigger (nagłówek +
        # linie), nawet gdy trigger nigdy nie padnie.
        mon = [ev for ev in self.events if ev.kind == "monitor"]
        self.assertTrue(mon)
        self.assertTrue(any("monitor dongla" in ev.text for ev in mon))

    def test_delay_emits_countdown(self):
        self._run(_plan(scenario="zwykly", duration_s=0.1,
                        trigger=planmod.Trigger(type="delay", seconds=1)))
        cd = [ev for ev in self.events if ev.kind == "countdown"]
        self.assertTrue(cd)
        self.assertIn("remaining_s", cd[0].data)

    def _trigger_wait_s(self, plan, rtt_script=None):
        """Ile ZEGAROWO minęło od wejścia w stan 'trigger' do otwarcia
        okna pomiaru (stan 'measure')."""
        marks = {}

        def watcher(ev):
            self.events.append(ev)
            if ev.kind != "state":
                return
            if ev.text == "trigger":
                marks["start"] = time.monotonic()
            elif ev.text == "measure":
                # setdefault: powtórka pomiaru emituje 'measure' ponownie.
                marks.setdefault("end", time.monotonic())

        results = AutoRunner(
            plan, self.manifest, "BTZ #1",
            sampler_factory=lambda p: self.sampler,
            rtt_factory=lambda prof: FakeRttReader(rtt_script),
            event_cb=watcher, cancel=threading.Event()).run()
        return results, marks["end"] - marks["start"]

    def test_start_pomiaru_ma_stala_podloge_po_flashu(self):
        # Płytka po flashu się resetuje – pierwsze sekundy to prąd
        # rozruchu, nie scenariusza. Nawet bez „startu po czasie”
        # odczekujemy podłogę i pokazujemy odliczanie.
        self._set_min_delay(0.6)
        results, waited = self._trigger_wait_s(_plan(
            duration_s=0.2, trigger=planmod.Trigger(type="delay", seconds=0)))
        self.assertEqual(results[0].status, "done")
        self.assertGreater(waited, 0.6 - 0.05, f"czekano tylko {waited:.2f} s")
        self.assertLess(waited, 0.6 + 0.5, f"czekano aż {waited:.2f} s")
        self.assertTrue([ev for ev in self.events if ev.kind == "countdown"],
                        "odliczanie musi być widoczne w UI")

    def test_wlasny_czas_startu_zastepuje_podloge_a_nie_dodaje(self):
        # Sedno: 0.9 s z planu przy podłodze 0.4 s daje 0.9 s, NIE 1.3 s.
        self._set_min_delay(0.4)
        _r, waited = self._trigger_wait_s(_plan(
            duration_s=0.2,
            trigger=planmod.Trigger(type="delay", seconds=0.9)))
        self.assertGreater(waited, 0.9 - 0.05)
        self.assertLess(waited, 0.9 + 0.35,
                        f"czasy się zsumowały: {waited:.2f} s")

    def test_krotszy_wlasny_czas_podnosi_sie_do_podlogi(self):
        # Druga strona max(): 0.2 s z planu nie skraca podłogi.
        self._set_min_delay(0.8)
        _r, waited = self._trigger_wait_s(_plan(
            duration_s=0.2,
            trigger=planmod.Trigger(type="delay", seconds=0.2)))
        self.assertGreater(waited, 0.8 - 0.05, f"czekano tylko {waited:.2f} s")

    def test_podloga_nie_dotyczy_triggera_rtt(self):
        # Przy RTT czekaniem jest sam wzorzec. Doliczenie podłogi
        # groziłoby przegapieniem linii wypisanej tuż po rozruchu.
        self._set_min_delay(5.0)
        _r, waited = self._trigger_wait_s(
            _plan(duration_s=0.2, rtt="trigger",
                  trigger=planmod.Trigger(type="rtt", pattern="Ready",
                                          timeout_s=5)),
            rtt_script=[(0.05, "System Ready now")])
        # Próg z zapasem: po złapaniu wzorca silnik odłącza J-Linka i daje
        # płytce ~1 s na uspokojenie – to nie jest podłoga startu.
        self.assertLess(waited, 2.0,
                        f"RTT czekał na podłogę zamiast na wzorzec "
                        f"({waited:.2f} s)")

    def test_sweep_distinct_builds_and_csv(self):
        # Seria (sweep): jeden "Pomiar 1" -> "1.1/1.2/1.3", każda wartość
        # budowana do OSOBNEGO katalogu, a wartość parametru trafia do CSV.
        base = dict(scenario="zwykly", duration_s=0.15, power_cycle=True,
                    trigger=planmod.Trigger(type="delay", seconds=0))
        steps = planmod.expand_sweep(
            1, "CONFIG_LPN_SENSOR_INTERVAL_S", "1, 5, 10", base)
        results = self._run(planmod.Plan(name="serie", board="btz",
                                         steps=steps))
        self.assertEqual([r.status for r in results], ["done"] * 3)
        self.assertEqual([r.label for r in results], ["1.1", "1.2", "1.3"])

        build_dirs = []
        for c in self.env.commands():
            if c.startswith("west build"):
                parts = c.split()
                build_dirs.append(parts[parts.index("-d") + 1])
        self.assertEqual(len(build_dirs), 3)
        self.assertEqual(len(set(build_dirs)), 3,
                         "każda wartość powinna mieć własny katalog builda")

        with open(core.CSV_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["pomiar_id"] for r in rows], ["1.1", "1.2", "1.3"])
        self.assertTrue(all(r["parametr"] == "CONFIG_LPN_SENSOR_INTERVAL_S"
                            for r in rows))
        self.assertEqual([r["wartosc"] for r in rows], ["1", "5", "10"])
        self.assertIn("-DCONFIG_LPN_SENSOR_INTERVAL_S=1", rows[0]["flagi"])

    def test_sweep_meta_and_events(self):
        import json
        base = dict(scenario="zwykly", duration_s=0.15, power_cycle=True,
                    trigger=planmod.Trigger(type="delay", seconds=0))
        # Parametr bez prefiksu CONFIG_ też jest akceptowany (normalizacja).
        steps = planmod.expand_sweep(
            1, "LPN_SENSOR_INTERVAL_S", ["2", "8"], base)
        results = self._run(planmod.Plan(name="serie", board="btz",
                                         steps=steps))
        meta = json.loads((results[0].session_dir / "meta.json").read_text())
        self.assertEqual(meta["step_label"], "1.1")
        self.assertEqual(meta["sweep"],
                         {"param": "CONFIG_LPN_SENSOR_INTERVAL_S",
                          "value": "2"})
        self.assertIn("-DCONFIG_LPN_SENSOR_INTERVAL_S=2", meta["flags"])
        measure = [ev for ev in self.events
                   if ev.kind == "state" and ev.text == "measure"]
        self.assertEqual(measure[0].data.get("label"), "1.1")
        self.assertEqual(measure[0].data.get("sweep"),
                         {"param": "CONFIG_LPN_SENSOR_INTERVAL_S",
                          "value": "2"})

    def test_flash_wymusza_reset_i_erase(self):
        # REGRESJA: bez --reset J-Link zostawia układ w stanie po
        # programowaniu (firmware nie startuje), więc pomiar łapie stary
        # stan i pobór prądu jest zawyżony. Tryb ręczny ma na to własny
        # test (test_tui.py) – tu pilnujemy ścieżki autonomicznej.
        self._run(_plan(trigger=planmod.Trigger(type="delay", seconds=0)))
        flashes = [c for c in self.env.commands()
                   if c.startswith("west flash")]
        self.assertTrue(flashes)
        self.assertTrue(all("--reset" in c for c in flashes), flashes)
        self.assertTrue(all("--erase" in c for c in flashes), flashes)

    def test_ostrzega_o_cudzej_sesji_jlinka(self):
        # Cudzy właściciel sondy zawyża pomiar; przebiegu nie blokujemy
        # (może iść z crona), ale musi to być widać w logu/UI.
        self.env.jlink_owners = [(4242, "nrfutil-device list --hotplug")]
        self._run(_plan(trigger=planmod.Trigger(type="delay", seconds=0)))
        notes = [ev.text for ev in self.events if ev.kind == "note"]
        self.assertTrue(any("Sondę J-Link trzyma inny program" in n
                            for n in notes), notes)

    def _measure_seconds(self, plan, sampler):
        """Ile ZEGAROWO trwało samo okno pomiaru (od stanu 'measure' do
        'step_done')."""
        marks = {}

        def watcher(ev):
            self.events.append(ev)
            if ev.kind == "state" and ev.text == "measure":
                marks["start"] = time.monotonic()
            elif ev.kind == "step_done":
                marks["end"] = time.monotonic()

        self.sampler = sampler
        results = AutoRunner(
            plan, self.manifest, "BTZ #1",
            sampler_factory=lambda p: sampler,
            rtt_factory=lambda prof: FakeRttReader(),
            event_cb=watcher, cancel=threading.Event()).run()
        return results, marks["end"] - marks["start"]

    def _allow_lossy(self, fraction=0.95):
        """Podnieś próg strat na czas testu – tu badamy CZAS trwania okna,
        nie politykę odrzucania (ta ma własne testy)."""
        from autorun import engine as eng
        orig = eng.MAX_LOST_FRACTION
        eng.MAX_LOST_FRACTION = fraction
        self.addCleanup(setattr, eng, "MAX_LOST_FRACTION", orig)

    def test_pomiar_konczy_sie_z_zegarem_mimo_zgubionych_probek(self):
        # REGRESJA: pętla kończyła się dopiero po zebraniu duration_s * rate
        # PRÓBEK, więc gdy PPK2 gubiło dane, pomiar ciągnął się dalej mimo
        # wyzerowanego odliczania (obserwacja: +10 s). Okno ma zamykać
        # zegar; braki to dziury w danych, nie powód do przedłużania.
        self._allow_lossy()
        plan = _plan(duration_s=2, sample_rate=100,
                     trigger=planmod.Trigger(type="delay", seconds=0))
        results, measured_s = self._measure_seconds(
            plan, _LossySampler(sample_rate=200, keep=0.5))
        # Połowa próbek przepada: przed poprawką okno trwało ~2x dłużej.
        self.assertLess(measured_s, 2 + 0.8, f"pomiar trwał {measured_s:.1f} s")
        self.assertGreater(measured_s, 2 - 0.5)
        # Dane są krótsze niż okno – i musi to być odnotowane.
        summary = results[0].summary
        self.assertLess(summary["samples"], 2 * 100)
        self.assertGreater(summary["lost_samples"], 0)

    def test_pelny_pomiar_zbiera_komplet_probek(self):
        # Kontrola do powyższego: gdy nic nie ginie, okno zegarowe daje
        # pełny komplet próbek (poprawka nie skraca zdrowego pomiaru).
        plan = _plan(duration_s=1, sample_rate=100,
                     trigger=planmod.Trigger(type="delay", seconds=0))
        results, measured_s = self._measure_seconds(
            plan, FakeSampler(sample_rate=200))
        self.assertLess(abs(results[0].summary["samples"] - 100), 12)
        self.assertEqual(results[0].summary["lost_samples"], 0)
        self.assertLess(measured_s, 1 + 0.8)

    def test_odliczanie_idzie_zegarem_a_nie_probkami(self):
        # REGRESJA (#22): odliczanie w trybie autonomicznym „zacinało się”
        # – ta sama sekunda pokazywała się dwa razy. Powód: UI liczyło
        # pozostały czas z `elapsed_s`, a ten idzie PRÓBKAMI, więc przy
        # zgubionych próbkach zostaje w tyle za zegarem. Zdarzenie `live`
        # musi nieść czas zegarowy pomiaru.
        self._allow_lossy()
        self.sampler = _LossySampler(sample_rate=200, keep=0.5)
        # Okno musi być dłuższe niż kilka sekund siatki statusów – od kiedy
        # kończy je zegar, krótki pomiar nie zdąży ich wyemitować tylu.
        self._run(_plan(duration_s=3.2,
                        trigger=planmod.Trigger(type="delay", seconds=0)))
        live = [ev for ev in self.events if ev.kind == "live"]
        self.assertGreaterEqual(len(live), 3, "za mało statusów na żywo")
        last = live[-1].data
        self.assertIsNotNone(last.get("wall_elapsed_s"))
        # Połowa próbek przepadła, więc oś próbek jest ~2x wolniejsza od
        # zegara – to właśnie ono zatrzymywało odliczanie.
        self.assertGreater(last["wall_elapsed_s"], last["elapsed_s"] + 0.5)
        # Zegar idzie równo: kolejne statusy co ~1 s (siatka bez dryfu).
        stamps = [ev.data["wall_elapsed_s"] for ev in live]
        for prev, nxt in zip(stamps, stamps[1:]):
            self.assertAlmostEqual(nxt - prev, 1.0, delta=0.35)

    def test_duze_straty_powtarzaja_pomiar_a_potem_odrzucaja(self):
        # Obserwacja z pola: PPK2 zgubiło 35% okna. Taki wynik jest
        # bezwartościowy (średnia nie opisuje przebiegu) i NIE MA prawa
        # trafić do dziennika jako zdrowy. Polityka: powtórz raz, druga
        # porażka = błąd kroku.
        plan = _plan(duration_s=1, sample_rate=100,
                     trigger=planmod.Trigger(type="delay", seconds=0))
        self.sampler = _LossySampler(sample_rate=200, keep=0.5)
        results = self._run(plan)
        # Krok kończy się błędem (dalej decyduje polityka planu).
        self.assertEqual(results[0].status, "error")
        self.assertIn("zgubiło", results[0].error)
        # Pomiar poszedł DWA razy (pierwotny + powtórka).
        measure = [ev for ev in self.events
                   if ev.kind == "state" and ev.text == "measure"]
        self.assertEqual(len(measure), 2, "brak automatycznego powtórzenia")
        # …i nic nie wylądowało w dzienniku jako zdrowy pomiar (dziennik
        # nawet nie powstał – nie było czego zapisać).
        if core.CSV_PATH.is_file():
            with open(core.CSV_PATH, newline="", encoding="utf-8") as f:
                self.assertEqual(list(csv.DictReader(f)), [])

    def test_drobne_straty_nie_powtarzaja_pomiaru(self):
        # Kontrola: braki poniżej progu przechodzą bez powtarzania – inaczej
        # każdy pomiar kręciłby się dwa razy.
        plan = _plan(duration_s=1, sample_rate=100,
                     trigger=planmod.Trigger(type="delay", seconds=0))
        self.sampler = FakeSampler(sample_rate=200)
        results = self._run(plan)
        self.assertEqual(results[0].status, "done")
        measure = [ev for ev in self.events
                   if ev.kind == "state" and ev.text == "measure"]
        self.assertEqual(len(measure), 1)

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
