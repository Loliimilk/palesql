import contextlib
import io
import sqlite3
import unittest
from unittest.mock import patch

import main


class FilectrlTests(unittest.TestCase):
	def test_filectrl_reports_python_api_limitation(self):
		with self.assertRaisesRegex(ValueError, r"sqlite3_file_control"):
			main.pale_filectrl(sqlite3.connect(":memory:"), "size_limit")

	def test_filectrl_rejects_unknown_operation(self):
		with self.assertRaisesRegex(ValueError, r"unknown filectrl operation"):
			main.pale_filectrl(sqlite3.connect(":memory:"), "unknown")

	def test_filectrl_requires_operation(self):
		with self.assertRaisesRegex(ValueError, r"Usage: \.filectrl"):
			main.pale_filectrl(sqlite3.connect(":memory:"), "")

	def test_filectrl_requires_database(self):
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".filectrl size_limit", ".exit"]), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		self.assertEqual(status, 1)
		self.assertIn("No database opened", out.getvalue())


if __name__ == "__main__":
	unittest.main()
