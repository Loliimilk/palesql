import contextlib
import io
import sqlite3
import unittest
from unittest.mock import patch

import main


class ProgressTests(unittest.TestCase):
	def run_commands(self, commands):
		connection = sqlite3.connect(":memory:", isolation_level=None)
		out, err = io.StringIO(), io.StringIO()
		with patch("builtins.input", side_effect=[".open demo", *commands, ".exit"]), \
			patch("main.pale_open", return_value=(connection, connection.cursor())), \
			contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			status = main.main()
		return status, out.getvalue(), err.getvalue()

	def test_progress_callback_runs_every_requested_opcode_interval(self):
		status, out, err = self.run_commands([
			".progress 1",
			"SELECT 1;",
		])
		self.assertEqual(status, 0, err)
		self.assertRegex(out, r"Progress 1\n")
		self.assertTrue(out.rstrip().endswith("1"), out)

	def test_zero_disables_progress_callback(self):
		status, out, err = self.run_commands([
			".progress 1",
			".progress 0",
			"SELECT 1;",
		])
		self.assertEqual(status, 0, err)
		self.assertNotIn("Progress", out)
		self.assertTrue(out.rstrip().endswith("1"), out)

	def test_progress_rejects_invalid_values(self):
		status, out, err = self.run_commands([
			".progress -1",
			".progress nope",
		])
		self.assertEqual(status, 0, err)
		self.assertEqual(out.count("Usage: .progress"), 2)

	def test_progress_output_follows_output_redirection(self):
		with self.subTest("output"):
			from pathlib import Path
			from tempfile import TemporaryDirectory
			with TemporaryDirectory() as directory:
				path = Path(directory) / "progress.txt"
				status, out, err = self.run_commands([
					f'.output "{path}"',
					".progress 1",
					"SELECT 1;",
				])
				self.assertEqual(status, 0, err)
				self.assertEqual(out, "")
				self.assertIn("Progress", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
	unittest.main()
