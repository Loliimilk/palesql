import sys
import argparse
import sqlite3
import os
import re
import io
import json
import time
import fnmatch
from pathlib import Path
from contextlib import redirect_stdout

from . import __version__
from .state import CLIState

def parse_cli_arguments(argv):
	parser = argparse.ArgumentParser(
		prog="palesql",
		description="A tiny, lightweight SQLite CLI client written in Python.",
		add_help=False,
	)
	parser.add_argument("--help", "-h", action="store_true", dest="show_help")
	parser.add_argument("--version", action="store_true", dest="show_version")
	parser.add_argument("--readonly", action="store_true", help="open the database read-only")
	parser.add_argument("--bail", action="store_true", help="stop after the first SQL or script error")
	parser.add_argument("--echo", action="store_true", help="echo commands read from startup scripts")
	parser.add_argument("--init", metavar="FILE", help="run a SQL/dot-command file before input")
	parser.add_argument("--batch", action="store_true", help="never enter interactive mode")
	parser.add_argument("--interactive", action="store_true", help="enter interactive mode after startup commands")
	parser.add_argument("--cmd", action="append", default=[], metavar="COMMAND", help="execute a SQL or dot-command")
	parser.add_argument("--nullvalue", metavar="TEXT", help="text used for SQL NULL")
	parser.add_argument("--separator", metavar="SEP", help="column separator (supports \\t, \\n, \\r)")
	mode_group = parser.add_mutually_exclusive_group()
	mode_group.add_argument("--csv", action="store_const", const="csv", dest="mode")
	mode_group.add_argument("--json", action="store_const", const="json", dest="mode")
	mode_group.add_argument("--table", action="store_const", const="table", dest="mode")
	mode_group.add_argument("--box", action="store_const", const="box", dest="mode")
	header_group = parser.add_mutually_exclusive_group()
	header_group.add_argument("--header", action="store_true", dest="headers")
	header_group.add_argument("--no-header", action="store_false", dest="headers")
	parser.set_defaults(headers=None, mode=None)
	parser.add_argument("database", nargs="?", help="database file or SQLite URI")
	parser.add_argument("sql", nargs="?", help="SQL to execute, then exit")
	return parser, parser.parse_args(argv)


def print_cli_help(parser):
	print(parser.format_help(), end="")
	pale_help()


def cli_input(prompt, history):
	if os.name != "nt" or not sys.stdin.isatty():
		return input(prompt)
	try:
		import msvcrt
	except ImportError:
		return input(prompt)
	sys.stdout.write(prompt)
	sys.stdout.flush()
	buffer = []
	history_index = len(history)
	draft = ""
	while True:
		key = msvcrt.getwch()
		if key in ("\r", "\n"):
			sys.stdout.write("\n")
			return "".join(buffer)
		if key == "\003":
			raise KeyboardInterrupt
		if key == "\004":
			if not buffer:
				raise EOFError
			continue
		if key in ("\b", "\x7f"):
			if buffer:
				buffer.pop()
				sys.stdout.write("\b \b")
				sys.stdout.flush()
			continue
		if key in ("\x00", "\xe0"):
			code = ord(msvcrt.getwch())
			if code in (72, 80):
				if code == 72:
					if history_index == len(history):
						draft = "".join(buffer)
					history_index = max(0, history_index - 1)
				else:
					history_index = min(len(history), history_index + 1)
				value = draft if history_index == len(history) else history[history_index]
				for _ in buffer:
					sys.stdout.write("\b \b")
				buffer = list(value)
				sys.stdout.write(value)
				sys.stdout.flush()
			continue
		buffer.append(key)
		sys.stdout.write(key)
		sys.stdout.flush()


from .output import (
    parse_separators,
    write_result,
)









from .database import (
    get_databases,
    pale_open,
    pale_open_readonly,
)
from .sql import (
    analyze_expert,
    execute_sql,
    parse_expert_arguments,
    sql_context,
)
from .commands.schema import (
    handle_schema_command,
    pale_columns,
    pale_count,
    pale_dbinfo,
    pale_describe,
    pale_find,
    pale_foreignkeys,
    pale_fullschema,
    pale_imposter,
    pale_incremental_integrity_check,
    pale_indexes,
    pale_integrity_check,
    pale_lint,
    pale_schema,
    pale_schemajson,
    pale_selftest,
    pale_tables,
    pale_tablesize,
    pale_triggers,
    pale_views,
)
from .commands.settings import (
    handle_connection_setting,
    handle_local_setting,
    pale_dbconfig,
    pale_encoding,
    pale_filectrl,
    pale_foreign_keys,
    pale_journal_mode,
    pale_load_extension,
    pale_unmodule,
    pale_vfsinfo,
    pale_vfsname,
)
from .commands.files import (
    handle_file_command,
)
from .commands.system import (
    handle_system_state,
    handle_system_runtime,
    pale_www,
)
from .commands.core import (
    handle_core_state,
    handle_core_connection,
    pale_help,
    pale_status,
    pale_version,
)
from .commands.diagnostics import Diagnostics, handle_diagnostic_setting, handle_runtime_diagnostic
from .commands.connections import (
    close_all,
    handle_connection_command,
)
from .commands.dispatcher import CommandDispatcher


def main(argv=None):
	state = CLIState(console=sys.stdout, testcase_output=io.StringIO())
	if argv is None:
		argv = []
	parser, cli = parse_cli_arguments(argv)
	if cli.show_help:
		print_cli_help(parser)
		return 0
	if cli.show_version:
		pale_version()
		return 0
	if cli.readonly and cli.database is None:
		print("Argument error: --readonly requires a database", file=sys.stderr)
		return 2
	interactive = sys.stdin.isatty() and sys.stdout.isatty()
	if cli.interactive:
		interactive = True
	if cli.batch:
		interactive = False
	if interactive:
		print(
			f"\nhaiiiiiiiiii :3 rawr certified SQL CLI :p\n"
			f"PaleSQL {__version__} by Shikaru Software\n"
			f"Type .help for commands.\n"
		)

	diagnostics = Diagnostics(state.console)
	dispatcher = CommandDispatcher(handle_core_state, handle_local_setting, handle_system_state, handle_diagnostic_setting)
	if cli.headers is not None:
		state.headers = cli.headers
	if cli.mode is not None:
		state.mode = cli.mode
	if cli.nullvalue is not None:
		state.nullvalue = cli.nullvalue
	if cli.bail:
		state.bail = True
	if cli.echo:
		state.echo = True
	if cli.separator is not None:
		try:
			separators = parse_separators(cli.separator)
			state.column_separator = separators[0]
			state.row_separator = separators[1] if len(separators) == 2 else None
		except ValueError as error:
			print(f"Argument error: {error}", file=sys.stderr)
			return 2
	startup_queue = []
	startup_exit = bool(cli.sql is not None or cli.cmd) and not cli.interactive
	startup_commands = list(cli.cmd)
	if cli.init:
		startup_commands.insert(0, ".read " + json.dumps(str(Path(cli.init).expanduser().resolve())))
	for startup_command in startup_commands:
		if startup_command.lstrip().startswith(".") or sqlite3.complete_statement(startup_command):
			startup_queue.append(startup_command)
		else:
			startup_queue.append(startup_command.rstrip() + ";")
	if cli.sql is not None:
		startup_queue.append(cli.sql if sqlite3.complete_statement(cli.sql) else cli.sql.rstrip() + ";")

	def protected_backup_paths(include_sidecars=True):
		paths = [active[0] for active in state.scripts]
		for seq, name, file in get_databases(state.cursor):
			if file:
				suffixes = ("", "-wal", "-shm", "-journal") if include_sidecars else ("",)
				paths.extend(Path(file + suffix).resolve() for suffix in suffixes)
		for handle in (state.output_file, state.once_file):
			if handle:
				paths.append(Path(handle.name).resolve())
		return paths

	def finish_once():
		if state.once_file:
			state.once_file.close()
			state.once_file = None
			if state.output_file:
				state.output_file.close()
				state.output_file = None
			sys.stdout = state.console

	def run_expert(statement, options):
		try:
			report = analyze_expert(state.connection, statement, sample=options[0], verbose=options[1])
			with redirect_stdout(state.once_file if state.once_file is not None else sys.stdout):
				print(report, end="" if report.endswith("\n") else "\n")
		except (OSError, ValueError, sqlite3.Error) as error:
			context = f"{state.source_context[0]}:{state.source_context[1]}: " if state.source_context else ""
			print(f"Expert error: {context}{error}", file=sys.stderr)
			state.failed = True
			if state.bail and state.scripts:
				state.scripts.clear()
		finally:
			finish_once()

	def run_sql(buffer):
		if state.expert_options is not None:
			if not sqlite3.complete_statement(buffer):
				return buffer if sql_context(buffer)[1] or sql_context(buffer)[0] == "/*" else ""
			options, state.expert_options = state.expert_options, None
			run_expert(buffer, options)
			return ""
		start = 0
		for index, char in enumerate(buffer):
			if char == ";" and sqlite3.complete_statement(buffer[start:index + 1]):
				statement = buffer[start:index + 1]
				capture_output = io.StringIO() if (state.www_pending or state.testcase_name is not None) else None
				with redirect_stdout(capture_output if capture_output is not None else (state.once_file if state.once_file is not None else sys.stdout)):
					success = execute_sql(state.cursor, statement, state.headers, state.mode, state.nullvalue, state.column_separator, state.row_separator, state.column_widths, state.crlf, state.changes, state.source_context, state.parameters, state.timer, state.stats, state.eqp, state.explain, state.scanstats, diagnostics.log_callback)
				if capture_output is not None:
					captured = capture_output.getvalue()
					if state.testcase_name is not None:
						state.testcase_output.write(captured)
					if state.testcase_name is not None and not state.www_pending:
						write_result(captured)
				if state.www_pending:
					state.www_pending = False
					if success:
						try:
							pale_www(captured)
						except OSError as error:
							print(f"WWW error: {error}", file=sys.stderr)
							state.failed = True
				if not success:
					state.failed = True
					if state.bail and state.scripts:
						state.scripts.clear()
						return ""
				if state.once_file and (not success or state.cursor.description is not None):
					finish_once()
				start = index + 1
		remaining = buffer[start:]
		context_state, has_sql = sql_context(remaining)
		if has_sql or context_state == "/*":
			return remaining
		if remaining.strip() and remaining.lstrip().startswith(("--", "/*")):
			try:
				state.cursor.execute(remaining)
			except sqlite3.Error as error:
				print(f"SQL error: {error}", file=sys.stderr)
				state.failed = True
		return ""

	if cli.database:
		try:
			state.connection, state.cursor = pale_open_readonly(cli.database) if cli.readonly else pale_open(cli.database)
			state.connections.append((state.connection, state.cursor))
			state.active_connection = 0
		except (OSError, ValueError, sqlite3.Error) as error:
			print(f"Database error: {error}", file=sys.stderr)
			state.failed = True

	try:
		while True:
			if state.bail and state.failed:
				if state.scripts:
					state.scripts.clear()
				if startup_queue:
					startup_queue.clear()
				if startup_exit and not state.scripts:
					break
				state.sql_buffer = ""
			try:
				if state.scripts:
					try:
						state.source_context = (state.scripts[-1][0], 0)
						line_number, inp = next(state.scripts[-1][1])
						state.scripts[-1] = (state.scripts[-1][0], state.scripts[-1][1], line_number)
						state.source_context = (state.scripts[-1][0], line_number)
						inp = inp.rstrip("\r\n")
						if state.echo:
							print(inp)
					except StopIteration:
						path, lines, last_line = state.scripts.pop()
						if sql_context(state.sql_buffer)[1] or sql_context(state.sql_buffer)[0] == "/*":
							print(f"SQL error: {path}:{last_line}: incomplete statement", file=sys.stderr)
							state.failed = True
							state.expert_options = None
							state.sql_buffer = ""
						continue
				else:
					state.source_context = None
					if startup_queue:
						inp = startup_queue.pop(0)
						if state.echo:
							print(inp)
					elif startup_exit:
						break
					else:
						with redirect_stdout(state.console):
							prompt = (state.continue_prompt if state.sql_buffer else state.main_prompt) if interactive else ""
							inp = cli_input(prompt, state.history) if interactive else input(prompt)
			except EOFError:
				if sql_context(state.sql_buffer)[1] or sql_context(state.sql_buffer)[0] == "/*":
					print("SQL error: incomplete statement at end of input", file=sys.stderr)
					return 1
				break
			except KeyboardInterrupt:
				if state.connection is not None:
					try:
						state.connection.interrupt()
					except sqlite3.Error:
						pass
				print("^C", file=state.console)
				state.sql_buffer = ""
				state.expert_options = None
				continue
			if inp.strip():
				state.history.append(inp.rstrip("\r\n"))
				if len(state.history) > 1000:
					del state.history[:-1000]
			if sql_context(state.sql_buffer)[0] not in ("", "--"):
				state.sql_buffer += inp + "\n"
				try:
					state.sql_buffer = run_sql(state.sql_buffer)
				except KeyboardInterrupt:
					if state.connection is not None:
						state.connection.interrupt()
					print("^C", file=state.console)
					state.sql_buffer = ""
					state.expert_options = None
				continue
			raw_input = inp

			message = re.fullmatch(r"\.print(?:[ \t](.*))?", inp.lstrip(), re.IGNORECASE)
			if message:
				newline = "\r\n" if state.crlf and not sys.stdout.isatty() else "\n"
				write_result((message[1] or "") + newline)
				continue
			inp = inp.strip()

			if not inp:
				continue

			parts = inp.split(None, 1)
			command = parts[0]
			argument = parts[1] if len(parts) == 2 else ""
			command = command.lower()
			argument = argument.strip()

			if dispatcher.dispatch(command, argument, state):
				if state.exit_requested:
					break
				continue

			if state.exit_requested:
				break

			if handle_core_connection(
				command,
				argument,
				state,
				diagnostics,
				interactive,
				open_database=pale_open,
			):
				continue

			if state.safe_mode and command in (".shell", ".system", ".load", ".output", ".once", ".archive", ".backup", ".restore", ".save", ".clone", ".import", ".filectrl", ".unmodule", ".imposter", ".session", ".www", ".vacuum"):
				if state.safe_nonce is None:
					print(f"Safe mode: {command} is disabled", file=sys.stderr)
					state.failed = True
					continue
				state.safe_nonce = None

			if handle_connection_command(
				command,
				argument,
				state,
				diagnostics,
				interactive,
				open_database=pale_open,
				open_readonly=pale_open_readonly,
			):
				continue

			if handle_system_runtime(command, argument, state):
				continue

			if handle_connection_setting(command, argument, state):
				continue

			if handle_file_command(command, argument, state, protected_backup_paths, finish_once):
				continue

			if handle_runtime_diagnostic(command, argument, state, diagnostics, protected_backup_paths, run_expert):
				continue

			if state.connection is None:
				if state.scripts:
					print(
						f"Read error: no database opened; stopped {state.scripts[-1][0]}. "
						"Use .open <database> before .read.",
						file=sys.stderr,
					)
					state.failed = True
					state.scripts.clear()
					state.sql_buffer = ""
				else:
					print("No database opened", file=sys.stderr if state.output_file else sys.stdout)
					if startup_exit:
						state.failed = True
				continue

			if command == ".vacuum":
				if argument:
					print("Usage: .vacuum", file=sys.stderr if state.output_file else sys.stdout)
					continue
				try:
					state.connection.execute("VACUUM")
					print("Vacuum complete")
				except sqlite3.Error as error:
					print(f"Vacuum error: {error}", file=sys.stderr)
					state.failed = True
				continue

			if command == ".watch":
				match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s+([\s\S]+)", argument)
				if not match:
					print("Usage: .watch <seconds> <SQL>", file=sys.stderr if state.output_file else sys.stdout)
					continue
				interval = float(match[1])
				if interval < 0:
					print("Watch interval must not be negative", file=sys.stderr)
					state.failed = True
					continue
				statement = match[2].strip()
				if not statement.endswith(";"):
					statement += ";"
				try:
					while True:
						success = execute_sql(state.cursor, statement, state.headers, state.mode, state.nullvalue, state.column_separator, state.row_separator, state.column_widths, state.crlf, state.changes, state.source_context, state.parameters, state.timer, state.stats, state.eqp, state.explain, state.scanstats, diagnostics.log_callback)
						if not success:
							state.failed = True
							break
						if not interactive:
							break
						time.sleep(interval)
				except KeyboardInterrupt:
					print("Watch stopped")
				continue

			if handle_schema_command(command, argument, state):
				continue

			if command.startswith("."):
				print(f"Unknown command: {command}", file=sys.stderr if state.output_file else sys.stdout)
				if startup_exit:
					state.failed = True
				continue

			state.sql_buffer += raw_input + "\n"
			try:
				state.sql_buffer = run_sql(state.sql_buffer)
			except KeyboardInterrupt:
				if state.connection is not None:
					state.connection.interrupt()
				print("^C", file=state.console)
				state.sql_buffer = ""
				state.expert_options = None

	finally:
		diagnostics.close(state.connection)
		sys.stdout = state.console
		if state.once_file:
			state.once_file.close()
		if state.output_file:
			state.output_file.close()
		close_all(state.connections)
	return 1 if state.failed else 0


def run():
	return main(sys.argv[1:])


if __name__ == "__main__":
	sys.exit(run())
