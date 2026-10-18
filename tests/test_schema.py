import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class StructureTests(unittest.TestCase):
	def setUp(self):
		self.con = sqlite3.connect(":memory:")
		self.addCleanup(self.con.close)

	def output(self, function, *args):
		stream = io.StringIO()
		with contextlib.redirect_stdout(stream):
			function(self.con.cursor(), *args)
		return stream.getvalue()

	def setup_schema(self):
		self.con.executescript("""
			CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT 'guest');
			CREATE TABLE orders(id INTEGER PRIMARY KEY, user_id REFERENCES users(id));
			CREATE INDEX orders_user ON orders(user_id);
			CREATE VIEW user_names AS SELECT name FROM users;
			CREATE TRIGGER users_added AFTER INSERT ON users BEGIN SELECT 1; END;
		""")

	def test_lists_and_patterns(self):
		self.setup_schema()
		self.assertEqual(main.pale_tables(self.con.cursor(), "user%"), [("users",)])
		self.assertEqual(main.pale_views(self.con.cursor()), [("user_names",)])
		self.assertEqual(main.pale_triggers(self.con.cursor()), [("users_added",)])
		self.assertEqual(main.pale_indexes(self.con.cursor(), "ORDERS"), [("orders_user",)])
		self.assertEqual(main.pale_indexes(self.con.cursor(), ""), [("orders_user",)])
		result = self.output(main.pale_schema, "user%")
		self.assertIn('"users"', result)
		self.assertIn('"user_names"', result)
		self.assertIn('"users_added"', result)
		self.assertNotIn('"orders"', result)

	def test_schema_exact_object_and_table_dependencies(self):
		self.setup_schema()
		self.assertIn('"orders_user"', self.output(main.pale_schema, "orders"))
		self.assertEqual(self.output(main.pale_schema, "orders_user").count("CREATE "), 1)
		self.assertIn("No schema found", self.output(main.pale_schema, "absent"))

	def test_generated_and_hidden_columns(self):
		self.con.execute("CREATE TABLE generated(a INT, b INT AS (a+1), c INT AS (a+2) STORED)")
		result = self.output(main.pale_columns, "generated")
		self.assertIn("generated virtual", result)
		self.assertIn("generated stored", result)
		self.con.execute("CREATE VIRTUAL TABLE docs USING fts5(body)")
		self.assertIn("hidden", self.output(main.pale_columns, "docs"))

	def test_column_defaults_and_constraints(self):
		self.setup_schema()
		result = self.output(main.pale_columns, "users")
		self.assertIn("'guest'", result)
		self.assertIn("NOT NULL", result)
		self.assertIn("PRIMARY KEY", result)
		self.assertIn("(none)", result)

	def test_missing_table_and_empty_foreign_keys(self):
		self.setup_schema()
		self.assertIn("not found", self.output(main.pale_foreignkeys, "absent"))
		self.assertIn("not found", self.output(main.pale_columns, "absent"))
		self.assertEqual(self.output(main.pale_foreignkeys, "users"), "No foreign keys found\n")
		self.assertIn("user_id", self.output(main.pale_foreignkeys, "orders"))

	def test_attached_and_temp_objects(self):
		self.setup_schema()
		self.con.executescript("""
			ATTACH ':memory:' AS extra;
			CREATE TABLE extra.users(other INT PRIMARY KEY);
			CREATE TABLE extra.child(ref REFERENCES users(other));
			CREATE INDEX extra.child_ref ON child(ref);
			CREATE VIEW extra.names AS SELECT other FROM users;
			CREATE TRIGGER extra.added AFTER INSERT ON users BEGIN SELECT 1; END;
			CREATE TEMP TABLE scratch(x);
		""")
		self.assertIn(("extra.users",), main.pale_tables(self.con.cursor(), "extra.user%"))
		self.assertIn(("temp.scratch",), main.pale_tables(self.con.cursor(), "%"))
		self.assertIn(("extra.names",), main.pale_views(self.con.cursor()))
		self.assertIn(("extra.added",), main.pale_triggers(self.con.cursor()))
		self.assertEqual(main.pale_indexes(self.con.cursor(), "extra.child"), [("extra.child_ref",)])
		self.assertIn("other", self.output(main.pale_columns, "extra.users"))
		self.assertIn("ref", self.output(main.pale_foreignkeys, "extra.child"))
		self.assertIn('"extra"."users"', self.output(main.pale_schema, "extra.user%"))
		restored = sqlite3.connect(":memory:")
		self.addCleanup(restored.close)
		restored.executescript(self.output(main.pale_fullschema))
		self.assertEqual(restored.execute("SELECT count(*) FROM extra.users").fetchone(), (0,))
		self.assertEqual(restored.execute("SELECT count(*) FROM temp.scratch").fetchone(), (0,))

	def test_quoted_identifiers(self):
		self.con.execute('CREATE TABLE "it\'s a table" ("a""b" INTEGER)')
		self.assertIn('a"b', self.output(main.pale_columns, "it's a table"))
		self.assertIn("No foreign keys", self.output(main.pale_foreignkeys, "it's a table"))
		restored = sqlite3.connect(":memory:")
		self.addCleanup(restored.close)
		restored.executescript(self.output(main.pale_fullschema))
		self.assertIn("not found", self.output(main.pale_columns, "x'); DROP TABLE users; --"))

	def test_schema_comments(self):
		self.con.executescript('CREATE TABLE /* comment */ "odd name"(x); CREATE UNIQUE INDEX /* comment */ "odd index" ON "odd name"(x);')
		restored = sqlite3.connect(":memory:")
		self.addCleanup(restored.close)
		restored.executescript(self.output(main.pale_fullschema))
		self.assertEqual(restored.execute('SELECT count(*) FROM "odd name"').fetchone(), (0,))

	def test_comments_touching_object_names(self):
		self.con.executescript("CREATE TABLE t(x); CREATE VIEW v/*comment*/AS SELECT x FROM t; CREATE VIEW v2--comment\nAS SELECT x FROM t; CREATE TRIGGER tr/*comment*/AFTER INSERT ON t BEGIN SELECT 1; END;")
		for function in (main.pale_schema, main.pale_fullschema):
			with self.subTest(command=function.__name__):
				restored = sqlite3.connect(":memory:")
				try:
					restored.executescript(self.output(function))
					self.assertEqual(restored.execute("SELECT * FROM v").description[0][0], "x")
					self.assertEqual(restored.execute("SELECT * FROM v2").description[0][0], "x")
				finally:
					restored.close()

	def test_temp_table_takes_precedence(self):
		self.con.executescript("CREATE TABLE p(id PRIMARY KEY); CREATE TABLE t(main_col REFERENCES p(id)); CREATE TEMP TABLE t(temp_col);")
		self.assertEqual(self.con.execute("SELECT * FROM t").description[0][0], "temp_col")
		self.assertIn("temp_col", self.output(main.pale_columns, "T"))
		self.assertNotIn("main_col", self.output(main.pale_columns, "T"))
		self.assertEqual(self.output(main.pale_foreignkeys, "t"), "No foreign keys found\n")
		self.assertIn("main_col", self.output(main.pale_columns, "main.t"))
		self.assertIn("main_col", self.output(main.pale_foreignkeys, "main.t"))

	def test_unicode_identifiers_are_distinct(self):
		self.con.executescript('CREATE TABLE "Ä"(upper_col); CREATE TABLE "ä"(lower_col); CREATE INDEX upper_index ON "Ä"(upper_col); CREATE INDEX lower_index ON "ä"(lower_col);')
		self.assertIn("lower_col", self.output(main.pale_columns, "ä"))
		self.assertNotIn("upper_col", self.output(main.pale_columns, "ä"))
		self.assertEqual(main.pale_indexes(self.con.cursor(), "ä"), [("lower_index",)])
		self.assertEqual(main.pale_indexes(self.con.cursor(), "Ä"), [("upper_index",)])
		self.con.executescript('ATTACH ":memory:" AS "Ä"; ATTACH ":memory:" AS "ä"; PRAGMA "Ä".user_version=11; PRAGMA "ä".user_version=22;')
		result = self.output(main.pale_dbinfo, "ä")
		info = {key.strip(): value for key, value in (line.split(" | ", 1) for line in result.splitlines())}
		self.assertEqual(info["user version"], "22")

	def test_fullschema_fts_and_stats_replay(self):
		self.setup_schema()
		self.con.executescript("CREATE VIRTUAL TABLE docs USING fts5(body); INSERT INTO users VALUES(1,'private payload'); ANALYZE;")
		result = self.output(main.pale_fullschema)
		self.assertNotIn('CREATE TABLE "main"."docs_config"', result)
		self.assertNotIn("private payload", result)
		restored = sqlite3.connect(":memory:")
		self.addCleanup(restored.close)
		restored.executescript(result)
		self.assertEqual(self.con.execute("SELECT * FROM sqlite_stat1 ORDER BY tbl,idx").fetchall(), restored.execute("SELECT * FROM sqlite_stat1 ORDER BY tbl,idx").fetchall())
		self.assertEqual(restored.execute("SELECT count(*) FROM users").fetchone(), (0,))

	def test_stats_literals(self):
		self.setup_schema()
		self.con.executescript("INSERT INTO users VALUES(1,'x'); ANALYZE;")
		for value in ("a'b", "a\x00b", b"\x00\xff", None):
			self.con.execute("UPDATE sqlite_stat1 SET stat=?", (value,))
			restored = sqlite3.connect(":memory:")
			try:
				restored.executescript(self.output(main.pale_fullschema))
				self.assertEqual(restored.execute("SELECT stat FROM sqlite_stat1").fetchall(), self.con.execute("SELECT stat FROM sqlite_stat1").fetchall())
			finally:
				restored.close()

	def test_lint_collation(self):
		self.con.executescript("CREATE TABLE p(id TEXT COLLATE NOCASE PRIMARY KEY); CREATE TABLE c(pid TEXT REFERENCES p(id)); CREATE INDEX c_pid ON c(pid); PRAGMA foreign_keys=ON;")
		self.assertIn("Consider an index", self.output(main.pale_lint))
		self.con.execute("CREATE INDEX good ON c(pid COLLATE NOCASE)")
		self.assertEqual(self.output(main.pale_lint), "No foreign key or index issues found\n")

	def test_lint_composite_and_partial_indexes(self):
		self.con.executescript("CREATE TABLE p(a,b,PRIMARY KEY(a,b)); CREATE TABLE c(x,y,z,FOREIGN KEY(x,y) REFERENCES p(a,b)); PRAGMA foreign_keys=ON;")
		self.assertIn("Consider an index", self.output(main.pale_lint))
		self.con.execute("CREATE INDEX bad ON c(x,y) WHERE z=1")
		self.assertIn("Consider an index", self.output(main.pale_lint))
		self.con.execute("CREATE INDEX good ON c(y,x,z)")
		self.assertEqual(self.output(main.pale_lint), "No foreign key or index issues found\n")

	def test_lint_parent_primary_key(self):
		self.con.executescript("CREATE TABLE p(id INTEGER PRIMARY KEY); CREATE TABLE c(id INTEGER PRIMARY KEY REFERENCES p); PRAGMA foreign_keys=ON;")
		self.assertEqual(self.output(main.pale_lint), "No foreign key or index issues found\n")

	def test_lint_schema_errors_and_missing_parent(self):
		self.con.executescript("CREATE TABLE p(a); CREATE TABLE c(x REFERENCES p(a)); CREATE TABLE orphan(x REFERENCES missing);")
		result = self.output(main.pale_lint)
		self.assertIn("Foreign key error", result)
		self.assertIn("Missing parent table", result)
		self.assertIn("enforcement is disabled", result)

	def test_lint_violation_and_attached_database(self):
		self.con.executescript("ATTACH ':memory:' AS extra; CREATE TABLE extra.p(id PRIMARY KEY); CREATE TABLE extra.c(id PRIMARY KEY REFERENCES p) WITHOUT ROWID; INSERT INTO extra.c VALUES(9);")
		self.assertIn("Foreign key violation: extra.c, rowid None", self.output(main.pale_lint))

	def test_dbinfo_wal_files(self):
		with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
			path = Path(folder) / "data.db"
			con = sqlite3.connect(path)
			try:
				con.executescript("PRAGMA journal_mode=WAL; CREATE TABLE t(x); INSERT INTO t VALUES(zeroblob(20000));")
				stream = io.StringIO()
				with contextlib.redirect_stdout(stream):
					main.pale_dbinfo(con.cursor(), "MAIN")
				info = {key.strip(): value for key, value in (line.split(" | ", 1) for line in stream.getvalue().splitlines())}
				self.assertEqual(int(info["WAL bytes"]), Path(str(path) + "-wal").stat().st_size)
				self.assertEqual(int(info["file bytes"]), path.stat().st_size)
				self.assertEqual(int(info["logical size (bytes)"]), con.execute("PRAGMA page_size").fetchone()[0] * con.execute("PRAGMA page_count").fetchone()[0])
			finally:
				con.close()

	def test_read_only_and_cli(self):
		self.setup_schema()
		before = self.con.serialize()
		commands = [".tables", ".schema user%", ".columns users", ".indexes", ".foreignkeys orders", ".views", ".triggers", ".fullschema", ".dbinfo", ".lint"]
		stream = io.StringIO()
		with patch("builtins.input", side_effect=commands + [".open demo"] + commands + [".help", ".exit"]), patch("main.pale_open", return_value=(type("Connection", (), {"close": lambda self: None})(), self.con.cursor())), contextlib.redirect_stdout(stream):
			main.main()
		self.assertEqual(before, self.con.serialize())
		self.assertEqual(stream.getvalue().count("No database opened"), 10)
		self.assertNotIn("Unknown command", stream.getvalue())
		self.assertIn(".schema [pattern]", stream.getvalue())


if __name__ == "__main__":
	unittest.main()
