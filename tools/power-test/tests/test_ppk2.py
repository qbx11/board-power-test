# ============================================================
#  Testy bezpieczeństwa sterownika PPK2 (autorun/ppk2.py)
# ============================================================
# Bez sprzętu: podstawiamy atrapę urządzenia (ppk2-api) pod
# Ppk2ApiSampler._ppk2 i sprawdzamy TWARDY limit napięcia, bezpieczny
# stan startowy oraz zwolnienie portu przy zamknięciu.

import unittest

import common  # noqa: F401  (dokłada TOOL_DIR do sys.path)

from autorun import ppk2
from autorun.plan import VOLTAGE_MAX_MV, VOLTAGE_MIN_MV, voltage_to_mV


class FakePPK2:
    """Atrapa ppk2_api.PPK2_API – loguje wywołania, ma port `ser`."""

    def __init__(self):
        self.voltages = []
        self.dut = []
        self.ser = _FakeSerial()

    def set_source_voltage(self, mV):
        self.voltages.append(mV)

    def toggle_DUT_power(self, state):
        self.dut.append(state)

    def stop_measuring(self):
        pass


class _FakeSerial:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class VoltageGuardTest(unittest.TestCase):
    def test_parse_range_inclusive(self):
        self.assertEqual(voltage_to_mV("2.0"), VOLTAGE_MIN_MV)
        self.assertEqual(voltage_to_mV("3.3"), VOLTAGE_MAX_MV)
        self.assertEqual(voltage_to_mV(3.0), 3000)

    def test_parse_rejects_out_of_range(self):
        for bad in ("1.9", "3.4", "5.0", "0", "-1"):
            with self.assertRaises(ValueError, msg=bad):
                voltage_to_mV(bad)

    def test_parse_rejects_garbage(self):
        for bad in ("", "abc", None):
            with self.assertRaises(ValueError):
                voltage_to_mV(bad)

    def test_check_voltage_mV_guard(self):
        self.assertEqual(ppk2.check_voltage_mV(2000), 2000)
        self.assertEqual(ppk2.check_voltage_mV(3300), 3300)
        for bad in (1999, 3301, 5000, 0):
            with self.assertRaises(ppk2.Ppk2Error):
                ppk2.check_voltage_mV(bad)

    def test_set_voltage_blocks_dangerous_command(self):
        """Nawet z pominięciem walidacji planu sterownik NIE wyśle
        groźnego napięcia do urządzenia."""
        s = ppk2.Ppk2ApiSampler()
        s._ppk2 = FakePPK2()
        s.set_voltage(3300)
        self.assertEqual(s._ppk2.voltages, [3300])   # bezpieczne przeszło
        with self.assertRaises(ppk2.Ppk2Error):
            s.set_voltage(5000)                       # groźne odrzucone
        # do urządzenia NIE poszła groźna komenda
        self.assertEqual(s._ppk2.voltages, [3300])

    def test_close_cuts_power_and_frees_port(self):
        s = ppk2.Ppk2ApiSampler()
        fake = FakePPK2()
        s._ppk2 = fake
        s._measuring = True
        s.close()
        self.assertIn("OFF", fake.dut)         # zasilanie odcięte
        self.assertTrue(fake.ser.closed)       # port zwolniony
        self.assertIsNone(s._ppk2)

    def test_dut_power_tracks_state(self):
        s = ppk2.Ppk2ApiSampler()
        s._ppk2 = FakePPK2()
        s.dut_power(True)
        self.assertTrue(s._dut_on)
        s.dut_power(False)
        self.assertFalse(s._dut_on)


if __name__ == "__main__":
    unittest.main()
