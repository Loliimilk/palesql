import contextlib
import io
import sqlite3
import unittest
from unittest.mock import patch

import main


class VfsnameTests(unittest.TestCase):
	def test_vfsname_reports_python_api_limitation(self):
		with self.assertRaisesRegex(ValueError, r"does not expose the active VFS name"):
			main.pale_vfsname(sqlite3.connect(":memory:"))

	def test_vfsname_rejects_arguments(self):
		with self.assertRaisesRegex(ValueError, r"Usage: \.vfsname"):
			main.pale_vfsname(sqlite3.connect(":memory:"), "main")

	def test_vfsname_requires_database(self):
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".vfsname", ".exit"]), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		self.assertEqual(status, 1)
		self.assertIn("No database opened", out.getvalue())


if __name__ == "__main__":
	unittest.main()
