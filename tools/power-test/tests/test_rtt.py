# ============================================================
#  Testy konsoli RTT (autorun/rtt.py)
# ============================================================
# Bez sprzętu: pod `pylink` podstawiamy atrapę (import w rtt.py jest
# leniwy, wewnątrz attach()), więc da się sprawdzić samo OTOCZENIE
# otwierania sesji J-Link – a tam siedzi regresja, która wracała:
# pylink ładuje bibliotekę SEGGERa w NASZYM procesie, omijając child_env()
# z power_test, więc DISPLAY/WAYLAND_DISPLAY trzeba zdjąć ręcznie – bez
# tego J-Link EDU/EDU Mini pokazuje dialog licencyjny i pomiar staje.

import os
import sys
import types
import unittest

import common  # noqa: F401  (dokłada TOOL_DIR do sys.path)

from autorun.rtt import LinePatternMatcher, PylinkRttReader, RttError


class FakeJLink:
    """Atrapa pylink.JLink – zapisuje, co widziała w środowisku."""

    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.seen_env = {}
        self.closed = False

    def _snapshot(self, where):
        self.seen_env[where] = {k: os.environ.get(k)
                                for k in ("DISPLAY", "WAYLAND_DISPLAY")}
        if self.fail_on == where:
            raise OSError("atrapa: sonda niedostępna")

    def open(self, serial_no=None):
        self._snapshot("open")

    def set_tif(self, iface):
        self._snapshot("set_tif")

    def connect(self, device):
        self._snapshot("connect")

    def rtt_start(self):
        self._snapshot("rtt_start")

    def rtt_read(self, channel, size):
        return b""

    def rtt_stop(self):
        pass

    def close(self):
        self.closed = True


def fake_pylink(jlink):
    """Modul-atrapa `pylink` z JLink() i enums.JLinkInterfaces.SWD."""
    mod = types.ModuleType("pylink")
    mod.JLink = lambda: jlink
    enums = types.ModuleType("pylink.enums")
    enums.JLinkInterfaces = types.SimpleNamespace(SWD=1)
    mod.enums = enums
    return mod


class RttDisplayTests(unittest.TestCase):
    """REGRESJA: DISPLAY musi zniknąć na czas otwierania sesji J-Link
    i wrócić po niej (TUI działa dalej w tym samym procesie)."""

    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("DISPLAY", "WAYLAND_DISPLAY")}
        os.environ["DISPLAY"] = ":0"
        os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        self.addCleanup(self._restore)
        self._saved_pylink = sys.modules.get("pylink")

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if self._saved_pylink is None:
            sys.modules.pop("pylink", None)
        else:
            sys.modules["pylink"] = self._saved_pylink

    def _attach(self, fail_on=None):
        jlink = FakeJLink(fail_on=fail_on)
        sys.modules["pylink"] = fake_pylink(jlink)
        reader = PylinkRttReader("NRF54L15_M33")
        return reader, jlink

    def test_display_zdjety_na_czas_otwierania_sesji(self):
        reader, jlink = self._attach()
        reader.attach()
        for where in ("open", "set_tif", "connect", "rtt_start"):
            self.assertEqual(jlink.seen_env[where],
                             {"DISPLAY": None, "WAYLAND_DISPLAY": None},
                             f"DISPLAY widoczny w {where} – wróci dialog EDU")

    def test_display_przywrocony_po_attach(self):
        reader, _ = self._attach()
        reader.attach()
        self.assertEqual(os.environ.get("DISPLAY"), ":0")
        self.assertEqual(os.environ.get("WAYLAND_DISPLAY"), "wayland-0")

    def test_display_przywrocony_takze_po_bledzie(self):
        reader, _ = self._attach(fail_on="connect")
        with self.assertRaises(RttError):
            reader.attach()
        self.assertEqual(os.environ.get("DISPLAY"), ":0")
        self.assertEqual(os.environ.get("WAYLAND_DISPLAY"), "wayland-0")

    def test_detach_zamyka_sesje(self):
        reader, jlink = self._attach()
        reader.attach()
        reader.detach()
        self.assertTrue(jlink.closed)
        reader.detach()               # drugi raz = no-op, bez wyjątku

    def test_brak_pylink_daje_czytelny_blad(self):
        sys.modules["pylink"] = None  # import pylink -> ImportError
        with self.assertRaises(RttError) as ctx:
            PylinkRttReader("NRF54L15_M33").attach()
        self.assertIn("pylink-square", str(ctx.exception))


class MatcherTests(unittest.TestCase):

    def test_pierwsza_pasujaca_regula_wygrywa(self):
        rules = [types.SimpleNamespace(pattern="Poll", label="P"),
                 types.SimpleNamespace(pattern="Poll sent", label="PS")]
        m = LinePatternMatcher(rules)
        self.assertEqual(m.match("Friend Poll sent"), ("P", "Poll"))
        self.assertIsNone(m.match("nic tu nie ma"))

    def test_etykieta_domyslnie_rowna_wzorcowi(self):
        m = LinePatternMatcher([types.SimpleNamespace(pattern="boot",
                                                      label="")])
        self.assertEqual(m.match("boot ok"), ("boot", "boot"))


if __name__ == "__main__":
    unittest.main()
