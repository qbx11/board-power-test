# ============================================================
#  autorun/rtt.py – konsola RTT przez J-Link (pylink-square)
# ============================================================
# RTT służy dwóm rzeczom w trybie autonomicznym:
#   - trigger startu pomiaru (czekaj na linię pasującą do regexa),
#   - auto-etykiety na wykresie (rtt = "continuous").
# Jedyny plik dotykający `pylink` (import leniwy w attach()) – silnik
# widzi protokół RttReader, testy podstawiają FakeRttReader.
#
# UWAGA sprzętowa: czytanie RTT wymaga AKTYWNEJ sesji J-Link, a
# podłączony debugger dodaje własny prąd. Tryby (pole `rtt` w planie)
# i ich kompromisy są opisane w plans/nocny.example.toml.

import re
import time


class RttError(RuntimeError):
    pass


# Nazwa targetu J-Link z targetu west (np. 'BTZ_EndDevice/nrf54l15/
# cpuapp' -> 'NRF54L15_M33'). Wystarcza rodzina nRF54L15; inne płytki
# można dopisać tutaj.
_DEVICE_MAP = (("nrf54l15", "NRF54L15_M33"),)


def jlink_device_for(board_target):
    t = board_target.lower()
    for needle, device in _DEVICE_MAP:
        if needle in t:
            return device
    raise RttError(f"nie znam nazwy targetu J-Link dla płytki "
                   f"'{board_target}' – dopisz ją do _DEVICE_MAP "
                   "w autorun/rtt.py")


class LinePatternMatcher:
    """Dopasowanie linii RTT do reguł auto-etykiet: lista (regex,
    etykieta) -> match(line) zwraca (etykieta, wzorzec) albo None."""

    def __init__(self, rules):
        # rules: iterable obiektów z .pattern i .label (plan.LabelRule)
        self._rules = [(re.compile(r.pattern), r.label or r.pattern,
                        r.pattern) for r in rules]

    def match(self, line):
        for rx, label, pattern in self._rules:
            if rx.search(line):
                return label, pattern
        return None


class PylinkRttReader:
    """Czytnik RTT na pylink: attach() otwiera sesję J-Link i startuje
    RTT, readline() skleja bajty kanału 0 w linie, detach() zamyka
    sesję (target dalej działa – close bez resetu)."""

    def __init__(self, device, serial_no=None):
        self.device = device
        self.serial_no = serial_no
        self._jlink = None
        self._buf = b""

    def attach(self):
        try:
            import pylink
        except ImportError:
            raise RttError("brak biblioteki 'pylink-square' – uruchom "
                           "przez `board-power-test` (launcher instaluje "
                           "zależności) albo: pip install pylink-square")
        try:
            self._jlink = pylink.JLink()
            self._jlink.open(serial_no=self.serial_no)
            self._jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)
            self._jlink.connect(self.device)
            self._jlink.rtt_start()
        except Exception as e:
            self._jlink = None
            raise RttError(
                f"nie mogę podłączyć się do RTT ({self.device}): {e}\n"
                "Sprawdź: kabel SWD wpięty? J-Link Software zainstalowane "
                "(pylink używa biblioteki SEGGER)? firmware ma włączone "
                "CONFIG_USE_SEGGER_RTT?")
        # RTT potrzebuje chwili na znalezienie control blocku w RAM.
        time.sleep(0.5)

    def readline(self, timeout_s=1.0):
        """Jedna linia logu RTT (bez końcówki) albo None po timeout."""
        deadline = time.monotonic() + timeout_s
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line, self._buf = self._buf[:nl], self._buf[nl + 1:]
                return line.rstrip(b"\r").decode("utf-8",
                                                 errors="replace")
            if time.monotonic() >= deadline:
                return None
            try:
                data = self._jlink.rtt_read(0, 4096)
            except Exception as e:
                raise RttError(f"błąd odczytu RTT: {e}")
            if data:
                self._buf += bytes(data)
            else:
                time.sleep(0.02)

    def detach(self):
        """Zamknij sesję J-Link bez resetu targetu (firmware działa
        dalej) – używane w rtt='trigger' tuż przed oknem pomiaru."""
        if self._jlink is None:
            return
        try:
            self._jlink.rtt_stop()
        except Exception:
            pass
        try:
            self._jlink.close()
        except Exception:
            pass
        self._jlink = None
