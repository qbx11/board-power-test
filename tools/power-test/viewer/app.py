# ============================================================
#  viewer/app.py – okno główne (QMainWindow) + start aplikacji
# ============================================================
# Lewy panel = biblioteka sesji, środek = zakładki z otwartymi widokami
# (SessionView / CompareView), pasek narzędzi = tryb etykiet, skala log,
# pojemność baterii, eksport. run_app() spina to i uruchamia pętlę Qt.

import sys
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets

from . import export as exportmod
from .library import LibrarySidebar
from .plot import CompareView, SessionView


def _default_sessions_root():
    # tools/power-test/viewer -> repo/reports/sessions
    return Path(__file__).resolve().parents[3] / "reports" / "sessions"


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, sessions_root):
        super().__init__()
        self.setWindowTitle("board-power-test — wykres poboru prądu")
        self.resize(1200, 760)
        self.sessions_root = sessions_root

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(
            lambda i: self.tabs.removeTab(i))
        self.setCentralWidget(self.tabs)

        self.sidebar = LibrarySidebar(sessions_root)
        self.sidebar.openSession.connect(self.open_session)
        self.sidebar.compareSessions.connect(self.open_compare)
        dock = QtWidgets.QDockWidget("Biblioteka", self)
        dock.setWidget(self.sidebar)
        dock.setFeatures(
            QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.addDockWidget(
            QtCore.Qt.DockWidgetArea.LeftDockWidgetArea, dock)

        self._build_toolbar()

    def _build_toolbar(self):
        tb = self.addToolBar("Narzędzia")
        tb.setMovable(False)

        self.add_action = QtGui.QAction("Dodaj etykietę", self)
        self.add_action.setCheckable(True)
        self.add_action.setToolTip("Kliknij na wykresie, aby postawić "
                                   "etykietę w danym punkcie czasu")
        self.add_action.toggled.connect(self._toggle_add)
        tb.addAction(self.add_action)

        self.log_action = QtGui.QAction("Skala log", self)
        self.log_action.setCheckable(True)
        self.log_action.toggled.connect(self._toggle_log)
        tb.addAction(self.log_action)

        tb.addSeparator()
        tb.addWidget(QtWidgets.QLabel(" bateria [mAh]: "))
        self.cap_spin = QtWidgets.QDoubleSpinBox()
        self.cap_spin.setRange(1, 100000)
        self.cap_spin.setValue(220)      # CR2032 ~220 mAh
        self.cap_spin.setDecimals(0)
        self.cap_spin.valueChanged.connect(self._set_capacity)
        tb.addWidget(self.cap_spin)

        tb.addSeparator()
        png = QtGui.QAction("Eksport PNG", self)
        png.triggered.connect(self._export_png)
        tb.addAction(png)
        csv = QtGui.QAction("Eksport CSV", self)
        csv.triggered.connect(self._export_csv)
        tb.addAction(csv)

    # ---------- otwieranie ----------

    def open_session(self, directory, live=False):
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, SessionView) and \
                    str(w.reader.dir) == str(directory):
                self.tabs.setCurrentIndex(i)      # już otwarta
                return
        try:
            view = SessionView(directory, live=live)
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "Błąd", str(e))
            return
        title = view.reader.meta.get("scenario", Path(directory).name)
        idx = self.tabs.addTab(view, ("● " if live else "") + title)
        self.tabs.setCurrentIndex(idx)

    def open_compare(self, dirs):
        view = CompareView(dirs)
        idx = self.tabs.addTab(view, f"Porównanie ({len(dirs)})")
        self.tabs.setCurrentIndex(idx)

    def _current(self):
        return self.tabs.currentWidget()

    # ---------- narzędzia ----------

    def _toggle_add(self, on):
        w = self._current()
        if isinstance(w, SessionView):
            w.set_add_mode(on)

    def _toggle_log(self, on):
        w = self._current()
        if hasattr(w, "set_ylog"):
            w.set_ylog(on)

    def _set_capacity(self, val):
        w = self._current()
        if isinstance(w, SessionView):
            w.set_capacity(val)

    def _export_png(self):
        w = self._current()
        if w is None:
            return
        item = (w.detail_item if isinstance(w, SessionView)
                else getattr(w, "item", None))
        if item is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Eksport PNG", "wykres.png", "PNG (*.png)")
        if path:
            exportmod.export_png(item, path)

    def _export_csv(self):
        w = self._current()
        if not isinstance(w, SessionView):
            QtWidgets.QMessageBox.information(
                self, "Eksport CSV", "Otwórz pojedynczą sesję.")
            return
        (t0, t1), _ = w.detail_item.viewRange()
        win = w.reader.tier_for_span(max(1e-3, t1 - t0))
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Eksport CSV (widoczny zakres)", "fragment.csv",
            "CSV (*.csv)")
        if path:
            n = exportmod.export_csv(path, w.reader, win, max(0, t0), t1)
            self.statusBar().showMessage(
                f"Zapisano {n} rekordów (tier {win} ms) do {path}", 5000)


def run_app(live=None, open_dirs=None, sessions_root=None):
    root = Path(sessions_root) if sessions_root else _default_sessions_root()
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow(root)
    win.show()
    if live:
        win.open_session(live, live=True)
    elif open_dirs:
        if len(open_dirs) >= 2:
            win.open_compare(open_dirs)
        else:
            for d in open_dirs:
                win.open_session(d)
    sys.exit(app.exec())
