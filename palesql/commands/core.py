import shlex
import sqlite3
import sys
import fnmatch

from .. import __version__
from ..database import get_databases, pale_open

CORE_COMMANDS = frozenset({
    ".help", ".version", ".open", ".close", ".exit", ".quit", ".clear", ".status",
})


def parse_single_argument(argument, usage):
    lexer = shlex.shlex(argument, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    values = list(lexer)
    if len(values) != 1 or not values[0]:
        raise ValueError(usage)
    return values[0]


def pale_help():
	print(
		".help - show commands\n"
		".read <file.sql> - run UTF-8 SQL/dot commands; nested paths are relative to the script\n"
		".output [file] - write normal output to a UTF-8 file (overwrites); no file restores stdout\n"
		".once <file> - write the next SQL result to a file, then restore stdout\n"
		".import <file> <table> - import CSV/TSV or separated data into a table\n"
		".print <text> - print literal text (no argument prints a blank line)\n"
		".prompt <main> <continue> - set prompts; quote text containing spaces\n"
		".open <database> - open or create database\n"
		".cd <directory> - change PaleSQL working directory\n"
		".pwd - show PaleSQL working directory\n"
		".history - show recent SQL and dot commands\n"
		".clearhistory - clear command history\n"
		".status - show a compact database and CLI status\n"
		".describe <table> - show extended table information\n"
		".memory - switch to a new in-memory database\n"
		".reopen - reopen the current database file\n"
		".readonly <file> - open a database read-only\n"
		".find <text> - search text columns in all tables\n"
		".count [table] - count rows in one table or all tables\n"
		".tablesize [table] - show table row counts and file size\n"
		".vacuum - compact the current database\n"
		".schemajson - show the schema as JSON\n"
		".watch <seconds> <SQL> - repeat a SQL query until interrupted\n"
		".shell <command> - execute a command through the system shell\n"
		".system <command> - alias for .shell (potentially dangerous)\n"
		".safe on|off - block shell, extension, and file operations\n"
		".auth on|off - show SQLite authorizer callbacks\n"
		".nonce <string> - allow one blocked command in safe mode\n"
		".imposter <index> <table> - create a temporary index diagnostic snapshot\n"
		".session ... - manage a text changeset session (Python fallback)\n"
		".www - show the next SQL result in a browser\n"
		".testcase NAME - begin a CLI output test case\n"
		".check GLOB - check testcase output against a glob pattern\n"
		".connection new|list|use N|close N - manage independent SQLite connections\n"
		".connections - list independent SQLite connections\n"
		".close - close current database\n"
		".database - show current main database\n"
		".databases - show all attached databases\n"
		".dbinfo [database] - show database information, defaults to main\n"
		".tables [pattern] - show tables, optionally filtered by pattern\n"
		".views - show views\n"
		".triggers - show triggers\n"
		".columns <table> - show table columns\n"
		".indexes [table] - show indexes, optionally filtered by table\n"
		".foreignkeys <table> - show table foreign keys\n"
		".schema [pattern] - show schema, optionally filtered by name/pattern\n"
		".fullschema - show full schema and statistics\n"
		".dump [objects] - output SQL to restore tables, data, indexes, triggers, and views\n"
		".recover - output best-effort recovery SQL for the current database (readable pages only)\n"
		".backup [database] <file> - create a database backup\n"
		".restore [database] <file> - restore a database from a backup file\n"
		".save <file> - save the current database to a file\n"
		".clone <file> - clone the current database to a new file\n"
		".archive [options] [files] - manage a SQLite Archive (sqlar)\n"
		".excel - use spreadsheet-friendly CSV output with headers\n"
		".dbtotxt - show a hex dump of the physical main database file\n"
		".sha3sum [pattern] - show a deterministic SHA3-256 hash of database content\n"
	".integrity - run SQLite integrity_check on the current database\n"
		".intck - incremental integrity check, table by table\n"
		".selftest - run a consistency self-test of the current database\n"
		".lint - check foreign keys and suggest indexes\n"
		".nullvalue <text> - set NULL text; JSON and quote keep native NULL values\n"
		".separator <column> [row] - set list/csv/tabs separators; supports quotes and \\t, \\n, \\r\n"
		"Changing .mode resets separators to that mode's defaults.\n"
		".crlf on|off - file output line endings (default: off/LF); explicit row separator wins\n"
		".headers on|off - show or hide column names in SQL results\n"
		".changes on|off - show changed row counts after INSERT/UPDATE/DELETE\n"
		".timeout <milliseconds> - set the SQLite busy timeout\n"
		".busy_timeout <milliseconds> - alias for .timeout\n"
		".bail on|off - stop .read scripts after the first error\n"
		".echo on|off - show lines executed from .read scripts\n"
		".parameter init|set|unset|list|clear - manage SQL parameter bindings\n"
		".limit [name] [value] - view or change SQLite runtime limits\n"
		".dbconfig [name] [on|off] - view or change SQLite connection options\n"
		".foreign_keys [on|off] - view or change PRAGMA foreign_keys\n"
		".journal_mode [mode] - view or change PRAGMA journal_mode\n"
		".encoding [UTF-8|UTF-16le|UTF-16be] - view or change database encoding\n"
		".load <file> [entrypoint] - load a SQLite extension\n"
		".unmodule <name> - remove a virtual-table module\n"
		".vfsinfo - show SQLite VFS information\n"
		".vfsname - show the active SQLite VFS name\n"
		".filectrl <operation> [arguments] - SQLite file-control operation\n"
		".timer on|off - show SQL execution time\n"
		".stats on|off - show SQL execution statistics\n"
		".scanstats on|off|est - show scan metrics (estimated with Python sqlite3)\n"
		".progress N - invoke a progress callback every N SQLite opcodes (0 disables)\n"
		".trace on|off|stdout|stderr|<file> - show SQL executed by SQLite\n"
		".log off|stdout|stderr|<file> - log SQLite errors and messages\n"
		".eqp on|off|full - show EXPLAIN QUERY PLAN for SQL\n"
		".explain on|off|auto - format EXPLAIN results\n"
		".expert [--verbose] [--sample 0..100] - analyze the next SQL instead of executing it\n"
		".expert <SQL> - analyze SQL directly using the Python index advisor\n"
		".width <n1> <n2> ... - column/table widths; 0 = auto, omitted columns = auto\n"
		"Widths wrap long values and may shrink to fit the terminal.\n"
		".mode <mode> - set output format (default: list)\n"
		"Modes: list, column, table, box, csv, tabs, json, jsonpretty, line, markdown, quote\n"
		"jsonpretty formats JSON with two-space indentation.\n"
		"JSON: arrays with headers off, objects with headers on; duplicate names get suffixes.\n"
		"JSON represents non-finite numbers as null. Text formats display NULL using .nullvalue (default: empty).\n"
		"BLOB values use hexadecimal text; quote mode uses SQL literals.\n"
		".clear - clear SQL buffer\n"
		".version - show PaleSQL, Python and SQLite versions\n"
		".exit or .quit - exit PaleSQL\n"
		"Use database.table to select an object in an attached database.\n"
	)


def pale_version():
    print(f"PaleSQL version {__version__}")
    print(f"Python version {sys.version.split()[0]}")
    print(f"SQLite library version {sqlite3.sqlite_version}")
    print()


def pale_status(connection, cursor, output_mode, show_headers):
    main = next((row for row in get_databases(cursor) if row[1] == "main"), None)
    database = main[2] if main and main[2] else ":memory:"
    foreign_keys = connection.execute("PRAGMA foreign_keys;").fetchone()[0]
    journal_mode = connection.execute("PRAGMA journal_mode;").fetchone()[0]
    print(f"database: {database}")
    print(f"SQLite version: {sqlite3.sqlite_version}")
    print(f"foreign_keys: {'on' if foreign_keys else 'off'}")
    print(f"journal_mode: {journal_mode}")
    print(f"output mode: {output_mode}")
    print(f"headers: {'on' if show_headers else 'off'}")
    print(f"transaction state: {'active' if connection.in_transaction else 'inactive'}")


def handle_core_state(command, argument, state):
    if command == ".help":
        pale_help()
        return True
    if command == ".version":
        pale_version()
        return True
    if command in (".exit", ".quit", "exit", "quit"):
        state.exit_requested = True
        return True
    if command == ".clear":
        state.clear_sql()
        print("Buffer is cleaned")
        return True
    if command == ".testcase":
        if not argument:
            print("Usage: .testcase NAME", file=sys.stderr if state.output_file else sys.stdout)
            return True
        from io import StringIO

        state.testcase_name = argument
        state.testcase_output = StringIO()
        return True
    if command == ".check":
        if not argument:
            print("Usage: .check GLOB", file=sys.stderr if state.output_file else sys.stdout)
            return True
        if state.testcase_name is None:
            print("Check error: no testcase is active", file=sys.stderr)
            state.failed = True
            return True
        pattern = argument[1:-1] if len(argument) >= 2 and argument[0] in ("'", '"') and argument[-1] == argument[0] else argument
        if fnmatch.fnmatchcase(state.testcase_output.getvalue().rstrip("\r\n"), pattern):
            print("ok")
        else:
            print(f"Check failed for testcase {state.testcase_name}", file=sys.stderr)
            state.failed = True
        return True
    return False


def handle_core_connection(command, argument, state, diagnostics, interactive=False, open_database=pale_open):
    error_target = sys.stderr if state.output_file else sys.stdout
    if command == ".open":
        if not argument:
            print("Usage: .open <database>", file=error_target)
            return True
        try:
            name = parse_single_argument(argument, "Usage: .open <database>")
        except ValueError as error:
            print(error, file=error_target)
            return True
        if state.connection is not None:
            diagnostics.clear_trace(state.connection)
            state.connection.close()
            if state.active_connection is not None:
                state.connections.pop(state.active_connection)
                state.active_connection = None
        state.expert_options = None
        try:
            state.connection, state.cursor = open_database(name)
            state.connections.append((state.connection, state.cursor))
            state.active_connection = len(state.connections) - 1
        except sqlite3.Error as error:
            print("Database error:", error, file=error_target)
            state.connection = None
            state.cursor = None
            return True
        state.clear_sql()
        if interactive:
            print("Database opened", file=state.console)
        return True
    if command == ".status":
        if argument:
            print("Usage: .status", file=error_target)
        elif state.connection is None:
            print("database: none")
            print(f"SQLite version: {sqlite3.sqlite_version}")
            print("foreign_keys: unavailable")
            print("journal_mode: unavailable")
            print(f"output mode: {state.mode}")
            print(f"headers: {'on' if state.headers else 'off'}")
            print("transaction state: inactive")
        else:
            pale_status(state.connection, state.cursor, state.mode, state.headers)
        return True
    if command != ".close" or state.connection is None:
        return False
    diagnostics.clear_trace(state.connection)
    state.connection.close()
    if state.active_connection is not None:
        state.connections.pop(state.active_connection)
    state.expert_options = None
    if state.connections:
        state.active_connection = min(state.active_connection, len(state.connections) - 1)
        state.connection, state.cursor = state.connections[state.active_connection]
    else:
        state.connection, state.cursor, state.active_connection = None, None, None
    state.clear_sql()
    if interactive:
        print("Database closed", file=state.console)
    return True
