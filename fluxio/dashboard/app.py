"""Веб-дашборд Fluxio (FastAPI) — admin + health + SSE логи.

Фаза 1: /health, /admin/, /admin/logs (SSE), /api/v1/status
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


def create_app(container: ServiceContainer | None = None) -> FastAPI:
    """Создать FastAPI приложение с DI-контейнером."""
    global _container
    _container = container

    app = FastAPI(title="Fluxio Dashboard", version="1.0.0")

    # --- Health ---

    @app.get("/health")
    async def health() -> JSONResponse:
        """Health check для Docker healthcheck."""
        uptime = int((datetime.now(timezone.utc) - _started_at).total_seconds())
        return JSONResponse({
            "status": "ok",
            "started_at": _started_at.isoformat(),
            "uptime_seconds": uptime,
        })

    # --- API v1 ---

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

        # Статус circuit breaker если доступен
        if _container and _container.is_initialized(CircuitBreaker):
            cb = await _container.get(CircuitBreaker)
            data["circuit_breaker"] = cb.status()

        return JSONResponse(data)

    @app.get("/api/v1/workers")
    async def api_workers() -> JSONResponse:
        """Статус воркеров (заглушка для Фазы 2)."""
        return JSONResponse({
            "workers": [
                {"name": "scanner", "status": "не реализован", "last_run": None},
                {"name": "updater", "status": "не реализован", "last_run": None},
                {"name": "buyer", "status": "не реализован", "last_run": None},
                {"name": "order_tracker", "status": "не реализован", "last_run": None},
            ]
        })

    # --- SSE логи ---

    @app.get("/sse/logs")
    async def sse_logs(request: Request) -> StreamingResponse:
        """Server-Sent Events: realtime логи."""
        queue = subscribe_logs()

        async def event_generator():
            try:
                # Сначала отправить буфер
                for line in get_log_buffer():
                    yield f"data: {line}\n\n"

                # Потом стримить новые
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

    # --- Admin HTML ---

    @app.get("/admin/", response_class=HTMLResponse)
    @app.get("/admin", response_class=HTMLResponse)
    async def admin_index() -> str:
        """Системный дашборд — статус, воркеры, circuit breaker."""
        uptime = int((datetime.now(timezone.utc) - _started_at).total_seconds())
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)

        mode = "DRY RUN" if config.trading.dry_run else "БОЕВОЙ"
        mode_color = "#4caf50" if config.trading.dry_run else "#f44336"

        cb_html = ""
        if _container and _container.is_initialized(CircuitBreaker):
            cb = await _container.get(CircuitBreaker)
            st = cb.status()
            state_color = {"closed": "#4caf50", "open": "#f44336", "half_open": "#ff9800"}
            cb_html = f"""
            <div class="card">
              <h2>Circuit Breaker: {st['name']}</h2>
              <div class="metric">
                <span class="label">Состояние:</span>
                <span style="color: {state_color.get(st['state'], '#fff')}">{st['state'].upper()}</span>
              </div>
              <div class="metric">
                <span class="label">Ошибок подряд:</span> {st['failure_count']} / {st['threshold']}
              </div>
              <div class="metric">
                <span class="label">Всего запросов:</span> {st['total_calls']}
              </div>
            </div>"""

        return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fluxio — Admin</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: #0f1923; color: #c7d5e0;
         margin: 0; padding: 20px; }}
  h1 {{ color: #66c0f4; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }}
  .card {{ background: #1b2838; border-radius: 8px; padding: 20px; border: 1px solid #2a475e; }}
  .card h2 {{ color: #66c0f4; margin: 0 0 12px; font-size: 16px; }}
  .metric {{ margin: 6px 0; }}
  .label {{ color: #8f98a0; }}
  .status {{ font-size: 20px; font-weight: 600; }}
  nav {{ margin-bottom: 20px; }}
  nav a {{ color: #66c0f4; margin-right: 16px; text-decoration: none; }}
  nav a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<h1>Fluxio — Admin</h1>
<nav>
  <a href="/admin/">Статус</a>
  <a href="/admin/logs">Логи (SSE)</a>
  <a href="/api/v1/status">API Status</a>
  <a href="/api/v1/workers">API Workers</a>
  <a href="/health">Health</a>
</nav>
<div class="grid">
  <div class="card">
    <h2>Система</h2>
    <div class="metric">
      <span class="label">Режим:</span>
      <span class="status" style="color: {mode_color}">{mode}</span>
    </div>
    <div class="metric">
      <span class="label">Аптайм:</span> {hours}ч {minutes}м {seconds}с
    </div>
    <div class="metric">
      <span class="label">Дисконт:</span> ≥{config.trading.min_discount_percent}%
    </div>
    <div class="metric">
      <span class="label">Дневной лимит:</span> ${config.trading.daily_limit_usd}
    </div>
    <div class="metric">
      <span class="label">Цена:</span> ${config.trading.min_price_usd} – ${config.trading.max_price_usd}
    </div>
  </div>
  {cb_html}
  <div class="card">
    <h2>Воркеры</h2>
    <div class="metric"><span class="label">Scanner:</span> ожидает Фазу 2</div>
    <div class="metric"><span class="label">Updater:</span> ожидает Фазу 2</div>
    <div class="metric"><span class="label">Buyer:</span> ожидает Фазу 3</div>
    <div class="metric"><span class="label">OrderTracker:</span> ожидает Фазу 3</div>
  </div>
</div>
</body>
</html>"""

    @app.get("/admin/logs", response_class=HTMLResponse)
    async def admin_logs() -> str:
        """Страница SSE логов в реальном времени."""
        return """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fluxio — Логи</title>
<style>
  body { font-family: 'Consolas', 'Courier New', monospace; background: #0f1923; color: #c7d5e0;
         margin: 0; padding: 20px; }
  h1 { color: #66c0f4; font-family: 'Segoe UI', sans-serif; }
  nav { margin-bottom: 20px; font-family: 'Segoe UI', sans-serif; }
  nav a { color: #66c0f4; margin-right: 16px; text-decoration: none; }
  #logs { background: #1b2838; border: 1px solid #2a475e; border-radius: 8px;
          padding: 16px; height: 75vh; overflow-y: auto; font-size: 13px;
          line-height: 1.5; }
  .log-line { white-space: pre-wrap; word-break: break-all; }
  .ERROR { color: #f44336; }
  .WARNING { color: #ff9800; }
  .INFO { color: #4caf50; }
  .DEBUG { color: #8f98a0; }
  #status { color: #8f98a0; margin-bottom: 10px; font-family: 'Segoe UI', sans-serif; }
</style>
</head>
<body>
<h1>Fluxio — Логи</h1>
<nav>
  <a href="/admin/">← Admin</a>
</nav>
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
</script>
</body>
</html>"""

    return app


# Обратная совместимость — app для uvicorn без контейнера
app = create_app()
