# ============================================================
#  viewer – okno wykresu poboru prądu (PyQt6 + pyqtgraph)
# ============================================================
# Osobny PROCES (uruchamiany z power_test.launch_viewer): PyQt i Textual
# nie współdzielą pętli zdarzeń ani terminala. Czyta sesje zapisane
# przez autorun.session – zarówno na żywo (--live, doczytuje rosnące
# pliki), jak i z historii (--open / biblioteka). Import autorun.session
# jest jedyną zależnością od reszty narzędzia; poza tym tylko numpy/Qt.
