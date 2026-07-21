# ============================================================
#  Testy rdzenia (power_test.py): komendy, walidacja, CSV, CLI
# ============================================================
#   .venv/bin/python -m unittest discover -s tools/power-test/tests -v

import argparse
import contextlib
import csv
import io
import unittest
from unittest.mock import patch

from common import FakeEnv, core


def run_args(scenarios, dry_run=True, sample=None, no_erase=False):
    return argparse.Namespace(scenarios=scenarios, all=False, profile=None,
                              sample=sample, no_erase=no_erase,
                              dry_run=dry_run)


class CoreTests(unittest.TestCase):

    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)
        self.manifest = core.load_manifest()
        self.scenarios = self.manifest["scenarios"]
        self.profile = self.manifest["boards"]["btz"]

    # --- komendy build/flash ---

    def test_build_cmd_source_buduje_z_katalogu_zespolu(self):
        cmd, build_dir = core.make_build_cmd(
            "zrodlowy", self.scenarios["zrodlowy"], "btz", self.profile)
        self.assertEqual(build_dir, "build_zrodlowy")
        # źródłem jest katalog z manifestu, nie repo narzędzia
        self.assertIn(str(self.env.source_dir), cmd)
        self.assertNotIn(str(self.env.repo), cmd[cmd.index("-d") + 2:])
        # katalog builda nadal w repo narzędzia
        self.assertEqual(cmd[cmd.index("-d") + 1],
                         str(self.env.repo / "build_zrodlowy"))
        # cmake_args przekazane po "--"
        self.assertIn("-DEXTRA_CONF_FILE=low_power.conf",
                      cmd[cmd.index("--"):])

    def test_build_cmd_zwykly_regresja(self):
        cmd, build_dir = core.make_build_cmd(
            "zwykly", self.scenarios["zwykly"], "btz", self.profile)
        self.assertEqual(build_dir, "build_zwykly")
        self.assertEqual(cmd[cmd.index("--") - 1], str(self.env.repo))
        self.assertIn("-DCONFIG_SLEEP_SYSTEM_OFF_RESET_ONLY=y", cmd)

    def test_flash_cmd_hex_nrfutil(self):
        cmd = core.flash_cmd_for(self.scenarios["hexowy"], None,
                                 self.profile)
        self.assertEqual(cmd, ["nrfutil", "device", "program", "--firmware",
                               str(self.env.hex_path),
                               "--options", "chip_erase_mode=ERASE_ALL"])

    def test_flash_cmd_hex_no_erase(self):
        cmd = core.flash_cmd_for(self.scenarios["hexowy"], None,
                                 self.profile, erase=False)
        self.assertNotIn("--options", cmd)

    def test_flash_cmd_zwykly_regresja(self):
        cmd = core.flash_cmd_for(self.scenarios["zwykly"], "build_zwykly",
                                 self.profile)
        self.assertEqual(cmd, ["west", "flash", "-d",
                               str(self.env.repo / "build_zwykly"),
                               "-r", "jlink", "--erase"])

    # --- walidacja manifestu ---

    def test_walidacja_source_i_hex_naraz(self):
        errors = core.validate_scenarios(["zly_oba"], self.scenarios)
        self.assertEqual(len(errors), 1)
        self.assertIn("wykluczają", errors[0])

    def test_walidacja_brak_pliku_hex(self):
        errors = core.validate_scenarios(["zly_brak_pliku"], self.scenarios)
        self.assertEqual(len(errors), 1)
        self.assertIn("nie istnieje", errors[0])

    def test_walidacja_hex_z_cmake_args(self):
        scen = {"hex": "gotowe/firmware.hex", "cmake_args": ["-DX=y"]}
        errors = core.validate_scenarios(["s"], {"s": scen})
        self.assertTrue(any("cmake_args" in e for e in errors))

    def test_walidacja_brak_katalogu_source(self):
        scen = {"source": "nie_ma_takiego"}
        errors = core.validate_scenarios(["s"], {"s": scen})
        self.assertEqual(len(errors), 1)
        self.assertIn("katalog źródeł", errors[0])

    def test_walidacja_poprawnych_wpisow(self):
        errors = core.validate_scenarios(["zwykly", "zrodlowy", "hexowy"],
                                         self.scenarios)
        self.assertEqual(errors, [])

    # --- kolumna "flagi" w CSV ---

    def test_flagi_csv(self):
        self.assertEqual(core.scenario_flags(self.scenarios["zwykly"]),
                         "-DCONFIG_SLEEP_SYSTEM_OFF_RESET_ONLY=y")
        self.assertEqual(core.scenario_flags(self.scenarios["zrodlowy"]),
                         "-DEXTRA_CONF_FILE=low_power.conf "
                         "source=app_zespolu")
        self.assertEqual(core.scenario_flags(self.scenarios["hexowy"]),
                         "hex=gotowe/firmware.hex")

    def test_make_row_nazwy_kolumn_bez_zmian(self):
        row = core.make_row("hexowy", self.scenarios["hexowy"], self.profile,
                            "TEST #1", "3.0", 1.0, "")
        self.assertEqual(list(row), core.CSV_FIELDS)
        self.assertEqual(row["flagi"], "hex=gotowe/firmware.hex")


class CliTests(unittest.TestCase):

    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)

    def run_cli(self, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            core.cmd_run(args)
        return out.getvalue()

    def test_dry_run_wszystkie_warianty(self):
        out = self.run_cli(run_args(["zwykly", "zrodlowy", "hexowy"]))
        # FAZA 1: budują się tylko 2 z 3 scenariuszy
        self.assertIn("budowanie 2 obraz(ów)", out)
        self.assertIn("gotowy hex (gotowe/firmware.hex) – bez budowania", out)
        self.assertEqual(out.count("west build"), 2)
        self.assertIn(str(self.env.source_dir), out)
        # FAZA 2: hex flashowany przez nrfutil, reszta przez west flash
        self.assertIn("nrfutil device program --firmware "
                      f"{self.env.hex_path} --options "
                      "chip_erase_mode=ERASE_ALL", out)
        self.assertEqual(out.count("west flash"), 2)
        # dry-run: nic nie zostało faktycznie wykonane
        self.assertEqual(self.env.commands(), [])

    def test_dry_run_tylko_hex_bez_budowania(self):
        out = self.run_cli(run_args(["hexowy"]))
        self.assertIn("budowanie 0 obraz(ów)", out)
        self.assertIn("nic do budowania", out)
        self.assertNotIn("west build", out)

    def test_dry_run_no_erase_dla_hex(self):
        out = self.run_cli(run_args(["hexowy"], no_erase=True))
        self.assertIn("nrfutil device program", out)
        self.assertNotIn("chip_erase_mode", out)

    def test_walidacja_przed_faza_1_source_i_hex(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(run_args(["zly_oba"]))
        self.assertIn("wykluczają", str(ctx.exception.code))

    def test_walidacja_przed_faza_1_brak_pliku(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(run_args(["zwykly", "zly_brak_pliku"]))
        self.assertIn("nie istnieje", str(ctx.exception.code))

    def test_pelny_przebieg_source_zapisuje_csv(self):
        # Nie-dry-run: fałszywy west naprawdę jest wołany, a odpowiedzi
        # użytkownika (flash OK / SWD odłączone / wynik / uwagi) podajemy
        # przez zmockowane input().
        answers = iter(["", "tak", "2.5 mA", "po poprawce"])
        with patch("builtins.input", lambda prompt="": next(answers)):
            out = self.run_cli(run_args(["zrodlowy"], dry_run=False,
                                        sample="TEST #1"))
        cmds = self.env.commands()
        builds = [c for c in cmds if c.startswith("west build")]
        self.assertEqual(len(builds), 1)
        self.assertIn(str(self.env.source_dir), builds[0])
        self.assertIn("build out-of-tree", out)  # fałszywy SDK z HOME
        self.assertTrue(any(c.startswith("west flash") for c in cmds))
        with open(core.CSV_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[-1]["flagi"],
                         "-DEXTRA_CONF_FILE=low_power.conf source=app_zespolu")
        self.assertEqual(rows[-1]["prad_uA"], "2500.0")


if __name__ == "__main__":
    unittest.main()
