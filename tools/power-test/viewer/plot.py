# ============================================================
#  viewer/plot.py – widok pojedynczej sesji (szczegół + przegląd)
# ============================================================
# Wzorzec overview+detail: górny wykres to zoom, dolny pokazuje CAŁOŚĆ
# z prostokątem zaznaczenia (LinearRegionItem) sprzężonym dwukierunkowo
# z zakresem górnego – jak minimapa. Zoom dobiera tier (LOD) tak, by
# rysować ~O(pikseli) rekordów niezależnie od długości sesji.

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, QtWidgets

from autorun import session as sess

from . import stats as statmod
from .annotations import AnnotationLayer

# Monochromatyczny motyw spójny z TUI: ciemne tło, jasna linia, wyraźny
# grid. Pasmo min–max półprzezroczyste wokół średniej.
pg.setConfigOption("background", "#111111")
pg.setConfigOption("foreground", "#cccccc")
pg.setConfigOptions(antialias=True)

AVG_PEN = pg.mkPen("#e6e6e6", width=1.5)
BAND_BRUSH = pg.mkBrush(120, 170, 255, 60)
BAND_PEN = pg.mkPen(120, 170, 255, 90)
OVERVIEW_PEN = pg.mkPen("#8899bb", width=1)
REGION_BRUSH = pg.mkBrush(200, 200, 120, 40)


def _style_axes(plot_item, ylog=False):
    """Wyraźne osie i grid – jeden z powodów, dla których to okno bije
    Power Profiler: mocniejszy kontrast siatki, opisane jednostki."""
    plot_item.showGrid(x=True, y=True, alpha=0.35)
    plot_item.setLabel("bottom", "czas", units="s")
    plot_item.setLabel("left", "prąd", units="A" if not ylog else "")
    plot_item.getAxis("left").setLabel(
        "prąd", units="A")
    for ax in ("left", "bottom"):
        plot_item.getAxis(ax).setTextPen("#cccccc")
        plot_item.getAxis(ax).setPen("#666666")


def _load_tier(reader, window_ms, t0, t1):
    """Rekordy tieru [t0,t1] + oś czasu (środek okna)."""
    win_s = window_ms / 1000.0
    n = reader.tier_len(window_ms)
    if not n:
        return np.empty(0), np.empty(0, sess.RECORD_DTYPE)
    i0 = max(0, int(t0 / win_s))
    i1 = min(n, int(np.ceil(t1 / win_s)) + 1)
    recs = reader.read_tier(window_ms, i0, i1 - i0)
    t = (i0 + np.arange(len(recs))) * win_s + win_s / 2
    return t, recs


COMPARE_PENS = ["#e6e6e6", "#66cc88", "#ffcc44", "#88bbff", "#ff8888",
                "#cc88ff", "#88dddd"]


class CompareView(QtWidgets.QWidget):
    """Nakładka kilku sesji: średni prąd każdej wyrównany do t=0, wspólne
    osie + legenda. Zoom/pan przez mysz; LOD dobierane per widoczny
    zakres. Pozwala porównać profile różnych scenariuszy/egzemplarzy."""

    def __init__(self, session_dirs, parent=None):
        super().__init__(parent)
        self.readers = []
        for d in session_dirs:
            try:
                self.readers.append(sess.SessionReader(d))
            except ValueError:
                pass
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(QtWidgets.QLabel(
            f"<b>Porównanie {len(self.readers)} sesji</b> "
            "(wyrównane do t=0)"))
        self.plot = pg.PlotWidget()
        self.item = self.plot.getPlotItem()
        _style_axes(self.item)
        self.item.addLegend(offset=(-10, 10))
        lay.addWidget(self.plot)
        self._curves = []
        for i, r in enumerate(self.readers):
            pen = pg.mkPen(COMPARE_PENS[i % len(COMPARE_PENS)], width=1.5)
            name = r.meta.get("label", r.meta.get("scenario", r.dir.name))
            self._curves.append(self.item.plot([], [], pen=pen, name=name))
        self.item.sigXRangeChanged.connect(self._refresh)
        self._fit()

    def _fit(self):
        dur = max((r.tier_len(r.windows_ms[0]) * r.windows_ms[0] / 1000.0
                   for r in self.readers), default=1.0)
        self.item.setXRange(0, max(1.0, dur), padding=0.02)
        self._refresh()

    def _refresh(self):
        (t0, t1), _ = self.item.viewRange()
        for r, curve in zip(self.readers, self._curves):
            win = r.tier_for_span(max(1e-3, t1 - t0))
            t, recs = _load_tier(r, win, t0, t1)
            if len(recs):
                curve.setData(t, recs["avg"])

    def set_ylog(self, on):
        self.item.setLogMode(y=on)


class SessionView(QtWidgets.QWidget):
    """Wykres jednej sesji: detal (zoom) nad przeglądem całości; pasmo
    min–max, adnotacje, kursor, ruchome zaznaczenie do statystyk."""

    def __init__(self, session_dir, live=False, parent=None):
        super().__init__(parent)
        self.reader = sess.SessionReader(session_dir)
        self.live = live
        self.following = live          # w trybie live trzymaj się prawej
        self._syncing = False          # blokada rekurencji sygnałów
        self._add_mode = False
        self._build_ui()
        self._load_initial()
        if live:
            self._timer = QtCore.QTimer(self)
            self._timer.timeout.connect(self._on_tick)
            self._timer.start(200)

    # ---------- budowa UI ----------

    def _build_ui(self):
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        self.header = QtWidgets.QLabel()
        self.header.setStyleSheet("color:#aaaaaa; padding:2px;")
        lay.addWidget(self.header)

        self.detail = pg.PlotWidget()
        self.detail_item = self.detail.getPlotItem()
        _style_axes(self.detail_item)
        lay.addWidget(self.detail, stretch=4)

        self.overview = pg.PlotWidget()
        self.overview.setMaximumHeight(130)
        self.overview_item = self.overview.getPlotItem()
        self.overview_item.showGrid(x=True, y=True, alpha=0.25)
        self.overview_item.setLabel("left", "prąd", units="A")
        self.overview_item.setMouseEnabled(x=False, y=False)
        lay.addWidget(self.overview)

        self.stats_label = QtWidgets.QLabel("zaznacz fragment, aby "
                                            "policzyć statystyki")
        self.stats_label.setStyleSheet(
            "color:#cccccc; padding:4px; background:#1a1a1a;")
        self.stats_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        lay.addWidget(self.stats_label)

        # Krzywe detalu: średnia + pasmo min/max (FillBetween).
        self.avg_curve = self.detail_item.plot([], [], pen=AVG_PEN)
        self.min_curve = self.detail_item.plot([], [], pen=BAND_PEN)
        self.max_curve = self.detail_item.plot([], [], pen=BAND_PEN)
        self.band = pg.FillBetweenItem(self.min_curve, self.max_curve,
                                       brush=BAND_BRUSH)
        self.detail_item.addItem(self.band)

        # Trwała krzywa przeglądu (setData przy odświeżaniu – bez
        # ponownego dodawania itemów) + zaznaczenie sprzężone z zoomem.
        self.overview_curve = self.overview_item.plot([], [],
                                                      pen=OVERVIEW_PEN)
        self.nav_region = pg.LinearRegionItem(brush=REGION_BRUSH,
                                              pen=OVERVIEW_PEN)
        self.nav_region.setZValue(10)
        self.overview_item.addItem(self.nav_region)
        self.nav_region.sigRegionChanged.connect(self._region_to_detail)
        self.detail_item.sigXRangeChanged.connect(self._detail_to_region)

        # Ruchome zaznaczenie do STATYSTYK (na detalu).
        self.stat_region = pg.LinearRegionItem(
            brush=pg.mkBrush(255, 255, 255, 25),
            pen=pg.mkPen("#dddddd", style=QtCore.Qt.PenStyle.DashLine))
        self.stat_region.setZValue(9)
        self.detail_item.addItem(self.stat_region)
        self.stat_region.sigRegionChanged.connect(self._update_stats)

        # Kursor (krzyż + odczyt).
        self.vline = pg.InfiniteLine(angle=90, pen=pg.mkPen("#555555"))
        self.hline = pg.InfiniteLine(angle=0, pen=pg.mkPen("#555555"))
        self.detail_item.addItem(self.vline, ignoreBounds=True)
        self.detail_item.addItem(self.hline, ignoreBounds=True)
        self.cursor_label = pg.TextItem(color="#dddddd", anchor=(0, 1))
        self.detail_item.addItem(self.cursor_label)
        self.detail.scene().sigMouseMoved.connect(self._on_mouse)
        self.detail.scene().sigMouseClicked.connect(self._on_click)

        self.annotations = AnnotationLayer(self.detail_item)

    # ---------- ładowanie ----------

    def _load_initial(self):
        m = self.reader.meta
        summ = m.get("summary", {})
        self.header.setText(self._header_text(m, summ))
        dur = self._duration()
        # Przegląd: najgrubszy tier przez całość.
        self._refresh_overview()
        # Detal: pierwsze okno (całość, jeśli krótka).
        span = min(dur, max(1.0, dur))
        self.detail_item.setXRange(0, span if span > 0 else 1,
                                   padding=0)
        self.stat_region.setRegion([dur * 0.25, dur * 0.5]
                                   if dur > 0 else [0, 1])
        self._refresh_detail()
        self._refresh_annotations()

    def _header_text(self, m, summ):
        parts = [f"<b>{m.get('label', m.get('scenario', '?'))}</b>",
                 m.get("board", ""), f"{m.get('voltage_V', '?')} V"]
        if summ:
            parts.append(f"śr {statmod.format_current(summ.get('avg_uA', 0))}")
            parts.append(f"czas {statmod.format_duration(summ.get('duration_s', 0))}")
        state = m.get("state")
        if state and state != "done":
            parts.append(f"[{state}]")
        return "  ·  ".join(p for p in parts if p)

    def _duration(self):
        """Długość osi czasu z najdrobniejszego tieru (rośnie w live)."""
        base = self.reader.windows_ms[0]
        n = self.reader.tier_len(base)
        if n:
            return n * base / 1000.0
        return self.reader.meta.get("summary", {}).get("duration_s", 1.0)

    # ---------- rysowanie ----------

    def _refresh_overview(self):
        win = self.reader.windows_ms[-1]
        t, recs = _load_tier(self.reader, win, 0, self._duration())
        if len(recs):
            self.overview_curve.setData(t, recs["avg"])

    def _refresh_detail(self):
        (t0, t1), _ = self.detail_item.viewRange()
        span = max(1e-3, t1 - t0)
        win = self.reader.tier_for_span(span)
        t, recs = _load_tier(self.reader, win, t0, t1)
        if not len(recs):
            return
        self.avg_curve.setData(t, recs["avg"])
        self.min_curve.setData(t, recs["min"])
        self.max_curve.setData(t, recs["max"])

    def _refresh_annotations(self):
        self.annotations.set_data(self.reader.annotations())

    # ---------- sprzężenie zoom <-> przegląd ----------

    def _detail_to_region(self):
        if self._syncing:
            return
        self._syncing = True
        (t0, t1), _ = self.detail_item.viewRange()
        self.nav_region.setRegion([t0, t1])
        self._syncing = False
        # Przy ręcznym zoomie porzuć auto-śledzenie prawej krawędzi.
        if self.live:
            dur = self._duration()
            self.following = t1 >= dur - max(0.5, dur * 0.02)
        self._refresh_detail()

    def _region_to_detail(self):
        if self._syncing:
            return
        self._syncing = True
        t0, t1 = self.nav_region.getRegion()
        self.detail_item.setXRange(t0, t1, padding=0)
        self._syncing = False
        self._refresh_detail()

    # ---------- kursor / kliknięcia ----------

    def _on_mouse(self, pos):
        if not self.detail_item.sceneBoundingRect().contains(pos):
            return
        pt = self.detail_item.vb.mapSceneToView(pos)
        self.vline.setPos(pt.x())
        self.hline.setPos(pt.y())
        self.cursor_label.setText(
            f"t={statmod.format_duration(max(0, pt.x()))}  "
            f"{statmod.format_current(pt.y())}")
        self.cursor_label.setPos(pt.x(), pt.y())

    def _on_click(self, event):
        if not self._add_mode or event.button() != \
                QtCore.Qt.MouseButton.LeftButton:
            return
        pt = self.detail_item.vb.mapSceneToView(event.scenePos())
        t_s = max(0.0, pt.x())
        text, ok = QtWidgets.QInputDialog.getText(
            self, "Nowa etykieta",
            f"Etykieta w t={statmod.format_duration(t_s)}:")
        if ok and text.strip():
            sess.append_manual_annotation(self.reader.dir, t_s,
                                          text.strip())
            self._refresh_annotations()
        self.set_add_mode(False)

    def set_add_mode(self, on):
        self._add_mode = on
        self.detail.setCursor(
            QtCore.Qt.CursorShape.CrossCursor if on
            else QtCore.Qt.CursorShape.ArrowCursor)

    def set_ylog(self, on):
        self.detail_item.setLogMode(y=on)
        self.overview_item.setLogMode(y=on)

    # ---------- statystyki zaznaczenia ----------

    def _update_stats(self):
        t0, t1 = self.stat_region.getRegion()
        if t1 <= t0:
            return
        win = self.reader.tier_for_span(t1 - t0)
        _, recs = _load_tier(self.reader, win, t0, t1)
        st = statmod.region_stats(recs)
        if not st:
            self.stats_label.setText("(brak danych w zaznaczeniu)")
            return
        dur = t1 - t0
        volt = self._voltage()
        ce = statmod.charge_and_energy(st["avg_uA"], dur, volt)
        html = (
            f"<b>zaznaczenie {statmod.format_duration(dur)}</b> &nbsp; "
            f"śr <b>{statmod.format_current(st['avg_uA'])}</b> &nbsp; "
            f"min {statmod.format_current(st['min_uA'])} &nbsp; "
            f"max {statmod.format_current(st['max_uA'])} &nbsp; "
            f"ładunek {ce['charge_uC']:.4g} µC "
            f"({ce['charge_mAh']:.4g} mAh)")
        life = statmod.battery_life(st["avg_uA"], self._capacity_mAh)
        if life:
            html += (f" &nbsp; bateria {self._capacity_mAh:g} mAh ≈ "
                     f"<b>{statmod.format_duration(life * 3600)}</b>")
        self.stats_label.setText(html)

    _capacity_mAh = 220.0     # domyślna pojemność do estymaty (CR2032)

    def set_capacity(self, mAh):
        self._capacity_mAh = mAh
        self._update_stats()

    def _voltage(self):
        try:
            return float(str(self.reader.meta.get("voltage_V", "0"))
                         .replace(",", "."))
        except ValueError:
            return None

    # ---------- live ----------

    def _on_tick(self):
        base = self.reader.windows_ms[0]
        grew = self.reader.tier_len(base)
        status = self.reader.status()
        self._refresh_overview()
        if self.following:
            dur = self._duration()
            (t0, t1), _ = self.detail_item.viewRange()
            span = t1 - t0
            self._syncing = True
            self.detail_item.setXRange(max(0, dur - span), dur,
                                       padding=0)
            self._syncing = False
        self._refresh_detail()
        self._refresh_annotations()
        if status.get("avg_1s_uA") is not None:
            self.header.setText(
                self._header_text(self.reader.meta, {}) +
                f"  ·  na żywo {statmod.format_current(status['avg_1s_uA'])}")
        if status.get("state") in ("done", "cancelled", "error",
                                   "trigger_timeout"):
            self._timer.stop()
            self.reader.reload_meta()
            self.live = self.following = False
            self.header.setText(self._header_text(
                self.reader.meta, self.reader.meta.get("summary", {})))
