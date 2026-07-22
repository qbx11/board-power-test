# ============================================================
#  viewer/__main__.py – wejście okna wykresu
# ============================================================
#   python -m viewer --live  <katalog_sesji>   (podgląd na żywo)
#   python -m viewer --open  <katalog…>        (historia, jedna/wiele)
#   python -m viewer                           (biblioteka sesji)

import argparse
import sys
from pathlib import Path

# Pakiet uruchamiany jako `python -m viewer` z katalogu tools/power-test
# (tak robi launch_viewer) – autorun jest wtedy na ścieżce. Gdyby jednak
# ktoś odpalił inaczej, dołóż katalog nadrzędny.
_TOOL_DIR = Path(__file__).resolve().parent.parent
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))


def main():
    ap = argparse.ArgumentParser(
        prog="viewer",
        description="Okno wykresu poboru prądu (sesje autorun).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--live", metavar="DIR",
                   help="śledź sesję na żywo (doczytuj rosnące pliki)")
    g.add_argument("--open", nargs="+", metavar="DIR",
                   help="otwórz sesję/sesje z historii")
    ap.add_argument("--sessions-root", metavar="DIR",
                    help="katalog biblioteki (dom. reports/sessions "
                         "względem repo)")
    args = ap.parse_args()

    from viewer.app import run_app
    run_app(live=args.live, open_dirs=args.open,
            sessions_root=args.sessions_root)


if __name__ == "__main__":
    main()
