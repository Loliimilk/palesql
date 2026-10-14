import sys
import sqlite3
import os
import re
import io
import json
import math
import unicodedata
import shutil
import shlex
import csv
from pathlib import Path
from contextlib import redirect_stdout


PALESQL_VERSION = "0.4.0"
PYTHON_VERSION = sys.version.split()[0]
SQLITE_LIBRARY_VERSION = sqlite3.sqlite_version
OUTPUT_MODES = ("list", "column", "table", "box", "csv", "tabs", "json", "jsonpretty", "line", "markdown", "quote")


def pale_open(name):
	if not name.lower().endswith(".db"):
		name += ".db"

	con = sqlite3.connect(name, isolation_level=None)
	cursor = con.cursor()

	return con, cursor


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
		".backup [database] <file> - create a database backup\n"
		".restore [database] <file> - restore a database from a backup file\n"
		".save <file> - save the current database to a file\n"
		".clone <file> - clone the current database to a new file\n"
		".excel - use spreadsheet-friendly CSV output with headers\n"
		".dbtotxt - show a hex dump of the physical main database file\n"
		".lint - check foreign keys and suggest indexes\n"
		".nullvalue <text> - set NULL text; JSON and quote keep native NULL values\n"
		".separator <column> [row] - set list/csv/tabs separators; supports quotes and \\t, \\n, \\r\n"
		"Changing .mode resets separators to that mode's defaults.\n"
		".crlf on|off - file output line endings (default: off/LF); explicit row separator wins\n"
		".headers on|off - show or hide column names in SQL results\n"
		".changes on|off - show changed row counts after INSERT/UPDATE/DELETE\n"
		".timeout <milliseconds> - set the SQLite busy timeout\n"
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

def execute_sql(cursor, sql, headers=False, mode="list", nullvalue="", column_separator=None, row_separator=None, column_widths=(), crlf=False, changes=False):
	try:
		cursor.execute(sql)

		if cursor.description:
			names = [column[0] for column in cursor.description]
			newline = "\r\n" if crlf and not sys.stdout.isatty() else "\n"
			print_results(names, cursor.fetchall(), headers, mode, nullvalue, column_separator, row_separator, column_widths, newline)
		elif changes and cursor.rowcount >= 0:
			print(f"changes: {cursor.rowcount}")
		return True

	except sqlite3.Error as error:
		print("SQL error:", error, file=sys.stderr)
		return False

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
	headers = False
	mode = "list"
	nullvalue = ""
	changes = False
	column_separator = None
	row_separator = None
	column_widths = ()
	crlf = False
	main_prompt = "PaleSQL: "
	continue_prompt = "    ...: "
	failed = False
	scripts = []
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

	def run_sql(buffer):
		nonlocal failed, once_file, output_file
		start = 0
		for index, char in enumerate(buffer):
			if char == ";" and sqlite3.complete_statement(buffer[start:index + 1]):
				statement = buffer[start:index + 1]
				with redirect_stdout(once_file if once_file is not None else sys.stdout):
					success = execute_sql(cursor, statement, headers, mode, nullvalue, column_separator, row_separator, column_widths, crlf, changes)
				if not success:
					failed = True
				if once_file and (not success or cursor.description is not None):
					once_file.close()
					once_file = None
					if output_file:
						output_file.close()
						output_file = None
					sys.stdout = console
				start = index + 1
		remaining = buffer[start:]
		return remaining if sql_context(remaining)[1] or sql_context(remaining)[0] == "/*" else ""

	try:
		while True:
			try:
				if scripts:
					try:
						inp = next(scripts[-1][1]).rstrip("\r\n")
					except StopIteration:
						path, lines = scripts.pop()
						if sql_context(sql)[1] or sql_context(sql)[0] == "/*":
							print(f"SQL error: incomplete statement at end of {path}", file=sys.stderr)
							failed = True
						sql = ""
						continue
				else:
					with redirect_stdout(console):
						inp = input((continue_prompt if sql else main_prompt) if interactive else "")
			except EOFError:
				if sql_context(sql)[1] or sql_context(sql)[0] == "/*":
					print("SQL error: incomplete statement at end of input", file=sys.stderr)
					return 1
				break
			if sql_context(sql)[0] not in ("", "--"):
				sql += inp + "\n"
				sql = run_sql(sql)
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
					scripts.append((path, iter(io.StringIO(text))))
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

			if command == ".timeout":
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
					print("Usage: .timeout <milliseconds> (non-negative integer)", file=sys.stderr if output_file else sys.stdout)
					continue
				except sqlite3.Error as error:
					print(f"Timeout error: {error}", file=sys.stderr)
					failed = True
				continue

			if command == ".clear":
				sql = ""
				print("Buffer is cleaned")
				continue

			if command in (".exit", ".quit") or inp.lower() in ("exit", "quit"):
				break

			if command == ".open":
				if not argument:
					print("No name of database", file=sys.stderr if output_file else sys.stdout)
					continue

				if con is not None:
					con.close()

				try:
					con, cursor = pale_open(argument)

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
				con.close()
				con = None
				cursor = None
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

			sql = run_sql(sql)

	finally:
		sys.stdout = console
		if once_file:
			once_file.close()
		if output_file:
			output_file.close()
		if con is not None:
			con.close()
	return 1 if failed else 0


if __name__ == "__main__":
	sys.exit(main())
