# DB.md — Fluxio: База данных и хранение

> Детальная документация по PostgreSQL, Redis и паттернам работы с данными.
> Версия: 2.0 | Дата: 2026-03-11

---

## 1. ОБЗОР ХРАНИЛИЩ

| Хранилище | Назначение | Драйвер |
|-----------|-----------|---------|
| **PostgreSQL 15** | Основные данные: предметы, покупки, история цен, статистика | SQLAlchemy 2.0 + asyncpg |
| **Redis 7** | Очереди обновлений, кэш цен, идемпотентность, rate limit, pub/sub | aioredis / redis.asyncio |

```
┌─────────────────┐    ┌─────────────────┐
│   PostgreSQL    │    │     Redis       │
│                 │    │                 │
│ • Каталог       │    │ • Очереди       │
│ • Покупки       │    │ • Кэш цен      │
│ • История цен   │    │ • Идемпотентн.  │
│ • Статистика    │    │ • Rate limits   │
│ • Логи API      │    │ • Pub/Sub       │
│ • Конфиг версии │    │ • Счётчики      │
└────────┬────────┘    └────────┬────────┘
         │                      │
    ┌────▼──────────────────────▼────┐
    │        Unit of Work            │
    │    (транзакционный контроль)   │
    └────────────────────────────────┘
```

---

## 2. POSTGRESQL — СХЕМА ТАБЛИЦ

### 2.1 Каталог предметов: `items`

Главная таблица — уникальные предметы с данными всех площадок.

```sql
CREATE TABLE items (
    id              SERIAL PRIMARY KEY,
    market_hash_name VARCHAR(255) NOT NULL UNIQUE,
    item_name       VARCHAR(500) NOT NULL,
    app_id          INTEGER NOT NULL DEFAULT 570,    -- 570=Dota2, 730=CS2

    -- CS2DT данные
    cs2dt_item_id   BIGINT UNIQUE,
    price_usd       NUMERIC(10,4),                   -- мин. цена CS2DT (USD)
    auto_deliver_price_usd NUMERIC(10,4),             -- цена с автодоставкой
    quantity        INTEGER DEFAULT 0,                -- кол-во на рынке
    auto_deliver_quantity INTEGER DEFAULT 0,
    buy_type        VARCHAR(20) NOT NULL DEFAULT 'normal', -- normal|quick|both

    -- Steam данные
    steam_price_usd NUMERIC(10,4),                   -- медиана Steam
    steam_volume_24h INTEGER,                         -- объём торгов 24ч

    -- Метаданные предмета
    hero            VARCHAR(100),
    slot            VARCHAR(50),
    rarity          VARCHAR(50),
    quality         VARCHAR(50),
    image_url       TEXT,

    -- Временные метки
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    steam_updated_at TIMESTAMPTZ,                     -- когда обновляли Steam цену

    -- Legacy (совместимость, будет удалено)
    min_price_cny   NUMERIC(10,4),
    listings_count  INTEGER DEFAULT 0
);

-- Индексы
CREATE INDEX idx_items_price_usd ON items(price_usd);
CREATE INDEX idx_items_buy_type ON items(buy_type);
CREATE INDEX idx_items_app_id ON items(app_id);
CREATE INDEX idx_items_steam_updated ON items(steam_updated_at);
```

### 2.2 Покупки: `purchases`

```sql
CREATE TABLE purchases (
    id              SERIAL PRIMARY KEY,
    product_id      VARCHAR(100) NOT NULL UNIQUE,     -- ID лота на площадке
    order_id        VARCHAR(100),
    market_hash_name VARCHAR(255) NOT NULL,
    app_id          INTEGER NOT NULL DEFAULT 570,

    -- Цены (всё в USD)
    price_usd       NUMERIC(10,4) NOT NULL,           -- цена покупки
    steam_price_usd NUMERIC(10,4),                    -- эталон Steam
    net_profit_usd  NUMERIC(10,4),                    -- чистая прибыль (после комиссии)
    discount_percent NUMERIC(5,2),

    -- Площадка
    platform        VARCHAR(50) NOT NULL DEFAULT 'cs2dt', -- cs2dt|c5game|...

    -- Статус
    status          VARCHAR(50) NOT NULL,              -- pending|success|failed|cancelled|delivered|unknown
    delivery_type   VARCHAR(20),                       -- instant|p2p
    dry_run         BOOLEAN DEFAULT FALSE,

    -- Детали
    api_response    JSONB,
    purchased_at    TIMESTAMPTZ DEFAULT NOW(),
    delivered_at    TIMESTAMPTZ,
    notes           TEXT
);

-- Индексы
CREATE INDEX idx_purchases_status ON purchases(status);
CREATE INDEX idx_purchases_date ON purchases(purchased_at);
CREATE INDEX idx_purchases_hash ON purchases(market_hash_name);
CREATE INDEX idx_purchases_dry_run ON purchases(dry_run);
```

### 2.3 Активные заказы: `active_orders`

```sql
CREATE TABLE active_orders (
    id              SERIAL PRIMARY KEY,
    order_id        VARCHAR(100) NOT NULL UNIQUE,
    purchase_id     INTEGER REFERENCES purchases(id),  -- FK!
    platform        VARCHAR(50) NOT NULL DEFAULT 'cs2dt',
    status          VARCHAR(50) NOT NULL,
    delivery_type   VARCHAR(20),                       -- instant|p2p
    expected_delivery_at TIMESTAMPTZ,                  -- для P2P: +12ч от создания
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_checked_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_active_orders_status ON active_orders(status);
```

### 2.4 История цен: `price_history`

> Высокочастотная таблица — партиционирование по месяцам рекомендуется при >10M записей.

```sql
CREATE TABLE price_history (
    id              BIGSERIAL PRIMARY KEY,
    market_hash_name VARCHAR(255) NOT NULL,
    platform        VARCHAR(20) NOT NULL,              -- 'cs2dt'|'c5game'|'steam'
    price           NUMERIC(10,4) NOT NULL,
    currency        VARCHAR(5) NOT NULL DEFAULT 'USD',
    sales_count     INTEGER,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_price_history_name_time ON price_history(market_hash_name, recorded_at DESC);
CREATE INDEX idx_price_history_platform ON price_history(platform, recorded_at DESC);
```

### 2.5 Steam данные: `steam_items` + `steam_sales_history`

```sql
CREATE TABLE steam_items (
    id              SERIAL PRIMARY KEY,
    hash_name       TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    app_id          INTEGER NOT NULL DEFAULT 570,

    -- Листинги
    sell_listings    INTEGER DEFAULT 0,
    sell_price_usd   FLOAT DEFAULT 0,

    -- priceoverview
    median_price_usd FLOAT,
    lowest_price_usd FLOAT,
    volume_24h       INTEGER,

    -- Ордера (опционально)
    item_nameid      INTEGER,                          -- кэшируется навсегда
    buy_order_price_usd  FLOAT,
    buy_order_count  INTEGER,
    sell_order_price_usd FLOAT,
    sell_order_count INTEGER,

    -- Метки
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    icon_url         TEXT,
    item_type        TEXT
);

CREATE INDEX idx_steam_items_hash ON steam_items(hash_name);
CREATE INDEX idx_steam_items_updated ON steam_items(updated_at);

CREATE TABLE steam_sales_history (
    id              SERIAL PRIMARY KEY,
    hash_name       TEXT NOT NULL,
    sale_date       TEXT NOT NULL,                     -- "Mar 07 2026 12"
    price_usd       FLOAT NOT NULL,
    volume          INTEGER NOT NULL,

    UNIQUE(hash_name, sale_date)
);

CREATE INDEX idx_steam_sales_hash ON steam_sales_history(hash_name);
```

### 2.6 Служебные таблицы

```sql
-- Watchlist
CREATE TABLE watchlist (
    id              SERIAL PRIMARY KEY,
    market_hash_name VARCHAR(255) NOT NULL UNIQUE,
    max_price_usd   NUMERIC(10,4),
    notes           TEXT,
    added_at        TIMESTAMPTZ DEFAULT NOW(),
    is_active       BOOLEAN DEFAULT TRUE
);

-- Blacklist
CREATE TABLE blacklist (
    id              SERIAL PRIMARY KEY,
    market_hash_name VARCHAR(255) NOT NULL UNIQUE,
    reason          TEXT,
    steam_url       TEXT,
    added_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Daily stats (P&L aggregates)
CREATE TABLE daily_stats (
    id              SERIAL PRIMARY KEY,
    date            DATE NOT NULL UNIQUE,
    app_id          INTEGER NOT NULL DEFAULT 570,
    items_purchased INTEGER DEFAULT 0,
    total_spent_usd NUMERIC(10,4) DEFAULT 0,
    potential_value_usd NUMERIC(10,4) DEFAULT 0,
    potential_profit_usd NUMERIC(10,4) DEFAULT 0
);

-- API logs (debug)
CREATE TABLE api_logs (
    id              SERIAL PRIMARY KEY,
    endpoint        VARCHAR(255) NOT NULL,
    method          VARCHAR(10) NOT NULL,
    platform        VARCHAR(50),
    request_body    JSONB,
    response_body   JSONB,
    status_code     INTEGER,
    duration_ms     INTEGER,
    logged_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Config versions (audit trail)
CREATE TABLE config_versions (
    id              SERIAL PRIMARY KEY,
    config_yaml     TEXT NOT NULL,
    saved_at        TIMESTAMPTZ DEFAULT NOW(),
    description     TEXT
);
```

### 2.7 Retention Policy

| Таблица | Retention | Частота очистки |
|---------|-----------|----------------|
| `price_history` | 90 дней | Ежедневно (cron) |
| `api_logs` | 90 дней | Ежедневно |
| `config_versions` | Последние 20 | При сохранении |
| `steam_sales_history` | 90 дней | Еженедельно |

### 2.8 Бэкапы
- `pg_dump` ежедневно → `./backups/db_YYYY-MM-DD.sql.gz`
- Хранить последние 30 бэкапов
- Docker-сервис `pgbackup` (уже настроен)

---

## 3. UNIT OF WORK ПАТТЕРН

### 3.1 Проблема
Текущий repository вызывает `session.commit()` внутри каждого метода — это нарушает транзакционные границы. Если нужно сохранить покупку + обновить баланс атомарно — невозможно.

### 3.2 Решение

```python
class UnitOfWork:
    """Контекстный менеджер для транзакций."""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def __aenter__(self) -> "UnitOfWork":
        self.session = self._session_factory()
        self.items = ItemRepository(self.session)
        self.purchases = PurchaseRepository(self.session)
        self.prices = PriceRepository(self.session)
        self.stats = StatsRepository(self.session)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type:
            await self.session.rollback()
        await self.session.close()

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
```

### 3.3 Использование

```python
# Атомарная операция: покупка + статистика
async with uow:
    purchase = await uow.purchases.create(product_id=..., price_usd=...)
    await uow.stats.increment_daily(spent=purchase.price_usd)
    await uow.commit()  # обе операции в одной транзакции

# Если ошибка — автоматический rollback
```

### 3.4 Правило
> **Repository НИКОГДА не вызывает commit()/rollback().**
> Это делает только UnitOfWork или вызывающий код.

---

## 4. REPOSITORY PATTERN

### 4.1 Разделение по доменам

| Файл | Класс | Ответственность |
|------|-------|----------------|
| `repos/item_repo.py` | `ItemRepository` | CRUD для items, steam_items, поиск, upsert |
| `repos/purchase_repo.py` | `PurchaseRepository` | Покупки, active_orders, статусы |
| `repos/price_repo.py` | `PriceRepository` | price_history, steam_sales_history, агрегации |
| `repos/stats_repo.py` | `StatsRepository` | daily_stats, P&L расчёты, отчёты |

### 4.2 Пример: ItemRepository

```python
class ItemRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_hash_name(self, name: str) -> Item | None:
        result = await self._session.execute(
            select(Item).where(Item.market_hash_name == name)
        )
        return result.scalar_one_or_none()

    async def upsert(self, data: ItemData) -> Item:
        """Вставить или обновить предмет."""
        item = await self.get_by_hash_name(data.market_hash_name)
        if item:
            for key, value in data.dict(exclude_none=True).items():
                setattr(item, key, value)
            item.last_seen_at = func.now()
        else:
            item = Item(**data.dict())
            self._session.add(item)
        # НЕ вызываем commit — это делает UoW
        return item

    async def get_candidates(
        self, app_id: int, min_price: Decimal, max_price: Decimal
    ) -> list[Item]:
        """Получить предметы в ценовом диапазоне."""
        result = await self._session.execute(
            select(Item)
            .where(Item.app_id == app_id)
            .where(Item.price_usd.between(min_price, max_price))
            .where(Item.quantity > 0)
            .order_by(Item.price_usd)
        )
        return list(result.scalars().all())
```

### 4.3 PurchaseRepository

```python
class PurchaseRepository:
    async def create(self, **kwargs) -> Purchase:
        purchase = Purchase(**kwargs)
        self._session.add(purchase)
        return purchase

    async def get_today_spent(self) -> Decimal:
        """Сумма потраченного за сегодня (для safety check)."""
        today = date.today()
        result = await self._session.execute(
            select(func.coalesce(func.sum(Purchase.price_usd), 0))
            .where(func.date(Purchase.purchased_at) == today)
            .where(Purchase.dry_run == False)
            .where(Purchase.status.in_(["success", "pending", "delivered"]))
        )
        return result.scalar()

    async def get_same_item_count_24h(self, market_hash_name: str) -> int:
        """Кол-во покупок того же предмета за 24 часа."""
        since = datetime.utcnow() - timedelta(hours=24)
        result = await self._session.execute(
            select(func.count())
            .select_from(Purchase)
            .where(Purchase.market_hash_name == market_hash_name)
            .where(Purchase.purchased_at >= since)
            .where(Purchase.dry_run == False)
        )
        return result.scalar()
```

---

## 5. ASYNC SESSION FACTORY

```python
# db/session.py
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

def create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(
        database_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        echo=False,
    )

def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
```

---

## 6. ALEMBIC МИГРАЦИИ

### 6.1 Настройка
```ini
# alembic.ini
[alembic]
script_location = fluxio/db/migrations
sqlalchemy.url = driver://user:pass@host/db  # переопределяется в env.py
```

### 6.2 env.py — async
```python
from fluxio.config import config
from fluxio.db.models import Base

config_section = context.config.get_main_option("sqlalchemy.url")
target_metadata = Base.metadata

# Используем async engine
connectable = create_async_engine(config.env.database_url)
```

### 6.3 Автоприменение при старте
```python
# В main.py или entrypoint
async def apply_migrations():
    """Запустить alembic upgrade head при старте."""
    from alembic import command
    from alembic.config import Config
    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")
```

### 6.4 Правила миграций
- Каждое изменение схемы — отдельная миграция
- Имя: `YYYY_MM_DD_HHMM_описание.py`
- Всегда писать `downgrade()` (откат)
- Тестировать на копии БД перед продом
- **Первая миграция**: initial — создание всех таблиц из текущих моделей

---

## 7. REDIS — КЛЮЧИ И СТРУКТУРЫ

### 7.1 Полная карта ключей

```
# ═══════════════ ОЧЕРЕДИ ОБНОВЛЕНИЙ ═══════════════

steam:update_queue              Sorted Set
  score = приоритет (0-100)
  member = market_hash_name
  Описание: очередь предметов для обновления Steam цен.
  Приоритеты: 100=срочно, 80=высокий, 50=средний, 20=низкий

steam:freshness                 Hash
  field = market_hash_name
  value = ISO timestamp
  Описание: время последнего обновления Steam цен для каждого предмета

# ═══════════════ КАНДИДАТЫ ═══════════════

arb:candidates                  Set
  member = market_hash_name
  Описание: предметы с profit >= min_discount. Обновляется Updater.

# ═══════════════ ИДЕМПОТЕНТНОСТЬ ═══════════════

arb:bought:{hash_name}          Set        TTL: 7 дней
  member = product_id
  Описание: купленные лоты для предотвращения дублей

purchased:{product_id}          String     TTL: 24ч
  value = "1"
  Описание: быстрая проверка перед покупкой

# ═══════════════ ДНЕВНЫЕ СЧЁТЧИКИ ═══════════════

arb:daily_spent                 String     TTL: до 00:00 UTC
  value = потрачено сегодня (USD)

arb:daily_count                 String     TTL: до 00:00 UTC
  value = куплено штук сегодня

arb:hourly_count                String     TTL: 1 час
  value = куплено за последний час (для kill switch)

# ═══════════════ КЭШ ЦЕН ═══════════════

steam:price:{hash_name}         Hash       TTL: 30 мин
  fields: median, lowest, volume
  Описание: кэш priceoverview

steam:nameid:{hash_name}        String     TTL: бессрочно
  value = item_nameid
  Описание: кэш nameid (не меняется)

# ═══════════════ RATE LIMITING ═══════════════

ratelimit:steam                 String
  value = Token bucket state

ratelimit:cs2dt                 String
  value = Token bucket state

ratelimit:steam:channel:{N}     String
  value = Token bucket per proxy channel

# ═══════════════ CIRCUIT BREAKER ═══════════════

cb:errors:{platform}            String     TTL: 5 мин
  value = количество последовательных ошибок

cb:state:{platform}             String     TTL: circuit_breaker_timeout
  value = "open" | "closed" | "half-open"

# ═══════════════ PUB/SUB ═══════════════

channel:worker_status           Pub/Sub
  Описание: воркеры публикуют свой статус каждые 30с

channel:purchases               Pub/Sub
  Описание: уведомления о новых покупках → Telegram, Dashboard SSE

channel:alerts                  Pub/Sub
  Описание: системные алерты → Telegram
```

### 7.2 Приоритеты очереди обновлений

```
Приоритет 100 (СРОЧНО):
  - Нет данных Steam вообще (новый предмет)
  - Цена CS2DT в рабочем диапазоне
  - Предварительная прибыль может быть >= min_discount

Приоритет 80 (ВЫСОКИЙ):
  - Был кандидатом в прошлом цикле
  - Данные Steam старше 30 мин

Приоритет 50 (СРЕДНИЙ):
  - Цена CS2DT в рабочем диапазоне
  - Данные Steam старше 2 часов

Приоритет 20 (НИЗКИЙ):
  - Данные Steam старше 6 часов

Приоритет 0 (ПРОПУСК):
  - Данные Steam свежие (< порога)
  - Предмет в blacklist
```

---

## 8. ИНДЕКСЫ И ОПТИМИЗАЦИЯ

### 8.1 Обязательные индексы

| Таблица | Индекс | Зачем |
|---------|--------|-------|
| items | `(price_usd)` | Фильтрация по ценовому диапазону |
| items | `(app_id)` | Фильтрация по игре |
| items | `(steam_updated_at)` | Поиск устаревших Steam цен |
| purchases | `(status)` | Подсчёт активных/pending |
| purchases | `(purchased_at)` | Дневная статистика |
| purchases | `(market_hash_name)` | Лимит одинаковых |
| price_history | `(market_hash_name, recorded_at DESC)` | Временные ряды |
| active_orders | `(status)` | Фильтрация активных |

### 8.2 Рекомендации по производительности
- `VACUUM ANALYZE` еженедельно для price_history
- Партиционирование price_history по месяцам при >10M записей
- Материализованное представление для daily_stats (обновление каждый час)
- Connection pool: `pool_size=5, max_overflow=10`

---

## 9. ПОТОК ДАННЫХ

```
CS2DT API ──► SCANNER ──► PostgreSQL (items)
                │                │
                ▼                ▼
          Redis очередь    Redis freshness
          (sorted set)     (hash: timestamp)
                │
                ▼
           UPDATER ──► Steam API
                │
                ▼
          PostgreSQL (steam_items, steam_sales_history)
                │
                ▼
          Redis (arb:candidates)
                │
                ▼
            BUYER ──► Safety Checks ──► CS2DT buy / dry_run
                │
                ▼
          PostgreSQL (purchases, active_orders)
                │
                ▼
         ORDER TRACKER ──► CS2DT order status
                │
                ▼
          PostgreSQL (purchases.delivered_at)
```

---

*Документ: DB.md v2.0*
*Проект: Fluxio*
*Дата: 2026-03-11*
