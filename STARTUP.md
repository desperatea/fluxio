# Запуск Fluxio

## Требования

- Docker + Docker Compose
- Node.js 18+ (для MCP серверов, опционально)
- `.env` файл в корне проекта (рядом с `docker-compose.yml`)

## Быстрый старт

```bash
cd /home/user/ruslan/fluxio/fluxio

# 1. Запуск всех контейнеров
docker compose up -d

# 2. Проверка статуса
docker compose ps

# 3. Проверка дашборда
curl http://localhost:8080/health
```

Дашборд: **http://localhost:8080**

## Контейнеры

| Контейнер | Назначение | Порт |
|-----------|-----------|------|
| fluxio-bot | Бот + дашборд | 8080 |
| fluxio-postgres | База данных | 5433 |
| fluxio-redis | Кэш + очереди | 6379 |
| fluxio-pgbackup | Бэкап БД (раз в сутки) | - |

## Полезные команды

```bash
# Логи бота (в реальном времени)
docker compose logs -f bot

# Логи всех контейнеров
docker compose logs -f

# Перезапуск бота
docker compose restart bot

# Пересборка после изменений в коде
docker compose build bot && docker compose up -d bot

# Остановка всех контейнеров
docker compose down

# Остановка с удалением данных (ОСТОРОЖНО)
docker compose down -v
```

## Запуск тестов

```bash
docker run --rm -v "$(pwd)":/app -w /app --network fluxio_botnet fluxio-bot python -m pytest tests/ -v
```

## Дашборд — страницы

- `/` — главная
- `/items` — каталог предметов
- `/candidates` — кандидаты на покупку
- `/purchases` — история покупок
- `/admin/` — статус воркеров, circuit breaker
- `/admin/logs` — логи в реальном времени (SSE)

## Режим работы

Бот работает в режиме **DRY RUN** (`dry_run: true` в `config.yaml`).
Это значит: весь конвейер работает по-настоящему (сканирование, проверка цен, анализ), но реальные покупки НЕ совершаются.

Для включения реальных покупок — поменять `dry_run: false` в `config.yaml` и перезапустить бота.

## Устранение проблем

**Бот падает в цикле (Restarting)**
```bash
docker compose logs bot --tail=30
```
Частая причина — Alembic не находит миграцию. Решение — пересобрать образ:
```bash
docker compose build bot && docker compose up -d bot
```

**Порт занят**
```bash
# Проверить, кто занимает порт
sudo lsof -i :8080
```

**Права на директории**
```bash
chmod 777 logs/ backups/ data/ config_history/
```
