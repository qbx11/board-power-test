# ============================================================
#  Wspólna uprzęż testowa (tools/power-test/tests)
# ============================================================
# Testy nie dotykają prawdziwego środowiska: dostają tymczasowe "repo"
# z własnym scenarios.toml, fałszywy `west` i `nrfutil` (skrypty bash
# logujące argumenty do pliku wskazanego przez $CMD_LOG) oraz fałszywy
# SDK NCS w podmienionym HOME (~/ncs/v3.4.0/.west), żeby przećwiczyć
# ścieżkę "build out-of-tree". PATH i HOME są podmieniane na czas testu
# i przywracane w cleanup().
#
# Uruchamianie (konwencja projektu – python z .venv repo):
#   .venv/bin/python -m unittest discover -s tools/power-test/tests -v

import os
import sys
import tempfile
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import power_test as core  # noqa: E402

# Manifest testowy: zwykły scenariusz (regresja), source, hex oraz
# dwa celowo błędne wpisy do testów walidacji.
MANIFEST = """
[defaults]
profile  = "btz"
voltage  = "3.0"
settle_s = 5

[boards.btz]
board  = "BTZ_EndDevice/nrf54l15/cpuapp"
runner = "jlink"

[boards.dk]
board = "nrf54l15dk/nrf54l15/cpuapp"

[scenarios.zwykly]
label       = "Zwykły"
description = "Firmware z tego repo (regresja)."
cmake_args  = ["-DCONFIG_SLEEP_SYSTEM_OFF_RESET_ONLY=y"]
expected    = "~0.5 uA (DK zmierzone ~0.95 uA)"

[scenarios.zrodlowy]
label       = "Źródłowy"
description = "Aplikacja zespołu (pole source)."
source      = "app_zespolu"
cmake_args  = ["-DEXTRA_CONF_FILE=low_power.conf"]

[scenarios.hexowy]
label       = "Hexowy"
description = "Gotowa binarka (pole hex)."
hex         = "gotowe/firmware.hex"

[scenarios.zly_oba]
label       = "Błędny: source i hex"
description = "source i hex naraz – ma nie przejść walidacji."
source      = "app_zespolu"
hex         = "gotowe/firmware.hex"

[scenarios.zly_brak_pliku]
label       = "Błędny: brak pliku"
description = "hex wskazuje nieistniejący plik."
hex         = "gotowe/nie_ma.hex"
"""

# Fałszywe narzędzie: loguje wywołanie, `west topdir` zwraca błąd
# (repo testowe leży "poza workspace'em" -> szukanie SDK w HOME),
# a `west build -d <dir>` tworzy <dir>/zephyr/zephyr.hex jak prawdziwy
# west (od tego zależy pomijanie gotowych buildów).
FAKE_TOOL = """#!/bin/bash
echo "$(basename "$0") $*" >> "$CMD_LOG"
[ "$1" = "topdir" ] && exit 1
if [ "$1" = "build" ]; then
  prev=""; d=""
  for a in "$@"; do [ "$prev" = "-d" ] && d="$a"; prev="$a"; done
  [ -n "$d" ] && mkdir -p "$d/zephyr" && touch "$d/zephyr/zephyr.hex"
fi
exit 0
"""


class FakeEnv:
    """Tymczasowe repo + fałszywe west/nrfutil/SDK; podmienia HOME, PATH
    i stałe modułu power_test (ROOT, MANIFEST_PATH, CSV_PATH)."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bpt-test-")
        base = Path(self._tmp.name).resolve()

        self.repo = base / "repo"
        (self.repo / "gotowe").mkdir(parents=True)
        app = self.repo / "app_zespolu"
        app.mkdir()
        (app / "CMakeLists.txt").write_text("# atrapa aplikacji zespołu\n")
        (app / "prj.conf").write_text("# atrapa\n")
        self.source_dir = app
        self.hex_path = self.repo / "gotowe" / "firmware.hex"
        self.hex_path.write_text(":00000001FF\n")
        (self.repo / "scenarios.toml").write_text(MANIFEST)

        self.home = base / "home"
        self.workspace = self.home / "ncs" / "v3.4.0"
        (self.workspace / ".west").mkdir(parents=True)

        fakebin = base / "bin"
        fakebin.mkdir()
        for tool in ("west", "nrfutil"):
            path = fakebin / tool
            path.write_text(FAKE_TOOL)
            path.chmod(0o755)
        self.log = base / "cmd.log"
        self.log.touch()

        self._saved_env = dict(os.environ)
        os.environ["HOME"] = str(self.home)
        os.environ["PATH"] = f"{fakebin}:/usr/bin:/bin"
        os.environ["CMD_LOG"] = str(self.log)
        for var in ("NCS_WORKSPACE", "NCS_VERSION", "BOARD_ROOT",
                    "BPT_SAVED_PYTHONHOME", "BPT_SAVED_PYTHONPATH"):
            os.environ.pop(var, None)

        self._saved_core = (core.ROOT, core.MANIFEST_PATH, core.CSV_PATH)
        core.ROOT = self.repo
        core.MANIFEST_PATH = self.repo / "scenarios.toml"
        core.CSV_PATH = self.repo / "reports" / "pomiary.csv"

    def commands(self):
        """Zalogowane wywołania fałszywych narzędzi (jedna linia = jedno)."""
        return [line for line in self.log.read_text().splitlines() if line]

    def cleanup(self):
        core.ROOT, core.MANIFEST_PATH, core.CSV_PATH = self._saved_core
        os.environ.clear()
        os.environ.update(self._saved_env)
        self._tmp.cleanup()
