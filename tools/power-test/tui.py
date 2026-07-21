# ============================================================
#  board-power-test – interfejs okienkowy (TUI, Textual)
# ============================================================
# Uruchamiany przez power_test.py, gdy nie podano argumentów i biblioteka
# `textual` jest dostępna (launcher instaluje ją w .venv repo). Cała
# logika (manifest, komendy west, dziennik CSV) jest w power_test.py –
# ten plik to wyłącznie warstwa prezentacji: klikalne listy, pola
# tekstowe, dialogi i podgląd logu builda na żywo.

import asyncio
import csv
import shlex
import shutil

from textual import work
from textual.app import App
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label,
                             RichLog, Select, SelectionList, Static)
from textual.widgets.selection_list import Selection

import power_test as core


class ConfirmScreen(ModalScreen[bool]):
    """Dialog z pytaniem; `no=None` daje pojedynczy przycisk (twardy krok)."""

    def __init__(self, text, yes="OK", no="Anuluj"):
        super().__init__()
        self.text, self.yes, self.no = text, yes, no

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(self.text, classes="dialog-text")
            with Horizontal(classes="dialog-buttons"):
                yield Button(self.yes, variant="success", id="yes")
                if self.no is not None:
                    yield Button(self.no, variant="default", id="no")

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "yes")


class MeasureScreen(ModalScreen):
    """Instrukcja pomiaru + pola: średni prąd i uwagi. Zwraca
    (prąd, uwagi) albo None przy pominięciu."""

    def __init__(self, scen_name, scen, voltage, settle_s):
        super().__init__()
        self.scen_name, self.scen = scen_name, scen
        self.voltage, self.settle_s = voltage, settle_s

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(f"[b]POMIAR: {self.scen_name}[/b] – "
                         f"{self.scen.get('description', '')}")
            yield Static(core.measure_instructions(self.scen, self.voltage,
                                                   self.settle_s),
                         classes="dialog-text")
            yield Label("Średni prąd [µA]:")
            yield Input(placeholder="np. 0.95", id="current")
            yield Label("Uwagi (opcjonalnie):")
            yield Input(placeholder="np. 'przed poprawką HW'", id="notes")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Zapisz", variant="success", id="save")
                yield Button("Pomiń (bez zapisu)", variant="warning", id="skip")

    def on_mount(self):
        self.query_one("#current", Input).focus()

    def on_input_submitted(self, event):
        self._save()

    def on_button_pressed(self, event):
        if event.button.id == "skip":
            self.dismiss(None)
        else:
            self._save()

    def _save(self):
        raw = self.query_one("#current", Input).value.strip().replace(",", ".")
        try:
            current = float(raw)
        except ValueError:
            self.app.notify("Podaj liczbę w µA, np. 0.95 albo 7,3.",
                            severity="error")
            self.query_one("#current", Input).focus()
            return
        self.dismiss((current, self.query_one("#notes", Input).value.strip()))


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
    """Przebieg: FAZA 1 buduje wszystkie obrazy (log na żywo),
    FAZA 2 – flash + pomiar scenariusz po scenariuszu."""

    BINDINGS = [("escape", "app.pop_screen", "Przerwij i wróć")]

    def __init__(self, prof_name, profile, names, sample):
        super().__init__()
        self.prof_name, self.profile = prof_name, profile
        self.names, self.sample = names, sample

    def compose(self):
        yield Header()
        yield Static("", id="status")
        yield RichLog(id="log", highlight=False, markup=False, wrap=True)
        yield Footer()

    def on_mount(self):
        self.flow()

    async def run_west(self, cmd, cwd):
        log = self.query_one("#log", RichLog)
        log.write(f"$ {shlex.join(cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            log.write(line.decode(errors="replace").rstrip())
        if await proc.wait() != 0:
            raise RuntimeError(f"'{cmd[0]} {cmd[1]}' zakończył się błędem "
                               "(szczegóły w logu wyżej)")

    @work
    async def flow(self):
        log = self.query_one("#log", RichLog)
        status = self.query_one("#status", Static)
        scenarios = self.app.scenarios
        defaults = self.app.defaults
        total = len(self.names)
        try:
            if shutil.which("west") is None:
                raise RuntimeError(
                    "brak 'west' w PATH – uruchom przez `board-power-test` "
                    "(launcher startuje środowisko NCS) albo w terminalu "
                    "nRF Connect")
            workspace = await asyncio.to_thread(core.find_west_workspace)
            if workspace != core.ROOT:
                log.write(f"Workspace NCS: {workspace} (build out-of-tree)")

            # --- FAZA 1: wszystkie buildy z góry ---
            built = {}
            for i, name in enumerate(self.names, 1):
                status.update(f"FAZA 1/2 – buduję obraz {i}/{total}: [b]{name}[/b]")
                cmd, build_dir = core.make_build_cmd(
                    name, scenarios[name], self.prof_name, self.profile)
                await self.run_west(cmd, workspace)
                built[name] = build_dir
            log.write(f"--- zbudowano {total} obraz(ów) ---")

            # --- FAZA 2: flash + pomiar ---
            saved = []
            for i, name in enumerate(self.names, 1):
                scen = scenarios[name]
                voltage = str(scen.get("voltage",
                                       defaults.get("voltage", "3.0")))
                settle_s = scen.get("settle_s", defaults.get("settle_s", 5))
                status.update(f"FAZA 2/2 – scenariusz {i}/{total}: [b]{name}[/b]")

                ok = await self.app.push_screen_wait(ConfirmScreen(
                    f"[b]{name}[/b] – {scen.get('description', '')}\n\n"
                    "Programator podłączony i płytka ZASILONA\n"
                    "(np. VOUT z PPK2)?",
                    yes="Wgraj (flash --erase)", no="Pomiń scenariusz"))
                if not ok:
                    log.write(f"Pominięto {name}.")
                    continue
                await self.run_west(core.make_flash_cmd(built[name],
                                                        self.profile),
                                    workspace)

                # Twarde potwierdzenie SWD – jedyny przycisk, bez obejścia.
                await self.app.push_screen_wait(ConfirmScreen(
                    "[b]ODŁĄCZ przewód SWD/J-Link![/b]\n\n"
                    "Podłączony debugger dodaje własny prąd\n"
                    "i unieważnia pomiar minimum.",
                    yes="SWD ODŁĄCZONY – przejdź do pomiaru", no=None))

                result = await self.app.push_screen_wait(
                    MeasureScreen(name, scen, voltage, settle_s))
                if result is None:
                    log.write(f"Pominięto zapis scenariusza {name}.")
                    continue
                current, notes = result
                core.append_row(core.make_row(name, scen, self.profile,
                                              self.sample, voltage, current,
                                              notes), verbose=False)
                saved.append(f"{name}: {current} µA")
                log.write(f"Zapisano: {name} = {current} µA")

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
            status.update(f"[red]BŁĄD:[/red] {msg}")
            log.write(f"BŁĄD: {msg}")
            log.write("(Esc = powrót do ustawień)")


class PowerTestApp(App):
    TITLE = "board-power-test"
    SUB_TITLE = "pomiar poboru prądu płytek (PPK2)"
    BINDINGS = [("ctrl+q", "quit", "Wyjście")]
    CSS = """
    #setup { padding: 1 2; }
    .h { margin-top: 1; text-style: bold; color: $accent; }
    #profile, #sample { width: 70; }
    #scenarios { border: round $accent; max-height: 12; width: 70; }
    #actions { margin-top: 1; height: auto; }
    #actions Button { margin-right: 2; }

    #status { padding: 0 1; background: $boost; height: 1; }
    #log { border: round $primary; }

    ModalScreen { align: center middle; }
    .dialog { background: $surface; border: thick $accent;
              padding: 1 2; width: 90; max-width: 100%; height: auto; }
    .dialog-text { margin-bottom: 1; }
    .dialog-buttons { margin-top: 1; height: auto; }
    .dialog-buttons Button { margin-right: 2; }
    .results DataTable { height: 18; }
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
        yield Header()
        with Vertical(id="setup"):
            yield Label("Profil płytki", classes="h")
            yield Select(((f"{n} – {b['board']}", n)
                          for n, b in self.boards.items()),
                         value=default_prof, allow_blank=False, id="profile")
            yield Label("Scenariusze (klik / spacja = zaznacz)", classes="h")
            yield SelectionList(*(Selection(
                f"{n} – {s.get('description', '')}", n)
                for n, s in self.scenarios.items()), id="scenarios")
            yield Label("Egzemplarz płytki (trafia do dziennika CSV)",
                        classes="h")
            yield Input(placeholder="np. BTZ #2", id="sample")
            with Horizontal(id="actions"):
                yield Button("▶ Start", variant="success", id="start")
                yield Button("Zaznacz wszystkie", id="select_all")
                yield Button("Wyniki", id="results")
                yield Button("Wyjście", variant="error", id="quit")
        yield Footer()

    def on_button_pressed(self, event):
        if event.button.id == "quit":
            self.exit()
        elif event.button.id == "select_all":
            self.query_one("#scenarios", SelectionList).select_all()
        elif event.button.id == "results":
            self.push_screen(ResultsScreen())
        elif event.button.id == "start":
            self._start()

    def _start(self):
        selected = self.query_one("#scenarios", SelectionList).selected
        names = [n for n in self.scenarios if n in selected]  # kolejność manifestu
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
