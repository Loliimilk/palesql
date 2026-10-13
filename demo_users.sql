-- Fictional data for manual PaleSQL testing. Run against an empty database.
PRAGMA foreign_keys = ON;

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    email TEXT UNIQUE,
    age INTEGER CHECK (age IS NULL OR age >= 0),
    city TEXT,
    bio TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT '2026-10-01 12:00:00',
    avatar BLOB
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
    category TEXT NOT NULL
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'paid', 'shipped', 'cancelled')),
    created_at TEXT NOT NULL,
    note TEXT
);

CREATE TABLE order_items (
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    total_cents INTEGER GENERATED ALWAYS AS (quantity * unit_price_cents) STORED,
    PRIMARY KEY (order_id, product_id)
);

CREATE TABLE payments (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    method TEXT NOT NULL CHECK (method IN ('card', 'cash', 'transfer')),
    paid_at TEXT NOT NULL
);

CREATE TABLE order_audit (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    old_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX users_city ON users(city);
CREATE INDEX orders_user ON orders(user_id);
CREATE INDEX orders_status ON orders(status);
CREATE INDEX items_product ON order_items(product_id);
CREATE INDEX payments_order ON payments(order_id);
CREATE INDEX audit_order ON order_audit(order_id);

CREATE TRIGGER orders_status_changed
AFTER UPDATE OF status ON orders
WHEN OLD.status <> NEW.status
BEGIN
    INSERT INTO order_audit(order_id, old_status, new_status)
    VALUES (NEW.id, OLD.status, NEW.status);
END;

INSERT INTO users(id, username, full_name, email, age, city, bio, active, avatar) VALUES
(1, 'lera', 'Валерия Соколова', 'lera@example.test', 24, 'Владивосток', 'Люблю море и SQL :3', 1, NULL),
(2, 'alex', 'Алексей Морозов', 'alex@example.test', 31, 'Москва', 'Python, SQLite, Rust', 1, NULL),
(3, 'ada', 'Ada Example', 'ada@example.test', 28, 'London', 'Says "hello", then writes code.', 1, NULL),
(4, 'sean', 'Sean O''Reilly', 'sean@example.test', 35, 'Dublin', 'It''s a nice day.', 1, NULL),
(5, 'yuki', '田中 ゆき', 'yuki@example.test', 22, '東京', '猫とコーヒー 🐈', 1, NULL),
(6, 'zoe', 'Zoë Müller', 'zoe@example.test', 27, 'Berlin', 'Unicode: café, naïve, Straße', 1, NULL),
(7, 'guest', 'Гость без почты', NULL, NULL, NULL, NULL, 0, NULL),
(8, 'empty', 'Пустые строки', 'empty@example.test', 19, '', '', 1, X''),
(9, 'multiline', 'Многострочный Текст', 'lines@example.test', 40, 'Казань', 'Первая строка
Вторая строка
Третья строка', 1, NULL),
(10, 'markdown', 'Markdown Tester', 'md@example.test', 26, 'Санкт-Петербург', 'a|b, <tag>, **bold**, backslash: \', 1, NULL),
(11, 'tabs', 'TSV Tester', 'tabs@example.test', 33, 'Томск', 'left' || char(9) || 'middle' || char(9) || 'right', 1, NULL),
(12, 'windows', 'CRLF Tester', 'crlf@example.test', 29, 'Омск', 'first' || char(13) || char(10) || 'second', 1, NULL),
(13, 'binary', 'Binary Tester', 'binary@example.test', 30, 'Пермь', 'BLOB: 00 01 7F 80 FF', 1, X'00017F80FF'),
(14, 'nul', 'NUL Tester', 'nul@example.test', 32, 'Уфа', 'before' || char(0) || 'after', 1, NULL),
(15, 'retired', 'Архивный Пользователь', 'retired@example.test', 65, 'Хабаровск', 'Неактивный профиль', 0, NULL),
(16, 'long_name', 'Пользователь с очень длинным именем для проверки ширины колонок', 'long@example.test', 23, 'Петропавловск-Камчатский', 'Длинное описание, которое должно оставаться читаемым в табличных режимах вывода.', 1, NULL);

INSERT INTO products VALUES
(1, 'Клавиатура', 790000, 'electronics'),
(2, 'Мышь', 249000, 'electronics'),
(3, 'Кружка "SQL inside"', 99000, 'accessories'),
(4, 'Блокнот | Notes', 45000, 'stationery'),
(5, 'Наклейки 🐈', 15000, 'accessories'),
(6, 'USB-кабель', 59000, 'electronics');

INSERT INTO orders VALUES
(1001, 1, 'paid', '2026-10-01 10:00:00', 'Позвонить перед доставкой'),
(1002, 2, 'shipped', '2026-10-02 11:30:00', NULL),
(1003, 3, 'new', '2026-10-03 09:15:00', 'Gift, please'),
(1004, 1, 'new', '2026-10-04 14:00:00', ''),
(1005, 5, 'paid', '2026-10-05 16:20:00', 'ありがとうございます'),
(1006, 4, 'cancelled', '2026-10-06 08:45:00', 'Customer''s request'),
(1007, 6, 'shipped', '2026-10-07 18:00:00', NULL),
(1008, 10, 'new', '2026-10-08 12:10:00', 'Handle | with care');

INSERT INTO order_items(order_id, product_id, quantity, unit_price_cents) VALUES
(1001, 1, 1, 790000), (1001, 5, 2, 15000),
(1002, 2, 1, 249000), (1002, 6, 2, 59000),
(1003, 3, 2, 99000), (1003, 4, 1, 45000),
(1004, 5, 3, 15000), (1005, 3, 1, 99000),
(1005, 5, 4, 15000), (1006, 1, 1, 790000),
(1007, 2, 1, 249000), (1007, 4, 2, 45000),
(1008, 4, 3, 45000), (1008, 6, 1, 59000);

INSERT INTO payments VALUES
(1, 1001, 820000, 'card', '2026-10-01 10:05:00'),
(2, 1002, 367000, 'transfer', '2026-10-02 11:35:00'),
(3, 1005, 159000, 'card', '2026-10-05 16:25:00'),
(4, 1007, 339000, 'cash', '2026-10-07 18:05:00');

INSERT INTO order_audit(order_id, old_status, new_status, changed_at) VALUES
(1001, 'new', 'paid', '2026-10-01 10:05:00'),
(1002, 'paid', 'shipped', '2026-10-03 12:00:00');

CREATE VIEW order_summary AS
SELECT o.id AS order_id, u.username, o.status,
       SUM(i.total_cents) AS total_cents,
       ROUND(SUM(i.total_cents) / 100.0, 2) AS total_rub
FROM orders o JOIN users u ON u.id = o.user_id
JOIN order_items i ON i.order_id = o.id
GROUP BY o.id, u.username, o.status;

CREATE VIEW user_summary AS
SELECT u.id, u.username, u.city, u.active,
       COUNT(s.order_id) AS order_count,
       COALESCE(SUM(s.total_cents), 0) AS ordered_cents
FROM users u LEFT JOIN orders o ON o.user_id = u.id AND o.status <> 'cancelled'
LEFT JOIN order_summary s ON s.order_id = o.id
GROUP BY u.id, u.username, u.city, u.active;

ANALYZE;
