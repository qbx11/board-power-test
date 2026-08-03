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
# zegara. Wystarczy więc, że atrapa wypluwa próbki `speedup` razy
# szybciej niż sprzęt: zapisane dane opisują pełne 20 minut, a realnie
# zajmuje to 12 sekund. Silnik skaluje jeszcze zegar odliczania i
# oczekiwania (engine._time_scale), żeby UI pokazywało czasy z planu.
#
# --- Dlaczego atrapa ma niską częstotliwość ---
# Przy 100 kS/s i skrócie ×100 trzeba by generować 10 mln próbek na
# sekundę (40 MB/s do kaskady tierów) – nierealne. Atrapa raportuje
# więc MOCK_RATE (2 kS/s), co przy ×100 daje 200 tys. próbek/s realnie.
# Karta z próbkowaniem <= MOCK_RATE działa dokładnie (decymacja liczy
# się normalnie); wyższe wartości są przycięte do MOCK_RATE, więc
# meta.json takiej sesji mówi 2000, a nie 100000.

import re
import threading
import time

import numpy as np

from .ppk2 import check_voltage_mV

MOCK_RATE = 2000            # „sprzętowa” częstotliwość atrapy [próbki/s]
MOCK_SPEEDUP = 100.0        # ile razy szybciej niż realny czas
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
    karty różnią się jak na sprzęcie. Test podaje seed i ma powtarzalność."""

    def __init__(self, speedup=MOCK_SPEEDUP, hw_rate=MOCK_RATE, seed=None):
        self.speedup = max(1.0, float(speedup))
        self.hw_rate = max(1, int(hw_rate))
        self.seed = seed

    def __repr__(self):
        return (f"MockConfig(speedup={self.speedup:g}, "
                f"hw_rate={self.hw_rate}, seed={self.seed})")

    @property
    def summary(self):
        """Jednolinijkowy opis do plan.log i paska w UI."""
        return f"×{self.speedup:g}, {self.hw_rate} S/s"


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
        self.sample_rate = self.cfg.hw_rate
        self.speedup = self.cfg.speedup
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
        self.baseline = baseline_from_expected(scen.get("expected"))
        self.period_s = period_from_sweep(getattr(step, "sweep", None))
        self._reseed()
        self.log.append(f"step baseline={self.baseline:g} "
                        f"period={self.period_s:g}")

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


def mock_sampler_factory(cfg):
    """Fabryka samplera dla AutoRunnera (podpis jak default_sampler_factory)."""
    def factory(plan):
        return MockSampler(cfg)
    return factory
