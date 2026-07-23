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

from .plan import VOLTAGE_MAX_MV, VOLTAGE_MIN_MV

SAMPLE_RATE = 100_000     # PPK2 sampluje stałe 100 kS/s


class Ppk2Error(RuntimeError):
    pass


def check_voltage_mV(mv):
    """Twardy strażnik napięcia [VOLTAGE_MIN_MV, VOLTAGE_MAX_MV] tuż przed
    komendą do PPK2 – OSTATNIA linia obrony przed podaniem groźnego
    napięcia na płytkę (biblioteka ppk2-api klampuje dopiero do 5000 mV).
    Zwraca int mV albo Ppk2Error; nigdy nie klampuje po cichu."""
    try:
        mv = int(mv)
    except (TypeError, ValueError):
        raise Ppk2Error(f"napięcie '{mv}' nie jest liczbą mV")
    if not (VOLTAGE_MIN_MV <= mv <= VOLTAGE_MAX_MV):
        raise Ppk2Error(
            f"ODMOWA: napięcie {mv} mV poza bezpiecznym zakresem "
            f"{VOLTAGE_MIN_MV}–{VOLTAGE_MAX_MV} mV – nie podaję na płytkę "
            "(twardy limit ochrony sprzętu)")
    return mv


def find_ppk2(port=""):
    """Port szeregowy PPK2: jawnie wskazany albo autodetekcja
    (PPK2_API.list_devices). Czytelne błędy przy braku/wielu.

    Uwaga: jedno fizyczne PPK2 wystawia KILKA interfejsów CDC, więc na
    macOS/Linux pojawia się kilka /dev/... o TYM SAMYM numerze seryjnym.
    Grupujemy porty po numerze seryjnym, żeby nie brać interfejsów jednego
    urządzenia za 'kilka PPK2'; przy jednym urządzeniu zwracamy pierwszy
    (deterministycznie) port."""
    if port:
        return port
    from ppk2_api.ppk2_api import PPK2_API
    from serial.tools import list_ports
    found = PPK2_API.list_devices()
    if not found:
        raise Ppk2Error(
            "nie znaleziono PPK2. Podłącz Power Profiler Kit II po USB "
            "(port MCU) albo wskaż port w planie: ppk2_port = \"...\"")
    # Mapa port -> numer seryjny = fizyczne urządzenie (kilka portów o tym
    # samym serialu to jedno PPK2).
    serial_of = {p.device: p.serial_number for p in list_ports.comports()}
    by_device = {}
    for dev in found:
        by_device.setdefault(serial_of.get(dev) or dev, []).append(dev)
    if len(by_device) > 1:
        reps = sorted(sorted(ports)[0] for ports in by_device.values())
        raise Ppk2Error(
            f"znaleziono kilka PPK2 ({', '.join(reps)}) – wskaż port "
            "w planie: ppk2_port = \"...\"")
    return sorted(next(iter(by_device.values())))[0]


class Ppk2ApiSampler:
    """Właściwy sampler na bibliotece ppk2-api. Cykl życia:
    open() -> set_voltage() -> dut_power(True) -> start() ->
    read()* -> stop() -> close()."""

    sample_rate = SAMPLE_RATE

    def __init__(self, port=""):
        self._port_hint = port
        self._ppk2 = None
        self._measuring = False
        self._dut_on = False
        self._last_voltage_mv = None

    def open(self):
        from ppk2_api.ppk2_api import PPK2_API
        port = find_ppk2(self._port_hint)
        try:
            self._ppk2 = PPK2_API(port)
            # Modyfikatory kalibracyjne z urządzenia – BEZ nich dekodowanie
            # liczy prąd z domyślnych, błędnych stałych (odczyt zawyżony
            # o rzędy wielkości, np. mA zamiast µA).
            self._load_calibration()
            self._ppk2.use_source_meter()
            # BEZPIECZNY STAN STARTOWY: najpierw odetnij zasilanie DUT,
            # potem ustaw napięcie na dolny bezpieczny limit. Dzięki temu
            # żaden kod nie poda na płytkę nieznanego napięcia (np.
            # pozostałego z poprzedniej sesji PPK2) – silnik i tak nadpisze
            # je właściwą wartością przed włączeniem zasilania.
            self._ppk2.toggle_DUT_power("OFF")
            self._dut_on = False
            self.set_voltage(VOLTAGE_MIN_MV)
        except Ppk2Error:
            raise
        except Exception as e:
            raise Ppk2Error(f"nie mogę otworzyć PPK2 na '{port}': {e}")
        self.port = port

    def _load_calibration(self):
        """Niezawodne wczytanie modyfikatorów kalibracji. Odczyt metadanych
        w ppk2-api bywa wyścigowy (szuka 'END' w 5 próbach), a przy porażce
        get_modifiers() zwraca None i BIBLIOTEKA CICHO zostaje przy domyślnych,
        błędnych stałych. Czyścimy bufor, ponawiamy, a trwały brak kalibracji
        traktujemy jak twardy błąd (lepiej nie mierzyć niż mierzyć źle)."""
        ser = getattr(self._ppk2, "ser", None)
        last = None
        for _ in range(15):
            try:
                if ser is not None:
                    ser.reset_input_buffer()
                if self._ppk2.get_modifiers():      # True dopiero po sparsie
                    return
            except Exception as e:                  # port jeszcze niegotowy
                last = e
            time.sleep(0.2)
        raise Ppk2Error(
            "nie udało się wczytać kalibracji PPK2 (metadata). Odłącz PPK2 od "
            "nRF Connect / Power Profiler (zajmuje port) i podłącz ponownie"
            + (f" [{last}]" if last else ""))

    def set_voltage(self, millivolts):
        # Twardy limit PRZED komendą do urządzenia – gdyby walidacja planu
        # została ominięta, i tak nie podamy groźnego napięcia na płytkę.
        mv = check_voltage_mV(millivolts)
        self._ppk2.set_source_voltage(mv)
        self._last_voltage_mv = mv

    def dut_power(self, on):
        self._ppk2.toggle_DUT_power("ON" if on else "OFF")
        self._dut_on = bool(on)
        # Chwila na ustabilizowanie zasilania płytki.
        time.sleep(0.3)

    def start(self):
        # WYRÓWNANIE strumienia: PPK2 nadaje ciągłe próbki po 4 bajty, a
        # get_samples() trzyma ciągłość przez `remainder`. Zaległe bajty
        # (ogon metadanych / poprzedniej sesji) albo niepełna reszta po
        # stop() przesunęłyby próbki o 1–3 bajty = KOMPLETNIE błędne odczyty.
        # Dlatego przed startem czyścimy bufor portu i zerujemy remainder.
        ser = getattr(self._ppk2, "ser", None)
        if ser is not None:
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
        self._ppk2.remainder = {"sequence": b"", "len": 0}
        self._ppk2.start_measuring()
        self._measuring = True

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
            self.dut_power(False)      # ODETNIJ zasilanie płytki
        except Exception:
            pass               # sprzątanie po błędzie – nie maskuj oryginału
        # Jawnie zwolnij port szeregowy: ppk2-api nie ma metody close/
        # disconnect (port zamyka dopiero __del__/GC), a bez tego kolejne
        # otwarcie PPK2 potrafi trafić na zajęty port.
        try:
            ser = getattr(self._ppk2, "ser", None)
            if ser is not None:
                ser.close()
        except Exception:
            pass
        self._ppk2 = None
