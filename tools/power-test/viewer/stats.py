# ============================================================
#  viewer/stats.py – statystyki zaznaczonego fragmentu wykresu
# ============================================================
# Czyste funkcje (bez Qt) – liczą avg/min/max/ładunek/estymatę baterii
# z rekordów tieru albo surowych próbek. Testowalne osobno.

import numpy as np


def region_stats(records):
    """Statystyki z rekordów tieru (min,avg,max,n) w zaznaczeniu.
    Średnia ważona liczbą próbek – żeby niepełne okna na krańcach nie
    przeważały. Zwraca dict albo None dla pustego zaznaczenia."""
    if records is None or not len(records):
        return None
    n = records["n"].astype(np.float64)
    total_n = n.sum()
    if total_n <= 0:
        return None
    avg = float((records["avg"] * n).sum() / total_n)
    return {"avg_uA": avg,
            "min_uA": float(records["min"].min()),
            "max_uA": float(records["max"].max()),
            "samples": int(total_n)}


def raw_stats(samples):
    """Statystyki z surowych próbek (µA)."""
    if samples is None or not len(samples):
        return None
    return {"avg_uA": float(samples.mean()),
            "min_uA": float(samples.min()),
            "max_uA": float(samples.max()),
            "samples": int(len(samples))}


def charge_and_energy(avg_uA, duration_s, voltage_V=None):
    """Ładunek i (jeśli znane napięcie) energia zaznaczenia.
    charge[µC] = avg[µA]·t[s];  mAh = µC / 3.6e6 · 1000."""
    charge_uC = avg_uA * duration_s
    out = {"charge_uC": charge_uC,
           "charge_mAh": charge_uC / 3.6e9 * 1000}
    if voltage_V:
        # energia[µJ] = ładunek[µC]·U[V];  mWh = µJ / 3.6e6
        out["energy_uJ"] = charge_uC * voltage_V
        out["energy_mWh"] = charge_uC * voltage_V / 3.6e6
    return out


def battery_life(avg_uA, capacity_mAh):
    """Szacowany czas pracy [h] przy średnim prądzie zaznaczenia i
    podanej pojemności baterii. None gdy prąd ~0."""
    if avg_uA <= 0 or capacity_mAh <= 0:
        return None
    return capacity_mAh * 1000.0 / avg_uA        # mAh->µAh / µA = h


def format_current(uA):
    """µA/mA/A z sensownym rzędem wielkości do etykiet."""
    a = abs(uA)
    if a < 1000:
        return f"{uA:.3g} µA"
    if a < 1e6:
        return f"{uA / 1000:.3g} mA"
    return f"{uA / 1e6:.3g} A"


def format_duration(seconds):
    if seconds < 60:
        return f"{seconds:.3g} s"
    if seconds < 3600:
        return f"{seconds / 60:.2f} min"
    if seconds < 86400:
        return f"{seconds / 3600:.2f} h"
    return f"{seconds / 86400:.2f} dni"
