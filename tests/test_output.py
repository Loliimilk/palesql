import contextlib
import io
import json
import unittest

from palesql.output import print_results
from palesql.sql import execute_sql


class StreamingCursor:
    description = [("value",)]

    def __init__(self, count):
        self.count = count
        self.executed = []

    def execute(self, statement, parameters=None):
        self.executed.append((statement, parameters))
        return self

    def __iter__(self):
        return iter((index,) for index in range(self.count))


class OutputTests(unittest.TestCase):
    def capture_results(self, *arguments, **keywords):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_results(*arguments, **keywords)
        return output.getvalue()

    def test_csv_escapes_unicode_null_and_embedded_newlines(self):
        result = self.capture_results(
            ["name", "note"],
            [("Зоя", None), ("Ada", 'one,"two"\nthree')],
            True,
            "csv",
            "(null)",
        )
        self.assertEqual(
            result,
            'name,note\r\nЗоя,(null)\r\nAda,"one,""two""\nthree"\r\n',
        )

    def test_json_keeps_null_and_uses_hex_for_blobs(self):
        result = self.capture_results(
            ["name", "payload", "missing"],
            [("Лера", b"\x00\xff", None)],
            True,
            "json",
            "ignored",
        )
        self.assertEqual(
            json.loads(result),
            [{"name": "Лера", "payload": "00ff", "missing": None}],
        )

    def test_large_list_query_uses_cursor_iteration(self):
        cursor = StreamingCursor(10_000)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            success = execute_sql(cursor, "SELECT value FROM generated_rows", mode="list")
        self.assertTrue(success)
        self.assertEqual(cursor.executed, [("SELECT value FROM generated_rows", None)])
        self.assertEqual(len(output.getvalue().splitlines()), 10_000)
        self.assertTrue(output.getvalue().endswith("9999\n"))


if __name__ == "__main__":
    unittest.main()
