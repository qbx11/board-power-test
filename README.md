# board-power-test

Narzędzie do pomiaru poboru prądu za pomocą PPK2.

## Instalacja

```sh
git clone https://github.com/qbx11/board-power-test.git
cd board-power-test
./scripts/install.sh        # symlink w ~/.local/bin, bez sudo
board-power-test            # start
```

**Wymagania:**
- Python ≥ 3.11 - `sudo apt install python3.11 python3.11-venv` (Ubuntu/Debian)
- nRF Util (`nrfutil`) - pobierz instalator ze strony Nordic Semiconductor, potem doinstaluj toolchain-manager i sam SDK:
  ```sh
  nrfutil install toolchain-manager
  nrfutil toolchain-manager install --ncs-version v3.4.0   # SDK NCS v3.4.0, inna lokalizacja: export NCS_WORKSPACE=/ścieżka
  ```
- `west` w PATH do budowania i wgrywania firmware'u - dociąga się razem z toolchainem NCS powyżej (albo ręcznie: `pip install west`)
- J-Link (flash) - instalator „J-Link Software and Documentation Pack" ze strony SEGGER
- PPK2 + nRF Connect Power Profiler (pomiar ręczny) - aplikacja „Power Profiler" w nRF Connect for Desktop (strona Nordic Semiconductor)
- płytka `BTZ_EndDevice`: dołączona w repo (`boards/goodbyte/BTZ_EndDevice/`) - build działa od razu po `git clone`.

## Dwa tryby pomiaru


**Pomiar ręczny** - weryfikacja baseline'u nowej płytki: czy działa
poprawnie i nie pobiera za dużo prądu. Zaznaczasz scenariusze z listy,
narzędzie po kolei buduje i wgrywa każdy z nich, a Ty prowadzony jesteś
przez pomiar w Nordic Power Profiler i wpisujesz odczytaną wartość. Wynik
trafia do wspólnego dziennika.

**Tryb autonomiczny** - narzędzie samo zasila płytkę z PPK2, mierzy prąd
przez zadany czas i zapisuje wynik. Kreator kart „Pomiar 1, 2, …” pozwala ułożyć całą sekwencję
pomiarów, każdy z własnym czasem, napięciem, warunkiem startu i opcjonalnym
podglądem logów z portu szeregowego.

Aplikacja startuje w trybie autonomicznym; na ręczny przełącza się
kliknięciem w napis **„Pomiar ręczny”** u góry ekranu.

## Scenariusze i płytki

Każdy pomiar to **scenariusz** (firmware repo + flagi, cudza aplikacja albo
gotowy `.hex`). Przycisk **„Dodaj kod”** dodaje nowy scenariusz - wskazujesz
katalog aplikacji albo plik `.hex`, reszta (build, wpis na liście) dzieje się
sama.

W trybie ręcznym scenariusze są wprost na ekranie (checklista z opisem
i ✕ do usunięcia); w trybie autonomicznym tę samą listę pokazuje przycisk
**„Scenariusze”**. Nie da się usunąć wpisu, który jest akurat wybrany
w karcie „Pomiar N” albo zaznaczony na liście trybu ręcznego - ani
ostatniego wpisu w manifeście.

Płytkę wybiera się z listy profili nad scenariuszami; nowy profil (target
budowania, ewentualny overlay sprzętowy) dodaje się wpisem w
`scenarios.toml`.

## Sonda J-Link tylko dla nas

Sonda J-Link ma **jednego właściciela**. Jeśli trzyma ją inny program -
najczęściej **nRF Connect for Desktop**, którego demony
`nrfutil device list --hotplug` żyją w tle tak długo, jak otwarta jest
aplikacja - pomiar wychodzi **zawyżony**: przy cudzej sesji J-Linka runner
nie wygasza debug interface'u po wgraniu, więc układ nie schodzi do podłogi
snu (setki µA zamiast ~0,5 µA), a sonda EDU/EDU Mini dorzuca do tego dialog
licencyjny przy każdym flashu.

Narzędzie sprawdza to samo przed wgrywaniem (szuka biblioteki
`libjlinkarm` w obcych procesach) i pokazuje dialog: *Sprawdziłem - ponów* /
*Mierz mimo to* / *Przerwij*. Tryb autonomiczny nie blokuje przebiegu
(może startować bez nikogo przy klawiaturze), ale wpisuje ostrzeżenie do
`plan.log` i pokazuje je w interfejsie. **Zamykaj całe nRF Connect for
Desktop**, nie tylko kartę aplikacji.

## Serie pomiarów (sweep parametru)

Seria to jedna karta „Pomiar N", która rozwija się na wiele pomiarów.
Narzędzie buduje osobny obraz dla każdej wartości parametru.
Każdy pomiar dostaje osobny wiersz w dzienniku.

Seria działa tylko w trybie autonomicznym.
Tryb ręczny i pliki planów `plans/*.toml` nie obsługują serii.

Przykład poniżej używa aplikacji LPN.
Parametr `CONFIG_LPN_SENSOR_INTERVAL_S` ustawia odstęp między publikacjami
temperatury do Frienda.

### 1. Dodaj parametr do Kconfig aplikacji

Parametr musi być symbolem Kconfig budowanej aplikacji.
Flaga builda nie zmieni stałej `#define`.

Otwórz plik `Kconfig` w katalogu głównym aplikacji.
Dopisz symbol z typem, wartością domyślną i zakresem.

```kconfig
# app/apps/lpn/Kconfig
config LPN_SENSOR_INTERVAL_S
	int "Interwal odczytu i publikacji temperatury do Frienda [s]"
	default 10
	range 1 86400
	help
	  Odstep miedzy kolejnymi publikacjami Sensor Server -> Friend.
	  Sterowane build-time: -DCONFIG_LPN_SENSOR_INTERVAL_S=<sekundy>.

source "Kconfig.zephyr"
```

Ustaw wartość domyślną na dotychczasowe zachowanie.
Build bez flagi daje wtedy ten sam wynik co przedtem.

Ostrzeżenie: plik musi kończyć się linią `source "Kconfig.zephyr"`.
Bez tej linii build nie widzi symboli Zephyra.

### 2. Odczytaj parametr w kodzie

Zastąp stałą `#define` symbolem Kconfig.
Zostaw fallback na build bez app-level Kconfig.

```c
/* app/apps/lpn/src/main.c */
#ifndef CONFIG_LPN_SENSOR_INTERVAL_S
#define CONFIG_LPN_SENSOR_INTERVAL_S 10
#endif
#define SENSOR_READ_INTERVAL   K_SECONDS(CONFIG_LPN_SENSOR_INTERVAL_S)

static void sensor_read_work_handler(struct k_work *work)
{
	model_handler_publish_temp();
	/* Przeplanuj siebie -> odczyt i publikacja co SENSOR_READ_INTERVAL. */
	k_work_reschedule(&sensor_read_work, SENSOR_READ_INTERVAL);
}
```

### 3. Dodaj scenariusz z aplikacją

Naciśnij „Dodaj kod" na ekranie głównym.
Wpisz ścieżkę do katalogu aplikacji w pole „Ścieżka".
Ścieżka względna liczy się od katalogu repo.

Naciśnij „Przeglądaj…", aby wskazać katalog w eksploratorze.
Eksplorator startuje z katalogu nad repo.

Wpisz nazwę i opis.
Oba pola są opcjonalne.
Naciśnij „Dodaj".

Narzędzie rozpoznaje rodzaj firmware po ścieżce.
Katalog aplikacji dostaje wariant `source` i build west-em.
Plik `.hex` dostaje wariant `hex` i pomija build.

Ostrzeżenie: seria wymaga wariantu `source`.
Narzędzie odrzuca serię na wariancie `hex` przed startem przebiegu.

Nowy scenariusz jest od razu na liście kart, bez restartu.
Narzędzie dopisuje wpis do `scenarios.toml`.
Przenieś wpis do `scenarios.local.toml`, gdy nie chcesz go na remote.

### 4. Włącz serię na karcie pomiaru

Uruchom tryb autonomiczny.
Wybierz swój scenariusz na karcie „Pomiar N".
Podaj czas jednego pomiaru, na przykład `20m`.
Rozwiń „Ustawienia zaawansowane".
Włącz „Seria: sweep parametru (jedna karta = wiele pomiarów)".

### 5. Podaj parametr i wartości

Wpisz symbol Kconfig w pole „Parametr".
Narzędzie przyjmuje trzy zapisy tej samej nazwy:

```text
CONFIG_LPN_SENSOR_INTERVAL_S
LPN_SENSOR_INTERVAL_S
-DCONFIG_LPN_SENSOR_INTERVAL_S
```

Wpisz wartości w pole „Wartości".
Rozdziel wartości przecinkiem albo spacją.

```text
Parametr:  CONFIG_LPN_SENSOR_INTERVAL_S
Wartości:  10, 30, 50
```

Narzędzie zachowuje kolejność wartości.
Narzędzie usuwa duplikaty i zostawia pierwsze wystąpienie.
Zwinięta karta pokazuje dopisek `· sweep CONFIG_LPN_SENSOR_INTERVAL_S ×3`.

### 6. Sprawdź plan przed startem

Karta „Pomiar N" rozwija się na kroki „Pomiar N.1, N.2, …".
Każdy krok dostaje jedną flagę builda:

```sh
west build ... -- -DCONFIG_LPN_SENSOR_INTERVAL_S=10   # Pomiar N.1
west build ... -- -DCONFIG_LPN_SENSOR_INTERVAL_S=30   # Pomiar N.2
west build ... -- -DCONFIG_LPN_SENSOR_INTERVAL_S=50   # Pomiar N.3
```

Wszystkie kroki serii dziedziczą z karty czas, napięcie, warunek startu i zapis.
Policz czas całego przebiegu przed startem.
Trzy wartości po 20 minut zajmują godzinę, plus build każdego obrazu.

### 7. Uruchom przebieg

Narzędzie buduje obraz tuż przed jego pomiarem.
Każda kombinacja flag dostaje własny katalog builda:

```text
build_lpn         -> CONFIG_LPN_SENSOR_INTERVAL_S=10
build_lpn_krok2   -> CONFIG_LPN_SENSOR_INTERVAL_S=30
build_lpn_krok3   -> CONFIG_LPN_SENSOR_INTERVAL_S=50
```

Obrazy nie nadpisują się.

## Wyniki

Każdy pomiar trafia do wspólnego dziennika `reports/pomiary.csv` - przycisk
**„Wyniki”** pokazuje go w interfejsie. Pomiary z trybu autonomicznego
zapisują dodatkowo pełną sesję (dane, etykiety) w `reports/sessions/`.
