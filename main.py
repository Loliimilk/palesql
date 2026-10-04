import sys
import sqlite3

PALESQL_VERSION = "0.1.0"
PYTHON_VERSION = sys.version.split()[0]
SQLITE_LIBRARY_VERSION = sqlite3.sqlite_version

print (f"""\nhaiiiiiiiiii :3 rawr certified SQL CLI :p 
PaleSQL {PALESQL_VERSION} by Shikaru Software
Type .help for commands.\n""")

sql = ""

con = None
cursor = None

def pale_open(name):
	if name.lower().endswith(".db"):
		con = sqlite3.connect(name)
	else:
		con = sqlite3.connect(name+".db")
	cursor = con.cursor()
	return con, cursor

def pale_help():
	print(
		'.help - show commands\n'
		'.open <database> - open or create database\n'
		'.close - close current database\n'
		'.database - show current main database\n'
		'.databases - show all attached databases\n'
		'.clear - clear SQL buffer\n'
		'.version - show PaleSQL, Python and SQLite versions\n'
		'.exit or .quit - exit PaleSQL\n'
		)
def pale_version():
	print(
		f"PaleSQL version {PALESQL_VERSION}\n"
		f"Python version {PYTHON_VERSION}\n"
		f"SQLite library version {SQLITE_LIBRARY_VERSION}\n"
	)

def pale_database():
	cursor.execute('''PRAGMA database_list;''')
	print (f"{'seq':<5} | {'name':<5} | file")
	row = next((row for row in cursor.fetchall() if row[1] == "main"), None)
	print(f"{row[0]:<5} | {row[1]:<5} | {row[2]}")

def pale_databases():
	cursor.execute('''PRAGMA database_list;''')
	print (f"{'seq':<5} | {'name':<5} | file")
	for row in cursor.fetchall():
		print(f"{row[0]:<5} | {row[1]:<5} | {row[2]}")



while True:
	inp = input("PaleSQL: ").strip()

	command, _, argument = inp.partition(" ")  
	argument = argument.strip()
	command = command.lower()

	if command == ".help":
		pale_help()
		continue

	if command == ".version":
		pale_version()
		continue

	if command == ".open":
		if argument == "":
			print("No name of database")
			continue
		if con is not None:
			con.close()

		con, cursor = pale_open(argument)
		print("Database opened")
		sql = ""
		continue

	if command == ".clear":
		print("Buffer is cleaned")
		sql = ""
		continue

	if inp.lower() in (".exit", "exit", "quit", ".quit"):
		break

	if con is None:
		print("No database opened")
		continue

	if command == ".database":
		pale_database()
		continue

	if command == ".databases":
		pale_databases()
		continue

	if command == ".close":
		con.close()
		con = None
		cursor = None
		sql = ""
		print("Database closed")
		continue

	sql += inp + '\n'

	if sqlite3.complete_statement(sql):
		try:
			cursor.execute(sql)
			if cursor.description:
				print(cursor.fetchall())
			sql = ""
			con.commit()
		except sqlite3.Error as error:
			print("SQL error:", error)
			sql=""

if con is not None:
	con.close()