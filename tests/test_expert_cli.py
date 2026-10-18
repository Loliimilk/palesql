import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


REPORT = "CREATE INDEX users_name ON users(name);\nSEARCH users USING INDEX users_name (name=?)\n"


class ExpertCliTests(unittest.TestCase):

	def run_cli(self, commands, *, backend_error=None):
		connections = []

		def open_database(name):
			connection = sqlite3.connect(":memory:", isolation_level=None)
			connection.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT); INSERT INTO users VALUES(1, 'Ada');")
			connections.append(connection)
			self.addCleanup(connection.close)
			return connection, connection.cursor()

		stdout = io.StringIO()
		stderr = io.StringIO()
		with patch("main.pale_open", side_effect=open_database), \
			patch("main.analyze_expert", return_value=REPORT, side_effect=backend_error) as analyze, \
			patch("builtins.input", side_effect=[*commands, ".quit"]), \
			contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
			status = main.main()
		return status, stdout.getvalue(), stderr.getvalue(), analyze, connections

	def test_next_multiline_query_is_analyzed_without_execution(self):
		status, output, errors, analyze, connections = self.run_cli([
			".open test", ".expert", "UPDATE users", "SET name = 'changed'", "WHERE id = 1;", "SELECT name FROM users;",
		])
		self.assertEqual(status, 0, errors)
		self.assertEqual(errors, "")
		analyze.assert_called_once()
		self.assertIs(analyze.call_args.args[0], connections[0])
		self.assertEqual(analyze.call_args.args[1].strip(), "UPDATE users\nSET name = 'changed'\nWHERE id = 1;")
		self.assertEqual(analyze.call_args.kwargs, {"sample": 0, "verbose": False})
		self.assertIn(REPORT.strip(), output)
		self.assertTrue(output.endswith("Ada\n"), output)

	def test_multiple_statements_in_one_buffer_are_analyzed_together(self):
		statement = "UPDATE users SET name = 'changed'; DELETE FROM users;"
		status, output, errors, analyze, _ = self.run_cli([
			".open test", ".expert", statement, "SELECT name FROM users;",
		])
		self.assertEqual(status, 0, errors)
		analyze.assert_called_once()
		self.assertEqual(analyze.call_args.args[1].strip(), statement)
		self.assertTrue(output.endswith("Ada\n"), output)

	def test_semicolons_in_strings_and_comments_do_not_finish_early(self):
		statement = "SELECT 'literal;still literal'\n/* comment; */ FROM users\nWHERE name = 'Ada';"
		status, _, errors, analyze, _ = self.run_cli([
			".open test", ".expert", *statement.splitlines(),
		])
		self.assertEqual(status, 0, errors)
		analyze.assert_called_once()
		self.assertEqual(analyze.call_args.args[1].strip(), statement)

	def test_inline_sql_preserves_quoted_values_and_option_like_text(self):
		statement = "SELECT 'it''s --sample 9', name FROM users WHERE name = 'Ada';"
		status, _, errors, analyze, _ = self.run_cli([
			".open test", ".expert " + statement,
		])
		self.assertEqual(status, 0, errors)
		analyze.assert_called_once()
		self.assertEqual(analyze.call_args.args[1].strip(), statement)

	def test_inline_query_does_not_require_a_semicolon(self):
		status, _, errors, analyze, _ = self.run_cli([
			".open test", ".expert SELECT name FROM users WHERE id = 1",
		])
		self.assertEqual(status, 0, errors)
		analyze.assert_called_once()
		self.assertEqual(analyze.call_args.args[1].strip(), "SELECT name FROM users WHERE id = 1")

	def test_options_apply_to_next_query_only(self):
		status, _, errors, analyze, _ = self.run_cli([
			".open test", ".expert --verbose --sample 50", "SELECT name FROM users;", ".expert", "SELECT id FROM users;",
		])
		self.assertEqual(status, 0, errors)
		self.assertEqual(analyze.call_count, 2)
		self.assertEqual(analyze.call_args_list[0].kwargs, {"sample": 50, "verbose": True})
		self.assertEqual(analyze.call_args_list[1].kwargs, {"sample": 0, "verbose": False})

	def test_inline_options_reach_backend(self):
		status, _, errors, analyze, _ = self.run_cli([
			".open test", ".expert --sample 100 --verbose SELECT name FROM users;",
		])
		self.assertEqual(status, 0, errors)
		analyze.assert_called_once()
		self.assertEqual(analyze.call_args.kwargs, {"sample": 100, "verbose": True})
		self.assertEqual(analyze.call_args.args[1].strip(), "SELECT name FROM users;")

	def test_invalid_options_do_not_arm_analyzer(self):
		for options in ("--sample", "--sample -1", "--sample 101", "--sample abc", "--unknown"):
			with self.subTest(options=options):
				status, output, errors, analyze, _ = self.run_cli([
					".open test", ".expert " + options, "SELECT name FROM users;",
				])
				self.assertEqual(status, 1)
				self.assertIn("Expert error:", errors)
				analyze.assert_not_called()
				self.assertTrue(output.endswith("Ada\n"), output)

	def test_expert_requires_an_open_database(self):
		status, output, errors, analyze, _ = self.run_cli([".expert"])
		self.assertEqual(status, 1)
		self.assertIn("No database opened", output + errors)
		analyze.assert_not_called()

	def test_invalid_options_cancel_previously_pending_analysis(self):
		status, output, errors, analyze, _ = self.run_cli([
			".open test", ".expert --sample 50", ".expert --sample invalid", "SELECT name FROM users;",
		])
		self.assertEqual(status, 1)
		self.assertIn("Expert error:", errors)
		analyze.assert_not_called()
		self.assertEqual(output, "Ada\n")

	def test_once_captures_report_and_restores_stdout(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "expert report.txt"
			status, output, errors, analyze, _ = self.run_cli([
				".open test", f'.once "{path}"', ".expert", "SELECT name FROM users;", "SELECT id FROM users;",
			])
			self.assertEqual(status, 0, errors)
			analyze.assert_called_once()
			self.assertEqual(path.read_text(encoding="utf-8").strip(), REPORT.strip())
			self.assertNotIn(REPORT.strip(), output)
			self.assertEqual(output, "1\n")

	def test_output_keeps_report_and_following_results(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "report.txt"
			status, output, errors, _, _ = self.run_cli([
				".open test", f'.output "{path}"', ".expert SELECT name FROM users;", "SELECT id FROM users;", ".output", "SELECT name FROM users;",
			])
			self.assertEqual(status, 0, errors)
			contents = path.read_text(encoding="utf-8")
			self.assertIn(REPORT.strip(), contents)
			self.assertTrue(contents.endswith("1\n"), contents)
			self.assertEqual(output, "Ada\n")

	def test_once_report_overrides_output_then_returns_to_console(self):
		with tempfile.TemporaryDirectory() as directory:
			output_path = Path(directory) / "regular.txt"
			once_path = Path(directory) / "expert.txt"
			status, output, errors, _, _ = self.run_cli([
				".open test", f'.output "{output_path}"', "SELECT id FROM users;", f'.once "{once_path}"',
				".expert SELECT name FROM users;", "SELECT name FROM users;",
			])
			self.assertEqual(status, 0, errors)
			self.assertEqual(output_path.read_text(encoding="utf-8"), "1\n")
			self.assertEqual(once_path.read_text(encoding="utf-8").strip(), REPORT.strip())
			self.assertEqual(output, "Ada\n")

	def test_backend_error_does_not_execute_query_or_remain_armed(self):
		status, output, errors, analyze, _ = self.run_cli([
			".open test", ".expert", "DELETE FROM users;", "SELECT name FROM users;",
		], backend_error=ValueError("native preparation failed"))
		self.assertEqual(status, 1)
		analyze.assert_called_once()
		self.assertIn("Expert error: native preparation failed", errors)
		self.assertEqual(output, "Ada\n")

	def test_backend_error_consumes_once_and_goes_to_stderr(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "expert.txt"
			status, output, errors, _, _ = self.run_cli([
				".open test", f'.once "{path}"', ".expert SELECT name FROM users;", "SELECT id FROM users;",
			], backend_error=ValueError("native preparation failed"))
			self.assertEqual(status, 1)
			self.assertIn("Expert error: native preparation failed", errors)
			self.assertEqual(path.read_text(encoding="utf-8"), "")
			self.assertEqual(output, "1\n")

	def test_read_analyzes_next_query_and_then_executes_normally(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "expert script.sql"
			path.write_text(".expert --sample 25\nDELETE FROM users;\nSELECT name FROM users;\n", encoding="utf-8")
			status, output, errors, analyze, _ = self.run_cli([".open test", f'.read "{path}"'])
			self.assertEqual(status, 0, errors)
			analyze.assert_called_once()
			self.assertEqual(analyze.call_args.kwargs, {"sample": 25, "verbose": False})
			self.assertTrue(output.endswith("Ada\n"), output)

	def test_read_backend_error_reports_file_and_line_and_honors_bail(self):
		for bail in ("on", "off"):
			with self.subTest(bail=bail), tempfile.TemporaryDirectory() as directory:
				path = Path(directory) / "bad expert.sql"
				path.write_text(".expert\nDELETE FROM users;\nINSERT INTO users VALUES(2, 'Bob');\n", encoding="utf-8")
				status, output, errors, analyze, _ = self.run_cli([
					".open test", f".bail {bail}", f'.read "{path}"', "SELECT count(*) FROM users;",
				], backend_error=ValueError("native preparation failed"))
				self.assertEqual(status, 1)
				analyze.assert_called_once()
				self.assertIn(f"{path.resolve()}:2:", errors)
				self.assertIn("native preparation failed", errors)
				self.assertEqual(output, "1\n" if bail == "on" else "2\n")

	def test_inline_read_error_reports_its_own_line(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "bad inline.sql"
			path.write_text("-- first line\n.expert SELECT missing FROM users;\n", encoding="utf-8")
			status, _, errors, analyze, _ = self.run_cli([
				".open test", f'.read "{path}"',
			], backend_error=ValueError("no such column: missing"))
			self.assertEqual(status, 1)
			analyze.assert_called_once()
			self.assertIn(f"{path.resolve()}:2:", errors)

	def test_clear_discards_pending_analysis_and_partial_sql(self):
		status, output, errors, analyze, _ = self.run_cli([
			".open test", ".expert", "SELECT id", ".clear", "INSERT INTO users VALUES(2, 'Bob');", "SELECT count(*) FROM users;",
		])
		self.assertEqual(status, 0, errors)
		analyze.assert_not_called()
		self.assertTrue(output.endswith("2\n"), output)

	def test_open_discards_pending_analysis(self):
		status, output, errors, analyze, _ = self.run_cli([
			".open first", ".expert", ".open second", "INSERT INTO users VALUES(2, 'Bob');", "SELECT count(*) FROM users;",
		])
		self.assertEqual(status, 0, errors)
		analyze.assert_not_called()
		self.assertTrue(output.endswith("2\n"), output)

	def test_close_discards_pending_analysis(self):
		status, output, errors, analyze, _ = self.run_cli([
			".open first", ".expert", ".close", ".open second", "INSERT INTO users VALUES(2, 'Bob');", "SELECT count(*) FROM users;",
		])
		self.assertEqual(status, 0, errors)
		analyze.assert_not_called()
		self.assertTrue(output.endswith("2\n"), output)


if __name__ == "__main__":
	unittest.main()
