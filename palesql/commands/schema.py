import json
import os
import re
import shlex
import sqlite3
import sys

from ..database import (
    database_objects,
    find_table,
    get_databases,
    identifier_key,
    object_label,
    pale_database,
    pale_databases,
    quote_name,
    shadow_tables,
    table_pragma,
)


SCHEMA_COMMANDS = frozenset({
    ".tables", ".schema", ".fullschema", ".columns", ".indexes", ".views", ".triggers",
    ".foreignkeys", ".dbinfo", ".describe", ".find", ".count", ".tablesize", ".schemajson",
    ".integrity", ".intck", ".selftest", ".lint", ".database", ".databases", ".imposter",
})


def pale_dbinfo(cursor, database_name):
    database = next((row for row in get_databases(cursor) if identifier_key(row[1]) == identifier_key(database_name)), None)
    if database is None:
        print(f"Database '{database_name}' not found")
        return
    _, name, filename = database
    quoted_name = quote_name(name)
    info = {}
    for pragma in ("page_size", "page_count", "encoding", "schema_version", "freelist_count", "user_version", "journal_mode"):
        cursor.execute(f"PRAGMA {quoted_name}.{pragma};")
        info[pragma] = cursor.fetchone()[0]
    print(f"{'database':<20} | {name}")
    print(f"{'file':<20} | {filename or '(no file)'}")
    for pragma, value in info.items():
        print(f"{pragma.replace('_', ' '):<20} | {value}")
    print(f"{'logical size (bytes)':<20} | {info['page_size'] * info['page_count']}")
    print(f"{'free space (bytes)':<20} | {info['page_size'] * info['freelist_count']}")
    if filename:
        total = 0
        for label, suffix in (("file bytes", ""), ("WAL bytes", "-wal"), ("SHM bytes", "-shm"), ("journal bytes", "-journal")):
            try:
                size = os.path.getsize(filename + suffix)
            except FileNotFoundError:
                size = 0
            except OSError as error:
                print(f"{label:<20} | unavailable: {error}")
                total = None
                continue
            print(f"{label:<20} | {size}")
            if total is not None:
                total += size
        if total is not None:
            print(f"{'total file bytes':<20} | {total}")


def pale_tables(cursor, pattern):
    rows = []
    shadows = shadow_tables(cursor)
    for database, _, name, _, _ in database_objects(cursor, "table"):
        if name.startswith("sqlite_") or (database, name) in shadows:
            continue
        cursor.execute("SELECT ? LIKE ? OR ? LIKE ?", (name, pattern, f"{database}.{name}", pattern))
        if cursor.fetchone()[0]:
            rows.append((object_label(database, name),))
    return rows


def pale_views(cursor):
    return [(object_label(database, name),) for database, _, name, _, _ in database_objects(cursor, "view")]


def pale_triggers(cursor):
    return [(object_label(database, name),) for database, _, name, _, _ in database_objects(cursor, "trigger")]


def pale_indexes(cursor, table_name):
    rows = []
    for database, _, name, table, _ in database_objects(cursor, "index"):
        if not table_name or identifier_key(table) == identifier_key(table_name) or identifier_key(f"{database}.{table}") == identifier_key(table_name):
            rows.append((object_label(database, name),))
    return rows


def pale_imposter(cursor, argument):
    values = shlex.split(argument)
    if len(values) != 2:
        raise ValueError("Usage: .imposter <index> <table>")
    index_name, imposter_name = values
    index = next((row for row in database_objects(cursor, "index") if row[0] == "main" and identifier_key(row[2]) == identifier_key(index_name)), None)
    if index is None:
        raise ValueError(f"index not found: {index_name}")
    table_name = index[3]
    keys = [row for row in table_pragma(cursor, "main", "index_xinfo", index_name) if len(row) > 5 and row[5] and row[1] >= 0]
    if not keys:
        raise ValueError("index has no ordinary columns; expression indexes are not supported")
    table_info = {row[0]: (row[1], row[2]) for row in table_pragma(cursor, "main", "table_xinfo", table_name)}
    columns = []
    for row in sorted(keys, key=lambda item: item[0]):
        if row[1] not in table_info:
            raise ValueError(f"cannot map index column {row[1]}")
        name, declared_type = table_info[row[1]]
        columns.append((name, declared_type or ""))
    if len({identifier_key(name) for name, _ in columns}) != len(columns):
        raise ValueError("index columns must have unique names")
    definition = ", ".join(f"{quote_name(name)} {declared_type}".rstrip() for name, declared_type in columns)
    column_sql = ", ".join(quote_name(name) for name, _ in columns)
    cursor.execute(f"DROP TABLE IF EXISTS temp.{quote_name(imposter_name)}")
    cursor.execute(f"CREATE TEMP TABLE {quote_name(imposter_name)} ({definition})")
    cursor.execute(f"INSERT INTO temp.{quote_name(imposter_name)} ({column_sql}) SELECT {column_sql} FROM main.{quote_name(table_name)}")
    print(f"Imposter snapshot '{imposter_name}' created from index '{index_name}' ({cursor.rowcount} rows)")


def schema_rows(cursor, pattern="%"):
    rows = []
    shadows = shadow_tables(cursor)
    for database, kind, name, table, sql in database_objects(cursor):
        if sql is None or name.startswith("sqlite_") or (database, name) in shadows:
            continue
        cursor.execute("SELECT ? LIKE ? OR ? LIKE ? OR ? LIKE ? OR ? LIKE ?", (name, pattern, table, pattern, f"{database}.{name}", pattern, f"{database}.{table}", pattern))
        if cursor.fetchone()[0]:
            rows.append((database, kind, name, sql))
    order = {"table": 0, "index": 1, "view": 2, "trigger": 3}
    return sorted(rows, key=lambda row: (order[row[1]], row[0], row[2]))


def schema_sql(database, kind, name, sql):
    space = r"(?:\s|/\*[\s\S]*?\*/|--[^\n]*(?:\n|$))+"
    prefix = rf"\A\s*CREATE{space}(?:TEMP(?:ORARY)?{space})?(?:(?P<modifier>VIRTUAL|UNIQUE){space})?(?:TABLE|INDEX|VIEW|TRIGGER){space}(?:IF{space}NOT{space}EXISTS{space})?"
    identifier = r'''(?:"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\]|'(?:''|[^'])*'|(?:(?!/\*|--)[^\s(;])+)'''
    match = re.match(prefix + identifier, sql, re.IGNORECASE)
    if match is None:
        raise ValueError(f"Cannot read schema for {database}.{name}")
    modifier = match.group("modifier")
    start = "CREATE " + (modifier.upper() + " " if modifier else "") + kind.upper() + " "
    return start + quote_name(database) + "." + quote_name(name) + sql[match.end():].rstrip().rstrip(";") + ";"


def pale_schema(cursor, pattern=""):
    rows = schema_rows(cursor, pattern or "%")
    if not rows:
        print(f"-- No schema found for '{pattern}'" if pattern else "-- No schema found")
    for database, kind, name, sql in rows:
        print(schema_sql(database, kind, name, sql))


def pale_lint(cursor):
    objects = database_objects(cursor, "table")
    tables = {(identifier_key(database), identifier_key(name)) for database, _, name, _, _ in objects}
    issues = 0
    has_foreign_keys = False
    for database, _, table, _, _ in objects:
        foreign_keys = table_pragma(cursor, database, "foreign_key_list", table)
        if not foreign_keys:
            continue
        has_foreign_keys = True
        label = object_label(database, table)
        try:
            for child, rowid, parent, key_id in table_pragma(cursor, database, "foreign_key_check", table):
                print(f"Foreign key violation: {label}, rowid {rowid}, key {key_id}, parent {parent}")
                issues += 1
        except sqlite3.Error as error:
            print(f"Foreign key error in '{label}': {error}")
            issues += 1
            continue
        keys = {}
        for row in foreign_keys:
            keys.setdefault(row[0], []).append(row)
        for key_id, rows in keys.items():
            rows.sort(key=lambda row: row[1])
            parent = rows[0][2]
            if (identifier_key(database), identifier_key(parent)) not in tables:
                print(f"Missing parent table: '{label}', key {key_id}, parent '{parent}'")
                issues += 1
                continue
            columns = table_pragma(cursor, database, "table_xinfo", parent)
            primary_key = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
            parent_columns = [row[4] for row in rows]
            if all(name is None for name in parent_columns):
                parent_columns = primary_key
            conditions = []
            for row, parent_column in zip(rows, parent_columns):
                conditions.extend((f"p.{quote_name(parent_column)} = c.{quote_name(row[3])}", f"p.{quote_name(parent_column)} = ?"))
            query = f"SELECT 1 FROM {quote_name(database)}.{quote_name(parent)} AS p CROSS JOIN {quote_name(database)}.{quote_name(table)} AS c WHERE " + " AND ".join(conditions)
            try:
                cursor.execute("EXPLAIN QUERY PLAN " + query, (None,) * len(rows))
                plan = cursor.fetchall()
                if not any(row[3].startswith("SEARCH c ") and "AUTOMATIC" not in row[3] for row in plan):
                    names = ", ".join(row[3] for row in rows)
                    print(f"Consider an index: '{label}' ({names}), foreign key {key_id}; lookup scans the table (check index columns and collations)")
                    issues += 1
            except sqlite3.Error as error:
                print(f"Cannot check index for '{label}', key {key_id}: {error}")
                issues += 1
    cursor.execute("PRAGMA foreign_keys;")
    if has_foreign_keys and not cursor.fetchone()[0]:
        print("Foreign key enforcement is disabled")
        issues += 1
    if not issues:
        print("No foreign key or index issues found")


def pale_integrity_check(cursor):
    try:
        cursor.execute("PRAGMA integrity_check")
        results = cursor.fetchall()
    except sqlite3.Error as error:
        raise ValueError(f"integrity check failed: {error}") from error
    for result in results:
        print(result[0] if result else "")


def pale_incremental_integrity_check(cursor):
    try:
        cursor.execute("SELECT name FROM main.sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        issues = []
        for table, in cursor.fetchall():
            escaped_table = table.replace("'", "''")
            cursor.execute(f"PRAGMA quick_check('{escaped_table}')")
            issues.extend(result[0] for result in cursor.fetchall() if result and result[0] != "ok")
    except sqlite3.Error as error:
        raise ValueError(f"incremental integrity check failed: {error}") from error
    for issue in issues:
        print(issue)
    if not issues:
        print("ok")


def pale_selftest(connection):
    issues = []
    cursor = connection.cursor()
    try:
        try:
            cursor.execute("PRAGMA quick_check")
            quick = [row[0] for row in cursor.fetchall()]
            if quick != ["ok"]:
                issues.extend(f"quick_check: {result}" for result in quick)
        except sqlite3.Error as error:
            issues.append(f"quick_check: {error}")
        try:
            cursor.execute("PRAGMA foreign_key_check")
            issues.extend(f"foreign key violation: {row}" for row in cursor.fetchall())
        except sqlite3.Error as error:
            issues.append(f"foreign_key_check: {error}")
        try:
            cursor.execute("SELECT type, name, sql FROM main.sqlite_schema WHERE sql IS NOT NULL ORDER BY name")
            for kind, name, _ in cursor.fetchall():
                if kind == "table" and not name.startswith("sqlite_"):
                    cursor.execute(f"SELECT count(*) FROM main.{quote_name(name)}")
                    cursor.fetchone()
                    cursor.execute(f"PRAGMA main.table_xinfo({quote_name(name)})")
                    cursor.fetchall()
        except sqlite3.Error as error:
            issues.append(f"schema/data read: {error}")
    finally:
        cursor.close()
    if issues:
        print("Self-test: FAILED")
        for issue in issues:
            print(f"  {issue}")
        return False
    print("Self-test: OK")
    return True


def pale_fullschema(cursor):
    for _, database, _ in get_databases(cursor):
        if database not in ("main", "temp"):
            print(f"ATTACH DATABASE ':memory:' AS {quote_name(database)};")
    pale_schema(cursor)
    found = False
    for database, _, name, _, _ in database_objects(cursor, "table"):
        if not name.startswith("sqlite_stat"):
            continue
        found = True
        qualified = quote_name(database) + "." + quote_name(name)
        print(f"ANALYZE {quote_name(database)}.sqlite_schema;")
        print(f"DELETE FROM {qualified};")
        cursor.execute(f"SELECT * FROM {qualified}")
        for row in cursor.fetchall():
            values = []
            for value in row:
                if isinstance(value, str) and "\x00" in value:
                    values.append("CAST(X'" + value.encode("utf-8").hex() + "' AS TEXT)")
                else:
                    cursor.execute("SELECT quote(?)", (value,))
                    values.append(cursor.fetchone()[0])
            print(f"INSERT INTO {qualified} VALUES ({', '.join(values)});")
        print(f"ANALYZE {quote_name(database)}.sqlite_schema;")
    if not found:
        print("-- No statistics found")


def pale_columns(cursor, table_name):
    table = find_table(cursor, table_name)
    if table is None:
        print(f"Table '{table_name}' not found")
        return
    database, name = table
    print(f"{'name':<20} | {'type':<12} | {'NOT NULL':<8} | {'DEFAULT':<20} | {'PRIMARY KEY':<11} | kind")
    for _, column, column_type, not_null, default, primary_key, hidden in table_pragma(cursor, database, "table_xinfo", name):
        default = "(none)" if default is None else default
        kind = {0: "normal", 1: "hidden", 2: "generated virtual", 3: "generated stored"}[hidden]
        print(f"{column:<20} | {column_type:<12} | {not_null:<8} | {default:<20} | {primary_key:<11} | {kind}")


def pale_foreignkeys(cursor, table_name):
    table = find_table(cursor, table_name)
    if table is None:
        print(f"Table '{table_name}' not found")
        return
    database, name = table
    rows = table_pragma(cursor, database, "foreign_key_list", name)
    if not rows:
        print("No foreign keys found")
        return
    print(f"{'id':<5} | {'seq':<5} | {'table':<20} | {'from':<20} | {'to':<20} | {'ON UPDATE':<12} | {'ON DELETE':<12} | MATCH")
    for key_id, seq, target, source, destination, on_update, on_delete, match in rows:
        print(f"{key_id:<5} | {seq:<5} | {target:<20} | {source:<20} | {'NULL' if destination is None else destination:<20} | {on_update:<12} | {on_delete:<12} | {match}")


def pale_describe(cursor, table_name):
    table = find_table(cursor, table_name)
    if table is None:
        print(f"Table '{table_name}' not found")
        return
    database, name = table
    objects = database_objects(cursor)
    create_sql = next((row[4] for row in objects if row[0] == database and row[2] == name and row[1] == "table"), None)
    print(f"Table: {object_label(database, name)}")
    print("Columns:")
    for _, column, column_type, not_null, default, primary_key, hidden in table_pragma(cursor, database, "table_xinfo", name):
        flags = []
        if not_null:
            flags.append("NOT NULL")
        if primary_key:
            flags.append(f"PK {primary_key}")
        if default is not None:
            flags.append(f"DEFAULT {default}")
        if hidden:
            flags.append("hidden")
        details = " " + " ".join(flags) if flags else ""
        display_type = column_type or '""'
        print(f"  {column} {display_type}{details}".rstrip())
    print("Foreign keys:")
    foreign_keys = table_pragma(cursor, database, "foreign_key_list", name)
    if foreign_keys:
        for _, _, target, source, destination, on_update, on_delete, _ in foreign_keys:
            print(f"  {source} -> {target}.{destination or '(rowid)'} ON UPDATE {on_update} ON DELETE {on_delete}")
    else:
        print("  none")
    print("Indexes:")
    indexes = [row for row in objects if row[0] == database and row[1] == "index" and identifier_key(row[3]) == identifier_key(name)]
    if indexes:
        for _, _, index_name, _, sql in indexes:
            print(f"  {index_name}{' (implicit)' if sql is None else ''}")
    else:
        print("  none")
    print("CREATE SQL:")
    print(f"  {create_sql or '(unavailable)'}")


def pale_schemajson(cursor):
    print(json.dumps([{"database": database, "type": kind, "name": name, "table": table, "sql": sql} for database, kind, name, table, sql in database_objects(cursor)], ensure_ascii=False, indent=2))


def pale_find(cursor, needle):
    matches = []
    for database, _, name, _, _ in database_objects(cursor, "table"):
        if name.startswith("sqlite_") or (database, name) in shadow_tables(cursor):
            continue
        columns = [row[1] for row in table_pragma(cursor, database, "table_xinfo", name) if not row[6] and (not row[2] or any(item in row[2].upper() for item in ("CHAR", "TEXT", "CLOB")))]
        if not columns:
            continue
        try:
            cursor.execute(f"SELECT {', '.join(map(quote_name, columns))} FROM {quote_name(database)}.{quote_name(name)}")
            for row in cursor.fetchall():
                for column, value in zip(columns, row):
                    if value is not None and str(needle).casefold() in str(value).casefold():
                        matches.append((object_label(database, name), column, value))
        except sqlite3.Error:
            continue
    if matches:
        for table, column, value in matches:
            print(f"{table}.{column}: {value}")
    else:
        print("No matches")


def pale_count(cursor, table_name=""):
    if table_name:
        table = find_table(cursor, table_name)
        if table is None:
            print(f"Table '{table_name}' not found")
            return
        database, name = table
        print(f"{object_label(database, name)}: {cursor.execute(f'SELECT count(*) FROM {quote_name(database)}.{quote_name(name)}').fetchone()[0]}")
        return
    rows = []
    shadows = shadow_tables(cursor)
    for database, _, name, _, _ in database_objects(cursor, "table"):
        if not name.startswith("sqlite_") and (database, name) not in shadows:
            rows.append((object_label(database, name), cursor.execute(f"SELECT count(*) FROM {quote_name(database)}.{quote_name(name)}").fetchone()[0]))
    if rows:
        for name, count in rows:
            print(f"{name}: {count}")
    else:
        print("No tables found")


def pale_tablesize(cursor, table_name=""):
    if table_name:
        table = find_table(cursor, table_name)
        if table is None:
            print(f"Table '{table_name}' not found")
            return
        database, name = table
        print(f"{object_label(database, name)}: {cursor.execute(f'SELECT count(*) FROM {quote_name(database)}.{quote_name(name)}').fetchone()[0]} rows")
        return
    presented = False
    for database, _, name, _, _ in database_objects(cursor, "table"):
        if name.startswith("sqlite_"):
            continue
        print(f"{object_label(database, name)}: {cursor.execute(f'SELECT count(*) FROM {quote_name(database)}.{quote_name(name)}').fetchone()[0]} rows")
        presented = True
    for _, database, filename in get_databases(cursor):
        if filename:
            try:
                print(f"{database} file size: {os.path.getsize(filename)} bytes")
            except OSError:
                print(f"{database} file size: unavailable")
    if not presented:
        print("No tables found")


def handle_schema_command(command, argument, state):
    error_target = sys.stderr if state.output_file else sys.stdout
    if command not in SCHEMA_COMMANDS or state.connection is None:
        return False
    if command == ".describe":
        if not argument:
            print("Usage: .describe <table>", file=error_target)
        else:
            pale_describe(state.cursor, argument)
        return True
    if command == ".find":
        if not argument:
            print("Usage: .find <text>", file=error_target)
        else:
            needle = argument[1:-1] if len(argument) >= 2 and argument[0] in ("'", '"') and argument[-1] == argument[0] else argument
            pale_find(state.cursor, needle)
        return True
    if command == ".count":
        pale_count(state.cursor, argument)
        return True
    if command == ".tablesize":
        pale_tablesize(state.cursor, argument)
        return True
    if command == ".schemajson":
        if argument:
            print("Usage: .schemajson", file=error_target)
        else:
            pale_schemajson(state.cursor)
        return True
    if command == ".tables":
        tables = pale_tables(state.cursor, argument or "%")
        for table in tables or [("No tables found",)]:
            print(table[0])
        return True
    if command == ".views":
        views = pale_views(state.cursor)
        for view in views or [("No views found",)]:
            print(view[0])
        return True
    if command == ".triggers":
        triggers = pale_triggers(state.cursor)
        for trigger in triggers or [("No triggers found",)]:
            print(trigger[0])
        return True
    if command == ".indexes":
        indexes = pale_indexes(state.cursor, argument)
        for index in indexes or [("No indexes found",)]:
            print(index[0])
        return True
    if command == ".schema":
        pale_schema(state.cursor, argument)
        return True
    if command == ".fullschema":
        pale_fullschema(state.cursor)
        return True
    if command == ".lint":
        pale_lint(state.cursor)
        return True
    if command in (".integrity", ".intck"):
        if argument:
            print(f"Usage: {command}", file=error_target)
            return True
        try:
            (pale_incremental_integrity_check if command == ".intck" else pale_integrity_check)(state.cursor)
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f"Integrity check error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".selftest":
        if argument:
            print("Usage: .selftest", file=error_target)
            return True
        try:
            if not pale_selftest(state.connection):
                state.failed = True
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f"Self-test error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".imposter":
        try:
            pale_imposter(state.cursor, argument)
        except (ValueError, sqlite3.Error) as error:
            print(f"Imposter error: {error}", file=sys.stderr)
            state.failed = True
        return True
    if command == ".columns":
        if not argument:
            print("No table name provided", file=error_target)
        else:
            pale_columns(state.cursor, argument)
        return True
    if command == ".foreignkeys":
        if not argument:
            print("No table name provided", file=error_target)
        else:
            pale_foreignkeys(state.cursor, argument)
        return True
    if command == ".database":
        pale_database(state.cursor)
        return True
    if command == ".databases":
        pale_databases(state.cursor)
        return True
    if command == ".dbinfo":
        pale_dbinfo(state.cursor, argument or "main")
        return True
    return False
