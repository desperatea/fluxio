# PHASES.md — Fluxio: Фазы реализации

> Порядок разработки с учётом текущего состояния кода и приоритетов.
> Версия: 2.0 | Дата: 2026-03-11

---

## ТЕКУЩЕЕ СОСТОЯНИЕ

### Что уже работает ✅
- CS2DT Client (724 строки, все эндпоинты)
- Steam Client (625 строк, прокси, multi-cookie)
- C5Game Client (587 строк, legacy)
- Конфигурация + hot reload
- Rate Limiter (Token Bucket)
- SQLAlchemy модели (12 таблиц)
- Repository (монолитный, требует разбиения)
- Analyzer (дисконт, ликвидность, антиманипуляция)
- Docker Compose (bot, postgres, redis, pgbackup)

### Критические баги 🔴
1. `bot/main.py` вызывает несуществующий метод `get_steam_info()` → краш при старте
2. `repository.py` — инвертированная логика `deactivate_stale_listings()`
3. `analyzer.py` — division by zero при `steam_price == 0`
4. `dashboard/app.py` — race condition JSON I/O + XSS

### Что отсутствует ❌
- Protocol интерфейсы, ServiceContainer, Unit of Work
- Redis интеграция (настроен, но не используется в коде)
- Buyer (заглушка 25 строк)
- Telegram бот (заглушка 28 строк)
- Alembic миграции (только скелет)
- Тесты (<20% покрытие)
- CI/CD, git repository

---

## ПРАВИЛА ФАЗ

1. **Одна фаза за сессию** — не переходить без подтверждения
2. **Каждая фаза заканчивается** тестом: `pytest tests/` + dry_run без ошибок
3. **Критерии завершения** (Definition of Done) описаны для каждой фазы
4. **Фаза 0 обязательна первой** — без неё код не запускается

---

## Фаза 0 — Стабилизация 🔧

> **Цель**: сделать код запускаемым, исправить критические баги, подготовить git.
> **Приоритет**: БЛОКИРУЮЩИЙ — без этого ничего не работает.

### Задачи

- [ ] **Фикс бага #1**: `main.py` — убрать вызов `get_steam_info()`, заменить на корректную проверку
- [ ] **Фикс бага #2**: `repository.py` — исправить инвертированную логику `deactivate_stale_listings()`
- [ ] **Фикс бага #3**: `analyzer.py` — добавить guard `if steam_price <= 0: return skip()`
- [ ] **Фикс бага #4**: `dashboard/app.py` — убрать JSON file I/O, заглушка пока
- [ ] **Переименование**: `bot/` → `fluxio/` (пакет Python)
- [ ] **Git**: `git init`, `.gitignore` (`.env`, `*.db`, `logs/`, `backups/`, `data/`, `__pycache__/`)
- [ ] **Alembic**: создать initial migration из текущих моделей
- [ ] **Валидация конфига**: `min_price <= max_price`, `discount > 0`, типы
- [ ] **Тест**: бот запускается без crash, config загружается, dry_run цикл работает

### Definition of Done
```
✅ python -m fluxio.main запускается без ошибок
✅ pytest tests/ проходит
✅ git status чист, .env не в репо
✅ alembic upgrade head создаёт все таблицы
```

### Ответственные агенты
- **Backend Dev**: фикс багов, переименование
- **DBA**: Alembic initial migration
- **DevOps**: git init, .gitignore

---

## Фаза 1 — Архитектура + Observability 🏗️

> **Цель**: модульная архитектура + дашборд/логи для удобства разработки следующих фаз.
> **Приоритет**: ВЫСОКИЙ — observability нужен сразу.

### Задачи

#### Архитектура
- [ ] **Protocol интерфейсы**: `interfaces/market_client.py`, `price_provider.py`, `strategy.py`, `notifier.py`
- [ ] **ServiceContainer**: `services/container.py` — создание и lifecycle всех сервисов
- [ ] **Unit of Work**: `db/unit_of_work.py` — контекстный менеджер транзакций
- [ ] **Разбить repository**: `repos/item_repo.py`, `purchase_repo.py`, `price_repo.py`, `stats_repo.py`
- [ ] **Session factory**: `db/session.py` — вынести из repository
- [ ] **Circuit Breaker**: `utils/circuit_breaker.py`
- [ ] **Рефакторинг main.py**: тонкий — только lifecycle + container

#### Observability
- [ ] **FastAPI дашборд (admin)**: `/admin/` — health, workers status, queues
- [ ] **/health endpoint**: для Docker healthcheck
- [ ] **SSE логи**: `/admin/logs` — realtime через Server-Sent Events
- [ ] **Loguru**: structured logging с уровнями, ротация
- [ ] **Worker status**: каждый воркер публикует heartbeat в Redis pub/sub

#### Конфигурация
- [ ] **Обновить config.yaml**: USD вместо CNY, новые секции (fees, safety, update_queue, games)
- [ ] **Обновить config.py**: новые классы (FeesConfig, SafetyConfig, UpdateQueueConfig, GameConfig)

### Definition of Done
```
✅ Все Protocol интерфейсы определены с type hints
✅ ServiceContainer создаёт и инжектит все зависимости
✅ UoW работает: begin → commit/rollback
✅ /health возвращает 200 OK
✅ /admin/ показывает статус системы
✅ /admin/logs стримит логи через SSE
✅ pytest tests/ — новые тесты для UoW, container, circuit breaker
```

### Ответственные агенты
- **Architect**: Protocol, DI, UoW дизайн
- **Backend Dev**: реализация
- **DBA**: разбиение repository, session factory
- **DevOps**: /health, Docker healthcheck, SSE

---

## Фаза 2 — Мониторинг (Redis + Workers) 📡

> **Цель**: полный pipeline сбора данных: CS2DT scan → Redis queue → Steam update.
> **Приоритет**: ВЫСОКИЙ — основа для покупок.

### Задачи

- [ ] **Worker: Scanner** — обход CS2DT market → upsert items → Redis queue с приоритетами
- [ ] **Worker: Updater** — Redis ZPOPMAX → Steam priceoverview/pricehistory → DB update
- [ ] **Redis интеграция**: aioredis client, все ключи из DB.md раздел 7
- [ ] **Кэш Steam цен**: Redis Hash с TTL 30 мин
- [ ] **Freshness tracking**: Redis Hash `steam:freshness`
- [ ] **Candidates**: Redis Set `arb:candidates` — обновляется Updater
- [ ] **Дашборд user**: `/items` — каталог предметов, `/candidates` — кандидаты
- [ ] **Метрики**: кол-во предметов, размер очереди, скорость обновления

### Definition of Done
```
✅ Scanner обходит весь рынок Dota 2 за < 5 мин
✅ Updater обновляет ~1200 предметов/час (Steam rate limit)
✅ Redis очередь работает: ZADD → ZPOPMAX
✅ arb:candidates содержит актуальных кандидатов
✅ /items и /candidates отображают данные
✅ Dry-run цикл Scanner+Updater работает без ошибок
```

### Ответственные агенты
- **Backend Dev**: воркеры Scanner, Updater
- **DBA**: Redis ключи, индексы
- **Tester**: интеграционные тесты с mock Steam API

---

## Фаза 3 — Покупка 💰

> **Цель**: автоматическая покупка выгодных предметов через CS2DT.
> **Приоритет**: КРИТИЧЕСКИЙ — основная бизнес-ценность.

### Задачи

- [ ] **Worker: Buyer** — анализ кандидатов → safety checks → CS2DT buy/quick-buy
- [ ] **Worker: OrderTracker** — отслеживание доставки (instant + P2P 12ч)
- [ ] **Safety Checks**: все 4 уровня из SPEC.md раздел 5.4
- [ ] **Идемпотентность**: Redis SISMEMBER перед покупкой
- [ ] **Dry-run режим**: полный цикл без реальных покупок
- [ ] **Strategy: Discount** — реализация из SPEC.md раздел 5.2
- [ ] **Логирование покупок**: PostgreSQL + Redis счётчики
- [ ] **Дашборд**: `/purchases` — таблица покупок

### Definition of Done
```
✅ Dry-run цикл: Scanner → Updater → Buyer находит и "покупает" предметы
✅ Safety checks блокируют: превышение лимита, дубль, низкий баланс
✅ OrderTracker отслеживает заказы, автоотмена зависших (>12ч P2P)
✅ Идемпотентность: повторная покупка того же product_id невозможна
✅ /purchases показывает историю (включая dry_run)
✅ Unit тесты для Buyer, Safety, Strategy
```

### Ответственные агенты
- **Backend Dev**: Buyer, OrderTracker
- **Security**: Safety checks, идемпотентность, kill switch
- **Tester**: все edge cases покупки

---

## Фаза 4 — Telegram + CLI 📱

> **Цель**: полное управление ботом через Telegram и командную строку.

### Задачи

- [ ] **Telegram бот**: aiogram, 25 команд из SPEC.md раздел 6
- [ ] **Semi-auto режим**: inline кнопки ✅/❌/🚫
- [ ] **Уведомления**: покупка, ошибка, баланс, лимит, API down
- [ ] **Ежедневный отчёт**: текстовое сообщение в Telegram в 09:00 UTC+3
- [ ] **CLI**: `typer` — `python -m fluxio scan`, `enrich`, `dry-run`, `status`
- [ ] **Очистить scripts/**: удалить дубли, перенести полезное в CLI

### Definition of Done
```
✅ Все 25 команд Telegram работают
✅ Semi-auto: бот предлагает сделку → пользователь подтверждает/отклоняет
✅ Уведомления приходят при каждом событии
✅ CLI команды работают: fluxio scan, fluxio status, fluxio dry-run
✅ scripts/ содержит только уникальные утилиты (< 5 файлов)
```

### Ответственные агенты
- **Backend Dev**: Telegram бот, CLI
- **DevOps**: интеграция в Docker

---

## Фаза 5 — Дашборд полный + Экспорт 📊

> **Цель**: полноценный веб-интерфейс для мониторинга и управления.

### Задачи

- [ ] **Дашборд user**: главная (баланс, P&L), статистика (графики Chart.js), экспорт
- [ ] **Дашборд admin**: конфиг управление, blacklist, API health, circuit breaker state
- [ ] **P&L трекер**: расчёт чистой прибыли с учётом комиссий
- [ ] **CSV/Excel экспорт**: openpyxl для Excel, csv stdlib
- [ ] **Авторизация**: middleware заглушка (basic auth как минимум)
- [ ] **CSRF защита**: FastAPI middleware
- [ ] **SSE**: realtime обновления покупок и статуса воркеров

### Definition of Done
```
✅ Все страницы из SPEC.md раздел 7 работают
✅ Графики P&L отображаются
✅ Экспорт CSV/Excel скачивается
✅ Нет XSS/CSRF уязвимостей
✅ Basic auth на /admin/
```

### Ответственные агенты
- **Backend Dev**: routes, templates, export
- **Security**: auth, CSRF, XSS аудит
- **DevOps**: static files, Docker

---

## Фаза 6 — Качество + CI/CD ✅

> **Цель**: production-ready код с тестами и автоматическим деплоем.

### Задачи

- [ ] **Unit тесты**: analyzer, safety, buyer, strategies — покрытие 70%+
- [ ] **Integration тесты**: API клиенты с aioresponses mock
- [ ] **E2E тест**: dry-run полного цикла (scan → analyze → buy)
- [ ] **GitHub Actions**: lint (ruff), test (pytest), build (Docker)
- [ ] **Docker production**: multi-stage build, non-root user, health check
- [ ] **Деплой**: docker-compose.prod.yml, автодеплой по merge в main
- [ ] **pyproject.toml**: dependencies, scripts, ruff config

### Definition of Done
```
✅ pytest --cov показывает 70%+ покрытие
✅ GitHub Actions: green на каждый push
✅ Docker image < 200MB, запускается от non-root
✅ Деплой на VPS по merge в main
✅ ruff lint без ошибок
```

### Ответственные агенты
- **Tester**: тесты, покрытие
- **DevOps**: CI/CD, Docker, деплой
- **Security**: финальный аудит

---

## Фаза 7 — Расширение 🚀

> **Цель**: новые игры, площадки, стратегии.

### Задачи

- [ ] **CS2 поддержка**: appId=730, отдельный конфиг в `games[]`
- [ ] **C5Game web scraping**: HTTP-перехват, session cookies, cert pinning
- [ ] **Strategy: CrossPlatform** — сравнение цен между площадками
- [ ] **Мультиаккаунт**: AccountManager, per-account config
- [ ] **Steam Web API**: инвентарь (GetPlayerItems), история трейдов (GetTradeOffers)
- [ ] **BUFF163**: PriceProvider реализация (при наличии API)

### Definition of Done
```
✅ CS2 предметы сканируются и покупаются
✅ C5Game web client проходит через cert pinning
✅ Мультиаккаунт: два CS2DT аккаунта работают параллельно
✅ Инвентарь Steam отображается в дашборде
```

### Ответственные агенты
- **Architect**: мультиаккаунт архитектура
- **Backend Dev**: новые клиенты и стратегии
- **Security**: cert pinning, cookie management

---

## ДОРОЖНАЯ КАРТА

```
Фаза 0 ──► Фаза 1 ──► Фаза 2 ──► Фаза 3 ──► Фаза 4 ──► Фаза 5 ──► Фаза 6 ──► Фаза 7
стабил.    архитект.   монитор.   покупка    telegram    дашборд    качество   расширен.
  │          │           │          │          │           │          │          │
  ▼          ▼           ▼          ▼          ▼           ▼          ▼          ▼
баги →    Protocol →  Scanner → Buyer →   25 команд → P&L →     70% тесты → CS2
git       DI         Updater   Safety    CLI        графики    CI/CD       C5 web
alembic   дашборд    Redis     dry-run   отчёты     экспорт    Docker      мульти
          admin      кэш       orders    semi-auto  auth       деплой      BUFF
```

---

*Документ: PHASES.md v2.0*
*Проект: Fluxio*
*Дата: 2026-03-11*
