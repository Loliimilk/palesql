import sqlite3

print ("""\nhaiiiiiiiiii :3 rawr certified SQL CLI :p 
PaleSQL 0.1.0 by Shikaru Software
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
		'.open - open database \n'
		'.close - close database \n'
		'.exit or .quit - exit PaleSQL\n'
		'.clear - clear SQL buffer\n'
		)

while True:
	inp = input("PaleSQL: ").strip()

	command, _, argument = inp.partition(" ")
	argument = argument.strip()
	command = command.lower()

	if command == ".help":
		pale_help()
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