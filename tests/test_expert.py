"""Independent behavior tests for the Python index advisor."""

import contextlib
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main import analyze_expert, parse_expert_arguments


def recommended_indexes(report):
	"""Extract executable index definitions without depending on generated names."""
	statements = []
	pending = ""
	for line in report.splitlines():
		if not pending and not re.match(r"\s*CREATE\s+(?:UNIQUE\s+)?INDEX\b", line, re.IGNORECASE):
			continue
		pending += line + "\n"
		if sqlite3.complete_statement(pending):
			statements.append(pending.strip())
			pending = ""
	if pending:
		raise AssertionError("Advisor emitted an incomplete CREATE INDEX statement")
	return statements


class ExpertBackendTests(unittest.TestCase):

	def setUp(self):
		self.con = sqlite3.connect(":memory:", isolation_level=None)
		self.addCleanup(self.con.close)
		self.con.executescript("""
			CREATE TABLE users(id INTEGER PRIMARY KEY, city TEXT, name TEXT, score INTEGER);
			CREATE TABLE orders(id INTEGER PRIMARY KEY, user_id INTEGER, amount INTEGER);
		""")
		self.con.executemany("INSERT INTO users VALUES(?, ?, ?, ?)", [
			(index, f"city-{index % 10}", f"User-{index}", index % 31) for index in range(1, 201)
		])
		self.con.executemany("INSERT INTO orders VALUES(?, ?, ?)", [
			(index, index % 200 + 1, index * 5) for index in range(1, 401)
		])

	def apply_and_verify(self, connection, query, *, report=None, parameters=(), database="main"):
		if report is None:
			report = analyze_expert(connection, query)
		statements = recommended_indexes(report)
		self.assertTrue(statements, report)
		quoted_database = '"' + database.replace('"', '""') + '"'
		before = {row[0] for row in connection.execute(f"SELECT name FROM {quoted_database}.sqlite_schema WHERE type='index'")}
		for statement in statements:
			connection.execute(statement)
		after = {row[0] for row in connection.execute(f"SELECT name FROM {quoted_database}.sqlite_schema WHERE type='index'")}
		created = after - before
		self.assertTrue(created, report)
		plan = "\n".join(str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + query, parameters))
		self.assertTrue(any(name in plan for name in created), f"Recommendations were not used:\n{report}\n{plan}")
		return report

	def test_simple_recommendations_are_executable_and_used(self):
		query = "SELECT * FROM users WHERE city = 'city-2';"
		report = analyze_expert(self.con, query)
		self.assertEqual(self.con.execute("PRAGMA index_list(users)").fetchall(), [])
		self.apply_and_verify(self.con, query, report=report)

	def test_view_filters_are_resolved_to_base_table(self):
		self.con.execute("CREATE VIEW active_users AS SELECT id, name, city FROM users WHERE score >= 0")
		self.apply_and_verify(self.con, "SELECT * FROM active_users WHERE city = 'city-2';")

	def test_cte_and_subquery_are_planned_by_sqlite(self):
		for query in (
			"WITH selected AS (SELECT * FROM users) SELECT * FROM selected WHERE city = 'city-2';",
			"SELECT * FROM (SELECT * FROM users) AS selected WHERE city = 'city-2';",
		):
			with self.subTest(query=query):
				connection = sqlite3.connect(":memory:", isolation_level=None)
				with contextlib.closing(connection):
					connection.deserialize(self.con.serialize())
					self.apply_and_verify(connection, query)

	def test_join_recommendations_are_used(self):
		query = "SELECT users.name, orders.amount FROM users JOIN orders ON orders.user_id = users.id WHERE users.city = 'city-2';"
		self.apply_and_verify(self.con, query)

	def test_quoted_unicode_identifiers_and_literal_dots(self):
		self.con.execute('CREATE TABLE "用户. archive"("名前""字段" TEXT, "都市.住" TEXT, extra TEXT)')
		self.con.executemany('INSERT INTO "用户. archive" VALUES(?, ?, ?)', [(f"name-{i}", f"city-{i % 9}", "value") for i in range(100)])
		self.apply_and_verify(self.con, 'SELECT * FROM "用户. archive" WHERE "都市.住" = \'city-2\';')

	def test_existing_full_index_is_not_duplicated(self):
		self.con.execute("CREATE INDEX existing_users_city ON users(city)")
		report = analyze_expert(self.con, "SELECT * FROM users WHERE city = 'city-2';")
		self.assertEqual(recommended_indexes(report), [], report)

	def test_existing_partial_index_satisfies_matching_query(self):
		self.con.execute("CREATE INDEX existing_users_city ON users(city) WHERE city IS NOT NULL")
		report = analyze_expert(self.con, "SELECT * FROM users WHERE city = 'city-2';")
		self.assertEqual(recommended_indexes(report), [], report)

	def test_existing_expression_index_is_respected(self):
		self.con.execute("CREATE INDEX existing_users_lower_name ON users(lower(name))")
		report = analyze_expert(self.con, "SELECT * FROM users WHERE lower(name) = 'user-17';")
		self.assertEqual(recommended_indexes(report), [], report)

	def test_parameters_are_prepared_without_values(self):
		for expression, bindings in (
			("city = ?", (None,)),
			("city = ?3", (None, None, None)),
			("city = :city", {"city": None}),
			("city = @city", {"city": None}),
			("city = $city", {"city": None}),
		):
			with self.subTest(expression=expression), contextlib.closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
				connection.deserialize(self.con.serialize())
				self.apply_and_verify(connection, f"SELECT * FROM users WHERE {expression};", parameters=bindings)

	def test_analyzing_dml_never_executes_it(self):
		self.con.execute("CREATE TABLE archive(id INTEGER, city TEXT, name TEXT, score INTEGER)")
		before = self.con.execute("SELECT * FROM users ORDER BY id").fetchall()
		changes = self.con.total_changes
		for query in (
			"UPDATE users SET name = 'changed' WHERE city = 'city-2';",
			"DELETE FROM users WHERE city = 'city-2';",
			"INSERT INTO archive SELECT * FROM users WHERE city = 'city-2';",
		):
			with self.subTest(query=query):
				report = analyze_expert(self.con, query)
				self.assertIsInstance(report, str)
				self.assertEqual(self.con.execute("SELECT * FROM users ORDER BY id").fetchall(), before)
				self.assertEqual(self.con.execute("SELECT count(*) FROM archive").fetchone()[0], 0)
				self.assertEqual(self.con.total_changes, changes)

	def test_multiple_queries_are_analyzed_without_side_effects(self):
		report = analyze_expert(self.con, "SELECT * FROM users WHERE city = 'city-2'; DELETE FROM orders WHERE user_id = 7;")
		self.assertTrue(recommended_indexes(report), report)
		self.assertEqual(self.con.execute("SELECT count(*) FROM users").fetchone()[0], 200)
		self.assertEqual(self.con.execute("SELECT count(*) FROM orders").fetchone()[0], 400)

	def test_invalid_sql_and_mutating_schema_commands_are_rejected(self):
		for query in (
			"SELECT FROM users;",
			"DROP TABLE users;",
			"CREATE TABLE injected(value);",
			"PRAGMA user_version=999;",
			"ATTACH ':memory:' AS injected;",
			"BEGIN; SELECT * FROM users; COMMIT;",
			"SELECT * FROM users; DROP TABLE users;",
			"SELECT * FROM users;\n.shell echo unexpected",
		):
			with self.subTest(query=query):
				with self.assertRaises(ValueError):
					analyze_expert(self.con, query)
				self.assertEqual(self.con.execute("SELECT count(*) FROM users").fetchone()[0], 200)
				self.assertEqual(self.con.execute("PRAGMA user_version").fetchone()[0], 0)
				self.assertEqual([row[1] for row in self.con.execute("PRAGMA database_list") if row[1] != "temp"], ["main"])

	def test_live_transaction_and_uncommitted_schema_survive_analysis(self):
		self.con.execute("BEGIN")
		self.con.execute("CREATE TABLE pending(city TEXT, note TEXT)")
		self.con.execute("INSERT INTO pending VALUES('Tokyo', 'not committed')")
		changes = self.con.total_changes
		report = analyze_expert(self.con, "SELECT * FROM pending WHERE city='Tokyo';")
		self.assertTrue(recommended_indexes(report), report)
		self.assertTrue(self.con.in_transaction)
		self.assertEqual(self.con.total_changes, changes)
		self.assertEqual(self.con.execute("PRAGMA index_list(pending)").fetchall(), [])
		self.assertEqual(self.con.execute("SELECT note FROM pending").fetchone()[0], "not committed")
		self.con.rollback()
		self.assertIsNone(self.con.execute("SELECT name FROM sqlite_schema WHERE name='pending'").fetchone())

	def test_wal_database_is_analyzed_without_checkpoint_or_commit(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "source.db"
			with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as connection:
				self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
				connection.execute("CREATE TABLE users(city TEXT, name TEXT)")
				connection.executemany("INSERT INTO users VALUES(?, ?)", [(f"city-{i % 10}", f"name-{i}") for i in range(100)])
				connection.execute("BEGIN")
				connection.execute("INSERT INTO users VALUES('pending', 'uncommitted')")
				report = analyze_expert(connection, "SELECT * FROM users WHERE city='pending';")
				self.assertTrue(recommended_indexes(report), report)
				self.assertTrue(connection.in_transaction)
				self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
				self.assertEqual(connection.execute("SELECT count(*) FROM users").fetchone()[0], 101)
				connection.rollback()
				self.assertEqual(connection.execute("SELECT count(*) FROM users").fetchone()[0], 100)

	def test_attached_schema_recommendations_target_the_right_database(self):
		self.con.execute('ATTACH DATABASE ":memory:" AS "extra data"')
		self.con.execute('CREATE TABLE "extra data".users(id INTEGER, city TEXT, name TEXT)')
		self.con.executemany('INSERT INTO "extra data".users VALUES(?, ?, ?)', [(i, f"city-{i % 10}", f"name-{i}") for i in range(100)])
		self.apply_and_verify(self.con, 'SELECT * FROM "extra data".users WHERE city=\'city-2\';', database="extra data")
		self.assertEqual(self.con.execute("PRAGMA main.index_list(users)").fetchall(), [])

	def test_temp_shadowing_is_rejected_instead_of_analyzing_main_table(self):
		self.con.execute("CREATE TEMP TABLE users(city TEXT, other TEXT)")
		self.con.execute("INSERT INTO temp.users VALUES('temp-city', 'only temp')")
		with self.assertRaisesRegex(ValueError, "(?i)temp"):
			analyze_expert(self.con, "SELECT * FROM users WHERE city='temp-city';")
		self.assertEqual(self.con.execute("SELECT other FROM users").fetchone()[0], "only temp")

	def test_unfiltered_select_does_not_propose_useless_indexes(self):
		report = analyze_expert(self.con, "SELECT * FROM users;")
		self.assertEqual(recommended_indexes(report), [], report)

	def test_verbose_sampling_reports_approximation_and_preserves_source_statistics(self):
		before = self.con.execute("SELECT type, name, sql FROM sqlite_schema ORDER BY name").fetchall()
		report = analyze_expert(self.con, "SELECT * FROM users WHERE city='city-2';", sample=25, verbose=True)
		self.assertRegex(report.lower(), "candidate")
		self.assertIn("analysis_limit", report)
		self.assertEqual(self.con.execute("SELECT type, name, sql FROM sqlite_schema ORDER BY name").fetchall(), before)

	def test_argument_parser_preserves_sql_and_validates_percent(self):
		self.assertEqual(parse_expert_arguments(""), (0, False, None))
		self.assertEqual(parse_expert_arguments("--verbose --sample 100"), (100, True, None))
		query = "SELECT 'it''s --sample 8' FROM users;"
		self.assertEqual(parse_expert_arguments("--sample 10 " + query), (10, False, query))
		for arguments in ("--sample", "--sample -1", "--sample 101", "--sample 1.5", "--sample bad", "--unknown"):
			with self.subTest(arguments=arguments), self.assertRaises(ValueError):
				parse_expert_arguments(arguments)

	def test_equality_then_range_and_order_use_correct_composite_order(self):
		for query, expected in (
			("SELECT * FROM users WHERE city='city-2' AND score>10;", ["city", "score"]),
			("SELECT * FROM users WHERE city='city-2' ORDER BY name;", ["city", "name"]),
		):
			with self.subTest(query=query), contextlib.closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
				connection.deserialize(self.con.serialize())
				report = self.apply_and_verify(connection, query)
				indexes = connection.execute("PRAGMA index_list(users)").fetchall()
				self.assertEqual(len(indexes), 1, report)
				columns = [row[2] for row in connection.execute(f'PRAGMA index_info("{indexes[0][1]}")')]
				self.assertEqual(columns, expected, report)
				self.assertNotIn("TEMP B-TREE", report)

	def test_or_branches_can_need_two_indexes_at_once(self):
		query = "SELECT * FROM users WHERE city='city-2' OR name='User-34';"
		report = self.apply_and_verify(self.con, query)
		self.assertEqual(len(recommended_indexes(report)), 2, report)
		for _, name, *_ in self.con.execute("PRAGMA index_list(users)"):
			self.assertIn("USING INDEX " + name, report)

	def test_mixed_parameters_do_not_rewrite_literals_comments_or_identifiers(self):
		self.con.execute('CREATE TABLE odd("cash$column", "?literal", payload)')
		query = """SELECT '? :literal', "?literal", "cash$column" FROM odd
			WHERE "cash$column" = :city OR "?literal" = ?3
			OR payload = $variable::scope(suffix) OR payload = ? -- :comment
			/* $also_not_a_parameter */;"""
		report = analyze_expert(self.con, query)
		self.assertTrue(recommended_indexes(report), report)
		self.assertEqual(self.con.execute("SELECT count(*) FROM odd").fetchone()[0], 0)

	def test_parameters_with_invalid_numbers_are_still_rejected(self):
		for query in ("SELECT * FROM users WHERE city=?0", "SELECT * FROM users WHERE city=?999999999"):
			with self.subTest(query=query), self.assertRaises(ValueError):
				analyze_expert(self.con, query)

	def test_source_authorizer_is_preserved(self):
		def authorizer(action, arg1, arg2, database, trigger):
			return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DELETE else sqlite3.SQLITE_OK
		self.con.set_authorizer(authorizer)
		analyze_expert(self.con, "DELETE FROM users WHERE city='city-2';")
		with self.assertRaises(sqlite3.DatabaseError):
			self.con.execute("DELETE FROM users")
		self.assertEqual(self.con.execute("SELECT count(*) FROM users").fetchone()[0], 200)

	def test_extension_loading_is_not_allowed_during_analysis(self):
		with self.assertRaises(ValueError):
			analyze_expert(self.con, "SELECT load_extension('nonexistent')")

	def test_candidate_and_time_limits_are_explicit(self):
		with patch("main.MAX_CANDIDATES", 1):
			report = analyze_expert(self.con, "SELECT * FROM users WHERE city='city-2';")
		self.assertIn("Candidate search limited", report)
		before = self.con.serialize()
		with patch("main.MAX_SECONDS", 0), self.assertRaisesRegex(ValueError, "budget"):
			analyze_expert(self.con, "SELECT * FROM users WHERE city='city-2';")
		self.assertEqual(self.con.serialize(), before)

	def test_full_sampling_leaves_schema_data_and_statistics_unchanged(self):
		self.con.execute("ANALYZE")
		before = self.con.serialize()
		report = analyze_expert(self.con, "SELECT * FROM users WHERE city='city-2';", sample=100)
		self.assertEqual(self.con.serialize(), before)
		self.assertTrue(recommended_indexes(report), report)

	def test_grouping_and_distinct_can_use_index_order(self):
		for query in ("SELECT city,count(*) FROM users GROUP BY city;", "SELECT DISTINCT city FROM users;"):
			with self.subTest(query=query):
				report = analyze_expert(self.con, query)
				self.assertTrue(recommended_indexes(report), report)
				self.assertNotIn("TEMP B-TREE", report)

	def test_empty_database_and_unavailable_functions(self):
		with contextlib.closing(sqlite3.connect(":memory:")) as empty:
			self.assertIn("(no new indexes)", analyze_expert(empty, "SELECT 1;"))
		self.con.create_function("custom_function", 1, lambda value: value)
		with self.assertRaisesRegex(ValueError, "custom_function"):
			analyze_expert(self.con, "SELECT custom_function(city) FROM users;")


if __name__ == "__main__":
	unittest.main()
