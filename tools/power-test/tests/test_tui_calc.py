# ============================================================
#  Testy trybu „Kalkulator poboru prądu" (TUI)
# ============================================================
# Trzeci tryb obok pomiaru ręcznego i autonomicznego. Nic nie mierzy i nie
# czyta zapisanych sesji – liczy prąd średni z modelu
# I_avg = I_baseline + Q/T (autorun/energy.py) z liczb wpisanych w polach.

import unittest

from common import FakeEnv, core  # noqa: F401  (core: patchowane stałe)

import tui
from textual.widgets import Input, Label, Static


class CalcTuiTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)

    @staticmethod
    def section(app, protocol="ble_mesh"):
        return app.query_one(f"#calc-{protocol}", tui.CalculatorSection)

    @staticmethod
    def result(section):
        """Wynik tak, jak go widać: liczba z ramki plus rozkład budżetu."""
        return (str(section.query_one(".calc-average-value", Static).render())
                + "\n"
                + str(section.query_one(".calc-budget", Static).render()))

    def fill(self, section, **fields):
        """Wypełnij pola sekcji: baseline / send_charge / send_period /
        poll_charge / poll_period."""
        for name, value in fields.items():
            selector = "." + "calc-" + name.replace("_", "-")
            section.query_one(selector, Input).value = value

    # ---------- tryb ----------

    async def test_trzeci_tryb_w_przelaczniku(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            labels = [str(m.render()) for m in app.query(tui.ModeLabel)]
            self.assertEqual(labels, ["Pomiar ręczny", "Tryb autonomiczny",
                                      "Kalkulator poboru prądu"])
            # Start jak dotąd: tryb autonomiczny, kalkulator schowany.
            self.assertEqual(app.mode, "auto")
            self.assertFalse(app.query_one("#calculator").display)

    async def test_kalkulator_chowa_to_co_mierzy(self):
        # Kalkulator nie rusza płytki, więc profil, egzemplarz, rebuild,
        # Start i Dodaj kod nie mają w nim czego robić.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            self.assertEqual(app.mode, "calc")
            self.assertTrue(app.query_one("#calculator").display)
            # 'Wyniki' też nie: dziennik zbiera POMIARY, a kalkulator
            # niczego do niego nie dopisuje. Zostaje samo 'Wyjście'.
            for hidden in ("#measurements", "#scenarios", "#profile",
                           "#sample", "#pristine", "#start", "#add_fw",
                           "#results_btn"):
                self.assertFalse(app.query_one(hidden).display, hidden)
            self.assertTrue(app.query_one("#quit").display)

    async def test_powrot_do_trybow_pomiarowych(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            await pilot.click("#mode-label-auto")
            await pilot.pause()
            self.assertTrue(app.query_one("#measurements").display)
            self.assertTrue(app.query_one("#profile").display)
            self.assertTrue(app.query_one("#results_btn").display)
            self.assertFalse(app.query_one("#calculator").display)
            await pilot.click("#mode-label-standard")
            await pilot.pause()
            self.assertTrue(app.query_one("#scenarios").display)
            self.assertTrue(app.query_one("#profile").display)
            self.assertFalse(app.query_one("#calculator").display)

    async def test_trzy_sekcje_po_jednej_na_protokol(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            sections = list(app.query(tui.CalculatorSection))
            self.assertEqual([s.protocol for s in sections],
                             ["ble_mesh", "thread", "zigbee"])

    async def test_sekcje_nie_dziela_sie_liczbami(self):
        # Każdy protokół ma własne pola – wpisanie liczb w mesha nie ma
        # przestawiać Thready ani Zigbee.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            self.fill(self.section(app, "ble_mesh"), baseline="2.4",
                      send_charge="22", send_period="30")
            await pilot.pause()
            self.assertIn("3.1 µA", self.result(self.section(app, "ble_mesh")))
            # Thread nie ma własnych składników, więc zostaje pusty.
            self.assertIn("Wpisz", self.result(self.section(app, "thread")))
            # Zigbee ma wpisany z góry heartbeat ZBOSS, więc liczy od razu –
            # ale TYLKO jego, bez liczb wklepanych do mesha.
            zigbee = self.result(self.section(app, "zigbee"))
            self.assertIn("heartbeat", zigbee)
            self.assertNotIn("wysłania", zigbee)

    # ---------- liczenie ----------

    async def test_liczy_prad_z_wpisanych_liczb(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            sec = self.section(app)
            self.fill(sec, baseline="2", send_charge="30", send_period="10",
                      poll_charge="600", poll_period="60")
            await pilot.pause()
            # 2 + 30/10 + 600/60 = 15 µA
            self.assertIn("15.0 µA", self.result(sec))
            # Rozkład budżetu: poll to dwie trzecie.
            self.assertIn("polle", self.result(sec))
            self.assertIn("67%", self.result(sec))

    async def test_wynik_nadaza_za_pisaniem(self):
        # Bez klikania czegokolwiek – liczba zmienia się przy każdym znaku.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            sec = self.section(app)
            self.fill(sec, baseline="2", send_charge="30", send_period="10")
            await pilot.pause()
            self.assertIn("5.0 µA", self.result(sec))
            self.fill(sec, send_period="30")
            await pilot.pause()
            self.assertIn("3.0 µA", self.result(sec))

    async def test_pusty_interwal_polla_znaczy_bez_polla(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            sec = self.section(app)
            self.fill(sec, baseline="2", send_charge="30", send_period="10",
                      poll_charge="600")
            await pilot.pause()
            self.assertIn("5.0 µA", self.result(sec))   # 2 + 3
            self.assertNotIn("polle", self.result(sec))

    async def test_sama_bezczynnosc_tez_jest_wynikiem(self):
        # Węzeł, który nic nie robi, ma prąd bezczynności – i tyle.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            sec = self.section(app)
            self.fill(sec, baseline="2.4")
            await pilot.pause()
            self.assertIn("2.4 µA", self.result(sec))
            self.assertIn("100%", self.result(sec))

    async def test_przecinek_dziesietny_dziala(self):
        # Klawiatura polska daje przecinek – pole ma go przyjąć, a nie
        # milczeć jak przy śmieciu.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            sec = self.section(app)
            self.fill(sec, baseline="2,5")
            await pilot.pause()
            self.assertAlmostEqual(sec.baseline_uA(), 2.5)

    async def test_smiec_w_polu_nie_wysypuje_wyniku(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            sec = self.section(app)
            self.fill(sec, baseline="abc", send_charge="-", send_period="10")
            await pilot.pause()
            self.assertIn("Wpisz", self.result(sec))
            self.assertEqual(sec.terms(), [])

    async def test_pola_maja_podpowiedzi_rzedow_wielkosci(self):
        # Ładunek jednego wybudzenia to nie liczba z katalogu – bez
        # podpowiedzi nikt nie wie, czy wpisać 20, czy 20 000.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            sec = self.section(app)
            for selector in (".calc-baseline", ".calc-send-charge",
                             ".calc-send-period", ".calc-poll-charge",
                             ".calc-poll-period"):
                self.assertTrue(sec.query_one(selector, Input).placeholder,
                                selector)

    async def test_etykiety_nie_gina_w_markupie_rich(self):
        # REGRESJA: Label renderuje treść przez markup Rich, w którym '[s]'
        # jest znacznikiem PRZEKREŚLENIA. "Interwał send [s]:" wychodziło
        # więc jako "Interwał send" z przekreślonym dwukropkiem, a
        # "Interwał poll [s] — pusto = bez polla:" miało przekreśloną całą
        # końcówkę. Dlatego jednostki są w nawiasach okrągłych.
        #
        # Sprawdzamy WIDZIANY tekst wobec pełnej, spisanej listy: render()
        # zwraca treść już przetworzoną przez markup, więc zjedzenie
        # znacznika objawia się brakującym fragmentem w tym porównaniu.
        # (Samo puszczenie render() przez Text.from_markup nic nie wykrywa –
        # markup jest wtedy już rozwinięty.)
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            labels = [str(w.render()) for w in self.section(app).query(Label)]
            self.assertEqual(labels, [
                "BLE Mesh",
                "Prąd bezczynności (baseline) w µA:",
                "Ładunek jednego wysłania (µC):",
                "Interwał send (s):",
                "Ładunek jednego polla (µC):",
                "Interwał poll (s):"])

    # ---------- polle w oknie aktywnym po wysyłce (tylko Thread) ----------

    async def test_pole_polli_po_wyslaniu_tylko_w_thready(self):
        # BLE Mesh i Zigbee nie mają okna aktywnego po wysyłce, więc nie
        # mają i pola – inaczej byłoby to zaproszenie do wpisania liczby,
        # której model tam nie użyje.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            self.assertEqual(
                len(self.section(app, "thread").query(".calc-active-polls")), 1)
            for other in ("ble_mesh", "zigbee"):
                self.assertEqual(
                    len(self.section(app, other).query(".calc-active-polls")),
                    0, other)

    async def test_polle_po_wyslaniu_wchodza_do_wzoru(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            sec = self.section(app, "thread")
            self.fill(sec, baseline="2", send_charge="30", send_period="60",
                      poll_charge="600", poll_period="15")
            await pilot.pause()
            # Bez okna aktywnego: 2 + 0.5 + 600/15 = 42.5
            self.assertIn("42.5 µA", self.result(sec))
            # Trzy polle w oknie aktywnym: slow polle spadają z czterech na
            # trzy (3*600/60 = 30), a okno dokłada 3*600/60 = 30.
            self.fill(sec, active_polls="3")
            await pilot.pause()
            self.assertIn("62.5 µA", self.result(sec))
            self.assertIn("fast polle", self.result(sec))
            self.assertIn("slow polle", self.result(sec))

    async def test_jeden_poll_w_oknie_aktywnym_nic_nie_kosztuje(self):
        # Licznik slow polla startuje od OSTATNIEGO polla w oknie aktywnym,
        # więc jeden fast poll zajmuje miejsce slow polla, zamiast się do
        # niego dokładać – wynik ma zostać ten sam.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            sec = self.section(app, "thread")
            self.fill(sec, poll_charge="600", poll_period="15",
                      send_period="60")
            await pilot.pause()
            self.assertIn("40.0 µA", self.result(sec))
            self.fill(sec, active_polls="1")
            await pilot.pause()
            self.assertIn("40.0 µA", self.result(sec))
            # Nie przez zaokrąglenie: slow polli jest teraz trzy, nie
            # cztery, i tyle samo prądu co poll z okna.
            self.assertEqual([round(t.current_uA(), 6) for t in sec.terms()],
                             [30.0, 10.0])

    async def test_gesta_wysylka_zjada_slow_polla_calkiem(self):
        # Wysyłka co 10 s przy slow pollu co 60 s: licznik nigdy nie wybije,
        # zostają same polle z okna aktywnego.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            sec = self.section(app, "thread")
            self.fill(sec, baseline="2", send_charge="30", send_period="10",
                      poll_charge="600", poll_period="60", active_polls="2")
            await pilot.pause()
            # 2 + 3 + 0 + 2*600/10 = 125
            self.assertIn("125.0 µA", self.result(sec))
            # Wiersz slow polla zostaje w rozkładzie, wyzerowany: to sama
            # w sobie odpowiedź („przy tych interwałach slow polla nie ma”),
            # a nie brak składnika.
            self.assertEqual([(t.name, round(t.current_uA(), 6))
                              for t in sec.terms()],
                             [("send", 3.0), ("poll", 0.0), ("active", 120.0)])

    async def test_sprzezenie_tylko_w_thready(self):
        # BLE Mesh i Zigbee nie mają okna aktywnego, więc ich slow poll
        # liczy się dalej po staremu: pełne Q/T, bez oglądania się na send.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            for proto in ("ble_mesh", "zigbee"):
                sec = self.section(app, proto)
                self.fill(sec, poll_charge="600", poll_period="15",
                          send_period="60")
                if proto == "zigbee":
                    # Zigbee dostał od tego czasu własny heartbeat ZBOSS
                    # (EXTRA_TERMS) – wyłączamy go, bo test sprawdza
                    # sprzężenie polla z oknem aktywnym, nie heartbeat.
                    self.fill(sec, heartbeat_charge="")
                await pilot.pause()
                self.assertIn("40.0 µA", self.result(sec), proto)

    async def test_polle_po_wyslaniu_biora_ladunek_zwyklego_polla(self):
        # Nie ma osobnego pola ładunku – jeden taki poll kosztuje tyle, co
        # zwykły, a różni się tylko liczba i to, że wypada raz na send.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            sec = self.section(app, "thread")
            self.assertEqual(len(sec.query(".calc-active-charge")), 0)
            # Bez interwału polla (węzeł nie pollue cyklicznie) okno
            # aktywne wciąż się liczy.
            self.fill(sec, send_charge="30", send_period="10",
                      poll_charge="600", active_polls="1")
            await pilot.pause()
            self.assertEqual([t.name for t in sec.terms()],
                             ["send", "active"])
            self.assertIn("63.0 µA", self.result(sec))   # 3 + 60

    async def test_puste_pole_polli_znaczy_bez_okna_aktywnego(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            sec = self.section(app, "thread")
            self.fill(sec, baseline="2", send_charge="30", send_period="10",
                      poll_charge="600", poll_period="60")
            await pilot.pause()
            for empty in ("", "0", "abc"):
                self.fill(sec, active_polls=empty)
                await pilot.pause()
                self.assertIn("15.0 µA", self.result(sec), repr(empty))
                self.assertNotIn("fast polle", self.result(sec))

    async def test_etykiety_thready_z_polem_okna_aktywnego(self):
        # Etykieta pola okna aktywnego ma się MIEŚCIĆ w kolumnie (29 znaków
        # przy 120 kolumnach terminala) – dłuższa wersja z jednostką
        # ucinała się w połowie słowa, a Label nie zawija.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            labels = [str(w.render())
                      for w in self.section(app, "thread").query(Label)]
            self.assertEqual(labels, [
                "Thread",
                "Prąd bezczynności (baseline) w µA:",
                "Ładunek jednego wysłania (µC):",
                "Interwał send (s):",
                "Ładunek jednego polla (µC):",
                "Interwał poll (s):",
                "Fast polli po send:"])
            self.assertLessEqual(len(labels[-1]), 29)

    # ---------- tabelka wartości oczekiwanych ----------

    async def test_klik_w_wiersz_wpisuje_wartosc_do_pola(self):
        app = tui.PowerTestApp()
        # Okno wysokie na tyle, żeby wszystkie trzy sekcje (BLE Mesh/Thread/
        # Zigbee) mieściły się bez przewijania – klik Pilota trafia tylko
        # w to, co faktycznie widać na ekranie.
        async with app.run_test(size=(160, 200)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            sec = self.section(app, "thread")
            ref_label = [w for w in sec.query(tui.RefLabel)
                         if w.field == "send-charge"][0]
            await pilot.click(ref_label)
            await pilot.pause()
            self.assertEqual(sec.query_one(".calc-send-charge", Input).value,
                              "95.5")

    async def test_edycja_referencji_nie_rusza_pola_dopoki_nie_klikniesz(self):
        app = tui.PowerTestApp()
        async with app.run_test(size=(160, 200)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            sec = self.section(app, "zigbee")
            ref_input = sec.query_one(".calc-ref-value.calc-ref-baseline",
                                       Input)
            ref_input.value = "9.9"
            await pilot.pause()
            self.assertEqual(sec.baseline_uA(), 0.0)
            ref_label = [w for w in sec.query(tui.RefLabel)
                         if w.field == "baseline"][0]
            await pilot.click(ref_label)
            await pilot.pause()
            self.assertAlmostEqual(sec.baseline_uA(), 9.9)

    async def test_sekcje_maja_wlasne_referencje(self):
        # Tabelka jest w każdej sekcji osobno – edycja w BLE Mesh nie ma
        # przestawiać referencji w Thread ani Zigbee.
        app = tui.PowerTestApp()
        async with app.run_test(size=(160, 70)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            mesh = self.section(app, "ble_mesh")
            mesh.query_one(".calc-ref-value.calc-ref-send-charge",
                           Input).value = "5"
            await pilot.pause()
            # Każda sekcja trzyma SWOJĄ referencję: Thread i Zigbee mają
            # własne z REF_OVERRIDES (pomiar Thread / BTZ Zigbee).
            for other, expected in (("thread", "95.5"), ("zigbee", "46.2")):
                value = self.section(app, other).query_one(
                    ".calc-ref-value.calc-ref-send-charge", Input).value
                self.assertEqual(value, expected, other)

    async def test_thread_ma_wlasne_wartosci_oczekiwane(self):
        # Thread rozkłada się odwrotnie niż węzeł LPN: wysyłka Mattera
        # droga, poll tani. Liczby z mesha dawały tam wynik obok
        # rzeczywistości, dopóki nie nadpisało się ich ręcznie.
        app = tui.PowerTestApp()
        async with app.run_test(size=(160, 200)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            expected = {"ble_mesh": ("1.54", "20", "56"),
                        "thread": ("2.4", "95.5", "11"),
                        "zigbee": ("3.2", "46.2", "19.8")}
            for proto, values in expected.items():
                sec = self.section(app, proto)
                self.assertEqual(
                    tuple(sec.ref_default(f)
                          for f in ("baseline", "send-charge", "poll-charge")),
                    values, proto)

    async def test_podpowiedzi_pol_ida_za_tabelka(self):
        # Placeholder i liczba w tabelce to ta sama wartość – wpisane
        # dwa razy rozjechałyby się przy pierwszej zmianie którejś z nich.
        app = tui.PowerTestApp()
        async with app.run_test(size=(160, 200)) as pilot:
            await pilot.click("#mode-label-calc")
            await pilot.pause()
            for proto in ("ble_mesh", "thread", "zigbee"):
                sec = self.section(app, proto)
                for field in ("baseline", "send-charge", "poll-charge"):
                    ref = sec.query_one(f".calc-ref-value.calc-ref-{field}",
                                        Input).value
                    hint = sec.query_one(f".calc-{field}", Input).placeholder
                    self.assertEqual(hint, f"np. {ref}", f"{proto}/{field}")

    async def test_nie_ma_juz_kalibracji_ani_deep_sleepu(self):
        # Kalkulator liczy z wpisanych liczb – nie czyta przebiegów sesji
        # i nie ma drugiego pola podłogi.
        app = tui.PowerTestApp()
        async with app.run_test(size=(120, 70)) as pilot:
            sec = self.section(app)
            for gone in (".calc-session", ".calc-load", ".calc-floor-deep",
                         ".calc-pick-deep", ".calc-pick-idle"):
                self.assertEqual(len(sec.query(gone)), 0, gone)
            self.assertFalse(hasattr(sec, "load_calibration"))


if __name__ == "__main__":
    unittest.main()
