import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class FakeConnection:
	def __init__(self, error=None):
		self.events = []
		self.error = error

	def enable_load_extension(self, enabled):
		self.events.append(("enable", enabled))

	def load_extension(self, path, *, entrypoint=None):
		self.events.append(("load", path, entrypoint))
		if self.error:
			raise self.error


class LoadTests(unittest.TestCase):
	def test_load_extension_disables_loading_after_success(self):
		connection = FakeConnection()
		with tempfile.TemporaryDirectory() as directory:
			main.pale_load_extension(connection, "extension.dll init_extension", Path(directory))
		self.assertEqual(connection.events[0], ("enable", True))
		self.assertEqual(connection.events[1][0], "load")
		self.assertEqual(connection.events[1][2], "init_extension")
		self.assertEqual(connection.events[-1], ("enable", False))

	def test_load_extension_disables_loading_after_error(self):
		connection = FakeConnection(sqlite3.OperationalError("load failed"))
		with self.assertRaises(sqlite3.OperationalError):
			main.pale_load_extension(connection, "extension.dll")
		self.assertEqual(connection.events[-1], ("enable", False))

	def test_load_extension_rejects_invalid_arguments(self):
		with self.assertRaisesRegex(ValueError, r"Usage: \.load"):
			main.pale_load_extension(FakeConnection(), "")
		with self.assertRaisesRegex(ValueError, r"Usage: \.load"):
			main.pale_load_extension(FakeConnection(), "one two three")

	def test_load_requires_database(self):
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".load extension.dll", ".exit"]), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		self.assertEqual(status, 1)
		self.assertIn("No database opened", out.getvalue())


if __name__ == "__main__":
	unittest.main()
