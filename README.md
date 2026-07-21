# nRF54L15 – pomiar poboru energii (PPK2)

Firmware Zephyr / nRF Connect SDK, który wprowadza nRF54L15 w **jeden, wybrany na
etapie budowania tryb uśpienia i nie robi nic więcej**. Służy do pomiaru minimalnego
poboru prądu przez **Power Profiler Kit II (PPK2)** i porównania go z datasheetem.

- Wybór głębokości snu: **build-time (Kconfig)** – osobny obraz/hex na tryb, żeby w
  obrazie pomiarowym nie było włączone nic zbędnego (bez konsoli, serial, logów, RTT).
- Testowane na **NCS v3.4.0 (LTS)**.
- Docelowy target: **`BTZ_EndDevice/nrf54l15/cpuapp`** (board root:
  `Projekt-BLE-Mesh/app`). Ta płytka ma już `sw0`, DC/DC i LFXO – wszystkie warianty
  działają na niej od ręki. Płytka referencyjna do testów: `nrf54l15dk/nrf54l15/cpuapp`.

## Warianty snu

**Statyczne** (chip zasypia i nie robi nic – pomiar samego prądu snu):

| `-DCONFIG_...=y`                     | Co robi firmware                                                        | Odpowiednik w datasheet            |
|--------------------------------------|-------------------------------------------------------------------------|------------------------------------|
| `SLEEP_SYSTEM_ON_IDLE`               | `k_sleep(K_FOREVER)`; wątek idle → WFI, RAM + LFCLK/RTC aktywne          | System ON, IDLE (RAM+RTC)          |
| `SLEEP_SYSTEM_OFF_WAKE_GPIO`         | `sys_poweroff()`; wybudzenie przyciskiem `sw0` (SENSE), bez retencji RAM | System OFF + wybudzenie GPIO       |
| `SLEEP_SYSTEM_OFF_RESET_ONLY` *(dom.)* | `sys_poweroff()`; wybudzenie tylko reset/pin                            | **System OFF – absolutne minimum** |
| `SLEEP_SYSTEM_OFF_RAM_RETAINED`      | `sys_poweroff()` + retencja regionu RAM z devicetree                    | System OFF z retencją RAM          |

**Periodyczne** (symulacja „wybudzam się co T, wysyłam, śpię" – pomiar prądu
**średniego** przez pełny cykl, do porównania która strategia zużywa mniej):

| `-DCONFIG_...=y`               | Co robi firmware                                                             | Wzorzec dla            |
|--------------------------------|-----------------------------------------------------------------------------|------------------------|
| `SLEEP_PERIODIC_SYSTEM_ON`     | pętla: błysk aktywności → `k_sleep(T)` (System ON idle, program kontynuuje)  | częstych wysyłek       |
| `SLEEP_PERIODIC_SYSTEM_OFF`    | błysk aktywności → uzbrojenie **GRTC** na T → `sys_poweroff()` (reboot co cykl) | rzadkich wysyłek    |

Parametry wariantów periodycznych (Kconfig):

| Symbol | Znaczenie | Domyślnie |
|---|---|---|
| `CONFIG_PERIODIC_PERIOD_MS` | okres T między wybudzeniami [ms] | `10000` (10 s) |
| `CONFIG_PERIODIC_ACTIVE_MS` | długość błysku CPU (zamiennik pracy/TX) [ms] | `5` |

Domyślny tryb (bez podania `-DCONFIG_...`) to `SLEEP_SYSTEM_OFF_RESET_ONLY`.

## Struktura

```
board-power-test/
├── CMakeLists.txt
├── Kconfig            # choice: wybór trybu snu (build-time)
├── prj.conf           # baza: konsola/serial/logi/RTT WYŁĄCZONE
├── debug.conf         # sanity przez UART (NIE do pomiaru!)
├── debug_rtt.conf     # sanity przez RTT / J-Link RTT Viewer (NIE do pomiaru!)
├── scenarios.toml     # manifest scenariuszy dla narzędzia power-test
├── scenarios/custom/  # własne conf/overlay dla scenariuszy custom
├── bin/board-power-test   # globalny launcher (auto-start środowiska NCS)
├── scripts/install.sh # instalacja komendy `board-power-test` (symlink ~/.local/bin)
├── scripts/build.sh   # buduje wybrany tryb jedną komendą (bez GUI)
├── tools/power-test/  # CLI: build -> flash -> pomiar -> dziennik CSV
├── reports/           # pomiary.csv – dziennik pomiarów (tworzony przez narzędzie)
├── src/main.c
└── boards/
    └── nrf54l15dk_nrf54l15_cpuapp.overlay   # region retencji RAM (dla wariantu RAM_RETAINED)
```

## Narzędzie zespołowe: `power-test` (build → flash → pomiar → dziennik)

CLI dla zespołu do szybkiego przetestowania nowej płytki we wszystkich trybach snu.
**Nie mierzy prądu i niczego nie ocenia** – pomiar robisz jak dotąd w nRF Connect
Power Profiler; narzędzie automatyzuje build+flash czystych obrazów, prowadzi przez
procedurę pomiaru (w tym twarde „odłącz SWD") i zapisuje wyniki do wspólnej tabeli.

Wymagania: Python ≥ 3.11 (bez `pip install` – tylko stdlib). O `west` nie musisz
dbać – launcher sam startuje środowisko NCS, gdy trzeba (patrz niżej).

**Instalacja (raz, bez sudo):**

```sh
./scripts/install.sh          # symlink w ~/.local/bin
```

Od tej pory w **dowolnym terminalu i katalogu** wpisujesz po prostu:

```sh
board-power-test                      # bez argumentów: MENU (tryb prowadzony)
board-power-test list                 # dostępne scenariusze
board-power-test run reset_only idle  # wybrane scenariusze
board-power-test run --all            # cała macierz trybów
board-power-test run --all -p dk      # na płytce referencyjnej DK
board-power-test run reset_only -n    # dry-run: tylko pokaż komendy
board-power-test report               # tabela zebranych pomiarów
```

Gdy `west` nie jest w PATH (zwykły terminal), launcher **sam** uruchamia CLI
wewnątrz środowiska NCS przez `nrfutil toolchain-manager` (domyślnie v3.4.0;
inna wersja: `NCS_VERSION=v3.5.0 board-power-test`, wyłączenie auto-startu:
`BPT_NO_NCS_LAUNCH=1`). Bez instalacji narzędzie działa też po staremu:
`python3 tools/power-test/power_test.py ...` z katalogu projektu.

Przebieg jednego scenariusza: `west build` (pristine, osobny `build_<scenariusz>/`)
→ `west flash --erase` (kasowanie domyślnie – stan pinów/UICR zostaje z poprzedniego
obrazu; wyłączenie: `--no-erase`) → instrukcja pomiaru (napięcie, czas ustabilizowania,
wartość oczekiwana wg datasheetu) → **wymagane potwierdzenie odłączenia SWD** →
wpisanie odczytu z Power Profilera → wiersz w `reports/pomiary.csv` (commituj do repo –
to wspólna historia pomiarów; kolumny m.in. egzemplarz płytki, napięcie, flagi builda).

Scenariusze (standardowe i własne) definiuje **`scenarios.toml`** – dodanie własnego
to skopiowanie wpisu i podmiana flag (wzór z `EXTRA_CONF_FILE`/`DTC_OVERLAY_FILE`
na dole pliku; własne conf/overlay wrzuć do `scenarios/custom/`). Tam też profile
płytek (`[boards.btz]`, `[boards.dk]`) i domyślne napięcie pomiaru. Board root:
env `BOARD_ROOT` nadpisuje wartość z manifestu (jak w `scripts/build.sh`).

## Budowanie

> Buduj w **terminalu nRF Connect** (gdzie `west` jest w PATH) albo w **nRF Connect for
> VS Code**. Tryb snu wybierasz jedną flagą `-DCONFIG_<TRYB>=y` — bez żadnego Kconfig GUI.

### Najprościej: skrypt `scripts/build.sh`
Uruchom **z katalogu projektu, w terminalu nRF Connect**. Pod każdym przykładem builda
gotowa komenda do wgrania (J-Link mini EDU) — kopiuj obie linie razem:
```sh
./scripts/build.sh reset_only              # absolutne minimum (domyślny wzorzec)
west flash -d build_reset_only -r jlink

./scripts/build.sh idle                     # System ON idle
west flash -d build_idle -r jlink

./scripts/build.sh periodic_off 60000 5     # System OFF co 60 s, błysk 5 ms
west flash -d build_periodic_off -r jlink

./scripts/build.sh periodic_on  10000 5     # System ON co 10 s, błysk 5 ms
west flash -d build_periodic_on -r jlink

./scripts/build.sh reset_only --rtt         # wersja z logami RTT (sanity, patrz niżej)
west flash -d build_reset_only -r jlink
```

Skrypt zakłada, że repo z płytką (`Projekt-BLE-Mesh`) leży w `../NCS-Projects/Projekt-BLE-Mesh`
**obok** tego projektu. Jeśli masz inny układ katalogów, wskaż board root zmienną środowiskową:
```sh
BOARD_ROOT=/ścieżka/do/Projekt-BLE-Mesh/app ./scripts/build.sh reset_only
```

### Co znaczy komenda (jeśli robisz ręcznie)
```
west build  -b BTZ_EndDevice/nrf54l15/cpuapp  -p always  --  -DCONFIG_SLEEP_SYSTEM_OFF_RESET_ONLY=y
     │             │                              │                    │
   buduj      która płytka                    pristine       który tryb snu (jedna flaga)
```
Zmieniasz tylko flagę `-DCONFIG_...=y` (z tabeli „Warianty snu"); dla periodycznych
dokładasz `-DCONFIG_PERIODIC_PERIOD_MS=...`. Definicja płytki `BTZ_EndDevice` leży w innym
repo, więc dołóż `-DBOARD_ROOT=<ścieżka>/Projekt-BLE-Mesh/app` (patrz sekcja B).

### A) nRF Connect for VS Code (bez Kconfig GUI)
1. *Add build configuration* → SDK **v3.4.0**, board `BTZ_EndDevice/nrf54l15/cpuapp`
   (upewnij się, że board root wskazuje `Projekt-BLE-Mesh/app`).
2. Wybór trybu snu — w polu **„Extra CMake arguments”** wpisz jedną flagę, np.
   `-DCONFIG_SLEEP_SYSTEM_OFF_RESET_ONLY=y` (dla periodycznych dołóż
   `-DCONFIG_PERIODIC_PERIOD_MS=60000`). To działa **bez** żadnego Kconfig GUI.
3. *Build*. Flash: użyj przycisku *Flash* (albo `west flash -r jlink` dla J-Link mini EDU).

> Alternatywnie, jeśli Twoja wersja rozszerzenia pokazuje akcję *Kconfig* / *menuconfig*,
> tryb jest w menu *„nRF54L15 power measurement → Głębokość snu do pomiaru”*. Ale nie
> jest potrzebne — flaga w punkcie 2 wystarcza.

### B) Wiersz poleceń (terminal nRF Connect, gdzie `west` jest w PATH)
Ustaw RAZ ścieżkę do repo z definicją płytki. Pod każdą komendą `west build` jest od razu
gotowa komenda `west flash` (J-Link mini EDU) — kopiuj obie linie razem:
```sh
export BOARD_ROOT=$HOME/Projects/NCS-Projects/Projekt-BLE-Mesh/app   # <- popraw na swoją ścieżkę (przykład, nie kopiuj 1:1)

# --- warianty statyczne (po jednym pełnym poleceniu na tryb) ---
west build -b BTZ_EndDevice/nrf54l15/cpuapp -p always -d build_reset_only   -- -DBOARD_ROOT=$BOARD_ROOT -DCONFIG_SLEEP_SYSTEM_OFF_RESET_ONLY=y
west flash -d build_reset_only -r jlink

west build -b BTZ_EndDevice/nrf54l15/cpuapp -p always -d build_idle         -- -DBOARD_ROOT=$BOARD_ROOT -DCONFIG_SLEEP_SYSTEM_ON_IDLE=y
west flash -d build_idle -r jlink

west build -b BTZ_EndDevice/nrf54l15/cpuapp -p always -d build_wake_gpio    -- -DBOARD_ROOT=$BOARD_ROOT -DCONFIG_SLEEP_SYSTEM_OFF_WAKE_GPIO=y
west flash -d build_wake_gpio -r jlink

west build -b BTZ_EndDevice/nrf54l15/cpuapp -p always -d build_ram_retained -- -DBOARD_ROOT=$BOARD_ROOT -DCONFIG_SLEEP_SYSTEM_OFF_RAM_RETAINED=y
west flash -d build_ram_retained -r jlink

# --- warianty periodyczne (okres T i długość błysku w ms) ---
west build -b BTZ_EndDevice/nrf54l15/cpuapp -p always -d build_periodic_on  -- -DBOARD_ROOT=$BOARD_ROOT -DCONFIG_SLEEP_PERIODIC_SYSTEM_ON=y  -DCONFIG_PERIODIC_PERIOD_MS=10000 -DCONFIG_PERIODIC_ACTIVE_MS=5
west flash -d build_periodic_on -r jlink

west build -b BTZ_EndDevice/nrf54l15/cpuapp -p always -d build_periodic_off -- -DBOARD_ROOT=$BOARD_ROOT -DCONFIG_SLEEP_PERIODIC_SYSTEM_OFF=y -DCONFIG_PERIODIC_PERIOD_MS=10000 -DCONFIG_PERIODIC_ACTIVE_MS=5
west flash -d build_periodic_off -r jlink
```
Płytka referencyjna DK: usuń `-DBOARD_ROOT=$BOARD_ROOT` i zmień `-b` na
`-b nrf54l15dk/nrf54l15/cpuapp`. To **ten sam kod** (`src/main.c`, ten sam wariant) —
inny jest tylko cel budowania, więc buduj do osobnego katalogu (`build_dk_*`). DK ma
wbudowany debugger, więc `west flash` sam wybierze runner (bez `-r jlink`):
```sh
# reset_only na nRF54L15 DK (bez BOARD_ROOT — definicja DK jest w SDK)
west build -b nrf54l15dk/nrf54l15/cpuapp -p always -d build_dk_reset_only -- -DCONFIG_SLEEP_SYSTEM_OFF_RESET_ONLY=y
west flash -d build_dk_reset_only
```

### C) `west` nie jest w PATH?
Otwórz powłokę z gotowym środowiskiem NCS (bez ręcznego ustawiania ścieżek), a potem
buduj jak w sekcji A/B:
```sh
nrfutil toolchain-manager launch --ncs-version v3.4.0 --shell
# teraz `west` działa:
cd <ten-projekt>
./scripts/build.sh reset_only
```

## Pomiar PPK2 (tryb Source meter – zasilanie samego SoC)

> Cel: zasilić **wyłącznie** nRF54L15, żeby prąd interfejsu debug i regulatorów płytki
> nie zaburzał odczytów rzędu sub-µA.

1. W nRF Connect **Power Profiler** ustaw PPK2 w tryb **Source meter** i napięcie
   docelowe (np. 1,8 V lub 3,0 V – zgodnie z warunkami z datasheetu, do których
   porównujesz).
2. Zasil SoC **tylko** z PPK2: `VOUT → VDD` układu, `GND ↔ GND`. Odłącz własne
   zasilanie płytki/USB DK i (na DK) odetnij odpowiednią zworkę zasilania SoC, aby
   reszta płytki nie była zasilana. *(na DK: patrz User Guide, „Measuring current”)*.
3. Wgraj obraz danego trybu przez debugger, a następnie **odłącz przewód SWD** na czas
   pomiaru – podłączony debugger potrafi dodać własny prąd.
4. Odczytaj średni prąd. Dla wariantu `WAKE_GPIO` sprawdź też: naciśnięcie `sw0`
   wybudza układ (chwilowy skok prądu) i wraca on do snu.

## Oczekiwane wartości vs datasheet

Rzędy wielkości (typowe, do zweryfikowania z datasheetem nRF54L15, sekcja
*„Power management / Current consumption”*):

- **System OFF, bez retencji RAM** (`RESET_ONLY`): rząd **~0,3–0,5 µA**.
- **System OFF z retencją RAM** (`RAM_RETAINED`): wyraźnie wyżej – rośnie z ilością
  utrzymywanego RAM.
- **System ON, IDLE** (`SYSTEM_ON_IDLE`): rząd **~1–2 µA** (zależnie od LFXO/LFRC).

> Dokładne liczby zależą od warunków: napięcie, temperatura, **DC/DC vs LDO**, źródło
> LFCLK (LFXO/LFRC) i ilość retencjonowanego RAM. To właśnie te warunki z datasheetu
> są punktem odniesienia testu. Uwaga: obraz `RAM_RETAINED` domyślnie utrzymuje **4 KB**
> (region z overlay) – to pokazuje *trend* kosztu retencji; aby zbliżyć się do
> katalogowej „pełnej retencji”, zwiększ region (patrz niżej).

> **Zmierzone (ten sam firmware `reset_only`, ta sama procedura PPK2):**
> `nrf54l15dk/nrf54l15/cpuapp` (referencyjny DK) → **~0,95 µA**, zgodnie z datasheetem.
> `BTZ_EndDevice/nrf54l15/cpuapp` → **~7,3 µA**. Skoro ten sam firmware na DK trafia w
> datasheet, różnica na `BTZ_EndDevice` to sprawa **sprzętu/BOM tej płytki**, nie
> firmware ani metodyki pomiaru.

### Analiza obniżenia poboru na BTZ_EndDevice (2026-07)

Głęboka analiza (schemat + firmware + datasheet/errata Nordic) wykazała:

**Firmware jest już praktycznie optymalny dla System OFF — nic tu nie zostało do dokręcenia:**
- NCS v3.4.0 **automatycznie aplikuje erraty poboru** przy starcie (`system_nrf54l.c`):
  errata **[37]** („wyższy prąd po pin-reset/power-cycle”, zapis `NRF_TAD+0x40C=1`)
  oraz **[31]** — identycznie na DK i na BTZ_EndDevice.
- DC/DC włączone (`&vregmain`=DCDC), retencja RAM poprawnie wyłączona w `reset_only`,
  RESETREAS czyszczony przy boot. Peryferia `status="okay"` w devicetree **nie kosztują
  nic w System OFF** (bloki odcięte od zasilania).

**Oba „firmware’owe” tropy sprawdzone i odrzucone:**
- Szyna czujników (P0.00/Q2): R2 (1 MΩ) i tak trzyma Q2 wyłączony, a pull-upy I²C wiszą
  na *przełączanej* szynie `VDD_susp`, nie na stałym VDD. Firmware’owy fix (już w kodzie)
  nie zmienił pomiaru → to nie czujniki.
- Przełącznik LED P2.06/Q3: dren/źródło idą tylko na złącze J2 (nie na pokładową VDD),
  bramka ściągana do źródła przez R7 → domyślnie wyłączony. Nie jest przyczyną.

**Wniosek: ~6,4 µA nadwyżki to sprzęt płytki.** Jedyna zawsze-aktywna ścieżka DC na
schemacie (poza SoC) to upływ wsteczny **diody Schottky D1** w sieci ORowania wokół Q1
(VDD′ → D1 wstecznie → R1 470k → GND) — ale jest **ograniczona** (przy ~6 µA spadek na
R1 niemal wyłączyłby Q1, a płytka działa → D1 to najwyżej ~1 µA). Schemat nie pokazuje
oczywistego pojedynczego źródła 6 µA → trzeba **izolacji na stole**.

**Do zrobienia (pomiary sprzętowe, rozstrzygające):**
1. Zmierz napięcie na **R1** (470k) w śnie → prąd D1 = U/470k. Albo odlutuj D1 i zmierz
   ponownie.
2. Sprawdź, gdzie jeszcze łączy się sieć **+3.3V** zasilana przez D1 (J1/J4, testpointy).

**Test firmware’owy (zbudowany, czeka na pomiar): NFC-as-GPIO.** Piny NFC (P1.02/P1.03)
są domyślnie w trybie NFC; koszt tego trybu w System OFF nie jest udokumentowany dla
nRF54L15. Overlay `boards/BTZ_EndDevice_nrf54l15_cpuapp.overlay` zawiera teraz
`&uicr { nfct-pins-as-gpios; }`. Zbuduj i **wgraj z `--erase`** (zapis UICR jest trwały),
potem zmierz — jeśli prąd spadnie, tryb NFC był (częścią) winowajcy:
> ```sh
> west build -b BTZ_EndDevice/nrf54l15/cpuapp -p always -d build_reset_only -- -DBOARD_ROOT=$BOARD_ROOT -DCONFIG_SLEEP_SYSTEM_OFF_RESET_ONLY=y
> west flash -d build_reset_only -r jlink --erase
> ```
> Jeśli NFC ma być funkcją produktu — usuń blok `&uicr` z overlaya.

## Wysyłka „co jakiś czas”: który tryb zużyje mniej? (punkt przecięcia)

Dla scenariusza „budzę się co T, coś wysyłam, śpię" liczy się **prąd średni przez
pełny cykl**, nie prąd samego snu:

```
prąd średni ≈ ( ładunek_aktywny  +  prąd_snu × T ) / T
```

- **System ON** (`SLEEP_PERIODIC_SYSTEM_ON`): wyższy prąd snu (~µA), ale program
  kontynuuje – **brak kosztu rebootu** i re-inicjalizacji. Wygrywa przy **częstych**
  wysyłkach.
- **System OFF** (`SLEEP_PERIODIC_SYSTEM_OFF`): niższy prąd snu, ale każde wybudzenie
  to **pełny reboot** (init + rozruch/kalibracja radia) – narzut płacony co cykl.
  Wygrywa przy **rzadkich** wysyłkach.

Punkt przecięcia (kiedy OFF zaczyna się opłacać):
`(prąd_snu_ON − prąd_snu_OFF) × T > narzut_rebootu`. Zależy od Twojego HW – **zmierz**.

**Procedura pomiaru:**
1. Zbuduj oba warianty periodyczne z tym samym `PERIODIC_PERIOD_MS` i `PERIODIC_ACTIVE_MS`.
2. W PPK2 uśredniaj przez **kilka pełnych cykli** (Power Profiler pokazuje średni prąd/ładunek).
3. Powtórz dla kilku wartości `T` (np. 1 s, 10 s, 60 s, 300 s) – dostaniesz krzywą i
   punkt, w którym linie się przecinają.

> **Uwaga:** `PERIODIC_ACTIVE_MS` to tylko zastępczy „błysk" CPU (`k_busy_wait`).
> Realne nadawanie radiem pobiera znacznie więcej – aby wynik był miarodajny, zastąp
> `activity_burst()` w `src/main.c` swoją faktyczną pracą (odczyt + TX). Przy niskim
> duty cycle to właśnie energia TX często decyduje o wszystkim.

## Płytka BTZ_EndDevice (i inne własne)

Docelowa płytka **`BTZ_EndDevice/nrf54l15/cpuapp`** jest już w pełni obsłużona:

- **`sw0 = &button0`** ✅ – wariant `SLEEP_SYSTEM_OFF_WAKE_GPIO` działa od ręki.
- **DC/DC** ✅ – `&vregmain { regulator-initial-mode = <NRF5X_REG_MODE_DCDC> }`, więc
  obowiązują katalogowe wartości „DC/DC".
- **LFXO** ✅ – LF clock z kryształu; prąd `SYSTEM_ON_IDLE` odczytuj z kolumny LFXO.
- **Region retencji RAM** – dołączony `boards/BTZ_EndDevice_nrf54l15_cpuapp.overlay`
  (pełne 256 KB SRAM → 4 KB regionu retained u góry, `cpuapp_sram` 252 KB). Aby zbliżyć
  się do katalogowej „pełnej retencji", zwiększ region i zmniejsz `cpuapp_sram`.

> Board root (katalog `Projekt-BLE-Mesh/app`) skrypt wykrywa sam (zakłada układ
> `../NCS-Projects/Projekt-BLE-Mesh` obok projektu). Ręcznie: `-DBOARD_ROOT=<ścieżka>/Projekt-BLE-Mesh/app`.

**Inna własna płytka nRF54L15:** zadbaj w jej devicetree o alias `sw0`, tryb DC/DC na
`&vregmain`, źródło LFCLK oraz własny region `zephyr,retained-ram` (adresy wg jej mapy RAM)
i dodaj analogiczny plik `boards/<twoja_płytka>_nrf54l15_cpuapp.overlay`.

## Sanity-check (czy firmware działa) — przez RTT (J-Link)

Jeśli programujesz **J-Link mini EDU** i patrzysz w **J-Link RTT Viewer**, użyj fragmentu
`debug_rtt.conf` (kieruje logi na RTT, nie na UART). **To NIE jest obraz do pomiaru
minimum** — RTT wymaga podłączonego J-Linka, co podnosi pobór.

```sh
# skryptem:
./scripts/build.sh wake_gpio --rtt
west flash -d build_wake_gpio -r jlink

# albo ręcznie (popraw ścieżkę do repo z płytką):
west build -b BTZ_EndDevice/nrf54l15/cpuapp -p always \
  -- -DBOARD_ROOT=$HOME/Projects/NCS-Projects/Projekt-BLE-Mesh/app \
     -DCONFIG_SLEEP_SYSTEM_OFF_WAKE_GPIO=y -DEXTRA_CONF_FILE=debug_rtt.conf
west flash -r jlink
```

Potem w **J-Link RTT Viewer**: *Connect* → target **nRF54L15** (Cortex-M33), interfejs
**SWD**. Zobaczysz `[nRF54L15 power-test] board=... reset_cause=...` i `Tryb: ...`.

Co zobaczysz w RTT zależnie od trybu:
- `periodic_off` / `reset_only` / `wake_gpio`: **System OFF** = po zaśnięciu chip jest
  zgaszony i RTT milknie; przy `periodic_off` co `T` następuje reboot → **nowy** komunikat
  startowy w każdym cyklu (włącz auto-reconnect w Viewerze).
- `periodic_on`: pętla wypisuje `wybudzenie #N` co `T` (RTT działa cały czas).
- `idle`: jeden komunikat, potem cisza (śpi w System ON).

> Masz też `debug.conf` (logi przez **UART**) — użyj go, jeśli wolisz terminal szeregowy
> (np. na DK) zamiast RTT.

## Rozwiązywanie problemów

- **Prąd wyraźnie wyższy niż w datasheecie:** sprawdź czy odłączony jest J-Link/SWD, czy
  PPK2 jest w Source meter i zasila *tylko* SoC, czy tryb to LDO zamiast DC/DC, oraz czy nie
  budujesz przypadkiem z `debug.conf`/`debug_rtt.conf`.
- **RTT Viewer nic nie pokazuje:** to build bez konsoli (pomiarowy) — przebuduj z `--rtt`;
  sprawdź device (nRF54L15, SWD) i czy J-Link jest podłączony; przy System OFF włącz
  auto-reconnect (chip milknie w śnie).
- **`WAKE_GPIO` nie kompiluje się (`#error sw0`):** płytka nie ma aliasu `sw0` – dodaj go
  w devicetree.
- **IDE (clang) zgłasza „kernel.h not found”:** to tylko brak ścieżek include przed
  pierwszym buildem w katalogu projektu – zbuduj raz z `build/` w projekcie, aby powstał
  `compile_commands.json`.
