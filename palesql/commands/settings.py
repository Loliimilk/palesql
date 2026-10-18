import re
import shlex
import sqlite3
import sys
from pathlib import Path

from ..output import OUTPUT_MODES, parse_separators, sql_literal
from ..sql import SQLITE_LIMITS, limit_name, parameter_name, parameter_value


SETTINGS_COMMANDS = frozenset({
    ".headers", ".mode", ".excel", ".nullvalue", ".separator", ".width", ".crlf",
    ".changes", ".bail", ".echo", ".parameter", ".timeout", ".busy_timeout", ".prompt",
    ".foreign_keys", ".journal_mode", ".dbconfig", ".limit",
})


SQLITE_DBCONFIGS = {
    name.removeprefix("SQLITE_DBCONFIG_").lower(): value
    for name in dir(sqlite3)
    if name.startswith("SQLITE_DBCONFIG_")
    for value in (getattr(sqlite3, name),)
}

SQLITE_FILECTRL_OPERATIONS = (
    "lockstate", "last_errno", "size_limit", "chunk_size", "file_pointer",
    "sync_omitted", "win32_av_retry", "persist_wal", "pdb",
    "begin_atomic_write", "commit_atomic_write", "rollback_atomic_write",
    "has_mmap", "size_hint", "vfsname", "powersafe_override",
    "reserve_bytes", "tempfilename",
)


def _arguments(argument):
    lexer = shlex.shlex(argument, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def handle_local_setting(command, argument, state):
    error_target = sys.stderr if state.output_file else sys.stdout
    if command == ".prompt":
        try:
            prompts = _arguments(argument)
            if len(prompts) != 2:
                raise ValueError
        except ValueError:
            print('Usage: .prompt <main> <continue> (example: .prompt "sql> " "...> ")', file=error_target)
            return True
        state.main_prompt, state.continue_prompt = prompts
        return True
    if command == ".crlf":
        if argument.lower() not in ("on", "off"):
            print("Usage: .crlf on|off", file=error_target)
            return True
        state.crlf = argument.lower() == "on"
        if state.output_file:
            state.output_file.reconfigure(newline="\r\n" if state.crlf else "\n")
        if state.once_file:
            state.once_file.reconfigure(newline="\r\n" if state.crlf else "\n")
        return True
    if command == ".width":
        try:
            widths = tuple(int(value) for value in argument.split())
            if not widths or any(width < 0 for width in widths):
                raise ValueError
        except ValueError:
            print("Usage: .width <n1> <n2> ... (non-negative integers; 0 = auto)", file=error_target)
            return True
        state.column_widths = widths
        return True
    if command == ".separator":
        try:
            separators = parse_separators(argument)
        except ValueError as error:
            print(error, file=error_target)
            return True
        state.column_separator = separators[0]
        if len(separators) == 2:
            state.row_separator = separators[1]
        return True
    if command == ".nullvalue":
        if not argument:
            print('Usage: .nullvalue <text> (use "" for empty text)', file=error_target)
            return True
        if len(argument) >= 2 and argument[0] in ("\"", "'") and argument[-1] == argument[0]:
            argument = argument[1:-1]
        state.nullvalue = argument
        return True
    if command == ".mode":
        if argument.lower() not in OUTPUT_MODES:
            print("Usage: .mode " + "|".join(OUTPUT_MODES), file=error_target)
            return True
        state.mode = argument.lower()
        state.column_separator = None
        state.row_separator = None
        return True
    if command == ".excel":
        if argument:
            print("Usage: .excel", file=error_target)
            return True
        state.mode = "csv"
        state.headers = True
        state.column_separator = None
        state.row_separator = None
        return True
    if command == ".headers":
        if argument.lower() not in ("on", "off"):
            print("Usage: .headers on|off", file=error_target)
            return True
        state.headers = argument.lower() == "on"
        return True
    if command == ".changes":
        if argument.lower() not in ("on", "off"):
            print("Usage: .changes on|off", file=error_target)
            return True
        state.changes = argument.lower() == "on"
        return True
    if command == ".bail":
        if argument.lower() not in ("on", "off"):
            print("Usage: .bail on|off", file=error_target)
            return True
        state.bail = argument.lower() == "on"
        return True
    if command == ".echo":
        if argument.lower() not in ("on", "off"):
            print("Usage: .echo on|off", file=error_target)
            return True
        state.echo = argument.lower() == "on"
        return True
    if command != ".parameter":
        return False
    try:
        values = _arguments(argument)
        if not values:
            raise ValueError("Usage: .parameter init|set|unset|list|clear")
        action = values[0].lower()
        if action == "init" and len(values) == 1:
            return True
        if action == "clear" and len(values) == 1:
            state.parameters.clear()
            return True
        if action == "list" and len(values) == 1:
            for name, value in state.parameters.items():
                print(f"{name} = {sql_literal(value)}")
            return True
        if action == "set" and len(values) == 3:
            state.parameters[parameter_name(values[1])] = parameter_value(values[2])
            return True
        if action == "unset" and len(values) == 2:
            state.parameters.pop(parameter_name(values[1]), None)
            return True
        raise ValueError("Usage: .parameter init|set|unset|list|clear")
    except ValueError as error:
        print(error, file=error_target)
        return True


def handle_connection_setting(command, argument, state):
    commands = {
        ".timeout", ".busy_timeout", ".limit", ".dbconfig", ".foreign_keys", ".journal_mode",
        ".encoding", ".load", ".filectrl", ".unmodule", ".vfsinfo", ".vfsname",
    }
    if command not in commands:
        return False
    error_target = sys.stderr if state.output_file else sys.stdout
    if state.connection is None:
        print("No database opened", file=error_target)
        state.failed = True
        return True
    if command in (".timeout", ".busy_timeout"):
        try:
            milliseconds = int(argument)
            if milliseconds < 0 or re.fullmatch(r"[0-9]+", argument) is None:
                raise ValueError
            state.connection.execute(f"PRAGMA busy_timeout = {milliseconds}")
        except ValueError:
            print(f"Usage: {command} <milliseconds> (non-negative integer)", file=error_target)
        except sqlite3.Error as error:
            print(f"Timeout error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".limit":
        try:
            values = _arguments(argument)
            if len(values) > 2:
                raise ValueError("Usage: .limit [name] [value]")
            if not values:
                for name, category in SQLITE_LIMITS.items():
                    print(f"{name} {state.connection.getlimit(category)}")
                return True
            name = limit_name(values[0])
            category = SQLITE_LIMITS[name]
            if len(values) == 1:
                print(f"{name} {state.connection.getlimit(category)}")
                return True
            value = int(values[1])
            if value < 0 or re.fullmatch(r"[0-9]+", values[1]) is None:
                raise ValueError("limit value must be a non-negative integer")
            state.connection.setlimit(category, value)
            print(f"{name} {state.connection.getlimit(category)}")
        except (ValueError, sqlite3.Error, AttributeError) as error:
            print(f"Limit error: {error}", file=sys.stderr)
            state.failed = True
        return True
    handlers = {
        ".dbconfig": (pale_dbconfig, state.connection, "DB config"),
        ".foreign_keys": (pale_foreign_keys, state.cursor, "Foreign keys"),
        ".journal_mode": (pale_journal_mode, state.cursor, "Journal mode"),
        ".encoding": (pale_encoding, state.cursor, "Encoding"),
        ".filectrl": (pale_filectrl, state.connection, "File control"),
        ".unmodule": (pale_unmodule, state.connection, "Unmodule"),
        ".vfsinfo": (pale_vfsinfo, state.connection, "VFS info"),
        ".vfsname": (pale_vfsname, state.connection, "VFS name"),
    }
    try:
        if command == ".load":
            base_path = state.scripts[-1][0].parent if state.scripts else None
            pale_load_extension(state.connection, argument, base_path)
        else:
            handler, target, label = handlers[command]
            handler(target, argument)
    except (OSError, ValueError, sqlite3.Error) as error:
        label = "Load" if command == ".load" else handlers[command][2]
        print(f"{label} error: {error}", file=sys.stderr)
        state.failed = True
    return True


def pale_dbconfig(connection, argument=""):
    if not hasattr(connection, "getconfig") or not hasattr(connection, "setconfig"):
        raise ValueError("this Python sqlite3 does not expose sqlite3_db_config")
    values = _arguments(argument)
    if len(values) > 2:
        raise ValueError("Usage: .dbconfig [name] [on|off]")
    if not values:
        for name, option in SQLITE_DBCONFIGS.items():
            try:
                value = connection.getconfig(option)
            except (AttributeError, sqlite3.Error) as error:
                raise ValueError(f"{name}: {error}") from error
            print(f"{name:>28}: {int(bool(value))}")
        return
    name = values[0].lower()
    if name not in SQLITE_DBCONFIGS:
        raise ValueError(f"unknown dbconfig option: {name}")
    option = SQLITE_DBCONFIGS[name]
    if len(values) == 1:
        print(f"{name:>28}: {int(bool(connection.getconfig(option)))}")
        return
    setting = values[1].lower()
    if setting in ("on", "true", "yes", "1"):
        setting_value = 1
    elif setting in ("off", "false", "no", "0"):
        setting_value = 0
    else:
        raise ValueError("Usage: .dbconfig [name] [on|off]")
    connection.setconfig(option, setting_value)
    print(f"{name:>28}: {int(bool(connection.getconfig(option)))}")


def pale_foreign_keys(cursor, argument=""):
    argument = argument.strip().lower()
    if argument not in ("", "on", "off"):
        raise ValueError("Usage: .foreign_keys on|off")
    if argument:
        cursor.execute(f"PRAGMA foreign_keys = {argument}")
    cursor.execute("PRAGMA foreign_keys")
    print(f"foreign_keys: {'on' if cursor.fetchone()[0] else 'off'}")


def pale_journal_mode(cursor, argument=""):
    argument = argument.strip().lower()
    allowed = {"delete", "truncate", "persist", "memory", "wal", "off"}
    if argument and argument not in allowed:
        raise ValueError("Usage: .journal_mode [delete|truncate|persist|memory|wal|off]")
    cursor.execute(f"PRAGMA journal_mode = {argument}" if argument else "PRAGMA journal_mode")
    row = cursor.fetchone()
    if not row or row[0] is None:
        raise sqlite3.DatabaseError("SQLite did not return a journal mode")
    print(f"journal_mode: {str(row[0]).lower()}")


def pale_encoding(cursor, argument=""):
    encodings = {
        "utf-8": "UTF-8", "utf8": "UTF-8", "utf-16le": "UTF-16le",
        "utf16le": "UTF-16le", "utf-16be": "UTF-16be", "utf16be": "UTF-16be",
    }
    argument = argument.strip().lower()
    if argument and argument not in encodings:
        raise ValueError("Usage: .encoding [UTF-8|UTF-16le|UTF-16be]")
    if argument:
        cursor.execute(f"PRAGMA encoding = '{encodings[argument]}'")
    cursor.execute("PRAGMA encoding")
    row = cursor.fetchone()
    if not row or row[0] is None:
        raise sqlite3.DatabaseError("SQLite did not return an encoding")
    print(f"encoding: {row[0]}")


def pale_load_extension(connection, argument, base_path=None):
    values = _arguments(argument)
    if len(values) not in (1, 2) or not values[0]:
        raise ValueError("Usage: .load <file> [entrypoint]")
    path = Path(values[0]).expanduser()
    if not path.is_absolute() and base_path is not None:
        path = base_path / path
    path = path.resolve()
    connection.enable_load_extension(True)
    try:
        connection.load_extension(str(path), entrypoint=values[1]) if len(values) == 2 else connection.load_extension(str(path))
    finally:
        connection.enable_load_extension(False)
    print(f"Loaded extension: {path}")


def pale_filectrl(connection, argument=""):
    values = _arguments(argument)
    if not values:
        raise ValueError("Usage: .filectrl <operation> [arguments]")
    operation = values[0].lower()
    if operation not in SQLITE_FILECTRL_OPERATIONS:
        raise ValueError(f"unknown filectrl operation: {operation}")
    if not hasattr(connection, "file_control"):
        raise ValueError("sqlite3_file_control is not exposed by Python's standard sqlite3 module")
    raise ValueError("PaleSQL cannot call sqlite3_file_control on this connection")


def pale_unmodule(connection, argument=""):
    values = _arguments(argument)
    if len(values) != 1 or not values[0]:
        raise ValueError("Usage: .unmodule <name>")
    if not hasattr(connection, "drop_module"):
        raise ValueError("Python's standard sqlite3 module does not expose virtual-table module removal")
    raise ValueError("PaleSQL cannot remove virtual-table modules on this connection")


def pale_vfsinfo(connection, argument=""):
    if argument.strip():
        raise ValueError("Usage: .vfsinfo")
    if not hasattr(connection, "vfsinfo"):
        raise ValueError("Python's standard sqlite3 module does not expose SQLite VFS information")
    raise ValueError("PaleSQL cannot read VFS information on this connection")


def pale_vfsname(connection, argument=""):
    if argument.strip():
        raise ValueError("Usage: .vfsname")
    if not hasattr(connection, "vfsname"):
        raise ValueError("Python's standard sqlite3 module does not expose the active VFS name")
    raise ValueError("PaleSQL cannot read the active VFS name on this connection")
