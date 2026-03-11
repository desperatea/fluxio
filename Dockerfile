FROM python:3.11-slim AS base

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Непривилегированный пользователь
RUN groupadd --gid 1000 botuser && \
    useradd --uid 1000 --gid botuser --create-home botuser

WORKDIR /app

# Зависимости Python (кэшируемый слой)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Исходный код
COPY fluxio/ fluxio/
COPY alembic.ini .
COPY config.yaml .

# Директории для данных
RUN mkdir -p logs backups config_history data && \
    chown -R botuser:botuser /app

USER botuser

# Healthcheck — проверяем что процесс Python жив
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD pgrep -f "python -m fluxio.main" || exit 1

# Запуск: миграции + бот
CMD ["sh", "-c", "alembic upgrade head 2>/dev/null; python -m fluxio.main"]
