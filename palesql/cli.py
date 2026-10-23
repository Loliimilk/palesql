import sys
import argparse
import sqlite3
import re
import io
import json
import time
import fnmatch
from pathlib import Path
from contextlib import redirect_stdout

from . import __version__
from .state import PaleState


_SCRIPT_FINISHED = object()


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


def cli_input(prompt):
	return input(prompt)


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
    SCHEMA_COMMANDS,
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
from .commands.dispatcher import CommandDispatcher, CommandSpec, command_for


def _command_group(names, handler, *args, requires_connection=False, dangerous=False, files=False, **kwargs):
	return {
		name: CommandSpec(
			command_for(name, handler, *args, **kwargs),
			requires_connection=requires_connection,
			dangerous=dangerous,
			files=files,
		)
		for name in names
	}


def command_print(state, argument):
	newline = "\r\n" if state.crlf and not sys.stdout.isatty() else "\n"
	write_result(argument + newline)
	return True


def command_vacuum(state, argument):
	if state.connection is None:
		return False
	if argument:
		print("Usage: .vacuum", file=sys.stderr if state.output_file else sys.stdout)
		return True
	try:
		state.connection.execute("VACUUM")
		print("Vacuum complete")
	except sqlite3.Error as error:
		print(f"Vacuum error: {error}", file=sys.stderr)
		state.failed = True
	return True


def command_watch(interactive, log_callback):
	def handler(state, argument):
		if state.connection is None:
			return False
		match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s+([\s\S]+)", argument)
		if not match:
			print("Usage: .watch <seconds> <SQL>", file=sys.stderr if state.output_file else sys.stdout)
			return True
		interval = float(match[1])
		if interval < 0:
			print("Watch interval must not be negative", file=sys.stderr)
			state.failed = True
			return True
		statement = match[2].strip()
		if not statement.endswith(";"):
			statement += ";"
		try:
			while True:
				success = execute_sql(state.cursor, statement, state.headers, state.mode, state.nullvalue, state.column_separator, state.row_separator, state.column_widths, state.crlf, state.changes, state.source_context, state.parameters, state.timer, state.stats, state.eqp, state.explain, state.scanstats, log_callback)
				if not success:
					state.failed = True
					break
				if not interactive:
					break
				time.sleep(interval)
		except KeyboardInterrupt:
			print("Watch stopped")
		return True
	return handler


def build_command_dispatcher(diagnostics, interactive, protected_backup_paths, finish_once, run_expert):
	COMMANDS = {
		**_command_group((".help", ".version", ".exit", ".clear", ".testcase", ".check"), handle_core_state),
		**_command_group((".open", ".close", ".status"), handle_core_connection, diagnostics, interactive, open_database=pale_open),
		**_command_group((".prompt", ".crlf", ".width", ".separator", ".nullvalue", ".mode", ".excel", ".headers", ".changes", ".bail", ".echo", ".parameter"), handle_local_setting),
		**_command_group((".timeout", ".limit", ".dbconfig", ".foreign_keys", ".journal_mode", ".encoding", ".vfsinfo", ".vfsname"), handle_connection_setting, requires_connection=True),
		**_command_group((".load", ".filectrl", ".unmodule"), handle_connection_setting, requires_connection=True, dangerous=True, files=True),
		**_command_group((".safe", ".nonce", ".www", ".history", ".clearhistory", ".cd", ".pwd"), handle_system_state),
		**_command_group((".shell",), handle_system_runtime, dangerous=True),
		**_command_group((".session",), handle_system_runtime, requires_connection=True, dangerous=True),
		**_command_group((".timer", ".stats", ".scanstats", ".eqp", ".explain"), handle_diagnostic_setting),
		**_command_group((".progress", ".trace", ".expert", ".auth"), handle_runtime_diagnostic, diagnostics, protected_backup_paths, run_expert, requires_connection=True),
		**_command_group((".log",), handle_runtime_diagnostic, diagnostics, protected_backup_paths, run_expert, files=True),
		**_command_group((".connection", ".connections", ".memory", ".readonly"), handle_connection_command, diagnostics, interactive, open_database=pale_open, open_readonly=pale_open_readonly),
		**_command_group((".reopen",), handle_connection_command, diagnostics, interactive, open_database=pale_open, open_readonly=pale_open_readonly, requires_connection=True),
		**_command_group((".read",), handle_file_command, protected_backup_paths, finish_once, files=True),
		**_command_group((".output", ".once"), handle_file_command, protected_backup_paths, finish_once, dangerous=True, files=True),
		**_command_group((".dump", ".recover", ".dbtotxt", ".sha3sum"), handle_file_command, protected_backup_paths, finish_once, requires_connection=True, files=True),
		**_command_group((".import", ".backup", ".restore", ".save", ".clone", ".archive"), handle_file_command, protected_backup_paths, finish_once, requires_connection=True, dangerous=True, files=True),
		**_command_group(SCHEMA_COMMANDS - {".imposter"}, handle_schema_command, requires_connection=True),
		**_command_group((".imposter",), handle_schema_command, requires_connection=True, dangerous=True),
		".print": CommandSpec(command_print),
		".vacuum": CommandSpec(command_vacuum, requires_connection=True, dangerous=True),
		".watch": CommandSpec(command_watch(interactive, diagnostics.log_callback), requires_connection=True),
	}
	return CommandDispatcher(COMMANDS)


class PaleCLI:
	def __init__(self, argv=None):
		self.argv = [] if argv is None else argv
		self.state = PaleState(console=sys.stdout, testcase_output=io.StringIO())
		self.parser, self.options = parse_cli_arguments(self.argv)
		self.interactive = False
		self.diagnostics = None
		self.dispatcher = None
		self.startup_queue = []
		self.startup_exit = False

	def configure(self):
		options = self.options
		self.interactive = sys.stdin.isatty() and sys.stdout.isatty()
		if options.interactive:
			self.interactive = True
		if options.batch:
			self.interactive = False
		if self.interactive:
			print(
				f"\nhaiiiiiiiiii :3 rawr certified SQL CLI :p\n"
				f"PaleSQL {__version__} by Shikaru Software\n"
				f"Type .help for commands.\n"
			)
		state = self.state
		self.diagnostics = Diagnostics(state.console)
		if options.headers is not None:
			state.headers = options.headers
		if options.mode is not None:
			state.mode = options.mode
		if options.nullvalue is not None:
			state.nullvalue = options.nullvalue
		if options.bail:
			state.bail = True
		if options.echo:
			state.echo = True
		if options.separator is not None:
			separators = parse_separators(options.separator)
			state.column_separator = separators[0]
			state.row_separator = separators[1] if len(separators) == 2 else None
		self.startup_exit = bool(options.sql is not None or options.cmd) and not options.interactive
		startup_commands = list(options.cmd)
		if options.init:
			startup_commands.insert(0, ".read " + json.dumps(str(Path(options.init).expanduser().resolve())))
		for command in startup_commands:
			if command.lstrip().startswith(".") or sqlite3.complete_statement(command):
				self.startup_queue.append(command)
			else:
				self.startup_queue.append(command.rstrip() + ";")
		if options.sql is not None:
			self.startup_queue.append(options.sql if sqlite3.complete_statement(options.sql) else options.sql.rstrip() + ";")
		self.dispatcher = build_command_dispatcher(
			self.diagnostics,
			self.interactive,
			self.protected_backup_paths,
			self.finish_once,
			self.run_expert,
		)

	def protected_backup_paths(self, include_sidecars=True):
		paths = [active[0] for active in self.state.scripts]
		for _, _, filename in get_databases(self.state.cursor):
			if filename:
				suffixes = ("", "-wal", "-shm", "-journal") if include_sidecars else ("",)
				paths.extend(Path(filename + suffix).resolve() for suffix in suffixes)
		for handle in (self.state.output_file, self.state.once_file):
			if handle:
				paths.append(Path(handle.name).resolve())
		return paths

	def finish_once(self):
		state = self.state
		if not state.once_file:
			return
		handles = [state.once_file]
		state.once_file = None
		if state.output_file:
			handles.append(state.output_file)
			state.output_file = None
		sys.stdout = state.console
		for handle in handles:
			try:
				handle.close()
			except OSError as error:
				print(f"Output error: {error}", file=sys.stderr)
				state.failed = True

	def run_expert(self, statement, options):
		state = self.state
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
			self.finish_once()

	def run_sql(self, buffer):
		state = self.state
		if state.expert_options is not None:
			if not sqlite3.complete_statement(buffer):
				return buffer if sql_context(buffer)[1] or sql_context(buffer)[0] == "/*" else ""
			options, state.expert_options = state.expert_options, None
			self.run_expert(buffer, options)
			return ""
		start = 0
		for index, char in enumerate(buffer):
			if char == ";" and sqlite3.complete_statement(buffer[start:index + 1]):
				statement = buffer[start:index + 1]
				capture_output = io.StringIO() if (state.www_pending or state.testcase_name is not None) else None
				try:
					with redirect_stdout(capture_output if capture_output is not None else (state.once_file if state.once_file is not None else sys.stdout)):
						success = execute_sql(state.cursor, statement, state.headers, state.mode, state.nullvalue, state.column_separator, state.row_separator, state.column_widths, state.crlf, state.changes, state.source_context, state.parameters, state.timer, state.stats, state.eqp, state.explain, state.scanstats, self.diagnostics.log_callback)
				except OSError as error:
					# A full disk or an interrupted file handle must not turn a normal
					# CLI operation into a Python traceback.  Drop the failing redirect so
					# later interactive commands can still be shown on the console.
					if state.once_file:
						self.finish_once()
					elif state.output_file:
						try:
							state.output_file.close()
						except OSError:
							pass
						state.output_file = None
						sys.stdout = state.console
					print(f"Output error: {error}", file=sys.stderr)
					state.failed = True
					return ""
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
					self.finish_once()
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

	def open_initial_database(self):
		if not self.options.database:
			return
		try:
			connection, cursor = pale_open_readonly(self.options.database) if self.options.readonly else pale_open(self.options.database)
			self.state.connection, self.state.cursor = connection, cursor
			self.state.connections.append((connection, cursor))
			self.state.active_connection = 0
		except (OSError, ValueError, sqlite3.Error) as error:
			print(f"Database error: {error}", file=sys.stderr)
			self.state.failed = True

	def cancel_input(self):
		state = self.state
		if state.connection is not None:
			try:
				state.connection.interrupt()
			except sqlite3.Error:
				pass
		print("^C", file=state.console)
		state.clear_sql()

	def next_input(self):
		state = self.state
		if state.scripts:
			try:
				state.source_context = (state.scripts[-1][0], 0)
				line_number, value = next(state.scripts[-1][1])
				state.scripts[-1] = (state.scripts[-1][0], state.scripts[-1][1], line_number)
				state.source_context = (state.scripts[-1][0], line_number)
				value = value.rstrip("\r\n")
				if state.echo:
					print(value)
				return value
			except StopIteration:
				path, _, last_line = state.scripts.pop()
				if sql_context(state.sql_buffer)[1] or sql_context(state.sql_buffer)[0] == "/*":
					print(f"SQL error: {path}:{last_line}: incomplete statement", file=sys.stderr)
					state.failed = True
					state.clear_sql()
				return _SCRIPT_FINISHED
		state.source_context = None
		if self.startup_queue:
			value = self.startup_queue.pop(0)
			if state.echo:
				print(value)
			return value
		if self.startup_exit:
			return None
		with redirect_stdout(state.console):
			prompt = (state.continue_prompt if state.sql_buffer else state.main_prompt) if self.interactive else ""
			return cli_input(prompt) if self.interactive else input(prompt)

	def handle_unknown_or_sql(self, raw_input, command, argument):
		state = self.state
		if state.connection is None:
			if state.scripts:
				print(
					f"Read error: no database opened; stopped {state.scripts[-1][0]}. "
					"Use .open <database> before .read.",
					file=sys.stderr,
				)
				state.failed = True
				state.scripts.clear()
				state.clear_sql()
			else:
				print("No database opened", file=sys.stderr if state.output_file else sys.stdout)
				if self.startup_exit:
					state.failed = True
			return
		if command.startswith("."):
			print(f"Unknown command: {command}", file=sys.stderr if state.output_file else sys.stdout)
			if self.startup_exit:
				state.failed = True
			return
		state.sql_buffer += raw_input + "\n"
		try:
			state.sql_buffer = self.run_sql(state.sql_buffer)
		except KeyboardInterrupt:
			self.cancel_input()

	def input_loop(self):
		state = self.state
		while True:
			if state.bail and state.failed:
				if state.scripts:
					state.scripts.clear()
				if self.startup_queue:
					self.startup_queue.clear()
				if self.startup_exit and not state.scripts:
					break
				state.clear_sql()
			try:
				value = self.next_input()
			except EOFError:
				if sql_context(state.sql_buffer)[1] or sql_context(state.sql_buffer)[0] == "/*":
					print("SQL error: incomplete statement at end of input", file=sys.stderr)
					return 1
				break
			except KeyboardInterrupt:
				self.cancel_input()
				continue
			if value is _SCRIPT_FINISHED:
				continue
			if value is None:
				break
			if value.strip():
				state.history.append(value.rstrip("\r\n"))
				if len(state.history) > 1000:
					del state.history[:-1000]
			if sql_context(state.sql_buffer)[0] not in ("", "--"):
				state.sql_buffer += value + "\n"
				try:
					state.sql_buffer = self.run_sql(state.sql_buffer)
				except KeyboardInterrupt:
					self.cancel_input()
				continue
			raw_input = value
			message = re.fullmatch(r"\.print(?:[ \t](.*))?", value.lstrip(), re.IGNORECASE)
			if message:
				self.dispatcher.dispatch(".print", state, message[1] or "")
				continue
			value = value.strip()
			if not value:
				continue
			parts = value.split(None, 1)
			command = parts[0].lower()
			argument = parts[1].strip() if len(parts) == 2 else ""
			if self.dispatcher.dispatch(command, state, argument):
				if state.exit_requested:
					break
				continue
			if state.exit_requested:
				break
			self.handle_unknown_or_sql(raw_input, command, argument)

	def close(self):
		self.diagnostics.close(self.state.connection)
		sys.stdout = self.state.console
		for handle in (self.state.once_file, self.state.output_file):
			if handle:
				try:
					handle.close()
				except OSError as error:
					print(f"Output error: {error}", file=sys.stderr)
					self.state.failed = True
		close_all(self.state.connections)

	def run(self):
		if self.options.show_help:
			print_cli_help(self.parser)
			return 0
		if self.options.show_version:
			pale_version()
			return 0
		if self.options.readonly and self.options.database is None:
			print("Argument error: --readonly requires a database", file=sys.stderr)
			return 2
		try:
			self.configure()
		except ValueError as error:
			print(f"Argument error: {error}", file=sys.stderr)
			return 2
		self.open_initial_database()
		try:
			result = self.input_loop()
		finally:
			self.close()
		return 1 if self.state.failed else (result if result is not None else 0)


def main(argv=None):
	return PaleCLI(argv).run()


def run():
	return main(sys.argv[1:])


if __name__ == "__main__":
	sys.exit(run())
