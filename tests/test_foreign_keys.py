import contextlib
import io
import sqlite3
import unittest
from unittest.mock import patch

import main


class ForeignKeysTests(unittest.TestCase):
	def run_commands(self, commands):
		connection = sqlite3.connect(":memory:", isolation_level=None)
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".open demo", *commands, ".exit"]), \
			patch("main.pale_open", return_value=(connection, connection.cursor())), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		return status, out.getvalue(), err.getvalue()

	def test_foreign_keys_reads_and_changes_setting(self):
		status, out, err = self.run_commands([
			".foreign_keys",
			".foreign_keys on",
			".foreign_keys",
			".foreign_keys off",
		])
		self.assertEqual(status, 0, err)
		self.assertEqual(out.splitlines(), [
			"foreign_keys: off",
			"foreign_keys: on",
			"foreign_keys: on",
			"foreign_keys: off",
		])

	def test_foreign_keys_rejects_invalid_setting(self):
		status, out, err = self.run_commands([".foreign_keys maybe"])
		self.assertEqual(status, 1)
		self.assertIn("Usage: .foreign_keys on|off", err)

	def test_foreign_keys_requires_database(self):
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".foreign_keys", ".exit"]), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		self.assertEqual(status, 1)
		self.assertIn("No database opened", out.getvalue())


if __name__ == "__main__":
	unittest.main()
