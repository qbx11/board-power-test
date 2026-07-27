# BTZ_EndDevice (nRF54L15)

Definicja własnej płytki dla Zephyr/NCS. Cel budowania:

```
BTZ_EndDevice/nrf54l15/cpuapp
```

Sprzęt:

```
LED         P2.08   aktywny stanem niskim
Przycisk    P1.14   aktywny stanem niskim (zewnetrzny pull up)
I2C SDA     P0.03
I2C SCL     P0.04   (instancja i2c30)
Zasilanie czujnikow  P0.00  (enable stanem niskim, regulator-fixed)
Czujniki    STS4X 0x44, BMI270 0x68, LTR329 0x29
Konsola     SEGGER RTT (domyslnie w defconfig plytki)
```

## 1. Instalacja definicji płytki

Repozytorium zawiera folder `goodbyte`. Skopiuj go do katalogu `boards` w swoim drzewie Zephyr (NCS):

```
git clone https://github.com/<uzytkownik>/<repo>.git
cp -r <repo>/goodbyte $HOME/ncs/v3.4.0/zephyr/boards/
```

Wynik: `$HOME/ncs/v3.4.0/zephyr/boards/goodbyte/BTZ_EndDevice/`.

Alternatywnie trzymaj folder poza drzewem i wskaż go przy budowaniu przez `-DBOARD_ROOT=<sciezka_do_folderu_z_katalogiem_boards>`.

## 2. Build i flash z linii poleceń (bez rozszerzenia nRF Connect for VS Code)

Wymagania: zainstalowany NCS/Zephyr z `west`, toolchain dostępny w PATH, programator J-Link.

```
cd $HOME/ncs/v3.4.0
west build -b BTZ_EndDevice/nrf54l15/cpuapp -p always path/to/app
west flash
```

1. J-Link jest domyślnym runnerem (ustawione w `board.cmake`, urządzenie `nRF54L15_M33`).
2. Wymuszenie runnera: `west flash -r jlink`.
3. Pełne wymazanie w razie problemów: `west flash --erase`.
4. Jeśli `west` lub toolchain nie są w PATH, otwórz najpierw powłokę toolchainu NCS, potem powtórz komendy.

## 3. Logi przez SEGGER RTT

Płytka kieruje konsolę na RTT, więc `printk` i logi lecą przez J-Link bez UART. Uruchom viewer:

```
/opt/SEGGER/JLink_V952/JLinkRTTViewerExe
```

Ścieżka zależy od wersji J-Link; podmień `JLink_V952` na swoją.

## 4. Ustawienia RTT Viewer

W oknie Connection to J-Link ustaw:

1. Connection to J-Link: `USB`
2. Specify Target Device: `nRF54L15_M33`
3. Target Interface and Speed: `SWD`, `4000 kHz`
4. RTT Control Block: `Auto Detection`

Zatwierdź `OK`. Logi pojawiają się w zakładce Terminal 0. Zaznacz `Auto Reconnect`, aby viewer łączył się po każdym flashu bez ponownego pytania.

## 5. Skrót w menu Ubuntu (uruchamianie bez terminala)

Utwórz plik `~/.local/share/applications/jlink_rtt_viewer.desktop`:

```
[Desktop Entry]
Type=Application
Name=J-Link RTT Viewer
Comment=SEGGER RTT Viewer (logi przez J-Link)
Exec=/opt/SEGGER/JLink_V952/JLinkRTTViewerExe
Icon=utilities-terminal
Terminal=false
Categories=Development;Debugger;
```

Odśwież menu aplikacji:

```
update-desktop-database ~/.local/share/applications
```

Pozycja `J-Link RTT Viewer` pojawi się w launcherze. Uruchamiasz ją kliknięciem, bez terminala.

## 6. Czujniki I2C w aplikacji

Definicja płytki opisuje sprzęt. Aby czujniki działały, aplikacja w `prj.conf` potrzebuje:

```
CONFIG_I2C=y
CONFIG_REGULATOR=y
CONFIG_REGULATOR_FIXED=y
CONFIG_SENSOR=y
```

Sterowniki włączają się automatycznie z węzłów devicetree: `CONFIG_STS4X`, `CONFIG_BMI270`, `CONFIG_LTR55X` (LTR329).
