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
		".fullschema - show full schema and statistics\n"
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

def pale_schema(cursor, table_name):
	cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
	row = cursor.fetchone()

	if row is None:
		print(f"Table '{table_name}' not found")
		return

	print(row[0])


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
				if not argument:
					print("No table name provided")
					continue

				pale_schema(cursor, argument)
				continue

			if command == ".fullschema":
				pale_fullschema(cursor)
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
