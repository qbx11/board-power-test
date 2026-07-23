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
import os
import shlex
import shutil
import subprocess
import threading

from pathlib import Path

from rich.markup import escape

from textual import work
from textual.app import App
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (Button, Checkbox, Collapsible, DataTable,
                             DirectoryTree, Input, Label, Log, Select, Static)

import power_test as core

# Logo GoodByte – nagłówek ekranu głównego. Czcionka blokowa (figlet
# „ANSI Shadow”), monochromatyczna jak reszta interfejsu; pod spodem
# podpis narzędzia. Statyczny tekst, bez zależności runtime.
LOGO = """\
╭──────────────────────────────────────────────────────────────────────╮

  ██████╗  ██████╗  ██████╗ ██████╗ ██████╗ ██╗   ██╗████████╗███████╗
 ██╔════╝ ██╔═══██╗██╔═══██╗██╔══██╗██╔══██╗╚██╗ ██╔╝╚══██╔══╝██╔════╝
 ██║  ███╗██║   ██║██║   ██║██║  ██║██████╔╝ ╚████╔╝    ██║   █████╗
 ██║   ██║██║   ██║██║   ██║██║  ██║██╔══██╗  ╚██╔╝     ██║   ██╔══╝
 ╚██████╔╝╚██████╔╝╚██████╔╝██████╔╝██████╔╝   ██║      ██║   ███████╗
  ╚═════╝  ╚═════╝  ╚═════╝ ╚═════╝ ╚═════╝    ╚═╝      ╚═╝   ╚══════╝

[#888888]                    b o a r d   p o w e r   t e s t[/]
╰──────────────────────────────────────────────────────────────────────╯"""


def _stream(cmd, cwd, on_line, handle=None):
    """Uruchom proces i strumieniuj linie wyjścia (wołane w wątku).
    env=child_env(): procesy west dostają z powrotem PYTHONHOME/PYTHONPATH
    toolchaina, które naszemu pythonowi zdjęto przy starcie.
    `handle['proc']` pozwala wołającemu ubić proces (Esc w trakcie);
    po zdjęciu UI wyjście jest drenowane bez raportowania, żeby nie
    zostawić wiszącego potoku ani wyjątku w wątku."""
    with subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace",
                          env=core.child_env()) as proc:
        if handle is not None:
            handle["proc"] = proc
        ui_alive = True
        for line in proc.stdout:
            if ui_alive:
                try:
                    on_line(line.rstrip())
                except Exception:
                    ui_alive = False   # widok zdjęty (Esc) – drenuj cicho
        return proc.wait()


def _display_path(path):
    """Ścieżka do pokazania użytkownikowi: względna do repo, jeśli
    firmware leży w nim albo obok (jak w manifeście), inaczej z ~."""
    try:
        rel = os.path.relpath(path, core.ROOT)
    except ValueError:
        rel = None
    if rel is not None and rel.count("..") <= 3:
        return rel
    s, home = str(path), str(Path.home())
    return "~" + s[len(home):] if s.startswith(home) else s


def _label(name, item):
    """Nazwa do wyświetlenia: `label` z manifestu, inaczej klucz."""
    return item.get("label", name)


class Check(Checkbox):
    """Checkbox z ptaszkiem (✓) zamiast domyślnego X w stanie zaznaczonym."""

    BUTTON_INNER = "✓"


class DescArrow(Static):
    """Strzałka w linii tytułu scenariusza – rozwija/zwija opis pod
    spodem (osobny Static pełnej szerokości, więc tekst opisu ma stałe,
    małe wcięcie zamiast zaczynać się dopiero za nazwą)."""

    def __init__(self, desc_id, **kwargs):
        super().__init__("▶", classes="scen-arrow scen-icon", **kwargs)
        self.desc_id = desc_id

    def on_click(self, event):
        event.stop()
        desc = self.screen.query_one(f"#{self.desc_id}", Static)
        shown = not desc.has_class("shown")
        desc.set_class(shown, "shown")
        self.update("▼" if shown else "▶")


class DeleteCross(Static):
    """✕ w wierszu scenariusza – usuwa wpis z manifestu (z osobnym
    potwierdzeniem; zebrane pomiary w CSV zostają)."""

    def __init__(self, scen_name, **kwargs):
        super().__init__("✕", classes="scen-del scen-icon", **kwargs)
        self.scen_name = scen_name

    def on_click(self, event):
        event.stop()
        self.app.confirm_remove(self.scen_name)


class ModeLabel(Static):
    """Podpis przy przełączniku trybu ('Pomiar ręczny' / 'Tryb autonomiczny')
    – klikalny, żeby nie trzeba było celować w mały suwak."""

    def __init__(self, text, mode, **kwargs):
        super().__init__(text, **kwargs)
        self.mode = mode

    def on_click(self, event):
        event.stop()
        self.app._set_mode(self.mode)


class CardDelete(Static):
    """✕ w nagłówku karty 'Pomiar N' – usuwa kartę z kreatora."""

    def __init__(self, uid, **kwargs):
        super().__init__("✕", classes="card-del", **kwargs)
        self.uid = uid

    def on_click(self, event):
        event.stop()
        self.app.remove_measurement(self.uid)


class CardTitle(Static):
    """Nagłówek karty 'Pomiar N' – klik zwija/rozwija kartę, żeby po
    dodaniu wielu pomiarów wciąż było je widać jako listę."""

    def on_click(self, event):
        event.stop()
        card = self.parent
        while card is not None and not isinstance(card, MeasurementCard):
            card = card.parent
        if card is not None:
            card.toggle_collapsed()


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


class FirmwareTree(DirectoryTree):
    """Drzewo plików eksploratora: tylko katalogi i pliki .hex (reszta
    to szum przy wskazywaniu firmware'u), bez plików ukrytych.
    Ikony znakowe zamiast emoji – spójnie z monochromatycznym UI."""

    ICON_NODE = "▸ "
    ICON_NODE_EXPANDED = "▾ "
    ICON_FILE = "· "

    def filter_paths(self, paths):
        return [p for p in paths
                if not p.name.startswith(".")
                and (p.is_dir() or p.suffix.lower() == ".hex")]


class BrowseScreen(ModalScreen):
    """Eksplorator plików do 'Dodaj kod': wskaż katalog aplikacji albo
    plik .hex zamiast wpisywać ścieżkę ręcznie (wpisywanie dalej działa).
    Klik/Enter na pliku .hex wybiera go od razu; katalog wybiera przycisk
    'Wybierz ten katalog'. Zwraca Path albo None."""

    def __init__(self, start=None):
        super().__init__()
        # Start: katalog nad repo – tam zwykle leżą projekty zespołu.
        self.current = Path(start or core.ROOT.parent).resolve()

    def compose(self):
        with Vertical(classes="dialog browse"):
            yield Static("[b]Wskaż firmware[/b]\n"
                         "Katalog aplikacji Zephyr/NCS albo plik .hex "
                         "(klik w plik wybiera go od razu).",
                         classes="dialog-text")
            yield Static("", id="browse-root")
            yield FirmwareTree(self.current, id="browse-tree")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Wybierz ten katalog", id="choose")
                yield Button("W górę", id="up")
                yield Button("Anuluj", id="cancel")

    def on_mount(self):
        tree = self.query_one("#browse-tree", FirmwareTree)
        # Klik w katalog ma go tylko ROZWIJAĆ (obsługa niżej) – domyślne
        # przełączanie zwijało duże poddrzewa, a po skurczeniu zawartości
        # widok był przycinany do samej góry listy.
        tree.auto_expand = False
        tree.focus()
        self._show_root()

    def _show_root(self):
        self.query_one("#browse-root", Static).update(
            f"[#888888]{_display_path(self.current)}[/]")

    def on_directory_tree_file_selected(self, event):
        event.stop()
        self.dismiss(event.path)

    def on_directory_tree_directory_selected(self, event):
        event.stop()
        if not event.node.is_expanded:
            event.node.expand()      # zwijanie: strzałka przy nazwie

    def on_button_pressed(self, event):
        tree = self.query_one("#browse-tree", FirmwareTree)
        if event.button.id == "choose":
            node = tree.cursor_node or tree.root
            self.dismiss(Path(node.data.path))
        elif event.button.id == "up":
            parent = self.current.parent
            if parent != self.current:
                self.current = parent
                tree.path = parent      # reaktywne: przeładowuje drzewo
                self._show_root()
        else:
            self.dismiss(None)


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
            with Horizontal(id="path-row"):
                yield Input(placeholder="np. ../moj-projekt/app  albo  "
                                        "../moj-projekt/build/zephyr/"
                                        "zephyr.hex",
                            id="path")
                yield Button("Przeglądaj…", id="browse")
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
        elif event.button.id == "browse":
            self.app.push_screen(BrowseScreen(), callback=self._browsed)
        else:
            self.dismiss(None)

    def _browsed(self, path):
        """Ścieżka z eksploratora -> pole tekstowe (można ją jeszcze
        poprawić ręcznie przed dodaniem). Skracana jak w manifeście:
        względna do repo albo z ~, żeby mieściła się w polu."""
        if path:
            field = self.query_one("#path", Input)
            field.value = _display_path(path)
            field.focus()

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

    def __init__(self, prof_name, profile, names, sample, pristine=False,
                 reset=True, swd_reminder=True):
        super().__init__()
        self.prof_name, self.profile = prof_name, profile
        self.names, self.sample = names, sample
        self.pristine = pristine
        self.reset = reset
        self.swd_reminder = swd_reminder
        # Pełny zapis przebiegu (komendy + ich wyjście) do skopiowania
        # klawiszem C – przydatne zwłaszcza, gdy build padnie.
        self.transcript = []

    def compose(self):
        yield Static("", id="status")
        # Nagłówek okna logów: przycisk kopiowania tuż nad nimi, po prawej.
        with Horizontal(id="cmds-head"):
            yield Static("Logi budowania", id="cmds-title")
            yield Button("Kopiuj log", id="copy_log")
        yield VerticalScroll(id="cmds")
        yield Static("Esc — przerwij i wróć   ·   zaznacz tekst i skopiuj "
                     "(macOS: ⌥+przeciągnij, potem ⌘C)", id="hint")

    def on_button_pressed(self, event):
        if event.button.id == "copy_log":
            self._copy_log()

    def _copy_log(self):
        """Kopiuj pełny zapis przebiegu do schowka. Najpierw lokalne
        narzędzie (pbcopy itd. – pewne), a niezależnie OSC 52 dla terminali,
        które je wspierają. Gdy schowka brak – zapis do pliku i ścieżka."""
        text = "\n".join(self.transcript).strip()
        if not text:
            self.app.notify("Nie ma jeszcze logów do skopiowania.",
                            severity="warning")
            return
        self.app.copy_to_clipboard(text)          # OSC 52 (gdzie działa)
        tool = core.copy_to_clipboard(text)       # systemowy schowek (pewne)
        if tool:
            self.app.notify(f"Skopiowano log budowania do schowka ({tool}).")
        else:
            path = core.save_text_log(text)
            self.app.notify(f"Brak narzędzia schowka — zapisano log do {path}.")

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
        self.transcript += ["", f"# {title}", f"$ {shlex.join(cmd)}"]

        frame = {"i": 0}

        def tick():
            frame["i"] = (frame["i"] + 1) % len(self.SPINNER)
            section.title = f"{self.SPINNER[frame['i']]} {title}"

        spinner = self.set_interval(1 / 8, tick)
        handle = {}
        lines = []

        def on_line(line):
            lines.append(line)
            self.transcript.append(line)
            out.write_line(line)

        try:
            rc = await asyncio.to_thread(
                _stream, cmd, cwd,
                lambda line: self.app.call_from_thread(on_line, line),
                handle)
        except asyncio.CancelledError:
            # Esc w trakcie: ubij proces west/nrfutil, żeby nie wisiał
            # w tle i nie sypał wyjątkami po zamknięciu aplikacji.
            proc = handle.get("proc")
            if proc is not None and proc.poll() is None:
                proc.terminate()
            raise
        finally:
            spinner.stop()
            # Narzędzia flashujące (J-Link/nrfutil) potrafią pisać wprost
            # do /dev/tty i zresetować tryb myszy – odnów go, zanim pojawi
            # się kolejny klikalny dialog (SWD/pomiar).
            self.app._reassert_mouse()
        if rc != 0:
            section.title = f"✗ {title} — kod {rc}"
            section.collapsed = False
            raise RuntimeError(f"'{title}' zakończone błędem (kod {rc})")
        section.title = f"✓ {title}"

        # Po buildzie: podsumowanie zajętości pamięci jako tabelka Markdown
        # (od razu do skopiowania). Przy flashu parser zwraca None.
        report = core.parse_memory_report(lines)
        if report is not None:
            box = Static(report, classes="mem-report", markup=False)
            box.border_title = "pamięć (Markdown — skopiuj)"
            await self.query_one("#cmds").mount(box)
            self.query_one("#cmds").scroll_end(animate=False)

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
            # Gotowe buildy (ten sam obraz, ta sama komenda) są pomijane,
            # chyba że zaznaczono 'Wymuś pełny rebuild'.
            built, plans = {}, {}
            for name in to_build:
                plans[name] = core.make_build_cmd(
                    name, scenarios[name], self.prof_name, self.profile,
                    defaults.get("profile"),
                    pristine="always" if self.pristine else "auto")
            will_build = [n for n in to_build
                          if self.pristine
                          or not core.build_up_to_date(plans[n][1],
                                                       plans[n][0])]
            build_no = 0
            for name in self.names:
                scen = scenarios[name]
                if "hex" in scen:
                    built[name] = None
                    self.note(f"{name}: gotowy hex ({scen['hex']}) – "
                              "bez budowania.")
                    continue
                cmd, build_dir = plans[name]
                if name not in will_build:
                    built[name] = build_dir
                    self.note(f"{name}: gotowy build ({build_dir}/) – "
                              "pomijam.")
                    continue
                build_no += 1
                status.update(f"FAZA 1/2 · build {build_no}/"
                              f"{len(will_build)} · {name}")
                await self.run_west(cmd, workspace, f"build {name}")
                core.record_build(build_dir, cmd)
                built[name] = build_dir
            if will_build:
                self.note(f"Zbudowano {len(will_build)} obraz(ów).")
            elif to_build:
                self.note("Wszystkie obrazy gotowe – nic do budowania.")
            else:
                self.note("Nic do budowania (same gotowe pliki hex).")

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
                                scen, built[name], self.profile,
                                reset=self.reset),
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

                    # Twarde potwierdzenie SWD – jedyny przycisk. Można je
                    # wyłączyć (nie każdy mierzy z podłączonym programatorem).
                    if self.swd_reminder:
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
            if self.app.screen is self:   # Esc mógł już zdjąć ekran
                self.app.pop_screen()
        except (SystemExit, RuntimeError) as e:
            msg = str(e) or "przerwano"
            if not msg.startswith("BŁĄD"):
                msg = f"BŁĄD: {msg}"
            status.update(msg)
            self.note("(Esc = powrót do ustawień)")


class MeasurementCard(Vertical):
    """Jedna karta 'Pomiar N' w kreatorze trybu autonomicznego: scenariusz
    + czas, a start-po-czasie / RTT / napięcie / zapis w zwijanych
    ustawieniach zaawansowanych (domyślnie schowane i wyłączone).
    Czyta/ustawia własną konfigurację, nie dotyka innych kart."""

    def __init__(self, uid, scenarios, number, config=None, collapsed=False):
        super().__init__(classes="measurement-card", id=f"card_{uid}")
        self.uid = uid
        self.scenarios = scenarios
        self.number = number
        self._config = config or {}
        self.collapsed = collapsed

    def compose(self):
        c = self._config
        opts = [(_label(n, s), n) for n, s in self.scenarios.items()]
        with Horizontal(classes="card-head"):
            yield CardTitle(f"▾ Pomiar {self.number}", classes="card-title")
            yield CardDelete(self.uid)
        # Ciało karty (chowane przy zwinięciu). Selecty MUSZĄ powstać jako
        # widoczne – Select zamontowany od razu jako display:none nie tworzy
        # overlaya; ukrycie następuje dopiero w on_mount (post-mount).
        with Vertical(classes="card-body"):
            yield Label("Scenariusz:")
            # Domyślnie NIC nie zaznaczone (blank). Wartość podajemy tylko gdy
            # config faktycznie ma znany scenariusz – blankiem steruje sam
            # widget (sentinel zależny od wersji Textual).
            scen0 = c.get("scenario")
            sel_kw = dict(allow_blank=True, prompt="— wybierz scenariusz —",
                          classes="card-scenario")
            if scen0 in self.scenarios:
                sel_kw["value"] = scen0
            yield Select(opts, **sel_kw)
            yield Label("Czas pomiaru (np. 30s / 20m / 8h):")
            yield Input(value=c.get("duration", ""), placeholder="np. 8h",
                        classes="card-duration")
            with Collapsible(title="Ustawienia zaawansowane", collapsed=True,
                             classes="card-adv"):
                # Start po czasie – opcjonalny; pole pojawia się po włączeniu.
                yield Check("Start pomiaru po czasie od wgrania (np. 20s)",
                            value=c.get("delay_on", False),
                            classes="card-delay-on")
                yield Input(value=c.get("delay_s", "20s"),
                            placeholder="np. 30s", classes="card-delay-s")
                # Konsola RTT – opcjonalna; pola pojawiają się po włączeniu.
                yield Check("Konsola RTT (start po logu / etykiety)",
                            value=c.get("rtt_on", False),
                            classes="card-rtt-on")
                with Vertical(classes="card-rtt-box"):
                    yield Select([("start po logu", "trigger"),
                                  ("etykiety (continuous)", "continuous")],
                                 value=c.get("rtt_mode", "trigger"),
                                 allow_blank=False, classes="card-rtt")
                    yield Input(value=c.get("pattern", ""),
                                placeholder="wzorzec logu RTT",
                                classes="card-pattern")
                with Horizontal(classes="card-row card-vs-row"):
                    with Vertical(classes="card-col"):
                        yield Label("Napięcie (V, 2.0–3.3):")
                        yield Input(value=c.get("voltage", "3.0"),
                                    classes="card-voltage")
                    with Vertical(classes="card-col"):
                        yield Label("Zapis danych:")
                        yield Select([("downsampled", "downsampled"),
                                      ("raw (duże!)", "raw"),
                                      ("both", "both")],
                                     value=c.get("storage", "downsampled"),
                                     allow_blank=False, classes="card-storage")
                yield Label("Próbki na sekundę:")
                yield Select([("100000 (max)", 100000), ("10000", 10000),
                              ("1000", 1000), ("100", 100), ("10", 10),
                              ("1", 1)],
                             value=c.get("sample_rate", 100000),
                             allow_blank=False, classes="card-rate")
            with Horizontal(classes="card-apply-row"):
                yield Button("Zastosuj do wszystkich", classes="card-apply")
                yield Button("Zastosuj do następnych",
                             classes="card-apply-next")

    def on_mount(self):
        # Post-mount: dopiero teraz ukrywamy zaawansowane pola i (ewentualnie)
        # zwijamy kartę – overlaye Selectów już istnieją, więc bezpiecznie.
        self._sync_advanced()
        self.query_one(".card-body").display = not self.collapsed
        self._refresh_title()

    def on_checkbox_changed(self, event):
        # Checkboxy karty (start-po-czasie / RTT) sterują widocznością swoich
        # pól – nie puszczamy zdarzenia wyżej (App liczy tylko scen-check).
        if event.control.has_class("card-delay-on") or \
           event.control.has_class("card-rtt-on"):
            self._sync_advanced()
            event.stop()

    def _sync_advanced(self):
        self.query_one(".card-delay-s").display = \
            self.query_one(".card-delay-on", Checkbox).value
        self.query_one(".card-rtt-box").display = \
            self.query_one(".card-rtt-on", Checkbox).value

    def toggle_collapsed(self):
        self.set_collapsed(not self.collapsed)

    def set_collapsed(self, collapsed):
        self.collapsed = collapsed
        self.query_one(".card-body").display = not collapsed
        self._refresh_title()

    def set_number(self, number):
        self.number = number
        self._refresh_title()

    def _refresh_title(self):
        title = self.query_one(".card-title", CardTitle)
        if not self.collapsed:
            title.update(f"▾ Pomiar {self.number}")
            return
        scen = self._scenario()
        if not scen:
            title.update(f"▸ Pomiar {self.number} — (wybierz scenariusz)")
            return
        label = _label(scen, self.scenarios.get(scen, {}))
        # Numer dokładamy tylko, gdy nazwa scenariusza się powtarza.
        dupes = sum(1 for card in self.app.query(MeasurementCard)
                    if card._scenario() == scen)
        suffix = f" · Pomiar {self.number}" if dupes > 1 else ""
        title.update(f"▸ {label}{suffix}")

    def _scenario(self):
        """Wybrany scenariusz albo '' gdy blank (sentinel zależny od wersji
        Textual – rozpoznajemy blank po tym, że nie jest znaną nazwą)."""
        scen = self.query_one(".card-scenario", Select).value
        return scen if scen in self.scenarios else ""

    def get_config(self):
        return {
            "scenario": self._scenario(),
            "duration": self.query_one(".card-duration", Input).value.strip(),
            "delay_on": self.query_one(".card-delay-on", Checkbox).value,
            "delay_s": self.query_one(".card-delay-s", Input).value.strip(),
            "rtt_on": self.query_one(".card-rtt-on", Checkbox).value,
            "rtt_mode": self.query_one(".card-rtt", Select).value,
            "pattern": self.query_one(".card-pattern", Input).value.strip(),
            "voltage": self.query_one(".card-voltage", Input).value.strip(),
            "storage": self.query_one(".card-storage", Select).value,
            "sample_rate": self.query_one(".card-rate", Select).value,
        }

    def apply_shared(self, cfg):
        """Ustaw wszystko OPRÓCZ scenariusza (dla 'Zastosuj do wszystkich')."""
        self.query_one(".card-duration", Input).value = cfg["duration"]
        self.query_one(".card-delay-on", Checkbox).value = cfg["delay_on"]
        self.query_one(".card-delay-s", Input).value = cfg["delay_s"]
        self.query_one(".card-rtt-on", Checkbox).value = cfg["rtt_on"]
        self.query_one(".card-rtt", Select).value = cfg["rtt_mode"]
        self.query_one(".card-pattern", Input).value = cfg["pattern"]
        self.query_one(".card-voltage", Input).value = cfg["voltage"]
        self.query_one(".card-storage", Select).value = cfg["storage"]
        self.query_one(".card-rate", Select).value = cfg["sample_rate"]
        self._sync_advanced()


class Ppk2ConnectScreen(ModalScreen):
    """Ekran połączenia z PPK2 przed startem: wykrycie portu (bez pomiaru)
    i info o napięciu. Zwraca True (Start) albo None (Anuluj). Sam pomiar
    otwiera PPK2 ponownie – tu tylko potwierdzamy, że urządzenie jest."""

    def __init__(self, detect=None):
        super().__init__()
        # detect() -> port (str) albo wyjątek; podmieniane w testach.
        self._detect = detect
        self._connected = False

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static("[b]Połączenie z PPK2[/b]\n"
                         "Sprawdź, czy Power Profiler Kit II jest podłączony "
                         "po USB, potem uruchom pomiary.",
                         classes="dialog-text")
            yield Static("Napięcie: ustawiane per pomiar (domyślnie 3.0 V, "
                         "limit 2.0–3.3 V).", classes="dialog-text")
            yield Static("[#888888]PPK2: niesprawdzony[/]", id="ppk2-status")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Połącz / sprawdź", id="ppk2_detect")
                yield Button("Start pomiary", id="ppk2_start", disabled=True)
                yield Button("Anuluj", id="ppk2_cancel")

    def on_button_pressed(self, event):
        if event.button.id == "ppk2_cancel":
            self.dismiss(None)
        elif event.button.id == "ppk2_detect":
            self._do_detect()
        elif event.button.id == "ppk2_start" and self._connected:
            self.dismiss(True)

    def _do_detect(self):
        status = self.query_one("#ppk2-status", Static)
        try:
            if self._detect is not None:
                port = self._detect()
            else:
                from autorun.ppk2 import find_ppk2
                port = find_ppk2()
        except Exception as e:
            self._connected = False
            # Komunikat błędu może zawierać nawiasy [] – escapujemy, żeby nie
            # rozjechać znaczników Rich; i wyłączamy WŁAŚCIWY przycisk startu.
            status.update("[#cc6666]PPK2: nie znaleziono — "
                          f"{escape(str(e))}[/]")
            self.query_one("#ppk2_start", Button).disabled = True
            return
        self._connected = True
        status.update(f"PPK2: podłączony ({port})")
        self.query_one("#ppk2_start", Button).disabled = False


class AutoRunScreen(Screen):
    """Pulpit trybu autonomicznego: postęp kroków, prąd na żywo, logi
    build/flash, przycisk otwarcia wykresu. Silnik (autorun.engine)
    biegnie w wątku; zdarzenia wracają przez call_from_thread. Esc =
    przerwij (sesja jest domykana czysto)."""

    BINDINGS = [("escape", "cancel", "Przerwij")]

    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, plan, sample):
        super().__init__()
        self.plan = plan               # gotowy autorun.plan.Plan (z okna)
        self.sample = sample
        self.cancel = threading.Event()
        self.pause = threading.Event()  # Stop/Wznów pomiaru (pauza)
        self.run_dir = None
        self.live_session = None
        self._done = False
        self._n_steps = len(plan.steps)
        # Aktywna zwijana sekcja komendy (build/flash) + jej spinner.
        self._active_section = None
        self._active_log = None
        self._spinner = None
        self._spin_i = 0

    def compose(self):
        yield Static("", id="status")
        # Tabelka zakończonych pomiarów (na górze) – wypełnia się po każdym
        # kroku, a jego okna build/flash znikają.
        yield DataTable(id="results", zebra_stripes=False,
                        cursor_type="none")
        yield Static("Budowanie i wgrywanie", id="cmds-title")
        yield VerticalScroll(id="cmds")
        # Duże okno pomiaru POD logami build/flash.
        with Vertical(id="measure-panel"):
            yield Static("", id="measure-head")
            with Horizontal(id="measure-row"):
                yield Static("", id="measure-avg")
                yield Static("", id="measure-remain")
            yield Static("", id="measure-inst")
            yield Button("Stop", id="stop_measure")
        yield Static("Esc — przerwij (sesja zostaje zapisana)", id="hint")

    def on_mount(self):
        table = self.query_one("#results", DataTable)
        table.add_columns("#", "Scenariusz", "Średni", "Min", "Max", "Czas")
        table.display = False          # pokaże się z pierwszym wynikiem
        self.query_one("#measure-panel").display = False
        self.flow()

    def note(self, text):
        cmds = self.query_one("#cmds")
        cmds.mount(Static(text, classes="note"))
        cmds.scroll_end(animate=False)

    # --- zwijane sekcje komend (build/flash), jak w trybie ręcznym ---

    def _cmd_start(self, title):
        log = Log(classes="cmd-log")
        section = Collapsible(log, title=f"{self.SPINNER[0]} {title}",
                              collapsed=True)
        cmds = self.query_one("#cmds")
        cmds.mount(section)
        cmds.scroll_end(animate=False)
        self._active_section = section
        self._active_log = log
        self._active_title = title
        self._spin_i = 0
        self._spinner = self.set_interval(1 / 8, self._tick)

    def _tick(self):
        if self._active_section is None:
            return
        self._spin_i = (self._spin_i + 1) % len(self.SPINNER)
        self._active_section.title = \
            f"{self.SPINNER[self._spin_i]} {self._active_title}"

    def _cmd_line(self, text):
        if self._active_log is not None:
            self._active_log.write_line(text)
        else:
            self.note(f"[#888888]{text}[/]")

    def _cmd_end(self, rc, title):
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner = None
        if self._active_section is not None:
            if rc == 0:
                self._active_section.title = f"✓ {title}"
            else:
                self._active_section.title = f"✗ {title} — kod {rc}"
                self._active_section.collapsed = False
        self._active_section = None
        self._active_log = None

    def action_cancel(self):
        if self._done:
            self.app.pop_screen()
            return
        self.cancel.set()
        self.query_one("#status", Static).update(
            "Przerywam po bieżącym odczycie… (Esc jeszcze raz = powrót)")

    def on_button_pressed(self, event):
        if event.button.id == "stop_measure":
            self._toggle_pause()

    def _toggle_pause(self):
        """Stop = pauza pomiaru i czasu; drugi klik = wznów ten sam pomiar."""
        btn = self.query_one("#stop_measure", Button)
        if self.pause.is_set():
            self.pause.clear()
            btn.label = "Stop"
        else:
            self.pause.set()
            btn.label = "Wznów"

    @staticmethod
    def _fmt_uA(uA):
        """Prąd w czytelnej jednostce: nA / µA / mA / A."""
        if uA is None:
            return "—"
        a = abs(uA)
        if a < 1:
            return f"{uA * 1000:.1f} nA"
        if a < 1000:
            return f"{uA:.1f} µA"
        if a < 1e6:
            return f"{uA / 1000:.3f} mA"
        return f"{uA / 1e6:.3f} A"

    @staticmethod
    def _fmt_time(s):
        s = max(0, int(round(s)))
        h, r = divmod(s, 3600)
        m, sec = divmod(r, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"

    # --- most zdarzenia silnika -> UI (wołane z wątku) ---

    def _on_event(self, ev):
        status = self.query_one("#status", Static)
        if ev.kind == "plan_start":
            self.run_dir = ev.data.get("run_dir")
            self.note(f"[b]Plan {ev.text}[/b] – {ev.data.get('steps')} "
                      "krok(ów)")
        elif ev.kind == "phase":
            status.update(ev.text)
            self.note(f"[b]{ev.text}[/b]")
        elif ev.kind == "state":
            label = {"build": "budowanie", "power": "zasilanie",
                     "flash": "wgrywanie", "trigger": "czekam na trigger",
                     "measure": "POMIAR", "build_failed": "build padł"
                     }.get(ev.text, ev.text)
            detail = ev.data.get("detail", "")
            status.update(f"krok {ev.step} · {ev.name} · {label}"
                          + (f" ({detail})" if detail else ""))
            if ev.text == "measure":
                self._start_measure_panel(ev.step, ev.name)
        elif ev.kind == "session":
            # Wykres (osobne okno) chwilowo wyłączony – zajmiemy się później.
            self.live_session = ev.data.get("dir")
        elif ev.kind == "live":
            self._update_measure(ev)
        elif ev.kind == "paused":
            self._set_paused(ev, True)
        elif ev.kind == "resumed":
            self._set_paused(ev, False)
        elif ev.kind == "annotation":
            self.note(f"  ⟟ etykieta: {ev.text} @ {ev.data.get('t_s')} s")
        elif ev.kind == "cmd_start":
            self._cmd_start(ev.text)
        elif ev.kind == "cmd_end":
            self._cmd_end(ev.data.get("rc", 0), ev.data.get("title", ""))
        elif ev.kind == "line":
            self._cmd_line(ev.text)
        elif ev.kind == "note":
            self.note(ev.text)
        elif ev.kind == "step_done":
            self._finish_step(ev)
        elif ev.kind == "plan_done":
            self._finish(ev)

    # --- duże okno pomiaru + tabelka wyników ---

    def _start_measure_panel(self, step, name):
        panel = self.query_one("#measure-panel")
        panel.display = True
        self.pause.clear()
        self.query_one("#stop_measure", Button).label = "Stop"
        self.query_one("#measure-head", Static).update(
            f"[b]POMIAR[/b] · krok {step}/{self._n_steps} · {name}")
        self.query_one("#measure-avg", Static).update("[b]—[/b]")
        self.query_one("#measure-remain", Static).update("")
        self.query_one("#measure-inst", Static).update("")

    def _update_measure(self, ev):
        d = ev.data
        remain = max(0.0, (d.get("duration_s") or 0) - (d.get("elapsed_s") or 0))
        self.query_one("#measure-avg", Static).update(
            f"[b]{self._fmt_uA(d.get('avg_uA'))}[/b]")
        self.query_one("#measure-remain", Static).update(
            f"pozostało [b]{self._fmt_time(remain)}[/b]")
        self.query_one("#measure-inst", Static).update(
            f"[#888888]teraz {self._fmt_uA(d.get('inst_uA'))} · "
            f"próbek {d.get('samples', 0):,}[/]")

    def _set_paused(self, ev, paused):
        head = self.query_one("#measure-head", Static)
        btn = self.query_one("#stop_measure", Button)
        if paused:
            d = ev.data
            step, name = ev.step, ev.name
            head.update(f"[b]PAUZA[/b] · krok {step}/{self._n_steps} · {name}"
                        f"  (śr {self._fmt_uA(d.get('avg_uA'))})")
            btn.label = "Wznów"
        else:
            head.update(f"[b]POMIAR[/b] · krok {ev.step}/{self._n_steps} · "
                        f"{ev.name}")
            btn.label = "Stop"

    def _finish_step(self, ev):
        """Po pomiarze: usuń okna build/flash tego kroku i dopisz wynik do
        tabelki na górze."""
        d = ev.data
        self.query_one("#cmds").remove_children()
        table = self.query_one("#results", DataTable)
        table.display = True
        table.add_row(str(ev.step), ev.name,
                      self._fmt_uA(d.get("avg_uA")),
                      self._fmt_uA(d.get("min_uA")),
                      self._fmt_uA(d.get("max_uA")),
                      self._fmt_time(d.get("duration_s") or 0))
        self.query_one("#measure-panel").display = False

    def _finish(self, ev):
        self._done = True
        status = self.query_one("#status", Static)
        if ev.data.get("cancelled"):
            status.update("Przerwano. Sesje zapisane.")
        else:
            status.update("Zakończono plan.")
        self.query_one("#measure-panel").display = False
        if self.run_dir:
            rel = Path(self.run_dir)
            try:
                rel = rel.relative_to(core.ROOT)
            except ValueError:
                pass
            self.note(f"Sesje: {rel}/  ·  Esc = powrót")

    @work(thread=True)
    def flow(self):
        """Wątek roboczy: zbuduj runner i wykonaj plan. event_cb marshaluje
        każde zdarzenie na wątek UI (call_from_thread)."""
        from autorun.engine import AutoRunError, AutoRunner

        def emit(ev):
            self.app.call_from_thread(self._on_event, ev)

        try:
            manifest = core.load_manifest()
            runner = AutoRunner(self.plan, manifest, self.sample,
                                event_cb=emit, cancel=self.cancel,
                                pause=self.pause)
            runner.run()
        except AutoRunError as e:
            self.app.call_from_thread(self._fail, str(e))
        except Exception as e:                      # sprzęt, biblioteki
            self.app.call_from_thread(self._fail, f"{type(e).__name__}: {e}")

    def _fail(self, msg):
        self._done = True
        self.query_one("#status", Static).update(f"BŁĄD: {msg}")
        self.note("[b]Plan przerwany błędem.[/b] Esc = powrót.")


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
                  height: 1; width: 1fr; }
    .scen-check:focus { text-style: bold; }
    .scen-check.-on { text-style: bold; }
    /* Grupa ikon (rozwiń/opcje/usuń) doklejona do prawej krawędzi wiersza
       (checkbox ma width:1fr) – stała pozycja niezależnie od długości
       nazwy scenariusza, zamiast kupić się zaraz za tekstem. */
    .scen-actions { width: auto; height: 1; }
    .scen-icon { width: 4; height: 1; content-align: center middle; }
    .scen-icon:hover { background: #333333; color: $text; }
    .scen-arrow { color: #888888; }
    .scen-del { color: #666666; }

    /* Przełącznik trybów: same klikalne teksty (bez suwaka, bez
       animacji); aktywna strona pogrubiona i jaśniejsza. */
    #mode-toggle { height: auto; width: 72; max-width: 100%;
                   margin-bottom: 1; align: left middle; }
    .mode-label { width: auto; color: #666666; margin: 0 1; }
    .mode-label:hover { color: #999999; }
    .mode-label.active { color: $text; text-style: bold; }
    .mode-sep { width: auto; color: #444444; }

    /* Kreator autonomiczny: karty 'Pomiar N'. */
    #measurements { width: 72; max-width: 100%; height: auto; }
    .measurement-card { width: 100%; height: auto; border: round #555555;
                        background: transparent; padding: 0 1;
                        margin-bottom: 1; }
    .card-head { height: 1; }
    .card-title { width: 1fr; text-style: bold; color: $text; }
    .card-title:hover { color: #bbbbbb; }
    .card-del { width: 4; content-align: center middle; color: #666666; }
    .card-del:hover { background: #333333; color: $text; }
    .card-body { height: auto; }
    .card-row { height: auto; }
    .card-col { width: 1fr; height: auto; padding-right: 1; }
    .card-delay-on, .card-rtt-on { border: none; background: transparent;
                     padding: 0; height: 1; width: auto; margin-top: 1; }
    .card-delay-s, .card-voltage { width: 100%; }
    .card-rtt-box { height: auto; }
    /* Wyraźniejszy odstęp między sekcją RTT a napięciem/zapisem. */
    .card-vs-row { margin-top: 2; }
    .card-adv { background: transparent; }
    .card-apply-row { height: auto; }
    .card-apply, .card-apply-next { min-width: 0; margin: 1 2 1 0; }
    #add_measurement { min-width: 0; width: 72; max-width: 100%; }
    /* Widoczność .auto-only / .standard-only ustawia _apply_mode() w
       on_mount (PO zamontowaniu) – nie przez display:none w CSS, bo
       Select zamontowany od razu jako display:none nie tworzy overlaya. */
    #pristine, #reset, #swd_reminder { border: none; background: transparent; padding: 0;
                height: 1; margin-top: 1; }
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
    /* Rząd akcji mieści się dokładnie w obrysie ramek (72 kolumny):
       bez sztucznego min-width przycisków. */
    #actions { margin-top: 1; height: auto; }
    #actions Button { margin-right: 2; min-width: 0; }

    #status { background: transparent; padding: 0 1; height: 1;
              text-style: bold; }
    /* Tabelka zakończonych pomiarów (na górze). */
    #results { background: transparent; height: auto; max-height: 12;
               margin: 1 1 0 1; }
    #results > .datatable--header { background: transparent;
                                    color: #888888; text-style: none; }
    #cmds-title { height: 1; color: #777777; padding: 0 1; margin-top: 1; }
    #cmds { padding: 0 1; height: auto; max-height: 22; }
    /* Duże okno pomiaru pod logami build/flash. */
    #measure-panel { height: auto; border: round #888888; margin: 1 1;
                     padding: 1 2; background: transparent; }
    #measure-head { height: 1; color: $text; }
    #measure-row { height: auto; margin-top: 1; }
    #measure-avg { width: 1fr; height: 2; text-style: bold;
                   content-align: left middle; }
    #measure-remain { width: 1fr; height: 2; content-align: right middle;
                      color: #aaaaaa; }
    #measure-inst { height: 1; color: #888888; }
    #stop_measure { min-width: 12; margin-top: 1; }
    #hint { background: transparent; color: #777777; padding: 0 1;
            height: 1; dock: bottom; }
    .note { color: $text; padding: 0 1; }
    Collapsible { background: transparent; border: none; padding: 0; }
    CollapsibleTitle { color: $text; }
    CollapsibleTitle:hover { background: transparent; text-style: bold; }
    .cmd-log { height: 14; border: round #555555; background: transparent;
               margin: 0 1 1 2; }
    /* Tabelka pamięci po buildzie – wąska ramka, tekst monospace MD do
       skopiowania. */
    .mem-report { height: auto; width: auto; max-width: 100%;
                  border: round #555555; border-title-color: #888888;
                  background: transparent; color: $text;
                  padding: 0 1; margin: 0 1 1 2; }

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

    #path-row { height: auto; }
    #path-row #path { width: 1fr; }
    #path-row #browse { margin-left: 2; min-width: 0; }

    /* Eksplorator plików ('Przeglądaj…'): monochromatyczne drzewo. */
    #browse-root { color: #888888; text-wrap: nowrap;
                   text-overflow: ellipsis; }
    #browse-tree { height: 16; border: round #555555; background: transparent;
                   padding: 0 1; margin-top: 1; }
    #browse-tree:focus { border: round #aaaaaa; }
    #browse-tree > .directory-tree--folder { color: $text; text-style: bold; }
    #browse-tree > .directory-tree--file { color: $text; }
    #browse-tree > .directory-tree--extension { color: #888888; }
    #browse-tree > .tree--guides,
    #browse-tree > .tree--guides-hover,
    #browse-tree > .tree--guides-selected { color: #444444; }
    #browse-tree > .tree--cursor { background: #333333; text-style: none; }
    #browse-tree > .tree--highlight { text-style: none; }
    #browse-tree > .tree--highlight-line { background: transparent; }
    """

    def _reassert_mouse(self):
        """Ponownie włącz w terminalu raportowanie myszy (sekwencje
        ?1000/1003/1015/1006h). Textual zapisuje je RAZ przy starcie i
        odnawia tylko przy SIGTSTP/SIGCONT – nie przy zwykłym odzyskaniu
        fokusu okna. Na Linuksie przy kilku otwartych oknach terminala
        tryb myszy bywa gubiony: przełączanie fokusu między oknami albo
        narzędzie piszące wprost do /dev/tty (J-Link/nrfutil) potrafi go
        zresetować i wtedy aplikacja przestaje reagować na kliknięcia aż
        do restartu. Ponowny zapis jest idempotentny (gdy tryb i tak jest
        włączony – nic nie psuje), więc wołamy go po powrocie fokusu."""
        driver = getattr(self, "_driver", None)
        enable = getattr(driver, "_enable_mouse_support", None)
        if enable is not None:
            try:
                enable()
            except Exception:
                pass

    def on_app_focus(self, event):
        # Okno odzyskało fokus (np. powrót z innego okna terminala) –
        # odnów tryb myszy, żeby kliknięcia znów działały. Uzupełnia
        # wewnętrzny handler Textuala (oba się wywołują, patrz MRO).
        self._reassert_mouse()

    def __init__(self):
        super().__init__()
        self.manifest = core.load_manifest()
        self.defaults = self.manifest.get("defaults", {})
        self.boards = self.manifest.get("boards", {})
        self.scenarios = self.manifest.get("scenarios", {})
        if not self.boards or not self.scenarios:
            core.die("manifest musi zawierać sekcje [boards.*] i [scenarios.*]")
        self.mode = "standard"          # standard | auto
        self._card_uid = 0              # licznik kart 'Pomiar N'
        self._card_template = None      # config dziedziczony przez nowe karty

    def compose(self):
        default_prof = self.defaults.get("profile")
        if default_prof not in self.boards:
            default_prof = next(iter(self.boards))
        # VerticalScroll: przy małym oknie menu się przewija zamiast ucinać.
        with VerticalScroll(id="setup"):
            # Przełącznik trybów na samej górze: pomiar ręczny (Power
            # Profiler) vs tryb autonomiczny (plan + PPK2 + wykres).
            # Same klikalne teksty (bez animowanego suwaka) – kliknięcie
            # w tekst przełącza tryb, aktywny jest wytłuszczony.
            with Horizontal(id="mode-toggle"):
                yield ModeLabel("Pomiar ręczny", "standard",
                                id="mode-label-standard",
                                classes="mode-label active")
                yield Static("│", classes="mode-sep")
                yield ModeLabel("Tryb autonomiczny", "auto",
                                id="mode-label-auto", classes="mode-label")
            yield Static(LOGO, id="logo")
            yield Label("Płytka", classes="h")
            yield Select(((b["board"], n) for n, b in self.boards.items()),
                         value=default_prof, allow_blank=False, id="profile")
            # --- Tryb ręczny: checklista scenariuszy ---
            yield Label("Scenariusze", classes="h standard-only")
            with Vertical(id="scenarios", classes="standard-only"):
                # Wybór i opis to OSOBNE cele kliknięcia: checkbox z pełną
                # nazwą zaznacza scenariusz, a strzałka za nazwą rozwija
                # opis (pełną szerokością, z małym wcięciem). Bez wartości
                # oczekiwanych – te pokazuje dopiero instrukcja pomiaru.
                for n, s in self.scenarios.items():
                    yield self._scenario_row(n, s)
            # --- Tryb autonomiczny: kreator kart 'Pomiar N' ---
            # Kolejność kart = kolejność wykonania. Scenariusz + czas na
            # wierzchu; start-po-czasie / RTT / napięcie / zapis w zwijanych
            # ustawieniach zaawansowanych (domyślnie schowane i wyłączone).
            yield Label("Pomiary", classes="h auto-only")
            with Vertical(id="measurements", classes="auto-only"):
                yield MeasurementCard(0, self.scenarios, 1)
            yield Button("+ Dodaj pomiar", id="add_measurement",
                         classes="auto-only")

            yield Label("Egzemplarz płytki (trafia do dziennika CSV)",
                        classes="h")
            yield Input(placeholder="np. BTZ #2", id="sample")
            yield Check("Wymuś pełny rebuild (gotowe buildy są "
                        "normalnie pomijane)", value=False, id="pristine")
            yield Check("Zresetuj płytkę po wgraniu (J-Link)",
                        value=True, id="reset", classes="standard-only")
            yield Check("Przypomnij o odpięciu programatora (SWD/J-Link)",
                        value=True, id="swd_reminder",
                        classes="standard-only")
            with Horizontal(id="actions"):
                yield Button("Start", id="start")
                yield Button("Zaznacz wszystkie", id="select_all",
                             classes="standard-only")
                yield Button("Dodaj kod", id="add_fw")
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
                Check(_label(n, s), value=value, classes="scen-check",
                      id=f"check_{n}"),
                Horizontal(
                    DescArrow(f"desc_{n}", id=f"arrow_{n}"),
                    DeleteCross(n, id=f"del_{n}"),
                    classes="scen-actions"),
                classes="scenario-head"),
            Static(body, classes="scen-desc", id=f"desc_{n}"),
            classes="scenario-row", id=f"row_{n}")

    def confirm_remove(self, name):
        """✕ przy scenariuszu: potwierdzenie i usunięcie wpisu."""
        def done(ok):
            if ok:
                self._remove_scenario(name)
        label = _label(name, self.scenarios.get(name, {}))
        self.push_screen(ConfirmScreen(
            f"[b]Usunąć scenariusz „{label}”?[/b]\n\n"
            "Wpis zniknie z scenarios.toml.\n"
            "Zebrane pomiary w reports/pomiary.csv zostają.",
            yes="Usuń", no="Anuluj"), callback=done)

    def _remove_scenario(self, name):
        try:
            core.remove_scenario(name)
        except ValueError as e:
            self.notify(str(e), severity="error")
            return
        self.scenarios.pop(name, None)
        self.query_one(f"#row_{name}").remove()
        self._update_select_all()
        self.notify(f"Usunięto scenariusz '{name}' z scenarios.toml.")

    def on_mount(self):
        self._apply_mode()

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
        elif event.button.id == "add_measurement":
            self.add_measurement()
        elif event.button.has_class("card-apply-next"):
            self._apply_to_following(event.button)
        elif event.button.has_class("card-apply"):
            self._apply_to_all(event.button)
        elif event.button.id == "start":
            self._start()

    # ---------- przełączanie trybów ----------

    def _set_mode(self, mode):
        if mode != self.mode:
            self.mode = mode
            self._apply_mode()

    def _apply_mode(self):
        """Pokaż/ukryj elementy zależne od trybu i wytłuść aktywną
        etykietę przełącznika. Widoczność sterowana klasą na widgetach
        (.auto-only / .standard-only)."""
        auto = self.mode == "auto"
        self.query_one("#mode-label-standard", Static).set_class(
            not auto, "active")
        self.query_one("#mode-label-auto", Static).set_class(auto, "active")
        for w in self.query(".auto-only"):
            w.display = auto
        for w in self.query(".standard-only"):
            w.display = not auto
        self.query_one("#start", Button).label = (
            "Dalej: PPK2 →" if auto else "Start")

    # ---------- karty 'Pomiar N' (kreator autonomiczny) ----------

    def add_measurement(self):
        """Dodaj kartę pomiaru. Wcześniejsze karty zwijają się do jednego
        wiersza (widać całą listę), a nowa dziedziczy szablon ustawiony
        'do następnych' albo config poprzedniej (oprócz scenariusza)."""
        cards = list(self.query(MeasurementCard))
        for c in cards:
            c.set_collapsed(True)
        cfg = None
        if self._card_template is not None:
            cfg = dict(self._card_template)   # szablon 'do następnych' – z czasem
        elif cards:
            cfg = dict(cards[-1].get_config())
            cfg["duration"] = ""              # nowy pomiar: czas pusty
        if cfg is not None:
            cfg.pop("scenario", None)      # nowy pomiar: scenariusz do wyboru
        self._card_uid += 1
        card = MeasurementCard(self._card_uid, self.scenarios,
                               len(cards) + 1, cfg)
        self.query_one("#measurements").mount(card)
        # Przewiń do nowej karty dopiero PO zamontowaniu (inaczej wymusza
        # przedwczesny layout Selecta, zanim powstanie jego overlay).
        self.call_after_refresh(card.scroll_visible, animate=False)

    def remove_measurement(self, uid):
        cards = list(self.query(MeasurementCard))
        if len(cards) <= 1:
            self.notify("Musi zostać co najmniej jeden pomiar.",
                        severity="warning")
            return
        remaining = [c for c in cards if c.uid != uid]
        for c in cards:
            if c.uid == uid:
                c.remove()
                break
        # Przenumeruj z listy POZOSTAŁYCH (remove() jest asynchroniczny,
        # więc ponowne odpytanie drzewa wciąż widzi znikającą kartę).
        for i, card in enumerate(remaining, 1):
            card.set_number(i)

    def _refresh_card_titles(self):
        for card in self.query(MeasurementCard):
            card._refresh_title()

    def _card_of(self, widget):
        while widget is not None and not isinstance(widget, MeasurementCard):
            widget = widget.parent
        return widget

    def _apply_to_all(self, button):
        """'Zastosuj do wszystkich' – skopiuj config karty (bez scenariusza)
        na WSZYSTKIE pozostałe, już istniejące karty."""
        card = self._card_of(button)
        if card is None:
            return
        cfg = card.get_config()
        for other in self.query(MeasurementCard):
            if other is not card:
                other.apply_shared(cfg)
        self._refresh_card_titles()
        self.notify("Zastosowano ustawienia do wszystkich pomiarów.")

    def _apply_to_following(self, button):
        """'…do następnych' – zapamiętaj config jako szablon; każdy KOLEJNY
        dodany pomiar dostanie te ustawienia (bez zmiany istniejących)."""
        card = self._card_of(button)
        if card is None:
            return
        cfg = dict(card.get_config())
        cfg.pop("scenario", None)
        self._card_template = cfg
        self.notify("Nowe pomiary będą dziedziczyć te ustawienia.")

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
        self._update_select_all()

    def _update_select_all(self):
        """'Zaznacz wszystkie' wygląda na wciśnięty dokładnie wtedy, gdy
        zaznaczone są wszystkie scenariusze."""
        boxes = self.query(".scen-check")
        self.query_one("#select_all", Button).set_class(
            bool(boxes) and all(box.value for box in boxes), "pressed")

    def _start(self):
        sample = self.query_one("#sample", Input).value.strip()
        prof_name = self.query_one("#profile", Select).value
        if not sample:
            self.notify("Podaj egzemplarz płytki (np. 'BTZ #2').",
                        severity="error")
            self.query_one("#sample", Input).focus()
            return
        if self.mode == "auto":
            self._start_auto(sample, prof_name)
            return
        names = [n for n in self.scenarios
                 if self.query_one(f"#check_{n}", Checkbox).value]
        if not names:
            self.notify("Zaznacz co najmniej jeden scenariusz.",
                        severity="error")
            return
        self.push_screen(RunScreen(prof_name, self.boards[prof_name],
                                   names, sample,
                                   pristine=self.query_one("#pristine",
                                                           Checkbox).value,
                                   reset=self.query_one("#reset",
                                                        Checkbox).value,
                                   swd_reminder=self.query_one(
                                       "#swd_reminder", Checkbox).value))

    def _start_auto(self, sample, prof_name):
        """Zbuduj plan z kart, potem ekran połączenia z PPK2; po 'Start'
        na tamtym ekranie odpal pulpit pomiaru."""
        try:
            plan = self._build_auto_plan(prof_name)
        except ValueError as e:
            self.notify(str(e), severity="error")
            return
        from autorun.plan import validate_plan
        errors = validate_plan(plan, self.manifest)
        if errors:
            self.notify("Błędy konfiguracji:\n" + "\n".join(errors),
                        severity="error", timeout=8)
            return

        def go(ok):
            if ok:
                self.push_screen(AutoRunScreen(plan, sample))
        self.push_screen(Ppk2ConnectScreen(), callback=go)

    def _build_auto_plan(self, prof_name):
        """Plan trybu autonomicznego z kart 'Pomiar N'. Kolejność kroków =
        kolejność kart. Trigger: RTT 'start po logu' > 'start po czasie' >
        od razu; przy RTT continuous wzorzec staje się auto-etykietą."""
        from autorun.plan import (LabelRule, Plan, PlanStep, Storage,
                                  Trigger, parse_duration)

        pristine = self.query_one("#pristine", Checkbox).value
        cards = list(self.query(MeasurementCard))
        if not cards:
            raise ValueError("Dodaj co najmniej jeden pomiar.")
        steps = []
        for card in cards:
            c = card.get_config()
            if not c["scenario"]:
                raise ValueError(f"Pomiar {card.number}: wybierz scenariusz.")
            try:
                dur_s = parse_duration(c["duration"])
            except ValueError as e:
                raise ValueError(f"Pomiar {card.number}: {e}")
            rtt = c["rtt_mode"] if c["rtt_on"] else "off"
            labels = []
            if rtt == "trigger":
                trigger = Trigger(type="rtt", pattern=c["pattern"],
                                  timeout_s=180.0)
            else:
                if c["delay_on"]:
                    try:
                        secs = parse_duration(c["delay_s"])
                    except ValueError:
                        raise ValueError(
                            f"Pomiar {card.number}: start po czasie – "
                            f"'{c['delay_s']}' nie jest czasem (np. 30s).")
                else:
                    secs = 0.0
                trigger = Trigger(type="delay", seconds=secs)
                if rtt == "continuous" and c["pattern"]:
                    labels = [LabelRule(pattern=c["pattern"],
                                        label=c["pattern"])]
            steps.append(PlanStep(
                scenario=c["scenario"], duration_s=dur_s,
                voltage=c["voltage"], trigger=trigger, rtt=rtt,
                sample_rate=c["sample_rate"],
                storage=Storage(mode=c["storage"], window_ms=1),
                labels=labels, pristine=pristine))
        return Plan(name="interfejs", board=prof_name, steps=steps)


if __name__ == "__main__":
    PowerTestApp().run()
