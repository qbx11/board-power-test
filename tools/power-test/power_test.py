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
import shlex
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("power-test wymaga Pythona >= 3.11 (tomllib). "
             f"Ten interpreter to {sys.version.split()[0]}.")

import tomllib  # noqa: E402  (import po sprawdzeniu wersji, celowo)

# Katalog projektu: dwa poziomy nad tym plikiem (tools/power-test/..)
ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "scenarios.toml"
CSV_PATH = ROOT / "reports" / "pomiary.csv"
CSV_FIELDS = ["data", "plytka", "egzemplarz", "scenariusz", "flagi",
              "napiecie_V", "prad_uA", "oczekiwane", "uwagi"]


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
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        die(f"scenarios.toml nie parsuje się: {e}")


def child_env():
    """Środowisko dla procesów west: przywróć PYTHONHOME/PYTHONPATH zdjęte
    przy starcie (re-exec na górze pliku) – narzędzia toolchaina NCS ich
    potrzebują, tylko naszemu pythonowi szkodzą."""
    env = os.environ.copy()
    for var in ("PYTHONHOME", "PYTHONPATH"):
        saved = env.pop("BPT_SAVED_" + var, None)
        if saved:
            env[var] = saved
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
    faktycznie zawiera boards/ (jak w scripts/build.sh)."""
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

    workspace = ROOT
    if not args.dry_run:
        if shutil.which("west") is None:
            die("brak 'west' w PATH. Otwórz terminal nRF Connect lub uruchom:\n"
                "  nrfutil toolchain-manager launch --ncs-version v3.4.0 --shell")
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
    print(f"\n=== FAZA 1/2: budowanie {len(names)} obraz(ów) ===")
    built = {}
    for name in names:
        scen = scenarios[name]
        if not scen.get("cmake_args"):
            die(f"scenariusz '{name}' nie ma cmake_args w manifeście")
        print(f"\n--- build: {name} – {scen.get('description', '')}")
        cmd, build_dir = make_build_cmd(name, scen, prof_name, profile)
        run_cmd(cmd, args.dry_run, cwd=workspace)
        built[name] = build_dir

    # --- FAZA 2: flash + pomiar, scenariusz po scenariuszu ---
    print(f"\n=== FAZA 2/2: flash + pomiar ({len(names)} scenariusz(y)) ===")
    for name in names:
        measure_scenario(name, scenarios[name], built[name], profile,
                         defaults, sample, args, workspace)

    if not args.dry_run:
        print(f"\nGotowe. Podgląd zebranych pomiarów: "
              f"python3 tools/power-test/{Path(__file__).name} report")


def make_build_cmd(name, scen, prof_name, profile):
    """Komenda `west build` + katalog builda dla scenariusza.

    Ścieżki absolutne, bo west może być wołany z katalogu SDK
    (build out-of-tree, gdy repo leży poza workspace'em)."""
    build_dir = f"build_{name}" if prof_name == "btz" else f"build_{prof_name}_{name}"
    cmd = ["west", "build", "-b", profile["board"], "-p", "always",
           "-d", str(ROOT / build_dir), str(ROOT)]
    root_arg = board_root_arg(profile)
    extra = [root_arg] if root_arg else []
    return cmd + ["--"] + extra + list(scen["cmake_args"]), build_dir


def make_flash_cmd(build_dir, profile, erase=True):
    """Komenda `west flash`; --erase domyślnie (stan pinów/UICR potrafi
    zostać z poprzedniego obrazu i zafałszować pomiar)."""
    cmd = ["west", "flash", "-d", str(ROOT / build_dir)]
    if profile.get("runner"):
        cmd += ["-r", profile["runner"]]
    if erase:
        cmd += ["--erase"]
    return cmd


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
    run_cmd(make_flash_cmd(build_dir, profile, erase=not args.no_erase),
            args.dry_run, cwd=workspace)

    # --- Instrukcja pomiaru (Power Profiler robi resztę) ---
    print(f"\n--- POMIAR ({name}) ---------------------------------------")
    print(measure_instructions(scen, voltage, settle_s))
    print("-" * 59)

    if args.dry_run:
        return

    # --- 4. Twarde potwierdzenie odłączenia SWD (bez tego pomiar
    #        minimum jest śmieciem – wymagamy wpisania 'tak') ---
    while ask("Potwierdź, że przewód SWD/J-Link jest ODŁĄCZONY (wpisz 'tak'): ").lower() != "tak":
        pass

    # --- 5. Wynik z Power Profilera -> dziennik CSV ---
    current = None
    while current is None:
        raw = ask("Średni prąd [uA] (albo 'pomin', by nie zapisywać): ")
        if raw.lower() in ("pomin", "pomiń", "p"):
            print(f"Pominięto zapis scenariusza '{name}'.")
            return
        try:
            current = float(raw.replace(",", "."))
        except ValueError:
            print("Podaj liczbę w uA, np. 0.95 albo 7,3.")

    uwagi = ask("Uwagi (Enter = brak): ")
    append_row(make_row(name, scen, profile, sample, voltage, current, uwagi))


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
        "flagi": " ".join(scen["cmake_args"]),
        "napiecie_V": voltage,
        "prad_uA": current,
        "oczekiwane": scen.get("expected", ""),
        "uwagi": uwagi,
    }


def append_row(row, verbose=True):
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not CSV_PATH.exists()
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)
    if verbose:
        print(f"Zapisano: {CSV_PATH.relative_to(ROOT)}")


def cmd_report(args):
    if not CSV_PATH.is_file():
        die(f"brak pomiarów ({CSV_PATH.relative_to(ROOT)} nie istnieje). "
            "Najpierw uruchom `run`.")
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        die("plik pomiarów jest pusty")
    cols = ["data", "egzemplarz", "scenariusz", "napiecie_V",
            "prad_uA", "oczekiwane", "uwagi"]
    print_table(tuple(cols), [tuple(r.get(c, "") for c in cols) for r in rows])
    print(f"\n({len(rows)} pomiarów; pełne dane, w tym flagi builda: "
          f"{CSV_PATH.relative_to(ROOT)})")


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
        print(f"  {i}. {profiles[n].get('label', n)} – "
              f"{profiles[n]['board']}{mark}")
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
                               sample=None, no_erase=False, dry_run=False))


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
    run.add_argument("--dry-run", "-n", action="store_true",
                     help="tylko pokaż komendy i instrukcję, nic nie wykonuj")
    run.set_defaults(func=cmd_run)

    sub.add_parser("report", help="tabela zebranych pomiarów (reports/pomiary.csv)") \
       .set_defaults(func=cmd_report)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
