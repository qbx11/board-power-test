# ============================================================
#  autorun – tryb autonomiczny (silnik pomiarowy PPK2)
# ============================================================
# Pakiet headless: plan (plans/*.toml) -> build -> flash -> trigger ->
# pomiar prądu z PPK2 -> zapis sesji (reports/sessions/) + wiersz CSV.
# Importuje power_test (komendy west, CSV), nigdy tui. Biblioteki
# sprzętowe (ppk2_api, pylink) są importowane leniwie wewnątrz
# wrapperów (ppk2.py, rtt.py) – reszta pakietu działa bez nich
# (testy, walidacja planów, dry-run).
