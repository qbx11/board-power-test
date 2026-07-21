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

## Komendy

| Komenda | Działanie |
| --- | --- |
| `board-power-test` | interfejs okienkowy (TUI); bez biblioteki `textual` — menu tekstowe |
| `board-power-test list` | lista scenariuszy |
| `board-power-test run <scenariusz…>` | build → flash → pomiar → zapis do CSV |
| `board-power-test run --all` | wszystkie scenariusze po kolei |
| `board-power-test report` | tabela zebranych pomiarów |

Flagi `run`:

| Flaga | Znaczenie |
| --- | --- |
| `-p, --profile <profil>` | profil płytki (dom. z `[defaults]`), np. `dk` |
| `-s, --sample <opis>` | egzemplarz płytki, np. `BTZ #2` (pomija pytanie) |
| `--no-erase` | flash bez `--erase` |
| `-n, --dry-run` | tylko pokaż komendy, nic nie wykonuj |

Zmienne środowiskowe: `BOARD_ROOT`, `NCS_VERSION` (dom. `v3.4.0`),
`NCS_WORKSPACE`, `BPT_NO_TUI=1`.

## Przebieg pomiaru

**FAZA 1 — build.** `west build` (pristine, katalog `build_<scenariusz>/`) dla
wszystkich wybranych scenariuszy. Scenariusze z gotową binarką (`hex`) tę fazę
pomijają.

**FAZA 2 — flash + pomiar, scenariusz po scenariuszu:**
1. **Flash** — `west flash --erase`. Nieudany flash nie przerywa przebiegu:
   ponów / pomiń / przerwij.
2. **Pomiar** — narzędzie wypisuje instrukcję (napięcie, VOUT→VDD, czas
   ustabilizowania, wartość oczekiwana). Ustaw Power Profiler w tryb Source
   meter i zasil sam SoC z PPK2.
3. **Odłącz SWD/J-Link** — podłączony debugger dodaje własny prąd (wymagane
   potwierdzenie).
4. **Wpis wyniku** (µA lub mA) → wiersz w `reports/pomiary.csv`.

`reports/pomiary.csv` to wspólna historia pomiarów — **commituj ten plik.**

> Nowa płytka: zacznij od `run reset_only -p dk`. Znany dobry wynik DK
> (~0,95 µA) weryfikuje procedurę i sprzęt, zanim zmierzysz nową płytkę.

## Konfiguracja

Scenariusze i profile płytek definiuje `scenarios.toml`. Wbudowane scenariusze:

- **statyczne** (prąd snu): `reset_only`, `idle`, `wake_gpio`, `ram_retained`
- **periodyczne** (prąd średni cyklu): `periodic_on`, `periodic_off`

Własny scenariusz = skopiowanie wpisu w `scenarios.toml` i podmiana flag; wzory
są w komentarzach na końcu pliku. Opisy i wartości oczekiwane pokazuje
`board-power-test list`.
