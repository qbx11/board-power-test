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
import math
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
                             DirectoryTree, Input, Label, Log, RadioButton,
                             RadioSet, Select, Static, TabbedContent, TabPane,
                             TextArea)

import power_test as core

# Limit krotności karty ('x1 … x5') trzyma plan – tam też rozwijają się
# powtórki na osobne kroki, więc UI nie ma własnej, drugiej prawdy.
from autorun.plan import REPEAT_MAX

# Model kalkulatora poboru prądu i kalibracja z przebiegu sesji – cała
# matematyka siedzi tam, interfejs tylko zbiera liczby z pól.
from autorun import energy

# Domyślny operational dataset Thread (hex) dla triggera 'chip' w kartach
# pomiaru – ten sam, co domyślny w scripts/pair_and_subscribe.py. Pole w UI
# jest edytowalne; to tylko wygodna wartość startowa dla typowego setupu.
CHIP_DATASET_DEFAULT = (
    "0e08000000000001000000030000174a0300000e35060004001fffe0"
    "0208813ba4b5a068fddf0708fddc8e685e36d6cc0510b840138392a6efbee6"
    "1680bdca9ae7fd030f4f70656e5468726561642d666236650102fb6e04108c"
    "97ec5b81873b78c371537a24886bef0c0402a0f7f8")

# Logo GoodByte – nagłówek ekranu głównego. Czcionka blokowa (Small Mono
# 12), monochromatyczna jak reszta interfejsu; pod spodem podpis
# narzędzia. Statyczny tekst, bez zależności runtime.
LOGO = """\
      ▗▄        ▄▄              ▗▖▗▄▄▖                      ▄▖
      █        █▀▀▌             ▐▌▐▛▀▜▌      ▐▌              █
      █       ▐▌    ▟█▙  ▟█▙  ▟█▟▌▐▌ ▐▌▝█ █▌▐███  ▟█▙        █
      █       ▐▌▗▄▖▐▛ ▜▌▐▛ ▜▌▐▛ ▜▌▐███  █▖█  ▐▌  ▐▙▄▟▌       █
     ▀▙       ▐▌▝▜▌▐▌ ▐▌▐▌ ▐▌▐▌ ▐▌▐▌ ▐▌ ▐█▛  ▐▌  ▐▛▀▀▘       ▟▀
      █        █▄▟▌▝█▄█▘▝█▄█▘▝█▄█▌▐▙▄▟▌  █▌  ▐▙▄ ▝█▄▄▌       █
      █         ▀▀  ▝▀▘  ▝▀▘  ▝▀▝▘▝▀▀▀   █    ▀▀  ▝▀▀        █
      ▜▄                                █▌                  ▄▛
[#888888]                  b o a r d   p o w e r   t e s t[/]"""


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
            # Esc mógł paść, zanim proces powstał – ubij go od razu.
            if handle.get("abort") and proc.poll() is None:
                proc.terminate()
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


def _fmt_hms(seconds):
    """Czas jako 'h:mm:ss', a poniżej godziny 'mm:ss' – ten sam format na
    ekranie przebiegu (zegar pomiaru) i w kreatorze (czas kart i suma),
    żeby te same liczby nie wyglądały w dwóch miejscach inaczej."""
    seconds = max(0, int(round(seconds)))
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _fmt_uA(uA):
    """Prąd w czytelnej jednostce: nA / µA / mA / A. Wspólny dla ekranu
    przebiegu (zmierzony prąd) i kalkulatora (policzony) – ta sama liczba
    nie ma wyglądać w dwóch miejscach inaczej."""
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


def _plural_measurements(n):
    """'1 pomiar' / '3 pomiary' / '7 pomiarów' – odmiana do podpisu sumy."""
    if n == 1:
        return "1 pomiar"
    if 2 <= n % 10 <= 4 and n % 100 not in (12, 13, 14):
        return f"{n} pomiary"
    return f"{n} pomiarów"


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

    def toggle(self):
        desc = self.screen.query_one(f"#{self.desc_id}", Static)
        shown = not desc.has_class("shown")
        desc.set_class(shown, "shown")
        self.update("▼" if shown else "▶")

    def on_click(self, event):
        event.stop()
        self.toggle()


class ScenName(Static):
    """Nazwa scenariusza w okienku „Scenariusze” – klik rozwija/zwija opis.
    W trybie ręcznym tę linię zajmuje checkbox (zaznaczenie = zmierz to),
    tu nie ma czego zaznaczać, więc cała nazwa działa jak strzałka."""

    def __init__(self, text, arrow_id, **kwargs):
        super().__init__(text, classes="scen-name", **kwargs)
        self.arrow_id = arrow_id

    def on_click(self, event):
        event.stop()
        self.screen.query_one(f"#{self.arrow_id}", DescArrow).toggle()


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


class RepeatButton(Button):
    """Krotność w nagłówku karty 'Pomiar N': klik przestawia x1 -> x2 -> …
    -> x5 -> x1. x1 to jeden pomiar (jak dotąd), xK to K OSOBNYCH pomiarów
    tej samej karty, jeden po drugim – każdy z własnym flashem, oknem
    pomiaru, katalogiem sesji i wierszem w raporcie. Wspólne dla
    protokołów, dlatego siedzi w nagłówku, a nie w zakładce."""

    def __init__(self, count=1, **kwargs):
        super().__init__(classes="card-repeat", **kwargs)
        self.count = count

    @property
    def count(self):
        return self._count

    @count.setter
    def count(self, value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 1
        self._count = max(1, min(value, REPEAT_MAX))
        self.label = f"x{self._count}"
        # Krotność > 1 wyróżniamy, bo zmienia CZAS przebiegu – przy dwóch
        # cyfrach w kolumnie kart musi być widać, która karta jedzie kilka
        # razy, bez czytania każdej z bliska.
        self.set_class(self._count > 1, "on")

    def bump(self):
        """Następna krotność w cyklu; po x5 wracamy do x1."""
        self.count = 1 if self._count >= REPEAT_MAX else self._count + 1


# Nazwy składników modelu w interfejsie. Silnik trzyma je jako klucze
# ('send' / 'poll'), bo po nich rozpoznaje role interwałów w sesji.
TERM_LABELS = {"send": "wysłania", "poll": "polle",
               "heartbeat": "heartbeat"}

# Składniki, których NIE ma każdy protokół – dokładane do sekcji tylko tam,
# gdzie występują. Zigbee ma heartbeat schedulera ZBOSS: co ~1 s procesor
# budzi się na ~2 ms, żeby obsłużyć kolejkę timerów stosu, i wraca do snu.
# To NIE jest keepalive ani poll – nic nie leci przez radio (0.92 µC wobec
# ~46 µC realnego wysłania), ale przy interwałach rzędu minut ten składnik
# bywa drugą co do wielkości pozycją budżetu, więc model bez niego zaniża
# wynik. Wartości domyślne = pomiar z tego repo (0.92 µC co 1 s ≈ 0.92 µA).
# Siedzi w prekompilowanym libzboss.a, więc aplikacja go nie wyłączy –
# pole zostaje edytowalne, bo inna wersja stosu może mieć inny koszt.
# Pola: (rola, etykieta ładunku, ładunek, etykieta interwału, interwał,
#        nazwa w tabelce wartości oczekiwanych)
EXTRA_TERMS = {
    "zigbee": (("heartbeat", "Ładunek ZBOSS (µC):", "0.92",
                "Interwał ZBOSS (s):", "1", "Ładunek ZBOSS"),),
}

# Wartości oczekiwane są rzędem wielkości z pomiarów – a te różnią się
# między protokołami. REF_FIELDS trzyma liczby z węzła LPN (BLE Mesh),
# a tu nadpisujemy je tam, gdzie mamy własny pomiar. Zigbee: BTZ_EndDevice
# z tego repo (baseline 3.2 µA, wysłanie 46.2 µC, poll 19.8 µC).
REF_OVERRIDES = {
    "zigbee": {"baseline": "3.2", "send-charge": "46.2",
               "poll-charge": "19.8"},
}

# Tabelka "wartości oczekiwanych" przy każdej sekcji kalkulatora: rząd
# wielkości zmierzony na węźle LPN (te same liczby, co placeholdery pól).
# Każda sekcja ma WŁASNĄ kopię tych pól (patrz CalculatorSection.compose),
# więc edycja w jednym protokole nie rusza pozostałych dwóch.
REF_FIELDS = (
    ("baseline", "Baseline (µA)", "2.4"),
    ("send-charge", "Ładunek send", "22"),
    ("poll-charge", "Ładunek poll", "1478"),
)


class RefLabel(Static):
    """Nazwa wiersza w tabelce wartości oczekiwanych – klik kopiuje liczbę
    z pola obok (edytowalnego) do odpowiadającego pola kalkulatora tej
    samej sekcji. Sama liczba w tabelce zostaje nietknięta, więc kolejny
    klik po edycji wpisuje już nową wartość."""

    def __init__(self, text, field, **kwargs):
        super().__init__(text, classes="calc-ref-name", **kwargs)
        self.field = field

    def on_click(self, event):
        event.stop()
        value = self.parent.query_one(Input).value
        section = self.parent
        while section is not None and not isinstance(section, CalculatorSection):
            section = section.parent
        if section is not None:
            section.apply_reference(self.field, value)


class CalculatorSection(Vertical):
    """Sekcja kalkulatora poboru prądu dla JEDNEGO protokołu.

    Model jest ten sam dla wszystkich trzech (patrz autorun/energy.py):
    prąd średni to prąd bezczynności plus po jednym składniku na rodzaj
    wybudzenia. Sekcje są osobne, bo każdy protokół ma własne, wpisane
    liczby – a nie bo liczą inaczej.

    Sekcja niczego nie mierzy i nie czyta zapisanych przebiegów. Wszystkie
    liczby wpisuje człowiek; placeholdery podpowiadają rzędy wielkości
    zmierzone w tym repo na węźle LPN."""

    def __init__(self, protocol, label, **kwargs):
        super().__init__(classes="calc-section", id=f"calc-{protocol}",
                         **kwargs)
        self.protocol = protocol
        self.proto_label = label

    def compose(self):
        yield Label(self.proto_label, classes="h calc-head")
        with Horizontal(classes="calc-body"):
            with Vertical(classes="calc-fields"):
                # Jednostki w nawiasach OKRĄGŁYCH, nie kwadratowych: Label
                # renderuje treść przez markup Rich, w którym '[s]' jest
                # znacznikiem przekreślenia – nawias znikał, a resztę
                # wiersza przekreślało.
                yield Label("Prąd bezczynności (baseline) w µA:")
                yield Input(placeholder="np. 2.4", classes="calc-baseline")
                with Horizontal(classes="calc-row"):
                    with Vertical(classes="calc-col"):
                        yield Label("Ładunek jednego wysłania (µC):")
                        yield Input(placeholder="np. 22",
                                    classes="calc-send-charge")
                    with Vertical(classes="calc-col"):
                        yield Label("Interwał send (s):")
                        yield Input(placeholder="np. 30",
                                    classes="calc-send-period")
                with Horizontal(classes="calc-row"):
                    with Vertical(classes="calc-col"):
                        yield Label("Ładunek jednego polla (µC):")
                        yield Input(placeholder="np. 1478",
                                    classes="calc-poll-charge")
                    with Vertical(classes="calc-col"):
                        # Puste pola polla znaczą, że węzeł nie pollue –
                        # etykieta tego nie tłumaczy, bo to widać po
                        # wyniku (składnik po prostu nie wchodzi do
                        # rozkładu).
                        yield Label("Interwał poll (s):")
                        yield Input(placeholder="np. 60",
                                    classes="calc-poll-period")
                # Składniki własne protokołu (patrz EXTRA_TERMS). Wpisane
                # z góry, a nie jako placeholder: heartbeat jest w stosie
                # zawsze, więc model ma go liczyć bez proszenia. Puste
                # pole interwału nadal go wyłącza, jak każdy inny składnik.
                for role, charge_label, charge_value, period_label, \
                        period_value, _ref in EXTRA_TERMS.get(self.protocol, ()):
                    with Horizontal(classes="calc-row"):
                        with Vertical(classes="calc-col"):
                            yield Label(charge_label)
                            yield Input(value=charge_value,
                                        classes=f"calc-{role}-charge")
                        with Vertical(classes="calc-col"):
                            yield Label(period_label)
                            yield Input(value=period_value,
                                        classes=f"calc-{role}-period")
                # Wynik w ramce: to jedyna rzecz w sekcji, po którą się tu
                # przyszło, więc nie ma być kolejnym wierszem tabelki.
                # Podpis po lewej, liczba dociągnięta do prawej krawędzi.
                with Horizontal(classes="calc-average"):
                    yield Static("Średni pobór prądu",
                                classes="calc-average-label")
                    yield Static("—", classes="calc-average-value")
                yield Static("", classes="calc-budget")
            # Tabelka wartości oczekiwanych: osobna kolumna po prawej, żeby
            # klik w wiersz i edycja liczby obok nie kolidowały z polami
            # kalkulatora w kolumnie po lewej.
            with Vertical(classes="calc-ref"):
                yield Static("Wartości oczekiwane", classes="calc-ref-head")
                extra_refs = tuple(
                    (f"{role}-charge", ref_label, charge_value)
                    for role, _cl, charge_value, _pl, _pv, ref_label
                    in EXTRA_TERMS.get(self.protocol, ()))
                overrides = REF_OVERRIDES.get(self.protocol, {})
                for field, label, default in REF_FIELDS + extra_refs:
                    with Horizontal(classes="calc-ref-row"):
                        yield RefLabel(label, field)
                        yield Input(value=overrides.get(field, default),
                                    classes=f"calc-ref-value calc-ref-{field}")

    def on_mount(self):
        self.refresh_result()

    # ---------- odczyt pól ----------

    def _value(self, selector):
        """Liczba z pola albo None (puste / nie liczba). Kalkulator nie
        krzyczy o niewypełnione pola – po prostu nie liczy tego składnika."""
        text = self.query_one(selector, Input).value.strip().replace(",", ".")
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
        return value if value >= 0 else None

    def baseline_uA(self):
        return self._value(".calc-baseline") or 0.0

    def terms(self):
        """Składniki modelu z pól. Składnik istnieje, gdy ma i ładunek,
        i interwał – puste pole interwału znaczy „tego wybudzenia nie ma”
        (np. węzeł, który nie pollue)."""
        out = []
        roles = ("send", "poll") + tuple(
            row[0] for row in EXTRA_TERMS.get(self.protocol, ()))
        for role in roles:
            charge = self._value(f".calc-{role}-charge")
            period = self._value(f".calc-{role}-period")
            if charge is None or not period:
                continue
            out.append(energy.Term(name=role, charge_uC=charge,
                                   period_s=period))
        return out

    # ---------- wynik ----------

    def refresh_result(self):
        value = self.query_one(".calc-average-value", Static)
        rows = self.query_one(".calc-budget", Static)
        baseline = self.baseline_uA()
        terms = self.terms()
        if not terms and not baseline:
            value.update("—")
            rows.update("Wpisz prąd bezczynności i koszt wybudzeń.")
            return
        value.update(_fmt_uA(energy.average_uA(baseline, terms)))
        lines = []
        for share in energy.budget(baseline, terms):
            name = TERM_LABELS.get(share.name, share.name)
            # Rozkład ZAWSZE w µA, nawet gdy składnik schodzi poniżej 1 µA:
            # ta tabela służy do porównywania wierszy ze sobą, a jeden
            # wiersz w nA psułby porównanie mimo poprawnej wartości.
            lines.append(f"{name:<14}{share.current_uA:9.2f} µA"
                         f"{share.percent:6.0f}%")
        rows.update("\n".join(lines))

    def on_input_changed(self, event):
        # Zdarzenia NIE zatrzymujemy: App też ich słucha (suma czasów
        # w kreatorze trybu autonomicznego).
        self.refresh_result()

    # ---------- tabelka wartości oczekiwanych ----------

    def apply_reference(self, field, value):
        """Wpisz liczbę z tabelki wartości oczekiwanych do pola `field`
        ('baseline' / 'send-charge' / ... – patrz REF_FIELDS)."""
        value = value.strip()
        if not value:
            return
        self.query_one(f".calc-{field}", Input).value = value
        self.refresh_result()


class ConfirmScreen(ModalScreen[bool]):
    """Dialog z pytaniem; `no=None` daje pojedynczy przycisk (twardy krok).
    `danger=True` oznacza przycisk 'yes' jako nieodwracalny (czerwony hover)."""

    def __init__(self, text, yes="OK", no="Anuluj", danger=False):
        super().__init__()
        self.text, self.yes, self.no, self.danger = text, yes, no, danger

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(self.text, classes="dialog-text")
            with Horizontal(classes="dialog-buttons"):
                yield Button(self.yes, id="yes",
                            classes="danger" if self.danger else "")
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

    ICON_NODE = "▶ "
    ICON_NODE_EXPANDED = "▼ "
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
    """Podgląd dziennika pomiarów (reports/pomiary.csv), filtrowany do
    bieżącego trybu: w trybie autonomicznym pokazuje tylko pomiary
    autonomiczne (mają zapisaną sesję), w ręcznym – tylko ręczne. Każdy tryb
    dostaje kolumny właściwe dla siebie (autonomiczny: seria/parametr/min/max/
    czas; ręczny: klasyczne pola z 'oczekiwane')."""

    # Wiersz autonomiczny odróżniamy po zapisanej ścieżce sesji (tryb ręczny
    # nigdy jej nie ustawia). pomiar_id/parametr/wartosc mówią, który pomiar
    # z serii (sweep) miał jaką wartość flagi build.
    MANUAL_COLS = ["data", "egzemplarz", "scenariusz", "napiecie_V",
                   "prad_uA", "oczekiwane", "flash_B", "flash_pct",
                   "ram_B", "ram_pct", "uwagi"]
    # 'egzemplarz' jest w OBU trybach: bez niego nie wiadomo, której płytki
    # dotyczy wiersz, a dziennik zbiera wyniki z wielu egzemplarzy.
    AUTO_COLS = ["data", "egzemplarz", "pomiar_id", "scenariusz", "parametr",
                 "wartosc", "parametr2", "wartosc2", "napiecie_V", "prad_uA",
                 "flash_B", "flash_pct", "ram_B", "ram_pct", "czas_s"]
    # Kolumny pokazywane tylko wtedy, gdy JAKIŚ widoczny wiersz je wypełnia.
    # Bez tego dziennik bez serii dwuparametrowej albo ze scenariuszami na
    # gotowym hexie (brak builda = brak tabelki pamięci) niósłby stale puste
    # kolumny, a te zjadają szerokość potrzebną nazwie scenariusza.
    OPTIONAL_COLS = ("parametr2", "wartosc2",
                     "flash_B", "flash_pct", "ram_B", "ram_pct")
    # Kolumny liczbowe pokazywane z dokładnością do 2 miejsc po przecinku
    # (surowe wartości w CSV zostają pełne). min/max prądu celowo NIE są
    # pokazywane w tabeli – są w CSV i w podglądzie wykresu sesji.
    _TWO_DP = ("prad_uA", "czas_s", "flash_pct", "ram_pct")

    def __init__(self, mode=None):
        super().__init__()
        self._mode = mode

    @staticmethod
    def _fmt_cell(col, value):
        if col in ResultsScreen._TWO_DP and value not in ("", None):
            try:
                return f"{float(value):.2f}"
            except (TypeError, ValueError):
                return value
        return value

    def compose(self):
        with Vertical(classes="dialog results"):
            yield Static("", id="results-title")
            yield DataTable()
            with Horizontal(classes="dialog-buttons"):
                yield Button("Zamknij", id="close")

    def on_mount(self):
        mode = self._mode or getattr(self.app, "mode", None)
        auto = mode == "auto"
        which = "tryb autonomiczny" if auto else "tryb ręczny"
        self.query_one("#results-title", Static).update(
            f"[b]Zebrane pomiary — {which}[/b] · reports/pomiary.csv")
        table = self.query_one(DataTable)
        cols = self.AUTO_COLS if auto else self.MANUAL_COLS
        rows = []
        if core.CSV_PATH.is_file():
            # Wiersze wczytujemy PRZED nagłówkiem: dopiero komplet danych
            # mówi, które kolumny opcjonalne mają w ogóle treść.
            with open(core.CSV_PATH, newline="", encoding="utf-8") as f:
                rows = [r for r in csv.DictReader(f)
                        if bool(r.get("sesja")) == auto]   # tylko ten tryb
        cols = [c for c in cols
                if c not in self.OPTIONAL_COLS
                or any(r.get(c) for r in rows)]
        table.add_columns(*cols)
        for row in rows:
            table.add_row(*(self._fmt_cell(c, row.get(c, "")) for c in cols))

    def on_button_pressed(self, event):
        self.dismiss()


def _plural_entries(n):
    """'1 wpis' / '3 wpisy' / '7 wpisów' – odmiana do podpisu listy."""
    if n == 1:
        return "1 wpis"
    if 2 <= n % 10 <= 4 and n % 100 not in (12, 13, 14):
        return f"{n} wpisy"
    return f"{n} wpisów"


class ScenariosScreen(ModalScreen):
    """Okienko „Scenariusze” – pełna lista wpisów z manifestu: ▶ (albo klik
    w nazwę) rozwija opis, ✕ usuwa wpis. To ta sama lista, którą tryb
    ręczny ma wprost na ekranie; w trybie autonomicznym scenariusze
    wybiera się w kartach „Pomiar N”, więc lista mieszka w dialogu.
    Usuwanie idzie przez app.confirm_remove(), czyli z tymi samymi
    blokadami (wpis w użyciu / ostatni w manifeście)."""

    BINDINGS = [("escape", "close", "Zamknij")]

    def compose(self):
        with Vertical(classes="dialog scenarios"):
            yield Static("", id="scen-title")
            with VerticalScroll(id="scen-list"):
                for n, s in self.app.scenarios.items():
                    yield self._row(n, s)
            yield Static("Wpisy z scenarios.toml (i prywatnego "
                         "scenarios.local.toml). Nowy dodaje przycisk "
                         "„Dodaj kod”.", classes="scen-hint")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Zamknij", id="close")

    def on_mount(self):
        self._refresh_title()

    @staticmethod
    def _row(name, scen):
        body = scen.get("description", "")
        if scen.get("note"):
            body += f"\nUwaga: {scen['note']}"
        return Vertical(
            Horizontal(
                ScenName(_label(name, scen), f"arrow_{name}"),
                Horizontal(DescArrow(f"desc_{name}", id=f"arrow_{name}"),
                           DeleteCross(name, id=f"del_{name}"),
                           classes="scen-actions"),
                classes="scenario-head"),
            Static(body or "(bez opisu)", classes="scen-desc",
                   id=f"desc_{name}"),
            classes="scenario-row", id=f"row_{name}")

    def _refresh_title(self):
        self.query_one("#scen-title", Static).update(
            f"[b]Scenariusze[/b] · {_plural_entries(len(self.app.scenarios))}")

    def remove_row(self, name):
        """Wołane przez aplikację po skasowaniu wpisu z manifestu – lista
        w dialogu ma zniknąć razem z nim, bez zamykania okienka."""
        for row in self.query(f"#row_{name}"):
            row.remove()
        self._refresh_title()

    def action_close(self):
        self.dismiss()

    def on_button_pressed(self, event):
        self.dismiss()


class _Aborted(Exception):
    """Esc w trakcie komendy: użytkownik przerwał przebieg (to nie błąd)."""


class RunScreen(Screen):
    """Przebieg: FAZA 1 buduje wszystkie obrazy, FAZA 2 – flash + pomiar.
    Każda komenda to zwijana sekcja: tytuł = preview, rozwija się
    klikiem albo automatycznie przy błędzie."""

    BINDINGS = [("escape", "abort", "Przerwij i wróć")]

    HINT = "Esc — przerwij i wróć"

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
        # Esc w trakcie komendy: uchwyt bieżącego procesu (do ubicia) i
        # flaga przerwania. Ekranu NIE zdejmujemy, dopóki wątek west/flash
        # żyje – pisanie do odpiętego Log-a rzuca NoActiveAppError.
        self._active_handle = None
        self._aborting = False
        # Okno „wgraj / pomiń” można zamknąć (żeby obejrzeć logi i tabelkę
        # pamięci); przebieg czeka wtedy na tym zdarzeniu, aż użytkownik
        # otworzy je z powrotem przyciskiem.
        self._reopen = None

    def compose(self):
        yield Static("", id="status")
        # Nagłówek okna logów: przycisk kopiowania tuż nad nimi, po prawej.
        with Horizontal(id="cmds-head"):
            yield Static("Logi budowania", id="cmds-title")
            # Widoczny tylko wtedy, gdy okno wgrywania jest zamknięte.
            yield Button("Wgraj na płytkę", id="reopen_flash")
            yield Button("Kopiuj log", id="copy_log")
        yield VerticalScroll(id="cmds")
        yield Static(self.HINT, id="hint")

    def on_button_pressed(self, event):
        if event.button.id == "copy_log":
            self._copy_log()
        elif event.button.id == "reopen_flash":
            self._reopen_flash()

    def _reopen_flash(self):
        """Klik „Wgraj na płytkę”: otwórz z powrotem zamknięte okno
        wgrywania (przebieg stoi na `self._reopen`)."""
        if self._reopen is not None:
            self._reopen.set()

    def action_abort(self):
        """Esc: gdy komenda (build/flash) trwa – ubij proces i oznacz
        przerwanie, ale NIE zdejmuj ekranu od razu. Wątek strumieniujący
        jeszcze pisze do Log-a, a pisanie do odpiętego widgetu rzuca
        NoActiveAppError; po zakończeniu wątku flow() sam wraca (przez
        _Aborted). Esc bez trwającej komendy = natychmiastowy powrót."""
        if self._active_handle is None:
            if self.app.screen is self:
                self.app.pop_screen()
            return
        if self._aborting:
            return
        self._aborting = True
        # Zaznacz zamiar także w uchwycie – gdyby proces jeszcze nie zdążył
        # powstać, _stream ubije go zaraz po Popen.
        self._active_handle["abort"] = True
        proc = self._active_handle.get("proc")
        if proc is not None and proc.poll() is None:
            proc.terminate()
        self.query_one("#status", Static).update("Przerywam bieżącą komendę…")

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
        self.query_one("#reopen_flash", Button).display = False
        self.flow()

    def note(self, text):
        self.query_one("#cmds").mount(Static(text, classes="note"))

    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    async def run_west(self, cmd, cwd, title, build_dir=None):
        """Komenda w zwijanej sekcji z animacją w trakcie działania;
        pełne wyjście po kliknięciu/błędzie. `build_dir` (tylko dla buildów)
        włącza zapamiętanie zajętości pamięci obok obrazu."""
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
        self._active_handle = handle
        lines = []

        def on_line(line):
            # Po Esc nie piszemy już do Log-a: ekran zaraz zniknie, a
            # kolejne wpisy tylko planują wątkowe workery, które sięgają
            # po odpiętą już aplikację (NoActiveAppError).
            if self._aborting:
                return
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
            self._active_handle = None
            # Narzędzia flashujące (J-Link/nrfutil) potrafią pisać wprost
            # do /dev/tty i zresetować tryb myszy – odnów go, zanim pojawi
            # się kolejny klikalny dialog (SWD/pomiar).
            self.app._reassert_mouse()
        # Esc przerwał komendę: wątek już się zwinął (ekran wciąż
        # zamontowany, więc bez NoActiveAppError) – wychodzimy czysto.
        if self._aborting:
            section.title = f"✗ {title} — przerwano"
            raise _Aborted()
        if rc != 0:
            section.title = f"✗ {title} — kod {rc}"
            section.collapsed = False
            raise RuntimeError(f"'{title}' zakończone błędem (kod {rc})")
        section.title = f"✓ {title}"

        # Po buildzie: podsumowanie zajętości pamięci jako tabelka Markdown
        # (od razu do skopiowania) i te same liczby zapamiętane obok obrazu,
        # żeby trafiły do dziennika. Przy flashu parser zwraca None.
        image = core.default_domain(build_dir) if build_dir else None
        if build_dir:
            core.record_memory(build_dir, lines)
        report = core.parse_memory_report(lines, image=image)
        if report is not None:
            box = Static(report, classes="mem-report", markup=False)
            box.border_title = "pamięć (Markdown — skopiuj)"
            await self.query_one("#cmds").mount(box)
            self.query_one("#cmds").scroll_end(animate=False)

    async def _ask_flash(self, name, scen, attempt):
        """Okno „wgraj / pomiń scenariusz” z trzecią opcją: zamknij je,
        żeby spokojnie obejrzeć logi builda i tabelkę pamięci. Po zamknięciu
        przebieg czeka na przycisk „Wgraj na płytkę” w nagłówku logów (albo
        na Esc). Zwraca True = wgrywamy, False = pomijamy scenariusz."""
        text = (f"[b]{_label(name, scen)}[/b]\n"
                f"{scen.get('description', '')}\n\n"
                "Programator podłączony i płytka ZASILONA\n"
                "(np. VOUT z PPK2)?")
        flash = ("Wgraj ponownie" if attempt > 1
                 else "Wgraj gotowy hex (z kasowaniem)"
                 if scen.get("hex") else "Wgraj (flash --erase)")
        btn = self.query_one("#reopen_flash", Button)
        hint = self.query_one("#hint", Static)
        while True:
            choice = await self.app.push_screen_wait(ChoiceScreen(
                text, [(flash, "flash"), ("Pomiń scenariusz", "skip"),
                       ("Zamknij okno", "close")]))
            if choice != "close":
                return choice == "flash"
            self._reopen = asyncio.Event()
            btn.display = True
            hint.update("Okno wgrywania zamknięte · „Wgraj na płytkę” — "
                        f"otwórz je z powrotem   ·   {self.HINT}")
            try:
                await self._reopen.wait()
            finally:
                self._reopen = None
            # Dopiero po powrocie z czekania – gdy Esc anulował workera,
            # ekran jest już zdjęty i pisanie do jego widgetów rzuciłoby
            # NoActiveAppError (jak przy Log-u w run_west()).
            btn.display = False
            hint.update(self.HINT)

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
                await self.run_west(cmd, workspace, f"build {name}",
                                    build_dir=build_dir)
                core.record_build(build_dir, cmd)
                built[name] = build_dir
            if will_build:
                self.note(f"Zbudowano {len(will_build)} obraz(ów).")
            elif to_build:
                self.note("Wszystkie obrazy gotowe – nic do budowania.")
            else:
                self.note("Nic do budowania (same gotowe pliki hex).")

            # --- FAZA 2: flash + pomiar ---
            # Konfliktu o sondę J-Link tu NIE sprawdzamy. Tryb ręczny mierzy
            # w nRF Connect Power Profiler, więc nRF Connect for Desktop MUSI
            # być otwarty – a jego demony 'nrfutil device list --hotplug'
            # trzymają libjlinkarm przez cały czas życia aplikacji. Dialog
            # wyskakiwałby więc przed każdym flashem, zawsze do przeklikania
            # przez „Mierz mimo to”. Ostrzeżenie o cudzej sesji J-Linka
            # zostaje tam, gdzie ma sens: przed startem przebiegu
            # autonomicznego (PowerTestApp._auto_check_jlink) i w logu
            # kroku (autorun.engine).
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
                    if not await self._ask_flash(name, scen, attempt):
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
                                                  current, notes,
                                                  build_dir=built[name]),
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
                "Dziennik: reports/pomiary.csv (lokalny, poza repo).",
                yes="OK", no=None))
            if self.app.screen is self:   # Esc mógł już zdjąć ekran
                self.app.pop_screen()
        except _Aborted:
            # Esc w trakcie komendy: proces ubity, wątek zwinięty – wróć
            # czysto do ustawień (bez komunikatu błędu).
            if self.app.screen is self:
                self.app.pop_screen()
        except (SystemExit, RuntimeError) as e:
            msg = str(e) or "przerwano"
            if not msg.startswith("BŁĄD"):
                msg = f"BŁĄD: {msg}"
            status.update(msg)
            self.note("(Esc = powrót do ustawień)")


# --- Protokoły w ustawieniach zaawansowanych karty ---
# Każda karta „Pomiar N” wybiera protokół zakładką. Serię (sweep) ma każdy
# protokół; monitor dongla tylko te, w których dogaduje się z nim drugie
# urządzenie. Thread mierzymy przez Matter: wybór tej zakładki SAM włącza
# parowanie węzła i subskrypcję atrybutu (trigger 'chip'), więc dongla tam
# nie ma – rolę drugiego urządzenia gra chip-tool. Każda zakładka ma WŁASNY
# komplet pól – symbol Kconfig serii mesha nie ma sensu w Zigbee, więc
# przełączenie protokołu nie może przenosić wpisanych wartości.
#
# Pola serii per protokół: (etykieta parametru, domyślny symbol Kconfig,
# etykieta wartości) dla pierwszej i drugiej osi. BLE Mesh zna oba swoje
# parametry LPN, więc są wpisane od razu – i w SEKUNDACH, bo tak się o nich
# myśli. Sensor interval jest w sekundach także w Kconfigu, poll interval
# w jednostkach 100 ms; przelicza autorun.plan.sweep_flag_value (×10),
# a dziennik trzyma to, co wpisane.
MESH_SWEEP = (("LPN sensor interval (symbol Kconfig)",
               "CONFIG_LPN_SENSOR_INTERVAL_S",
               "Wartości (s, po przecinku lub spacją)"),
              ("Poll interval (symbol Kconfig)",
               "CONFIG_BT_MESH_LPN_POLL_TIMEOUT",
               "Wartości (s – flaga dostaje ×10)"))
# Thread i Zigbee nie mają jeszcze ustalonych parametrów serii – pola
# zostają puste, z ogólnymi etykietami.
ANY_SWEEP = (("Parametr (symbol Kconfig)", "",
              "Wartości (po przecinku lub spacją)"),
             ("Drugi parametr (opcjonalny)", "",
              "Wartości drugiego parametru"))
#             symbol       etykieta    monitor  matter  pola serii
PROTOCOLS = (("ble_mesh", "BLE Mesh", True, False, MESH_SWEEP),
             ("thread", "Thread", False, True, ANY_SWEEP),
             ("zigbee", "Zigbee", True, False, ANY_SWEEP))
# Nowa karta startuje BEZ protokołu: żadna zakładka nie jest aktywna, więc
# pomiar jest zwykły – bez serii, bez monitora dongla, bez Mattera. Protokół
# włącza kliknięcie zakładki, a kliknięcie AKTYWNEJ zakładki z niego wychodzi
# (MeasurementCard.on_click); wpisane pola czekają wtedy w swojej zakładce.
DEFAULT_PROTOCOL = ""
# Port dongla wpisywany w karcie od razu (BLE Mesh i Zigbee).
DEFAULT_DONGLE_PORT = "/dev/ttyACM1"
# Tryby pracy triggera Matter (zakładka Thread), w kolejności z RadioSet:
#   pair – parowanie + subskrypcja (urządzenie zostaje SIT-em),
#   icd  – parowanie z rejestracją klienta check-in + subskrypcja (LIT),
#   skip – tylko subskrypcja, węzeł już jest w fabryce.
CHIP_MODES = ("pair", "icd", "skip")
CHIP_MODE_DEFAULT = "pair"


# Pola protokołu nie mają checkboxów „włącz” – liczy się to, co wpisane.
def _sweep_on(cfg):
    """Czy karta ma serię: gdy WARTOŚCI którejś osi są wypełnione. Sama nazwa
    parametru nie wystarcza, bo symbole Kconfig są w zakładce wpisane
    domyślnie (PROTOCOLS) – inaczej każda karta byłaby serią bez wartości."""
    return any(cfg.get(k) for k in ("sweep_values", "sweep_values2"))


def _sweep_axes(cfg):
    """Osie serii z konfiguracji karty -> [(parametr, wartości), …] dla
    expand_sweep(). Oś liczy się, gdy ma WARTOŚCI: puste pole wartości
    znaczy „tej osi nie ma” (sam wpisany symbol niczego nie mierzy), więc
    można sweepować dowolną z dwóch osi albo obie. Wartości bez nazwy
    parametru to przeoczenie – mówimy o tym wprost, zamiast po cichu
    ignorować połowę konfiguracji."""
    axes = []
    for suffix in ("", "2"):
        param = cfg.get(f"sweep_param{suffix}", "")
        values = cfg.get(f"sweep_values{suffix}", "")
        if not values:
            continue
        if not param:
            which = "drugiej osi serii" if suffix else "serii"
            raise ValueError(f"wartości {which} bez nazwy parametru")
        axes.append((param, values))
    if not axes:
        raise ValueError("seria bez wartości")
    return axes


class MeasurementCard(Vertical):
    """Jedna karta 'Pomiar N' w kreatorze trybu autonomicznego: scenariusz
    + czas, a reszta w zwijanych ustawieniach zaawansowanych (domyślnie
    schowanych). Tam u góry zakładki protokołu – każda ma serię (sweep),
    BLE Mesh i Zigbee dodatkowo monitor dongla, Thread pola Mattera – a pod
    nimi wspólne dla protokołów start-po-czasie / RTT / napięcie / zapis /
    próbkowanie. Czyta/ustawia własną konfigurację, nie dotyka innych kart."""

    def __init__(self, uid, scenarios, number, config=None, collapsed=False):
        super().__init__(classes="measurement-card", id=f"card_{uid}")
        self.uid = uid
        self.scenarios = scenarios
        self.number = number
        self._config = config or {}
        self.collapsed = collapsed
        # Protokół aktywny PRZED bieżącym kliknięciem – patrz on_mouse_down.
        self._proto_before_click = ""

    def compose(self):
        c = self._config
        opts = [(_label(n, s), n) for n, s in self.scenarios.items()]
        with Horizontal(classes="card-head"):
            yield CardTitle(f"▼ {self.number}", classes="card-title")
            # Krotność w nagłówku, nie w zaawansowanych: widać ją także na
            # zwiniętej karcie, więc czas całego planu da się oszacować
            # jednym spojrzeniem na listę pomiarów.
            yield RepeatButton(c.get("repeat", 1))
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
            yield Input(value=c.get("duration", ""), placeholder="np. 20m",
                        classes="card-duration")
            with Collapsible(title="Ustawienia zaawansowane", collapsed=True,
                             classes="card-adv"):
                # Protokół u góry: pola serii, monitora dongla i Mattera
                # siedzą w zakładkach, bo każde z nich ma sens tylko w
                # swoim protokole. Reszta ustawień (start, RTT, napięcie,
                # zapis, próbkowanie) zostaje POD zakładkami – to sprzęt
                # i pomiar, wspólne dla protokołów.
                active = c.get("protocol", DEFAULT_PROTOCOL)
                with TabbedContent(initial=active, classes="card-proto"):
                    for proto, label, monitor, matter, sweep in PROTOCOLS:
                        with TabPane(label, id=proto):
                            # Pola wypełniamy tylko w zakładce, z której
                            # config pochodzi – reszta protokołów startuje
                            # pusta, żeby karta nie podsuwała symbolu
                            # Kconfig z cudzego stosu. Domyślne symbole
                            # (BLE Mesh) idą z PROTOCOLS, więc są na miejscu
                            # także w zakładce nieaktywnej.
                            yield from self._protocol_fields(
                                c if proto == active else {}, monitor, matter,
                                sweep)
                # Start po czasie i konsola RTT – SCHOWANE z widoku w trybach
                # protokołów, ale wciąż w drzewie: plan czyta ich wartości
                # (domyślnie wyłączone), a wcześniejsze karty i testy dalej
                # działają. Odsłonięcie to zdjęcie .display = False
                # w _sync_advanced().
                with Vertical(classes="card-hidden-adv"):
                    # Bez startu po czasie i tak czekamy MIN_START_DELAY_S na
                    # rozruch płytki; wpisany czas nie dokłada się do tych
                    # sekund, tylko je zastępuje (liczy się większy).
                    yield Check("Start pomiaru po czasie od wgrania (np. 20s)",
                                value=c.get("delay_on", False),
                                classes="card-delay-on")
                    yield Input(value=c.get("delay_s", "20s"),
                                placeholder="np. 30s", classes="card-delay-s")
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
                        yield Label("Napięcie (V, 1.8–3.6):")
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

    @staticmethod
    def _protocol_fields(c, monitor, matter, sweep):
        """Zawartość jednej zakładki protokołu. Każda ma serię (sweep) –
        etykiety i domyślne symbole bierze z `sweep` (PROTOCOLS), bo BLE Mesh
        zna nazwy swoich parametrów, a Thread i Zigbee jeszcze nie. Monitor
        dongla tylko przy `monitor`, pola Mattera tylko przy `matter`. Klasy
        pól powtarzają się między zakładkami – dlatego pytamy o nie zawsze
        przez konkretny panel (MeasurementCard.field), nigdy przez samą
        kartę."""
        # Seria (sweep): jedna karta -> wiele pomiarów "N.1, N.2, …", każdy
        # budowany z inną flagą -DCONFIG_...=<wartość>. Pola są od razu
        # gotowe do wpisania – wpisane WARTOŚCI włączają serię (sam symbol
        # nie, bo bywa wpisany domyślnie), puste zostawiają jeden pomiar.
        (param1, default1, values1), (param2, default2, values2) = sweep
        yield Label(f"{param1}:")
        yield Input(value=c.get("sweep_param", default1),
                    placeholder="np. CONFIG_MOJ_PARAMETR",
                    classes="card-sweep-param")
        yield Label(f"{values1}:")
        yield Input(value=c.get("sweep_values", ""),
                    placeholder="np. 10, 60, 300 — pusto = bez tej osi",
                    classes="card-sweep-values")
        # Druga oś jest OPCJONALNA: wypełnione wartości dają iloczyn
        # kartezjański (3 × 2 = 6 pomiarów), puste – serię po jednej osi.
        yield Label(f"{param2}:")
        yield Input(value=c.get("sweep_param2", default2),
                    placeholder="np. CONFIG_MOJ_PARAMETR",
                    classes="card-sweep-param2")
        yield Label(f"{values2}:")
        yield Input(value=c.get("sweep_values2", ""),
                    placeholder="np. 60, 120, 200 — pusto = bez tej osi",
                    classes="card-sweep-values2")
        if matter:
            yield from MeasurementCard._matter_fields(c)
        if not monitor:
            return
        # Monitor dongla – logi z osobnego urządzenia (np. węzeł Friend).
        # Widoczny przed i podczas pomiaru; wpisany port włącza monitor,
        # a wpisany fragment logu – start pomiaru po tym logu. Oddzielony
        # od serii samym odstępem (bez nagłówka), więc kontener istnieje
        # tylko po to, żeby ten odstęp dało się ustawić w CSS.
        with Vertical(classes="card-monitor-box"):
            # Port wpisany na sztywno (nie placeholder) – dongiel siedzi u nas
            # zawsze na tym samym /dev/ttyACM1, więc monitor ma być włączony
            # od razu. Gdy dongla nie ma, silnik pisze „monitor niedostępny”
            # i mierzy dalej; pomiar przerywa tylko wtedy, gdy to z tego logu
            # miał ruszyć start.
            yield Label("Port dongla:")
            yield Input(value=c.get("serial_port", DEFAULT_DONGLE_PORT),
                        placeholder=DEFAULT_DONGLE_PORT,
                        classes="card-serial-port")
            yield Label("Start pomiaru po logu (zawiera tekst):")
            yield Input(value=c.get("serial_pattern", ""),
                        placeholder="np. Friendship z LPN nawiazany",
                        classes="card-serial-pattern")

    @staticmethod
    def _matter_fields(c):
        """Pola Mattera (zakładka Thread). Sam wybór tej zakładki włącza
        po flashu parowanie węzła i subskrypcję atrybutu, a pomiar rusza na
        PIERWSZYM raporcie – dlatego pola mają wpisane wartości, nie
        placeholdery: karta ma działać od razu po przełączeniu protokołu.
        Oddzielone od serii samym odstępem (jak monitor dongla), więc
        kontener istnieje tylko po to, żeby ustawić ten odstęp w CSS."""
        with Vertical(classes="card-chip-box"):
            with Horizontal(classes="card-row"):
                with Vertical(classes="card-col"):
                    yield Label("Node ID:")
                    yield Input(value=c.get("chip_node_id", "5"),
                                placeholder="5", classes="card-chip-node")
                with Vertical(classes="card-col"):
                    yield Label("Discriminator:")
                    yield Input(value=c.get("chip_discriminator", "3840"),
                                placeholder="3840", classes="card-chip-disc")
                with Vertical(classes="card-col"):
                    yield Label("Setup PIN:")
                    yield Input(value=c.get("chip_pin", "20202021"),
                                placeholder="20202021",
                                classes="card-chip-pin")
            # Dataset (długi hex, ~222 znaki) w TextArea z zawijaniem – Input
            # tej długości renderuje się pusty, bo zakładka Thread powstaje
            # niewidoczna (aktywne jest BLE Mesh) i pole nie zna swojej
            # szerokości.
            yield Label("Dataset Thread (hex):")
            yield TextArea(c.get("chip_dataset", CHIP_DATASET_DEFAULT),
                           soft_wrap=True, compact=True,
                           show_line_numbers=False,
                           classes="card-chip-dataset")
            # Cluster na pełną szerokość – „temperaturemeasurement” nie mieści
            # się w wąskiej kolumnie (z tego samego powodu byłby pusty).
            yield Label("Cluster:")
            yield Input(value=c.get("chip_cluster", "temperaturemeasurement"),
                        classes="card-chip-cluster")
            with Horizontal(classes="card-row"):
                with Vertical(classes="card-col"):
                    yield Label("Atrybut:")
                    yield Input(value=c.get("chip_attribute",
                                            "measured-value"),
                                classes="card-chip-attr")
                with Vertical(classes="card-col"):
                    yield Label("Endpoint:")
                    yield Input(value=c.get("chip_endpoint", "1"),
                                placeholder="1", classes="card-chip-endpoint")
            with Horizontal(classes="card-row"):
                with Vertical(classes="card-col"):
                    yield Label("Min interval [s]:")
                    yield Input(value=c.get("chip_min", "1"),
                                placeholder="1", classes="card-chip-min")
                with Vertical(classes="card-col"):
                    yield Label("Max interval [s]:")
                    yield Input(value=c.get("chip_max", "60"),
                                placeholder="60", classes="card-chip-max")
                with Vertical(classes="card-col"):
                    yield Label("Timeout 1. wartości:")
                    yield Input(value=c.get("chip_timeout", "120s"),
                                placeholder="120s",
                                classes="card-chip-timeout")
            # Trzy tryby wykluczają się nawzajem, więc jeden wybór, a nie
            # osobne checkboxy: rejestracja ICD idzie WYŁĄCZNIE w trakcie
            # commissioningu, więc „z rejestracją” i „już sparowany” nie mogą
            # być zaznaczone naraz. Przy wyborze nie da się tego pomylić.
            # RadioSet, nie Select – zakładka Thread powstaje niewidoczna
            # (aktywne jest BLE Mesh), a Select zamontowany jako display:none
            # nie tworzy overlaya (patrz komentarz przy datasecie).
            yield Label("Co robimy po flashu:")
            mode = c.get("chip_mode", CHIP_MODE_DEFAULT)
            with RadioSet(classes="card-chip-mode"):
                yield RadioButton("Parowanie + subskrypcja",
                                  value=mode == "pair",
                                  classes="chip-mode-pair")
                yield RadioButton(
                    "Parowanie z rejestracją ICD + subskrypcja (tryb LIT)",
                    value=mode == "icd", classes="chip-mode-icd")
                yield RadioButton(
                    "Tylko subskrypcja (węzeł już sparowany)",
                    value=mode == "skip", classes="chip-mode-skip")

    def on_mount(self):
        # Post-mount: dopiero teraz ukrywamy zaawansowane pola i (ewentualnie)
        # zwijamy kartę – overlaye Selectów już istnieją, więc bezpiecznie.
        self._sync_advanced()
        self.query_one(".card-body").display = not self.collapsed
        # Karta bez protokołu: Tabs w swoim on_mount SAM podświetla pierwszą
        # zakładkę, więc pustego stanu nie da się podać w compose – cofamy to
        # po zamontowaniu całego drzewa (call_after_refresh, nie on_mount, bo
        # kolejność montowania rodzica i dzieci nie jest gwarantowana).
        if not self._config.get("protocol", DEFAULT_PROTOCOL):
            self.call_after_refresh(self.set_protocol, "")
        self._refresh_title()

    def on_checkbox_changed(self, event):
        # Checkboxy karty (start-po-czasie / RTT) sterują widocznością swoich
        # pól – nie puszczamy zdarzenia wyżej (App liczy tylko scen-check).
        self._sync_advanced()
        event.stop()

    def on_button_pressed(self, event):
        # Krotność obsługujemy TU i zatrzymujemy zdarzenie – klik zmienia
        # tylko tę kartę. Przyciski „Zastosuj do …” zostawiamy w spokoju:
        # bez event.stop() lecą dalej, do App.on_button_pressed.
        if isinstance(event.button, RepeatButton):
            event.button.bump()
            # Krotność mnoży liczbę pomiarów, więc zmienia i tytuł tej karty
            # (gdy zwinięta), i sumę czasów pod listą.
            self._refresh_title()
            self.app._refresh_total()
            event.stop()

    def on_tabbed_content_tab_activated(self, event):
        # Każda zakładka ma własne pola serii, więc zmiana protokołu zmienia
        # też serię – dopisek o niej w tytule i suma czasów muszą za tym
        # nadążyć (inna zakładka = inna liczba kombinacji).
        self._refresh_title()
        self.app._refresh_total()
        event.stop()

    def on_tabbed_content_cleared(self, event):
        # Wyjście z protokołu (żadna zakładka nie jest aktywna) też zmienia
        # serię – karta staje się zwykłym pomiarem.
        self._refresh_title()
        self.app._refresh_total()
        event.stop()

    def on_mouse_down(self, event):
        """Migawka aktywnego protokołu PRZED kliknięciem. Klik w zakładkę
        aktywuje ją, zanim Click dojdzie do karty (Tabs konsumuje Tab.Clicked
        pierwszy), więc bez tej migawki nie da się odróżnić „wybrałem inną
        zakładkę” od „kliknąłem tę, która już była aktywna”."""
        self._proto_before_click = self.protocol()

    def on_click(self, event):
        """Klik w AKTYWNĄ zakładkę protokołu = wyjście z trybu: żadna zakładka
        nie jest aktywna i karta jest zwykłym pomiarem (bez serii, monitora
        i Mattera). Wpisane pola zostają w swojej zakładce i wracają po
        ponownym kliknięciu. Klik w inną zakładkę zmienia protokół jak
        dotąd – tym zajmuje się sam TabbedContent."""
        proto = self._clicked_protocol(event.screen_offset)
        if proto is None or proto != self._proto_before_click:
            return
        self.set_protocol("")
        event.stop()

    def _clicked_protocol(self, screen_offset):
        """Protokół, w którego ZAKŁADKĘ (nie panel) trafił klik, albo None.
        Pytamy o region zakładki, nie o widget zdarzenia, bo tak samo robi
        Textual przy klikaniu podkreślenia zakładek."""
        tabs = self.query_one(".card-proto", TabbedContent)
        for proto, *_ in PROTOCOLS:
            tab = tabs.get_tab(proto)
            if screen_offset in tab.region:
                return proto
        return None

    def set_protocol(self, protocol):
        """Ustaw protokół karty; '' = żaden (wyjście z trybu)."""
        self.query_one(".card-proto", TabbedContent).active = protocol
        self._refresh_title()

    def _sync_advanced(self):
        self.query_one(".card-delay-s").display = \
            self.query_one(".card-delay-on", Checkbox).value
        self.query_one(".card-rtt-box").display = \
            self.query_one(".card-rtt-on", Checkbox).value
        # Chowamy dopiero tutaj (post-mount), a nie przez CSS: Select
        # zamontowany od razu jako display:none nie tworzy overlaya.
        self.query_one(".card-hidden-adv").display = False

    def protocol(self):
        """Symbol wybranego protokołu ('ble_mesh' / 'thread' / 'zigbee')
        albo '' – żaden, czyli karta jest zwykłym pomiarem."""
        return str(self.query_one(".card-proto", TabbedContent).active)

    def field(self, selector, protocol=None):
        """Pole protokołu (domyślnie wybranego) albo None, gdy ten protokół
        go nie ma – Thread mierzymy bez dongla, więc nie ma tam pól monitora,
        a BLE Mesh i Zigbee nie mają pól Mattera. None także wtedy, gdy żaden
        protokół nie jest aktywny: nie ma wtedy zakładki, o którą pytać.
        Każda zakładka trzyma własny komplet pól o tych samych klasach,
        dlatego pytamy przez konkretny panel, a nie przez kartę. Bez
        wymuszania typu widgetu – w zakładkach są nie tylko Inputy, ale i
        TextArea (dataset) oraz Checkbox („węzeł już sparowany”)."""
        proto = self.protocol() if protocol is None else protocol
        if not proto:
            return None
        pane = self.query_one(".card-proto", TabbedContent).get_pane(proto)
        found = pane.query(selector)
        return found.first() if found else None

    def _field_value(self, selector):
        widget = self.field(selector)
        return widget.value.strip() if widget is not None else ""

    def _field_text(self, selector):
        """Treść TextArei protokołu bez białych znaków. Dataset to jeden ciąg
        hex, a TextArea przyjmuje Enter – więc sklejamy, cokolwiek wpisano."""
        widget = self.field(selector)
        return "".join(widget.text.split()) if widget is not None else ""

    def _field_flag(self, selector):
        """Checkbox protokołu; brak pola w tym protokole = nie zaznaczony."""
        widget = self.field(selector)
        return bool(widget.value) if widget is not None else False

    def chip_mode(self, protocol=None):
        """Wybrany tryb triggera Matter ('pair' | 'icd' | 'skip').
        Poza zakładką Thread RadioSetu nie ma – wtedy domyślny."""
        rs = self.field(".card-chip-mode", protocol)
        if rs is None:
            return CHIP_MODE_DEFAULT
        idx = rs.pressed_index
        return CHIP_MODES[idx] if 0 <= idx < len(CHIP_MODES) \
            else CHIP_MODE_DEFAULT

    def set_chip_mode(self, mode, protocol=None):
        """Zaznacz tryb. RadioSet nie ma settera na pressed_index – wybór
        ustawia się przez .value przycisku, a RadioSet sam odznacza resztę."""
        rs = self.field(".card-chip-mode", protocol)
        if rs is None:
            return
        cls = {"pair": ".chip-mode-pair", "icd": ".chip-mode-icd",
               "skip": ".chip-mode-skip"}.get(mode)
        if cls is None:
            return
        rs.query_one(cls, RadioButton).value = True

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
        """Nagłówek karty. Rozwinięta pokazuje sam numer – reszta stoi
        w polach pod nim. Zwinięta dokłada scenariusz, wartości serii
        i czas, bo to jedyny wiersz, jaki wtedy widać:

            ▶ 1  LPN OFF · (10, 60, 300), (5, 10) · 6 × 20:00 = 2:00:00

        Krotności NIE piszemy – mówi ją przycisk 'xN' obok, a liczba
        pomiarów przed '×' i tak ją już zawiera."""
        title = self.query_one(".card-title", CardTitle)
        if not self.collapsed:
            title.update(f"▼ {self.number}")
            return
        scen = self._scenario()
        if not scen:
            title.update(f"▶ {self.number}  (wybierz scenariusz)")
            return
        parts = [_label(scen, self.scenarios.get(scen, {}))]
        parts += [p for p in (self._sweep_title(), self._time_title()) if p]
        title.update(f"▶ {self.number}  " + " · ".join(parts))

    def _sweep_title(self):
        """Wartości serii do tytułu zwiniętej karty: '(10, 60, 300), (5, 10)'
        – jedna para nawiasów na oś, w kolejności osi. Pokazujemy WARTOŚCI,
        nie symbole Kconfig: symbol bywa wpisany domyślnie (PROTOCOLS), więc
        nie mówi nic nowego, a to wartości decydują, ile pomiarów wyjdzie.
        Wypisujemy je po sparsowaniu, żeby '10,60' i '10, 60' wyglądały tak
        samo. Pusty, gdy pola wartości wybranego protokołu są puste albo gdy
        żaden protokół nie jest aktywny. Osie liczymy tak samo jak plan
        (_sweep_axes), żeby tytuł nie obiecywał pomiarów, których nie będzie."""
        from autorun.plan import parse_sweep_values
        try:
            cfg = self.get_config()
            if not _sweep_on(cfg):
                return ""
            axes = [parse_sweep_values(v) for _p, v in _sweep_axes(cfg)]
        except ValueError:
            # Wartości bez nazwy parametru – plan to odrzuci przed startem,
            # a tytuł mówi wprost, czego brakuje.
            return "(brak parametru)"
        except Exception:
            return ""
        return ", ".join("(" + ", ".join(str(v) for v in vals) + ")"
                         for vals in axes)

    def _time_title(self):
        """Czas karty do tytułu zwiniętej karty: samo okno pomiaru przy
        jednym pomiarze, a przy serii/powtórkach mnożenie z sumą
        ('6 × 20:00 = 2:00:00'). Liczymy TYLKO okna pomiarowe – build,
        flash i triggery zależą od sprzętu i cache'u, więc doliczone
        dawałyby liczbę, która i tak się nie sprawdzi. Puste, gdy czas
        nie jest jeszcze wpisany albo nie jest czasem."""
        secs = self.duration_s()
        if secs is None:
            return ""
        n = self.plan_size()
        if n <= 1:
            return _fmt_hms(secs)
        return f"{n} × {_fmt_hms(secs)} = {_fmt_hms(secs * n)}"

    def duration_s(self):
        """Okno pomiaru karty w sekundach albo None, gdy pole jest puste
        lub nie jest czasem (kreator zgłosi to dopiero przy Starcie)."""
        from autorun.plan import parse_duration
        text = self.query_one(".card-duration", Input).value.strip()
        if not text:
            return None
        try:
            return parse_duration(text)
        except ValueError:
            return None

    def plan_size(self):
        """Ile POMIARÓW da ta karta: kombinacje serii × krotność. Tyle
        wierszy trafi do dziennika i tyle okien pomiarowych zajmie karta
        w przebiegu. Niepełną serię liczymy jak brak serii – plan odrzuci
        ją przed startem z konkretnym błędem, a tytuł nie ma w tym czasie
        udawać, że wie lepiej."""
        from autorun.plan import parse_sweep_values
        combos = 1
        try:
            cfg = self.get_config()
            if _sweep_on(cfg):
                for _p, values in _sweep_axes(cfg):
                    combos *= len(parse_sweep_values(values))
        except Exception:
            combos = 1
        return combos * self.repeat()

    def plan_seconds(self):
        """Suma okien pomiarowych karty (0.0, gdy czas nie jest wpisany)."""
        secs = self.duration_s()
        return 0.0 if secs is None else secs * self.plan_size()

    def repeat(self):
        """Krotność z przycisku w nagłówku (x1…x5)."""
        return self.query_one(".card-repeat", RepeatButton).count

    def _scenario(self):
        """Wybrany scenariusz albo '' gdy blank (sentinel zależny od wersji
        Textual – rozpoznajemy blank po tym, że nie jest znaną nazwą)."""
        scen = self.query_one(".card-scenario", Select).value
        return scen if scen in self.scenarios else ""

    def refresh_options(self):
        """Przeładuj listę scenariuszy w Selecie po zmianie manifestu
        („Dodaj kod” / usunięcie wpisu). Select dostaje opcje raz, przy
        tworzeniu karty, więc bez tego nowego scenariusza nie dałoby się
        wybrać w już istniejącej karcie. set_options() kasuje zaznaczenie –
        odtwarzamy je, o ile wybrany scenariusz nadal istnieje."""
        select = self.query_one(".card-scenario", Select)
        current = select.value
        select.set_options([(_label(n, s), n)
                            for n, s in self.scenarios.items()])
        if current in self.scenarios:
            select.value = current
        self._refresh_title()

    def get_config(self):
        return {
            "scenario": self._scenario(),
            "protocol": self.protocol(),
            # Pola protokołów: bierzemy TYLKO z wybranej zakładki. To, co
            # wpisane w pozostałych, czeka tam na swój protokół i nie ma
            # wpływu na ten pomiar.
            "sweep_param": self._field_value(".card-sweep-param"),
            "sweep_values": self._field_value(".card-sweep-values"),
            "sweep_param2": self._field_value(".card-sweep-param2"),
            "sweep_values2": self._field_value(".card-sweep-values2"),
            "duration": self.query_one(".card-duration", Input).value.strip(),
            "delay_on": self.query_one(".card-delay-on", Checkbox).value,
            "delay_s": self.query_one(".card-delay-s", Input).value.strip(),
            "rtt_on": self.query_one(".card-rtt-on", Checkbox).value,
            "rtt_mode": self.query_one(".card-rtt", Select).value,
            "pattern": self.query_one(".card-pattern", Input).value.strip(),
            "serial_port": self._field_value(".card-serial-port"),
            "serial_pattern": self._field_value(".card-serial-pattern"),
            # Matter: bez osobnego „włącz” – decyduje protokół. Poza
            # zakładką Thread pól nie ma, więc wychodzą puste.
            "chip_node_id": self._field_value(".card-chip-node"),
            "chip_discriminator": self._field_value(".card-chip-disc"),
            "chip_pin": self._field_value(".card-chip-pin"),
            "chip_dataset": self._field_text(".card-chip-dataset"),
            "chip_cluster": self._field_value(".card-chip-cluster"),
            "chip_attribute": self._field_value(".card-chip-attr"),
            "chip_endpoint": self._field_value(".card-chip-endpoint"),
            "chip_min": self._field_value(".card-chip-min"),
            "chip_max": self._field_value(".card-chip-max"),
            "chip_timeout": self._field_value(".card-chip-timeout"),
            "chip_mode": self.chip_mode(),
            "voltage": self.query_one(".card-voltage", Input).value.strip(),
            "storage": self.query_one(".card-storage", Select).value,
            "sample_rate": self.query_one(".card-rate", Select).value,
            "repeat": self.query_one(".card-repeat", RepeatButton).count,
        }

    def apply_shared(self, cfg):
        """Ustaw wszystko OPRÓCZ scenariusza (dla 'Zastosuj do wszystkich')."""
        self.query_one(".card-proto", TabbedContent).active = cfg["protocol"]
        # Pola przepisujemy do zakładki protokołu Z KONFIGURACJI – zakładki
        # pozostałych protokołów zostają nietknięte, bo ich wartości nie mają
        # sensu poza własnym stosem. Karta bez protokołu nie ma czego
        # przepisywać (field() zwraca None): przenosi się samo wyjście z trybu.
        for key, selector in (("sweep_param", ".card-sweep-param"),
                              ("sweep_values", ".card-sweep-values"),
                              ("sweep_param2", ".card-sweep-param2"),
                              ("sweep_values2", ".card-sweep-values2"),
                              ("serial_port", ".card-serial-port"),
                              ("serial_pattern", ".card-serial-pattern"),
                              ("chip_node_id", ".card-chip-node"),
                              ("chip_discriminator", ".card-chip-disc"),
                              ("chip_pin", ".card-chip-pin"),
                              ("chip_cluster", ".card-chip-cluster"),
                              ("chip_attribute", ".card-chip-attr"),
                              ("chip_endpoint", ".card-chip-endpoint"),
                              ("chip_min", ".card-chip-min"),
                              ("chip_max", ".card-chip-max"),
                              ("chip_timeout", ".card-chip-timeout")):
            widget = self.field(selector, cfg["protocol"])
            if widget is not None:
                widget.value = cfg[key]
        # Dataset osobno – TextArea trzyma treść w .text, nie w .value.
        dataset = self.field(".card-chip-dataset", cfg["protocol"])
        if dataset is not None:
            dataset.text = cfg["chip_dataset"]
        # Tryb Mattera osobno – RadioSet nie ma .value (patrz set_chip_mode).
        self.set_chip_mode(cfg["chip_mode"], cfg["protocol"])
        self.query_one(".card-duration", Input).value = cfg["duration"]
        self.query_one(".card-delay-on", Checkbox).value = cfg["delay_on"]
        self.query_one(".card-delay-s", Input).value = cfg["delay_s"]
        self.query_one(".card-rtt-on", Checkbox).value = cfg["rtt_on"]
        self.query_one(".card-rtt", Select).value = cfg["rtt_mode"]
        self.query_one(".card-pattern", Input).value = cfg["pattern"]
        self.query_one(".card-voltage", Input).value = cfg["voltage"]
        self.query_one(".card-storage", Select).value = cfg["storage"]
        self.query_one(".card-rate", Select).value = cfg["sample_rate"]
        self.query_one(".card-repeat", RepeatButton).count = cfg["repeat"]
        self._sync_advanced()
        self._refresh_title()


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
            yield Static("[b]Połączenie z PPK2[/b]", classes="dialog-text")
            yield Static("Napięcie: ustawiane per pomiar (domyślnie 3.0 V, "
                         "limit 1.8–3.6 V).", classes="dialog-text")
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
        self._saved = False            # kliknięto "Zapisz pomiary"?
        self._n_steps = len(plan.steps)
        # Aktywna zwijana sekcja komendy (build/flash) + jej spinner.
        self._active_section = None
        self._active_log = None
        # Linie bieżącej komendy – po buildzie wyłuskujemy z nich tabelkę
        # zajętości pamięci (jak w trybie ręcznym).
        self._active_lines = None
        self._spinner = None
        self._spin_i = 0

    def compose(self):
        yield Static("", id="status")
        # Tabelka zakończonych pomiarów (na górze) – wypełnia się po każdym
        # kroku, a jego okna build/flash znikają.
        yield DataTable(id="results", zebra_stripes=False,
                        cursor_type="none")
        # Surowe dane sesji (raw.bin/tiery) zajmują dużo miejsca, więc ich
        # zachowanie jest opcjonalne – przycisk pod tabelą wyników pokazuje
        # się dopiero w widoku "zakończono".
        yield Button("Zapisz pomiary", id="save_results")
        yield VerticalScroll(id="cmds")
        # Monitor dongla (serial) – logi widoczne przed i podczas pomiaru.
        with Vertical(id="dongle-panel"):
            yield Static("Monitor dongla", id="dongle-title")
            yield Log(id="dongle-log")
        # Duże okno pomiaru POD logami build/flash.
        with Vertical(id="measure-panel"):
            yield Static("", id="measure-head")
            with Horizontal(id="measure-row"):
                yield Static("", id="measure-avg")
                yield Static("", id="measure-remain")
            yield Static("", id="measure-inst")
            yield Button("Stop", id="stop_measure")
        yield Static("Esc — przerwij pomiar", id="hint")

    def on_mount(self):
        table = self.query_one("#results", DataTable)
        table.add_columns("#", "Scenariusz", "Parametr", "Średni", "Max",
                          "Czas")
        table.display = False          # pokaże się z pierwszym wynikiem
        self.query_one("#measure-panel").display = False
        self.query_one("#dongle-panel").display = False
        self.query_one("#save_results").display = False   # dopiero po planie
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
        self._active_lines = []
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
        if self._active_lines is not None:
            self._active_lines.append(text)
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
        lines = self._active_lines or []
        self._active_section = None
        self._active_log = None
        self._active_lines = None
        # Po buildzie: podsumowanie zajętości pamięci jako tabelka Markdown –
        # ta sama co w trybie ręcznym. Przy flashu parser zwraca None. Tu nie
        # znamy katalogu builda (zdarzenia niosą tylko linie), więc przy
        # sysbuildzie z kilkoma obrazami pokazujemy wszystkie tabelki; do
        # dziennika trafia właściwa – wybiera ją silnik (core.record_memory).
        report = core.parse_memory_report(lines)
        if report is not None:
            box = Static(report, classes="mem-report", markup=False)
            box.border_title = "pamięć (Markdown — skopiuj)"
            cmds = self.query_one("#cmds")
            cmds.mount(box)
            cmds.scroll_end(animate=False)
        # Narzędzia flashujące (J-Link/nrfutil) potrafią pisać wprost do
        # /dev/tty i zresetować tryb myszy – odnów go, jak w run_west().
        self.app._reassert_mouse()

    def action_cancel(self):
        if self._done:
            self._exit_when_done()
            return
        self.cancel.set()
        self.query_one("#status", Static).update(
            "Przerywam po bieżącym odczycie… (Esc jeszcze raz = powrót)")

    def on_button_pressed(self, event):
        if event.button.id == "stop_measure":
            self._toggle_pause()
        elif event.button.id == "save_results":
            self._save_results()

    # --- opcjonalny zapis surowych danych sesji ---

    def _run_dir_rel(self):
        """Ścieżka katalogu przebiegu względem repo (albo absolutna)."""
        try:
            return str(Path(self.run_dir).relative_to(core.ROOT))
        except (ValueError, TypeError):
            return str(self.run_dir)

    def _save_results(self):
        """Zachowaj surowe dane sesji tego przebiegu (domyślnie kasowane
        przy wyjściu). Wiersze w reports/pomiary.csv i tak już są – tu tylko
        blokujemy skasowanie ciężkich katalogów raw.bin/tiery."""
        self._saved = True
        btn = self.query_one("#save_results", Button)
        btn.label = "Zapisano ✓"
        btn.disabled = True
        self.query_one("#status", Static).update(
            f"Zapisano pomiary: {self._run_dir_rel()}/")
        self.query_one("#hint", Static).update("Esc — powrót")

    def _exit_when_done(self):
        """Wyjście z widoku zakończenia. Jeśli nie zapisano, pytamy, czy
        skasować surowe dane sesji (zajmują dużo miejsca)."""
        if self._saved or not (self.run_dir and Path(self.run_dir).is_dir()):
            self.app.pop_screen()
            return
        self.app.push_screen(
            ConfirmScreen(
                f"Nie zapisano surowych danych sesji "
                f"({self._run_dir_rel()}/).\n"
                "Usunąć je i zwolnić miejsce, czy zostawić na dysku?\n"
                "(Wiersze w reports/pomiary.csv zostają tak czy inaczej.)",
                yes="Usuń", no="Zostaw", danger=True),
            callback=self._discard_decided)

    def _discard_decided(self, delete):
        if delete and self.run_dir:
            shutil.rmtree(self.run_dir, ignore_errors=True)
        self.app.pop_screen()

    def _toggle_pause(self):
        """Stop = pauza pomiaru i czasu; drugi klik = wznów ten sam pomiar."""
        btn = self.query_one("#stop_measure", Button)
        if self.pause.is_set():
            self.pause.clear()
            btn.label = "Stop"
        else:
            self.pause.set()
            btn.label = "Wznów"

    _fmt_uA = staticmethod(_fmt_uA)

    _fmt_time = staticmethod(_fmt_hms)

    @classmethod
    def _fmt_countdown(cls, s):
        """Czas POZOSTAŁY – zaokrąglany w GÓRĘ. Przy round() wartość
        odczytana chwilę po pełnej sekundzie (np. 9,6 s) pokazywała się
        jako ta sama liczba co poprzedni odczyt i sekunda na ekranie
        „stała” dwa takty; ceil daje równe 10, 9, 8, …, a zero pojawia
        się dopiero, gdy naprawdę nie ma już czasu."""
        return cls._fmt_time(math.ceil(max(0.0, s)))

    @staticmethod
    def _step_label(ev):
        """Etykieta kroku do wyświetlenia: 'N.M' dla serii, 'N/k' dla
        powtórki, inaczej numer."""
        return ev.data.get("label") or str(ev.step)

    def _step_of(self, ev):
        """'3/2 z 6' – etykieta kroku i liczba kroków planu. Ukośnik jest
        zajęty przez numer powtórki, więc miejsca w planie NIE dopisujemy
        drugim ukośnikiem ('3/2/6' czytało się jak trzy poziomy)."""
        return f"{self._step_label(ev)} z {self._n_steps}"

    @staticmethod
    def _sweep_str(sweep):
        """Pary parametr=wartość serii do pokazania (bez prefiksu CONFIG_,
        osie po przecinku: 'A=10, B=100'); pusto, gdy krok nie jest z serii."""
        pairs = []
        for axis in sweep or ():
            param = (axis.get("param") or "").removeprefix("CONFIG_")
            if param:
                pairs.append(f"{param}={axis.get('value') or ''}")
        return ", ".join(pairs)

    # --- most zdarzenia silnika -> UI (wołane z wątku) ---

    def _on_event(self, ev):
        status = self.query_one("#status", Static)
        if ev.kind == "plan_start":
            self.run_dir = ev.data.get("run_dir")
        elif ev.kind == "phase":
            pass                                  # bez śmieci w widoku budowania
        elif ev.kind == "state":
            state_label = {"build": "budowanie", "power": "zasilanie",
                           "flash": "wgrywanie",
                           "trigger": "czekam na trigger",
                           "measure": "POMIAR", "build_failed": "build padł"
                           }.get(ev.text, ev.text)
            detail = ev.data.get("detail", "")
            sweep = self._sweep_str(ev.data.get("sweep"))
            head = f"Pomiar {self._step_label(ev)} · {ev.name}"
            if sweep:
                head += f" ({sweep})"
            status.update(f"{head} · {state_label}"
                          + (f" ({detail})" if detail else ""))
            if ev.text == "measure":
                self._start_measure_panel(ev)
        elif ev.kind == "session":
            # Wykres (osobne okno) chwilowo wyłączony – zajmiemy się później.
            self.live_session = ev.data.get("dir")
        elif ev.kind == "countdown":
            status.update(
                f"{ev.name} · start pomiaru za "
                f"[b]{self._fmt_countdown(ev.data.get('remaining_s', 0))}[/b]")
        elif ev.kind == "monitor":
            self._monitor_line(ev.text)
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
            pass          # notatki silnika nie zaśmiecają widoku budowania
        elif ev.kind == "step_done":
            self._finish_step(ev)
        elif ev.kind == "plan_done":
            self._finish(ev)

    # --- duże okno pomiaru + tabelka wyników ---

    def _start_measure_panel(self, ev):
        panel = self.query_one("#measure-panel")
        panel.display = True
        self.pause.clear()
        self.query_one("#stop_measure", Button).label = "Stop"
        sweep = self._sweep_str(ev.data.get("sweep"))
        head = f"[b]POMIAR[/b] · Pomiar {self._step_of(ev)} · {ev.name}"
        if sweep:
            head += f" · {sweep}"
        self.query_one("#measure-head", Static).update(head)
        self.query_one("#measure-avg", Static).update("[b]—[/b]")
        self.query_one("#measure-remain", Static).update("")
        self.query_one("#measure-inst", Static).update("")

    @staticmethod
    def _remaining_s(d):
        """Ile jeszcze potrwa pomiar. Liczymy z czasu ZEGAROWEGO
        (`wall_elapsed_s`), a nie z `elapsed_s` liczonego próbkami – ten
        drugi przy zgubionych próbkach zostaje w tyle i odliczanie
        „zacinało się” na tej samej sekundzie. Fallback na elapsed_s dla
        zdarzeń bez czasu zegarowego (pauza starszego silnika)."""
        elapsed = d.get("wall_elapsed_s")
        if elapsed is None:
            elapsed = d.get("elapsed_s") or 0
        return max(0.0, (d.get("duration_s") or 0) - elapsed)

    def _update_measure(self, ev):
        d = ev.data
        self.query_one("#measure-avg", Static).update(
            f"[b]{self._fmt_uA(d.get('avg_uA'))}[/b]")
        self.query_one("#measure-remain", Static).update(
            f"pozostało [b]{self._fmt_countdown(self._remaining_s(d))}[/b]")
        self.query_one("#measure-inst", Static).update(
            f"[#888888]teraz {self._fmt_uA(d.get('inst_uA'))} · "
            f"próbek {d.get('samples', 0):,}[/]")

    def _monitor_line(self, text):
        panel = self.query_one("#dongle-panel")
        if not panel.display:
            panel.display = True
        self.query_one("#dongle-log", Log).write_line(text)

    def _set_paused(self, ev, paused):
        head = self.query_one("#measure-head", Static)
        btn = self.query_one("#stop_measure", Button)
        sweep = self._sweep_str(ev.data.get("sweep"))
        tail = f" · {sweep}" if sweep else ""
        base = f"Pomiar {self._step_of(ev)} · {ev.name}{tail}"
        if paused:
            head.update(f"[b]PAUZA[/b] · {base}"
                        f"  (śr {self._fmt_uA(ev.data.get('avg_uA'))})")
            btn.label = "Wznów"
        else:
            head.update(f"[b]POMIAR[/b] · {base}")
            btn.label = "Stop"

    def _warn_lost_samples(self, d):
        """Ostrzeż, gdy PPK2 zgubiło zauważalny kawałek danych. Próg 2%
        okna – drobne braki na styku odczytów są normalne."""
        lost = d.get("lost_samples") or 0
        got = d.get("samples") or 0
        if not lost or lost < 0.02 * (lost + got):
            return
        self.note(f"[#cc9900]⚠ PPK2 zgubiło {lost:,} próbek "
                  f"({lost / (lost + got):.0%} okna) – USB nie nadążyło; "
                  "średnia policzona z tego, co dotarło.[/]")

    def _finish_step(self, ev):
        """Po pomiarze: usuń okna build/flash tego kroku i dopisz wynik do
        tabelki na górze."""
        d = ev.data
        self.query_one("#cmds").remove_children()
        table = self.query_one("#results", DataTable)
        table.display = True
        table.add_row(self._step_label(ev), ev.name,
                      self._sweep_str(d.get("sweep")) or "—",
                      self._fmt_uA(d.get("avg_uA")),
                      self._fmt_uA(d.get("max_uA")),
                      self._fmt_time(d.get("duration_s") or 0))
        self.query_one("#measure-panel").display = False
        # Zgubione próbki muszą być WIDAĆ. Okno pomiaru zamyka zegar, więc
        # braki nie objawiają się już przeciągniętym pomiarem – bez tej
        # linijki dziurawe dane wyglądałyby jak zdrowe.
        self._warn_lost_samples(d)
        # Sprzątnij monitor dongla tego kroku (następny odsłoni się sam).
        self.query_one("#dongle-log", Log).clear()
        self.query_one("#dongle-panel").display = False

    def _finish(self, ev):
        self._done = True
        status = self.query_one("#status", Static)
        status.update("Przerwano plan." if ev.data.get("cancelled")
                      else "Zakończono plan.")
        self.query_one("#measure-panel").display = False
        # Surowe dane sesji leżą na dysku tymczasowo – zostają tylko po
        # kliknięciu "Zapisz pomiary", inaczej kasujemy je przy wyjściu
        # (wiersze w reports/pomiary.csv są zapisane niezależnie).
        if self.run_dir and Path(self.run_dir).is_dir():
            self.query_one("#save_results", Button).display = True
            self.note(f"Surowe dane sesji: {self._run_dir_rel()}/")
            self.query_one("#hint", Static).update(
                "Zapisz pomiary — zachowaj surowe dane sesji · "
                "Esc — wyjście (bez zapisu dane sesji zostaną usunięte)")
        else:
            self.query_one("#hint", Static).update("Esc — powrót")

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
    .scen-icon { width: 3; height: 1; content-align: center middle; }
    .scen-icon:hover { background: #333333; color: $text; }
    .scen-arrow { color: #888888; }
    .scen-del { color: #666666; }

    /* Przełącznik trybów: same klikalne teksty (bez suwaka, bez
       animacji); aktywna strona pogrubiona i jaśniejsza. */
    #mode-toggle { height: auto; width: 72; max-width: 100%;
                   margin-bottom: 1; align: center middle; }
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
    .card-del { width: 3; content-align: center middle; color: #666666; }
    .card-del:hover { background: #333333; color: $text; }
    /* Krotność karty: przycisk bez ramki i tła, żeby nagłówek (wysoki na
       jeden wiersz) został linią tekstu, a nie paskiem widgetów. */
    .card-repeat { width: 4; min-width: 0; height: 1; padding: 0;
                   border: none; background: transparent; color: #666666;
                   text-style: none; }
    /* REGRESJA: ramkę trzeba zdjąć JAWNIE w każdym stanie. Reguły
       Button:hover / Button:focus / Button.-active są wyżej w
       specyficzności niż sama klasa, więc po najechaniu i po kliknięciu
       wracała 'border: round' – a w nagłówku wysokim na jeden wiersz
       widać wtedy górną krawędź ramki zamiast napisu ('x2' -> '╭──╮').
       Podświetlenie zostaje, tylko robi je tło, nie ramka. */
    .card-repeat:hover, .card-repeat:focus, .card-repeat.-active {
                   border: none; background: #333333; color: $text; }
    .card-repeat.on, .card-repeat.on:hover, .card-repeat.on:focus,
    .card-repeat.on.-active { color: $text; text-style: bold; }
    .card-body { height: auto; }
    .card-row { height: auto; }
    .card-col { width: 1fr; height: auto; padding-right: 1; }
    .card-delay-on, .card-rtt-on {
                     border: none; background: transparent;
                     padding: 0; height: 1; width: auto; margin-top: 1; }
    .card-delay-s, .card-voltage { width: 100%; }
    .card-sweep-param, .card-sweep-values { width: 100%; }
    .card-sweep-param2, .card-sweep-values2 { width: 100%; }
    .card-serial-port, .card-serial-pattern { width: 100%; }
    .card-rtt-box, .card-hidden-adv { height: auto; }
    /* Zakładki protokołu (BLE Mesh / Thread / Zigbee) u góry ustawień
       zaawansowanych, wyśrodkowane nad panelem. Panel bez własnego tła –
       karta ma już swoje. */
    .card-proto { height: auto; background: transparent; }
    .card-proto #tabs-list { align-horizontal: center; }
    .card-proto TabPane { height: auto; padding: 0 1; background: transparent; }
    .card-proto ContentSwitcher { height: auto; }
    /* Monitor dongla i pola Mattera oddzielone od serii pustym odstępem –
       tyle wystarczy, żeby było widać, że to osobny segment zakładki. */
    .card-monitor-box, .card-chip-box { height: auto; margin-top: 2; }
    .card-chip-cluster { width: 100%; }
    .card-chip-dataset { width: 100%; height: 6; border: round #555555; }
    /* Wybór trybu Mattera: ta sama okrągła ramka co dataset, żeby oba
       segmenty zakładki wyglądały jak jedna rodzina. Domyślna ramka
       RadioSetu jest gruba i ciągnie na siebie uwagę. */
    .card-chip-mode { width: 100%; height: auto; margin-top: 1;
                      border: round #555555; background: transparent;
                      padding: 0 1; }
    .card-chip-mode RadioButton { width: 100%; height: 1;
                                  border: none; background: transparent;
                                  padding: 0; }
    .card-chip-mode RadioButton:hover { background: #333333; }
    /* Wyraźniejszy odstęp między sekcją RTT a napięciem/zapisem. */
    .card-vs-row { margin-top: 2; }
    .card-adv { background: transparent; }
    .card-apply-row { height: auto; align-horizontal: center; }
    .card-apply, .card-apply-next { min-width: 0; margin: 1 1; }
    #add_measurement { min-width: 0; width: 72; max-width: 100%; }
    /* Czas łączny pod listą kart: przygaszony, bo to podsumowanie, nie
       pole do wypełnienia; wyrównany do prawej krawędzi kolumny kart, żeby
       liczba stała pod czasami z nagłówków, a nie pod ich numerami. */
    #total-time { width: 72; max-width: 100%; height: 1;
                  text-align: right; color: #999999; }
    /* Kalkulator poboru prądu: sekcje wyglądają jak karty pomiaru, bo są
       tym samym rodzajem rzeczy – blokiem pól z wynikiem. Szersze niż
       pozostałe panele (72), bo doszła kolumna z tabelką wartości
       oczekiwanych po prawej. */
    #calculator { width: 100; max-width: 100%; height: auto; }
    .calc-section { width: 100%; height: auto; border: round #555555;
                    background: transparent; padding: 0 1;
                    margin-bottom: 1; }
    .calc-head { margin-top: 0; }
    .calc-body { height: auto; width: 100%; }
    .calc-fields { width: 1fr; height: auto; padding-right: 2; }
    .calc-row { height: auto; width: 100%; }
    .calc-col { width: 1fr; height: auto; padding-right: 1; }
    .calc-baseline { width: 100%; }
    .calc-send-charge, .calc-send-period { width: 100%; }
    .calc-poll-charge, .calc-poll-period { width: 100%; }
    /* Wynik w ramce – wyróżniony, bo to on jest odpowiedzią sekcji.
       Jaśniejsza obwódka niż pola, żeby wzrok siadał tutaj. */
    .calc-average { height: 3; width: 100%; margin-top: 1;
                    border: round #aaaaaa; padding: 0 1; }
    .calc-average-label { width: auto; text-style: bold; }
    .calc-average-value { width: 1fr; text-align: right;
                          text-style: bold; color: $text; }
    .calc-budget { height: auto; margin-top: 1; color: #999999; }
    /* Tabelka wartości oczekiwanych: kolumna po prawej stronie sekcji.
       Nazwa pola (RefLabel) jest klikalna – kopiuje liczbę z pola obok
       (edytowalnego Input) do kalkulatora; sama tabelka niczego nie liczy. */
    .calc-ref { width: 33; height: auto; border-left: round #555555;
                padding: 0 0 0 2; }
    .calc-ref-head { color: #999999; margin-bottom: 1; }
    /* Wiersz ma wysokość pola (3: obwódka/tekst/obwódka) – nazwa dostaje
       taką samą wysokość i wyśrodkowuje w niej tekst (content-align),
       więc obie kolumny stoją na tej samej linii bez sztuczek z
       align-vertical na rzędzie. */
    .calc-ref-row { height: 3; width: 100%; margin-bottom: 1; }
    /* Obwódka jest ZAWSZE obecna (w kolorze tła – niewidoczna), żeby
       najechanie myszką tylko zmieniało jej kolor, a nie dokładało
       obwódkę i przesuwało layout o dodatkowe wiersze/kolumny. */
    .calc-ref-name { width: 1fr; height: 3; content-align: left middle;
                     text-wrap: nowrap; text-overflow: ellipsis;
                     border: round #121212; padding: 0 1; }
    .calc-ref-name:hover { border: round #aaaaaa; }
    /* Input rezerwuje domyślnie padding 0 2 (Input.DEFAULT_CSS) – przy
       szerokości 9 zostawiało to na tekst tylko 3 kolumny (9 - 2 obwódka
       - 4 padding), więc "2.4" i "1478" (3-4 znaki) się nie mieściły i
       renderowały jako obcięty, nieczytelny fragment. Szerokość dopasowana
       do najdłuższej wartości (4 cyfry) minimalizuje puste miejsce po
       prawej – Input renderuje tekst od lewej i nie ma opcji center. */
    .calc-ref-value { width: 9; padding: 0 1; }
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
    /* Akcja nieodwracalna (np. "Usuń") – czerwony hover/focus ostrzega
       przed kliknięciem, spójnie z jedynym innym wyjątkiem od
       monochromatycznej palety (błąd PPK2, #cc6666). */
    Button.danger:hover, Button.danger:focus {
        border: round #cc6666; color: #cc6666; }
    /* Rząd akcji ma taką samą szerokość jak reszta panelu (72 kolumny,
       jak #scenarios / #mode-toggle / #measurements) – przyciski
       zachowują naturalną szerokość, a odstępy między nimi (spacery
       1fr) rosną, żeby rozłożyć je równomiernie na tej szerokości
       zamiast kupić się po lewej stronie. */
    #actions { margin-top: 1; height: auto; width: 72; max-width: 100%; }
    #actions Button { min-width: 0; }
    .actions-gap { width: 1fr; height: 1; }

    #status { background: transparent; padding: 0 1; height: 1;
              text-style: bold; }
    /* Tabelka zakończonych pomiarów (na górze). */
    #results { background: transparent; height: auto; max-height: 12;
               margin: 1 1 0 1; }
    #results > .datatable--header { background: transparent;
                                    color: #888888; text-style: none; }
    /* Opcjonalny zapis surowych danych sesji – pod tabelą wyników. */
    #save_results { margin: 1 1 0 1; min-width: 18; }
    /* Nagłówek logów: kontener Horizontal ma domyślnie height:1fr, co
       zjadałoby górną połowę ekranu i spychało logi na środek – ogranicz
       go do wysokości zawartości (jeden wiersz). */
    #cmds-head { height: auto; }
    /* width:1fr jawnie: bez tego Static rozpycha się na całą szerokość
       nagłówka i wypycha przyciski poza ekran (były nieklikalne). */
    #cmds-title { width: 1fr; height: 1; color: #777777; padding: 0 1;
                  margin-top: 1; }
    /* Powrót do zamkniętego okna wgrywania – obok „Kopiuj log”. */
    #reopen_flash { margin-right: 1; }
    #cmds { padding: 0 1; height: auto; }
    /* Tryb ręczny: logi build/flash kumulują się przez cały przebieg, więc
       obszar logów jest przyklejonym do góry panelem (1fr) z własnym
       scrollem – jak w trybie autonomicznym – zamiast rozpychać i
       przewijać cały ekran (wczesne logi nie uciekają poza widok). */
    RunScreen #cmds { height: 1fr; }
    /* Monitor dongla (serial) – logi przed i podczas pomiaru. Panel
       wypełnia wolną wysokość (1fr) i jest jedynym przewijanym obszarem
       w środku ekranu – dzięki temu sam Screen nie musi się przewijać i
       nie pojawia się drugi (pionowy) suwak tuż obok suwaka logu. */
    #dongle-panel { height: 1fr; }
    #dongle-title { height: 1; color: #777777; padding: 0 1; margin-top: 1; }
    #dongle-log { height: 1fr; min-height: 8; border: round #555555;
                  background: transparent; margin: 0 1; overflow-x: auto; }
    /* Duże okno pomiaru pod logami build/flash. */
    #measure-panel { height: auto; border: round #555555; margin: 1 1;
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
    /* Rozwinięta sekcja build/flash: 30 wierszy, bo przy buildzie chodzi o
       to, żeby naraz widzieć kawałek wyjścia westa, a nie przewijać je po
       kilka linijek. Ta sama reguła obsługuje oba tryby. */
    .cmd-log { height: 30; border: round #555555; background: transparent;
               margin: 0 1 1 2; overflow-x: auto; }
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
    /* Podgląd wyników szerszy niż zwykłe dialogi – mieści komplet kolumn
       (seria/parametr/min/max) bez poziomego scrolla na typowym terminalu. */
    .dialog.results { width: 96%; max-width: 100%; }
    .results DataTable { height: 18; width: 100%; background: transparent; }

    /* Okienko „Scenariusze” (tryb autonomiczny) – ta sama lista co
       w trybie ręcznym (#scenarios), tyle że w dialogu. */
    #scen-list { border: round #555555; background: transparent;
                 height: auto; max-height: 18; overflow-y: auto;
                 padding: 0 1; }
    .scen-name { width: 1fr; height: 1; text-wrap: nowrap;
                 text-overflow: ellipsis; }
    .scen-name:hover { text-style: bold; }
    .scen-hint { color: #888888; margin-top: 1; }
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
        # Start w trybie autonomicznym: to jest tryb, w którym narzędzie
        # samo mierzy (PPK2). Ręczny zostaje pod kliknięciem w przełącznik.
        self.mode = "auto"        # standard | auto | calc
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
                yield Static("│", classes="mode-sep")
                yield ModeLabel("Kalkulator poboru prądu", "calc",
                                id="mode-label-calc", classes="mode-label")
            yield Static(LOGO, id="logo")
            yield Label("Płytka", classes="h measure-only")
            yield Select(((b["board"], n) for n, b in self.boards.items()),
                         value=default_prof, allow_blank=False, id="profile",
                         classes="measure-only")
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
            # Suma czasów zamyka sekcję pomiarów – stoi pod listą kart,
            # której dotyczy, a nad polami niezwiązanymi z czasem.
            yield Static("", id="total-time", classes="auto-only")

            # --- Kalkulator poboru prądu: trzy sekcje, po jednej na
            # protokół. Nie mierzy niczego i nie czyta zapisanych sesji –
            # liczy prąd średni z modelu I_avg = I_baseline + Q/T z liczb
            # wpisanych w polach.
            with Vertical(id="calculator", classes="calc-only"):
                for proto, label, _mon, _matter, _sweep in PROTOCOLS:
                    yield CalculatorSection(proto, label)

            yield Label("Egzemplarz płytki", classes="h measure-only")
            yield Input(placeholder="np. BTZ #2", id="sample",
                        classes="measure-only")
            yield Check("Wymuś pełny rebuild (gotowe buildy są "
                        "normalnie pomijane)", value=False, id="pristine",
                        classes="measure-only")
            yield Check("Zresetuj płytkę po wgraniu (J-Link)",
                        value=True, id="reset", classes="standard-only")
            yield Check("Przypomnij o odpięciu programatora (SWD/J-Link)",
                        value=True, id="swd_reminder",
                        classes="standard-only")
            with Horizontal(id="actions"):
                yield Button("Start", id="start", classes="measure-only")
                yield Static(classes="actions-gap measure-only")
                yield Button("Zaznacz wszystkie", id="select_all",
                             classes="standard-only")
                yield Static(classes="actions-gap standard-only")
                yield Button("Dodaj kod", id="add_fw",
                             classes="measure-only")
                yield Static(classes="actions-gap measure-only")
                # Lista scenariuszy (opis + usuwanie) tylko w trybie
                # autonomicznym – w ręcznym stoi wprost na ekranie.
                yield Button("Scenariusze", id="scenarios_btn",
                             classes="auto-only")
                yield Static(classes="actions-gap auto-only")
                yield Button("Wyniki", id="results_btn",
                             classes="measure-only")
                yield Static(classes="actions-gap measure-only")
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

    def _main_screen(self):
        """Ekran główny (pod ewentualnymi dialogami). App.query widzi tylko
        wierzchni ekran, więc listę scenariuszy, karty i przyciski ustawień
        odpytujemy tutaj – inaczej spod okienka „Scenariusze” nic byśmy nie
        znaleźli."""
        return self.screen_stack[0]

    def _scenario_in_use(self, name):
        """Powód, dla którego scenariusza nie wolno teraz usunąć, albo None.
        Blokujemy wpis wybrany w karcie „Pomiar N” kreatora i zaznaczony na
        liście trybu ręcznego (usunięcie zostawiłoby zaplanowany pomiar bez
        firmware'u), a także ostatni wpis w manifeście – bez żadnego
        scenariusza narzędzie nie wystartuje."""
        label = _label(name, self.scenarios.get(name, {}))
        if len(self.scenarios) <= 1:
            return (f"Nie można usunąć „{label}” — to ostatni scenariusz "
                    "w manifeście, a bez żadnego wpisu narzędzie się nie "
                    "uruchomi.")
        main = self._main_screen()
        reasons = []
        numbers = sorted({card.number for card in main.query(MeasurementCard)
                          if card._scenario() == name})
        if numbers:
            which = ", ".join(f"Pomiar {n}" for n in numbers)
            reasons.append(f"jest wybrany w kreatorze ({which})")
        for box in main.query(f"#check_{name}"):
            if box.value:
                reasons.append("jest zaznaczony na liście trybu ręcznego")
        if not reasons:
            return None
        return (f"Nie można usunąć „{label}” — " + " i ".join(reasons)
                + ". Zmień wybór i spróbuj ponownie.")

    def confirm_remove(self, name):
        """✕ przy scenariuszu: blokady, potwierdzenie i usunięcie wpisu."""
        blocked = self._scenario_in_use(name)
        if blocked:
            self.notify(blocked, severity="error", timeout=8)
            return

        def done(ok):
            if ok:
                self._remove_scenario(name)
        label = _label(name, self.scenarios.get(name, {}))
        self.push_screen(ConfirmScreen(
            f"[b]Usunąć scenariusz „{label}”?[/b]\n\n"
            "Wpis zniknie z manifestu (scenarios.toml albo\n"
            "prywatnego scenarios.local.toml).\n"
            "Zebrane pomiary w reports/pomiary.csv zostają.",
            yes="Usuń", no="Anuluj", danger=True), callback=done)

    def _remove_scenario(self, name):
        # Blokady sprawdzamy jeszcze raz: między otwarciem potwierdzenia
        # a kliknięciem „Usuń” stan mógł się zmienić.
        blocked = self._scenario_in_use(name)
        if blocked:
            self.notify(blocked, severity="error", timeout=8)
            return
        try:
            core.remove_scenario(name)
        except (ValueError, OSError) as e:
            self.notify(f"Nie udało się usunąć: {e}", severity="error",
                        timeout=8)
            return
        self.scenarios.pop(name, None)
        for row in self._main_screen().query(f"#row_{name}"):
            row.remove()
        for screen in self.screen_stack:
            if isinstance(screen, ScenariosScreen):
                screen.remove_row(name)
        self._refresh_card_scenarios()
        # Po refreshu (remove() jest asynchroniczny – checkbox jeszcze
        # chwilę wisi w drzewie).
        self.call_after_refresh(self._update_select_all)
        self.notify(f"Usunięto scenariusz '{name}' z manifestu.")

    def _refresh_card_scenarios(self):
        """Karty „Pomiar N” dostają opcje Selecta przy tworzeniu, więc po
        zmianie manifestu trzeba je przeładować – inaczej świeżo dodanego
        scenariusza nie da się wybrać, a skasowany dalej straszy na liście."""
        for card in self._main_screen().query(MeasurementCard):
            card.refresh_options()

    def on_mount(self):
        self._apply_mode()
        self._refresh_total()

    def on_input_changed(self, event):
        # Czas i wartości serii wpisuje się w Inputach, a od nich zależy
        # suma pod listą pomiarów – niech nadąża za pisaniem. Zdarzenia NIE
        # zatrzymujemy: Inputy karty nie mają innych odbiorców, ale i tak
        # nie ma powodu ich obcinać.
        self._refresh_total()

    def on_button_pressed(self, event):
        if event.button.id == "quit":
            self.exit()
        elif event.button.id == "select_all":
            for box in self.query(".scen-check"):
                box.value = True
        elif event.button.id == "add_fw":
            self.push_screen(AddScreen(), callback=self._scenario_added)
        elif event.button.id == "scenarios_btn":
            self.push_screen(ScenariosScreen())
        elif event.button.id == "results_btn":
            self.push_screen(ResultsScreen(self.mode))
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
        etykietę przełącznika.

        Widoczność sterowana klasą na widgetach: `.standard-only`,
        `.auto-only` i `.calc-only` należą do jednego trybu, a
        `.measure-only` do dwóch, które faktycznie mierzą – kalkulator nie
        rusza płytki, więc profil, egzemplarz i przyciski przebiegu nie mają
        w nim czego robić. Widgety BEZ żadnej z tych klas (logo, Wyniki,
        Wyjście) są wszędzie."""
        for mode in ("standard", "auto", "calc"):
            self.query_one(f"#mode-label-{mode}", Static).set_class(
                self.mode == mode, "active")
            for w in self.query(f".{mode}-only"):
                w.display = self.mode == mode
        for w in self.query(".measure-only"):
            w.display = self.mode != "calc"
        start = self.query_one("#start", Button)
        start.label = "Dalej: PPK2 →" if self.mode == "auto" else "Start"
        # Szerokość `auto` przycisku nie przelicza się po samej zmianie
        # etykiety – bez tego dłuższy podpis trybu autonomicznego zostawał
        # przycięty do szerokości słowa "Start" ("Dalej").
        start.refresh(layout=True)

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
        self.call_after_refresh(self._refresh_total)

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
        # Suma z POZOSTAŁYCH kart – remove() jest asynchroniczny, więc
        # liczymy dopiero po odświeżeniu drzewa.
        self.call_after_refresh(self._refresh_total)

    def _refresh_card_titles(self):
        for card in self.query(MeasurementCard):
            card._refresh_title()
        self._refresh_total()

    def _refresh_total(self):
        """Czas łączny wszystkich kart pod listą pomiarów. Suma OKIEN
        pomiarowych (czas × kombinacje serii × krotność) – bez buildu,
        flasha i triggerów, więc realny przebieg będzie dłuższy.

        Liczymy tylko karty z WPISANYM czasem, żeby liczba w nawiasie
        opisywała dokładnie to, co pokazuje zegar. Nowa karta dziedziczy
        serię poprzedniej (bez czasu), więc bez tego warunku suma mówiłaby
        np. '6:00:00 (36 pomiarów)' – zegar z jednej karty, liczba z dwóch.
        Gdy żadna karta nie ma jeszcze czasu, piszemy '—' zamiast mylącego
        '00:00'."""
        main = self._main_screen()
        found = main.query("#total-time")
        if not found:
            return              # ekran jeszcze nie złożony
        total = found.first(Static)
        timed = []
        for card in main.query(MeasurementCard):
            # Karta w trakcie usuwania wciąż jest w drzewie (remove() jest
            # asynchroniczny), ale jej pól już nie ma – pomijamy ją, zamiast
            # wysypywać odświeżanie sumy na brakującym widgecie.
            if not (card.query(".card-repeat") and card.query(".card-duration")):
                continue
            if card.duration_s() is not None:
                timed.append(card)
        if not timed:
            total.update("Łącznie: —")
            return
        seconds = sum(card.plan_seconds() for card in timed)
        count = sum(card.plan_size() for card in timed)
        total.update(f"Łącznie: {_fmt_hms(seconds)}  "
                     f"({_plural_measurements(count)})")

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
        """'…do następnych' – ustaw config (bez scenariusza) na wszystkich
        kartach LEŻĄCYCH NIŻEJ i zapamiętaj go jako szablon dla kolejnych
        dodanych pomiarów. Wcześniej działał tylko szablon, więc przycisk
        nic nie robił, gdy karty niżej już istniały."""
        card = self._card_of(button)
        if card is None:
            return
        cfg = dict(card.get_config())
        cfg.pop("scenario", None)
        self._card_template = cfg
        # Kolejność z DOM = kolejność kart na ekranie; „niżej” szukamy po
        # tożsamości widgetu, nie po ==.
        cards = list(self.query(MeasurementCard))
        seen = False
        below = []
        for other in cards:
            if other is card:
                seen = True
            elif seen:
                below.append(other)
        for other in below:
            other.apply_shared(cfg)
        self._refresh_card_titles()
        if below:
            self.notify(f"Zastosowano ustawienia do {len(below)} kolejnych "
                        "pomiarów; nowe też je odziedziczą.")
        else:
            self.notify("Nowe pomiary będą dziedziczyć te ustawienia.")

    def _scenario_added(self, result):
        """Po 'Dodaj kod': nowy scenariusz od razu do wyboru, bez restartu
        aplikacji – wiersz na liście trybu ręcznego ORAZ przeładowane opcje
        w kartach „Pomiar N” (te mają własną kopię listy). Zaznaczamy go
        tylko w trybie ręcznym, bo tam checkbox znaczy „zmierz to”;
        w autonomicznym scenariusz wybiera się w karcie pomiaru."""
        if not result:
            return
        name, entry = result
        self.scenarios[name] = entry
        standard = self.mode == "standard"
        self._main_screen().query_one("#scenarios").mount(
            self._scenario_row(name, entry, value=standard))
        self._refresh_card_scenarios()
        self.call_after_refresh(self._update_select_all)
        self.notify(f"Dodano scenariusz '{name}' (zapisany "
                    "w scenarios.toml)."
                    + ("" if standard else " Wybierz go w karcie pomiaru."))

    def on_checkbox_changed(self, event):
        self._update_select_all()

    def _update_select_all(self):
        """'Zaznacz wszystkie' wygląda na wciśnięty dokładnie wtedy, gdy
        zaznaczone są wszystkie scenariusze."""
        main = self._main_screen()
        boxes = main.query(".scen-check")
        main.query_one("#select_all", Button).set_class(
            bool(boxes) and all(box.value for box in boxes), "pressed")

    def _start(self):
        sample = self.query_one("#sample", Input).value.strip()
        prof_name = self.query_one("#profile", Select).value
        if not sample:
            self.notify("Podaj egzemplarz płytki (np. 'nRF54 #1').",
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

        self._auto_check_jlink(plan, sample)

    def _push_auto_run(self, plan, sample):
        def go(ok):
            if ok:
                self.push_screen(AutoRunScreen(plan, sample))
        self.push_screen(Ppk2ConnectScreen(), callback=go)

    def _auto_check_jlink(self, plan, sample):
        """Blokada startu przebiegu autonomicznego, dopóki sondę J-Link
        trzyma inny program (cudza sesja zawyża CAŁY przebieg – a ten
        trwa godzinami, więc lepiej wyłożyć się teraz niż nad ranem)."""
        owners = core.jlink_owners()
        if not owners:
            self._push_auto_run(plan, sample)
            return

        def decided(choice):
            if choice == "retry":
                self._auto_check_jlink(plan, sample)
            elif choice == "ignore":
                self._push_auto_run(plan, sample)
        self.push_screen(ChoiceScreen(
            "[b]Sondę J-Link trzyma inny program[/b]\n\n"
            + core.jlink_conflict_message(owners),
            [("Sprawdziłem – ponów", "retry"),
             ("Mierz mimo to", "ignore"),
             ("Przerwij", "abort")]), callback=decided)

    def _build_auto_plan(self, prof_name):
        """Plan trybu autonomicznego z kart 'Pomiar N'. Kolejność kroków =
        kolejność kart. Trigger (priorytet): Matter (protokół Thread) >
        log dongla > RTT 'start po logu' > 'start po czasie' > od razu;
        przy RTT continuous wzorzec staje się auto-etykietą. Przy starcie
        po czasie (i 'od razu') silnik trzyma własną podłogę na rozruch
        płytki – bierze WIĘKSZY z dwóch czasów, nie sumę, więc tutaj nic
        nie doliczamy."""
        from autorun.plan import (LabelRule, Plan, PlanStep, Storage,
                                  Trigger, expand_repeats, expand_sweep,
                                  parse_duration)

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
            matter = c["protocol"] == "thread"
            # Matter sam wyznacza start (1. odczyt z subskrypcji), więc tryb
            # RTT 'trigger' (start po logu) traci sens – zostaje 'continuous'
            # (etykiety) albo 'off'.
            if matter and rtt == "trigger":
                rtt = "off"
            # Pola serii i monitora czytamy z zakładki wybranego protokołu
            # (get_config), więc protokół bez monitora – Thread – po prostu
            # nie ma czego tu podać.
            monitor_port = c["serial_port"]
            labels = []
            if rtt == "continuous" and c["pattern"]:
                labels = [LabelRule(pattern=c["pattern"],
                                    label=c["pattern"])]
            if matter:
                # Zakładka Thread = pomiar przez Mattera: po flashu parujemy
                # węzeł i otwieramy subskrypcję atrybutu, a pomiar rusza na
                # pierwszym raporcie. Bez osobnego „włącz” – wybór protokołu
                # JEST włącznikiem, więc brakujące pole to błąd, a nie cicha
                # zmiana startu na 'od razu'.
                if not c["chip_node_id"]:
                    raise ValueError(
                        f"Pomiar {card.number}: Matter – podaj Node ID.")
                if c["chip_mode"] != "skip":
                    if not c["chip_dataset"]:
                        raise ValueError(
                            f"Pomiar {card.number}: Matter – podaj dataset "
                            "Thread (hex) albo wybierz 'tylko subskrypcja'.")
                    if not c["chip_discriminator"]:
                        raise ValueError(
                            f"Pomiar {card.number}: Matter – podaj "
                            "discriminator albo 'tylko subskrypcja'.")
                try:
                    chip_to = (parse_duration(c["chip_timeout"])
                               if c["chip_timeout"] else 120.0)
                except ValueError:
                    raise ValueError(
                        f"Pomiar {card.number}: Matter – timeout "
                        f"'{c['chip_timeout']}' nie jest czasem (np. 120s).")
                trigger = Trigger(
                    type="chip", timeout_s=chip_to,
                    node_id=c["chip_node_id"],
                    dataset=c["chip_dataset"],
                    pin=c["chip_pin"] or "20202021",
                    discriminator=c["chip_discriminator"],
                    cluster=c["chip_cluster"] or "temperaturemeasurement",
                    attribute=c["chip_attribute"] or "measured-value",
                    endpoint=c["chip_endpoint"] or "1",
                    min_interval=c["chip_min"] or "1",
                    max_interval=c["chip_max"] or "60",
                    skip_pairing=c["chip_mode"] == "skip",
                    icd_registration=c["chip_mode"] == "icd")
            elif monitor_port and c["serial_pattern"]:
                trigger = Trigger(type="serial", pattern=c["serial_pattern"],
                                  timeout_s=180.0)
            elif rtt == "trigger":
                trigger = Trigger(type="rtt", pattern=c["pattern"],
                                  timeout_s=180.0)
            elif c["delay_on"]:
                try:
                    secs = parse_duration(c["delay_s"])
                except ValueError:
                    raise ValueError(
                        f"Pomiar {card.number}: start po czasie – "
                        f"'{c['delay_s']}' nie jest czasem (np. 30s).")
                trigger = Trigger(type="delay", seconds=secs)
            else:
                trigger = Trigger(type="delay", seconds=0.0)
            base = dict(
                scenario=c["scenario"], duration_s=dur_s,
                voltage=c["voltage"], trigger=trigger, rtt=rtt,
                monitor_port=monitor_port, sample_rate=c["sample_rate"],
                storage=Storage(mode=c["storage"], window_ms=1),
                labels=labels, pristine=pristine,
                protocol=c["protocol"])
            # power_cycle zostaje domyślne (True) – kreator go nie ustawia,
            # więc każdy pomiar z interfejsu ma po flashu odcięcie VOUT.
            # Wyłączyć da się tylko planem: power_cycle = false w plans/*.toml.
            if _sweep_on(c):
                # Seria: jedna karta -> "Pomiar N.1 … N.M" (osobne kroki,
                # każdy z inną flagą -DCONFIG_...=<wartość>, wspólny czas).
                # Druga oś opcjonalna – wypełniona daje iloczyn kartezjański.
                try:
                    card_steps = expand_sweep(
                        card.number, _sweep_axes(c), base)
                except ValueError as e:
                    raise ValueError(f"Pomiar {card.number}: {e}")
            else:
                card_steps = [PlanStep(label=str(card.number), **base)]
            # Krotność ('x1 … x5' w nagłówku karty) na końcu, bo mnoży to,
            # co karta już wyprodukowała: przy serii każda wartość dostaje
            # swoje powtórki obok siebie (N.1/1, N.1/2, N.2/1, …).
            steps.extend(expand_repeats(card_steps, c["repeat"]))
        return Plan(name="interfejs", board=prof_name, steps=steps)


if __name__ == "__main__":
    PowerTestApp().run()
