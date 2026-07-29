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

import threading
import time
from collections import deque

import numpy as np

from .plan import VOLTAGE_MAX_MV, VOLTAGE_MIN_MV

SAMPLE_RATE = 100_000     # PPK2 sampluje stałe 100 kS/s
BYTES_PER_SAMPLE = 4      # jedna próbka = 4 bajty (~400 kB/s strumienia)
# Sufit bufora surowych bajtów. Przy zdrowym przebiegu leży tam garść
# milisekund; tyle danych oznacza, że konsument stanął – i lepiej zgłosić
# błąd, niż po cichu mielić dane sprzed pół minuty.
MAX_BUFFER_S = 30
# Jak długo wątek drenujący czeka na bajty, zanim sprawdzi, czy ma skończyć.
DRAIN_TIMEOUT_S = 0.1
# Odstęp po komendzie REGULATOR_SET. PPK2 nie potwierdza jej niczym, a dwie
# komendy wysłane pod rząd (bezpieczne minimum przy open() + właściwe napięcie
# kroku milisekundy później) potrafią skończyć się tak, że druga nie dojdzie
# do regulatora – płytka jedzie wtedy na napięciu z pierwszej. Nie ma odczytu
# napięcia z urządzenia, więc jedyne, co możemy zrobić, to nie wysyłać ich
# seriami i dać regulatorowi chwilę.
VOLTAGE_SETTLE_S = 0.2


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
    read()* -> stop() -> close().

    PPK2 nadaje SWOBODNIE 100 kS/s (~400 kB/s) – bez kontroli przepływu,
    bez retransmisji i bez bufora na urządzeniu. Kto nie opróżni portu na
    czas, tego próbki przepadają na zawsze. Bufor tty starcza na kilkanaście
    milisekund, a dekodowanie w ppk2-api idzie pętlą Pythona po KAŻDEJ
    próbce (~1,5 ms na 10 ms strumienia) – gdy port drenowała ta sama pętla,
    która dekoduje, decymuje i zapisuje tiery, przez większość czasu portu
    nie czytał NIKT. Stąd braki rzędu dziesiątek procent.

    Dlatego port drenuje osobny wątek, który nie robi NIC poza ser.read()
    do bufora bajtów. Dekodowanie zostaje u konsumenta (read()) – wartości
    liczy dokładnie ten sam kod ppk2-api co dotąd."""

    sample_rate = SAMPLE_RATE

    def __init__(self, port=""):
        self._port_hint = port
        self._ppk2 = None
        self._measuring = False
        self._dut_on = False
        self._last_voltage_mv = None
        # Wątek drenujący port + jego bufor surowych bajtów.
        self._reader = None
        self._reader_stop = threading.Event()
        self._reader_error = None
        self._chunks = deque()
        self._buffered = 0
        self._overflow = False
        self._lock = threading.Lock()
        self._max_buffered = 0        # diagnostyka: szczyt zajętości bufora

    def open(self):
        from ppk2_api.ppk2_api import PPK2_API
        port = find_ppk2(self._port_hint)
        try:
            self._ppk2 = PPK2_API(port)
            # Timeout na porcie: bez niego blokujący ser.read() w wątku
            # drenującym nie wróciłby, gdy urządzenie przestanie nadawać –
            # i stop() wisiałby na join().
            try:
                self._ppk2.ser.timeout = DRAIN_TIMEOUT_S
            except Exception:
                pass
            # PPK2 mogło zostać w trakcie nadawania próbek (poprzednia sesja
            # przerwana Esc albo ubity proces) – ucisz je, zanim cokolwiek
            # z niego przeczytamy.
            self._quiesce()
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

    def _drain(self):
        """Wyrzuć wszystko, co urządzenie zdążyło nadać. Po AVERAGE_STOP
        PPK2 nadaje jeszcze przez chwilę – zaległe próbki wymieszałyby się
        z odpowiedzią na następną komendę."""
        ser = getattr(self._ppk2, "ser", None)
        if ser is None:
            return
        for _ in range(5):
            try:
                ser.reset_input_buffer()
            except Exception:
                return
            time.sleep(0.05)
            if not getattr(ser, "in_waiting", 0):
                return

    def _quiesce(self):
        """Ucisz urządzenie: zatrzymaj nadawanie i opróżnij bufor. Wołane
        przy otwarciu ORAZ przy zamknięciu – bez tego PPK2 zostawione
        w środku strumienia (sesja przerwana Esc) nie umiało oddać
        metadanych przy następnym otwarciu i jedynym ratunkiem było
        fizyczne przepięcie kabla USB."""
        try:
            self._ppk2.stop_measuring()
        except Exception:
            pass
        self._measuring = False
        self._drain()

    def _load_calibration(self):
        """Niezawodne wczytanie modyfikatorów kalibracji. Odczyt metadanych
        w ppk2-api bywa wyścigowy (szuka 'END' w 5 próbach), a przy porażce
        get_modifiers() zwraca None i BIBLIOTEKA CICHO zostaje przy domyślnych,
        błędnych stałych. Uciszamy urządzenie, ponawiamy, a trwały brak
        kalibracji traktujemy jak twardy błąd (lepiej nie mierzyć niż mierzyć
        źle)."""
        last = None
        for _ in range(15):
            try:
                # Ponowne uciszenie, a nie samo czyszczenie bufora: jeśli
                # urządzenie wciąż nadaje próbki, sam flush nic nie da –
                # metadane znowu utoną w strumieniu.
                self._quiesce()
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
        # Chwila na dojście komendy do regulatora – patrz VOLTAGE_SETTLE_S.
        time.sleep(VOLTAGE_SETTLE_S)

    def dut_power(self, on):
        self._ppk2.toggle_DUT_power("ON" if on else "OFF")
        self._dut_on = bool(on)
        # Chwila na ustabilizowanie zasilania płytki.
        time.sleep(0.3)

    def start(self):
        # Nigdy dwa wątki na jednym porcie (pauza/wznowienie, powtórzony
        # pomiar) – poprzedni musi być domknięty.
        self._join_reader()
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
        # Ta sama zasada dla stanu filtra szpilek/średniej kroczącej w
        # ppk2-api: przeniesiony z poprzedniego pomiaru dokładałby do
        # pierwszych próbek poziom sprzed przerwy (biblioteka sama go nie
        # zeruje). None = "zainicjuj pierwszą próbką".
        self._ppk2.rolling_avg = None
        self._ppk2.rolling_avg4 = None
        self._ppk2.prev_range = None
        self._ppk2.consecutive_range_samples = 0
        self._ppk2.after_spike = 0
        with self._lock:
            self._chunks.clear()
            self._buffered = 0
            self._max_buffered = 0
        self._overflow = False
        self._reader_error = None
        self._ppk2.start_measuring()
        self._measuring = True
        # Wątek startuje PO start_measuring, żeby nie buforował ogona
        # sprzed pomiaru.
        self._reader_stop.clear()
        self._reader = threading.Thread(target=self._drain_loop, daemon=True,
                                        name="ppk2-drain")
        self._reader.start()

    def _drain_loop(self):
        """Jedyne zadanie: zabierać bajty z portu i odkładać do bufora.
        Żadnego dekodowania ani zapisu – każda milisekunda spędzona tu na
        czymkolwiek innym to ryzyko przepełnienia bufora tty. `ser.read()`
        blokuje w jądrze i oddaje GIL, więc wątek nie kręci się na pusto."""
        ser = self._ppk2.ser
        cap = MAX_BUFFER_S * SAMPLE_RATE * BYTES_PER_SAMPLE
        while not self._reader_stop.is_set():
            try:
                data = ser.read(max(1, ser.in_waiting))
            except Exception as e:
                self._reader_error = e
                return
            if not data:
                continue
            with self._lock:
                if self._buffered + len(data) > cap:
                    self._overflow = True
                    continue          # bufor pełny – konsument stanął
                self._chunks.append(data)
                self._buffered += len(data)
                self._max_buffered = max(self._max_buffered, self._buffered)

    def read(self):
        """Nowe próbki [µA] od poprzedniego read() (może być pusto –
        wołający sam decyduje, ile spać między odczytami). Bajty zebrał
        już wątek drenujący; tutaj tylko je dekodujemy."""
        if self._reader_error is not None:
            err, self._reader_error = self._reader_error, None
            raise Ppk2Error(f"odczyt z portu PPK2 przerwany: {err}")
        if self._overflow:
            self._overflow = False
            raise Ppk2Error(
                f"bufor PPK2 przepełniony (>{MAX_BUFFER_S} s surowych "
                "danych) – konsument nie nadąża z przetwarzaniem")
        with self._lock:
            if not self._chunks:
                return np.empty(0, np.float32)
            data = b"".join(self._chunks)
            self._chunks.clear()
            self._buffered = 0
        samples, _digital = self._ppk2.get_samples(data)
        return np.asarray(samples, np.float32)

    def _join_reader(self):
        self._reader_stop.set()
        if self._reader is not None:
            # Join z zapasem na jeden pełny timeout blokującego read().
            self._reader.join(timeout=DRAIN_TIMEOUT_S * 10 + 1)
            self._reader = None

    def stop(self):
        # NAJPIERW wątek: dopóki żyje, walczyłby o port z stop_measuring()
        # i z opróżnianiem bufora przy zamykaniu.
        self._join_reader()
        if self._measuring:
            self._ppk2.stop_measuring()
            self._measuring = False

    def close(self):
        if self._ppk2 is None:
            return
        # Każdy krok w osobnym try: gdy padnie stop(), zasilanie płytki i tak
        # MUSI zostać odcięte.
        try:
            self.stop()
        except Exception:
            pass               # sprzątanie po błędzie – nie maskuj oryginału
        try:
            self.dut_power(False)      # ODETNIJ zasilanie płytki
        except Exception:
            pass
        # Zostaw urządzenie ciche i z pustym buforem. Zamknięcie portu
        # w środku strumienia próbek (przerwanie pomiaru Esc) zostawiało
        # PPK2 w stanie, w którym następny pomiar nie ruszał bez fizycznego
        # restartu urządzenia.
        try:
            self._quiesce()
        except Exception:
            pass
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
        self._dut_on = False
