import contextlib
import io
import sqlite3
import unittest
from unittest.mock import patch

import main


class BusyTimeoutTests(unittest.TestCase):
	def run_commands(self, commands):
		connection = sqlite3.connect(":memory:", isolation_level=None)
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".open demo", *commands, ".exit"]), \
			patch("main.pale_open", return_value=(connection, connection.cursor())), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		return status, out.getvalue(), err.getvalue()

	def test_busy_timeout_alias_sets_timeout(self):
		status, out, err = self.run_commands([
			".busy_timeout 1250",
			"PRAGMA busy_timeout;",
		])
		self.assertEqual(status, 0, err)
		self.assertEqual(out, "1250\n")

	def test_busy_timeout_alias_rejects_invalid_values(self):
		status, out, err = self.run_commands([".busy_timeout -1", ".busy_timeout nope"])
		self.assertEqual(status, 0, err)
		self.assertEqual(out.count("Usage: .busy_timeout"), 2)


if __name__ == "__main__":
	unittest.main()
