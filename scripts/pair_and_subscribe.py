#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pair_and_subscribe.py – po flashu: sparuj węzeł Matter i otwórz subskrypcję
atrybutu MeasuredValue, sygnalizując moment PIERWSZEGO odczytu.

Sekwencja (wszystko w JEDNEJ sesji `chip-tool interactive start`):
    1. cd <chip-dir>                       (domyślnie /home/goodbyte/KZ/connectedhomeip)
    2. rm -f /tmp/chip_*                    (świeży stan fabryki/KVS przed parowaniem)
    3. chip-tool interactive start         (proces zostaje ŻYWY – subskrypcja trwa)
    4. w konsoli interaktywnej, po kolei:
       pairing ble-thread <node> hex:<dataset> <pin> <discriminator> [--icd-registration true …]
       [icdmanagement read operating-mode <node> 0]        (przy --verify-icd)
       <cluster> subscribe <attribute> <min> <max> <node> <endpoint>

DLACZEGO parowanie jest W ŚRODKU sesji interaktywnej, a nie osobnym procesem:
zarejestrowany LIT ICD (patrz --icd-registration) usypia na CAŁY LIT slow poll,
u nas nawet na godzinę. Wiadomość CASE Sigma1 wysłana do śpiącego węzła leży
w buforze routera-rodzica, aż dziecko zapolluje – handshake nie ma szans
i kończy się timeoutem. Parując w tej samej sesji, subskrypcja wchodzi na
sesję CASE zostawioną przez commissioning ("Found an existing secure session"),
gdy węzeł jest jeszcze w ActiveMode. Zimnego CASE do śpiącego LIT-a nie da się
nawiązać w ogóle – dlatego rozdzielenie na dwa procesy (jak było wcześniej)
działa tylko w SIT, gdzie poll ma najwyżej 15 s.

Gdy z subskrypcji przyjdzie pierwsza wartość MeasuredValue, skrypt:
    * wypisuje wyraźny marker na stdout (linia zaczyna się od "FIRST-VALUE"),
    * jeśli podano --on-first-value CMD, uruchamia CMD w tle (hak pod start
      pomiaru prądu w aplikacji – to miejsce spina trigger silnika),
a potem TRZYMA subskrypcję otwartą aż do Ctrl-C / SIGTERM.

Wszystkie parametry są konfigurowalne (--help). Domyślne wartości odpowiadają
setupowi z rozmowy (node 5, endpoint 1, ten dataset Thread, pin 20202021,
discriminator 3840, subskrypcja 1/60 s).
"""

import argparse
import glob
import os
import re
import signal
import subprocess
import sys
import threading
import time

# --- domyślne wartości setupu -------------------------------------------------

CHIP_DIR_DEFAULT = "/home/goodbyte/KZ/connectedhomeip"
CHIP_TOOL_DEFAULT = "./out/linux-x64-chip-tool/chip-tool"
KVS_GLOB_DEFAULT = "/tmp/chip_*"

# Dataset Thread (operational dataset) w hex – bez prefiksu "hex:", skrypt go
# dokłada sam. Wartość z rozmowy; nadpiszesz przez --dataset.
DATASET_DEFAULT = (
    "0e08000000000001000000030000174a0300000e35060004001fffe0"
    "0208813ba4b5a068fddf0708fddc8e685e36d6cc0510b840138392a6efbee6"
    "1680bdca9ae7fd030f4f70656e5468726561642d666236650102fb6e04108c"
    "97ec5b81873b78c371537a24886bef0c0402a0f7f8"
)

# Markery w wyjściu chip-toola (examples/chip-tool/commands/pairing/
# PairingCommand.cpp:539,553 oraz commands/common/Commands.cpp:179).
PAIR_OK_RE = r"Device commissioning completed with success"
PAIR_FAIL_RE = r"Device commissioning Failure|Run command failure"
# Atrybuty klastra ICD Management czytane przy --verify-icd.
OPERATING_MODE_RE = r"OperatingMode:\s*(\d+)"
REGISTERED_CLIENTS_RE = r"RegisteredClients:\s*(\d+) entries"

# chip-tool koloruje wyjście; kody ANSI psułyby regexy i czytelność logu.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def log(msg):
    """Log skryptu (odróżnialny od wyjścia chip-toola prefiksem)."""
    print(f"[pair-sub] {msg}", flush=True)


def build_argparser():
    ap = argparse.ArgumentParser(
        description="Parowanie Matter + subskrypcja MeasuredValue po flashu.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--chip-dir", default=CHIP_DIR_DEFAULT,
                    help="katalog connectedhomeip (cwd dla chip-toola)")
    ap.add_argument("--chip-tool", default=CHIP_TOOL_DEFAULT,
                    help="ścieżka do binarki chip-tool (względem --chip-dir)")
    ap.add_argument("--kvs-glob", default=KVS_GLOB_DEFAULT,
                    help="wzorzec plików KVS do skasowania przed parowaniem")
    ap.add_argument("--no-wipe", action="store_true",
                    help="nie kasuj KVS (pomiń rm -f); użyj gdy węzeł już sparowany")
    ap.add_argument("--skip-pairing", action="store_true",
                    help="pomiń parowanie, od razu otwórz subskrypcję "
                         "(węzeł już w fabryce)")

    ap.add_argument("--node-id", default="5", help="Node ID węzła")
    ap.add_argument("--dataset", default=DATASET_DEFAULT,
                    help="operational dataset Thread w hex (bez 'hex:')")
    ap.add_argument("--pin", default="20202021", help="setup PIN code")
    ap.add_argument("--discriminator", default="3840", help="discriminator")

    ap.add_argument("--icd-registration", action="store_true",
                    help="zarejestruj kontroler jako klienta check-in ICD "
                         "podczas parowania – BEZ tego urządzenie z "
                         "CHIP_ICD_LIT_SUPPORT pracuje jako SIT i pollue "
                         "co najwyżej co SIT_SLOW_POLL_LIMIT")
    ap.add_argument("--icd-stay-active-duration", type=int, default=30000,
                    help="ile ms LIT ICD ma zostać aktywny po parowaniu "
                         "(okno na subskrypcję); urządzenie i tak obcina do "
                         "30000 – kGuaranteedStayActiveDuration")
    ap.add_argument("--verify-icd", action="store_true",
                    help="przed subskrypcją odczytaj OperatingMode i "
                         "RegisteredClients; przy --icd-registration "
                         "OperatingMode != 1 (LIT) przerywa pomiar")

    ap.add_argument("--cluster", default="temperaturemeasurement",
                    help="klaster do subskrypcji")
    ap.add_argument("--attribute", default="measured-value",
                    help="atrybut do subskrypcji")
    ap.add_argument("--endpoint", default="1", help="endpoint węzła")
    ap.add_argument("--min-interval", default="1",
                    help="min interval subskrypcji [s]")
    ap.add_argument("--max-interval", default="60",
                    help="max interval subskrypcji [s]; ICD i tak wynegocjuje "
                         "swoje IdleModeDuration (ReadHandler.cpp)")

    ap.add_argument("--match", default=r"(?i)MeasuredValue[^0-9-]*(-?\d+)",
                    help="regex wykrywający pierwszą wartość w raporcie")
    ap.add_argument("--on-first-value", default=None,
                    help="komenda (shell) uruchamiana w tle przy 1. wartości "
                         "– hak pod start pomiaru prądu")
    ap.add_argument("--pair-timeout", type=float, default=180.0,
                    help="limit czasu parowania [s]")
    ap.add_argument("--verify-timeout", type=float, default=30.0,
                    help="limit czasu odczytu atrybutów ICD [s]")
    ap.add_argument("--value-timeout", type=float, default=120.0,
                    help="limit oczekiwania na 1. MeasuredValue [s]")
    return ap


class ReplDied(Exception):
    """chip-tool interactive zakończył się, zanim doczekaliśmy markera."""


class _Watch:
    """Jeden wzorzec wypatrywany w strumieniu REPL-a."""

    def __init__(self, pattern):
        self.rx = re.compile(pattern)
        self.hit = threading.Event()
        self.value = ""

    def feed(self, line):
        m = self.rx.search(line)
        if m:
            self.value = m.group(1) if m.groups() else line
            self.hit.set()


class Repl:
    """Sesja `chip-tool interactive start` sterowana przez stdin.

    Wątek drenujący czyta stdout przez CAŁE życie procesu – bez tego bufor
    pipe by się zapchał i subskrypcja (a więc raporty) zamarłaby w trakcie
    pomiaru. Każda wysłana komenda jest wykonywana przez REPL do końca,
    zanim ruszy następna, więc kolejność jest zachowana bez synchronizacji."""

    def __init__(self, cmd, cwd):
        self.proc = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
        self.dead = threading.Event()
        self._lock = threading.Lock()
        self._watchers = []
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def watch(self, pattern):
        """Załóż wzorzec. Rób to PRZED wysłaniem komendy, która ma go
        wywołać – szybka odpowiedź zdążyłaby przelecieć przez drenaż."""
        w = _Watch(pattern)
        with self._lock:
            self._watchers.append(w)
        return w

    def _drain(self):
        try:
            for raw in self.proc.stdout:
                line = ANSI_RE.sub("", raw).rstrip()
                print(f"    | {line}", flush=True)
                with self._lock:
                    pending = [w for w in self._watchers if not w.hit.is_set()]
                for w in pending:
                    w.feed(line)
        finally:
            self.dead.set()

    def send(self, cmd):
        log(f"> {cmd}")
        try:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            raise ReplDied(f"nie udało się wysłać komendy: {e}")

    def wait_any(self, watches, timeout, what):
        """Czekaj na PIERWSZY z markerów. Zwraca trafiony _Watch albo None
        przy timeoucie. ReplDied, gdy chip-tool padł wcześniej."""
        deadline = time.monotonic() + timeout
        while True:
            for w in watches:
                if w.hit.is_set():
                    return w
            if self.dead.is_set():
                # Drenaż ustawia `dead` dopiero po przetworzeniu wszystkich
                # linii, ale marker mógł paść między pętlą wyżej a tym
                # sprawdzeniem – dlatego jeszcze jedno spojrzenie.
                for w in watches:
                    if w.hit.is_set():
                        return w
                raise ReplDied(
                    f"chip-tool zakończył się (kod {self.proc.returncode}) "
                    f"w trakcie: {what}")
            if time.monotonic() > deadline:
                return None
            time.sleep(0.05)

    def wait(self, watch, timeout, what):
        return self.wait_any([watch], timeout, what) is not None

    def hold(self):
        """Trzymaj sesję (a więc subskrypcję) aż do śmierci procesu."""
        while not self.dead.wait(0.5):
            pass
        return self.proc.returncode

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def pairing_command(args):
    dataset = args.dataset
    if not dataset.startswith("hex:"):
        dataset = "hex:" + dataset
    parts = ["pairing", "ble-thread", args.node_id, dataset, args.pin,
             args.discriminator]
    if args.icd_registration:
        parts += ["--icd-registration", "true",
                  "--icd-stay-active-duration",
                  str(args.icd_stay_active_duration)]
    return " ".join(parts)


def do_pairing(repl, args):
    """Parowanie w sesji REPL. True = sukces."""
    ok = repl.watch(PAIR_OK_RE)
    bad = repl.watch(PAIR_FAIL_RE)
    repl.send(pairing_command(args))
    hit = repl.wait_any([ok, bad], args.pair_timeout, "parowanie")
    if hit is None:
        log(f"BŁĄD: parowanie nie skończyło się w {args.pair_timeout:g} s")
        return False
    if hit is bad:
        log("BŁĄD: parowanie zakończone niepowodzeniem – przerywam")
        return False
    log("parowanie OK")
    return True


def verify_icd(repl, args):
    """Odczyt OperatingMode + RegisteredClients przed subskrypcją.

    Zwraca False TYLKO wtedy, gdy prosiliśmy o rejestrację, a węzeł
    odpowiedział, że jest w SIT – wtedy pomiar zmierzyłby nie ten tryb,
    co trzeba, i lepiej przerwać niż zapisać nieprawdziwy wiersz. Gdy
    odczyt w ogóle nie dojdzie (np. build bez CHIP_ICD_LIT_SUPPORT nie
    wystawia atrybutu), tylko ostrzegamy – brak odpowiedzi nie dowodzi
    złego trybu."""
    mode = repl.watch(OPERATING_MODE_RE)
    repl.send(f"icdmanagement read operating-mode {args.node_id} 0")
    if not repl.wait(mode, args.verify_timeout, "odczyt operating-mode"):
        log("UWAGA: nie udało się odczytać OperatingMode – jadę dalej, ale "
            "tryb ICD jest niepotwierdzony")
        return True

    # Marker maszynowy, żeby dziennik pomiaru miał tryb wprost.
    print(f"ICD-MODE {mode.value}", flush=True)
    label = {"0": "SIT", "1": "LIT"}.get(mode.value, "?")
    log(f"OperatingMode = {mode.value} ({label})")

    clients = repl.watch(REGISTERED_CLIENTS_RE)
    repl.send(f"icdmanagement read registered-clients {args.node_id} 0")
    if repl.wait(clients, args.verify_timeout, "odczyt registered-clients"):
        log(f"RegisteredClients = {clients.value}")

    if args.icd_registration and mode.value != "1":
        log("BŁĄD: prosiliśmy o rejestrację ICD, a węzeł pracuje w SIT "
            f"(OperatingMode = {mode.value}). Pomiar byłby nie tego trybu "
            "– przerywam")
        return False
    return True


def subscribe(repl, args):
    """Subskrypcja + oczekiwanie na pierwszą wartość. 0 = OK."""
    first = repl.watch(args.match)
    repl.send(f"{args.cluster} subscribe {args.attribute} "
              f"{args.min_interval} {args.max_interval} "
              f"{args.node_id} {args.endpoint}")
    if not repl.wait(first, args.value_timeout, "pierwsza wartość"):
        log(f"BŁĄD: brak MeasuredValue w {args.value_timeout:g} s – zamykam")
        return 2

    # Marker maszynowy – po tym łapie trigger silnika (engine._ChipSession).
    print(f"FIRST-VALUE {first.value}", flush=True)
    log(f"pierwszy MeasuredValue = {first.value} → subskrypcja otwarta")
    if args.on_first_value:
        log(f"uruchamiam hak: {args.on_first_value}")
        subprocess.Popen(args.on_first_value, shell=True)
    return 0


def main():
    args = build_argparser().parse_args()

    chip_tool = args.chip_tool
    tool_abs = (chip_tool if os.path.isabs(chip_tool)
                else os.path.join(args.chip_dir, chip_tool))
    if not os.path.exists(tool_abs):
        log(f"BŁĄD: nie znaleziono chip-tool: {tool_abs}")
        return 1

    if not args.no_wipe:
        stale = glob.glob(args.kvs_glob)
        for p in stale:
            try:
                os.remove(p)
            except OSError:
                pass
        log(f"skasowano KVS: {args.kvs_glob} ({len(stale)} plików)")

    repl = Repl([chip_tool, "interactive", "start"], args.chip_dir)

    # Zamknięcie na Ctrl-C / SIGTERM: ubij interaktywny chip-tool.
    def shutdown(*_):
        log("sygnał kończący – zamykam subskrypcję")
        repl.stop()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        if not args.skip_pairing and not do_pairing(repl, args):
            return 1
        if args.verify_icd and not verify_icd(repl, args):
            return 1
        rc = subscribe(repl, args)
        if rc != 0:
            return rc
    except ReplDied as e:
        log(f"BŁĄD: {e}")
        return 1
    else:
        # Sukces: subskrypcja żyje aż do sygnału (albo śmierci chip-toola).
        repl.hold()
        log(f"chip-tool interactive zakończył się "
            f"(kod {repl.proc.returncode})")
        return 0
    finally:
        # Każda ścieżka błędu MUSI ubić chip-toola – inaczej zostaje żywy
        # proces trzymający sesję CASE i następny krok serii nie sparuje.
        # Po udanym hold() proces już nie żyje i stop() jest no-opem.
        repl.stop()


if __name__ == "__main__":
    sys.exit(main())
