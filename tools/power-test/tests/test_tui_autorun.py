# ============================================================
#  Testy pilotowe TUI trybu autonomicznego
# ============================================================
# Konfiguracja trybu autonomicznego odbywa się w oknie: przełącznik
# trybów na górze + panel ustawień (bez plików planu). Sprzęt (PPK2/RTT)
# podmieniamy na atrapy (fakes), więc pulpit działa headless.

import unittest

from common import FakeEnv, core  # noqa: F401
from fakes import FakeRttReader, FakeSampler

import tui
from autorun import engine as eng
from textual.widgets import Checkbox, Input, Static


class AutorunTuiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)
        # Podmień sprzęt na atrapy (przywracane po teście).
        self._orig = (eng.default_sampler_factory, eng.default_rtt_factory,
                      core.launch_viewer)
        eng.default_sampler_factory = lambda plan: FakeSampler(
            sample_rate=2000)
        eng.default_rtt_factory = lambda profile: FakeRttReader()
        # Nie odpalaj prawdziwego procesu okna wykresu w teście.
        core.launch_viewer = lambda *a, **k: None
        self.addCleanup(self._restore)

    def _restore(self):
        (eng.default_sampler_factory, eng.default_rtt_factory,
         core.launch_viewer) = self._orig

    async def wait_until(self, pilot, cond, timeout=20.0):
        elapsed = 0.0
        while elapsed < timeout:
            if cond(pilot.app):
                return
            await pilot.pause(0.05)
            elapsed += 0.05
        self.fail("timeout")

    async def test_mode_toggle_reveals_config(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            # Domyślnie tryb ręczny – panel autonomiczny ukryty.
            self.assertEqual(app.mode, "standard")
            self.assertFalse(app.query_one("#auto-config").display)
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            self.assertEqual(app.mode, "auto")
            self.assertTrue(app.query_one("#auto-config").display)
            # Ustawienia manualne (przypomnienie SWD) znikają.
            self.assertFalse(app.query_one("#swd_reminder").display)

    async def test_configure_and_run_in_app(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            app.query_one("#check_zwykly", Checkbox).value = True
            # Krótki pomiar + natychmiastowy trigger prosto z okna.
            app.query_one("#auto_duration", Input).value = "0.2"
            app.query_one("#auto_trigger_val", Input).value = "0"
            app.query_one("#sample", Input).value = "BTZ #1"
            await pilot.pause()
            app.query_one("#start", tui.Button).scroll_visible(
                animate=False)
            await pilot.pause()
            await pilot.click("#start")
            await pilot.pause()
            self.assertIsInstance(app.screen, tui.AutoRunScreen)
            await self.wait_until(pilot, lambda a: a.screen._done)
            status = app.screen.query_one("#status", Static)
            self.assertIn("Zakończono", str(status.render()))
        import csv
        with open(core.CSV_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["scenariusz"], "zwykly")

    async def test_per_step_override(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            # Nadpisz krok przez API ekranu konfiguracji (bez klikania ⚙).
            app.auto_overrides["zwykly"] = {"duration": "0.1",
                                            "trigger_type": "delay",
                                            "trigger_val": "0"}
            plan = app._build_auto_plan(["zwykly"], "btz")
            self.assertEqual(len(plan.steps), 1)
            self.assertEqual(plan.steps[0].duration_s, 0.1)
            self.assertEqual(plan.steps[0].trigger.type, "delay")


if __name__ == "__main__":
    unittest.main()
