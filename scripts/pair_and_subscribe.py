#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pair_and_subscribe.py – po flashu: sparuj węzeł Matter i otwórz subskrypcję
atrybutu MeasuredValue, sygnalizując moment PIERWSZEGO odczytu.

Sekwencja (dokładnie jak w ręcznym przepływie chip-tool):
    1. cd <chip-dir>                       (domyślnie /home/goodbyte/KZ/connectedhomeip)
    2. rm -f /tmp/chip_*                    (świeży stan fabryki/KVS przed parowaniem)
    3. chip-tool pairing ble-thread <node> hex:<dataset> <pin> <discriminator>
    4. chip-tool interactive start         (proces zostaje ŻYWY – subskrypcja trwa)
    5. w konsoli interaktywnej:
       <cluster> subscribe <attribute> <min> <max> <node> <endpoint>
       (domyślnie: temperaturemeasurement subscribe measured-value 1 60 5 1)

Gdy z subskrypcji przyjdzie pierwsza wartość MeasuredValue, skrypt:
    * wypisuje wyraźny marker na stdout (linia zaczyna się od "FIRST-VALUE"),
    * jeśli podano --on-first-value CMD, uruchamia CMD w tle (hak pod start
      pomiaru prądu w aplikacji – to miejsce spina przyszły trigger silnika),
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

    ap.add_argument("--cluster", default="temperaturemeasurement",
                    help="klaster do subskrypcji")
    ap.add_argument("--attribute", default="measured-value",
                    help="atrybut do subskrypcji")
    ap.add_argument("--endpoint", default="1", help="endpoint węzła")
    ap.add_argument("--min-interval", default="1",
                    help="min interval subskrypcji [s]")
    ap.add_argument("--max-interval", default="60",
                    help="max interval subskrypcji [s]")

    ap.add_argument("--match", default=r"(?i)MeasuredValue[^0-9-]*(-?\d+)",
                    help="regex wykrywający pierwszą wartość w raporcie")
    ap.add_argument("--on-first-value", default=None,
                    help="komenda (shell) uruchamiana w tle przy 1. wartości "
                         "– hak pod start pomiaru prądu")
    ap.add_argument("--pair-timeout", type=float, default=180.0,
                    help="limit czasu parowania [s]")
    ap.add_argument("--value-timeout", type=float, default=120.0,
                    help="limit oczekiwania na 1. MeasuredValue [s]")
    return ap


def run_streamed(cmd, cwd, timeout=None):
    """Uruchom komendę, streamuj jej wyjście (stdout+stderr) linia po linii
    z prefiksem. Zwraca kod wyjścia. Rzuca TimeoutError po przekroczeniu."""
    log(f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", bufsize=1)
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        for line in proc.stdout:
            print(f"    | {line.rstrip()}", flush=True)
            if deadline is not None and time.monotonic() > deadline:
                proc.terminate()
                raise TimeoutError
        return proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()


def do_pairing(args, chip_tool):
    dataset = args.dataset
    if not dataset.startswith("hex:"):
        dataset = "hex:" + dataset
    cmd = [chip_tool, "pairing", "ble-thread", args.node_id, dataset,
           args.pin, args.discriminator]
    rc = run_streamed(cmd, cwd=args.chip_dir, timeout=args.pair_timeout)
    if rc != 0:
        log(f"BŁĄD: parowanie zwróciło kod {rc} – przerywam")
        return False
    log("parowanie OK")
    return True


def open_subscription(args, chip_tool):
    """Odpal chip-tool interactive, wyślij komendę subscribe, czekaj na
    pierwszą wartość, potem trzymaj subskrypcję otwartą do sygnału."""
    proc = subprocess.Popen(
        [chip_tool, "interactive", "start"],
        cwd=args.chip_dir, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", bufsize=1)

    sub_cmd = (f"{args.cluster} subscribe {args.attribute} "
               f"{args.min_interval} {args.max_interval} "
               f"{args.node_id} {args.endpoint}")

    # Zamknięcie na Ctrl-C / SIGTERM: ubij interaktywny chip-tool.
    def shutdown(*_):
        log("sygnał kończący – zamykam subskrypcję")
        try:
            proc.terminate()
        except Exception:
            pass
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # REPL potrzebuje chwili na baner startowy, zanim przyjmie komendę.
    def send_subscribe():
        time.sleep(2.0)
        log(f"> {sub_cmd}")
        try:
            proc.stdin.write(sub_cmd + "\n")
            proc.stdin.flush()
        except Exception as e:
            log(f"nie udało się wysłać subscribe: {e}")
    threading.Thread(target=send_subscribe, daemon=True).start()

    rx = re.compile(args.match)
    first_seen = False
    deadline = time.monotonic() + args.value_timeout

    for line in proc.stdout:
        line = line.rstrip()
        print(f"    | {line}", flush=True)
        if not first_seen:
            m = rx.search(line)
            if m:
                first_seen = True
                val = m.group(1) if m.groups() else ""
                # Marker maszynowy – po tym łapie się przyszły trigger silnika.
                print(f"FIRST-VALUE {val}", flush=True)
                log(f"pierwszy MeasuredValue = {val} → subskrypcja otwarta")
                if args.on_first_value:
                    log(f"uruchamiam hak: {args.on_first_value}")
                    subprocess.Popen(args.on_first_value, shell=True)
            elif time.monotonic() > deadline:
                log(f"BŁĄD: brak MeasuredValue w {args.value_timeout:g} s "
                    "– zamykam")
                proc.terminate()
                return 2

    rc = proc.wait()
    log(f"chip-tool interactive zakończył się (kod {rc})")
    return 0 if first_seen else (rc or 1)


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

    if not args.skip_pairing:
        if not do_pairing(args, chip_tool):
            return 1

    return open_subscription(args, chip_tool)


if __name__ == "__main__":
    sys.exit(main())
