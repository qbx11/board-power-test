# ============================================================
#  Testy rdzenia (power_test.py): komendy, walidacja, CSV, CLI
# ============================================================
#   .venv/bin/python -m unittest discover -s tools/power-test/tests -v

import argparse
import contextlib
import csv
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import FakeEnv, core


def run_args(scenarios, dry_run=True, sample=None, no_erase=False,
             no_reset=False, no_swd_reminder=False, pristine=False):
    return argparse.Namespace(scenarios=scenarios, all=False, profile=None,
                              sample=sample, no_erase=no_erase,
                              no_reset=no_reset,
                              no_swd_reminder=no_swd_reminder, dry_run=dry_run,
                              pristine=pristine)


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

    def test_parse_memory_report_do_markdown(self):
        out = ("-- west build output --\n"
               "Memory region         Used Size  Region Size  %age Used\n"
               "           FLASH:      118436 B      1536 KB      7.53%\n"
               "             RAM:       25696 B       188 KB     13.35%\n"
               "        IDT_LIST:          0 GB        32 KB      0.00%\n"
               "Generating files...\n")
        md = core.parse_memory_report(out)
        self.assertEqual(md.splitlines()[0],
                         "| Memory region | Used Size | Region Size | "
                         "%age Used |")
        self.assertEqual(md.splitlines()[1], "| --- | --- | --- | --- |")
        self.assertIn("| FLASH | 118436 B | 1536 KB | 7.53% |", md)
        self.assertIn("| IDT_LIST | 0 GB | 32 KB | 0.00% |", md)

    def test_parse_memory_report_brak_tabeli(self):
        # Wyjście flasha (bez sekcji Memory region) -> None.
        self.assertIsNone(core.parse_memory_report(
            "flashing...\nApplication programmed\nDone\n"))

    def test_memory_usage_liczby_do_dziennika(self):
        out = ("Memory region         Used Size  Region Size  %age Used\n"
               "           FLASH:      118436 B      1536 KB      7.53%\n"
               "             RAM:       25696 B       188 KB     13.35%\n"
               "        IDT_LIST:          0 GB        32 KB      0.00%\n")
        # IDT_LIST celowo pomijamy – to nie jest pamięć, o którą ktoś pyta.
        self.assertEqual(core.memory_usage(out),
                         {"flash_B": 118436, "flash_pct": 7.53,
                          "ram_B": 25696, "ram_pct": 13.35})
        self.assertIsNone(core.memory_usage("nic tu nie ma\n"))

    def test_memory_usage_jednostki_1024(self):
        # Zephyr potrafi podać użycie w KB/MB – w dzienniku trzymamy bajty,
        # żeby dało się porównywać obrazy między sobą.
        out = ("Memory region         Used Size  Region Size  %age Used\n"
               "           FLASH:        1536 KB      2 MB     75.00%\n"
               "             RAM:           1 MB      2 MB     50.00%\n")
        self.assertEqual(core.memory_usage(out),
                         {"flash_B": 1536 * 1024, "flash_pct": 75.0,
                          "ram_B": 1024 * 1024, "ram_pct": 50.0})

    def _sysbuild_output(self):
        # Tak wygląda build sysbuilda: każdy obraz zapowiedziany linią ninja
        # 'Performing build step for ...', każdy z własną tabelką.
        return ("[10/50] Performing build step for 'mcuboot'\n"
                "Memory region         Used Size  Region Size  %age Used\n"
                "           FLASH:       24576 B        48 KB     50.00%\n"
                "             RAM:        4096 B        16 KB     25.00%\n"
                "[50/50] Performing build step for 'board-power-test'\n"
                "Memory region         Used Size  Region Size  %age Used\n"
                "           FLASH:      118436 B      1536 KB      7.53%\n"
                "             RAM:       25696 B       188 KB     13.35%\n")

    def test_memory_usage_bierze_obraz_aplikacji_nie_bootloadera(self):
        # REGRESJA: przy MCUboot bootloader buduje się PIERWSZY, więc
        # branie pierwszej napotkanej tabelki dawało zajętość bootloadera
        # podpisaną jako zajętość aplikacji.
        out = self._sysbuild_output()
        self.assertEqual(core.memory_usage(out, image="board-power-test"),
                         {"flash_B": 118436, "flash_pct": 7.53,
                          "ram_B": 25696, "ram_pct": 13.35})
        self.assertEqual(core.memory_usage(out, image="mcuboot")["flash_B"],
                         24576)

    def test_memory_usage_kilka_obrazow_bez_wskazania(self):
        # Nie wiadomo który obraz -> nic nie zapisujemy. Lepiej puste
        # kolumny niż liczba przypisana nie temu obrazowi.
        self.assertIsNone(core.memory_usage(self._sysbuild_output()))
        # Za to na EKRANIE pokazujemy wszystkie tabelki, podpisane.
        md = core.parse_memory_report(self._sysbuild_output())
        self.assertIn("**mcuboot**", md)
        self.assertIn("**board-power-test**", md)
        self.assertIn("| FLASH | 118436 B | 1536 KB | 7.53% |", md)

    def test_record_i_load_memory(self):
        # Zapamiętane przy buildzie liczby czyta się potem z katalogu –
        # to jest ścieżka dla POMINIĘTEGO builda (linker nic nie drukuje,
        # gdy nie ma czego budować).
        build = core.ROOT / "build_test_mem"
        (build / "zephyr").mkdir(parents=True, exist_ok=True)
        out = ("Memory region         Used Size  Region Size  %age Used\n"
               "           FLASH:      118436 B      1536 KB      7.53%\n"
               "             RAM:       25696 B       188 KB     13.35%\n")
        self.assertEqual(core.record_memory("build_test_mem", out)["flash_B"],
                         118436)
        self.assertEqual(core.load_memory_usage("build_test_mem"),
                         {"flash_B": 118436, "flash_pct": 7.53,
                          "ram_B": 25696, "ram_pct": 13.35})

    def test_load_memory_bez_pliku_i_bez_katalogu(self):
        self.assertIsNone(core.load_memory_usage("build_nie_ma_takiego"))
        self.assertIsNone(core.load_memory_usage(None))

    def test_record_memory_wybiera_domene_z_domains_yaml(self):
        # Przy sysbuildzie nazwę obrazu aplikacji bierzemy z domains.yaml,
        # więc zapis nie wymaga podpowiedzi od wołającego.
        build = core.ROOT / "build_test_sysbuild"
        build.mkdir(parents=True, exist_ok=True)
        (build / "domains.yaml").write_text(
            "default: board-power-test\nbuild_dir: /x\n", encoding="utf-8")
        usage = core.record_memory("build_test_sysbuild",
                                   self._sysbuild_output())
        self.assertEqual(usage["flash_B"], 118436)     # aplikacja, nie MCUboot

    def test_make_row_dokleja_pamiec_z_katalogu_builda(self):
        build = core.ROOT / "build_test_row"
        build.mkdir(parents=True, exist_ok=True)
        core.record_memory("build_test_row",
                           "Memory region         Used Size  Region Size  "
                           "%age Used\n"
                           "           FLASH:      118436 B      1536 KB"
                           "      7.53%\n")
        scen = self.scenarios["zwykly"]
        row = core.make_row("zwykly", scen, self.profile, "BTZ #1", "3.0",
                            1.5, "", build_dir="build_test_row")
        self.assertEqual(row["flash_B"], 118436)
        # Bez katalogu builda (gotowy hex) kolumny zostają puste.
        row2 = core.make_row("zwykly", scen, self.profile, "BTZ #1", "3.0",
                             1.5, "")
        self.assertNotIn("flash_B", row2)

    def test_copy_to_clipboard_bez_narzedzia(self):
        # Gdy w PATH nie ma pbcopy/xclip/... -> None (wołający robi fallback).
        from unittest import mock
        with mock.patch("power_test.shutil.which", return_value=None):
            self.assertIsNone(core.copy_to_clipboard("cokolwiek"))

    def test_save_text_log_pisze_plik(self):
        rel = core.save_text_log("linia1\nlinia2", name="test-build.log")
        path = core.ROOT / rel
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text(encoding="utf-8"), "linia1\nlinia2")

    def test_flash_cmd_hex_nrfutil(self):
        # Domyślnie erase + reset: nrfutil dostaje oba w jednym --options.
        cmd = core.flash_cmd_for(self.scenarios["hexowy"], None,
                                 self.profile)
        self.assertEqual(cmd, ["nrfutil", "device", "program", "--firmware",
                               str(self.env.hex_path), "--options",
                               "chip_erase_mode=ERASE_ALL,reset=RESET_SYSTEM"])

    def test_flash_cmd_hex_no_erase(self):
        # Bez erase, ale reset domyślnie zostaje -> samo reset= w --options.
        cmd = core.flash_cmd_for(self.scenarios["hexowy"], None,
                                 self.profile, erase=False)
        self.assertNotIn("chip_erase_mode=ERASE_ALL", cmd)
        self.assertEqual(cmd[cmd.index("--options") + 1], "reset=RESET_SYSTEM")

    def test_flash_cmd_hex_no_erase_no_reset(self):
        # Oba wyłączone -> nrfutil bez --options w ogóle.
        cmd = core.flash_cmd_for(self.scenarios["hexowy"], None,
                                 self.profile, erase=False, reset=False)
        self.assertNotIn("--options", cmd)

    def test_flash_cmd_zwykly_regresja(self):
        # Domyślnie west flash z --erase i wymuszonym --reset.
        cmd = core.flash_cmd_for(self.scenarios["zwykly"], "build_zwykly",
                                 self.profile)
        self.assertEqual(cmd, ["west", "flash", "-d",
                               str(self.env.repo / "build_zwykly"),
                               "-r", "jlink", "--erase", "--reset"])

    def test_flash_cmd_zwykly_no_reset(self):
        # --no-reset (opcja odznaczona) usuwa tylko --reset, erase zostaje.
        cmd = core.flash_cmd_for(self.scenarios["zwykly"], "build_zwykly",
                                 self.profile, reset=False)
        self.assertNotIn("--reset", cmd)
        self.assertIn("--erase", cmd)

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
        # Pomiar ręczny wypełnia kolumny bazowe; kolumny trybu
        # autonomicznego (min/max, czas, sesja) dokłada silnik autorun.
        self.assertEqual(list(row), core.CSV_BASE_FIELDS)
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


class RemoveScenarioTests(unittest.TestCase):

    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)

    def test_usuwa_wpis_nie_ruszajac_reszty(self):
        before = set(core.load_manifest()["scenarios"])
        core.remove_scenario("zrodlowy")
        after = core.load_manifest()["scenarios"]   # plik dalej się parsuje
        self.assertEqual(set(after), before - {"zrodlowy"})
        self.assertNotIn("[scenarios.zrodlowy]",
                         core.MANIFEST_PATH.read_text())
        # sąsiednie wpisy nietknięte
        self.assertEqual(after["zwykly"]["cmake_args"],
                         ["-DCONFIG_SLEEP_SYSTEM_OFF_RESET_ONLY=y"])

    def test_usuwa_wpis_dodany_przez_add(self):
        name, _ = core.add_scenario("gotowe/firmware.hex", base=core.ROOT)
        core.remove_scenario(name)
        self.assertNotIn(name, core.load_manifest()["scenarios"])

    def test_blad_gdy_nie_istnieje(self):
        with self.assertRaises(ValueError) as ctx:
            core.remove_scenario("nie_ma")
        self.assertIn("nie istnieje", str(ctx.exception))

    def _write_local(self, text):
        local = core.MANIFEST_PATH.with_name(core.LOCAL_MANIFEST_NAME)
        local.write_text(text, encoding="utf-8")
        return local

    def test_usuwa_wpis_z_manifestu_lokalnego(self):
        # Scenariusz z prywatnego scenarios.local.toml też musi dać się
        # usunąć – inaczej „usunięty" wracałby przy następnym starcie.
        local = self._write_local('[scenarios.prywatny]\n'
                                  'hex = "gotowe/firmware.hex"\n')
        self.assertIn("prywatny", core.load_manifest()["scenarios"])
        core.remove_scenario("prywatny")
        self.assertNotIn("prywatny", core.load_manifest()["scenarios"])
        self.assertNotIn("[scenarios.prywatny]", local.read_text())

    def test_usuwa_wpis_nadpisany_lokalnie_z_obu_plikow(self):
        local = self._write_local('[scenarios.zwykly]\n'
                                  'label = "Nadpisany lokalnie"\n')
        core.remove_scenario("zwykly")
        self.assertNotIn("zwykly", core.load_manifest()["scenarios"])
        self.assertNotIn("[scenarios.zwykly]", local.read_text())
        self.assertNotIn("[scenarios.zwykly]",
                         core.MANIFEST_PATH.read_text())


class BuildSkipTests(unittest.TestCase):
    """Pomijanie budowania, gdy build_<scenariusz>/ ma gotowy obraz."""

    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)
        manifest = core.load_manifest()
        self.scen = manifest["scenarios"]["zwykly"]
        self.profile = manifest["boards"]["btz"]
        self.cmd, self.build_dir = core.make_build_cmd(
            "zwykly", self.scen, "btz", self.profile, "btz")

    def _przygotuj_gotowy_build(self):
        d = core.ROOT / self.build_dir / "zephyr"
        d.mkdir(parents=True)
        (d / "zephyr.hex").write_text(":00000001FF\n")
        core.record_build(self.build_dir, self.cmd)

    def test_swiezy_katalog_wymaga_builda(self):
        self.assertFalse(core.build_up_to_date(self.build_dir, self.cmd))

    def test_gotowy_build_jest_wykrywany(self):
        self._przygotuj_gotowy_build()
        self.assertTrue(core.build_up_to_date(self.build_dir, self.cmd))
        # tryb pristine (-p) nie wpływa na odcisk komendy
        cmd_pristine, _ = core.make_build_cmd(
            "zwykly", self.scen, "btz", self.profile, "btz",
            pristine="always")
        self.assertTrue(core.build_up_to_date(self.build_dir, cmd_pristine))

    def _przygotuj_gotowy_build_sysbuild(self, domena="board-power-test"):
        """Układ katalogów jak po `west build` z sysbuildem: <build>/zephyr/
        istnieje, ale obraz leży w <build>/<domena>/zephyr/."""
        root = core.ROOT / self.build_dir
        (root / "zephyr").mkdir(parents=True)     # pusty, jak u sysbuilda
        app = root / domena / "zephyr"
        app.mkdir(parents=True)
        (app / "zephyr.hex").write_text(":00000001FF\n")
        (root / "domains.yaml").write_text(
            f"default: {domena}\nbuild_dir: {root}\ndomains:\n"
            f"  - name: {domena}\n    build_dir: {root / domena}\n",
            encoding="utf-8")
        core.record_build(self.build_dir, self.cmd)

    def test_gotowy_build_sysbuilda_jest_wykrywany(self):
        # REGRESJA: sysbuild kładzie obraz w podkatalogu domeny, a
        # sprawdzanie tylko <build>/zephyr/zephyr.hex dawało "brak obrazu"
        # przy komplecie plików – każdy przebieg budował wszystko od nowa.
        self._przygotuj_gotowy_build_sysbuild()
        self.assertIsNotNone(core.built_hex(self.build_dir))
        self.assertTrue(core.build_up_to_date(self.build_dir, self.cmd))

    def test_sysbuild_bez_obrazu_wymaga_builda(self):
        # Sam domains.yaml nie wystarcza – bez hexa build musi ruszyć
        # (przerwany build zostawia metadane bez obrazu).
        root = core.ROOT / self.build_dir
        root.mkdir(parents=True)
        (root / "domains.yaml").write_text("default: board-power-test\n",
                                           encoding="utf-8")
        core.record_build(self.build_dir, self.cmd)
        self.assertIsNone(core.built_hex(self.build_dir))
        self.assertFalse(core.build_up_to_date(self.build_dir, self.cmd))

    def test_inna_komenda_uniewaznia_build(self):
        self._przygotuj_gotowy_build()
        inne = self.cmd + ["-DEXTRA_CONF_FILE=inny.conf"]
        self.assertFalse(core.build_up_to_date(self.build_dir, inne))

    def test_cli_drugi_przebieg_bez_budowania(self):
        def przebieg(**kw):
            answers = iter(["", "tak", "pomin"])
            out = io.StringIO()
            with patch("builtins.input", lambda p="": next(answers)), \
                 contextlib.redirect_stdout(out):
                core.cmd_run(run_args(["zwykly"], dry_run=False,
                                      sample="T#1", **kw))
            return out.getvalue()

        przebieg()
        builds = [c for c in self.env.commands()
                  if c.startswith("west build")]
        self.assertEqual(len(builds), 1)          # pierwszy raz: build

        self.env.log.write_text("")               # wyczyść log
        out = przebieg()
        cmds = self.env.commands()
        self.assertFalse(any(c.startswith("west build") for c in cmds))
        self.assertTrue(any(c.startswith("west flash") for c in cmds))
        self.assertIn("gotowy build", out)

        self.env.log.write_text("")
        przebieg(pristine=True)                   # --pristine wymusza build
        self.assertTrue(any(c.startswith("west build") and "-p always" in c
                            for c in self.env.commands()))


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


class ChildEnvTests(unittest.TestCase):
    """Środowisko procesów west/nrfutil (child_env). REGRESJA: bez
    zdejmowania DISPLAY/WAYLAND_DISPLAY J-Link EDU/EDU Mini pokazuje
    dialog licencyjny przy każdym flashu i przebieg staje na klik."""

    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)

    def test_zdejmuje_display_i_wayland(self):
        os.environ["DISPLAY"] = ":0"
        os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        env = core.child_env()
        self.assertNotIn("DISPLAY", env)
        self.assertNotIn("WAYLAND_DISPLAY", env)
        # Środowiska SAMEJ aplikacji nie ruszamy (TUI dalej ma działać).
        self.assertEqual(os.environ.get("DISPLAY"), ":0")
        self.assertEqual(os.environ.get("WAYLAND_DISPLAY"), "wayland-0")

    def test_dziala_gdy_display_nie_bylo(self):
        os.environ.pop("DISPLAY", None)
        os.environ.pop("WAYLAND_DISPLAY", None)
        self.assertNotIn("DISPLAY", core.child_env())

    def test_przywraca_pythonhome_toolchaina(self):
        os.environ["BPT_SAVED_PYTHONHOME"] = "/ncs/python"
        env = core.child_env()
        self.assertEqual(env["PYTHONHOME"], "/ncs/python")
        self.assertNotIn("BPT_SAVED_PYTHONHOME", env)


class JlinkConflictTests(unittest.TestCase):
    """Wykrywanie cudzej sesji J-Linka na atrapie /proc. Cudzy właściciel
    sondy zawyża pomiar (runner nie wygasza debug interface'u) i wywołuje
    dialog EDU, więc musi być zauważony PRZED flashem."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bpt-proc-")
        self.proc = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _pid(self, pid, has_lib=True, cmdline="nrfutil-device list", ppid=1):
        d = self.proc / str(pid)
        d.mkdir()
        maps = ["7f0000000000-7f0000001000 r--p 0 00:00 0 /lib/libc.so\n"]
        if has_lib:
            maps.append("7f0000002000-7f0000003000 r-xp 00000000 103:05 1 "
                        "/opt/SEGGER/JLink_V952/libjlinkarm.so.9.52.0\n")
        (d / "maps").write_text("".join(maps))
        (d / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode())
        (d / "stat").write_text(f"{pid} (fake proc) S {ppid} 0 0 0\n")
        return d

    def test_zglasza_obcy_proces_z_biblioteka_jlinka(self):
        self._pid(4242, cmdline="nrfutil-device list --hotplug")
        owners = core.jlink_owners(self.proc)
        self.assertEqual([pid for pid, _ in owners], [4242])
        self.assertIn("--hotplug", owners[0][1])

    def test_pomija_procesy_bez_biblioteki(self):
        self._pid(4243, has_lib=False)
        self.assertEqual(core.jlink_owners(self.proc), [])

    def test_pomija_nas_samych(self):
        # pylink otwiera sesję J-Link w NASZYM procesie (tryb autonomiczny,
        # RTT) – to nie konflikt.
        self._pid(os.getpid())
        self.assertEqual(core.jlink_owners(self.proc), [])

    def test_pomija_naszych_potomkow(self):
        # JLinkExe odpalony przez naszego westa też nie jest konfliktem.
        self._pid(4244, cmdline="JLinkExe", ppid=os.getpid())
        self.assertEqual(core.jlink_owners(self.proc), [])

    def test_nieczytelne_maps_nie_wywala(self):
        d = self._pid(4245)
        (d / "maps").unlink()
        self.assertEqual(core.jlink_owners(self.proc), [])

    def test_brak_proc_zwraca_pusta_liste(self):
        self.assertEqual(core.jlink_owners(self.proc / "nie-ma"), [])

    def test_komunikat_wskazuje_nrf_connect(self):
        self.assertEqual(core.jlink_conflict_message([]), "")
        msg = core.jlink_conflict_message(
            [(4242, "nrfutil-device list --hotplug")])
        self.assertIn("pid 4242", msg)
        self.assertIn("nRF Connect for Desktop", msg)
        msg = core.jlink_conflict_message([(7, "JLinkGDBServer")])
        self.assertIn("JLinkGDBServer", msg)


class JlinkGuardCliTests(unittest.TestCase):
    """Pomiar ręczny (`run`) NIE pyta o zajętą sondę: robi się go w nRF
    Connect Power Profiler, więc nRF Connect for Desktop musi być otwarty,
    a jego demony hotplug trzymają libjlinkarm bez przerwy."""

    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)

    def test_run_nie_pyta_o_zajeta_sonde(self):
        self.env.jlink_owners = [(4242, "nrfutil-device list --hotplug")]
        # Kolejno: „programator podłączony?”, SWD odłączony, prąd, uwagi.
        answers = iter(["", "tak", "2.5 mA", ""])

        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                patch("builtins.input", lambda prompt="": next(answers)):
            core.cmd_run(run_args(["zwykly"], dry_run=False, sample="T #1"))
        self.assertNotIn("Sondę J-Link", out.getvalue())
        self.assertTrue(any(c.startswith("west flash")
                            for c in self.env.commands()))


if __name__ == "__main__":
    unittest.main()
