#!/usr/bin/env bash
# ============================================================
#  Budowanie jednego wariantu pomiarowego nRF54L15
# ============================================================
# Uruchom w terminalu nRF Connect (gdzie `west` jest w PATH) albo w powłoce z:
#   nrfutil toolchain-manager launch --ncs-version v3.4.0 --shell
#
# Użycie:
#   ./scripts/build.sh <tryb> [T_ms] [active_ms] [--rtt]
#
# Tryby: idle | wake_gpio | reset_only | ram_retained | periodic_on | periodic_off
#   T_ms, active_ms  – tylko dla periodic_* (okres i długość błysku), np. 60000 5
#   --rtt            – build diagnostyczny z logami przez RTT (J-Link RTT Viewer)
#
# Zmienne środowiskowe (opcjonalne, gdy masz inny układ katalogów):
#   BOARD       – domyślnie BTZ_EndDevice/nrf54l15/cpuapp
#   BOARD_ROOT  – katalog zawierający boards/ z definicją płytki;
#                 domyślnie ../NCS-Projects/Projekt-BLE-Mesh/app (obok tego repo)
set -euo pipefail

command -v west >/dev/null 2>&1 || {
	echo "Brak 'west' w PATH. Otwórz terminal nRF Connect lub uruchom:"
	echo "  nrfutil toolchain-manager launch --ncs-version v3.4.0 --shell"
	exit 1
}

APP="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP"
BOARD="${BOARD:-BTZ_EndDevice/nrf54l15/cpuapp}"
BOARD_ROOT="${BOARD_ROOT:-$APP/../NCS-Projects/Projekt-BLE-Mesh/app}"

case "${1:-}" in
	idle)          CFG="-DCONFIG_SLEEP_SYSTEM_ON_IDLE=y";;
	wake_gpio)     CFG="-DCONFIG_SLEEP_SYSTEM_OFF_WAKE_GPIO=y";;
	reset_only)    CFG="-DCONFIG_SLEEP_SYSTEM_OFF_RESET_ONLY=y";;
	ram_retained)  CFG="-DCONFIG_SLEEP_SYSTEM_OFF_RAM_RETAINED=y";;
	periodic_on)   CFG="-DCONFIG_SLEEP_PERIODIC_SYSTEM_ON=y";;
	periodic_off)  CFG="-DCONFIG_SLEEP_PERIODIC_SYSTEM_OFF=y";;
	*) echo "Użycie: $0 {idle|wake_gpio|reset_only|ram_retained|periodic_on|periodic_off} [T_ms] [active_ms] [--rtt]"; exit 1;;
esac
MODE="$1"; shift

EXTRA=""
[[ "${1:-}" =~ ^[0-9]+$ ]] && { EXTRA="$EXTRA -DCONFIG_PERIODIC_PERIOD_MS=$1"; shift; }
[[ "${1:-}" =~ ^[0-9]+$ ]] && { EXTRA="$EXTRA -DCONFIG_PERIODIC_ACTIVE_MS=$1"; shift; }
[[ "${1:-}" == "--rtt" || "${1:-}" == "--debug" ]] && { EXTRA="$EXTRA -DEXTRA_CONF_FILE=debug_rtt.conf"; shift; }

# Board root dokładamy tylko, jeśli faktycznie istnieje (np. w VS Code bywa już
# skonfigurowany i nie trzeba go podawać).
ROOT_ARG=""
if [ -d "$BOARD_ROOT/boards" ]; then
	ROOT_ARG="-DBOARD_ROOT=$(cd "$BOARD_ROOT" && pwd)"
fi

# shellcheck disable=SC2086
west build -b "$BOARD" -p always -d "build_$MODE" "$APP" -- $ROOT_ARG $CFG $EXTRA

echo
echo ">>> Gotowe. HEX: build_$MODE/nrf54l15-power-test/zephyr/zephyr.hex"
echo ">>> Wgraj (J-Link mini EDU):  west flash -d build_$MODE -r jlink"
