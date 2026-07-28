# ============================================================
#  Testy bezpieczeństwa sterownika PPK2 (autorun/ppk2.py)
# ============================================================
# Bez sprzętu: podstawiamy atrapę urządzenia (ppk2-api) pod
# Ppk2ApiSampler._ppk2 i sprawdzamy TWARDY limit napięcia, bezpieczny
# stan startowy oraz zwolnienie portu przy zamknięciu.

import time
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
        self.remainder = {"sequence": b"", "len": 0}
        self.started = False
        self.stops = 0

    def set_source_voltage(self, mV):
        self.voltages.append(mV)

    def toggle_DUT_power(self, state):
        self.dut.append(state)

    def start_measuring(self):
        self.started = True

    def stop_measuring(self):
        self.started = False
        self.stops += 1
        self.ser.streaming = False    # AVERAGE_STOP ucisza urządzenie


class _FakeSerial:
    """Port PPK2. `streaming` = urządzenie wciąż sypie próbkami, więc
    reset_input_buffer() nie opróżnia go na trwałe."""

    def __init__(self, streaming=False):
        self.closed = False
        self.flushed = False
        self.streaming = streaming

    @property
    def in_waiting(self):
        return 4096 if self.streaming else 0

    def reset_input_buffer(self):
        self.flushed = True

    def close(self):
        self.closed = True


def _boom(*_a, **_k):
    raise RuntimeError("port padł")


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

    def test_start_realigns_stream(self):
        # start() musi wyczyścić bufor portu i wyzerować `remainder`, żeby
        # próbki 4-bajtowe nie były przesunięte (to dawało błędne odczyty).
        s = ppk2.Ppk2ApiSampler()
        fake = FakePPK2()
        fake.remainder = {"sequence": b"xy", "len": 2}   # zaległa reszta
        s._ppk2 = fake
        s.start()
        self.assertTrue(fake.ser.flushed)
        self.assertEqual(fake.remainder, {"sequence": b"", "len": 0})
        self.assertTrue(fake.started)
        self.assertTrue(s._measuring)

    def test_close_cuts_power_and_frees_port(self):
        s = ppk2.Ppk2ApiSampler()
        fake = FakePPK2()
        s._ppk2 = fake
        s._measuring = True
        s.close()
        self.assertIn("OFF", fake.dut)         # zasilanie odcięte
        self.assertTrue(fake.ser.closed)       # port zwolniony
        self.assertIsNone(s._ppk2)

    def test_close_ucisza_urzadzenie_przed_zamknieciem_portu(self):
        # REGRESJA (#23): pomiar przerwany Esc zostawiał PPK2 w środku
        # strumienia próbek – kolejny pomiar nie ruszał bez fizycznego
        # restartu urządzenia. close() musi zatrzymać nadawanie i opróżnić
        # bufor, ZANIM zamknie port.
        #
        # Stan jak po Esc: pętla pomiaru zdążyła już zawołać stop(), więc
        # `_measuring` jest False i samo close() nic do urządzenia nie
        # wysyłało – a ono wciąż nadawało zaległe próbki.
        s = ppk2.Ppk2ApiSampler()
        fake = FakePPK2()
        fake.ser.streaming = True
        s._ppk2 = fake
        s._measuring = False
        s.close()
        self.assertGreaterEqual(fake.stops, 1)
        self.assertFalse(fake.ser.streaming)   # urządzenie ucichło
        self.assertTrue(fake.ser.flushed)      # bufor opróżniony
        self.assertTrue(fake.ser.closed)

    def test_close_odcina_zasilanie_mimo_bledu_stopu(self):
        # Gdy stop() poleci wyjątkiem, zasilanie płytki I TAK musi zniknąć
        # (wcześniej oba kroki dzieliły jeden try i dut_power się gubiło).
        s = ppk2.Ppk2ApiSampler()
        fake = FakePPK2()
        fake.stop_measuring = _boom
        s._ppk2 = fake
        s._measuring = True
        s.close()
        self.assertIn("OFF", fake.dut)
        self.assertTrue(fake.ser.closed)

    def test_dut_power_tracks_state(self):
        s = ppk2.Ppk2ApiSampler()
        s._ppk2 = FakePPK2()
        s.dut_power(True)
        self.assertTrue(s._dut_on)
        s.dut_power(False)
        self.assertFalse(s._dut_on)


class _CalPPK2:
    """Atrapa do testu _load_calibration: get_modifiers() zwraca None
    (porażka odczytu metadanych) dopóki nie minie `succeed_after` prób;
    ok=False = nigdy się nie udaje."""

    def __init__(self, succeed_after=0, ok=True):
        self.calls = 0
        self.succeed_after = succeed_after
        self.ok = ok
        self.ser = _FakeSerial()
        self.ser.reset_input_buffer = lambda: None

    def get_modifiers(self):
        self.calls += 1
        if not self.ok:
            return None
        return True if self.calls > self.succeed_after else None


class _FreeRunningSerial:
    """Port PPK2 nadający swobodnie: `in_waiting` rośnie z upływem czasu,
    a bajty można odebrać TYLKO raz. Bufor ma sufit – po jego przekroczeniu
    najstarsze dane przepadają, dokładnie jak w sterowniku tty. Dzięki temu
    da się bez sprzętu pokazać, że wolny konsument gubi próbki."""

    RATE_BPS = 100_000 * 4          # ~400 kB/s
    CAP = 4 * 1024                  # ciasny bufor jak tty

    def __init__(self):
        self.closed = False
        self.flushed = False
        self.timeout = None
        self.streaming = True
        self._t0 = time.monotonic()
        self._produced = 0           # ile bajtów urządzenie już nadało
        self._taken = 0              # ile host odebrał
        self.dropped = 0             # ile przepadło z przepełnienia

    def _advance(self):
        if not self.streaming:
            return
        want = int((time.monotonic() - self._t0) * self.RATE_BPS)
        want -= want % 4
        self._produced = max(self._produced, want)
        pending = self._produced - self._taken
        if pending > self.CAP:                 # przepełnienie – strata
            lost = pending - self.CAP
            self.dropped += lost
            self._taken += lost

    @property
    def in_waiting(self):
        self._advance()
        return self._produced - self._taken

    def read(self, n):
        self._advance()
        n = min(n, self._produced - self._taken)
        if n <= 0:
            time.sleep(0.001)                  # udaje blokadę do timeoutu
            return b""
        self._taken += n
        return b"\x00" * n

    def reset_input_buffer(self):
        self.flushed = True
        self._advance()
        self._taken = self._produced

    def close(self):
        self.closed = True


class _FreeRunningPPK2(FakePPK2):
    def __init__(self):
        super().__init__()
        self.ser = _FreeRunningSerial()

    def get_data(self):
        # Jak PPK2_API.get_data – zabiera to, co akurat leży w porcie.
        return self.ser.read(self.ser.in_waiting)

    def get_samples(self, buf):
        # Dekodowanie kosztuje czas – tak jak pętla Pythona w ppk2-api.
        n = len(buf) // 4
        time.sleep(n / 800_000)                # ~817 kS/s, jak zmierzone
        return [1.0] * n, []

    def stop_measuring(self):
        super().stop_measuring()
        self.ser.streaming = False


class DrainThreadTest(unittest.TestCase):
    """REGRESJA: port drenowała ta sama pętla, która dekoduje i zapisuje –
    przez większość czasu nie czytał go NIKT i PPK2 gubiło dziesiątki
    procent okna (zaobserwowane 35%)."""

    def _sampler(self):
        s = ppk2.Ppk2ApiSampler()
        s._ppk2 = _FreeRunningPPK2()
        self.addCleanup(s.stop)
        return s

    def test_watek_drenujacy_nadaza_mimo_wolnego_konsumenta(self):
        s = self._sampler()
        s.start()
        got = 0
        # Konsument celowo ospały (100 ms na cykl) – dokładnie ten wzorzec,
        # który wcześniej gubił dane.
        for _ in range(10):
            time.sleep(0.1)
            got += len(s.read())
        s.stop()
        got += len(s.read())               # ogon z bufora
        dropped = s._ppk2.ser.dropped
        self.assertEqual(dropped, 0, f"port przepełniony o {dropped} B")
        # ~1 s strumienia przy 100 kS/s; luz na rozjazd zegara.
        self.assertGreater(got, 70_000, f"odebrano tylko {got} probek")

    def test_bez_drenowania_dane_gina(self):
        # Kontrola dowodząca, że atrapa portu w ogóle potrafi gubić: ten sam
        # wzorzec BEZ wątku (odczyt dopiero w chwili konsumpcji).
        ser = _FreeRunningSerial()
        taken = 0
        for _ in range(10):
            time.sleep(0.1)
            taken += len(ser.read(ser.in_waiting))
        self.assertGreater(ser.dropped, 0)
        self.assertLess(taken, 100_000)      # ledwie ułamek strumienia

    def test_stop_domyka_watek(self):
        s = self._sampler()
        s.start()
        time.sleep(0.05)
        reader = s._reader
        s.stop()
        self.assertIsNone(s._reader)
        self.assertFalse(reader.is_alive())

    def test_przepelnienie_bufora_to_blad_a_nie_cisza(self):
        # Gdy konsument stanie na tyle długo, że bufor RAM się zapcha,
        # read() ma KRZYCZEĆ – inaczej mielilibyśmy dane sprzed pół minuty.
        s = self._sampler()
        s.start()
        s._overflow = True
        with self.assertRaises(ppk2.Ppk2Error) as ctx:
            s.read()
        self.assertIn("przepełniony", str(ctx.exception))


class _StreamingPPK2:
    """PPK2 zostawione w trakcie pomiaru: dopóki nadaje próbki,
    get_modifiers() nie znajduje metadanych."""

    def __init__(self):
        self.ser = _FakeSerial(streaming=True)
        self.stops = 0

    def stop_measuring(self):
        self.stops += 1
        self.ser.streaming = False

    def get_modifiers(self):
        return not self.ser.streaming


class LoadCalibrationTest(unittest.TestCase):
    def setUp(self):
        # bez realnego czekania między próbami
        self._sleep = ppk2.time.sleep
        ppk2.time.sleep = lambda *_: None
        self.addCleanup(lambda: setattr(ppk2.time, "sleep", self._sleep))

    def test_retries_then_succeeds(self):
        s = ppk2.Ppk2ApiSampler()
        s._ppk2 = _CalPPK2(succeed_after=3)
        s._load_calibration()                 # nie rzuca
        self.assertGreaterEqual(s._ppk2.calls, 4)

    def test_never_calibrated_raises(self):
        s = ppk2.Ppk2ApiSampler()
        s._ppk2 = _CalPPK2(ok=False)
        with self.assertRaises(ppk2.Ppk2Error) as ctx:
            s._load_calibration()
        self.assertIn("kalibrac", str(ctx.exception))

    def test_ucisza_urzadzenie_zostawione_w_pomiarze(self):
        # REGRESJA (#23): PPK2 porzucone w trakcie nadawania (poprzedni
        # przebieg przerwany Esc) topiło metadane w strumieniu próbek –
        # sam flush bufora nic nie dawał, trzeba było przepiąć USB.
        # Odczyt kalibracji musi najpierw wysłać AVERAGE_STOP.
        s = ppk2.Ppk2ApiSampler()
        s._ppk2 = _StreamingPPK2()
        s._load_calibration()                 # nie rzuca
        self.assertGreaterEqual(s._ppk2.stops, 1)


class _FakePort:
    def __init__(self, device, serial_number):
        self.device = device
        self.serial_number = serial_number


class FindPpk2Test(unittest.TestCase):
    """Autodetekcja portu bez sprzętu (podstawiamy list_devices/comports)."""

    def _patch(self, devices, ports):
        from ppk2_api.ppk2_api import PPK2_API
        from serial.tools import list_ports
        self._orig = (PPK2_API.list_devices, list_ports.comports)
        PPK2_API.list_devices = staticmethod(lambda: list(devices))
        list_ports.comports = lambda: list(ports)
        self.addCleanup(self._restore)

    def _restore(self):
        from ppk2_api.ppk2_api import PPK2_API
        from serial.tools import list_ports
        PPK2_API.list_devices, list_ports.comports = self._orig

    def test_explicit_port_passthrough(self):
        self.assertEqual(ppk2.find_ppk2("/dev/ttyX"), "/dev/ttyX")

    def test_one_device_two_interfaces(self):
        # Jedno PPK2 wystawia dwa porty o TYM SAMYM serialu – nie jest to
        # 'kilka PPK2'; zwracamy jeden, deterministycznie pierwszy.
        devs = ["/dev/cu.usbmodemAAA4", "/dev/cu.usbmodemAAA2"]
        ports = [_FakePort("/dev/cu.usbmodemAAA4", "AAA"),
                 _FakePort("/dev/cu.usbmodemAAA2", "AAA")]
        self._patch(devs, ports)
        self.assertEqual(ppk2.find_ppk2(), "/dev/cu.usbmodemAAA2")

    def test_none_found(self):
        self._patch([], [])
        with self.assertRaises(ppk2.Ppk2Error):
            ppk2.find_ppk2()

    def test_two_distinct_devices_error(self):
        devs = ["/dev/cu.usbmodemAAA2", "/dev/cu.usbmodemBBB2"]
        ports = [_FakePort("/dev/cu.usbmodemAAA2", "AAA"),
                 _FakePort("/dev/cu.usbmodemBBB2", "BBB")]
        self._patch(devs, ports)
        with self.assertRaises(ppk2.Ppk2Error) as ctx:
            ppk2.find_ppk2()
        self.assertIn("kilka PPK2", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
