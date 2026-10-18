import contextlib
import io
import sqlite3
import unittest
from unittest.mock import patch

import main


class EncodingTests(unittest.TestCase):
	def run_commands(self, commands):
		connection = sqlite3.connect(":memory:", isolation_level=None)
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".open demo", *commands, ".exit"]), \
			patch("main.pale_open", return_value=(connection, connection.cursor())), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		return status, out.getvalue(), err.getvalue()

	def test_encoding_reads_and_changes_setting(self):
		status, out, err = self.run_commands([
			".encoding",
			".encoding UTF-16le",
			".encoding",
		])
		self.assertEqual(status, 0, err)
		self.assertEqual(out.splitlines(), [
			"encoding: UTF-8",
			"encoding: UTF-16le",
			"encoding: UTF-16le",
		])

	def test_encoding_accepts_utf8_alias(self):
		status, out, err = self.run_commands([".encoding utf8"])
		self.assertEqual(status, 0, err)
		self.assertEqual(out, "encoding: UTF-8\n")

	def test_encoding_rejects_invalid_value(self):
		status, out, err = self.run_commands([".encoding latin1"])
		self.assertEqual(status, 1)
		self.assertIn("Usage: .encoding", err)

	def test_encoding_requires_database(self):
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".encoding", ".exit"]), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		self.assertEqual(status, 1)
		self.assertIn("No database opened", out.getvalue())


if __name__ == "__main__":
	unittest.main()
