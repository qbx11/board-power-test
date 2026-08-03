# ============================================================
#  Testy symulacji sprzętu (autorun/mock.py + tryb mock silnika)
# ============================================================
# Symulacja ma pozwolić przejść CAŁY przebieg bez PPK2, programatora i
# płytki. Pilnujemy tu trzech rzeczy: (1) przebieg reaguje na parametry
# kroku, więc na mocku da się sprawdzić sweep i rozrzut powtórek,
# (2) skrót czasu skraca REALNE czekanie, a dane opisują pełne okno
# z planu, (3) wyniki są odgrodzone – osobny katalog sesji i ani jeden
# wiersz w dzienniku pomiarów.

import csv
import json
import threading
import time
import unittest

import numpy as np

from common import FakeEnv
from fakes import FakeRttReader

import power_test as core
from autorun import engine as eng
from autorun import mock as mockmod
from autorun import plan as planmod
from autorun.engine import AutoRunner
from autorun.ppk2 import Ppk2Error


class MockWaveformTest(unittest.TestCase):
    """Sam model przebiegu, bez silnika."""

    def _avg_uA(self, sampler, seconds):
        """Średnia z `seconds` sekund PRZEBIEGU (nie realnych) – liczona
        wprost z modelu, jak zrobiłby to pomiar bez strat."""
        n = int(seconds * sampler.sample_rate)
        return float(sampler._waveform(np.arange(n, dtype=np.int64)).mean())

    def test_podloga_z_pola_expected(self):
        # Pole `expected` scenariusza jest opisowe ('~0.5 uA (DK zmierzone
        # ~0.95 uA)') – bierzemy pierwszą liczbę z jednostką.
        for text, want in (("~0.5 uA (DK zmierzone ~0.95 uA)", 0.5),
                           ("2 mA", 2000.0), ("~1,5 uA", 1.5),
                           ("500 nA", 0.5), ("", 1.0), (None, 1.0),
                           ("bez liczby", 1.0)):
            self.assertAlmostEqual(
                mockmod.baseline_from_expected(text), want, places=6,
                msg=text)

    def test_odstep_wybudzen_z_parametru_serii(self):
        # Wartość z karty jest w sekundach (przeliczenie na jednostki
        # Kconfiga robi plan.sweep_flag_value), więc bierzemy ją wprost.
        self.assertEqual(mockmod.period_from_sweep(
            [("CONFIG_BT_MESH_LPN_POLL_TIMEOUT", "120")]), 120.0)
        self.assertEqual(mockmod.period_from_sweep(
            [("CONFIG_LPN_SENSOR_INTERVAL_S", "5")]), 5.0)
        # Parametr niezwiązany z odstępem i brak serii -> wartość domyślna.
        self.assertEqual(mockmod.period_from_sweep([("CONFIG_LOG", "n")]),
                         mockmod.PERIOD_S)
        self.assertEqual(mockmod.period_from_sweep(None), mockmod.PERIOD_S)

    def test_srednia_maleje_z_odstepem_wybudzen(self):
        # To jest cel modelu: sweep po pollu ma dawać MALEJĄCĄ krzywą
        # średnich, żeby na mocku dało się obejrzeć raport i wykres serii.
        cfg = mockmod.MockConfig(seed=7)
        avgs = []
        for period in (30.0, 60.0, 120.0):
            s = mockmod.MockSampler(cfg)
            s.prepare_step(planmod.PlanStep(
                scenario="x", duration_s=600,
                sweep=[("CONFIG_BT_MESH_LPN_POLL_TIMEOUT", str(period))]),
                {"expected": "~1 uA"})
            self.assertEqual(s.period_s, period)
            avgs.append(self._avg_uA(s, 600))
        self.assertGreater(avgs[0], avgs[1])
        self.assertGreater(avgs[1], avgs[2])
        # Rząd wielkości jak na prawdziwej płytce (patrz stałe w mock.py):
        # kilkanaście-kilkadziesiąt µA przy pollu 30–120 s.
        self.assertTrue(5.0 < avgs[2] < avgs[0] < 60.0, avgs)

    def test_powtorki_tego_samego_kroku_daja_rozne_wyniki(self):
        # Bez tego powtórki x2–x5 byłyby identyczne i nie dałoby się na
        # nich sprawdzić niczego, co dotyczy rozrzutu.
        step = planmod.PlanStep(scenario="x", duration_s=600)
        s = mockmod.MockSampler(mockmod.MockConfig())     # seed=None
        avgs = []
        for _ in range(4):
            s.prepare_step(step, {"expected": "~1 uA"})
            avgs.append(self._avg_uA(s, 600))
        self.assertEqual(len(set(avgs)), len(avgs), avgs)

    def test_seed_daje_powtarzalnosc(self):
        step = planmod.PlanStep(scenario="x", duration_s=600)
        got = []
        for _ in range(2):
            s = mockmod.MockSampler(mockmod.MockConfig(seed=42))
            s.prepare_step(step, {"expected": "~1 uA"})
            got.append(self._avg_uA(s, 600))
        self.assertAlmostEqual(got[0], got[1], places=6)

    def test_pik_na_granicy_odczytow_jest_ten_sam(self):
        # Długość piku wynika z HASZA numeru cyklu, nie z losowania przy
        # odczycie – inaczej pik przecięty granicą porcji raz byłby długi,
        # raz krótki (i średnia zależałaby od tempa odczytów).
        s = mockmod.MockSampler(mockmod.MockConfig(seed=3))
        whole = s._waveform(np.arange(4000, dtype=np.int64))
        halves = np.concatenate([
            s._waveform(np.arange(0, 1500, dtype=np.int64)),
            s._waveform(np.arange(1500, 4000, dtype=np.int64))])
        # Szum jest losowy, więc porównujemy KSZTAŁT: gdzie stoi pik.
        self.assertTrue(np.array_equal(whole > 1000.0, halves > 1000.0))

    def test_odciete_zasilanie_to_brak_pradu(self):
        s = mockmod.MockSampler(mockmod.MockConfig(seed=1))
        s.dut_power(True)
        s.start()
        time.sleep(0.05)
        self.assertTrue(len(s.read()))
        s.dut_power(False)
        time.sleep(0.05)
        chunk = s.read()
        self.assertTrue(len(chunk))
        self.assertEqual(float(chunk.max()), 0.0)

    def test_stale_okno_pomiaru_zamiast_stalego_mnoznika(self):
        # Skrót czasu jest liczony PER KROK, tak żeby każdy pomiar trwał
        # tyle samo realnie: 20 minut z planu -> ×120, 8 godzin -> ×2880.
        cfg = mockmod.MockConfig(window_s=10.0)
        self.assertEqual(cfg.scale_for(1200.0), 120.0)
        self.assertEqual(cfg.scale_for(8 * 3600.0), 2880.0)
        # Pomiar krótszy niż okno symulacji leci w czasie realnym – nie ma
        # po co czekać DŁUŻEJ, niż każe plan.
        self.assertEqual(cfg.scale_for(10.0), 1.0)
        self.assertEqual(cfg.scale_for(3.0), 1.0)
        self.assertEqual(cfg.scale_for(0.0), 1.0)
        # Produkcyjne okno to 10 s (podpis checkboxa czyta tę stałą).
        self.assertEqual(mockmod.MOCK_WINDOW_S, 10.0)
        self.assertEqual(mockmod.MockConfig().window_s, 10.0)

    def test_czestotliwosc_trzyma_sufit_probek(self):
        # Stałe okno + sufit próbek = STAŁE tempo generowania. Bez sufitu
        # 8-godzinny pomiar musiałby wypluć 57 mln próbek w 10 s.
        cfg = mockmod.MockConfig(window_s=10.0)
        for duration in (30.0, 1200.0, 8 * 3600.0):
            rate = cfg.rate_for(duration)
            self.assertLessEqual(rate, mockmod.MOCK_RATE)
            self.assertGreaterEqual(rate, 1)
            self.assertLessEqual(rate * duration,
                                 mockmod.MOCK_MAX_SAMPLES * 1.01,
                                 f"{duration} s -> {rate} S/s")
            # Tempo generowania (próbki na sekundę REALNĄ) jest ograniczone.
            self.assertLessEqual(rate * cfg.scale_for(duration),
                                 mockmod.MOCK_MAX_SAMPLES / cfg.window_s * 1.01)
        # Krótkie okna dostają pełną częstotliwość atrapy.
        self.assertEqual(cfg.rate_for(30.0), mockmod.MOCK_RATE)
        # Długie – obniżoną, żeby zmieścić się w sufcie próbek.
        self.assertLess(cfg.rate_for(8 * 3600.0), mockmod.MOCK_RATE)

    def test_sampler_dostraja_sie_do_dlugosci_kroku(self):
        cfg = mockmod.MockConfig(window_s=10.0, seed=1)
        s = mockmod.MockSampler(cfg)
        s.prepare_step(planmod.PlanStep(scenario="x", duration_s=1200.0),
                       {"expected": "~1 uA"})
        self.assertEqual(s.speedup, 120.0)
        self.assertEqual(s.sample_rate, cfg.rate_for(1200.0))
        s.prepare_step(planmod.PlanStep(scenario="x", duration_s=5.0),
                       {"expected": "~1 uA"})
        self.assertEqual(s.speedup, 1.0)

    def test_napiecie_poza_zakresem_odmowa(self):
        # Ten sam strażnik co na sprzęcie: ścieżka ochrony płytki ma się
        # dać przejść bez PPK2.
        s = mockmod.MockSampler()
        with self.assertRaises(Ppk2Error):
            s.set_voltage(5000)
        s.set_voltage(3000)
        self.assertEqual(s.voltage_mV, 3000)


class MockRunTest(unittest.TestCase):
    """Cały przebieg silnika w symulacji (bez sprzętu, bez atrap testowych
    – silnik sam podstawia MockSampler)."""

    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)
        self.manifest = core.load_manifest()
        self.events = []
        self._patch_engine("MIN_START_DELAY_S", 0.0)
        self._patch_engine("INHIBIT_CMD", "systemd-inhibit-atrapa-brak")

    def _patch_engine(self, name, value):
        old = getattr(eng, name)
        setattr(eng, name, value)
        self.addCleanup(setattr, eng, name, old)

    def _run(self, plan, window_s=0.3, seed=5):
        # Żadnych atrap testowych: silnik ma sam podstawić wszystkie
        # atrapy symulacji (sampler, dongiel, RTT, chip). Podanie tu
        # rtt_factory zasłoniłoby atrapę mocka – jawne fabryki mają
        # pierwszeństwo.
        runner = AutoRunner(
            plan, self.manifest, "BTZ #1",
            event_cb=self.events.append,
            cancel=threading.Event(),
            mock=mockmod.MockConfig(window_s=window_s, seed=seed))
        self.runner = runner
        return runner.run()

    @staticmethod
    def _plan(**step):
        step.setdefault("scenario", "zwykly")
        step.setdefault("duration_s", 30.0)
        step.setdefault("trigger", planmod.Trigger(type="delay", seconds=0))
        return planmod.Plan(name="mock", board="btz",
                            steps=[planmod.PlanStep(**step)])

    def _notes(self):
        return [ev.text for ev in self.events if ev.kind == "note"]

    def test_pomiar_trwa_stale_okno_a_dane_opisuja_plan(self):
        # Sedno symulacji: pomiar zajmuje TYLE, ile okno symulacji (tu 0.3 s
        # zamiast produkcyjnych 10 s), a zapisana sesja opisuje PEŁNE 30 s
        # z planu – czas liczy się z próbek, nie z zegara.
        t0 = time.monotonic()
        results = self._run(self._plan(duration_s=30.0), window_s=0.3)
        realnie = time.monotonic() - t0
        r = results[0]
        self.assertEqual(r.status, "done")
        self.assertLess(realnie, 5.0, f"przebieg zajął {realnie:.1f} s")
        meta = json.loads((r.session_dir / "meta.json").read_text())
        self.assertAlmostEqual(meta["duration_s"], 30.0, delta=1.0)
        self.assertAlmostEqual(r.summary["duration_s"], 30.0, delta=1.0)
        # Częstotliwość w sesji to ta z atrapy (karta prosiła o 100 kS/s,
        # ale symulacja tyle nie wyrobi – patrz komentarz w mock.py).
        self.assertEqual(meta["sample_rate"], mockmod.MOCK_RATE)
        self.assertGreater(r.summary["samples"], 30 * mockmod.MOCK_RATE * 0.9)
        # Dane nie są płaskie: podłoga uśpienia i piki wybudzeń.
        self.assertLess(r.summary["min_uA"], r.summary["avg_uA"])
        self.assertGreater(r.summary["max_uA"], 1000.0)

    def test_sesje_ida_do_sessions_mock_a_dziennik_zostaje_pusty(self):
        results = self._run(self._plan())
        session = results[0].session_dir
        self.assertIn("sessions-mock", str(session))
        self.assertFalse((core.CSV_PATH.parent / "sessions").exists())
        # Dziennik: ani jednego wiersza (plik może nie istnieć wcale).
        rows = []
        if core.CSV_PATH.is_file():
            with open(core.CSV_PATH, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        self.assertEqual(rows, [])
        notes = " | ".join(self._notes())
        self.assertIn("SYMULACJA", notes)
        self.assertIn("NIE trafia do dziennika", notes)
        # Ta sama prawda w plan.log przebiegu.
        log = (self.runner.run_dir / "plan.log").read_text()
        self.assertIn("SYMULACJA", log)

    def test_build_jest_prawdziwy_a_flash_pominiety(self):
        # Wybór świadomy: build sprawdza flagi i Kconfig, więc leci
        # normalnie; flash bez płytki nie ma sensu.
        self._run(self._plan())
        cmds = self.env.commands()
        self.assertTrue([c for c in cmds if c.startswith("west build")])
        self.assertFalse([c for c in cmds if c.startswith("west flash")])
        lines = [ev.text for ev in self.events if ev.kind == "line"]
        self.assertTrue(any("[SYMULACJA] flash pominięty" in t
                            for t in lines), lines[:5])
        # Panel dostaje komendę, której nie wykonano – żeby było widać CO
        # poszłoby na płytkę.
        self.assertTrue(any("west flash" in t for t in lines))

    def test_powtorki_dostaja_swieze_losowanie(self):
        # x3 w karcie -> trzy osobne pomiary, każdy z NOWYM losowaniem
        # retransmisji. Sprawdzamy mechanizm (trzy różne ziarna), a nie
        # same średnie: przy oknie krótszym od kilku cykli wybudzeń średnia
        # przyjmuje kilka dyskretnych wartości i dwa przebiegi mogą wyjść
        # identyczne przez przypadek. Rozrzut samego modelu (długie okno)
        # pilnuje MockWaveformTest.
        steps = planmod.expand_repeats(
            [planmod.PlanStep(scenario="zwykly", duration_s=20.0, label="1",
                              trigger=planmod.Trigger(type="delay",
                                                      seconds=0))], 3)
        sampler = mockmod.MockSampler(mockmod.MockConfig(window_s=0.2))
        runner = AutoRunner(
            planmod.Plan(name="mock", board="btz", steps=steps),
            self.manifest, "BTZ #1",
            sampler_factory=lambda p: sampler,
            event_cb=self.events.append,
            mock=mockmod.MockConfig(window_s=0.2))
        results = runner.run()
        self.assertEqual([r.status for r in results], ["done"] * 3)
        self.assertEqual([r.label for r in results], ["1/1", "1/2", "1/3"])
        seeds = [entry for entry in sampler.log if entry.startswith("step ")]
        self.assertEqual(len(seeds), 3, seeds)
        self.assertEqual(len(set(seeds)), 3, seeds)

    def test_sweep_daje_malejaca_krzywa(self):
        steps = planmod.expand_sweep(
            1, [("CONFIG_BT_MESH_LPN_POLL_TIMEOUT", "30, 60, 120")],
            dict(scenario="zwykly", duration_s=240.0,
                 trigger=planmod.Trigger(type="delay", seconds=0)))
        results = self._run(planmod.Plan(name="mock", board="btz",
                                         steps=steps), window_s=0.3)
        avgs = [r.summary["avg_uA"] for r in results]
        self.assertEqual([r.status for r in results], ["done"] * 3)
        self.assertGreater(avgs[0], avgs[1], avgs)
        self.assertGreater(avgs[1], avgs[2], avgs)

    def test_odliczanie_pokazuje_sekundy_z_planu(self):
        # Start po czasie 60 s przy ×100 czeka realnie ~0,6 s, ale
        # odliczanie ma pokazywać sekundy PLANU – inaczej UI kłamałoby
        # o tym, na co czeka.
        t0 = time.monotonic()
        self._run(self._plan(duration_s=10.0,
                             trigger=planmod.Trigger(type="delay",
                                                     seconds=60)))
        self.assertLess(time.monotonic() - t0, 10.0)
        remaining = [ev.data["remaining_s"] for ev in self.events
                     if ev.kind == "countdown"]
        self.assertTrue(remaining)
        self.assertGreater(max(remaining), 30.0, remaining[:5])
        self.assertLessEqual(max(remaining), 60.0)

    # ---------- triggery: bez atrap każdy krok padłby na timeout ----------

    def test_trigger_z_logu_dongla(self):
        results = self._run(self._plan(
            duration_s=20.0, monitor_port="/dev/ttyMOCK",
            trigger=planmod.Trigger(type="serial",
                                    pattern="friendship z 0x0001",
                                    timeout_s=30.0)))
        self.assertEqual(results[0].status, "done")
        # Linia z wzorcem poszła do logu dongla sesji i do panelu.
        dongle = (results[0].session_dir / "dongle.log").read_text()
        self.assertIn("friendship z 0x0001", dongle)
        monitor = [ev.text for ev in self.events if ev.kind == "monitor"]
        self.assertTrue(any("friendship z 0x0001" in t for t in monitor),
                        monitor)
        # Bufor sprzed flasha nie może zjeść linii triggera: atrapa milczy
        # na starcie, więc drenaż nic nie wyrzuca.
        self.assertFalse(any("przed flashem" in t for t in monitor), monitor)

    def test_trigger_z_konsoli_rtt(self):
        results = self._run(self._plan(
            duration_s=20.0, rtt="trigger",
            trigger=planmod.Trigger(type="rtt", pattern="friendship",
                                    timeout_s=30.0)))
        self.assertEqual(results[0].status, "done")
        rtt_log = (results[0].session_dir / "rtt.log").read_text()
        self.assertIn("friendship", rtt_log)

    def test_etykiety_rtt_continuous(self):
        # rtt='continuous': atrapa wypisuje wzorce reguł, więc w sesji
        # powstają adnotacje – da się pracować nad ich wyświetlaniem.
        results = self._run(self._plan(
            duration_s=20.0, rtt="continuous",
            labels=[planmod.LabelRule(pattern="publikacja temperatury",
                                      label="publikacja")],
            trigger=planmod.Trigger(type="delay", seconds=0)))
        self.assertEqual(results[0].status, "done")
        marks = [ev.text for ev in self.events if ev.kind == "annotation"]
        self.assertTrue(marks, "brak adnotacji z etykiet RTT")

    def test_trigger_mattera(self):
        # Zakładka Thread: pomiar rusza na pierwszym raporcie subskrypcji.
        self._patch_engine("CHIP_START_SETTLE_S", 0.2)
        results = self._run(self._plan(
            duration_s=20.0,
            trigger=planmod.Trigger(type="chip", node_id="5",
                                    discriminator="3840", dataset="0e08",
                                    timeout_s=30.0)))
        self.assertEqual(results[0].status, "done")
        chip_log = (results[0].session_dir / "chip.log").read_text()
        self.assertIn("subscription established", chip_log)
        self.assertIn("FIRST-VALUE", chip_log)
        notes = " | ".join(self._notes())
        self.assertIn("pierwsza wartość", notes)

    def test_atrapy_nie_wchodza_gdy_nie_ma_mocka(self):
        # Fabryki podane jawnie mają pierwszeństwo nad atrapami symulacji
        # (na tym stoją wszystkie pozostałe testy silnika).
        marker = object()
        runner = AutoRunner(self._plan(), self.manifest, "BTZ #1",
                            chip_factory=lambda *a: marker,
                            mock=mockmod.MockConfig())
        self.assertIs(runner.chip_factory("c", None, None, 1, "s"), marker)
        # Bez mocka: prawdziwe fabryki.
        plain = AutoRunner(self._plan(), self.manifest, "BTZ #1")
        self.assertIs(plain.chip_factory, eng.default_chip_factory)
        self.assertIs(plain.serial_factory, eng.default_serial_factory)

    def test_bez_mocka_nic_sie_nie_zmienia(self):
        # Skala czasu poza symulacją to 1.0, a sesje wracają do
        # reports/sessions (regresja: mock nie może wyciekać do sprzętu).
        runner = AutoRunner(self._plan(), self.manifest, "BTZ #1",
                            sampler_factory=lambda p: mockmod.MockSampler(
                                mockmod.MockConfig(window_s=0.2, seed=1)),
                            rtt_factory=lambda prof: FakeRttReader(),
                            event_cb=self.events.append)
        self.assertEqual(runner._time_scale, 1.0)
        results = runner.run()
        self.assertIn("reports/sessions/", str(results[0].session_dir))
        with open(core.CSV_PATH, newline="", encoding="utf-8") as f:
            self.assertEqual(len(list(csv.DictReader(f))), 1)


if __name__ == "__main__":
    unittest.main()
