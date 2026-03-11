# ARCHITECTURE.md — Архитектура Fluxio v2.0

> Подробная архитектурная документация арбитражной платформы Fluxio.
> Дата: 2026-03-11. Версия: 2.0

---

## 1. ОБЗОР АРХИТЕКТУРЫ

### 1.1 Паттерн: Worker-based монолит

Fluxio — единый Python-процесс с несколькими независимыми asyncio-воркерами.
Каждый воркер — бесконечный цикл (`while True`) с собственным интервалом.
Координация через Redis (очереди, кэш, pub/sub), данные в PostgreSQL.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Fluxio Process                           │
│                                                                 │
│  ┌───────────┐  ┌───────────┐  ┌──────────┐  ┌──────────────┐  │
│  │  Scanner   │  │  Updater  │  │  Buyer   │  │ OrderTracker │  │
│  │ (15-30мин) │  │  (24/7)   │  │ (1-5мин) │  │   (5мин)     │  │
│  └─────┬──────┘  └─────┬─────┘  └────┬─────┘  └──────┬───────┘  │
│        │               │             │                │          │
│  ┌─────▼───────────────▼─────────────▼────────────────▼──────┐  │
│  │                 ServiceContainer (DI)                      │  │
│  │                                                            │  │
│  │  MarketClient │ PriceProvider │ Strategy │ Notifier │ UoW  │  │
│  └───────────────────────┬────────────────────────────────────┘  │
│                          │                                       │
│  ┌──────────┐    ┌───────▼──────┐    ┌─────────────┐            │
│  │PostgreSQL│    │    Redis     │    │  Dashboard   │            │
│  │ (данные) │    │(очереди,кэш) │    │(FastAPI+SSE) │            │
│  └──────────┘    └──────────────┘    └─────────────┘            │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 Почему монолит, а не микросервисы

| Критерий | Монолит | Микросервисы |
|----------|---------|--------------|
| Латентность | Вызов функции (нс) | HTTP/gRPC (мс) |
| Сложность деплоя | 1 процесс | Оркестрация |
| Общий стейт | Прямой доступ | Сериализация |
| Отладка | Один стектрейс | Distributed tracing |
| Масштаб Fluxio | 4 воркера, 1 разработчик | Избыточно |

Монолит — правильный выбор для текущего масштаба. При росте до 10+ воркеров
или 3+ разработчиков — выделение воркеров в отдельные процессы через Redis Streams.

### 1.3 Ключевые принципы

1. **Dependency Inversion** — воркеры зависят от Protocol, не от реализации
2. **Unit of Work** — repository не делает commit, транзакция управляется UoW
3. **Fail-safe** — 4 уровня защиты от потери средств
4. **Hot reload** — бизнес-параметры меняются без рестарта
5. **Idempotency** — повторный запуск не создаёт дублей покупок

---

## 2. PROTOCOL ИНТЕРФЕЙСЫ

Все абстракции определены через `typing.Protocol` — структурная типизация.
Классу не нужно явно наследовать Protocol — достаточно реализовать методы.

### 2.1 MarketClient — торговая площадка

```python
# fluxio/interfaces/market_client.py

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from fluxio.interfaces.models import BuyResult, MarketItem


@runtime_checkable
class MarketClient(Protocol):
    """Протокол для любой торговой площадки (CS2DT, C5Game, BUFF и др.).

    Каждая площадка реализует этот интерфейс.
    Подключение новой площадки = новый файл с классом,
    реализующим эти методы.
    """

    @property
    def platform_name(self) -> str:
        """Уникальное имя площадки ('cs2dt', 'c5game', 'buff')."""
        ...

    async def search_market(
        self,
        app_id: int,
        page: int = 1,
        page_size: int = 50,
    ) -> list[MarketItem]:
        """Поиск предметов на рынке.

        Args:
            app_id: Steam appId (570=Dota2, 730=CS2, 252490=Rust).
            page: Номер страницы (1-based).
            page_size: Размер страницы (макс 50).

        Returns:
            Список предметов с текущими ценами на площадке.
        """
        ...

    async def get_item_listings(
        self,
        item_id: str,
        page: int = 1,
    ) -> list[MarketItem]:
        """Получить конкретные листинги предмета (ордера на продажу).

        Args:
            item_id: ID предмета на площадке.
            page: Номер страницы.

        Returns:
            Список конкретных ордеров с ценами и productId.
        """
        ...

    async def get_balance(self) -> Decimal:
        """Текущий баланс аккаунта в валюте площадки.

        Returns:
            Баланс (USD для CS2DT, CNY для C5Game).
        """
        ...

    async def buy(
        self,
        product_id: str,
        price: Decimal,
        *,
        dry_run: bool = True,
    ) -> BuyResult:
        """Купить конкретный листинг.

        Args:
            product_id: Уникальный ID листинга на площадке.
            price: Ожидаемая цена (проверка на стороне API).
            dry_run: True = симуляция (не тратить деньги).

        Returns:
            Результат покупки с order_id и статусом.
        """
        ...

    async def get_order_status(self, order_id: str) -> str:
        """Статус заказа: 'pending' | 'delivered' | 'cancelled' | 'expired'."""
        ...

    async def close(self) -> None:
        """Закрыть HTTP-сессию и освободить ресурсы."""
        ...
```

### 2.2 PriceProvider — источник эталонных цен

```python
# fluxio/interfaces/price_provider.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PriceData:
    """Эталонная цена предмета."""
    market_hash_name: str
    median_price_usd: float       # Медиана продаж (основной ориентир)
    lowest_price_usd: float | None  # Минимальная цена листинга
    volume_24h: int | None         # Объём продаж за 24ч
    buy_order_price_usd: float | None  # Лучший buy order
    sales_7d: int | None           # Продаж за 7 дней (ликвидность)
    updated_at: str                # ISO timestamp последнего обновления


class PriceProvider(Protocol):
    """Протокол для источника эталонных цен.

    Основная реализация — Steam Market.
    Альтернативы: BUFF163, DMarket (будущее).
    """

    async def get_price(
        self,
        market_hash_name: str,
        app_id: int = 570,
    ) -> PriceData | None:
        """Получить актуальную цену предмета.

        Returns:
            PriceData или None если предмет не найден.
        """
        ...

    async def get_prices_batch(
        self,
        names: list[str],
        app_id: int = 570,
    ) -> dict[str, PriceData]:
        """Пакетное получение цен (для оптимизации).

        Returns:
            Словарь {market_hash_name: PriceData}.
        """
        ...

    async def get_sales_history(
        self,
        market_hash_name: str,
        app_id: int = 570,
        days: int = 30,
    ) -> list[tuple[str, float, int]]:
        """История продаж: [(date, price, volume), ...].

        Нужна для анализа антиманипуляции и трендов.
        """
        ...
```

### 2.3 Strategy — стратегия арбитража

```python
# fluxio/interfaces/strategy.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from fluxio.interfaces.models import MarketItem
from fluxio.interfaces.price_provider import PriceData


@dataclass
class AnalysisResult:
    """Результат анализа предмета стратегией."""
    should_buy: bool
    reason: str
    item: MarketItem
    discount_percent: float = 0.0
    expected_profit_usd: float = 0.0
    confidence: float = 0.0        # 0.0–1.0, уверенность в сделке
    tags: list[str] = field(default_factory=list)  # ['quick_flip', 'high_volume']


class Strategy(Protocol):
    """Протокол арбитражной стратегии.

    Каждая стратегия реализует метод analyze(),
    который решает — покупать предмет или нет.
    """

    @property
    def name(self) -> str:
        """Уникальное имя стратегии для логирования."""
        ...

    def analyze(
        self,
        item: MarketItem,
        reference: PriceData,
    ) -> AnalysisResult:
        """Проанализировать предмет и принять решение.

        Args:
            item: Предмет с ценой на площадке.
            reference: Эталонная цена (Steam Market).

        Returns:
            AnalysisResult с решением и обоснованием.
        """
        ...
```

### 2.4 Notifier — уведомления

```python
# fluxio/interfaces/notifier.py

from __future__ import annotations

from enum import Enum
from typing import Protocol


class EventType(Enum):
    """Типы событий для уведомлений."""
    PURCHASE_SUCCESS = "purchase_success"
    PURCHASE_ERROR = "purchase_error"
    LOW_BALANCE = "low_balance"
    DAILY_LIMIT = "daily_limit_reached"
    API_DOWN = "api_unavailable"
    GOOD_DEAL = "good_deal_found"
    CIRCUIT_OPEN = "circuit_breaker_open"
    KILL_SWITCH = "kill_switch_triggered"
    DAILY_REPORT = "daily_report"


class Notifier(Protocol):
    """Протокол отправки уведомлений.

    Основная реализация — Telegram.
    Альтернативы: Discord, email, webhook.
    """

    async def notify(
        self,
        event: EventType,
        message: str,
        *,
        urgent: bool = False,
    ) -> bool:
        """Отправить уведомление.

        Args:
            event: Тип события.
            message: Текст уведомления.
            urgent: Срочное — игнорирует quiet_mode.

        Returns:
            True если доставлено успешно.
        """
        ...

    async def send_daily_report(self, report: dict) -> bool:
        """Отправить ежедневный отчёт."""
        ...
```

---

## 3. SERVICE CONTAINER (DI)

### 3.1 Концепция

Кастомный DI-контейнер без внешних библиотек. Управляет жизненным циклом
всех сервисов: создание, внедрение зависимостей, graceful shutdown.

### 3.2 Реализация

```python
# fluxio/services/container.py

from __future__ import annotations

import asyncio
from typing import Any, TypeVar

from loguru import logger

T = TypeVar("T")


class ServiceContainer:
    """Контейнер зависимостей с управлением жизненным циклом.

    Принципы:
    - Ленивая инициализация: сервис создаётся при первом запросе
    - Singleton по умолчанию: один экземпляр на весь процесс
    - Graceful shutdown: закрытие в обратном порядке регистрации
    - Type-safe: регистрация и получение по типу
    """

    def __init__(self) -> None:
        self._factories: dict[type, Any] = {}
        self._instances: dict[type, Any] = {}
        self._init_order: list[type] = []

    def register(self, interface: type[T], factory: Any) -> None:
        """Зарегистрировать фабрику для интерфейса.

        Args:
            interface: Тип (Protocol или класс), по которому
                       будет запрашиваться сервис.
            factory: Callable или корутина, создающая экземпляр.

        Пример:
            container.register(MarketClient, lambda: CS2DTClient(config))
        """
        self._factories[interface] = factory
        logger.debug(f"Зарегистрирован сервис: {interface.__name__}")

    async def get(self, interface: type[T]) -> T:
        """Получить экземпляр сервиса (singleton).

        Если экземпляр не создан — вызывает фабрику.
        Если фабрика — корутина, await'ит её.

        Raises:
            KeyError: Сервис не зарегистрирован.
        """
        if interface in self._instances:
            return self._instances[interface]

        if interface not in self._factories:
            raise KeyError(
                f"Сервис {interface.__name__} не зарегистрирован. "
                f"Доступные: {[t.__name__ for t in self._factories]}"
            )

        factory = self._factories[interface]
        instance = factory()

        # Поддержка async-фабрик
        if asyncio.iscoroutine(instance):
            instance = await instance

        self._instances[interface] = instance
        self._init_order.append(interface)
        logger.info(f"Создан сервис: {interface.__name__}")
        return instance

    async def shutdown(self) -> None:
        """Graceful shutdown: закрытие в обратном порядке.

        Вызывает close() у каждого сервиса, если метод существует.
        """
        for iface in reversed(self._init_order):
            instance = self._instances.get(iface)
            if instance and hasattr(instance, "close"):
                try:
                    result = instance.close()
                    if asyncio.iscoroutine(result):
                        await result
                    logger.info(f"Закрыт сервис: {iface.__name__}")
                except Exception as e:
                    logger.error(f"Ошибка при закрытии {iface.__name__}: {e}")

        self._instances.clear()
        self._init_order.clear()

    def is_registered(self, interface: type) -> bool:
        """Проверить, зарегистрирован ли сервис."""
        return interface in self._factories

    def is_initialized(self, interface: type) -> bool:
        """Проверить, создан ли экземпляр сервиса."""
        return interface in self._instances
```

### 3.3 Инициализация при старте

```python
# fluxio/main.py — фрагмент

async def create_container(config: AppConfig) -> ServiceContainer:
    """Собрать контейнер зависимостей."""
    container = ServiceContainer()

    # Клиенты API
    container.register(MarketClient, lambda: CS2DTClient(
        app_key=config.env.cs2dt_app_key,
        app_secret=config.env.cs2dt_app_secret,
    ))
    container.register(PriceProvider, lambda: SteamClient(
        login_secure=config.env.steam_login_secure,
        proxy=config.env.https_proxy,
    ))

    # Стратегии
    container.register(Strategy, lambda: DiscountStrategy(config.trading))

    # Уведомления
    container.register(Notifier, lambda: TelegramNotifier(
        token=config.env.telegram_bot_token,
        chat_id=config.env.telegram_chat_id,
    ))

    # Unit of Work
    container.register(UnitOfWork, lambda: SQLAlchemyUoW(
        session_factory=async_session_factory,
    ))

    return container
```

### 3.4 Как добавить новый сервис

1. Определить Protocol в `fluxio/interfaces/`
2. Написать реализацию
3. Зарегистрировать в `create_container()`
4. Запрашивать через `container.get(MyProtocol)`

```python
# Шаг 1: Protocol
class PriceCache(Protocol):
    async def get(self, key: str) -> float | None: ...
    async def set(self, key: str, value: float, ttl: int) -> None: ...

# Шаг 2: Реализация
class RedisPriceCache:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def get(self, key: str) -> float | None:
        val = await self._redis.get(f"price:{key}")
        return float(val) if val else None

    async def set(self, key: str, value: float, ttl: int) -> None:
        await self._redis.setex(f"price:{key}", ttl, str(value))

# Шаг 3: Регистрация
container.register(PriceCache, lambda: RedisPriceCache(redis_pool))
```

---

## 4. ВОРКЕРЫ

### 4.1 Общая структура воркера

Все воркеры следуют единому паттерну:

```python
class BaseWorker:
    """Базовый класс воркера с единым циклом."""

    def __init__(self, container: ServiceContainer, interval: int) -> None:
        self._container = container
        self._interval = interval
        self._running = False

    async def start(self) -> None:
        """Запуск бесконечного цикла."""
        self._running = True
        logger.info(f"Воркер {self.name} запущен, интервал: {self._interval}с")

        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"[{self.name}] Ошибка в цикле: {e}")
                await self._on_error(e)

            await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        """Graceful остановка."""
        self._running = False
        logger.info(f"Воркер {self.name} остановлен")

    async def _tick(self) -> None:
        """Один цикл работы. Переопределяется в наследниках."""
        raise NotImplementedError

    async def _on_error(self, error: Exception) -> None:
        """Обработка ошибок. По умолчанию — уведомление."""
        notifier = await self._container.get(Notifier)
        await notifier.notify(EventType.API_DOWN, str(error))
```

### 4.2 Scanner — сканирование площадок

**Задача**: обход рынка CS2DT/C5Game, помещение предметов в Redis-очередь
для обновления Steam-цен.

**Интервал**: 15–30 минут (настраивается в `config.yaml`).

```
                    ┌─────────────┐
                    │   Scanner   │
                    └──────┬──────┘
                           │
              ┌────────────▼────────────┐
              │ MarketClient.search()   │
              │ (CS2DT: /v2/product/    │
              │  v2/search?appId=730)   │
              └────────────┬────────────┘
                           │
                    ┌──────▼──────┐
                    │ Фильтрация  │
                    │ по цене и   │
                    │ категории   │
                    └──────┬──────┘
                           │
              ┌────────────▼────────────┐
              │  Расчёт приоритета      │
              │  priority = f(price,    │
              │    freshness, volume)   │
              └────────────┬────────────┘
                           │
              ┌────────────▼────────────┐
              │  Redis ZADD             │
              │  update_queue:{app_id}  │
              │  score = priority       │
              └─────────────────────────┘
```

**Логика приоритета**:
```python
def calculate_priority(item: MarketItem, last_updated: datetime | None) -> float:
    """Чем выше приоритет — тем раньше обновится Steam-цена.

    Факторы:
    - Давность обновления (чем старее — тем приоритетнее)
    - Потенциальный дисконт (чем больше — тем приоритетнее)
    - Объём продаж (ликвидные предметы важнее)
    """
    priority = 0.0

    # Давность: каждый час без обновления = +10 баллов
    if last_updated:
        hours_stale = (now() - last_updated).total_seconds() / 3600
        priority += hours_stale * 10
    else:
        priority += 100  # Никогда не обновлялся — максимум

    # Предварительный дисконт (грубая оценка по кэшу)
    if cached_steam_price:
        rough_discount = (cached_steam_price - item.price) / cached_steam_price
        priority += rough_discount * 50

    return priority
```

### 4.3 Updater — обновление Steam-цен

**Задача**: извлечение предметов из Redis-очереди, запрос актуальных
Steam-цен, сохранение в БД.

**Режим**: работает непрерывно (24/7), ограничен rate limiter (~20 RPM Steam).

```
              ┌─────────────┐
              │   Updater   │
              └──────┬──────┘
                     │
        ┌────────────▼────────────┐
        │  Redis ZPOPMIN          │
        │  update_queue:{app_id}  │
        │  (берём самый           │
        │   приоритетный)         │
        └────────────┬────────────┘
                     │
        ┌────────────▼────────────┐
        │  RateLimiter.acquire()  │
        │  (Token Bucket,         │
        │   rate=0.33 req/s)      │
        └────────────┬────────────┘
                     │
        ┌────────────▼────────────┐
        │  Steam API              │
        │  priceoverview +        │
        │  pricehistory           │
        └────────────┬────────────┘
                     │
        ┌────────────▼────────────┐
        │  UoW.commit()           │
        │  items.steam_price_usd  │
        │  items.volume_24h       │
        │  price_history INSERT   │
        └────────────┬────────────┘
                     │
        ┌────────────▼────────────┐
        │  Redis SET              │
        │  steam_price:{name}     │
        │  TTL = 30 мин           │
        └─────────────────────────┘
```

**Стратегия свежести данных**:

| Категория | Условие | Интервал обновления |
|-----------|---------|---------------------|
| Кандидат на покупку | `discount >= 10%` | 30 мин |
| Обычный предмет | `discount < 10%` | 2 часа |
| Низкий приоритет | `volume_24h < 5` | 6 часов |
| Полная история | Для всех | 24 часа |

### 4.4 Buyer — покупка предметов

**Задача**: проверка кандидатов, прогон через стратегию и safety checks,
выполнение покупки (или dry-run).

**Интервал**: 1–5 минут.

```
              ┌─────────────┐
              │    Buyer    │
              └──────┬──────┘
                     │
        ┌────────────▼────────────┐
        │  Запрос кандидатов      │
        │  из БД (items с         │
        │  актуальной steam_price │
        │  и discount >= MIN)     │
        └────────────┬────────────┘
                     │
        ┌────────────▼────────────┐
        │  Strategy.analyze()     │
        │  (DiscountStrategy или  │
        │   CrossPlatformStrategy)│
        └────────────┬────────────┘
                     │
               ┌─────▼─────┐
               │ should_buy │──── False ──→ Пропуск
               │     ?      │
               └─────┬──────┘
                     │ True
        ┌────────────▼────────────┐
        │  Safety Checks          │
        │  1. Идемпотентность     │
        │     (Redis + DB)        │
        │  2. Лимиты              │
        │     (дневной, штучный)  │
        │  3. Kill switch         │
        │     (покупок/час)       │
        │  4. Circuit breaker     │
        │     (API доступен?)     │
        └────────────┬────────────┘
                     │ Все прошли
        ┌────────────▼────────────┐
        │  dry_run?               │
        │  ├── True: лог + запись │
        │  └── False:             │
        │      MarketClient.buy() │
        └────────────┬────────────┘
                     │
        ┌────────────▼────────────┐
        │  UoW: записать покупку  │
        │  Redis: idempotency key │
        │  Notifier: Telegram     │
        │  OrderTracker: добавить │
        └─────────────────────────┘
```

### 4.5 OrderTracker — отслеживание доставки

**Задача**: мониторинг статуса заказов после покупки.
Два типа доставки на CS2DT:
- **Instant** (auto_deliver) — трейд-оффер за секунды
- **P2P** — продавец отправляет вручную, до 12 часов

**Интервал**: 5 минут.

```
              ┌──────────────┐
              │ OrderTracker │
              └──────┬───────┘
                     │
        ┌────────────▼────────────┐
        │  SELECT active_orders   │
        │  WHERE status IN        │
        │  ('pending','waiting')  │
        └────────────┬────────────┘
                     │
        ┌────────────▼────────────┐
        │  MarketClient           │
        │  .get_order_status()    │
        └────────────┬────────────┘
                     │
            ┌────────▼────────┐
            │    Статус?      │
            ├─ delivered ──→ Обновить БД, Telegram "Доставлен!"
            ├─ cancelled ──→ Обновить БД, Telegram "Отменён"
            ├─ expired ────→ Auto-cancel, вернуть средства
            └─ pending ────→ Проверить таймаут:
                               P2P > 12ч? → Auto-cancel
                               Иначе → ждать
```

---

## 5. UNIT OF WORK

### 5.1 Зачем

Repository отвечает за CRUD-запросы. Но **repository не делает commit**.
Это позволяет:
- Выполнять несколько операций в одной транзакции
- Откатывать всё при ошибке в любой из операций
- Тестировать без реальных записей (rollback вместо commit)

### 5.2 Реализация

```python
# fluxio/db/unit_of_work.py

from __future__ import annotations

from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fluxio.db.repos.item_repo import ItemRepository
from fluxio.db.repos.purchase_repo import PurchaseRepository
from fluxio.db.repos.price_repo import PriceRepository
from fluxio.db.repos.stats_repo import StatsRepository


class UnitOfWork:
    """Unit of Work — управление транзакциями.

    Все репозитории работают в рамках одной сессии.
    Commit/rollback — только через UoW, не через repository.

    Использование:
        async with uow:
            item = await uow.items.get_by_name("AK-47 | Redline")
            item.steam_price_usd = 12.50
            await uow.prices.add_history(item.name, 12.50)
            await uow.commit()
            # Если исключение — автоматический rollback
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        self.items = ItemRepository(self._session)
        self.purchases = PurchaseRepository(self._session)
        self.prices = PriceRepository(self._session)
        self.stats = StatsRepository(self._session)
        return self

    async def __aexit__(self, exc_type: type | None, *_: object) -> None:
        if exc_type:
            await self.rollback()
        await self._session.close()

    async def commit(self) -> None:
        """Зафиксировать все изменения."""
        await self._session.commit()

    async def rollback(self) -> None:
        """Откатить все изменения."""
        await self._session.rollback()
```

### 5.3 Пример использования в Buyer

```python
async def execute_purchase(
    self,
    candidate: MarketItem,
    analysis: AnalysisResult,
) -> None:
    """Выполнить покупку с транзакционной гарантией."""
    uow: UnitOfWork = await self._container.get(UnitOfWork)

    async with uow:
        # 1. Проверка идемпотентности (в рамках транзакции)
        if await uow.purchases.exists(candidate.product_id):
            logger.info(f"Предмет {candidate.product_id} уже куплен")
            return

        # 2. Покупка через API
        client: MarketClient = await self._container.get(MarketClient)
        result = await client.buy(
            product_id=candidate.product_id,
            price=candidate.price,
            dry_run=self._config.trading.dry_run,
        )

        # 3. Записать в БД (оба INSERT в одной транзакции)
        await uow.purchases.create(
            product_id=candidate.product_id,
            order_id=result.order_id,
            market_hash_name=candidate.market_hash_name,
            price=candidate.price,
            discount=analysis.discount_percent,
        )
        await uow.stats.increment_daily(
            spent=float(candidate.price),
        )

        # 4. Один commit на всё
        await uow.commit()
```

### 5.4 Repository — только запросы, без commit

```python
# fluxio/db/repos/item_repo.py

class ItemRepository:
    """Репозиторий предметов. НЕ делает commit — только через UoW."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_name(self, market_hash_name: str) -> Item | None:
        """Найти предмет по market_hash_name."""
        stmt = select(Item).where(Item.market_hash_name == market_hash_name)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_candidates(
        self,
        min_discount: float,
        max_price: float,
        freshness_minutes: int = 30,
    ) -> list[Item]:
        """Кандидаты на покупку: есть steam-цена, дисконт >= порога."""
        cutoff = datetime.utcnow() - timedelta(minutes=freshness_minutes)
        stmt = (
            select(Item)
            .where(
                Item.steam_price_usd.isnot(None),
                Item.price_usd <= max_price,
                Item.last_seen_at >= cutoff,
            )
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def upsert(self, **kwargs: object) -> Item:
        """Создать или обновить предмет (add → flush, без commit)."""
        existing = await self.get_by_name(kwargs["market_hash_name"])
        if existing:
            for key, value in kwargs.items():
                setattr(existing, key, value)
            await self._session.flush()
            return existing
        item = Item(**kwargs)
        self._session.add(item)
        await self._session.flush()
        return item
```

---

## 6. STRATEGY PATTERN

### 6.1 Базовая стратегия

```python
# fluxio/core/strategies/base.py

from abc import ABC, abstractmethod

from fluxio.interfaces.models import MarketItem
from fluxio.interfaces.price_provider import PriceData
from fluxio.interfaces.strategy import AnalysisResult


class BaseStrategy(ABC):
    """Базовый класс для всех стратегий."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Имя стратегии."""
        ...

    @abstractmethod
    def analyze(
        self,
        item: MarketItem,
        reference: PriceData,
    ) -> AnalysisResult:
        """Анализ предмета."""
        ...

    def _calc_discount(
        self,
        buy_price: float,
        reference_price: float,
        fee_percent: float = 13.0,
    ) -> float:
        """Расчёт дисконта с учётом комиссии Steam.

        Формула: discount = (steam_net - buy_price) / steam_net * 100
        steam_net = steam_price * (1 - fee / 100)
        """
        if reference_price <= 0:
            return 0.0
        net_price = reference_price * (1 - fee_percent / 100)
        return (net_price - buy_price) / net_price * 100
```

### 6.2 DiscountStrategy — дисконт к Steam

```python
# fluxio/core/strategies/discount.py

class DiscountStrategy(BaseStrategy):
    """Стратегия простого дисконта к медиане Steam Market.

    Покупаем если:
    1. Цена на площадке < steam_median * (1 - steam_fee) * (1 - min_discount)
    2. Ликвидность >= min_sales_7d
    3. Нет признаков манипуляции ценой
    """

    @property
    def name(self) -> str:
        return "discount"

    def __init__(self, trading_config: TradingConfig) -> None:
        self._min_discount = trading_config.min_discount_percent
        self._min_sales = trading_config.min_sales_volume_7d
        self._steam_fee = 13.0

    def analyze(
        self,
        item: MarketItem,
        reference: PriceData,
    ) -> AnalysisResult:
        # 1. Расчёт дисконта
        discount = self._calc_discount(
            buy_price=item.price_usd,
            reference_price=reference.median_price_usd,
            fee_percent=self._steam_fee,
        )

        if discount < self._min_discount:
            return AnalysisResult(
                should_buy=False,
                reason=f"дисконт {discount:.1f}% < {self._min_discount}%",
                item=item,
                discount_percent=discount,
            )

        # 2. Ликвидность
        sales = reference.sales_7d or 0
        if sales < self._min_sales:
            return AnalysisResult(
                should_buy=False,
                reason=f"ликвидность {sales} < {self._min_sales} продаж/7д",
                item=item,
            )

        # 3. Расчёт ожидаемой прибыли
        steam_net = reference.median_price_usd * (1 - self._steam_fee / 100)
        expected_profit = steam_net - item.price_usd

        return AnalysisResult(
            should_buy=True,
            reason=f"дисконт {discount:.1f}%, прибыль ~${expected_profit:.2f}",
            item=item,
            discount_percent=discount,
            expected_profit_usd=expected_profit,
            confidence=min(discount / 50, 1.0),  # 50%+ дисконт = максимум
            tags=["discount", f"vol_{sales}"],
        )
```

### 6.3 Как добавить новую стратегию

1. Создать файл в `fluxio/core/strategies/`
2. Наследовать `BaseStrategy`
3. Реализовать `name` и `analyze()`
4. Зарегистрировать в `ServiceContainer`

```python
# Пример: CrossPlatformStrategy — арбитраж между площадками

class CrossPlatformStrategy(BaseStrategy):
    """Покупка на CS2DT, если на BUFF163 дороже."""

    @property
    def name(self) -> str:
        return "cross_platform"

    def analyze(self, item: MarketItem, reference: PriceData) -> AnalysisResult:
        # reference здесь — цена с BUFF163 (другой PriceProvider)
        spread = reference.median_price_usd - item.price_usd
        spread_pct = spread / item.price_usd * 100

        if spread_pct < 10:
            return AnalysisResult(False, f"спред {spread_pct:.1f}% < 10%", item)

        return AnalysisResult(
            should_buy=True,
            reason=f"кросс-платформенный спред {spread_pct:.1f}%",
            item=item,
            expected_profit_usd=spread,
            tags=["cross_platform"],
        )
```

### 6.4 Композиция стратегий

```python
class CompositeStrategy(BaseStrategy):
    """Запускает несколько стратегий, выбирает лучшую."""

    def __init__(self, strategies: list[BaseStrategy]) -> None:
        self._strategies = strategies

    @property
    def name(self) -> str:
        return "composite"

    def analyze(self, item: MarketItem, reference: PriceData) -> AnalysisResult:
        results = [s.analyze(item, reference) for s in self._strategies]
        buy_results = [r for r in results if r.should_buy]

        if not buy_results:
            # Вернуть отказ с наименьшим дисконтом (ближайший к порогу)
            return max(results, key=lambda r: r.discount_percent)

        # Выбрать с максимальной уверенностью
        return max(buy_results, key=lambda r: r.confidence)
```

---

## 7. CIRCUIT BREAKER

### 7.1 Концепция

Circuit Breaker защищает от каскадных сбоев при недоступности внешних API.
Три состояния:

```
        ┌─────────────────────────────────────────────────┐
        │                                                 │
        │    ┌──────────┐  N ошибок   ┌──────────┐       │
        │    │  CLOSED  │────────────→│   OPEN   │       │
        │    │(работаем)│             │ (пауза)  │       │
        │    └────┬─────┘             └────┬─────┘       │
        │         │                        │              │
        │         │ Успех              Таймаут            │
        │         │                    истёк              │
        │    ┌────▼──────────────────────▼─────┐         │
        │    │          HALF-OPEN              │         │
        │    │    (пробный запрос)             │         │
        │    └─────────────┬──────────────────┘         │
        │           ┌──────▼──────┐                     │
        │           │  Результат  │                     │
        │           ├─ Успех ──→ CLOSED                 │
        │           └─ Ошибка ─→ OPEN                   │
        └─────────────────────────────────────────────────┘
```

### 7.2 Реализация

```python
# fluxio/utils/circuit_breaker.py

from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Any, Callable, Coroutine

from loguru import logger


class CircuitState(Enum):
    CLOSED = "closed"        # Нормальная работа
    OPEN = "open"            # Запросы блокируются
    HALF_OPEN = "half_open"  # Пробный запрос


class CircuitBreakerOpen(Exception):
    """API недоступен — circuit breaker открыт."""
    pass


class CircuitBreaker:
    """Circuit Breaker с автоматическим health check.

    Args:
        name: Имя (для логирования).
        failure_threshold: Количество ошибок подряд для открытия.
        recovery_timeout: Секунды паузы перед пробным запросом.
        half_open_max_calls: Макс. пробных запросов в HALF_OPEN.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 300,
        half_open_max_calls: int = 1,
    ) -> None:
        self.name = name
        self._threshold = failure_threshold
        self._timeout = recovery_timeout
        self._half_open_max = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._half_open_calls = 0

    @property
    def state(self) -> CircuitState:
        """Текущее состояние с автоматическим переходом OPEN → HALF_OPEN."""
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time >= self._timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 0
                logger.info(
                    f"[CB:{self.name}] OPEN → HALF_OPEN "
                    f"(таймаут {self._timeout}с истёк)"
                )
        return self._state

    def record_success(self) -> None:
        """Записать успешный вызов."""
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            logger.info(f"[CB:{self.name}] HALF_OPEN → CLOSED (восстановлен)")
        elif self._state == CircuitState.CLOSED:
            self._failure_count = 0

    def record_failure(self) -> None:
        """Записать неудачный вызов."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            logger.warning(f"[CB:{self.name}] HALF_OPEN → OPEN (пробный запрос провалился)")
        elif self._failure_count >= self._threshold:
            self._state = CircuitState.OPEN
            logger.error(
                f"[CB:{self.name}] CLOSED → OPEN "
                f"({self._failure_count} ошибок подряд)"
            )

    def ensure_available(self) -> None:
        """Проверить доступность. Raises CircuitBreakerOpen."""
        current = self.state  # Триггерит автопереход
        if current == CircuitState.OPEN:
            remaining = self._timeout - (time.monotonic() - self._last_failure_time)
            raise CircuitBreakerOpen(
                f"[CB:{self.name}] API недоступен, "
                f"восстановление через {remaining:.0f}с"
            )
        if current == CircuitState.HALF_OPEN:
            if self._half_open_calls >= self._half_open_max:
                raise CircuitBreakerOpen(
                    f"[CB:{self.name}] HALF_OPEN: "
                    f"лимит пробных запросов исчерпан"
                )
            self._half_open_calls += 1
```

### 7.3 Использование в клиентах API

```python
class CS2DTClient:
    def __init__(self, app_key: str, app_secret: str) -> None:
        self._cb = CircuitBreaker(
            name="cs2dt",
            failure_threshold=5,
            recovery_timeout=300,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        self._cb.ensure_available()  # Проверяем CB перед запросом

        try:
            result = await self._do_request(method, path, **kwargs)
            self._cb.record_success()
            return result
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self._cb.record_failure()
            raise
```

---

## 8. RATE LIMITING

### 8.1 Token Bucket

Уже реализован в `bot/utils/rate_limiter.py`. Алгоритм:
- Корзина с `burst` токенами, пополняется со скоростью `rate` токенов/сек
- Каждый запрос забирает 1 токен
- Если токенов нет — `await asyncio.sleep()` до пополнения

```
    Время ──────────────────────────────────────→

    Токены: ████████  (burst=8)
            ███████   (запрос → -1)
            ██████    (запрос → -1)
            ███████   (пополнение +1)
            ████████  (пополнение +1, cap=burst)
            █         (5 запросов подряд)
            ░░░░░░░░  (пусто → ожидание 3с)
            ███       (пополнение за 3с)
```

### 8.2 Конфигурация по клиентам

| Клиент | Rate (req/s) | Burst | Обоснование |
|--------|-------------|-------|-------------|
| Steam priceoverview | 0.33 | 3 | Rate limit ~20 RPM |
| Steam pricehistory | 0.33 | 2 | Тот же лимит, но строже |
| CS2DT | 5.0 | 10 | API без жёстких ограничений |
| C5Game | 2.0 | 5 | Эмпирически подобрано |
| Telegram | 1.0 | 3 | 30 msg/sec глобально |

### 8.3 Инициализация

```python
# Лимитеры создаются в клиентах API:
class SteamClient:
    def __init__(self, ...) -> None:
        self._overview_limiter = TokenBucketRateLimiter(
            rate=0.33, burst=3, name="steam_overview"
        )
        self._history_limiter = TokenBucketRateLimiter(
            rate=0.33, burst=2, name="steam_history"
        )

    async def get_price_overview(self, name: str) -> dict:
        async with self._overview_limiter:
            return await self._request(...)
```

---

## 9. REDIS — ПОЛНАЯ СХЕМА ИСПОЛЬЗОВАНИЯ

### 9.1 Ключи и структуры данных

```
Redis Key                              Тип       TTL       Назначение
──────────────────────────────────────────────────────────────────────
update_queue:{app_id}                  ZSET      —         Очередь обновления Steam-цен
                                                           score=priority, member=hash_name

steam_price:{hash_name}               STRING    30мин     Кэш Steam-цены
                                                           value=JSON{median,lowest,volume}

idempotency:buy:{product_id}          STRING    24ч       Защита от двойной покупки
                                                           value=order_id

daily_spent:{date}                     STRING    48ч       Сумма трат за день (USD)
                                                           value=float

hourly_purchases:{hour}                STRING    2ч        Счётчик покупок в час
                                                           value=int (kill switch)

cb_state:{api_name}                    HASH      —         Состояние circuit breaker
                                                           {state, failures, last_fail}

scanner_lock:{app_id}                  STRING    30мин     Distributed lock для сканера
                                                           value=worker_id

config_version                         STRING    —         Текущая версия конфига
                                                           value=hash(config.yaml)
```

### 9.2 Pub/Sub каналы

```
Канал                         Публикует        Подписчик       Содержание
──────────────────────────────────────────────────────────────────────────
fluxio:purchases              Buyer            Dashboard       Новая покупка
fluxio:alerts                 Safety           Telegram        Алерт (kill switch и т.д.)
fluxio:config_reload          Config           Все воркеры     Конфиг обновлён
fluxio:scanner_done           Scanner          Updater         Скан завершён, очередь готова
```

---

## 10. ПОТОК ДАННЫХ

### 10.1 Полный цикл арбитража (ASCII-диаграмма)

```
  ┌─────────┐        ┌───────────────┐        ┌──────────────┐
  │  CS2DT  │        │  Steam Market │        │   Telegram   │
  │  API    │        │     API       │        │    Bot API   │
  └────┬────┘        └───────┬───────┘        └──────▲───────┘
       │                     │                       │
       │ search/listings     │ priceoverview         │ notify
       │                     │ pricehistory          │
  ═════▼═════════════════════▼═══════════════════════╪═══════════
  │                    Fluxio Process                │          │
  │                                                  │          │
  │  ┌──────────┐     1. Сканирование                │          │
  │  │ Scanner  │────────────────────────────────┐   │          │
  │  └──────────┘                                │   │          │
  │       │ search_market()                      │   │          │
  │       │                                      │   │          │
  │       ▼                                      │   │          │
  │  ┌─────────────┐  2. Очередь на обновление   │   │          │
  │  │ Redis ZADD  │  update_queue:570            │   │          │
  │  │ (приоритет) │                              │   │          │
  │  └──────┬──────┘                              │   │          │
  │         │ ZPOPMIN                             │   │          │
  │         ▼                                     │   │          │
  │  ┌──────────┐     3. Обновление цен           │   │          │
  │  │ Updater  │─── get_price_overview() ───────────→│          │
  │  └──────┬───┘                                 │   │          │
  │         │                                     │   │          │
  │         ▼                                     │   │          │
  │  ┌─────────────┐  4. Сохранение               │   │          │
  │  │ PostgreSQL  │  items + price_history        │   │          │
  │  │ (UoW)       │                               │   │          │
  │  └──────┬──────┘                               │   │          │
  │         │ Кандидаты с discount >= 15%           │   │          │
  │         ▼                                      │   │          │
  │  ┌──────────┐     5. Анализ + покупка          │   │          │
  │  │  Buyer   │─── Strategy.analyze() ──┐        │   │          │
  │  └──────┬───┘                         │        │   │          │
  │         │                    ┌────────▼──────┐ │   │          │
  │         │                    │ Safety Checks │ │   │          │
  │         │                    │ (4 уровня)    │ │   │          │
  │         │                    └────────┬──────┘ │   │          │
  │         │                             │ OK     │   │          │
  │         ├── buy() ────────────────────┼────────────→│          │
  │         │                             │        │   │          │
  │         ▼                             │        │   │          │
  │  ┌──────────────┐  6. Отслеживание    │        │   │          │
  │  │ OrderTracker │─── get_order_status()│       │   │          │
  │  └──────────────┘                     │        │   │          │
  │                                       │        │   │          │
  ════════════════════════════════════════════════════════════════
```

### 10.2 Последовательность для одного предмета

```
Время ──────────────────────────────────────────────────────────→

T+0мин    Scanner: CS2DT search → находит "AK-47 | Redline" за $3.50
          Redis ZADD update_queue:730 priority=85 "AK-47 | Redline"

T+2мин    Updater: ZPOPMIN → "AK-47 | Redline"
          Steam API: median=$6.20, volume_24h=45, sales_7d=312
          PostgreSQL: UPDATE items SET steam_price_usd=6.20
          Redis SET steam_price:"AK-47 | Redline" TTL=30min

T+3мин    Buyer: SELECT candidates WHERE discount >= 15%
          DiscountStrategy.analyze():
            net_steam = 6.20 * 0.87 = $5.39
            discount  = (5.39 - 3.50) / 5.39 = 35.1% ✓
            sales_7d  = 312 ≥ 10 ✓
          Safety: idempotency ✓, limits ✓, CB ✓, balance ✓
          CS2DT buy(product_id="xxx", price=3.50) → order_id="ORD-123"
          Telegram: "Куплен AK-47 | Redline за $3.50 (дисконт 35.1%)"

T+3мин    OrderTracker: добавить ORD-123 в active_orders

T+8мин    OrderTracker: get_order_status("ORD-123") → "delivered"
          PostgreSQL: UPDATE purchases SET status='delivered'
          Telegram: "Доставлен AK-47 | Redline"
```

---

## 11. СИСТЕМА БЕЗОПАСНОСТИ (4 УРОВНЯ)

### 11.1 Обзор уровней

```
┌──────────────────────────────────────────────────────────────┐
│                    УРОВЕНЬ 4: Circuit Breaker                │
│  API сломался → автоматическая пауза → health check          │
│  ┌──────────────────────────────────────────────────────────┐│
│  │                УРОВЕНЬ 3: Kill Switch                    ││
│  │  > 20 покупок/час → полная остановка + Telegram алерт    ││
│  │  ┌──────────────────────────────────────────────────────┐││
│  │  │             УРОВЕНЬ 2: Идемпотентность               │││
│  │  │  Redis key idempotency:buy:{product_id} TTL=24ч     │││
│  │  │  + проверка в БД purchases.product_id UNIQUE        │││
│  │  │  ┌──────────────────────────────────────────────────┐│││
│  │  │  │          УРОВЕНЬ 1: Лимиты                       ││││
│  │  │  │  - Дневной лимит трат (daily_limit_usd)         ││││
│  │  │  │  - Мин/макс цена предмета                       ││││
│  │  │  │  - Макс штук одного предмета за 24ч             ││││
│  │  │  │  - Порог остановки по балансу                    ││││
│  │  │  └──────────────────────────────────────────────────┘│││
│  │  └──────────────────────────────────────────────────────┘││
│  └──────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────┘
```

### 11.2 Порядок проверок в Buyer

```python
async def _safety_gate(self, candidate: MarketItem) -> SafetyCheck:
    """Все 4 уровня в правильном порядке."""

    # Уровень 4: Circuit Breaker (самый быстрый — O(1))
    try:
        self._circuit_breaker.ensure_available()
    except CircuitBreakerOpen as e:
        return SafetyCheck(False, str(e))

    # Уровень 3: Kill Switch
    hourly = await self._redis.get(f"hourly_purchases:{current_hour()}")
    if int(hourly or 0) >= self._config.safety.max_purchases_per_hour:
        await self._notifier.notify(EventType.KILL_SWITCH, "Kill switch!")
        return SafetyCheck(False, "kill switch: превышен лимит покупок/час")

    # Уровень 2: Идемпотентность (Redis → быстрее чем БД)
    if await self._redis.exists(f"idempotency:buy:{candidate.product_id}"):
        return SafetyCheck(False, "уже куплен (Redis)")

    # Уровень 1: Лимиты (БД)
    async with self._uow as uow:
        if await uow.purchases.exists(candidate.product_id):
            return SafetyCheck(False, "уже куплен (DB)")
        daily_spent = await uow.stats.get_daily_spent()
        if daily_spent + candidate.price > self._config.trading.daily_limit_usd:
            return SafetyCheck(False, "дневной лимит исчерпан")

    return SafetyCheck(True)
```

---

## 12. МАСШТАБИРОВАНИЕ

### 12.1 Добавление новой площадки

1. Создать `fluxio/api/buff/client.py`
2. Реализовать Protocol `MarketClient`
3. Зарегистрировать в контейнере
4. Scanner начинает обход новой площадки параллельно

```python
# fluxio/api/buff/client.py
class BUFFClient:
    """BUFF163 — китайская торговая площадка."""

    @property
    def platform_name(self) -> str:
        return "buff163"

    async def search_market(self, app_id: int, page: int = 1, ...) -> list[MarketItem]:
        # Реализация BUFF163 API
        ...

# Регистрация:
container.register(MarketClient, lambda: BUFFClient(...))
# Или мультиплатформенный режим:
container.register("market:buff", lambda: BUFFClient(...))
container.register("market:cs2dt", lambda: CS2DTClient(...))
```

### 12.2 Добавление новой игры

Архитектура параметризована через `app_id`. Добавление новой игры:

1. Добавить в `config.yaml`:
```yaml
games:
  - app_id: 570
    name: "Dota 2"
    enabled: true
  - app_id: 730
    name: "CS2"
    enabled: true    # ← включить
  - app_id: 252490
    name: "Rust"
    enabled: false
```

2. Scanner создаёт отдельную Redis-очередь `update_queue:730`
3. Updater обрабатывает все очереди параллельно
4. Все запросы параметризованы `app_id` — новый код не нужен

### 12.3 Добавление нового воркера

```python
# 1. Наследовать BaseWorker
class InventoryTracker(BaseWorker):
    """Мониторинг инвентаря Steam для подсчёта P&L."""

    @property
    def name(self) -> str:
        return "inventory_tracker"

    async def _tick(self) -> None:
        # Проверить инвентарь Steam, подсчитать стоимость
        ...

# 2. Добавить в main.py
async def main():
    workers = [
        Scanner(container, interval=900),
        Updater(container, interval=0),
        Buyer(container, interval=60),
        OrderTracker(container, interval=300),
        InventoryTracker(container, interval=3600),  # Каждый час
    ]
    await asyncio.gather(*[w.start() for w in workers])
```

### 12.4 Горизонтальное масштабирование (будущее)

При необходимости — выделение воркеров в отдельные процессы:

```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  Процесс 1   │   │  Процесс 2   │   │  Процесс 3   │
│  Scanner x3   │   │  Updater x5  │   │  Buyer x1    │
│  (3 площадки) │   │  (5 потоков) │   │  (singleton) │
└───────┬───────┘   └───────┬──────┘   └───────┬──────┘
        │                   │                   │
        └───────────────────▼───────────────────┘
                    ┌───────────────┐
                    │     Redis     │
                    │ (координация) │
                    └───────────────┘
```

Buyer должен оставаться singleton — иначе возможны двойные покупки.
Distributed lock через Redis (`scanner_lock:{app_id}`) гарантирует
единственность Scanner на площадку.

---

## 13. КОНФИГУРАЦИЯ

### 13.1 Двухуровневая система

```
┌──────────────────────────────────────────────┐
│                .env (секреты)                 │
│  Загружается один раз при старте              │
│  Изменение требует рестарт                    │
│  НЕ попадает в git                            │
│                                               │
│  CS2DT_APP_KEY, STEAM_LOGIN_SECURE,           │
│  POSTGRES_PASSWORD, TELEGRAM_BOT_TOKEN...     │
└──────────────────────────────────────────────┘

┌──────────────────────────────────────────────┐
│           config.yaml (бизнес-логика)         │
│  Горячая перезагрузка каждые 60 секунд        │
│  Изменение НЕ требует рестарт                 │
│  Попадает в git (без секретов)                │
│                                               │
│  min_discount_percent, daily_limit_usd,       │
│  dry_run, blacklist, whitelist...              │
└──────────────────────────────────────────────┘
```

### 13.2 Механизм горячей перезагрузки

```python
# Текущая реализация в bot/config.py

class AppConfig:
    def reload_if_changed(self) -> bool:
        """Проверить mtime файла, перечитать если изменился."""
        current_mtime = CONFIG_PATH.stat().st_mtime
        if current_mtime != self._config_mtime:
            self._backup_config()      # Бэкап в config_history/
            self._load_yaml()          # Перечитать
            return True
        return False
```

Воркеры вызывают `config.reload_if_changed()` в начале каждого цикла:

```python
class BaseWorker:
    async def _tick(self) -> None:
        if config.reload_if_changed():
            logger.info("Конфигурация обновлена")
            await self._on_config_reload()
        await self._do_work()
```

### 13.3 Версионирование конфига

- Каждое изменение `config.yaml` сохраняет бэкап в `config_history/`
- Формат: `config_2026-03-11_14-30-00.yaml`
- Хранятся последние 20 версий (настраивается `MAX_CONFIG_VERSIONS`)
- В БД таблица `config_versions` для аудита

---

## 14. МИГРАЦИИ БД

### 14.1 Alembic + авто-применение

```
fluxio/db/migrations/
├── env.py               # Конфигурация Alembic
├── script.py.mako       # Шаблон миграции
└── versions/
    ├── 001_initial.py
    ├── 002_add_items.py
    └── 003_add_steam_items.py
```

При старте приложения — автоматическое применение непримененных миграций:

```python
# fluxio/main.py

async def apply_migrations() -> None:
    """Авто-применение миграций Alembic при старте."""
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")
    logger.info("Миграции применены")
```

### 14.2 Схема БД (обзор)

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────────┐
│    items     │     │  price_history   │     │   purchases     │
│──────────────│     │──────────────────│     │─────────────────│
│ id           │◄────│ market_hash_name │     │ id              │
│ hash_name    │     │ platform         │     │ product_id (UK) │
│ app_id       │     │ price            │     │ order_id        │
│ price_usd    │     │ currency         │     │ hash_name       │
│ steam_price  │     │ recorded_at      │     │ price           │
│ volume_24h   │     └──────────────────┘     │ discount        │
│ last_seen_at │                               │ status          │
└──────────────┘     ┌──────────────────┐     │ dry_run         │
                     │  active_orders   │     │ purchased_at    │
┌──────────────┐     │──────────────────│     │ delivered_at    │
│  steam_items │     │ order_id (UK)    │     └─────────────────┘
│──────────────│     │ purchase_id      │
│ hash_name(UK)│     │ status           │     ┌─────────────────┐
│ median_price │     │ last_checked_at  │     │  daily_stats    │
│ lowest_price │     └──────────────────┘     │─────────────────│
│ volume_24h   │                               │ date (UK)       │
│ buy_order    │     ┌──────────────────┐     │ items_purchased │
│ sell_order   │     │    blacklist     │     │ total_spent     │
│ updated_at   │     │──────────────────│     │ potential_value │
└──────────────┘     │ hash_name (UK)   │     │ potential_profit│
                     │ reason           │     └─────────────────┘
                     └──────────────────┘
```

---

## 15. СТРУКТУРА ПРОЕКТА (ЦЕЛЕВАЯ)

```
fluxio/
├── fluxio/                          # Основной пакет
│   ├── __init__.py
│   ├── main.py                      # Entry point, lifecycle, asyncio.gather
│   ├── config.py                    # AppConfig + hot reload + config history
│   │
│   ├── interfaces/                  # Protocol определения (контракты)
│   │   ├── __init__.py
│   │   ├── market_client.py         # MarketClient Protocol
│   │   ├── price_provider.py        # PriceProvider Protocol + PriceData
│   │   ├── strategy.py              # Strategy Protocol + AnalysisResult
│   │   ├── notifier.py              # Notifier Protocol + EventType
│   │   └── models.py                # MarketItem, BuyResult (общие DTO)
│   │
│   ├── api/                         # Реализации клиентов API
│   │   ├── cs2dt/
│   │   │   ├── client.py            # CS2DTClient (MarketClient impl)
│   │   │   └── models.py            # DTO для CS2DT API
│   │   ├── steam/
│   │   │   ├── client.py            # SteamClient (PriceProvider impl)
│   │   │   └── models.py            # DTO для Steam API
│   │   └── c5game/
│   │       └── client.py            # C5GameClient (legacy, фаза 7)
│   │
│   ├── core/                        # Бизнес-логика
│   │   ├── workers/
│   │   │   ├── base.py              # BaseWorker
│   │   │   ├── scanner.py           # Scanner (scan → Redis queue)
│   │   │   ├── updater.py           # Updater (queue → Steam → DB)
│   │   │   ├── buyer.py             # Buyer (candidates → buy)
│   │   │   └── order_tracker.py     # OrderTracker (delivery monitoring)
│   │   ├── strategies/
│   │   │   ├── base.py              # BaseStrategy (ABC)
│   │   │   ├── discount.py          # DiscountStrategy
│   │   │   └── cross_platform.py    # CrossPlatformStrategy (будущее)
│   │   ├── analyzer.py              # ProfitAnalyzer (обёртка над Strategy)
│   │   └── safety.py                # SafetyCheck, run_all_checks
│   │
│   ├── db/
│   │   ├── models.py                # SQLAlchemy модели (13 таблиц)
│   │   ├── session.py               # async_session factory
│   │   ├── unit_of_work.py          # UnitOfWork (async context manager)
│   │   ├── repos/
│   │   │   ├── item_repo.py         # ItemRepository
│   │   │   ├── purchase_repo.py     # PurchaseRepository
│   │   │   ├── price_repo.py        # PriceRepository
│   │   │   └── stats_repo.py        # StatsRepository
│   │   └── migrations/
│   │       └── versions/            # Alembic миграции
│   │
│   ├── services/
│   │   └── container.py             # ServiceContainer (DI)
│   │
│   ├── notifications/
│   │   └── telegram.py              # TelegramNotifier (Notifier impl)
│   │
│   ├── dashboard/
│   │   ├── app.py                   # FastAPI + SSE
│   │   └── routes/
│   │       ├── admin.py             # /admin — системный мониторинг
│   │       └── user.py              # /dashboard — торговая статистика
│   │
│   └── utils/
│       ├── logger.py                # loguru конфигурация
│       ├── rate_limiter.py          # TokenBucketRateLimiter
│       ├── circuit_breaker.py       # CircuitBreaker
│       └── exporters.py             # Экспорт данных (CSV, JSON)
│
├── tests/
│   ├── unit/                        # Юнит-тесты (моки API)
│   ├── integration/                 # Интеграционные (TestContainers)
│   └── conftest.py                  # Фикстуры, dry_run=True
│
├── config.yaml                      # Бизнес-параметры (hot reload)
├── config_history/                  # Бэкапы конфига
├── .env                             # Секреты (НЕ в git)
├── .env.example                     # Шаблон .env
├── docker-compose.yml               # PostgreSQL + Redis
├── Dockerfile
├── alembic.ini
├── pyproject.toml
├── SPEC.md                          # Полное ТЗ
├── ARCHITECTURE.md                  # Этот файл
└── CLAUDE.md                        # Правила для агента
```

---

## 16. ГЛОССАРИЙ

| Термин | Определение |
|--------|-------------|
| **market_hash_name** | Уникальный ID предмета в Steam (напр. "AK-47 \| Redline (Field-Tested)") |
| **product_id** | ID конкретного листинга (ордера на продажу) на площадке |
| **app_id** | Steam Application ID (570=Dota 2, 730=CS2) |
| **дисконт** | Разница между ценой на площадке и эталонной ценой Steam (%) |
| **dry_run** | Режим симуляции: все проверки выполняются, но покупка не совершается |
| **UoW** | Unit of Work — паттерн управления транзакциями |
| **CB** | Circuit Breaker — паттерн защиты от каскадных сбоев |
| **kill switch** | Аварийная остановка при превышении лимита покупок в час |
| **freshness** | Актуальность Steam-цены (< 30 мин для кандидатов) |
| **instant delivery** | Автоматическая доставка (бот продавца отправляет трейд-оффер) |
| **P2P delivery** | Ручная доставка продавцом (до 12 часов) |
