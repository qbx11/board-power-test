# Raport: diagnostyka i redukcja poboru prądu w trybie uśpienia (nRF54L15 / BTZ_EndDevice)

**Data:** 2026-07-20
**Autor:** Jakub Janik
**Płytka:** BTZ_EndDevice (nRF54L15/cpuapp), płytka referencyjna: nRF54L15 DK
**Firmware:** projekt `nrf54l15-power-test`, wariant `reset_only` (System OFF, absolutne minimum)
**Przyrząd:** Power Profiler Kit II (PPK2), tryb Source meter

## Streszczenie

Zdiagnozowano i usunięto przyczynę nadmiernego poboru prądu na płytce BTZ_EndDevice
w trybie System OFF. Pobór spadł z **~7,3 µA** do **~0,91 µA średnio** (baseline
~0,66 µA), czyli do poziomu katalogowego dla nRF54L15 w System OFF.

Przyczyną nie był firmware ani sam układ SoC, lecz **stały upływ prądu przez rezystor
R1 (470 kΩ)** w sekcji Power Input, wynikający ze sposobu zasilania płytki. Naprawa
polegała na zmianie źródła zasilania (zasilanie przez JP1 zamiast przez wejście +3,3 V),
co usunęło napięcie z rezystora R1.

Pozostałe po naprawie okresowe „piki" do 7 µA nie są usterką: występują identycznie na
płytce referencyjnej DK i są normalnym zachowaniem przetwornicy DC/DC przy skrajnie
małym obciążeniu.

## 1. Problem wyjściowy

Wariant `reset_only` wprowadza układ w System OFF (`sys_poweroff()`) i nie robi nic
więcej. Oczekiwany pobór wg datasheetu nRF54L15 dla System OFF (bez retencji RAM) to
rząd **~0,6-0,8 µA**. Pomiar na BTZ_EndDevice pokazywał jednak **ponad 7 µA**, czyli
około dziesięciokrotnie za dużo. Celem było ustalenie przyczyny i sprowadzenie poboru
do poziomu katalogowego.

## 2. Proces diagnostyczny (skrót)

Zanim ustalono przyczynę sprzętową, metodycznie wykluczono typowe źródła błędu (kroki
w tej kolejności oszczędzają czas przy następnym takim problemie):

- **Konfiguracja obrazu pomiarowego** potwierdzona jako minimalna: konsola, UART, logi
  i RTT wyłączone, retencja RAM wyłączona w wariancie `reset_only`, wybrany właściwy
  wariant snu. To wyklucza „mierzę zły build".
- **Podłączony debugger** odłączany na czas pomiaru (podpięty J-Link dokłada własny prąd).
- **Eksperymenty firmware ze stanem GPIO** (bramka zasilania czujników, LED, przycisk):
  próba wymuszania „bezpiecznych" stanów pinów. Wniosek metodyczny na przyszłość:
  **nie sterować pinów sprzętowych na podstawie samego devicetree bez weryfikacji
  polaryzacji na sprzęcie**. Jedna taka próba (wymuszenie pull-upa na przycisku)
  podniosła pobór z ~7 µA do ~150 µA, bo wewnętrzny pull-up walczył z pinem ściągniętym
  na zewnątrz do masy. Zmiany wycofano.
- **Weryfikacja polaryzacji multimetrem** (pin bramki zasilania czujników): potwierdzono,
  że czujniki I2C są realnie odłączone (stan wysoki na pinie = czujniki bez zasilania,
  dodatkowo trzyma to rezystor podciągający bramkę).

Żaden z tych kroków nie obniżył poboru, co skierowało dochodzenie na sprzęt płytki.

## 3. Pomiar referencyjny na nRF54L15 DK (krok kluczowy)

Ten sam kod (`reset_only`) zbudowano dla płytki referencyjnej **nRF54L15 DK** i zmierzono
tą samą metodą PPK2. Wynik: **~0,95 µA**, zgodny z datasheetem.

Wniosek rozstrzygający: skoro identyczny firmware trafia w wartość katalogową na DK, to
**firmware i metodyka pomiaru są poprawne**, a cała nadwyżka na BTZ_EndDevice pochodzi ze
**sprzętu tej płytki**. To najtańszy i najszybszy test do wykonania jako pierwszy przy
każdym takim problemie: porównanie z płytką referencyjną natychmiast oddziela „prąd SoC"
od „prądu reszty płytki".

Istotna różnica konstrukcyjna, która to tłumaczy: na DK PPK2 zasila **wyłącznie** pin
zasilania samego SoC (VDDM), a reszta płytki (debugger, LED, przyciski) idzie z USB i nie
wchodzi do pomiaru. Na BTZ_EndDevice jest **jedna wspólna sieć VDD** zasilająca SoC i całą
resztę, więc PPK2 mierzy sumę: SoC plus wszystkie upływy płytki.

## 4. Analiza schematu

Przeanalizowano schemat pod kątem każdej stale aktywnej ścieżki DC z sieci VDD do masy
(niezależnej od stanu snu SoC). Zidentyfikowano m.in.: bramkę zasilania czujników (P-MOSFET
z rezystorem podciągającym, domyślnie wyłączona), przełącznik zasilania LED (wyprowadzony
na złącze, nie na pokładową szynę VDD), sieć NFC (blokowana kondensatorami, brak ścieżki
DC), złącze debug oraz sekcję Power Input z rezystorem R1 (470 kΩ) i diodą Schottky D1.

Analiza wskazała sekcję Power Input jako jedyne miejsce z realną, stałą ścieżką prądu
zależną od sposobu zasilania płytki (patrz punkt 6).

## 5. Wniosek pośredni: firmware jest już optymalny

Zweryfikowano w źródłach nRF Connect SDK (v3.4.0), że obraz pomiarowy jest już praktycznie
optymalny dla System OFF i nie ma tu czego poprawiać:

- SDK **automatycznie aplikuje przy starcie erraty poboru** dla nRF54L15 (m.in. errata
  dotycząca podwyższonego prądu po resecie), identycznie na DK i na BTZ_EndDevice.
- DC/DC włączone, retencja RAM poprawnie wyłączona w `reset_only`, peryferia w devicetree
  nie kosztują nic w System OFF (bloki są odcięte od zasilania).
- Dodatkowy test przełączenia pinów NFC na GPIO (zapis UICR) nie zmienił poboru, co
  wyklucza NFC jako przyczynę.

Ostateczny wniosek tego etapu: **nadwyżka ~6,4 µA to sprzęt płytki**, dochodzenie przenosi
się w całości na warstwę sprzętową.

## 6. Zidentyfikowana przyczyna i naprawa: rezystor R1 w sekcji Power Input

### Co było źle

Sekcja Power Input realizuje przełączanie źródła zasilania między baterią CR2032 a
zewnętrznym zasilaniem +3,3 V, w oparciu o tranzystor P-MOSFET Q1 (AO3401A):

- **Bateria CR2032** (VBat) jest podana na źródło Q1, a wyjście Q1 (VDD') idzie dalej
  przez JP1/SW1 do głównej sieci VDD.
- **Bramka Q1** jest ściągana do masy przez **R1 (470 kΩ)**. Przy zasilaniu z baterii
  bramka siedzi przy ~0 V, Q1 jest załączony i przepuszcza VBat do VDD'. Przez R1 nie
  płynie wtedy praktycznie żaden prąd (napięcie na R1 bliskie 0 V).
- Gdy płytka jest **zasilana z wejścia +3,3 V**, to napięcie +3,3 V trafia na węzeł
  bramki (wyłączając Q1 i zasilając płytkę przez diodę Schottky D1). W tej konfiguracji
  **R1 (470 kΩ) znajduje się bezpośrednio pod napięciem zasilania i tworzy stałą ścieżkę
  prądu do masy.**

To jest źródło nadmiarowego poboru. Prąd przez R1 to zwykłe U/R:

| Napięcie zasilania | Prąd przez R1 (470 kΩ) | Baseline + upływ R1 |
|---|---|---|
| 3,0 V | 6,38 µA | 0,66 + 6,38 = **7,04 µA** |
| 3,3 V | 7,02 µA | 0,66 + 7,02 = **7,68 µA** |

Wartości pokrywają się z obserwowanymi ~7,3 µA, co **ilościowo potwierdza**, że to R1
odpowiadał za nadwyżkę. Ten prąd płynie stale, niezależnie od stanu snu SoC, dlatego
pojawiał się w każdym pomiarze jako stałe przesunięcie ~7 µA.

### Naprawa

Zmieniono sposób zasilania płytki: **zamiast zasilać z wejścia +3,3 V, zasilamy przez
JP1** (tor bateryjny przez Q1). Rezystora R1 nie usuwano. W tej konfiguracji sieć +3,3 V
nie jest pobudzona, węzeł bramki Q1 siedzi przy ~0 V, więc **na R1 nie ma napięcia i nie
płynie przez niego prąd**. Nadwyżka ~7 µA znika.

### Lekcja na przyszłość

Przy pomiarach na poziomie pojedynczych mikroamperów **każdy rezystor podciągający lub
ściągający, który w wybranej konfiguracji zasilania znajdzie się pod pełnym napięciem
sieci, zdominuje wynik**. Rezystor 470 kΩ przy 3 V to już 6,4 µA. Przed pomiarem warto
przejść po schemacie i policzyć U/R dla każdej stałej ścieżki na sieci zasilania, oraz
świadomie wybrać tor zasilania, który nie pobudza takich rezystorów.

## 7. Wyniki po naprawie

Pomiar PPK2 po zmianie zasilania (`reset_only`, System OFF, 100 kHz, okno 343 ms):

| Metryka | Wartość |
|---|---|
| Baseline (między pikami) | **0,66 µA** |
| Mediana | 0,64 µA |
| **Średnia (całość)** | **0,91 µA** |
| Szczyt maksymalny | 7,2 µA (chwilowy) |
| Udział czasu w „pikach" | 8,7 % |

Pobór spadł z ~7,3 µA do ~0,91 µA średnio, czyli do poziomu katalogowego dla System OFF.

<!-- MIEJSCE NA WYKRESY -->

*Wykres 1: pełny przebieg prądu w czasie (widoczna niska linia bazowa i okresowe piki).*

<!-- ![Wykres 1](sciezka/do/wykres1.png) -->

*Wykres 2: powiększenie pojedynczego piku (ostre narastanie i wykładniczy zjazd).*

<!-- ![Wykres 2](sciezka/do/wykres2.png) -->

*Wykres 3 (opcjonalnie): histogram/rozkład prądu.*

<!-- ![Wykres 3](sciezka/do/wykres3.png) -->

## 8. Analiza okresowych „pików" do 7 µA

Po naprawie na przebiegu pozostają okresowe piki. Analiza danych pomiarowych:

- **Wartość 7 µA to chwilowy szczyt, nie stały pobór.** Przez ~91 % czasu prąd wynosi
  ~0,66 µA. Dla żywotności baterii liczy się średnia (0,91 µA), a nie szczyt.
- **Kształt pojedynczego zdarzenia:** ostre narastanie z 0,64 do 7,2 µA w jednej próbce
  (poniżej 10 µs), a następnie **czysto wykładniczy zjazd** o stałej czasowej
  tau = 4,2 ms (dopasowanie R^2 = 0,97). To sygnatura ładowania kondensatora przez
  rezystor (przebieg RC), a nie aktywności cyfrowej.
- **Okresowość:** zdarzenia powtarzają się co ~64 ms (~15,6 Hz).
- **Amplituda:** maksimum w całym zbiorze to 7,2 µA. Gdyby to była praca CPU
  (reboot/wybudzenie), widoczne byłyby miliampery. **CPU ani razu nie jest aktywny,
  układ pozostaje w głębokim śnie przez cały czas.**
- Ładunek na pojedyncze zdarzenie to ~26 nC, co uśrednione daje ~0,4 µA dodatku do linii
  bazowej i tłumaczy różnicę między baseline 0,66 µA a średnią 0,91 µA.

Firmware `reset_only` nie generuje żadnej okresowości (jednorazowe `main()`, potem System
OFF na zawsze), więc źródło piku jest sprzętowo-regulatorowe, nie programowe.

## 9. Piki występują też na DK: normalne zachowanie DC/DC

Ten sam kod uruchomiony na płytce referencyjnej **nRF54L15 DK dał identyczne okresowe
piki**. To eliminuje sprzęt BTZ_EndDevice jako przyczynę (DK nie ma sekcji Power Input,
czujników ani sieci NFC tej płytki).

Wspólny mianownik obu pomiarów: ten sam SoC, ten sam firmware (System OFF), ten sam tryb
regulatora **DC/DC** (obie płytki ustawiają DC/DC), ten sam przyrząd PPK2.

Najbardziej prawdopodobne wyjaśnienie: **przetwornica DC/DC (VREGMAIN) w trybie
impulsowym (PFM) przy skrajnie małym obciążeniu**. Przy poborze rzędu 0,6 µA przetwornica
nie pracuje ciągle, tylko okresowo „doładowuje" kondensator wyjściowy krótkim impulsem i
znów się wyłącza. Na wejściu (mierzonym przez PPK2) daje to dokładnie taki obraz: ostry
impuls i wykładnicze doładowanie kondensatora, powtarzane okresowo. To normalne i
oczekiwane zachowanie, które **nie pogarsza średniego poboru** (0,91 µA pozostaje na
poziomie katalogowym). Drugorzędnie część okresowego zafalowania może pochodzić z samego
PPK2 w trybie Source meter przy sub-µA.

Ewentualne potwierdzenie (jeśli będzie potrzebne): zbudowanie obrazu z regulatorem w
trybie LDO zamiast DC/DC. Jeśli piki znikną (płaska linia, minimalnie wyższa średnia),
potwierdza to udział przetwornicy DC/DC. Dla poboru w System OFF różnica LDO vs DC/DC jest
i tak pomijalna.

## 10. Wnioski i lista kontrolna na przyszłość

1. **Najpierw zmierz płytkę referencyjną (DK) tym samym kodem.** To natychmiast oddziela
   prąd SoC od prądu reszty płytki i oszczędza godziny szukania w niewłaściwym miejscu.
2. **Policz U/R dla każdej stałej ścieżki na sieci zasilania.** Rezystor 470 kΩ przy 3 V
   to już ~6,4 µA. Przy pomiarach sub-µA takie rezystory dominują wynik.
3. **Świadomie wybieraj tor zasilania.** Ta sama płytka zasilana z różnych wejść może mieć
   różny pobór spoczynkowy, jeśli któreś wejście pobudza rezystory podciągające
   (jak R1 w tym przypadku).
4. **Nie steruj pinów sprzętowych na podstawie samego devicetree.** Weryfikuj polaryzację
   na sprzęcie (multimetr), inaczej łatwo pogorszyć pobór (u nas skok do 150 µA).
5. **Odróżniaj szczyt od średniej.** Chwilowe piki (tu do 7 µA co 64 ms) nie są problemem,
   jeśli średnia jest niska. Dla baterii liczy się średnia (0,91 µA).
6. **Okresowe impulsy w śnie to często normalna praca DC/DC (PFM),** a nie usterka,
   zwłaszcza gdy widać je również na płytce referencyjnej.
