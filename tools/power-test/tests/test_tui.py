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
            await pilot.click(button)
            await self.wait_until(pilot, lambda a: a.screen is not screen,
                                  msg="zamknięcie dialogu")
        self.fail("przebieg nie zakończył się w rozsądnej liczbie dialogów")


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
