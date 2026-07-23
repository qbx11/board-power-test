# ============================================================
#  viewer/export.py – eksport widoku i zaznaczenia
# ============================================================

import csv

import numpy as np


def export_csv(path, reader, window_ms, t0_s, t1_s):
    """Zapisz rekordy tieru `window_ms` z zakresu [t0,t1] do CSV
    (czas_s, min_uA, avg_uA, max_uA, probki). Zwraca liczbę wierszy."""
    win_s = window_ms / 1000.0
    i0 = max(0, int(t0_s / win_s))
    i1 = int(np.ceil(t1_s / win_s))
    recs = reader.read_tier(window_ms, i0, i1 - i0)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["czas_s", "min_uA", "avg_uA", "max_uA", "probki"])
        for k, r in enumerate(recs):
            t = (i0 + k) * win_s
            w.writerow([f"{t:.6f}", f"{r['min']:.4f}", f"{r['avg']:.4f}",
                        f"{r['max']:.4f}", int(r["n"])])
    return len(recs)


def export_png(plot_item, path):
    """Zrzut PNG bieżącego widoku wykresu (pyqtgraph ImageExporter)."""
    import pyqtgraph.exporters as exporters
    exporter = exporters.ImageExporter(plot_item)
    exporter.export(str(path))
