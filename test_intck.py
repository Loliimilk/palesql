import contextlib
import io
import sqlite3
import unittest
from unittest.mock import patch

import main


class IntegrityCheckTests(unittest.TestCase):
	def run_commands(self, commands, connection=None):
		connection = connection or sqlite3.connect(":memory:", isolation_level=None)
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".open demo", *commands, ".exit"]), \
			patch("main.pale_open", return_value=(connection, connection.cursor())), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		return status, out.getvalue(), err.getvalue(), connection

	def test_intck_reports_ok_for_healthy_database(self):
		status, out, err, connection = self.run_commands([
			"CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT);",
			"INSERT INTO users VALUES(1, 'Ada');",
			".intck",
		])
		self.assertEqual(status, 0, err)
		self.assertEqual(out, "ok\n")
		connection.close()

	def test_intck_reports_usage_for_arguments(self):
		status, out, err, connection = self.run_commands([".intck main"])
		self.assertEqual(status, 0)
		self.assertIn("Usage: .intck", out)
		self.assertEqual(err, "")
		connection.close()

	def test_intck_does_not_change_an_open_transaction(self):
		connection = sqlite3.connect(":memory:")
		connection.execute("CREATE TABLE items(value TEXT)")
		connection.execute("BEGIN")
		connection.execute("INSERT INTO items VALUES('uncommitted')")
		out = io.StringIO()
		with contextlib.redirect_stdout(out):
			main.pale_integrity_check(connection.cursor())
		self.assertEqual(out.getvalue(), "ok\n")
		self.assertTrue(connection.in_transaction)
		self.assertEqual(connection.execute("SELECT value FROM items").fetchone()[0], "uncommitted")
		connection.rollback()
		connection.close()


if __name__ == "__main__":
	unittest.main()
