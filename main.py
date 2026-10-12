import sys
import sqlite3


PALESQL_VERSION = "0.2.0"
PYTHON_VERSION = sys.version.split()[0]
SQLITE_LIBRARY_VERSION = sqlite3.sqlite_version


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
		".schema [table] - show schema, optionally filtered by object name\n"
		".fullschema - show full schema and statistics\n"
		".lint - check foreign keys and suggest indexes\n"
		".clear - clear SQL buffer\n"
		".version - show PaleSQL, Python and SQLite versions\n"
		".exit or .quit - exit PaleSQL\n"
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

def pale_dbinfo(cursor, database_name):
	rows = get_databases(cursor)
	database = next((row for row in rows if row[1].lower() == database_name.lower()), None)

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

	print(f"{'size (bytes)':<20} | {info['page_size'] * info['page_count']}")
	print(f"{'free space (bytes)':<20} | {info['page_size'] * info['freelist_count']}")

def pale_tables(cursor, pattern):
	cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?", (pattern,))
	return cursor.fetchall()

def pale_views(cursor):
	cursor.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")
	return cursor.fetchall()

def pale_triggers(cursor):
	cursor.execute("SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name")
	return cursor.fetchall()

def pale_indexes(cursor, table_name):
	if table_name:
		cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? COLLATE NOCASE ORDER BY name", (table_name,))
	else:
		cursor.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")

	return cursor.fetchall()

def execute_sql(cursor, sql):
	try:
		cursor.execute(sql)

		if cursor.description:
			print(cursor.fetchall())

	except sqlite3.Error as error:
		print("SQL error:", error)

def pale_schema(cursor, table_name=""):
	sql = "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT GLOB 'sqlite_*'"
	parameters = ()

	if table_name:
		sql += " AND (name=? COLLATE NOCASE OR tbl_name=? COLLATE NOCASE)"
		parameters = (table_name, table_name)

	sql += " ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 ELSE 3 END, name"
	cursor.execute(sql, parameters)
	rows = cursor.fetchall()

	if not rows:
		if table_name:
			print(f"Object '{table_name}' not found")
		else:
			print("-- No schema found")
		return

	for sql, in rows:
		print(sql.rstrip().rstrip(";") + ";")


def pale_lint(cursor):
	cursor.execute("SELECT name FROM main.sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*' ORDER BY name")
	tables = [row[0] for row in cursor.fetchall()]
	issues = 0
	has_foreign_keys = False

	for table in tables:
		quoted_table = table.replace("'", "''")
		cursor.execute(f"PRAGMA main.foreign_key_list('{quoted_table}');")
		foreign_keys = cursor.fetchall()

		if not foreign_keys:
			continue

		has_foreign_keys = True
		try:
			cursor.execute(f"PRAGMA main.foreign_key_check('{quoted_table}');")
			for child, rowid, parent, key_id in cursor.fetchall():
				print(f"Foreign key violation: {child}, rowid {rowid}, key {key_id}, parent {parent}")
				issues += 1
		except sqlite3.Error as error:
			print(f"Foreign key error in '{table}': {error}")
			issues += 1

		cursor.execute(f"PRAGMA main.index_list('{quoted_table}');")
		indexes = cursor.fetchall()
		indexed_columns = []

		for seq, name, unique, origin, partial in indexes:
			if partial:
				continue
			quoted_index = name.replace("'", "''")
			cursor.execute(f"PRAGMA main.index_info('{quoted_index}');")
			indexed_columns.append([row[2].lower() if row[2] else None for row in cursor.fetchall()])

		cursor.execute(f"PRAGMA main.table_info('{quoted_table}');")
		columns = cursor.fetchall()
		primary_key = [row[1].lower() for row in sorted(columns, key=lambda row: row[5]) if row[5]]
		indexed_columns.append(primary_key)
		keys = {}

		for key_id, seq, parent, child_column, parent_column, on_update, on_delete, match in foreign_keys:
			keys.setdefault(key_id, []).append((seq, child_column))
			if seq == 0 and parent.lower() not in [name.lower() for name in tables]:
				print(f"Missing parent table: '{table}', key {key_id}, parent '{parent}'")
				issues += 1

		for key_id, columns in keys.items():
			names = [name for seq, name in sorted(columns)]
			wanted = {name.lower() for name in names}
			if not any(set(index[:len(names)]) == wanted for index in indexed_columns):
				print(f"Consider an index: '{table}' ({', '.join(names)}), foreign key {key_id}; no full index with these leading columns")
				issues += 1

	cursor.execute("PRAGMA foreign_keys;")
	if has_foreign_keys and not cursor.fetchone()[0]:
		print("Foreign key enforcement is disabled")
		issues += 1

	if not issues:
		print("No foreign key or index issues found")


def pale_fullschema(cursor):
	cursor.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT GLOB 'sqlite_*' ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 ELSE 3 END, name")
	rows = cursor.fetchall()

	if not rows:
		print("-- No schema found")

	for sql, in rows:
		print(sql.rstrip().rstrip(";") + ";")

	cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'sqlite_stat*' ORDER BY name")
	tables = cursor.fetchall()

	if not tables:
		print("-- No statistics found")
		return

	print("ANALYZE sqlite_schema;")

	for table_name, in tables:
		quoted_name = '"' + table_name.replace('"', '""') + '"'
		print(f"DELETE FROM {quoted_name};")
		cursor.execute(f"SELECT * FROM {quoted_name}")
		rows = cursor.fetchall()

		for row in rows:
			placeholders = ", ".join("quote(?)" for value in row)
			cursor.execute(f"SELECT {placeholders}", row)
			values = ", ".join(cursor.fetchone())
			print(f"INSERT INTO {quoted_name} VALUES ({values});")

	print("ANALYZE sqlite_schema;")


def pale_columns(cursor, table_name):
	quoted_name = table_name.replace("'", "''")
	cursor.execute(f"PRAGMA table_info('{quoted_name}');")
	rows = cursor.fetchall()

	if not rows:
		print(f"Table '{table_name}' not found")
		return

	print(f"{'name':<20} | {'type':<12} | {'NOT NULL':<8} | {'DEFAULT':<20} | PRIMARY KEY")

	for cid, name, column_type, not_null, default, primary_key in rows:
		default = "NULL" if default is None else default
		print(f"{name:<20} | {column_type:<12} | {not_null:<8} | {default:<20} | {primary_key}")


def pale_foreignkeys(cursor, table_name):
	quoted_name = table_name.replace("'", "''")
	cursor.execute(f"PRAGMA foreign_key_list('{quoted_name}');")
	rows = cursor.fetchall()

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
				execute_sql(cursor, sql)
				sql = ""

	finally:
		if con is not None:
			con.close()


if __name__ == "__main__":
	main()
