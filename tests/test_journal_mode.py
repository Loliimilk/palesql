import contextlib
import io
import sqlite3
import unittest
from unittest.mock import patch

import main


class JournalModeTests(unittest.TestCase):
	def run_commands(self, commands):
		connection = sqlite3.connect(":memory:", isolation_level=None)
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".open demo", *commands, ".exit"]), \
			patch("main.pale_open", return_value=(connection, connection.cursor())), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		return status, out.getvalue(), err.getvalue()

	def test_journal_mode_reads_and_changes_setting(self):
		status, out, err = self.run_commands([
			".journal_mode",
			".journal_mode memory",
		])
		self.assertEqual(status, 0, err)
		self.assertEqual(out.splitlines(), [
			"journal_mode: memory",
			"journal_mode: memory",
		])

	def test_journal_mode_rejects_invalid_mode(self):
		status, out, err = self.run_commands([".journal_mode invalid"])
		self.assertEqual(status, 1)
		self.assertIn("Usage: .journal_mode", err)

	def test_journal_mode_requires_database(self):
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".journal_mode", ".exit"]), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		self.assertEqual(status, 1)
		self.assertIn("No database opened", out.getvalue())


if __name__ == "__main__":
	unittest.main()
