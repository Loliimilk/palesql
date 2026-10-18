import contextlib
import io
import unittest
from unittest.mock import patch

from palesql.cli import PaleCLI


class CoreCliTests(unittest.TestCase):
    def run_cli(self, arguments, input_events, *, interactive=False):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            cli = PaleCLI((["--interactive"] if interactive else []) + list(arguments))
            target = "palesql.cli.cli_input" if interactive else "builtins.input"
            with patch(target, side_effect=input_events):
                status = cli.run()
        return status, output.getvalue(), errors.getvalue()

    def test_empty_memory_database_accepts_schema_commands(self):
        status, output, errors = self.run_cli([":memory:"], [".tables", ".exit"])
        self.assertEqual(status, 0, errors)
        self.assertEqual(output, "No tables found\n")

    def test_interrupt_clears_pending_input_and_keeps_the_cli_running(self):
        status, output, errors = self.run_cli([":memory:"], [KeyboardInterrupt, ".exit"], interactive=True)
        self.assertEqual(status, 0, errors)
        self.assertIn("^C", output)
        self.assertNotIn("Traceback", output + errors)

    def test_eof_returns_success_for_complete_input_and_error_for_partial_sql(self):
        status, output, errors = self.run_cli([":memory:"], [EOFError])
        self.assertEqual(status, 0, output + errors)

        status, output, errors = self.run_cli([":memory:"], ["SELECT 1", EOFError])
        self.assertEqual(status, 1)
        self.assertIn("incomplete statement", errors)
        self.assertNotIn("Traceback", output + errors)

    def test_unknown_command_is_human_readable_in_interactive_mode(self):
        status, output, errors = self.run_cli([":memory:"], [".missing", ".exit"])
        self.assertEqual(status, 0, errors)
        self.assertIn("Unknown command: .missing", output)
        self.assertNotIn("Traceback", output + errors)


if __name__ == "__main__":
    unittest.main()
