import contextlib
import io
import sqlite3
import unittest
from unittest.mock import patch

import main


class ScanstatsTests(unittest.TestCase):
	def run_commands(self, commands):
		connection = sqlite3.connect(":memory:", isolation_level=None)
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".open demo", *commands, ".exit"]), \
			patch("main.pale_open", return_value=(connection, connection.cursor())), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		return status, out.getvalue(), err.getvalue()

	def test_est_reports_plan_based_scan_metrics(self):
		status, out, err = self.run_commands([
			"CREATE TABLE items(id INTEGER, value TEXT);",
			"INSERT INTO items VALUES(1, 'one');",
			".scanstats est",
			"SELECT * FROM items WHERE id = 1;",
		])
		self.assertEqual(status, 0, err)
		self.assertIn("Scanstats (estimated):", out)
		self.assertIn("scans=1", out)
		self.assertIn("searches=0", out)

	def test_on_explicitly_reports_python_fallback(self):
		status, out, err = self.run_commands([
			"CREATE TABLE items(id INTEGER);",
			".scanstats on",
			"SELECT * FROM items;",
		])
		self.assertEqual(status, 0, err)
		self.assertIn("Scanstats (estimated fallback):", out)
		self.assertIn("runtime sqlite3_stmt_scanstatus is unavailable", out)

	def test_off_is_quiet_and_invalid_mode_is_reported(self):
		status, out, err = self.run_commands([
			".scanstats maybe",
			".scanstats off",
			"SELECT 1;",
		])
		self.assertEqual(status, 0, err)
		self.assertIn("Usage: .scanstats on|off|est", out)
		self.assertNotIn("Scanstats", out)

	def test_non_query_statements_do_not_emit_scan_metrics(self):
		status, out, err = self.run_commands([
			".scanstats est",
			"CREATE TABLE items(id INTEGER);",
		])
		self.assertEqual(status, 0, err)
		self.assertNotIn("Scanstats", out)


if __name__ == "__main__":
	unittest.main()
