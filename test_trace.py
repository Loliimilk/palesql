import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class TraceTests(unittest.TestCase):
	def run_commands(self, commands):
		connection = sqlite3.connect(":memory:", isolation_level=None)
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".open demo", *commands, ".exit"]), \
			patch("main.pale_open", return_value=(connection, connection.cursor())), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		return status, out.getvalue(), err.getvalue()

	def test_trace_on_shows_sql_before_result(self):
		status, out, err = self.run_commands([
			".trace on",
			"SELECT 1;",
		])
		self.assertEqual(status, 0, err)
		self.assertIn("Trace: SELECT 1;", out)
		self.assertTrue(out.rstrip().endswith("1"), out)

	def test_trace_off_stops_output(self):
		status, out, err = self.run_commands([
			".trace on",
			".trace off",
			"SELECT 1;",
		])
		self.assertEqual(status, 0, err)
		self.assertNotIn("Trace:", out)
		self.assertEqual(out, "1\n")

	def test_trace_stderr_and_file_destinations(self):
		status, out, err = self.run_commands([
			".trace stderr",
			"SELECT 1;",
		])
		self.assertEqual(status, 0, out)
		self.assertIn("Trace: SELECT 1;", err)

		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "trace.log"
			status, out, err = self.run_commands([
				f'.trace "{path}"',
				"SELECT 2;",
			])
			self.assertEqual(status, 0, err)
			self.assertEqual(out, "2\n")
			self.assertIn("Trace: SELECT 2;", path.read_text(encoding="utf-8"))

	def test_trace_rejects_missing_argument(self):
		status, out, err = self.run_commands([".trace"])
		self.assertEqual(status, 0, err)
		self.assertIn("Usage: .trace", out)


if __name__ == "__main__":
	unittest.main()
