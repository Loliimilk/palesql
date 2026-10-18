import contextlib
import io
import sqlite3
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from palesql.commands.system import pale_session, pale_shell, session_changes


class SystemCommandTests(unittest.TestCase):
    def capture(self, function, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = function(*arguments)
        return result, output.getvalue()

    def test_session_tracks_changes_and_writes_restorable_changeset(self):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.executescript("CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT); INSERT INTO items VALUES (1, 'before');")
        state, output = self.capture(pale_session, connection, "open changes", None)
        self.assertIn("opened", output)
        state, _ = self.capture(pale_session, connection, "attach items", state)
        connection.executescript("UPDATE items SET name = 'after' WHERE id = 1; INSERT INTO items VALUES (2, 'new');")
        changes = session_changes(connection, state)
        self.assertEqual(len(changes), 2)
        self.assertTrue(any(change.startswith("UPDATE") for change in changes))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "changes.sql"
            _, output = self.capture(pale_session, connection, f'changeset "{path}"', state)
            self.assertIn("2 changes", output)
            script = path.read_text(encoding="utf-8")
        self.assertIn("BEGIN;", script)
        self.assertIn("COMMIT;", script)
        connection.close()

    def test_shell_writes_stdout_and_reports_nonzero_status(self):
        complete = subprocess.CompletedProcess("command", 3, stdout="hello\n", stderr="problem\n")
        output, errors = io.StringIO(), io.StringIO()
        with patch("palesql.commands.system.subprocess.run", return_value=complete), \
            contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            success = pale_shell("command")
        self.assertFalse(success)
        self.assertEqual(output.getvalue(), "hello\n")
        self.assertIn("problem", errors.getvalue())
        self.assertIn("status 3", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
