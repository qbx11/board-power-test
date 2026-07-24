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
- Python ≥ 3.11
- SDK NCS v3.4.0 (inna lokalizacja: `export NCS_WORKSPACE=/ścieżka`)
- nrfutil / `west` w PATH do budowania i wgrywania firmware'u
- J-Link (flash) oraz PPK2 + nRF Connect Power Profiler (pomiar ręczny)
- płytka `BTZ_EndDevice`: repo `Projekt-BLE-Mesh` (`export BOARD_ROOT=...`)

## Dwa tryby pomiaru


**Pomiar ręczny** — zaznaczasz scenariusze z listy, narzędzie po kolei
buduje i wgrywa każdy z nich, przypomina o odłączeniu programatora SWD
i prowadzi przez pomiar w Nordic Power Profiler — Ty tylko wpisujesz odczytaną
wartość. Wynik trafia do wspólnego dziennika.

**Tryb autonomiczny** — narzędzie samo zasila płytkę z PPK2, mierzy prąd
przez zadany czas i zapisuje wynik. Kreator kart „Pomiar 1, 2, …” pozwala ułożyć całą sekwencję
pomiarów, każdy z własnym czasem, napięciem, warunkiem startu i opcjonalnym
podglądem logów z portu szeregowego.

## Scenariusze i płytki

Każdy pomiar to **scenariusz** (firmware repo + flagi, cudza aplikacja albo
gotowy `.hex`). Przycisk **„Dodaj kod”** dodaje nowy scenariusz — wskazujesz
katalog aplikacji albo plik `.hex`, reszta (build, wpis na liście) dzieje się
sama.

Płytkę wybiera się z listy profili nad scenariuszami; nowy profil (target
budowania, ewentualny overlay sprzętowy) dodaje się wpisem w
`scenarios.toml`.

## Serie pomiarów (sweep parametru)

W trybie autonomicznym każda karta **„Pomiar N"** może być **serią**: w
ustawieniach zaawansowanych włącz *„Seria: sweep parametru"*, podaj symbol
Kconfig (np. `CONFIG_LPN_SENSOR_INTERVAL_S`) i listę wartości
(`1, 2, 5, 10, 20, 30, 60, 120, 300, 600`). Jedna karta rozwija się wtedy na
osobne pomiary **„Pomiar N.1, N.2, …"** — każdy budowany z inną flagą
`-DCONFIG_...=<wartość>` (osobny obraz, obrazy się nie nadpisują), mierzony
niezależnie i zapisany jako **osobny wiersz** w dzienniku. Kolumny
`parametr`/`wartosc` (oraz `pomiar_id` = „N.M") mówią, której wartości dotyczy
dany pomiar; wszystkie kroki serii dostają ten sam czas i warunek startu co
karta.

Żeby flaga działała, parametr musi być **symbolem Kconfig** w budowanej
aplikacji (nie `#define`). Przykład: aplikacje `lpn`/`lpn_mock` mają
`CONFIG_LPN_SENSOR_INTERVAL_S` (interwał wysyłki temperatury do Frienda,
domyślnie 10 s) — dodawanie kolejnych parametrów to wpis w `Kconfig` aplikacji
+ odczyt przez `CONFIG_...` w kodzie.

## Wyniki

Każdy pomiar trafia do wspólnego dziennika `reports/pomiary.csv` — przycisk
**„Wyniki”** pokazuje go w interfejsie. Pomiary z trybu autonomicznego
zapisują dodatkowo pełną sesję (dane, etykiety) w `reports/sessions/`.
