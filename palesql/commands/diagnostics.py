import sqlite3
import sys
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..sql import parse_expert_arguments, sql_context


DIAGNOSTIC_COMMANDS = frozenset({
    ".timer", ".stats", ".scanstats", ".eqp", ".explain", ".expert", ".integrity",
    ".intck", ".selftest", ".lint", ".trace", ".progress", ".log", ".auth",
})


def handle_diagnostic_setting(command, argument, state):
    error_target = sys.stderr if state.output_file else sys.stdout
    if command == ".timer":
        if argument.lower() not in ("on", "off"):
            print("Usage: .timer on|off", file=error_target)
            return True
        state.timer = argument.lower() == "on"
        return True
    if command == ".stats":
        if argument.lower() not in ("on", "off"):
            print("Usage: .stats on|off", file=error_target)
            return True
        state.stats = argument.lower() == "on"
        return True
    if command == ".scanstats":
        if argument.lower() not in ("on", "off", "est"):
            print("Usage: .scanstats on|off|est", file=error_target)
            return True
        state.scanstats = argument.lower()
        return True
    if command == ".eqp":
        if argument.lower() not in ("on", "off", "full"):
            print("Usage: .eqp on|off|full", file=error_target)
            return True
        state.eqp = argument.lower()
        return True
    if command == ".explain":
        if argument.lower() not in ("on", "off", "auto"):
            print("Usage: .explain on|off|auto", file=error_target)
            return True
        state.explain = argument.lower()
        return True
    return False


@dataclass
class Diagnostics:
    console: Any
    progress_calls: int = 0
    trace_mode: str = "off"
    trace_file: Any = None
    log_mode: str = "off"
    log_file: Any = None
    authorizer_enabled: bool = False

    def progress_handler(self):
        self.progress_calls += 1
        print(f"Progress {self.progress_calls}")
        return 0

    def trace_callback(self, statement):
        target = self.trace_file if self.trace_mode == "file" else sys.stderr if self.trace_mode == "stderr" else sys.stdout
        try:
            target.write(f"Trace: {statement}\n")
            target.flush()
        except (AttributeError, OSError, ValueError):
            pass

    def install_trace(self, connection):
        if not hasattr(connection, "set_trace_callback"):
            raise ValueError("this SQLite connection does not support tracing")
        connection.set_trace_callback(self.trace_callback)

    def clear_trace(self, connection):
        if connection is not None and hasattr(connection, "set_trace_callback"):
            connection.set_trace_callback(None)
        if self.trace_file:
            self.trace_file.close()
            self.trace_file = None
        self.trace_mode = "off"

    def authorizer_callback(self, action, arg1, arg2, database, source):
        if self.authorizer_enabled:
            print(
                f"Authorizer: action={action} arg1={arg1 or ''} arg2={arg2 or ''} "
                f"database={database or ''} source={source or ''}"
            )
        return sqlite3.SQLITE_OK

    def set_authorizer(self, connection, enabled):
        if connection is None or not hasattr(connection, "set_authorizer"):
            raise ValueError("this Python sqlite3 does not support authorizer callbacks")
        connection.set_authorizer(self.authorizer_callback if enabled else None)
        self.authorizer_enabled = enabled

    def log_callback(self, message):
        target = self.log_file if self.log_mode == "file" else self.console if self.log_mode == "stdout" else sys.stderr
        try:
            target.write(f"SQLite log: {message}\n")
            target.flush()
        except (AttributeError, OSError, ValueError):
            pass

    def clear_log(self):
        if self.log_file:
            self.log_file.close()
            self.log_file = None
        self.log_mode = "off"

    def close(self, connection):
        self.clear_trace(connection)
        self.clear_log()


def handle_runtime_diagnostic(command, argument, state, diagnostics, protected_backup_paths, run_expert):
    if command not in {".progress", ".trace", ".log", ".expert", ".auth"}:
        return False
    error_target = sys.stderr if state.output_file else sys.stdout
    if command == ".progress":
        if state.connection is None:
            print("No database opened", file=error_target)
            state.failed = True
            return True
        if re.fullmatch(r"[0-9]+", argument) is None or int(argument) > sys.maxsize:
            print("Usage: .progress <N> (non-negative integer; 0 disables)", file=error_target)
            return True
        diagnostics.progress_calls = 0
        state.connection.set_progress_handler(diagnostics.progress_handler, int(argument))
        return True
    if command == ".trace":
        if state.connection is None:
            print("No database opened", file=error_target)
            state.failed = True
            return True
        if not argument:
            print("Usage: .trace on|off|stdout|stderr|<file>", file=error_target)
            return True
        trace_argument = argument.lower()
        if trace_argument == "off":
            diagnostics.clear_trace(state.connection)
            return True
        if trace_argument in ("on", "stdout", "stderr"):
            try:
                diagnostics.clear_trace(state.connection)
                diagnostics.trace_mode = "stderr" if trace_argument == "stderr" else trace_argument
                diagnostics.install_trace(state.connection)
            except (OSError, ValueError, AttributeError) as error:
                diagnostics.trace_mode = "off"
                print(f"Trace error: {error}", file=sys.stderr)
                state.failed = True
            return True
        try:
            lexer = shlex.shlex(argument, posix=True)
            lexer.whitespace_split = True
            lexer.commenters = ""
            lexer.escape = ""
            paths = list(lexer)
            if len(paths) != 1 or not paths[0]:
                raise ValueError("Usage: .trace on|off|stdout|stderr|<file>")
            path = Path(paths[0]).expanduser().resolve()
            protected = protected_backup_paths()
            if diagnostics.trace_file:
                protected.append(Path(diagnostics.trace_file.name).resolve())
            if any(path == item or (path.exists() and item.exists() and path.samefile(item)) for item in protected):
                raise ValueError("Trace file must not overwrite an open database, script, or output file")
            trace_file = path.open("w", encoding="utf-8", newline="\n")
            diagnostics.clear_trace(state.connection)
            diagnostics.trace_file = trace_file
            diagnostics.trace_mode = "file"
            diagnostics.install_trace(state.connection)
        except (OSError, ValueError, AttributeError) as error:
            print(f"Trace error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".log":
        if not argument:
            print("Usage: .log off|stdout|stderr|<file>", file=error_target)
            return True
        log_argument = argument.lower()
        if log_argument == "off":
            diagnostics.clear_log()
            return True
        if log_argument in ("stdout", "stderr"):
            diagnostics.clear_log()
            diagnostics.log_mode = log_argument
            return True
        try:
            lexer = shlex.shlex(argument, posix=True)
            lexer.whitespace_split = True
            lexer.commenters = ""
            lexer.escape = ""
            paths = list(lexer)
            if len(paths) != 1 or not paths[0]:
                raise ValueError("Usage: .log off|stdout|stderr|<file>")
            path = Path(paths[0]).expanduser().resolve()
            protected = [active[0] for active in state.scripts]
            if state.connection is not None:
                protected.extend(protected_backup_paths())
            for handle in (state.output_file, state.once_file, diagnostics.trace_file, diagnostics.log_file):
                if handle:
                    protected.append(Path(handle.name).resolve())
            if any(path == item or (path.exists() and item.exists() and path.samefile(item)) for item in protected):
                raise ValueError("Log file must not overwrite an open database, script, or output file")
            log_file = path.open("w", encoding="utf-8", newline="\n")
            diagnostics.clear_log()
            diagnostics.log_file = log_file
            diagnostics.log_mode = "file"
        except (OSError, ValueError) as error:
            print(f"Log error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".expert":
        state.expert_options = None
        try:
            if state.connection is None:
                raise ValueError("No database opened")
            if sql_context(state.sql_buffer)[1]:
                raise ValueError("Finish the pending SQL or use .clear before .expert")
            sample, verbose, inline_sql = parse_expert_arguments(argument)
            if inline_sql is None:
                state.expert_options = (sample, verbose)
            else:
                run_expert(inline_sql, (sample, verbose))
        except (OSError, ValueError, sqlite3.Error) as error:
            context = f"{state.source_context[0]}:{state.source_context[1]}: " if state.source_context else ""
            print(f"Expert error: {context}{error}", file=sys.stderr)
            state.failed = True
        return True
    if argument.lower() not in ("on", "off"):
        print("Usage: .auth on|off", file=error_target)
        return True
    try:
        diagnostics.set_authorizer(state.connection, argument.lower() == "on")
        print(f"Authorizer {'on' if diagnostics.authorizer_enabled else 'off'}")
    except (ValueError, sqlite3.Error) as error:
        print(f"Authorizer error: {error}", file=sys.stderr)
        state.failed = True
    return True
