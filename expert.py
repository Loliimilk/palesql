"""A bounded index advisor using only the public Python sqlite3 API.

This is not SQLite's native sqlite3_expert. Candidate selection is exhaustive
within small limits; acceptance compares query-plan structure, not execution cost.
"""

import hashlib
import itertools
import math
import re
import sqlite3
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path


MAX_COLUMNS = 8
MAX_WIDTH = 3
MAX_CANDIDATES = 128
MAX_STATEMENTS = 32
MAX_SECONDS = 10
USAGE = "Usage: .expert [--verbose] [--sample 0..100] [SQL]"


def quote_name(name):
	return '"' + name.replace('"', '""') + '"'


def parse_expert_arguments(argument):
	sample, verbose = 0, False
	remaining = argument.strip()
	while remaining.startswith("--"):
		# Options are whitespace-delimited, whereas SQL is preserved verbatim.
		parts = remaining.split(None, 1)
		option = parts[0]
		remaining = parts[1].lstrip() if len(parts) > 1 else ""
		if option == "--verbose":
			verbose = True
		elif option == "--sample":
			parts = remaining.split(None, 1)
			if not parts or not re.fullmatch(r"[0-9]+", parts[0]):
				raise ValueError(USAGE)
			sample = int(parts[0])
			if not 0 <= sample <= 100:
				raise ValueError(USAGE)
			remaining = parts[1].lstrip() if len(parts) > 1 else ""
		else:
			raise ValueError(USAGE)
	if remaining[:1] in ("'", '"') and remaining[-1:] == remaining[:1] and len(remaining) > 1:
		remaining = remaining[1:-1]
	return sample, verbose, remaining or None


def sql_statements(sql):
	"""Split SQL without treating quoted/commented semicolons as boundaries."""
	if "\x00" in sql:
		raise ValueError("SQL contains a NUL character")
	statements, start, index, state, first = [], 0, 0, "", None
	while index < len(sql):
		char, pair = sql[index], sql[index:index + 2]
		if state == "--":
			if char == "\n":
				state = ""
		elif state == "/*":
			if pair == "*/":
				state = ""
				index += 1
		elif state:
			if char == state:
				if state != "]" and pair == state * 2:
					index += 1
				else:
					state = ""
		elif pair in ("--", "/*"):
			state = pair
			index += 1
		elif char in ("'", '"', "`", "["):
			if first is None:
				first = ""
			state = "]" if char == "[" else char
		elif char == ";":
			if first is not None:
				statements.append((sql[start:index + 1], first))
			start, first = index + 1, None
		elif not char.isspace() and first is None:
			match = re.match(r"[A-Za-z]+", sql[index:])
			first = match[0].upper() if match else ""
		index += 1
	if state not in ("", "--"):
		raise ValueError("incomplete SQL string, identifier or comment")
	if first is not None:
		statements.append((sql[start:], first))
	if not statements:
		raise ValueError("provide a SELECT, INSERT, UPDATE or DELETE statement")
	if len(statements) > MAX_STATEMENTS:
		raise ValueError(f"analyze at most {MAX_STATEMENTS} statements at a time")
	for _, keyword in statements:
		if keyword not in ("SELECT", "WITH", "VALUES", "INSERT", "UPDATE", "DELETE", "REPLACE"):
			raise ValueError("only SELECT, INSERT, UPDATE and DELETE statements can be analyzed")
	return [statement for statement, _ in statements]


@contextmanager
def snapshot(connection):
	if not hasattr(connection, "serialize"):
		raise ValueError(".expert needs Python 3.11+ with SQLite serialization support")
	databases = connection.execute("PRAGMA database_list").fetchall()
	if any(row[1] == "temp" for row in databases):
		if connection.execute("SELECT 1 FROM temp.sqlite_schema LIMIT 1").fetchone():
			raise ValueError(".expert does not support a non-empty temp schema; temp objects may shadow main tables")
	with tempfile.TemporaryDirectory(prefix="palesql-expert-") as directory:
		paths = []
		for number, name, _ in databases:
			if name == "temp":
				continue
			path = Path(directory) / f"snapshot-{number}.db"
			if connection.execute(f"PRAGMA {quote_name(name)}.page_count").fetchone()[0]:
				path.write_bytes(connection.serialize(name=name))
			paths.append((name, path))
		copy = sqlite3.connect(paths[0][1], isolation_level=None, cached_statements=0)
		try:
			for name, path in paths[1:]:
				copy.execute(f"ATTACH DATABASE ? AS {quote_name(name)}", (str(path),))
			for name, _ in paths:
				# These are disposable copies, including when the original uses WAL.
				copy.execute(f"PRAGMA {quote_name(name)}.journal_mode=MEMORY")
				copy.execute(f"PRAGMA {quote_name(name)}.synchronous=OFF")
			for setting in ("automatic_index", "foreign_keys", "recursive_triggers"):
				value = connection.execute("PRAGMA " + setting).fetchone()[0]
				copy.execute(f"PRAGMA {setting}={int(value)}")
			yield copy
		finally:
			copy.close()


def null_bindings(statement):
	"""Make named/numbered/mixed bindings positional, all bound to SQL NULL.

	The original statement is prepared by SQLite first, so invalid parameter
	names/numbers still fail. Quoted text and comments are copied unchanged.
	"""
	parts, count, index = [], 0, 0
	def name_char(char):
		return char.isalnum() or char in "_$" or ord(char) >= 128
	while index < len(statement):
		start, char = index, statement[index]
		if statement[index:index + 2] == "--":
			end = statement.find("\n", index + 2)
			index = len(statement) if end < 0 else end + 1
		elif statement[index:index + 2] == "/*":
			end = statement.find("*/", index + 2)
			index = len(statement) if end < 0 else end + 2
		elif char in ("'", '"', "`", "["):
			end_quote = "]" if char == "[" else char
			index += 1
			while index < len(statement):
				if statement[index] == end_quote:
					index += 1
					if end_quote != "]" and statement[index:index + 1] == end_quote:
						index += 1
						continue
					break
				index += 1
		elif char == "?" or (char in ":@$" and index + 1 < len(statement) and name_char(statement[index + 1])):
			index += 1
			if char == "?":
				while index < len(statement) and statement[index].isdigit():
					index += 1
			else:
				while index < len(statement):
					if name_char(statement[index]):
						index += 1
					elif statement[index:index + 2] == "::":
						index += 2
					else:
						break
				if statement[index:index + 1] == "(":
					end = statement.find(")", index + 1)
					if end >= 0:
						index = end + 1
			parts.append("?")
			count += 1
			continue
		elif char.isalpha() or char == "_" or ord(char) >= 128:
			index += 1
			while index < len(statement) and name_char(statement[index]):
				index += 1
		else:
			index += 1
		parts.append(statement[start:index])
	return "".join(parts), (None,) * count


def query_plans(connection, statements, reads=None):
	allowed = {sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION,
		sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_RECURSIVE}

	def authorize(action, table, column, database, trigger):
		if action not in allowed:
			return sqlite3.SQLITE_DENY
		if action == sqlite3.SQLITE_FUNCTION and column and column.lower() == "load_extension":
			return sqlite3.SQLITE_DENY
		if action == sqlite3.SQLITE_READ and reads is not None and column:
			reads.setdefault((database, table), Counter())[column] += 1
		return sqlite3.SQLITE_OK

	connection.set_authorizer(authorize)
	try:
		plans = []
		for statement in statements:
			try:
				rows = connection.execute("EXPLAIN QUERY PLAN " + statement).fetchall()
			except sqlite3.ProgrammingError as error:
				match = re.search(r"uses (\d+), and there are 0 supplied", str(error))
				if not match:
					raise
				normalized, bindings = null_bindings(statement)
				rows = connection.execute("EXPLAIN QUERY PLAN " + normalized, bindings).fetchall()
			plans.append([row[3] for row in rows])
		return plans
	finally:
		connection.set_authorizer(None)


def plan_score(plan):
	"""Lower is preferable. Covering scans alone do not count as improvements."""
	scans = sum(line.startswith("SCAN ") for line in plan)
	sorts = sum("USE TEMP B-TREE" in line for line in plan)
	automatic = sum("USING AUTOMATIC" in line for line in plan)
	constraints = sum(len(re.findall(r"(?:=|>|<)\?", line)) for line in plan if line.startswith("SEARCH "))
	return scans, sorts, automatic, -constraints


def scores(plans):
	return [plan_score(plan) for plan in plans]


def improves(candidate, previous):
	return all(new <= old for new, old in zip(candidate, previous)) and candidate != previous


def index_candidates(connection, reads, notes):
	tables = []
	for (database, table), columns in reads.items():
		if not database or not table or table.lower().startswith("sqlite_"):
			continue
		kind = connection.execute(f"SELECT type FROM {quote_name(database)}.sqlite_schema WHERE name=?", (table,)).fetchone()
		if not kind or kind[0] != "table":
			continue
		# Virtual tables and their shadow tables cannot accept ordinary indexes.
		table_types = {row[1]: row[2] for row in connection.execute(f"PRAGMA {quote_name(database)}.table_list")}
		if table_types.get(table) not in ("table", None):
			notes.append(f"Skipped virtual/shadow table: {database}.{table}")
			continue
		valid = {row[1] for row in connection.execute(f"PRAGMA {quote_name(database)}.table_xinfo({quote_name(table)})") if row[6] != 1}
		ordered = [name for name, _ in columns.most_common() if name in valid]
		if len(ordered) > MAX_COLUMNS:
			notes.append(f"Column search limited to {MAX_COLUMNS} columns for {database}.{table}.")
		ordered = ordered[:MAX_COLUMNS]
		if ordered:
			tables.append((database, table, ordered))
	result = []
	# Interleave tables so one wide table cannot consume the entire search budget.
	for width in range(1, MAX_WIDTH + 1):
		iterators = [iter(itertools.permutations(columns, width)) for _, _, columns in tables]
		for group in itertools.zip_longest(*iterators):
			for (database, table, _), columns in zip(tables, group):
				if columns is None:
					continue
				if len(result) >= MAX_CANDIDATES:
					notes.append(f"Candidate search limited to {MAX_CANDIDATES} indexes of up to {MAX_WIDTH} columns.")
					return result
				digest = hashlib.sha256(repr((database, table, columns)).encode("utf-8")).hexdigest()[:16]
				name = "palesql_idx_" + digest
				while connection.execute(f"SELECT 1 FROM {quote_name(database)}.sqlite_schema WHERE name=? COLLATE NOCASE", (name,)).fetchone():
					name += "_"
				qualified = f"{quote_name(database)}.{quote_name(name)}"
				definition = f"CREATE INDEX {qualified} ON {quote_name(table)} ({', '.join(map(quote_name, columns))});"
				result.append((database, table, name, qualified, definition, len(columns)))
	return result


def analyze_expert(connection, sql_text, *, sample=0, verbose=False):
	if not isinstance(sample, int) or not 0 <= sample <= 100:
		raise ValueError(USAGE)
	statements = sql_statements(sql_text)
	notes, selected, tried = [], [], 0
	deadline = time.monotonic() + MAX_SECONDS
	try:
		with snapshot(connection) as copy:
			copy.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
			reads = {}
			plans = query_plans(copy, statements, reads)
			candidates = index_candidates(copy, reads, notes)
			limits = {}
			if sample:
				for database, table, *_ in candidates:
					if (database, table) in limits:
						continue
					qualified = f"{quote_name(database)}.{quote_name(table)}"
					count = copy.execute(f"SELECT count(*) FROM {qualified}").fetchone()[0]
					limit = 0 if sample == 100 else max(1, math.ceil(count * sample / 100))
					limits[database, table] = limit
					copy.execute(f"PRAGMA analysis_limit={limit}")
					copy.execute(f"ANALYZE {qualified}")
				plans = query_plans(copy, statements)
				if sample < 100:
					notes.append(f"--sample {sample}: approximate ANALYZE using analysis_limit, not random sampling.")
			baseline = scores(plans)
			baseline_plans = plans
			installed = []
			for candidate in candidates:
				if time.monotonic() >= deadline:
					raise ValueError(f"analysis exceeded its {MAX_SECONDS}-second budget")
				database, table, name, qualified, definition, width = candidate
				copy.execute(definition)
				installed.append(candidate)
				if sample:
					copy.execute(f"PRAGMA analysis_limit={limits[database, table]}")
					copy.execute(f"ANALYZE {qualified}")
			plans = query_plans(copy, statements)
			current = scores(plans)
			tried += 1
			if improves(current, baseline):
				# Removing widest candidates first also gives narrower alternatives
				# a chance. Jointly useful JOIN indexes survive this process.
				for candidate in sorted(installed, key=lambda item: -item[5]):
					if time.monotonic() >= deadline:
						raise ValueError(f"analysis exceeded its {MAX_SECONDS}-second budget")
					copy.execute(f"DROP INDEX {candidate[3]}")
					trial_plans = query_plans(copy, statements)
					trial = scores(trial_plans)
					tried += 1
					if all(new <= old for new, old in zip(trial, current)):
						plans, current = trial_plans, trial
					else:
						copy.execute(candidate[4])
						if sample:
							copy.execute(f"PRAGMA analysis_limit={limits[candidate[0], candidate[1]]}")
							copy.execute(f"ANALYZE {candidate[3]}")
						selected.append(candidate)
				# Recheck the exact set that will be reported, after all pruning.
				plans = query_plans(copy, statements)
				while selected:
					used = "\n".join(line for plan in plans for line in plan)
					unused = [item for item in selected if not re.search(r"\b" + re.escape(item[2]) + r"\b", used)]
					if not unused:
						break
					for item in unused:
						copy.execute(f"DROP INDEX {item[3]}")
						selected.remove(item)
					plans = query_plans(copy, statements)
				if not improves(scores(plans), baseline):
					selected, plans = [], baseline_plans
			else:
				selected, plans = [], baseline_plans
	except sqlite3.Error as error:
		if time.monotonic() >= deadline:
			raise ValueError(f"analysis exceeded its {MAX_SECONDS}-second budget") from error
		raise ValueError(f"cannot analyze SQL: {error}") from error
	output = ["Python index advisor (bounded search; not native sqlite3_expert)."]
	if selected:
		output.extend(item[4] for item in selected)
	else:
		output.append("(no new indexes)")
	for number, (statement, plan) in enumerate(zip(statements, plans), 1):
		output.append(f"Query {number} plan:")
		if verbose:
			output.append(statement.strip())
		output.extend(plan or ["(no table access)"])
	if verbose:
		output.append(f"Candidates: {len(candidates)}; plans checked: {tried}; statistics sample: {sample}.")
	output.extend(dict.fromkeys(notes))
	return "\n".join(output) + "\n"
