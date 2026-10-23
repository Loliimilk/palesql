# PaleSQL

PaleSQL is a lightweight SQLite command line client written in Python. It runs regular SQL and adds practical dot commands for inspecting databases, formatting results, working with files, diagnostics, and session settings.

## Requirements

Running PaleSQL from source requires Python 3.11 or newer with the standard `sqlite3` module. It has no third party runtime dependencies.

## Prebuilt binaries

Download the archive for your platform from the [PaleSQL v1.0.0 release](https://github.com/Loliimilk/palesql/releases/tag/v1.0.0), extract it, and run the executable. These links point to v1.0.0 directly. Python does not need to be installed.

Available builds:

- Windows x64: [palesql-windows-x64.zip](https://github.com/Loliimilk/palesql/releases/download/v1.0.0/palesql-windows-x64.zip)
- macOS Intel: [palesql-macos-x86_64.zip](https://github.com/Loliimilk/palesql/releases/download/v1.0.0/palesql-macos-x86_64.zip)
- macOS Apple Silicon: [palesql-macos-arm64.zip](https://github.com/Loliimilk/palesql/releases/download/v1.0.0/palesql-macos-arm64.zip)
- Linux x86_64: [palesql-linux-x86_64.zip](https://github.com/Loliimilk/palesql/releases/download/v1.0.0/palesql-linux-x86_64.zip)

For example, on Windows:

```powershell
.\palesql-windows-x64.exe database.db
```

Windows may show a SmartScreen warning because the executable is new and unsigned. This indicates that the file or publisher has not built a reputation; it does not by itself mean the file is malicious. You can compare the archive's SHA-256 hash with the digest displayed on the GitHub release page:

```powershell
Get-FileHash .\palesql-windows-x64.zip -Algorithm SHA256
```

## Installation

Install from a local checkout:

```text
pip install .
```

This creates the `palesql` command in the active Python environment. The legacy launcher remains available with `python main.py`.

## Quick start

Open a database interactively:

```text
palesql database.db
```

Run one query and exit:

```text
palesql database.db "SELECT id, username FROM users;"
```

Start without installation:

```text
python -m palesql database.db
python -m palesql database.db "SELECT * FROM users;"
```

Useful first commands inside PaleSQL are `.tables`, `.schema users`, `.columns users`, `.indexes users`, `.help`, and `.quit`.

## Output modes

Use `.mode` to select the result format:

```text
.headers on
.mode table
SELECT * FROM users;

.mode jsonpretty
SELECT id, username FROM users;

.mode csv
.output users.csv
SELECT id, username FROM users;
.output
```

Available modes are `list`, `column`, `table`, `box`, `csv`, `tabs`, `json`, `jsonpretty`, `line`, `markdown`, and `quote`.

## Batch use

PaleSQL can execute SQL or dot commands without opening an interactive prompt:

```text
palesql --json --header database.db "SELECT * FROM users;"
palesql --cmd ".headers on" --cmd ".mode csv" database.db "SELECT * FROM users;"
palesql --init setup.sql database.db "SELECT count(*) FROM users;"
palesql --readonly database.db "SELECT * FROM users;"
```

Use `--bail` to stop after the first script or SQL error. The process exits with a nonzero status when batch or startup execution fails.

## Tests

Run the public automated test suite:

```text
python -m tests
```

The suite creates temporary databases and removes them when finished. A separate stress test can exercise larger data sets:

```text
python tests/stress.py
python tests/stress.py --rows 50000 --iterations 3
```

Pass `--database stress.db` to retain the stress-test database for manual inspection.

## Limitations

PaleSQL uses Python's built-in SQLite library, so features exposed by SQLite extensions or low level C APIs depend on the Python and SQLite build in use. Commands such as `.scanstats`, `.dbconfig`, `.filectrl`, `.unmodule`, and `.vfsinfo` may report that the required API is unavailable.

`.expert` is a bounded Python index advisor. It is not the native `sqlite3_expert` extension. Loading extensions and running shell commands can execute external code, so use `.safe on` when scripts should block those operations.

## Help and license

Run `palesql --help` for command line options, or `.help` inside PaleSQL for the complete dot-command reference.

PaleSQL is released under the [MIT License](LICENSE).
