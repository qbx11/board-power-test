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

Narzędzie blokuje usypianie komputera na czas całego przebiegu.
Blokada obejmuje bezczynność, jawny suspend i zamknięcie klapy.
Zamknij więc laptopa i zostaw pomiar; przebieg zwalnia blokadę na końcu.
Uśpienie w środku okna zabija strumień próbek z PPK2 i psuje pomiar.
Tryb ręczny blokady nie bierze, bo operator i tak siedzi przy klawiaturze.

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

## Protokoły

Każda karta „Pomiar N” wybiera protokół w „Ustawieniach zaawansowanych”.
Zakładki u góry: **BLE Mesh**, **Thread**, **Zigbee**.

Nowa karta startuje BEZ protokołu.
Naciśnij zakładkę, aby wejść w tryb protokołu.
Naciśnij AKTYWNĄ zakładkę, aby z niego wyjść.
Karta bez protokołu jest zwykłym pomiarem: bez serii, monitora i Mattera.
Wpisane pola czekają w swojej zakładce i wracają po ponownym wejściu.

Każda zakładka trzyma serię (sweep).
BLE Mesh i Zigbee mają dodatkowo monitor dongla; Thread mierzymy bez niego.

W zakładce Thread wybierasz, **co narzędzie robi po flashu**:

```text
Parowanie + subskrypcja                       -> węzeł zostaje SIT-em
Parowanie z rejestracją ICD + subskrypcja     -> tryb LIT
Tylko subskrypcja (węzeł już sparowany)       -> bez parowania
```

Trzy tryby wykluczają się nawzajem, więc jest to jeden wybór.
Rejestracja idzie wyłącznie w trakcie parowania, więc nie da się jej
połączyć z „już sparowany”.

Wybierz wariant z rejestracją, gdy mierzysz długie interwały pollowania.
Bez rejestracji urządzenie z `CHIP_ICD_LIT_SUPPORT` pracuje jako SIT.
SIT pollue co najwyżej co `CHIP_ICD_SIT_SLOW_POLL_LIMIT`, cokolwiek
stoi w `CHIP_ICD_SLOW_POLL_INTERVAL`.
Narzędzie odczytuje wtedy `OperatingMode` przed pomiarem i przerywa
krok, jeśli węzeł mimo wszystko jedzie w SIT.

To zwykłe pola do wpisania, bez checkboxa „włącz”.
Wpisana treść włącza funkcję, puste pole ją wyłącza.

```text
Zakładka aktywna         -> karta mierzy w tym protokole
Wartości serii wpisane   -> karta rozwija się na serię pomiarów
Port dongla wpisany      -> monitor dongla czyta logi w trakcie pomiaru
Fragment logu wpisany    -> pomiar startuje po tym logu
```

Monitor dongla otwiera port po wgraniu obrazu, nie przed.
Dongiel buforuje logi, dopóki port jest zamknięty.
Narzędzie wyrzuca ten bufor, bo powstał przed wgraniem obrazu.
Panel pokazuje wtedy `[pominięto N linii z buforu dongla sprzed flasha]`.
Pełną treść pominiętych linii ma `dongle.log` sesji.

Ostrzeżenie: log triggera pada w chwili dołączania węzła do sieci.
Narzędzie odczekuje po nim 10 s, aby ten ruch nie wchodził do średniej.

Zakładki nie dzielą się wartościami.
Symbol Kconfig serii mesha nie ma sensu w Zigbee, więc każdy protokół
pamięta własne wpisy, a pomiar bierze tylko te z wybranej zakładki.

Pod zakładkami leżą napięcie, zapis danych, próbkowanie i power-cycle.
Dotyczą sprzętu i pomiaru, więc są wspólne dla wszystkich protokołów.

**„Power-cycle po flashu”** jest domyślnie włączony i daje czysty zimny start.
Narzędzie odcina wtedy zasilanie płytki na pół sekundy po wgraniu obrazu.
Odznacz go, gdy firmware przenosi stan przez podtrzymaną sekcję RAM.
Odcięcie zasilania kasuje taki blok, więc pierwszy cykl wychodzi zimny.

**Przycisk „x1” w nagłówku karty** ustawia krotność pomiaru.
Klik przestawia go x1 → x2 → … → x5, a po x5 wraca do x1.
Krotność `xK` wykonuje ten pomiar K razy, jako K osobnych pomiarów.
Każda powtórka ma własny flash, katalog sesji i wiersz w dzienniku.
Etykiety powtórek to `N/1, N/2, …`, a w serii `N.M/1, N.M/2, …`.
Powtórki jednego ustawienia idą obok siebie, także w serii.
Obraz jest ten sam, więc narzędzie buduje go raz i tylko flashuje ponownie.
Krotność działa w każdym protokole i przenosi ją „Zastosuj do …”.
Powtarzaj pomiary, gdy szukasz rozrzutu — ten sam kod potrafi dać
7 µA i 28 µA, jeśli w oknie pomiaru wypadnie inna liczba retransmisji.

„Start pomiaru po czasie” i „Konsola RTT” są schowane z widoku.
Kod obu został na miejscu, tylko interfejs ich nie pokazuje.
Pomiar rusza po stałym odczekaniu na rozruch płytki, bez konsoli RTT.

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

### 4. Otwórz ustawienia serii na karcie pomiaru

Uruchom tryb autonomiczny.
Wybierz swój scenariusz na karcie „Pomiar N”.
Podaj czas jednego pomiaru, na przykład `20m`.
Rozwiń „Ustawienia zaawansowane”.
Naciśnij zakładkę swojego protokołu (patrz „Protokoły” wyżej).
Serię ma każda z nich; przykład niżej używa BLE Mesh.

Pola serii są od razu gotowe do wpisania, bez włączania checkboxem.
Puste pola WARTOŚCI oznaczają zwykły pojedynczy pomiar.

### 5. Podaj parametr i wartości

Zakładka BLE Mesh ma dwa pola parametrów z wpisanymi symbolami:

```text
LPN sensor interval:  CONFIG_LPN_SENSOR_INTERVAL_S
Poll interval:        CONFIG_BT_MESH_LPN_POLL_TIMEOUT
```

Zmień symbol w polu, gdy sweepujesz inny parametr.
Narzędzie przyjmuje trzy zapisy tej samej nazwy:

```text
CONFIG_LPN_SENSOR_INTERVAL_S
LPN_SENSOR_INTERVAL_S
-DCONFIG_LPN_SENSOR_INTERVAL_S
```

Wpisz wartości pod parametrem, który zmieniasz.
Rozdziel wartości przecinkiem albo spacją.

```text
LPN sensor interval:  Wartości (s):  10, 30, 50
Poll interval:        Wartości (s):  (puste – tej osi nie ma)
```

O serii decydują WARTOŚCI, nie symbole.
Sam symbol serii nie robi, bo oba są wpisane domyślnie.
Sweepuj dowolne z dwóch pól albo oba.

Wartości podajesz w SEKUNDACH w obu polach.
Kconfig liczy PollTimeout w jednostkach 100 ms, więc narzędzie mnoży ×10.

```text
Poll interval = 200  ->  -DCONFIG_BT_MESH_LPN_POLL_TIMEOUT=2000
```

Dziennik zapisuje wartość wpisaną (200); kolumna `flagi` niesie przeliczoną.
Przelicznik należy do symbolu `CONFIG_BT_MESH_LPN_POLL_TIMEOUT`.
Inny symbol w tym samym polu dostaje wartości bez zmian.

Ostrzeżenie: Kconfig przyjmuje PollTimeout od 1 s do 24473 s.
Narzędzie odrzuca wartości poza zakresem przed startem przebiegu.

Narzędzie zachowuje kolejność wartości.
Narzędzie usuwa duplikaty i zostawia pierwsze wystąpienie.
Zwinięta karta pokazuje dopisek `· sweep CONFIG_LPN_SENSOR_INTERVAL_S ×3`.

### 5a. Dwa parametry naraz (opcjonalnie)

Wypełnij oba pola wartości, aby zmierzyć wszystkie kombinacje.

```text
LPN sensor interval:  Wartości (s):  10, 20, 30
Poll interval:        Wartości (s):  100, 200
```

Powyższe daje sześć pomiarów w tej kolejności:

```text
10/100   10/200   20/100   20/200   30/100   30/200
```

Pierwszy parametr zmienia się najwolniej.
Zwinięta karta pokazuje `· sweep <parametr 1> ×3 · <parametr 2> ×2 = 6`.

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
