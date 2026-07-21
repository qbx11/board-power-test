# ============================================================
#  Testy pilotowe TUI (Textual, headless przez app.run_test)
# ============================================================
# Prawdziwy przebieg okienkowy na fałszywym west/nrfutil/SDK
# (patrz common.py): zaznaczamy scenariusze, klikamy Start i
# przeklikujemy dialogi FAZY 2, a potem sprawdzamy, jakie komendy
# faktycznie poszły do narzędzi.
#
#   .venv/bin/python -m unittest discover -s tools/power-test/tests -v

import unittest
from pathlib import Path

from common import FakeEnv, core  # noqa: F401  (core: patchowane stałe)

import tui
from textual.widgets import Checkbox, Input, Static


class TuiHarness(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)

    async def start_run(self, pilot, names, sample="TEST #1"):
        """Zaznacz scenariusze, wpisz egzemplarz i kliknij Start."""
        app = pilot.app
        for name in names:
            app.query_one(f"#check_{name}", Checkbox).value = True
        app.query_one("#sample", Input).value = sample
        await pilot.pause()
        await pilot.click("#start")
        await pilot.pause()

    async def wait_until(self, pilot, cond, timeout=15.0, msg="warunek"):
        elapsed = 0.0
        while elapsed < timeout:
            if cond(pilot.app):
                return
            await pilot.pause(0.05)
            elapsed += 0.05
        self.fail(f"timeout: {msg}")

    async def click_through_run(self, pilot):
        """Przeklikaj dialogi FAZY 2 (flash -> SWD -> pomiar 'Pomiń' ->
        podsumowanie) aż RunScreen wróci do ekranu głównego. Zwraca notki
        z przebiegu (zbierane w locie – po zdjęciu RunScreen już ich nie ma)."""
        app = pilot.app
        notes = set()

        def collect_notes():
            for s in app.screen_stack:
                if isinstance(s, tui.RunScreen):
                    notes.update(str(w.render()) for w in s.query(".note"))

        for _ in range(40):
            await self.wait_until(
                pilot,
                lambda a: not isinstance(a.screen, tui.RunScreen),
                msg="dialog albo koniec przebiegu")
            collect_notes()
            screen = app.screen
            if not isinstance(screen, (tui.ConfirmScreen, tui.ChoiceScreen,
                                       tui.MeasureScreen)):
                return notes  # RunScreen zdjęty – jesteśmy na ekranie głównym
            button = ("#skip" if isinstance(screen, tui.MeasureScreen)
                      else "#yes")
            # Klik z ponowieniem: pod obciążeniem zdarza się trafić w
            # dialog, zanim się w pełni ułoży – wtedy klik idzie w pustkę.
            for _ in range(5):
                await pilot.pause()
                await pilot.click(button)
                try:
                    await self.wait_until(pilot,
                                          lambda a: a.screen is not screen,
                                          timeout=3.0,
                                          msg="zamknięcie dialogu")
                    break
                except AssertionError:
                    continue
            else:
                self.fail("dialog nie zamknął się mimo ponawianych kliknięć")
        self.fail("przebieg nie zakończył się w rozsądnej liczbie dialogów")


class TuiSetupTests(TuiHarness):

    async def test_klik_w_nazwe_zaznacza_a_opis_tylko_rozwija(self):
        # Regresja: wybór scenariusza i rozwijanie opisu to osobne cele
        # kliknięcia – klik w nazwę NIE może otwierać opisu zamiast
        # zaznaczać.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            checkbox = app.query_one("#check_zwykly", Checkbox)
            desc = app.query_one("#desc_zwykly", Static)
            self.assertFalse(checkbox.value)

            await pilot.click("#check_zwykly")
            await pilot.pause()
            self.assertTrue(checkbox.value)               # klik w nazwę = wybór
            self.assertFalse(desc.has_class("shown"))     # ...bez opisu

            await pilot.click("#arrow_zwykly")
            await pilot.pause()
            self.assertTrue(desc.has_class("shown"))      # strzałka rozwija
            self.assertTrue(checkbox.value)               # ...nie rusza wyboru

            await pilot.click("#arrow_zwykly")
            await pilot.pause()
            self.assertFalse(desc.has_class("shown"))     # i zwija z powrotem

    async def test_opis_bez_wartosci_oczekiwanych(self):
        # W polu Scenariusze opis nie pokazuje oczekiwanych/zmierzonych
        # zużyć (pole `expected`) – te widać dopiero w instrukcji pomiaru.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            body = str(app.query_one("#desc_zwykly", Static).render())
            self.assertIn("Firmware z tego repo", body)
            self.assertNotIn("Oczekiwane", body)
            self.assertNotIn("0.95", body)

    async def test_zaznacz_wszystkie_odzwierciedla_stan(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            from textual.widgets import Button
            button = app.query_one("#select_all", Button)
            self.assertFalse(button.has_class("pressed"))

            await pilot.click("#select_all")
            await pilot.pause()
            self.assertTrue(button.has_class("pressed"))

            # odznaczenie choć jednego scenariusza "wyciska" przycisk
            app.query_one("#check_zwykly", Checkbox).value = False
            await pilot.pause()
            self.assertFalse(button.has_class("pressed"))

            app.query_one("#check_zwykly", Checkbox).value = True
            await pilot.pause()
            self.assertTrue(button.has_class("pressed"))


class TuiAddTests(TuiHarness):

    async def test_dodaj_firmware_hex_przez_dialog(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.click("#add_fw")
            await self.wait_until(pilot,
                                  lambda a: isinstance(a.screen,
                                                       tui.AddScreen),
                                  msg="dialog Dodaj firmware")
            app.screen.query_one("#path", Input).value = "gotowe/firmware.hex"
            app.screen.query_one("#label", Input).value = "Gotowy obraz"
            await pilot.click("#add")
            await self.wait_until(pilot,
                                  lambda a: not isinstance(a.screen,
                                                           tui.AddScreen),
                                  msg="zamknięcie dialogu")
            # nowy scenariusz od razu na liście i zaznaczony...
            checkbox = app.query_one("#check_firmware", Checkbox)
            self.assertTrue(checkbox.value)
            self.assertIn("firmware", app.scenarios)
            # ...i trwale zapisany w manifeście
            scen = tui.core.load_manifest()["scenarios"]["firmware"]
            self.assertEqual(scen["hex"], "gotowe/firmware.hex")
            self.assertEqual(scen["label"], "Gotowy obraz")

    async def test_dodaj_firmware_zla_sciezka_nie_zamyka_dialogu(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.click("#add_fw")
            await self.wait_until(pilot,
                                  lambda a: isinstance(a.screen,
                                                       tui.AddScreen),
                                  msg="dialog Dodaj firmware")
            app.screen.query_one("#path", Input).value = "nie_ma_takiego"
            await pilot.click("#add")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, tui.AddScreen)  # dialog trwa
            await pilot.click("#cancel")
            await pilot.pause()
            self.assertNotIn("nie_ma_takiego",
                             (tui.core.MANIFEST_PATH).read_text())


class TuiBrowseTests(TuiHarness):
    """Eksplorator plików w 'Dodaj kod' (przycisk 'Przeglądaj…')."""

    async def open_browser(self, pilot):
        app = pilot.app
        await pilot.click("#add_fw")
        await self.wait_until(pilot,
                              lambda a: isinstance(a.screen, tui.AddScreen),
                              msg="dialog Dodaj kod")
        await pilot.click("#browse")
        await self.wait_until(pilot,
                              lambda a: isinstance(a.screen,
                                                   tui.BrowseScreen),
                              msg="eksplorator plików")
        return app.screen.query_one("#browse-tree", tui.FirmwareTree)

    async def expand_child(self, pilot, node, name):
        """Rozwiń węzeł-katalog o danej nazwie i poczekaj na zawartość."""
        child = next(n for n in node.children
                     if Path(str(n.data.path)).name == name)
        child.expand()
        await self.wait_until(pilot, lambda a: len(child.children) > 0,
                              msg=f"zawartość katalogu {name}")
        return child

    async def test_wybor_pliku_hex_z_drzewa(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            tree = await self.open_browser(pilot)
            # start = katalog nad repo; schodzimy repo -> gotowe -> *.hex
            await self.wait_until(pilot,
                                  lambda a: len(tree.root.children) > 0,
                                  msg="wczytanie katalogu startowego")
            repo = await self.expand_child(pilot, tree.root, "repo")
            gotowe = await self.expand_child(pilot, repo, "gotowe")
            hex_node = gotowe.children[0]
            tree.move_cursor(hex_node)
            tree.action_select_cursor()   # jak Enter/klik na pliku
            await self.wait_until(pilot,
                                  lambda a: isinstance(a.screen,
                                                       tui.AddScreen),
                                  msg="powrót do dialogu")
            self.assertEqual(
                app.screen.query_one("#path", Input).value,
                str(self.env.hex_path))

    async def test_wybor_katalogu_przyciskiem(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            tree = await self.open_browser(pilot)
            await self.wait_until(pilot,
                                  lambda a: len(tree.root.children) > 0,
                                  msg="wczytanie katalogu startowego")
            repo = await self.expand_child(pilot, tree.root, "repo")
            app_dir = next(n for n in repo.children
                           if Path(str(n.data.path)).name == "app_zespolu")
            tree.move_cursor(app_dir)
            await pilot.click("#choose")
            await self.wait_until(pilot,
                                  lambda a: isinstance(a.screen,
                                                       tui.AddScreen),
                                  msg="powrót do dialogu")
            self.assertEqual(
                app.screen.query_one("#path", Input).value,
                str(self.env.source_dir))

    async def test_anuluj_nie_zmienia_pola(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.open_browser(pilot)
            await pilot.click("#cancel")
            await self.wait_until(pilot,
                                  lambda a: isinstance(a.screen,
                                                       tui.AddScreen),
                                  msg="powrót do dialogu")
            self.assertEqual(app.screen.query_one("#path", Input).value, "")

    def test_filtr_drzewa_katalogi_i_hex(self):
        paths = [self.env.repo / "gotowe",           # katalog -> zostaje
                 self.env.hex_path,                  # .hex    -> zostaje
                 self.env.repo / "scenarios.toml",   # inny plik -> odpada
                 self.env.repo / ".ukryty"]          # ukryty -> odpada
        # filter_paths nie używa self – wołamy przez klasę, bez budowania
        # widżetu poza aplikacją
        got = tui.FirmwareTree.filter_paths(None, paths)
        self.assertEqual(got, [self.env.repo / "gotowe", self.env.hex_path])


class TuiRunTests(TuiHarness):

    async def test_przebieg_mieszany_source_hex_i_regresja(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.start_run(pilot, ["zwykly", "zrodlowy", "hexowy"])
            notes = await self.click_through_run(pilot)

            cmds = self.env.commands()
            builds = [c for c in cmds if c.startswith("west build")]
            # FAZA 1: budują się tylko 2 scenariusze (hex bez builda)
            self.assertEqual(len(builds), 2)
            self.assertTrue(any(str(self.env.repo) in b
                                and "app_zespolu" not in b for b in builds))
            self.assertTrue(any(str(self.env.source_dir) in b for b in builds))
            self.assertFalse(any("firmware.hex" in b for b in builds))
            # FAZA 2: zwykłe przez west flash, hex przez nrfutil
            flashes = [c for c in cmds if c.startswith("west flash")]
            self.assertEqual(len(flashes), 2)
            self.assertTrue(any("build_zrodlowy" in f and "-r jlink" in f
                                and "--erase" in f for f in flashes))
            self.assertIn("nrfutil device program --firmware "
                          f"{self.env.hex_path} --options "
                          "chip_erase_mode=ERASE_ALL", cmds)
            # liczniki/notki zgadzają się ze stanem faktycznym
            self.assertTrue(any("Zbudowano 2 obraz(ów)." in n for n in notes))
            self.assertTrue(any("gotowy hex" in n for n in notes))

    async def test_tylko_hex_bez_fazy_builda(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.start_run(pilot, ["hexowy"])
            notes = await self.click_through_run(pilot)

            cmds = self.env.commands()
            # zero westa: ani builda, ani topdir (workspace niepotrzebny)
            self.assertFalse(any(c.startswith("west") for c in cmds))
            self.assertEqual(len([c for c in cmds
                                  if c.startswith("nrfutil device program")]),
                             1)
            self.assertTrue(any("Nic do budowania" in n for n in notes))

    async def test_walidacja_source_i_hex_zatrzymuje_przed_faza_1(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.start_run(pilot, ["zly_oba"])
            await self.wait_until(
                pilot,
                lambda a: isinstance(a.screen, tui.RunScreen)
                and "BŁĄD" in str(a.screen.query_one("#status",
                                                     Static).render()),
                msg="status BŁĄD po walidacji")
            status = str(app.screen.query_one("#status", Static).render())
            self.assertIn("wykluczają", status)
            self.assertEqual(self.env.commands(), [])  # nic nie ruszyło

    async def test_walidacja_brak_pliku_hex(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.start_run(pilot, ["zly_brak_pliku"])
            await self.wait_until(
                pilot,
                lambda a: isinstance(a.screen, tui.RunScreen)
                and "BŁĄD" in str(a.screen.query_one("#status",
                                                     Static).render()),
                msg="status BŁĄD po walidacji")
            self.assertIn("nie istnieje",
                          str(app.screen.query_one("#status",
                                                   Static).render()))
            self.assertEqual(self.env.commands(), [])


if __name__ == "__main__":
    unittest.main()
