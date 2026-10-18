import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from palesql.cli import PaleCLI


class ResourceCleanupTests(unittest.TestCase):
    def test_cli_closes_database_output_and_trace_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "data.db"
            output_path = root / "result.txt"
            trace_path = root / "trace.txt"
            console, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(console), contextlib.redirect_stderr(errors):
                cli = PaleCLI([
                    "--cmd", f'.output "{output_path}"',
                    "--cmd", f'.trace "{trace_path}"',
                    str(database),
                    "SELECT 1 AS value;",
                ])
                status = cli.run()

            self.assertEqual(status, 0, errors.getvalue())
            self.assertTrue(cli.state.output_file.closed)
            self.assertIsNone(cli.diagnostics.trace_file)
            with self.assertRaises(sqlite3.ProgrammingError):
                cli.state.cursor.execute("SELECT 1")
            self.assertEqual(output_path.read_text(encoding="utf-8"), "1\n")
            self.assertIn("Trace: SELECT 1 AS value", trace_path.read_text(encoding="utf-8"))

            moved_output = root / "moved-result.txt"
            moved_trace = root / "moved-trace.txt"
            output_path.replace(moved_output)
            trace_path.replace(moved_trace)
            self.assertTrue(moved_output.is_file())
            self.assertTrue(moved_trace.is_file())


if __name__ == "__main__":
    unittest.main()
