#!/usr/bin/env python3
# ============================================================
#  power-test – dyrygent pomiarów poboru prądu (nRF54L15 + PPK2)
# ============================================================
# Buduje i wgrywa CZYSTE obrazy pomiarowe (jeden scenariusz = jeden
# obraz, patrz scenarios.toml), prowadzi krok po kroku przez pomiar
# w nRF Connect Power Profiler i zapisuje wynik do reports/pomiary.csv.
#
# Narzędzie celowo NIE mierzy prądu i NICZEGO nie ocenia – pomiar
# robisz w Power Profilerze, a pole "oczekiwane" jest tylko
# wyświetlane obok, do porównania z datasheetem na oko.
#
# Wymagania: Python >= 3.11 (tomllib, w stdlib – zero pip install),
#            `west` w PATH (terminal nRF Connect) do build/flash.
#
# Użycie:
#   python3 tools/power-test/power_test.py                  # bez argumentów: MENU
#   python3 tools/power-test/power_test.py list
#   python3 tools/power-test/power_test.py run reset_only idle
#   python3 tools/power-test/power_test.py run --all --profile dk
#   python3 tools/power-test/power_test.py run reset_only --dry-run
#   python3 tools/power-test/power_test.py add ../moj-projekt/app
#   python3 tools/power-test/power_test.py report

import os
import sys

# Wewnątrz środowiska NCS (nrfutil toolchain-manager, Linux) zmienne
# PYTHONHOME/PYTHONPATH wskazują pythona toolchaina. Dla naszego pythona
# z .venv oznacza to pomieszaną stdlib (stdlib toolchaina + site-packages
# venva) i niestabilność – losowe błędy ContextVar/MessagePump w TUI.
# Re-exec z czystym środowiskiem; oryginały wędrują do BPT_SAVED_* i są
# przywracane procesom `west` (child_env), bo narzędzia toolchaina ich
# potrzebują.
if ((os.environ.get("PYTHONHOME") or os.environ.get("PYTHONPATH"))
        and sys.prefix != getattr(sys, "base_prefix", sys.prefix)
        and not os.environ.get("BPT_REEXECED")):
    _env = dict(os.environ)
    _env["BPT_REEXECED"] = "1"
    for _var in ("PYTHONHOME", "PYTHONPATH"):
        if _var in _env:
            _env["BPT_SAVED_" + _var] = _env.pop(_var)
    os.execve(sys.executable, [sys.executable] + sys.argv, _env)

import argparse
import csv
import json
import re
import shlex
import shutil
import subprocess
import unicodedata
from datetime import datetime
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("power-test wymaga Pythona >= 3.11 (tomllib). "
             f"Ten interpreter to {sys.version.split()[0]}.")

import tomllib  # noqa: E402  (import po sprawdzeniu wersji, celowo)

# Katalog projektu: dwa poziomy nad tym plikiem (tools/power-test/..)
ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "scenarios.toml"
# Prywatny manifest lokalny (poza gitem) – scala się na wierzch tego
# współdzielonego przy każdym wczytaniu. Ścieżkę liczymy od MANIFEST_PATH
# w load_manifest(), żeby testy podmieniające MANIFEST_PATH nie sięgały do
# prawdziwego repo.
LOCAL_MANIFEST_NAME = "scenarios.local.toml"
CSV_PATH = ROOT / "reports" / "pomiary.csv"
# Kolumny dziennika. Pierwsze dziewięć to schemat historyczny (pomiar
# ręczny z Power Profilera); cztery ostatnie dokłada tryb autonomiczny
# (autorun): min/max prądu, czas pomiaru i ścieżka sesji z wykresem.
# Wiersze ręczne zostawiają nowe pola puste – ensure_csv_schema()
# dopisuje brakujące kolumny do starego pliku bez utraty danych.
# pomiar_id/parametr/wartosc wypełnia tryb autonomiczny dla serii (sweep):
# etykieta "N.M" oraz sweepowany symbol Kconfig i jego wartość.
CSV_BASE_FIELDS = ["data", "plytka", "egzemplarz", "scenariusz", "flagi",
                   "napiecie_V", "prad_uA", "oczekiwane", "uwagi"]
CSV_AUTORUN_FIELDS = ["prad_min_uA", "prad_max_uA", "czas_s", "sesja",
                      "pomiar_id", "parametr", "wartosc"]
CSV_FIELDS = CSV_BASE_FIELDS + CSV_AUTORUN_FIELDS


def die(msg):
    sys.exit(f"BŁĄD: {msg}")


def ask(prompt):
    """input() z sensownym zachowaniem na Ctrl-C / koniec strumienia."""
    try:
        return input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print("\nPrzerwano.")
        sys.exit(130)


def load_manifest():
    if not MANIFEST_PATH.is_file():
        die(f"brak manifestu {MANIFEST_PATH}")
    try:
        with open(MANIFEST_PATH, "rb") as f:
            manifest = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        die(f"scenarios.toml nie parsuje się: {e}")
    # Prywatny manifest lokalny (poza gitem): scala się na wierzch – tabele
    # (scenarios/boards/defaults) są łączone po kluczach, więc plik lokalny
    # dokłada własne scenariusze albo nadpisuje pojedyncze wpisy.
    local = MANIFEST_PATH.with_name(LOCAL_MANIFEST_NAME)
    if local.is_file():
        try:
            with open(local, "rb") as f:
                overlay = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            die(f"{LOCAL_MANIFEST_NAME} nie parsuje się: {e}")
        for key, val in overlay.items():
            if isinstance(val, dict) and isinstance(manifest.get(key), dict):
                manifest[key].update(val)
            else:
                manifest[key] = val
    return manifest


def child_env():
    """Środowisko dla procesów west: przywróć PYTHONHOME/PYTHONPATH zdjęte
    przy starcie (re-exec na górze pliku) – narzędzia toolchaina NCS ich
    potrzebują, tylko naszemu pythonowi szkodzą."""
    env = os.environ.copy()
    for var in ("PYTHONHOME", "PYTHONPATH"):
        saved = env.pop("BPT_SAVED_" + var, None)
        if saved:
            env[var] = saved
    # J-Link EDU/EDU Mini wymusza dialog GUI "terms of use" przy KAŻDYM
    # połączeniu (licencja edukacyjna) – "Don't show again" go nie wyłącza
    # (SEGGER: to zachowanie zamierzone). Bez managera okien J-Link nie
    # pokazuje dialogów i przyjmuje domyślną opcję (auto-akceptacja), więc
    # zdejmujemy DISPLAY/WAYLAND – west flash i nrfutil działają wtedy
    # autonomicznie, bez ręcznego klikania.
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    return env


def run_cmd(cmd, dry, cwd=ROOT):
    """Wypisz i (poza --dry-run) wykonaj komendę."""
    print(f"\n>>> {shlex.join(cmd)}")
    if dry:
        return
    rc = subprocess.run(cmd, cwd=cwd, env=child_env()).returncode
    if rc != 0:
        die(f"komenda zakończyła się kodem {rc} – przerywam scenariusz")


def find_west_workspace():
    """Katalog, z którego wołamy westa.

    `west build` działa tylko wewnątrz workspace'u west. To repo zwykle
    leży POZA workspace'em NCS – wtedy budujemy "out-of-tree": west
    uruchamiany z katalogu SDK, a ścieżki aplikacji/builda są absolutne.
    Kolejność: workspace obejmujący to repo > env NCS_WORKSPACE >
    ~/ncs/<NCS_VERSION> (domyślna lokalizacja instalacji SDK)."""
    r = subprocess.run(["west", "topdir"], cwd=ROOT,
                       capture_output=True, text=True, env=child_env())
    if r.returncode == 0:
        return ROOT

    ver = os.environ.get("NCS_VERSION", "v3.4.0")
    candidates = []
    if os.environ.get("NCS_WORKSPACE"):
        candidates.append(Path(os.environ["NCS_WORKSPACE"]))
    candidates.append(Path.home() / "ncs" / ver)
    for c in candidates:
        if (c / ".west").is_dir():
            return c

    die(f"repo nie leży w workspace west, a nie znalazłem SDK NCS ({ver}).\n"
        f"  - zainstaluj SDK {ver} (nRF Connect for VS Code -> 'Install SDK'; "
        f"trafi do ~/ncs/{ver}), albo\n"
        "  - wskaż istniejący workspace: export NCS_WORKSPACE=/ścieżka/do/ncs/"
        f"{ver}")


def board_root_arg(profile):
    """-DBOARD_ROOT=... : env BOARD_ROOT > manifest; tylko gdy katalog
    faktycznie zawiera boards/."""
    root = os.environ.get("BOARD_ROOT") or profile.get("board_root")
    if not root:
        return None
    p = Path(root)
    if not p.is_absolute():
        p = ROOT / p
    p = p.resolve()
    if (p / "boards").is_dir():
        return f"-DBOARD_ROOT={p}"
    print(f"Uwaga: board_root '{p}' nie zawiera boards/ – pomijam "
          "(zadziała, jeśli board root jest skonfigurowany inaczej).")
    return None


def resolve_path(path_str):
    """Ścieżka z manifestu -> absolutna (względne liczone od katalogu repo)."""
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    return p.resolve()


def validate_scenarios(names, scenarios):
    """Walidacja wpisów manifestu PRZED startem FAZY 1 – zwraca listę
    czytelnych błędów (pusta = wszystko OK).

    Warianty wpisu (wzajemnie wykluczające się):
      - zwykły: `cmake_args` (firmware z tego repo),
      - `source` = katalog własnej aplikacji Zephyr/NCS (cmake_args opcjonalne),
      - `hex`    = gotowa binarka, bez budowania (cmake_args zabronione)."""
    errors = []
    for name in names:
        scen = scenarios[name]
        if "source" in scen and "hex" in scen:
            errors.append(f"scenariusz '{name}': pola 'source' i 'hex' "
                          "wykluczają się – zostaw jedno z nich")
            continue
        if "hex" in scen:
            if scen.get("cmake_args"):
                errors.append(f"scenariusz '{name}': 'cmake_args' nie działa "
                              "z 'hex' (gotowa binarka nie jest budowana)")
            p = resolve_path(scen["hex"])
            if not p.is_file():
                errors.append(f"scenariusz '{name}': plik hex "
                              f"'{scen['hex']}' nie istnieje ({p})")
        elif "source" in scen:
            p = resolve_path(scen["source"])
            if not p.is_dir():
                errors.append(f"scenariusz '{name}': katalog źródeł "
                              f"'{scen['source']}' nie istnieje ({p})")
        elif not scen.get("cmake_args"):
            errors.append(f"scenariusz '{name}' nie ma cmake_args w manifeście")
    return errors


def scenario_flags(scen):
    """Zawartość kolumny 'flagi' w CSV – ma mówić, CO zmierzono:
    flagi builda + ścieżka source, albo ścieżka gotowego hex."""
    parts = list(scen.get("cmake_args", []))
    if scen.get("source"):
        parts.append(f"source={scen['source']}")
    if scen.get("hex"):
        parts.append(f"hex={scen['hex']}")
    return " ".join(parts)


# ------------------------------------------------------------
#  Dodawanie własnego firmware jedną ścieżką (CLI `add` i TUI)
# ------------------------------------------------------------

def detect_firmware(path_str, base=None):
    """Co wskazuje ścieżka: katalog aplikacji Zephyr/NCS -> 'source',
    plik .hex -> 'hex'. Zwraca (rodzaj, ścieżka absolutna); przy złej
    ścieżce ValueError z czytelnym opisem. `base` – katalog, od którego
    liczyć ścieżki względne (CLI: bieżący katalog, TUI/manifest: repo)."""
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = Path(base or ROOT) / p
    p = p.resolve()
    if p.is_file():
        if p.suffix.lower() != ".hex":
            raise ValueError(f"'{path_str}' to plik, ale nie .hex – "
                             "gotowa binarka musi być plikiem .hex")
        return "hex", p
    if p.is_dir():
        if not (p / "CMakeLists.txt").is_file():
            raise ValueError(f"katalog '{p}' nie wygląda na aplikację "
                             "Zephyr/NCS (brak CMakeLists.txt)")
        return "source", p
    raise ValueError(f"ścieżka '{path_str}' nie istnieje ({p})")


def _slug(text):
    """Tekst -> klucz scenariusza (ascii, [a-z0-9_-], nie od cyfry)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii",
                                                      "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_-").lower()
    if not s:
        s = "firmware"
    if not re.match(r"[a-z_]", s):
        s = "fw_" + s
    return s


def _toml_str(value):
    """Wartość -> łańcuch TOML w cudzysłowach (escapowanie jak w JSON,
    które jest poprawnym podzbiorem basic string TOML-a)."""
    return json.dumps(value, ensure_ascii=False)


def add_scenario(path_str, name=None, label=None, description=None,
                 base=None):
    """Dopisz do scenarios.toml scenariusz z własnym firmware na
    podstawie SAMEJ ŚCIEŻKI (łatwa droga zamiast ręcznej edycji TOML):
    katalog aplikacji -> wariant `source`, plik .hex -> wariant `hex`.

    Ścieżka w manifeście: względna do repo, jeśli firmware leży w nim
    lub obok (czytelniej i przenośnie między maszynami zespołu),
    inaczej absolutna. Zwraca (nazwa, wpis)."""
    kind, p = detect_firmware(path_str, base=base)
    try:
        stored = os.path.relpath(p, ROOT)
    except ValueError:          # Windows: inny dysk
        stored = str(p)
    if stored.count("..") > 3:  # daleko poza repo – absolutna czytelniejsza
        stored = str(p)

    manifest = load_manifest()
    scenarios = manifest.get("scenarios", {})
    if name:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
            raise ValueError(f"nazwa '{name}' – dozwolone litery, cyfry, "
                             "'_' i '-', nie może zaczynać się cyfrą")
        if name in scenarios:
            raise ValueError(f"scenariusz '{name}' już istnieje – "
                             "podaj inną nazwę")
    else:
        name = base_name = _slug(p.stem if kind == "hex" else p.name)
        i = 2
        while name in scenarios:      # auto-numerowanie przy powtórce
            name = f"{base_name}_{i}"
            i += 1

    entry = {}
    if label:
        entry["label"] = label
    entry["description"] = description or (
        f"Gotowy obraz {stored} (bez budowania)." if kind == "hex"
        else f"Aplikacja z {stored} (build przez west).")
    entry[kind] = stored

    block = [f"\n[scenarios.{name}]"]
    for key, value in entry.items():
        block.append(f"{key:<11} = {_toml_str(value)}")
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    MANIFEST_PATH.write_text(text + "\n".join(block) + "\n",
                             encoding="utf-8")
    return name, entry


def remove_scenario(name):
    """Usuń wpis [scenarios.<name>] z scenarios.toml. Operacja tekstowa
    (a nie przepisanie sparsowanego TOML-a), żeby komentarze i
    formatowanie reszty manifestu zostały nietknięte."""
    if name not in load_manifest().get("scenarios", {}):
        raise ValueError(f"scenariusz '{name}' nie istnieje w manifeście")
    lines = MANIFEST_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    header = re.compile(rf"^\s*\[scenarios\.{re.escape(name)}\]\s*(#.*)?$")
    any_section = re.compile(r"^\s*\[")
    out, i = [], 0
    while i < len(lines):
        if header.match(lines[i]):
            i += 1
            while i < len(lines) and not any_section.match(lines[i]):
                i += 1
            while out and not out[-1].strip():   # puste linie nad wpisem
                out.pop()
            if i < len(lines):
                out.append("\n")
        else:
            out.append(lines[i])
            i += 1
    MANIFEST_PATH.write_text("".join(out), encoding="utf-8")


# ------------------------------------------------------------
#  Pomijanie budowania, gdy katalog builda ma już gotowy obraz
# ------------------------------------------------------------

def _build_fingerprint(cmd):
    """Komenda builda bez '-p <tryb>' (tryb pristine nie zmienia tego,
    CO się buduje – tylko czy od zera)."""
    out, skip = [], False
    for arg in cmd:
        if skip:
            skip = False
            continue
        if arg == "-p":
            skip = True
            continue
        out.append(arg)
    return shlex.join(out)


def build_up_to_date(build_dir, cmd):
    """Czy build_<scenariusz>/ ma gotowy obraz zbudowany DOKŁADNIE tą
    komendą (płytka, flagi, źródło)? Jeśli tak, build można pominąć.

    Uwaga: zmiany w samych plikach źródłowych nie są śledzone – od
    wymuszenia świeżego builda jest --pristine (CLI) / 'Wymuś pełny
    rebuild' (TUI)."""
    d = ROOT / build_dir
    marker = d / ".bpt_build_cmd"
    return ((d / "zephyr" / "zephyr.hex").is_file() and marker.is_file()
            and marker.read_text(encoding="utf-8").strip()
            == _build_fingerprint(cmd))


def record_build(build_dir, cmd):
    """Zapisz w katalogu builda, jaką komendą powstał obraz (znacznik
    dla build_up_to_date)."""
    (ROOT / build_dir / ".bpt_build_cmd").write_text(
        _build_fingerprint(cmd) + "\n", encoding="utf-8")


def resolve_profile(manifest, name):
    profiles = manifest.get("boards", {})
    name = name or manifest.get("defaults", {}).get("profile")
    if name not in profiles:
        die(f"nieznany profil płytki '{name}'. Dostępne: {', '.join(profiles) or '(brak)'}")
    return name, profiles[name]


def print_table(headers, rows):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in rows:
        print(fmt.format(*(str(c) for c in row)))


# ------------------------------------------------------------
#  Podkomendy
# ------------------------------------------------------------

def cmd_list(args):
    manifest = load_manifest()
    scenarios = manifest.get("scenarios", {})
    if not scenarios:
        die("manifest nie zawiera żadnych scenariuszy")
    rows = [(name, s.get("label", name), s.get("description", ""))
            for name, s in scenarios.items()]
    print_table(("scenariusz", "nazwa", "opis"), rows)


def cmd_run(args):
    manifest = load_manifest()
    defaults = manifest.get("defaults", {})
    prof_name, profile = resolve_profile(manifest, args.profile)
    scenarios = manifest.get("scenarios", {})

    names = list(scenarios) if args.all else args.scenarios
    if not names:
        die("podaj scenariusze (patrz `list`) albo użyj --all")
    unknown = [n for n in names if n not in scenarios]
    if unknown:
        die(f"nieznane scenariusze: {', '.join(unknown)}. "
            f"Dostępne: {', '.join(scenarios)}")

    # Walidacja manifestu (source/hex) PRZED startem FAZY 1 –
    # lepiej wyłożyć się teraz niż w połowie przebiegu.
    errors = validate_scenarios(names, scenarios)
    if errors:
        die("\n  ".join(["błędne wpisy w scenarios.toml:"] + errors))

    # Scenariusze `hex` mają gotową binarkę – nie budujemy ich (i bez
    # nich west może w ogóle nie być potrzebny).
    to_build = [n for n in names if "hex" not in scenarios[n]]

    workspace = ROOT
    if not args.dry_run:
        if to_build and shutil.which("west") is None:
            die("brak 'west' w PATH. Otwórz terminal nRF Connect lub uruchom:\n"
                "  nrfutil toolchain-manager launch --ncs-version v3.4.0 --shell")
        if (len(to_build) < len(names)
                and shutil.which("nrfutil") is None):
            die("brak 'nrfutil' w PATH – potrzebny do wgrania gotowego "
                "pliku hex (scenariusze z polem `hex`)")
        if to_build:
            workspace = find_west_workspace()
            if workspace != ROOT:
                print(f"Workspace NCS: {workspace} (repo poza workspace'em – "
                      "build out-of-tree)")

    # Identyfikacja egzemplarza – obowiązkowa, żeby wyniki różnych
    # sztuk płytki się nie pomieszały.
    sample = args.sample
    if not sample and not args.dry_run:
        while not sample:
            sample = ask("Egzemplarz płytki (np. 'BTZ #2'): ")

    # --- FAZA 1: zbuduj WSZYSTKIE obrazy z góry (buildy trwają;
    #     przy płytce i PPK2 nie ma potem na co czekać) ---
    print(f"\n=== FAZA 1/2: budowanie {len(to_build)} obraz(ów) ===")
    if not to_build:
        print("(nic do budowania – wybrane scenariusze mają gotowe pliki hex)")
    built = {}
    for name in names:
        scen = scenarios[name]
        if "hex" in scen:
            print(f"\n--- {name}: gotowy hex ({scen['hex']}) – bez budowania")
            built[name] = None
            continue
        cmd, build_dir = make_build_cmd(
            name, scen, prof_name, profile, defaults.get("profile"),
            pristine="always" if args.pristine else "auto")
        # Gotowy obraz zbudowany tą samą komendą -> bez budowania
        # (świeży build wymusza --pristine).
        if not args.pristine and build_up_to_date(build_dir, cmd):
            print(f"\n--- {name}: gotowy build ({build_dir}/) – pomijam "
                  "(wymuś przebudowanie: --pristine)")
            built[name] = build_dir
            continue
        print(f"\n--- build: {name} – {scen.get('description', '')}")
        run_cmd(cmd, args.dry_run, cwd=workspace)
        if not args.dry_run:
            record_build(build_dir, cmd)
        built[name] = build_dir

    # --- FAZA 2: flash + pomiar, scenariusz po scenariuszu ---
    print(f"\n=== FAZA 2/2: flash + pomiar ({len(names)} scenariusz(y)) ===")
    for name in names:
        measure_scenario(name, scenarios[name], built[name], profile,
                         defaults, sample, args, workspace)

    if not args.dry_run:
        print(f"\nGotowe. Podgląd zebranych pomiarów: "
              f"python3 tools/power-test/{Path(__file__).name} report")


def make_build_cmd(name, scen, prof_name, profile, default_prof=None,
                   pristine="auto"):
    """Komenda `west build` + katalog builda dla scenariusza.

    Źródłem jest to repo, chyba że wpis ma `source` – wtedy budujemy
    wskazaną aplikację zespołu, ale katalog builda i tak zostaje tutaj
    (build_<scenariusz>/). Ścieżki absolutne, bo west może być wołany
    z katalogu SDK (build out-of-tree, gdy repo leży poza workspace'em).

    Profil domyślny (`default_prof` z [defaults]) buduje do
    build_<scenariusz>/ – jak w README; pozostałe profile dostają prefiks
    (build_<profil>_<scenariusz>/), żeby ich obrazy się nie nadpisywały.

    `pristine` -> `west build -p`: 'auto' (domyślnie) buduje przyrostowo
    (ninja przebuduje tylko zmieniony kod), a pełny build robi tylko gdy
    west wykryje, że trzeba (pierwszy build, zmiana płytki/konfiguracji);
    'always' wymusza czysty build za każdym razem (flaga --pristine)."""
    prefix = "" if prof_name == default_prof else f"{prof_name}_"
    build_dir = f"build_{prefix}{name}"
    src = resolve_path(scen["source"]) if scen.get("source") else ROOT
    cmd = ["west", "build", "-b", profile["board"], "-p", pristine,
           "-d", str(ROOT / build_dir), str(src)]
    root_arg = board_root_arg(profile)
    extra = ([root_arg] if root_arg else []) + list(scen.get("cmake_args", []))
    if extra:
        cmd += ["--"] + extra
    return cmd, build_dir


def make_flash_cmd(build_dir, profile, erase=True, reset=True):
    """Komenda `west flash`; --erase domyślnie (stan pinów/UICR potrafi
    zostać z poprzedniego obrazu i zafałszować pomiar). --reset wymusza
    restart płytki po wgraniu (J-Link nie zawsze robi to sam – bez niego
    firmware nie startuje aż do ręcznego resetu i pomiar mierzy stary
    stan)."""
    cmd = ["west", "flash", "-d", str(ROOT / build_dir)]
    if profile.get("runner"):
        cmd += ["-r", profile["runner"]]
    if erase:
        cmd += ["--erase"]
    if reset:
        cmd += ["--reset"]
    return cmd


def make_hex_flash_cmd(hex_path, erase=True, reset=True):
    """Komenda wgrania GOTOWEGO pliku .hex (scenariusz z polem `hex`).

    Nie przez `west flash --hex-file`: on wymaga katalogu builda (runner
    i jego konfiguracja powstają przy buildzie), którego scenariusz `hex`
    celowo nie ma. Wgrywamy bezpośrednio `nrfutil device program` –
    nrfutil i tak jest wymaganiem narzędzia, obsługuje J-Link (DK i
    zewnętrzny) i ma odpowiednik `--erase` (pełne kasowanie chipu);
    bez erase zostaje domyślne kasowanie tylko zapisywanych stron.
    reset=True dokłada `reset=RESET_SYSTEM` – J-Link zresetuje płytkę po
    wgraniu, żeby firmware wystartował od razu (inaczej pomiar łapie stan
    sprzed restartu)."""
    cmd = ["nrfutil", "device", "program", "--firmware", str(hex_path)]
    opts = []
    if erase:
        opts.append("chip_erase_mode=ERASE_ALL")
    if reset:
        opts.append("reset=RESET_SYSTEM")
    if opts:
        cmd += ["--options", ",".join(opts)]
    return cmd


def flash_cmd_for(scen, build_dir, profile, erase=True, reset=True):
    """Właściwa komenda flash dla wpisu: gotowy hex albo katalog builda."""
    if scen.get("hex"):
        return make_hex_flash_cmd(resolve_path(scen["hex"]), erase=erase,
                                  reset=reset)
    return make_flash_cmd(build_dir, profile, erase=erase, reset=reset)


def measure_scenario(name, scen, build_dir, profile, defaults, sample, args,
                     workspace=ROOT):
    voltage = str(scen.get("voltage", defaults.get("voltage", "3.0")))
    settle_s = scen.get("settle_s", defaults.get("settle_s", 5))

    print("\n" + "=" * 60)
    print(f"  Scenariusz: {name}  [{profile['board']}]")
    print(f"  {scen.get('description', '')}")
    print("=" * 60)

    if not args.dry_run:
        ask("Programator podłączony i płytka ZASILONA (np. VOUT z PPK2)? "
            "[Enter = wgrywam] ")
    run_cmd(flash_cmd_for(scen, build_dir, profile, erase=not args.no_erase,
                          reset=not args.no_reset),
            args.dry_run, cwd=workspace)

    # --- Instrukcja pomiaru (Power Profiler robi resztę) ---
    print(f"\n--- POMIAR ({name}) ---------------------------------------")
    print(measure_instructions(scen, voltage, settle_s))
    print("-" * 59)

    if args.dry_run:
        return

    # --- 4. Twarde potwierdzenie odłączenia SWD (bez tego pomiar
    #        minimum jest śmieciem – wymagamy wpisania 'tak'). Można je
    #        pominąć (--no-swd-reminder), gdy pomiar idzie bez programatora. ---
    if not args.no_swd_reminder:
        while ask("Potwierdź, że przewód SWD/J-Link jest ODŁĄCZONY (wpisz 'tak'): ").lower() != "tak":
            pass

    # --- 5. Wynik z Power Profilera -> dziennik CSV ---
    current = None
    while current is None:
        raw = ask("Średni prąd, np. '0.95' (µA) albo '2.5 mA' "
                  "('pomin' = bez zapisu): ")
        if raw.lower() in ("pomin", "pomiń", "p"):
            print(f"Pominięto zapis scenariusza '{name}'.")
            return
        try:
            current = parse_current(raw)
        except ValueError:
            print("Podaj liczbę: '0.95' / '7,3' (µA) albo '2.5 mA'.")

    uwagi = ask("Uwagi (Enter = brak): ")
    append_row(make_row(name, scen, profile, sample, voltage, current, uwagi))


def parse_current(raw, default_unit="uA"):
    """Wartość prądu -> µA. Przyjmuje '0.95', '7,3', '2.5 mA',
    '950 uA' / '950 µA'. Bez jawnej jednostki liczy wg `default_unit`
    ('uA' albo 'mA') – TUI podaje tu jednostkę z Selecta, CLI zostaje
    przy domyślnych µA."""
    s = raw.strip().lower().replace(",", ".").replace("µ", "u")
    factor = 1000.0 if default_unit.lower() == "ma" else 1.0
    if s.endswith("ma"):
        factor, s = 1000.0, s[:-2]
    elif s.endswith("ua"):
        factor, s = 1.0, s[:-2]
    return round(float(s.strip()) * factor, 6)


def parse_memory_report(output):
    """Wyłuskaj z wyjścia builda tabelkę 'Memory region' (podsumowanie
    zajętości FLASH/RAM z linkera) i sformatuj ją jako tabelę Markdown –
    gotową do wklejenia np. do PR-a czy notatki. Zwraca tekst tabeli albo
    None, gdy w wyjściu jej nie ma (np. przy flashu, nie buildzie)."""
    lines = output.splitlines() if isinstance(output, str) else list(output)
    start = next((i for i, ln in enumerate(lines)
                  if "Memory region" in ln and "Used Size" in ln), None)
    if start is None:
        return None
    # Wiersz: 'NAZWA:  <liczba> <jedn>B  <liczba> <jedn>B  <proc>%'
    # (rozmiary mają spację między liczbą a jednostką, więc łapiemy je
    # regexem zamiast dzielić po białych znakach).
    row_re = re.compile(r"^\s*(?P<region>\w[\w.]*)\s*:\s+"
                        r"(?P<used>[\d,]+\s*[KMGT]?B)\s+"
                        r"(?P<size>[\d,]+\s*[KMGT]?B)\s+"
                        r"(?P<pct>[\d.]+\s*%)\s*$")

    def norm(s):
        return re.sub(r"\s+", " ", s).strip()

    rows = []
    for ln in lines[start + 1:]:
        m = row_re.match(ln)
        if m:
            rows.append((m["region"], norm(m["used"]), norm(m["size"]),
                         norm(m["pct"])))
        elif rows or ln.strip():
            break  # koniec tabeli (albo zaraz po nagłówku nie ma wierszy)
    if not rows:
        return None
    out = ["| Memory region | Used Size | Region Size | %age Used |",
           "| --- | --- | --- | --- |"]
    out += [f"| {r} | {u} | {s} | {p} |" for r, u, s, p in rows]
    return "\n".join(out)


def copy_to_clipboard(text):
    """Skopiuj tekst do SYSTEMOWEGO schowka lokalnym narzędziem: pbcopy
    (macOS), wl-copy/xclip/xsel (Linux). Zwraca nazwę użytego narzędzia
    albo None, gdy żadnego nie ma. Pewniejsze niż OSC 52 (sekwencja, której
    część terminali – m.in. macOS Terminal.app – nie obsługuje)."""
    if sys.platform == "darwin":
        tools = [["pbcopy"]]
    else:
        tools = [["wl-copy"], ["xclip", "-selection", "clipboard"],
                 ["xsel", "--clipboard", "--input"]]
    for tool in tools:
        if shutil.which(tool[0]) is None:
            continue
        try:
            subprocess.run(tool, input=text, text=True, check=True)
            return tool[0]
        except (OSError, subprocess.CalledProcessError):
            continue
    return None


def save_text_log(text, name="ostatni-build.log"):
    """Zapisz tekst do reports/<name> i zwróć ścieżkę względną do repo –
    fallback, gdy schowka nie ma (log i tak zostaje do skopiowania z pliku)."""
    path = CSV_PATH.parent / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path.relative_to(ROOT)


def measure_instructions(scen, voltage, settle_s):
    """Tekst instrukcji pomiaru – wspólny dla CLI i TUI."""
    text = (" 1. ODŁĄCZ przewód SWD/J-Link (podłączony debugger dodaje prąd!).\n"
            f" 2. nRF Connect Power Profiler: tryb Source meter, {voltage} V,\n"
            "    VOUT -> VDD samego SoC, GND <-> GND (nic innego nie zasilaj).\n"
            f" 3. Odczekaj ~{settle_s} s na ustabilizowanie, potem odczytaj "
            "średni prąd.")
    if scen.get("expected"):
        text += f"\n    Oczekiwane wg datasheet: {scen['expected']}"
    if scen.get("note"):
        text += f"\n    Uwaga: {scen['note']}"
    return text


def make_row(name, scen, profile, sample, voltage, current, uwagi):
    """Wiersz dziennika CSV – wspólny dla CLI i TUI."""
    return {
        "data": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "plytka": profile["board"],
        "egzemplarz": sample,
        "scenariusz": name,
        "flagi": scenario_flags(scen),
        "napiecie_V": voltage,
        "prad_uA": current,
        "oczekiwane": scen.get("expected", ""),
        "uwagi": uwagi,
    }


def ensure_csv_schema():
    """Dociągnij stary dziennik do bieżącego schematu CSV_FIELDS.

    Pierwsza wersja narzędzia zapisywała tylko CSV_BASE_FIELDS; tryb
    autonomiczny dokłada kolumny (min/max, czas, sesja). Jeśli istniejący
    plik ma węższy nagłówek, przepisujemy go RAZ: nowy nagłówek + stare
    wiersze uzupełnione pustymi polami. Bez pliku albo z aktualnym
    nagłówkiem nic nie robimy."""
    if not CSV_PATH.is_file():
        return
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        missing = [c for c in CSV_FIELDS if c not in header]
        if not missing:
            return
        rows = list(reader)
    fields = header + missing
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in fields})


def append_row(row, verbose=True):
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not CSV_PATH.exists()
    if not new_file:
        ensure_csv_schema()
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)
    if verbose:
        print(f"Zapisano: {CSV_PATH.relative_to(ROOT)}")


TOOL_DIR = Path(__file__).resolve().parent
VIEWER_DEPS = ["PyQt6", "pyqtgraph"]


def viewer_deps_present():
    """Czy okno wykresu (PyQt6 + pyqtgraph) da się zaimportować w
    aktualnym pythonie? (bez importowania Qt do naszego procesu)."""
    import importlib.util
    return all(importlib.util.find_spec(m) is not None
               for m in ("PyQt6", "pyqtgraph", "numpy"))


def ensure_viewer_deps(interactive=True):
    """Doinstaluj zależności viewera do venva przy pierwszym użyciu –
    ciężkie (PyQt6), więc trzymamy je poza bazowym bootstrapem. Zwraca
    True, gdy są dostępne."""
    if viewer_deps_present():
        return True
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        print("Okno wykresu wymaga PyQt6 + pyqtgraph. Uruchom przez "
              "`board-power-test` (instaluje do venva) albo zainstaluj "
              "ręcznie: pip install " + " ".join(VIEWER_DEPS))
        return False
    if interactive:
        ans = ask(f"Okno wykresu potrzebuje {', '.join(VIEWER_DEPS)} "
                  "(jednorazowa instalacja do venva). Zainstalować? [t/N]: ")
        if ans.lower() not in ("t", "tak", "y", "yes"):
            print("Pominięto – bez zależności okno się nie otworzy.")
            return False
    print(f"Instaluję {', '.join(VIEWER_DEPS)}…")
    rc = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                         *VIEWER_DEPS, "numpy"]).returncode
    if rc != 0:
        print("Instalacja nie powiodła się.")
        return False
    return viewer_deps_present()


def launch_viewer(paths=(), live=False, interactive=True):
    """Uruchom osobny proces okna wykresu (viewer.__main__). Osobny
    proces, bo PyQt i Textual nie współdzielą pętli zdarzeń ani
    terminala. env NIE przez child_env() – viewer chce czystego pythona
    z venva (przywrócone PYTHONHOME popsułoby import Qt)."""
    if not ensure_viewer_deps(interactive=interactive):
        return None
    cmd = [sys.executable, "-m", "viewer"]
    if live and paths:
        cmd += ["--live", str(paths[0])]
    elif paths:
        cmd += ["--open", *[str(p) for p in paths]]
    return subprocess.Popen(cmd, cwd=str(TOOL_DIR), env=os.environ.copy())


def cmd_add(args):
    """`add <ścieżka>` – najprostsza droga dodania cudzego kodu:
    narzędzie samo rozpoznaje katalog aplikacji (source) vs plik .hex
    i dopisuje gotowy wpis do scenarios.toml."""
    try:
        name, entry = add_scenario(args.path, name=args.name,
                                   label=args.label, description=args.desc,
                                   base=Path.cwd())
    except ValueError as e:
        die(str(e))
    kind = "hex" if "hex" in entry else "source"
    what = ("gotowy obraz, bez budowania" if kind == "hex"
            else "aplikacja budowana przez west")
    print(f"Dodano scenariusz '{name}' ({what}) do "
          f"{MANIFEST_PATH.relative_to(ROOT)}:")
    for key, value in entry.items():
        print(f"  {key:<11} = {_toml_str(value)}")
    print(f"\nUruchomienie:  board-power-test run {name}\n"
          "Wpis można doszlifować ręcznie w scenarios.toml "
          "(label, expected, voltage, cmake_args...).")


def cmd_report(args):
    if not CSV_PATH.is_file():
        die(f"brak pomiarów ({CSV_PATH.relative_to(ROOT)} nie istnieje). "
            "Najpierw uruchom `run`.")
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        die("plik pomiarów jest pusty")
    cols = ["data", "egzemplarz", "scenariusz", "parametr", "wartosc",
            "napiecie_V", "prad_uA", "oczekiwane", "uwagi"]
    print_table(tuple(cols), [tuple(r.get(c, "") for c in cols) for r in rows])
    print(f"\n({len(rows)} pomiarów; pełne dane, w tym flagi builda: "
          f"{CSV_PATH.relative_to(ROOT)})")


def cmd_autorun(args):
    """`autorun <plan.toml>` – tryb autonomiczny: build+flash+pomiar
    całego planu bez udziału człowieka (PPK2 sam zasila i mierzy).
    Bogaty podgląd na żywo jest w TUI; tu strumieniujemy zdarzenia
    tekstem – idealne do sesji nocnej przez SSH."""
    import threading

    from autorun.engine import AutoRunner
    from autorun.plan import load_plan, validate_plan

    try:
        plan = load_plan(args.plan)
    except ValueError as e:
        die(str(e))
    manifest = load_manifest()
    if args.board:
        plan.board = args.board
    errors = validate_plan(plan, manifest)
    if errors:
        die("\n  ".join(["błędy planu:"] + errors))

    sample = args.sample
    if not sample and not args.dry_run:
        while not sample:
            sample = ask("Egzemplarz płytki (np. 'BTZ #2'): ")

    def on_event(ev):
        if ev.kind in ("phase", "plan_start"):
            print(f"\n=== {ev.text or ev.data} ===")
        elif ev.kind == "note":
            print(ev.text)
        elif ev.kind == "state":
            detail = ev.data.get("detail", "")
            label = {"power": "zasilanie", "flash": "flash",
                     "build": "build", "trigger": "trigger",
                     "measure": "pomiar", "build_failed": "build padł",
                     }.get(ev.text, ev.text)
            print(f"  [krok {ev.step}] {ev.name} -> {label}"
                  + (f": {detail}" if detail else ""))
        elif ev.kind == "line":
            print(f"    {ev.text}")
        elif ev.kind == "live":
            d = ev.data
            print(f"    pomiar {ev.name}: {d.get('elapsed_s')}/"
                  f"{d.get('duration_s')} s  śr {d.get('avg_uA')} µA",
                  end="\r", flush=True)
        elif ev.kind == "annotation":
            print(f"\n    ⟟ etykieta: {ev.text} @ {ev.data.get('t_s')} s")
        elif ev.kind == "step_done":
            d = ev.data
            print(f"\n  [krok {ev.step}] {ev.name}: śr {d.get('avg_uA')} µA "
                  f"(min {d.get('min_uA')}, max {d.get('max_uA')})")

    cancel = threading.Event()
    runner = AutoRunner(plan, manifest, sample, event_cb=on_event,
                        cancel=cancel, dry_run=args.dry_run)
    try:
        results = runner.run()
    except KeyboardInterrupt:
        cancel.set()
        print("\nPrzerywam plan…")
        return
    except Exception as e:               # AutoRunError itd.
        die(str(e))

    print("\n\n=== PODSUMOWANIE PLANU ===")
    for r in results:
        avg = r.summary.get("avg_uA")
        extra = f"śr {avg} µA" if avg is not None else (r.error or "")
        print(f"  krok {r.label or r.index} {r.scenario}: {r.status}"
              + (f" – {extra}" if extra else ""))
    if not args.dry_run:
        print(f"\nSesje z wykresami: {runner.run_dir.relative_to(ROOT)}/\n"
              "Podgląd wykresu:  board-power-test viewer "
              f"{runner.run_dir.relative_to(ROOT)}")


def cmd_viewer(args):
    """`viewer [katalog…]` – otwórz okno wykresu (PyQt+pyqtgraph) na
    wskazanych sesjach albo bibliotekę historii (bez argumentów).
    Zależności Qt doinstalowują się leniwie przy pierwszym uruchomieniu."""
    launch_viewer(args.paths, live=False)


def cmd_interactive():
    """Tryb prowadzony (bez argumentów): menu zamiast komend."""
    manifest = load_manifest()
    profiles = manifest.get("boards", {})
    default_prof = manifest.get("defaults", {}).get("profile")
    if not profiles:
        die("manifest nie zawiera sekcji [boards.*]")

    # --- wybór profilu płytki ---
    prof_names = list(profiles)
    print("Płytka:")
    for i, n in enumerate(prof_names, 1):
        mark = "  (domyślna)" if n == default_prof else ""
        print(f"  {i}. {profiles[n]['board']}{mark}")
    prof_name = None
    while prof_name is None:
        raw = ask(f"Wybierz [Enter = {default_prof}]: ")
        if not raw and default_prof in profiles:
            prof_name = default_prof
        elif raw in profiles:
            prof_name = raw
        elif raw.isdigit() and 1 <= int(raw) <= len(prof_names):
            prof_name = prof_names[int(raw) - 1]
        else:
            print(f"Podaj numer 1-{len(prof_names)} albo nazwę profilu.")

    # --- wybór scenariuszy ---
    scenarios = manifest.get("scenarios", {})
    if not scenarios:
        die("manifest nie zawiera żadnych scenariuszy")
    scen_names = list(scenarios)
    print("\nScenariusze:")
    for i, n in enumerate(scen_names, 1):
        print(f"  {i}. {scenarios[n].get('label', n)} [{n}]\n"
              f"     {scenarios[n].get('description', '')}")
    chosen = None
    while chosen is None:
        raw = ask("Wybierz (np. '1 3', 'a' = wszystkie): ")
        tokens = raw.replace(",", " ").split()
        if raw.lower() in ("a", "all", "w", "wszystkie"):
            chosen = scen_names
        elif tokens and all(
                t in scenarios or (t.isdigit() and 1 <= int(t) <= len(scen_names))
                for t in tokens):
            chosen = [t if t in scenarios else scen_names[int(t) - 1]
                      for t in tokens]
        else:
            print(f"Podaj numery 1-{len(scen_names)} (odstępy/przecinki), "
                  "nazwy scenariuszy albo 'a'.")

    print(f"\nDo zrobienia: {', '.join(chosen)}  [profil: {prof_name}]")
    cmd_run(argparse.Namespace(scenarios=chosen, all=False, profile=prof_name,
                               sample=None, no_erase=False, no_reset=False,
                               no_swd_reminder=False, dry_run=False,
                               pristine=False))


def main():
    # Bez argumentów: interfejs okienkowy (TUI, Textual); gdy biblioteki
    # nie ma (offline / BPT_NO_TUI=1) – klasyczne menu tekstowe.
    if len(sys.argv) == 1:
        if os.environ.get("BPT_NO_TUI") != "1":
            try:
                from tui import PowerTestApp
                PowerTestApp().run()
                return
            except ImportError:
                print("(interfejs TUI niedostępny – brak biblioteki 'textual'; "
                      "używam menu tekstowego)")
        cmd_interactive()
        return

    ap = argparse.ArgumentParser(
        prog="power-test",
        description="Build+flash czystych obrazów pomiarowych i dziennik "
                    "pomiarów PPK2 (pomiar robisz w nRF Connect Power Profiler). "
                    "Uruchomienie bez argumentów otwiera tryb interaktywny (menu).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="wypisz scenariusze z scenarios.toml") \
       .set_defaults(func=cmd_list)

    run = sub.add_parser("run", help="build -> flash -> pomiar -> zapis do CSV")
    run.add_argument("scenarios", nargs="*", help="nazwy scenariuszy (patrz `list`)")
    run.add_argument("--all", action="store_true", help="wszystkie scenariusze po kolei")
    run.add_argument("--profile", "-p", help="profil płytki z [boards.*] (dom. z [defaults])")
    run.add_argument("--sample", "-s", help="egzemplarz płytki, np. 'BTZ #2'")
    run.add_argument("--no-erase", action="store_true",
                     help="flash bez --erase (domyślnie kasujemy, bo stan "
                          "pinów/UICR zostaje z poprzedniego obrazu)")
    run.add_argument("--no-reset", action="store_true",
                     help="flash bez wymuszonego resetu (domyślnie po wgraniu "
                          "resetujemy płytkę przez J-Link, żeby firmware "
                          "wystartował od razu)")
    run.add_argument("--no-swd-reminder", action="store_true",
                     help="pomiń przypomnienie o odpięciu programatora "
                          "(SWD/J-Link) przed pomiarem – gdy mierzysz bez "
                          "podłączonego debuggera")
    run.add_argument("--pristine", action="store_true",
                     help="wymuś czysty (pełny) build zamiast przyrostowego "
                          "(domyślnie west -p auto: buduje tylko zmiany)")
    run.add_argument("--dry-run", "-n", action="store_true",
                     help="tylko pokaż komendy i instrukcję, nic nie wykonuj")
    run.set_defaults(func=cmd_run)

    add = sub.add_parser(
        "add", help="dodaj scenariusz z własnym firmware (katalog aplikacji "
                    "Zephyr/NCS albo gotowy plik .hex)")
    add.add_argument("path", help="katalog aplikacji (source) albo plik .hex; "
                                  "rodzaj wykrywany automatycznie")
    add.add_argument("--name", help="klucz scenariusza (dom. z nazwy "
                                    "katalogu/pliku)")
    add.add_argument("--label", help="nazwa wyświetlana w interfejsie")
    add.add_argument("--desc", help="opis scenariusza")
    add.set_defaults(func=cmd_add)

    sub.add_parser("report", help="tabela zebranych pomiarów (reports/pomiary.csv)") \
       .set_defaults(func=cmd_report)

    auto = sub.add_parser(
        "autorun", help="tryb autonomiczny: wykonaj plan (plans/*.toml) – "
                        "build+flash+pomiar PPK2 bez udziału człowieka")
    auto.add_argument("plan", help="ścieżka do pliku planu (plans/<nazwa>.toml)")
    auto.add_argument("--sample", "-s", help="egzemplarz płytki, np. 'BTZ #2'")
    auto.add_argument("--profile", "-p", dest="board",
                      help="profil płytki z [boards.*] (nadpisuje pole planu)")
    auto.add_argument("--dry-run", "-n", action="store_true",
                      help="pokaż komendy i przejdź kroki bez sprzętu "
                           "(bez PPK2 i bez faktycznego pomiaru)")
    auto.set_defaults(func=cmd_autorun)

    view = sub.add_parser(
        "viewer", help="otwórz okno wykresu (PyQt) na sesjach pomiarowych "
                       "albo bibliotekę historii (bez argumentów)")
    view.add_argument("paths", nargs="*",
                      help="katalogi sesji (reports/sessions/…); bez nich "
                           "otwiera się biblioteka historii")
    view.set_defaults(func=cmd_viewer)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
