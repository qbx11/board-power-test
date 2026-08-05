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
            for other in ("thread", "zigbee"):
                self.assertIn("Wpisz", self.result(self.section(app, other)))

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
