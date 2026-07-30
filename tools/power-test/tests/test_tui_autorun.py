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
from textual.widgets import Collapsible, DataTable, Input, Select, Static
from textual.widgets._tabbed_content import ContentTab


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

    async def _proto(self, pilot, card, protocol):
        """Włącz protokół karty (nowa karta startuje BEZ protokołu, więc pola
        serii i monitora są dostępne dopiero po wybraniu zakładki)."""
        card.set_protocol(protocol)
        await pilot.pause()

    async def _click_tab(self, pilot, card, protocol):
        """Kliknij zakładkę protokołu tak, jak zrobiłby to użytkownik:
        z rozwiniętymi ustawieniami zaawansowanymi i zakładką w widoku
        (klik w element poza ekranem nigdzie nie trafia)."""
        card.query_one(".card-adv", Collapsible).collapsed = False
        await pilot.pause()
        tab = card.query_one(".card-proto", tui.TabbedContent).get_tab(protocol)
        tab.scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click(tab)
        await pilot.pause()

    async def test_start_w_trybie_autonomicznym(self):
        # Aplikacja startuje w trybie autonomicznym – to on mierzy sam
        # (PPK2), więc kreator kart ma być widoczny bez klikania.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            self.assertEqual(app.mode, "auto")
            self.assertTrue(app.query_one("#measurements").display)
            self.assertFalse(app.query_one("#scenarios").display)
            self.assertFalse(app.query_one("#swd_reminder").display)
            # Domyślnie jedna karta 'Pomiar 1'.
            self.assertEqual(len(list(app.query(tui.MeasurementCard))), 1)

    async def test_mode_toggle_reveals_wizard(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            # Ręczny: checklista widoczna, karty ukryte.
            await pilot.click("#mode-label-standard")
            await pilot.pause()
            self.assertEqual(app.mode, "standard")
            self.assertFalse(app.query_one("#measurements").display)
            self.assertTrue(app.query_one("#scenarios").display)
            # ...i z powrotem do autonomicznego.
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            self.assertEqual(app.mode, "auto")
            self.assertTrue(app.query_one("#measurements").display)
            self.assertFalse(app.query_one("#scenarios").display)

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
        # '…do następnych' zapamiętuje config – kolejny dodany pomiar go
        # dziedziczy.
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

    async def test_apply_to_following_zmienia_istniejace_karty_nizej(self):
        # REGRESJA (#24): przycisk działał tylko jako szablon dla pomiarów
        # jeszcze nieutworzonych – karty już stojące niżej ignorował.
        # Ma objąć wszystkie „niższe”, a wyższych nie ruszać.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            app.add_measurement()
            await pilot.pause()
            app.add_measurement()
            await pilot.pause()
            first, second, third = list(app.query(tui.MeasurementCard))
            for card, value in ((first, "1s"), (second, "2s"),
                                (third, "3s")):
                card.query_one(".card-duration", Input).value = value
            second.query_one(".card-voltage", Input).value = "3.3"
            app._apply_to_following(second.query_one(".card-apply-next",
                                                     tui.Button))
            await pilot.pause()
            # niżej: przejęły ustawienia
            self.assertEqual(
                third.query_one(".card-duration", Input).value, "2s")
            self.assertEqual(
                third.query_one(".card-voltage", Input).value, "3.3")
            # wyżej: nietknięte
            self.assertEqual(
                first.query_one(".card-duration", Input).value, "1s")
            self.assertNotEqual(
                first.query_one(".card-voltage", Input).value, "3.3")
            # i nadal jest szablonem dla nowo dodanych
            app.add_measurement()
            await pilot.pause()
            fourth = list(app.query(tui.MeasurementCard))[-1]
            self.assertEqual(
                fourth.query_one(".card-duration", Input).value, "2s")

    async def test_dodany_scenariusz_od_razu_do_wyboru_w_karcie(self):
        # Regresja: 'Dodaj kod' w trybie autonomicznym dopisywał wpis do
        # manifestu, ale Select istniejącej karty trzymał starą listę opcji
        # – nowego scenariusza nie dało się wybrać bez restartu.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            card.query_one(".card-scenario", Select).value = "zwykly"
            await pilot.pause()
            await pilot.click("#add_fw")
            await self.wait_until(pilot,
                                  lambda a: isinstance(a.screen,
                                                       tui.AddScreen))
            app.screen.query_one("#path", Input).value = "gotowe/firmware.hex"
            await pilot.click("#add")
            await self.wait_until(pilot,
                                  lambda a: not isinstance(a.screen,
                                                           tui.AddScreen))
            await pilot.pause()
            # dotychczasowy wybór przeżywa przeładowanie opcji…
            self.assertEqual(card._scenario(), "zwykly")
            # …a nowy scenariusz da się wybrać (nieznana wartość rzuciłaby)
            card.query_one(".card-scenario", Select).value = "firmware"
            card.query_one(".card-duration", Input).value = "30s"
            await pilot.pause()
            self.assertEqual(card._scenario(), "firmware")
            plan = app._build_auto_plan("btz")
            self.assertEqual(plan.steps[0].scenario, "firmware")

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

    async def test_chip_trigger_in_plan(self):
        # Zakładka Thread = pomiar przez Mattera: sam wybór protokołu daje
        # trigger 'chip', bez żadnego dodatkowego „włącz”. Domyślne wartości
        # pól wystarczą, żeby plan się zbudował; puste Node ID -> czytelny
        # błąd (blokuje start planu, zamiast po cichu wrócić do 'delay').
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "30s"
            await pilot.pause()
            # BLE Mesh (domyślny protokół) -> zwykły start
            self.assertEqual(
                app._build_auto_plan("btz").steps[0].trigger.type, "delay")
            card.query_one(".card-proto", tui.TabbedContent).active = "thread"
            await pilot.pause()
            # Sam Thread, bez dotykania pól -> chip z domyślnymi z karty.
            t = app._build_auto_plan("btz").steps[0].trigger
            self.assertEqual(t.type, "chip")
            self.assertEqual(t.node_id, "5")
            self.assertEqual(t.discriminator, "3840")
            self.assertEqual(t.pin, "20202021")
            self.assertEqual(t.dataset, tui.CHIP_DATASET_DEFAULT)
            self.assertEqual(t.cluster, "temperaturemeasurement")
            self.assertEqual(t.attribute, "measured-value")
            self.assertEqual(t.timeout_s, 120)
            self.assertFalse(t.skip_pairing)
            # Wpisane wartości trafiają do triggera.
            card.field(".card-chip-node").value = "7"
            card.field(".card-chip-disc").value = "1234"
            card.field(".card-chip-dataset").text = "0e08aa"
            card.field(".card-chip-timeout").value = "90s"
            card.field(".card-chip-endpoint").value = "2"
            card.field(".card-chip-skip").value = True
            await pilot.pause()
            t = app._build_auto_plan("btz").steps[0].trigger
            self.assertEqual(t.node_id, "7")
            self.assertEqual(t.discriminator, "1234")
            self.assertEqual(t.dataset, "0e08aa")
            self.assertEqual(t.timeout_s, 90)
            self.assertEqual(t.endpoint, "2")
            self.assertTrue(t.skip_pairing)
            # bez Node ID -> błąd
            card.field(".card-chip-node").value = ""
            await pilot.pause()
            with self.assertRaises(ValueError):
                app._build_auto_plan("btz")

    async def test_matter_bez_datasetu_tylko_gdy_juz_sparowany(self):
        # Parowanie potrzebuje datasetu i discriminatora; „węzeł już
        # sparowany” zdejmuje ten wymóg, bo wtedy tylko subskrybujemy.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "30s"
            card.query_one(".card-proto", tui.TabbedContent).active = "thread"
            await pilot.pause()
            card.field(".card-chip-dataset").text = ""
            await pilot.pause()
            with self.assertRaises(ValueError):
                app._build_auto_plan("btz")
            card.field(".card-chip-skip").value = True
            await pilot.pause()
            t = app._build_auto_plan("btz").steps[0].trigger
            self.assertEqual(t.type, "chip")
            self.assertTrue(t.skip_pairing)
            self.assertEqual(t.dataset, "")

    async def test_pola_matter_tylko_w_zakladce_thread(self):
        # Pola Mattera istnieją WYŁĄCZNIE w panelu Thread – w BLE Mesh i
        # Zigbee nie ma ich ani w drzewie, ani w konfiguracji karty.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            tabs = card.query_one(".card-proto", tui.TabbedContent)
            for proto in ("ble_mesh", "thread", "zigbee"):
                pane = tabs.get_pane(proto)
                for cls in (".card-chip-node", ".card-chip-disc",
                            ".card-chip-pin", ".card-chip-dataset",
                            ".card-chip-cluster", ".card-chip-attr",
                            ".card-chip-endpoint", ".card-chip-min",
                            ".card-chip-max", ".card-chip-timeout",
                            ".card-chip-skip"):
                    self.assertEqual(len(pane.query(cls)),
                                     1 if proto == "thread" else 0,
                                     f"{proto} {cls}")
            self.assertIsNone(card.field(".card-chip-node", "ble_mesh"))
            # Konfiguracja BLE Mesh nie niesie ustawień Mattera.
            cfg = card.get_config()
            self.assertEqual(cfg["chip_node_id"], "")
            self.assertEqual(cfg["chip_dataset"], "")
            self.assertFalse(cfg["chip_skip"])

    async def test_matter_przenosi_sie_do_wszystkich_kart(self):
        # „Zastosuj do wszystkich” przenosi protokół RAZEM z ustawieniami
        # Mattera – inaczej druga karta parowałaby się na cudzy węzeł.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            app.add_measurement()
            await pilot.pause()
            first, second = list(app.query(tui.MeasurementCard))
            first.query_one(".card-proto", tui.TabbedContent).active = "thread"
            await pilot.pause()
            first.field(".card-chip-node").value = "9"
            first.field(".card-chip-dataset").text = "0e08bb"
            first.field(".card-chip-skip").value = True
            await pilot.pause()
            app._apply_to_all(first.query_one(".card-apply", tui.Button))
            await pilot.pause()
            self.assertEqual(second.protocol(), "thread")
            cfg = second.get_config()
            self.assertEqual(cfg["chip_node_id"], "9")
            self.assertEqual(cfg["chip_dataset"], "0e08bb")
            self.assertTrue(cfg["chip_skip"])

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
            # Wynik trafił do tabelki na górze (jeden zakończony pomiar).
            table = app.screen.query_one("#results", DataTable)
            self.assertEqual(table.row_count, 1)
            # Okna build/flash (zwijane sekcje) sprzątnięte po pomiarze
            # (remove_children jest asynchroniczne – dajemy cykl pompy).
            await pilot.pause()
            from textual.widgets import Collapsible
            self.assertEqual(
                len(app.screen.query_one("#cmds").query(Collapsible)), 0)
        import csv
        with open(core.CSV_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["scenariusz"], "zwykly")

    async def test_tabela_pamieci_po_buildzie(self):
        # Po każdym buildzie pulpit trybu autonomicznego pokazuje tę samą
        # tabelkę zajętości pamięci co tryb ręczny. Znika razem z logami
        # kroku (remove_children po pomiarze), więc zbieramy ją w locie.
        app = tui.PowerTestApp()
        reports = set()
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
            await pilot.click("#start")
            await pilot.pause()
            await pilot.click("#ppk2_detect")
            await pilot.pause()
            await pilot.click("#ppk2_start")
            await pilot.pause()
            screen = app.screen
            self.assertIsInstance(screen, tui.AutoRunScreen)
            elapsed = 0.0
            while not screen._done and elapsed < 20.0:
                reports.update(str(w.render())
                               for w in screen.query(".mem-report"))
                await pilot.pause(0.05)
                elapsed += 0.05
            self.assertTrue(screen._done, "plan się nie zakończył")
        joined = "\n".join(reports)
        self.assertIn("| Memory region | Used Size | Region Size | "
                      "%age Used |", joined)
        self.assertIn("| --- | --- | --- | --- |", joined)
        self.assertIn("| FLASH | 118436 B | 1536 KB | 7.53% |", joined)
        self.assertIn("| RAM | 25696 B | 188 KB | 13.35% |", joined)

    async def test_sweep_card_expands_to_steps(self):
        # Karta z włączoną serią rozwija się na wiele kroków "N.M", każdy
        # z inną flagą -DCONFIG_...=<wartość>, wspólny (stały) czas.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            await self._proto(pilot, card, "ble_mesh")
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "10m"
            card.field(".card-sweep-param").value = \
                "CONFIG_LPN_SENSOR_INTERVAL_S"
            card.field(".card-sweep-values").value = "1, 5, 10"
            await pilot.pause()
            plan = app._build_auto_plan("btz")
            self.assertEqual(len(plan.steps), 3)
            self.assertEqual([s.label for s in plan.steps],
                             ["1.1", "1.2", "1.3"])
            self.assertTrue(all(s.scenario == "zwykly" for s in plan.steps))
            self.assertTrue(all(s.duration_s == 600 for s in plan.steps))
            self.assertEqual([s.sweep for s in plan.steps],
                             [[("CONFIG_LPN_SENSOR_INTERVAL_S", v)]
                              for v in ("1", "5", "10")])
            self.assertIn("-DCONFIG_LPN_SENSOR_INTERVAL_S=5",
                          plan.steps[1].build_extra_args)

    async def test_sweep_dwa_parametry_daja_iloczyn(self):
        # Drugi (opcjonalny) parametr: 3 wartości × 2 = 6 kroków, w
        # kolejności z issue #31 – pierwsza oś zmienia się najwolniej.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            await self._proto(pilot, card, "ble_mesh")
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "10m"
            card.field(".card-sweep-param").value = "CONFIG_P1"
            card.field(".card-sweep-values").value = "10, 20, 30"
            card.field(".card-sweep-param2").value = "CONFIG_P2"
            card.field(".card-sweep-values2").value = "100, 200"
            await pilot.pause()
            plan = app._build_auto_plan("btz")
            self.assertEqual([s.sweep for s in plan.steps], [
                [("CONFIG_P1", "10"), ("CONFIG_P2", "100")],
                [("CONFIG_P1", "10"), ("CONFIG_P2", "200")],
                [("CONFIG_P1", "20"), ("CONFIG_P2", "100")],
                [("CONFIG_P1", "20"), ("CONFIG_P2", "200")],
                [("CONFIG_P1", "30"), ("CONFIG_P2", "100")],
                [("CONFIG_P1", "30"), ("CONFIG_P2", "200")]])
            self.assertEqual([s.label for s in plan.steps],
                             [f"1.{i}" for i in range(1, 7)])
            self.assertEqual(plan.steps[3].build_extra_args,
                             ["-DCONFIG_P1=20", "-DCONFIG_P2=200"])
            # Zwinięta karta mówi wprost, ile pomiarów z tego wyjdzie.
            card.set_collapsed(True)
            await pilot.pause()
            title = str(card.query_one(".card-title", tui.CardTitle).render())
            self.assertIn("CONFIG_P1 ×3", title)
            self.assertIn("CONFIG_P2 ×2", title)
            self.assertIn("= 6", title)

    async def test_sweep_wartosci_bez_parametru_to_blad(self):
        # Wartości bez nazwy parametru to przeoczenie – czytelny błąd zamiast
        # po cichu zignorowanej osi. Sam parametr bez wartości błędem NIE
        # jest: symbole Kconfig są wpisane domyślnie, więc o serii decydują
        # wartości (patrz test niżej).
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            await self._proto(pilot, card, "ble_mesh")
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "10m"
            card.field(".card-sweep-param").value = ""
            card.field(".card-sweep-values").value = "1, 2"
            await pilot.pause()
            with self.assertRaises(ValueError):
                app._build_auto_plan("btz")
            # To samo na drugiej osi.
            card.field(".card-sweep-param").value = "CONFIG_P1"
            card.field(".card-sweep-param2").value = ""
            card.field(".card-sweep-values2").value = "100, 200"
            await pilot.pause()
            with self.assertRaises(ValueError):
                app._build_auto_plan("btz")

    async def test_sweep_parametr_bez_wartosci_to_zwykly_pomiar(self):
        # Symbole Kconfig są w zakładce wpisane domyślnie, więc sam parametr
        # nie może włączać serii – inaczej każda karta BLE Mesh byłaby serią
        # bez wartości. Liczą się WARTOŚCI, dowolnej z dwóch osi.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            await self._proto(pilot, card, "ble_mesh")
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "10m"
            await pilot.pause()
            # Oba symbole wpisane (domyślnie), żadnych wartości -> 1 pomiar.
            self.assertEqual(card.field(".card-sweep-param").value,
                             "CONFIG_LPN_SENSOR_INTERVAL_S")
            self.assertEqual(card.field(".card-sweep-param2").value,
                             "CONFIG_BT_MESH_LPN_POLL_TIMEOUT")
            plan = app._build_auto_plan("btz")
            self.assertEqual(len(plan.steps), 1)
            self.assertEqual(plan.steps[0].sweep, [])
            # Wartości TYLKO na drugiej osi -> seria po samym poll intervalu.
            card.field(".card-sweep-values2").value = "60, 120"
            await pilot.pause()
            plan = app._build_auto_plan("btz")
            self.assertEqual([s.sweep for s in plan.steps],
                             [[("CONFIG_BT_MESH_LPN_POLL_TIMEOUT", "60")],
                              [("CONFIG_BT_MESH_LPN_POLL_TIMEOUT", "120")]])

    async def test_poll_interval_w_sekundach_dostaje_flage_x10(self):
        # W zakładce BLE Mesh poll interval wpisujemy w SEKUNDACH, a Kconfig
        # liczy PollTimeout w jednostkach 100 ms: 200 s -> flaga =2000.
        # Dziennik (step.sweep -> kolumny parametr/wartosc) trzyma sekundy.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            await self._proto(pilot, card, "ble_mesh")
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "10m"
            card.field(".card-sweep-values2").value = "200"
            await pilot.pause()
            plan = app._build_auto_plan("btz")
            self.assertEqual(plan.steps[0].build_extra_args,
                             ["-DCONFIG_BT_MESH_LPN_POLL_TIMEOUT=2000"])
            self.assertEqual(plan.steps[0].sweep,
                             [("CONFIG_BT_MESH_LPN_POLL_TIMEOUT", "200")])
            # Sensor interval jest w sekundach także w Kconfigu – bez ×10.
            card.field(".card-sweep-values").value = "30"
            card.field(".card-sweep-values2").value = ""
            await pilot.pause()
            plan = app._build_auto_plan("btz")
            self.assertEqual(plan.steps[0].build_extra_args,
                             ["-DCONFIG_LPN_SENSOR_INTERVAL_S=30"])
            # Przelicznik jest przypisany do SYMBOLU, nie do pola: inny
            # symbol w tym samym polu idzie 1:1.
            card.field(".card-sweep-param2").value = \
                "CONFIG_BT_MESH_LPN_RETRY_TIMEOUT"
            card.field(".card-sweep-values").value = ""
            card.field(".card-sweep-values2").value = "5"
            await pilot.pause()
            plan = app._build_auto_plan("btz")
            self.assertEqual(plan.steps[0].build_extra_args,
                             ["-DCONFIG_BT_MESH_LPN_RETRY_TIMEOUT=5"])

    async def test_poll_interval_poza_zakresem_kconfiga_to_blad(self):
        # Zakres z Kconfiga (10..244735 jednostek = 1..24473.5 s) sprawdzamy
        # przed startem: inaczej build wywala się dopiero po kilku minutach.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            await self._proto(pilot, card, "ble_mesh")
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "10m"
            card.field(".card-sweep-values2").value = "0.5"
            await pilot.pause()
            with self.assertRaises(ValueError):
                app._build_auto_plan("btz")
            card.field(".card-sweep-values2").value = "30000"
            await pilot.pause()
            with self.assertRaises(ValueError):
                app._build_auto_plan("btz")

    async def test_puste_pola_serii_to_zwykly_pomiar(self):
        # Pola serii są zawsze widoczne (bez checkboxa "włącz"), więc karta
        # zostawiona pusto musi dać JEDEN pomiar, a nie serię ani błąd.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "10m"
            await pilot.pause()
            plan = app._build_auto_plan("btz")
            self.assertEqual(len(plan.steps), 1)
            self.assertEqual(plan.steps[0].label, "1")
            self.assertEqual(plan.steps[0].sweep, [])
            # Nowa karta jest bez protokołu, więc nie ma też monitora dongla.
            self.assertEqual(plan.steps[0].monitor_port, "")
            self.assertEqual(plan.steps[0].trigger.type, "delay")
            # Po włączeniu BLE Mesh port dongla jest w karcie wpisany od razu,
            # więc monitor rusza bez klikania – ale sam port nie robi z pomiaru
            # serii ani nie zmienia startu (do tego trzeba fragmentu logu).
            await self._proto(pilot, card, "ble_mesh")
            plan = app._build_auto_plan("btz")
            self.assertEqual(len(plan.steps), 1)
            self.assertEqual(plan.steps[0].monitor_port,
                             tui.DEFAULT_DONGLE_PORT)
            self.assertEqual(plan.steps[0].trigger.type, "delay")

    async def test_zakladki_protokolu_karta_startuje_bez_protokolu(self):
        # Trzy protokoły u góry ustawień zaawansowanych; nowa karta startuje
        # BEZ protokołu (żadna zakładka nie jest aktywna) = zwykły pomiar.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            tabs = card.query_one(".card-proto", tui.TabbedContent)
            self.assertEqual(
                [str(t.label) for t in tabs.query(ContentTab)],
                ["BLE Mesh", "Thread", "Zigbee"])
            self.assertEqual([p.id for p in tabs.query(tui.TabPane)],
                             ["ble_mesh", "thread", "zigbee"])
            self.assertEqual(card.protocol(), "")
            # Bez aktywnej zakładki nie ma o co pytać – pola są w drzewie,
            # ale konfiguracja karty ich nie bierze.
            self.assertIsNone(card.field(".card-sweep-param"))
            cfg = card.get_config()
            self.assertEqual(cfg["protocol"], "")
            self.assertEqual(cfg["sweep_param"], "")
            self.assertEqual(cfg["serial_port"], "")
            # Serię ma każdy protokół, monitor dongla wszystkie oprócz
            # Threada – i wszystko siedzi w swoim panelu, nie luzem w karcie.
            for proto in ("ble_mesh", "thread", "zigbee"):
                pane = tabs.get_pane(proto)
                for cls in (".card-sweep-param", ".card-sweep-values",
                            ".card-sweep-param2", ".card-sweep-values2"):
                    self.assertEqual(len(pane.query(cls)), 1,
                                     f"{proto} {cls}")
                for cls in (".card-serial-port", ".card-serial-pattern"):
                    self.assertEqual(len(pane.query(cls)),
                                     0 if proto == "thread" else 1,
                                     f"{proto} {cls}")
                self.assertIsNone(card.field(".card-serial-port", "thread"))
            # BLE Mesh zna nazwy swoich parametrów, więc ma je wpisane od
            # razu (także zanim ktokolwiek włączy tę zakładkę); Thread
            # i Zigbee startują z pustymi polami.
            self.assertEqual(card.field(".card-sweep-param", "ble_mesh").value,
                             "CONFIG_LPN_SENSOR_INTERVAL_S")
            self.assertEqual(card.field(".card-sweep-param2", "ble_mesh").value,
                             "CONFIG_BT_MESH_LPN_POLL_TIMEOUT")
            for proto in ("thread", "zigbee"):
                for cls in (".card-sweep-param", ".card-sweep-param2"):
                    self.assertEqual(card.field(cls, proto).value, "",
                                     f"{proto} {cls}")

    async def test_klik_w_aktywna_zakladke_wychodzi_z_protokolu(self):
        # Klik w zakładkę AKTYWNEGO protokołu wychodzi z trybu: karta staje
        # się zwykłym pomiarem, a wpisane pola czekają w swojej zakładce
        # i wracają po ponownym kliknięciu.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "10m"
            # Wejście w tryb: klik w zakładkę włącza protokół.
            await self._click_tab(pilot, card, "ble_mesh")
            self.assertEqual(card.protocol(), "ble_mesh")
            card.field(".card-sweep-values2").value = "60, 120"
            await pilot.pause()
            self.assertEqual(len(app._build_auto_plan("btz").steps), 2)
            # Wyjście z trybu: klik w TĘ SAMĄ zakładkę.
            await self._click_tab(pilot, card, "ble_mesh")
            self.assertEqual(card.protocol(), "")
            plan = app._build_auto_plan("btz")
            self.assertEqual(len(plan.steps), 1)      # bez serii
            self.assertEqual(plan.steps[0].sweep, [])
            self.assertEqual(plan.steps[0].monitor_port, "")
            # Powrót: wpisane wartości są nietknięte.
            await self._click_tab(pilot, card, "ble_mesh")
            self.assertEqual(card.protocol(), "ble_mesh")
            self.assertEqual(card.field(".card-sweep-values2").value,
                             "60, 120")
            self.assertEqual(len(app._build_auto_plan("btz").steps), 2)

    async def test_klik_w_inna_zakladke_zmienia_protokol(self):
        # Wyjście z trybu nie może zjadać zwykłego przełączania: klik w INNĄ
        # zakładkę zmienia protokół, a nie wyłącza go.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            for proto in ("ble_mesh", "thread", "zigbee", "ble_mesh"):
                await self._click_tab(pilot, card, proto)
                self.assertEqual(card.protocol(), proto)

    async def test_start_po_czasie_i_rtt_schowane_ale_zywe(self):
        # "Start pomiaru po czasie" i "Konsola RTT" znikają z widoku, ale
        # zostają w drzewie – plan dalej czyta ich wartości, więc schowanie
        # nie może zmienić tego, co się mierzy.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            self.assertFalse(card.query_one(".card-hidden-adv").display)
            for cls in (".card-delay-on", ".card-delay-s",
                        ".card-rtt-on", ".card-rtt", ".card-pattern"):
                self.assertEqual(len(card.query(cls)), 1, cls)
            cfg = card.get_config()
            self.assertFalse(cfg["delay_on"])
            self.assertFalse(cfg["rtt_on"])
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "10m"
            await pilot.pause()
            plan = app._build_auto_plan("btz")
            self.assertEqual(plan.steps[0].rtt, "off")
            self.assertEqual(plan.steps[0].trigger.type, "delay")
            self.assertEqual(plan.steps[0].trigger.seconds, 0)

    async def test_pola_protokolow_nie_mieszaja_sie(self):
        # Każda zakładka ma własne pola: seria wpisana w BLE Mesh nie może
        # wyjść na Threadzie, bo symbol Kconfig z mesha nie istnieje w tamtym
        # stosie. Thread nie ma też monitora dongla.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            await self._proto(pilot, card, "ble_mesh")
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "10m"
            card.field(".card-sweep-param").value = "CONFIG_MESH"
            card.field(".card-sweep-values").value = "1, 2, 3"
            card.field(".card-serial-port").value = "/dev/ttyACM1"
            card.field(".card-serial-pattern").value = "gotowe"
            card.query_one(".card-proto", tui.TabbedContent).active = "thread"
            await pilot.pause()
            self.assertEqual(card.protocol(), "thread")
            plan = app._build_auto_plan("btz")
            self.assertEqual(len(plan.steps), 1)       # pusta zakładka
            self.assertEqual(plan.steps[0].sweep, [])
            self.assertEqual(plan.steps[0].monitor_port, "")
            # Zwinięta karta nie obiecuje serii, której nie będzie.
            card.set_collapsed(True)
            await pilot.pause()
            title = str(card.query_one(".card-title", tui.CardTitle).render())
            self.assertNotIn("sweep", title)
            # Wpisy z BLE Mesh czekają w swojej zakładce nietknięte.
            self.assertEqual(card.field(".card-sweep-values", "ble_mesh").value,
                             "1, 2, 3")

    async def test_seria_dziala_w_kazdym_protokole(self):
        # Serię ma teraz każdy protokół, a Zigbee dodatkowo monitor dongla.
        for proto, port in (("thread", ""), ("zigbee", "/dev/ttyACM3")):
            with self.subTest(proto=proto):
                app = tui.PowerTestApp()
                async with app.run_test(size=(120, 70)) as pilot:
                    await pilot.click("#mode-label-auto")
                    await pilot.pause()
                    card = self._card(app)
                    card.query_one(".card-proto",
                                   tui.TabbedContent).active = proto
                    await pilot.pause()
                    card.query_one(".card-scenario", Select).value = "zwykly"
                    card.query_one(".card-duration", Input).value = "10m"
                    card.field(".card-sweep-param").value = "CONFIG_X"
                    card.field(".card-sweep-values").value = "10, 20"
                    if port:
                        card.field(".card-serial-port").value = port
                    await pilot.pause()
                    plan = app._build_auto_plan("btz")
                    self.assertEqual([s.label for s in plan.steps],
                                     ["1.1", "1.2"])
                    self.assertEqual(plan.steps[1].build_extra_args,
                                     ["-DCONFIG_X=20"])
                    self.assertTrue(all(s.monitor_port == port
                                        for s in plan.steps))

    async def test_protokol_przenosi_sie_do_wszystkich_kart(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            app.add_measurement()
            await pilot.pause()
            first, second = list(app.query(tui.MeasurementCard))
            first.query_one(".card-proto", tui.TabbedContent).active = "zigbee"
            await pilot.pause()
            app._apply_to_all(first.query_one(".card-apply", tui.Button))
            await pilot.pause()
            self.assertEqual(second.protocol(), "zigbee")

    async def test_results_screen_filtered_by_mode(self):
        # Dziennik z jednym wierszem ręcznym (bez sesji) i jednym
        # autonomicznym (z sesją). "Wyniki" pokazują tylko wiersze bieżącego
        # trybu, z kolumnami właściwymi dla trybu.
        manual = {c: "" for c in core.CSV_FIELDS}
        manual.update({"data": "2026-01-01 10:00", "scenariusz": "zwykly",
                       "prad_uA": "0.9"})
        auto = {c: "" for c in core.CSV_FIELDS}
        auto.update({"data": "2026-01-02 10:00", "scenariusz": "lpn",
                     "egzemplarz": "BTZ #7",
                     "prad_uA": "20.5", "pomiar_id": "1.1",
                     "parametr": "CONFIG_LPN_SENSOR_INTERVAL_S",
                     "wartosc": "10", "prad_min_uA": "-0.1",
                     "prad_max_uA": "180000", "czas_s": "120",
                     "sesja": "reports/sessions/run/x"})
        core.append_row(manual, verbose=False)
        core.append_row(auto, verbose=False)

        def kolumny(table):
            return [str(c.label) for c in table.columns.values()]

        # Kolumny opcjonalne (druga oś serii, pamięć) odpadają, gdy żaden
        # widoczny wiersz ich nie wypełnia – zostaje stały trzon.
        def obowiazkowe(cols):
            return [c for c in cols
                    if c not in tui.ResultsScreen.OPTIONAL_COLS]

        app = tui.PowerTestApp()
        async with app.run_test(size=(160, 50)) as pilot:
            app.push_screen(tui.ResultsScreen("standard"))
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            self.assertEqual(table.row_count, 1)          # tylko ręczny
            self.assertEqual(kolumny(table),
                             obowiazkowe(tui.ResultsScreen.MANUAL_COLS))
            app.pop_screen()
            await pilot.pause()
            app.push_screen(tui.ResultsScreen("auto"))
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            self.assertEqual(table.row_count, 1)          # tylko autonomiczny
            cols = kolumny(table)
            self.assertEqual(cols, obowiazkowe(tui.ResultsScreen.AUTO_COLS))
            # min/max prądu nie są pokazywane w tabeli.
            self.assertNotIn("prad_min_uA", tui.ResultsScreen.AUTO_COLS)
            self.assertNotIn("prad_max_uA", tui.ResultsScreen.AUTO_COLS)
            # prąd i czas z dokładnością do 2 miejsc po przecinku.
            cells = list(table.get_row_at(0))
            self.assertEqual(cells[cols.index("prad_uA")], "20.50")
            self.assertEqual(cells[cols.index("czas_s")], "120.00")
            # REGRESJA: tryb autonomiczny gubił kolumnę z egzemplarzem
            # płytki, więc w dzienniku z kilku płytek nie dało się
            # odróżnić, czyj to wynik.
            self.assertIn("egzemplarz", cols)
            self.assertEqual(cells[cols.index("egzemplarz")], "BTZ #7")

    async def test_results_screen_pokazuje_druga_os_serii(self):
        # Kolumny parametr2/wartosc2 doklejają się do tabeli tylko wtedy,
        # gdy w dzienniku jest pomiar dwuparametrowy (symbole Kconfig są
        # długie – stale puste kolumny zjadałyby szerokość).
        row = {c: "" for c in core.CSV_FIELDS}
        row.update({"data": "2026-01-02 10:00", "scenariusz": "lpn",
                    "prad_uA": "20.5", "pomiar_id": "1.4",
                    "parametr": "CONFIG_P1", "wartosc": "20",
                    "parametr2": "CONFIG_P2", "wartosc2": "200",
                    "czas_s": "120", "sesja": "reports/sessions/run/x"})
        core.append_row(row, verbose=False)

        app = tui.PowerTestApp()
        async with app.run_test(size=(160, 50)) as pilot:
            app.push_screen(tui.ResultsScreen("auto"))
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            labels = [str(c.label) for c in table.columns.values()]
            self.assertEqual(
                labels[labels.index("wartosc") + 1:][:2],
                ["parametr2", "wartosc2"])   # zaraz za pierwszą osią
            cells = list(table.get_row_at(0))
            self.assertEqual(cells[labels.index("wartosc2")], "200")

    async def test_results_screen_bez_drugiej_osi_nie_ma_kolumn(self):
        row = {c: "" for c in core.CSV_FIELDS}
        row.update({"data": "2026-01-02 10:00", "scenariusz": "lpn",
                    "prad_uA": "20.5", "pomiar_id": "1.1",
                    "parametr": "CONFIG_P1", "wartosc": "20",
                    "czas_s": "120", "sesja": "reports/sessions/run/x"})
        core.append_row(row, verbose=False)

        app = tui.PowerTestApp()
        async with app.run_test(size=(160, 50)) as pilot:
            app.push_screen(tui.ResultsScreen("auto"))
            await pilot.pause()
            labels = [str(c.label) for c in
                      app.screen.query_one(DataTable).columns.values()]
            self.assertNotIn("parametr2", labels)
            self.assertNotIn("wartosc2", labels)
            self.assertIn("parametr", labels)     # pierwsza oś zostaje

    def test_sweep_str_and_step_label(self):
        S = tui.AutoRunScreen
        self.assertEqual(S._sweep_str(None), "")
        self.assertEqual(
            S._sweep_str([{"param": "CONFIG_LPN_SENSOR_INTERVAL_S",
                           "value": "5"}]),
            "LPN_SENSOR_INTERVAL_S=5")
        # Dwie osie po przecinku – nagłówek okna pomiaru i kolumna "seria"
        # muszą pokazać obie, inaczej połowa kroków wygląda identycznie.
        self.assertEqual(
            S._sweep_str([{"param": "CONFIG_P1", "value": "20"},
                          {"param": "CONFIG_P2", "value": "200"}]),
            "P1=20, P2=200")
        ev = type("E", (), {"data": {"label": "1.2"}, "step": 1})()
        self.assertEqual(S._step_label(ev), "1.2")
        ev2 = type("E", (), {"data": {}, "step": 3})()
        self.assertEqual(S._step_label(ev2), "3")

    def test_fmt_uA_autoscale(self):
        f = tui.AutoRunScreen._fmt_uA
        self.assertEqual(f(1500), "1.500 mA")     # 1500 µA -> 1.5 mA
        self.assertEqual(f(0.5), "500.0 nA")
        self.assertTrue(f(250).endswith("µA"))
        self.assertTrue(f(2_000_000).endswith(" A"))
        self.assertEqual(f(None), "—")

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
            self.assertEqual(plan.steps[0].sample_rate, 100000)  # domyślnie max

    async def test_serial_monitor_in_plan(self):
        # Monitor dongla: port -> monitor_port, a 'start po logu' -> trigger
        # serial z fragmentem. Priorytet nad delay.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            await self._proto(pilot, card, "ble_mesh")
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "30s"
            card.field(".card-serial-port").value = "/dev/ttyACM1"
            card.field(".card-serial-pattern").value = \
                "Friendship z LPN nawiazany"
            await pilot.pause()
            plan = app._build_auto_plan("btz")
            self.assertEqual(plan.steps[0].monitor_port, "/dev/ttyACM1")
            self.assertEqual(plan.steps[0].trigger.type, "serial")
            self.assertEqual(plan.steps[0].trigger.pattern,
                             "Friendship z LPN nawiazany")

    async def test_serial_fields_apply_to_all(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            app.add_measurement()
            await pilot.pause()
            first, second = list(app.query(tui.MeasurementCard))
            await self._proto(pilot, first, "ble_mesh")
            first.field(".card-serial-port").value = "/dev/ttyACM2"
            await pilot.pause()
            app._apply_to_all(first.query_one(".card-apply", tui.Button))
            await pilot.pause()
            self.assertEqual(
                second.field(".card-serial-port").value,
                "/dev/ttyACM2")

    async def test_sample_rate_in_plan(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = self._card(app)
            card.query_one(".card-scenario", Select).value = "zwykly"
            card.query_one(".card-duration", Input).value = "30s"
            card.query_one(".card-rate", Select).value = 1000
            await pilot.pause()
            plan = app._build_auto_plan("btz")
            self.assertEqual(plan.steps[0].sample_rate, 1000)


class LostSamplesWarningTest(unittest.TestCase):
    """Od kiedy okno pomiaru zamyka zegar, zgubione próbki nie objawiają
    się już przeciągniętym pomiarem – muszą być widoczne wprost."""

    def _warned(self, data):
        seen = []
        screen = tui.AutoRunScreen.__new__(tui.AutoRunScreen)
        screen.note = seen.append
        tui.AutoRunScreen._warn_lost_samples(screen, data)
        return seen

    def test_ostrzega_przy_realnej_stracie(self):
        out = self._warned({"samples": 5000, "lost_samples": 5000})
        self.assertTrue(out)
        self.assertIn("5,000", out[0])
        self.assertIn("50%", out[0])

    def test_milczy_przy_komplecie_i_drobnicy(self):
        self.assertFalse(self._warned({"samples": 10000,
                                       "lost_samples": 0}))
        # drobne braki na styku odczytów (<2% okna) to nie awaria
        self.assertFalse(self._warned({"samples": 10000,
                                       "lost_samples": 100}))
        self.assertFalse(self._warned({"samples": 0}))


class CountdownFormatTest(unittest.TestCase):
    """REGRESJA (#22): odliczanie w trybie autonomicznym zacinało się –
    ta sama sekunda potrafiła wisieć dwa takty."""

    def test_kolejne_sekundy_sie_nie_powtarzaja(self):
        # Wartości spadające dokładnie co 1 s muszą dawać ZA KAŻDYM razem
        # inny napis, niezależnie od tego, w którym miejscu sekundy
        # wypadł odczyt. round() przy offsecie 0.5 pokazywało tę samą
        # liczbę dwa razy z rzędu.
        for offset in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9):
            shown = [tui.AutoRunScreen._fmt_countdown(s + offset)
                     for s in range(20, 0, -1)]
            self.assertEqual(len(set(shown)), len(shown), (offset, shown))

    def test_zero_dopiero_gdy_naprawde_koniec(self):
        self.assertEqual(tui.AutoRunScreen._fmt_countdown(0.4), "00:01")
        self.assertEqual(tui.AutoRunScreen._fmt_countdown(0.0), "00:00")
        self.assertEqual(tui.AutoRunScreen._fmt_countdown(-3.0), "00:00")

    def test_pozostalo_liczone_zegarem(self):
        # Czas zegarowy ma pierwszeństwo przed osią próbek (ta przy
        # zgubionych próbkach stoi w miejscu)…
        self.assertEqual(
            tui.AutoRunScreen._remaining_s(
                {"duration_s": 60, "elapsed_s": 10.0,
                 "wall_elapsed_s": 25.0}), 35.0)
        # …ale zdarzenie bez czasu zegarowego nadal działa.
        self.assertEqual(
            tui.AutoRunScreen._remaining_s(
                {"duration_s": 60, "elapsed_s": 10.0}), 50.0)


if __name__ == "__main__":
    unittest.main()
