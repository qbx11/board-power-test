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
            2, "CONFIG_LPN_SENSOR_INTERVAL_S", "1, 5, 10", base)
        self.assertEqual([s.label for s in steps], ["2.1", "2.2", "2.3"])
        self.assertTrue(all(s.scenario == "app" for s in steps))
        self.assertTrue(all(s.duration_s == 600 for s in steps))
        self.assertTrue(all(
            s.sweep_param == "CONFIG_LPN_SENSOR_INTERVAL_S" for s in steps))
        self.assertEqual([s.sweep_value for s in steps], ["1", "5", "10"])
        # Flaga serii doklejona ZA istniejącymi build_extra_args bazy.
        self.assertEqual(steps[1].build_extra_args,
                         ["-DCONFIG_LOG=n",
                          "-DCONFIG_LPN_SENSOR_INTERVAL_S=5"])

    def test_expand_sweep_validates(self):
        with self.assertRaises(ValueError):
            planmod.expand_sweep(1, "zły param", "1", dict(scenario="app",
                                                           duration_s=1))
        with self.assertRaises(ValueError):
            planmod.expand_sweep(1, "CONFIG_X", "", dict(scenario="app",
                                                         duration_s=1))

    def test_expanded_steps_validate_against_manifest(self):
        # Kroki z ekspansji są zwykłymi PlanStep – przechodzą walidację
        # planu tak jak ręczne kroki z build_extra_args.
        base = dict(scenario="app", duration_s=60)
        steps = planmod.expand_sweep(1, "CONFIG_LPN_SENSOR_INTERVAL_S",
                                     "1, 2", base)
        plan = planmod.Plan(name="t", board="btz", steps=steps)
        self.assertEqual(planmod.validate_plan(plan, MANIFEST), [])

    def test_sweep_on_hex_scenario_rejected(self):
        # Sweep (build_extra_args) na scenariuszu 'hex' -> błąd walidacji,
        # z etykietą kroku "N.M".
        base = dict(scenario="gotowy", duration_s=60)
        steps = planmod.expand_sweep(3, "CONFIG_X", "1, 2", base)
        errs = planmod.validate_plan(
            planmod.Plan(name="t", board="btz", steps=steps), MANIFEST)
        self.assertTrue(any("hex" in e for e in errs))
        self.assertTrue(any("3.1" in e or "3.2" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
