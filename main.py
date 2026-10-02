import sqlite3

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



while True:
	inp = input("PaleSQL: ").strip()

	command, _, argument = inp.partition(" ")
	argument = argument.strip()

	if command.lower() == ".open":
		if argument == "":
			print("No name of dnfratabase")
			continue
		if con is not None:
			con.close()

		con, cursor = pale_open(argument)
		print("Database opened")
		sql = ""
		continue

	if inp.lower() in (".exit", "exit", "quit"):
		break

	if con is None:
		print("No database opened")
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