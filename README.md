# board-power-test

Narzędzie zespołowe do pomiaru poboru prądu płytek z nRF54L15 (PPK2). Jedna
komenda prowadzi przez cały proces: build czystego obrazu pomiarowego → flash →
pomiar w nRF Connect Power Profiler → wspólny dziennik wyników
(`reports/pomiary.csv`). Narzędzie samo **nie mierzy i niczego nie ocenia** —
pomiar robisz w Power Profilerze, a wynik wpisujesz do dziennika.

## Uruchomienie

```sh
git clone https://github.com/qbx11/board-power-test.git
cd board-power-test
./scripts/install.sh        # symlink w ~/.local/bin, bez sudo
board-power-test            # interfejs okienkowy: profil -> scenariusze -> Start
```

Bez argumentów otwiera się **interfejs okienkowy w terminalu** (TUI); przy
pierwszym starcie launcher sam tworzy `.venv` z biblioteką `textual`. Bez sieci
(albo z `BPT_NO_TUI=1`) działa klasyczny tryb tekstowy — funkcje te same.

Wymagania:
- **Python ≥ 3.11**,
- **nrfutil** — gdy `west` nie jest w PATH, launcher sam uruchomi środowisko
  NCS, a braki zaproponuje doinstalować,
- **SDK NCS v3.4.0** (źródła, np. nRF Connect for VS Code → „Install SDK";
  inna lokalizacja: `export NCS_WORKSPACE=/ścieżka`),
- **J-Link** (flash) i **PPK2 + nRF Connect Power Profiler** (pomiar),
- dla `BTZ_EndDevice`: repo `Projekt-BLE-Mesh` (domyślnie szukane w
  `../NCS-Projects/Projekt-BLE-Mesh/app`; inaczej: `export BOARD_ROOT=...`).

## Codzienne użycie

```sh
board-power-test                      # bez argumentów: interfejs okienkowy (TUI)
board-power-test list                 # dostępne scenariusze
board-power-test run reset_only idle  # wybrane scenariusze (tryb CLI)
board-power-test run --all            # cała macierz trybów
board-power-test run --all -p dk      # na płytce referencyjnej DK
board-power-test run reset_only -n    # dry-run: tylko pokaż komendy
board-power-test report               # tabela zebranych pomiarów
```

Przydatne flagi `run`: `-s "BTZ #2"` (egzemplarz płytki bez pytania),
`--no-erase` (flash bez kasowania), `-n` (dry-run). Zmienne środowiskowe:
`BOARD_ROOT`, `NCS_VERSION` (dom. v3.4.0), `NCS_WORKSPACE`, `BPT_NO_TUI=1`.

Przy nowej płytce zacznij od `run reset_only -p dk` — znany dobry wynik DK
(~0,95 µA) weryfikuje procedurę i sprzęt pomiarowy, zanim zmierzysz nową płytkę.

### Przebieg (dwie fazy)

**FAZA 1 — build:** `west build` (pristine, katalog `build_<scenariusz>/`) dla
wszystkich wybranych scenariuszy z góry. Scenariusze z gotową binarką (pole
`hex`) tę fazę pomijają.

**FAZA 2 — flash + pomiar, scenariusz po scenariuszu:**
1. **Flash** — `west flash --erase` (scenariusz `hex`: `nrfutil device
   program` z pełnym kasowaniem); nieudany flash nie cofa przebiegu — dialog
   daje wybór: ponów / pomiń / przerwij.
2. **Instrukcja pomiaru** — napięcie, czas ustabilizowania, wartość oczekiwana.
3. **Potwierdzenie odłączenia SWD** — podłączony debugger dodaje własny prąd.
4. **Wpis wyniku** z Power Profilera (µA albo mA) → wiersz w
   `reports/pomiary.csv`. **Commituj ten plik** — to wspólna historia pomiarów.

## Pomiar PPK2 (tryb Source meter)

1. W Power Profilerze ustaw **Source meter** i napięcie ze scenariusza
   (np. 3,0 V).
2. Zasil **wyłącznie SoC** z PPK2: `VOUT → VDD`, `GND ↔ GND`; odłącz inne
   zasilanie (na DK odetnij zworkę zasilania SoC — patrz User Guide).
3. Flash idzie przez debugger (płytka wtedy zasilona!), na czas pomiaru
   **odłącz przewód SWD**.
4. Odczytaj **średni prąd** (periodyki: uśredniaj przez kilka pełnych okresów)
   i wpisz go w narzędziu.

## Scenariusze

Statyczne (prąd snu): `reset_only` (System OFF, minimum), `idle` (System ON,
RAM+RTC), `wake_gpio` (System OFF + wybudzenie `sw0`), `ram_retained`
(System OFF + retencja RAM). Periodyczne (prąd średni cyklu „obudź się –
wyślij – śpij"): `periodic_on` (sen w System ON) i `periodic_off` (System OFF,
reboot co cykl); okres i długość błysku ustawisz flagami
`-DCONFIG_PERIODIC_PERIOD_MS` / `-DCONFIG_PERIODIC_ACTIVE_MS` w
`scenarios.toml`. Opisy i wartości oczekiwane pokazuje `board-power-test list`
oraz TUI.

## Własne scenariusze

Scenariusze definiuje **`scenarios.toml`** — dodanie własnego to skopiowanie
wpisu i podmiana flag (wzory na dole pliku):

```toml
[scenarios.moj_test]
description = "System OFF + własny overlay z wyłączonym regulatorem X"
cmake_args  = [
  "-DCONFIG_SLEEP_SYSTEM_OFF_RESET_ONLY=y",
  "-DEXTRA_CONF_FILE=scenarios/custom/moj_test.conf",
  "-DDTC_OVERLAY_FILE=scenarios/custom/moj_test.overlay",
]
expected    = "..."     # tylko do wyświetlenia obok wyniku
voltage     = "1.8"     # opcjonalne nadpisanie napięcia pomiaru
```

Własne pliki conf/overlay wrzucaj do `scenarios/custom/`.

### Własny firmware zespołu (source / hex)

Scenariusz może mierzyć też **cudzy firmware** — dwa dodatkowe, wzajemnie
wykluczające się pola wpisu:

```toml
# wariant A: zbuduj własną aplikację Zephyr/NCS zespołu
[scenarios.moja_aplikacja]
description = "Build aplikacji zespołu i pomiar jak zwykle."
source      = "../moj-projekt/app"    # katalog z CMakeLists.txt/prj.conf
cmake_args  = ["-DEXTRA_CONF_FILE=low_power.conf"]   # opcjonalne

# wariant B: gotowa binarka – bez budowania (FAZA 1 pomijana)
[scenarios.gotowy_obraz]
description = "Pomiar obrazu zbudowanego poza narzędziem."
hex         = "../moj-projekt/build/zephyr/zephyr.hex"
```

- `source` — ścieżka względna (od katalogu repo) albo absolutna; build idzie
  zwykłym `west build` (katalog builda nadal `build_<scenariusz>/` w tym repo).
- `hex` — narzędzie tylko wgrywa wskazany plik (`nrfutil device program`,
  z pełnym kasowaniem; `--no-erase` działa jak dotąd); `cmake_args` zabronione.
- Ścieżki są sprawdzane przed startem FAZY 1; w dzienniku CSV kolumna `flagi`
  zawiera flagi builda + `source=...` albo `hex=...`.

## Profile płytek

Profile są w `scenarios.toml`: `[boards.btz]` (BTZ_EndDevice) i `[boards.dk]`
(DK referencyjny). Wybór: menu albo `-p <profil>`. Nowa płytka = overlay w
`boards/` + sekcja `[boards.<nazwa>]` (board, runner, ew. board_root).

## Testy narzędzia

```sh
.venv/bin/python -m unittest discover -s tools/power-test/tests -v
```
