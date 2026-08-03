# ============================================================
#  autorun/mock.py – symulacja sprzętu (praca bez PPK2 i bez płytki)
# ============================================================
# Atrapy pozwalają przejść CAŁY przebieg autonomiczny bez PPK2, bez
# programatora i bez płytki: rozwijanie interfejsu, raportu i logiki
# planu nie wymaga wtedy leżącego na biurku sprzętu. Symulacja jest
# świadomym wyborem operatora (checkbox w kreatorze), a jej wyniki są
# odgrodzone od prawdziwych: osobny katalog sesji (reports/sessions-mock)
# i ZERO wierszy w dzienniku pomiarów.
#
# --- Skąd bierze się skrót czasu ---
# Okno pomiaru zamyka się, gdy minie duration_s ALBO gdy zbierzemy
# duration_s * rate próbek (engine._measure). Czas w meta.json, granice
# tierów i ładunek w µC liczą się z LICZBY PRÓBEK
# (SessionWriter.elapsed_s = samples_written / sample_rate), a nie z
# zegara. Wystarczy więc, że atrapa wypluwa próbki szybciej niż sprzęt:
# zapisane dane opisują pełne 20 minut z planu, a realnie zajmuje to
# MOCK_WINDOW_S sekund. Silnik skaluje jeszcze zegar odliczania i
# oczekiwania (engine._time_scale), żeby UI pokazywało czasy z planu.
#
# KAŻDY pomiar w symulacji trwa MOCK_WINDOW_S sekund realnych – nie
# ułamek czasu z planu. Przy stałym mnożniku okno 30 s domykało się po
# 0,3 s, czyli szybciej, niż da się cokolwiek zobaczyć na ekranie; przy
# stałym oknie tempo pracy nie zależy od tego, co wpisano w kartę.
# Skrót jest więc liczony PER KROK: czas_z_planu / MOCK_WINDOW_S.
# Pomiar krótszy niż MOCK_WINDOW_S leci w czasie realnym (rozciąganie go
# byłoby czekaniem dłuższym niż sam plan).
#
# --- Dlaczego atrapa ma niską częstotliwość ---
# Przy 100 kS/s trzeba by generować miliony próbek na sekundę (dziesiątki
# MB/s do kaskady tierów) – nierealne. Atrapa trzyma więc dwa sufity:
# MOCK_RATE na częstotliwość i MOCK_MAX_SAMPLES na liczbę próbek jednego
# pomiaru. Razem ze stałym oknem dają STAŁE tempo generowania
# (MOCK_MAX_SAMPLES / MOCK_WINDOW_S), niezależne od długości pomiaru.
# Cena: długie okna dostają niższą częstotliwość (8 h -> ~70 S/s), więc
# meta.json takiej sesji mówi 70, a nie 100000. Karta z próbkowaniem
# poniżej częstotliwości atrapy działa dokładnie (decymacja liczy się
# normalnie), wyższe wartości są przycinane.

import re
import threading
import time

import numpy as np

from .ppk2 import check_voltage_mV

MOCK_WINDOW_S = 10.0        # ile REALNIE trwa jeden pomiar w symulacji
MOCK_RATE = 2000            # sufit „sprzętowej” częstotliwości [próbki/s]
MOCK_MAX_SAMPLES = 2_000_000    # sufit próbek jednego pomiaru (dane + CPU)
# Sufit jednego odczytu: po zacięciu (GC, zajęte UI) nie oddajemy nagle
# milionów próbek. Zaległość zostaje w liczniku i nadgania się przy
# kolejnych odczytach – okno i tak domknie licznik próbek.
MAX_CHUNK = 200_000

# --- Model przebiegu LPN: podłoga uśpienia + piki wybudzeń ---
# Wartości dobrane tak, żeby średnie wychodziły w okolicach tego, co
# realnie mierzymy na BTZ: ładunek jednego wybudzenia to ~825 µC, więc
# poll co 30 s daje ~28 µA, co 60 s ~14 µA, a co 120 s ~7 µA.
BASELINE_UA = 1.0           # podłoga, gdy scenariusz nie mówi inaczej
PEAK_UA = 5500.0            # radio nadaje (Poll + okno odbiorcze)
ACTIVE_S = 0.15             # ile trwa jedno wybudzenie
PERIOD_S = 30.0             # odstęp wybudzeń, gdy nie ma parametru serii
# Co któryś Poll ginie i leci retransmisja – wybudzenie jest wtedy dwa
# razy dłuższe. To główne źródło ROZRZUTU między powtórkami tego samego
# pomiaru (w realnych danych: 7 µA vs 28 µA na tych samych ustawieniach),
# więc atrapa musi je mieć – inaczej powtórki x2–x5 dawałyby identyczne
# liczby i nie dałoby się na nich niczego sprawdzić.
RETRY_PROB = 0.15


class MockConfig:
    """Parametry symulacji. `seed=None` (domyślnie) znaczy, że każdy
    pomiar dostaje własny losowy przebieg – dzięki temu powtórki jednej
    karty różnią się jak na sprzęcie. Test podaje seed i ma powtarzalność.

    Domyślne wartości czytamy ze stałych modułu przy KAŻDYM tworzeniu
    (nie w podpisie), żeby dało się je podmienić w testach – inaczej cała
    suite czekałaby po 10 s na każdy zasymulowany pomiar."""

    def __init__(self, window_s=None, hw_rate=None, max_samples=None,
                 seed=None):
        self.window_s = max(0.01, float(MOCK_WINDOW_S if window_s is None
                                        else window_s))
        self.hw_rate = max(1, int(MOCK_RATE if hw_rate is None else hw_rate))
        self.max_samples = max(1, int(MOCK_MAX_SAMPLES if max_samples is None
                                      else max_samples))
        self.seed = seed

    def __repr__(self):
        return (f"MockConfig(window_s={self.window_s:g}, "
                f"hw_rate={self.hw_rate}, seed={self.seed})")

    def scale_for(self, duration_s):
        """Ile sekund PRZEBIEGU mieści się w sekundzie realnej przy oknie
        `duration_s` z planu. Pomiar krótszy niż okno symulacji leci
        w czasie realnym (skala 1.0) – nie ma po co czekać dłużej, niż
        każe plan."""
        duration_s = float(duration_s or 0.0)
        if duration_s <= self.window_s:
            return 1.0
        return duration_s / self.window_s

    def rate_for(self, duration_s):
        """Częstotliwość atrapy dla okna `duration_s`: tyle, żeby zmieścić
        się w sufitach (MOCK_RATE i MOCK_MAX_SAMPLES). Razem ze stałym
        oknem daje stałe tempo generowania próbek."""
        duration_s = float(duration_s or 0.0)
        if duration_s <= 0:
            return self.hw_rate
        return max(1, min(self.hw_rate, int(self.max_samples / duration_s)))

    @property
    def summary(self):
        """Jednolinijkowy opis do plan.log i paska w UI."""
        return f"pomiar {self.window_s:g} s zamiast czasu z planu"


# Symbole Kconfiga, które w modelu przebiegu znaczą „odstęp wybudzeń”.
# step.sweep trzyma wartość WPISANĄ w kartę, czyli sekundy (przeliczenie
# na jednostki Kconfiga robi plan.sweep_flag_value), więc bierzemy ją
# wprost. Dopasowanie po fragmencie nazwy, bo Thread i Zigbee mają
# własne symbole, a sens jest ten sam.
PERIOD_HINTS = ("POLL_TIMEOUT", "POLL_INTERVAL", "INTERVAL", "PERIOD",
                "POLL")
_CURRENT_RE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*(m|µ|u|n)?\s*A",
                         re.IGNORECASE)
_UNIT_UA = {"m": 1000.0, "µ": 1.0, "u": 1.0, "n": 0.001}


def baseline_from_expected(text, default=BASELINE_UA):
    """Podłoga prądu z pola `expected` scenariusza ('~0.5 uA (DK …)' ->
    0.5). Bierzemy PIERWSZĄ liczbę z jednostką – pole jest opisowe i
    często ma w nawiasie drugi pomiar. Brak liczby = `default`."""
    m = _CURRENT_RE.search(str(text or ""))
    if not m:
        return default
    try:
        value = float(m.group(1).replace(",", "."))
    except ValueError:
        return default
    unit = (m.group(2) or "µ").lower()
    return max(0.0, value * _UNIT_UA.get(unit, 1.0))


def period_from_sweep(sweep, default=PERIOD_S):
    """Odstęp wybudzeń z parametru serii, jeśli któraś oś nim jest.
    Dzięki temu sweep po pollu daje MALEJĄCĄ krzywą średnich i widać, czy
    raport, wykres i sortowanie po parametrze działają."""
    for param, value in sweep or ():
        if not any(hint in str(param).upper() for hint in PERIOD_HINTS):
            continue
        try:
            secs = float(str(value).replace(",", "."))
        except ValueError:
            continue
        if secs > 0:
            return secs
    return default


class MockSampler:
    """Atrapa PPK2 o kształcie Ppk2ApiSampler (open / set_voltage /
    dut_power / start / read / stop / close + pole sample_rate).

    Napięcie sprawdzamy tym samym strażnikiem co sprzęt: ścieżka
    „ODMOWA: napięcie poza zakresem” ma się dać przejść bez PPK2, bo to
    ona chroni płytkę.

    `prepare_step(step, scen)` dostraja przebieg do mierzonego kroku
    (podłoga ze scenariusza, odstęp wybudzeń z parametru serii, nowe
    losowanie retransmisji). Prawdziwy sampler tej metody nie ma –
    silnik woła ją tylko, gdy istnieje."""

    def __init__(self, cfg=None):
        self.cfg = cfg or MockConfig()
        # Częstotliwość i skrót czasu zależą od DŁUGOŚCI mierzonego okna,
        # więc ustawia je prepare_step. Do tego czasu: sufit i czas realny.
        self.sample_rate = self.cfg.hw_rate
        self.speedup = 1.0
        self.port = "MOCK"
        self.voltage_mV = None
        self.dut = False
        self.baseline = BASELINE_UA
        self.period_s = PERIOD_S
        self.peak = PEAK_UA
        self.active_s = ACTIVE_S
        self.log = []                  # ślad wywołań (asercje w testach)
        self._running = False
        self._t0 = None
        self._emitted = 0
        self._lock = threading.Lock()
        self._seed = 0
        self._rng = np.random.default_rng(self.cfg.seed)
        self._reseed()

    # ---------- protokół samplera ----------

    def open(self):
        self.log.append("open")

    def set_voltage(self, millivolts):
        self.voltage_mV = check_voltage_mV(millivolts)
        self.log.append(f"voltage={self.voltage_mV}")

    def dut_power(self, on):
        self.dut = bool(on)
        self.log.append(f"dut={'ON' if on else 'OFF'}")

    def start(self):
        with self._lock:
            self._running = True
            self._t0 = time.monotonic()
            self._emitted = 0
        self.log.append("start")

    def read(self):
        """Próbki za wirtualny czas, który upłynął od start(): realna
        sekunda to `speedup` sekund przebiegu."""
        with self._lock:
            if not self._running:
                return np.empty(0, np.float32)
            elapsed = time.monotonic() - self._t0
            want = int(elapsed * self.sample_rate * self.speedup)
            n = min(want - self._emitted, MAX_CHUNK)
            if n <= 0:
                return np.empty(0, np.float32)
            first = self._emitted
            self._emitted += n
        # Odcięte zasilanie = brak prądu (power-cycle też ma być widoczny).
        if not self.dut:
            return np.zeros(n, np.float32)
        return self._waveform(np.arange(first, first + n, dtype=np.int64))

    def stop(self):
        with self._lock:
            self._running = False
        self.log.append("stop")

    def close(self):
        self.dut = False
        self.log.append("close")

    # ---------- model przebiegu ----------

    def prepare_step(self, step, scen):
        duration_s = getattr(step, "duration_s", 0.0)
        # Okno pomiaru ma trwać MOCK_WINDOW_S realnie, niezależnie od czasu
        # z planu: stąd skrót liczony per krok i częstotliwość dopasowana do
        # sufitu próbek. Silnik czyta sample_rate PO tym wywołaniu.
        self.sample_rate = self.cfg.rate_for(duration_s)
        self.speedup = self.cfg.scale_for(duration_s)
        self.baseline = baseline_from_expected(scen.get("expected"))
        self.period_s = period_from_sweep(getattr(step, "sweep", None))
        self._reseed()
        self.log.append(f"step baseline={self.baseline:g} "
                        f"period={self.period_s:g} rate={self.sample_rate} "
                        f"scale={self.speedup:g} seed={self._seed}")

    def _reseed(self):
        """Nowe losowanie retransmisji. Z jawnym seedem w konfiguracji
        przebieg jest powtarzalny (testy), bez niego każdy pomiar – a więc
        i każda powtórka karty – dostaje własny."""
        if self.cfg.seed is None:
            self._seed = int(self._rng.integers(1, 2**31 - 1))
        else:
            self._seed = int(self.cfg.seed)

    def _waveform(self, idx):
        """Próbki [µA] dla indeksów `idx`. Bezstanowo względem porcji:
        czy dany cykl ma retransmisję, wynika z HASZA numeru cyklu, nie z
        losowania przy odczycie – inaczej pik na granicy dwóch odczytów
        raz byłby długi, raz krótki."""
        t = idx / float(self.sample_rate)
        cycle = np.floor(t / self.period_s).astype(np.int64)
        phase = t - cycle * self.period_s
        retry = (cycle * 2654435761 + self._seed) % 1000 \
            < int(RETRY_PROB * 1000)
        active = np.where(retry, self.active_s * 2.0, self.active_s)
        out = np.full(len(idx), self.baseline, np.float32)
        np.copyto(out, np.float32(self.peak), where=phase < active)
        # Szum tylko tyle, żeby min/max i wykres nie były idealnie płaskie.
        noise = max(0.05, 0.1 * self.baseline)
        out += self._rng.normal(0.0, noise, len(idx)).astype(np.float32)
        np.maximum(out, 0.0, out=out)
        return out


# ============================================================
#  Atrapy źródeł triggera
# ============================================================
# Bez nich każdy krok w symulacji kończyłby się jako `trigger_timeout`:
# pomiar startuje na WZORCU z logu (dongiel/RTT) albo na pierwszym
# raporcie subskrypcji Mattera, a bez płytki nikt takiej linii nie
# wypisze. Atrapy czytają wzorzec z KROKU, który silnik właśnie wykonuje
# (`engine._cur_step`), i wstrzykują go w swój strumień.
#
# Uwaga o wzorcach RTT: trigger RTT dopasowuje REGEXEM, a etykiety
# (rtt='continuous') też. Atrapa wypisuje wzorzec dosłownie, więc łapią
# się zwykłe fragmenty tekstu ('publikacja temperatury'); wyrażenie z
# kotwicami czy klasami znaków trafi tylko przypadkiem.

# Kiedy (realne sekundy od attach) atrapa zaczyna gadać. Pierwsze
# ~0.35 s musi być CISZA: monitor dongla wyrzuca na starcie bufor sprzed
# flasha i kończy dopiero, gdy port zamilknie na STALE_QUIET_S (0.3 s).
# Bez tej ciszy drenaż trwałby do STALE_MAX_S, a linia triggera mogłaby
# w nim przepaść – dokładnie ten błąd naprawiał commit o buforze dongla.
QUIET_S = 0.35
TRIGGER_AT_S = 0.5          # kiedy pada linia z wzorcem triggera
# Odstęp kolejnych linii „życia” węzła podajemy w sekundach PRZEBIEGU, nie
# realnych: okno pomiaru w symulacji jest `speedup` razy krótsze, więc log
# liczony realnym zegarem nie zdążyłby wypisać ani jednej linii w trakcie
# pomiaru (a to on ma wyglądać na żywy – logi i etykiety RTT).
LINE_EVERY_VIRTUAL_S = 5.0

# Linie tła: tak wygląda log LPN-a i Frienda, gdy wszystko działa.
BOOT_LINES = ("*** Booting nRF Connect SDK v3.4.0 ***",
              "[SYMULACJA] mesh: inicjalizacja stosu",
              "[SYMULACJA] mesh: provisioning z retencji RAM (adres 0x0002)",
              "[SYMULACJA] lpn: friendship z 0x0001 nawiazany")
ALIVE_LINE = "[SYMULACJA] lpn: publikacja temperatury {n} (23.{n:02d} C)"


class _MockLineSource:
    """Wspólna mechanika atrap logu: harmonogram linii liczony od
    attach(), z ciszą na starcie, jedną linią zawierającą wzorzec
    triggera i potem cyklicznymi liniami przez cały pomiar."""

    # Cisza po attach() dotyczy TYLKO dongla – to jego bufor sprzed flasha
    # silnik wyrzuca, czekając na ciszę. RTT żadnego drenażu nie ma, a
    # okno pomiaru w symulacji jest krótsze niż ta cisza, więc czekanie
    # oznaczałoby pomiar bez ani jednej linii (i bez etykiet).
    quiet_s = QUIET_S

    def __init__(self, cfg, engine, trigger_types):
        self.cfg = cfg
        self.engine = engine
        self.trigger_types = trigger_types
        self.pattern = ""
        self.extra = []              # wzorce etykiet (rtt='continuous')
        self._t0 = None
        self._sent = 0
        self._fired = False
        self._alive = 0
        self._interval = LINE_EVERY_VIRTUAL_S

    def _read_step(self):
        """Wzorce z kroku, który silnik właśnie wykonuje. Fabryki są
        wołane per krok, więc to zawsze TEN krok."""
        step = getattr(self.engine, "_cur_step", None)
        if step is None:
            return
        trig = getattr(step, "trigger", None)
        if trig is not None and trig.type in self.trigger_types:
            self.pattern = trig.pattern or ""
        self.extra = [r.pattern for r in getattr(step, "labels", ()) or ()
                      if r.pattern]

    def attach(self):
        self._read_step()
        # Cisza na starcie i chwila do triggera zostają w REALNYCH
        # sekundach (drenaż buforu i czekanie na trigger też nimi chodzą),
        # a dalsze linie przeliczamy skalą czasu przebiegu.
        scale = getattr(self.engine, "_time_scale", 1.0) or 1.0
        self._interval = LINE_EVERY_VIRTUAL_S / scale
        self._t0 = time.monotonic()
        self._sent = 0
        self._fired = False
        self._alive = 0

    def detach(self):
        self._t0 = None

    def readline(self, timeout_s=1.0):
        """Kolejna linia albo None po timeout – jak prawdziwy czytnik."""
        deadline = time.monotonic() + timeout_s
        while True:
            line = self._next_line()
            if line is not None:
                return line
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))

    def _next_line(self):
        if self._t0 is None:
            return None
        elapsed = time.monotonic() - self._t0
        if elapsed < self.quiet_s:
            return None
        if self._sent < len(BOOT_LINES):
            self._sent += 1
            return BOOT_LINES[self._sent - 1]
        if not self._fired and elapsed >= TRIGGER_AT_S:
            self._fired = True
            if self.pattern:
                return f"[SYMULACJA] {self.pattern}"
        # Bez wzorca (rtt='continuous', trigger delay/chip) nie ma na co
        # czekać – linie życia lecą od razu, inaczej krótkie okno pomiaru
        # skończyłoby się przed pierwszą z nich.
        start_s = TRIGGER_AT_S if self.pattern else self.quiet_s
        due = start_s + (self._alive + 1) * self._interval
        if elapsed < due:
            return None
        self._alive += 1
        # Wzorce etykiet wplatamy między linie życia, żeby przy
        # rtt='continuous' powstały adnotacje sesji.
        if self.extra:
            pat = self.extra[(self._alive - 1) % len(self.extra)]
            if self._alive % 2 == 0:
                return f"[SYMULACJA] {pat}"
        return ALIVE_LINE.format(n=self._alive)


class MockSerialReader(_MockLineSource):
    """Atrapa dongla (kształt SerialLineReader): port jest fikcyjny, więc
    attach() nigdy nie podnosi DongleError."""

    def __init__(self, cfg, engine, port=""):
        super().__init__(cfg, engine, trigger_types=("serial",))
        self.port = port or "MOCK"
        self.baud = 0


class MockRttReader(_MockLineSource):
    """Atrapa konsoli RTT (kształt PylinkRttReader) – bez J-Linka."""

    quiet_s = 0.0               # RTT nie ma buforu do wyrzucenia

    def __init__(self, cfg, engine, device=""):
        super().__init__(cfg, engine, trigger_types=("rtt",))
        self.device = device or "MOCK"


class MockChipSession:
    """Atrapa triggera 'chip' (kształt engine._ChipSession): udaje
    parowanie Matter i subskrypcję atrybutu, po CHIP_FIRST_VALUE_S
    zgłasza pierwszy raport. Linie idą do chip.log i do panelu tak jak
    ze skryptu, żeby zakładka Thread wyglądała bez zmian."""

    LINES = ("[SYMULACJA] chip-tool: pairing ble-thread …",
             "[SYMULACJA] chip-tool: commissioning complete",
             "[SYMULACJA] chip-tool: subscribe-by-id 0x0402 0x0000 …",
             "[SYMULACJA] subscription established")
    FIRST_VALUE_S = 0.4         # realne sekundy do pierwszego raportu

    def __init__(self, cfg, engine, log_path, idx, scenario):
        self.cfg = cfg
        self.engine = engine
        self.log_path = log_path
        self.idx = idx
        self.scenario = scenario
        self.first_value = threading.Event()
        self.value = ""
        self.proc = None
        self._t0 = None

    def start(self):
        self._t0 = time.monotonic()
        with open(self.log_path, "a", encoding="utf-8") as log:
            for line in self.LINES:
                log.write(line + "\n")
                self.engine._emit("line", self.idx, self.scenario, line)

    def wait_first_value(self, timeout_s, check_cancel):
        """Podpis jak w _ChipSession. Czekanie skracamy skalą czasu –
        parowanie i tak jest udawane."""
        scale = getattr(self.engine, "_time_scale", 1.0) or 1.0
        deadline = time.monotonic() + min(timeout_s,
                                          self.FIRST_VALUE_S / scale + 1.0)
        while time.monotonic() < deadline:
            check_cancel()
            time.sleep(0.02)
            if time.monotonic() - self._t0 >= self.FIRST_VALUE_S / scale:
                break
        self.value = "2312"
        self.first_value.set()
        line = f"FIRST-VALUE {self.value}"
        with open(self.log_path, "a", encoding="utf-8") as log:
            log.write(f"[SYMULACJA] {line}\n")
        self.engine._emit("line", self.idx, self.scenario,
                          f"[SYMULACJA] {line}")

    def stop(self):
        self._t0 = None


# ============================================================
#  Fabryki dla AutoRunnera
# ============================================================

def mock_sampler_factory(cfg):
    """Fabryka samplera dla AutoRunnera (podpis jak default_sampler_factory)."""
    def factory(plan):
        return MockSampler(cfg)
    return factory


def mock_factories(cfg, engine):
    """Komplet fabryk symulacji: (sampler, rtt, serial, chip). Każda ma
    podpis swojej prawdziwej odpowiedniczki, więc silnik nie wie, że
    rozmawia z atrapą."""
    return (mock_sampler_factory(cfg),
            lambda profile: MockRttReader(cfg, engine),
            lambda port: MockSerialReader(cfg, engine, port),
            lambda cmd, eng, log_path, idx, scenario: MockChipSession(
                cfg, eng, log_path, idx, scenario))
