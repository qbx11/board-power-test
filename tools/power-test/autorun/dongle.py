# ============================================================
#  autorun/dongle.py – monitor logów z portu szeregowego (dongiel)
# ============================================================
# Osobne od RTT źródło logów: nRF52840 Dongle (albo dowolny nadajnik)
# wpięty do komputera po USB CDC, np. węzeł Friend w sieci Mesh. Logi są
# widoczne w aplikacji przed i podczas pomiaru, a pojawienie się zadanego
# FRAGMENTU tekstu (podłańcuch, nie regex) może startować pomiar – bez
# dotykania mierzonej płytki (więc bez narzutu prądowego jak przy RTT).
#
# Jedyny plik dotykający pyserial do odczytu dongla (import leniwy w
# attach()); silnik widzi protokół czytnika, testy podstawiają atrapę.

import time

DEFAULT_BAUD = 115200


class DongleError(RuntimeError):
    pass


class SerialLineReader:
    """Czytnik linii z portu szeregowego dongla. Interfejs jak RttReader:
    attach() otwiera port, readline() skleja bajty w linie, detach()
    zamyka port."""

    def __init__(self, port, baud=DEFAULT_BAUD):
        self.port = port
        self.baud = baud
        self._ser = None
        self._buf = b""

    def attach(self):
        try:
            import serial
        except ImportError:
            raise DongleError("brak biblioteki 'pyserial' – uruchom przez "
                              "`board-power-test` (launcher instaluje "
                              "zależności) albo: pip install pyserial")
        try:
            # timeout=0 = odczyt nieblokujący; sami pętlimy z własnym
            # timeoutem w readline().
            self._ser = serial.Serial(self.port, self.baud, timeout=0)
        except Exception as e:
            self._ser = None
            raise DongleError(
                f"nie mogę otworzyć portu dongla '{self.port}': {e}\n"
                "Sprawdź: dongiel wpięty i działa aplikacja (nie tryb DFU)? "
                "właściwy port (np. /dev/ttyACM0)? nikt inny go nie zajmuje "
                "(zamknij inny monitor/miniterm)?")

    def readline(self, timeout_s=1.0):
        """Jedna linia logu (bez końcówki) albo None po timeout."""
        deadline = time.monotonic() + timeout_s
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line, self._buf = self._buf[:nl], self._buf[nl + 1:]
                return line.rstrip(b"\r").decode("utf-8", errors="replace")
            if time.monotonic() >= deadline:
                return None
            try:
                data = self._ser.read(4096)
            except Exception as e:
                raise DongleError(f"błąd odczytu dongla '{self.port}': {e}")
            if data:
                self._buf += bytes(data)
            else:
                time.sleep(0.02)

    def detach(self):
        if self._ser is None:
            return
        try:
            self._ser.close()
        except Exception:
            pass
        self._ser = None
