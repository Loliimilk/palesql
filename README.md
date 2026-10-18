# palesql
A tiny, lightweight SQLite CLI client written in Python

Run with `python main.py`.

## Index recommendations

```text
.open app.db
.expert
SELECT * FROM users WHERE city = 'Vladivostok';

.expert --verbose --sample 100
SELECT * FROM users WHERE city = 'Vladivostok' ORDER BY name;
```

`.expert` analyzes the next complete SQL input instead of executing it. Multiline
SQL and several statements in one input are supported. The shortcut
`.expert SELECT ...` also works. Reports respect `.output` and `.once`; errors in
`.read` include the script location and respect `.bail`.

The advisor uses only Python's standard library. It is an independent analogue
of SQLite's experimental `sqlite3_expert`, not a binding or an exact port. SQLite
compiles the SQL, identifies referenced tables and columns, and checks candidate
indexes with `EXPLAIN QUERY PLAN` on a private copy. Recommendations include
`CREATE INDEX` statements and the resulting query plans. The original database,
including any open transaction, is left untouched; indexes are never applied
automatically.

`--verbose` shows analysis details. `--sample 0` (the default) keeps existing
statistics without running `ANALYZE`; `--sample 100` gathers full statistics on
the copy. Intermediate values use SQLite's approximate `analysis_limit`, not the
official expert's random sampling algorithm.

The search is bounded and uses plan structure, not measured query runtimes or
SQLite's internal cost estimates. It cannot guarantee the fastest query or find
every useful index. It searches ordinary column indexes, not new expression or
partial indexes. Existing indexes are considered by SQLite. A `(no new indexes)`
report means no improvement was found within this search. Parameters are analyzed
as `NULL`, rather than using `.parameter` values. Connection-local custom
functions, collations and extensions are not copied.

At most 8 referenced columns per table, 3 columns per candidate and 128 candidates
are searched, with a 10-second analysis budget. Truncated searches are reported.
New index candidates use each column's default collation and ascending order;
mixed sort directions and alternative collations are not searched. The advisor
copies `main` and attached databases, including uncommitted changes. A non-empty
`temp` schema is rejected explicitly to prevent analyzing a shadowed table in the
wrong database. Copying databases and building candidate indexes requires
temporary disk space and memory; large databases may exceed the analysis budget.

The advisor requires Python 3.11+ with `sqlite3.Connection.serialize()` support.

`.scanstats est` reports estimated scan/search/temp-sort counts from
`EXPLAIN QUERY PLAN`. `.scanstats on` uses the same fallback and labels it as
such because CPython's standard `sqlite3` module does not expose SQLite's
runtime `sqlite3_stmt_scanstatus()` API. `.scanstats off` disables the report.

`.progress N` installs SQLite's standard progress callback every `N` virtual
machine opcodes and prints `Progress <count>` from the callback. `.progress 0`
removes it. The callback never interrupts SQL; it is intended for observing
long statements. This uses `Connection.set_progress_handler()` from Python's
standard library.

`.trace on` shows each SQL statement as SQLite starts it, including implicit
statements such as transaction setup and trigger work. `.trace off` disables the
callback; `.trace stdout` and `.trace stderr` select an explicit stream, while a
quoted path writes the trace to a UTF-8 file. Python's callback receives the SQL
text with bound values expanded by SQLite where the library provides it.

`.log stdout`, `.log stderr`, and `.log <file>` record SQLite errors that PaleSQL
receives; `.log off` disables it. Python's standard `sqlite3` module does not
expose SQLite's process-wide logging hook, so this is a client-level fallback.

## Tests

```text
python -m unittest -v test_main test_expert test_expert_cli test_intck test_scanstats test_progress test_trace test_log
```
