import shlex
import sqlite3
import sys
from pathlib import Path

from ..database import get_databases, pale_open, pale_open_readonly


CONNECTION_COMMANDS = frozenset({
    ".connection", ".connections", ".database", ".databases", ".memory", ".reopen", ".readonly",
})


def parse_connection_action(argument):
    values = shlex.split(argument)
    return (values[0].lower() if values else "list"), values


def connection_rows(connections, active_index):
    rows = []
    for number, (_, cursor) in enumerate(connections, 1):
        row = cursor.execute("PRAGMA database_list").fetchone()
        filename = row[2] if row else ""
        rows.append((number, number - 1 == active_index, filename or ":memory:"))
    return rows


def new_memory_connection():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    return connection, connection.cursor()


def select_connection(connections, number):
    if not 1 <= number <= len(connections):
        raise ValueError("connection number out of range")
    return number - 1, *connections[number - 1]


def close_connection(connections, active_index, number):
    if not 1 <= number <= len(connections):
        raise ValueError("connection number out of range")
    index = number - 1
    connection, cursor = connections.pop(index)
    cursor.close()
    connection.close()
    if not connections:
        return None, None, None
    if active_index == index:
        active_index = min(index, len(connections) - 1)
        return active_index, *connections[active_index]
    if active_index > index:
        active_index -= 1
    return active_index, *connections[active_index]


def close_all(connections):
    closed = set()
    for connection, cursor in connections:
        if id(connection) not in closed:
            cursor.close()
            connection.close()
            closed.add(id(connection))


def _single_argument(argument, usage):
    lexer = shlex.shlex(argument, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    values = list(lexer)
    if len(values) != 1 or not values[0]:
        raise ValueError(usage)
    return values[0]


def handle_connection_command(
    command,
    argument,
    state,
    diagnostics,
    interactive=False,
    open_database=pale_open,
    open_readonly=pale_open_readonly,
):
    error_target = sys.stderr if state.output_file else sys.stdout
    if command == ".connections":
        if argument:
            print("Usage: .connections", file=error_target)
            return True
        command, argument = ".connection", "list"
    if command == ".connection":
        try:
            action, values = parse_connection_action(argument)
            if action == "list" and len(values) == 1:
                if not state.connections:
                    print("No connections")
                else:
                    for number, active, filename in connection_rows(state.connections, state.active_connection):
                        print(f"{number}{' *' if active else ''}\t{filename}")
                return True
            if action == "new" and len(values) == 1:
                state.connection, state.cursor = new_memory_connection()
                state.connections.append((state.connection, state.cursor))
                state.active_connection = len(state.connections) - 1
                print(f"Connection {state.active_connection + 1} opened")
                return True
            if action == "use" and len(values) == 2 and values[1].isdigit():
                diagnostics.clear_trace(state.connection)
                state.active_connection, state.connection, state.cursor = select_connection(state.connections, int(values[1]))
                print(f"Using connection {state.active_connection + 1}")
                return True
            if action == "close" and len(values) == 2 and values[1].isdigit():
                number = int(values[1])
                if state.active_connection == number - 1:
                    diagnostics.clear_trace(state.connection)
                state.active_connection, state.connection, state.cursor = close_connection(
                    state.connections, state.active_connection, number
                )
                print(f"Connection {number} closed")
                return True
            raise ValueError("Usage: .connection new|list|use N|close N")
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f"Connection error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".memory":
        if argument:
            print("Usage: .memory", file=error_target)
            return True
        try:
            connection, cursor = new_memory_connection()
            if state.connection is not None:
                diagnostics.clear_trace(state.connection)
                old_connection = state.connection
                state.connections[state.active_connection] = (connection, cursor)
                state.connection, state.cursor = connection, cursor
                old_connection.close()
            else:
                state.connections.append((connection, cursor))
                state.active_connection = len(state.connections) - 1
                state.connection, state.cursor = connection, cursor
            if diagnostics.authorizer_enabled:
                diagnostics.set_authorizer(state.connection, True)
            state.clear_sql()
            print("Opened :memory:")
        except sqlite3.Error as error:
            print(f"Memory database error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".reopen":
        if argument:
            print("Usage: .reopen", file=error_target)
            return True
        if state.connection is None:
            print("No database opened", file=error_target)
            return True
        try:
            main_row = next((row for row in get_databases(state.cursor) if row[1] == "main"), None)
            if not main_row or not main_row[2]:
                raise ValueError("cannot reopen an in-memory database")
            connection, cursor = sqlite3.connect(main_row[2], isolation_level=None), None
            cursor = connection.cursor()
            diagnostics.clear_trace(state.connection)
            old_connection = state.connection
            state.connections[state.active_connection] = (connection, cursor)
            state.connection, state.cursor = connection, cursor
            old_connection.close()
            if diagnostics.authorizer_enabled:
                diagnostics.set_authorizer(state.connection, True)
            state.clear_sql()
            print(f"Reopened {main_row[2]}")
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f"Reopen error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".readonly":
        try:
            filename = _single_argument(argument, "Usage: .readonly <file>")
            display_path = Path(filename).expanduser().resolve()
            connection, cursor = open_readonly(filename)
            if state.connection is not None:
                diagnostics.clear_trace(state.connection)
                old_connection = state.connection
                state.connections[state.active_connection] = (connection, cursor)
                state.connection, state.cursor = connection, cursor
                old_connection.close()
            else:
                state.connections.append((connection, cursor))
                state.active_connection = len(state.connections) - 1
                state.connection, state.cursor = connection, cursor
            if diagnostics.authorizer_enabled:
                diagnostics.set_authorizer(state.connection, True)
            state.clear_sql()
            print(f"Opened read-only: {display_path}")
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f"Read-only error: {error}", file=sys.stderr)
            state.failed = True
        return True
    return False
