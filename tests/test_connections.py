import sqlite3
import unittest

from palesql.commands.connections import (
    close_all,
    close_connection,
    connection_rows,
    new_memory_connection,
    parse_connection_action,
    select_connection,
)


class ConnectionCommandTests(unittest.TestCase):
    def test_parse_and_list_connections(self):
        first, first_cursor = new_memory_connection()
        second, second_cursor = new_memory_connection()
        connections = [(first, first_cursor), (second, second_cursor)]
        self.assertEqual(parse_connection_action("use 2"), ("use", ["use", "2"]))
        self.assertEqual(connection_rows(connections, 1), [(1, False, ":memory:"), (2, True, ":memory:")])
        close_all(connections)

    def test_select_and_close_keep_a_valid_active_connection(self):
        first, first_cursor = new_memory_connection()
        second, second_cursor = new_memory_connection()
        connections = [(first, first_cursor), (second, second_cursor)]
        index, connection, cursor = select_connection(connections, 2)
        self.assertEqual(index, 1)
        self.assertIs(connection, second)
        index, connection, cursor = close_connection(connections, index, 2)
        self.assertEqual(index, 0)
        self.assertIs(connection, first)
        self.assertEqual(cursor.execute("SELECT 1").fetchone(), (1,))
        with self.assertRaisesRegex(ValueError, "out of range"):
            select_connection(connections, 2)
        close_all(connections)

    def test_close_last_connection_returns_empty_active_state(self):
        connection, cursor = new_memory_connection()
        connections = [(connection, cursor)]
        self.assertEqual(close_connection(connections, 0, 1), (None, None, None))
        self.assertEqual(connections, [])


if __name__ == "__main__":
    unittest.main()
