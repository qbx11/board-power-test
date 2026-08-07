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
# Wyjątek ma Thread/Matter: po każdej wysyłce węzeł zostaje na chwilę
# w active mode i robi tam jeszcze kilka polli. Kosztują dokładnie tyle,
# co zwykły poll – inna jest tylko ICH LICZBA i to, że wypadają raz na
# interwał send, a nie raz na interwał poll (patrz active_window_term).
#
# I te dwa rodzaje polli NIE są niezależne. Slow poll odzywa się T_poll po
# OSTATNIM pollu, a nie co T_poll od początku świata – okno aktywne
# przestawia mu licznik. Między wysyłkami mieści się więc mniej slow polli,
# niż wychodzi z samego interwału (patrz slow_poll_count):
#
#     I_avg = I_baseline + Q_send/T_send
#                        + n_slow * Q_poll / T_send     (n_slow z licznika)
#                        + n_active * Q_poll / T_send
#
# Praktyczny skutek: PIERWSZY poll w oknie aktywnym jest za darmo. Przy
# slow pollu co 15 s i wysyłce co 60 s zwykłe pollowanie daje 4 polle na
# cykl; jedno okno aktywne z jednym pollem daje 3 slow polle plus ten
# jeden, czyli dalej 4. Dopiero drugi i kolejny poll w oknie coś dokładają.
#
# Liczby wpisuje człowiek. Narzędzie ich nie zgaduje i nie wylicza
# z zapisanych przebiegów – kalkulator odpowiada na pytanie „ile to weźmie,
# jeśli JEDNO wysłanie kosztuje tyle”, a nie „ile kosztuje wysłanie”.

import math
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


# Nazwa składnika „polle w oknie aktywnym po wysłaniu”. Osobna od 'poll',
# bo w rozkładzie budżetu to dwa różne wiersze: zwykłe pollowanie chodzi
# swoim interwałem, a te polle są doklejone do każdej wysyłki.
ACTIVE_WINDOW = "active"


def active_window_term(poll_charge_uC, count, send_period_s,
                       name=ACTIVE_WINDOW):
    """Składnik dla polli w oknie aktywnym po wysłaniu (Thread/Matter).

    Jeden taki poll kosztuje tyle samo, co zwykły – dlatego bierze ten sam
    ładunek. Cała paczka `count` polli wypada raz na interwał send:

        I_active = count * Q_poll / T_send

    Zwraca zwykły Term, więc dalej (średnia, rozkład budżetu) liczy się
    tak samo jak reszta – to nie jest drugi model, tylko trzeci składnik
    tego samego."""
    return Term(name=name,
                charge_uC=float(poll_charge_uC) * float(count),
                period_s=float(send_period_s))


def slow_poll_count(poll_period_s, send_period_s):
    """Ile slow polli wypada MIĘDZY dwiema wysyłkami.

    Licznik slow polla startuje od ostatniego polla w oknie aktywnym, więc
    polle wychodzą w T_poll, 2*T_poll, ... po wysyłce – i liczą się tylko
    te, które zdążą przed następną wysyłką. Ten, który wypadłby dokładnie
    w momencie wysyłki, już się nie liczy: wyprzedza go poll z okna
    aktywnego.

        n_slow = ceil(T_send / T_poll) - 1

    Slow poll co 15 s, wysyłka co 60 s -> 3 (o 15, 30 i 45 s), a nie 4.
    Wysyłka gęstsza niż interwał polla -> 0: slow poll nigdy nie dojrzewa.

    Zaokrąglenie idzie z zapasem 1e-9, bo T_send/T_poll liczone na floatach
    potrafi wyjść 4.000000000000001 – bez zapasu ceil() dorzucałby wtedy
    poll, którego nie ma."""
    if poll_period_s <= 0 or send_period_s <= 0:
        return 0
    ratio = float(send_period_s) / float(poll_period_s)
    return max(0, math.ceil(ratio - 1e-9) - 1)


def slow_poll_term(poll_charge_uC, poll_period_s, send_period_s, name="poll"):
    """Składnik zwykłego pollowania przy węźle z oknem aktywnym.

    To NIE jest Q_poll/T_poll: część slow polli nie dochodzi do skutku, bo
    okno aktywne przestawia im licznik. Liczy się więc tyle polli, ile
    faktycznie mieści się w cyklu, rozłożonych na interwał send."""
    return Term(name=name,
                charge_uC=float(poll_charge_uC)
                * slow_poll_count(poll_period_s, send_period_s),
                period_s=float(send_period_s))


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
