# SPEC.md — Fluxio v2.0
> Техническое задание для арбитражной платформы Fluxio.
> Язык реализации: Python 3.11+. Все логи, комментарии и сообщения — на русском языке.
> Дата: 2026-03-11. Версия: 2.0

---

## 1. ОБЗОР ПРОЕКТА

### 1.1 Цель
**Fluxio** — модульная арбитражная платформа для торговли скинами Steam, работающая 24/7:
1. Мониторит рынки нескольких торговых площадок (CS2DT, C5Game, и др.)
2. Анализирует каждый предмет на выгодность относительно **Steam Market** (медиана + buy orders)
3. Автоматически покупает предметы, соответствующие заданным стратегиям
4. Отслеживает доставку (trade offer, P2P до 12ч)
5. Уведомляет через **Telegram** и ведёт полную статистику через **веб-дашборд**

### 1.2 Поддерживаемые игры
| Игра | AppID | Статус |
|------|-------|--------|
| Dota 2 | 570 | Первая реализация |
| CS2 | 730 | Фаза 7 |
| Rust | 252490 | Будущее |

Архитектура параметризована через `appId` — добавление новых игр не требует изменений ядра.

### 1.3 Торговые площадки
| Площадка | Роль | Метод | Статус |
|----------|------|-------|--------|
| **CS2DT** (HaloSkins) | Основная покупка | API (app-key) | Реализовано |
| **C5Game** | Покупка (legacy) | Web HTTP-перехват | Фаза 7 |
| **BUFF163** | Эталон цены | Заложено в архитектуре | Будущее |
| **Steam Market** | Эталон цены + инвентарь | Public API + cookie | Реализовано |

Все площадки реализуют единый `Protocol MarketClient` — подключение новой площадки = новый файл.

### 1.4 Что НЕ входит в scope
- Продажа предметов (ни на Steam, ни где-либо ещё) — продажа вручную
- Поддержка нескольких Steam-аккаунтов для торговли (только для парсинга цен)
- ML/AI предсказание цен (зарезервировано для будущих версий)
- Мобильное приложение

### 1.5 Ключевые метрики
- **Скорость**: полный скан рынка Dota 2 (10K items) < 5 минут
- **Актуальность**: Steam цены кандидатов < 30 мин давности
- **Надёжность**: uptime 99%+, автовосстановление после сбоев
- **Безопасность**: 4 уровня защиты от петли покупок

---

## 2. ПРИНЦИПЫ РАЗРАБОТКИ

### 2.1 Три столпа
1. **Безопасность** — бот работает с реальными деньгами. Любой баг = потеря средств
2. **Скорость** — арбитражные окна закрываются за секунды. Медленный бот = упущенная прибыль
3. **Масштабируемость** — от одной игры/площадки к мультигейм мультиплатформе без переписывания

### 2.2 Обязательные правила кода
- Python 3.11+, async/await для всего I/O
- Type hints обязательны для всех функций
- Все логи и комментарии на русском языке
- Никогда не хардкодить секреты — только из `.env`
- `dry_run=True` по умолчанию во всех тестах и при первом запуске
- Только `aiohttp` для HTTP (синхронные запросы запрещены)
- Unit of Work для транзакций БД (repository не делает commit)

### 2.3 Запрещено
- Реализовывать несколько фаз за одну сессию без подтверждения
- Делать реальные покупки в тестах
- Коммитить `.env`, `.db`, `.log` файлы
- Отключать SSL верификацию (использовать certificate pinning)
- Игнорировать ошибки API (логировать + уведомлять)

---

## 3. АРХИТЕКТУРА (обзор)

> Подробности → [ARCHITECTURE.md](ARCHITECTURE.md)

### 3.1 Паттерн: Worker-based монолит
Один процесс, несколько независимых asyncio-воркеров:

```
┌─────────────────────────────────────────────────────────────┐
│                      Fluxio Process                         │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────────┐│
│  │ Scanner  │  │ Updater  │  │  Buyer   │  │OrderTracker ││
│  │(15-30мин)│  │(24/7)    │  │(1-5мин)  │  │(5мин)       ││
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────┬──────┘│
│       │              │             │                │       │
│  ┌────▼──────────────▼─────────────▼────────────────▼──┐   │
│  │              ServiceContainer (DI)                   │   │
│  │  MarketClient │ PriceProvider │ Strategy │ Notifier  │   │
│  └──────────────────────┬───────────────────────────────┘   │
│                         │                                   │
│  ┌──────────┐    ┌──────▼──────┐    ┌─────────────┐        │
│  │PostgreSQL│    │   Redis     │    │  Dashboard   │        │
│  │ (данные) │    │(очереди,кэш)│    │(FastAPI+SSE) │        │
│  └──────────┘    └─────────────┘    └─────────────┘        │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 Ключевые абстракции (Protocol)
```python
class MarketClient(Protocol):
    """Любая торговая площадка."""
    async def search_market(self, app_id: int, page: int) -> list[MarketItem]: ...
    async def get_balance(self) -> Decimal: ...
    async def buy(self, item_id: str, price: Decimal) -> BuyResult: ...

class PriceProvider(Protocol):
    """Любой источник эталонных цен."""
    async def get_price(self, market_hash_name: str) -> PriceData | None: ...

class Strategy(Protocol):
    """Стратегия арбитража."""
    def analyze(self, item: MarketItem, reference: PriceData) -> AnalysisResult: ...
```

### 3.3 Структура проекта (целевая)
```
fluxio/
├── fluxio/                      # Основной пакет (бывший bot/)
│   ├── __init__.py
│   ├── main.py                  # Entry point + lifecycle
│   ├── config.py                # AppConfig + hot reload
│   ├── interfaces/              # Protocol определения
│   │   ├── market_client.py
│   │   ├── price_provider.py
│   │   ├── strategy.py
│   │   └── notifier.py
│   ├── api/                     # Реализации клиентов
│   │   ├── cs2dt/
│   │   │   ├── client.py
│   │   │   └── models.py
│   │   ├── steam/
│   │   │   ├── client.py
│   │   │   └── models.py
│   │   └── c5game/
│   │       └── client.py        # Web HTTP (фаза 7)
│   ├── core/                    # Бизнес-логика
│   │   ├── workers/
│   │   │   ├── scanner.py
│   │   │   ├── updater.py
│   │   │   ├── buyer.py
│   │   │   └── order_tracker.py
│   │   ├── strategies/
│   │   │   ├── discount.py      # Дисконт к Steam
│   │   │   └── cross_platform.py # Кросс-площадочный (будущее)
│   │   ├── analyzer.py
│   │   └── safety.py
│   ├── db/
│   │   ├── models.py
│   │   ├── session.py           # async_session factory
│   │   ├── unit_of_work.py      # UoW context manager
│   │   ├── repos/
│   │   │   ├── item_repo.py
│   │   │   ├── purchase_repo.py
│   │   │   ├── price_repo.py
│   │   │   └── stats_repo.py
│   │   └── migrations/
│   ├── services/
│   │   └── container.py         # ServiceContainer (DI)
│   ├── notifications/
│   │   └── telegram.py
│   ├── dashboard/
│   │   ├── app.py               # FastAPI
│   │   ├── routes/
│   │   │   ├── admin.py         # Системный дашборд
│   │   │   └── user.py          # Пользовательский дашборд
│   │   └── templates/
│   └── utils/
│       ├── logger.py
│       ├── rate_limiter.py
│       ├── circuit_breaker.py
│       └── exporters.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── conftest.py
├── config.yaml
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── alembic.ini
├── pyproject.toml
├── SPEC.md                      # Этот файл
├── ARCHITECTURE.md
├── API.md
├── DB.md
├── PHASES.md
└── CLAUDE.md
```

---

## 4. КОНФИГУРАЦИЯ

### 4.1 `.env` — только секреты
```env
# CS2DT (основная площадка)
CS2DT_APP_KEY=your_key
CS2DT_APP_SECRET=your_secret

# C5Game (legacy API)
C5GAME_APP_KEY=your_key
C5GAME_APP_SECRET=your_secret

# Steam
STEAM_LOGIN_SECURE=your_cookie
SESSION_ID=your_session
BROWSER_ID=your_browser

# Telegram
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id

# PostgreSQL
POSTGRES_USER=bot
POSTGRES_PASSWORD=secret
POSTGRES_DB=fluxio
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

# Redis
REDIS_URL=redis://localhost:6379/0

# Прокси (для Steam)
HTTP_PROXY=http://user:pass@host:port
HTTPS_PROXY=http://user:pass@host:port

# Логирование
LOG_LEVEL=INFO
```

### 4.2 `config.yaml` — бизнес-параметры (горячая перезагрузка каждые 60с)
```yaml
trading:
  min_discount_percent: 15        # Минимальный дисконт к эталону (%)
  min_price_usd: 0.10             # Минимальная цена предмета (USD)
  max_price_usd: 5.00             # Максимальная цена предмета (USD)
  max_single_purchase_usd: 5.00   # Макс. бюджет на одну покупку (USD)
  daily_limit_usd: 100.0          # Дневной лимит трат (USD)
  stop_balance_usd: 10.0          # Остановить при остатке ниже (USD)
  max_same_item_count: 3          # Макс. штук одного предмета за 24ч
  min_sales_volume_7d: 10         # Мин. продаж за 7 дней (ликвидность)
  dry_run: true                   # true = симуляция
  semi_auto: false                # true = подтверждение через Telegram

fees:
  steam_fee_percent: 13.0         # Комиссия Steam Market
  cs2dt_fee_percent: 0.0          # Комиссия CS2DT
  c5game_fee_percent: 2.5         # Комиссия C5Game

monitoring:
  interval_seconds: 300           # Интервал сканирования (5 мин)
  price_history_days: 30
  new_listing_check: true

anti_manipulation:
  max_price_growth_2w_percent: 30
  min_sales_at_current_price: 5

safety:
  max_purchases_per_hour: 20      # Лимит покупок в час (kill switch)
  balance_anomaly_percent: 20     # Аномалия: баланс упал на >20% за цикл
  circuit_breaker_threshold: 5    # Ошибок подряд до отключения API
  circuit_breaker_timeout_min: 5  # Минут паузы после отключения

update_queue:
  scanner_interval_seconds: 900   # 15 мин
  buyer_interval_seconds: 60      # 1 мин
  freshness_candidate_minutes: 30
  freshness_normal_hours: 2
  freshness_low_hours: 6
  full_history_interval_hours: 24

notifications:
  quiet_mode: false
  daily_report_time: "09:00"      # UTC+3
  events:
    purchase_success: true
    purchase_error: true
    low_balance: true
    daily_limit_reached: true
    api_unavailable: true
    good_deal_found: false

games:
  - app_id: 570
    name: "Dota 2"
    enabled: true
  - app_id: 730
    name: "CS2"
    enabled: false

blacklist:
  items: []

whitelist:
  enabled: false
  items: []
```

---

## 5. БИЗНЕС-ЛОГИКА

### 5.1 Эталон цены (Steam)
```
1. Получить priceoverview → median_price, lowest_price, volume_24h
2. Если предмет новый или данные > 24ч → дополнительно pricehistory:
   - Медиана продаж за 30 дней
   - Объём продаж за 7/30 дней
3. Если volume_7d < min_sales_volume_7d → неликвид, пропустить
4. Эталон = median_price (с учётом buy orders если доступны)
5. Чистая прибыль = эталон * (1 - steam_fee_percent/100) - цена_покупки
```

### 5.2 Критерии покупки (Strategy: Discount)
```python
def analyze(item: MarketItem, reference: PriceData) -> AnalysisResult:
    # 1. Есть Steam цена?
    if not reference:
        return skip("Нет данных Steam")

    # 2. Чистая прибыль после комиссии
    net_price = reference.median * (1 - config.fees.steam_fee_percent / 100)
    discount = (net_price - item.price) / net_price * 100
    if discount < config.trading.min_discount_percent:
        return skip(f"Дисконт {discount:.1f}% < {config.min_discount_percent}%")

    # 3. Ликвидность
    if reference.volume_7d < config.trading.min_sales_volume_7d:
        return skip(f"Ликвидность {reference.volume_7d} < {config.min_sales_volume_7d}")

    # 4. Ценовой диапазон
    if not (config.trading.min_price_usd <= item.price <= config.trading.max_price_usd):
        return skip("Вне ценового диапазона")

    # 5. Чёрный список
    if item.market_hash_name in blacklist:
        return skip("В чёрном списке")

    # 6. Антиманипуляция
    if is_pumped(item, reference):
        return skip("Подозрение на pump & dump")

    # 7. Белый список
    if config.whitelist.enabled and item.market_hash_name not in whitelist:
        return skip("Не в белом списке")

    return profitable(discount=discount, net_profit=net_price - item.price)
```

### 5.3 Антиманипуляция
```
Предмет считается «накачанным» если:
  - Цена выросла > max_price_growth_2w_percent за 2 недели
  И
  - Продаж по текущей цене за месяц < min_sales_at_current_price
```

### 5.4 Safety Checks (4 уровня защиты)
```
Уровень 1: ЛИМИТЫ
  - max_single_purchase_usd → макс. цена одной покупки
  - daily_limit_usd → дневной лимит трат
  - stop_balance_usd → минимальный остаток баланса
  - max_same_item_count → макс. одинаковых предметов за 24ч

Уровень 2: ИДЕМПОТЕНТНОСТЬ
  - Redis Set: purchased:{product_id} с TTL 7 дней
  - Перед покупкой → SISMEMBER, после → SADD

Уровень 3: KILL SWITCH (автоматический)
  - > max_purchases_per_hour покупок за час → STOP + Telegram
  - Баланс упал на > balance_anomaly_percent → STOP + Telegram

Уровень 4: CIRCUIT BREAKER
  - > circuit_breaker_threshold ошибок API подряд → отключить на timeout
  - Health check каждые 30с для автовосстановления
```

---

## 6. TELEGRAM БОТ

### 6.1 Команды (25)
| Команда | Описание |
|---------|----------|
| `/start` | Приветствие, список команд |
| `/status` | Состояние бота (работает/пауза/dry-run) |
| `/balance` | Баланс на всех площадках |
| `/stop` | Экстренная остановка |
| `/pause` | Пауза мониторинга |
| `/resume` | Возобновить работу |
| `/stats` | Статистика за сегодня и всё время |
| `/stats 7d` | Статистика за 7 дней |
| `/config` | Текущий конфиг (без секретов) |
| `/setdiscount 20` | Изменить мин. дисконт |
| `/setlimit 100` | Изменить дневной лимит |
| `/setprice 0.5 10` | Изменить мин/макс цену |
| `/blacklist add <name>` | Добавить в ЧС |
| `/blacklist list` | Показать ЧС |
| `/blacklist remove <name>` | Удалить из ЧС |
| `/watchlist add <name> [цена]` | Добавить в watchlist |
| `/watchlist list` | Показать watchlist |
| `/dryrun on\|off` | Dry-run режим |
| `/semiauto on\|off` | Полуавтоматический режим |
| `/quiet on\|off` | Тихий режим |
| `/export` | CSV покупок за 30 дней |
| `/workers` | Статус всех воркеров |
| `/queue` | Размер очередей Redis |
| `/candidates` | Текущие кандидаты на покупку |
| `/report` | Ежедневный отчёт |

### 6.2 Уведомления
```
✅ КУПЛЕНО: {name}
   Цена: ${price} | Эталон: ${steam_price}
   Дисконт: {discount}% | Чистая прибыль: ${net_profit}
   Баланс: ${balance}

❌ ОШИБКА ПОКУПКИ: {name} — {error}

⚠️ НИЗКИЙ БАЛАНС: ${balance} (порог: ${stop_balance})

🚫 ДНЕВНОЙ ЛИМИТ: ${spent} / ${limit} потрачено

🔴 API {platform} НЕДОСТУПЕН > {N} мин (circuit breaker active)

📊 ЕЖЕДНЕВНЫЙ ОТЧЁТ ({date}):
   Куплено: {count} | Потрачено: ${spent}
   Эталон: ${value} | Чистая прибыль: ${profit}
```

### 6.3 Semi-auto режим
При `semi_auto: true` перед покупкой:
```
🎯 ВЫГОДНЫЙ ПРЕДМЕТ:
{name}
Цена: ${price} | Эталон: ${steam_price}
Дисконт: {discount}% | Прибыль: ${net_profit}
Продаж 30д: {sales}

[✅ Купить] [❌ Пропустить] [🚫 В чёрный список]
```
Таймаут: 60 секунд → автоматически пропустить.

---

## 7. ВЕБ-ДАШБОРД

### 7.1 Стек
- **FastAPI** + **Jinja2** + **Chart.js**
- **Server-Sent Events (SSE)** для realtime обновлений
- Порт: 8080 (настраивается)

### 7.2 Две части

#### Admin (системный)
| Страница | URL | Содержимое |
|---------|-----|------------|
| Health | `/admin/` | Статус воркеров, аптайм, memory, CPU |
| Workers | `/admin/workers` | Состояние каждого воркера, последний цикл |
| Queues | `/admin/queues` | Redis очереди, размеры, throughput |
| Logs | `/admin/logs` | Realtime логи через SSE (последние 500) |
| Config | `/admin/config` | Текущий конфиг, история изменений |
| API Health | `/admin/api-health` | Статус каждого API, circuit breaker state |

#### User (пользовательский)
| Страница | URL | Содержимое |
|---------|-----|------------|
| Главная | `/` | Баланс, сводка за день, P&L |
| Каталог | `/items` | Все отслеживаемые предметы, цены, статус |
| Кандидаты | `/candidates` | Текущие арбитражные возможности |
| Покупки | `/purchases` | Таблица покупок с фильтрами |
| Статистика | `/stats` | Графики P&L, трат по дням |
| Чёрный список | `/blacklist` | Управление ЧС |
| Экспорт | `/export` | CSV/Excel скачивание |

### 7.3 API endpoints
```
GET  /health                    # Docker healthcheck
GET  /api/v1/status             # Полный статус бота (JSON)
GET  /api/v1/workers            # Статус воркеров
GET  /api/v1/candidates         # Текущие кандидаты
GET  /api/v1/purchases          # Покупки с пагинацией
GET  /api/v1/stats/daily        # Дневная статистика
GET  /api/v1/stats/range        # Статистика за период
GET  /sse/logs                  # Server-Sent Events: логи
GET  /sse/workers               # Server-Sent Events: воркеры
```

---

## 8. АГЕНТЫ РАЗРАБОТКИ (Claude Code)

### 8.1 Концепция
Разработка ведётся как работа команды. Каждый агент имеет свою роль, зону ответственности и правила. При запуске сессии разработки — указывать роль.

### 8.2 Роли

| # | Роль | Описание | Фокус |
|---|------|----------|-------|
| 1 | **PM** | Менеджер проекта | Приоритеты фаз, трекинг прогресса, ревью результатов |
| 2 | **Architect** | Архитектор | Protocol, DI, паттерны, ревью архитектурных решений |
| 3 | **Backend Dev** | Разработчик | Реализация фич, API клиенты, воркеры, бизнес-логика |
| 4 | **Tester** | Тестировщик | Unit/integration тесты, edge cases, покрытие 70%+ |
| 5 | **Security** | Безопасник | Аудит, SSL, secrets, OWASP, rate limits, safety checks |
| 6 | **DevOps** | Инженер инфраструктуры | Docker, CI/CD, мониторинг, деплой, /health |
| 7 | **DBA** | Администратор БД | Схема, миграции Alembic, индексы, оптимизация запросов |

### 8.3 Правила агентов
- Каждый агент читает весь SPEC v2 (все 5 файлов) перед началом работы
- Агент работает ТОЛЬКО в своей зоне ответственности
- При необходимости выйти за рамки — документировать и предупредить
- Все агенты соблюдают общие правила из CLAUDE.md
- Один агент может выполнять задачи нескольких ролей при явном указании

---

## 9. ССЫЛКИ НА ДЕТАЛЬНЫЕ ДОКУМЕНТЫ

| Документ | Содержание |
|----------|------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Модульная архитектура, Protocol, DI, воркеры, диаграммы потоков данных |
| [API.md](API.md) | CS2DT API, Steam API, C5Game web, rate limits, retry стратегии |
| [DB.md](DB.md) | PostgreSQL схема, Redis ключи, Alembic миграции, Unit of Work |
| [PHASES.md](PHASES.md) | Фазы реализации 0-7, чеклисты, критерии завершения |
| [CLAUDE.md](CLAUDE.md) | Правила для агентов, секции по ролям |

---

*Документ: SPEC.md v2.0*
*Проект: Fluxio — арбитражная платформа для скинов Steam*
*Дата: 2026-03-11*
