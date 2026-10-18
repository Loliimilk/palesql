# PaleSQL

A tiny, lightweight SQLite CLI client written in Python.

## Implemented

PaleSQL supports:

* regular SQL through `sqlite3`;
* multiline queries and multiple SQL statements;
* explicit `BEGIN`, `COMMIT`, and `ROLLBACK` transactions;
* opening databases, read only mode, URI databases, and multiple connections;
* `list`, `column`, `table`, `box`, `csv`, `tabs`, `json`, `jsonpretty`, `line`, `markdown`, and `quote` output modes;
* import, export, dump, backup, restore, save, clone, and SQLite Archive;
* `.read`, `.output`, `.once`, command history, and the continuation prompt;
* SQLite diagnostics, integrity checks, recovery, `.expert`, trace, log, timer, and statistics;
* safe mode, extension support, and compatibility wrappers for Python API limitations;
* CLI arguments `--help`, `--version`, `--readonly`, `--cmd`, `--init`, `--batch`, `--interactive`, `--bail`, `--echo`, `--csv`, `--json`, `--table`, `--box`, `--header`, `--no-header`, `--separator`, and `--nullvalue`.

## Requirements

Python 3.11 or newer with the `sqlite3` module is required.

PaleSQL and its tests use only the Python standard library. No packages need to be installed through `pip`. The dependency policy is recorded in `requirements.txt`.

## Running PaleSQL

```text
python main.py
python main.py database.db
python main.py database.db "SELECT * FROM users;"
python main.py --readonly database.db
python main.py --json database.db "SELECT * FROM users;"
```

## Tests

Run the standard test suite with Python:

```text
python -m unittest
```

Run the detailed test report with the name of every check:

```text
python -m tests
```

The test database is created in a temporary directory. Persistent databases and test data are not written to the repository.

## Stress test

The stress test runs separately and is not included in the regular 129 tests:

```text
python tests/stress.py
python tests/stress.py --rows 50000 --iterations 3
```

The script creates a temporary database, inserts the requested number of rows, checks every output mode, JSON, BLOB, `NULL`, line breaks, indexes, and the integrity check. The temporary database is removed after the run.

To keep the database for manual inspection:

```text
python tests/stress.py --rows 50000 --database stress.db
```

## Structure

* `main.py` contains the current PaleSQL implementation.
* `tests/` contains unit tests, integration tests, fixtures, and the separate stress test.
* `requirements.txt` documents that there are no external dependencies.
