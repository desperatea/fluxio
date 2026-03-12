"""Веб-дашборд Fluxio (FastAPI) — admin + user + health + SSE логи.

Фаза 1: /health, /admin/, /admin/logs (SSE), /api/v1/status
Фаза 2: /items, /candidates, /api/v1/workers (реальный статус), /api/v1/candidates
Фаза 3: /purchases, /api/v1/purchases
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.responses import StreamingResponse

from fluxio.config import config
from fluxio.services.container import ServiceContainer
from fluxio.utils.circuit_breaker import CircuitBreaker
from fluxio.utils.logger import get_log_buffer, subscribe_logs, unsubscribe_logs

# Время запуска
_started_at = datetime.now(timezone.utc)

# Ссылка на контейнер (устанавливается при create_app)
_container: ServiceContainer | None = None


def _css() -> str:
    """Общие CSS стили для всех страниц."""
    return """
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0f1923; color: #c7d5e0;
         margin: 0; padding: 20px; }
  h1 { color: #66c0f4; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }
  .card { background: #1b2838; border-radius: 8px; padding: 20px; border: 1px solid #2a475e; }
  .card h2 { color: #66c0f4; margin: 0 0 12px; font-size: 16px; }
  .metric { margin: 6px 0; }
  .label { color: #8f98a0; }
  .status { font-size: 20px; font-weight: 600; }
  nav { margin-bottom: 20px; }
  nav a { color: #66c0f4; margin-right: 16px; text-decoration: none; }
  nav a:hover { text-decoration: underline; }
  table { width: 100%; border-collapse: collapse; background: #1b2838; border-radius: 8px; }
  th { background: #2a475e; color: #66c0f4; padding: 10px 14px; text-align: left; }
  td { padding: 8px 14px; border-bottom: 1px solid #2a475e; color: #c7d5e0; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #243547; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; }
  .badge-ok { background: #1a472a; color: #4caf50; }
  .badge-warn { background: #3d2b00; color: #ff9800; }
  .badge-err { background: #3d0000; color: #f44336; }
  """


def _nav(current: str = "") -> str:
    """Навигационное меню."""
    links = [
        ("/", "Главная"),
        ("/items", "Каталог"),
        ("/candidates", "Кандидаты"),
        ("/purchases", "Покупки"),
        ("/admin/", "Admin"),
        ("/admin/logs", "Логи"),
    ]
    parts = []
    for href, label in links:
        active = ' style="font-weight:bold"' if href == current else ""
        parts.append(f'<a href="{href}"{active}>{label}</a>')
    return "<nav>" + "".join(parts) + "</nav>"


def create_app(container: ServiceContainer | None = None) -> FastAPI:
    """Создать FastAPI приложение с DI-контейнером."""
    global _container
    _container = container

    app = FastAPI(title="Fluxio Dashboard", version="2.0.0")

    # ─── Health ────────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> JSONResponse:
        """Health check для Docker healthcheck."""
        uptime = int((datetime.now(timezone.utc) - _started_at).total_seconds())
        return JSONResponse({
            "status": "ok",
            "started_at": _started_at.isoformat(),
            "uptime_seconds": uptime,
        })

    # ─── API v1 ────────────────────────────────────────────────────────────────

    @app.get("/api/v1/status")
    async def api_status() -> JSONResponse:
        """Полный статус бота (JSON)."""
        uptime = int((datetime.now(timezone.utc) - _started_at).total_seconds())
        data: dict[str, Any] = {
            "status": "running",
            "mode": "dry_run" if config.trading.dry_run else "live",
            "uptime_seconds": uptime,
            "config": {
                "min_discount_percent": config.trading.min_discount_percent,
                "min_price_usd": config.trading.min_price_usd,
                "max_price_usd": config.trading.max_price_usd,
                "daily_limit_usd": config.trading.daily_limit_usd,
            },
        }

        if _container and _container.is_initialized(CircuitBreaker):
            cb = await _container.get(CircuitBreaker)
            data["circuit_breaker"] = cb.status()

        return JSONResponse(data)

    @app.get("/api/v1/workers")
    async def api_workers() -> JSONResponse:
        """Статус воркеров — реальное состояние из объектов воркеров."""
        workers: list[dict[str, Any]] = []

        if _container:
            from fluxio.core.workers.buyer import BuyerWorker
            from fluxio.core.workers.order_tracker import OrderTrackerWorker
            from fluxio.core.workers.scanner import ScannerWorker
            from fluxio.core.workers.updater import UpdaterWorker

            for worker_type in (ScannerWorker, UpdaterWorker, BuyerWorker, OrderTrackerWorker):
                if _container.is_initialized(worker_type):
                    worker = await _container.get(worker_type)
                    st = worker.status.to_dict()
                    # Добавляем метрики сканера
                    if worker_type is ScannerWorker and worker.last_result:
                        st["last_scan"] = {
                            "items_upserted": worker.last_result.items_upserted,
                            "queue_entries": worker.last_result.queue_entries_added,
                            "duration_seconds": round(worker.last_result.duration_seconds, 1),
                            "errors": worker.last_result.errors,
                        }
                    # Добавляем метрики Buyer / OrderTracker
                    if hasattr(worker, "stats"):
                        st["stats"] = worker.stats()
                    workers.append(st)
                else:
                    name = worker_type.__name__.replace("Worker", "").lower()
                    workers.append({"name": name, "running": False, "last_run_at": None})
        else:
            workers = [
                {"name": "scanner", "running": False, "last_run_at": None},
                {"name": "updater", "running": False, "last_run_at": None},
                {"name": "buyer", "running": False, "last_run_at": None},
                {"name": "order_tracker", "running": False, "last_run_at": None},
            ]

        return JSONResponse({"workers": workers})

    @app.get("/api/v1/candidates")
    async def api_candidates() -> JSONResponse:
        """Кандидаты на покупку из Redis."""
        try:
            from fluxio.utils.redis_client import KEY_CANDIDATES, get_redis
            redis = await get_redis()
            names = list(await redis.smembers(KEY_CANDIDATES))
            queue_len = await redis.zcard("steam:update_queue")
        except Exception as e:
            return JSONResponse({"error": str(e), "candidates": [], "queue_size": 0})

        # Получаем данные из БД для кандидатов
        candidates: list[dict[str, Any]] = []
        if names:
            from fluxio.db.session import async_session_factory
            from fluxio.db.unit_of_work import UnitOfWork
            async with UnitOfWork(async_session_factory) as uow:
                for name in names[:50]:  # Лимит 50 для API
                    item = await uow.items.get_by_name(name)
                    if item:
                        steam_p = float(item.steam_price_usd) if item.steam_price_usd else None
                        item_p = float(item.price_usd) if item.price_usd else None
                        discount = None
                        if steam_p and item_p and steam_p > 0:
                            fee = config.fees.steam_fee_percent / 100
                            net = steam_p * (1 - fee)
                            discount = round((net - item_p) / net * 100, 1) if net > 0 else None
                        candidates.append({
                            "market_hash_name": name,
                            "price_usd": item_p,
                            "steam_price_usd": steam_p,
                            "discount_percent": discount,
                            "steam_volume_24h": item.steam_volume_24h,
                        })

        return JSONResponse({
            "candidates": candidates,
            "total": len(names),
            "queue_size": queue_len,
        })

    # ─── SSE логи ──────────────────────────────────────────────────────────────

    @app.get("/sse/logs")
    async def sse_logs(request: Request) -> StreamingResponse:
        """Server-Sent Events: realtime логи."""
        queue = subscribe_logs()

        async def event_generator():
            try:
                for line in get_log_buffer():
                    yield f"data: {line}\n\n"

                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=30.0)
                        yield f"data: {msg}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                unsubscribe_logs(queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # ─── User Dashboard HTML ───────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def user_index() -> str:
        """Главная страница — баланс, сводка, ссылки."""
        uptime = int((datetime.now(timezone.utc) - _started_at).total_seconds())
        h, rem = divmod(uptime, 3600)
        m, s = divmod(rem, 60)
        mode = "DRY RUN" if config.trading.dry_run else "БОЕВОЙ"
        mode_color = "#4caf50" if config.trading.dry_run else "#f44336"

        try:
            from fluxio.utils.redis_client import KEY_CANDIDATES, KEY_UPDATE_QUEUE, get_redis
            redis = await get_redis()
            queue_len = await redis.zcard(KEY_UPDATE_QUEUE)
            candidates_count = await redis.scard(KEY_CANDIDATES)
        except Exception:
            queue_len = 0
            candidates_count = 0

        return f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><title>Fluxio</title>
<style>{_css()}</style></head>
<body>
<h1>Fluxio</h1>
{_nav("/")}
<div class="grid">
  <div class="card">
    <h2>Система</h2>
    <div class="metric"><span class="label">Режим:</span>
      <span class="status" style="color:{mode_color}">{mode}</span></div>
    <div class="metric"><span class="label">Аптайм:</span> {h}ч {m}м {s}с</div>
    <div class="metric"><span class="label">Дисконт:</span> ≥{config.trading.min_discount_percent}%</div>
    <div class="metric"><span class="label">Дневной лимит:</span> ${config.trading.daily_limit_usd}</div>
    <div class="metric"><span class="label">Цена:</span> ${config.trading.min_price_usd} – ${config.trading.max_price_usd}</div>
  </div>
  <div class="card">
    <h2>Redis</h2>
    <div class="metric"><span class="label">Очередь Steam:</span> {queue_len} предметов</div>
    <div class="metric"><span class="label">Кандидатов:</span> {candidates_count}</div>
  </div>
  <div class="card">
    <h2>Быстрые ссылки</h2>
    <div class="metric"><a href="/items">Каталог предметов</a></div>
    <div class="metric"><a href="/candidates">Кандидаты на покупку</a></div>
    <div class="metric"><a href="/admin/">Системный дашборд</a></div>
    <div class="metric"><a href="/admin/logs">Логи (SSE)</a></div>
    <div class="metric"><a href="/api/v1/workers">API: воркеры</a></div>
  </div>
</div>
</body></html>"""

    @app.get("/items", response_class=HTMLResponse)
    async def user_items() -> str:
        """Каталог предметов из БД."""
        items_html = ""
        total = 0

        try:
            from fluxio.db.session import async_session_factory
            from fluxio.db.unit_of_work import UnitOfWork
            async with UnitOfWork(async_session_factory) as uow:
                all_items = await uow.items.get_all()
                total = len(all_items)
                rows = []
                for item in all_items[:200]:  # Лимит 200 на страницу
                    p = float(item.price_usd) if item.price_usd else None
                    sp = float(item.steam_price_usd) if item.steam_price_usd else None
                    discount = ""
                    badge = ""
                    if p and sp and sp > 0:
                        fee = config.fees.steam_fee_percent / 100
                        net = sp * (1 - fee)
                        d = (net - p) / net * 100 if net > 0 else 0
                        discount = f"{d:.1f}%"
                        if d >= config.trading.min_discount_percent:
                            badge = '<span class="badge badge-ok">✓</span>'
                    price_str = f"${p:.4f}" if p else "—"
                    steam_str = f"${sp:.4f}" if sp else "нет"
                    upd = ""
                    if hasattr(item, "steam_updated_at") and item.steam_updated_at:
                        upd = item.steam_updated_at.strftime("%H:%M")
                    rows.append(
                        f"<tr><td>{item.market_hash_name}</td>"
                        f"<td>{price_str}</td>"
                        f"<td>{steam_str}</td>"
                        f"<td>{discount} {badge}</td>"
                        f"<td>{item.steam_volume_24h or '—'}</td>"
                        f"<td>{upd}</td></tr>"
                    )
                items_html = "".join(rows)
        except Exception as e:
            items_html = f"<tr><td colspan='6'>Ошибка: {e}</td></tr>"

        return f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><title>Fluxio — Каталог</title>
<style>{_css()}</style></head>
<body>
<h1>Каталог предметов</h1>
{_nav("/items")}
<p style="color:#8f98a0">Всего предметов: {total} | Показано: до 200</p>
<table>
<thead><tr>
  <th>market_hash_name</th><th>Цена CS2DT</th><th>Steam медиана</th>
  <th>Дисконт</th><th>Volume 24h</th><th>Обновлён</th>
</tr></thead>
<tbody>{items_html}</tbody>
</table>
</body></html>"""

    @app.get("/candidates", response_class=HTMLResponse)
    async def user_candidates() -> str:
        """Текущие кандидаты на арбитраж из Redis."""
        rows_html = ""
        total = 0
        queue_len = 0

        try:
            from fluxio.utils.redis_client import KEY_CANDIDATES, KEY_UPDATE_QUEUE, get_redis
            redis = await get_redis()
            names = list(await redis.smembers(KEY_CANDIDATES))
            total = len(names)
            queue_len = await redis.zcard(KEY_UPDATE_QUEUE)

            if names:
                from fluxio.db.session import async_session_factory
                from fluxio.db.unit_of_work import UnitOfWork
                async with UnitOfWork(async_session_factory) as uow:
                    rows = []
                    for name in names[:100]:
                        item = await uow.items.get_by_name(name)
                        if not item:
                            continue
                        p = float(item.price_usd) if item.price_usd else 0.0
                        sp = float(item.steam_price_usd) if item.steam_price_usd else 0.0
                        discount = ""
                        profit = ""
                        if p > 0 and sp > 0:
                            fee = config.fees.steam_fee_percent / 100
                            net = sp * (1 - fee)
                            d = (net - p) / net * 100 if net > 0 else 0
                            discount = f"{d:.1f}%"
                            profit = f"${net - p:.4f}"
                        rows.append(
                            f"<tr>"
                            f"<td>{name}</td>"
                            f"<td>${p:.4f}</td>"
                            f"<td>${sp:.4f}</td>"
                            f"<td><strong style='color:#4caf50'>{discount}</strong></td>"
                            f"<td>{profit}</td>"
                            f"<td>{item.steam_volume_24h or '—'}</td>"
                            f"</tr>"
                        )
                    rows_html = "".join(rows)
        except Exception as e:
            rows_html = f"<tr><td colspan='6'>Ошибка: {e}</td></tr>"

        if not rows_html:
            rows_html = "<tr><td colspan='6' style='text-align:center;color:#8f98a0'>Кандидатов нет. Запусти Scanner + Updater.</td></tr>"

        return f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><title>Fluxio — Кандидаты</title>
<style>{_css()}</style></head>
<body>
<h1>Кандидаты на покупку</h1>
{_nav("/candidates")}
<p style="color:#8f98a0">
  Кандидатов: <strong style="color:#4caf50">{total}</strong> |
  В очереди обновления: {queue_len}
</p>
<table>
<thead><tr>
  <th>market_hash_name</th><th>Цена CS2DT</th><th>Steam медиана</th>
  <th>Дисконт</th><th>Прибыль</th><th>Volume 24h</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>
</body></html>"""

    # ─── Purchases ─────────────────────────────────────────────────────────────

    @app.get("/api/v1/purchases")
    async def api_purchases() -> JSONResponse:
        """История покупок (JSON)."""
        try:
            from fluxio.db.session import async_session_factory
            from fluxio.db.unit_of_work import UnitOfWork
            async with UnitOfWork(async_session_factory) as uow:
                purchases = await uow.purchases.get_all(limit=100)
                items = []
                for p in purchases:
                    items.append({
                        "id": p.id,
                        "product_id": p.product_id,
                        "order_id": p.order_id,
                        "market_hash_name": p.market_hash_name,
                        "price_usd": float(p.price_usd) if p.price_usd else None,
                        "steam_price_usd": float(p.steam_price_usd) if p.steam_price_usd else None,
                        "discount_percent": float(p.discount_percent) if p.discount_percent else None,
                        "status": p.status,
                        "dry_run": p.dry_run,
                        "purchased_at": p.purchased_at.isoformat() if p.purchased_at else None,
                        "delivered_at": p.delivered_at.isoformat() if p.delivered_at else None,
                    })
            return JSONResponse({"purchases": items, "total": len(items)})
        except Exception as e:
            return JSONResponse({"error": str(e), "purchases": [], "total": 0})

    @app.get("/purchases", response_class=HTMLResponse)
    async def user_purchases() -> str:
        """Страница истории покупок."""
        rows_html = ""
        total = 0
        total_spent = 0.0

        try:
            from fluxio.db.session import async_session_factory
            from fluxio.db.unit_of_work import UnitOfWork
            async with UnitOfWork(async_session_factory) as uow:
                purchases = await uow.purchases.get_all(limit=200)
                total = len(purchases)
                rows = []
                for p in purchases:
                    price = float(p.price_usd) if p.price_usd else 0
                    steam = float(p.steam_price_usd) if p.steam_price_usd else 0
                    disc = f"{float(p.discount_percent):.1f}%" if p.discount_percent else "—"
                    ts = p.purchased_at.strftime("%Y-%m-%d %H:%M") if p.purchased_at else "—"
                    delivered = p.delivered_at.strftime("%H:%M") if p.delivered_at else "—"

                    # Статус с цветом
                    status_colors = {
                        "success": "#4caf50",
                        "pending": "#ff9800",
                        "failed": "#f44336",
                        "cancelled": "#8f98a0",
                    }
                    status_color = status_colors.get(p.status, "#c7d5e0")
                    dry_badge = ' <span class="badge badge-warn">DRY</span>' if p.dry_run else ""

                    rows.append(
                        f"<tr>"
                        f"<td>{p.market_hash_name}</td>"
                        f"<td>${price:.4f}</td>"
                        f"<td>${steam:.4f}</td>"
                        f"<td>{disc}</td>"
                        f"<td><span style='color:{status_color}'>{p.status}</span>{dry_badge}</td>"
                        f"<td>{ts}</td>"
                        f"<td>{delivered}</td>"
                        f"</tr>"
                    )
                    if not p.dry_run and p.status in ("success", "pending"):
                        total_spent += price
                rows_html = "".join(rows)
        except Exception as e:
            rows_html = f"<tr><td colspan='7'>Ошибка: {e}</td></tr>"

        if not rows_html:
            rows_html = "<tr><td colspan='7' style='text-align:center;color:#8f98a0'>Покупок пока нет.</td></tr>"

        return f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><title>Fluxio — Покупки</title>
<style>{_css()}</style></head>
<body>
<h1>История покупок</h1>
{_nav("/purchases")}
<p style="color:#8f98a0">
  Всего: <strong>{total}</strong> |
  Потрачено (live): <strong style="color:#ff9800">${total_spent:.2f}</strong>
</p>
<table>
<thead><tr>
  <th>Предмет</th><th>Цена</th><th>Steam</th>
  <th>Дисконт</th><th>Статус</th><th>Дата</th><th>Доставка</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>
</body></html>"""

    # ─── Admin HTML ────────────────────────────────────────────────────────────

    @app.get("/admin/", response_class=HTMLResponse)
    @app.get("/admin", response_class=HTMLResponse)
    async def admin_index() -> str:
        """Системный дашборд — статус, воркеры, circuit breaker."""
        uptime = int((datetime.now(timezone.utc) - _started_at).total_seconds())
        hours, rem = divmod(uptime, 3600)
        minutes, seconds = divmod(rem, 60)

        mode = "DRY RUN" if config.trading.dry_run else "БОЕВОЙ"
        mode_color = "#4caf50" if config.trading.dry_run else "#f44336"

        # Circuit Breaker статус
        cb_html = ""
        if _container and _container.is_initialized(CircuitBreaker):
            cb = await _container.get(CircuitBreaker)
            st = cb.status()
            state_color = {"closed": "#4caf50", "open": "#f44336", "half_open": "#ff9800"}
            cb_html = f"""<div class="card">
              <h2>Circuit Breaker: {st['name']}</h2>
              <div class="metric"><span class="label">Состояние:</span>
                <span style="color:{state_color.get(st['state'], '#fff')}">{st['state'].upper()}</span></div>
              <div class="metric"><span class="label">Ошибок подряд:</span> {st['failure_count']} / {st['threshold']}</div>
              <div class="metric"><span class="label">Всего запросов:</span> {st['total_calls']}</div>
            </div>"""

        # Статус воркеров
        workers_html = ""
        if _container:
            from fluxio.core.workers.buyer import BuyerWorker as _BuyerW
            from fluxio.core.workers.order_tracker import OrderTrackerWorker as _TrackerW
            from fluxio.core.workers.scanner import ScannerWorker
            from fluxio.core.workers.updater import UpdaterWorker

            for worker_type in (ScannerWorker, UpdaterWorker, _BuyerW, _TrackerW):
                if _container.is_initialized(worker_type):
                    w = await _container.get(worker_type)
                    st = w.status
                    color = "#4caf50" if st.running else "#f44336"
                    last = st.last_run_at.strftime("%H:%M:%S") if st.last_run_at else "—"
                    err = f'<div class="metric" style="color:#f44336">Ошибка: {st.last_error}</div>' if st.last_error else ""
                    workers_html += f"""<div class="card">
                      <h2>Воркер: {st.name}</h2>
                      <div class="metric"><span class="label">Статус:</span>
                        <span style="color:{color}">{"работает" if st.running else "остановлен"}</span></div>
                      <div class="metric"><span class="label">Циклов:</span> {st.cycles}</div>
                      <div class="metric"><span class="label">Обработано:</span> {st.items_processed}</div>
                      <div class="metric"><span class="label">Последний запуск:</span> {last}</div>
                      {err}
                    </div>"""

        if not workers_html:
            workers_html = '<div class="card"><h2>Воркеры</h2><div class="metric" style="color:#8f98a0">Не инициализированы</div></div>'

        # Redis метрики
        redis_html = ""
        try:
            from fluxio.utils.redis_client import KEY_CANDIDATES, KEY_UPDATE_QUEUE, get_redis
            redis = await get_redis()
            queue_len = await redis.zcard(KEY_UPDATE_QUEUE)
            cand_count = await redis.scard(KEY_CANDIDATES)
            redis_html = f"""<div class="card">
              <h2>Redis</h2>
              <div class="metric"><span class="label">steam:update_queue:</span> {queue_len}</div>
              <div class="metric"><span class="label">arb:candidates:</span> {cand_count}</div>
            </div>"""
        except Exception as e:
            redis_html = f'<div class="card"><h2>Redis</h2><div class="metric" style="color:#f44336">Ошибка: {e}</div></div>'

        return f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fluxio — Admin</title><style>{_css()}</style></head>
<body>
<h1>Fluxio — Admin</h1>
<nav>
  <a href="/admin/">Статус</a>
  <a href="/admin/logs">Логи (SSE)</a>
  <a href="/">User Dashboard</a>
  <a href="/api/v1/status">API Status</a>
  <a href="/health">Health</a>
</nav>
<div class="grid">
  <div class="card">
    <h2>Система</h2>
    <div class="metric"><span class="label">Режим:</span>
      <span class="status" style="color:{mode_color}">{mode}</span></div>
    <div class="metric"><span class="label">Аптайм:</span> {hours}ч {minutes}м {seconds}с</div>
    <div class="metric"><span class="label">Дисконт:</span> ≥{config.trading.min_discount_percent}%</div>
    <div class="metric"><span class="label">Дневной лимит:</span> ${config.trading.daily_limit_usd}</div>
    <div class="metric"><span class="label">Цена:</span> ${config.trading.min_price_usd} – ${config.trading.max_price_usd}</div>
  </div>
  {cb_html}
  {redis_html}
  {workers_html}
</div>
</body></html>"""

    @app.get("/admin/logs", response_class=HTMLResponse)
    async def admin_logs() -> str:
        """Страница SSE логов в реальном времени."""
        return """<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fluxio — Логи</title>
<style>
  body { font-family: 'Consolas', 'Courier New', monospace; background: #0f1923; color: #c7d5e0;
         margin: 0; padding: 20px; }
  h1 { color: #66c0f4; font-family: 'Segoe UI', sans-serif; }
  nav { margin-bottom: 20px; font-family: 'Segoe UI', sans-serif; }
  nav a { color: #66c0f4; margin-right: 16px; text-decoration: none; }
  #logs { background: #1b2838; border: 1px solid #2a475e; border-radius: 8px;
          padding: 16px; height: 75vh; overflow-y: auto; font-size: 13px; line-height: 1.5; }
  .log-line { white-space: pre-wrap; word-break: break-all; }
  .ERROR { color: #f44336; } .WARNING { color: #ff9800; }
  .INFO { color: #4caf50; } .DEBUG { color: #8f98a0; }
  #status { color: #8f98a0; margin-bottom: 10px; font-family: 'Segoe UI', sans-serif; }
</style></head>
<body>
<h1>Fluxio — Логи</h1>
<nav><a href="/admin/">← Admin</a><a href="/">User</a></nav>
<div id="status">Подключение...</div>
<div id="logs"></div>
<script>
const logsDiv = document.getElementById('logs');
const statusDiv = document.getElementById('status');
let lineCount = 0;
function getLogClass(text) {
  if (text.includes('| ERROR |')) return 'ERROR';
  if (text.includes('| WARNING |')) return 'WARNING';
  if (text.includes('| INFO |')) return 'INFO';
  if (text.includes('| DEBUG |')) return 'DEBUG';
  return '';
}
function connect() {
  const es = new EventSource('/sse/logs');
  es.onopen = () => { statusDiv.textContent = 'Подключено (SSE)'; };
  es.onmessage = (e) => {
    const div = document.createElement('div');
    div.className = 'log-line ' + getLogClass(e.data);
    div.textContent = e.data;
    logsDiv.appendChild(div);
    lineCount++;
    if (lineCount > 1000) { logsDiv.removeChild(logsDiv.firstChild); lineCount--; }
    logsDiv.scrollTop = logsDiv.scrollHeight;
  };
  es.onerror = () => {
    statusDiv.textContent = 'Отключено. Переподключение...';
    es.close();
    setTimeout(connect, 3000);
  };
}
connect();
</script></body></html>"""

    return app


# Обратная совместимость — app для uvicorn без контейнера
app = create_app()
