import contextlib
import io
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from palesql.cli import PaleCLI
from palesql.commands.connections import close_all, new_memory_connection
from palesql.commands.files import import_file
from palesql.commands.schema import pale_tables
from palesql.sql import execute_sql


ROOT = Path(__file__).resolve().parents[1]


def run_palesql(*arguments):
    return subprocess.run(
        [sys.executable, "-m", "palesql", "--batch", *map(str, arguments)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class FailingWriter:
    name = "simulated-full-disk.txt"

    def isatty(self):
        return False

    def write(self, _text):
        raise OSError("No space left on device")

    def flush(self):
        return None

    def close(self):
        return None


class HardeningTests(unittest.TestCase):
    def test_corrupt_database_and_recovery_report_clean_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "corrupt.db"
            database.write_bytes(b"this is not an SQLite database")
            query = run_palesql(database, "SELECT name FROM sqlite_schema;")
            recover = run_palesql("--cmd", ".recover", database)

        for result in (query, recover):
            message = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("database", message.lower())
            self.assertNotIn("Traceback", message)

    def test_large_select_streams_rows_without_fetchall(self):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            success = execute_sql(
                connection.cursor(),
                "WITH RECURSIVE rows(value) AS "
                "(VALUES(1) UNION ALL SELECT value + 1 FROM rows WHERE value < 50000) "
                "SELECT value FROM rows;",
                mode="list",
            )
        connection.close()
        self.assertTrue(success)
        self.assertEqual(len(output.getvalue().splitlines()), 50_000)
        self.assertTrue(output.getvalue().endswith("50000\n"))

    def test_large_csv_import_is_batched_and_atomic(self):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        cursor = connection.cursor()
        cursor.execute("CREATE TABLE records(id INTEGER, value TEXT)")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "records.csv"
            with source.open("w", encoding="utf-8", newline="") as stream:
                for number in range(12_500):
                    stream.write(f"{number},value-{number}\n")
            inserted = import_file(cursor, source, "records")
        self.assertEqual(inserted, 12_500)
        self.assertEqual(cursor.execute("SELECT count(*) FROM records").fetchone()[0], 12_500)
        connection.close()

    def test_import_rolls_back_when_a_late_csv_row_is_invalid(self):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        cursor = connection.cursor()
        cursor.execute("CREATE TABLE records(id INTEGER, value TEXT)")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "broken.csv"
            source.write_text("1,first\n2,second\n3,too,many\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "expected 2 columns"):
                import_file(cursor, source, "records")
        self.assertEqual(cursor.execute("SELECT count(*) FROM records").fetchone()[0], 0)
        connection.close()

    def test_import_rolls_back_if_it_is_interrupted(self):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        cursor = connection.cursor()
        cursor.execute("CREATE TABLE records(id INTEGER, value TEXT)")

        def interrupted_rows(*_arguments, **_keywords):
            yield ["1", "first"]
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "records.csv"
            source.write_text("placeholder\n", encoding="utf-8")
            with patch("palesql.commands.files.csv.reader", side_effect=interrupted_rows):
                with self.assertRaises(KeyboardInterrupt):
                    import_file(cursor, source, "records")
        self.assertEqual(cursor.execute("SELECT count(*) FROM records").fetchone()[0], 0)
        connection.close()

    def test_very_long_sql_statement_executes(self):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        statement = "SELECT length('" + "x" * 100_000 + "');"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            success = execute_sql(connection.cursor(), statement)
        connection.close()
        self.assertTrue(success)
        self.assertEqual(output.getvalue(), "100000\n")

    def test_thousands_of_tables_are_listed(self):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        cursor = connection.cursor()
        for number in range(1_000):
            cursor.execute(f"CREATE TABLE table_{number}(value INTEGER)")
        rows = pale_tables(cursor, "%")
        connection.close()
        self.assertEqual(len(rows), 1_000)
        self.assertEqual(rows[0], ("table_0",))
        self.assertEqual(rows[-1], ("table_999",))

    def test_many_open_close_cycles_and_connections_release_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "cycles.db"
            for _ in range(50):
                connection = sqlite3.connect(database)
                connection.execute("CREATE TABLE IF NOT EXISTS records(value INTEGER)")
                connection.close()

        connections = [new_memory_connection() for _ in range(50)]
        cursors = [cursor for _, cursor in connections]
        close_all(connections)
        for cursor in cursors:
            with self.assertRaises(sqlite3.ProgrammingError):
                cursor.execute("SELECT 1")

    def test_large_blob_and_unusual_unicode_round_trip_through_output(self):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        cursor = connection.cursor()
        blob = bytes(range(256)) * 1024
        text = "Привет, 世界 🦄\u200d💻 e\u0301\x00"
        cursor.execute("CREATE TABLE payloads(blob_value BLOB, text_value TEXT)")
        cursor.execute("INSERT INTO payloads VALUES (?, ?)", (blob, text))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            success = execute_sql(cursor, "SELECT blob_value, text_value FROM payloads;", mode="quote")
        connection.close()
        self.assertTrue(success)
        rendered = output.getvalue()
        self.assertIn(text.encode("utf-8").hex(), rendered)
        self.assertIn(blob.hex(), rendered)

    def test_invalid_utf8_read_and_import_fail_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad_script = root / "invalid.sql"
            bad_csv = root / "invalid.csv"
            database = root / "records.db"
            bad_script.write_bytes(b"SELECT 1;\xff")
            bad_csv.write_bytes(b"1,valid\n2,\xff\n")
            setup = sqlite3.connect(database)
            setup.execute("CREATE TABLE records(id INTEGER, value TEXT)")
            setup.commit()
            setup.close()
            read = run_palesql("--cmd", f'.read "{bad_script}"', ":memory:")
            imported = run_palesql("--cmd", f'.import "{bad_csv}" records', database)

        for result in (read, imported):
            message = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("error", message.lower())
            self.assertNotIn("Traceback", message)

    def test_incomplete_read_and_output_targets_fail_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "incomplete.sql"
            script.write_text("CREATE TABLE unfinished(\n", encoding="utf-8")
            database = root / "data.db"
            for command in (f'.read "{script}"', f'.output "{root}"', f'.once "{root}"'):
                result = run_palesql("--cmd", command, database, "SELECT 1;")
                message = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("error", message.lower())
                self.assertNotIn("Traceback", message)

    def test_interrupt_during_execution_clears_buffer_and_keeps_cli_alive(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            cli = PaleCLI(["--interactive", ":memory:"])
            with patch("palesql.cli.cli_input", side_effect=["SELECT 1;", ".exit"]), patch(
                "palesql.cli.execute_sql", side_effect=KeyboardInterrupt
            ):
                status = cli.run()
        self.assertEqual(status, 0, errors.getvalue())
        self.assertIn("^C", output.getvalue())
        self.assertEqual(cli.state.sql_buffer, "")
        self.assertNotIn("Traceback", output.getvalue() + errors.getvalue())

    def test_sql_error_can_be_rolled_back_and_later_commands_still_run(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "rollback.db"
            setup = sqlite3.connect(database)
            setup.execute("CREATE TABLE entries(value TEXT)")
            setup.commit()
            setup.close()
            result = run_palesql(
                "--cmd", "BEGIN;",
                "--cmd", "INSERT INTO entries VALUES ('discard');",
                "--cmd", "INSERT INTO missing_table VALUES (1);",
                "--cmd", "ROLLBACK;",
                database,
                "SELECT count(*) FROM entries;",
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SQL error:", result.stderr)
        self.assertTrue(result.stdout.endswith("0\n"), result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_sqlite_length_limit_reports_an_error_without_traceback(self):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        if not hasattr(connection, "setlimit"):
            self.skipTest("sqlite3.Connection.setlimit is unavailable")
        original = connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 128)
        output, errors = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                success = execute_sql(connection.cursor(), "SELECT '" + "x" * 512 + "';")
        finally:
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, original)
            connection.close()
        self.assertFalse(success)
        self.assertIn("SQL error:", errors.getvalue())
        self.assertNotIn("Traceback", output.getvalue() + errors.getvalue())

    def test_write_error_during_redirect_returns_to_console_without_traceback(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            cli = PaleCLI([":memory:"])
            cli.configure()
            cli.open_initial_database()
            cli.state.output_file = FailingWriter()
            sys.stdout = cli.state.output_file
            cli.run_sql("SELECT 1;")
            cli.close()
        self.assertTrue(cli.state.failed)
        self.assertIsNone(cli.state.output_file)
        self.assertIn("Output error: No space left on device", errors.getvalue())
        self.assertNotIn("Traceback", output.getvalue() + errors.getvalue())


if __name__ == "__main__":
    unittest.main()
