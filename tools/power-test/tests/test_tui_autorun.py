# ============================================================
#  Testy pilotowe TUI trybu autonomicznego (kreator kart)
# ============================================================
# Nowy kreator: przełącznik trybów -> karty 'Pomiar N' (scenariusz +
# czas + zaawansowane) -> ekran połączenia z PPK2 -> pulpit. Sprzęt
# (PPK2/RTT) i wykrycie portu podmieniamy na atrapy, więc całość działa
# headless.

import unittest

from common import FakeEnv, core  # noqa: F401
from fakes import FakeRttReader, FakeSampler

import tui
from autorun import engine as eng
from autorun import ppk2 as ppk2mod
from textual.widgets import Input, Select, Static


class AutorunTuiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)
        self._orig = (eng.default_sampler_factory, eng.default_rtt_factory,
                      core.launch_viewer, ppk2mod.find_ppk2)
        eng.default_sampler_factory = lambda plan: FakeSampler(
            sample_rate=2000)
        eng.default_rtt_factory = lambda profile: FakeRttReader()
        core.launch_viewer = lambda *a, **k: None
        ppk2mod.find_ppk2 = lambda port="": "FAKEPORT"   # wykrycie PPK2
        self.addCleanup(self._restore)

    def _restore(self):
        (eng.default_sampler_factory, eng.default_rtt_factory,
         core.launch_viewer, ppk2mod.find_ppk2) = self._orig

    async def wait_until(self, pilot, cond, timeout=20.0):
        elapsed = 0.0
        while elapsed < timeout:
            if cond(pilot.app):
                return
            await pilot.pause(0.05)
            elapsed += 0.05
        self.fail("timeout")

    def _card(self, app):
        return app.query_one(tui.MeasurementCard)

    async def test_mode_toggle_reveals_wizard(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            self.assertEqual(app.mode, "standard")
            # Karty ukryte, checklista widoczna w trybie ręcznym.
            self.assertFalse(app.query_one("#measurements").display)
            self.assertTrue(app.query_one("#scenarios").display)
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            self.assertEqual(app.mode, "auto")
            self.assertTrue(app.query_one("#measurements").display)
            self.assertFalse(app.query_one("#scenarios").display)
            self.assertFalse(app.query_one("#swd_reminder").display)
            # Domyślnie jedna karta 'Pomiar 1'.
            self.assertEqual(len(list(app.query(tui.MeasurementCard))), 1)

    async def test_add_and_remove_measurements(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            app.add_measurement()
            await pilot.pause()
            app.add_measurement()
            await pilot.pause()
            cards = list(app.query(tui.MeasurementCard))
            self.assertEqual(len(cards), 3)
            self.assertEqual([c.number for c in cards], [1, 2, 3])
            # Usuń środkową kartę -> przenumerowanie.
            app.remove_measurement(cards[1].uid)
            await pilot.pause()
            cards = list(app.query(tui.MeasurementCard))
            self.assertEqual([c.number for c in cards], [1, 2])

    async def test_add_collapses_previous(self):
        # '+ Dodaj pomiar' zwija wcześniejsze karty do jednego wiersza,
        # a nowa zostaje rozwinięta (widać całą listę pomiarów).
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            app.add_measurement()
            await pilot.pause()
            first, second = list(app.query(tui.MeasurementCard))
            self.assertTrue(first.collapsed)
            self.assertFalse(second.collapsed)
            self.assertFalse(first.query_one(".card-body").display)
            # Klik w tytuł rozwija z powrotem.
            first.query_one(".card-title", tui.CardTitle).on_click(
                type("E", (), {"stop": lambda self: None})())
            await pilot.pause()
            self.assertFalse(first.collapsed)

    async def test_apply_to_following_templates_new_cards(self):
        # '…do następnych' zapamiętuje config; kolejny dodany pomiar go
        # dziedziczy, ale istniejące karty zostają nietknięte.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            first = self._card(app)
            first.query_one(".card-duration", Input).value = "77s"
            app._apply_to_following(first.query_one(".card-apply-next",
                                                    tui.Button))
            app.add_measurement()
            await pilot.pause()
            _, second = list(app.query(tui.MeasurementCard))
            self.assertEqual(
                second.query_one(".card-duration", Input).value, "77s")

    async def test_rtt_and_delay_in_plan(self):
        # RTT domyślnie wyłączony -> 'off'; po włączeniu tryb 'trigger'
        # trafia do planu. Osobno: start-po-czasie parsuje jednostki ('45s').
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "30s"
            card.query_one(".card-delay-on", tui.Check).value = True
            card.query_one(".card-delay-s", Input).value = "45s"
            await pilot.pause()
            plan = app._build_auto_plan("btz")
            self.assertEqual(plan.steps[0].rtt, "off")
            self.assertEqual(plan.steps[0].trigger.type, "delay")
            self.assertEqual(plan.steps[0].trigger.seconds, 45)
            # Teraz włącz RTT 'start po logu'.
            card.query_one(".card-rtt-on", tui.Check).value = True
            card.query_one(".card-rtt", Select).value = "trigger"
            card.query_one(".card-pattern", Input).value = "Ready"
            await pilot.pause()
            plan = app._build_auto_plan("btz")
            self.assertEqual(plan.steps[0].rtt, "trigger")
            self.assertEqual(plan.steps[0].trigger.type, "rtt")
            self.assertEqual(plan.steps[0].trigger.pattern, "Ready")

    async def test_apply_to_all(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            app.add_measurement()
            await pilot.pause()
            first, second = list(app.query(tui.MeasurementCard))
            first.query_one(".card-duration", Input).value = "42s"
            second.query_one(".card-duration", Input).value = "1h"
            app._apply_to_all(first.query_one(".card-apply", tui.Button))
            await pilot.pause()
            self.assertEqual(
                second.query_one(".card-duration", Input).value, "42s")

    async def test_configure_and_run(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "0.2"
            app.query_one("#sample", Input).value = "BTZ #1"
            await pilot.pause()
            app.query_one("#start", tui.Button).scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click("#start")           # 'Dalej: PPK2 →'
            await pilot.pause()
            self.assertIsInstance(app.screen, tui.Ppk2ConnectScreen)
            await pilot.click("#ppk2_detect")
            await pilot.pause()
            self.assertTrue(app.screen._connected)
            await pilot.click("#ppk2_start")
            await pilot.pause()
            self.assertIsInstance(app.screen, tui.AutoRunScreen)
            await self.wait_until(pilot, lambda a: a.screen._done)
            self.assertIn("Zakończono",
                          str(app.screen.query_one("#status", Static)
                              .render()))
        import csv
        with open(core.CSV_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["scenariusz"], "zwykly")

    async def test_new_measurement_duration_empty(self):
        # Nowy pomiar startuje z pustym czasem (bez szablonu nie dziedziczy),
        # ale 'Zastosuj do wszystkich' czas rozsyła.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            first = self._card(app)
            self.assertEqual(
                first.query_one(".card-duration", Input).value, "")
            first.query_one(".card-duration", Input).value = "8h"
            app.add_measurement()
            await pilot.pause()
            _, second = list(app.query(tui.MeasurementCard))
            self.assertEqual(
                second.query_one(".card-duration", Input).value, "")
            app._apply_to_all(first.query_one(".card-apply", tui.Button))
            await pilot.pause()
            self.assertEqual(
                second.query_one(".card-duration", Input).value, "8h")

    async def test_ppk2_detect_failure_no_crash(self):
        # Brak PPK2: 'Połącz / sprawdź' pokazuje błąd i zostawia Start
        # wyłączony – nie wywala się (regresja: pytał o nieistniejący #start).
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 60)) as pilot:
            def boom():
                raise RuntimeError("brak [PPK2] na USB")   # nawiasy w treści
            app.push_screen(tui.Ppk2ConnectScreen(detect=boom))
            await pilot.pause()
            await pilot.click("#ppk2_detect")
            await pilot.pause()
            self.assertFalse(app.screen._connected)
            self.assertTrue(
                app.screen.query_one("#ppk2_start", tui.Button).disabled)
            self.assertIn("nie znaleziono",
                          str(app.screen.query_one("#ppk2-status", Static)
                              .render()))

    async def test_build_plan_from_cards(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "30s"
            await pilot.pause()
            plan = app._build_auto_plan("btz")
            self.assertEqual(len(plan.steps), 1)
            self.assertEqual(plan.steps[0].scenario, "zwykly")
            self.assertEqual(plan.steps[0].duration_s, 30)
            self.assertEqual(plan.steps[0].trigger.type, "delay")
            self.assertEqual(plan.steps[0].trigger.seconds, 0)  # start od razu


if __name__ == "__main__":
    unittest.main()
