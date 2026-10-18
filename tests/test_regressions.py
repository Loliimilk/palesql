import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RegressionTests(unittest.TestCase):
    def test_read_completion_does_not_skip_a_following_startup_query(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "startup.sql"
            script.write_text(
                "CREATE TABLE startup_rows(value TEXT);\n"
                "INSERT INTO startup_rows VALUES ('ready');\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, "-m", "palesql", "--init", str(script), ":memory:", "SELECT value FROM startup_rows;"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ready")


if __name__ == "__main__":
    unittest.main()
