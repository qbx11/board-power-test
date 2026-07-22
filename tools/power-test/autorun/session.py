# ============================================================
#  autorun/session.py – zapis i odczyt sesji pomiarowych
# ============================================================
# Sesja = katalog reports/sessions/<przebieg>/<czas>_<scenariusz>/:
#   meta.json                – metadane + podsumowanie (po finalize)
#   status.json              – stan na żywo, podmieniany atomowo ~1 Hz
#   tiers/tier_*.bin         – rekordy (min,avg,max,n) rosnących okien
#   raw.bin                  – surowe float32 µA (tylko mode raw/both)
#   annotations_auto.jsonl   – etykiety z RTT (pisze silnik)
#   annotations_manual.jsonl – etykiety ręczne (pisze viewer)
#   rtt.log / run.log        – logi konsoli RTT i przebiegu kroku
#
# Katalog sesji jest jedynym kanałem między silnikiem a viewerem
# (żadnych socketów): viewer doczytuje nowe bajty tierów i tail-uje
# JSONL-e. Dwa pliki adnotacji = dwa strumienie z JEDNYM piszącym
# każdy, więc nie trzeba blokad między procesami.

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from .downsample import Cascade, RECORD_DTYPE, tier_name, tier_windows_ms

SCHEMA_VERSION = 1
RECORD_SIZE = RECORD_DTYPE.itemsize    # 16 B


def new_session_dir(root, scenario):
    """Świeży katalog sesji <root>/<YYYYmmdd_HHMMSS>_<scenariusz>/
    (przy kolizji sekundowej dokleja licznik)."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    d = Path(root) / f"{stamp}_{scenario}"
    i = 2
    while d.exists():
        d = Path(root) / f"{stamp}_{scenario}_{i}"
        i += 1
    d.mkdir(parents=True)
    return d


def _atomic_json(path, payload):
    """Zapis JSON przez plik tymczasowy + os.replace – czytelnik
    (viewer w osobnym procesie) nigdy nie zobaczy pliku w połowie."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


class SessionWriter:
    """Zapis jednej sesji pomiarowej: próbki -> kaskada tierów (+
    opcjonalnie surowe), adnotacje, status na żywo, podsumowanie.
    write_samples woła wątek pomiarowy; annotate może wołać równolegle
    wątek RTT (własna blokada na pliku adnotacji)."""

    def __init__(self, directory, meta, sample_rate, storage_mode,
                 window_ms):
        self.dir = Path(directory)
        self.sample_rate = float(sample_rate)
        self.storage_mode = storage_mode
        (self.dir / "tiers").mkdir(exist_ok=True)
        self.windows_ms = tier_windows_ms(window_ms)
        self._cascade = Cascade(sample_rate, window_ms)
        self._tier_files = {
            win: open(self.dir / "tiers" / f"{tier_name(win)}.bin", "ab")
            for win in self.windows_ms}
        self._raw_file = (open(self.dir / "raw.bin", "ab")
                          if storage_mode in ("raw", "both") else None)
        self._ann_lock = threading.Lock()
        self._ann_path = self.dir / "annotations_auto.jsonl"

        # Sumy do podsumowania (float64 – godziny próbek 100 kS/s
        # zjadłyby precyzję float32).
        self.samples_written = 0
        self._sum = 0.0
        self._min = float("inf")
        self._max = float("-inf")
        self.gaps = []            # [(t_s, brakujące_próbki), ...]

        self.meta = {"schema_version": SCHEMA_VERSION,
                     "sample_rate": sample_rate,
                     "storage": {"mode": storage_mode,
                                 "window_ms": window_ms},
                     "tiers": [f"{tier_name(w)}.bin"
                               for w in self.windows_ms],
                     "start": datetime.now().isoformat(timespec="seconds"),
                     "state": "measuring",
                     **meta}
        _atomic_json(self.dir / "meta.json", self.meta)
        self.update_status("measuring")

    @property
    def elapsed_s(self):
        """Czas pomiaru liczony PRÓBKAMI (oś wykresu), nie zegarem –
        przy zgubionych próbkach oś się nie rozjeżdża, a braki są
        osobno w `gaps`."""
        return self.samples_written / self.sample_rate

    def write_samples(self, samples):
        arr = np.asarray(samples, np.float32)
        if not len(arr):
            return
        if self._raw_file is not None:
            self._raw_file.write(arr.tobytes())
        if self.storage_mode != "raw":
            for win, recs in self._cascade.push(arr):
                if len(recs):
                    self._tier_files[win].write(recs.tobytes())
        self.samples_written += len(arr)
        self._sum += float(arr.sum(dtype=np.float64))
        self._min = min(self._min, float(arr.min()))
        self._max = max(self._max, float(arr.max()))

    def record_gap(self, missing_samples):
        """Odnotuj zgubione próbki (USB nie nadążył) – viewer zacieniuje
        to miejsce zamiast udawać ciągłość."""
        self.gaps.append([round(self.elapsed_s, 3),
                          int(missing_samples)])

    def annotate(self, t_s, label, source="rtt", pattern=None,
                 rtt_line=None):
        entry = {"t_s": round(float(t_s), 4), "label": label,
                 "source": source,
                 "created": datetime.now().isoformat(timespec="seconds")}
        if pattern:
            entry["pattern"] = pattern
        if rtt_line:
            entry["rtt_line"] = rtt_line
        with self._ann_lock:
            with open(self._ann_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def update_status(self, state, avg_1s_uA=None):
        _atomic_json(self.dir / "status.json", {
            "state": state,
            "elapsed_s": round(self.elapsed_s, 3),
            "avg_1s_uA": (round(avg_1s_uA, 3)
                          if avg_1s_uA is not None else None),
            "samples": self.samples_written,
            "updated": time.time()})

    def summary(self):
        if not self.samples_written:
            return {"samples": 0}
        return {"samples": self.samples_written,
                "duration_s": round(self.elapsed_s, 3),
                "avg_uA": round(self._sum / self.samples_written, 4),
                "min_uA": round(self._min, 4),
                "max_uA": round(self._max, 4),
                # suma µA-próbek / częstotliwość = µA·s = µC
                "charge_uC": round(self._sum / self.sample_rate, 4),
                "lost_samples": sum(g[1] for g in self.gaps)}

    def finalize(self, state="done", extra_meta=None):
        """Domknij tiery, dopisz podsumowanie do meta.json. Sesja
        przerwana (Esc) też przechodzi tędy – częściowe dane są
        poprawną, przeglądalną sesją."""
        if self.storage_mode != "raw":
            for win, recs in self._cascade.flush():
                if len(recs):
                    self._tier_files[win].write(recs.tobytes())
        for f in self._tier_files.values():
            f.close()
        if self._raw_file is not None:
            self._raw_file.close()
        self.meta.update({"state": state, "summary": self.summary(),
                          "gaps": self.gaps,
                          "end": datetime.now().isoformat(
                              timespec="seconds")})
        if extra_meta:
            self.meta.update(extra_meta)
        _atomic_json(self.dir / "meta.json", self.meta)
        self.update_status(state)
        return self.meta["summary"]


class SessionReader:
    """Odczyt sesji (viewer, statystyki, testy). Tiery czytane przez
    memmap – slice dowolnego zoomu nie wciąga całego pliku; plik może
    rosnąć w trakcie (live) – refresh() podnosi widoczny rozmiar."""

    def __init__(self, directory):
        self.dir = Path(directory)
        meta_path = self.dir / "meta.json"
        if not meta_path.is_file():
            raise ValueError(f"'{directory}' nie wygląda na sesję "
                             "(brak meta.json)")
        self.meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.sample_rate = float(self.meta.get("sample_rate", 100_000))
        base = int(self.meta.get("storage", {}).get("window_ms", 1))
        self.windows_ms = tier_windows_ms(base)
        self._sizes = {}

    def reload_meta(self):
        self.meta = json.loads((self.dir / "meta.json")
                               .read_text(encoding="utf-8"))

    def status(self):
        p = self.dir / "status.json"
        if not p.is_file():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}          # wyścig z zapisem – następny tick doczyta

    def tier_path(self, window_ms):
        return self.dir / "tiers" / f"{tier_name(window_ms)}.bin"

    def tier_len(self, window_ms):
        p = self.tier_path(window_ms)
        return p.stat().st_size // RECORD_SIZE if p.is_file() else 0

    def read_tier(self, window_ms, start=0, count=None):
        """Rekordy tieru [start:start+count] (memmap, bez kopiowania
        całości). Zwraca strukturalną tablicę RECORD_DTYPE."""
        n = self.tier_len(window_ms)
        if not n:
            return np.empty(0, RECORD_DTYPE)
        start = max(0, min(start, n))
        stop = n if count is None else max(start, min(start + count, n))
        mm = np.memmap(self.tier_path(window_ms), dtype=RECORD_DTYPE,
                       mode="r", shape=(n,))
        return np.array(mm[start:stop])

    def tier_for_span(self, span_s, max_records=4000):
        """Najdrobniejszy tier, w którym `span_s` mieści się w
        max_records rekordach – rysowanie zoomu zawsze O(pikseli)."""
        for win in self.windows_ms:
            if span_s * 1000 / win <= max_records:
                return win
        return self.windows_ms[-1]

    def has_raw(self):
        return (self.dir / "raw.bin").is_file()

    def read_raw(self, t0_s, t1_s, max_samples=400_000):
        """Surowe próbki [t0, t1] (tylko mode raw/both); przycięte do
        max_samples, żeby zoom nie wciągnął gigabajtów."""
        p = self.dir / "raw.bin"
        n = p.stat().st_size // 4
        i0 = max(0, int(t0_s * self.sample_rate))
        i1 = min(n, int(t1_s * self.sample_rate))
        if i1 <= i0:
            return np.empty(0, np.float32), i0
        i1 = min(i1, i0 + max_samples)
        mm = np.memmap(p, dtype=np.float32, mode="r", shape=(n,))
        return np.array(mm[i0:i1]), i0

    def annotations(self):
        """Adnotacje auto + ręczne, scalone i posortowane po czasie."""
        out = []
        for name, source in (("annotations_auto.jsonl", "rtt"),
                             ("annotations_manual.jsonl", "manual")):
            p = self.dir / name
            if not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue   # ostatnia linia może być w trakcie zapisu
                entry.setdefault("source", source)
                out.append(entry)
        return sorted(out, key=lambda e: e.get("t_s", 0))


def append_manual_annotation(directory, t_s, label):
    """Ręczna etykieta z viewera – osobny plik (jeden piszący proces),
    ta sama struktura co auto."""
    entry = {"t_s": round(float(t_s), 4), "label": label,
             "source": "manual",
             "created": datetime.now().isoformat(timespec="seconds")}
    with open(Path(directory) / "annotations_manual.jsonl", "a",
              encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def scan_sessions(root, max_depth=2):
    """Biblioteka sesji: wszystkie katalogi z meta.json pod `root`
    (przebiegi planów grupują sesje o poziom głębiej). Zwraca listę
    (ścieżka, meta) posortowaną od najnowszej."""
    root = Path(root)
    if not root.is_dir():
        return []
    found = []
    for pattern in ["*/meta.json", "*/*/meta.json"][:max_depth]:
        for meta_path in root.glob(pattern):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            found.append((meta_path.parent, meta))
    return sorted(found, key=lambda pm: pm[1].get("start", ""),
                  reverse=True)
