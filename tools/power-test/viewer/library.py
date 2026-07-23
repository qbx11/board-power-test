# ============================================================
#  viewer/library.py – biblioteka sesji (panel boczny)
# ============================================================
# Lista wszystkich zapisanych sesji (skan reports/sessions). Pojedyncze
# kliknięcie otwiera sesję; zaznaczenie wielu (checkbox) + „Porównaj"
# nakłada je na jeden wykres.

from PyQt6 import QtCore, QtWidgets

from autorun import session as sess

from . import stats as statmod


class LibrarySidebar(QtWidgets.QWidget):
    """Panel z listą sesji. Sygnały: openSession(str dir),
    compareSessions(list[str])."""

    openSession = QtCore.pyqtSignal(str)
    compareSessions = QtCore.pyqtSignal(list)

    def __init__(self, sessions_root, parent=None):
        super().__init__(parent)
        self.root = sessions_root
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(QtWidgets.QLabel("<b>Biblioteka sesji</b>"))

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["sesja", "śr", "czas"])
        self.tree.setRootIsDecorated(False)
        self.tree.itemDoubleClicked.connect(self._open)
        self.tree.header().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.tree)

        row = QtWidgets.QHBoxLayout()
        self.compare_btn = QtWidgets.QPushButton("Porównaj zaznaczone")
        self.compare_btn.clicked.connect(self._compare)
        refresh = QtWidgets.QPushButton("Odśwież")
        refresh.clicked.connect(self.reload)
        row.addWidget(self.compare_btn)
        row.addWidget(refresh)
        lay.addLayout(row)
        self.reload()

    def reload(self):
        self.tree.clear()
        for path, meta in sess.scan_sessions(self.root):
            summ = meta.get("summary", {})
            label = meta.get("label", meta.get("scenario", path.name))
            state = meta.get("state", "")
            title = label if state in ("", "done") else f"{label} [{state}]"
            avg = (statmod.format_current(summ["avg_uA"])
                   if summ.get("avg_uA") is not None else "—")
            dur = (statmod.format_duration(summ["duration_s"])
                   if summ.get("duration_s") else "—")
            item = QtWidgets.QTreeWidgetItem([title, avg, dur])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(0, f"{path.name}\n{meta.get('start', '')}")
            item.setFlags(item.flags()
                          | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, QtCore.Qt.CheckState.Unchecked)
            self.tree.addTopLevelItem(item)

    def _open(self, item):
        self.openSession.emit(
            item.data(0, QtCore.Qt.ItemDataRole.UserRole))

    def _checked_dirs(self):
        out = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.checkState(0) == QtCore.Qt.CheckState.Checked:
                out.append(it.data(0, QtCore.Qt.ItemDataRole.UserRole))
        return out

    def _compare(self):
        dirs = self._checked_dirs()
        if len(dirs) >= 2:
            self.compareSessions.emit(dirs)
