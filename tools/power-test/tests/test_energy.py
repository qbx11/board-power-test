# ============================================================
#  Testy modelu kalkulatora poboru prądu (autorun/energy.py)
# ============================================================
# Model to jedno działanie: I_avg = I_baseline + Q_send/T_send +
# Q_poll/T_poll. Testy pilnują arytmetyki i przypadków brzegowych pól
# (pusty interwał, zera), bo to one decydują, czy kalkulator pokaże liczbę,
# czy się wysypie.

import unittest

from autorun import energy


class ModelTest(unittest.TestCase):

    def test_prad_sredni_to_bezczynnosc_plus_skladniki(self):
        terms = [energy.Term("send", charge_uC=30.0, period_s=10.0),
                 energy.Term("poll", charge_uC=600.0, period_s=60.0)]
        # 2 + 30/10 + 600/60 = 2 + 3 + 10
        self.assertAlmostEqual(energy.average_uA(2.0, terms), 15.0)

    def test_sama_bezczynnosc_bez_wybudzen(self):
        self.assertAlmostEqual(energy.average_uA(2.4, []), 2.4)

    def test_skladnik_bez_interwalu_nic_nie_dodaje(self):
        # Puste pole interwału (0) nie ma zerować całego wyniku ani wysadzać
        # dzielenia – składnik po prostu nie istnieje.
        terms = [energy.Term("poll", charge_uC=600.0, period_s=0.0)]
        self.assertAlmostEqual(energy.average_uA(2.0, terms), 2.0)

    def test_dluzszy_interwal_to_mniejszy_prad(self):
        # Sedno modelu: liniowość w 1/T. Podwojenie interwału połowi udział
        # składnika.
        short = energy.Term("poll", charge_uC=600.0, period_s=60.0)
        longer = energy.Term("poll", charge_uC=600.0, period_s=120.0)
        self.assertAlmostEqual(short.current_uA(), 10.0)
        self.assertAlmostEqual(longer.current_uA(), 5.0)

    # ---------- okno aktywne po wysyłce (Thread/Matter) ----------

    def test_polle_po_wyslaniu_ida_interwalem_send(self):
        # Trzy polle po każdej wysyłce, każdy po 600 µC, wysyłka co 60 s:
        # 3 * 600 / 60 = 30 µA. Interwał polla nie ma tu nic do rzeczy.
        term = energy.active_window_term(600.0, 3, 60.0)
        self.assertAlmostEqual(term.current_uA(), 30.0)
        self.assertEqual(term.name, energy.ACTIVE_WINDOW)

    def test_polle_po_wyslaniu_kosztuja_tyle_co_zwykle(self):
        # Sedno zmiany: różni je LICZBA i częstość, nie cena jednego polla.
        one = energy.Term("poll", charge_uC=600.0, period_s=60.0)
        five = energy.active_window_term(600.0, 5, 60.0)
        self.assertAlmostEqual(five.current_uA(), 5 * one.current_uA())

    def test_okno_aktywne_doklada_sie_do_reszty(self):
        terms = [energy.Term("send", charge_uC=30.0, period_s=10.0),
                 energy.Term("poll", charge_uC=600.0, period_s=60.0),
                 energy.active_window_term(600.0, 2, 10.0)]
        # 2 + 3 + 10 + 2*600/10 = 135
        self.assertAlmostEqual(energy.average_uA(2.0, terms), 135.0)
        rows = energy.budget(2.0, terms)
        self.assertEqual([r.name for r in rows],
                         ["bezczynność", "send", "poll", energy.ACTIVE_WINDOW])
        self.assertAlmostEqual(sum(r.percent for r in rows), 100.0, places=6)

    # ---------- slow poll z przestawianym licznikiem ----------

    def test_slow_poll_liczy_sie_od_ostatniego_polla(self):
        # Slow poll co 15 s, wysyłka co 60 s: polle o 15, 30 i 45 s, czyli
        # TRZY, a nie cztery – czwarty wypadłby dokładnie w momencie
        # wysyłki, a tam wyprzedza go poll z okna aktywnego.
        self.assertEqual(energy.slow_poll_count(15.0, 60.0), 3)

    def test_slow_poll_gdy_interwaly_nie_dziela_sie_rowno(self):
        # Wysyłka co 70 s przy pollu co 15 s: 15, 30, 45, 60 – cztery.
        self.assertEqual(energy.slow_poll_count(15.0, 70.0), 4)

    def test_slow_poll_nie_dojrzewa_przy_gestych_wysylkach(self):
        # Wysyłka częściej niż interwał polla: licznik nigdy nie wybija.
        self.assertEqual(energy.slow_poll_count(15.0, 10.0), 0)
        # Równo na styk – poll wypadłby w momencie wysyłki, więc też nie.
        self.assertEqual(energy.slow_poll_count(15.0, 15.0), 0)

    def test_slow_poll_nie_lapie_sie_na_blad_floata(self):
        # 0.3/0.1 to na floatach 2.9999999999999996, a 60/0.5 bywa
        # 120.00000000000001 – bez zapasu w ceil() wychodziłby raz poll
        # za mało, raz za dużo.
        self.assertEqual(energy.slow_poll_count(0.1, 0.3), 2)
        self.assertEqual(energy.slow_poll_count(0.5, 60.0), 119)

    def test_slow_poll_bez_interwalow_nie_dzieli_przez_zero(self):
        self.assertEqual(energy.slow_poll_count(0.0, 60.0), 0)
        self.assertEqual(energy.slow_poll_count(15.0, 0.0), 0)

    def test_pierwszy_poll_w_oknie_aktywnym_jest_za_darmo(self):
        # Sedno sprzężenia: JEDEN poll w oknie aktywnym nie dokłada prądu,
        # bo zajmuje miejsce slow polla, który przez niego nie wybije.
        bez_okna = energy.Term("poll", charge_uC=600.0, period_s=15.0)
        z_oknem = [energy.slow_poll_term(600.0, 15.0, 60.0),
                   energy.active_window_term(600.0, 1, 60.0)]
        self.assertAlmostEqual(energy.average_uA(0.0, [bez_okna]), 40.0)
        self.assertAlmostEqual(energy.average_uA(0.0, z_oknem), 40.0)
        # Dopiero drugi i kolejny poll w oknie coś dokładają.
        trzy = [energy.slow_poll_term(600.0, 15.0, 60.0),
                energy.active_window_term(600.0, 3, 60.0)]
        self.assertAlmostEqual(energy.average_uA(0.0, trzy), 60.0)

    def test_budzet_pokazuje_gdzie_idzie_prad(self):
        terms = [energy.Term("send", charge_uC=30.0, period_s=10.0),
                 energy.Term("poll", charge_uC=600.0, period_s=60.0)]
        rows = energy.budget(2.0, terms)
        self.assertEqual([r.name for r in rows],
                         ["bezczynność", "send", "poll"])
        self.assertAlmostEqual(sum(r.percent for r in rows), 100.0, places=6)
        # Poll to 10 z 15 µA – dwie trzecie budżetu.
        self.assertAlmostEqual(dict((r.name, r.percent) for r in rows)["poll"],
                               200 / 3, places=6)

    def test_budzet_z_zerami_nie_dzieli_przez_zero(self):
        # Wszystkie pola puste – rozkład ma wyjść z zerami, a nie wysypać
        # się na dzieleniu przez zero.
        rows = energy.budget(0.0, [])
        self.assertEqual([r.name for r in rows], ["bezczynność"])
        self.assertEqual(rows[0].percent, 0.0)


if __name__ == "__main__":
    unittest.main()
