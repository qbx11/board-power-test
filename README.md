# board-power-test

Narzędzie mierzy pobór prądu płytki za pomocą PPK2.

## Instalacja

```sh
git clone https://github.com/qbx11/board-power-test.git
cd board-power-test
./scripts/install.sh        # symlink w ~/.local/bin, bez sudo
board-power-test            # start
```

**Wymagania:**
- Python ≥ 3.11 - `sudo apt install python3.11 python3.11-venv` (Ubuntu/Debian)
- `nrfutil` - pobierz instalator ze strony Nordic Semiconductor, potem doinstaluj toolchain i SDK:
  ```sh
  nrfutil install toolchain-manager
  nrfutil toolchain-manager install --ncs-version v3.4.0   # inna lokalizacja: export NCS_WORKSPACE=/ścieżka
  ```
- `west` w PATH - dociąga się z toolchainem NCS; ręcznie: `pip install west`
- J-Link - instalator „J-Link Software and Documentation Pack” ze strony SEGGER
- PPK2 i „Power Profiler” w nRF Connect for Desktop - do pomiaru ręcznego
- płytka `BTZ_EndDevice` - leży w repo (`boards/goodbyte/BTZ_EndDevice/`), build działa po `git clone`

## Dwa tryby pomiaru

**Pomiar ręczny** sprawdza baseline nowej płytki.
Zaznacz scenariusze na liście.
Narzędzie buduje i wgrywa każdy z nich po kolei.
Zmierz prąd w Nordic Power Profiler i wpisz odczyt.
Wynik trafia do wspólnego dziennika.

**Tryb autonomiczny** mierzy prąd bez Twojego udziału.
Narzędzie zasila płytkę z PPK2, mierzy przez zadany czas i zapisuje wynik.
Ułóż sekwencję pomiarów na kartach „Pomiar 1, 2, …”.
Każda karta ma własny czas, napięcie, warunek startu i podgląd logów.

Aplikacja startuje w trybie autonomicznym.
Kliknij napis **„Pomiar ręczny”** u góry ekranu, aby zmienić tryb.

## Scenariusze i płytki

Scenariusz to jeden obraz firmware plus notatki do pomiaru.
Rodzaje: firmware z repo z flagami, cudza aplikacja albo gotowy `.hex`.
Naciśnij **„Dodaj kod”**, aby dodać scenariusz (patrz krok 3 niżej).

Tryb ręczny pokazuje scenariusze wprost na ekranie.
Tryb autonomiczny pokazuje tę samą listę pod przyciskiem **„Scenariusze”**.
Usuń wpis przyciskiem ✕ przy jego nazwie.

Narzędzie blokuje usunięcie w trzech przypadkach.
Wpis jest wybrany w karcie „Pomiar N”.
Wpis jest zaznaczony na liście trybu ręcznego.
Wpis jest ostatni w manifeście.

Wybierz płytkę z listy profili nad scenariuszami.
Dodaj nowy profil (target budowania, overlay sprzętowy) wpisem w `scenarios.toml`.

## Serie pomiarów (sweep parametru)

Seria to jedna karta „Pomiar N”, która rozwija się na wiele pomiarów.
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

Naciśnij „Dodaj kod” na ekranie głównym.
Wpisz ścieżkę do katalogu aplikacji w pole „Ścieżka”.
Ścieżka względna liczy się od katalogu repo.

Naciśnij „Przeglądaj…”, aby wskazać katalog w eksploratorze.
Eksplorator startuje z katalogu nad repo.

Wpisz nazwę i opis.
Oba pola są opcjonalne.
Naciśnij „Dodaj”.

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
Wybierz swój scenariusz na karcie „Pomiar N”.
Podaj czas jednego pomiaru, na przykład `20m`.
Rozwiń „Ustawienia zaawansowane”.
Włącz „Seria: sweep parametru (jedna karta = wiele pomiarów)”.

### 5. Podaj parametr i wartości

Wpisz symbol Kconfig w pole „Parametr”.
Narzędzie przyjmuje trzy zapisy tej samej nazwy:

```text
CONFIG_LPN_SENSOR_INTERVAL_S
LPN_SENSOR_INTERVAL_S
-DCONFIG_LPN_SENSOR_INTERVAL_S
```

Wpisz wartości w pole „Wartości”.
Rozdziel wartości przecinkiem albo spacją.

```text
Parametr:  CONFIG_LPN_SENSOR_INTERVAL_S
Wartości:  10, 30, 50
```

Narzędzie zachowuje kolejność wartości.
Narzędzie usuwa duplikaty i zostawia pierwsze wystąpienie.
Zwinięta karta pokazuje dopisek `· sweep CONFIG_LPN_SENSOR_INTERVAL_S ×3`.

### 5a. Drugi parametr (opcjonalnie)

Pola „Drugi parametr” i „Wartości drugiego parametru” są opcjonalne.
Zostaw je puste, gdy zmieniasz tylko jeden parametr.

Wypełnione dają wszystkie kombinacje obu list.

```text
Parametr:    CONFIG_P1     Wartości:    10, 20, 30
Parametr 2:  CONFIG_P2     Wartości 2:  100, 200
```

Powyższe daje sześć pomiarów w tej kolejności:

```text
10/100   10/200   20/100   20/200   30/100   30/200
```

Pierwszy parametr zmienia się najwolniej.
Zwinięta karta pokazuje `· sweep CONFIG_P1 ×3 · CONFIG_P2 ×2 = 6`.

Ostrzeżenie: liczba pomiarów to iloczyn, nie suma.
Dwie listy po dziesięć wartości dają sto pomiarów.

Dziennik zapisuje drugą oś w kolumnach `parametr2` i `wartosc2`.

### 6. Sprawdź plan przed startem

Karta „Pomiar N” rozwija się na kroki „Pomiar N.1, N.2, …”.
Kroki numerują się po kolei, także przy dwóch parametrach.
Każdy krok dostaje po jednej fladze builda na parametr:

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

Każdy pomiar trafia do wspólnego dziennika `reports/pomiary.csv`.
Przycisk **„Wyniki”** pokazuje dziennik w interfejsie.
Tryb autonomiczny zapisuje dodatkowo pełną sesję z danymi i etykietami w `reports/sessions/`.

### Zajętość pamięci

Tabelka `Memory region` z końca builda trafia do wyników.
Dziennik dostaje kolumny `flash_B`, `flash_pct`, `ram_B` i `ram_pct`.
Działa to w obu trybach pomiaru.

Narzędzie zapamiętuje te liczby w katalogu builda (`.bpt_memory.json`).
Dzięki temu pomiar na gotowym obrazie też je ma, choć build się nie wykonał.

Scenariusz na gotowym pliku `.hex` zostaje bez tych kolumn.
Nie ma builda, więc nie ma tabelki linkera.

Sysbuild buduje kilka obrazów (aplikacja, MCUboot).
Do wyników trafia obraz aplikacji, czyli domena domyślna z `domains.yaml`.
