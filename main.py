import sqlite3

sql = ""

con = None
cursor = None

def pale_open(name):
	con = sqlite3.connect(name+".db")
	cursor = con.cursor()
	return con, cursor



while True:
	inp = input("PaleSQL: ").strip()

	command, _, argument = inp.partition(" ")

	if inp.startswith(".open "):
		if con:
			con.close()

		con, cursor = pale_open(argument)
		print("Database opened")
		continue

	if inp in (".exit", "exit", "quit"):
		break

con.close()