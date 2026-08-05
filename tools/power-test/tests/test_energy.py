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
