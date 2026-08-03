# ============================================================
#  Testy wczytywania i walidacji planów (autorun/plan.py)
# ============================================================

import tomllib
import unittest
from pathlib import Path

import common  # noqa: F401  (dołącza TOOL_DIR do sys.path)

from autorun import plan as planmod


def _write_plan(tmpdir, text):
    p = Path(tmpdir) / "p.toml"
    p.write_text(text, encoding="utf-8")
    return p


MANIFEST = tomllib.loads("""
[defaults]
profile = "btz"
[boards.btz]
board = "BTZ/nrf54l15/cpuapp"
runner = "jlink"
[boards.dk]
board = "nrf54l15dk/nrf54l15/cpuapp"
[scenarios.reset_only]
cmake_args = ["-DCONFIG_X=y"]
[scenarios.app]
source = "app_dir"
[scenarios.gotowy]
hex = "fw.hex"
""")


class DurationTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(planmod.parse_duration("45s"), 45)
        self.assertEqual(planmod.parse_duration("20m"), 1200)
        self.assertEqual(planmod.parse_duration("8h"), 28800)
        self.assertEqual(planmod.parse_duration("1h30m"), 5400)
        self.assertEqual(planmod.parse_duration(90), 90)
        self.assertEqual(planmod.parse_duration("2,5s"), 2.5)

    def test_bad(self):
        for bad in ("", "abc", "0s", "-5", "10x"):
            with self.assertRaises(ValueError):
                planmod.parse_duration(bad)


class LoadTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_minimal(self):
        p = _write_plan(self.dir, """
[plan]
name = "t"
[[plan.steps]]
scenario = "reset_only"
duration = "30s"
""")
        plan = planmod.load_plan(p)
        self.assertEqual(plan.name, "t")
        self.assertEqual(len(plan.steps), 1)
        step = plan.steps[0]
        self.assertEqual(step.duration_s, 30)
        self.assertEqual(step.trigger.type, "delay")
        self.assertEqual(step.rtt, "off")
        self.assertTrue(step.power_cycle)
        self.assertEqual(planmod.validate_plan(plan, MANIFEST), [])

    def test_full_step(self):
        p = _write_plan(self.dir, """
[plan]
name = "t"
board = "btz"
[[plan.steps]]
scenario = "app"
duration = "2h"
voltage = "2.5"
trigger = { type = "rtt", pattern = "Ready", timeout = "60s" }
rtt = "continuous"
storage = { mode = "both", window_ms = 2 }
build_extra_args = ["-DCONFIG_LOG=y"]
[[plan.steps.labels]]
pattern = "Poll"
label = "Friend Poll"
""")
        plan = planmod.load_plan(p)
        step = plan.steps[0]
        self.assertEqual(step.voltage, "2.5")
        self.assertEqual(step.trigger.type, "rtt")
        self.assertEqual(step.trigger.timeout_s, 60)
        self.assertEqual(step.storage.mode, "both")
        self.assertEqual(step.storage.window_ms, 2)
        self.assertEqual(step.build_extra_args, ["-DCONFIG_LOG=y"])
        self.assertEqual(len(step.labels), 1)
        self.assertEqual(planmod.validate_plan(plan, MANIFEST), [])

    def test_chip_trigger_icd_fields(self):
        p = _write_plan(self.dir, """
[plan]
name = "t"
[[plan.steps]]
scenario = "reset_only"
duration = "30s"
trigger = { type = "chip", node_id = "5", dataset = "0e08aa", \
discriminator = "3840", icd_registration = true, icd_stay_active_ms = 15000 }
""")
        plan = planmod.load_plan(p)
        trig = plan.steps[0].trigger
        self.assertTrue(trig.icd_registration)
        self.assertEqual(trig.icd_stay_active_ms, 15000)
        self.assertEqual(planmod.validate_plan(plan, MANIFEST), [])

    def test_chip_trigger_icd_defaults_off(self):
        p = _write_plan(self.dir, """
[plan]
name = "t"
[[plan.steps]]
scenario = "reset_only"
duration = "30s"
trigger = { type = "chip", node_id = "5", dataset = "0e08aa", \
discriminator = "3840" }
""")
        trig = planmod.load_plan(p).steps[0].trigger
        self.assertFalse(trig.icd_registration)
        self.assertEqual(trig.icd_stay_active_ms, 30000)

    def test_missing_sections(self):
        with self.assertRaises(ValueError):
            planmod.load_plan(_write_plan(self.dir, '[plan]\nname="t"\n'))
        with self.assertRaises(ValueError):
            planmod.load_plan(_write_plan(self.dir, 'x = 1\n'))


class ValidateTest(unittest.TestCase):
    def _plan(self, **step):
        step.setdefault("scenario", "reset_only")
        step.setdefault("duration_s", 30)
        return planmod.Plan(name="t", steps=[planmod.PlanStep(**step)])

    def test_unknown_scenario(self):
        errs = planmod.validate_plan(self._plan(scenario="nie_ma"),
                                     MANIFEST)
        self.assertTrue(any("nieznany scenariusz" in e for e in errs))

    def test_bad_sample_rate(self):
        errs = planmod.validate_plan(self._plan(sample_rate=500), MANIFEST)
        self.assertTrue(any("sample_rate" in e for e in errs))
        # Dozwolona wartość przechodzi.
        self.assertEqual(
            planmod.validate_plan(self._plan(sample_rate=1000), MANIFEST), [])

    def test_serial_trigger_needs_port_and_pattern(self):
        # trigger serial bez portu i bez wzorca -> dwa błędy
        step = planmod.PlanStep(
            scenario="reset_only", duration_s=30,
            trigger=planmod.Trigger(type="serial", pattern=""))
        errs = planmod.validate_plan(planmod.Plan(name="t", steps=[step]),
                                     MANIFEST)
        self.assertTrue(any("monitor_port" in e for e in errs))
        self.assertTrue(any("pattern" in e for e in errs))
        # z portem i fragmentem przechodzi
        ok = planmod.PlanStep(
            scenario="reset_only", duration_s=30,
            monitor_port="/dev/ttyACM0",
            trigger=planmod.Trigger(type="serial", pattern="Friendship"))
        self.assertEqual(
            planmod.validate_plan(planmod.Plan(name="t", steps=[ok]),
                                  MANIFEST), [])

    def test_chip_trigger_requires_fields(self):
        # bez node_id/dataset/discriminator -> trzy błędy
        step = planmod.PlanStep(
            scenario="reset_only", duration_s=30,
            trigger=planmod.Trigger(type="chip"))
        errs = planmod.validate_plan(planmod.Plan(name="t", steps=[step]),
                                     MANIFEST)
        self.assertTrue(any("node_id" in e for e in errs))
        self.assertTrue(any("dataset" in e for e in errs))
        self.assertTrue(any("discriminator" in e for e in errs))
        # komplet do parowania przechodzi
        ok = planmod.PlanStep(
            scenario="reset_only", duration_s=30,
            trigger=planmod.Trigger(type="chip", node_id="5",
                                    dataset="0e08aa", discriminator="3840"))
        self.assertEqual(
            planmod.validate_plan(planmod.Plan(name="t", steps=[ok]),
                                  MANIFEST), [])
        # skip_pairing zdejmuje wymóg dataset/discriminator (zostaje node_id)
        skip = planmod.PlanStep(
            scenario="reset_only", duration_s=30,
            trigger=planmod.Trigger(type="chip", node_id="5",
                                    skip_pairing=True))
        self.assertEqual(
            planmod.validate_plan(planmod.Plan(name="t", steps=[skip]),
                                  MANIFEST), [])
        # zły regex match -> błąd
        badre = planmod.PlanStep(
            scenario="reset_only", duration_s=30,
            trigger=planmod.Trigger(type="chip", node_id="5",
                                    skip_pairing=True, match="[unclosed"))
        errs = planmod.validate_plan(planmod.Plan(name="t", steps=[badre]),
                                     MANIFEST)
        self.assertTrue(any("match" in e for e in errs))

    def test_icd_registration_excludes_skip_pairing(self):
        # Rejestracja ICD idzie wyłącznie w commissioningu, więc razem
        # ze skip_pairing byłaby cicho zignorowana -> błąd, nie milczenie.
        step = planmod.PlanStep(
            scenario="reset_only", duration_s=30,
            trigger=planmod.Trigger(type="chip", node_id="5",
                                    skip_pairing=True,
                                    icd_registration=True))
        errs = planmod.validate_plan(planmod.Plan(name="t", steps=[step]),
                                     MANIFEST)
        self.assertTrue(any("icd_registration" in e for e in errs))
        # Sama rejestracja przy normalnym parowaniu przechodzi.
        ok = planmod.PlanStep(
            scenario="reset_only", duration_s=30,
            trigger=planmod.Trigger(type="chip", node_id="5",
                                    dataset="0e08aa", discriminator="3840",
                                    icd_registration=True))
        self.assertEqual(
            planmod.validate_plan(planmod.Plan(name="t", steps=[ok]),
                                  MANIFEST), [])

    def test_icd_stay_active_must_be_positive(self):
        step = planmod.PlanStep(
            scenario="reset_only", duration_s=30,
            trigger=planmod.Trigger(type="chip", node_id="5",
                                    dataset="0e08aa", discriminator="3840",
                                    icd_stay_active_ms=0))
        errs = planmod.validate_plan(planmod.Plan(name="t", steps=[step]),
                                     MANIFEST)
        self.assertTrue(any("icd_stay_active_ms" in e for e in errs))

    def test_rtt_trigger_needs_rtt_on(self):
        step = planmod.PlanStep(
            scenario="reset_only", duration_s=30,
            trigger=planmod.Trigger(type="rtt", pattern="X"), rtt="off")
        errs = planmod.validate_plan(planmod.Plan(name="t", steps=[step]),
                                     MANIFEST)
        self.assertTrue(any("rtt = 'trigger'" in e for e in errs))

    def test_labels_need_continuous(self):
        step = planmod.PlanStep(
            scenario="reset_only", duration_s=30, rtt="trigger",
            trigger=planmod.Trigger(type="rtt", pattern="X"),
            labels=[planmod.LabelRule(pattern="P")])
        errs = planmod.validate_plan(planmod.Plan(name="t", steps=[step]),
                                     MANIFEST)
        self.assertTrue(any("continuous" in e for e in errs))

    def test_build_override_conflict(self):
        step = planmod.PlanStep(
            scenario="reset_only", duration_s=30,
            build_cmd="west build", build_extra_args=["-DX=y"])
        errs = planmod.validate_plan(planmod.Plan(name="t", steps=[step]),
                                     MANIFEST)
        self.assertTrue(any("wykluczają się" in e for e in errs))

    def test_hex_no_build(self):
        step = planmod.PlanStep(scenario="gotowy", duration_s=30,
                                build_extra_args=["-DX=y"])
        errs = planmod.validate_plan(planmod.Plan(name="t", steps=[step]),
                                     MANIFEST)
        self.assertTrue(any("hex" in e for e in errs))

    def test_bad_regex(self):
        step = planmod.PlanStep(
            scenario="reset_only", duration_s=30, rtt="continuous",
            trigger=planmod.Trigger(type="rtt", pattern="[unclosed"))
        errs = planmod.validate_plan(planmod.Plan(name="t", steps=[step]),
                                     MANIFEST)
        self.assertTrue(any("regex" in e for e in errs))

    def test_bad_board(self):
        errs = planmod.validate_plan(
            planmod.Plan(name="t", board="nie_ma",
                         steps=[planmod.PlanStep("reset_only", 30)]),
            MANIFEST)
        self.assertTrue(any("profil" in e for e in errs))

    def test_voltage_out_of_range_rejected(self):
        for bad in ("5.0", "1.5", "3.7", "0"):
            errs = planmod.validate_plan(
                self._plan(voltage=bad), MANIFEST)
            self.assertTrue(any("napięcie" in e for e in errs),
                            f"powinien odrzucić {bad}")

    def test_voltage_in_range_ok(self):
        for good in ("1.8", "3.0", "3.6"):
            errs = planmod.validate_plan(
                self._plan(voltage=good), MANIFEST)
            self.assertEqual(errs, [], f"powinien przyjąć {good}")

    def test_voltage_from_defaults_validated(self):
        # Krok bez napięcia spada na wbudowany domyślny "3.0" V (w zakresie).
        errs = planmod.validate_plan(self._plan(), MANIFEST)
        self.assertFalse(any("napięcie" in e for e in errs))


class SweepTest(unittest.TestCase):
    def test_normalize_param(self):
        for raw in ("CONFIG_LPN_SENSOR_INTERVAL_S",
                    "LPN_SENSOR_INTERVAL_S",
                    "-DCONFIG_LPN_SENSOR_INTERVAL_S"):
            self.assertEqual(planmod.normalize_sweep_param(raw),
                             "CONFIG_LPN_SENSOR_INTERVAL_S")

    def test_normalize_param_bad(self):
        for bad in ("", "  ", "123abc", "ma spacje", "CONFIG=x"):
            with self.assertRaises(ValueError):
                planmod.normalize_sweep_param(bad)

    def test_parse_values(self):
        self.assertEqual(planmod.parse_sweep_values("1, 2, 5"),
                         ["1", "2", "5"])
        self.assertEqual(planmod.parse_sweep_values("10 20  30"),
                         ["10", "20", "30"])
        # duplikaty zdjęte, kolejność pierwszych wystąpień zachowana
        self.assertEqual(planmod.parse_sweep_values("5, 1, 5, 2"),
                         ["5", "1", "2"])
        self.assertEqual(planmod.parse_sweep_values([1, 2, 3]),
                         ["1", "2", "3"])

    def test_parse_values_empty(self):
        for bad in ("", "  ", ",,", []):
            with self.assertRaises(ValueError):
                planmod.parse_sweep_values(bad)

    def test_expand_sweep(self):
        base = dict(scenario="app", duration_s=600,
                    build_extra_args=["-DCONFIG_LOG=n"])
        steps = planmod.expand_sweep(
            2, [("CONFIG_LPN_SENSOR_INTERVAL_S", "1, 5, 10")], base)
        self.assertEqual([s.label for s in steps], ["2.1", "2.2", "2.3"])
        self.assertTrue(all(s.scenario == "app" for s in steps))
        self.assertTrue(all(s.duration_s == 600 for s in steps))
        self.assertEqual([s.sweep for s in steps],
                         [[("CONFIG_LPN_SENSOR_INTERVAL_S", v)]
                          for v in ("1", "5", "10")])
        # Flaga serii doklejona ZA istniejącymi build_extra_args bazy.
        self.assertEqual(steps[1].build_extra_args,
                         ["-DCONFIG_LOG=n",
                          "-DCONFIG_LPN_SENSOR_INTERVAL_S=5"])

    def test_expand_sweep_dwie_osie_daje_iloczyn(self):
        # Dwie osie -> iloczyn kartezjański, PIERWSZA oś zmienia się
        # najwolniej: 10/100, 10/200, 20/100, … (kolejność z issue #31).
        steps = planmod.expand_sweep(
            1, [("CONFIG_P1", "10, 20, 30"), ("CONFIG_P2", "100, 200")],
            dict(scenario="app", duration_s=60))
        self.assertEqual([s.sweep for s in steps], [
            [("CONFIG_P1", "10"), ("CONFIG_P2", "100")],
            [("CONFIG_P1", "10"), ("CONFIG_P2", "200")],
            [("CONFIG_P1", "20"), ("CONFIG_P2", "100")],
            [("CONFIG_P1", "20"), ("CONFIG_P2", "200")],
            [("CONFIG_P1", "30"), ("CONFIG_P2", "100")],
            [("CONFIG_P1", "30"), ("CONFIG_P2", "200")]])
        # Numeracja płaska przez wszystkie kombinacje, nie siatka N.M.K.
        self.assertEqual([s.label for s in steps],
                         ["1.1", "1.2", "1.3", "1.4", "1.5", "1.6"])
        # Każdy krok dostaje po jednej fladze na oś.
        self.assertEqual(steps[3].build_extra_args,
                         ["-DCONFIG_P1=20", "-DCONFIG_P2=200"])

    def test_expand_sweep_validates(self):
        base = dict(scenario="app", duration_s=1)
        with self.assertRaises(ValueError):
            planmod.expand_sweep(1, [("zły param", "1")], base)
        with self.assertRaises(ValueError):
            planmod.expand_sweep(1, [("CONFIG_X", "")], base)
        with self.assertRaises(ValueError):
            planmod.expand_sweep(1, [], base)          # seria bez parametru
        # Ten sam symbol na obu osiach: dwie sprzeczne flagi w jednej
        # komendzie builda, więc połowa kroków mierzyłaby to samo.
        with self.assertRaises(ValueError):
            planmod.expand_sweep(
                1, [("CONFIG_X", "1, 2"), ("-DCONFIG_X", "3")], base)

    def test_sweep_flag_value_przelicza_jednostke_symbolu(self):
        # Symbole z SWEEP_UNITS podajemy w jednostce karty, a flaga dostaje
        # jednostkę Kconfiga: poll interval 200 s -> =2000 (100 ms).
        # Przelicznik należy do SYMBOLU, więc pozostałe idą bez zmian.
        self.assertEqual(
            planmod.sweep_flag_value("CONFIG_BT_MESH_LPN_POLL_TIMEOUT", "200"),
            "2000")
        self.assertEqual(
            planmod.sweep_flag_value("CONFIG_LPN_SENSOR_INTERVAL_S", "200"),
            "200")
        self.assertEqual(
            planmod.sweep_flag_value("CONFIG_BT_MESH_LPN_RETRY_TIMEOUT", "8"),
            "8")
        # Krańce zakresu Kconfiga (10..244735 jednostek) przechodzą.
        for secs, flag in (("1", "10"), ("24473.5", "244735")):
            self.assertEqual(
                planmod.sweep_flag_value(
                    "CONFIG_BT_MESH_LPN_POLL_TIMEOUT", secs), flag)
        # Poza zakresem, nie-liczba i wartość nie dająca całości jednostek.
        for bad in ("0.9", "24474", "0", "-5", "abc", "0.55"):
            with self.assertRaises(ValueError, msg=bad):
                planmod.sweep_flag_value(
                    "CONFIG_BT_MESH_LPN_POLL_TIMEOUT", bad)

    def test_expand_sweep_przelicza_flage_a_dziennik_trzyma_wpisane(self):
        # step.sweep (kolumny parametr/wartosc) trzyma wartość WPISANĄ,
        # a build_extra_args przeliczoną – inaczej na osi X wykresu byłyby
        # jednostki 100 ms zamiast sekund.
        base = dict(scenario="app", duration_s=60)
        steps = planmod.expand_sweep(
            2, [("BT_MESH_LPN_POLL_TIMEOUT", "120, 200")], base)
        self.assertEqual([s.sweep for s in steps],
                         [[("CONFIG_BT_MESH_LPN_POLL_TIMEOUT", "120")],
                          [("CONFIG_BT_MESH_LPN_POLL_TIMEOUT", "200")]])
        self.assertEqual([s.build_extra_args for s in steps],
                         [["-DCONFIG_BT_MESH_LPN_POLL_TIMEOUT=1200"],
                          ["-DCONFIG_BT_MESH_LPN_POLL_TIMEOUT=2000"]])
        # Wartość poza zakresem Kconfiga zatrzymuje plan przed startem.
        with self.assertRaises(ValueError):
            planmod.expand_sweep(
                2, [("CONFIG_BT_MESH_LPN_POLL_TIMEOUT", "60, 30000")], base)

    def test_expanded_steps_validate_against_manifest(self):
        # Kroki z ekspansji są zwykłymi PlanStep – przechodzą walidację
        # planu tak jak ręczne kroki z build_extra_args.
        base = dict(scenario="app", duration_s=60)
        steps = planmod.expand_sweep(
            1, [("CONFIG_LPN_SENSOR_INTERVAL_S", "1, 2")], base)
        plan = planmod.Plan(name="t", board="btz", steps=steps)
        self.assertEqual(planmod.validate_plan(plan, MANIFEST), [])

    def test_expand_repeats(self):
        # Krotność karty ('x3'): ten sam krok trzy razy, każdy z własną
        # etykietą 'N/k'. Ukośnik, nie kropka – kropka jest zajęta przez
        # serię, więc po etykiecie widać, co jest czym.
        base = planmod.PlanStep(scenario="app", duration_s=600, label="2",
                                build_extra_args=["-DCONFIG_LOG=n"])
        steps = planmod.expand_repeats([base], 3)
        self.assertEqual([s.label for s in steps], ["2/1", "2/2", "2/3"])
        # Powtórka jest KOPIĄ: cała reszta pól bez zmian, także flagi builda
        # (ten sam obraz -> silnik buduje raz, ale flashuje przed każdym).
        self.assertTrue(all(s.scenario == "app" for s in steps))
        self.assertTrue(all(s.duration_s == 600 for s in steps))
        self.assertTrue(all(s.build_extra_args == ["-DCONFIG_LOG=n"]
                            for s in steps))
        self.assertIsNot(steps[0], steps[1])
        # x1 (i mniej) zostawia krok w spokoju – bez sufiksu '/1', żeby
        # zwykły pomiar wyglądał w raporcie jak dotąd.
        for count in (1, 0, -2):
            self.assertEqual([s.label
                              for s in planmod.expand_repeats([base], count)],
                             ["2"])
        # Powyżej limitu UI obcinamy do REPEAT_MAX.
        self.assertEqual(len(planmod.expand_repeats([base], 99)),
                         planmod.REPEAT_MAX)

    def test_expand_repeats_po_serii_klei_powtorki_obok_siebie(self):
        # Seria ×2 z krotnością x3 daje A A A B B B, nie A B A B A B:
        # powtórki jednego ustawienia mierzą się w najbliższych sobie
        # warunkach, więc widoczny rozrzut jest rozrzutem POMIARU.
        steps = planmod.expand_repeats(
            planmod.expand_sweep(3, [("CONFIG_P", "10, 20")],
                                 dict(scenario="app", duration_s=60)), 3)
        self.assertEqual([s.label for s in steps],
                         ["3.1/1", "3.1/2", "3.1/3",
                          "3.2/1", "3.2/2", "3.2/3"])
        self.assertEqual([s.sweep[0][1] for s in steps],
                         ["10", "10", "10", "20", "20", "20"])

    def test_expand_repeats_bez_etykiety_numeruje_od_pozycji(self):
        # Kroki spoza kreatora (plan z TOML) etykiety nie mają – powtórka
        # nie może wyjść jako '/2', bo taki wiersz nic nie mówi.
        steps = planmod.expand_repeats(
            [planmod.PlanStep(scenario="app", duration_s=1),
             planmod.PlanStep(scenario="app", duration_s=1)], 2)
        self.assertEqual([s.label for s in steps],
                         ["1/1", "1/2", "2/1", "2/2"])

    def test_repeated_steps_validate_against_manifest(self):
        # Powtórki to zwykłe PlanStep – przechodzą walidację planu.
        steps = planmod.expand_repeats(
            [planmod.PlanStep(scenario="app", duration_s=60, label="1")], 3)
        plan = planmod.Plan(name="t", board="btz", steps=steps)
        self.assertEqual(planmod.validate_plan(plan, MANIFEST), [])

    def test_sweep_on_hex_scenario_rejected(self):
        # Sweep (build_extra_args) na scenariuszu 'hex' -> błąd walidacji,
        # z etykietą kroku "N.M".
        base = dict(scenario="gotowy", duration_s=60)
        steps = planmod.expand_sweep(3, [("CONFIG_X", "1, 2")], base)
        errs = planmod.validate_plan(
            planmod.Plan(name="t", board="btz", steps=steps), MANIFEST)
        self.assertTrue(any("hex" in e for e in errs))
        self.assertTrue(any("3.1" in e or "3.2" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
