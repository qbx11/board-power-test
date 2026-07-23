# ============================================================
#  Fake sprzęt do testów trybu autonomicznego (autorun)
# ============================================================
# PPK2 i J-Link nie są dostępne w CI, a i tak nie chcemy zależeć od
# fizycznego sprzętu. FakeSampler generuje syntetyczny przebieg prądu
# (baza snu + okresowe piki), FakeRttReader oddaje zaplanowane linie
# logu. Oba mają DOKŁADNIE ten sam kształt co Ppk2ApiSampler /
# PylinkRttReader – silnik nie wie, że rozmawia z atrapą.

import threading
import time

import numpy as np


class FakeSampler:
    """Sampler bez sprzętu: syntetyczny przebieg (µA) generowany w
    tempie zbliżonym do prawdziwego PPK2, ale w skróconej skali czasu
    (sample_rate ustawiany w teście, np. 1000 zamiast 100000, żeby
    krótki pomiar dał sensowną liczbę próbek szybko)."""

    def __init__(self, port="", sample_rate=1000, baseline=1.0,
                 spike=500.0, period_s=0.1, active_s=0.005):
        self.sample_rate = sample_rate
        self.baseline = baseline
        self.spike = spike
        self.period_s = period_s
        self.active_s = active_s
        self.port = "FAKE"
        self.voltage_mV = None
        self.dut = False
        self._running = False
        self._t0 = None
        self._emitted = 0
        self.log = []                 # ślad wywołań do asercji w testach

    def open(self):
        self.log.append("open")

    def set_voltage(self, millivolts):
        self.voltage_mV = int(millivolts)
        self.log.append(f"voltage={self.voltage_mV}")

    def dut_power(self, on):
        self.dut = bool(on)
        self.log.append(f"dut={'ON' if on else 'OFF'}")

    def start(self):
        self._running = True
        self._t0 = time.monotonic()
        self._emitted = 0
        self.log.append("start")

    def read(self):
        """Zwróć tyle próbek, ile 'upłynęło' od ostatniego read()."""
        if not self._running:
            return np.empty(0, np.float32)
        elapsed = time.monotonic() - self._t0
        want = int(elapsed * self.sample_rate)
        n = want - self._emitted
        if n <= 0:
            return np.empty(0, np.float32)
        idx = np.arange(self._emitted, self._emitted + n)
        self._emitted = want
        return self._waveform(idx)

    def _waveform(self, idx):
        t = idx / self.sample_rate
        phase = np.mod(t, self.period_s)
        active = phase < self.active_s
        out = np.full(len(idx), self.baseline, np.float32)
        out[active] = self.spike
        return out

    def stop(self):
        self._running = False
        self.log.append("stop")

    def close(self):
        self.log.append("close")


class FakeRttReader:
    """Czytnik RTT bez J-Linka: oddaje zaplanowane linie. `script` to
    lista (opóźnienie_s, tekst) – linia pojawia się po tym czasie od
    attach(). Bez skryptu readline() zwraca None (cisza na konsoli)."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self._attached_at = None
        self._idx = 0
        self.attached = False
        self.detached = False
        self.log = []

    def attach(self):
        self._attached_at = time.monotonic()
        self.attached = True
        self.log.append("attach")

    def readline(self, timeout_s=1.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._idx >= len(self.script):
                time.sleep(0.01)
                continue
            delay, text = self.script[self._idx]
            if time.monotonic() - self._attached_at >= delay:
                self._idx += 1
                return text
            time.sleep(0.005)
        return None

    def detach(self):
        self.attached = False
        self.detached = True
        self.log.append("detach")


class FakeSerialReader(FakeRttReader):
    """Monitor dongla bez sprzętu – ten sam interfejs co FakeRttReader
    (attach/readline/detach ze skryptem (opóźnienie_s, tekst))."""
    pass
