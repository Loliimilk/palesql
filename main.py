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


PALESQL_VERSION = "0.2.0"
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
		".lint - check foreign keys and suggest indexes\n"
		".nullvalue <text> - set NULL text; JSON and quote keep native NULL values\n"
		".separator <column> [row] - set list/csv/tabs separators; supports quotes and \\t, \\n, \\r\n"
		"Changing .mode resets separators to that mode's defaults.\n"
		".headers on|off - show or hide column names in SQL results\n"
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

def text_width(text):
	return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)

def wrap_cell(text, width):
	lines, line, used = [], "", 0
	for char in text:
		size = text_width(char)
		if used + size > width and line:
			lines.append(line)
			line, used = "", 0
		line += char
		used += size
	lines.append(line)
	return lines

def print_grid(names, rows, headers, mode, nullvalue="", column_widths=()):
	data = ([names] if headers else []) + rows
	if not data:
		return
	data = [[result_text(value, nullvalue).replace("\r", "\\r").replace("\t", "\\t").replace("\x00", "\\0").split("\n") for value in row] for row in data]
	widths = [max(text_width(line) for row in data for line in row[index]) for index in range(len(names))]
	minimums = [max(1, max((text_width(char) for row in data for line in row[index] for char in line), default=1)) for index in range(len(names))]
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
					print_grid([name], [[row[index]] for row in rows], headers, mode, nullvalue, column_widths[index:index + 1])
				return
			for row in data:
				for line in row[0]:
					for part in wrap_cell(line, limit):
						print(part)
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
		print(parts[0] + parts[1].join(stroke * (width + 2) for width in widths) + parts[2])
	if mode != "column":
		border(top)
	for index, row in enumerate(data):
		for line_number in range(max(len(cell) for cell in row)):
			cells = []
			for cell, width in zip(row, widths):
				line = cell[line_number] if line_number < len(cell) else ""
				cells.append(line + " " * (width - text_width(line)))
			print("  ".join(cells).rstrip() if mode == "column" else vertical + " " + (" " + vertical + " ").join(cells) + " " + vertical)
		if headers and index == 0:
			if mode == "column":
				print("  ".join("-" * width for width in widths))
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

def print_results(names, rows, headers, mode, nullvalue="", column_separator=None, row_separator=None, column_widths=()):
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
		print(json.dumps(values, ensure_ascii=False, allow_nan=False, indent=2 if mode == "jsonpretty" else None))
	elif mode in ("csv", "tabs"):
		output = io.StringIO(newline="")
		separator = column_separator if column_separator is not None else "," if mode == "csv" else "\t"
		ending = row_separator if row_separator is not None else "\r\n"
		for row in ([names] if headers else []) + rows:
			fields = []
			for value in row:
				text = result_text(value, nullvalue)
				if any(char in text for char in ('"', '\r', '\n')) or (separator and separator in text) or (ending and ending in text) or (len(row) == 1 and not text):
					text = '"' + text.replace('"', '""') + '"'
				fields.append(text)
			output.write(separator.join(fields) + ending)
		if hasattr(sys.stdout, "buffer"):
			# Avoid Windows newline translation corrupting CSV records and fields.
			sys.stdout.flush()
			sys.stdout.buffer.write(output.getvalue().encode(sys.stdout.encoding or "utf-8"))
			sys.stdout.buffer.flush()
		else:
			sys.stdout.write(output.getvalue())
	elif mode in ("column", "table", "box"):
		print_grid(names, rows, headers, mode, nullvalue, column_widths)
	elif mode == "markdown":
		def cell(value):
			return result_text(value, nullvalue).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
		print("| " + " | ".join(cell(name) if headers else "" for name in names) + " |")
		print("| " + " | ".join("---" for name in names) + " |")
		for row in rows:
			print("| " + " | ".join(cell(value) for value in row) + " |")
	elif mode == "line":
		for index, row in enumerate(rows):
			if index:
				print()
			for name, value in zip(names, row):
				text = result_text(value, nullvalue).replace("\r", "\\r").replace("\n", "\\n")
				print(f"{name} = {text}" if headers else text)
	else:
		separator = "," if mode == "quote" else column_separator if column_separator is not None else "|"
		ending = "\n" if mode == "quote" or row_separator is None else row_separator
		format_value = sql_literal if mode == "quote" else lambda value: result_text(value, nullvalue)
		if headers:
			print(separator.join(format_value(name) for name in names), end=ending)
		for row in rows:
			print(separator.join(format_value(value) for value in row), end=ending)

def execute_sql(cursor, sql, headers=False, mode="list", nullvalue="", column_separator=None, row_separator=None, column_widths=()):
	try:
		cursor.execute(sql)

		if cursor.description:
			names = [column[0] for column in cursor.description]
			print_results(names, cursor.fetchall(), headers, mode, nullvalue, column_separator, row_separator, column_widths)

	except sqlite3.Error as error:
		print("SQL error:", error)

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


def main():
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
	column_separator = None
	row_separator = None
	column_widths = ()

	try:
		while True:
			inp = input("PaleSQL: ").strip()

			if not inp:
				continue

			command, _, argument = inp.partition(" ")
			command = command.lower()
			argument = argument.strip()

			if command == ".help":
				pale_help()
				continue

			if command == ".version":
				pale_version()
				continue

			if command == ".width":
				try:
					widths = tuple(int(value) for value in argument.split())
					if not widths or any(width < 0 for width in widths):
						raise ValueError
				except ValueError:
					print("Usage: .width <n1> <n2> ... (non-negative integers; 0 = auto)")
					continue
				column_widths = widths
				continue

			if command == ".separator":
				try:
					separators = parse_separators(argument)
				except ValueError as error:
					print(error)
					continue
				column_separator = separators[0]
				if len(separators) == 2:
					row_separator = separators[1]
				continue

			if command == ".nullvalue":
				if not argument:
					print('Usage: .nullvalue <text> (use "" for empty text)')
					continue
				if len(argument) >= 2 and argument[0] in ("\"", "'") and argument[-1] == argument[0]:
					argument = argument[1:-1]
				nullvalue = argument
				continue

			if command == ".mode":
				if argument.lower() not in OUTPUT_MODES:
					print("Usage: .mode " + "|".join(OUTPUT_MODES))
					continue
				mode = argument.lower()
				column_separator = None
				row_separator = None
				continue

			if command == ".headers":
				if argument.lower() not in ("on", "off"):
					print("Usage: .headers on|off")
					continue

				headers = argument.lower() == "on"
				continue

			if command == ".clear":
				sql = ""
				print("Buffer is cleaned")
				continue

			if command in (".exit", ".quit") or inp.lower() in ("exit", "quit"):
				break

			if command == ".open":
				if not argument:
					print("No name of database")
					continue

				if con is not None:
					con.close()

				try:
					con, cursor = pale_open(argument)

				except sqlite3.Error as error:
					print("Database error:", error)
					con = None
					cursor = None
					continue

				sql = ""
				print("Database opened")
				continue

			if con is None:
				print("No database opened")
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
					print("No table name provided")
					continue

				pale_columns(cursor, argument)
				continue

			if command == ".foreignkeys":
				if not argument:
					print("No table name provided")
					continue

				pale_foreignkeys(cursor, argument)
				continue

			if command == ".close":
				con.close()
				con = None
				cursor = None
				sql = ""

				print("Database closed")
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
				print(f"Unknown command: {command}")
				continue

			sql += inp + "\n"

			if sqlite3.complete_statement(sql):
				execute_sql(cursor, sql, headers, mode, nullvalue, column_separator, row_separator, column_widths)
				sql = ""

	finally:
		if con is not None:
			con.close()


if __name__ == "__main__":
	main()
