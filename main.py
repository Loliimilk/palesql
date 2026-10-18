import sys
import sqlite3
import os
import re
import io
import json
import math
import time
import unicodedata
import shutil
import shlex
import csv
import hashlib
import itertools
import tempfile
import fnmatch
import subprocess
import webbrowser
import html
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from contextlib import redirect_stdout


PALESQL_VERSION = "0.13.0"
PYTHON_VERSION = sys.version.split()[0]
SQLITE_LIBRARY_VERSION = sqlite3.sqlite_version
OUTPUT_MODES = ("list", "column", "table", "box", "csv", "tabs", "json", "jsonpretty", "line", "markdown", "quote")
SQLITE_LIMITS = {
	"length": sqlite3.SQLITE_LIMIT_LENGTH,
	"sql_length": sqlite3.SQLITE_LIMIT_SQL_LENGTH,
	"column": sqlite3.SQLITE_LIMIT_COLUMN,
	"expr_depth": sqlite3.SQLITE_LIMIT_EXPR_DEPTH,
	"compound_select": sqlite3.SQLITE_LIMIT_COMPOUND_SELECT,
	"vdbe_op": sqlite3.SQLITE_LIMIT_VDBE_OP,
	"function_arg": sqlite3.SQLITE_LIMIT_FUNCTION_ARG,
	"attached": sqlite3.SQLITE_LIMIT_ATTACHED,
	"like_pattern_length": sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH,
	"variable_number": sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,
	"trigger_depth": sqlite3.SQLITE_LIMIT_TRIGGER_DEPTH,
	"worker_threads": sqlite3.SQLITE_LIMIT_WORKER_THREADS,
}
SQLITE_DBCONFIGS = {
	name.removeprefix("SQLITE_DBCONFIG_").lower(): value
	for name in dir(sqlite3)
	if name.startswith("SQLITE_DBCONFIG_")
	for value in (getattr(sqlite3, name),)
}
SQLITE_FILECTRL_OPERATIONS = (
	"lockstate",
	"last_errno",
	"size_limit",
	"chunk_size",
	"file_pointer",
	"sync_omitted",
	"win32_av_retry",
	"persist_wal",
	"pdb",
	"begin_atomic_write",
	"commit_atomic_write",
	"rollback_atomic_write",
	"has_mmap",
	"size_hint",
	"vfsname",
	"powersafe_override",
	"reserve_bytes",
	"tempfilename",
)


MAX_COLUMNS = 8
MAX_WIDTH = 3
MAX_CANDIDATES = 128
MAX_STATEMENTS = 32
MAX_SECONDS = 10
USAGE = "Usage: .expert [--verbose] [--sample 0..100] [SQL]"


def parse_expert_arguments(argument):
	sample, verbose = 0, False
	remaining = argument.strip()
	while remaining.startswith("--"):
		# Options are whitespace-delimited, whereas SQL is preserved verbatim.
		parts = remaining.split(None, 1)
		option = parts[0]
		remaining = parts[1].lstrip() if len(parts) > 1 else ""
		if option == "--verbose":
			verbose = True
		elif option == "--sample":
			parts = remaining.split(None, 1)
			if not parts or not re.fullmatch(r"[0-9]+", parts[0]):
				raise ValueError(USAGE)
			sample = int(parts[0])
			if not 0 <= sample <= 100:
				raise ValueError(USAGE)
			remaining = parts[1].lstrip() if len(parts) > 1 else ""
		else:
			raise ValueError(USAGE)
	if remaining[:1] in ("'", '"') and remaining[-1:] == remaining[:1] and len(remaining) > 1:
		remaining = remaining[1:-1]
	return sample, verbose, remaining or None


def sql_statements(sql):
	"""Split SQL without treating quoted/commented semicolons as boundaries."""
	if "\x00" in sql:
		raise ValueError("SQL contains a NUL character")
	statements, start, index, state, first = [], 0, 0, "", None
	while index < len(sql):
		char, pair = sql[index], sql[index:index + 2]
		if state == "--":
			if char == "\n":
				state = ""
		elif state == "/*":
			if pair == "*/":
				state = ""
				index += 1
		elif state:
			if char == state:
				if state != "]" and pair == state * 2:
					index += 1
				else:
					state = ""
		elif pair in ("--", "/*"):
			state = pair
			index += 1
		elif char in ("'", '"', "`", "["):
			if first is None:
				first = ""
			state = "]" if char == "[" else char
		elif char == ";":
			if first is not None:
				statements.append((sql[start:index + 1], first))
			start, first = index + 1, None
		elif not char.isspace() and first is None:
			match = re.match(r"[A-Za-z]+", sql[index:])
			first = match[0].upper() if match else ""
		index += 1
	if state not in ("", "--"):
		raise ValueError("incomplete SQL string, identifier or comment")
	if first is not None:
		statements.append((sql[start:], first))
	if not statements:
		raise ValueError("provide a SELECT, INSERT, UPDATE or DELETE statement")
	if len(statements) > MAX_STATEMENTS:
		raise ValueError(f"analyze at most {MAX_STATEMENTS} statements at a time")
	for _, keyword in statements:
		if keyword not in ("SELECT", "WITH", "VALUES", "INSERT", "UPDATE", "DELETE", "REPLACE"):
			raise ValueError("only SELECT, INSERT, UPDATE and DELETE statements can be analyzed")
	return [statement for statement, _ in statements]


@contextmanager
def snapshot(connection):
	if not hasattr(connection, "serialize"):
		raise ValueError(".expert needs Python 3.11+ with SQLite serialization support")
	databases = connection.execute("PRAGMA database_list").fetchall()
	if any(row[1] == "temp" for row in databases):
		if connection.execute("SELECT 1 FROM temp.sqlite_schema LIMIT 1").fetchone():
			raise ValueError(".expert does not support a non-empty temp schema; temp objects may shadow main tables")
	with tempfile.TemporaryDirectory(prefix="palesql-expert-") as directory:
		paths = []
		for number, name, _ in databases:
			if name == "temp":
				continue
			path = Path(directory) / f"snapshot-{number}.db"
			if connection.execute(f"PRAGMA {quote_name(name)}.page_count").fetchone()[0]:
				path.write_bytes(connection.serialize(name=name))
			paths.append((name, path))
		copy = sqlite3.connect(paths[0][1], isolation_level=None, cached_statements=0)
		try:
			for name, path in paths[1:]:
				copy.execute(f"ATTACH DATABASE ? AS {quote_name(name)}", (str(path),))
			for name, _ in paths:
				# These are disposable copies, including when the original uses WAL.
				copy.execute(f"PRAGMA {quote_name(name)}.journal_mode=MEMORY")
				copy.execute(f"PRAGMA {quote_name(name)}.synchronous=OFF")
			for setting in ("automatic_index", "foreign_keys", "recursive_triggers"):
				value = connection.execute("PRAGMA " + setting).fetchone()[0]
				copy.execute(f"PRAGMA {setting}={int(value)}")
			yield copy
		finally:
			copy.close()


def null_bindings(statement):
	"""Make named/numbered/mixed bindings positional, all bound to SQL NULL.

	The original statement is prepared by SQLite first, so invalid parameter
	names/numbers still fail. Quoted text and comments are copied unchanged.
	"""
	parts, count, index = [], 0, 0
	def name_char(char):
		return char.isalnum() or char in "_$" or ord(char) >= 128
	while index < len(statement):
		start, char = index, statement[index]
		if statement[index:index + 2] == "--":
			end = statement.find("\n", index + 2)
			index = len(statement) if end < 0 else end + 1
		elif statement[index:index + 2] == "/*":
			end = statement.find("*/", index + 2)
			index = len(statement) if end < 0 else end + 2
		elif char in ("'", '"', "`", "["):
			end_quote = "]" if char == "[" else char
			index += 1
			while index < len(statement):
				if statement[index] == end_quote:
					index += 1
					if end_quote != "]" and statement[index:index + 1] == end_quote:
						index += 1
						continue
					break
				index += 1
		elif char == "?" or (char in ":@$" and index + 1 < len(statement) and name_char(statement[index + 1])):
			index += 1
			if char == "?":
				while index < len(statement) and statement[index].isdigit():
					index += 1
			else:
				while index < len(statement):
					if name_char(statement[index]):
						index += 1
					elif statement[index:index + 2] == "::":
						index += 2
					else:
						break
				if statement[index:index + 1] == "(":
					end = statement.find(")", index + 1)
					if end >= 0:
						index = end + 1
			parts.append("?")
			count += 1
			continue
		elif char.isalpha() or char == "_" or ord(char) >= 128:
			index += 1
			while index < len(statement) and name_char(statement[index]):
				index += 1
		else:
			index += 1
		parts.append(statement[start:index])
	return "".join(parts), (None,) * count


def query_plans(connection, statements, reads=None):
	allowed = {sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION,
		sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_RECURSIVE}

	def authorize(action, table, column, database, trigger):
		if action not in allowed:
			return sqlite3.SQLITE_DENY
		if action == sqlite3.SQLITE_FUNCTION and column and column.lower() == "load_extension":
			return sqlite3.SQLITE_DENY
		if action == sqlite3.SQLITE_READ and reads is not None and column:
			reads.setdefault((database, table), Counter())[column] += 1
		return sqlite3.SQLITE_OK

	connection.set_authorizer(authorize)
	try:
		plans = []
		for statement in statements:
			try:
				rows = connection.execute("EXPLAIN QUERY PLAN " + statement).fetchall()
			except sqlite3.ProgrammingError as error:
				match = re.search(r"uses (\d+), and there are 0 supplied", str(error))
				if not match:
					raise
				normalized, bindings = null_bindings(statement)
				rows = connection.execute("EXPLAIN QUERY PLAN " + normalized, bindings).fetchall()
			plans.append([row[3] for row in rows])
		return plans
	finally:
		connection.set_authorizer(None)


def plan_score(plan):
	"""Lower is preferable. Covering scans alone do not count as improvements."""
	scans = sum(line.startswith("SCAN ") for line in plan)
	sorts = sum("USE TEMP B-TREE" in line for line in plan)
	automatic = sum("USING AUTOMATIC" in line for line in plan)
	constraints = sum(len(re.findall(r"(?:=|>|<)\?", line)) for line in plan if line.startswith("SEARCH "))
	return scans, sorts, automatic, -constraints


def scores(plans):
	return [plan_score(plan) for plan in plans]


def improves(candidate, previous):
	return all(new <= old for new, old in zip(candidate, previous)) and candidate != previous


def index_candidates(connection, reads, notes):
	tables = []
	for (database, table), columns in reads.items():
		if not database or not table or table.lower().startswith("sqlite_"):
			continue
		kind = connection.execute(f"SELECT type FROM {quote_name(database)}.sqlite_schema WHERE name=?", (table,)).fetchone()
		if not kind or kind[0] != "table":
			continue
		# Virtual tables and their shadow tables cannot accept ordinary indexes.
		table_types = {row[1]: row[2] for row in connection.execute(f"PRAGMA {quote_name(database)}.table_list")}
		if table_types.get(table) not in ("table", None):
			notes.append(f"Skipped virtual/shadow table: {database}.{table}")
			continue
		valid = {row[1] for row in connection.execute(f"PRAGMA {quote_name(database)}.table_xinfo({quote_name(table)})") if row[6] != 1}
		ordered = [name for name, _ in columns.most_common() if name in valid]
		if len(ordered) > MAX_COLUMNS:
			notes.append(f"Column search limited to {MAX_COLUMNS} columns for {database}.{table}.")
		ordered = ordered[:MAX_COLUMNS]
		if ordered:
			tables.append((database, table, ordered))
	result = []
	# Interleave tables so one wide table cannot consume the entire search budget.
	for width in range(1, MAX_WIDTH + 1):
		iterators = [iter(itertools.permutations(columns, width)) for _, _, columns in tables]
		for group in itertools.zip_longest(*iterators):
			for (database, table, _), columns in zip(tables, group):
				if columns is None:
					continue
				if len(result) >= MAX_CANDIDATES:
					notes.append(f"Candidate search limited to {MAX_CANDIDATES} indexes of up to {MAX_WIDTH} columns.")
					return result
				digest = hashlib.sha256(repr((database, table, columns)).encode("utf-8")).hexdigest()[:16]
				name = "palesql_idx_" + digest
				while connection.execute(f"SELECT 1 FROM {quote_name(database)}.sqlite_schema WHERE name=? COLLATE NOCASE", (name,)).fetchone():
					name += "_"
				qualified = f"{quote_name(database)}.{quote_name(name)}"
				definition = f"CREATE INDEX {qualified} ON {quote_name(table)} ({', '.join(map(quote_name, columns))});"
				result.append((database, table, name, qualified, definition, len(columns)))
	return result


def analyze_expert(connection, sql_text, *, sample=0, verbose=False):
	if not isinstance(sample, int) or not 0 <= sample <= 100:
		raise ValueError(USAGE)
	statements = sql_statements(sql_text)
	notes, selected, tried = [], [], 0
	deadline = time.monotonic() + MAX_SECONDS
	try:
		with snapshot(connection) as copy:
			copy.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
			reads = {}
			plans = query_plans(copy, statements, reads)
			candidates = index_candidates(copy, reads, notes)
			limits = {}
			if sample:
				for database, table, *_ in candidates:
					if (database, table) in limits:
						continue
					qualified = f"{quote_name(database)}.{quote_name(table)}"
					count = copy.execute(f"SELECT count(*) FROM {qualified}").fetchone()[0]
					limit = 0 if sample == 100 else max(1, math.ceil(count * sample / 100))
					limits[database, table] = limit
					copy.execute(f"PRAGMA analysis_limit={limit}")
					copy.execute(f"ANALYZE {qualified}")
				plans = query_plans(copy, statements)
				if sample < 100:
					notes.append(f"--sample {sample}: approximate ANALYZE using analysis_limit, not random sampling.")
			baseline = scores(plans)
			baseline_plans = plans
			installed = []
			for candidate in candidates:
				if time.monotonic() >= deadline:
					raise ValueError(f"analysis exceeded its {MAX_SECONDS}-second budget")
				database, table, name, qualified, definition, width = candidate
				copy.execute(definition)
				installed.append(candidate)
				if sample:
					copy.execute(f"PRAGMA analysis_limit={limits[database, table]}")
					copy.execute(f"ANALYZE {qualified}")
			plans = query_plans(copy, statements)
			current = scores(plans)
			tried += 1
			if improves(current, baseline):
				# Removing widest candidates first also gives narrower alternatives
				# a chance. Jointly useful JOIN indexes survive this process.
				for candidate in sorted(installed, key=lambda item: -item[5]):
					if time.monotonic() >= deadline:
						raise ValueError(f"analysis exceeded its {MAX_SECONDS}-second budget")
					copy.execute(f"DROP INDEX {candidate[3]}")
					trial_plans = query_plans(copy, statements)
					trial = scores(trial_plans)
					tried += 1
					if all(new <= old for new, old in zip(trial, current)):
						plans, current = trial_plans, trial
					else:
						copy.execute(candidate[4])
						if sample:
							copy.execute(f"PRAGMA analysis_limit={limits[candidate[0], candidate[1]]}")
							copy.execute(f"ANALYZE {candidate[3]}")
						selected.append(candidate)
				# Recheck the exact set that will be reported, after all pruning.
				plans = query_plans(copy, statements)
				while selected:
					used = "\n".join(line for plan in plans for line in plan)
					unused = [item for item in selected if not re.search(r"\b" + re.escape(item[2]) + r"\b", used)]
					if not unused:
						break
					for item in unused:
						copy.execute(f"DROP INDEX {item[3]}")
						selected.remove(item)
					plans = query_plans(copy, statements)
				if not improves(scores(plans), baseline):
					selected, plans = [], baseline_plans
			else:
				selected, plans = [], baseline_plans
	except sqlite3.Error as error:
		if time.monotonic() >= deadline:
			raise ValueError(f"analysis exceeded its {MAX_SECONDS}-second budget") from error
		raise ValueError(f"cannot analyze SQL: {error}") from error
	output = ["Python index advisor (bounded search; not native sqlite3_expert)."]
	if selected:
		output.extend(item[4] for item in selected)
	else:
		output.append("(no new indexes)")
	for number, (statement, plan) in enumerate(zip(statements, plans), 1):
		output.append(f"Query {number} plan:")
		if verbose:
			output.append(statement.strip())
		output.extend(plan or ["(no table access)"])
	if verbose:
		output.append(f"Candidates: {len(candidates)}; plans checked: {tried}; statistics sample: {sample}.")
	output.extend(dict.fromkeys(notes))
	return "\n".join(output) + "\n"

def pale_open(name):
	name = str(name)
	if name == ":memory:":
		con = sqlite3.connect(name, isolation_level=None)
	elif name.lower().startswith("file:"):
		con = sqlite3.connect(name, uri=True, isolation_level=None)
	else:
		if not name.lower().endswith(".db"):
			name += ".db"
		con = sqlite3.connect(name, isolation_level=None)
	cursor = con.cursor()

	return con, cursor


def parse_single_argument(argument, usage):
	lexer = shlex.shlex(argument, posix=True)
	lexer.whitespace_split = True
	lexer.commenters = ""
	lexer.escape = ""
	values = list(lexer)
	if len(values) != 1 or not values[0]:
		raise ValueError(usage)
	return values[0]


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
	print(
		f"PaleSQL version {PALESQL_VERSION}\n"
		f"Python version {PYTHON_VERSION}\n"
		f"SQLite library version {SQLITE_LIBRARY_VERSION}\n"
	)


def pale_dbconfig(connection, argument=""):
	if not hasattr(connection, "getconfig") or not hasattr(connection, "setconfig"):
		raise ValueError("this Python sqlite3 does not expose sqlite3_db_config")
	lexer = shlex.shlex(argument, posix=True)
	lexer.whitespace_split = True
	lexer.commenters = ""
	values = list(lexer)
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
		try:
			value = connection.getconfig(option)
		except (AttributeError, sqlite3.Error) as error:
			raise ValueError(f"{name}: {error}") from error
		print(f"{name:>28}: {int(bool(value))}")
		return
	setting = values[1].lower()
	if setting in ("on", "true", "yes", "1"):
		setting_value = 1
	elif setting in ("off", "false", "no", "0"):
		setting_value = 0
	else:
		raise ValueError("Usage: .dbconfig [name] [on|off]")
	try:
		connection.setconfig(option, setting_value)
		value = connection.getconfig(option)
	except (AttributeError, sqlite3.Error) as error:
		raise ValueError(f"{name}: {error}") from error
	print(f"{name:>28}: {int(bool(value))}")


def pale_foreign_keys(cursor, argument=""):
	argument = argument.strip().lower()
	if argument not in ("", "on", "off"):
		raise ValueError("Usage: .foreign_keys on|off")
	if argument:
		cursor.execute(f"PRAGMA foreign_keys = {argument}")
	cursor.execute("PRAGMA foreign_keys")
	value = cursor.fetchone()[0]
	print(f"foreign_keys: {'on' if value else 'off'}")


def pale_journal_mode(cursor, argument=""):
	argument = argument.strip().lower()
	allowed = {"delete", "truncate", "persist", "memory", "wal", "off"}
	if argument and argument not in allowed:
		raise ValueError("Usage: .journal_mode [delete|truncate|persist|memory|wal|off]")
	if argument:
		cursor.execute(f"PRAGMA journal_mode = {argument}")
	else:
		cursor.execute("PRAGMA journal_mode")
	row = cursor.fetchone()
	if not row or row[0] is None:
		raise sqlite3.DatabaseError("SQLite did not return a journal mode")
	print(f"journal_mode: {str(row[0]).lower()}")


def pale_encoding(cursor, argument=""):
	argument = argument.strip().lower()
	encodings = {
		"utf-8": "UTF-8",
		"utf8": "UTF-8",
		"utf-16le": "UTF-16le",
		"utf16le": "UTF-16le",
		"utf-16be": "UTF-16be",
		"utf16be": "UTF-16be",
	}
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
	lexer = shlex.shlex(argument, posix=True)
	lexer.whitespace_split = True
	lexer.commenters = ""
	values = list(lexer)
	if len(values) not in (1, 2) or not values[0]:
		raise ValueError("Usage: .load <file> [entrypoint]")
	path = Path(values[0]).expanduser()
	if not path.is_absolute() and base_path is not None:
		path = base_path / path
	path = path.resolve()
	connection.enable_load_extension(True)
	try:
		if len(values) == 2:
			connection.load_extension(str(path), entrypoint=values[1])
		else:
			connection.load_extension(str(path))
	finally:
		connection.enable_load_extension(False)
	print(f"Loaded extension: {path}")


def pale_filectrl(connection, argument=""):
	lexer = shlex.shlex(argument, posix=True)
	lexer.whitespace_split = True
	lexer.commenters = ""
	values = list(lexer)
	if not values:
		raise ValueError("Usage: .filectrl <operation> [arguments]")
	operation = values[0].lower()
	if operation not in SQLITE_FILECTRL_OPERATIONS:
		raise ValueError(f"unknown filectrl operation: {operation}")
	if not hasattr(connection, "file_control"):
		raise ValueError("sqlite3_file_control is not exposed by Python's standard sqlite3 module")
	raise ValueError("PaleSQL cannot call sqlite3_file_control on this connection")


def pale_unmodule(connection, argument=""):
	lexer = shlex.shlex(argument, posix=True)
	lexer.whitespace_split = True
	lexer.commenters = ""
	values = list(lexer)
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


def get_databases(cursor):
	cursor.execute("PRAGMA database_list;")
	return cursor.fetchall()


def print_databases(rows):
	print(f"{'seq':<5} | {'name':<8} | file")

	for seq, name, file in rows:
		print(f"{seq:<5} | {name:<8} | {file}")


def pale_database(cursor):
	rows = get_databases(cursor)
	main = next((row for row in rows if row[1] == "main"), None)

	if main is None:
		print("Main database not found")
		return

	print_databases([main])


def pale_databases(cursor):
	print_databases(get_databases(cursor))

def quote_name(name):
	return '"' + name.replace('"', '""') + '"'

def identifier_key(name):
	# SQLite folds ASCII letters only when comparing identifiers.
	return name.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))

def database_objects(cursor, object_type=None):
	objects = []
	for seq, database, file in get_databases(cursor):
		cursor.execute(f"SELECT type, name, tbl_name, sql FROM {quote_name(database)}.sqlite_master ORDER BY name")
		for kind, name, table, sql in cursor.fetchall():
			if object_type is None or kind == object_type:
				objects.append((database, kind, name, table, sql))
	return objects

def object_label(database, name):
	return name if database == "main" else f"{database}.{name}"

def find_table(cursor, table_name):
	objects = database_objects(cursor)
	objects.sort(key=lambda row: 0 if row[0] == "temp" else 1)
	for database, kind, name, table, sql in objects:
		if kind in ("table", "view") and identifier_key(f"{database}.{name}") == identifier_key(table_name):
			return database, name
	for database, kind, name, table, sql in objects:
		if kind in ("table", "view") and identifier_key(name) == identifier_key(table_name):
			return database, name
	return None

def table_pragma(cursor, database, pragma, name):
	cursor.execute(f"PRAGMA {quote_name(database)}.{pragma}({quote_name(name)});")
	return cursor.fetchall()

def shadow_tables(cursor):
	cursor.execute("PRAGMA table_list;")
	return {(row[0], row[1]) for row in cursor.fetchall() if row[2] == "shadow"}

def pale_dbinfo(cursor, database_name):
	rows = get_databases(cursor)
	database = next((row for row in rows if identifier_key(row[1]) == identifier_key(database_name)), None)

	if database is None:
		print(f"Database '{database_name}' not found")
		return

	seq, name, file = database
	quoted_name = '"' + name.replace('"', '""') + '"'
	info = {}

	for pragma in ("page_size", "page_count", "encoding", "schema_version", "freelist_count", "user_version", "journal_mode"):
		cursor.execute(f"PRAGMA {quoted_name}.{pragma};")
		info[pragma] = cursor.fetchone()[0]

	print(f"{'database':<20} | {name}")
	print(f"{'file':<20} | {file or '(no file)'}")

	for pragma, value in info.items():
		label = pragma.replace("_", " ")
		print(f"{label:<20} | {value}")

	print(f"{'logical size (bytes)':<20} | {info['page_size'] * info['page_count']}")
	print(f"{'free space (bytes)':<20} | {info['page_size'] * info['freelist_count']}")
	if file:
		total = 0
		for label, suffix in (("file bytes", ""), ("WAL bytes", "-wal"), ("SHM bytes", "-shm"), ("journal bytes", "-journal")):
			try:
				size = os.path.getsize(file + suffix)
			except FileNotFoundError:
				size = 0
			except OSError as error:
				print(f"{label:<20} | unavailable: {error}")
				total = None
				continue
			print(f"{label:<20} | {size}")
			if total is not None:
				total += size
		if total is not None:
			print(f"{'total file bytes':<20} | {total}")

def pale_tables(cursor, pattern):
	rows = []
	shadows = shadow_tables(cursor)
	for database, kind, name, table, sql in database_objects(cursor, "table"):
		if name.startswith("sqlite_") or (database, name) in shadows:
			continue
		cursor.execute("SELECT ? LIKE ? OR ? LIKE ?", (name, pattern, f"{database}.{name}", pattern))
		if cursor.fetchone()[0]:
			rows.append((object_label(database, name),))
	return rows

def pale_views(cursor):
	return [(object_label(database, name),) for database, kind, name, table, sql in database_objects(cursor, "view")]

def pale_triggers(cursor):
	return [(object_label(database, name),) for database, kind, name, table, sql in database_objects(cursor, "trigger")]

def pale_indexes(cursor, table_name):
	rows = []
	for database, kind, name, table, sql in database_objects(cursor, "index"):
		if not table_name or identifier_key(table) == identifier_key(table_name) or identifier_key(f"{database}.{table}") == identifier_key(table_name):
			rows.append((object_label(database, name),))
	return rows


def pale_imposter(cursor, argument):
	values = shlex.split(argument)
	if len(values) != 2:
		raise ValueError("Usage: .imposter <index> <table>")
	index_name, imposter_name = values
	index = next((row for row in database_objects(cursor, "index") if row[0] == "main" and identifier_key(row[2]) == identifier_key(index_name)), None)
	if index is None:
		raise ValueError(f"index not found: {index_name}")
	table_name = index[3]
	info = table_pragma(cursor, "main", "index_xinfo", index_name)
	keys = [row for row in info if len(row) > 5 and row[5] and row[1] >= 0]
	if not keys:
		raise ValueError("index has no ordinary columns; expression indexes are not supported")
	table_info = {row[0]: (row[1], row[2]) for row in table_pragma(cursor, "main", "table_xinfo", table_name)}
	columns = []
	for row in sorted(keys, key=lambda item: item[0]):
		if row[1] not in table_info:
			raise ValueError(f"cannot map index column {row[1]}")
		name, declared_type = table_info[row[1]]
		columns.append((name, declared_type or ""))
	if len({identifier_key(name) for name, unused in columns}) != len(columns):
		raise ValueError("index columns must have unique names")
	definition = ", ".join(f"{quote_name(name)} {declared_type}".rstrip() for name, declared_type in columns)
	column_sql = ", ".join(quote_name(name) for name, unused in columns)
	cursor.execute(f"DROP TABLE IF EXISTS temp.{quote_name(imposter_name)}")
	cursor.execute(f"CREATE TEMP TABLE {quote_name(imposter_name)} ({definition})")
	cursor.execute(f"INSERT INTO temp.{quote_name(imposter_name)} ({column_sql}) SELECT {column_sql} FROM main.{quote_name(table_name)}")
	print(f"Imposter snapshot '{imposter_name}' created from index '{index_name}' ({cursor.rowcount} rows)")

def result_text(value, nullvalue=""):
	if value is None:
		return nullvalue
	if isinstance(value, bytes):
		return value.hex()
	return str(value)

def sql_literal(value):
	if value is None:
		return "NULL"
	if isinstance(value, bytes):
		return "X'" + value.hex() + "'"
	if isinstance(value, str):
		if "\x00" in value:
			return "CAST(X'" + value.encode("utf-8").hex() + "' AS TEXT)"
		return "'" + value.replace("'", "''") + "'"
	if isinstance(value, float) and not math.isfinite(value):
		return "NULL" if math.isnan(value) else ("-9e999" if value < 0 else "9e999")
	return str(value)

def text_clusters(text):
	clusters = []
	for char in text:
		code = ord(char)
		regional = 0x1F1E6 <= code <= 0x1F1FF
		extend = unicodedata.category(char).startswith("M") or char == "\u200d" or 0x1F3FB <= code <= 0x1F3FF or 0xE0020 <= code <= 0xE007F
		if clusters and (extend or clusters[-1].endswith("\u200d") or (regional and len(clusters[-1]) == 1 and 0x1F1E6 <= ord(clusters[-1]) <= 0x1F1FF)):
			clusters[-1] += char
		else:
			clusters.append(char)
	return clusters


def cluster_width(cluster):
	widths = [0 if unicodedata.category(char).startswith("M") or unicodedata.category(char) == "Cf" else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in cluster]
	if "\u200d" in cluster or "\ufe0f" in cluster or "\u20e3" in cluster or any(0x1F1E6 <= ord(char) <= 0x1F1FF for char in cluster):
		return max(2, max(widths, default=0))
	return max(widths, default=0)


def text_width(text):
	return sum(cluster_width(cluster) for cluster in text_clusters(text))


def wrap_cell(text, width):
	lines, line, used = [], "", 0
	for cluster in text_clusters(text):
		size = cluster_width(cluster)
		if used + size > width and line:
			lines.append(line)
			line, used = "", 0
		line += cluster
		used += size
	lines.append(line)
	return lines

def write_result(text):
	if not sys.stdout.isatty() and hasattr(sys.stdout, "buffer"):
		sys.stdout.flush()
		sys.stdout.buffer.write(text.encode(sys.stdout.encoding or "utf-8"))
		sys.stdout.buffer.flush()
	else:
		sys.stdout.write(text)

def print_grid(names, rows, headers, mode, nullvalue="", column_widths=(), newline="\n"):
	emit = lambda text="": write_result(text + newline)
	data = ([names] if headers else []) + rows
	if not data:
		return
	data = [[result_text(value, nullvalue).replace("\r", "\\r").replace("\t", "\\t").replace("\x00", "\\0").split("\n") for value in row] for row in data]
	widths = [max(text_width(line) for row in data for line in row[index]) for index in range(len(names))]
	minimums = [max(1, max((text_width(char) for row in data for line in row[index] for char in text_clusters(line)), default=1)) for index in range(len(names))]
	if mode in ("column", "table"):
		for index, width in enumerate(column_widths[:len(names)]):
			if width:
				widths[index] = max(width, minimums[index])
	if sys.stdout.isatty():
		limit = max(2, shutil.get_terminal_size().columns - 1)
		overhead = 2 * (len(names) - 1) if mode == "column" else 3 * len(names) + 1
		if sum(minimums) + overhead > limit:
			if len(names) > 1:
				# Keep every value when even the narrowest multi-column grid cannot fit.
				for index, name in enumerate(names):
					print_grid([name], [[row[index]] for row in rows], headers, mode, nullvalue, column_widths[index:index + 1], newline)
				return
			for row in data:
				for line in row[0]:
					for part in wrap_cell(line, limit):
						emit(part)
			return
		widths = [max(min(width, limit), minimum) for width, minimum in zip(widths, minimums)]
		while sum(widths) + overhead > limit:
			index = max((i for i in range(len(widths)) if widths[i] > minimums[i]), key=lambda i: widths[i])
			widths[index] -= 1
	data = [[[part for line in cell for part in wrap_cell(line, width)] for cell, width in zip(row, widths)] for row in data]
	if mode == "box":
		top, middle, bottom, vertical = ("┌", "┬", "┐"), ("├", "┼", "┤"), ("└", "┴", "┘"), "│"
	else:
		top = middle = bottom = ("+", "+", "+")
		vertical = "|"
	def border(parts):
		stroke = "─" if mode == "box" else "-"
		emit(parts[0] + parts[1].join(stroke * (width + 2) for width in widths) + parts[2])
	if mode != "column":
		border(top)
	for index, row in enumerate(data):
		for line_number in range(max(len(cell) for cell in row)):
			cells = []
			for cell, width in zip(row, widths):
				line = cell[line_number] if line_number < len(cell) else ""
				cells.append(line + " " * (width - text_width(line)))
			emit("  ".join(cells).rstrip() if mode == "column" else vertical + " " + (" " + vertical + " ").join(cells) + " " + vertical)
		if headers and index == 0:
			if mode == "column":
				emit("  ".join("-" * width for width in widths))
			else:
				border(middle)
	if mode != "column":
		border(bottom)

def parse_separators(argument):
	lexer = shlex.shlex(argument, posix=True)
	lexer.whitespace_split = True
	lexer.commenters = ""
	lexer.escape = ""
	values = list(lexer)
	if len(values) not in (1, 2):
		raise ValueError("Usage: .separator <column-separator> [row-separator]")
	escapes = {"t": "\t", "n": "\n", "r": "\r", "\\": "\\"}
	return [re.sub(r"\\([tnr\\])", lambda match: escapes[match[1]], value) for value in values]

def print_results(names, rows, headers, mode, nullvalue="", column_separator=None, row_separator=None, column_widths=(), newline=None):
	ending = newline if newline is not None else "\n"
	emit = lambda text="": write_result(text + ending)
	if mode in ("json", "jsonpretty"):
		values = [[value.hex() if isinstance(value, bytes) else None if isinstance(value, float) and not math.isfinite(value) else value for value in row] for row in rows]
		if headers:
			keys = []
			for name in names:
				key, suffix = name, 2
				while key in keys or (key != name and key in names):
					key = f"{name}_{suffix}"
					suffix += 1
				keys.append(key)
			values = [dict(zip(keys, row)) for row in values]
		emit(json.dumps(values, ensure_ascii=False, allow_nan=False, indent=2 if mode == "jsonpretty" else None).replace("\n", ending))
	elif mode in ("csv", "tabs"):
		output = io.StringIO(newline="")
		separator = column_separator if column_separator is not None else "," if mode == "csv" else "\t"
		ending = row_separator if row_separator is not None else newline if newline is not None else "\r\n"
		for row in ([names] if headers else []) + rows:
			fields = []
			for value in row:
				text = result_text(value, nullvalue)
				if any(char in text for char in ('"', '\r', '\n')) or (separator and separator in text) or (ending and ending in text) or (len(row) == 1 and not text):
					text = '"' + text.replace('"', '""') + '"'
				fields.append(text)
			output.write(separator.join(fields) + ending)
		write_result(output.getvalue())
	elif mode in ("column", "table", "box"):
		print_grid(names, rows, headers, mode, nullvalue, column_widths, ending)
	elif mode == "markdown":
		def cell(value):
			return result_text(value, nullvalue).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
		emit("| " + " | ".join(cell(name) if headers else "" for name in names) + " |")
		emit("| " + " | ".join("---" for name in names) + " |")
		for row in rows:
			emit("| " + " | ".join(cell(value) for value in row) + " |")
	elif mode == "line":
		for index, row in enumerate(rows):
			if index:
				emit()
			for name, value in zip(names, row):
				text = result_text(value, nullvalue).replace("\r", "\\r").replace("\n", "\\n")
				emit(f"{name} = {text}" if headers else text)
	else:
		separator = "," if mode == "quote" else column_separator if column_separator is not None else "|"
		ending = ending if mode == "quote" or row_separator is None else row_separator
		format_value = sql_literal if mode == "quote" else lambda value: result_text(value, nullvalue)
		if headers:
			emit(separator.join(format_value(name) for name in names))
		for row in rows:
			emit(separator.join(format_value(value) for value in row))


def print_results_stream(names, cursor, headers, mode, nullvalue="", column_separator=None, row_separator=None, newline=None):
	"""Render row-oriented modes without materializing the complete result set."""
	ending = newline if newline is not None else "\n"
	result_count = 0
	if mode in ("csv", "tabs"):
		separator = column_separator if column_separator is not None else "," if mode == "csv" else "\t"
		ending = row_separator if row_separator is not None else newline if newline is not None else "\r\n"
		rows = ([names] if headers else [])
		for row in rows:
			fields = []
			for value in row:
				text = result_text(value, nullvalue)
				if any(char in text for char in ('"', '\r', '\n')) or (separator and separator in text) or (ending and ending in text) or (len(row) == 1 and not text):
					text = '"' + text.replace('"', '""') + '"'
				fields.append(text)
			write_result(separator.join(fields) + ending)
		for row in cursor:
			result_count += 1
			fields = []
			for value in row:
				text = result_text(value, nullvalue)
				if any(char in text for char in ('"', '\r', '\n')) or (separator and separator in text) or (ending and ending in text) or (len(row) == 1 and not text):
					text = '"' + text.replace('"', '""') + '"'
				fields.append(text)
			write_result(separator.join(fields) + ending)
		return result_count
	if mode == "line":
		for index, row in enumerate(cursor):
			result_count += 1
			if index:
				write_result(ending)
			for name, value in zip(names, row):
				text = result_text(value, nullvalue).replace("\r", "\\r").replace("\n", "\\n")
				write_result((f"{name} = " if headers else "") + text + ending)
		return result_count
	separator = "," if mode == "quote" else column_separator if column_separator is not None else "|"
	ending = ending if mode == "quote" or row_separator is None else row_separator
	format_value = sql_literal if mode == "quote" else lambda value: result_text(value, nullvalue)
	if headers:
		write_result(separator.join(format_value(name) for name in names) + ending)
	for row in cursor:
		result_count += 1
		write_result(separator.join(format_value(value) for value in row) + ending)
	return result_count

def print_scanstats(cursor, sql, mode, parameters=None):
	if mode == "off" or not re.match(r"\s*(SELECT|INSERT|UPDATE|DELETE|REPLACE|WITH)\b", sql, re.IGNORECASE):
		return
	try:
		if parameters:
			cursor.execute("EXPLAIN QUERY PLAN " + sql, parameters)
		else:
			cursor.execute("EXPLAIN QUERY PLAN " + sql)
		rows = cursor.fetchall()
		scans = sum("SCAN " in row[3].upper() for row in rows)
		searches = sum("SEARCH " in row[3].upper() for row in rows)
		temporary = sum("TEMP B-TREE" in row[3].upper() for row in rows)
		prefix = "Scanstats (estimated)" if mode == "est" else "Scanstats (estimated fallback)"
		suffix = "" if mode == "est" else "; runtime sqlite3_stmt_scanstatus is unavailable in Python sqlite3"
		print(f"{prefix}: loops={len(rows)} scans={scans} searches={searches} temp_btrees={temporary}{suffix}")
	except sqlite3.Error as error:
		print(f"Scanstats unavailable: {error}")


def execute_sql(cursor, sql, headers=False, mode="list", nullvalue="", column_separator=None, row_separator=None, column_widths=(), crlf=False, changes=False, error_context=None, parameters=None, timer=False, stats=False, eqp="off", explain="auto", scanstats="off", log_callback=None):
	started = time.perf_counter()
	try:
		if eqp != "off" and re.match(r"\s*(SELECT|INSERT|UPDATE|DELETE|REPLACE|WITH)\b", sql, re.IGNORECASE):
			bound = parameters if parameters else None
			try:
				cursor.execute("EXPLAIN QUERY PLAN " + sql, bound) if bound else cursor.execute("EXPLAIN QUERY PLAN " + sql)
				plan_rows = cursor.fetchall()
				print("QUERY PLAN")
				for row in plan_rows:
					print(row[3])
				if eqp == "full":
					cursor.execute("EXPLAIN " + sql, bound) if bound else cursor.execute("EXPLAIN " + sql)
					bytecode = cursor.fetchall()
					print("BYTECODE")
					for row in bytecode:
						print("|".join("" if value is None else str(value) for value in row))
			except sqlite3.Error:
				pass
		if parameters:
			cursor.execute(sql, parameters)
		else:
			cursor.execute(sql)

		result_count = 0
		if cursor.description:
			names = [column[0] for column in cursor.description]
			newline = "\r\n" if crlf and not sys.stdout.isatty() else "\n"
			formatted_mode = mode
			formatted_headers = headers
			if explain != "off" and re.match(r"\s*EXPLAIN\b", sql, re.IGNORECASE):
				formatted_mode = "column"
				formatted_headers = True
			if formatted_mode in ("list", "tabs", "csv", "line", "quote"):
				result_count = print_results_stream(names, cursor, formatted_headers, formatted_mode, nullvalue, column_separator, row_separator, newline)
			else:
				rows = cursor.fetchall()
				result_count = len(rows)
				print_results(names, rows, formatted_headers, formatted_mode, nullvalue, column_separator, row_separator, column_widths, newline)
		elif changes and cursor.rowcount >= 0:
			print(f"changes: {cursor.rowcount}")
		if timer:
			print(f"Time: {time.perf_counter() - started:.6f} seconds")
		if stats:
			changed_count = cursor.rowcount if cursor.rowcount >= 0 else 0
			print(f"Stats: rows={result_count} changes={changed_count} total_changes={cursor.connection.total_changes}")
		print_scanstats(cursor, sql, scanstats, parameters)
		return True

	except sqlite3.Error as error:
		if timer:
			print(f"Time: {time.perf_counter() - started:.6f} seconds")
		location = f"{error_context[0]}:{error_context[1]}: " if error_context else ""
		message = f"SQL error: {location}{error}"
		if log_callback:
			log_callback(message)
		print(message, file=sys.stderr)
		return False

def parameter_name(name):
	if not name or name[0] not in ":$@?":
		raise ValueError("parameter name must start with :, $, @, or ?")
	name = name[1:]
	if not name:
		raise ValueError("parameter name cannot be empty")
	return name

def parameter_value(value):
	if value.lower() == "null":
		return None
	if re.fullmatch(r"[+-]?\d+", value):
		return int(value)
	if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][+-]?\d+)?|[+-]?\d+[eE][+-]?\d+", value):
		return float(value)
	return value

def limit_name(name):
	name = name.lower().replace("-", "_")
	if name.startswith("sqlite_limit_"):
		name = name[13:]
	if name not in SQLITE_LIMITS:
		raise ValueError("unknown SQLite limit: " + name)
	return name


def parse_import_arguments(argument):
	lexer = shlex.shlex(argument, posix=True)
	lexer.whitespace_split = True
	lexer.commenters = ""
	lexer.escape = ""
	values = list(lexer)
	if len(values) != 2:
		raise ValueError("Usage: .import <file> <table>")
	return values

def import_file(cursor, filename, table_name, nullvalue="", column_separator=None):
	path = Path(filename).expanduser().resolve()
	if not path.is_file():
		raise OSError(f"file not found: {path}")
	database, name = find_table(cursor, table_name) or (None, None)
	if database is None or name is None:
		raise ValueError(f"table not found: {table_name}")
	if database == "temp":
		qualified = quote_name(name)
	else:
		qualified = f"{quote_name(database)}.{quote_name(name)}"
	columns = [row[1] for row in table_pragma(cursor, database, "table_xinfo", name) if row[6] == 0]
	if not columns:
		raise ValueError(f"table has no writable columns: {table_name}")
	delimiter = column_separator if column_separator is not None else "\t" if path.suffix.lower() in (".tsv", ".tab") else ","
	if len(delimiter) != 1:
		raise ValueError("import separator must be one character; use .separator for CSV/TSV output only")
	with path.open("r", encoding="utf-8-sig", newline="") as source:
		reader = csv.reader(source, delimiter=delimiter)
		rows = list(reader)
	if not rows:
		return 0
	if any(len(row) != len(columns) for row in rows):
		bad = next(row for row in rows if len(row) != len(columns))
		raise ValueError(f"expected {len(columns)} columns, got {len(bad)}")
	placeholders = ", ".join("?" for column in columns)
	column_sql = ", ".join(quote_name(column) for column in columns)
	cursor.execute("BEGIN")
	try:
		cursor.executemany(f"INSERT INTO {qualified} ({column_sql}) VALUES ({placeholders})", [[None if nullvalue and value == nullvalue else value for value in row] for row in rows])
		cursor.execute("COMMIT")
	except Exception:
		cursor.execute("ROLLBACK")
		raise
	return len(rows)

def schema_rows(cursor, pattern="%"):
	rows = []
	shadows = shadow_tables(cursor)
	for database, kind, name, table, sql in database_objects(cursor):
		if sql is None or name.startswith("sqlite_") or (database, name) in shadows:
			continue
		cursor.execute("SELECT ? LIKE ? OR ? LIKE ? OR ? LIKE ? OR ? LIKE ?", (name, pattern, table, pattern, f"{database}.{name}", pattern, f"{database}.{table}", pattern))
		if cursor.fetchone()[0]:
			rows.append((database, kind, name, sql))
	order = {"table": 0, "index": 1, "view": 2, "trigger": 3}
	return sorted(rows, key=lambda row: (order[row[1]], row[0], row[2]))

def pale_dump(cursor, object_names=()):
	objects = [row for row in database_objects(cursor) if row[0] == "main" and not row[2].startswith("sqlite_")]
	shadows = shadow_tables(cursor)
	objects = [row for row in objects if (row[0], row[2]) not in shadows]
	if object_names:
		wanted = {identifier_key(name) for name in object_names}
		objects = [row for row in objects if identifier_key(row[2]) in wanted or identifier_key(row[3]) in wanted]
	if not objects:
		print("No objects found", file=sys.stderr if not sys.stdout.isatty() else sys.stdout)
		return

	tables = [row for row in objects if row[1] == "table" and row[4]]
	indexes = [row for row in objects if row[1] == "index" and row[4]]
	triggers = [row for row in objects if row[1] == "trigger" and row[4]]
	views = [row for row in objects if row[1] == "view" and row[4]]
	print("BEGIN TRANSACTION;")
	for database, kind, name, table, sql in sorted(tables, key=lambda row: row[2]):
		print(sql.rstrip().rstrip(";") + ";")
	for database, kind, name, table, sql in sorted(tables, key=lambda row: row[2]):
		columns = [row[1] for row in table_pragma(cursor, database, "table_xinfo", name) if row[6] == 0]
		if not columns:
			continue
		column_sql = ", ".join(quote_name(column) for column in columns)
		cursor.execute(f"SELECT {column_sql} FROM {quote_name(name)}")
		for values in cursor.fetchall():
			literals = ", ".join(sql_literal(value) for value in values)
			print(f"INSERT INTO {quote_name(name)} ({column_sql}) VALUES ({literals});")
	for database, kind, name, table, sql in sorted(indexes, key=lambda row: row[2]):
		print(sql.rstrip().rstrip(";") + ";")
	for database, kind, name, table, sql in sorted(triggers, key=lambda row: row[2]):
		print(sql.rstrip().rstrip(";") + ";")
	for database, kind, name, table, sql in sorted(views, key=lambda row: row[2]):
		print(sql.rstrip().rstrip(";") + ";")
	print("COMMIT;")


def recovery_corruption(error):
	code = getattr(error, "sqlite_errorcode", None)
	return (code is not None and code & 255 in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)) or any(
		phrase in str(error).lower() for phrase in ("malformed", "corrupt", "not a database")
	)


def pale_recover(connection):
	"""Emit a restorable SQL script from data SQLite can still read."""
	cursor = connection.cursor()
	try:
		cursor.execute("SELECT type, name, tbl_name, sql FROM main.sqlite_schema WHERE sql IS NOT NULL ORDER BY name")
		objects = cursor.fetchall()
		shadows = shadow_tables(cursor)
	except sqlite3.Error as error:
		raise ValueError(f"cannot read database schema: {error}") from error
	finally:
		cursor.close()
	has_sequence = any(row[1] == "sqlite_sequence" for row in objects)
	objects = [row for row in objects if not row[1].startswith("sqlite_") and ("main", row[1]) not in shadows]
	tables = sorted((row for row in objects if row[0] == "table"), key=lambda row: row[1])
	later = sorted((row for row in objects if row[0] in ("index", "trigger", "view")), key=lambda row: ({"index": 0, "trigger": 1, "view": 2}[row[0]], row[1]))
	incomplete = False
	print("-- PaleSQL best-effort recovery: only data readable by SQLite can be extracted.")
	print("PRAGMA foreign_keys=OFF;")
	print("BEGIN TRANSACTION;")
	for kind, name, table, sql in tables:
		print(sql.rstrip().rstrip(";") + ";")
	for kind, name, table, sql in tables:
		qualified = quote_name(name)
		cursor = connection.cursor()
		try:
			column_info = table_pragma(cursor, "main", "table_xinfo", name)
			columns = [row[1] for row in column_info if row[6] == 0]
			if not columns:
				continue
			column_sql = ", ".join(quote_name(column) for column in columns)
			insert = f"INSERT INTO {qualified} ({column_sql}) VALUES"
			declared = {identifier_key(row[1]) for row in column_info}
			aliases = [alias for alias in ("rowid", "_rowid_", "oid") if identifier_key(alias) not in declared]
			rowid = None if re.search(r"\bWITHOUT\s+ROWID\b", sql, re.IGNORECASE) else (aliases[0] if aliases else None)
			if rowid:
				insert = f"INSERT INTO {qualified} ({rowid}, {column_sql}) VALUES"
				try:
					cursor.execute(f"SELECT {rowid} FROM {qualified} ORDER BY {rowid} LIMIT 1")
					first = cursor.fetchone()
					cursor.execute(f"SELECT {rowid} FROM {qualified} ORDER BY {rowid} DESC LIMIT 1")
					last = cursor.fetchone()
				except sqlite3.Error as error:
					if not recovery_corruption(error):
						raise
					first, last = (-2**63,), (2**63 - 1,)
				if first is None:
					continue
				pending = [(first[0], last[0])]
				failures = 0
				while pending:
					low, high = pending.pop()
					last_emitted = low - 1
					scan = connection.cursor()
					try:
						scan.execute(f"SELECT {rowid}, {column_sql} FROM {qualified} WHERE {rowid} BETWEEN ? AND ? ORDER BY {rowid}", (low, high))
						while batch := scan.fetchmany(512):
							for values in batch:
								last_emitted = values[0]
								print(f"{insert} ({', '.join(sql_literal(value) for value in values)});")
					except sqlite3.Error as error:
						if not recovery_corruption(error):
							raise
						incomplete = True
						failures += 1
						remaining = max(low, last_emitted + 1)
						if remaining < high and failures < 1024:
							middle = remaining + (high - remaining) // 2
							pending.extend(((middle + 1, high), (remaining, middle)))
					finally:
						scan.close()
				if failures >= 1024:
						print(f"Recover warning: {name}: stopped after 1024 unreadable ranges", file=sys.stderr)
						break
				if failures:
					print(f"Recover warning: {name}: some rowid ranges were unreadable", file=sys.stderr)
			else:
				try:
					cursor.execute(f"SELECT {column_sql} FROM {qualified}")
					while batch := cursor.fetchmany(512):
						for values in batch:
							print(f"{insert} ({', '.join(sql_literal(value) for value in values)});")
				except sqlite3.Error as error:
					if not recovery_corruption(error):
						raise
					incomplete = True
					print(f"Recover warning: {name}: stopped at unreadable data ({error})", file=sys.stderr)
		except sqlite3.Error as error:
			incomplete = True
			print(f"Recover warning: {name}: could not read table ({error})", file=sys.stderr)
		finally:
			cursor.close()
	if has_sequence:
		cursor = connection.cursor()
		try:
			cursor.execute("SELECT name, seq FROM main.sqlite_sequence")
			sequences = cursor.fetchall()
			print("DELETE FROM sqlite_sequence;")
			for name, sequence in sequences:
				print(f"INSERT INTO sqlite_sequence(name, seq) VALUES ({sql_literal(name)}, {sql_literal(sequence)});")
		except sqlite3.Error as error:
			incomplete = True
			print(f"Recover warning: sqlite_sequence: could not read sequence values ({error})", file=sys.stderr)
		finally:
			cursor.close()
	for kind, name, table, sql in later:
		print(sql.rstrip().rstrip(";") + ";")
	print("COMMIT;")
	return not incomplete


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
		for key, row in before_rows.items() - after_rows.keys():
			where = " AND ".join(f"{quote_name(column)} = {sql_literal(value)}" for column, value in zip(before["keys"], key))
			changes.append(f"DELETE FROM {quote_name(table)} WHERE {where};")
		for key, row in after_rows.items() - before_rows.keys():
			values = ", ".join(sql_literal(value) for value in row)
			changes.append(f"INSERT INTO {quote_name(table)} ({column_sql}) VALUES ({values});")
		for key in before_rows.keys() & after_rows.keys():
			if before_rows[key] == after_rows[key]:
				continue
			where = " AND ".join(f"{quote_name(column)} = {sql_literal(value)}" for column, value in zip(before["keys"], key))
			values = ", ".join(f"{quote_name(column)} = {sql_literal(value)}" for column, value in zip(before["columns"], after_rows[key]))
			changes.append(f"UPDATE {quote_name(table)} SET {values} WHERE {where};")
	return changes


def pale_session(connection, argument, state):
	values = shlex.split(argument, posix=False)
	values = [value[1:-1] if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0] else value for value in values]
	if not values:
		print("Usage: .session open NAME|attach TABLE|detach TABLE|list|isempty|changeset FILE|patchset FILE|enable|disable|close")
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
		print(f"{state['name']} ({'enabled' if state['enabled'] else 'disabled'}): {', '.join(sorted(state['tables'])) or 'no tables'}")
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
		changes = session_changes(connection, state)
		print("yes" if not changes else "no")
		return state
	if action in ("changeset", "patchset") and len(values) == 2:
		changes = session_changes(connection, state)
		Path(values[1]).expanduser().resolve().write_text("BEGIN;\n" + "\n".join(changes) + "\nCOMMIT;\n", encoding="utf-8")
		print(f"{action} written: {values[1]} ({len(changes)} changes)")
		return state
	if action == "close" and len(values) == 1:
		print(f"Session '{state['name']}' closed")
		return None
	raise ValueError("Usage: .session open NAME|attach TABLE|detach TABLE|list|isempty|changeset FILE|patchset FILE|enable|disable|close")


def parse_archive_arguments(argument):
	lexer = shlex.shlex(argument, posix=True)
	lexer.whitespace_split = True
	lexer.commenters = ""
	values = list(lexer)
	action = None
	archive = "archive.sqlar"
	root = Path.cwd()
	pattern = None
	verbose = False
	dryrun = False
	files = []
	index = 0
	actions = {"-c": "create", "--create": "create", "-u": "update", "--update": "update", "-i": "insert", "--insert": "insert", "-r": "remove", "--remove": "remove", "-t": "list", "--list": "list", "-x": "extract", "--extract": "extract"}
	while index < len(values):
		value = values[index]
		if value in actions:
			if action is not None:
				raise ValueError("choose one archive action: -c, -u, -i, -r, -t, or -x")
			action = actions[value]
		elif value in ("-f", "--file"):
			index += 1
			if index >= len(values) or not values[index]:
				raise ValueError("Usage: .archive [options] [files]")
			archive = values[index]
		elif value in ("-C", "--directory"):
			index += 1
			if index >= len(values) or not values[index]:
				raise ValueError("Usage: .archive [options] [files]")
			root = Path(values[index]).expanduser().resolve()
		elif value in ("--glob",):
			index += 1
			if index >= len(values) or not values[index]:
				raise ValueError("Usage: .archive [options] [files]")
			pattern = values[index]
		elif value in ("-v", "--verbose"):
			verbose = True
		elif value in ("-n", "--dryrun"):
			dryrun = True
		elif value.startswith("-"):
			raise ValueError(f"unknown archive option: {value}")
		else:
			files.append(value)
		index += 1
	if action is None:
		action = "list"
	return action, Path(archive).expanduser().resolve(), root, pattern, verbose, dryrun, files


def pale_archive(argument):
	action, archive, root, pattern, verbose, dryrun, files = parse_archive_arguments(argument)
	if action in ("create", "update", "insert") and not files:
		raise ValueError("archive action requires at least one file")
	if action in ("list", "remove", "extract") and files and pattern:
		raise ValueError("use either archive names or --glob, not both")
	if action == "extract" and not root.is_dir():
		if dryrun:
			print(f"Would create directory: {root}")
		else:
			root.mkdir(parents=True, exist_ok=True)
	if not archive.exists() and action in ("list", "remove", "extract", "update", "insert"):
		raise ValueError(f"archive not found: {archive}")
	if dryrun and action in ("create", "update", "insert") and not archive.exists():
		print(f"Would create archive: {archive}")
		return
	db = sqlite3.connect(str(archive))
	try:
		db.execute("CREATE TABLE IF NOT EXISTS sqlar(name TEXT PRIMARY KEY, mode INT, mtime INT, sz INT, data BLOB)")
		if action == "create":
			if not dryrun:
				db.execute("DELETE FROM sqlar")
			else:
				print(f"Would clear archive: {archive}")
		if action in ("create", "update", "insert"):
			for source_name in files:
				path = (root / source_name).resolve() if not Path(source_name).is_absolute() else Path(source_name).resolve()
				if path == archive or (path.exists() and archive.exists() and path == archive.resolve()):
					raise ValueError("archive cannot contain itself")
				if not path.exists():
					raise OSError(f"archive source not found: {path}")
				paths = [path] if path.is_file() else [item for item in path.rglob("*") if item.is_file()]
				for item in paths:
					name = item.relative_to(root).as_posix() if item.is_relative_to(root) else item.name
					stat = item.stat()
					if dryrun:
						print(f"Would add: {name}")
						continue
					data = item.read_bytes()
					db.execute("INSERT OR REPLACE INTO sqlar(name, mode, mtime, sz, data) VALUES (?, ?, ?, ?, ?)", (name, stat.st_mode, int(stat.st_mtime), len(data), data))
					if verbose:
						print(f"Added: {name} ({len(data)} bytes)")
		elif action == "list":
				rows = db.execute("SELECT name, mode, mtime, sz FROM sqlar ORDER BY name").fetchall()
				for name, mode, mtime, size in rows:
					if pattern and not fnmatch.fnmatchcase(name, pattern):
						continue
					print(f"{name}\t{size}")
		elif action == "remove":
				names = files or [row[0] for row in db.execute("SELECT name FROM sqlar") if pattern and fnmatch.fnmatchcase(row[0], pattern)]
				for name in names:
					if dryrun:
						print(f"Would remove: {name}")
					else:
						db.execute("DELETE FROM sqlar WHERE name = ?", (name,))
		elif action == "extract":
				rows = db.execute("SELECT name, mode, mtime, sz, data FROM sqlar ORDER BY name").fetchall()
				for name, mode, mtime, size, data in rows:
					if files and name not in files or pattern and not fnmatch.fnmatchcase(name, pattern):
						continue
					destination = (root / Path(name)).resolve()
					if destination != root and root not in destination.parents:
						raise ValueError(f"unsafe archive path: {name}")
					if dryrun:
						print(f"Would extract: {name}")
						continue
					destination.parent.mkdir(parents=True, exist_ok=True)
					destination.write_bytes(data)
					try:
						os.chmod(destination, mode & 0o777)
					except OSError:
						pass
					if verbose:
						print(f"Extracted: {name}")
		if not dryrun:
			db.commit()
	finally:
		db.close()


def parse_backup_arguments(argument, command=".backup"):
	lexer = shlex.shlex(argument, posix=True)
	lexer.whitespace_split = True
	lexer.commenters = ""
	lexer.escape = ""
	values = list(lexer)
	if len(values) not in (1, 2) or not values[-1]:
		raise ValueError(f"Usage: {command} [database] <file>")
	return ("main", values[0]) if len(values) == 1 else tuple(values)

def parse_single_file_argument(argument, command):
	lexer = shlex.shlex(argument, posix=True)
	lexer.whitespace_split = True
	lexer.commenters = ""
	lexer.escape = ""
	values = list(lexer)
	if len(values) != 1 or not values[0]:
		raise ValueError(f"Usage: {command} <file>")
	return values[0]

def backup_database(con, cursor, database_name, filename, protected=()):
	database = next((row[1] for row in get_databases(cursor) if identifier_key(row[1]) == identifier_key(database_name)), None)
	if database is None:
		raise ValueError(f"database not found: {database_name}")
	path = Path(filename).expanduser().resolve()
	if any(path == item or (path.exists() and item.exists() and path.samefile(item)) for item in protected):
		raise ValueError("Backup file must not overwrite an open database or active script")
	destination = sqlite3.connect(str(path))
	try:
		con.backup(destination, name=database)
	finally:
		destination.close()

def restore_database(con, cursor, database_name, filename):
	path = Path(filename).expanduser().resolve()
	if not path.is_file():
		raise OSError(f"file not found: {path}")
	databases = get_databases(cursor)
	database = next((row for row in databases if identifier_key(row[1]) == identifier_key(database_name)), None)
	if database is None:
		raise ValueError(f"database not found: {database_name}")
	if database[2] and path == Path(database[2]).resolve():
		raise ValueError("Restore file must differ from the destination database")
	source = sqlite3.connect(str(path))
	try:
		if database[1] == "main":
			source.backup(con, name="main")
		else:
			con.execute(f"DETACH DATABASE {quote_name(database[1])}")
			destination = sqlite3.connect(database[2])
			try:
				source.backup(destination, name="main")
			finally:
				destination.close()
			con.execute(f"ATTACH DATABASE ? AS {quote_name(database[1])}", (database[2],))
	finally:
		source.close()

def pale_dbtotxt(cursor):
	database = next((row for row in get_databases(cursor) if row[1] == "main"), None)
	if database is None or not database[2]:
		raise ValueError("main database has no physical file")
	path = Path(database[2])
	if not path.is_file():
		raise OSError(f"database file not found: {path}")
	with path.open("rb") as source:
		offset = 0
		while chunk := source.read(16):
			hex_bytes = " ".join(f"{byte:02x}" for byte in chunk)
			hex_bytes = f"{hex_bytes:<47}"
			ascii_bytes = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
			print(f"{offset:08x}  {hex_bytes}  |{ascii_bytes}|")
			offset += len(chunk)


def pale_sha3sum(connection, pattern=""):
	"""Print a deterministic SHA3-256 digest of the selected logical database content."""
	pattern = pattern or "%"
	hasher = hashlib.sha3_256()
	objects = connection.execute(
		"SELECT type, name, tbl_name, sql FROM main.sqlite_schema "
		"WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
		"AND (name LIKE ? OR tbl_name LIKE ?) ORDER BY type, name",
		(pattern, pattern),
	).fetchall()
	if not objects:
		print(f"sha3sum: {hasher.hexdigest()}")
		return

	def add(value):
		if value is None:
			encoded = b"N"
		elif isinstance(value, bytes):
			encoded = b"B" + value
		elif isinstance(value, str):
			encoded = b"T" + value.encode("utf-8")
		elif isinstance(value, float):
			encoded = b"F" + repr(value).encode("ascii")
		else:
			encoded = b"I" + str(value).encode("ascii")
		hasher.update(len(encoded).to_bytes(8, "big"))
		hasher.update(encoded)

	for kind, name, table, sql in objects:
		add(kind)
		add(name)
		add(table)
		add(sql)
		if kind != "table":
			continue
		try:
			info = connection.execute(f"PRAGMA main.table_xinfo({quote_name(name)})").fetchall()
			columns = [row[1] for row in info if row[6] == 0]
			if not columns:
				continue
			column_sql = ", ".join(quote_name(column) for column in columns)
			without_rowid = bool(re.search(r"\bWITHOUT\s+ROWID\b", sql or "", re.IGNORECASE))
			order = " ORDER BY rowid" if not without_rowid else ""
			rows = connection.execute(f"SELECT {column_sql} FROM {quote_name(name)}{order}")
			for row in rows:
				for value in row:
					add(value)
		except sqlite3.Error as error:
			raise ValueError(f"cannot hash table '{name}': {error}") from error
	print(f"sha3sum: {hasher.hexdigest()}")


def schema_sql(database, kind, name, sql):
	# Qualify the declared object, leaving its body and string literals intact.
	space = r"(?:\s|/\*[\s\S]*?\*/|--[^\n]*(?:\n|$))+"
	prefix = rf"\A\s*CREATE{space}(?:TEMP(?:ORARY)?{space})?(?:(?P<modifier>VIRTUAL|UNIQUE){space})?(?:TABLE|INDEX|VIEW|TRIGGER){space}(?:IF{space}NOT{space}EXISTS{space})?"
	identifier = r'''(?:"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\]|'(?:''|[^'])*'|(?:(?!/\*|--)[^\s(;])+)'''
	match = re.match(prefix + identifier, sql, re.IGNORECASE)
	if match is None:
		raise ValueError(f"Cannot read schema for {database}.{name}")
	modifier = match.group("modifier")
	start = "CREATE " + (modifier.upper() + " " if modifier else "") + kind.upper() + " "
	return start + quote_name(database) + "." + quote_name(name) + sql[match.end():].rstrip().rstrip(";") + ";"


def pale_schema(cursor, pattern=""):
	rows = schema_rows(cursor, pattern or "%")
	if not rows:
		print(f"-- No schema found for '{pattern}'" if pattern else "-- No schema found")
	for database, kind, name, sql in rows:
		print(schema_sql(database, kind, name, sql))


def pale_lint(cursor):
	objects = database_objects(cursor, "table")
	tables = {(identifier_key(database), identifier_key(name)) for database, kind, name, table, sql in objects}
	issues = 0
	has_foreign_keys = False

	for database, kind, table, unused, sql in objects:
		foreign_keys = table_pragma(cursor, database, "foreign_key_list", table)
		if not foreign_keys:
			continue
		has_foreign_keys = True
		label = object_label(database, table)
		try:
			violations = table_pragma(cursor, database, "foreign_key_check", table)
			for child, rowid, parent, key_id in violations:
				print(f"Foreign key violation: {label}, rowid {rowid}, key {key_id}, parent {parent}")
				issues += 1
		except sqlite3.Error as error:
			print(f"Foreign key error in '{label}': {error}")
			issues += 1
			continue

		keys = {}
		for row in foreign_keys:
			keys.setdefault(row[0], []).append(row)
		for key_id, rows in keys.items():
			rows.sort(key=lambda row: row[1])
			parent = rows[0][2]
			if (identifier_key(database), identifier_key(parent)) not in tables:
				print(f"Missing parent table: '{label}', key {key_id}, parent '{parent}'")
				issues += 1
				continue
			columns = table_pragma(cursor, database, "table_xinfo", parent)
			primary_key = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
			parent_columns = [row[4] for row in rows]
			if all(name is None for name in parent_columns):
				parent_columns = primary_key
			conditions = []
			for row, parent_column in zip(rows, parent_columns):
				conditions.append(f"p.{quote_name(parent_column)} = c.{quote_name(row[3])}")
				conditions.append(f"p.{quote_name(parent_column)} = ?")
			query = f"SELECT 1 FROM {quote_name(database)}.{quote_name(parent)} AS p CROSS JOIN {quote_name(database)}.{quote_name(table)} AS c WHERE " + " AND ".join(conditions)
			try:
				cursor.execute("EXPLAIN QUERY PLAN " + query, (None,) * len(rows))
				plan = cursor.fetchall()
				if not any(row[3].startswith("SEARCH c ") and "AUTOMATIC" not in row[3] for row in plan):
					names = ", ".join(row[3] for row in rows)
					print(f"Consider an index: '{label}' ({names}), foreign key {key_id}; lookup scans the table (check index columns and collations)")
					issues += 1
			except sqlite3.Error as error:
				print(f"Cannot check index for '{label}', key {key_id}: {error}")
				issues += 1

	cursor.execute("PRAGMA foreign_keys;")
	if has_foreign_keys and not cursor.fetchone()[0]:
		print("Foreign key enforcement is disabled")
		issues += 1
	if not issues:
		print("No foreign key or index issues found")


def pale_integrity_check(cursor):
	"""Run SQLite's full on-disk consistency check for the main database."""
	try:
		cursor.execute("PRAGMA integrity_check")
		results = cursor.fetchall()
	except sqlite3.Error as error:
		raise ValueError(f"integrity check failed: {error}") from error
	for result in results:
		print(result[0] if result else "")


def pale_incremental_integrity_check(cursor):
	"""Check each user table independently so large databases are bounded per step."""
	try:
		cursor.execute("SELECT name FROM main.sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
		tables = [row[0] for row in cursor.fetchall()]
		issues = []
		for table in tables:
			pragma_name = table.replace("'", "''")
			cursor.execute(f"PRAGMA quick_check('{pragma_name}')")
			issues.extend(result[0] for result in cursor.fetchall() if result and result[0] != "ok")
	except sqlite3.Error as error:
		raise ValueError(f"incremental integrity check failed: {error}") from error
	for issue in issues:
		print(issue)
	if not issues:
		print("ok")


def pale_selftest(connection):
	"""Run a compact, read-only health check over the current database."""
	issues = []
	cursor = connection.cursor()
	try:
		try:
			cursor.execute("PRAGMA quick_check")
			quick = [row[0] for row in cursor.fetchall()]
			if quick != ["ok"]:
				issues.extend(f"quick_check: {result}" for result in quick)
		except sqlite3.Error as error:
			issues.append(f"quick_check: {error}")

		try:
			cursor.execute("PRAGMA foreign_key_check")
			foreign_issues = cursor.fetchall()
			issues.extend(f"foreign key violation: {row}" for row in foreign_issues)
		except sqlite3.Error as error:
			issues.append(f"foreign_key_check: {error}")

		try:
			cursor.execute("SELECT type, name, sql FROM main.sqlite_schema WHERE sql IS NOT NULL ORDER BY name")
			objects = cursor.fetchall()
			tables = [name for kind, name, sql in objects if kind == "table" and not name.startswith("sqlite_")]
			for table in tables:
				cursor.execute(f"SELECT count(*) FROM main.{quote_name(table)}")
				cursor.fetchone()
				cursor.execute(f"PRAGMA main.table_xinfo({quote_name(table)})")
				cursor.fetchall()
		except sqlite3.Error as error:
			issues.append(f"schema/data read: {error}")
	finally:
		cursor.close()

	if issues:
		print("Self-test: FAILED")
		for issue in issues:
			print(f"  {issue}")
		return False
	print("Self-test: OK")
	return True


def pale_fullschema(cursor):
	for seq, database, file in get_databases(cursor):
		if database not in ("main", "temp"):
			print(f"ATTACH DATABASE ':memory:' AS {quote_name(database)};")
	pale_schema(cursor)
	found = False
	for database, kind, name, table, sql in database_objects(cursor, "table"):
		if not name.startswith("sqlite_stat"):
			continue
		found = True
		qualified = quote_name(database) + "." + quote_name(name)
		print(f"ANALYZE {quote_name(database)}.sqlite_schema;")
		print(f"DELETE FROM {qualified};")
		cursor.execute(f"SELECT * FROM {qualified}")
		rows = cursor.fetchall()
		for row in rows:
			values = []
			for value in row:
				if isinstance(value, str) and "\x00" in value:
					values.append("CAST(X'" + value.encode("utf-8").hex() + "' AS TEXT)")
				else:
					cursor.execute("SELECT quote(?)", (value,))
					values.append(cursor.fetchone()[0])
			print(f"INSERT INTO {qualified} VALUES ({', '.join(values)});")
		print(f"ANALYZE {quote_name(database)}.sqlite_schema;")
	if not found:
		print("-- No statistics found")


def pale_columns(cursor, table_name):
	table = find_table(cursor, table_name)
	if table is None:
		print(f"Table '{table_name}' not found")
		return
	database, name = table
	rows = table_pragma(cursor, database, "table_xinfo", name)

	print(f"{'name':<20} | {'type':<12} | {'NOT NULL':<8} | {'DEFAULT':<20} | {'PRIMARY KEY':<11} | kind")

	for cid, name, column_type, not_null, default, primary_key, hidden in rows:
		default = "(none)" if default is None else default
		kind = {0: "normal", 1: "hidden", 2: "generated virtual", 3: "generated stored"}[hidden]
		print(f"{name:<20} | {column_type:<12} | {not_null:<8} | {default:<20} | {primary_key:<11} | {kind}")


def pale_foreignkeys(cursor, table_name):
	table = find_table(cursor, table_name)
	if table is None:
		print(f"Table '{table_name}' not found")
		return
	database, name = table
	rows = table_pragma(cursor, database, "foreign_key_list", name)

	if not rows:
		print("No foreign keys found")
		return

	print(f"{'id':<5} | {'seq':<5} | {'table':<20} | {'from':<20} | {'to':<20} | {'ON UPDATE':<12} | {'ON DELETE':<12} | MATCH")

	for key_id, seq, table, from_column, to_column, on_update, on_delete, match in rows:
		to_column = "NULL" if to_column is None else to_column
		print(f"{key_id:<5} | {seq:<5} | {table:<20} | {from_column:<20} | {to_column:<20} | {on_update:<12} | {on_delete:<12} | {match}")


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


def pale_describe(cursor, table_name):
	table = find_table(cursor, table_name)
	if table is None:
		print(f"Table '{table_name}' not found")
		return
	database, name = table
	objects = database_objects(cursor)
	create_sql = next((row[4] for row in objects if row[0] == database and row[2] == name and row[1] == "table"), None)
	print(f"Table: {object_label(database, name)}")
	print("Columns:")
	for cid, column, column_type, not_null, default, primary_key, hidden in table_pragma(cursor, database, "table_xinfo", name):
		flags = []
		if not_null:
			flags.append("NOT NULL")
		if primary_key:
			flags.append(f"PK {primary_key}")
		if default is not None:
			flags.append(f"DEFAULT {default}")
		if hidden:
			flags.append("hidden")
		details = " " + " ".join(flags) if flags else ""
		print(f"  {column} {column_type or '""'}{details}".rstrip())
	print("Foreign keys:")
	foreign_keys = table_pragma(cursor, database, "foreign_key_list", name)
	if foreign_keys:
		for key_id, seq, target, source, destination, on_update, on_delete, match in foreign_keys:
			print(f"  {source} -> {target}.{destination or '(rowid)'} ON UPDATE {on_update} ON DELETE {on_delete}")
	else:
		print("  none")
	print("Indexes:")
	indexes = [row for row in objects if row[0] == database and row[1] == "index" and identifier_key(row[3]) == identifier_key(name)]
	if indexes:
		for _, _, index_name, _, sql in indexes:
			print(f"  {index_name}{' (implicit)' if sql is None else ''}")
	else:
		print("  none")
	print("CREATE SQL:")
	print(f"  {create_sql or '(unavailable)'}")


def pale_schemajson(cursor):
	objects = []
	for database, kind, name, table, sql in database_objects(cursor):
		objects.append({"database": database, "type": kind, "name": name, "table": table, "sql": sql})
	print(json.dumps(objects, ensure_ascii=False, indent=2))


def pale_find(cursor, needle):
	needle = str(needle)
	matches = []
	shadows = shadow_tables(cursor)
	for database, kind, name, table, sql in database_objects(cursor, "table"):
		if name.startswith("sqlite_") or (database, name) in shadows:
			continue
		columns = [row[1] for row in table_pragma(cursor, database, "table_xinfo", name) if not row[6] and (not row[2] or "CHAR" in row[2].upper() or "TEXT" in row[2].upper() or "CLOB" in row[2].upper())]
		if not columns:
			continue
		select = ", ".join(quote_name(column) for column in columns)
		try:
			cursor.execute(f"SELECT {select} FROM {quote_name(database)}.{quote_name(name)}")
			for row in cursor.fetchall():
				for column, value in zip(columns, row):
					if value is not None and needle.casefold() in str(value).casefold():
						matches.append((object_label(database, name), column, value))
		except sqlite3.Error:
			continue
	if matches:
		for table, column, value in matches:
			print(f"{table}.{column}: {value}")
	else:
		print("No matches")


def pale_count(cursor, table_name=""):
	if table_name:
		table = find_table(cursor, table_name)
		if table is None:
			print(f"Table '{table_name}' not found")
			return
		database, name = table
		count = cursor.execute(f"SELECT count(*) FROM {quote_name(database)}.{quote_name(name)}").fetchone()[0]
		print(f"{object_label(database, name)}: {count}")
		return
	rows = []
	shadows = shadow_tables(cursor)
	for database, kind, name, table, sql in database_objects(cursor, "table"):
		if name.startswith("sqlite_") or (database, name) in shadows:
			continue
		count = cursor.execute(f"SELECT count(*) FROM {quote_name(database)}.{quote_name(name)}").fetchone()[0]
		rows.append((object_label(database, name), count))
	if rows:
		for name, count in rows:
			print(f"{name}: {count}")
	else:
		print("No tables found")


def pale_tablesize(cursor, table_name=""):
	if table_name:
		table = find_table(cursor, table_name)
		if table is None:
			print(f"Table '{table_name}' not found")
			return
		database, name = table
		count = cursor.execute(f"SELECT count(*) FROM {quote_name(database)}.{quote_name(name)}").fetchone()[0]
		print(f"{object_label(database, name)}: {count} rows")
		return
	presented = False
	for database, kind, name, table, sql in database_objects(cursor, "table"):
		if name.startswith("sqlite_"):
			continue
		count = cursor.execute(f"SELECT count(*) FROM {quote_name(database)}.{quote_name(name)}").fetchone()[0]
		print(f"{object_label(database, name)}: {count} rows")
		presented = True
	for _, database, file in get_databases(cursor):
		if file:
			try:
				print(f"{database} file size: {os.path.getsize(file)} bytes")
			except OSError:
				print(f"{database} file size: unavailable")
	if not presented:
		print("No tables found")


def sql_context(sql):
	state, has_sql, index = "", False, 0
	while index < len(sql):
		char = sql[index]
		pair = sql[index:index + 2]
		if state == "--":
			if char == "\n":
				state = ""
		elif state == "/*":
			if pair == "*/":
				state = ""
				index += 1
		elif state:
			if char == state:
				if state != "]" and pair == state * 2:
					index += 1
				else:
					state = ""
		elif pair in ("--", "/*"):
			state = pair
			index += 1
		elif char in ("'", '"', "`", "["):
			state = "]" if char == "[" else char
			has_sql = True
		elif not char.isspace() and char != ";":
			has_sql = True
		index += 1
	return state, has_sql


def main():
	interactive = sys.stdin.isatty() and sys.stdout.isatty()
	if interactive:
		print(
			f"\nhaiiiiiiiiii :3 rawr certified SQL CLI :p\n"
			f"PaleSQL {PALESQL_VERSION} by Shikaru Software\n"
			f"Type .help for commands.\n"
		)

	sql = ""
	con = None
	cursor = None
	connections = []
	active_connection = None
	headers = False
	mode = "list"
	nullvalue = ""
	changes = False
	bail = False
	echo = False
	parameters = {}
	timer = False
	stats = False
	scanstats = "off"
	progress_calls = 0
	trace_mode = "off"
	trace_file = None
	log_mode = "off"
	log_file = None
	eqp = "off"
	explain = "auto"
	expert_options = None
	column_separator = None
	row_separator = None
	column_widths = ()
	crlf = False
	safe_mode = False
	safe_nonce = None
	auth_enabled = False
	session_state = None
	www_pending = False
	testcase_name = None
	testcase_output = io.StringIO()
	history = []
	main_prompt = "PaleSQL: "
	continue_prompt = "    ...: "
	failed = False
	scripts = []
	source_context = None
	console = sys.stdout
	output_file = None
	once_file = None

	def protected_backup_paths(include_sidecars=True):
		paths = [active[0] for active in scripts]
		for seq, name, file in get_databases(cursor):
			if file:
				suffixes = ("", "-wal", "-shm", "-journal") if include_sidecars else ("",)
				paths.extend(Path(file + suffix).resolve() for suffix in suffixes)
		for handle in (output_file, once_file):
			if handle:
				paths.append(Path(handle.name).resolve())
		return paths

	def progress_handler():
		nonlocal progress_calls
		progress_calls += 1
		print(f"Progress {progress_calls}")
		return 0

	def trace_callback(statement):
		target = trace_file if trace_mode == "file" else sys.stderr if trace_mode == "stderr" else sys.stdout
		try:
			target.write(f"Trace: {statement}\n")
			target.flush()
		except (AttributeError, OSError, ValueError):
			pass

	def install_trace():
		if not hasattr(con, "set_trace_callback"):
			raise ValueError("this SQLite connection does not support tracing")
		con.set_trace_callback(trace_callback)

	def authorizer_callback(action, arg1, arg2, database, source):
		if auth_enabled:
			print(f"Authorizer: action={action} arg1={arg1 or ''} arg2={arg2 or ''} database={database or ''} source={source or ''}")
		return sqlite3.SQLITE_OK

	def set_authorizer(enabled):
		if con is None or not hasattr(con, "set_authorizer"):
			raise ValueError("this Python sqlite3 does not support authorizer callbacks")
		con.set_authorizer(authorizer_callback if enabled else None)

	def clear_trace():
		nonlocal trace_file, trace_mode
		if con is not None and hasattr(con, "set_trace_callback"):
			con.set_trace_callback(None)
		if trace_file:
			trace_file.close()
			trace_file = None
		trace_mode = "off"

	def log_callback(message):
		target = log_file if log_mode == "file" else console if log_mode == "stdout" else sys.stderr
		try:
			target.write(f"SQLite log: {message}\n")
			target.flush()
		except (AttributeError, OSError, ValueError):
			pass

	def clear_log():
		nonlocal log_file, log_mode
		if log_file:
			log_file.close()
			log_file = None
		log_mode = "off"

	def finish_once():
		nonlocal once_file, output_file
		if once_file:
			once_file.close()
			once_file = None
			if output_file:
				output_file.close()
				output_file = None
			sys.stdout = console

	def run_expert(statement, options):
		nonlocal failed
		try:
			report = analyze_expert(con, statement, sample=options[0], verbose=options[1])
			with redirect_stdout(once_file if once_file is not None else sys.stdout):
				print(report, end="" if report.endswith("\n") else "\n")
		except (OSError, ValueError, sqlite3.Error) as error:
			context = f"{source_context[0]}:{source_context[1]}: " if source_context else ""
			print(f"Expert error: {context}{error}", file=sys.stderr)
			failed = True
			if bail and scripts:
				scripts.clear()
		finally:
			finish_once()

	def run_sql(buffer):
		nonlocal failed, expert_options, www_pending
		if expert_options is not None:
			if not sqlite3.complete_statement(buffer):
				return buffer if sql_context(buffer)[1] or sql_context(buffer)[0] == "/*" else ""
			options, expert_options = expert_options, None
			run_expert(buffer, options)
			return ""
		start = 0
		for index, char in enumerate(buffer):
			if char == ";" and sqlite3.complete_statement(buffer[start:index + 1]):
				statement = buffer[start:index + 1]
				capture_output = io.StringIO() if (www_pending or testcase_name is not None) else None
				with redirect_stdout(capture_output if capture_output is not None else (once_file if once_file is not None else sys.stdout)):
					success = execute_sql(cursor, statement, headers, mode, nullvalue, column_separator, row_separator, column_widths, crlf, changes, source_context, parameters, timer, stats, eqp, explain, scanstats, log_callback)
				if capture_output is not None:
					captured = capture_output.getvalue()
					if testcase_name is not None:
						testcase_output.write(captured)
					if testcase_name is not None and not www_pending:
						write_result(captured)
				if www_pending:
					www_pending = False
					if success:
						try:
							pale_www(captured)
						except OSError as error:
							print(f"WWW error: {error}", file=sys.stderr)
							failed = True
				if not success:
					failed = True
					if bail and scripts:
						scripts.clear()
						return ""
				if once_file and (not success or cursor.description is not None):
					finish_once()
				start = index + 1
		remaining = buffer[start:]
		state, has_sql = sql_context(remaining)
		if has_sql or state == "/*":
			return remaining
		if remaining.strip() and remaining.lstrip().startswith(("--", "/*")):
			try:
				cursor.execute(remaining)
			except sqlite3.Error as error:
				print(f"SQL error: {error}", file=sys.stderr)
				failed = True
		return ""

	try:
		while True:
			if bail and failed and scripts:
				scripts.clear()
				sql = ""
			try:
				if scripts:
					try:
						source_context = (scripts[-1][0], 0)
						line_number, inp = next(scripts[-1][1])
						scripts[-1] = (scripts[-1][0], scripts[-1][1], line_number)
						source_context = (scripts[-1][0], line_number)
						inp = inp.rstrip("\r\n")
						if echo:
							print(inp)
					except StopIteration:
						path, lines, last_line = scripts.pop()
						if sql_context(sql)[1] or sql_context(sql)[0] == "/*":
							print(f"SQL error: {path}:{last_line}: incomplete statement", file=sys.stderr)
							failed = True
							expert_options = None
						sql = ""
						continue
				else:
					source_context = None
					with redirect_stdout(console):
						prompt = (continue_prompt if sql else main_prompt) if interactive else ""
						inp = cli_input(prompt, history) if interactive else input(prompt)
			except EOFError:
				if sql_context(sql)[1] or sql_context(sql)[0] == "/*":
					print("SQL error: incomplete statement at end of input", file=sys.stderr)
					return 1
				break
			except KeyboardInterrupt:
				if con is not None:
					try:
						con.interrupt()
					except sqlite3.Error:
						pass
				print("^C", file=console)
				sql = ""
				expert_options = None
				continue
			if inp.strip():
				history.append(inp.rstrip("\r\n"))
				if len(history) > 1000:
					del history[:-1000]
			if sql_context(sql)[0] not in ("", "--"):
				sql += inp + "\n"
				try:
					sql = run_sql(sql)
				except KeyboardInterrupt:
					if con is not None:
						con.interrupt()
					print("^C", file=console)
					sql = ""
					expert_options = None
				continue
			raw_input = inp

			message = re.fullmatch(r"\.print(?:[ \t](.*))?", inp.lstrip(), re.IGNORECASE)
			if message:
				newline = "\r\n" if crlf and not sys.stdout.isatty() else "\n"
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

			if command == ".safe":
				if argument.lower() not in ("on", "off"):
					print("Usage: .safe on|off", file=sys.stderr if output_file else sys.stdout)
					continue
				safe_mode = argument.lower() == "on"
				print(f"Safe mode {'on' if safe_mode else 'off'}")
				continue

			if command == ".nonce":
				if not argument:
					print("Usage: .nonce <string>", file=sys.stderr if output_file else sys.stdout)
					continue
				safe_nonce = argument
				print("Nonce armed for the next safe-mode command")
				continue

			if command == ".www":
				if safe_mode:
					print("Safe mode: .www is disabled", file=sys.stderr)
					failed = True
					continue
				if argument:
					print("Usage: .www", file=sys.stderr if output_file else sys.stdout)
					continue
				www_pending = True
				print("The next SQL result will open in a browser")
				continue

			if command == ".testcase":
				if not argument:
					print("Usage: .testcase NAME", file=sys.stderr if output_file else sys.stdout)
					continue
				testcase_name = argument
				testcase_output = io.StringIO()
				continue

			if command == ".check":
				if not argument:
					print("Usage: .check GLOB", file=sys.stderr if output_file else sys.stdout)
					continue
				if testcase_name is None:
					print("Check error: no testcase is active", file=sys.stderr)
					failed = True
					continue
				pattern = argument[1:-1] if len(argument) >= 2 and argument[0] in ("'", '"') and argument[-1] == argument[0] else argument
				if fnmatch.fnmatchcase(testcase_output.getvalue().rstrip("\r\n"), pattern):
					print("ok")
				else:
					print(f"Check failed for testcase {testcase_name}", file=sys.stderr)
					failed = True
				continue

			if safe_mode and command in (".shell", ".system", ".load", ".output", ".once", ".archive", ".backup", ".restore", ".save", ".clone", ".import", ".filectrl", ".unmodule", ".imposter", ".session", ".www", ".vacuum"):
				if safe_nonce is None:
					print(f"Safe mode: {command} is disabled", file=sys.stderr)
					failed = True
					continue
				safe_nonce = None

			if command == ".history":
				if argument:
					print("Usage: .history", file=sys.stderr if output_file else sys.stdout)
					continue
				if history:
					for number, entry in enumerate(history, 1):
						print(f"{number:4d}  {entry}")
				else:
					print("No history")
				continue

			if command == ".clearhistory":
				if argument:
					print("Usage: .clearhistory", file=sys.stderr if output_file else sys.stdout)
					continue
				history.clear()
				print("History cleared")
				continue

			if command in (".output", ".once"):
				try:
					if command == ".once" and not argument:
						raise ValueError("Usage: .once <file>")
					if argument:
						if argument[0] in ("'", '"'):
							lexer = shlex.shlex(argument, posix=True)
							lexer.whitespace_split = True
							lexer.commenters = ""
							lexer.escape = ""
							paths = list(lexer)
							if len(paths) != 1 or not paths[0]:
								raise ValueError("Usage: .output [file]")
							argument = paths[0]
						path = Path(argument).expanduser().resolve()
						protected = [active[0] for active in scripts]
						if con is not None:
							for seq, name, file in get_databases(cursor):
								if file:
									protected.extend(Path(file + suffix).resolve() for suffix in ("", "-wal", "-shm", "-journal"))
						if any(path == item or (path.exists() and item.exists() and path.samefile(item)) for item in protected):
							raise ValueError("Output file must not overwrite an open database or active script")
						if command == ".output" and output_file and Path(output_file.name).resolve() == path:
							output_file.flush()
							if once_file:
								once_file.close()
								once_file = None
							continue
						if any(handle and (Path(handle.name).resolve() == path or (path.exists() and path.samefile(handle.name))) for handle in (output_file, once_file)):
							raise ValueError("File is already used by .output or .once; choose another file")
						new_output = path.open("w", encoding="utf-8", newline="\r\n" if crlf else "\n")
					else:
						new_output = None
					if once_file:
						once_file.close()
						once_file = None
					if command == ".once":
						once_file = new_output
						continue
					if output_file:
						output_file.close()
					output_file = new_output
					sys.stdout = output_file if output_file is not None else console
				except (OSError, ValueError) as error:
					print(f"Output error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".import":
				try:
					if con is None:
						raise ValueError("No database opened")
					filename, table_name = parse_import_arguments(argument)
					path = Path(filename).expanduser()
					if not path.is_absolute() and scripts:
						path = scripts[-1][0].parent / path
					import_file(cursor, path, table_name, nullvalue, column_separator)
				except (OSError, UnicodeError, ValueError, csv.Error, sqlite3.Error) as error:
					print(f"Import error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".read":
				try:
					if not argument:
						raise ValueError("Usage: .read <file.sql>")
					if argument[0] in ("'", '"'):
						lexer = shlex.shlex(argument, posix=True)
						lexer.whitespace_split = True
						lexer.commenters = ""
						lexer.escape = ""
						paths = list(lexer)
						if len(paths) != 1 or not paths[0]:
							raise ValueError("Usage: .read <file.sql>")
						argument = paths[0]
					if sql_context(sql)[1]:
						raise ValueError("Finish or clear the current SQL statement before .read")
					path = Path(argument).expanduser()
					if not path.is_absolute() and scripts:
						path = scripts[-1][0].parent / path
					path = path.resolve()
					if any(path == active[0] for active in scripts) or len(scripts) >= 32:
						raise ValueError("Recursive .read or nesting limit exceeded")
					text = path.read_text(encoding="utf-8-sig")
					scripts.append((path, iter(enumerate(io.StringIO(text), 1)), 0))
					sql = ""
				except (OSError, UnicodeError, ValueError) as error:
					print(f"Read error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".dump":
				try:
					if con is None:
						raise ValueError("No database opened")
					lexer = shlex.shlex(argument, posix=True)
					lexer.whitespace_split = True
					lexer.commenters = ""
					lexer.escape = ""
					pale_dump(cursor, list(lexer))
				except (ValueError, OSError, sqlite3.Error) as error:
					print(f"Dump error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".recover":
				try:
					if argument:
						raise ValueError("Usage: .recover")
					if con is None:
						raise ValueError("No database opened")
					with redirect_stdout(once_file if once_file is not None else sys.stdout):
						if not pale_recover(con):
							failed = True
				except (ValueError, OSError, sqlite3.Error) as error:
					print(f"Recover error: {error}", file=sys.stderr)
					failed = True
				finally:
					finish_once()
				continue

			if command == ".backup":
				try:
					if con is None:
						raise ValueError("No database opened")
					database_name, filename = parse_backup_arguments(argument)
					backup_database(con, cursor, database_name, filename, protected_backup_paths())
				except (OSError, ValueError, sqlite3.Error) as error:
					print(f"Backup error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".restore":
				try:
					if con is None:
						raise ValueError("No database opened")
					database_name, filename = parse_backup_arguments(argument, ".restore")
					restore_database(con, cursor, database_name, filename)
				except (OSError, ValueError, sqlite3.Error) as error:
					print(f"Restore error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".save":
				try:
					if con is None:
						raise ValueError("No database opened")
					filename = parse_single_file_argument(argument, ".save")
					backup_database(con, cursor, "main", filename, protected_backup_paths(False))
				except (OSError, ValueError, sqlite3.Error) as error:
					print(f"Save error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".clone":
				try:
					if con is None:
						raise ValueError("No database opened")
					filename = parse_single_file_argument(argument, ".clone")
					backup_database(con, cursor, "main", filename, protected_backup_paths(False))
				except (OSError, ValueError, sqlite3.Error) as error:
					print(f"Clone error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".archive":
				try:
					pale_archive(argument)
				except (OSError, ValueError, sqlite3.Error) as error:
					print(f"Archive error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".excel":
				if argument:
					print("Usage: .excel", file=sys.stderr if output_file else sys.stdout)
					continue
				mode = "csv"
				headers = True
				column_separator = None
				row_separator = None
				continue

			if command == ".dbtotxt":
				if argument:
					print("Usage: .dbtotxt", file=sys.stderr if output_file else sys.stdout)
					continue
				try:
					if con is None:
						raise ValueError("No database opened")
					pale_dbtotxt(cursor)
				except (OSError, ValueError) as error:
					print(f"DB dump error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".sha3sum":
				try:
					if con is None:
						raise ValueError("No database opened")
					if len(argument.split()) > 1:
						raise ValueError("Usage: .sha3sum [pattern]")
					pale_sha3sum(con, argument)
				except (ValueError, OSError, sqlite3.Error) as error:
					print(f"SHA3 error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".help":
				pale_help()
				continue

			if command == ".version":
				pale_version()
				continue

			if command == ".prompt":
				try:
					lexer = shlex.shlex(argument, posix=True)
					lexer.whitespace_split = True
					lexer.commenters = ""
					lexer.escape = ""
					prompts = list(lexer)
					if len(prompts) != 2:
						raise ValueError
				except ValueError:
					print('Usage: .prompt <main> <continue> (example: .prompt "sql> " "...> ")', file=sys.stderr if output_file else sys.stdout)
					continue
				main_prompt, continue_prompt = prompts
				continue

			if command == ".crlf":
				if argument.lower() not in ("on", "off"):
					print("Usage: .crlf on|off", file=sys.stderr if output_file else sys.stdout)
					continue
				crlf = argument.lower() == "on"
				if output_file:
					output_file.reconfigure(newline="\r\n" if crlf else "\n")
				if once_file:
					once_file.reconfigure(newline="\r\n" if crlf else "\n")
				continue

			if command == ".width":
				try:
					widths = tuple(int(value) for value in argument.split())
					if not widths or any(width < 0 for width in widths):
						raise ValueError
				except ValueError:
					print("Usage: .width <n1> <n2> ... (non-negative integers; 0 = auto)", file=sys.stderr if output_file else sys.stdout)
					continue
				column_widths = widths
				continue

			if command == ".separator":
				try:
					separators = parse_separators(argument)
				except ValueError as error:
					print(error, file=sys.stderr if output_file else sys.stdout)
					continue
				column_separator = separators[0]
				if len(separators) == 2:
					row_separator = separators[1]
				continue

			if command == ".nullvalue":
				if not argument:
					print('Usage: .nullvalue <text> (use "" for empty text)', file=sys.stderr if output_file else sys.stdout)
					continue
				if len(argument) >= 2 and argument[0] in ("\"", "'") and argument[-1] == argument[0]:
					argument = argument[1:-1]
				nullvalue = argument
				continue

			if command == ".mode":
				if argument.lower() not in OUTPUT_MODES:
					print("Usage: .mode " + "|".join(OUTPUT_MODES), file=sys.stderr if output_file else sys.stdout)
					continue
				mode = argument.lower()
				column_separator = None
				row_separator = None
				continue

			if command == ".headers":
				if argument.lower() not in ("on", "off"):
					print("Usage: .headers on|off", file=sys.stderr if output_file else sys.stdout)
					continue

				headers = argument.lower() == "on"
				continue

			if command == ".changes":
				if argument.lower() not in ("on", "off"):
					print("Usage: .changes on|off", file=sys.stderr if output_file else sys.stdout)
					continue
				changes = argument.lower() == "on"
				continue

			if command in (".timeout", ".busy_timeout"):
				if con is None:
					print("No database opened", file=sys.stderr if output_file else sys.stdout)
					failed = True
					continue
				try:
					milliseconds = int(argument)
					if milliseconds < 0 or re.fullmatch(r"[0-9]+", argument) is None:
						raise ValueError
					con.execute(f"PRAGMA busy_timeout = {milliseconds}")
				except ValueError:
					print(f"Usage: {command} <milliseconds> (non-negative integer)", file=sys.stderr if output_file else sys.stdout)
					continue
				except sqlite3.Error as error:
					print(f"Timeout error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".bail":
				if argument.lower() not in ("on", "off"):
					print("Usage: .bail on|off", file=sys.stderr if output_file else sys.stdout)
					continue
				bail = argument.lower() == "on"
				continue

			if command == ".echo":
				if argument.lower() not in ("on", "off"):
					print("Usage: .echo on|off", file=sys.stderr if output_file else sys.stdout)
					continue
				echo = argument.lower() == "on"
				continue

			if command == ".parameter":
				try:
					lexer = shlex.shlex(argument, posix=True)
					lexer.whitespace_split = True
					lexer.commenters = ""
					lexer.escape = ""
					values = list(lexer)
					if not values:
						raise ValueError("Usage: .parameter init|set|unset|list|clear")
					action = values[0].lower()
					if action == "init" and len(values) == 1:
						continue
					if action == "clear" and len(values) == 1:
						parameters.clear()
						continue
					if action == "list" and len(values) == 1:
						for name, value in parameters.items():
							print(f"{name} = {sql_literal(value)}")
						continue
					if action == "set" and len(values) == 3:
						parameters[parameter_name(values[1])] = parameter_value(values[2])
						continue
					if action == "unset" and len(values) == 2:
						parameters.pop(parameter_name(values[1]), None)
						continue
					raise ValueError("Usage: .parameter init|set|unset|list|clear")
				except ValueError as error:
					print(error, file=sys.stderr if output_file else sys.stdout)
				continue

			if command == ".limit":
				if con is None:
					print("No database opened", file=sys.stderr if output_file else sys.stdout)
					failed = True
					continue
				try:
					lexer = shlex.shlex(argument, posix=True)
					lexer.whitespace_split = True
					lexer.commenters = ""
					lexer.escape = ""
					values = list(lexer)
					if len(values) > 2:
						raise ValueError("Usage: .limit [name] [value]")
					if not values:
						for name, category in SQLITE_LIMITS.items():
							print(f"{name} {con.getlimit(category)}")
						continue
					name = limit_name(values[0])
					category = SQLITE_LIMITS[name]
					if len(values) == 1:
						print(f"{name} {con.getlimit(category)}")
						continue
					value = int(values[1])
					if value < 0 or re.fullmatch(r"[0-9]+", values[1]) is None:
						raise ValueError("limit value must be a non-negative integer")
					con.setlimit(category, value)
					print(f"{name} {con.getlimit(category)}")
				except (ValueError, sqlite3.Error, AttributeError) as error:
					print(f"Limit error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".dbconfig":
				if con is None:
					print("No database opened", file=sys.stderr if output_file else sys.stdout)
					failed = True
					continue
				try:
					pale_dbconfig(con, argument)
				except (ValueError, OSError, sqlite3.Error) as error:
					print(f"DB config error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".foreign_keys":
				if con is None:
					print("No database opened", file=sys.stderr if output_file else sys.stdout)
					failed = True
					continue
				try:
					pale_foreign_keys(cursor, argument)
				except (ValueError, sqlite3.Error) as error:
					print(f"Foreign keys error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".journal_mode":
				if con is None:
					print("No database opened", file=sys.stderr if output_file else sys.stdout)
					failed = True
					continue
				try:
					pale_journal_mode(cursor, argument)
				except (ValueError, sqlite3.Error) as error:
					print(f"Journal mode error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".encoding":
				if con is None:
					print("No database opened", file=sys.stderr if output_file else sys.stdout)
					failed = True
					continue
				try:
					pale_encoding(cursor, argument)
				except (ValueError, sqlite3.Error) as error:
					print(f"Encoding error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".load":
				if con is None:
					print("No database opened", file=sys.stderr if output_file else sys.stdout)
					failed = True
					continue
				try:
					base_path = scripts[-1][0].parent if scripts else None
					pale_load_extension(con, argument, base_path)
				except (OSError, ValueError, sqlite3.Error) as error:
					print(f"Load error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".filectrl":
				if con is None:
					print("No database opened", file=sys.stderr if output_file else sys.stdout)
					failed = True
					continue
				try:
					pale_filectrl(con, argument)
				except (ValueError, OSError, sqlite3.Error) as error:
					print(f"File control error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".unmodule":
				if con is None:
					print("No database opened", file=sys.stderr if output_file else sys.stdout)
					failed = True
					continue
				try:
					pale_unmodule(con, argument)
				except (ValueError, OSError, sqlite3.Error) as error:
					print(f"Unmodule error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".vfsinfo":
				if con is None:
					print("No database opened", file=sys.stderr if output_file else sys.stdout)
					failed = True
					continue
				try:
					pale_vfsinfo(con, argument)
				except (ValueError, OSError, sqlite3.Error) as error:
					print(f"VFS info error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".vfsname":
				if con is None:
					print("No database opened", file=sys.stderr if output_file else sys.stdout)
					failed = True
					continue
				try:
					pale_vfsname(con, argument)
				except (ValueError, OSError, sqlite3.Error) as error:
					print(f"VFS name error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".timer":
				if argument.lower() not in ("on", "off"):
					print("Usage: .timer on|off", file=sys.stderr if output_file else sys.stdout)
					continue
				timer = argument.lower() == "on"
				continue

			if command == ".stats":
				if argument.lower() not in ("on", "off"):
					print("Usage: .stats on|off", file=sys.stderr if output_file else sys.stdout)
					continue
				stats = argument.lower() == "on"
				continue

			if command == ".scanstats":
				if argument.lower() not in ("on", "off", "est"):
					print("Usage: .scanstats on|off|est", file=sys.stderr if output_file else sys.stdout)
					continue
				scanstats = argument.lower()
				continue

			if command == ".progress":
				if con is None:
					print("No database opened", file=sys.stderr if output_file else sys.stdout)
					failed = True
					continue
				if re.fullmatch(r"[0-9]+", argument) is None or int(argument) > sys.maxsize:
					print("Usage: .progress <N> (non-negative integer; 0 disables)", file=sys.stderr if output_file else sys.stdout)
					continue
				progress_calls = 0
				con.set_progress_handler(progress_handler, int(argument))
				continue

			if command == ".trace":
				if con is None:
					print("No database opened", file=sys.stderr if output_file else sys.stdout)
					failed = True
					continue
				if not argument:
					print("Usage: .trace on|off|stdout|stderr|<file>", file=sys.stderr if output_file else sys.stdout)
					continue
				trace_argument = argument.lower()
				if trace_argument == "off":
					clear_trace()
					continue
				if trace_argument in ("on", "stdout", "stderr"):
					try:
						clear_trace()
						trace_mode = "stderr" if trace_argument == "stderr" else trace_argument
						install_trace()
					except (OSError, ValueError, AttributeError) as error:
						trace_mode = "off"
						print(f"Trace error: {error}", file=sys.stderr)
						failed = True
					continue
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
					if trace_file:
						protected.append(Path(trace_file.name).resolve())
					if any(path == item or (path.exists() and item.exists() and path.samefile(item)) for item in protected):
						raise ValueError("Trace file must not overwrite an open database, script, or output file")
					new_trace_file = path.open("w", encoding="utf-8", newline="\n")
					clear_trace()
					trace_file = new_trace_file
					trace_mode = "file"
					install_trace()
				except (OSError, ValueError, AttributeError) as error:
					print(f"Trace error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".log":
				if not argument:
					print("Usage: .log off|stdout|stderr|<file>", file=sys.stderr if output_file else sys.stdout)
					continue
				log_argument = argument.lower()
				if log_argument == "off":
					clear_log()
					continue
				if log_argument in ("stdout", "stderr"):
					clear_log()
					log_mode = log_argument
					continue
				try:
					lexer = shlex.shlex(argument, posix=True)
					lexer.whitespace_split = True
					lexer.commenters = ""
					lexer.escape = ""
					paths = list(lexer)
					if len(paths) != 1 or not paths[0]:
						raise ValueError("Usage: .log off|stdout|stderr|<file>")
					path = Path(paths[0]).expanduser().resolve()
					protected = [active[0] for active in scripts]
					if con is not None:
						protected.extend(protected_backup_paths())
					for handle in (output_file, once_file, trace_file, log_file):
						if handle:
							protected.append(Path(handle.name).resolve())
					if any(path == item or (path.exists() and item.exists() and path.samefile(item)) for item in protected):
						raise ValueError("Log file must not overwrite an open database, script, or output file")
					new_log_file = path.open("w", encoding="utf-8", newline="\n")
					clear_log()
					log_file = new_log_file
					log_mode = "file"
				except (OSError, ValueError) as error:
					print(f"Log error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".eqp":
				if argument.lower() not in ("on", "off", "full"):
					print("Usage: .eqp on|off|full", file=sys.stderr if output_file else sys.stdout)
					continue
				eqp = argument.lower()
				continue

			if command == ".explain":
				if argument.lower() not in ("on", "off", "auto"):
					print("Usage: .explain on|off|auto", file=sys.stderr if output_file else sys.stdout)
					continue
				explain = argument.lower()
				continue

			if command == ".expert":
				expert_options = None
				try:
					if con is None:
						raise ValueError("No database opened")
					if sql_context(sql)[1]:
						raise ValueError("Finish the pending SQL or use .clear before .expert")
					sample, verbose, inline_sql = parse_expert_arguments(argument)
					if inline_sql is None:
						expert_options = (sample, verbose)
					else:
						run_expert(inline_sql, (sample, verbose))
				except (OSError, ValueError, sqlite3.Error) as error:
					context = f"{source_context[0]}:{source_context[1]}: " if source_context else ""
					print(f"Expert error: {context}{error}", file=sys.stderr)
					failed = True
				continue

			if command == ".clear":
				sql = ""
				expert_options = None
				print("Buffer is cleaned")
				continue

			if command in (".exit", ".quit") or inp.lower() in ("exit", "quit"):
				break

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
					failed = True
				continue

			if command == ".pwd":
				if argument:
					print("Usage: .pwd", file=sys.stderr if output_file else sys.stdout)
					continue
				print(Path.cwd())
				continue

			if command in (".shell", ".system"):
				try:
					if not pale_shell(argument):
						failed = True
				except (OSError, ValueError) as error:
					print(f"Shell error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".connections":
				if argument:
					print("Usage: .connections", file=sys.stderr if output_file else sys.stdout)
					continue
				argument = "list"
				command = ".connection"

			if command == ".connection":
				try:
					values = shlex.split(argument)
					action = values[0].lower() if values else "list"
					if action == "list" and len(values) == 1:
						if not connections:
							print("No connections")
						else:
							for number, (item_con, item_cursor) in enumerate(connections, 1):
								row = item_cursor.execute("PRAGMA database_list").fetchone()
								filename = row[2] if row else ""
								marker = " *" if number - 1 == active_connection else ""
								print(f"{number}{marker}\t{filename or ':memory:'}")
						continue
					if action == "new" and len(values) == 1:
						new_con = sqlite3.connect(":memory:", isolation_level=None)
						new_cursor = new_con.cursor()
						connections.append((new_con, new_cursor))
						con, cursor = new_con, new_cursor
						active_connection = len(connections) - 1
						print(f"Connection {active_connection + 1} opened")
						continue
					if action == "use" and len(values) == 2 and values[1].isdigit():
						number = int(values[1])
						if not 1 <= number <= len(connections):
							raise ValueError("connection number out of range")
						clear_trace()
						active_connection = number - 1
						con, cursor = connections[active_connection]
						print(f"Using connection {number}")
						continue
					if action == "close" and len(values) == 2 and values[1].isdigit():
						number = int(values[1])
						if not 1 <= number <= len(connections):
							raise ValueError("connection number out of range")
						index = number - 1
						item_con, item_cursor = connections.pop(index)
						item_cursor.close()
						item_con.close()
						if not connections:
							con, cursor, active_connection = None, None, None
						elif active_connection == index:
							active_connection = min(index, len(connections) - 1)
							con, cursor = connections[active_connection]
						elif active_connection > index:
							active_connection -= 1
						print(f"Connection {number} closed")
						continue
					raise ValueError("Usage: .connection new|list|use N|close N")
				except (OSError, ValueError, sqlite3.Error) as error:
					print(f"Connection error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".open":
				if not argument:
					print("Usage: .open <database>", file=sys.stderr if output_file else sys.stdout)
					continue
				try:
					open_name = parse_single_argument(argument, "Usage: .open <database>")
				except ValueError as error:
					print(error, file=sys.stderr if output_file else sys.stdout)
					continue

				if con is not None:
					clear_trace()
					con.close()
					if active_connection is not None:
						connections.pop(active_connection)
						active_connection = None
				expert_options = None

				try:
					con, cursor = pale_open(open_name)
					connections.append((con, cursor))
					active_connection = len(connections) - 1

				except sqlite3.Error as error:
					print("Database error:", error, file=sys.stderr if output_file else sys.stdout)
					con = None
					cursor = None
					continue

				sql = ""
				if interactive:
					print("Database opened", file=console)
				continue

			if con is None:
				if scripts:
					print(f"Read error: no database opened; stopped {scripts[-1][0]}. Use .open <database> before .read.", file=sys.stderr)
					failed = True
					scripts.clear()
					sql = ""
				else:
					print("No database opened", file=sys.stderr if output_file else sys.stdout)
				continue

			# DB is opened

			if command == ".describe":
				if not argument:
					print("Usage: .describe <table>", file=sys.stderr if output_file else sys.stdout)
					continue
				pale_describe(cursor, argument)
				continue

			if command == ".find":
				if not argument:
					print("Usage: .find <text>", file=sys.stderr if output_file else sys.stdout)
					continue
				pale_find(cursor, argument[1:-1] if len(argument) >= 2 and argument[0] in ("'", '"') and argument[-1] == argument[0] else argument)
				continue

			if command == ".count":
				pale_count(cursor, argument)
				continue

			if command == ".tablesize":
				pale_tablesize(cursor, argument)
				continue

			if command == ".schemajson":
				if argument:
					print("Usage: .schemajson", file=sys.stderr if output_file else sys.stdout)
					continue
				pale_schemajson(cursor)
				continue

			if command == ".vacuum":
				if argument:
					print("Usage: .vacuum", file=sys.stderr if output_file else sys.stdout)
					continue
				try:
					con.execute("VACUUM")
					print("Vacuum complete")
				except sqlite3.Error as error:
					print(f"Vacuum error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".watch":
				match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s+([\s\S]+)", argument)
				if not match:
					print("Usage: .watch <seconds> <SQL>", file=sys.stderr if output_file else sys.stdout)
					continue
				interval = float(match[1])
				if interval < 0:
					print("Watch interval must not be negative", file=sys.stderr)
					failed = True
					continue
				statement = match[2].strip()
				if not statement.endswith(";"):
					statement += ";"
				try:
					while True:
						success = execute_sql(cursor, statement, headers, mode, nullvalue, column_separator, row_separator, column_widths, crlf, changes, source_context, parameters, timer, stats, eqp, explain, scanstats, log_callback)
						if not success:
							failed = True
							break
						if not interactive:
							break
						time.sleep(interval)
				except KeyboardInterrupt:
					print("Watch stopped")
				continue

			if command == ".auth":
				if argument.lower() not in ("on", "off"):
					print("Usage: .auth on|off", file=sys.stderr if output_file else sys.stdout)
					continue
				try:
					auth_enabled = argument.lower() == "on"
					set_authorizer(auth_enabled)
					print(f"Authorizer {'on' if auth_enabled else 'off'}")
				except (ValueError, sqlite3.Error) as error:
					print(f"Authorizer error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".memory":
				if argument:
					print("Usage: .memory", file=sys.stderr if output_file else sys.stdout)
					continue
				try:
					new_con = sqlite3.connect(":memory:", isolation_level=None)
					new_cursor = new_con.cursor()
					if con is not None:
						clear_trace()
						old_con = con
						connections[active_connection] = (new_con, new_cursor)
						con, cursor = new_con, new_cursor
						old_con.close()
					else:
						connections.append((new_con, new_cursor))
						active_connection = len(connections) - 1
						con, cursor = new_con, new_cursor
					if auth_enabled:
						set_authorizer(True)
					sql = ""
					print("Opened :memory:")
				except sqlite3.Error as error:
					print(f"Memory database error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".reopen":
				if argument:
					print("Usage: .reopen", file=sys.stderr if output_file else sys.stdout)
					continue
				if con is None:
					print("No database opened", file=sys.stderr if output_file else sys.stdout)
					continue
				try:
					main_row = next((row for row in get_databases(cursor) if row[1] == "main"), None)
					if not main_row or not main_row[2]:
						raise ValueError("cannot reopen an in-memory database")
					new_con = sqlite3.connect(main_row[2], isolation_level=None)
					new_cursor = new_con.cursor()
					clear_trace()
					old_con = con
					connections[active_connection] = (new_con, new_cursor)
					con, cursor = new_con, new_cursor
					old_con.close()
					if auth_enabled:
						set_authorizer(True)
					sql = ""
					print(f"Reopened {main_row[2]}")
				except (OSError, ValueError, sqlite3.Error) as error:
					print(f"Reopen error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".readonly":
				try:
					filename = parse_single_argument(argument, "Usage: .readonly <file>")
					path = Path(filename).expanduser().resolve()
					uri = "file:" + str(path).replace("\\", "/") + "?mode=ro"
					new_con = sqlite3.connect(uri, uri=True, isolation_level=None)
					new_cursor = new_con.cursor()
					if con is not None:
						clear_trace()
						old_con = con
						connections[active_connection] = (new_con, new_cursor)
						con, cursor = new_con, new_cursor
						old_con.close()
					else:
						connections.append((new_con, new_cursor))
						active_connection = len(connections) - 1
						con, cursor = new_con, new_cursor
					if auth_enabled:
						set_authorizer(True)
					sql = ""
					print(f"Opened read-only: {path}")
				except (OSError, ValueError, sqlite3.Error) as error:
					print(f"Read-only error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".status":
				if argument:
					print("Usage: .status", file=sys.stderr if output_file else sys.stdout)
					continue
				if con is None:
					print("database: none")
					print(f"SQLite version: {sqlite3.sqlite_version}")
					print("foreign_keys: unavailable")
					print("journal_mode: unavailable")
					print(f"output mode: {mode}")
					print(f"headers: {'on' if headers else 'off'}")
					print("transaction state: inactive")
				else:
					pale_status(con, cursor, mode, headers)
				continue

			if command == ".imposter":
				try:
					pale_imposter(cursor, argument)
				except (ValueError, sqlite3.Error) as error:
					print(f"Imposter error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".session":
				try:
					session_state = pale_session(con, argument, session_state)
				except (OSError, ValueError, sqlite3.Error) as error:
					print(f"Session error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".tables":
				tables = pale_tables(cursor, argument or "%")

				if tables:
					for table in tables:
						print(table[0])
				else:
					print("No tables found")
				continue

			if command == ".views":
				views = pale_views(cursor)

				if views:
					for view in views:
						print(view[0])
				else:
					print("No views found")
				continue

			if command == ".triggers":
				triggers = pale_triggers(cursor)

				if triggers:
					for trigger in triggers:
						print(trigger[0])
				else:
					print("No triggers found")
				continue

			if command == ".indexes":
				indexes = pale_indexes(cursor, argument)

				if indexes:
					for index in indexes:
						print(index[0])
				else:
					print("No indexes found")
				continue

			if command == ".schema":
				pale_schema(cursor, argument)
				continue

			if command == ".fullschema":
				pale_fullschema(cursor)
				continue

			if command == ".lint":
				pale_lint(cursor)
				continue

			if command in (".integrity", ".intck"):
				if argument:
					print(f"Usage: {command}", file=sys.stderr if output_file else sys.stdout)
					continue
				try:
					if command == ".intck":
						pale_incremental_integrity_check(cursor)
					else:
						pale_integrity_check(cursor)
				except (OSError, ValueError, sqlite3.Error) as error:
					print(f"Integrity check error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".selftest":
				if argument:
					print("Usage: .selftest", file=sys.stderr if output_file else sys.stdout)
					continue
				try:
					if not pale_selftest(con):
						failed = True
				except (OSError, ValueError, sqlite3.Error) as error:
					print(f"Self-test error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".columns":
				if not argument:
					print("No table name provided", file=sys.stderr if output_file else sys.stdout)
					continue

				pale_columns(cursor, argument)
				continue

			if command == ".foreignkeys":
				if not argument:
					print("No table name provided", file=sys.stderr if output_file else sys.stdout)
					continue

				pale_foreignkeys(cursor, argument)
				continue

			if command == ".close":
				clear_trace()
				con.close()
				if active_connection is not None:
					connections.pop(active_connection)
				expert_options = None
				if connections:
					active_connection = min(active_connection, len(connections) - 1)
					con, cursor = connections[active_connection]
				else:
					con, cursor, active_connection = None, None, None
				sql = ""

				if interactive:
					print("Database closed", file=console)
				continue

			if command == ".database":
				pale_database(cursor)
				continue

			if command == ".databases":
				pale_databases(cursor)
				continue

			if command == ".dbinfo":
				pale_dbinfo(cursor, argument or "main")
				continue

			if command.startswith("."):
				print(f"Unknown command: {command}", file=sys.stderr if output_file else sys.stdout)
				continue

			sql += raw_input + "\n"
			try:
				sql = run_sql(sql)
			except KeyboardInterrupt:
				if con is not None:
					con.interrupt()
				print("^C", file=console)
				sql = ""
				expert_options = None

	finally:
		clear_trace()
		clear_log()
		sys.stdout = console
		if once_file:
			once_file.close()
		if output_file:
			output_file.close()
		closed = set()
		for item_con, item_cursor in connections:
			if id(item_con) not in closed:
				item_cursor.close()
				item_con.close()
				closed.add(id(item_con))
	return 1 if failed else 0


if __name__ == "__main__":
	sys.exit(main())
