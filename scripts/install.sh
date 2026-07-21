#!/usr/bin/env bash
# ============================================================
#  Instalacja globalnej komendy `board-power-test`
# ============================================================
# Tworzy symlink w ~/.local/bin (bez sudo). Po instalacji wpisujesz
# `board-power-test` w dowolnym terminalu i startuje menu narzędzia.
# Inny katalog docelowy: BPT_BIN_DIR=/gdzie/chcesz ./scripts/install.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="${BPT_BIN_DIR:-$HOME/.local/bin}"

mkdir -p "$BIN_DIR"
ln -sf "$REPO/bin/board-power-test" "$BIN_DIR/board-power-test"
echo "Zainstalowano: $BIN_DIR/board-power-test -> $REPO/bin/board-power-test"

case ":$PATH:" in
*":$BIN_DIR:"*)
	echo "OK: $BIN_DIR jest w PATH. Wpisz: board-power-test"
	;;
*)
	echo
	echo "UWAGA: $BIN_DIR nie jest w PATH. Dodaj do ~/.zshrc (albo ~/.bashrc):"
	echo "  export PATH=\"$BIN_DIR:\$PATH\""
	echo "potem otwórz nowy terminal i wpisz: board-power-test"
	;;
esac
