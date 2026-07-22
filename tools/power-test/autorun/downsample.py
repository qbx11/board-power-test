# ============================================================
#  autorun/downsample.py – składanie próbek w tiery min/avg/max
# ============================================================
# PPK2 daje ~100 000 próbek/s – wielogodzinny pomiar to miliardy
# punktów. Zamiast rysować (i trzymać) wszystkie, składamy je ONLINE
# w rekordy (min, avg, max, liczba_próbek) o rosnących oknach czasowych:
# okno bazowe (dom. 1 ms) i kolejne tiery ×10 (10 ms, 100 ms, 1 s).
# Piki prądu nie giną (min/max zostają), a wykres dowolnego zoomu
# czyta zawsze O(pikseli) rekordów z właściwego tieru.

import numpy as np

# Rekord tieru: 16 bajtów, czas niejawny (indeks rekordu × okno).
RECORD_DTYPE = np.dtype([("min", "<f4"), ("avg", "<f4"),
                         ("max", "<f4"), ("n", "<u4")])
TIER_FANOUT = 10      # ile rekordów niższego tieru składa się w jeden
TIER_LEVELS = 4       # okno bazowe + 3 kolejne (1 ms -> 10/100/1000 ms)


def tier_windows_ms(base_window_ms, levels=TIER_LEVELS):
    """Okna czasowe tierów [ms], od bazowego w górę (×10)."""
    return [base_window_ms * TIER_FANOUT ** i for i in range(levels)]


def tier_name(window_ms):
    """Nazwa pliku tieru: tier_1ms / tier_100ms / tier_1s / tier_10s."""
    if window_ms % 1000 == 0:
        return f"tier_{window_ms // 1000}s"
    return f"tier_{window_ms}ms"


class BaseTier:
    """Składanie surowych próbek (float µA) w rekordy okna bazowego.
    push() przyjmuje kolejne tablice dowolnej długości i zwraca komplet
    PEŁNYCH rekordów; niedomknięte okno czeka w buforze na kolejne
    próbki (albo na flush() przy końcu pomiaru)."""

    def __init__(self, samples_per_window):
        self.win = int(samples_per_window)
        if self.win < 1:
            raise ValueError("okno tieru musi mieć >= 1 próbkę")
        self._rest = np.empty(0, np.float32)

    def push(self, samples):
        buf = np.concatenate([self._rest,
                              np.asarray(samples, np.float32)])
        full = len(buf) // self.win
        self._rest = buf[full * self.win:]
        if not full:
            return np.empty(0, RECORD_DTYPE)
        head = buf[:full * self.win].reshape(full, self.win)
        out = np.empty(full, RECORD_DTYPE)
        out["min"] = head.min(axis=1)
        out["avg"] = head.mean(axis=1)
        out["max"] = head.max(axis=1)
        out["n"] = self.win
        return out

    def flush(self):
        """Domknij niepełne okno na końcu pomiaru (rekord z mniejszym
        `n` – średnia liczona z tego, co faktycznie przyszło)."""
        if not len(self._rest):
            return np.empty(0, RECORD_DTYPE)
        out = np.empty(1, RECORD_DTYPE)
        out["min"] = self._rest.min()
        out["avg"] = self._rest.mean()
        out["max"] = self._rest.max()
        out["n"] = len(self._rest)
        self._rest = np.empty(0, np.float32)
        return out


class FoldTier:
    """Składanie rekordów niższego tieru w rekordy ×TIER_FANOUT:
    min z minimów, max z maksimów, średnia ważona liczbą próbek."""

    def __init__(self, fanout=TIER_FANOUT):
        self.fanout = int(fanout)
        self._rest = np.empty(0, RECORD_DTYPE)

    def push(self, records):
        buf = np.concatenate([self._rest, records])
        full = len(buf) // self.fanout
        self._rest = buf[full * self.fanout:]
        if not full:
            return np.empty(0, RECORD_DTYPE)
        return self._fold(buf[:full * self.fanout]
                          .reshape(full, self.fanout))

    def flush(self):
        if not len(self._rest):
            return np.empty(0, RECORD_DTYPE)
        out = self._fold(self._rest.reshape(1, len(self._rest)))
        self._rest = np.empty(0, RECORD_DTYPE)
        return out

    @staticmethod
    def _fold(grid):
        out = np.empty(len(grid), RECORD_DTYPE)
        n = grid["n"].astype(np.float64)
        total = n.sum(axis=1)
        out["min"] = grid["min"].min(axis=1)
        out["max"] = grid["max"].max(axis=1)
        out["avg"] = (grid["avg"] * n).sum(axis=1) / np.maximum(total, 1)
        out["n"] = total
        return out


class Cascade:
    """Pełna kaskada tierów: surowe próbki -> lista (okno_ms, rekordy)
    dla każdego tieru, gotowa do dopisania do plików sesji."""

    def __init__(self, sample_rate, base_window_ms, levels=TIER_LEVELS):
        base_samples = round(sample_rate * base_window_ms / 1000)
        self.windows_ms = tier_windows_ms(base_window_ms, levels)
        self._base = BaseTier(base_samples)
        self._folds = [FoldTier() for _ in range(levels - 1)]

    def push(self, samples):
        """-> [(okno_ms, np.ndarray[RECORD_DTYPE]), ...] po jednym
        wpisie na tier (tablica może być pusta)."""
        out = []
        recs = self._base.push(samples)
        out.append((self.windows_ms[0], recs))
        for win, fold in zip(self.windows_ms[1:], self._folds):
            recs = fold.push(recs)
            out.append((win, recs))
        return out

    def flush(self):
        """Domknięcie wszystkich niepełnych okien na końcu pomiaru."""
        out = []
        recs = self._base.flush()
        out.append((self.windows_ms[0], recs))
        for win, fold in zip(self.windows_ms[1:], self._folds):
            recs = np.concatenate([fold.push(recs), fold.flush()])
            out.append((win, recs))
        return out
