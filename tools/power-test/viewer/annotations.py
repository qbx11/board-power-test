# ============================================================
#  viewer/annotations.py – warstwa etykiet na wykresie detalu
# ============================================================
# Znaczniki czasu (pionowa linia + podpis) rysowane z scalonej listy
# adnotacji sesji (auto z RTT + ręczne). Auto i ręczne różni kolor.

import pyqtgraph as pg
from PyQt6 import QtCore

AUTO_PEN = pg.mkPen("#66cc88", width=1, style=QtCore.Qt.PenStyle.DashLine)
MANUAL_PEN = pg.mkPen("#ffcc44", width=1, style=QtCore.Qt.PenStyle.DashLine)


class AnnotationLayer:
    """Zarządza obiektami InfiniteLine na wykresie – set_data() zdejmuje
    poprzednie i rysuje aktualną listę adnotacji (idempotentne przy
    odświeżaniu live)."""

    def __init__(self, plot_item):
        self.plot_item = plot_item
        self._lines = []

    def clear(self):
        for line in self._lines:
            self.plot_item.removeItem(line)
        self._lines = []

    def set_data(self, annotations):
        self.clear()
        for ann in annotations:
            pen = MANUAL_PEN if ann.get("source") == "manual" else AUTO_PEN
            line = pg.InfiniteLine(
                pos=ann.get("t_s", 0), angle=90, pen=pen, movable=False,
                label=ann.get("label", ""),
                labelOpts={"position": 0.92, "color": pen.color(),
                           "movable": True, "fill": (20, 20, 20, 160)})
            self.plot_item.addItem(line, ignoreBounds=True)
            self._lines.append(line)
