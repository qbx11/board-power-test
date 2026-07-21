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


def run_args(scenarios, dry_run=True, sample=None, no_erase=False,
             pristine=False):
    return argparse.Namespace(scenarios=scenarios, all=False, profile=None,
                              sample=sample, no_erase=no_erase,
                              dry_run=dry_run, pristine=pristine)


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
            "zrodlowy", self.scenarios["zrodlowy"], "btz", self.profile, "btz")
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
            "zwykly", self.scenarios["zwykly"], "btz", self.profile, "btz")
        self.assertEqual(build_dir, "build_zwykly")
        self.assertEqual(cmd[cmd.index("--") - 1], str(self.env.repo))
        self.assertIn("-DCONFIG_SLEEP_SYSTEM_OFF_RESET_ONLY=y", cmd)

    def test_build_cmd_domyslnie_pristine_auto(self):
        # Domyślnie build przyrostowy: -p auto (ninja buduje tylko zmiany).
        cmd, _ = core.make_build_cmd(
            "zwykly", self.scenarios["zwykly"], "btz", self.profile, "btz")
        self.assertEqual(cmd[cmd.index("-p") + 1], "auto")

    def test_build_cmd_pristine_wymuszony(self):
        cmd, _ = core.make_build_cmd(
            "zwykly", self.scenarios["zwykly"], "btz", self.profile, "btz",
            pristine="always")
        self.assertEqual(cmd[cmd.index("-p") + 1], "always")

    def test_build_cmd_profil_nienbedomyslny_ma_prefiks(self):
        # Profil inny niż domyślny buduje do build_<profil>_<scenariusz>/,
        # żeby obrazy różnych płytek się nie nadpisywały.
        _, build_dir = core.make_build_cmd(
            "zwykly", self.scenarios["zwykly"], "dk",
            self.manifest["boards"]["dk"], "btz")
        self.assertEqual(build_dir, "build_dk_zwykly")

    def test_parse_current_jednostka_domyslna(self):
        # Bez sufiksu liczy wg jednostki domyślnej; jawny sufiks wygrywa.
        self.assertEqual(core.parse_current("2.5", default_unit="mA"), 2500.0)
        self.assertEqual(core.parse_current("0,95", default_unit="uA"), 0.95)
        self.assertEqual(core.parse_current("2.5 mA", default_unit="uA"), 2500.0)

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


class AddScenarioTests(unittest.TestCase):
    """`add_scenario` – dodawanie cudzego kodu jedną ścieżką."""

    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)

    def test_dodanie_hex_z_pliku(self):
        name, entry = core.add_scenario("gotowe/firmware.hex", base=core.ROOT)
        self.assertEqual(name, "firmware")
        self.assertEqual(entry["hex"], "gotowe/firmware.hex")  # względna
        # wpis naprawdę jest w manifeście, parsuje się i przechodzi walidację
        scenarios = core.load_manifest()["scenarios"]
        self.assertEqual(scenarios["firmware"]["hex"], "gotowe/firmware.hex")
        self.assertEqual(core.validate_scenarios(["firmware"], scenarios), [])

    def test_dodanie_source_z_katalogu(self):
        name, entry = core.add_scenario(str(self.env.source_dir))
        self.assertEqual(name, "app_zespolu")
        self.assertEqual(entry["source"], "app_zespolu")
        scenarios = core.load_manifest()["scenarios"]
        self.assertEqual(core.validate_scenarios([name], scenarios), [])

    def test_label_i_opis_z_polskimi_znakami_i_cudzyslowem(self):
        label = 'Moja "apka" — żółć'
        name, _ = core.add_scenario("app_zespolu", label=label,
                                    description="opis z ą i \"cytatem\"",
                                    base=core.ROOT)
        scen = core.load_manifest()["scenarios"][name]
        self.assertEqual(scen["label"], label)
        self.assertEqual(scen["description"], 'opis z ą i "cytatem"')

    def test_auto_numerowanie_przy_powtorce(self):
        first, _ = core.add_scenario("gotowe/firmware.hex", base=core.ROOT)
        second, _ = core.add_scenario("gotowe/firmware.hex", base=core.ROOT)
        self.assertEqual((first, second), ("firmware", "firmware_2"))

    def test_bledy_wykrywania(self):
        (self.env.repo / "notatki.txt").write_text("x")
        (self.env.repo / "pusty_katalog").mkdir()
        for path, blad in (("nie_ma_takiego", "nie istnieje"),
                           ("notatki.txt", "nie .hex"),
                           ("pusty_katalog", "CMakeLists.txt")):
            with self.assertRaises(ValueError) as ctx:
                core.add_scenario(path, base=core.ROOT)
            self.assertIn(blad, str(ctx.exception))

    def test_jawna_nazwa_zajeta_lub_zla(self):
        with self.assertRaises(ValueError) as ctx:
            core.add_scenario("app_zespolu", name="zwykly", base=core.ROOT)
        self.assertIn("już istnieje", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            core.add_scenario("app_zespolu", name="1zly", base=core.ROOT)
        self.assertIn("nie może zaczynać się cyfrą", str(ctx.exception))

    def test_cli_add(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            core.cmd_add(argparse.Namespace(path=str(self.env.hex_path),
                                            name=None, label=None, desc=None))
        self.assertIn("Dodano scenariusz 'firmware'", out.getvalue())
        self.assertIn("firmware", core.load_manifest()["scenarios"])


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

    def test_dry_run_pristine_flaga(self):
        # Domyślnie -p auto; --pristine przełącza na -p always.
        out = self.run_cli(run_args(["zwykly"]))
        self.assertIn("-p auto", out)
        self.assertNotIn("-p always", out)
        out = self.run_cli(run_args(["zwykly"], pristine=True))
        self.assertIn("-p always", out)

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
