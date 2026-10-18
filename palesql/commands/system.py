import html
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

from ..database import quote_name
from ..output import sql_literal, write_result


SYSTEM_COMMANDS = frozenset({
    ".cd", ".pwd", ".history", ".clearhistory", ".shell", ".system", ".safe", ".nonce", ".www",
})


def handle_system_state(command, argument, state):
    error_target = sys.stderr if state.output_file else sys.stdout
    if command == ".safe":
        if argument.lower() not in ("on", "off"):
            print("Usage: .safe on|off", file=error_target)
            return True
        state.safe_mode = argument.lower() == "on"
        print(f"Safe mode {'on' if state.safe_mode else 'off'}")
        return True
    if command == ".nonce":
        if not argument:
            print("Usage: .nonce <string>", file=error_target)
            return True
        state.safe_nonce = argument
        print("Nonce armed for the next safe-mode command")
        return True
    if command == ".www":
        if state.safe_mode:
            print("Safe mode: .www is disabled", file=sys.stderr)
            state.failed = True
            return True
        if argument:
            print("Usage: .www", file=error_target)
            return True
        state.www_pending = True
        print("The next SQL result will open in a browser")
        return True
    if command == ".history":
        if argument:
            print("Usage: .history", file=error_target)
            return True
        if state.history:
            for number, entry in enumerate(state.history, 1):
                print(f"{number:4d}  {entry}")
        else:
            print("No history")
        return True
    if command == ".clearhistory":
        if argument:
            print("Usage: .clearhistory", file=error_target)
            return True
        state.history.clear()
        print("History cleared")
        return True
    if command == ".cd":
        try:
            values = shlex.split(argument) if argument[:1] in ("'", '"') else ([argument] if argument else [])
            if len(values) != 1:
                raise ValueError("Usage: .cd <directory>")
            path = Path(values[0]).expanduser().resolve()
            if not path.is_dir():
                raise ValueError(f"directory not found: {path}")
            os.chdir(path)
            print(path)
        except (OSError, ValueError) as error:
            print(f"CD error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".pwd":
        if argument:
            print("Usage: .pwd", file=error_target)
            return True
        print(Path.cwd())
        return True
    return False


def pale_www(text):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".html", prefix="palesql-", delete=False) as page:
        page.write("<!doctype html><meta charset='utf-8'><title>PaleSQL result</title><pre>")
        page.write(html.escape(text))
        page.write("</pre>")
    path = Path(page.name).resolve()
    if not webbrowser.open(path.as_uri()):
        raise OSError("could not open the system browser")
    print(f"Opened result in browser: {path}")


def pale_shell(argument):
    if not argument.strip():
        raise ValueError("Usage: .shell <command>")
    result = subprocess.run(argument, shell=True, capture_output=True, text=True, errors="replace")
    if result.stdout:
        write_result(result.stdout)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode:
        print(f"Shell command exited with status {result.returncode}", file=sys.stderr)
        return False
    return True


def session_snapshot(connection, table_name):
    info = connection.execute(f"PRAGMA main.table_xinfo({quote_name(table_name)})").fetchall()
    columns = [row[1] for row in info if row[6] == 0]
    if not columns:
        raise ValueError(f"table has no visible columns: {table_name}")
    primary_key = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
    key_columns = primary_key or columns
    column_sql = ", ".join(quote_name(column) for column in columns)
    rows = connection.execute(f"SELECT {column_sql} FROM main.{quote_name(table_name)}").fetchall()
    return {"columns": columns, "keys": key_columns, "rows": rows}


def session_changes(connection, state):
    changes = []
    for table, before in state["tables"].items():
        after = session_snapshot(connection, table)
        if after["columns"] != before["columns"] or after["keys"] != before["keys"]:
            raise ValueError(f"table schema changed during session: {table}")
        indices = [before["columns"].index(column) for column in before["keys"]]
        before_rows = {tuple(row[index] for index in indices): row for row in before["rows"]}
        after_rows = {tuple(row[index] for index in indices): row for row in after["rows"]}
        column_sql = ", ".join(quote_name(column) for column in before["columns"])
        for key in sorted(before_rows.keys() - after_rows.keys(), key=repr):
            where = " AND ".join(
                f"{quote_name(column)} = {sql_literal(value)}" for column, value in zip(before["keys"], key)
            )
            changes.append(f"DELETE FROM {quote_name(table)} WHERE {where};")
        for key in sorted(after_rows.keys() - before_rows.keys(), key=repr):
            row = after_rows[key]
            values = ", ".join(sql_literal(value) for value in row)
            changes.append(f"INSERT INTO {quote_name(table)} ({column_sql}) VALUES ({values});")
        for key in sorted(before_rows.keys() & after_rows.keys(), key=repr):
            if before_rows[key] == after_rows[key]:
                continue
            where = " AND ".join(
                f"{quote_name(column)} = {sql_literal(value)}" for column, value in zip(before["keys"], key)
            )
            values = ", ".join(
                f"{quote_name(column)} = {sql_literal(value)}" for column, value in zip(before["columns"], after_rows[key])
            )
            changes.append(f"UPDATE {quote_name(table)} SET {values} WHERE {where};")
    return changes


def pale_session(connection, argument, state):
    values = shlex.split(argument, posix=False)
    values = [value[1:-1] if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0] else value for value in values]
    usage = "Usage: .session open NAME|attach TABLE|detach TABLE|list|isempty|changeset FILE|patchset FILE|enable|disable|close"
    if not values:
        print(usage)
        return state
    action = values[0].lower()
    if action == "open" and len(values) == 2:
        if state is not None:
            raise ValueError("a session is already open")
        state = {"name": values[1], "tables": {}, "enabled": True}
        print(f"Session '{values[1]}' opened")
        return state
    if state is None:
        raise ValueError("no session is open")
    if action == "list" and len(values) == 1:
        tables = ", ".join(sorted(state["tables"])) or "no tables"
        print(f"{state['name']} ({'enabled' if state['enabled'] else 'disabled'}): {tables}")
        return state
    if action == "attach" and len(values) == 2:
        if not state["enabled"]:
            raise ValueError("session is disabled")
        state["tables"][values[1]] = session_snapshot(connection, values[1])
        print(f"Session attached: {values[1]}")
        return state
    if action == "detach" and len(values) == 2:
        state["tables"].pop(values[1], None)
        print(f"Session detached: {values[1]}")
        return state
    if action in ("enable", "disable") and len(values) == 1:
        state["enabled"] = action == "enable"
        print(f"Session {'enabled' if state['enabled'] else 'disabled'}")
        return state
    if action == "isempty" and len(values) == 1:
        print("yes" if not session_changes(connection, state) else "no")
        return state
    if action in ("changeset", "patchset") and len(values) == 2:
        changes = session_changes(connection, state)
        Path(values[1]).expanduser().resolve().write_text("BEGIN;\n" + "\n".join(changes) + "\nCOMMIT;\n", encoding="utf-8")
        print(f"{action} written: {values[1]} ({len(changes)} changes)")
        return state
    if action == "close" and len(values) == 1:
        print(f"Session '{state['name']}' closed")
        return None
    raise ValueError(usage)


def handle_system_runtime(command, argument, state):
    if command in (".shell", ".system"):
        try:
            if not pale_shell(argument):
                state.failed = True
        except (OSError, ValueError) as error:
            print(f"Shell error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command != ".session" or state.connection is None:
        return False
    try:
        state.session_state = pale_session(state.connection, argument, state.session_state)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Session error: {error}", file=sys.stderr)
        state.failed = True
    return True
