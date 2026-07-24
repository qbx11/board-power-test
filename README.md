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

## Wyniki

Każdy pomiar trafia do wspólnego dziennika `reports/pomiary.csv` — przycisk
**„Wyniki”** pokazuje go w interfejsie. Pomiary z trybu autonomicznego
zapisują dodatkowo pełną sesję (dane, etykiety) w `reports/sessions/`.
