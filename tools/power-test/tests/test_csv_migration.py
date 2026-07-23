# ============================================================
#  Testy migracji schematu dziennika CSV (ensure_csv_schema)
# ============================================================

import csv
import unittest

from common import FakeEnv

import power_test as core


class CsvMigrationTest(unittest.TestCase):
    def setUp(self):
        self.env = FakeEnv()

    def tearDown(self):
        self.env.cleanup()

    def _write_old_csv(self):
        core.CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(core.CSV_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=core.CSV_BASE_FIELDS)
            w.writeheader()
            w.writerow({"data": "2026-01-01 10:00", "plytka": "b",
                        "egzemplarz": "BTZ #1", "scenariusz": "idle",
                        "flagi": "-DX=y", "napiecie_V": "3.0",
                        "prad_uA": "1.5", "oczekiwane": "~1 uA",
                        "uwagi": ""})

    def test_migrates_old_header(self):
        self._write_old_csv()
        core.ensure_csv_schema()
        with open(core.CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            self.assertEqual(reader.fieldnames, core.CSV_FIELDS)
            rows = list(reader)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["prad_uA"], "1.5")
        self.assertEqual(rows[0]["sesja"], "")        # nowe pole puste

    def test_append_after_migration(self):
        self._write_old_csv()
        # Wiersz autorun z pełnym kompletem pól.
        row = {c: "" for c in core.CSV_FIELDS}
        row.update({"data": "2026-01-02 03:00", "scenariusz": "reset_only",
                    "prad_uA": 0.4, "prad_min_uA": 0.3, "prad_max_uA": 12.0,
                    "czas_s": 3600, "sesja": "reports/sessions/x"})
        core.append_row(row, verbose=False)
        with open(core.CSV_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["prad_uA"], "1.5")   # stary zachowany
        self.assertEqual(rows[1]["prad_max_uA"], "12.0")

    def test_idempotent(self):
        self._write_old_csv()
        core.ensure_csv_schema()
        before = core.CSV_PATH.read_text()
        core.ensure_csv_schema()
        self.assertEqual(core.CSV_PATH.read_text(), before)


if __name__ == "__main__":
    unittest.main()
