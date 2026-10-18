import contextlib
import io
import sqlite3
import unittest
from unittest.mock import patch

import main


HAS_DBCONFIG = (
	hasattr(sqlite3.Connection, "getconfig")
	and hasattr(sqlite3.Connection, "setconfig")
)


class DbconfigTests(unittest.TestCase):
	def run_commands(self, commands):
		connection = sqlite3.connect(":memory:", isolation_level=None)
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".open demo", *commands, ".exit"]), \
			patch("main.pale_open", return_value=(connection, connection.cursor())), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		return status, out.getvalue(), err.getvalue()

	@unittest.skipUnless(HAS_DBCONFIG, "sqlite3_db_config API unavailable")
	def test_dbconfig_lists_supported_options(self):
		status, out, err = self.run_commands([".dbconfig"])
		self.assertEqual(status, 0, err)
		self.assertIn("enable_fkey", out)
		self.assertIn("trusted_schema", out)

	@unittest.skipUnless(HAS_DBCONFIG, "sqlite3_db_config API unavailable")
	def test_dbconfig_reads_and_changes_option(self):
		status, out, err = self.run_commands([
			".dbconfig enable_fkey",
			".dbconfig enable_fkey on",
			".dbconfig enable_fkey",
		])
		self.assertEqual(status, 0, err)
		self.assertRegex(out, r"enable_fkey:\s+0")
		self.assertRegex(out, r"enable_fkey:\s+1")

	@unittest.skipUnless(HAS_DBCONFIG, "sqlite3_db_config API unavailable")
	def test_dbconfig_accepts_boolean_aliases(self):
		status, out, err = self.run_commands([
			".dbconfig enable_fkey true",
			".dbconfig enable_fkey false",
		])
		self.assertEqual(status, 0, err)
		self.assertRegex(out, r"enable_fkey:\s+0")

	@unittest.skipUnless(HAS_DBCONFIG, "sqlite3_db_config API unavailable")
	def test_dbconfig_rejects_unknown_option(self):
		status, out, err = self.run_commands([".dbconfig unknown"])
		self.assertEqual(status, 1)
		self.assertIn("unknown dbconfig option", err)

	def test_dbconfig_reports_an_unavailable_api_without_traceback(self):
		if HAS_DBCONFIG:
			return
		status, out, err = self.run_commands([".dbconfig"])
		self.assertEqual(status, 1)
		self.assertEqual(out, "")
		self.assertIn("does not expose sqlite3_db_config", err)
		self.assertNotIn("Traceback", out + err)


if __name__ == "__main__":
	unittest.main()
