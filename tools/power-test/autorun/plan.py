# ============================================================
#  autorun/plan.py – wczytywanie i walidacja planów (plans/*.toml)
# ============================================================
# Plan = uporządkowana lista kroków; każdy krok wskazuje scenariusz
# z scenarios.toml i nadpisuje szczegóły pomiaru (czas, napięcie,
# trigger startu, tryb RTT, zapis danych, dodatkowe flagi builda).
# Sam manifest scenariuszy zostaje nietknięty – plany żyją w osobnych
# plikach, więc można je wersjonować i wymieniać niezależnie.

import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

RTT_MODES = ("off", "trigger", "continuous")
STORAGE_MODES = ("downsampled", "raw", "both")
# Dozwolone częstotliwości próbkowania [próbki/s]. PPK2 sampluje sprzętowo
# 100 kS/s; niższe wartości silnik uzyskuje przez uśrednianie (decymację).
SAMPLE_RATES = (1, 10, 100, 1000, 10000, 100000)
ERROR_POLICIES = ("skip", "abort")

# --- TWARDY limit napięcia źródła PPK2 podawanego na testowaną płytkę ---
# PPK2 fizycznie potrafi 800–5000 mV (a biblioteka ppk2-api klampuje
# dopiero do 5000 mV), więc bez własnego ograniczenia dałoby się podać
# na płytkę np. 5 V i ją zniszczyć. Trzymamy bezpieczny zakres dla
# układów nRF: minimum 2.0 V, maksimum 3.3 V (włącznie). Limit jest
# egzekwowany DWUKROTNIE: przy walidacji planu (błąd przed startem) i w
# sterowniku PPK2 (autorun/ppk2.py) tuż przed komendą do urządzenia.
VOLTAGE_MIN_MV = 2000
VOLTAGE_MAX_MV = 3300

_DUR_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([smh])", re.IGNORECASE)


def voltage_to_mV(value):
    """Napięcie ('3.3' / '3,3' / 3.3) -> mV (int), z TWARDYM ograniczeniem
    do [VOLTAGE_MIN_MV, VOLTAGE_MAX_MV] włącznie. ValueError przy złym
    formacie albo poza zakresem – nigdy nie klampujemy po cichu, bo to
    napięcie ląduje na fizycznej płytce."""
    try:
        volts = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError(f"napięcie '{value}' nie jest liczbą "
                         "(podaj np. '3.0')")
    mv = round(volts * 1000)
    if not (VOLTAGE_MIN_MV <= mv <= VOLTAGE_MAX_MV):
        raise ValueError(
            f"napięcie {volts:g} V poza dozwolonym zakresem "
            f"{VOLTAGE_MIN_MV / 1000:g}–{VOLTAGE_MAX_MV / 1000:g} V "
            "(twardy limit ochrony testowanej płytki)")
    return mv


def parse_duration(value):
    """Czas -> sekundy (float). Przyjmuje liczbę (sekundy) albo tekst
    z jednostkami: '45s', '20m', '8h', także złożone '1h30m'."""
    if isinstance(value, (int, float)):
        if value <= 0:
            raise ValueError(f"czas musi być dodatni ({value})")
        return float(value)
    s = str(value).strip().lower().replace(",", ".")
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return parse_duration(float(s))
    parts = _DUR_RE.findall(s)
    if not parts or _DUR_RE.sub("", s).strip():
        raise ValueError(f"nie rozumiem czasu '{value}' – podaj np. "
                         "'45s', '20m', '8h', '1h30m' albo liczbę sekund")
    mult = {"s": 1.0, "m": 60.0, "h": 3600.0}
    total = sum(float(num) * mult[unit] for num, unit in parts)
    if total <= 0:
        raise ValueError(f"czas musi być dodatni ({value})")
    return total


@dataclass
class Trigger:
    """Warunek startu pomiaru po flashu: 'delay' = odczekaj N sekund,
    'rtt' = czekaj (max `timeout_s`) na linię logu RTT pasującą do
    regexa `pattern`."""
    type: str = "delay"
    seconds: float = 0.0
    pattern: str = ""
    timeout_s: float = 120.0


@dataclass
class Storage:
    """Zapis próbek: 'downsampled' = tylko tiery min/avg/max (okno
    bazowe `window_ms`), 'raw' = tylko surowe 100 kS/s, 'both' = oba."""
    mode: str = "downsampled"
    window_ms: int = 1


@dataclass
class LabelRule:
    """Auto-etykieta: linia RTT pasująca do `pattern` stawia na wykresie
    znacznik `label` (domyślnie treść wzorca)."""
    pattern: str
    label: str = ""


@dataclass
class PlanStep:
    scenario: str
    duration_s: float
    voltage: str = ""            # puste = z manifestu (scenariusz/defaults)
    trigger: Trigger = field(default_factory=Trigger)
    rtt: str = "off"
    monitor_port: str = ""       # dongiel serial (logi); puste = brak monitora
    sample_rate: int = 100000    # próbki/s (decymacja z 100 kS/s PPK2)
    storage: Storage = field(default_factory=Storage)
    build_extra_args: list = field(default_factory=list)
    build_cmd: str = ""          # pełny override komendy builda (shlex)
    pristine: bool = False
    power_cycle: bool = True
    labels: list = field(default_factory=list)
    # --- seria (sweep): jeden "Pomiar N" rozbity na "Pomiar N.M" ---
    label: str = ""              # etykieta w UI/CSV ("N.M"); puste = numer kroku
    sweep_param: str = ""        # symbol Kconfig serii, np. CONFIG_LPN_SENSOR_INTERVAL_S
    sweep_value: str = ""        # wartość tej serii (do wyników), np. "5"


# ------------------------------------------------------------
#  Seria pomiarów (sweep): jedna karta "Pomiar N" z parametrem i listą
#  wartości -> N osobnych kroków "Pomiar N.1 … N.M", każdy budowany z inną
#  flagą -DCONFIG_...=<wartość>. Silnik nadaje każdej kombinacji flag własny
#  katalog builda, więc obrazy się nie nadpisują (engine._build_spec).
# ------------------------------------------------------------

_SWEEP_PARAM_RE = re.compile(r"^(?:-D)?(?:CONFIG_)?([A-Za-z][A-Za-z0-9_]*)$")


def normalize_sweep_param(param):
    """Nazwa parametru serii -> kanoniczny symbol Kconfig 'CONFIG_XXX'.
    Przyjmuje 'CONFIG_FOO', 'FOO' albo '-DCONFIG_FOO'. ValueError, gdy nie
    wygląda na symbol Kconfig."""
    m = _SWEEP_PARAM_RE.match(str(param).strip())
    if not m:
        raise ValueError(
            f"parametr serii '{param}' nie wygląda na symbol Kconfig "
            "(np. CONFIG_LPN_SENSOR_INTERVAL_S)")
    return "CONFIG_" + m.group(1)


def parse_sweep_values(raw):
    """'1, 2, 5' / '1 2 5' -> ['1','2','5'] (kolejność zachowana, duplikaty
    zdjęte zachowując pierwsze wystąpienie). Lista też dozwolona.
    ValueError, gdy po odrzuceniu pustych nic nie zostaje."""
    if isinstance(raw, (list, tuple)):
        tokens = [str(t).strip() for t in raw]
    else:
        tokens = re.split(r"[,\s]+", str(raw).strip())
    seen, out = set(), []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    if not out:
        raise ValueError("lista wartości serii jest pusta")
    return out


def expand_sweep(number, param, values, base):
    """Rozwiń jedną serię (karta 'Pomiar N' z sweepem) na listę PlanStep –
    po jednym kroku na wartość. Każdy krok dostaje flagę -DCONFIG_...=<v>
    doklejoną do build_extra_args, etykietę 'N.M' oraz zapamiętaną parę
    (parametr, wartość) do raportu. `base` = wspólne pola PlanStep (scenario,
    duration_s, trigger, rtt, ...); pola build_extra_args/label/sweep_* z
    `base` są ignorowane (ustawiamy je per wartość). ValueError przy pustej
    liście albo złym parametrze."""
    symbol = normalize_sweep_param(param)
    vals = parse_sweep_values(values)
    base_extra = list(base.get("build_extra_args", []))
    common = {k: v for k, v in base.items()
              if k not in ("build_extra_args", "label",
                           "sweep_param", "sweep_value")}
    return [PlanStep(build_extra_args=base_extra + [f"-D{symbol}={v}"],
                     label=f"{number}.{j}", sweep_param=symbol,
                     sweep_value=v, **common)
            for j, v in enumerate(vals, 1)]


@dataclass
class Plan:
    name: str
    description: str = ""
    board: str = ""              # profil z [boards.*]; puste = defaults
    on_build_error: str = "skip"
    on_step_error: str = "skip"
    ppk2_port: str = ""          # puste = autodetekcja
    steps: list = field(default_factory=list)
    path: Path = None


def _step_from_toml(raw, idx):
    """Sekcja [[plan.steps]] -> PlanStep (bez walidacji krzyżowej –
    tę robi validate_plan, z kontekstem manifestu)."""
    trig_raw = raw.get("trigger", {})
    trigger = Trigger(
        type=trig_raw.get("type", "delay"),
        seconds=float(trig_raw.get("seconds", 0)),
        pattern=trig_raw.get("pattern", ""),
        timeout_s=(parse_duration(trig_raw["timeout"])
                   if "timeout" in trig_raw else 120.0))
    stor_raw = raw.get("storage", {})
    storage = Storage(mode=stor_raw.get("mode", "downsampled"),
                      window_ms=int(stor_raw.get("window_ms", 1)))
    labels = [LabelRule(pattern=lr.get("pattern", ""),
                        label=lr.get("label", ""))
              for lr in raw.get("labels", [])]
    return PlanStep(
        scenario=raw.get("scenario", ""),
        duration_s=parse_duration(raw.get("duration", 0)),
        voltage=str(raw.get("voltage", "")),
        trigger=trigger,
        rtt=raw.get("rtt", "off"),
        monitor_port=str(raw.get("monitor_port", "")),
        sample_rate=int(raw.get("sample_rate", 100000)),
        storage=storage,
        build_extra_args=list(raw.get("build_extra_args", [])),
        build_cmd=raw.get("build_cmd", ""),
        pristine=bool(raw.get("pristine", False)),
        power_cycle=bool(raw.get("power_cycle", True)),
        labels=labels)


def load_plan(path):
    """Wczytaj plan z pliku TOML. Błędy składni/typów -> ValueError
    z czytelnym opisem (spójnie z resztą narzędzia)."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"plan '{path}' nie istnieje")
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"plan {path.name} nie parsuje się: {e}")
    head = data.get("plan")
    if not isinstance(head, dict):
        raise ValueError(f"plan {path.name}: brak sekcji [plan]")
    steps_raw = head.get("steps", [])
    if not steps_raw:
        raise ValueError(f"plan {path.name}: brak kroków [[plan.steps]]")
    try:
        steps = [_step_from_toml(raw, i)
                 for i, raw in enumerate(steps_raw, 1)]
    except (ValueError, TypeError) as e:
        raise ValueError(f"plan {path.name}: {e}")
    return Plan(
        name=head.get("name", path.stem),
        description=head.get("description", ""),
        board=head.get("board", ""),
        on_build_error=head.get("on_build_error", "skip"),
        on_step_error=head.get("on_step_error", "skip"),
        ppk2_port=head.get("ppk2_port", ""),
        steps=steps,
        path=path)


def validate_plan(plan, manifest):
    """Walidacja planu względem manifestu PRZED startem przebiegu –
    zwraca listę czytelnych błędów (pusta = OK), jak validate_scenarios."""
    errors = []
    scenarios = manifest.get("scenarios", {})
    boards = manifest.get("boards", {})

    if plan.board and plan.board not in boards:
        errors.append(f"plan: nieznany profil płytki '{plan.board}'. "
                      f"Dostępne: {', '.join(boards) or '(brak)'}")
    for what, val in (("on_build_error", plan.on_build_error),
                      ("on_step_error", plan.on_step_error)):
        if val not in ERROR_POLICIES:
            errors.append(f"plan: {what} = '{val}' – dozwolone: "
                          + " | ".join(ERROR_POLICIES))

    prof_name = plan.board or manifest.get("defaults", {}).get("profile")
    profile = boards.get(prof_name, {})

    for i, step in enumerate(plan.steps, 1):
        who = f"krok {step.label or i} ({step.scenario or '?'})"
        scen = scenarios.get(step.scenario)
        if scen is None:
            errors.append(f"{who}: nieznany scenariusz '{step.scenario}'. "
                          f"Dostępne: {', '.join(scenarios)}")
            continue
        if step.rtt not in RTT_MODES:
            errors.append(f"{who}: rtt = '{step.rtt}' – dozwolone: "
                          + " | ".join(RTT_MODES))
        if step.storage.mode not in STORAGE_MODES:
            errors.append(f"{who}: storage.mode = '{step.storage.mode}' – "
                          "dozwolone: " + " | ".join(STORAGE_MODES))
        if step.storage.window_ms < 1:
            errors.append(f"{who}: storage.window_ms musi być >= 1")
        if step.sample_rate not in SAMPLE_RATES:
            errors.append(f"{who}: sample_rate = {step.sample_rate} – "
                          "dozwolone: " + ", ".join(map(str, SAMPLE_RATES)))
        if step.trigger.type not in ("delay", "rtt", "serial"):
            errors.append(f"{who}: trigger.type = '{step.trigger.type}' – "
                          "dozwolone: delay | rtt | serial")
        if step.trigger.type == "serial":
            if not step.trigger.pattern:
                errors.append(f"{who}: trigger serial wymaga pola 'pattern' "
                              "(fragment logu)")
            if not step.monitor_port:
                errors.append(f"{who}: trigger serial wymaga 'monitor_port' "
                              "(port dongla, np. /dev/ttyACM0)")
        if step.trigger.type == "rtt":
            if not step.trigger.pattern:
                errors.append(f"{who}: trigger rtt wymaga pola 'pattern'")
            elif not _regex_ok(step.trigger.pattern):
                errors.append(f"{who}: trigger.pattern nie jest poprawnym "
                              f"regexem: '{step.trigger.pattern}'")
            if step.rtt == "off":
                errors.append(f"{who}: trigger rtt wymaga rtt = 'trigger' "
                              "albo 'continuous' (jest 'off')")
        if step.labels and step.rtt != "continuous":
            errors.append(f"{who}: auto-etykiety (labels) działają tylko "
                          "przy rtt = 'continuous' – znaczniki powstają "
                          "z logów czytanych PODCZAS pomiaru")
        for lr in step.labels:
            if not lr.pattern:
                errors.append(f"{who}: etykieta bez pola 'pattern'")
            elif not _regex_ok(lr.pattern):
                errors.append(f"{who}: wzorzec etykiety nie jest poprawnym "
                              f"regexem: '{lr.pattern}'")
        if step.rtt != "off" and profile and not profile.get("runner"):
            # RTT idzie przez J-Link; DK ma debugger wbudowany (bez
            # runnera w manifeście), więc tylko ostrzegamy przy braku
            # samego profilu, a nie wymuszamy runnera.
            pass
        if step.build_cmd and step.build_extra_args:
            errors.append(f"{who}: 'build_cmd' (pełny override) i "
                          "'build_extra_args' wykluczają się – zostaw jedno")
        if "hex" in scen and (step.build_cmd or step.build_extra_args
                              or step.pristine):
            errors.append(f"{who}: scenariusz 'hex' (gotowa binarka) nie "
                          "jest budowany – build_cmd/build_extra_args/"
                          "pristine nie mają sensu")
        if step.build_cmd:
            try:
                shlex.split(step.build_cmd)
            except ValueError as e:
                errors.append(f"{who}: build_cmd nie parsuje się: {e}")
        # Napięcie źródła PPK2: sprawdź EFEKTYWNĄ wartość (krok ->
        # scenariusz -> [defaults]) tak, jak liczy ją silnik – twardy
        # limit ochrony płytki. Odrzucamy przed startem przebiegu.
        eff_voltage = (step.voltage or scen.get("voltage")
                       or manifest.get("defaults", {}).get("voltage",
                                                           "3.0"))
        try:
            voltage_to_mV(eff_voltage)
        except ValueError as e:
            errors.append(f"{who}: {e}")
    return errors


def _regex_ok(pattern):
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False


def find_plans(plans_dir):
    """Lista planów w katalogu plans/ (posortowana po nazwie pliku);
    pliki *.example.toml pomijamy – to wzorce do kopiowania."""
    d = Path(plans_dir)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.toml")
                  if not p.name.endswith(".example.toml"))
