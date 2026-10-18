import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE users(
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    full_name TEXT,
    city TEXT,
    avatar BLOB,
    note TEXT DEFAULT 'guest'
);
CREATE TABLE orders(
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    amount REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX orders_user_id ON orders(user_id);
CREATE VIEW user_names AS
    SELECT id, username, full_name FROM users;
CREATE TRIGGER users_audit AFTER INSERT ON users
BEGIN
    UPDATE users SET note = 'created' WHERE id = NEW.id;
END;
INSERT INTO users(id, username, full_name, city, avatar)
VALUES
    (1, 'alex', 'Alex Morgan', 'Toronto', X'00FF10'),
    (2, 'ada', 'Ada Example', 'London', NULL),
    (3, 'sean', 'Sean O''Reilly', 'Dublin', X'89504E47');
INSERT INTO orders(user_id, amount) VALUES (1, 10.50), (1, 20.00), (2, 99.95);
"""


@contextmanager
def test_database():
    with tempfile.TemporaryDirectory(prefix="palesql-tests-") as directory:
        path = Path(directory) / "fixture.db"
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SCHEMA_SQL)
            connection.commit()
        finally:
            connection.close()
        yield path


def open_fixture_connection():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.executescript(SCHEMA_SQL)
    return connection
