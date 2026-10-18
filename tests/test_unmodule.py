import contextlib
import io
import sqlite3
import unittest
from unittest.mock import patch

import main


class UnmoduleTests(unittest.TestCase):
	def test_unmodule_reports_python_api_limitation(self):
		with contextlib.closing(sqlite3.connect(":memory:")) as connection:
			with self.assertRaisesRegex(ValueError, r"does not expose virtual-table"):
				main.pale_unmodule(connection, "fts5")

	def test_unmodule_requires_name(self):
		with contextlib.closing(sqlite3.connect(":memory:")) as connection:
			with self.assertRaisesRegex(ValueError, r"Usage: \.unmodule"):
				main.pale_unmodule(connection, "")

	def test_unmodule_requires_database(self):
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".unmodule fts5", ".exit"]), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		self.assertEqual(status, 1)
		self.assertIn("No database opened", out.getvalue())


if __name__ == "__main__":
	unittest.main()
