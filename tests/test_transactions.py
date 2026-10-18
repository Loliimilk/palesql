import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_palesql(*arguments):
    return subprocess.run(
        [sys.executable, "-m", "palesql", *map(str, arguments)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class TransactionTests(unittest.TestCase):
    def test_begin_rollback_and_commit_keep_sql_transaction_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "transactions.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE entries(value TEXT)")
            connection.commit()
            connection.close()

            rolled_back = run_palesql(
                "--cmd", "BEGIN;",
                "--cmd", "INSERT INTO entries VALUES ('discard');",
                "--cmd", "ROLLBACK;",
                database,
                "SELECT count(*) FROM entries;",
            )
            self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
            self.assertEqual(rolled_back.stdout.strip(), "0")

            committed = run_palesql(
                "--cmd", "BEGIN;",
                "--cmd", "INSERT INTO entries VALUES ('keep');",
                "--cmd", "COMMIT;",
                database,
                "SELECT value FROM entries;",
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)
            self.assertEqual(committed.stdout.strip(), "keep")

    def test_locked_database_reports_a_regular_sql_error_and_fails_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "locked.db"
            holder = sqlite3.connect(database, isolation_level=None)
            holder.execute("CREATE TABLE entries(value TEXT)")
            holder.execute("BEGIN EXCLUSIVE")
            try:
                result = run_palesql(
                    "--cmd", ".timeout 50",
                    database,
                    "INSERT INTO entries VALUES ('blocked');",
                )
            finally:
                holder.rollback()
                holder.close()
        message = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("locked", message.lower())
        self.assertNotIn("Traceback", message)

    def test_syntax_error_has_a_nonzero_batch_exit_code_without_traceback(self):
        result = run_palesql(":memory:", "SELEC 1;")
        message = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SQL error:", message)
        self.assertNotIn("Traceback", message)

    def test_attach_database_is_available_to_following_sql(self):
        result = run_palesql(
            "--cmd", "ATTACH DATABASE ':memory:' AS extra;",
            "--cmd", "CREATE TABLE extra.items(value TEXT);",
            "--cmd", "INSERT INTO extra.items VALUES ('attached');",
            ":memory:",
            "SELECT value FROM extra.items;",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "attached")


if __name__ == "__main__":
    unittest.main()
