import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class LogTests(unittest.TestCase):
	def run_commands(self, commands):
		connection = sqlite3.connect(":memory:", isolation_level=None)
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".open demo", *commands, ".exit"]), \
			patch("main.pale_open", return_value=(connection, connection.cursor())), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		return status, out.getvalue(), err.getvalue()

	def test_log_stdout_records_sql_errors(self):
		status, out, err = self.run_commands([
			".log stdout",
			"SELECT missing;",
		])
		self.assertEqual(status, 1)
		self.assertIn("SQLite log: SQL error:", out)
		self.assertIn("SQL error:", err)

	def test_log_stderr_records_sql_errors(self):
		status, out, err = self.run_commands([
			".log stderr",
			"SELECT missing;",
		])
		self.assertEqual(status, 1)
		self.assertEqual(out, "")
		self.assertEqual(err.count("SQLite log: SQL error:"), 1)

	def test_log_off_disables_logging(self):
		status, out, err = self.run_commands([
			".log stdout",
			".log off",
			"SELECT missing;",
		])
		self.assertEqual(status, 1)
		self.assertNotIn("SQLite log:", out)
		self.assertIn("SQL error:", err)

	def test_log_file_records_sql_errors(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "sqlite.log"
			status, out, err = self.run_commands([
				f'.log "{path}"',
				"SELECT missing;",
			])
			self.assertEqual(status, 1)
			self.assertEqual(out, "")
			self.assertIn("SQLite log: SQL error:", path.read_text(encoding="utf-8"))

	def test_log_requires_destination(self):
		status, out, err = self.run_commands([".log"])
		self.assertEqual(status, 0, err)
		self.assertIn("Usage: .log off|stdout|stderr|<file>", out)


if __name__ == "__main__":
	unittest.main()
