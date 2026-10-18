import io
import json
import math
import re
import shutil
import shlex
import sys
import unicodedata


OUTPUT_MODES = (
    "list", "column", "table", "box", "csv", "tabs", "json",
    "jsonpretty", "line", "markdown", "quote",
)


def result_text(value, nullvalue=""):
    if value is None:
        return nullvalue
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def sql_literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    if isinstance(value, str):
        if "\x00" in value:
            return "CAST(X'" + value.encode("utf-8").hex() + "' AS TEXT)"
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, float) and not math.isfinite(value):
        return "NULL" if math.isnan(value) else ("-9e999" if value < 0 else "9e999")
    return str(value)


def text_clusters(text):
    clusters = []
    for char in text:
        code = ord(char)
        regional = 0x1F1E6 <= code <= 0x1F1FF
        extend = (
            unicodedata.category(char).startswith("M")
            or char == "\u200d"
            or 0x1F3FB <= code <= 0x1F3FF
            or 0xE0020 <= code <= 0xE007F
        )
        if clusters and (
            extend
            or clusters[-1].endswith("\u200d")
            or (regional and len(clusters[-1]) == 1 and 0x1F1E6 <= ord(clusters[-1]) <= 0x1F1FF)
        ):
            clusters[-1] += char
        else:
            clusters.append(char)
    return clusters


def cluster_width(cluster):
    widths = [
        0 if unicodedata.category(char).startswith("M") or unicodedata.category(char) == "Cf"
        else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        for char in cluster
    ]
    if "\u200d" in cluster or "\ufe0f" in cluster or "\u20e3" in cluster or any(0x1F1E6 <= ord(char) <= 0x1F1FF for char in cluster):
        return max(2, max(widths, default=0))
    return max(widths, default=0)


def text_width(text):
    return sum(cluster_width(cluster) for cluster in text_clusters(text))


def wrap_cell(text, width):
    lines, line, used = [], "", 0
    for cluster in text_clusters(text):
        size = cluster_width(cluster)
        if used + size > width and line:
            lines.append(line)
            line, used = "", 0
        line += cluster
        used += size
    lines.append(line)
    return lines


def write_result(text):
    if not sys.stdout.isatty() and hasattr(sys.stdout, "buffer"):
        sys.stdout.flush()
        sys.stdout.buffer.write(text.encode(sys.stdout.encoding or "utf-8"))
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(text)


def print_grid(names, rows, headers, mode, nullvalue="", column_widths=(), newline="\n"):
    emit = lambda text="": write_result(text + newline)
    data = ([names] if headers else []) + rows
    if not data:
        return
    data = [[result_text(value, nullvalue).replace("\r", "\\r").replace("\t", "\\t").replace("\x00", "\\0").split("\n") for value in row] for row in data]
    widths = [max(text_width(line) for row in data for line in row[index]) for index in range(len(names))]
    minimums = [max(1, max((text_width(char) for row in data for line in row[index] for char in text_clusters(line)), default=1)) for index in range(len(names))]
    if mode in ("column", "table"):
        for index, width in enumerate(column_widths[:len(names)]):
            if width:
                widths[index] = max(width, minimums[index])
    if sys.stdout.isatty():
        limit = max(2, shutil.get_terminal_size().columns - 1)
        overhead = 2 * (len(names) - 1) if mode == "column" else 3 * len(names) + 1
        if sum(minimums) + overhead > limit:
            if len(names) > 1:
                for index, name in enumerate(names):
                    print_grid([name], [[row[index]] for row in rows], headers, mode, nullvalue, column_widths[index:index + 1], newline)
                return
            for row in data:
                for line in row[0]:
                    for part in wrap_cell(line, limit):
                        emit(part)
            return
        widths = [max(min(width, limit), minimum) for width, minimum in zip(widths, minimums)]
        while sum(widths) + overhead > limit:
            index = max((i for i in range(len(widths)) if widths[i] > minimums[i]), key=lambda i: widths[i])
            widths[index] -= 1
    data = [[[part for line in cell for part in wrap_cell(line, width)] for cell, width in zip(row, widths)] for row in data]
    if mode == "box":
        top, middle, bottom, vertical = ("┌", "┬", "┐"), ("├", "┼", "┤"), ("└", "┴", "┘"), "│"
    else:
        top = middle = bottom = ("+", "+", "+")
        vertical = "|"

    def border(parts):
        stroke = "─" if mode == "box" else "-"
        emit(parts[0] + parts[1].join(stroke * (width + 2) for width in widths) + parts[2])

    if mode != "column":
        border(top)
    for index, row in enumerate(data):
        for line_number in range(max(len(cell) for cell in row)):
            cells = []
            for cell, width in zip(row, widths):
                line = cell[line_number] if line_number < len(cell) else ""
                cells.append(line + " " * (width - text_width(line)))
            emit("  ".join(cells).rstrip() if mode == "column" else vertical + " " + (" " + vertical + " ").join(cells) + " " + vertical)
        if headers and index == 0:
            if mode == "column":
                emit("  ".join("-" * width for width in widths))
            else:
                border(middle)
    if mode != "column":
        border(bottom)


def parse_separators(argument):
    lexer = shlex.shlex(argument, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    values = list(lexer)
    if len(values) not in (1, 2):
        raise ValueError("Usage: .separator <column-separator> [row-separator]")
    escapes = {"t": "\t", "n": "\n", "r": "\r", "\\": "\\"}
    return [re.sub(r"\\([tnr\\])", lambda match: escapes[match[1]], value) for value in values]


def print_results(names, rows, headers, mode, nullvalue="", column_separator=None, row_separator=None, column_widths=(), newline=None):
    ending = newline if newline is not None else "\n"
    emit = lambda text="": write_result(text + ending)
    if mode in ("json", "jsonpretty"):
        values = [[value.hex() if isinstance(value, bytes) else None if isinstance(value, float) and not math.isfinite(value) else value for value in row] for row in rows]
        if headers:
            keys = []
            for name in names:
                key, suffix = name, 2
                while key in keys or (key != name and key in names):
                    key = f"{name}_{suffix}"
                    suffix += 1
                keys.append(key)
            values = [dict(zip(keys, row)) for row in values]
        emit(json.dumps(values, ensure_ascii=False, allow_nan=False, indent=2 if mode == "jsonpretty" else None).replace("\n", ending))
    elif mode in ("csv", "tabs"):
        output = io.StringIO(newline="")
        separator = column_separator if column_separator is not None else "," if mode == "csv" else "\t"
        ending = row_separator if row_separator is not None else newline if newline is not None else "\r\n"
        for row in ([names] if headers else []) + rows:
            fields = []
            for value in row:
                text = result_text(value, nullvalue)
                if any(char in text for char in ('"', '\r', '\n')) or (separator and separator in text) or (ending and ending in text) or (len(row) == 1 and not text):
                    text = '"' + text.replace('"', '""') + '"'
                fields.append(text)
            output.write(separator.join(fields) + ending)
        write_result(output.getvalue())
    elif mode in ("column", "table", "box"):
        print_grid(names, rows, headers, mode, nullvalue, column_widths, ending)
    elif mode == "markdown":
        def cell(value):
            return result_text(value, nullvalue).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
        emit("| " + " | ".join(cell(name) if headers else "" for name in names) + " |")
        emit("| " + " | ".join("---" for name in names) + " |")
        for row in rows:
            emit("| " + " | ".join(cell(value) for value in row) + " |")
    elif mode == "line":
        for index, row in enumerate(rows):
            if index:
                emit()
            for name, value in zip(names, row):
                text = result_text(value, nullvalue).replace("\r", "\\r").replace("\n", "\\n")
                emit(f"{name} = {text}" if headers else text)
    else:
        separator = "," if mode == "quote" else column_separator if column_separator is not None else "|"
        ending = ending if mode == "quote" or row_separator is None else row_separator
        format_value = sql_literal if mode == "quote" else lambda value: result_text(value, nullvalue)
        if headers:
            emit(separator.join(format_value(name) for name in names))
        for row in rows:
            emit(separator.join(format_value(value) for value in row))


def print_results_stream(names, cursor, headers, mode, nullvalue="", column_separator=None, row_separator=None, newline=None):
    ending = newline if newline is not None else "\n"
    result_count = 0
    if mode in ("csv", "tabs"):
        separator = column_separator if column_separator is not None else "," if mode == "csv" else "\t"
        ending = row_separator if row_separator is not None else newline if newline is not None else "\r\n"
        for row in ([names] if headers else []):
            fields = []
            for value in row:
                text = result_text(value, nullvalue)
                if any(char in text for char in ('"', '\r', '\n')) or (separator and separator in text) or (ending and ending in text) or (len(row) == 1 and not text):
                    text = '"' + text.replace('"', '""') + '"'
                fields.append(text)
            write_result(separator.join(fields) + ending)
        for row in cursor:
            result_count += 1
            fields = []
            for value in row:
                text = result_text(value, nullvalue)
                if any(char in text for char in ('"', '\r', '\n')) or (separator and separator in text) or (ending and ending in text) or (len(row) == 1 and not text):
                    text = '"' + text.replace('"', '""') + '"'
                fields.append(text)
            write_result(separator.join(fields) + ending)
        return result_count
    if mode == "line":
        for index, row in enumerate(cursor):
            result_count += 1
            if index:
                write_result(ending)
            for name, value in zip(names, row):
                text = result_text(value, nullvalue).replace("\r", "\\r").replace("\n", "\\n")
                write_result((f"{name} = " if headers else "") + text + ending)
        return result_count
    separator = "," if mode == "quote" else column_separator if column_separator is not None else "|"
    ending = ending if mode == "quote" or row_separator is None else row_separator
    format_value = sql_literal if mode == "quote" else lambda value: result_text(value, nullvalue)
    if headers:
        write_result(separator.join(format_value(name) for name in names) + ending)
    for row in cursor:
        result_count += 1
        write_result(separator.join(format_value(value) for value in row) + ending)
    return result_count
