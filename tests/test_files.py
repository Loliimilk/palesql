import contextlib
import io
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from palesql.commands.files import (
    import_file,
    pale_archive,
    pale_dbtotxt,
    pale_dump,
    pale_recover,
    pale_sha3sum,
    parse_archive_arguments,
    parse_backup_arguments,
    parse_import_arguments,
    parse_single_file_argument,
)
from tests.fixtures import open_fixture_connection, test_database


class FileCommandTests(unittest.TestCase):
    def capture(self, function, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = function(*arguments)
        return result, output.getvalue()

    def test_import_uses_csv_and_nullvalue(self):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        cursor = connection.cursor()
        cursor.execute("CREATE TABLE records(id INTEGER, name TEXT)")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "records.csv"
            path.write_text("1,first\n2,(null)\n", encoding="utf-8")
            inserted = import_file(cursor, path, "records", "(null)")
        self.assertEqual(inserted, 2)
        self.assertEqual(cursor.execute("SELECT id, name FROM records ORDER BY id").fetchall(), [(1, "first"), (2, None)])
        connection.close()

    def test_import_argument_parsing_supports_spaces_and_rejects_missing_table(self):
        self.assertEqual(parse_import_arguments('"input data.csv" users'), ["input data.csv", "users"])
        with self.assertRaisesRegex(ValueError, "Usage"):
            parse_import_arguments("input.csv")

    def test_dump_is_restorable_and_includes_schema_data_and_indexes(self):
        connection = open_fixture_connection()
        _, dump = self.capture(pale_dump, connection.cursor())
        restored = sqlite3.connect(":memory:")
        restored.executescript(dump)
        self.assertEqual(restored.execute("SELECT count(*) FROM users").fetchone()[0], 3)
        self.assertEqual(restored.execute("SELECT count(*) FROM orders").fetchone()[0], 3)
        self.assertIsNotNone(restored.execute("SELECT sql FROM sqlite_schema WHERE name = 'orders_user_id'").fetchone()[0])
        restored.close()
        connection.close()

    def test_recover_from_healthy_database_emits_restorable_script(self):
        connection = open_fixture_connection()
        complete, script = self.capture(pale_recover, connection)
        self.assertTrue(complete)
        restored = sqlite3.connect(":memory:")
        restored.executescript(script)
        self.assertEqual(restored.execute("SELECT username FROM users ORDER BY id").fetchall(), [("alex",), ("ada",), ("sean",)])
        restored.close()
        connection.close()

    def test_archive_can_create_list_and_extract_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("archive content", encoding="utf-8")
            archive = root / "files.sqlar"
            self.capture(pale_archive, f'-c -f "{archive}" -C "{root}" source.txt')
            _, listing = self.capture(pale_archive, f'-t -f "{archive}"')
            self.assertEqual(listing, "source.txt\t15\n")
            destination = root / "extracted"
            self.capture(pale_archive, f'-x -f "{archive}" -C "{destination}"')
            self.assertEqual((destination / "source.txt").read_text(encoding="utf-8"), "archive content")

    def test_archive_and_backup_argument_parsers_reject_ambiguous_input(self):
        action, archive, root, pattern, verbose, dryrun, files = parse_archive_arguments("-t -v --glob '*.txt'")
        self.assertEqual((action, archive.name, root, pattern, verbose, dryrun, files), ("list", "archive.sqlar", Path.cwd(), "*.txt", True, False, []))
        self.assertEqual(parse_backup_arguments('"backup copy.db"'), ("main", "backup copy.db"))
        self.assertEqual(parse_single_file_argument('"copy.db"', ".save"), "copy.db")
        with self.assertRaisesRegex(ValueError, "choose one"):
            parse_archive_arguments("-c -t")

    def test_dbtotxt_reports_sqlite_header_and_sha3sum_is_content_sensitive(self):
        with test_database() as path:
            connection = sqlite3.connect(path, isolation_level=None)
            _, dump = self.capture(pale_dbtotxt, connection.cursor())
            _, first = self.capture(pale_sha3sum, connection)
            connection.execute("UPDATE users SET city = 'Paris' WHERE id = 1")
            _, second = self.capture(pale_sha3sum, connection)
            connection.close()
        self.assertIn("SQLite format 3", dump)
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^sha3sum: [0-9a-f]{64}\n$")


if __name__ == "__main__":
    unittest.main()
