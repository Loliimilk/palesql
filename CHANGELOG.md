# Changelog

All notable PaleSQL changes are listed here. Versions before 0.16.0 are summarized from the feature roadmap.

## 0.18.0

- Added hardening coverage for corrupt databases, large queries and imports, long SQL, many tables and connections, BLOBs, Unicode, invalid UTF-8, interrupted scripts, output failures, rollbacks, and SQLite limits.
- Streamed CSV imports in bounded batches while preserving all-or-nothing transactions.
- Added GitHub Actions coverage for Windows and Ubuntu on Python 3.11, 3.12, and 3.13.
- Restored Python 3.11 compatibility and made dbconfig tests account for the unavailable standard-library API on that version.

## 0.17.0rc1

- Added `PaleState`, an explicit command dispatcher, command aliases, and command metadata.
- Reduced `main()` to application startup and moved the lifecycle into `PaleCLI`.
- Expanded automated tests for CLI behavior, output, transactions, resources, and regressions.
- Added release documentation and package verification.

## 0.16.0

- Split the single-file CLI into the `palesql` package and command modules.
- Added `python -m palesql` and the installable `palesql` console entry point.

## 0.15.0

- Added command line arguments for database startup, SQL execution, formats, headers, batch execution, and read-only mode.

## 0.14.0

- Completed CLI behavior for multiline input, prompts, comments, paths, history, interrupts, EOF, large results, and URI databases.

## 0.13.0

- Added PaleSQL convenience commands for status, history, descriptions, read-only opening, reopening, and in-memory databases.

## 0.12.0

- Added selected compatibility commands from the SQLite shell, including authorizer, testcase, check, nonce, session, imposter, and browser output support.

## 0.11.0

- Added working-directory and system-shell commands with safe mode controls.

## 0.10.0

- Added independent connection management with `.connection` and `.connections`.

## 0.9.0

- Added integrity, recovery, SHA3, and archive-related commands.

## 0.8.0

- Added SQLite extension and low level integration commands where Python's SQLite API permits them.

## 0.7.0

- Added SQLite configuration helpers for foreign keys, journal mode, encoding, timeouts, and pragmas.

## 0.6.0

- Added diagnostics and performance tools including timer, stats, query plans, trace, progress, integrity checks, and the Python `.expert` advisor.

## 0.5.0

- Added transaction and session-state controls for changes, bail, echo, parameters, limits, and timeouts.

## 0.4.0

- Added file, script, import, export, dump, backup, restore, save, clone, and spreadsheet workflows.

## 0.3.0

- Added output headers, formats, separators, widths, null text, CRLF output, print, and prompts.

## 0.2.0

- Added database structure inspection commands for tables, schemas, columns, indexes, views, triggers, foreign keys, full schema, database information, and linting.

## 0.1.0

- Introduced the initial Python SQLite command line client.
