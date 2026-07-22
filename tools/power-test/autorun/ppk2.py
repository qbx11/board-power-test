# ============================================================
#  autorun/ppk2.py – wrapper Power Profiler Kit II (ppk2-api)
# ============================================================
# Jedyny plik dotykający biblioteki `ppk2_api` (import leniwy, w
# open()) – reszta silnika widzi tylko protokół Ppk2Sampler, a testy
# podstawiają FakeSampler o tym samym kształcie.
#
# PPK2 pracuje jako SOURCE METER: sam zasila płytkę (VOUT -> VDD)
# napięciem set_voltage() i mierzy prąd ~100 000 próbek/s. read()
# zwraca próbki w µA jako np.ndarray – dekodowanie robi ppk2-api,
# my tylko pakujemy wynik w numpy.

import time

import numpy as np

SAMPLE_RATE = 100_000     # PPK2 sampluje stałe 100 kS/s


class Ppk2Error(RuntimeError):
    pass


def find_ppk2(port=""):
    """Port szeregowy PPK2: jawnie wskazany albo autodetekcja
    (PPK2_API.list_devices). Czytelne błędy przy braku/wielu."""
    if port:
        return port
    from ppk2_api.ppk2_api import PPK2_API
    found = PPK2_API.list_devices()
    if not found:
        raise Ppk2Error(
            "nie znaleziono PPK2. Podłącz Power Profiler Kit II po USB "
            "(port MCU) albo wskaż port w planie: ppk2_port = \"...\"")
    if len(found) > 1:
        raise Ppk2Error(
            f"znaleziono kilka PPK2 ({', '.join(found)}) – wskaż port "
            "w planie: ppk2_port = \"...\"")
    return found[0]


class Ppk2ApiSampler:
    """Właściwy sampler na bibliotece ppk2-api. Cykl życia:
    open() -> set_voltage() -> dut_power(True) -> start() ->
    read()* -> stop() -> close()."""

    sample_rate = SAMPLE_RATE

    def __init__(self, port=""):
        self._port_hint = port
        self._ppk2 = None
        self._measuring = False

    def open(self):
        from ppk2_api.ppk2_api import PPK2_API
        port = find_ppk2(self._port_hint)
        try:
            self._ppk2 = PPK2_API(port)
            # Modyfikatory kalibracyjne z urządzenia – bez nich
            # dekodowanie próbek nie działa.
            self._ppk2.get_modifiers()
            self._ppk2.use_source_meter()
        except Exception as e:
            raise Ppk2Error(f"nie mogę otworzyć PPK2 na '{port}': {e}")
        self.port = port

    def set_voltage(self, millivolts):
        self._ppk2.set_source_voltage(int(millivolts))

    def dut_power(self, on):
        self._ppk2.toggle_DUT_power("ON" if on else "OFF")
        # Chwila na ustabilizowanie zasilania płytki.
        time.sleep(0.3)

    def start(self):
        self._ppk2.start_measuring()
        self._measuring = True
        time.sleep(0.05)
        self._ppk2.get_data()      # odrzuć pierwszy, śmieciowy bufor

    def read(self):
        """Nowe próbki [µA] od poprzedniego read() (może być pusto –
        wołający sam decyduje, ile spać między odczytami)."""
        data = self._ppk2.get_data()
        if not data:
            return np.empty(0, np.float32)
        samples, _digital = self._ppk2.get_samples(data)
        return np.asarray(samples, np.float32)

    def stop(self):
        if self._measuring:
            self._ppk2.stop_measuring()
            self._measuring = False

    def close(self):
        if self._ppk2 is None:
            return
        try:
            self.stop()
            self.dut_power(False)
        except Exception:
            pass               # sprzątanie po błędzie – nie maskuj oryginału
        self._ppk2 = None
