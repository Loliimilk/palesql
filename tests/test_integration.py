"""Black-box checks for the published PaleSQL command line."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests.fixtures import test_database


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"


def run_palesql(*arguments):
    """Run the real executable and return its completed process."""

    return subprocess.run(
        [sys.executable, str(MAIN), *map(str, arguments)],
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


if __name__ == "__main__":
    unittest.main()
