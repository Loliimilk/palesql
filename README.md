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

PaleSQL and its tests use only the Python standard library at runtime. No third party runtime packages are required. The dependency policy is recorded in `requirements.txt`.

## Running PaleSQL

```text
python -m palesql
python -m palesql database.db
python -m palesql database.db "SELECT * FROM users;"
python -m palesql --readonly database.db
python -m palesql --json database.db "SELECT * FROM users;"
```

`python main.py` remains available for existing scripts. To install a `palesql` command in the active Python environment:

```text
pip install .
palesql database.db
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

The stress test runs separately and is not included in the regular automated suite:

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

* `palesql/` is the application package. `cli.py` owns the command loop, `state.py` owns session state, `database.py`, `sql.py`, and `output.py` own their respective layers.
* `palesql/commands/` groups core, schema, files, settings, diagnostics, connections, and system commands.
* `palesql/__main__.py` enables `python -m palesql`.
* `main.py` is a small compatibility launcher for existing scripts.
* `tests/` contains unit tests, integration tests, fixtures, and the separate stress test.
* `pyproject.toml` defines the installable package and `palesql` console command.
* `requirements.txt` documents that there are no external runtime dependencies.
