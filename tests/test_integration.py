import json
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests.fixtures import test_database


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"


def run_palesql(*arguments):
    return subprocess.run(
        [sys.executable, str(MAIN), *map(str, arguments)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def run_package(*arguments):
    return subprocess.run(
        [sys.executable, "-m", "palesql", *map(str, arguments)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class CommandLineIntegrationTests(unittest.TestCase):
    def test_version_and_help_are_available_without_database(self):
        version = run_palesql("--version")
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn("PaleSQL version", version.stdout)

        help_result = run_palesql("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn(".tables", help_result.stdout)
        self.assertIn("--readonly", help_result.stdout)

    def test_python_module_entry_point_runs_the_same_cli(self):
        version = run_package("--version")
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn("PaleSQL version", version.stdout)

        with test_database() as database:
            result = run_package(database, "SELECT count(*) FROM users;")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "3")

    def test_console_entry_point_uses_process_arguments(self):
        output = io.StringIO()
        with test_database() as database, patch.object(
            sys,
            "argv",
            ["palesql", str(database), "SELECT count(*) FROM users;"],
        ), redirect_stdout(output):
            from palesql import cli

            result = cli.run()
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "3\n")

    def test_status_reports_cli_state_without_a_database(self):
        result = run_package("--batch", "--cmd", ".status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("database: none", result.stdout)
        self.assertIn("transaction state: inactive", result.stdout)

    def test_json_query_uses_real_database_and_preserves_unicode(self):
        with test_database() as database:
            result = run_palesql(
                "--json",
                "--header",
                database,
                "SELECT username, full_name FROM users ORDER BY id;",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual(rows[0]["username"], "alex")
        self.assertEqual(rows[0]["full_name"], "Alex Morgan")

    def test_readonly_database_rejects_write(self):
        with test_database() as database:
            result = run_palesql(
                "--readonly",
                database,
                "INSERT INTO users(username) VALUES ('blocked');",
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("readonly", (result.stdout + result.stderr).lower())

    def test_readonly_uri_forces_read_only_mode(self):
        with test_database() as database:
            writable_uri = database.resolve().as_uri() + "?mode=rw"
            result = run_package(
                "--readonly",
                writable_uri,
                "INSERT INTO users(username) VALUES ('blocked');",
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("readonly", (result.stdout + result.stderr).lower())

    def test_init_script_finishes_before_the_startup_query_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "startup.sql"
            script.write_text(
                "CREATE TABLE startup_rows(value TEXT);\n"
                "INSERT INTO startup_rows VALUES ('ready');\n",
                encoding="utf-8",
            )
            result = run_package("--init", script, ":memory:", "SELECT value FROM startup_rows;")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ready")


if __name__ == "__main__":
    unittest.main()
