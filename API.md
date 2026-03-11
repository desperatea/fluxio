# Fluxio API -- Документация внешних интеграций

> Skin-арбитраж платформа для Steam-игр.
> Все HTTP-запросы через `aiohttp` (async/await).
> Актуальность: 2026-03-11.

---

## Содержание

1. [Обзор API-клиентов](#1-обзор-api-клиентов)
2. [CS2DT (HaloSkins) -- основная площадка](#2-cs2dt-haloskins----основная-площадка)
3. [Steam Market -- ценовой референс](#3-steam-market----ценовой-референс)
4. [Steam Web API -- инвентарь и трейды](#4-steam-web-api----инвентарь-и-трейды)
5. [C5Game Web -- HTTP-перехват (Фаза 7)](#5-c5game-web----http-перехват-фаза-7)
6. [BUFF163 -- будущий ценовой референс](#6-buff163----будущий-ценовой-референс)
7. [Rate Limiting](#7-rate-limiting)
8. [Circuit Breaker](#8-circuit-breaker)
9. [Общие паттерны](#9-общие-паттерны)
10. [Кэширование (Redis)](#10-кэширование-redis)

---

## 1. Обзор API-клиентов

| Клиент | Класс | Статус | Base URL | Auth | Валюта |
|--------|-------|--------|----------|------|--------|
| CS2DT (HaloSkins) | `CS2DTClient` | **Активен** | `https://openapi.cs2dt.com` | query `app-key` | USD (T-Coin) |
| Steam Market | `SteamClient` | **Активен** | `https://steamcommunity.com/market` | cookie `steamLoginSecure` | USD |
| Steam Web API | -- | Запланирован | `https://api.steampowered.com` | query `key` | -- |
| C5Game (Open API) | `C5GameClient` | Справочный | `https://openapi.c5game.com` | query `app-key` | CNY |
| C5Game Web | -- | Фаза 7 | `https://www.c5game.com` | session cookies | CNY |
| BUFF163 | -- | Запланирован | -- | -- | CNY |

### Переменные окружения (.env)

```dotenv
# CS2DT
CS2DT_APP_KEY=...
CS2DT_APP_SECRET=...

# C5Game (справочный)
C5GAME_APP_KEY=...
C5GAME_APP_SECRET=...

# Steam
STEAM_API_KEY=...
STEAM_LOGIN_SECURE=...
SESSION_ID=...
BROUSER_ID=...
# Дополнительные наборы cookies: STEAM_LOGIN_SECURE2, SESSION_ID2 и т.д.

# Прокси
STEAM_PROXY=host:port:user:pass
STEAM_PROXY_FILE=proxy.txt

# Trade URL
TRADE_URL=https://steamcommunity.com/tradeoffer/new/?partner=...&token=...
```

---

## 2. CS2DT (HaloSkins) -- основная площадка

**Файл**: `bot/api/cs2dt_client.py`
**Класс**: `CS2DTClient`
**Документация API**: https://opendoc.cs2dt.com/en

### 2.1 Авторизация

- **Метод**: query-параметр `app-key` добавляется ко всем запросам автоматически
- **Подпись**: НЕ требуется
- **IP Whitelist**: обязателен -- управление через https://admin.cs2dt.com/#/user/api
- **SSL**: стандартный (валидный сертификат)

### 2.2 Формат ответа

Все ответы CS2DT имеют единую структуру:

```json
{
  "success": true,
  "errorCode": null,
  "errorMsg": null,
  "data": { ... }
}
```

При ошибке:

```json
{
  "success": false,
  "errorCode": 100001,
  "errorMsg": "IP is not in the whitelist",
  "data": null
}
```

Клиент автоматически разворачивает `data` -- методы возвращают содержимое поля `data`.

### 2.3 Коды ошибок

| Код | Описание | Действие |
|-----|----------|----------|
| `100001` | IP не в whitelist | Добавить IP в панели admin.cs2dt.com |
| `100002` | Невалидный app-key | Проверить переменную `CS2DT_APP_KEY` |
| `200001` | Предмет уже продан | Пропустить, взять следующий лот |
| `200002` | Недостаточно средств | Остановить покупки, уведомить |
| `200003` | Цена изменилась (maxPrice < текущая) | Пересканировать лот |
| `429` | Rate limit (HTTP или "Too Many" в теле) | Exponential backoff |
| `5xx` | Серверная ошибка | Retry с backoff |

### 2.4 Эндпоинты

#### 2.4.1 Аккаунт

##### GET /v2/user/v1/t-coin/balance -- Баланс

```python
data = await client.get_balance()
# data = {"userId": 1503955537416060928, "name": "tImoThy43", "data": "10.00"}
# Поле "data" (строка) -- баланс в USD
```

**Ответ:**

```json
{
  "userId": 1503955537416060928,
  "name": "tImoThy43",
  "data": "10.00"
}
```

##### GET /v1/currency/rate/usd-cny -- Курс USD/CNY

```python
rate = await client.get_usd_cny_rate()
# rate = 6.9187
```

##### GET /v2/user/steam-info -- Проверка Steam аккаунта

**Query-параметры:**

| Параметр | Тип | Обязателен | Описание |
|----------|-----|------------|----------|
| `type` | int | да | 1=для покупки, 2=для продажи |
| `appId` | int | да | 730=CS2, 570=Dota2 |
| `tradeUrl` | string | да | Steam Trade URL |

**Ответ:**

```json
{
  "checkStatus": 1,
  "steamInfo": {
    "steamId": "76561199548064835",
    "nickName": "tImoThy43"
  },
  "statusList": []
}
```

`checkStatus`: 0=проверяется, 1=OK, 2=проблема.

---

#### 2.4.2 Рынок -- поиск

##### GET /v2/product/v2/search -- Поиск предметов

**Query-параметры:**

| Параметр | Тип | По умолч. | Описание |
|----------|-----|-----------|----------|
| `appId` | int | **обязат.** | 570=Dota2, 730=CS2 |
| `page` | int | 1 | Номер страницы |
| `limit` | int | 50 | Предметов на странице |
| `orderBy` | int | 0 | 0=по времени, 1=цена вверх, 2=цена вниз |
| `keyword` | string | -- | Поиск по названию |
| `minPrice` | string | -- | Мин. цена USD |
| `maxPrice` | string | -- | Макс. цена USD |
| `onlyWithAutoDeliverPrice` | int | -- | 1=только автодоставка |

```python
data = await client.search_market(app_id=730, page=1, limit=50)
```

**Ответ:**

```json
{
  "total": 12345,
  "pages": 247,
  "page": 1,
  "limit": 50,
  "list": [
    {
      "itemId": 123456,
      "itemName": "AK-47 | Redline",
      "marketHashName": "AK-47 | Redline (Field-Tested)",
      "priceInfo": {
        "price": "5.20",
        "autoDeliverPrice": "5.50",
        "quantity": 15
      }
    }
  ]
}
```

##### GET /v2/product/v1/sell/list -- Листинги предмета

**Query-параметры:**

| Параметр | Тип | По умолч. | Описание |
|----------|-----|-----------|----------|
| `itemId` | int/str | **обязат.** | ID предмета (из search) |
| `page` | int | 1 | Номер страницы |
| `limit` | int | 50 | Лотов на странице |
| `orderBy` | int | 2 | 0=по умолч., 2=цена вверх, 3=цена вниз |
| `delivery` | int | -- | 1=ручная, 2=автоматическая |
| `minPrice` | string | -- | Мин. цена |
| `maxPrice` | string | -- | Макс. цена |

```python
data = await client.get_sell_list(item_id=123456, delivery=2)
```

**Ответ:**

```json
{
  "total": 15,
  "list": [
    {
      "id": 789012,
      "price": "5.20",
      "cnyPrice": "37.50",
      "delivery": 2,
      "assetInfo": {
        "classId": "310776560",
        "instanceId": "480085569",
        "marketHashName": "AK-47 | Redline (Field-Tested)"
      }
    }
  ]
}
```

> Поле `id` -- это `productId` для покупки через `buy()`.

##### GET /v2/product/v1/sell/product -- Детали лота

**Query:** `productId` (string) -- ID лота.

##### POST /v2/product/price/info -- Цены пакетом (макс 200)

**Тело запроса:**

```json
{
  "appId": 730,
  "marketHashNameList": [
    "AK-47 | Redline (Field-Tested)",
    "AWP | Asiimov (Field-Tested)"
  ]
}
```

**Ответ:**

```json
[
  {
    "marketHashName": "AK-47 | Redline (Field-Tested)",
    "price": "5.20",
    "quantity": 15,
    "autoDeliverPrice": "5.50",
    "avgPrice": "5.35",
    "medianPrice": "5.30"
  }
]
```

> Лимит: максимум 200 `marketHashName` за один запрос.

##### POST /v2/product/v1/info -- Инфо по productId (макс 5000)

**Тело:** `{"productIdList": [789012, 789013]}`

##### POST /v2/product/v2/list/ids -- Статус лотов по productId (макс 100)

**Тело:** `{"productIds": [789012, 789013]}`

Возвращает только лоты, которые ещё в продаже.

##### GET /v2/product/v1/filters -- Фильтры

**Query:** `appId` (int) -- ID игры.

Возвращает доступные категории, типы, редкости для фильтрации поиска.

---

#### 2.4.3 Покупка

##### POST /v2/trade/v2/buy -- Покупка по productId

**Тело запроса:**

```json
{
  "productId": 789012,
  "outTradeNo": "550e8400-e29b-41d4-a716-446655440000",
  "tradeUrl": "https://steamcommunity.com/tradeoffer/new/?partner=...&token=...",
  "maxPrice": 5.50
}
```

| Поле | Тип | Обязат. | Описание |
|------|-----|---------|----------|
| `productId` | int | да | ID лота из sell/list |
| `outTradeNo` | string | да | UUID нашего заказа (идемпотентность) |
| `tradeUrl` | string | да | Steam Trade URL покупателя |
| `maxPrice` | float | нет | Макс. допустимая цена (приоритет над buyPrice) |
| `buyPrice` | float | нет | Точная цена для проверки |

**Ответ:**

```json
{
  "buyPrice": 5.20,
  "orderId": "1234567890",
  "delivery": 2,
  "offerId": "5678901234"
}
```

##### POST /v2/trade/v2/quick-buy -- Быстрая покупка

Покупает самый дешёвый лот по `itemId` или `marketHashName`.

**Тело запроса:**

```json
{
  "outTradeNo": "550e8400-e29b-41d4-a716-446655440000",
  "tradeUrl": "https://steamcommunity.com/tradeoffer/new/?partner=...&token=...",
  "maxPrice": 5.50,
  "itemId": 123456,
  "delivery": 2,
  "lowPrice": 1
}
```

| Поле | Тип | Обязат. | Описание |
|------|-----|---------|----------|
| `outTradeNo` | string | да | UUID нашего заказа |
| `tradeUrl` | string | да | Steam Trade URL |
| `maxPrice` | float | да | Макс. допустимая цена |
| `itemId` | int | нет* | ID предмета |
| `appId` | int | нет* | ID игры (нужен с marketHashName) |
| `marketHashName` | string | нет* | Имя предмета |
| `delivery` | int | нет | 1=ручная, 2=авто |
| `lowPrice` | int | нет | 1=по мин. цене |

> *Обязателен либо `itemId`, либо `appId` + `marketHashName`.

---

#### 2.4.4 Заказы покупателя

##### GET /v2/order/buyer/list -- Список заказов (v1)

**Query:** `appId`, `page`, `limit`, `status` (опц.).

##### POST /v2/order/buyer/v2/list -- Список заказов (v2)

Расширенная версия с фильтрацией по `orderIdList` и `outTradeNos`.

**Статусы заказов:**

| Код | Статус | Описание |
|-----|--------|----------|
| 1 | WaitingDelivery | Ожидание отправки продавцом |
| 2 | Delivery | Предмет отправлен |
| 3 | WaitingAccept | Трейд-оффер ожидает принятия |
| 10 | Successful | Успешно завершён |
| 11 | Fail | Ошибка / отмена |

##### GET /v1/order/v2/buy/detail -- Детали заказа

**Query:** `orderId` или `outTradeNo`.

##### POST /v1/order/buyer-cancel -- Отмена заказа

**Тело:** `{"orderId": "..."}`

> Штраф за отмену: 2% от цены (мин 0.01T, макс 10T).

---

#### 2.4.5 Продажа

##### POST /v2/open/sell/v1/create -- Выставить на продажу

**Тело:**

```json
{
  "appId": 730,
  "assetType": 1,
  "products": [
    {
      "assetId": "12345678",
      "classId": "310776560",
      "instanceId": "480085569",
      "marketHashName": "AK-47 | Redline (Field-Tested)",
      "price": 5.50,
      "steamId": "76561199548064835"
    }
  ]
}
```

`assetType`: 1=обычный, 2=с cooldown.

##### POST /v2/open/sell/v1/cancel -- Снять с продажи

**Тело:** `{"assetType": 1, "products": [{"productId": "..."}]}`

##### POST /v2/open/sell/v1/update -- Обновить цену

**Тело:**

```json
{
  "appId": 730,
  "assetType": 1,
  "products": [{"productId": "...", "price": 5.75}],
  "requestId": "unique-request-id"
}
```

##### GET /v2/open/sell/v1/list -- Мои лоты

**Query:** `assetType` (опц.).

##### POST /v2/open/sell/v1/price/reference -- Референсные цены

Получить рекомендованные цены для выставления.

---

### 2.5 Rate Limiting (CS2DT)

```python
# Конфигурация в CS2DTClient.__init__
self._rate_limiter = TokenBucketRateLimiter(rate=5.0, burst=5, name="cs2dt")
self._semaphore = asyncio.Semaphore(3)
```

- **QPS**: 5 запросов/сек
- **Burst**: 5 (можно отправить 5 запросов мгновенно)
- **Параллельных соединений**: макс 3 (Semaphore)
- **Timeout**: 30 секунд

### 2.6 Retry (CS2DT)

| Сценарий | Max retries | Base delay | Max delay | Backoff |
|----------|-------------|------------|-----------|---------|
| HTTP 429 | 5 | 1.0с | 16.0с | x2 |
| HTTP 5xx | 3 | 5.0с | 20.0с | x2 |
| Сетевая ошибка | 3 | 5.0с | 20.0с | x2 |

Формула задержки: `delay = min(base_delay * (backoff_factor ^ attempt), max_delay)`

Пример для 429: 1с -> 2с -> 4с -> 8с -> 16с

---

## 3. Steam Market -- ценовой референс

**Файл**: `bot/api/steam_client.py`
**Класс**: `SteamClient`

### 3.1 Авторизация

- **priceoverview**: публичный, без авторизации
- **pricehistory**: требует cookie `steamLoginSecure`
- **Доп. cookies**: `sessionid`, `browserid`
- **Множественные аккаунты**: поддержка `STEAM_LOGIN_SECURE`, `STEAM_LOGIN_SECURE2`, ..., `STEAM_LOGIN_SECURE5`

### 3.2 Прокси-ротация

Steam агрессивно лимитирует по IP. Клиент поддерживает ротацию прокси:

**Загрузка прокси** (приоритет):
1. Файл: `proxy.txt.txt`, `proxy.txt`, `proxies.txt` в корне проекта
2. Переменная `STEAM_PROXY_FILE` в .env
3. Переменные `STEAM_PROXY`, `HTTP_PROXY`, `HTTP_PROXY2`, `HTTPS_PROXY`

**Форматы прокси:**
```
host:port:user:password    -> http://user:password@host:port
host:port                  -> http://host:port
http://user:pass@host:port -> как есть
socks5://host:port         -> как есть
```

**Алгоритм ротации**: round-robin (`_next_proxy()`)

Прямое подключение (без прокси) всегда добавляется как последний канал.

**Per-channel rate limiting:**
```python
# Канал = прокси или прямое подключение
# Каждый канал имеет свой TokenBucketRateLimiter
rate_per_channel = 0.33 if channel_count > 10 else 0.2  # 1 req/3s или 1 req/5s
```

**Параллельность:**
```python
max_parallel = min(channel_count, 25)
self._semaphore = asyncio.Semaphore(max_parallel)
```

### 3.3 Cookie Management

- Каждый канал получает свой набор cookies (round-robin по `_cookie_sets`)
- **Auto-disable**: 3 последовательных ошибки pricehistory -> `_has_auth = False`
- При отключении pricehistory клиент переключается на priceoverview (fallback)
- `pricehistory` всегда идёт через прямое подключение (IP привязан к cookies)

### 3.4 Эндпоинты

#### 3.4.1 GET /market/priceoverview/ -- Текущая цена (публичный)

**Query-параметры:**

| Параметр | Тип | Описание |
|----------|-----|----------|
| `appid` | int | 570=Dota2, 730=CS2 |
| `currency` | int | 1=USD, 23=CNY |
| `market_hash_name` | string | URL-encoded имя предмета |

```python
data = await client.get_price_overview("AK-47 | Redline (Field-Tested)", app_id=730)
```

**Ответ:**

```json
{
  "success": true,
  "lowest_price": "$5.15",
  "median_price": "$5.30",
  "volume": "842"
}
```

**Парсинг цены** (`_parse_price_string()`):

| Формат | Результат |
|--------|-----------|
| `"$0.03"` | `0.03` |
| `"$1,234.56"` | `1234.56` |
| `"1.234,56"` (EU) | `1234.56` |

#### 3.4.2 GET /market/pricehistory/ -- История продаж (auth)

**Требует**: cookie `steamLoginSecure`.

**Query-параметры:**

| Параметр | Тип | Описание |
|----------|-----|----------|
| `appid` | int | ID игры |
| `currency` | int | 1=USD |
| `market_hash_name` | string | URL-encoded имя |

```python
history = await client.get_price_history("AK-47 | Redline (Field-Tested)", app_id=730)
```

**Ответ:**

```json
{
  "success": true,
  "prices": [
    ["Mar 07 2026 12: +0", 5.20, "15"],
    ["Mar 07 2026 13: +0", 5.25, "8"],
    ["Mar 08 2026 01: +0", 5.18, "12"]
  ]
}
```

**Парсинг даты** (формат Steam):

```
"Mar 07 2026 12: +0"
       |
       v  split(": +")[0]
"Mar 07 2026 12"
       |
       v  strptime("%b %d %Y %H")
datetime(2026, 3, 7, 12, 0)
```

```python
clean = date_str.split(": +")[0].strip()
dt = datetime.strptime(clean, "%b %d %Y %H")
```

#### 3.4.3 GET /market/search/render/ -- Поиск предметов

**Query-параметры:**

| Параметр | Тип | Описание |
|----------|-----|----------|
| `appid` | int | ID игры |
| `norender` | int | 1 (JSON-ответ) |
| `start` | int | Offset для пагинации |
| `count` | int | Кол-во (макс 100) |
| `sort_column` | string | `name`, `price`, `quantity` |
| `sort_dir` | string | `asc`, `desc` |
| `query` | string | Поисковый запрос |

**Ответ:**

```json
{
  "success": true,
  "total_count": 15000,
  "results": [
    {
      "name": "AK-47 | Redline (Field-Tested)",
      "hash_name": "AK-47 | Redline (Field-Tested)",
      "sell_listings": 842,
      "sell_price": 530,
      "sell_price_text": "$5.30",
      "app_icon": "...",
      "asset_description": { ... }
    }
  ]
}
```

#### 3.4.4 GET /market/listings/{appid}/{name} -- Страница листинга (HTML)

Используется для извлечения `item_nameid`:

```python
nameid = await client.get_item_nameid("AK-47 | Redline (Field-Tested)", app_id=730)
# nameid = 176208497
```

Парсинг HTML -- поиск паттерна:
```javascript
Market_LoadOrderSpread( 176208497 );
// или
ItemActivityTicker.Start( 176208497 );
```

#### 3.4.5 GET /market/itemordershistogram -- Ордера (buy/sell orders)

**Query-параметры:**

| Параметр | Тип | Описание |
|----------|-----|----------|
| `country` | string | `US` |
| `language` | string | `english` |
| `currency` | int | 1=USD |
| `item_nameid` | int | ID из страницы листинга |
| `two_factor` | int | 0 |
| `norender` | int | 1 |

**Ответ:**

```json
{
  "success": 1,
  "sell_order_graph": [[5.15, 3, "3 for $5.15"], [5.20, 8, "8 for $5.20"]],
  "buy_order_graph": [[5.10, 5, "5 for $5.10"], [5.00, 12, "12 for $5.00"]],
  "highest_buy_order": "510",
  "lowest_sell_order": "515",
  "buy_order_summary": { ... },
  "sell_order_summary": { ... }
}
```

> Цены в `highest_buy_order` / `lowest_sell_order` -- в центах (510 = $5.10).

### 3.5 Вычисление медианной цены

Метод `get_median_price()` возвращает `SteamPriceData`:

```python
@dataclass
class SteamPriceData:
    median_price_usd: float   # Медианная цена за N дней
    lowest_price_usd: float   # Минимальная цена
    sales_count: int           # Количество продаж
    source: str                # "pricehistory" | "priceoverview"
```

**Стратегия fallback:**

1. Попытка `pricehistory` (полные данные за 30 дней)
2. Если недоступен -> `priceoverview` (только текущие данные)

**Алгоритм расчёта из pricehistory:**
- Фильтрация записей за последние N дней
- Каждая запись взвешивается по объёму (volume)
- `statistics.median()` по развёрнутому списку цен

### 3.6 Retry (Steam)

```python
RETRY_STEAM = RetryConfig(max_retries=4, base_delay=10.0, max_delay=60.0, backoff_factor=2.0)
```

| Попытка | Задержка |
|---------|----------|
| 1 | 10с |
| 2 | 20с |
| 3 | 40с |
| 4 | 60с (max) |

Не-JSON ответ (HTML страница логина) -- возвращает `None` без retry.

---

## 4. Steam Web API -- инвентарь и трейды

**Base URL**: `https://api.steampowered.com`
**Auth**: query-параметр `key` (Steam API Key)
**Статус**: архитектурно запланирован

### 4.1 Эндпоинты

#### GET /IEconService/GetTradeOffers/v1/ -- Трейд-офферы

| Параметр | Тип | Описание |
|----------|-----|----------|
| `key` | string | Steam API Key |
| `get_received_offers` | bool | Получить входящие офферы |
| `get_sent_offers` | bool | Получить исходящие офферы |
| `active_only` | bool | Только активные |
| `time_historical_cutoff` | int | Timestamp начала |

#### GET /ISteamUser/GetPlayerSummaries/v2/ -- Информация о пользователе

| Параметр | Тип | Описание |
|----------|-----|----------|
| `key` | string | Steam API Key |
| `steamids` | string | Список SteamID через запятую |

#### GET /IEconItems_{appid}/GetPlayerItems/v1/ -- Инвентарь

| Параметр | Тип | Описание |
|----------|-----|----------|
| `key` | string | Steam API Key |
| `steamid` | string | SteamID64 пользователя |

> Альтернатива: `https://steamcommunity.com/inventory/{steamid}/{appid}/2?l=english&count=5000` (без API-ключа, но с rate limit).

---

## 5. C5Game Web -- HTTP-перехват (Фаза 7)

**Статус**: архитектурно запланирован, не реализован.

### 5.1 Концепция

C5Game не предоставляет публичный API для просмотра рынка конечными пользователями. Подход -- перехват HTTP-запросов из браузера:

1. Анализ запросов через DevTools (Network tab)
2. Реверс-инжиниринг эндпоинтов и параметров
3. Реализация клиента с session cookies

### 5.2 Известные параметры

- **Base URL**: `https://www.c5game.com`
- **Auth**: session cookies (из браузера)
- **SSL**: самоподписанный сертификат -> `ssl.CERT_NONE`
- **Rate limit**: неизвестен (агрессивный)

### 5.3 Существующий C5Game Open API (справочный)

**Файл**: `bot/api/c5game_client.py`
**Класс**: `C5GameClient`

Документация: https://90nj2dplv3.apifox.cn/

| Эндпоинт | Метод | Описание |
|----------|-------|----------|
| `/merchant/account/v1/balance` | GET | Баланс |
| `/merchant/market/v2/products/{gameId}` | POST | Листинг рынка |
| `/merchant/sale/v1/search` | GET | Наши лоты |
| `/merchant/product/price/batch` | POST | Цены пакетом |
| `/merchant/order/v2/buyer` | POST | Заказы покупателя |
| `/merchant/trade/v2/quick-buy` | POST | Быстрая покупка |
| `/merchant/trade/v2/normal-buy` | POST | Обычная покупка |

> SSL: самоподписанный сертификат -- `ssl.check_hostname = False`, `ssl.verify_mode = CERT_NONE`.
> Rate limit: реальный ~1 QPS (заявленный 50 QPS).

---

## 6. BUFF163 -- будущий ценовой референс

**Статус**: запланирован через протокол `PriceProvider`.

Интеграция будет реализована через абстрактный интерфейс, позволяющий подключать произвольные источники цен:

```python
class PriceProvider(Protocol):
    async def get_price(self, market_hash_name: str) -> PriceData | None: ...
    async def get_prices_batch(self, names: list[str]) -> dict[str, PriceData]: ...
```

---

## 7. Rate Limiting

### 7.1 Конфигурация по клиентам

| Клиент | QPS | Burst | Max Parallel | Timeout | Алгоритм |
|--------|-----|-------|-------------|---------|----------|
| CS2DT | 5.0 | 5 | 3 | 30с | Token Bucket |
| Steam (>10 каналов) | 0.33/канал | 1 | min(N, 25) | 30с | Token Bucket / канал |
| Steam (<=10 каналов) | 0.2/канал | 1 | min(N, 25) | 30с | Token Bucket / канал |
| C5Game | 1.5 | 1 | 1 | 30с | Token Bucket |

### 7.2 Retry конфигурации

| Конфиг | Max retries | Base delay | Max delay | Backoff | Применение |
|--------|-------------|------------|-----------|---------|------------|
| `RETRY_429` | 5 | 1.0с | 16.0с | x2 | Rate limit (все клиенты) |
| `RETRY_5XX` | 3 | 5.0с | 20.0с | x2 | Серверные ошибки (CS2DT, C5Game) |
| `RETRY_STEAM` | 4 | 10.0с | 60.0с | x2 | Все ошибки Steam |

### 7.3 Token Bucket -- реализация

**Файл**: `bot/utils/rate_limiter.py`

```python
class TokenBucketRateLimiter:
    def __init__(self, rate: float, burst: int, name: str) -> None:
        self.rate = rate        # Токенов/сек
        self.burst = burst      # Макс. ёмкость
        self._tokens = float(burst)
        self._last_refill = time.monotonic()

    async def acquire(self) -> None:
        """Блокирует до получения токена."""
        async with self._lock:
            self._refill()
            if self._tokens < 1.0:
                wait_time = (1.0 - self._tokens) / self.rate
                await asyncio.sleep(wait_time)
                self._refill()
            self._tokens -= 1.0
```

**Принцип**: токены пополняются непрерывно со скоростью `rate` токенов/сек. Максимальная ёмкость = `burst`. Один запрос = один токен. Если токенов нет -- `acquire()` блокирует до пополнения.

---

## 8. Circuit Breaker

### 8.1 Конфигурация по клиентам

| Клиент | Тип | Порог | Действие |
|--------|-----|-------|----------|
| CS2DT | Exception-based | `CS2DTAPIError` propagation | Bubble up (caller decides) |
| Steam pricehistory | Cookie-based | 3 подряд ошибки | Auto-disable pricehistory |
| Steam priceoverview | None | Возвращает `None` | Graceful degradation |
| C5Game | Exception-based | `C5GameAPIError` propagation | Bubble up |

### 8.2 Steam Auto-Disable

```python
# В SteamClient.get_price_history():
self._auth_fail_count += 1
if self._auth_fail_count >= 3 and self._has_auth:
    self._has_auth = False
    # Логирование: "cookies протухли -- pricehistory отключён"
```

После отключения pricehistory:
- `get_price_history()` сразу возвращает `None`
- `get_median_price()` переключается на fallback (`priceoverview`)
- Восстановление: только ручной перезапуск с новыми cookies

---

## 9. Общие паттерны

### 9.1 Структура клиента

Все API-клиенты следуют одной архитектуре:

```python
class BaseClient:
    # 1. Инициализация
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._rate_limiter = TokenBucketRateLimiter(...)
        self._semaphore = asyncio.Semaphore(N)

    # 2. Управление сессией
    async def _get_session(self) -> aiohttp.ClientSession: ...
    async def close(self) -> None: ...

    # 3. Базовый запрос с retry
    async def _request(self, endpoint_key, params, json_body, retry_config) -> dict: ...

    # 4. Бизнес-методы
    async def get_balance(self) -> dict: ...
    async def search_market(self, ...) -> dict: ...
```

### 9.2 Метод _request() -- жизненный цикл

```
_request(endpoint_key)
    |
    v
[Resolve endpoint: method, path]
    |
    v
[for attempt in range(max_retries + 1):]
    |
    v
[Semaphore.acquire] -- ограничение параллельности
    |
    v
[RateLimiter.acquire] -- ожидание токена
    |
    v
[session.request(method, path, params, json)] -- HTTP-запрос
    |
    +-- 429 -> sleep(backoff) -> retry
    +-- 5xx -> sleep(backoff) -> retry
    +-- ClientError/Timeout -> sleep(backoff) -> retry
    +-- success=false -> raise APIError
    +-- success=true -> return data
    |
[Semaphore.release] -- освобождение
```

> Sleep выполняется ВНЕ семафора, чтобы не блокировать другие запросы.

### 9.3 Иерархия ошибок

```
Exception
  |
  +-- CS2DTAPIError
  |     .status_code: int | None      # HTTP-статус
  |     .error_code: int | None       # Бизнес-код (100001, 200001, ...)
  |     .response_data: dict | None   # Полный ответ API
  |
  +-- C5GameAPIError
  |     .status_code: int | None
  |     .error_code: int | None
  |     .response_data: dict | None
  |
  +-- aiohttp.ClientError            # Сетевые ошибки (retry)
  +-- asyncio.TimeoutError           # Таймаут (retry)
```

### 9.4 Маскировка секретов в логах

```python
@property
def _masked_key(self) -> str:
    if len(self._app_key) <= 8:
        return "***"
    return f"{self._app_key[:4]}***{self._app_key[-4:]}"
# Пример: "abcd***wxyz"
```

### 9.5 User-Agent (Steam)

```
Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36
(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36
```

---

## 10. Кэширование (Redis)

**Переменная**: `REDIS_URL` (по умолч. `redis://localhost:6379/0`)

### 10.1 TTL по типам данных

| Данные | Ключ Redis | TTL | Описание |
|--------|------------|-----|----------|
| Steam priceoverview | `steam:price:{hash_name}` | 5 мин | Текущая цена |
| Steam pricehistory | `steam:history:{hash_name}` | 1 час | История продаж |
| Steam median price | `steam:median:{hash_name}:{days}` | 15 мин | Расчётная медиана |
| CS2DT search results | `cs2dt:search:{query_hash}` | 2 мин | Результаты поиска |
| CS2DT sell list | `cs2dt:sell:{item_id}` | 1 мин | Листинги лота |
| CS2DT price batch | `cs2dt:prices:{hash}` | 5 мин | Пакетные цены |
| CS2DT balance | `cs2dt:balance` | 30 сек | Баланс аккаунта |
| USD/CNY rate | `rate:usd_cny` | 1 час | Курс валют |
| Blacklist | `config:blacklist` | -- | Без TTL (config) |

### 10.2 Стратегия инвалидации

- **По покупке**: `cs2dt:balance`, `cs2dt:sell:{item_id}` инвалидируются
- **По таймеру**: TTL-based expiration
- **Ручная**: через dashboard или CLI

---

> Файл сгенерирован автоматически. Последнее обновление: 2026-03-11.
