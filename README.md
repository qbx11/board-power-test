# board-power-test

Narzędzie zespołowe do sprawdzania, czy **customowa płytka z nRF54L15 jest dobrze
zaprojektowana pod kątem poboru prądu**. Jedna komenda w terminalu prowadzi przez
cały proces: build czystego obrazu pomiarowego → flash → pomiar PPK2 → wspólny
dziennik wyników. Porównanie tej samej macierzy trybów na płytce referencyjnej (DK)
i na nowej płytce szybko ujawnia błędy designu (upływy, wiszące piny, zasilane
peryferia), które inaczej łatwo przeoczyć.

Zasada działania: firmware wprowadza układ w **jeden, wybrany na etapie budowania
tryb uśpienia i nie robi nic więcej** (osobny obraz na tryb — w obrazie pomiarowym
nie ma konsoli, logów ani RTT, więc pomiar jest czysty). Narzędzie **nie mierzy
prądu i niczego nie ocenia** — pomiar robisz w nRF Connect **Power Profiler**
(PPK2), a wynik wpisujesz do prowadzonego przez narzędzie dziennika.

## Szybki start

```sh
git clone https://github.com/qbx11/board-power-test.git
cd board-power-test
./scripts/install.sh        # symlink w ~/.local/bin, bez sudo
board-power-test            # menu: profil płytki -> scenariusze -> jedziemy
```

Wymagania:
- **Python ≥ 3.11** (tylko stdlib, zero `pip install`),
- **nrfutil** — o środowisko NCS nie musisz dbać: gdy `west` nie jest w PATH,
  launcher sam je uruchomi, a brakujące elementy (`toolchain-manager`, toolchain
  NCS v3.4.0) zaproponuje doinstalować; alternatywnie pracuj w terminalu nRF Connect,
- **J-Link** (flash) i **PPK2 + nRF Connect Power Profiler** (pomiar),
- dla `BTZ_EndDevice`: repo z definicją płytki (`Projekt-BLE-Mesh`) — domyślnie
  szukane w `../NCS-Projects/Projekt-BLE-Mesh/app`, inny układ katalogów:
  `export BOARD_ROOT=/ścieżka/do/Projekt-BLE-Mesh/app`.

## Codzienne użycie

```sh
board-power-test                      # bez argumentów: MENU (tryb prowadzony)
board-power-test list                 # dostępne scenariusze
board-power-test run reset_only idle  # wybrane scenariusze
board-power-test run --all            # cała macierz trybów
board-power-test run --all -p dk      # na płytce referencyjnej DK
board-power-test run reset_only -n    # dry-run: tylko pokaż komendy
board-power-test report               # tabela zebranych pomiarów
```

Przydatne flagi `run`: `-s "BTZ #2"` (egzemplarz płytki bez pytania),
`--no-erase` (flash bez kasowania), `-n` (dry-run). Zmienne środowiskowe:
`BOARD_ROOT`, `NCS_VERSION` (dom. v3.4.0), `BPT_NO_NCS_LAUNCH=1` (nie startuj
środowiska NCS automatycznie).

### Przebieg jednego scenariusza

1. **Build** — `west build` (pristine, osobny katalog `build_<scenariusz>/`).
2. **Flash** — `west flash --erase`; kasowanie jest domyślne, bo stan pinów
   i UICR potrafi zostać z poprzedniego obrazu i zafałszować pomiar.
3. **Instrukcja pomiaru** — napięcie, czas ustabilizowania, wartość oczekiwana
   wg datasheetu (tylko do porównania na oko — narzędzie nie ocenia).
4. **Twarde potwierdzenie odłączenia SWD** — trzeba wpisać `tak`; podłączony
   debugger dodaje własny prąd i unieważnia pomiar minimum.
5. **Wpis wyniku** z Power Profilera → wiersz w `reports/pomiary.csv`
   (egzemplarz płytki, napięcie, flagi builda, uwagi). **Commituj ten plik** —
   to wspólna historia pomiarów zespołu.

### Metodyka: najpierw DK, potem nowa płytka

Przy każdej nowej płytce zacznij od jednego przebiegu `reset_only` na DK
(`run reset_only -p dk`). DK ma znany dobry wynik (~0,95 µA), więc ten przebieg
weryfikuje **procedurę i sprzęt pomiarowy** — dopiero potem mierz nową płytkę.
Inaczej nie wiesz, czy dziwny wynik to płytka, czy błąd pomiaru.

## Scenariusze

**Statyczne** (chip zasypia i nie robi nic — pomiar samego prądu snu):

| Scenariusz | Co robi firmware | Odpowiednik w datasheet |
|---|---|---|
| `reset_only` *(dom.)* | `sys_poweroff()`; wybudzenie tylko reset/pin | **System OFF — absolutne minimum** |
| `idle` | `k_sleep(K_FOREVER)`; wątek idle → WFI, RAM + LFCLK/RTC aktywne | System ON, IDLE (RAM+RTC) |
| `wake_gpio` | `sys_poweroff()`; wybudzenie przyciskiem `sw0` (SENSE) | System OFF + wybudzenie GPIO |
| `ram_retained` | `sys_poweroff()` + retencja regionu RAM z devicetree | System OFF z retencją RAM |

**Periodyczne** (symulacja „budzę się co T, wysyłam, śpię" — pomiar prądu
**średniego** przez pełny cykl):

| Scenariusz | Co robi firmware | Wzorzec dla |
|---|---|---|
| `periodic_on` | pętla: błysk aktywności → `k_sleep(T)` (System ON, program kontynuuje) | częstych wysyłek |
| `periodic_off` | błysk → uzbrojenie **GRTC** na T → `sys_poweroff()` (reboot co cykl) | rzadkich wysyłek |

Parametry periodyków (w `scenarios.toml`, flagi `-DCONFIG_PERIODIC_PERIOD_MS` /
`-DCONFIG_PERIODIC_ACTIVE_MS`): okres T (dom. 10 s) i długość błysku CPU (dom. 5 ms).
Błysk to tylko zastępcza praca (`k_busy_wait`) — dla miarodajnego wyniku zastąp
`activity_burst()` w `src/main.c` swoim realnym kodem (odczyt czujnika + TX).

**Który periodyk wygrywa?** Liczy się prąd średni: System ON nie płaci za reboot
(wygrywa przy częstych wysyłkach), System OFF ma niższy prąd snu, ale każde
wybudzenie to pełny reboot (wygrywa przy rzadkich). Zmierz oba dla kilku T
(1 s / 10 s / 60 s / 300 s) i znajdź punkt przecięcia dla swojego sprzętu.

## Pomiar PPK2 (tryb Source meter — zasilanie samego SoC)

> Cel: zasilić **wyłącznie** nRF54L15, żeby prąd interfejsu debug i regulatorów
> płytki nie zaburzał odczytów rzędu sub-µA.

1. W nRF Connect **Power Profiler** ustaw PPK2 w tryb **Source meter** i napięcie
   docelowe (np. 3,0 V — narzędzie wypisze wartość ze scenariusza).
2. Zasil SoC **tylko** z PPK2: `VOUT → VDD`, `GND ↔ GND`. Odłącz inne zasilanie
   płytki/USB; na DK odetnij odpowiednią zworkę zasilania SoC (patrz User Guide,
   „Measuring current").
3. Flash idzie przez debugger (płytka musi być wtedy zasilona!), a na czas pomiaru
   **odłącz przewód SWD** — narzędzie wymusza potwierdzenie tego kroku.
4. Odczytaj **średni prąd** (przy periodykach: uśredniaj przez kilka pełnych
   okresów T) i wpisz go w narzędziu. Dla `wake_gpio` sprawdź dodatkowo, że
   `sw0` wybudza układ (chwilowy skok prądu) i sen wraca.

### Oczekiwane rzędy wielkości (weryfikuj z datasheetem nRF54L15)

- `reset_only`: **~0,3–0,5 µA** (DK zmierzone: ~0,95 µA),
- `idle`: **~1–2 µA** (zależnie od LFXO/LFRC),
- `ram_retained`: wyraźnie wyżej — rośnie z ilością utrzymywanego RAM (dom. region 4 KB).

Dokładne liczby zależą od napięcia, temperatury, DC/DC vs LDO i źródła LFCLK — to
te warunki z datasheetu są punktem odniesienia. Przykład realnego dochodzenia
(BTZ_EndDevice: 7,3 µA zamiast ~1 µA i dlaczego): `docs/analiza-btz-enddevice.md`.

## Własne scenariusze

Scenariusze definiuje **`scenarios.toml`** — dodanie własnego to skopiowanie wpisu
i podmiana flag (żadnych zmian w narzędziu):

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

Własne pliki conf/overlay wrzucaj do `scenarios/custom/`. Zupełnie nowy wariant
firmware = nowa pozycja w `Kconfig` (choice) + gałąź w `src/main.c` — utrzymuj
zasadę „jeden czysty obraz na scenariusz".

## Profile płytek i dodanie własnej

Profile są w `scenarios.toml`: `[boards.btz]` (BTZ_EndDevice, runner `jlink`,
board_root z repo `Projekt-BLE-Mesh`) i `[boards.dk]` (DK referencyjny, definicja
w SDK). Wybór: menu albo `-p <profil>`; `BOARD_ROOT` z env nadpisuje manifest.

Nowa własna płytka nRF54L15:
1. W jej devicetree zadbaj o: alias `sw0` (dla `wake_gpio`), DC/DC na `&vregmain`,
   źródło LFCLK, region `compatible = "zephyr,retained-ram"` (dla `ram_retained`).
2. Dodaj `boards/<twoja_płytka>_nrf54l15_cpuapp.overlay` (wzór: pliki w `boards/`).
3. Dopisz `[boards.<nazwa>]` w `scenarios.toml` (board, runner, ew. board_root).

## Struktura repo

```
board-power-test/
├── bin/board-power-test   # globalny launcher (auto-start środowiska NCS)
├── scripts/install.sh     # instalacja komendy (symlink ~/.local/bin)
├── scripts/build.sh       # ręczny build jednego trybu (bez narzędzia)
├── tools/power-test/      # CLI: build -> flash -> pomiar -> dziennik CSV
├── scenarios.toml         # manifest scenariuszy i profili płytek
├── scenarios/custom/      # własne conf/overlay dla scenariuszy custom
├── reports/               # pomiary.csv – dziennik pomiarów (commituj!)
├── src/main.c             # firmware pomiarowe (jeden tryb na obraz)
├── Kconfig                # choice: wybór trybu snu (build-time)
├── prj.conf               # baza: konsola/serial/logi/RTT WYŁĄCZONE
├── debug.conf / debug_rtt.conf   # buildy diagnostyczne (NIE do pomiaru!)
├── boards/                # overlaye płytek (retencja RAM itd.)
└── docs/                  # analizy i raporty pomiarów
```

## Budowanie ręczne (bez narzędzia)

Narzędzie woła dokładnie to samo, co zrobiłbyś ręcznie — w razie potrzeby:

```sh
./scripts/build.sh reset_only          # tryby: idle|wake_gpio|reset_only|ram_retained|periodic_on|periodic_off
west flash -d build_reset_only -r jlink

# albo w pełni ręcznie:
west build -b BTZ_EndDevice/nrf54l15/cpuapp -p always -d build_reset_only \
  -- -DBOARD_ROOT=$BOARD_ROOT -DCONFIG_SLEEP_SYSTEM_OFF_RESET_ONLY=y
```

## Sanity-check firmware (RTT, J-Link)

Żeby zobaczyć, że firmware żyje (NIE do pomiaru — RTT wymaga podłączonego J-Linka):

```sh
./scripts/build.sh wake_gpio --rtt
west flash -d build_wake_gpio -r jlink
```

W **J-Link RTT Viewer**: Connect → nRF54L15 (Cortex-M33), SWD. Zobaczysz banner
i `Tryb: ...`; w trybach System OFF chip milknie po zaśnięciu (przy `periodic_off`
włącz auto-reconnect — co T jest reboot i nowy banner), `periodic_on` wypisuje
`wybudzenie #N`, `idle` — jeden komunikat i cisza.

## Rozwiązywanie problemów

- **`nrfutil command 'toolchain-manager' not found`** — launcher sam zaproponuje
  instalację; ręcznie: `nrfutil install toolchain-manager`, potem (jeśli trzeba)
  `nrfutil toolchain-manager install --ncs-version v3.4.0`.
- **Prąd wyraźnie wyższy niż w datasheecie** — sprawdź: SWD odłączony? PPK2 w
  Source meter i zasila *tylko* SoC? DC/DC (nie LDO)? build bez
  `debug.conf`/`debug_rtt.conf`? Porównaj z przebiegiem kontrolnym na DK; jeśli DK
  trafia w datasheet, a Twoja płytka nie — problem jest w sprzęcie płytki
  (przykład dochodzenia: `docs/analiza-btz-enddevice.md`).
- **Ostrzeżenie o `board_root`** — ustaw `export BOARD_ROOT=/ścieżka/do/Projekt-BLE-Mesh/app`
  albo popraw ścieżkę w `scenarios.toml`.
- **`WAKE_GPIO` nie kompiluje się (`#error sw0`)** — płytka nie ma aliasu `sw0`
  w devicetree.
- **RTT Viewer nic nie pokazuje** — to build pomiarowy (bez konsoli); przebuduj
  z `--rtt`. Przy System OFF włącz auto-reconnect.
- **Testowane na NCS v3.4.0 (LTS)** — inna wersja: `NCS_VERSION=... board-power-test`.
