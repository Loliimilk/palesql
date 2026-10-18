import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODES = ("list", "column", "table", "box", "csv", "tabs", "json", "jsonpretty", "line", "markdown", "quote")


def run_cli(database, sql, commands=()):
    options = [item for command in commands for item in ("--cmd", command)]
    return subprocess.run(
        [sys.executable, "-m", "palesql", "--batch", *options, str(database), sql],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def build_database(path, rows):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE stress_rows(
            id INTEGER PRIMARY KEY,
            payload TEXT NOT NULL,
            amount REAL NOT NULL,
            blob_value BLOB NOT NULL,
            nullable TEXT
        );
        CREATE INDEX stress_rows_amount ON stress_rows(amount);
        """
    )
    values = (
        (index, f"payload-{index:08d}-quote-'-newline\nvalue", index / 10, bytes([index % 256, 0, 255]), None if index % 7 == 0 else f"value-{index}")
        for index in range(1, rows + 1)
    )
    connection.executemany("INSERT INTO stress_rows VALUES (?, ?, ?, ?, ?)", values)
    connection.commit()
    connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a standalone PaleSQL stress test")
    parser.add_argument("--rows", type=int, default=10_000, help="rows to generate")
    parser.add_argument("--iterations", type=int, default=2, help="iterations for each output mode")
    parser.add_argument("--database", type=Path, help="keep the generated database at this path")
    args = parser.parse_args(argv)
    if args.rows < 1 or args.iterations < 1:
        parser.error("--rows and --iterations must be positive")

    temporary = None
    if args.database is None:
        temporary = tempfile.TemporaryDirectory(prefix="palesql-stress-")
        database = Path(temporary.name) / "stress.db"
    else:
        database = args.database.expanduser().resolve()
        database.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    try:
        print(f"PaleSQL stress test: {args.rows} rows, {args.iterations} iterations")
        build_database(database, args.rows)
        query = "SELECT id, payload, amount, blob_value, nullable FROM stress_rows ORDER BY id LIMIT 100;"
        for mode in MODES:
            for _ in range(args.iterations):
                result = run_cli(database, query, (f".mode {mode}",))
                if result.returncode != 0:
                    raise RuntimeError(f"mode {mode} failed: {result.stderr or result.stdout}")
                if mode in ("json", "jsonpretty"):
                    json.loads(result.stdout)
        integrity = run_cli(database, "SELECT count(*) FROM stress_rows;", (".integrity",))
        if integrity.returncode != 0 or "ok" not in integrity.stdout.lower():
            raise RuntimeError(f"integrity check failed: {integrity.stderr or integrity.stdout}")
        elapsed = time.perf_counter() - started
        print(f"PASS: {len(MODES)} modes, {args.iterations} iterations, {elapsed:.2f}s")
        print(f"Database: {database if args.database else 'temporary'}")
        return 0
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
