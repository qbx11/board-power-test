# board-power-test

Narzędzie do pomiaru poboru prądu płytek Nordic za pomocą PPK2. Jedna
komenda buduje czysty obraz pomiarowy, wgrywa go i prowadzi przez pomiar
w nRF Connect Power Profiler. **Pomiar wykonujesz w Power Profilerze** — narzędzie
zapisuje odczyt do wspólnego dziennika `reports/pomiary.csv`.

Ma też **tryb autonomiczny**: wykonuje cały plan (kolejność kodów, czasy,
warunki startu) bez udziału człowieka — sam zasila płytkę z PPK2, mierzy
prąd i rysuje wykres w osobnym oknie. Patrz [Tryb autonomiczny](#tryb-autonomiczny).

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
voltage     = "3.0"         # opc. napięcie [V]; tryb autonom.: limit 2.0–3.3 V
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

## Tryb autonomiczny

Zamiast ręcznego pomiaru w Power Profilerze narzędzie potrafi **samo**
zbudować i wgrać kolejne kody, zasilić płytkę z PPK2 (tryb source meter),
zmierzyć pobór prądu przez zadany czas i narysować wykres. Można zostawić
płytkę na całą noc.

W interfejsie przełącznik trybów na górze okna zmienia **Pomiar ręczny** na
**Tryb autonomiczny** (kliknięcie w tekst). Kreator: wybierasz płytkę, potem
wypełniasz karty **Pomiar 1, 2, …** (scenariusz — domyślnie nic nie wybrane —
i czas; start-po-czasie oraz konsola RTT są opcjonalne i schowane w zwijanych
*ustawieniach zaawansowanych* razem z napięciem i zapisem danych, domyślnie
wyłączone). „+ Dodaj pomiar" dodaje kolejną kartę i **zwija poprzednie do
jednego wiersza** (nazwa scenariusza, numer gdy się powtarza) — klik w wiersz
rozwija kartę z powrotem, więc łatwo wrócić do wcześniejszego pomiaru w długiej
liście. „Zastosuj do wszystkich" przepisuje ustawienia karty (bez scenariusza)
na wszystkie istniejące pomiary; „…do następnych" zapamiętuje je jako szablon
dla każdego **kolejno dodawanego** pomiaru. Kolejność kart = kolejność
wykonania. „Dalej → PPK2" otwiera ekran połączenia (wykrycie PPK2), a „Start"
uruchamia przebieg: **każdy kod buduje się i wgrywa tuż przed swoim pomiarem**
(nie wszystkie z góry), po czym leci pomiar i podgląd wykresu na żywo.

Z CLI (albo do powtarzalnych, wersjonowanych przebiegów) ten sam pomiar opisuje
**plan** w `plans/<nazwa>.toml` (wzór: `plans/nocny.example.toml`). Plan to
uporządkowana lista kroków; każdy wskazuje scenariusz ze `scenarios.toml`
i mówi, jak go zmierzyć:

```toml
[plan]
name  = "nocny"
board = "btz"

[[plan.steps]]
scenario = "reset_only"
duration = "8h"                             # ile mierzyć: "45s"/"20m"/"8h"/sekundy
voltage  = "3.0"                            # napięcie źródła PPK2 (limit 2.0–3.3 V)
trigger  = { type = "delay", seconds = 20 } # start pomiaru 20 s po flashu…
rtt      = "off"
storage  = { mode = "downsampled", window_ms = 1 }

[[plan.steps]]
scenario = "mesh-reliability-tester"
duration = "2h"
trigger  = { type = "rtt", pattern = "Friend established", timeout = "180s" }  # …albo po logu RTT
rtt      = "continuous"                      # etykiety z logów RTT na wykresie
  [[plan.steps.labels]]
  pattern = "Friend Poll sent"
  label   = "Friend Poll"
```

Uruchomienie planu z CLI:

```sh
board-power-test autorun plans/nocny.toml -s "BTZ #2"
board-power-test autorun plans/nocny.toml --dry-run   # pokaż kroki bez sprzętu
```

Każdy pomiar zapisuje **sesję** w `reports/sessions/<przebieg>/<czas>_<scenariusz>/`
(dane wykresu, metadane, etykiety) i wiersz podsumowania (średnia/min/max, czas,
ścieżka sesji) w `reports/pomiary.csv`.

### Okno wykresu

```sh
board-power-test viewer                       # biblioteka historii
board-power-test viewer reports/sessions/…/…  # konkretna sesja
```

Osobne okno (PyQt6 + pyqtgraph, doinstalowuje się przy pierwszym uruchomieniu):
podgląd na żywo, zoom z minimapą całości, statystyki zaznaczenia (ładunek, estymata
baterii), etykiety automatyczne (z RTT) i ręczne (klik na wykresie), porównywanie
wielu sesji, eksport PNG/CSV.

**Tryby RTT** (pole `rtt`): `off` — J-Link odpięty, najniższy szum (do minimów snu);
`trigger` — podłączony tylko do złapania wzorca startu; `continuous` — podłączony
przez cały pomiar (etykiety, ale prąd z narzutem debuggera). Szczegóły w komentarzach
`plans/nocny.example.toml`.

## Testy

```sh
.venv/bin/python -m unittest discover -s tools/power-test/tests
```
