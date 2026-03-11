"""Доперенос sale_listings из c5game_bot.db в PostgreSQL."""

import sqlite3
import json
import time

import psycopg2
from psycopg2.extras import execute_values

sq = sqlite3.connect("c5game_bot.db")
cur = sq.cursor()
cur.execute("SELECT COUNT(*) FROM sale_listings")
total = cur.fetchone()[0]
print(f"sale_listings: {total} записей для переноса")

if total == 0:
    exit()

pg = psycopg2.connect(host="localhost", port=5432, user="bot", password="secret", dbname="c5game_bot")
pgcur = pg.cursor()

cols = [
    "c5_id", "market_hash_name", "item_name", "price_cny",
    "seller_id", "delivery", "accept_bargain", "raw_data",
    "is_active", "scanned_at",
]

cur.execute(f"SELECT {','.join(cols)} FROM sale_listings")

BATCH = 2000
t0 = time.time()
migrated = 0

while True:
    rows = cur.fetchmany(BATCH)
    if not rows:
        break
    # raw_data (idx 7) нужно преобразовать из строки в JSON
    values = []
    for row in rows:
        row = list(row)
        # accept_bargain (idx 6): int -> bool
        row[6] = bool(row[6])
        # is_active (idx 8): int -> bool
        row[8] = bool(row[8])
        # raw_data (idx 7): str -> JSON
        if row[7] is not None:
            try:
                row[7] = json.loads(row[7]) if isinstance(row[7], str) else row[7]
            except json.JSONDecodeError:
                row[7] = None
        row[7] = psycopg2.extras.Json(row[7]) if row[7] is not None else None
        values.append(tuple(row))

    cols_str = ",".join(cols)
    template = f"({','.join(['%s'] * len(cols))})"
    execute_values(
        pgcur,
        f"INSERT INTO sale_listings ({cols_str}) VALUES %s ON CONFLICT (c5_id) DO NOTHING",
        values,
        template=template,
    )
    pg.commit()
    migrated += len(values)
    print(f"  {migrated}/{total}...")

elapsed = time.time() - t0
print(f"sale_listings: {migrated} записей перенесено за {elapsed:.1f}с")

pgcur.execute("SELECT COUNT(*) FROM sale_listings")
print(f"Проверка PostgreSQL: {pgcur.fetchone()[0]} записей")

sq.close()
pg.close()
