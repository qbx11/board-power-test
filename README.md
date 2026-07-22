# board-power-test

Narzędzie do pomiaru poboru prądu płytek Nordic za pomocą PPK2. Jedna
komenda buduje czysty obraz pomiarowy, wgrywa go i prowadzi przez pomiar
w nRF Connect Power Profiler. **Pomiar wykonujesz w Power Profilerze** — narzędzie
zapisuje odczyt do wspólnego dziennika `reports/pomiary.csv`.

## Instalacja

```sh
git clone https://github.com/qbx11/board-power-test.git
cd board-power-test
./scripts/install.sh        # symlink w ~/.local/bin, bez sudo
board-power-test            # start
```

Wymagania:
- Python ≥ 3.11
- SDK NCS v3.4.0 (inna lokalizacja: `export NCS_WORKSPACE=/ścieżka`)
- nrfutil — gdy `west` nie jest w PATH, launcher sam uruchomi środowisko NCS
- J-Link (flash) oraz PPK2 + nRF Connect Power Profiler (pomiar)
- płytka `BTZ_EndDevice`: repo `Projekt-BLE-Mesh` (`export BOARD_ROOT=...`)

## Dodawanie własnego kodu

Najszybsza droga — jedna komenda albo przycisk:

```sh
board-power-test add ../moj-projekt/app                      # katalog aplikacji -> build west-em
board-power-test add ../moj-projekt/build/zephyr/zephyr.hex  # gotowa binarka -> bez budowania
```

Narzędzie samo rozpoznaje rodzaj (katalog z `CMakeLists.txt` = `source`,
plik `.hex` = `hex`), dopisuje wpis do `scenarios.toml` i podaje komendę
uruchomienia; opcje: `--name`, `--label`, `--desc`. To samo w interfejsie:
przycisk **„Dodaj kod"** — ścieżkę wpisujesz/wklejasz albo wskazujesz
w eksploratorze plików (przycisk „Przeglądaj…"); nowy scenariusz od razu
pojawia się na liście (zaznaczony). Wpis można potem doszlifować ręcznie —
szczegóły poniżej. Scenariusz usuniesz krzyżykiem **✕** przy jego nazwie
(wpis znika z `scenarios.toml`; zebrane pomiary w CSV zostają).

Gotowe buildy nie budują się ponownie: gdy `build_<scenariusz>/` zawiera już
obraz zbudowany tą samą komendą, faza builda go pomija. Zmiany w samych
źródłach nie są śledzone — świeży build wymusza `--pristine` (CLI) albo
„Wymuś pełny rebuild" (interfejs).

Po wgraniu płytka jest **resetowana przez J-Link** (`west flash --reset`,
a dla gotowych `.hex` `nrfutil ... reset=RESET_SYSTEM`), żeby firmware
wystartował od razu, a nie dopiero po ręcznym resecie — inaczej pomiar łapie
stan sprzed restartu. Reset jest domyślnie włączony; wyłącza go `--no-reset`
(CLI) albo odznaczenie „Zresetuj płytkę po wgraniu" (interfejs).

Przed pomiarem narzędzie **przypomina o odpięciu programatora** (SWD/J-Link
dodaje własny prąd i psuje pomiar minimum). Kto mierzy bez podłączonego
debuggera, może to przypomnienie wyłączyć: `--no-swd-reminder` (CLI) albo
odznaczenie „Przypomnij o odpięciu programatora" (interfejs).

Każdy pomiar to **scenariusz** — wpis `[scenarios.<nazwa>]` w `scenarios.toml`
(klucz identyfikuje scenariusz w CSV, nie zmieniaj go po zebraniu pomiarów).
Firmware pochodzi z jednego z trzech źródeł.

**1. Firmware z repo + flagi Kconfig.** Domyślnie budowany jest `src/main.c`;
tryb snu i parametry wybierasz flagami. Własny `.conf`/overlay wrzuć do
`scenarios/custom/`.

```toml
[scenarios.moj_test]
label       = "Mój test — regulator X"     # nazwa w interfejsie (opc.)
description = "System OFF + overlay wyłączający regulator X."
cmake_args  = [
  "-DCONFIG_SLEEP_SYSTEM_OFF_RESET_ONLY=y",
  "-DEXTRA_CONF_FILE=scenarios/custom/moj_test.conf",
  "-DDTC_OVERLAY_FILE=scenarios/custom/moj_test.overlay",
]
expected    = "~0.5 uA"     # tylko wyświetlane
voltage     = "1.8"         # opc. napięcie [V]
```

Tryby snu (flaga `-DCONFIG_<...>=y`, jeden na obraz):

| Kconfig | Tryb |
| --- | --- |
| `SLEEP_SYSTEM_OFF_RESET_ONLY` | System OFF, wybudzenie tylko resetem (minimum) |
| `SLEEP_SYSTEM_ON_IDLE` | System ON idle (RAM + RTC podtrzymane) |
| `SLEEP_SYSTEM_OFF_WAKE_GPIO` | System OFF + wybudzenie przyciskiem `sw0` |
| `SLEEP_SYSTEM_OFF_RAM_RETAINED` | System OFF + retencja regionu RAM |
| `SLEEP_PERIODIC_SYSTEM_ON` | Cykl: błysk aktywności + sen w System ON |
| `SLEEP_PERIODIC_SYSTEM_OFF` | Cykl: błysk aktywności + sen w System OFF (reboot) |

Periodyczne strojisz flagami `-DCONFIG_PERIODIC_PERIOD_MS=10000` (okres) i
`-DCONFIG_PERIODIC_ACTIVE_MS=5` (długość błysku).

**2. Własna aplikacja (`source`).** Buduje CUDZY katalog Zephyr/NCS zamiast repo;
`cmake_args` (opcjonalne) trafiają do tego builda.

```toml
[scenarios.moja_aplikacja]
description = "Build aplikacji zespołu i pomiar jak zwykle."
source      = "../moj-projekt/app"                # katalog z CMakeLists.txt
cmake_args  = ["-DEXTRA_CONF_FILE=low_power.conf"] # opc., względem tej aplikacji
```

**3. Gotowa binarka (`hex`).** Pomija budowanie — narzędzie tylko programuje
układ (z pełnym kasowaniem). `cmake_args` zabronione.

```toml
[scenarios.gotowy_obraz]
description = "Pomiar obrazu zbudowanego poza narzędziem."
hex         = "../moj-projekt/build/zephyr/zephyr.hex"
```

Pozostałe pola wpisu: `expected` (wartość wyświetlana przy pomiarze), `settle_s`
(czas ustabilizowania [s]), `note` (uwaga w instrukcji). `source` i `hex`
wykluczają się; ścieżki sprawdzane są **przed** budowaniem. Uruchomienie:
`board-power-test run moj_test` (albo zaznacz w interfejsie).

## Dodawanie własnej płytki

Płytkę opisuje **profil** `[boards.<nazwa>]`. Wybierasz go flagą `-p <nazwa>`
albo w menu; domyślny ustawia `[defaults].profile`.

```toml
[boards.mojaplytka]
board      = "moja_plytka/nrf54l15/cpuapp"   # target `west build -b`
runner     = "jlink"                          # opc.: `west flash -r` (DK pomiń)
board_root = "../moj-projekt/app"             # opc.: katalog z boards/<definicja>;
                                              #       nadpisuje go export BOARD_ROOT
```

**Overlay płytki.** Region retencji RAM (dla scenariusza `ram_retained`) i inne
poprawki sprzętowe wstaw w `boards/`. Nazwa pliku musi odpowiadać targetowi —
ukośniki/myślniki zamień na podkreślenia — wtedy Zephyr aplikuje go automatycznie:

```
"moja_plytka/nrf54l15/cpuapp" → boards/moja_plytka_nrf54l15_cpuapp.overlay
```

Wzorzec regionu retencji jest w `boards/nrf54l15dk_nrf54l15_cpuapp.overlay`
(dostosuj adresy do mapy RAM swojej płytki). Pomiar na nowej płytce:
`board-power-test run reset_only -p mojaplytka`.

## Testy

```sh
.venv/bin/python -m unittest discover -s tools/power-test/tests
```
