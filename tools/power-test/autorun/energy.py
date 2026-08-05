# ============================================================
#  Kalkulator poboru prądu – model
# ============================================================
# Węzeł sieci bezprzewodowej (BLE Mesh LPN, Thread SED/LIT, Zigbee SED)
# robi w kółko to samo: śpi, budzi się co T_send żeby wysłać dane i budzi
# się co T_poll żeby odebrać. Średni prąd to więc prąd bezczynności plus po
# jednym składniku na każdy rodzaj wybudzenia:
#
#     I_avg = I_baseline + Q_send / T_send + Q_poll / T_poll
#
# gdzie Q to ŁADUNEK jednego wybudzenia w µC (czyli µA·s). Model jest
# liniowy w 1/T i ten sam dla wszystkich trzech protokołów – różnią się
# tylko nazwy interwałów i typowe wartości, nie matematyka. Dlatego
# kalkulator ma trzy sekcje o identycznej budowie, a nie trzy modele.
#
# Liczby wpisuje człowiek. Narzędzie ich nie zgaduje i nie wylicza
# z zapisanych przebiegów – kalkulator odpowiada na pytanie „ile to weźmie,
# jeśli JEDNO wysłanie kosztuje tyle”, a nie „ile kosztuje wysłanie”.

from dataclasses import dataclass


@dataclass(frozen=True)
class Term:
    """Jeden rodzaj wybudzenia: ładunek `charge_uC` co `period_s` sekund."""
    name: str
    charge_uC: float
    period_s: float

    def current_uA(self):
        """Udział tego składnika w prądzie średnim."""
        if self.period_s <= 0:
            return 0.0
        return self.charge_uC / self.period_s


@dataclass(frozen=True)
class Share:
    """Wiersz rozkładu budżetu: ile µA i jaki procent całości."""
    name: str
    current_uA: float
    percent: float


def average_uA(baseline_uA, terms):
    """Prąd średni z modelu: bezczynność + suma składników."""
    return float(baseline_uA) + sum(t.current_uA() for t in terms)


def budget(baseline_uA, terms):
    """Rozkład prądu średniego na bezczynność i poszczególne wybudzenia.

    Po to, żeby było widać, GDZIE szukać oszczędności: przy interwałach
    z pomiarów tego repo bezczynność to bywa 8% budżetu, a polle 89% –
    skracanie snu nie zmieni tam nic, a wydłużenie polla zmieni wszystko."""
    total = average_uA(baseline_uA, terms)
    rows = [Share("bezczynność", float(baseline_uA), 0.0)]
    rows += [Share(t.name, t.current_uA(), 0.0) for t in terms]
    if total <= 0:
        return rows
    return [Share(r.name, r.current_uA, r.current_uA / total * 100)
            for r in rows]
