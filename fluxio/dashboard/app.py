"""Веб-дашборд (FastAPI) — заглушка для Фазы 0.

Полная реализация дашборда (admin + user) — Фаза 1.
Сейчас: /health endpoint + минимальная страница статуса.

Запуск:
    uvicorn bot.dashboard.app:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Fluxio Dashboard", version="0.3.0")

# Время запуска для health check
_started_at = datetime.now(timezone.utc)


@app.get("/health")
async def health() -> JSONResponse:
    """Health check для Docker healthcheck."""
    return JSONResponse({
        "status": "ok",
        "started_at": _started_at.isoformat(),
        "uptime_seconds": int((datetime.now(timezone.utc) - _started_at).total_seconds()),
    })


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    """Минимальная страница статуса."""
    uptime = int((datetime.now(timezone.utc) - _started_at).total_seconds())
    hours, remainder = divmod(uptime, 3600)
    minutes, seconds = divmod(remainder, 60)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fluxio — Статус</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: #0f1923; color: #c7d5e0;
         display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
  .card {{ background: #1b2838; border-radius: 8px; padding: 40px; text-align: center;
           border: 1px solid #2a475e; }}
  h1 {{ color: #66c0f4; margin: 0 0 16px; }}
  .status {{ color: #4caf50; font-size: 24px; font-weight: 600; }}
  .uptime {{ color: #8f98a0; margin-top: 12px; }}
  .info {{ color: #8f98a0; margin-top: 8px; font-size: 14px; }}
</style>
</head>
<body>
<div class="card">
  <h1>Fluxio</h1>
  <div class="status">Работает</div>
  <div class="uptime">Аптайм: {hours}ч {minutes}м {seconds}с</div>
  <div class="info">Полный дашборд — Фаза 1</div>
</div>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
