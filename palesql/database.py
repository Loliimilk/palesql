import os
import re
import sqlite3
from pathlib import Path


def pale_open(name):
    name = str(name)
    if name == ":memory:":
        connection = sqlite3.connect(name, isolation_level=None)
    elif name.lower().startswith("file:"):
        connection = sqlite3.connect(name, uri=True, isolation_level=None)
    else:
        if not name.lower().endswith(".db"):
            name += ".db"
        connection = sqlite3.connect(name, isolation_level=None)
    return connection, connection.cursor()


def pale_open_readonly(name):
    name = str(name)
    if name == ":memory:":
        raise ValueError("read-only mode requires a database file")
    if name.lower().startswith("file:"):
        if re.search(r"(?:[?&])mode=", name, re.IGNORECASE):
            uri = re.sub(r"([?&])mode=[^&#]*", r"\1mode=ro", name, flags=re.IGNORECASE)
        else:
            uri = name + ("&" if "?" in name else "?") + "mode=ro"
    else:
        path = Path(name).expanduser()
        if not path.name.lower().endswith(".db"):
            path = path.with_name(path.name + ".db")
        uri = "file:" + str(path.resolve()).replace("\\", "/") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    return connection, connection.cursor()


def quote_name(name):
    return '"' + name.replace('"', '""') + '"'


def identifier_key(name):
    return name.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def get_databases(cursor):
    cursor.execute("PRAGMA database_list;")
    return cursor.fetchall()


def print_databases(rows):
    print(f"{'seq':<5} | {'name':<8} | file")
    for seq, name, file in rows:
        print(f"{seq:<5} | {name:<8} | {file}")


def pale_database(cursor):
    main = next((row for row in get_databases(cursor) if row[1] == "main"), None)
    if main is None:
        print("Main database not found")
        return
    print_databases([main])


def pale_databases(cursor):
    print_databases(get_databases(cursor))


def database_objects(cursor, object_type=None):
    objects = []
    for _, database, _ in get_databases(cursor):
        cursor.execute(f"SELECT type, name, tbl_name, sql FROM {quote_name(database)}.sqlite_master ORDER BY name")
        for kind, name, table, sql in cursor.fetchall():
            if object_type is None or kind == object_type:
                objects.append((database, kind, name, table, sql))
    return objects


def object_label(database, name):
    return name if database == "main" else f"{database}.{name}"


def find_table(cursor, table_name):
    objects = database_objects(cursor)
    objects.sort(key=lambda row: 0 if row[0] == "temp" else 1)
    for database, kind, name, _, _ in objects:
        if kind in ("table", "view") and identifier_key(f"{database}.{name}") == identifier_key(table_name):
            return database, name
    for database, kind, name, _, _ in objects:
        if kind in ("table", "view") and identifier_key(name) == identifier_key(table_name):
            return database, name
    return None


def table_pragma(cursor, database, pragma, name):
    cursor.execute(f"PRAGMA {quote_name(database)}.{pragma}({quote_name(name)});")
    return cursor.fetchall()


def shadow_tables(cursor):
    cursor.execute("PRAGMA table_list;")
    return {(row[0], row[1]) for row in cursor.fetchall() if row[2] == "shadow"}


def database_file(cursor, database_name="main"):
    return next((row for row in get_databases(cursor) if identifier_key(row[1]) == identifier_key(database_name)), None)


def backup_database(connection, cursor, database_name, filename, protected=()):
    database = next((row[1] for row in get_databases(cursor) if identifier_key(row[1]) == identifier_key(database_name)), None)
    if database is None:
        raise ValueError(f"database not found: {database_name}")
    path = Path(filename).expanduser().resolve()
    if any(path == item or (path.exists() and item.exists() and path.samefile(item)) for item in protected):
        raise ValueError("Backup file must not overwrite an open database or active script")
    destination = sqlite3.connect(str(path))
    try:
        connection.backup(destination, name=database)
    finally:
        destination.close()


def restore_database(connection, cursor, database_name, filename):
    path = Path(filename).expanduser().resolve()
    if not path.is_file():
        raise OSError(f"file not found: {path}")
    database = database_file(cursor, database_name)
    if database is None:
        raise ValueError(f"database not found: {database_name}")
    if database[2] and path == Path(database[2]).resolve():
        raise ValueError("Restore file must differ from the destination database")
    source = sqlite3.connect(str(path))
    try:
        if database[1] == "main":
            source.backup(connection, name="main")
        else:
            connection.execute(f"DETACH DATABASE {quote_name(database[1])}")
            destination = sqlite3.connect(database[2])
            try:
                source.backup(destination, name="main")
            finally:
                destination.close()
            connection.execute(f"ATTACH DATABASE ? AS {quote_name(database[1])}", (database[2],))
    finally:
        source.close()


def main_database_paths(cursor, include_sidecars=True):
    suffixes = ("", "-wal", "-shm", "-journal") if include_sidecars else ("",)
    paths = []
    for _, _, filename in get_databases(cursor):
        if filename:
            paths.extend(Path(filename + suffix).resolve() for suffix in suffixes)
    return paths
