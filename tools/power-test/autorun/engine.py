# ============================================================
#  autorun/engine.py – silnik trybu autonomicznego
# ============================================================
# AutoRunner wykonuje plan bez udziału człowieka:
#   FAZA 1: zbuduj wszystkie obrazy z góry (jak cmd_run),
#   FAZA 2: per krok – zasil płytkę z PPK2, flash, [power-cycle],
#           czekaj na trigger (delay / wzorzec RTT), mierz prąd przez
#           zadany czas, sfinalizuj sesję i dopisz wiersz CSV.
#
# Silnik działa w wątku wołającego: TUI odpala go przez
# asyncio.to_thread i dostaje zdarzenia przez event_cb (opakowane
# call_from_thread), CLI woła run() wprost i drukuje zdarzenia.
# Przerwanie = threading.Event `cancel` – sprawdzany we wszystkich
# pętlach; przerwany pomiar jest domykany (częściowa sesja zostaje).

import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np
from datetime import datetime
from pathlib import Path

import power_test as core

from .plan import validate_plan, voltage_to_mV
from .ppk2 import Ppk2Error
from .dongle import DEFAULT_BAUD, DongleError, SerialLineReader
from .rtt import LinePatternMatcher, PylinkRttReader, RttError, \
    jlink_device_for
from .session import SessionWriter, _atomic_json, new_session_dir

# Ile czasu bez ŻADNYCH próbek uznajemy za zerwane połączenie z PPK2
# (po 3 nieudanych restartach pomiaru stosowana jest polityka kroku).
STALL_TIMEOUT_S = 10.0
READ_INTERVAL_S = 0.01     # ~10 ms między odczytami portu PPK2
# Ile okna pomiaru wolno zgubić, zanim wynik uznamy za bezwartościowy.
# Przy takich brakach średnia nie opisuje już przebiegu prądu.
MAX_LOST_FRACTION = 0.05
MEASURE_ATTEMPTS = 2       # pierwotny pomiar + jedno powtórzenie
# Okno wskaźnika „teraz” w UI. Statusy lecą co 1 s, ale liczenie „teraz”
# z całej sekundy dawało średnią ze 100 000 próbek – jej błąd standardowy
# to ułamek promila, więc na ekranie stała ta sama liczba do końca pomiaru
# i wyglądało to jak zawieszony odczyt. Krótsze okno pokazuje, co płytka
# robi TERAZ, a nie ile wyniosła średnia z ostatniej sekundy.
INST_WINDOW_S = 0.1
# Płytka po flashu resetuje się i przechodzi rozruch – pierwsze sekundy
# to prąd bootowania, nie prąd scenariusza. Dlatego każdy start "po
# czasie" ma tu podłogę. NIE dodajemy jej do czasu z planu, tylko bierzemy
# większy z dwóch (30 s w planie = 30 s, nie 35 s).
MIN_START_DELAY_S = 5.0


def effective_delay_s(trigger):
    """Ile sekund realnie czekamy po flashu przy triggerze 'delay'.
    Dla RTT/serial 0 – tam czekaniem jest sam wzorzec i doliczenie
    sekund groziłoby przegapieniem linii wypisanej tuż po rozruchu."""
    if trigger.type != "delay":
        return 0.0
    return max(trigger.seconds, MIN_START_DELAY_S)


def _sweep_payload(step):
    """Osie serii kroku dla zdarzeń i meta.json: lista {'param','value'}
    (kolejność jak w planie) albo None, gdy krok nie jest z serii. None,
    a nie pusta lista – odbiorcy testują to jednym `if`."""
    if not step.sweep:
        return None
    return [{"param": p, "value": v} for p, v in step.sweep]


# --- Blokada usypiania na czas przebiegu autonomicznego ---
# Przebieg trwa godzinami i nikt przy nim nie siedzi, więc logind uśpiłby
# maszynę po bezczynności (typowo 15 min na baterii) w środku okna pomiaru.
# Suspend zabija strumień próbek z PPK2: silnik to wykryje (STALL_TIMEOUT_S)
# i pomiar powtórzy albo odrzuci, ale punkt i tak przepada – a przy nocnym
# planie razem z nim cała noc. Blokujemy więc na CAŁY przebieg (buildy,
# flashe, wszystkie okna): bezczynność, jawny suspend i zamknięcie klapy,
# żeby dało się zamknąć laptopa i zostawić pomiar. Tryb ręczny blokady nie
# bierze – tam operator klika, więc system i tak nie jest bezczynny.
INHIBIT_WHAT = "idle:sleep:handle-lid-switch"
INHIBIT_CMD = "systemd-inhibit"


class _SleepInhibitor:
    """Blokada usypiania trzymana przez proces-potomka `systemd-inhibit`:
    dopóki on żyje, logind nie uśpi maszyny. Zwolnienie = ubicie procesu,
    więc blokada nie przetrwa awarii narzędzia – i tak ma być, bo inaczej
    zostawiałaby maszynę bez usypiania po cichu.

    Potomek dostaje własną sesję i ubijamy CAŁĄ grupę procesów: sam
    `systemd-inhibit` tylko czeka na komendę, którą uruchomił (`sleep`),
    a to ona trzyma deskryptor blokady."""

    def __init__(self, why):
        self.why = why
        self._proc = None

    def start(self):
        """Weź blokadę. Zwraca (co_zablokowane, powód_braku) – nigdy nie
        podnosi wyjątku, bo pomiar jest ważniejszy niż blokada."""
        if shutil.which(INHIBIT_CMD) is None:
            return None, f"brak {INHIBIT_CMD} (nie-systemd?)"
        try:
            self._proc = subprocess.Popen(
                [INHIBIT_CMD, f"--what={INHIBIT_WHAT}",
                 "--who=board-power-test", f"--why={self.why}",
                 "--mode=block", "sleep", "infinity"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            self._proc = None
            return None, str(e)
        return INHIBIT_WHAT, None

    def stop(self):
        """Zwolnij blokadę. Idempotentne – wołane na każdej ścieżce wyjścia
        z run()."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


class AutoRunError(RuntimeError):
    pass


@dataclass
class EngineEvent:
    """Zdarzenie dla UI. kind: plan_start / phase / step_start / state /
    line / note / live / annotation / step_done / plan_done."""
    kind: str
    step: int = 0              # numer kroku (1..N), 0 = całość planu
    name: str = ""             # scenariusz kroku
    text: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class StepResult:
    index: int
    scenario: str
    status: str                # done/skipped/build_failed/trigger_timeout/
    #                            error/cancelled
    session_dir: Path = None
    summary: dict = field(default_factory=dict)
    error: str = ""
    label: str = ""            # "N.M" dla serii; puste = użyj index


def default_sampler_factory(plan):
    from .ppk2 import Ppk2ApiSampler
    return Ppk2ApiSampler(plan.ppk2_port)


def default_rtt_factory(profile):
    return PylinkRttReader(jlink_device_for(profile["board"]))


def default_serial_factory(port):
    return SerialLineReader(port)


# Dongiel buforuje logi, dopóki nikt nie trzyma portu otwartego – build
# trwa minuty, a USB CDC pamięta – i wyrzuca cały bufor w momencie otwarcia
# portu. Te linie powstały PRZED flashem (stara firmware, stary krok serii),
# więc nie mogą uzbroić triggera: startowałyby pomiar w środku resetu i
# dołączania do sieci. Po attach czytamy je więc i wyrzucamy, aż port ucichnie
# na STALE_QUIET_S. STALE_MAX_S ogranicza drenaż, gdy dongiel gada bez przerwy
# i cisza nie nadchodzi (wtedy resztę bufora traktujemy już jako świeżą).
STALE_QUIET_S = 0.3
STALE_MAX_S = 2.0


class _SerialMonitor:
    """Wątek monitora dongla: czyta linie z portu, pokazuje je w UI
    (zdarzenie 'monitor'), zapisuje do dongle.log i – jeśli podano
    `trig_sub` – ustawia `hit`, gdy w linii pojawi się ten FRAGMENT
    (podłańcuch). Żyje przez oczekiwanie na trigger ORAZ cały pomiar.

    Zanim ruszy wątek, `start()` wyrzuca to, co dongiel nabuforował przy
    zamkniętym porcie (patrz STALE_QUIET_S) – inaczej pierwsza porcja po
    otwarciu portu, cała sprzed flasha, fałszywie startowałaby pomiar."""

    def __init__(self, reader, engine, log_path, idx, scenario, trig_sub):
        self.reader = reader
        self.engine = engine
        self.log_path = log_path
        self.idx = idx
        self.scenario = scenario
        self.trig_sub = trig_sub
        self.hit = threading.Event()
        self.dropped = 0                # linii wyrzuconych jako sprzed flasha
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self.reader.attach()            # może podnieść DongleError
        self._drop_stale()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _drop_stale(self):
        """Wyczytaj i wyrzuć bufor dongla sprzed flasha. Pełna treść idzie do
        dongle.log z prefiksem '[przed flashem]' (audyt zostaje – widać, co
        Friend wypisał w trakcie buildu), ale NIE do panelu i NIE do triggera;
        panel dostaje jedną notkę, którą emituje _start_monitor po nagłówku.
        Pierwsza linia takiej porcji bywa urwana (port otwarty w środku linii)
        – i to też jest w porządku, bo idzie do kosza razem z resztą."""
        deadline = time.monotonic() + STALE_MAX_S
        with open(self.log_path, "a", encoding="utf-8") as log:
            while time.monotonic() < deadline:
                line = self.reader.readline(timeout_s=STALE_QUIET_S)
                if line is None:
                    break               # cisza na porcie = bufor wyczytany
                self.dropped += 1
                log.write(f"[przed flashem] {line}\n")
            log.flush()

    def _loop(self):
        with open(self.log_path, "a", encoding="utf-8") as log:
            while not self._stop.is_set():
                try:
                    line = self.reader.readline(timeout_s=0.3)
                except Exception as e:
                    self.engine._emit("monitor", self.idx, self.scenario,
                                      text=f"[monitor dongla przerwany: {e}]")
                    return
                if line is None:
                    continue
                log.write(line + "\n")
                log.flush()
                self.engine._emit("monitor", self.idx, self.scenario,
                                  text=line)
                if self.trig_sub and self.trig_sub in line:
                    self.hit.set()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        try:
            self.reader.detach()
        except Exception:
            pass


# Skrypt parowania + subskrypcji (patrz też README). Żyje w scripts/ obok
# instalatora. Sekwencja: rm /tmp/chip_* -> pairing ble-thread -> interactive
# start -> subscribe; na pierwszej wartości wypisuje marker FIRST-VALUE i
# TRZYMA subskrypcję otwartą aż do zamknięcia procesu.
CHIP_SCRIPT = core.ROOT / "scripts" / "pair_and_subscribe.py"
CHIP_FIRST_VALUE_MARK = "FIRST-VALUE"
# Zapas nad `trigger.timeout_s` (który dotyczy czekania na 1. wartość) na
# samo parowanie BLE+Thread, zanim silnik uzna sesję chip za zawieszoną.
# Skrypt ma własne, ciaśniejsze limity (pairing/wartość) i wychodzi pierwszy –
# to tylko bezpiecznik na twardo zawieszony proces.
CHIP_PAIR_ALLOWANCE_S = 240.0
# Po pierwszym raporcie (FIRST-VALUE) subskrypcja jeszcze się "układa" –
# pierwsze sekundy to ruch Thread/Matter po parowaniu, nie normalna praca
# węzła. Odczekaj tyle, żeby ten pik nie wchodził do pomiaru.
CHIP_START_SETTLE_S = 10.0
# To samo po triggerze z dongla: log, na który czekamy, pada zwykle w chwili
# dołączania węzła do sieci (u nas Friendship z LPN nawiązany + pierwsza
# publikacja), a wtedy radio jeszcze pracuje na pełnych obrotach. Bez tego
# zapasu pierwszy cykl organizacyjny wchodziłby do średniej.
SERIAL_START_SETTLE_S = 10.0


class _ChipSession:
    """Trigger 'chip': parowanie Matter + otwarta subskrypcja atrybutu.

    Odpala scripts/pair_and_subscribe.py i wątkiem drenuje jego stdout przez
    CAŁE życie procesu – to konieczne, bo inaczej bufor pipe by się zapchał i
    subskrypcja (a więc raporty) by zamarły w trakcie pomiaru. Gdy w strumieniu
    padnie marker FIRST-VALUE, ustawia `first_value` (silnik startuje pomiar).
    Linie do momentu pierwszej wartości idą też do UI (postęp parowania); potem
    już tylko do chip.log, żeby nie zalewać ekranu raportami subskrypcji.
    Subskrypcja żyje aż do stop() (wołane po pomiarze w _run_step)."""

    def __init__(self, cmd, engine, log_path, idx, scenario):
        self.cmd = cmd
        self.engine = engine
        self.log_path = log_path
        self.idx = idx
        self.scenario = scenario
        self.first_value = threading.Event()
        self.value = ""
        self.proc = None
        self._thread = None

    def start(self):
        self.proc = subprocess.Popen(
            self.cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace",
            bufsize=1, env=core.child_env())
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self):
        with open(self.log_path, "a", encoding="utf-8") as log:
            for line in self.proc.stdout:
                line = line.rstrip()
                log.write(line + "\n")
                log.flush()
                if not self.first_value.is_set():
                    self.engine._emit("line", self.idx, self.scenario, line)
                    if line.startswith(CHIP_FIRST_VALUE_MARK):
                        parts = line.split(None, 1)
                        self.value = parts[1] if len(parts) > 1 else ""
                        self.first_value.set()

    def wait_first_value(self, timeout_s, check_cancel):
        """Blokuj do markera FIRST-VALUE. _TriggerTimeout po `timeout_s`;
        AutoRunError, gdy skrypt padnie wcześniej (parowanie/subskrypcja
        nie doszły do skutku)."""
        deadline = time.monotonic() + timeout_s
        while not self.first_value.is_set():
            check_cancel()
            if self.proc.poll() is not None:
                raise AutoRunError(
                    "chip: skrypt parowania/subskrypcji zakończył się przed "
                    f"pierwszą wartością (kod {self.proc.returncode}) – "
                    f"sprawdź {self.log_path.name}")
            if time.monotonic() >= deadline:
                raise _TriggerTimeout(
                    f"chip: pierwsza wartość nie przyszła w {timeout_s:g} s")
            time.sleep(0.1)

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()          # skrypt łapie SIGTERM i ubija chip-tool
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._thread is not None:
            self._thread.join(timeout=3)
        # Wątek drenujący już wyszedł (EOF po zakończeniu procesu) – bezpiecznie
        # domknij pipe, żeby nie zostawiać otwartego deskryptora.
        if self.proc.stdout is not None:
            try:
                self.proc.stdout.close()
            except Exception:
                pass


class AutoRunner:

    def __init__(self, plan, manifest, sample, *, sampler_factory=None,
                 rtt_factory=None, serial_factory=None, event_cb=None,
                 cancel=None, pause=None, dry_run=False, mock=None):
        self.plan = plan
        self.manifest = manifest
        self.sample = sample
        # SYMULACJA (mock): cały przebieg bez PPK2, programatora i płytki –
        # patrz autorun/mock.py. Domyślne fabryki wskazują wtedy atrapy, ale
        # jawnie podana fabryka ma pierwszeństwo (testy podstawiają własne).
        self.mock = mock
        if mock is not None and sampler_factory is None:
            from .mock import mock_sampler_factory
            sampler_factory = mock_sampler_factory(mock)
        self.sampler_factory = sampler_factory or default_sampler_factory
        self.rtt_factory = rtt_factory or default_rtt_factory
        self.serial_factory = serial_factory or default_serial_factory
        self.event_cb = event_cb or (lambda ev: None)
        self.cancel = cancel or threading.Event()
        # pause: gdy ustawiony, pętla pomiaru wstrzymuje sampler i zamraża
        # czas (oś próbek) do czasu wyczyszczenia – Stop/Wznów w UI.
        self.pause = pause or threading.Event()
        self.dry_run = dry_run

        self.defaults = manifest.get("defaults", {})
        self.scenarios = manifest.get("scenarios", {})
        self.prof_name, self.profile = core.resolve_profile(
            manifest, plan.board or None)
        self.run_dir = None
        self._plan_log = None
        self._sampler = None
        self._dut_on = False
        # Blokada usypiania: brana w run() na cały przebieg, zwalniana
        # w finally (patrz _SleepInhibitor).
        self._inhibitor = None
        # Dedup katalogów builda MIĘDZY krokami (build just-in-time):
        # ten sam scenariusz+flagi budowany raz, kolejne wystąpienia
        # korzystają z gotowego obrazu.
        self._used_dirs = {}
        self._done_dirs = {}
        # Kontekst bieżącego kroku (dla etykiet "N.M" i wartości sweepa):
        # ustawiany na starcie _run_step, doklejany do zdarzeń w _emit.
        self._cur_label = ""
        self._cur_sweep = None
        # Sesja triggera 'chip' (parowanie + subskrypcja) bieżącego kroku:
        # ustawiana w _wait_trigger, zamykana w _run_step (finally), żeby
        # subskrypcja żyła przez cały pomiar i została ubita po nim.
        self._chip = None

    # ---------- pomocnicze ----------

    @property
    def _time_scale(self):
        """Ile sekund PRZEBIEGU mieści się w sekundzie realnej. 1.0 na
        sprzęcie; w symulacji `speedup`, bo atrapa oddaje próbki tyle razy
        szybciej. Skala dotyczy zegara (odliczanie, oczekiwania) – dane
        sesji liczą się z próbek, więc są wierne bez żadnej korekty."""
        return self.mock.speedup if self.mock is not None else 1.0

    def _sleep_scaled(self, seconds):
        """Oczekiwanie na sprzęt (rozruch płytki, uspokojenie po odcięciu
        zasilania) skrócone w symulacji – tam nie ma czego czekać."""
        time.sleep(seconds / self._time_scale)

    def _emit(self, kind, step=0, name="", text="", data=None, **extra):
        """Zdarzenie do UI. Dane można podać słownikiem (data={...}) albo
        pojedynczymi kwargami (detail=...) – oba trafiają do EngineEvent.data."""
        payload = dict(data or {})
        payload.update(extra)
        # Zdarzenia kroku (step != 0) niosą etykietę wyświetlaną ("N.M" dla
        # serii, inaczej numer) i – jeśli krok jest z serii – parę
        # parametr/wartość, żeby UI mogło je pokazać bez znajomości planu.
        if step:
            payload.setdefault("label", self._cur_label or str(step))
            if self._cur_sweep:
                payload.setdefault("sweep", self._cur_sweep)
        self.event_cb(EngineEvent(kind, step, name, text, payload))

    def _log(self, text, files=()):
        """Linia do plan.log (i opcjonalnie logów kroku) + zdarzenie."""
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {text}"
        for f in (self._plan_log, *files):
            if f is not None:
                f.write(line + "\n")
                f.flush()
        return line

    def _note(self, text, step=0, name="", files=()):
        self._emit("note", step, name, self._log(text, files))

    def _check_cancel(self):
        if self.cancel.is_set():
            raise _Cancelled()

    def _run_streamed(self, cmd, cwd, title, step=0, name="",
                      log_file=None, capture=None):
        """Subprocess ze strumieniowaniem linii do zdarzeń i logu –
        odpowiednik tui._stream, ale po stronie silnika (bez UI).
        `capture` = lista, do której dopisujemy wyjście (build – po tabelkę
        pamięci); przy None nie trzymamy logu w pamięci.
        Przerwanie (cancel) ubija proces."""
        header = f"$ {shlex.join(cmd)}"
        self._log(f"{title}: {header}", files=(log_file,) if log_file
                  else ())
        # cmd_start/cmd_end obejmują komendę – UI grupuje wyjście w zwijaną
        # sekcję (jak tryb ręczny), logi lecą zdarzeniami `line` pomiędzy.
        self._emit("cmd_start", step, name, text=title)
        self._emit("line", step, name, header)
        if self.dry_run:
            self._emit("cmd_end", step, name, data={"rc": 0, "title": title})
            return 0
        with subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True,
                              errors="replace",
                              env=core.child_env()) as proc:
            for line in proc.stdout:
                line = line.rstrip()
                if log_file is not None:
                    log_file.write(line + "\n")
                if capture is not None:
                    capture.append(line)
                self._emit("line", step, name, line)
                if self.cancel.is_set():
                    proc.terminate()
            if log_file is not None:
                log_file.flush()
            rc = proc.wait()
        self._emit("cmd_end", step, name, data={"rc": rc, "title": title})
        self._check_cancel()
        return rc

    # ---------- budowanie ----------

    def _voltage_for(self, step):
        scen = self.scenarios[step.scenario]
        return str(step.voltage or scen.get("voltage")
                   or self.defaults.get("voltage", "3.0"))

    def _build_spec(self, idx, step, used_dirs):
        """(komenda, katalog builda) dla kroku. Dwa kroki z tym samym
        scenariuszem, ale innymi flagami, dostają osobne katalogi
        (sufiks _krokN), żeby obrazy się nie nadpisywały."""
        scen = self.scenarios[step.scenario]
        pristine = "always" if step.pristine else "auto"
        cmd, build_dir = core.make_build_cmd(
            step.scenario, scen, self.prof_name, self.profile,
            self.defaults.get("profile"), pristine=pristine)
        if step.build_extra_args:
            if "--" not in cmd:
                cmd.append("--")
            cmd += list(step.build_extra_args)
        if step.build_cmd:
            src = (core.resolve_path(scen["source"])
                   if scen.get("source") else core.ROOT)
            cmd = [a.format(board=self.profile["board"],
                            build_dir=str(core.ROOT / build_dir),
                            src=str(src))
                   for a in shlex.split(step.build_cmd)]
        fingerprint = core._build_fingerprint(cmd)
        if used_dirs.get(build_dir, fingerprint) != fingerprint:
            new_dir = f"{build_dir}_krok{idx}"
            cmd = [a.replace(str(core.ROOT / build_dir),
                             str(core.ROOT / new_dir)) for a in cmd]
            build_dir = new_dir
            fingerprint = core._build_fingerprint(cmd)
        used_dirs[build_dir] = fingerprint
        return cmd, build_dir

    def _build_step(self, idx, step, workspace):
        """Zbuduj obraz kroku TUŻ przed jego pomiarem (just-in-time).
        Zwraca katalog builda, None (gotowy hex) albo BUILD_FAILED (build
        padł, a polityka to skip). Przy on_build_error='abort' podnosi
        AutoRunError."""
        scen = self.scenarios[step.scenario]
        if "hex" in scen:
            self._note(f"pomiar {idx} ({step.scenario}): gotowy hex "
                       f"({scen['hex']}) – bez budowania", idx,
                       step.scenario)
            return None
        cmd, build_dir = self._build_spec(idx, step, self._used_dirs)
        if build_dir in self._done_dirs:
            self._note(f"pomiar {idx} ({step.scenario}): ten sam obraz co "
                       f"pomiar {self._done_dirs[build_dir]} – bez "
                       "ponownego builda", idx, step.scenario)
            return build_dir
        if (not step.pristine and not self.dry_run
                and core.build_up_to_date(build_dir, cmd)):
            self._done_dirs[build_dir] = idx
            self._note(f"pomiar {idx} ({step.scenario}): gotowy build "
                       f"({build_dir}/) – pomijam", idx, step.scenario)
            return build_dir
        self._emit("state", idx, step.scenario, "build")
        # Wyjście builda zbieramy, żeby wyłuskać z niego tabelkę zajętości
        # pamięci – liczby lądują obok obrazu i stamtąd trafiają do
        # dziennika (także gdy następny krok ten build pominie).
        out = []
        rc = self._run_streamed(cmd, workspace, f"build {step.scenario}",
                                idx, step.scenario, capture=out)
        if rc != 0:
            self._note(f"pomiar {idx} ({step.scenario}): build padł "
                       f"(kod {rc})", idx, step.scenario)
            if self.plan.on_build_error == "abort":
                raise AutoRunError(
                    f"build pomiaru {idx} ({step.scenario}) zakończony "
                    f"błędem (kod {rc}); plan ma on_build_error = 'abort'")
            return BUILD_FAILED
        if not self.dry_run:
            core.record_build(build_dir, cmd)
            core.record_memory(build_dir, out)
        self._done_dirs[build_dir] = idx
        return build_dir

    # ---------- pomiar ----------

    def _ensure_dut_power(self, on):
        if self.dry_run or self._sampler is None:
            return
        if self._dut_on != on:
            self._sampler.dut_power(on)
            self._dut_on = on

    def _set_voltage(self, voltage):
        """Napięcie źródła PPK2 (twardy limit w samplerze; Ppk2Error ->
        AutoRunError, bo o losie kroku decyduje polityka planu).

        Wołane KILKA razy w kroku – przed włączeniem zasilania DUT i po
        KAŻDYM jego włączeniu. Powód: komenda REGULATOR_SET wysłana przy
        odciętym wyjściu nie zawsze dochodzi do regulatora (przy otwarciu
        PPK2 idzie bezpieczne minimum, a właściwe napięcie kroku
        milisekundy później), więc PIERWSZY pomiar w sesji jechał na tym
        minimum – przy zasilaniu przez DC/DC prąd wychodził wtedy ~1,5×
        za duży, mimo 'napiecie_V = 3.0' w raporcie. Ponowna komenda z tą
        samą wartością przy WŁĄCZONYM wyjściu jest nieszkodliwa (dokładnie
        to robi suwak w nRF Connect) i wyrównuje stan regulatora."""
        if self.dry_run or self._sampler is None:
            return
        try:
            self._sampler.set_voltage(voltage_to_mV(voltage))
        except (ValueError, Ppk2Error) as e:
            raise AutoRunError(f"napięcie źródła PPK2: {e}")

    def _chip_cmd(self, trig):
        """argv skryptu parowania+subskrypcji (scripts/pair_and_subscribe.py)
        z parametrów triggera 'chip'. Puste pola pomijamy – skrypt ma własne
        domyślne (chip-dir, chip-tool, match)."""
        cmd = [sys.executable, str(CHIP_SCRIPT),
               "--node-id", trig.node_id,
               "--endpoint", trig.endpoint,
               "--cluster", trig.cluster,
               "--attribute", trig.attribute,
               "--min-interval", trig.min_interval,
               "--max-interval", trig.max_interval,
               "--value-timeout", str(trig.timeout_s)]
        if trig.pin:
            cmd += ["--pin", trig.pin]
        if trig.dataset:
            cmd += ["--dataset", trig.dataset]
        if trig.discriminator:
            cmd += ["--discriminator", trig.discriminator]
        if trig.chip_dir:
            cmd += ["--chip-dir", trig.chip_dir]
        if trig.chip_tool:
            cmd += ["--chip-tool", trig.chip_tool]
        if trig.match:
            cmd += ["--match", trig.match]
        if trig.skip_pairing:
            cmd.append("--skip-pairing")
        if trig.no_wipe:
            cmd.append("--no-wipe")
        if trig.icd_registration:
            # Weryfikacja OperatingMode idzie w komplecie z rejestracją: to
            # jedyny sposób, żeby przy CONFIG_LOG=n wyłapać, że węzeł jednak
            # jechał w SIT, zanim zapiszemy wiersz pomiaru.
            cmd += ["--icd-registration", "--verify-icd",
                    "--icd-stay-active-duration", str(trig.icd_stay_active_ms)]
        return cmd

    def _wait_trigger(self, idx, step, session_dir, run_log, monitor=None):
        """Warunek startu pomiaru. Zwraca (czytnik_rtt | None) – przy
        rtt='continuous' połączenie zostaje otwarte na czas pomiaru."""
        trig = step.trigger
        if trig.type == "delay":
            # Podłoga, nie doliczenie: plan prosi o 30 s -> czekamy 30 s.
            delay_s = effective_delay_s(trig)
            self._emit("state", idx, step.scenario, "trigger",
                       detail=f"start za {delay_s:g} s")
            self._note(f"trigger: delay {delay_s:g} s", idx,
                       step.scenario, files=(run_log,))
            if self.dry_run:
                return None
            self._countdown(idx, step, delay_s)
            if step.rtt != "continuous":
                return None
            reader = self.rtt_factory(self.profile)
            reader.attach()
            return reader

        if trig.type == "serial":
            # Czekaj, aż monitor dongla wypisze linię zawierającą fragment.
            self._emit("state", idx, step.scenario, "trigger",
                       detail=f"czekam na log dongla: {trig.pattern!r}")
            self._note(f"trigger: serial fragment={trig.pattern!r} "
                       f"timeout={trig.timeout_s:g} s", idx, step.scenario,
                       files=(run_log,))
            if self.dry_run:
                return None
            if monitor is None:
                raise AutoRunError("trigger serial: monitor dongla nie "
                                   "wystartował (sprawdź port)")
            deadline = time.monotonic() + trig.timeout_s
            while not monitor.hit.is_set():
                self._check_cancel()
                if time.monotonic() >= deadline:
                    raise _TriggerTimeout(
                        f"fragment {trig.pattern!r} nie pojawił się na logu "
                        f"dongla w {trig.timeout_s:g} s")
                time.sleep(0.1)
            self._note(f"trigger dongla złapany: {trig.pattern!r} – odczekuję "
                       f"{SERIAL_START_SETTLE_S:g} s przed startem pomiaru",
                       idx, step.scenario, files=(run_log,))
            self._emit("state", idx, step.scenario, "trigger",
                       detail=f"start za {SERIAL_START_SETTLE_S:g} s")
            self._sleep_cancellable(SERIAL_START_SETTLE_S)
            return None

        if trig.type == "chip":
            # Po flashu: sparuj węzeł Matter i otwórz subskrypcję atrybutu;
            # pomiar startuje CHIP_START_SETTLE_S po PIERWSZYM raporcie
            # (marker FIRST-VALUE ze scripts/pair_and_subscribe.py), żeby
            # pominąć poparowaniowy pik. Subskrypcja żyje przez cały
            # pomiar – proces zamyka _run_step (finally) przez self._chip.
            cmd = self._chip_cmd(trig)
            self._emit("state", idx, step.scenario, "trigger",
                       detail="Matter: parowanie + subskrypcja")
            self._note(f"trigger: chip node={trig.node_id} "
                       f"{trig.cluster}/{trig.attribute} ep={trig.endpoint} "
                       f"timeout={trig.timeout_s:g} s"
                       + (" rejestracja ICD (LIT)"
                          if trig.icd_registration else ""),
                       idx, step.scenario, files=(run_log,))
            self._log(f"chip: $ {shlex.join(cmd)}", files=(run_log,))
            if self.dry_run:
                return None
            # cmd_start/cmd_end obejmują parowanie+subskrypcję – UI grupuje
            # wyjście skryptu w zwijaną sekcję (jak build/flash). Zaczynamy
            # PRZED chip.start(), żeby linie z wątku drenującego trafiły do niej.
            title = f"chip {step.scenario}: parowanie + subskrypcja"
            self._emit("cmd_start", idx, step.scenario, text=title)
            chip = _ChipSession(cmd, self, session_dir / "chip.log",
                                idx, step.scenario)
            chip.start()
            try:
                chip.wait_first_value(trig.timeout_s + CHIP_PAIR_ALLOWANCE_S,
                                      self._check_cancel)
            except BaseException:
                self._emit("cmd_end", idx, step.scenario,
                           data={"rc": 1, "title": title})
                chip.stop()
                raise
            self._emit("cmd_end", idx, step.scenario,
                       data={"rc": 0, "title": title})
            self._chip = chip
            self._note(f"chip: pierwsza wartość ({chip.value}) – odczekuję "
                       f"{CHIP_START_SETTLE_S:g} s przed startem pomiaru",
                       idx, step.scenario, files=(run_log,))
            self._emit("state", idx, step.scenario, "trigger",
                       detail=f"Matter: start za {CHIP_START_SETTLE_S:g} s")
            self._sleep_cancellable(CHIP_START_SETTLE_S)
            if step.rtt == "continuous":
                reader = self.rtt_factory(self.profile)
                reader.attach()
                return reader
            return None

        # trigger rtt: czekaj na wzorzec na konsoli RTT
        self._emit("state", idx, step.scenario, "trigger",
                   detail=f"czekam na RTT: {trig.pattern!r}")
        self._note(f"trigger: rtt pattern={trig.pattern!r} "
                   f"timeout={trig.timeout_s:g} s", idx, step.scenario,
                   files=(run_log,))
        if self.dry_run:
            return None
        rx = re.compile(trig.pattern)
        reader = self.rtt_factory(self.profile)
        reader.attach()
        rtt_log = open(session_dir / "rtt.log", "a", encoding="utf-8")
        try:
            deadline = time.monotonic() + trig.timeout_s
            while True:
                self._check_cancel()
                if time.monotonic() >= deadline:
                    raise _TriggerTimeout(
                        f"wzorzec {trig.pattern!r} nie pojawił się na "
                        f"RTT w {trig.timeout_s:g} s")
                line = reader.readline(timeout_s=0.5)
                if line is None:
                    continue
                rtt_log.write(line + "\n")
                if rx.search(line):
                    self._note(f"trigger RTT złapany: {line!r}", idx,
                               step.scenario, files=(run_log,))
                    break
        except BaseException:
            reader.detach()
            raise
        finally:
            rtt_log.close()
        if step.rtt == "continuous":
            return reader
        # rtt='trigger': zamknij J-Link PRZED pomiarem (podłączony
        # debugger dodaje prąd); krótka chwila na uspokojenie.
        reader.detach()
        self._sleep_scaled(1.0)
        return None

    def _sleep_cancellable(self, seconds):
        deadline = time.monotonic() + seconds / self._time_scale
        while time.monotonic() < deadline:
            self._check_cancel()
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def _countdown(self, idx, step, seconds):
        """Odliczanie do startu pomiaru (trigger delay): co ~0.5 s emituje
        pozostały czas, żeby UI mogło pokazać odliczanie po flashu.
        W symulacji czekanie jest krótsze, ale ODLICZANIE pokazuje sekundy
        z planu – przewija się po prostu tyle razy szybciej."""
        scale = self._time_scale
        deadline = time.monotonic() + seconds / scale
        while True:
            self._check_cancel()
            remaining = (deadline - time.monotonic()) * scale
            if remaining <= 0:
                break
            self._emit("countdown", idx, step.scenario,
                       data={"remaining_s": round(remaining, 1)})
            # Częściej niż raz na sekundę: pojedyncze zacięcie (GC, zajęte
            # UI) nie zabiera wtedy całej sekundy z odliczania. `remaining`
            # jest w sekundach PRZEBIEGU, więc na sen wraca przez skalę.
            time.sleep(min(0.25, remaining / scale))

    def _start_monitor(self, idx, step, session_dir):
        """Uruchom monitor dongla, jeśli krok podał `monitor_port`. Gdy port
        jest źródłem triggera, brak monitora to błąd kroku; przy monitorze
        'tylko do podglądu' porażka otwarcia nie przerywa pomiaru."""
        if not step.monitor_port or self.dry_run:
            return None
        trig_sub = (step.trigger.pattern
                    if step.trigger.type == "serial" else "")
        mon = _SerialMonitor(self.serial_factory(step.monitor_port), self,
                             session_dir / "dongle.log", idx, step.scenario,
                             trig_sub)
        try:
            mon.start()
        except DongleError as e:
            if step.trigger.type == "serial":
                raise AutoRunError(f"monitor dongla: {e}")
            self._emit("monitor", idx, step.scenario,
                       text=f"[monitor dongla niedostępny: {e}]")
            return None
        # Nagłówek od razu odsłania panel monitora – logi widać JUŻ podczas
        # czekania na trigger, zanim padnie pierwsza linia z dongla.
        self._emit("monitor", idx, step.scenario,
                   text=f"[monitor dongla: {step.monitor_port} @ "
                        f"{DEFAULT_BAUD}]")
        # Po nagłówku, żeby kolejność w panelu była czytelna: najpierw skąd
        # czytamy, potem czego nie liczymy. Treść pominiętych linii jest
        # w dongle.log (prefiks '[przed flashem]').
        if mon.dropped:
            self._emit("monitor", idx, step.scenario,
                       text=f"[pominięto {mon.dropped} linii z buforu dongla "
                            "sprzed flasha]")
        return mon

    def _do_pause(self, sampler, writer, idx, step, wall_elapsed_s=0.0):
        """Pauza pomiaru (Stop w UI): zatrzymaj sampler i zamroź oś czasu,
        czekaj na wznowienie albo cancel. Płytka zostaje zasilona – to ten
        sam pomiar. Zwraca czas trwania pauzy (do korekty rozliczania
        zgubionych próbek po wznowieniu)."""
        paused_at = time.monotonic()
        try:
            sampler.stop()
        except Exception:
            pass
        avg = writer.avg_uA
        writer.update_status("paused", None)
        self._emit("paused", idx, step.scenario, data={
            "avg_uA": round(avg, 3) if avg is not None else None,
            "elapsed_s": round(writer.elapsed_s, 1),
            "wall_elapsed_s": round(wall_elapsed_s, 1),
            "duration_s": step.duration_s})
        while self.pause.is_set():
            self._check_cancel()
            time.sleep(0.1)
        try:
            sampler.start()
        except Exception:
            pass
        self._emit("resumed", idx, step.scenario)
        return time.monotonic() - paused_at

    def _measure(self, idx, step, writer, rtt_reader):
        """Pętla pomiaru: czytaj PPK2, karm sesję, raportuj na żywo.
        Koniec, gdy minie OKNO duration_s liczone zegarem (albo wcześniej
        zbierzemy komplet próbek), albo przy cancel. Wątek RTT
        (continuous) stawia auto-etykiety równolegle.

        Okno wyznacza zegar, nie licznik próbek: PPK2 potrafi zgubić
        próbki (USB nie nadąża), a przy warunku „zbieraj, aż będzie
        duration_s * rate próbek” pomiar ciągnął się o tyle dłużej, ile
        danych przepadło – nawet kilkanaście sekund po wyzerowaniu
        odliczania. Braki są danymi, których nie ma, a nie powodem, by
        trzymać płytkę pod pomiarem dłużej: idą do `gaps` /
        `lost_samples`."""
        sampler = self._sampler
        # Decymacja 100 kS/s -> wybrana częstotliwość (writer.sample_rate):
        # uśredniamy grupy po `decim` próbek, resztę przenosimy między
        # odczytami, żeby granice grup się nie rozjeżdżały.
        eff_rate = writer.sample_rate
        decim = max(1, round(sampler.sample_rate / eff_rate))
        target = step.duration_s * eff_rate
        decim_rest = np.empty(0, np.float32)
        stop_rtt = threading.Event()
        rtt_thread = None
        if rtt_reader is not None:
            matcher = LinePatternMatcher(step.labels)
            rtt_thread = threading.Thread(
                target=self._rtt_label_loop,
                args=(rtt_reader, matcher, writer, stop_rtt, idx, step),
                daemon=True)
            rtt_thread.start()

        sampler.start()
        errors = 0
        # Skala zegara: 1.0 na sprzęcie, `speedup` w symulacji. Okno i
        # odliczanie liczą się wtedy w sekundach PRZEBIEGU, a nie realnych
        # (atrapa oddaje próbki tyle razy szybciej).
        scale = self._time_scale
        last_data = time.monotonic()
        sec_sum, sec_n = 0.0, 0
        expected_base = time.monotonic()
        # Odliczanie w UI chodzi po SIATCE co 1 s liczonej od startu pomiaru,
        # nie „1 s od poprzedniego statusu”: odczyt PPK2 i zapis tierów
        # potrafią zjeść kilkadziesiąt ms, a przy „od poprzedniego” ten
        # naddatek kumulował się i sekundy na ekranie robiły się dłuższe.
        next_status = expected_base
        # Okno „teraz”: własna, krótsza siatka. `last_inst` trzyma ostatnie
        # ZAMKNIĘTE okno, żeby wartość nie zależała od tego, ile milisekund
        # przed statusem akurat wpadł ostatni odczyt.
        inst_sum, inst_n = 0.0, 0
        next_inst = expected_base + INST_WINDOW_S
        last_inst = None
        reported_deficit = 0
        try:
            while True:
                self._check_cancel()
                if self.pause.is_set():
                    paused_s = self._do_pause(
                        sampler, writer, idx, step,
                        (time.monotonic() - expected_base) * scale)
                    expected_base += paused_s
                    next_status += paused_s
                    # Okno „teraz” przesuwamy tak samo i zaczynamy je od
                    # nowa: próbki sprzed pauzy nie należą do tego samego
                    # kawałka przebiegu co te po wznowieniu.
                    next_inst += paused_s
                    inst_sum, inst_n = 0.0, 0
                    last_data = time.monotonic()
                    continue
                # KONIEC: zamknięte okno czasowe (pauzy się nie liczą) albo
                # komplet próbek. Sprawdzamy PO pauzie, żeby Stop w UI nie
                # skracał pomiaru.
                if ((time.monotonic() - expected_base) * scale
                        >= step.duration_s
                        or writer.samples_written >= target):
                    break
                time.sleep(READ_INTERVAL_S)
                try:
                    chunk = sampler.read()
                except Exception as e:
                    # Zerwany odczyt USB: do 3 restartów pomiaru, potem
                    # polityka kroku.
                    errors += 1
                    if errors > 3:
                        raise AutoRunError(
                            f"PPK2 nie odpowiada po 3 próbach ({e}). Zamknij "
                            "nRF Connect / Power Profiler i sprawdź kabel USB")
                    self._note(f"błąd odczytu PPK2 ({e}) – restart "
                               f"pomiaru {errors}/3", idx, step.scenario)
                    try:
                        sampler.stop()
                        sampler.start()
                    except Exception:
                        pass
                    continue
                now = time.monotonic()
                # Żywotność PPK2 sprawdzamy po SUROWYCH próbkach (decymacja
                # do niskiej częstotliwości i tak oddaje dane rzadko).
                if len(chunk):
                    last_data = now
                elif now - last_data > STALL_TIMEOUT_S:
                    raise AutoRunError(
                        f"PPK2 nie przysłał żadnych próbek przez "
                        f"{STALL_TIMEOUT_S:g} s – zerwane połączenie?")
                if decim > 1:
                    buf = np.concatenate([decim_rest,
                                          np.asarray(chunk, np.float32)])
                    full = len(buf) // decim
                    decim_rest = buf[full * decim:]
                    chunk = (buf[:full * decim].reshape(full, decim)
                             .mean(axis=1).astype(np.float32)
                             if full else np.empty(0, np.float32))
                if len(chunk):
                    over = writer.samples_written + len(chunk) - target
                    if over > 0:
                        chunk = chunk[:len(chunk) - int(over)]
                    writer.write_samples(chunk)
                    # Jedna suma karmi oba okna: sekundowe (status.json dla
                    # viewera) i krótkie („teraz” w UI).
                    chunk_sum = float(chunk.sum())
                    sec_sum += chunk_sum
                    sec_n += len(chunk)
                    inst_sum += chunk_sum
                    inst_n += len(chunk)
                if now >= next_inst:
                    # Puste okno (niska częstotliwość po decymacji, przerwa
                    # w danych) NIE kasuje wskazania – zostaje ostatnie
                    # znane, zamiast migać na „—”.
                    if inst_n:
                        last_inst = inst_sum / inst_n
                    inst_sum, inst_n = 0.0, 0
                    next_inst += INST_WINDOW_S
                    if next_inst <= now:
                        next_inst = now + INST_WINDOW_S
                if now >= next_status:
                    # Rozliczenie zgubionych próbek: ile powinno przyjść
                    # wg zegara vs ile przyszło (w jednostkach efektywnej
                    # częstotliwości; nadwyżka deficytu -> meta.gaps).
                    wall_elapsed = (now - expected_base) * scale
                    expected = wall_elapsed * eff_rate
                    deficit = int(expected - writer.samples_written
                                  - reported_deficit)
                    if deficit > eff_rate * 0.2:
                        writer.record_gap(deficit)
                        reported_deficit += deficit
                    # „teraz” = ostatnie zamknięte okno INST_WINDOW_S; przy
                    # pomiarze krótszym niż to okno bierzemy to, co jest,
                    # żeby pierwszy status nie pokazywał „—”.
                    inst = last_inst
                    if inst is None and inst_n:
                        inst = inst_sum / inst_n
                    avg = writer.avg_uA           # skumulowana od startu
                    # status.json zostaje przy średniej SEKUNDOWEJ – pole
                    # nazywa się avg_1s_uA i viewer czyta je jako sekundę.
                    writer.update_status(
                        "measuring", sec_sum / sec_n if sec_n else None)
                    self._emit("live", idx, step.scenario, data={
                        "avg_uA": round(avg, 3) if avg is not None else None,
                        "inst_uA": round(inst, 3) if inst is not None else None,
                        "elapsed_s": round(writer.elapsed_s, 1),
                        # Czas ZEGAROWY pomiaru (bez pauz) – tylko do
                        # odliczania w UI. `elapsed_s` liczy się próbkami,
                        # więc przy zgubionych próbkach zostaje w tyle za
                        # rzeczywistością i odliczanie potrafiło pokazać tę
                        # samą sekundę dwa razy z rzędu.
                        "wall_elapsed_s": round(wall_elapsed, 1),
                        "duration_s": step.duration_s,
                        "samples": writer.samples_written})
                    sec_sum, sec_n = 0.0, 0
                    # Kolejny punkt siatki; po dłuższym zacięciu (np. restart
                    # odczytu) łapiemy najbliższą przyszłą sekundę zamiast
                    # nadrabiać serią zaległych statusów.
                    next_status += 1.0
                    if next_status <= now:
                        next_status = now + 1.0
            # Domknij rozliczenie braków: między ostatnim statusem a końcem
            # okna też mogło ich zabraknąć, a od kiedy okno wyznacza zegar,
            # zgubione próbki to JEDYNY ślad po tym, że dane są dziurawe
            # (wcześniej widać je było jako przeciągnięty pomiar).
            missing = int(target - writer.samples_written - reported_deficit)
            if missing > eff_rate * 0.2:      # ten sam próg co w pętli
                writer.record_gap(missing)
                reported_deficit += missing
            if reported_deficit > eff_rate * 0.5:    # ponad pół sekundy
                self._note(
                    f"pomiar {idx} ({step.scenario}): PPK2 zgubiło "
                    f"{reported_deficit} próbek "
                    f"(~{reported_deficit / eff_rate:.1f} s z "
                    f"{step.duration_s:g} s) – USB nie nadążyło; okno "
                    "pomiaru zamknięte zgodnie z zegarem",
                    idx, step.scenario)
        finally:
            stop_rtt.set()
            try:
                sampler.stop()
            except Exception:
                pass
            if rtt_thread is not None:
                rtt_thread.join(timeout=3)
            if rtt_reader is not None:
                rtt_reader.detach()

    @staticmethod
    def _lost_fraction(writer):
        """Jaka część okna pomiaru przepadła (0..1). Braki są w `gaps`;
        mianownik to całe okno, czyli to, co przyszło + to, co zginęło."""
        lost = sum(g[1] for g in writer.gaps)
        total = lost + writer.samples_written
        return (lost / total) if total else 0.0

    def _measure_with_retry(self, idx, step, session_dir, writer, rtt_reader,
                            new_writer, run_log):
        """Pomiar z kontrolą strat. Gdy PPK2 zgubi więcej niż
        MAX_LOST_FRACTION okna, wynik jest bezwartościowy – powtarzamy
        pomiar raz, do świeżej sesji (stara zostaje na dysku jako
        'discarded', żeby dało się dojść, co się stało). Druga porażka to
        błąd kroku: śmieciowy pomiar NIE ma prawa trafić do dziennika jako
        zdrowy. Zwraca (katalog_sesji, writer) użytego pomiaru."""
        for attempt in range(1, MEASURE_ATTEMPTS + 1):
            self._measure(idx, step, writer, rtt_reader)
            lost = self._lost_fraction(writer)
            if lost <= MAX_LOST_FRACTION:
                return session_dir, writer
            msg = (f"pomiar {idx} ({step.scenario}): PPK2 zgubiło "
                   f"{lost:.0%} okna (limit {MAX_LOST_FRACTION:.0%})")
            if attempt >= MEASURE_ATTEMPTS:
                writer.finalize("lossy")
                raise AutoRunError(
                    f"{msg} – również przy powtórzeniu. Wynik odrzucony: "
                    "przy takich brakach średnia nie opisuje przebiegu. "
                    "Odciąż komputer (zamknij nRF Connect, przeglądarkę), "
                    "użyj innego portu USB albo obniż 'Próbki na sekundę'")
            self._note(f"{msg} – powtarzam pomiar", idx, step.scenario,
                       files=(run_log,))
            writer.finalize("discarded")
            session_dir = new_session_dir(self.run_dir, step.scenario)
            writer = new_writer(session_dir)
            # UI zaczyna kartę pomiaru od nowa (odliczanie, podgląd sesji).
            self._emit("state", idx, step.scenario, "measure")
            self._emit("session", idx, step.scenario,
                       data={"dir": str(session_dir), "live": True})

    def _rtt_label_loop(self, reader, matcher, writer, stop, idx, step):
        """Wątek auto-etykiet (rtt='continuous'): każda linia RTT do
        rtt.log, linie pasujące do reguł -> adnotacje sesji. Czas
        etykiety = bieżąca pozycja pomiaru (próbki/rate)."""
        with open(writer.dir / "rtt.log", "a", encoding="utf-8") as log:
            while not stop.is_set():
                try:
                    line = reader.readline(timeout_s=0.5)
                except RttError as e:
                    self._note(f"RTT przerwane w trakcie pomiaru: {e}",
                               idx, step.scenario)
                    return
                if line is None:
                    continue
                log.write(line + "\n")
                log.flush()
                hit = matcher.match(line)
                if hit is not None:
                    label, pattern = hit
                    t_s = writer.elapsed_s
                    writer.annotate(t_s, label, pattern=pattern,
                                    rtt_line=line)
                    self._emit("annotation", idx, step.scenario, label,
                               t_s=round(t_s, 3))

    # ---------- krok ----------

    def _run_step(self, idx, step, workspace):
        scen = self.scenarios[step.scenario]
        voltage = self._voltage_for(step)
        # Kontekst dla _emit: etykieta "N.M" (albo numer) i para sweepa.
        self._cur_label = step.label or str(idx)
        self._cur_sweep = _sweep_payload(step)
        self._emit("step_start", idx, step.scenario,
                   data={"duration_s": step.duration_s,
                         "voltage": voltage})
        # BUILD tuż przed pomiarem (just-in-time). Build padł + skip ->
        # ten krok jako build_failed (bez flasha/pomiaru); abort podnosi
        # AutoRunError wyżej.
        build_dir = self._build_step(idx, step, workspace)
        if build_dir is BUILD_FAILED:
            self._emit("state", idx, step.scenario, "build_failed")
            return StepResult(idx, step.scenario, "build_failed")
        if self.dry_run:
            return self._dry_step(idx, step, scen, voltage, build_dir,
                                  workspace)
        session_dir = new_session_dir(self.run_dir, step.scenario)
        run_log = open(session_dir / "run.log", "a", encoding="utf-8")
        monitor = None
        try:
            # Zasilanie z PPK2 (source meter) – płytka musi mieć prąd,
            # żeby J-Link mógł ją w ogóle zaprogramować. Twardy limit
            # napięcia: set_voltage odmówi (i podnosi wyjątek) PRZED
            # włączeniem zasilania, więc groźne napięcie nigdy nie trafi
            # na płytkę. Błąd zamieniamy na AutoRunError (polityka kroku).
            self._emit("state", idx, step.scenario, "power",
                       detail=f"{voltage} V")
            self._set_voltage(voltage)
            self._ensure_dut_power(True)
            # Powtórka przy WŁĄCZONYM już wyjściu – bez niej pierwszy pomiar
            # w sesji jechał na napięciu z otwarcia PPK2 (patrz _set_voltage).
            self._set_voltage(voltage)

            self._emit("state", idx, step.scenario, "flash")
            flash_cmd = core.flash_cmd_for(scen, build_dir, self.profile)
            if self.mock is not None:
                self._mock_flash(flash_cmd, idx, step, run_log)
            else:
                rc = self._run_streamed(
                    flash_cmd, workspace, f"flash {step.scenario}", idx,
                    step.scenario, log_file=run_log)
                if rc != 0:
                    raise AutoRunError(f"flash zakończony błędem (kod {rc})")

            if step.power_cycle and not self.dry_run:
                # Czysty zimny start: chwilowe odcięcie zasilania po
                # flashu (stan z sesji programowania nie zostaje).
                self._note("power-cycle płytki (czysty start)", idx,
                           step.scenario, files=(run_log,))
                self._sampler.dut_power(False)
                self._sleep_scaled(0.5)
                self._sampler.dut_power(True)
                self._dut_on = True
                # Po odcięciu i podaniu zasilania regulator dostaje wartość
                # jeszcze raz – pomiar ma jechać na napięciu z planu.
                self._set_voltage(voltage)

            # Monitor dongla (jeśli podano port) startuje PRZED oknem
            # triggera i żyje przez cały pomiar – logi widać przed i podczas.
            monitor = self._start_monitor(idx, step, session_dir)
            rtt_reader = self._wait_trigger(idx, step, session_dir,
                                            run_log, monitor)

            # Symulacja: dostrój przebieg do TEGO kroku (podłoga ze
            # scenariusza, odstęp wybudzeń z parametru serii, nowe
            # losowanie retransmisji). Prawdziwy sampler tej metody nie ma.
            prepare = getattr(self._sampler, "prepare_step", None)
            if prepare is not None:
                prepare(step, scen)

            self._emit("state", idx, step.scenario, "measure")
            self._emit("session", idx, step.scenario,
                       data={"dir": str(session_dir), "live": True})
            hw_rate = (self._sampler.sample_rate
                       if not self.dry_run else 100_000)
            # Efektywna częstotliwość = sprzętowa / decymacja; okno bazowe
            # tieru dobrane tak, by miało >= 1 próbkę (przy niskich rate).
            decim = max(1, round(hw_rate / step.sample_rate))
            eff_rate = hw_rate // decim
            window_ms = max(step.storage.window_ms,
                            math.ceil(1000 / eff_rate))
            def _new_writer(directory):
                return SessionWriter(
                    directory,
                    meta=self._session_meta(idx, step, scen, voltage,
                                            build_dir),
                    sample_rate=eff_rate,
                    storage_mode=step.storage.mode,
                    window_ms=window_ms)

            writer = _new_writer(session_dir)
            if self.dry_run:
                summary = writer.finalize("done")
                return StepResult(idx, step.scenario, "done",
                                  session_dir, summary)
            try:
                session_dir, writer = self._measure_with_retry(
                    idx, step, session_dir, writer, rtt_reader,
                    _new_writer, run_log)
            except _Cancelled:
                summary = writer.finalize("cancelled")
                self._append_csv(step, scen, voltage, summary,
                                 session_dir, "przerwano",
                                 build_dir=build_dir)
                return StepResult(idx, step.scenario, "cancelled",
                                  session_dir, summary)
            summary = writer.finalize("done")
            self._append_csv(step, scen, voltage, summary, session_dir,
                             build_dir=build_dir)
            self._emit("step_done", idx, step.scenario, data=summary)
            self._note(f"krok {idx} ({step.scenario}): "
                       f"avg {summary.get('avg_uA')} µA, "
                       f"min {summary.get('min_uA')} µA, "
                       f"max {summary.get('max_uA')} µA", idx,
                       step.scenario, files=(run_log,))
            return StepResult(idx, step.scenario, "done", session_dir,
                              summary)
        except _TriggerTimeout as e:
            self._fail_meta(session_dir, idx, step, scen, voltage,
                            "trigger_timeout", str(e))
            return StepResult(idx, step.scenario, "trigger_timeout",
                              session_dir, error=str(e))
        except AutoRunError as e:
            self._fail_meta(session_dir, idx, step, scen, voltage,
                            "error", str(e))
            return StepResult(idx, step.scenario, "error", session_dir,
                              error=str(e))
        finally:
            # Subskrypcja chip żyła przez pomiar – zamknij ją (skrypt ubija
            # chip-tool na SIGTERM). Robimy to PRZED monitorem/logiem, żeby
            # zwolnić Thread/CASE, niezależnie od tego, jak krok się skończył.
            if self._chip is not None:
                self._chip.stop()
                self._chip = None
            if monitor is not None:
                monitor.stop()
            run_log.close()

    def _dry_step(self, idx, step, scen, voltage, build_dir, workspace):
        """Krok w trybie dry-run: pokaż komendy flash + trigger, ale nie
        dotykaj sprzętu ani dysku sesji."""
        self._emit("state", idx, step.scenario, "power",
                   detail=f"{voltage} V")
        self._emit("state", idx, step.scenario, "flash")
        self._run_streamed(core.flash_cmd_for(scen, build_dir,
                                              self.profile),
                           workspace, f"flash {step.scenario}", idx,
                           step.scenario)
        self._wait_trigger(idx, step, None, None)
        self._emit("state", idx, step.scenario, "measure",
                   detail=f"{step.duration_s:g} s (dry-run – bez pomiaru)")
        return StepResult(idx, step.scenario, "done")

    def _mock_flash(self, cmd, idx, step, run_log):
        """Flash w symulacji: komenda idzie do logu i do panelu tak jak
        prawdziwa (zwijana sekcja wygląda bez zmian), ale nikt jej nie
        wykonuje – nie ma płytki ani programatora."""
        title = f"flash {step.scenario}"
        header = f"$ {shlex.join(cmd)}"
        self._log(f"{title}: {header}", files=(run_log,))
        self._emit("cmd_start", idx, step.scenario, text=title)
        self._emit("line", idx, step.scenario, header)
        self._emit("line", idx, step.scenario,
                   "[SYMULACJA] flash pominięty – bez płytki i programatora")
        self._emit("cmd_end", idx, step.scenario,
                   data={"rc": 0, "title": title})

    def _session_meta(self, idx, step, scen, voltage, build_dir):
        return {"plan": self.plan.name, "step": idx,
                "step_label": step.label or str(idx),
                "scenario": step.scenario,
                "label": scen.get("label", step.scenario),
                "sweep": _sweep_payload(step),
                "flags": core.scenario_flags(scen)
                + (" " + " ".join(step.build_extra_args)
                   if step.build_extra_args else ""),
                "board": self.profile["board"],
                "sample": self.sample,
                "voltage_V": voltage,
                "build_dir": build_dir if isinstance(build_dir, str)
                else None,
                "duration_s": step.duration_s,
                # `seconds` = realne czekanie (z podłogą), nie życzenie
                # z planu – meta ma opisywać ten pomiar, nie zamiar.
                "trigger": {"type": step.trigger.type,
                            "seconds": effective_delay_s(step.trigger),
                            "pattern": step.trigger.pattern,
                            **({"node_id": step.trigger.node_id,
                                "cluster": step.trigger.cluster,
                                "attribute": step.trigger.attribute,
                                "endpoint": step.trigger.endpoint}
                               if step.trigger.type == "chip" else {})},
                "rtt": step.rtt}

    def _fail_meta(self, session_dir, idx, step, scen, voltage, status,
                   error):
        """Krok padł przed/po pomiarze: minimalne meta.json, żeby sesja
        była widoczna w bibliotece z powodem błędu."""
        self._note(f"krok {idx} ({step.scenario}): {status} – {error}",
                   idx, step.scenario)
        if not (session_dir / "meta.json").is_file():
            _atomic_json(session_dir / "meta.json", {
                "schema_version": 1, "state": status, "error": error,
                "start": datetime.now().isoformat(timespec="seconds"),
                **self._session_meta(idx, step, scen, voltage, None)})

    def _append_csv(self, step, scen, voltage, summary, session_dir,
                    note="", build_dir=None):
        if not summary.get("samples"):
            return
        # Zmyślone µA nie mają prawa wejść do dziennika, z którego czytamy
        # wyniki i rysujemy wykresy. Sesja zostaje (w sessions-mock), więc
        # przebieg da się obejrzeć – po prostu nie ma go w pomiary.csv.
        if self.mock is not None:
            self._note("SYMULACJA: wynik NIE trafia do dziennika pomiarów")
            return
        # Domyślna 'uwaga': plan + (dla serii) sweepowany parametr i jego
        # wartość, żeby kolumna niosła treść nawet w widokach bez kolumn
        # parametr/wartosc.
        note_default = f"autorun: plan {self.plan.name}"
        for param, value in step.sweep:
            note_default += f" · {param}={value}"
        row = core.make_row(step.scenario, scen, self.profile,
                            self.sample, voltage, summary["avg_uA"],
                            note or note_default,
                            build_dir=build_dir if isinstance(build_dir, str)
                            else None)
        # scenario_flags() nie zna build_extra_args (są per krok, nie w
        # manifeście) – dokładamy je, żeby kolumna 'flagi' oddawała
        # faktycznie zbudowany obraz (bez tego wartość sweepa ginie w CSV).
        if step.build_extra_args:
            extra = " ".join(step.build_extra_args)
            row["flagi"] = f"{row['flagi']} {extra}".strip()
        row.update({
            "prad_min_uA": summary["min_uA"],
            "prad_max_uA": summary["max_uA"],
            "czas_s": summary["duration_s"],
            "sesja": str(Path(session_dir).relative_to(core.ROOT)),
            "pomiar_id": step.label})
        # Osie serii w kolumnach parametr/wartosc i parametr2/wartosc2.
        # Krok spoza serii zostawia je puste, jednoosiowy – tylko drugą parę.
        # Dziennik ma dwie pary kolumn (tyle wystawia interfejs); komplet
        # flag – ile by ich nie było – jest w kolumnie 'flagi' i w 'uwagi'.
        for (param, value), (col_p, col_v) in zip(
                step.sweep, (("parametr", "wartosc"),
                             ("parametr2", "wartosc2"))):
            row[col_p], row[col_v] = param, value
        core.append_row(row, verbose=False)

    # ---------- przebieg ----------

    def run(self):
        """Wykonaj cały plan; zwraca listę StepResult (po jednym na
        krok). Wyjątki AutoRunError = twarde zatrzymanie planu."""
        errors = validate_plan(self.plan, self.manifest)
        errors += core.validate_scenarios(
            sorted({s.scenario for s in self.plan.steps
                    if s.scenario in self.scenarios}), self.scenarios)
        if errors:
            raise AutoRunError("\n  ".join(
                [f"błędy planu '{self.plan.name}':"] + errors))

        # Symulacja pisze do OSOBNEGO katalogu: prawdziwe dane pomiarowe
        # zostają nietknięte, a sessions-mock można kasować bez myślenia.
        sessions_root = core.CSV_PATH.parent / (
            "sessions-mock" if self.mock is not None else "sessions")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.dry_run:
            self.run_dir = sessions_root / f"{stamp}_{self.plan.name}"
        else:
            # Katalog przebiegu przez new_session_dir, nie mkdir(): dwa
            # przebiegi tego samego planu w TEJ SAMEJ sekundzie (restart
            # zaraz po Esc) trafiały na istniejącą nazwę i przewracały się
            # na FileExistsError. Tu kolizja dokleja licznik, jak w sesjach
            # kroków.
            self.run_dir = new_session_dir(sessions_root, self.plan.name)
            self._plan_log = open(self.run_dir / "plan.log", "a",
                                  encoding="utf-8")
        results = []
        try:
            self._emit("plan_start", text=self.plan.name,
                       data={"steps": len(self.plan.steps),
                             "run_dir": str(self.run_dir)})
            self._log(f"plan {self.plan.name}: {len(self.plan.steps)} "
                      f"krok(ów), profil {self.prof_name} "
                      f"({self.profile['board']}), egzemplarz "
                      f"{self.sample}")
            if self.mock is not None:
                self._note(f"SYMULACJA ({self.mock.summary}): bez PPK2, bez "
                           "programatora i bez płytki. Liczby są zmyślone, "
                           "sesje idą do sessions-mock, dziennik pomiarów "
                           "zostaje nietknięty")

            # Blokada usypiania na cały przebieg (patrz INHIBIT_WHAT).
            # Brak blokady nie zatrzymuje pomiaru – tylko ostrzegamy, żeby
            # dało się później zrozumieć urwany strumień próbek.
            # Symulacja trwa sekundy i nie ma czego chronić – blokady
            # usypiania nie bierzemy (nie odpalamy procesu bez powodu).
            if not self.dry_run and self.mock is None:
                self._inhibitor = _SleepInhibitor(
                    f"pomiar prądu: plan {self.plan.name}")
                what, why_not = self._inhibitor.start()
                if what:
                    self._note(f"blokada usypiania na czas przebiegu ({what})")
                else:
                    self._note("UWAGA: nie udało się zablokować usypiania "
                               f"({why_not}) – system może uśpić maszynę "
                               "w środku pomiaru")

            # Cudza sesja J-Linka zawyża pomiar (patrz core.jlink_owners).
            # Przebiegu NIE blokujemy – może startować z crona/SSH bez
            # nikogo przy klawiaturze – ale wpisujemy to do plan.log i
            # pokazujemy w UI, żeby wynik dał się później zinterpretować.
            # W symulacji nie tykamy sondy, więc cudza sesja J-Linka nie ma
            # jak zawyżyć pomiaru – ostrzeżenie byłoby tylko szumem.
            if not self.dry_run and self.mock is None:
                owners = core.jlink_owners()
                if owners:
                    self._note("UWAGA: " + core.jlink_conflict_message(owners))

            needs_west = any("hex" not in self.scenarios[s.scenario]
                             for s in self.plan.steps)
            workspace = core.ROOT
            if needs_west and not self.dry_run:
                workspace = core.find_west_workspace()
                if workspace != core.ROOT:
                    self._note(f"workspace NCS: {workspace} "
                               "(build out-of-tree)")

            # PPK2 otwierany raz na cały przebieg (zasilanie/pomiar); build
            # go nie używa, więc kolejność build->zasilanie->flash->pomiar
            # per krok jest bezpieczna.
            if not self.dry_run:
                self._sampler = self.sampler_factory(self.plan)
                self._sampler.open()
                self._note(f"PPK2 otwarty ({getattr(self._sampler, 'port', '?')})")

            self._emit("phase", text=f"Pomiary: {len(self.plan.steps)} "
                                     "(build każdego kodu tuż przed jego "
                                     "pomiarem)")
            for idx, step in enumerate(self.plan.steps, 1):
                self._check_cancel()
                result = self._run_step(idx, step, workspace)
                result.label = step.label or str(idx)
                results.append(result)
                if result.status == "cancelled":
                    break
                if (result.status in ("error", "trigger_timeout")
                        and self.plan.on_step_error == "abort"):
                    raise AutoRunError(
                        f"krok {idx} ({step.scenario}): {result.error}; "
                        "plan ma on_step_error = 'abort'")
            # PPK2 zwalniamy PRZED ogłoszeniem końca planu: 'plan_done'
            # odblokowuje w UI wyjście z ekranu, a więc i start kolejnego
            # przebiegu. Gdy zamykanie zostawało na później, nowy przebieg
            # trafiał na wciąż otwarte (i wciąż nadające) PPK2 – stąd
            # „po Esc trzeba zrestartować PPK2”.
            self._close_sampler()
            self._emit("plan_done", data={
                "results": [(r.index, r.scenario, r.status)
                            for r in results]})
            return results
        except _Cancelled:
            self._note("przerwano plan (Esc)")
            self._close_sampler()
            self._emit("plan_done", data={"cancelled": True})
            return results
        finally:
            self._close_sampler()          # awaryjnie, gdy poleciał wyjątek
            self._release_inhibitor()
            if self._plan_log is not None:
                self._plan_log.close()
                self._plan_log = None

    def _release_inhibitor(self):
        """Zwolnij blokadę usypiania. Idempotentne – po przebiegu maszyna
        znów usypia normalnie, także gdy przebieg padł albo go przerwano."""
        if self._inhibitor is None:
            return
        inhibitor, self._inhibitor = self._inhibitor, None
        inhibitor.stop()

    def _close_sampler(self):
        """Odetnij zasilanie płytki i zwolnij PPK2. Idempotentne – wołane
        na każdej ścieżce wyjścia z run()."""
        if self._sampler is None:
            return
        sampler, self._sampler = self._sampler, None
        try:
            if self._dut_on:
                sampler.dut_power(False)
                self._dut_on = False
        except Exception:
            pass                     # close() i tak odcina zasilanie
        sampler.close()


class _Cancelled(Exception):
    pass


class _TriggerTimeout(Exception):
    pass


BUILD_FAILED = object()    # znacznik w mapie buildów FAZY 1
