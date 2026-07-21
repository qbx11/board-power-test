/*
 * nRF54L15 – pomiar poboru energii (PPK2).
 *
 * Firmware wprowadza układ w JEDEN, wybrany na etapie budowania tryb uśpienia
 * i nie robi nic więcej (poza opcjonalnym "błyskiem" aktywności w trybach
 * periodycznych). Wybór trybu: Kconfig choice (patrz Kconfig / README).
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <zephyr/kernel.h>
#include <zephyr/sys/poweroff.h>

#if defined(CONFIG_SLEEP_SYSTEM_OFF_WAKE_GPIO) || DT_NODE_EXISTS(DT_NODELABEL(sensor_pwr))
#include <zephyr/drivers/gpio.h>
#endif

#if defined(CONFIG_SLEEP_PERIODIC_SYSTEM_OFF)
#include <zephyr/drivers/timer/nrf_grtc_timer.h>
#endif

#if defined(CONFIG_HWINFO)
#include <zephyr/drivers/hwinfo.h>
#endif

/* Lekki log tylko gdy jest aktywna konsola (build diagnostyczny z CONFIG_CONSOLE=y).
 * W obrazie pomiarowym CONFIG_CONSOLE jest wyłączone, więc APP_LOG znika
 * do pustej instrukcji i firmware naprawdę nic nie wypisuje.
 * (Uwaga: CONFIG_PRINTK bywa wymuszone na 'y' przez jądro, dlatego
 *  bramkujemy na CONSOLE, a nie na PRINTK.) */
#if defined(CONFIG_CONSOLE)
#define APP_LOG(...) printk(__VA_ARGS__)
#else
#define APP_LOG(...) do { } while (0)
#endif

#if defined(CONFIG_SLEEP_SYSTEM_OFF_WAKE_GPIO)
#if !DT_NODE_EXISTS(DT_ALIAS(sw0))
#error "Wariant SLEEP_SYSTEM_OFF_WAKE_GPIO wymaga aliasu 'sw0' (przycisk wybudzenia) w devicetree."
#endif
static const struct gpio_dt_spec wakeup_button = GPIO_DT_SPEC_GET(DT_ALIAS(sw0), gpios);
#endif

#if DT_NODE_EXISTS(DT_NODELABEL(sensor_pwr))
/* Bramka zasilania czujników I2C na BTZ_EndDevice (P0.00, active-low enable,
 * "regulator-boot-on" w devicetree). Sterownik regulator-fixed nie jest w tym
 * firmware budowany (brak CONFIG_REGULATOR/CONFIG_I2C/CONFIG_SENSOR), więc
 * nic normalnie tego pinu nie dotyka - a jego stan jest zatrzaskiwany przez
 * System OFF/reset, więc może zostać z poprzedniego firmware. Wymuszamy tu
 * jawnie stan wyłączony (potwierdzone na płytce: P0.00 wysoki = czujniki bez
 * zasilania), żeby pomiar prądu snu nie liczył też poboru czujników.
 */
static const struct gpio_dt_spec sensor_pwr_en =
	GPIO_DT_SPEC_GET(DT_NODELABEL(sensor_pwr), enable_gpios);
#endif

#if defined(CONFIG_SLEEP_PERIODIC_SYSTEM_ON) || defined(CONFIG_SLEEP_PERIODIC_SYSTEM_OFF)
/* Zamiennik realnej pracy/nadawania: CPU zajęty przez PERIODIC_ACTIVE_MS.
 * W realnym produkcie zastąp to swoim kodem (odczyt czujnika + TX). */
static inline void activity_burst(void)
{
	k_busy_wait((uint32_t)CONFIG_PERIODIC_ACTIVE_MS * 1000U);
}
#endif

int main(void)
{
#if defined(CONFIG_HWINFO)
	uint32_t reset_cause = 0U;

	(void)hwinfo_get_reset_cause(&reset_cause);
	APP_LOG("\n[nRF54L15 power-test] board=%s reset_cause=0x%08x\n",
		CONFIG_BOARD, reset_cause);
	(void)hwinfo_clear_reset_cause();
#endif

#if DT_NODE_EXISTS(DT_NODELABEL(sensor_pwr))
	if (gpio_is_ready_dt(&sensor_pwr_en)) {
		(void)gpio_pin_configure_dt(&sensor_pwr_en, GPIO_OUTPUT_INACTIVE);
	}
#endif

#if defined(CONFIG_SLEEP_SYSTEM_ON_IDLE)
	/* --- System ON idle ---
	 * Nic nie robimy. Wątek idle wchodzi w WFI, RAM i LFCLK/RTC (GRTC)
	 * pozostają aktywne. Referencyjny prąd "śpiącego" RTOS. */
	APP_LOG("Tryb: System ON idle (k_sleep FOREVER)\n");
	k_sleep(K_FOREVER);

#elif defined(CONFIG_SLEEP_PERIODIC_SYSTEM_ON)
	/* --- Periodyk, System ON ---
	 * Błysk aktywności co T, między nimi k_sleep(T) -> System ON idle.
	 * Program KONTYNUUJE (RAM + RTC żyją), brak kosztu rebootu. */
	APP_LOG("Tryb: Periodyk System ON, T=%d ms, aktywne=%d ms\n",
		CONFIG_PERIODIC_PERIOD_MS, CONFIG_PERIODIC_ACTIVE_MS);
	while (1) {
#if defined(CONFIG_CONSOLE)
		static uint32_t wake;

		APP_LOG("wybudzenie #%u\n", wake++);
#endif
		activity_burst();
		k_sleep(K_MSEC(CONFIG_PERIODIC_PERIOD_MS));
	}

#elif defined(CONFIG_SLEEP_PERIODIC_SYSTEM_OFF)
	/* --- Periodyk, System OFF ---
	 * Błysk aktywności -> uzbrojenie GRTC na T -> sys_poweroff().
	 * Wybudzenie po T = restart od main() (pełny reboot, RAM znika). */
	APP_LOG("Tryb: Periodyk System OFF, T=%d ms, aktywne=%d ms\n",
		CONFIG_PERIODIC_PERIOD_MS, CONFIG_PERIODIC_ACTIVE_MS);
	activity_burst();

	int err = z_nrf_grtc_wakeup_prepare((uint64_t)CONFIG_PERIODIC_PERIOD_MS * 1000ULL);

	if (err < 0) {
		APP_LOG("Nie udalo sie uzbroic GRTC jako wybudzenia (%d)\n", err);
	}
	sys_poweroff();
	/* nieosiągalne */

#else /* --- jednorazowe warianty System OFF --- */

#if defined(CONFIG_SLEEP_SYSTEM_OFF_WAKE_GPIO)
	/* Skonfiguruj przycisk jako źródło wybudzenia z System OFF.
	 * GPIO_INT_LEVEL_ACTIVE ustawia SENSE, który wybudza z OFF. */
	int rc = gpio_pin_configure_dt(&wakeup_button, GPIO_INPUT);

	if (rc == 0) {
		rc = gpio_pin_interrupt_configure_dt(&wakeup_button,
						     GPIO_INT_LEVEL_ACTIVE);
	}
	if (rc != 0) {
		APP_LOG("Blad konfiguracji sw0 jako wybudzenia (%d)\n", rc);
	}
	APP_LOG("Tryb: System OFF, wybudzenie przyciskiem sw0\n");

#elif defined(CONFIG_SLEEP_SYSTEM_OFF_RAM_RETAINED)
	/* Retencja RAM jest przywracana przez warstwę SoC podczas
	 * sys_poweroff() dla regionu "zephyr,retained-ram" z devicetree
	 * (overlay płytki). Tu nic dodatkowego nie trzeba robić. */
	APP_LOG("Tryb: System OFF z retencja RAM (region z devicetree)\n");

#else /* CONFIG_SLEEP_SYSTEM_OFF_RESET_ONLY */
	APP_LOG("Tryb: System OFF, wybudzenie tylko reset (minimum)\n");
#endif

	sys_poweroff();
	/* nieosiągalne */

#endif /* wybór trybu */

	return 0;
}
