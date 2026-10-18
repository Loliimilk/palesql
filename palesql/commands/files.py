import csv
import fnmatch
import hashlib
import io
import os
import re
import shlex
import sqlite3
import sys
from pathlib import Path
from contextlib import redirect_stdout

from ..database import (
    backup_database,
    database_objects,
    find_table,
    get_databases,
    identifier_key,
    quote_name,
    restore_database,
    shadow_tables,
    table_pragma,
)
from ..output import sql_literal
from ..sql import sql_context


FILE_COMMANDS = frozenset({
    ".read", ".output", ".once", ".import", ".dump", ".recover", ".backup", ".restore",
    ".save", ".clone", ".archive", ".dbtotxt", ".sha3sum",
})


def parse_import_arguments(argument):
    lexer = shlex.shlex(argument, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    values = list(lexer)
    if len(values) != 2:
        raise ValueError("Usage: .import <file> <table>")
    return values


def import_file(cursor, filename, table_name, nullvalue="", column_separator=None):
    path = Path(filename).expanduser().resolve()
    if not path.is_file():
        raise OSError(f"file not found: {path}")
    database, name = find_table(cursor, table_name) or (None, None)
    if database is None or name is None:
        raise ValueError(f"table not found: {table_name}")
    if database == "temp":
        qualified = quote_name(name)
    else:
        qualified = f"{quote_name(database)}.{quote_name(name)}"
    columns = [row[1] for row in table_pragma(cursor, database, "table_xinfo", name) if row[6] == 0]
    if not columns:
        raise ValueError(f"table has no writable columns: {table_name}")
    delimiter = column_separator if column_separator is not None else "\t" if path.suffix.lower() in (".tsv", ".tab") else ","
    if len(delimiter) != 1:
        raise ValueError("import separator must be one character; use .separator for CSV/TSV output only")
    placeholders = ", ".join("?" for column in columns)
    column_sql = ", ".join(quote_name(column) for column in columns)
    statement = f"INSERT INTO {qualified} ({column_sql}) VALUES ({placeholders})"
    inserted = 0
    batch = []
    # Keep a single transaction for all-or-nothing imports, but do not retain an
    # entire input file in memory.  A bounded batch makes large CSV imports work
    # the same way as small ones.
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, delimiter=delimiter)
        cursor.execute("BEGIN")
        try:
            for row in reader:
                if len(row) != len(columns):
                    raise ValueError(f"expected {len(columns)} columns, got {len(row)}")
                batch.append([None if nullvalue and value == nullvalue else value for value in row])
                if len(batch) == 1000:
                    cursor.executemany(statement, batch)
                    inserted += len(batch)
                    batch.clear()
            if batch:
                cursor.executemany(statement, batch)
                inserted += len(batch)
            cursor.execute("COMMIT")
        except BaseException:
            cursor.execute("ROLLBACK")
            raise
    return inserted


def pale_dump(cursor, object_names=()):
    objects = [row for row in database_objects(cursor) if row[0] == "main" and not row[2].startswith("sqlite_")]
    shadows = shadow_tables(cursor)
    objects = [row for row in objects if (row[0], row[2]) not in shadows]
    if object_names:
        wanted = {identifier_key(name) for name in object_names}
        objects = [row for row in objects if identifier_key(row[2]) in wanted or identifier_key(row[3]) in wanted]
    if not objects:
        print("No objects found", file=sys.stderr if not sys.stdout.isatty() else sys.stdout)
        return

    tables = [row for row in objects if row[1] == "table" and row[4]]
    indexes = [row for row in objects if row[1] == "index" and row[4]]
    triggers = [row for row in objects if row[1] == "trigger" and row[4]]
    views = [row for row in objects if row[1] == "view" and row[4]]
    print("BEGIN TRANSACTION;")
    for _, _, _, _, sql in sorted(tables, key=lambda row: row[2]):
        print(sql.rstrip().rstrip(";") + ";")
    for database, _, name, _, _ in sorted(tables, key=lambda row: row[2]):
        columns = [row[1] for row in table_pragma(cursor, database, "table_xinfo", name) if row[6] == 0]
        if not columns:
            continue
        column_sql = ", ".join(quote_name(column) for column in columns)
        cursor.execute(f"SELECT {column_sql} FROM {quote_name(name)}")
        for values in cursor.fetchall():
            literals = ", ".join(sql_literal(value) for value in values)
            print(f"INSERT INTO {quote_name(name)} ({column_sql}) VALUES ({literals});")
    for _, _, _, _, sql in sorted(indexes, key=lambda row: row[2]):
        print(sql.rstrip().rstrip(";") + ";")
    for _, _, _, _, sql in sorted(triggers, key=lambda row: row[2]):
        print(sql.rstrip().rstrip(";") + ";")
    for _, _, _, _, sql in sorted(views, key=lambda row: row[2]):
        print(sql.rstrip().rstrip(";") + ";")
    print("COMMIT;")


def recovery_corruption(error):
    code = getattr(error, "sqlite_errorcode", None)
    return (code is not None and code & 255 in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)) or any(
        phrase in str(error).lower() for phrase in ("malformed", "corrupt", "not a database")
    )


def pale_recover(connection):
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT type, name, tbl_name, sql FROM main.sqlite_schema WHERE sql IS NOT NULL ORDER BY name")
        objects = cursor.fetchall()
        shadows = shadow_tables(cursor)
    except sqlite3.Error as error:
        raise ValueError(f"cannot read database schema: {error}") from error
    finally:
        cursor.close()
    has_sequence = any(row[1] == "sqlite_sequence" for row in objects)
    objects = [row for row in objects if not row[1].startswith("sqlite_") and ("main", row[1]) not in shadows]
    tables = sorted((row for row in objects if row[0] == "table"), key=lambda row: row[1])
    later = sorted(
        (row for row in objects if row[0] in ("index", "trigger", "view")),
        key=lambda row: ({"index": 0, "trigger": 1, "view": 2}[row[0]], row[1]),
    )
    incomplete = False
    print("-- PaleSQL best-effort recovery: only data readable by SQLite can be extracted.")
    print("PRAGMA foreign_keys=OFF;")
    print("BEGIN TRANSACTION;")
    for _, _, _, sql in tables:
        print(sql.rstrip().rstrip(";") + ";")
    for _, name, _, sql in tables:
        qualified = quote_name(name)
        cursor = connection.cursor()
        try:
            column_info = table_pragma(cursor, "main", "table_xinfo", name)
            columns = [row[1] for row in column_info if row[6] == 0]
            if not columns:
                continue
            column_sql = ", ".join(quote_name(column) for column in columns)
            insert = f"INSERT INTO {qualified} ({column_sql}) VALUES"
            declared = {identifier_key(row[1]) for row in column_info}
            aliases = [alias for alias in ("rowid", "_rowid_", "oid") if identifier_key(alias) not in declared]
            rowid = None if re.search(r"\bWITHOUT\s+ROWID\b", sql, re.IGNORECASE) else (aliases[0] if aliases else None)
            if rowid:
                insert = f"INSERT INTO {qualified} ({rowid}, {column_sql}) VALUES"
                try:
                    cursor.execute(f"SELECT {rowid} FROM {qualified} ORDER BY {rowid} LIMIT 1")
                    first = cursor.fetchone()
                    cursor.execute(f"SELECT {rowid} FROM {qualified} ORDER BY {rowid} DESC LIMIT 1")
                    last = cursor.fetchone()
                except sqlite3.Error as error:
                    if not recovery_corruption(error):
                        raise
                    first, last = (-2**63,), (2**63 - 1,)
                if first is None:
                    continue
                pending = [(first[0], last[0])]
                failures = 0
                while pending:
                    low, high = pending.pop()
                    last_emitted = low - 1
                    scan = connection.cursor()
                    try:
                        scan.execute(
                            f"SELECT {rowid}, {column_sql} FROM {qualified} WHERE {rowid} BETWEEN ? AND ? ORDER BY {rowid}",
                            (low, high),
                        )
                        while batch := scan.fetchmany(512):
                            for values in batch:
                                last_emitted = values[0]
                                print(f"{insert} ({', '.join(sql_literal(value) for value in values)});")
                    except sqlite3.Error as error:
                        if not recovery_corruption(error):
                            raise
                        incomplete = True
                        failures += 1
                        remaining = max(low, last_emitted + 1)
                        if remaining < high and failures < 1024:
                            middle = remaining + (high - remaining) // 2
                            pending.extend(((middle + 1, high), (remaining, middle)))
                    finally:
                        scan.close()
                if failures >= 1024:
                    print(f"Recover warning: {name}: stopped after 1024 unreadable ranges", file=sys.stderr)
                    break
                if failures:
                    print(f"Recover warning: {name}: some rowid ranges were unreadable", file=sys.stderr)
            else:
                try:
                    cursor.execute(f"SELECT {column_sql} FROM {qualified}")
                    while batch := cursor.fetchmany(512):
                        for values in batch:
                            print(f"{insert} ({', '.join(sql_literal(value) for value in values)});")
                except sqlite3.Error as error:
                    if not recovery_corruption(error):
                        raise
                    incomplete = True
                    print(f"Recover warning: {name}: stopped at unreadable data ({error})", file=sys.stderr)
        except sqlite3.Error as error:
            incomplete = True
            print(f"Recover warning: {name}: could not read table ({error})", file=sys.stderr)
        finally:
            cursor.close()
    if has_sequence:
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT name, seq FROM main.sqlite_sequence")
            sequences = cursor.fetchall()
            print("DELETE FROM sqlite_sequence;")
            for name, sequence in sequences:
                print(f"INSERT INTO sqlite_sequence(name, seq) VALUES ({sql_literal(name)}, {sql_literal(sequence)});")
        except sqlite3.Error as error:
            incomplete = True
            print(f"Recover warning: sqlite_sequence: could not read sequence values ({error})", file=sys.stderr)
        finally:
            cursor.close()
    for _, _, _, sql in later:
        print(sql.rstrip().rstrip(";") + ";")
    print("COMMIT;")
    return not incomplete


def parse_archive_arguments(argument):
    lexer = shlex.shlex(argument, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    values = list(lexer)
    action = None
    archive = "archive.sqlar"
    root = Path.cwd()
    pattern = None
    verbose = False
    dryrun = False
    files = []
    index = 0
    actions = {
        "-c": "create", "--create": "create", "-u": "update", "--update": "update",
        "-i": "insert", "--insert": "insert", "-r": "remove", "--remove": "remove",
        "-t": "list", "--list": "list", "-x": "extract", "--extract": "extract",
    }
    while index < len(values):
        value = values[index]
        if value in actions:
            if action is not None:
                raise ValueError("choose one archive action: -c, -u, -i, -r, -t, or -x")
            action = actions[value]
        elif value in ("-f", "--file"):
            index += 1
            if index >= len(values) or not values[index]:
                raise ValueError("Usage: .archive [options] [files]")
            archive = values[index]
        elif value in ("-C", "--directory"):
            index += 1
            if index >= len(values) or not values[index]:
                raise ValueError("Usage: .archive [options] [files]")
            root = Path(values[index]).expanduser().resolve()
        elif value == "--glob":
            index += 1
            if index >= len(values) or not values[index]:
                raise ValueError("Usage: .archive [options] [files]")
            pattern = values[index]
        elif value in ("-v", "--verbose"):
            verbose = True
        elif value in ("-n", "--dryrun"):
            dryrun = True
        elif value.startswith("-"):
            raise ValueError(f"unknown archive option: {value}")
        else:
            files.append(value)
        index += 1
    return action or "list", Path(archive).expanduser().resolve(), root, pattern, verbose, dryrun, files


def pale_archive(argument):
    action, archive, root, pattern, verbose, dryrun, files = parse_archive_arguments(argument)
    if action in ("create", "update", "insert") and not files:
        raise ValueError("archive action requires at least one file")
    if action in ("list", "remove", "extract") and files and pattern:
        raise ValueError("use either archive names or --glob, not both")
    if action == "extract" and not root.is_dir():
        if dryrun:
            print(f"Would create directory: {root}")
        else:
            root.mkdir(parents=True, exist_ok=True)
    if not archive.exists() and action in ("list", "remove", "extract", "update", "insert"):
        raise ValueError(f"archive not found: {archive}")
    if dryrun and action in ("create", "update", "insert") and not archive.exists():
        print(f"Would create archive: {archive}")
        return
    database = sqlite3.connect(str(archive))
    try:
        database.execute("CREATE TABLE IF NOT EXISTS sqlar(name TEXT PRIMARY KEY, mode INT, mtime INT, sz INT, data BLOB)")
        if action == "create":
            if dryrun:
                print(f"Would clear archive: {archive}")
            else:
                database.execute("DELETE FROM sqlar")
        if action in ("create", "update", "insert"):
            for source_name in files:
                path = (root / source_name).resolve() if not Path(source_name).is_absolute() else Path(source_name).resolve()
                if path == archive or (path.exists() and archive.exists() and path == archive.resolve()):
                    raise ValueError("archive cannot contain itself")
                if not path.exists():
                    raise OSError(f"archive source not found: {path}")
                paths = [path] if path.is_file() else [item for item in path.rglob("*") if item.is_file()]
                for item in paths:
                    name = item.relative_to(root).as_posix() if item.is_relative_to(root) else item.name
                    stat = item.stat()
                    if dryrun:
                        print(f"Would add: {name}")
                        continue
                    data = item.read_bytes()
                    database.execute(
                        "INSERT OR REPLACE INTO sqlar(name, mode, mtime, sz, data) VALUES (?, ?, ?, ?, ?)",
                        (name, stat.st_mode, int(stat.st_mtime), len(data), data),
                    )
                    if verbose:
                        print(f"Added: {name} ({len(data)} bytes)")
        elif action == "list":
            for name, _, _, size in database.execute("SELECT name, mode, mtime, sz FROM sqlar ORDER BY name"):
                if not pattern or fnmatch.fnmatchcase(name, pattern):
                    print(f"{name}\t{size}")
        elif action == "remove":
            names = files or [row[0] for row in database.execute("SELECT name FROM sqlar") if pattern and fnmatch.fnmatchcase(row[0], pattern)]
            for name in names:
                if dryrun:
                    print(f"Would remove: {name}")
                else:
                    database.execute("DELETE FROM sqlar WHERE name = ?", (name,))
        elif action == "extract":
            for name, mode, _, _, data in database.execute("SELECT name, mode, mtime, sz, data FROM sqlar ORDER BY name"):
                if (files and name not in files) or (pattern and not fnmatch.fnmatchcase(name, pattern)):
                    continue
                destination = (root / Path(name)).resolve()
                if destination != root and root not in destination.parents:
                    raise ValueError(f"unsafe archive path: {name}")
                if dryrun:
                    print(f"Would extract: {name}")
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                try:
                    os.chmod(destination, mode & 0o777)
                except OSError:
                    pass
                if verbose:
                    print(f"Extracted: {name}")
        if not dryrun:
            database.commit()
    finally:
        database.close()


def parse_backup_arguments(argument, command=".backup"):
    lexer = shlex.shlex(argument, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    values = list(lexer)
    if len(values) not in (1, 2) or not values[-1]:
        raise ValueError(f"Usage: {command} [database] <file>")
    return ("main", values[0]) if len(values) == 1 else tuple(values)


def parse_single_file_argument(argument, command):
    lexer = shlex.shlex(argument, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    values = list(lexer)
    if len(values) != 1 or not values[0]:
        raise ValueError(f"Usage: {command} <file>")
    return values[0]


def pale_dbtotxt(cursor):
    databases = cursor.execute("PRAGMA database_list").fetchall()
    database = next((row for row in databases if row[1] == "main"), None)
    if database is None or not database[2]:
        raise ValueError("main database has no physical file")
    path = Path(database[2])
    if not path.is_file():
        raise OSError(f"database file not found: {path}")
    with path.open("rb") as source:
        offset = 0
        while chunk := source.read(16):
            hex_bytes = f"{' '.join(f'{byte:02x}' for byte in chunk):<47}"
            ascii_bytes = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
            print(f"{offset:08x}  {hex_bytes}  |{ascii_bytes}|")
            offset += len(chunk)


def pale_sha3sum(connection, pattern=""):
    pattern = pattern or "%"
    hasher = hashlib.sha3_256()
    objects = connection.execute(
        "SELECT type, name, tbl_name, sql FROM main.sqlite_schema "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "AND (name LIKE ? OR tbl_name LIKE ?) ORDER BY type, name",
        (pattern, pattern),
    ).fetchall()
    if not objects:
        print(f"sha3sum: {hasher.hexdigest()}")
        return

    def add(value):
        if value is None:
            encoded = b"N"
        elif isinstance(value, bytes):
            encoded = b"B" + value
        elif isinstance(value, str):
            encoded = b"T" + value.encode("utf-8")
        elif isinstance(value, float):
            encoded = b"F" + repr(value).encode("ascii")
        else:
            encoded = b"I" + str(value).encode("ascii")
        hasher.update(len(encoded).to_bytes(8, "big"))
        hasher.update(encoded)

    for kind, name, table, sql in objects:
        add(kind)
        add(name)
        add(table)
        add(sql)
        if kind != "table":
            continue
        try:
            info = connection.execute(f"PRAGMA main.table_xinfo({quote_name(name)})").fetchall()
            columns = [row[1] for row in info if row[6] == 0]
            if not columns:
                continue
            column_sql = ", ".join(quote_name(column) for column in columns)
            without_rowid = bool(re.search(r"\bWITHOUT\s+ROWID\b", sql or "", re.IGNORECASE))
            order = " ORDER BY rowid" if not without_rowid else ""
            for row in connection.execute(f"SELECT {column_sql} FROM {quote_name(name)}{order}"):
                for value in row:
                    add(value)
        except sqlite3.Error as error:
            raise ValueError(f"cannot hash table '{name}': {error}") from error
    print(f"sha3sum: {hasher.hexdigest()}")


def _path_argument(argument, usage):
    lexer = shlex.shlex(argument, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    values = list(lexer)
    if len(values) != 1 or not values[0]:
        raise ValueError(usage)
    return values[0]


def _arguments(argument):
    lexer = shlex.shlex(argument, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return list(lexer)


def handle_file_command(command, argument, state, protected_backup_paths, finish_once):
    if command not in FILE_COMMANDS:
        return False
    error_target = sys.stderr if state.output_file else sys.stdout
    if command in (".output", ".once"):
        try:
            if command == ".once" and not argument:
                raise ValueError("Usage: .once <file>")
            if argument:
                filename = _path_argument(argument, "Usage: .output [file]")
                path = Path(filename).expanduser().resolve()
                protected = [active[0] for active in state.scripts]
                if state.connection is not None:
                    for _, _, file in get_databases(state.cursor):
                        if file:
                            protected.extend(Path(file + suffix).resolve() for suffix in ("", "-wal", "-shm", "-journal"))
                if any(path == item or (path.exists() and item.exists() and path.samefile(item)) for item in protected):
                    raise ValueError("Output file must not overwrite an open database or active script")
                if command == ".output" and state.output_file and Path(state.output_file.name).resolve() == path:
                    state.output_file.flush()
                    if state.once_file:
                        state.once_file.close()
                        state.once_file = None
                    return True
                if any(
                    handle and (Path(handle.name).resolve() == path or (path.exists() and path.samefile(handle.name)))
                    for handle in (state.output_file, state.once_file)
                ):
                    raise ValueError("File is already used by .output or .once; choose another file")
                new_output = path.open("w", encoding="utf-8", newline="\r\n" if state.crlf else "\n")
            else:
                new_output = None
            if state.once_file:
                state.once_file.close()
                state.once_file = None
            if command == ".once":
                state.once_file = new_output
                return True
            if state.output_file:
                state.output_file.close()
            state.output_file = new_output
            sys.stdout = state.output_file if state.output_file is not None else state.console
        except (OSError, ValueError) as error:
            print(f"Output error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".import":
        try:
            if state.connection is None:
                raise ValueError("No database opened")
            filename, table_name = parse_import_arguments(argument)
            path = Path(filename).expanduser()
            if not path.is_absolute() and state.scripts:
                path = state.scripts[-1][0].parent / path
            import_file(state.cursor, path, table_name, state.nullvalue, state.column_separator)
        except (OSError, UnicodeError, ValueError, csv.Error, sqlite3.Error) as error:
            print(f"Import error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".read":
        try:
            filename = _path_argument(argument, "Usage: .read <file.sql>")
            if sql_context(state.sql_buffer)[1]:
                raise ValueError("Finish or clear the current SQL statement before .read")
            path = Path(filename).expanduser()
            if not path.is_absolute() and state.scripts:
                path = state.scripts[-1][0].parent / path
            path = path.resolve()
            if any(path == active[0] for active in state.scripts) or len(state.scripts) >= 32:
                raise ValueError("Recursive .read or nesting limit exceeded")
            text = path.read_text(encoding="utf-8-sig")
            state.scripts.append((path, iter(enumerate(io.StringIO(text), 1)), 0))
            state.clear_sql()
        except (OSError, UnicodeError, ValueError) as error:
            print(f"Read error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".dump":
        try:
            if state.connection is None:
                raise ValueError("No database opened")
            pale_dump(state.cursor, _arguments(argument))
        except (ValueError, OSError, sqlite3.Error) as error:
            print(f"Dump error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".recover":
        try:
            if argument:
                raise ValueError("Usage: .recover")
            if state.connection is None:
                raise ValueError("No database opened")
            with redirect_stdout(state.once_file if state.once_file is not None else sys.stdout):
                if not pale_recover(state.connection):
                    state.failed = True
        except (ValueError, OSError, sqlite3.Error) as error:
            print(f"Recover error: {error}", file=sys.stderr)
            state.failed = True
        finally:
            finish_once()
        return True
    if command == ".backup":
        try:
            if state.connection is None:
                raise ValueError("No database opened")
            database_name, filename = parse_backup_arguments(argument)
            backup_database(state.connection, state.cursor, database_name, filename, protected_backup_paths())
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f"Backup error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".restore":
        try:
            if state.connection is None:
                raise ValueError("No database opened")
            database_name, filename = parse_backup_arguments(argument, ".restore")
            restore_database(state.connection, state.cursor, database_name, filename)
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f"Restore error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command in (".save", ".clone"):
        try:
            if state.connection is None:
                raise ValueError("No database opened")
            filename = parse_single_file_argument(argument, command)
            backup_database(state.connection, state.cursor, "main", filename, protected_backup_paths(False))
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f"{'Save' if command == '.save' else 'Clone'} error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".archive":
        try:
            pale_archive(argument)
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f"Archive error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".dbtotxt":
        if argument:
            print("Usage: .dbtotxt", file=error_target)
            return True
        try:
            if state.connection is None:
                raise ValueError("No database opened")
            pale_dbtotxt(state.cursor)
        except (OSError, ValueError) as error:
            print(f"DB dump error: {error}", file=sys.stderr)
            state.failed = True
        return True
    try:
        if state.connection is None:
            raise ValueError("No database opened")
        if len(argument.split()) > 1:
            raise ValueError("Usage: .sha3sum [pattern]")
        pale_sha3sum(state.connection, argument)
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"SHA3 error: {error}", file=sys.stderr)
        state.failed = True
    return True
