# Тестовая база PaleSQL

`demo_users.db` — вымышленные пользователи и небольшой магазин. Существующий `test.db` не затрагивается.

16 пользователей, 6 товаров, 8 заказов, 14 позиций заказов, 4 платежа, 2 записи аудита. Есть индексы, внешние ключи, составной ключ, вычисляемая колонка, два VIEW, триггер и статистика ANALYZE. Цены хранятся целыми числами в копейках.

Запуск из папки проекта:

```text
python main.py
.open demo_users
PRAGMA foreign_keys = ON;
.headers on
.mode box
SELECT id, username, city, active FROM users ORDER BY id;
```

Формат меняется командой `.mode list|column|table|box|csv|tabs|json|line|markdown|quote` — укажи одно имя, например `.mode csv`.

## Запросы для форматов

```sql
SELECT id, username, full_name, city FROM users WHERE id <= 6;
SELECT id, username, email, age, city, bio FROM users WHERE id IN (7, 8);
SELECT id, username, bio FROM users WHERE id BETWEEN 9 AND 12;
SELECT id, username, avatar FROM users WHERE id IN (8, 13);
SELECT bio FROM users WHERE id = 14;
SELECT * FROM order_summary ORDER BY order_id;
SELECT * FROM user_summary ORDER BY ordered_cents DESC;
SELECT id, username FROM users WHERE id < 0;
SELECT 1 AS value, 2 AS value, 3 AS value_2;
```

Строки 7 и 8 сравнивают NULL и пустой текст; 9–12 содержат переносы, Markdown, табуляции и CRLF; 13 — BLOB; 14 — NUL внутри текста; 16 — длинное имя. В JSON `.headers on` даёт объекты, `.headers off` — массивы.

## Структура

```text
.tables
.schema user%
.columns order_items
.indexes orders
.foreignkeys order_items
.views
.triggers
.fullschema
.dbinfo
.lint
```

Контроль FK включается для каждого соединения отдельно. Если его не включить, `.lint` об этом сообщит.

## Проверка триггера без сохранения изменений

```sql
BEGIN;
UPDATE orders SET status = 'paid' WHERE id = 1004;
SELECT * FROM order_audit WHERE order_id = 1004;
ROLLBACK;
```

## Пересоздание в новом файле

`demo_users.sql` содержит схему и все исходные данные. Выполни в PowerShell из папки проекта; команда откажется перезаписывать существующий файл:

```powershell
@'
import sqlite3
from pathlib import Path
target = Path('demo_users_fresh.db')
with target.open('xb'):
    pass
con = sqlite3.connect(target)
try:
    con.executescript(Path('demo_users.sql').read_text(encoding='utf-8'))
finally:
    con.close()
'@ | python -B -
```
