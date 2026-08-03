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
from textual.widgets import Checkbox, Input, Select, Static


class TuiHarness(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)

    async def manual_mode(self, pilot):
        """Przełącz na pomiar ręczny. Aplikacja startuje w trybie
        autonomicznym, więc widgety trybu ręcznego (#scenarios, #check_*,
        #reset) są wtedy ukryte i nie da się w nie kliknąć."""
        await self.click_ready(pilot, "#mode-label-standard")
        await self.wait_until(pilot, lambda a: a.mode == "standard",
                              msg="przełączenie na tryb ręczny")

    async def start_run(self, pilot, names, sample="TEST #1"):
        """Zaznacz scenariusze, wpisz egzemplarz i kliknij Start."""
        await self.manual_mode(pilot)
        app = pilot.app
        for name in names:
            app.query_one(f"#check_{name}", Checkbox).value = True
        app.query_one("#sample", Input).value = sample
        await pilot.pause()
        await self.click_ready(pilot, "#start")
        await pilot.pause()

    async def wait_until(self, pilot, cond, timeout=15.0, msg="warunek"):
        elapsed = 0.0
        while elapsed < timeout:
            if cond(pilot.app):
                return
            await pilot.pause(0.05)
            elapsed += 0.05
        self.fail(f"timeout: {msg}")

    @staticmethod
    def _laid_out(screen, selector):
        """Czy `selector` jest już na wierzchnim ekranie I MA ROZMIAR."""
        # `any`, nie `all`: selektory zbiorcze (np. "Button") trafiają też
        # w widgety celowo ukryte, które rozmiaru nigdy nie dostaną.
        return any(n.size.width and n.size.height
                   for n in screen.query(selector))

    async def wait_for(self, pilot, selector, timeout=15.0):
        await self.wait_until(
            pilot, lambda a: self._laid_out(a.screen, selector),
            timeout=timeout, msg=f"gotowy element {selector}")

    async def click_ready(self, pilot, selector, timeout=15.0):
        """Kliknij dopiero w UŁOŻONY element.

        `push_screen` podmienia `app.screen` natychmiast, ale compose
        i layout dzieją się dopiero w kolejnych cyklach pętli komunikatów.
        Klik w tym oknie kończy się na dwa sposoby, oba widziane w tej
        klasie testów: albo NoMatches (dziecka jeszcze nie ma), albo –
        gorzej – widget istnieje, lecz ma rozmiar 0, więc pilot trafia
        w punkt (0,0), klik przepada bez śladu i test wisi do timeoutu.
        Dlatego czekamy na niezerowy rozmiar celu, nie na sam typ ekranu.

        Cel PRZEWIJAMY do widoku: panel ustawień rośnie z każdą nową opcją,
        a klik poza widoczny obszar to OutOfBounds (pilot nie przewija sam).
        Bez tego dodanie jednej linijki w interfejsie wywracało testy, które
        z tą linijką nie mają nic wspólnego."""
        await self.wait_for(pilot, selector, timeout)
        for node in pilot.app.screen.query(selector):
            if node.size.width and node.size.height:
                node.scroll_visible(animate=False)
                break
        await pilot.pause()
        await pilot.click(selector)

    async def click_and_close(self, pilot, selector, timeout=15.0):
        """Klik w przycisk dialogu + pewność, że dialog FAKTYCZNIE zniknął.
        Bez tego kolejny krok testu ogląda jeszcze zamykany ekran i bierze
        go za następny dialog (stąd np. szukanie '#flash' na oknie blokady
        J-Linka)."""
        screen = pilot.app.screen
        await self.click_ready(pilot, selector, timeout)
        await self.wait_until(pilot, lambda a: a.screen is not screen,
                              timeout=timeout,
                              msg=f"zamknięcie dialogu po {selector}")

    @staticmethod
    def forward_button(screen):
        """Przycisk „idź dalej" napotkanego dialogu: w oknie pomiaru
        pomijamy zapis, w oknie wgrywania flashujemy, w zwykłym
        potwierdzeniu klikamy 'tak'.

        ChoiceScreen występuje w KILKU wariantach (wgrywanie, blokada
        J-Linka), więc rozstrzygamy po przyciskach faktycznie obecnych na
        ekranie, a nie po samej klasie – inaczej na oknie blokady szukamy
        '#flash', którego tam nie ma."""
        if isinstance(screen, tui.MeasureScreen):
            return "#skip"
        if isinstance(screen, tui.ChoiceScreen):
            for candidate in ("#flash", "#ignore"):
                if screen.query(candidate):
                    return candidate
            return "#flash"
        return "#yes"

    async def click_through_run(self, pilot, current=None):
        """Przeklikaj dialogi FAZY 2 (flash -> SWD -> pomiar 'Pomiń' ->
        podsumowanie) aż RunScreen wróci do ekranu głównego. Zwraca notki
        z przebiegu (zbierane w locie – po zdjęciu RunScreen już ich nie ma).
        `current` = wpisz taki prąd [µA] w oknie pomiaru i ZAPISZ zamiast
        pomijać (wtedy przebieg dopisuje wiersz do dziennika)."""
        app = pilot.app
        notes = set()
        # Tabelki pamięci też znikają z RunScreen po jego zdjęciu – zbieramy
        # je w locie do atrybutu, żeby testy mogły je sprawdzić po przebiegu.
        self.mem_reports = set()

        def collect_notes():
            for s in app.screen_stack:
                if isinstance(s, tui.RunScreen):
                    notes.update(str(w.render()) for w in s.query(".note"))
                    self.mem_reports.update(
                        str(w.render()) for w in s.query(".mem-report"))

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
            # Dopiero ułożony dialog mówi, KTÓRY to wariant ChoiceScreen.
            await self.wait_for(pilot, "Button")
            if current is not None and isinstance(screen, tui.MeasureScreen):
                screen.query_one("#current", Input).value = str(current)
                await pilot.pause()
                button = "#save"
            else:
                button = self.forward_button(app.screen)
            # Klik z ponowieniem: click_ready czeka na ułożony przycisk,
            # ale gdyby dialog i tak nie zareagował, próbujemy jeszcze raz.
            for _ in range(5):
                try:
                    await self.click_ready(pilot, button, timeout=3.0)
                    await self.wait_until(pilot,
                                          lambda a: a.screen is not screen,
                                          timeout=3.0,
                                          msg="zamknięcie dialogu")
                    break
                except AssertionError:
                    continue
            else:
                self.fail(f"dialog {type(screen).__name__} nie zamknął się "
                          f"mimo ponawianych kliknięć w {button}")
        self.fail("przebieg nie zakończył się w rozsądnej liczbie dialogów")

    async def drive_collecting_confirm_texts(self, pilot):
        """Jak click_through_run, ale zwraca teksty napotkanych dialogów
        ConfirmScreen – żeby sprawdzić, czy pojawiło się (albo nie)
        przypomnienie o odpięciu programatora."""
        app = pilot.app
        texts = []
        for _ in range(40):
            await self.wait_until(
                pilot,
                lambda a: not isinstance(a.screen, tui.RunScreen),
                msg="dialog albo koniec przebiegu")
            screen = app.screen
            if isinstance(screen, tui.ConfirmScreen):
                texts.append(screen.text)
            if not isinstance(screen, (tui.ConfirmScreen, tui.ChoiceScreen,
                                       tui.MeasureScreen)):
                return texts  # RunScreen zdjęty – koniec przebiegu
            # Dopiero ułożony dialog mówi, KTÓRY to wariant ChoiceScreen.
            await self.wait_for(pilot, "Button")
            button = self.forward_button(app.screen)
            # Klik z ponowieniem: click_ready czeka na ułożony przycisk,
            # ale gdyby dialog i tak nie zareagował, próbujemy jeszcze raz.
            for _ in range(5):
                try:
                    await self.click_ready(pilot, button, timeout=3.0)
                    await self.wait_until(pilot,
                                          lambda a: a.screen is not screen,
                                          timeout=3.0,
                                          msg="zamknięcie dialogu")
                    break
                except AssertionError:
                    continue
            else:
                self.fail(f"dialog {type(screen).__name__} nie zamknął się "
                          f"mimo ponawianych kliknięć w {button}")
        self.fail("przebieg nie zakończył się w rozsądnej liczbie dialogów")


class TuiSetupTests(TuiHarness):

    async def test_klik_w_nazwe_zaznacza_a_opis_tylko_rozwija(self):
        # Regresja: wybór scenariusza i rozwijanie opisu to osobne cele
        # kliknięcia – klik w nazwę NIE może otwierać opisu zamiast
        # zaznaczać.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.manual_mode(pilot)
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

    async def test_zaznaczenie_pokazuje_ptaszek_a_nie_x(self):
        # Zaznaczone checkboxy pokazują ✓ (a nie domyślny X Textuala).
        self.assertEqual(tui.Check.BUTTON_INNER, "✓")
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            box = app.query_one("#check_zwykly", Checkbox)
            self.assertIsInstance(box, tui.Check)
            box.value = True
            await pilot.pause()
            self.assertIn("✓", str(box.render()))
            self.assertNotIn("X", str(box.render()))

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
            await self.manual_mode(pilot)
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


class TuiMouseTests(TuiHarness):
    """Regresja buga: przy kilku otwartych oknach terminala na Linuksie
    tryb myszy bywa gubiony (przełączanie fokusu okien / narzędzie piszące
    do /dev/tty), przez co kliknięcia przestają działać. Aplikacja musi
    odnawiać raportowanie myszy przy odzyskaniu fokusu."""

    async def test_odzyskanie_fokusu_odnawia_mysz(self):
        from textual import events
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            calls = []
            app._reassert_mouse = lambda: calls.append(True)
            app.post_message(events.AppFocus())
            await pilot.pause()
            self.assertTrue(calls, "AppFocus powinien odnowić tryb myszy")

    async def test_reassert_wola_sterownik_i_nie_wywala_sie_bez_niego(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            # Sterownik z metodą włączania myszy – ma zostać wywołany.
            hits = []

            class FakeDriver:
                def _enable_mouse_support(self):
                    hits.append(True)

            real_driver = app._driver
            try:
                app._driver = FakeDriver()
                app._reassert_mouse()
                self.assertEqual(len(hits), 1)

                # Sterownik bez tej metody (np. headless) – bez wyjątku, no-op.
                app._driver = object()
                app._reassert_mouse()      # nie może rzucić
            finally:
                app._driver = real_driver  # przywróć – teardown go używa


class TuiAddTests(TuiHarness):

    async def test_dodaj_firmware_hex_przez_dialog(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            # Nowy wpis zaznacza się sam tylko w trybie ręcznym – tam
            # checkbox znaczy „zmierz to”.
            await self.manual_mode(pilot)
            await self.click_ready(pilot, "#add_fw")
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
            await self.click_ready(pilot, "#add_fw")
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
        await self.click_ready(pilot, "#add_fw")
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
            # ścieżka w polu skrócona do postaci względnej (jak w manifeście)
            self.assertEqual(
                app.screen.query_one("#path", Input).value,
                "gotowe/firmware.hex")

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
                app.screen.query_one("#path", Input).value, "app_zespolu")

    async def test_klik_w_katalog_tylko_rozwija(self):
        # Regresja: ponowny klik w katalog (nawyk podwójnego kliku z GUI)
        # zwijał poddrzewo i widok skakał na górę listy.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            tree = await self.open_browser(pilot)
            await self.wait_until(pilot,
                                  lambda a: len(tree.root.children) > 0,
                                  msg="wczytanie katalogu startowego")
            repo = next(n for n in tree.root.children
                        if Path(str(n.data.path)).name == "repo")
            for _ in range(2):        # dwa "kliki" w ten sam katalog
                tree.move_cursor(repo)
                tree.action_select_cursor()
                await pilot.pause(0.2)
            self.assertTrue(repo.is_expanded)   # dalej rozwinięty

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


class TuiRemoveTests(TuiHarness):

    async def test_usuniecie_scenariusza_krzyzykiem(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.manual_mode(pilot)
            await pilot.click("#del_zrodlowy")
            await self.wait_until(pilot,
                                  lambda a: isinstance(a.screen,
                                                       tui.ConfirmScreen),
                                  msg="potwierdzenie usunięcia")
            await pilot.click("#yes")
            await self.wait_until(
                pilot,
                lambda a: not a.query(f"#row_zrodlowy"),
                msg="zniknięcie wiersza")
            self.assertNotIn("zrodlowy", app.scenarios)
            self.assertNotIn("zrodlowy",
                             tui.core.load_manifest()["scenarios"])

    async def test_anulowanie_nie_usuwa(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.manual_mode(pilot)
            await pilot.click("#del_zrodlowy")
            await self.wait_until(pilot,
                                  lambda a: isinstance(a.screen,
                                                       tui.ConfirmScreen),
                                  msg="potwierdzenie usunięcia")
            await pilot.click("#no")
            await pilot.pause()
            self.assertIn("zrodlowy", tui.core.load_manifest()["scenarios"])
            self.assertTrue(app.query("#row_zrodlowy"))


class TuiScenariosDialogTests(TuiHarness):
    """Okienko „Scenariusze" – lista wpisów z manifestu w trybie
    autonomicznym (w ręcznym ta sama lista stoi wprost na ekranie)."""

    async def open_dialog(self, pilot):
        await pilot.click("#mode-label-auto")
        await pilot.pause()
        await self.click_ready(pilot, "#scenarios_btn")
        await self.wait_until(pilot,
                              lambda a: isinstance(a.screen,
                                                   tui.ScenariosScreen),
                              msg="okienko Scenariusze")

    async def test_przycisk_tylko_w_trybie_autonomicznym(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            # Start jest w trybie autonomicznym, więc przycisk widać od razu.
            self.assertTrue(app.query_one("#scenarios_btn").display)
            await self.manual_mode(pilot)
            self.assertFalse(app.query_one("#scenarios_btn").display)
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            self.assertTrue(app.query_one("#scenarios_btn").display)

    async def test_lista_rozwijanie_opisu_i_zamkniecie(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.open_dialog(pilot)
            self.assertEqual(len(app.screen.query(".scenario-row")),
                             len(app.scenarios))
            desc = app.screen.query_one("#desc_zwykly", Static)
            self.assertFalse(desc.has_class("shown"))
            await pilot.click("#arrow_zwykly")
            await pilot.pause()
            self.assertTrue(desc.has_class("shown"))
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, tui.ScenariosScreen)

    async def test_usuniecie_z_okienka(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.open_dialog(pilot)
            await pilot.click("#del_zrodlowy")
            await self.wait_until(pilot,
                                  lambda a: isinstance(a.screen,
                                                       tui.ConfirmScreen),
                                  msg="potwierdzenie usunięcia")
            await pilot.click("#yes")
            await self.wait_until(
                pilot,
                lambda a: isinstance(a.screen, tui.ScenariosScreen)
                and not a.screen.query("#row_zrodlowy"),
                msg="zniknięcie wiersza w okienku")
            self.assertNotIn("zrodlowy", app.scenarios)
            self.assertNotIn("zrodlowy",
                             tui.core.load_manifest()["scenarios"])
            # …i z listy trybu ręcznego pod spodem
            self.assertFalse(app._main_screen().query("#row_zrodlowy"))

    async def test_nie_usuwa_scenariusza_wybranego_w_karcie(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            card = app.query_one(tui.MeasurementCard)
            card.query_one(".card-scenario", Select).value = "zrodlowy"
            await pilot.pause()
            await self.click_ready(pilot, "#scenarios_btn")
            await self.wait_until(pilot,
                                  lambda a: isinstance(a.screen,
                                                       tui.ScenariosScreen),
                                  msg="okienko Scenariusze")
            await pilot.click("#del_zrodlowy")
            await pilot.pause(0.2)
            # bez pytania o potwierdzenie – okienko i wpis zostają
            self.assertIsInstance(app.screen, tui.ScenariosScreen)
            self.assertIn("zrodlowy", tui.core.load_manifest()["scenarios"])
            self.assertIn("Pomiar 1", app._scenario_in_use("zrodlowy"))

    async def test_nie_usuwa_scenariusza_zaznaczonego_w_trybie_recznym(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            app.query_one("#check_zrodlowy", Checkbox).value = True
            await pilot.pause()
            await self.open_dialog(pilot)
            await pilot.click("#del_zrodlowy")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, tui.ScenariosScreen)
            self.assertIn("zrodlowy", tui.core.load_manifest()["scenarios"])
            self.assertIn("zaznaczony", app._scenario_in_use("zrodlowy"))

    async def test_nie_usuwa_ostatniego_scenariusza(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.open_dialog(pilot)
            keep = "zwykly"
            for name in [n for n in app.scenarios if n != keep]:
                tui.core.remove_scenario(name)
                app.scenarios.pop(name)
                app.screen.remove_row(name)
            await pilot.pause()
            await pilot.click(f"#del_{keep}")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, tui.ScenariosScreen)
            self.assertIn(keep, tui.core.load_manifest()["scenarios"])
            self.assertIn("ostatni scenariusz", app._scenario_in_use(keep))


class TuiRunTests(TuiHarness):

    async def test_pomiar_reczny_zapisuje_zajetosc_pamieci(self):
        # Tryb ręczny też buduje firmware, więc jego wiersze w dzienniku
        # dostają zajętość pamięci z tabelki linkera – tak samo jak
        # autonomiczne. Scenariusz na gotowym hexie builda nie ma, więc
        # zostaje bez tych kolumn.
        import csv
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.start_run(pilot, ["zwykly", "hexowy"])
            await self.click_through_run(pilot, current=1.5)
        with open(core.CSV_PATH, newline="", encoding="utf-8") as f:
            rows = {r["scenariusz"]: r for r in csv.DictReader(f)}
        self.assertEqual(rows["zwykly"]["flash_B"], "118436")
        self.assertEqual(rows["zwykly"]["ram_B"], "25696")
        self.assertEqual(rows["zwykly"]["flash_pct"], "7.53")
        self.assertEqual(rows["hexowy"]["flash_B"], "")

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
            # Opcja resetu jest domyślnie zaznaczona -> west dostaje --reset,
            # a nrfutil reset=RESET_SYSTEM (obok kasowania) w jednym --options.
            self.assertTrue(all("--reset" in f for f in flashes))
            self.assertIn("nrfutil device program --firmware "
                          f"{self.env.hex_path} --options "
                          "chip_erase_mode=ERASE_ALL,reset=RESET_SYSTEM", cmds)
            # liczniki/notki zgadzają się ze stanem faktycznym
            self.assertTrue(any("Zbudowano 2 obraz(ów)." in n for n in notes))
            self.assertTrue(any("gotowy hex" in n for n in notes))

    async def test_reset_domyslnie_zaznaczony_i_odznaczenie_go_wylacza(self):
        # Opcja "Zresetuj płytkę po wgraniu" jest domyślnie zaznaczona;
        # po jej odznaczeniu komendy flash idą bez wymuszonego resetu.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            self.assertTrue(app.query_one("#reset", Checkbox).value)
            app.query_one("#reset", Checkbox).value = False
            await self.start_run(pilot, ["zwykly", "hexowy"])
            await self.click_through_run(pilot)

            cmds = self.env.commands()
            flashes = [c for c in cmds if c.startswith("west flash")]
            self.assertTrue(flashes)
            self.assertFalse(any("--reset" in f for f in flashes))
            self.assertTrue(any(c.startswith("nrfutil device program")
                                and "reset=RESET_SYSTEM" not in c
                                for c in cmds))

    async def test_tabela_pamieci_md_po_buildzie(self):
        # Po zbudowaniu scenariusza źródłowego pokazuje się tabelka pamięci
        # sformatowana jako Markdown (gotowa do skopiowania).
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.start_run(pilot, ["zrodlowy"])
            await self.click_through_run(pilot)
            joined = "\n".join(self.mem_reports)
            self.assertIn("| Memory region | Used Size | Region Size | "
                          "%age Used |", joined)
            self.assertIn("| --- | --- | --- | --- |", joined)
            self.assertIn("| FLASH | 118436 B | 1536 KB | 7.53% |", joined)
            self.assertIn("| RAM | 25696 B | 188 KB | 13.35% |", joined)

    async def test_kopiowanie_logu_budowania(self):
        # Przycisk "Kopiuj log budowania" kopiuje pełny zapis przebiegu
        # (komendy + wyjście + tabelka pamięci) do systemowego schowka.
        captured = []
        orig = tui.core.copy_to_clipboard
        tui.core.copy_to_clipboard = lambda text: (captured.append(text)
                                                   or "pbcopy")
        self.addCleanup(setattr, tui.core, "copy_to_clipboard", orig)
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.start_run(pilot, ["zrodlowy"])
            await self.wait_until(
                pilot,
                lambda a: any(isinstance(s, tui.RunScreen)
                              for s in a.screen_stack),
                msg="RunScreen")
            run_screen = next(s for s in app.screen_stack
                              if isinstance(s, tui.RunScreen))
            # Poczekaj, aż WYJŚCIE builda (nie sama linia komendy) zostanie
            # wystreamowane do zapisu – inaczej kopiujemy przed spłynięciem
            # tabelki pamięci (wyścig asyncio.to_thread -> call_from_thread).
            await self.wait_until(
                pilot,
                lambda a: any("Memory region" in l
                              for l in run_screen.transcript),
                msg="log budowania")
            self.assertTrue(run_screen.query("#copy_log"))   # jest przycisk
            run_screen._copy_log()
            await pilot.pause()
            self.assertTrue(captured)
            self.assertIn("west build", captured[0])
            self.assertIn("Memory region", captured[0])

    async def test_zamkniecie_i_ponowne_otwarcie_okna_wgrywania(self):
        # Okno "wgraj / pomiń" można zamknąć (żeby obejrzeć logi builda);
        # przebieg czeka wtedy na przycisk "Wgraj na płytkę", który
        # otwiera je z powrotem.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.start_run(pilot, ["hexowy"])
            await self.wait_until(
                pilot, lambda a: isinstance(a.screen, tui.ChoiceScreen),
                msg="okno wgrywania")
            run_screen = next(s for s in app.screen_stack
                              if isinstance(s, tui.RunScreen))
            self.assertFalse(run_screen.query_one("#reopen_flash").display)
            await pilot.click("#close")
            await self.wait_until(
                pilot, lambda a: a.screen is run_screen, msg="powrót do logów")
            # Przycisk powrotu odsłonięty, przebieg stoi na oknie wgrywania.
            self.assertTrue(run_screen.query_one("#reopen_flash").display)
            self.assertFalse(self.env.commands())     # nic nie wgrano

            await pilot.pause()          # odsłonięty przycisk musi się ułożyć
            await pilot.click("#reopen_flash")
            await self.wait_until(
                pilot, lambda a: isinstance(a.screen, tui.ChoiceScreen),
                msg="ponowne otwarcie okna wgrywania")
            self.assertFalse(run_screen.query_one("#reopen_flash").display)
            await self.click_through_run(pilot)
            self.assertTrue(any(c.startswith("nrfutil device program")
                                for c in self.env.commands()))

    async def test_swd_reminder_domyslnie_pokazywany(self):
        # Domyślnie (checkbox zaznaczony) przed pomiarem pojawia się
        # przypomnienie o odpięciu programatora.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            self.assertTrue(app.query_one("#swd_reminder", Checkbox).value)
            await self.start_run(pilot, ["hexowy"])
            texts = await self.drive_collecting_confirm_texts(pilot)
            self.assertTrue(any("ODŁĄCZ przewód SWD" in t for t in texts))

    async def test_swd_reminder_odznaczony_pomija_okienko(self):
        # Po odznaczeniu opcji okienko z przypomnieniem SWD się nie pojawia.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            app.query_one("#swd_reminder", Checkbox).value = False
            await self.start_run(pilot, ["hexowy"])
            texts = await self.drive_collecting_confirm_texts(pilot)
            self.assertFalse(any("ODŁĄCZ przewód SWD" in t for t in texts))

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

    async def test_gotowy_build_pomijany_w_tui(self):
        # przygotuj gotowy build zwykly (obraz + znacznik komendy)
        manifest = tui.core.load_manifest()
        cmd, build_dir = tui.core.make_build_cmd(
            "zwykly", manifest["scenarios"]["zwykly"],
            "btz", manifest["boards"]["btz"], "btz")
        d = tui.core.ROOT / build_dir / "zephyr"
        d.mkdir(parents=True)
        (d / "zephyr.hex").write_text(":00000001FF\n")
        tui.core.record_build(build_dir, cmd)

        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.start_run(pilot, ["zwykly"])
            notes = await self.click_through_run(pilot)
            cmds = self.env.commands()
            self.assertFalse(any(c.startswith("west build") for c in cmds))
            self.assertTrue(any(c.startswith("west flash") for c in cmds))
            self.assertTrue(any("gotowy build" in n for n in notes))

    async def test_esc_w_trakcie_builda_wraca_do_menu(self):
        # wolny west: build trwa, Esc przerywa przebieg bez wywrotki
        west = self.env.log.parent / "bin" / "west"
        west.write_text('#!/bin/bash\n'
                        'echo "$(basename "$0") $*" >> "$CMD_LOG"\n'
                        '[ "$1" = "topdir" ] && exit 1\n'
                        '[ "$1" = "build" ] && sleep 5\nexit 0\n')
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.start_run(pilot, ["zwykly"])
            await self.wait_until(
                pilot,
                lambda a: any(c.startswith("west build")
                              for c in self.env.commands()),
                msg="start builda")
            await pilot.press("escape")
            await self.wait_until(
                pilot,
                lambda a: not isinstance(a.screen, tui.RunScreen),
                msg="powrót do menu po Esc")
            await pilot.pause(0.3)
            # aplikacja dalej działa: da się otworzyć np. dialog dodawania
            await self.click_ready(pilot, "#add_fw")
            await self.wait_until(pilot,
                                  lambda a: isinstance(a.screen,
                                                       tui.AddScreen),
                                  msg="aplikacja żyje po Esc")

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


class TuiJlinkGuardTests(TuiHarness):
    """Tryb ręczny NIE sprawdza, czy sondę J-Link trzyma inny program.
    Pomiar ręczny robi się w nRF Connect Power Profiler, więc nRF Connect
    for Desktop musi być otwarty, a jego demony hotplug trzymają
    libjlinkarm bez przerwy – dialog wyskakiwałby przed każdym flashem."""

    async def test_tryb_reczny_nie_pyta_o_zajeta_sonde(self):
        self.env.jlink_owners = [(4242, "nrfutil-device list --hotplug")]
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await self.start_run(pilot, ["zwykly"])
            notes = await self.click_through_run(pilot)
        # Przebieg doszedł do końca bez ani jednego dialogu o sondzie.
        self.assertTrue(any(c.startswith("west flash")
                            for c in self.env.commands()))
        self.assertFalse(any("J-Link" in n for n in notes), notes)


if __name__ == "__main__":
    unittest.main()
