# ============================================================
#  board-power-test – interfejs okienkowy (TUI, Textual)
# ============================================================
# Uruchamiany przez power_test.py, gdy nie podano argumentów i biblioteka
# `textual` jest dostępna (launcher instaluje ją w .venv repo). Cała
# logika (manifest, komendy west, dziennik CSV) jest w power_test.py –
# ten plik to wyłącznie warstwa prezentacji.
#
# Zasady designu: minimalistycznie, monochromatycznie (jeden kolor,
# bez kolorowych wypełnień – tylko ramki i typografia). Wyjścia komend
# są zwinięte (tytuł = preview); rozwijają się po kliknięciu albo
# automatycznie przy błędzie.
#
# Uwaga implementacyjna: procesy west uruchamiamy przez subprocess.Popen
# w wątku (asyncio.to_thread), NIE przez asyncio.create_subprocess_exec –
# transporty asyncio-subprocess wywracały pętlę zdarzeń na Linuksie
# ("Event loop is closed", ContextVar token errors). stdin=DEVNULL, żeby
# dziecko (J-Link itp.) nie dotykało terminala, który trzyma Textual.

import asyncio
import csv
import shlex
import shutil
import subprocess

from textual import work
from textual.app import App
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (Button, Checkbox, Collapsible, DataTable, Input,
                             Label, Log, Select, Static)

import power_test as core

# Logo GoodByte (pixelart półblokami, wygenerowane z logo firmowego) –
# nagłówek ekranu głównego. Monochromatyczne jak reszta interfejsu.
LOGO = """\
╭─                                                                ─╮
        ▄▄▄▄▄                  ▄  ▄▄▄▄
       █▀▀  ▀                  █  █▀ ▀█▄      ██
      ▄█   ▄  ▄█▀▀█  █▀▀█▄ ▄█▀██  █▄▄▄█ █▄  █▀██▀ ▄▀▀█▄
      ▀█  ▀▀█ █   ████   ███   █  █▀  ▀█ █▄██ ██ ██▄▄██
       ▀█▄▄▄█ ▀█▄▄█▀ █▄▄██ █▄▄▄█  █▄▄▄█▀  ██  ██▄ █▄▄▄
         ▀▀▀    ▀▀    ▀▀    ▀▀▀▀  ▀▀▀▀    █    ▀▀  ▀▀▀
[#888888]               e m b e d d e d   s y s t e m s[/]
╰─                                                                ─╯"""


def _stream(cmd, cwd, on_line):
    """Uruchom proces i strumieniuj linie wyjścia (wołane w wątku).
    env=child_env(): procesy west dostają z powrotem PYTHONHOME/PYTHONPATH
    toolchaina, które naszemu pythonowi zdjęto przy starcie."""
    with subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace",
                          env=core.child_env()) as proc:
        for line in proc.stdout:
            on_line(line.rstrip())
        return proc.wait()


def _label(name, item):
    """Nazwa do wyświetlenia: `label` z manifestu, inaczej klucz."""
    return item.get("label", name)


class DescArrow(Static):
    """Strzałka w linii tytułu scenariusza – rozwija/zwija opis pod
    spodem (osobny Static pełnej szerokości, więc tekst opisu ma stałe,
    małe wcięcie zamiast zaczynać się dopiero za nazwą)."""

    def __init__(self, desc_id, **kwargs):
        super().__init__("▶", classes="scen-arrow", **kwargs)
        self.desc_id = desc_id

    def on_click(self, event):
        event.stop()
        desc = self.screen.query_one(f"#{self.desc_id}", Static)
        shown = not desc.has_class("shown")
        desc.set_class(shown, "shown")
        self.update("▼" if shown else "▶")


class ConfirmScreen(ModalScreen[bool]):
    """Dialog z pytaniem; `no=None` daje pojedynczy przycisk (twardy krok)."""

    def __init__(self, text, yes="OK", no="Anuluj"):
        super().__init__()
        self.text, self.yes, self.no = text, yes, no

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(self.text, classes="dialog-text")
            with Horizontal(classes="dialog-buttons"):
                yield Button(self.yes, id="yes")
                if self.no is not None:
                    yield Button(self.no, id="no")

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "yes")


class ChoiceScreen(ModalScreen[str]):
    """Dialog z kilkoma opcjami; zwraca id klikniętego przycisku."""

    def __init__(self, text, choices):
        super().__init__()
        self.text, self.choices = text, choices  # choices: [(label, id), ...]

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(self.text, classes="dialog-text")
            with Horizontal(classes="dialog-buttons"):
                for label, choice_id in self.choices:
                    yield Button(label, id=choice_id)

    def on_button_pressed(self, event):
        self.dismiss(event.button.id)


class MeasureScreen(ModalScreen):
    """Instrukcja pomiaru + pola: średni prąd (µA/mA) i uwagi. Zwraca
    (prąd_w_µA, uwagi), "reflash" (wgraj płytkę ponownie) albo None
    przy pominięciu."""

    def __init__(self, scen_name, scen, voltage, settle_s):
        super().__init__()
        self.scen_name, self.scen = scen_name, scen
        self.voltage, self.settle_s = voltage, settle_s

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(f"[b]POMIAR: {_label(self.scen_name, self.scen)}[/b]"
                         f"\n{self.scen.get('description', '')}")
            yield Static(core.measure_instructions(self.scen, self.voltage,
                                                   self.settle_s),
                         classes="dialog-text")
            yield Label("Średni prąd:")
            with Horizontal(id="current-row"):
                yield Input(placeholder="np. 0.95", id="current")
                yield Select([("µA", "uA"), ("mA", "mA")], value="uA",
                             allow_blank=False, id="unit")
            yield Label("Uwagi (opcjonalnie):")
            yield Input(placeholder="np. 'przed poprawką HW'", id="notes")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Zapisz", id="save")
                yield Button("Wgraj ponownie", id="reflash")
                yield Button("Pomiń (bez zapisu)", id="skip")

    def on_mount(self):
        self.query_one("#current", Input).focus()

    def on_input_submitted(self, event):
        self._save()

    def on_button_pressed(self, event):
        if event.button.id == "skip":
            self.dismiss(None)
        elif event.button.id == "reflash":
            self.dismiss("reflash")
        else:
            self._save()

    def _save(self):
        # Jedno źródło parsowania (core.parse_current): Select daje jednostkę
        # domyślną, ale jawny sufiks w polu (np. '2.5 mA') ma pierwszeństwo.
        raw = self.query_one("#current", Input).value
        unit = self.query_one("#unit", Select).value
        try:
            current = core.parse_current(raw, default_unit=unit)  # -> µA
        except ValueError:
            self.app.notify("Podaj liczbę, np. 0.95 albo 7,3.",
                            severity="error")
            self.query_one("#current", Input).focus()
            return
        self.dismiss((current, self.query_one("#notes", Input).value.strip()))


class AddScreen(ModalScreen):
    """Dodanie scenariusza z własnym firmware jedną ścieżką: katalog
    aplikacji Zephyr/NCS -> wariant `source` (build przez west), plik
    .hex -> wariant `hex` (bez budowania). Wpis dopisuje
    core.add_scenario do scenarios.toml; zwraca (nazwa, wpis) albo None."""

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static("[b]Dodaj własny firmware[/b]\n"
                         "Podaj katalog aplikacji Zephyr/NCS (będzie "
                         "budowana west-em) albo gotowy plik .hex (bez "
                         "budowania) – rodzaj wykrywany automatycznie.\n"
                         "Ścieżka względna = od katalogu repo.",
                         classes="dialog-text")
            yield Label("Ścieżka (katalog aplikacji albo plik .hex):")
            yield Input(placeholder="np. ../moj-projekt/app  albo  "
                                    "../moj-projekt/build/zephyr/zephyr.hex",
                        id="path")
            yield Label("Nazwa w interfejsie (opcjonalnie):")
            yield Input(placeholder="np. Moja aplikacja — sen", id="label")
            yield Label("Opis (opcjonalnie):")
            yield Input(placeholder="np. firmware sprzedażowe v1.2",
                        id="desc")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Dodaj", id="add")
                yield Button("Anuluj", id="cancel")

    def on_mount(self):
        self.query_one("#path", Input).focus()

    def on_input_submitted(self, event):
        self._add()

    def on_button_pressed(self, event):
        if event.button.id == "add":
            self._add()
        else:
            self.dismiss(None)

    def _add(self):
        path = self.query_one("#path", Input).value.strip()
        if not path:
            self.app.notify("Podaj ścieżkę do firmware.", severity="error")
            self.query_one("#path", Input).focus()
            return
        try:
            name, entry = core.add_scenario(
                path,
                label=self.query_one("#label", Input).value.strip() or None,
                description=self.query_one("#desc",
                                           Input).value.strip() or None,
                base=core.ROOT)
        except ValueError as e:
            self.app.notify(str(e), severity="error")
            return
        self.dismiss((name, entry))


class ResultsScreen(ModalScreen):
    """Podgląd dziennika pomiarów (reports/pomiary.csv)."""

    def compose(self):
        with Vertical(classes="dialog results"):
            yield Static("[b]Zebrane pomiary[/b] – reports/pomiary.csv")
            yield DataTable()
            with Horizontal(classes="dialog-buttons"):
                yield Button("Zamknij", id="close")

    def on_mount(self):
        table = self.query_one(DataTable)
        cols = ["data", "egzemplarz", "scenariusz", "napiecie_V",
                "prad_uA", "oczekiwane", "uwagi"]
        table.add_columns(*cols)
        if core.CSV_PATH.is_file():
            with open(core.CSV_PATH, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    table.add_row(*(row.get(c, "") for c in cols))

    def on_button_pressed(self, event):
        self.dismiss()


class RunScreen(Screen):
    """Przebieg: FAZA 1 buduje wszystkie obrazy, FAZA 2 – flash + pomiar.
    Każda komenda to zwijana sekcja: tytuł = preview, rozwija się
    klikiem albo automatycznie przy błędzie."""

    BINDINGS = [("escape", "app.pop_screen", "Przerwij i wróć")]

    def __init__(self, prof_name, profile, names, sample):
        super().__init__()
        self.prof_name, self.profile = prof_name, profile
        self.names, self.sample = names, sample

    def compose(self):
        yield Static("", id="status")
        yield VerticalScroll(id="cmds")
        yield Static("Esc — przerwij i wróć", id="hint")

    def on_mount(self):
        self.flow()

    def note(self, text):
        self.query_one("#cmds").mount(Static(text, classes="note"))

    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    async def run_west(self, cmd, cwd, title):
        """Komenda w zwijanej sekcji z animacją w trakcie działania;
        pełne wyjście po kliknięciu/błędzie."""
        out = Log(classes="cmd-log")
        section = Collapsible(out, title=f"{self.SPINNER[0]} {title}",
                              collapsed=True)
        cmds = self.query_one("#cmds")
        await cmds.mount(section)
        cmds.scroll_end(animate=False)
        out.write_line(f"$ {shlex.join(cmd)}")

        frame = {"i": 0}

        def tick():
            frame["i"] = (frame["i"] + 1) % len(self.SPINNER)
            section.title = f"{self.SPINNER[frame['i']]} {title}"

        spinner = self.set_interval(1 / 8, tick)
        try:
            rc = await asyncio.to_thread(
                _stream, cmd, cwd,
                lambda line: self.app.call_from_thread(out.write_line, line))
        finally:
            spinner.stop()
        if rc != 0:
            section.title = f"✗ {title} — kod {rc}"
            section.collapsed = False
            raise RuntimeError(f"'{title}' zakończone błędem (kod {rc})")
        section.title = f"✓ {title}"

    @work
    async def flow(self):
        status = self.query_one("#status", Static)
        scenarios = self.app.scenarios
        defaults = self.app.defaults
        total = len(self.names)
        try:
            # Walidacja wpisów source/hex PRZED startem FAZY 1.
            errors = core.validate_scenarios(self.names, scenarios)
            if errors:
                raise RuntimeError("\n".join(errors))

            # Scenariusze `hex` mają gotową binarkę – FAZA 1 ich nie dotyczy.
            to_build = [n for n in self.names if "hex" not in scenarios[n]]
            if to_build and shutil.which("west") is None:
                raise RuntimeError(
                    "brak 'west' w PATH – uruchom przez `board-power-test` "
                    "(launcher startuje środowisko NCS) albo w terminalu "
                    "nRF Connect")
            if len(to_build) < total and shutil.which("nrfutil") is None:
                raise RuntimeError(
                    "brak 'nrfutil' w PATH – potrzebny do wgrania gotowego "
                    "pliku hex (scenariusze z polem `hex`)")
            workspace = core.ROOT
            if to_build:
                workspace = await asyncio.to_thread(core.find_west_workspace)
                if workspace != core.ROOT:
                    self.note(f"Workspace NCS: {workspace} (build out-of-tree)")

            # --- FAZA 1: wszystkie buildy z góry ---
            built = {}
            build_no = 0
            for name in self.names:
                scen = scenarios[name]
                if "hex" in scen:
                    built[name] = None
                    self.note(f"{name}: gotowy hex ({scen['hex']}) – "
                              "bez budowania.")
                    continue
                build_no += 1
                status.update(f"FAZA 1/2 · build {build_no}/{len(to_build)}"
                              f" · {name}")
                cmd, build_dir = core.make_build_cmd(
                    name, scen, self.prof_name, self.profile,
                    defaults.get("profile"))
                await self.run_west(cmd, workspace, f"build {name}")
                built[name] = build_dir
            self.note(f"Zbudowano {len(to_build)} obraz(ów)." if to_build
                      else "Nic do budowania (same gotowe pliki hex).")

            # --- FAZA 2: flash + pomiar ---
            saved = []
            for i, name in enumerate(self.names, 1):
                scen = scenarios[name]
                voltage = str(scen.get("voltage",
                                       defaults.get("voltage", "3.0")))
                settle_s = scen.get("settle_s", defaults.get("settle_s", 5))
                status.update(f"FAZA 2/2 · scenariusz {i}/{total} · {name}")

                # Pętla scenariusza: flash (z ponawianiem po błędzie),
                # pomiar – a z okna pomiaru można wrócić do flasha
                # ("Wgraj ponownie") bez cofania całego przebiegu.
                attempt = 1
                while True:
                    ok = await self.app.push_screen_wait(ConfirmScreen(
                        f"[b]{_label(name, scen)}[/b]\n"
                        f"{scen.get('description', '')}\n\n"
                        "Programator podłączony i płytka ZASILONA\n"
                        "(np. VOUT z PPK2)?",
                        yes=("Wgraj ponownie" if attempt > 1
                             else "Wgraj gotowy hex (z kasowaniem)"
                             if scen.get("hex") else "Wgraj (flash --erase)"),
                        no="Pomiń scenariusz"))
                    if not ok:
                        self.note(f"Pominięto {name}.")
                        break

                    # Flash z ponawianiem: zły kabel/programator nie cofa
                    # przebiegu – popraw i spróbuj jeszcze raz.
                    flashed = False
                    while True:
                        try:
                            await self.run_west(core.flash_cmd_for(
                                scen, built[name], self.profile),
                                workspace, f"flash {name}")
                            flashed = True
                            break
                        except RuntimeError as err:
                            choice = await self.app.push_screen_wait(
                                ChoiceScreen(
                                    f"[b]Flash nie powiódł się[/b]\n{err}\n\n"
                                    "Sprawdź: kabel SWD wpięty? programator\n"
                                    "widzi płytkę? płytka zasilona\n"
                                    "(VOUT z PPK2)?",
                                    [("Ponów flash", "retry"),
                                     ("Pomiń scenariusz", "skip"),
                                     ("Przerwij wszystko", "abort")]))
                            if choice == "retry":
                                continue
                            if choice == "skip":
                                break
                            raise
                    if not flashed:
                        self.note(f"Pominięto {name} (flash nieudany).")
                        break

                    # Twarde potwierdzenie SWD – jedyny przycisk.
                    await self.app.push_screen_wait(ConfirmScreen(
                        "[b]ODŁĄCZ przewód SWD/J-Link![/b]\n\n"
                        "Podłączony debugger dodaje własny prąd\n"
                        "i unieważnia pomiar minimum.",
                        yes="SWD ODŁĄCZONY – przejdź do pomiaru", no=None))

                    result = await self.app.push_screen_wait(
                        MeasureScreen(name, scen, voltage, settle_s))
                    if result == "reflash":
                        attempt += 1
                        self.note(f"{name}: ponowne wgranie na życzenie.")
                        continue
                    if result is None:
                        self.note(f"Pominięto zapis scenariusza {name}.")
                        break
                    current, notes = result
                    core.append_row(core.make_row(name, scen, self.profile,
                                                  self.sample, voltage,
                                                  current, notes),
                                    verbose=False)
                    saved.append(f"{name}: {current} µA")
                    self.note(f"Zapisano: {name} = {current} µA")
                    break

            status.update("Gotowe.")
            summary = ("\n".join(saved) if saved
                       else "(nic nie zapisano)")
            await self.app.push_screen_wait(ConfirmScreen(
                f"[b]Zakończono.[/b] Zapisane pomiary "
                f"({self.sample}):\n\n{summary}\n\n"
                "Dziennik: reports/pomiary.csv (commituj do repo!)",
                yes="OK", no=None))
            self.app.pop_screen()
        except (SystemExit, RuntimeError) as e:
            msg = str(e) or "przerwano"
            status.update(f"BŁĄD: {msg}")
            self.note("(Esc = powrót do ustawień)")


class PowerTestApp(App):
    TITLE = "board-power-test"
    ENABLE_COMMAND_PALETTE = False  # bez przycisku/skrótu palety komend
    BINDINGS = [("ctrl+q", "quit", "Wyjście")]

    # Monochromatycznie: jeden kolor (odcienie szarości), zero kolorowych
    # wypełnień – tylko ramki i typografia. Focus = jaśniejsza ramka;
    # przyciski nie zmieniają tła w żadnym stanie (nic nie wygląda na
    # "wciśnięte" na stałe).
    CSS = """
    * { scrollbar-background: transparent;
        scrollbar-background-hover: transparent;
        scrollbar-background-active: transparent;
        scrollbar-color: #444444;
        scrollbar-color-hover: #777777;
        scrollbar-color-active: #777777;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1; }

    #setup { padding: 1 2; }
    #logo { color: $text; width: 72; max-width: 100%; height: auto;
            margin-bottom: 1; text-wrap: nowrap; text-overflow: clip; }
    .h { margin-top: 1; text-style: bold; }
    #profile, #sample, #scenarios { width: 72; max-width: 100%; }
    #scenarios { border: round #555555; background: transparent;
                 height: auto; max-height: 18; overflow-y: auto;
                 padding: 0 1; }
    .scenario-row { height: auto; }
    .scenario-head { height: 1; }
    .scen-check { border: none; background: transparent; padding: 0;
                  height: 1; width: auto; }
    .scen-check:focus { text-style: bold; }
    .scen-check.-on { text-style: bold; }
    .scen-arrow { width: 3; color: #888888; padding: 0 0 0 1; }
    .scen-arrow:hover { color: $text; }
    .scen-desc { display: none; color: #888888; margin: 0 0 0 4; }
    .scen-desc.shown { display: block; }
    /* X w checkboksie: niewidoczny gdy odznaczony (kolor tła), widoczny
       po zaznaczeniu – inaczej nie widać, co jest wybrane. */
    ToggleButton > .toggle--button { background: transparent;
                                     color: $background; }
    ToggleButton.-on > .toggle--button { background: transparent;
                                         color: $text; }
    Input { background: transparent; border: round #555555; }
    Input:focus { border: round #aaaaaa; }
    SelectCurrent { background: transparent; border: round #555555; }
    Select:focus SelectCurrent { border: round #aaaaaa; }
    SelectOverlay { background: $surface; border: round #555555; }
    SelectOverlay OptionList { background: transparent; }
    OptionList > .option-list--option-highlighted { background: #333333;
                                                    text-style: none; }

    Button { background: transparent; border: round #555555;
             color: $text; min-width: 10; text-style: none; }
    Button:hover { background: transparent; border: round #aaaaaa; }
    Button:focus { background: transparent; border: round #aaaaaa;
                   text-style: bold; }
    Button.-active { background: transparent; border: round #aaaaaa; }
    Button.pressed, Button.pressed:hover, Button.pressed:focus {
        background: #333333; border: round #aaaaaa; }
    #actions { margin-top: 1; height: auto; }
    #actions Button { margin-right: 2; }

    #status { background: transparent; padding: 0 1; height: 1;
              text-style: bold; }
    #hint { background: transparent; color: #777777; padding: 0 1;
            height: 1; dock: bottom; }
    #cmds { padding: 0 1; }
    .note { color: $text; padding: 0 1; }
    Collapsible { background: transparent; border: none; padding: 0; }
    CollapsibleTitle { color: $text; }
    CollapsibleTitle:hover { background: transparent; text-style: bold; }
    .cmd-log { height: 14; border: round #555555; background: transparent;
               margin: 0 1 1 2; }

    /* Dymki (notify): monochromatycznie jak dialogi i bez sztywnej
       szerokości 60 – przy małym oknie dymek się dopasowuje zamiast
       łamać tekst w wąskiej kolumnie. */
    Toast { width: auto; min-width: 24; max-width: 90%; padding: 0 1;
            background: $surface; border: round #aaaaaa; }
    Toast.-information, Toast.-warning, Toast.-error {
        border: round #aaaaaa; }
    Toast .toast--title { color: $text; text-style: bold; }

    ModalScreen { align: center middle; }
    .dialog { background: $surface; border: round #aaaaaa;
              padding: 1 2; width: 90; max-width: 100%; height: auto; }
    .dialog-text { margin-bottom: 1; }
    .dialog-buttons { margin-top: 1; height: auto; }
    .dialog-buttons Button { margin-right: 2; }
    .results DataTable { height: 18; background: transparent; }
    #current-row { height: auto; }
    #current-row #current { width: 32; }
    #current-row #unit { width: 12; margin-left: 2; }
    """

    def __init__(self):
        super().__init__()
        self.manifest = core.load_manifest()
        self.defaults = self.manifest.get("defaults", {})
        self.boards = self.manifest.get("boards", {})
        self.scenarios = self.manifest.get("scenarios", {})
        if not self.boards or not self.scenarios:
            core.die("manifest musi zawierać sekcje [boards.*] i [scenarios.*]")

    def compose(self):
        default_prof = self.defaults.get("profile")
        if default_prof not in self.boards:
            default_prof = next(iter(self.boards))
        # VerticalScroll: przy małym oknie menu się przewija zamiast ucinać.
        with VerticalScroll(id="setup"):
            yield Static(LOGO, id="logo")
            yield Label("Płytka", classes="h")
            yield Select(((b["board"], n) for n, b in self.boards.items()),
                         value=default_prof, allow_blank=False, id="profile")
            yield Label("Scenariusze", classes="h")
            with Vertical(id="scenarios"):
                # Wybór i opis to OSOBNE cele kliknięcia: checkbox z pełną
                # nazwą zaznacza scenariusz, a strzałka za nazwą rozwija
                # opis (pełną szerokością, z małym wcięciem). Bez wartości
                # oczekiwanych – te pokazuje dopiero instrukcja pomiaru.
                for n, s in self.scenarios.items():
                    yield self._scenario_row(n, s)
            yield Label("Egzemplarz płytki (trafia do dziennika CSV)",
                        classes="h")
            yield Input(placeholder="np. BTZ #2", id="sample")
            with Horizontal(id="actions"):
                yield Button("Start", id="start")
                yield Button("Zaznacz wszystkie", id="select_all")
                yield Button("Dodaj firmware", id="add_fw")
                yield Button("Wyniki", id="results")
                yield Button("Wyjście", id="quit")

    def _scenario_row(self, n, s, value=False):
        """Wiersz listy scenariuszy – wspólny dla compose() i dodawania
        scenariusza w locie ('Dodaj firmware')."""
        body = s.get("description", "")
        if s.get("note"):
            body += f"\nUwaga: {s['note']}"
        return Vertical(
            Horizontal(
                Checkbox(_label(n, s), value=value, classes="scen-check",
                         id=f"check_{n}"),
                DescArrow(f"desc_{n}", id=f"arrow_{n}"),
                classes="scenario-head"),
            Static(body, classes="scen-desc", id=f"desc_{n}"),
            classes="scenario-row")

    def on_button_pressed(self, event):
        if event.button.id == "quit":
            self.exit()
        elif event.button.id == "select_all":
            for box in self.query(".scen-check"):
                box.value = True
        elif event.button.id == "add_fw":
            self.push_screen(AddScreen(), callback=self._scenario_added)
        elif event.button.id == "results":
            self.push_screen(ResultsScreen())
        elif event.button.id == "start":
            self._start()

    def _scenario_added(self, result):
        """Po 'Dodaj firmware': nowy scenariusz od razu na liście
        (i zaznaczony), bez restartu aplikacji."""
        if not result:
            return
        name, entry = result
        self.scenarios[name] = entry
        self.query_one("#scenarios").mount(
            self._scenario_row(name, entry, value=True))
        self.notify(f"Dodano scenariusz '{name}' (zapisany "
                    "w scenarios.toml).")

    def on_checkbox_changed(self, event):
        """'Zaznacz wszystkie' wygląda na wciśnięty dokładnie wtedy, gdy
        zaznaczone są wszystkie scenariusze."""
        boxes = self.query(".scen-check")
        self.query_one("#select_all", Button).set_class(
            bool(boxes) and all(box.value for box in boxes), "pressed")

    def _start(self):
        names = [n for n in self.scenarios
                 if self.query_one(f"#check_{n}", Checkbox).value]
        sample = self.query_one("#sample", Input).value.strip()
        prof_name = self.query_one("#profile", Select).value
        if not names:
            self.notify("Zaznacz co najmniej jeden scenariusz.",
                        severity="error")
            return
        if not sample:
            self.notify("Podaj egzemplarz płytki (np. 'BTZ #2').",
                        severity="error")
            self.query_one("#sample", Input).focus()
            return
        self.push_screen(RunScreen(prof_name, self.boards[prof_name],
                                   names, sample))


if __name__ == "__main__":
    PowerTestApp().run()
