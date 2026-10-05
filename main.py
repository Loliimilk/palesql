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
		".tables [pattern] - show tables, optionally filtered by pattern\n"
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

def pale_tables(cursor, pattern):
	cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?", (pattern,))
	return cursor.fetchall()

def execute_sql(cursor, sql):
	try:
		cursor.execute(sql)

		if cursor.description:
			print(cursor.fetchall())

	except sqlite3.Error as error:
		print("SQL error:", error)


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