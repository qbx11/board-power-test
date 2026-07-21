# Analiza poboru prądu płytki BTZ_EndDevice (2026-07)

Historia dochodzenia, dlaczego `BTZ_EndDevice` pobiera w System OFF więcej niż
płytka referencyjna DK — zachowana jako przykład metodyki pracy z tym narzędziem.

**Zmierzone (ten sam firmware `reset_only`, ta sama procedura PPK2):**
`nrf54l15dk/nrf54l15/cpuapp` (referencyjny DK) → **~0,95 µA**, zgodnie z datasheetem.
`BTZ_EndDevice/nrf54l15/cpuapp` → **~7,3 µA**. Skoro ten sam firmware na DK trafia w
datasheet, różnica na `BTZ_EndDevice` to sprawa **sprzętu/BOM tej płytki**, nie
firmware ani metodyki pomiaru.

## Ustalenia

Głęboka analiza (schemat + firmware + datasheet/errata Nordic) wykazała:

**Firmware jest już praktycznie optymalny dla System OFF — nic tu nie zostało do dokręcenia:**
- NCS v3.4.0 **automatycznie aplikuje erraty poboru** przy starcie (`system_nrf54l.c`):
  errata **[37]** („wyższy prąd po pin-reset/power-cycle”, zapis `NRF_TAD+0x40C=1`)
  oraz **[31]** — identycznie na DK i na BTZ_EndDevice.
- DC/DC włączone (`&vregmain`=DCDC), retencja RAM poprawnie wyłączona w `reset_only`,
  RESETREAS czyszczony przy boot. Peryferia `status="okay"` w devicetree **nie kosztują
  nic w System OFF** (bloki odcięte od zasilania).

**Oba „firmware’owe” tropy sprawdzone i odrzucone:**
- Szyna czujników (P0.00/Q2): R2 (1 MΩ) i tak trzyma Q2 wyłączony, a pull-upy I²C wiszą
  na *przełączanej* szynie `VDD_susp`, nie na stałym VDD. Firmware’owy fix (już w kodzie)
  nie zmienił pomiaru → to nie czujniki.
- Przełącznik LED P2.06/Q3: dren/źródło idą tylko na złącze J2 (nie na pokładową VDD),
  bramka ściągana do źródła przez R7 → domyślnie wyłączony. Nie jest przyczyną.

**Wniosek: ~6,4 µA nadwyżki to sprzęt płytki.** Jedyna zawsze-aktywna ścieżka DC na
schemacie (poza SoC) to upływ wsteczny **diody Schottky D1** w sieci ORowania wokół Q1
(VDD′ → D1 wstecznie → R1 470k → GND) — ale jest **ograniczona** (przy ~6 µA spadek na
R1 niemal wyłączyłby Q1, a płytka działa → D1 to najwyżej ~1 µA). Schemat nie pokazuje
oczywistego pojedynczego źródła 6 µA → trzeba **izolacji na stole**.

## Do zrobienia (pomiary sprzętowe, rozstrzygające)

1. Zmierz napięcie na **R1** (470k) w śnie → prąd D1 = U/470k. Albo odlutuj D1 i zmierz
   ponownie.
2. Sprawdź, gdzie jeszcze łączy się sieć **+3.3V** zasilana przez D1 (J1/J4, testpointy).

## Test firmware’owy (zbudowany, czeka na pomiar): NFC-as-GPIO

Piny NFC (P1.02/P1.03) są domyślnie w trybie NFC; koszt tego trybu w System OFF nie
jest udokumentowany dla nRF54L15. Overlay `boards/BTZ_EndDevice_nrf54l15_cpuapp.overlay`
zawiera `&uicr { nfct-pins-as-gpios; }`. Zbuduj i **wgraj z `--erase`** (zapis UICR jest
trwały; narzędzie robi to domyślnie), potem zmierz — jeśli prąd spadnie, tryb NFC był
(częścią) winowajcy. Jeśli NFC ma być funkcją produktu — usuń blok `&uicr` z overlaya.

Zobacz też: `docs/raport-pobor-pradu-2026-07-20.md` i surowe dane `pomiary-ppk2/pomiary.csv`.
