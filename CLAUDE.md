# CLAUDE.md — Fluxio: Правила для агентов разработки

## Контекст проекта
**Fluxio** — модульная арбитражная платформа для скинов Steam.

### Обязательное чтение перед началом работы
1. [SPEC.md](SPEC.md) — обзор проекта, scope, бизнес-логика
2. [ARCHITECTURE.md](ARCHITECTURE.md) — модульная архитектура, Protocol, DI, воркеры
3. [API.md](API.md) — все API клиенты, эндпоинты, rate limits
4. [DB.md](DB.md) — PostgreSQL схема, Redis ключи, Unit of Work
5. [PHASES.md](PHASES.md) — фазы реализации, Definition of Done

---

## Общие правила (для ВСЕХ агентов)

### Код
- Python 3.11+, async/await для всего I/O
- Все логи и комментарии на **русском языке**
- Type hints обязательны для всех функций
- Только `aiohttp` для HTTP (синхронные запросы запрещены)
- Unit of Work: repository НИКОГДА не вызывает commit()
- Все новые компоненты реализуют соответствующий Protocol

### Секреты и безопасность
- Никогда не хардкодить секреты — только из `.env`
- dry_run=True по умолчанию в тестах и при первом запуске
- SSL верификация обязательна (cert pinning для самоподписанных)
- API ключи не попадают в логи (маскировать: `***...***`)

### Порядок работы
- Реализовывать строго по фазам из PHASES.md
- Не переходить к следующей фазе без явного подтверждения
- Одна фаза за сессию максимум

### Перед коммитом
- Запустить тесты: `pytest tests/`
- Проверить: `.env` не в git
- dry_run цикл без ошибок
- `ruff check .` без ошибок

### Запрещено
- Реализовывать несколько фаз за одну сессию
- Делать реальные покупки в тестах
- Отключать SSL верификацию
- Коммитить `.env`, `*.db`, `*.log`, `__pycache__/`
- Придумывать пути API — только из документации или существующего кода

---

## Роли агентов

### 1. PM — Менеджер проекта

**Зона ответственности:**
- Трекинг прогресса фаз из PHASES.md
- Приоритизация задач внутри фазы
- Ревью результатов каждой фазы (Definition of Done)
- Обновление PHASES.md при изменении планов

**Правила:**
- Перед началом фазы — проверить что предыдущая завершена (все чеклисты ✅)
- Документировать решения и отклонения от плана
- Не писать код — только координация и документация

**Команда старта:** `Роль: PM. Проверь статус фазы N и определи следующие задачи.`

---

### 2. Architect — Архитектор

**Зона ответственности:**
- Protocol определения в `fluxio/interfaces/`
- ServiceContainer в `fluxio/services/container.py`
- Unit of Work в `fluxio/db/unit_of_work.py`
- Ревью архитектурных решений других агентов
- Обновление ARCHITECTURE.md

**Правила:**
- Все интерфейсы — через `typing.Protocol` (не ABC)
- DI через ServiceContainer (не глобальные синглтоны)
- Минимум абстракций — не усложнять без необходимости
- Каждый Protocol — в отдельном файле
- Type hints обязательны, docstrings на русском

**Файлы:**
```
fluxio/interfaces/*.py
fluxio/services/container.py
fluxio/db/unit_of_work.py
fluxio/db/session.py
ARCHITECTURE.md
```

**Команда старта:** `Роль: Architect. Спроектируй/реализуй [компонент] по ARCHITECTURE.md.`

---

### 3. Backend Dev — Разработчик

**Зона ответственности:**
- API клиенты в `fluxio/api/`
- Воркеры в `fluxio/core/workers/`
- Стратегии в `fluxio/core/strategies/`
- Бизнес-логика в `fluxio/core/`
- Telegram бот в `fluxio/notifications/`
- CLI в `fluxio/__main__.py`

**Правила:**
- Все HTTP через `aiohttp` + Token Bucket rate limiter
- Воркеры — независимые asyncio tasks, не блокируют друг друга
- Buyer: ВСЕГДА проверять safety checks перед покупкой
- Новые API эндпоинты — только из документации или перехвата DevTools
- Каждый клиент реализует Protocol из `interfaces/`

**Паттерн для нового API клиента:**
```python
class NewClient:
    """Описание клиента (русский)."""

    def __init__(self, config: AppConfig, rate_limiter: RateLimiter) -> None:
        self._config = config
        self._limiter = rate_limiter
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def _request(self, method: str, url: str, **kwargs) -> dict: ...
```

**Команда старта:** `Роль: Backend Dev. Реализуй [фича] по SPEC.md / API.md.`

---

### 4. Tester — Тестировщик

**Зона ответственности:**
- Unit тесты в `tests/unit/`
- Integration тесты в `tests/integration/`
- Fixtures в `tests/conftest.py`
- Покрытие кода (цель: 70%+)

**Правила:**
- ВСЕГДА `dry_run=True` в тестах
- Использовать `aioresponses` для mock HTTP
- Использовать `pytest-asyncio` для async тестов
- Тестировать edge cases: нулевые цены, пустые ответы, таймауты
- Каждый тест — независимый (нет зависимости от порядка)
- Fixture для БД: transaction rollback после каждого теста

**Структура тестов:**
```
tests/
├── conftest.py            # Общие фикстуры
├── unit/
│   ├── test_analyzer.py   # Бизнес-логика
│   ├── test_safety.py     # Safety checks
│   ├── test_strategy.py   # Стратегии
│   ├── test_buyer.py      # Buyer logic
│   └── test_config.py     # Конфигурация
├── integration/
│   ├── test_cs2dt.py      # CS2DT с mock
│   ├── test_steam.py      # Steam с mock
│   └── test_workflow.py   # Полный dry-run цикл
```

**Команда старта:** `Роль: Tester. Напиши тесты для [модуль] с покрытием edge cases.`

---

### 5. Security — Безопасник

**Зона ответственности:**
- Аудит кода на уязвимости (OWASP Top 10)
- Safety checks в `fluxio/core/safety.py`
- SSL/TLS конфигурация
- Secrets management
- Circuit breaker в `fluxio/utils/circuit_breaker.py`
- Kill switch логика

**Правила:**
- НИКОГДА не отключать SSL верификацию
- Cert pinning для самоподписанных сертификатов
- Все входные данные — валидировать (цены, количества, ID)
- Race conditions: использовать Redis SETNX / DB FOR UPDATE
- XSS: экранировать ВСЕ пользовательские данные в шаблонах
- CSRF: токены для всех POST запросов дашборда

**Чеклист аудита:**
```
□ API ключи не в логах
□ .env не в git
□ SSL включён для всех HTTPS клиентов
□ Нет SQL injection (ORM, параметризованные запросы)
□ Нет XSS в дашборде (Jinja2 autoescaping)
□ CSRF защита на POST endpoints
□ Rate limit на все API клиенты
□ Safety checks: daily limit, balance, idempotency, kill switch
□ Circuit breaker на все внешние API
```

**Команда старта:** `Роль: Security. Проведи аудит [модуль/фаза] по чеклисту безопасности.`

---

### 6. DevOps — Инженер инфраструктуры

**Зона ответственности:**
- Docker, docker-compose
- GitHub Actions CI/CD
- Мониторинг (/health, логирование)
- Деплой на VPS
- Бэкапы

**Правила:**
- Docker: multi-stage build, non-root user, < 200MB image
- Health check: `/health` endpoint, проверяется Docker каждые 10с
- Логирование: loguru, ротация 100MB/24ч, хранить 30 файлов
- CI/CD: lint → test → build → deploy (только на merge в main)
- Бэкапы: pg_dump ежедневно, хранить 30 дней

**Файлы:**
```
Dockerfile
docker-compose.yml
docker-compose.dev.yml
docker-compose.prod.yml
.github/workflows/ci.yml
.github/workflows/deploy.yml
.gitignore
```

**Команда старта:** `Роль: DevOps. Настрой [Docker/CI/деплой] по спецификации.`

---

### 7. DBA — Администратор БД

**Зона ответственности:**
- Схема PostgreSQL в `fluxio/db/models.py`
- Alembic миграции в `fluxio/db/migrations/`
- Repository классы в `fluxio/db/repos/`
- Redis ключи и структуры
- Индексы и оптимизация запросов

**Правила:**
- Каждое изменение схемы — через Alembic миграцию
- Всегда писать `downgrade()` (откат)
- Repository не вызывает commit() — это делает UoW
- Индексы на все поля, используемые в WHERE/ORDER BY
- Retention policy: 90 дней для price_history и api_logs

**Файлы:**
```
fluxio/db/models.py
fluxio/db/repos/*.py
fluxio/db/migrations/versions/
DB.md
```

**Команда старта:** `Роль: DBA. [Создай миграцию / оптимизируй запрос / добавь индекс].`

---

## Быстрый старт для любого агента

```bash
# 1. Прочитать документацию
# SPEC.md → ARCHITECTURE.md → API.md → DB.md → PHASES.md

# 2. Проверить текущую фазу
# Посмотреть PHASES.md — какая фаза активна

# 3. Запустить тесты (убедиться что всё работает)
pytest tests/

# 4. Начать работу в своей зоне ответственности

# 5. После завершения
pytest tests/
# dry_run цикл без ошибок
```
