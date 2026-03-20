"""Веб-дашборд Fluxio (FastAPI) — admin + user + health + SSE логи.

Фаза 1: /health, /admin/, /admin/logs (SSE), /api/v1/status
Фаза 2: /items, /candidates, /api/v1/workers (реальный статус), /api/v1/candidates
Фаза 3: /purchases, /api/v1/purchases
"""

from __future__ import annotations

import asyncio
import html as html_mod
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.responses import StreamingResponse

from fluxio.config import config
from fluxio.services.container import ServiceContainer
from fluxio.utils.circuit_breaker import CircuitBreaker
from fluxio.utils.logger import get_log_buffer, subscribe_logs, unsubscribe_logs
from loguru import logger


def _esc(value: object) -> str:
    """Экранировать значение для безопасной вставки в HTML."""
    return html_mod.escape(str(value))

# Время запуска
_started_at = datetime.now(timezone.utc)

# Ссылка на контейнер (устанавливается при create_app)
_container: ServiceContainer | None = None


# ─── Дизайн-система ─────────────────────────────────────────────────────────


def _css() -> str:
    """Полная CSS дизайн-система Fluxio — адаптивная, TG Mini App ready."""
    return """
/* ═══════════════════════════════════════════════════════════════════════════
   Design Tokens
   ═══════════════════════════════════════════════════════════════════════════ */
:root {
  /* Фон */
  --bg: #080b14;
  --bg-surface: #0f1219;
  --bg-elevated: #161b27;
  --bg-hover: #1c2233;
  --bg-sidebar: #0a0e17;

  /* Границы */
  --border: #1a1f2e;
  --border-light: #252d3d;

  /* Текст */
  --text: #f1f5f9;
  --text-secondary: #94a3b8;
  --text-muted: #64748b;

  /* Акценты */
  --accent: #3b82f6;
  --accent-hover: #2563eb;
  --accent-glow: rgba(59, 130, 246, 0.15);
  --accent-gradient: linear-gradient(135deg, #3b82f6, #8b5cf6);

  /* Статусы */
  --success: #22c55e;
  --success-bg: rgba(34, 197, 94, 0.1);
  --danger: #ef4444;
  --danger-bg: rgba(239, 68, 68, 0.1);
  --warning: #f59e0b;
  --warning-bg: rgba(245, 158, 11, 0.1);

  /* Layout */
  --sidebar-w: 240px;
  --topbar-h: 60px;
  --mobile-nav-h: 64px;
  --radius: 12px;
  --radius-sm: 8px;
  --radius-xs: 6px;

  /* Шрифт */
  --font: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  --font-mono: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;

  /* Тени */
  --shadow: 0 1px 3px rgba(0,0,0,0.3), 0 1px 2px rgba(0,0,0,0.2);
  --shadow-lg: 0 4px 12px rgba(0,0,0,0.4);

  /* Telegram Mini App — CSS-переменные перезапишут, если запущено в TG */
  --tg-viewport-height: 100dvh;
}

/* ═══════════════════════════════════════════════════════════════════════════
   Reset & Base
   ═══════════════════════════════════════════════════════════════════════════ */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html {
  font-family: var(--font);
  font-size: 14px;
  color: var(--text);
  background: var(--bg);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

body { background: var(--bg); min-height: 100dvh; overflow-x: hidden; }

a { color: var(--accent); text-decoration: none; }
a:hover { color: var(--text); }

/* ═══════════════════════════════════════════════════════════════════════════
   App Layout
   ═══════════════════════════════════════════════════════════════════════════ */
.app {
  display: flex;
  min-height: 100dvh;
}

/* ─── Sidebar ─────────────────────────────────────────────────────────── */
.sidebar {
  position: fixed;
  top: 0; left: 0;
  width: var(--sidebar-w);
  height: 100dvh;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  z-index: 100;
  transition: transform 0.25s ease;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.sidebar-logo {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 20px 20px 16px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

.sidebar-logo img { flex-shrink: 0; width: 32px; height: 32px; object-fit: contain; }

.sidebar-logo span {
  font-size: 20px;
  font-weight: 700;
  letter-spacing: -0.3px;
  color: var(--text);
}

.sidebar-section {
  padding: 16px 12px 8px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.8px;
  color: var(--text-muted);
}

.sidebar-nav { display: flex; flex-direction: column; gap: 2px; padding: 0 8px; flex: 1; }

.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  font-size: 14px;
  font-weight: 500;
  transition: all 0.15s;
  cursor: pointer;
  -webkit-tap-highlight-color: transparent;
  min-height: 40px;
}

.nav-item:hover { background: var(--bg-hover); color: var(--text); }

.nav-item.active {
  background: var(--accent-glow);
  color: var(--accent);
  font-weight: 600;
}

.nav-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  width: 3px;
  height: 24px;
  background: var(--accent);
  border-radius: 0 3px 3px 0;
}

.nav-item { position: relative; }

.nav-item i, .nav-item svg { width: 20px; height: 20px; flex-shrink: 0; }

.nav-spacer { flex: 1; }

/* ─── Top Bar ─────────────────────────────────────────────────────────── */
.main-area {
  flex: 1;
  margin-left: var(--sidebar-w);
  display: flex;
  flex-direction: column;
  min-height: 100dvh;
}

.topbar {
  position: sticky;
  top: 0;
  height: var(--topbar-h);
  background: rgba(8, 11, 20, 0.85);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 24px;
  gap: 16px;
  z-index: 50;
  flex-shrink: 0;
}

.topbar-search {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 8px 14px;
  min-width: 260px;
  max-width: 400px;
  flex: 1;
}

.topbar-search i { color: var(--text-muted); width: 18px; height: 18px; }

.topbar-search input {
  background: none;
  border: none;
  outline: none;
  color: var(--text);
  font-size: 13px;
  font-family: var(--font);
  width: 100%;
}

.topbar-search input::placeholder { color: var(--text-muted); }

.topbar-spacer { flex: 1; }

.topbar-icons { display: flex; align-items: center; gap: 6px; }

.topbar-icon {
  width: 36px; height: 36px;
  display: flex; align-items: center; justify-content: center;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.15s;
}

.topbar-icon:hover { background: var(--bg-hover); color: var(--text); }

.topbar-icon i, .topbar-icon svg { width: 18px; height: 18px; }

.mode-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-radius: 20px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  user-select: none;
  transition: all 0.15s;
  white-space: nowrap;
  flex-shrink: 0;
}

.mode-live { background: var(--danger-bg); color: var(--danger); border: 1px solid rgba(239,68,68,0.3); }
.mode-dry { background: var(--success-bg); color: var(--success); border: 1px solid rgba(34,197,94,0.3); }

/* Burger (мобильный) */
.burger {
  display: none;
  width: 40px; height: 40px;
  align-items: center; justify-content: center;
  border-radius: var(--radius-sm);
  color: var(--text);
  cursor: pointer;
  -webkit-tap-highlight-color: transparent;
}

/* ─── Content ─────────────────────────────────────────────────────────── */
.content {
  flex: 1;
  padding: 24px;
  max-width: 1400px;
  width: 100%;
  margin: 0 auto;
}

.page-title {
  font-size: 22px;
  font-weight: 700;
  color: var(--text);
  margin-bottom: 20px;
}

/* ─── Overlay (мобильный sidebar) ─────────────────────────────────────── */
.sidebar-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.6);
  z-index: 99;
}

/* ─── Mobile Bottom Nav ───────────────────────────────────────────────── */
.mobile-nav {
  display: none;
  position: fixed;
  bottom: 0; left: 0; right: 0;
  height: var(--mobile-nav-h);
  padding-bottom: env(safe-area-inset-bottom, 0);
  background: var(--bg-sidebar);
  border-top: 1px solid var(--border);
  z-index: 100;
  justify-content: space-around;
  align-items: center;
}

.mobile-nav-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 8px 12px;
  color: var(--text-muted);
  font-size: 10px;
  font-weight: 500;
  text-decoration: none;
  -webkit-tap-highlight-color: transparent;
  min-width: 56px;
}

.mobile-nav-item.active { color: var(--accent); }
.mobile-nav-item i { width: 22px; height: 22px; }

/* ═══════════════════════════════════════════════════════════════════════════
   UI Components
   ═══════════════════════════════════════════════════════════════════════════ */

/* ─── Cards ───────────────────────────────────────────────────────────── */
.grid { display: grid; gap: 16px; }
.grid-2 { grid-template-columns: repeat(2, 1fr); }
.grid-3 { grid-template-columns: repeat(3, 1fr); }
.grid-4 { grid-template-columns: repeat(4, 1fr); }
.grid-auto { grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }

.card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.card-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-secondary);
}

.card-value {
  font-size: 32px;
  font-weight: 700;
  color: var(--text);
  line-height: 1.1;
}

.card-sub {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 6px;
}

/* Stat Card (компактная) */
.stat-card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.stat-label { font-size: 12px; color: var(--text-muted); font-weight: 500; }
.stat-value { font-size: 24px; font-weight: 700; line-height: 1; }
.stat-sub { font-size: 12px; color: var(--text-muted); }
.progress-bar { height: 6px; background: var(--bg-elevated); border-radius: 3px; margin-top: 8px; overflow: hidden; }
.progress-fill { height: 100%; border-radius: 3px; background: linear-gradient(90deg, var(--success), #4ade80); transition: width 0.6s ease; }
.chart-container { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }
.chart-container canvas { max-height: 260px; }
.chart-title { font-size: 14px; font-weight: 600; color: var(--text-primary); margin-bottom: 12px; }

/* ─── Tables ──────────────────────────────────────────────────────────── */
.table-wrap {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}

.table-wrap table { width: 100%; border-collapse: collapse; }

.table-wrap thead th {
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  padding: 12px 16px;
  text-align: left;
  white-space: nowrap;
  border-bottom: 1px solid var(--border);
}

.table-wrap tbody td {
  padding: 10px 16px;
  font-size: 13px;
  color: var(--text);
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}

.table-wrap tbody tr:last-child td { border-bottom: none; }
.table-wrap tbody tr:hover td { background: var(--bg-hover); }

/* Sortable */
th.sortable { cursor: pointer; user-select: none; }
th.sortable:hover { color: var(--text); }
th.sortable::after { content: ' \2195'; opacity: 0.3; font-size: 11px; }
th.sort-desc::after { content: ' \2193'; opacity: 1; }
th.sort-asc::after { content: ' \2191'; opacity: 1; }

/* Scrollable table на мобильных */
.table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }

/* ─── Badges ──────────────────────────────────────────────────────────── */
.badge {
  display: inline-flex;
  align-items: center;
  padding: 3px 10px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.3px;
}

.badge-success { background: var(--success-bg); color: var(--success); }
.badge-danger { background: var(--danger-bg); color: var(--danger); }
.badge-warning { background: var(--warning-bg); color: var(--warning); }
.badge-info { background: var(--accent-glow); color: var(--accent); }
.badge-muted { background: rgba(100,116,139,0.15); color: var(--text-muted); }

/* ─── Buttons ─────────────────────────────────────────────────────────── */
.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border-radius: var(--radius-xs);
  border: none;
  font-size: 13px;
  font-weight: 600;
  font-family: var(--font);
  cursor: pointer;
  transition: all 0.15s;
  -webkit-tap-highlight-color: transparent;
  min-height: 36px;
}

.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover { background: var(--accent-hover); }

.btn-success { background: var(--success); color: #fff; }
.btn-success:hover { opacity: 0.9; }

.btn-danger { background: var(--danger-bg); color: var(--danger); border: 1px solid rgba(239,68,68,0.3); }
.btn-danger:hover { background: var(--danger); color: #fff; }

.btn-ghost { background: transparent; color: var(--text-secondary); border: 1px solid var(--border); }
.btn-ghost:hover { background: var(--bg-hover); color: var(--text); }

/* ─── Tabs ────────────────────────────────────────────────────────────── */
.tabs {
  display: flex;
  gap: 0;
  margin-bottom: 16px;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}

.tab {
  padding: 8px 18px;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  color: var(--text-muted);
  cursor: pointer;
  font-size: 13px;
  font-weight: 500;
  transition: all 0.15s;
  user-select: none;
  white-space: nowrap;
  -webkit-tap-highlight-color: transparent;
  min-height: 38px;
}

.tab:first-child { border-radius: var(--radius-xs) 0 0 var(--radius-xs); }
.tab:last-child { border-radius: 0 var(--radius-xs) var(--radius-xs) 0; }
.tab:not(:last-child) { border-right: none; }
.tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.tab:hover:not(.active) { background: var(--bg-hover); color: var(--text); }

.tab .count {
  font-size: 10px;
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 10px;
  background: rgba(255,255,255,0.1);
}

.tab.active .count { background: rgba(255,255,255,0.2); }

/* ─── Pill Toggles ────────────────────────────────────────────────────── */
.pills { display: flex; gap: 8px; flex-wrap: wrap; }

.pill {
  padding: 6px 14px;
  border-radius: 20px;
  border: 1px solid var(--border);
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}

.pill.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.pill:hover:not(.active) { border-color: var(--border-light); color: var(--text); }

/* ─── Metric Row ──────────────────────────────────────────────────────── */
.metric-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 0;
}

.metric-row + .metric-row { border-top: 1px solid var(--border); }
.metric-label { color: var(--text-muted); font-size: 13px; }
.metric-value { color: var(--text); font-weight: 600; font-size: 13px; }

/* ─── Charts Container ────────────────────────────────────────────────── */
.chart-card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
}

.chart-card canvas { max-height: 260px; }

/* ─── Worker Card ─────────────────────────────────────────────────────── */
.worker-card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px;
  display: flex;
  align-items: center;
  gap: 14px;
}

.worker-dot {
  width: 10px; height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}

.worker-dot.running { background: var(--success); box-shadow: 0 0 8px var(--success); }
.worker-dot.paused  { background: var(--warning); box-shadow: 0 0 8px var(--warning); }
.worker-dot.stopped { background: var(--danger); }

.worker-info { flex: 1; min-width: 0; }
.worker-name { font-weight: 600; font-size: 14px; }
.worker-meta { font-size: 12px; color: var(--text-muted); margin-top: 2px; }

.worker-btn {
  background: var(--bg-hover);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-secondary);
  cursor: pointer;
  padding: 6px 10px;
  font-size: 12px;
  transition: all 0.15s;
  flex-shrink: 0;
}
.worker-btn:hover { background: var(--bg-elevated); color: var(--text-primary); border-color: var(--border-light); }

/* ─── Item Card (Catalog) ─────────────────────────────────────────────── */
.item-card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  transition: border-color 0.15s;
}

.item-card:hover { border-color: var(--border-light); }

.item-img {
  width: 100%;
  height: 120px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-elevated);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.item-img img { max-width: 100%; max-height: 100%; object-fit: contain; }

.item-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.item-rarity { font-size: 11px; color: var(--text-muted); }

.item-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: auto;
}

.item-price { font-size: 16px; font-weight: 700; color: var(--text); }

.btn-buy {
  padding: 6px 16px;
  border-radius: var(--radius-xs);
  background: var(--success);
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  border: none;
  cursor: pointer;
}

.steam-icon-link {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  transition: background .15s, border-color .15s;
  text-decoration: none;
}
.steam-icon-link:hover {
  background: #1b2838;
  border-color: #66c0f4;
}
.steam-icon-link:hover svg { fill: #66c0f4; }
.steam-icon-link svg { transition: fill .15s; }

/* ─── Filter Panel ────────────────────────────────────────────────────── */
.filter-panel {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
}

.filter-panel h3 {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 12px;
}

.filter-group { margin-bottom: 16px; }

.filter-label {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 8px;
}

input[type="range"] {
  width: 100%;
  -webkit-appearance: none;
  height: 4px;
  background: var(--border);
  border-radius: 2px;
  outline: none;
}

input[type="range"]::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 16px; height: 16px;
  background: var(--accent);
  border-radius: 50%;
  cursor: pointer;
}

/* ─── Log Viewer ──────────────────────────────────────────────────────── */
.log-container {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px;
  height: calc(100dvh - var(--topbar-h) - 160px);
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
}

.log-line { white-space: pre-wrap; word-break: break-all; padding: 1px 0; }
.log-ERROR { color: var(--danger); }
.log-WARNING { color: var(--warning); }
.log-INFO { color: var(--success); }
.log-DEBUG { color: var(--text-muted); }

.log-status {
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 10px;
}

/* ─── Empty State ─────────────────────────────────────────────────────── */
.empty {
  text-align: center;
  padding: 48px 24px;
  color: var(--text-muted);
}

.empty i { width: 48px; height: 48px; margin-bottom: 16px; opacity: 0.3; }
.empty p { font-size: 14px; }

/* ═══════════════════════════════════════════════════════════════════════════
   Responsive
   ═══════════════════════════════════════════════════════════════════════════ */

/* Планшет */
@media (max-width: 1024px) {
  .grid-4 { grid-template-columns: repeat(2, 1fr); }
  .grid-3 { grid-template-columns: repeat(2, 1fr); }
  .content { padding: 16px; }
}

/* Мобильный */
@media (max-width: 768px) {
  .sidebar { transform: translateX(-100%); }
  .sidebar.open { transform: translateX(0); }
  .sidebar-overlay.open { display: block; }

  .main-area { margin-left: 0; }

  .burger { display: flex; }

  .mobile-nav { display: flex; }

  .content {
    padding: 16px 12px;
    padding-bottom: calc(var(--mobile-nav-h) + env(safe-area-inset-bottom, 0) + 16px);
  }

  .topbar-search { min-width: 0; flex: 0 1 160px; }
  .topbar-search input { font-size: 12px; }

  .grid-4, .grid-3, .grid-2 { grid-template-columns: 1fr; }
  .grid-auto { grid-template-columns: 1fr; }

  .page-title { font-size: 18px; }

  .stat-value { font-size: 20px; }

  .table-wrap { border-radius: var(--radius-sm); }

  .catalog-layout { grid-template-columns: 1fr !important; }
  .filter-panel { display: none; }
  .filter-panel.open { display: block; }

  .items-grid { grid-template-columns: repeat(2, 1fr) !important; }
}

/* Маленький мобильный / TG Mini App */
@media (max-width: 480px) {
  :root {
    --topbar-h: 52px;
    --radius: 10px;
  }

  html { font-size: 13px; }
  body { padding-top: env(safe-area-inset-top, 0); }

  .topbar { padding: 0 10px; gap: 8px; }
  .topbar-search { padding: 6px 10px; flex: 1 1 auto; }
  .topbar-icons { gap: 2px; }
  .topbar-icon { width: 32px; height: 32px; }
  .mode-toggle { padding: 4px 8px; font-size: 10px; }

  .content { padding: 12px 8px; }
  .items-grid { grid-template-columns: repeat(2, 1fr) !important; gap: 8px !important; }
}

@media (max-width: 360px) {
  .items-grid { grid-template-columns: 1fr !important; }
  .topbar-icons { display: none; }
}
"""


# ─── SVG Logo ────────────────────────────────────────────────────────────────


_LOGO_HTML = '<img src="/static/icon.png" alt="Fluxio" width="32" height="32">'


# ─── Layout Helpers ──────────────────────────────────────────────────────────


def _sidebar(current: str) -> str:
    """Боковая панель навигации."""
    nav_items = [
        ("admin", "/", "activity", "Service Status"),
        ("admin", "/admin/logs", "file-text", "Logs"),
        ("user", "/items", "grid-3x3", "Catalog"),
        ("user", "/candidates", "target", "Candidates"),
        ("user", "/purchases", "shopping-cart", "Purchases"),
        ("user", "/audit", "shield-check", "Audit"),
        ("user", "/price-map", "flame", "Price Map"),
    ]

    items_html = ""
    current_section = ""
    for section, href, icon, label in nav_items:
        if section != current_section:
            section_label = "Admin" if section == "admin" else "Trading"
            items_html += f'<div class="sidebar-section">{section_label}</div>'
            current_section = section
        active = " active" if href == current else ""
        items_html += (
            f'<a href="{href}" class="nav-item{active}">'
            f'<i data-lucide="{icon}"></i> {label}</a>'
        )

    return f"""<aside class="sidebar" id="sidebar">
  <div class="sidebar-logo">{_LOGO_HTML}<span>Fluxio</span></div>
  <nav class="sidebar-nav">{items_html}</nav>
</aside>
<div class="sidebar-overlay" id="sidebar-overlay" onclick="toggleSidebar()"></div>"""


def _topbar() -> str:
    """Верхняя панель."""
    mode = config.trading.dry_run
    mode_class = "mode-dry" if mode else "mode-live"
    mode_label = "DRY RUN" if mode else "LIVE"
    return f"""<header class="topbar">
  <div class="burger" onclick="toggleSidebar()"><i data-lucide="menu"></i></div>
  <div class="topbar-search">
    <i data-lucide="search"></i>
    <input type="text" placeholder="Search..." id="globalSearch">
  </div>
  <div class="topbar-spacer"></div>
  <div class="topbar-icons">
    <div class="topbar-icon"><i data-lucide="bell"></i></div>
    <div class="topbar-icon"><i data-lucide="message-square"></i></div>
    <div class="topbar-icon"><i data-lucide="bookmark"></i></div>
  </div>
  <div class="mode-toggle {mode_class}">{mode_label}</div>
</header>"""


def _mobile_nav(current: str) -> str:
    """Нижняя мобильная навигация."""
    items = [
        ("/", "activity", "Status"),
        ("/items", "grid-3x3", "Catalog"),
        ("/candidates", "target", "Candidates"),
        ("/purchases", "shopping-cart", "Purchases"),
        ("/admin/logs", "file-text", "Logs"),
    ]
    html = ""
    for href, icon, label in items:
        active = " active" if href == current else ""
        html += (
            f'<a href="{href}" class="mobile-nav-item{active}">'
            f'<i data-lucide="{icon}"></i>{label}</a>'
        )
    return f'<nav class="mobile-nav">{html}</nav>'


def _layout(title: str, current: str, content: str,
            extra_head: str = "", extra_css: str = "") -> str:
    """Обёртка всех страниц — sidebar + topbar + content + mobile nav."""
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Fluxio — {title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{_css()}{extra_css}</style>
{extra_head}
</head>
<body>
<div class="app">
{_sidebar(current)}
<div class="main-area">
{_topbar()}
<main class="content">
<h1 class="page-title">{title}</h1>
{content}
</main>
</div>
</div>
{_mobile_nav(current)}
<script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>
<script>
lucide.createIcons();
function toggleSidebar() {{
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('sidebar-overlay').classList.toggle('open');
}}
async function workerAction(name, action) {{
  try {{
    const res = await fetch(`/api/v1/workers/${{encodeURIComponent(name)}}/${{action}}`, {{
      method:'POST',
      headers: {{'X-Fluxio-CSRF': '1'}},
    }});
    if (res.ok) location.reload();
    else alert('Ошибка: ' + (await res.json()).error);
  }} catch(e) {{ alert('Ошибка: ' + e.message); }}
}}
document.addEventListener('click', function(e) {{
  const btn = e.target.closest('.worker-btn[data-worker]');
  if (btn) workerAction(btn.dataset.worker, btn.dataset.action);
}});
</script>
</body>
</html>"""


# ─── App Factory ─────────────────────────────────────────────────────────────


def create_app(container: ServiceContainer | None = None) -> FastAPI:
    """Создать FastAPI приложение с DI-контейнером."""
    global _container
    _container = container

    app = FastAPI(title="Fluxio Dashboard", version="2.0.0")
    app.state.container = container

    # ─── CSRF-защита для POST-запросов ───────────────────────────────────────
    from starlette.middleware.base import BaseHTTPMiddleware

    class CSRFMiddleware(BaseHTTPMiddleware):
        """Проверяет наличие заголовка X-Fluxio-CSRF на всех POST-запросах.

        Браузер не отправляет custom-заголовки в cross-origin запросах
        без CORS preflight, что делает CSRF-атаку невозможной.
        """

        async def dispatch(self, request: Request, call_next):  # type: ignore[override]
            if request.method in ("POST", "PUT", "DELETE", "PATCH"):
                csrf_header = request.headers.get("X-Fluxio-CSRF")
                if not csrf_header:
                    return JSONResponse(
                        {"error": "Отсутствует CSRF-заголовок"},
                        status_code=403,
                    )
            return await call_next(request)

    app.add_middleware(CSRFMiddleware)

    # ─── Manual Buy Router ──────────────────────────────────────────────────────
    from fluxio.dashboard.manual_buy import router as manual_buy_router
    app.include_router(manual_buy_router)

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

    # ─── Static files ─────────────────────────────────────────────────────────

    from pathlib import Path
    from fastapi.responses import FileResponse

    _static_dir = Path(__file__).parent / "static"

    @app.get("/static/{filename}")
    async def static_file(filename: str) -> FileResponse:
        """Отдача статических файлов (иконки и т.д.)."""
        path = _static_dir / filename
        if not path.exists() or not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        media_types = {".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
        media = media_types.get(path.suffix, "application/octet-stream")
        return FileResponse(path, media_type=media, headers={"Cache-Control": "public, max-age=86400"})

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

    # ─── Хелпер: поиск воркера по имени ──────────────────────────────────────

    async def _find_worker(name: str) -> Any:
        """Найти воркер по имени в контейнере."""
        if not _container:
            return None
        from fluxio.core.workers.buyer import BuyerWorker
        from fluxio.core.workers.enricher import EnricherWorker
        from fluxio.core.workers.order_tracker import OrderTrackerWorker
        from fluxio.core.workers.scanner import ScannerWorker
        from fluxio.core.workers.updater import UpdaterWorker

        _worker_map: dict[str, type] = {
            "scanner": ScannerWorker,
            "updater": UpdaterWorker,
            "enricher": EnricherWorker,
            "buyer": BuyerWorker,
            "order_tracker": OrderTrackerWorker,
        }
        wtype = _worker_map.get(name)
        if wtype and _container.is_initialized(wtype):
            return await _container.get(wtype)
        return None

    @app.get("/api/v1/workers")
    async def api_workers() -> JSONResponse:
        """Статус воркеров — реальное состояние из объектов воркеров."""
        workers: list[dict[str, Any]] = []

        if _container:
            from fluxio.core.workers.buyer import BuyerWorker
            from fluxio.core.workers.enricher import EnricherWorker
            from fluxio.core.workers.order_tracker import OrderTrackerWorker
            from fluxio.core.workers.scanner import ScannerWorker
            from fluxio.core.workers.updater import UpdaterWorker

            for worker_type in (ScannerWorker, UpdaterWorker, EnricherWorker, BuyerWorker, OrderTrackerWorker):
                if _container.is_initialized(worker_type):
                    worker = await _container.get(worker_type)
                    st = worker.status.to_dict()
                    # Добавляем метрики сканера
                    if worker_type is ScannerWorker and worker.last_result:
                        st["last_scan"] = {
                            "items_upserted": worker.last_result.items_upserted,
                            "pages_fetched": worker.last_result.pages_fetched,
                            "duration_sec": round(worker.last_result.duration_sec, 1),
                        }
                    workers.append(st)

        if not workers:
            workers = [
                {"name": "scanner", "running": False, "last_run_at": None},
                {"name": "updater", "running": False, "last_run_at": None},
                {"name": "enricher", "running": False, "last_run_at": None},
                {"name": "buyer", "running": False, "last_run_at": None},
                {"name": "order_tracker", "running": False, "last_run_at": None},
            ]

        return JSONResponse({"workers": workers})

    @app.post("/api/v1/workers/{worker_name}/pause")
    async def api_worker_pause(worker_name: str) -> JSONResponse:
        """Поставить воркер на паузу."""
        worker = await _find_worker(worker_name)
        if worker is None:
            return JSONResponse({"error": f"Воркер '{worker_name}' не найден"}, status_code=404)
        worker.pause()
        return JSONResponse({"ok": True, "name": worker_name, "paused": True})

    @app.post("/api/v1/workers/{worker_name}/resume")
    async def api_worker_resume(worker_name: str) -> JSONResponse:
        """Возобновить работу воркера."""
        worker = await _find_worker(worker_name)
        if worker is None:
            return JSONResponse({"error": f"Воркер '{worker_name}' не найден"}, status_code=404)
        worker.resume()
        return JSONResponse({"ok": True, "name": worker_name, "paused": False})

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
                for name in names:
                    item = await uow.items.get_by_name(name)
                    if item:
                        steam_p = float(item.steam_price_usd) if item.steam_price_usd else None
                        item_p = float(item.price_usd) if item.price_usd else None
                        discount = None
                        if steam_p and item_p and item_p > 0:
                            from fluxio.config import FeesConfig
                            net = FeesConfig.calc_net_steam(steam_p)
                            discount = round((net - item_p) / item_p * 100, 1) if net > 0 else None
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

    # ═══════════════════════════════════════════════════════════════════════════
    # HTML Pages
    # ═══════════════════════════════════════════════════════════════════════════

    # ─── Service Status (главная) ──────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    @app.get("/admin/", response_class=HTMLResponse)
    @app.get("/admin", response_class=HTMLResponse)
    async def page_service_status() -> str:
        """Объединённая страница Service Status — система, воркеры, прокси, Redis."""
        uptime = int((datetime.now(timezone.utc) - _started_at).total_seconds())
        d, rem = divmod(uptime, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        uptime_str = f"{d}d {h}h {m}m" if d else f"{h}h {m}m {s}s"
        mode = "Prod" if not config.trading.dry_run else "Dry Run"

        # ── Redis данные ──
        queue_scanner = 0
        queue_buyer = 0
        candidates_count = 0
        enrich_queue = 0
        try:
            from fluxio.utils.redis_client import (
                KEY_CANDIDATES,
                KEY_ENRICH_QUEUE,
                KEY_UPDATE_QUEUE,
                get_redis,
            )
            redis = await get_redis()
            queue_scanner = await redis.zcard(KEY_UPDATE_QUEUE)
            candidates_count = await redis.scard(KEY_CANDIDATES)
            enrich_queue = await redis.scard(KEY_ENRICH_QUEUE)
        except Exception:
            pass

        # ── БД статистика ──
        total_items = 0
        enriched_count = 0
        try:
            from sqlalchemy import func as sa_func, select

            from fluxio.db.models import Item
            from fluxio.db.session import async_session_factory
            from fluxio.db.unit_of_work import UnitOfWork
            async with UnitOfWork(async_session_factory) as uow:
                total_items = (await uow.session.execute(
                    select(sa_func.count()).select_from(Item)
                )).scalar() or 0
                enriched_count = (await uow.session.execute(
                    select(sa_func.count()).select_from(Item).where(Item.enriched_at.isnot(None))
                )).scalar() or 0
        except Exception:
            pass
        enriched_pct = round(enriched_count / total_items * 100, 1) if total_items > 0 else 0

        # ── Прокси ──
        proxy_total = 0
        proxy_active = 0
        try:
            if _container:
                from fluxio.api.steam_client import SteamClient
                if _container.is_initialized(SteamClient):
                    steam = await _container.get(SteamClient)
                    ps = steam.proxy_stats()
                    proxy_total = ps["total_proxies"]
                    proxy_active = ps["total_proxies"] - len(ps.get("bad_proxies", []))
        except Exception:
            pass

        # ── Circuit Breaker ──
        cb_state = "closed"
        cb_failures = 0
        cb_threshold = 5
        try:
            if _container and _container.is_initialized(CircuitBreaker):
                cb = await _container.get(CircuitBreaker)
                st = cb.status()
                cb_state = st["state"]
                cb_failures = st["failure_count"]
                cb_threshold = st["threshold"]
        except Exception:
            pass

        system_ok = cb_state == "closed"

        # ── Балансы платформ ──
        cs2dt_balance: float | None = None
        c5game_balance: float | None = None
        spent_today = 0.0
        total_spent_all = 0.0
        try:
            if _container:
                from fluxio.api.cs2dt_client import CS2DTClient
                if _container.is_initialized(CS2DTClient):
                    cs2dt = await _container.get(CS2DTClient)
                    bal_data = await cs2dt.get_balance()
                    cs2dt_balance = float(bal_data.get("data", 0))
        except Exception:
            pass

        try:
            if _container:
                from fluxio.api.c5game_client import C5GameClient
                if _container.is_registered(C5GameClient) and _container.is_initialized(C5GameClient):
                    c5 = await _container.get(C5GameClient)
                    bal_data = await c5.get_balance()
                    c5game_balance = float(bal_data.get("balance", 0))
        except Exception:
            pass

        try:
            from fluxio.db.session import async_session_factory
            from fluxio.db.unit_of_work import UnitOfWork
            from sqlalchemy import func as sa_func, select as sa_sel

            from fluxio.db.models import Purchase
            async with UnitOfWork(async_session_factory) as uow:
                spent_today = await uow.purchases.get_today_spent()
                result = await uow.session.execute(
                    sa_sel(sa_func.coalesce(sa_func.sum(Purchase.price_usd), 0))
                    .where(Purchase.dry_run == False)
                    .where(Purchase.status.in_(["pending", "success"]))
                )
                total_spent_all = float(result.scalar_one())
        except Exception:
            pass

        daily_limit = config.trading.daily_limit_usd
        daily_pct = min(round(spent_today / daily_limit * 100, 1), 100) if daily_limit > 0 else 0

        # ── Воркеры ──
        workers_html = ""
        if _container:
            from fluxio.core.workers.buyer import BuyerWorker as _BuyerW
            from fluxio.core.workers.enricher import EnricherWorker as _EnricherW
            from fluxio.core.workers.order_tracker import OrderTrackerWorker as _TrackerW
            from fluxio.core.workers.scanner import ScannerWorker
            from fluxio.core.workers.updater import UpdaterWorker

            for worker_type in (ScannerWorker, UpdaterWorker, _EnricherW, _BuyerW, _TrackerW):
                if _container.is_initialized(worker_type):
                    w = await _container.get(worker_type)
                    st = w.status
                    dot_class = "paused" if st.paused else ("running" if st.running else "stopped")
                    last = st.last_run_at.strftime("%H:%M:%S") if st.last_run_at else "—"
                    err_html = ""
                    if st.last_error:
                        err_html = f'<div style="color:var(--danger);font-size:11px;margin-top:4px">{_esc(st.last_error)}</div>'
                    if st.paused:
                        btn = f'<button class="worker-btn" data-worker="{_esc(st.name)}" data-action="resume">&#9654; Resume</button>'
                    else:
                        btn = f'<button class="worker-btn" data-worker="{_esc(st.name)}" data-action="pause">&#9646;&#9646; Pause</button>'
                    workers_html += f"""<div class="worker-card">
                      <div class="worker-dot {dot_class}"></div>
                      <div class="worker-info">
                        <div class="worker-name">{_esc(st.name)}</div>
                        <div class="worker-meta">Cycles: {st.cycles} &middot; Processed: {st.items_processed} &middot; Last: {last}</div>
                        {err_html}
                      </div>
                      {btn}
                    </div>"""

        if not workers_html:
            workers_html = '<div class="empty"><p>Workers not initialized</p></div>'

        # ── Сборка страницы ──
        content = f"""
<!-- Stat Cards Row -->
<div class="grid grid-4" style="margin-bottom:20px">
  <div class="stat-card">
    <div class="stat-label">Proxies</div>
    <div class="stat-value" style="color:var(--accent)">{proxy_active}<span style="font-size:14px;color:var(--text-muted)">/{proxy_total}</span></div>
    <div class="stat-sub">Active / Total</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Redis Queues</div>
    <div class="stat-value" style="color:var(--warning)">{queue_scanner + candidates_count}</div>
    <div class="stat-sub">Scanner: {queue_scanner} &middot; Candidates: {candidates_count}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Items Enriched</div>
    <div class="stat-value" style="color:var(--success)">{enriched_pct}%</div>
    <div class="stat-sub">{enriched_count} / {total_items} &middot; Queue: {enrich_queue}</div>
    <div class="progress-bar"><div class="progress-fill" style="width:{enriched_pct}%"></div></div>
  </div>
  <div class="stat-card">
    <div class="stat-label">System</div>
    <div style="display:flex;align-items:center;gap:8px;margin:4px 0">
      <span class="badge {'badge-success' if system_ok else 'badge-danger'}">{'OK' if system_ok else cb_state.upper()}</span>
    </div>
    <div class="stat-sub">Mode: {mode} &middot; Uptime: {uptime_str}</div>
    <div class="stat-sub">Discount: &ge;{config.trading.min_discount_percent}% &middot; Limit: ${config.trading.daily_limit_usd}</div>
  </div>
</div>

<!-- Балансы платформ -->
<div class="card" style="margin-bottom:20px">
  <div class="card-header"><div class="card-title">Balances &amp; Spending</div></div>
  <div class="grid" style="grid-template-columns:1fr 1fr 1fr;gap:16px">
    <div style="padding:12px 0">
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:4px">CS2DT (HaloSkins)</div>
      <div style="font-size:24px;font-weight:700;color:var(--accent)">{"$" + f"{cs2dt_balance:.2f}" if cs2dt_balance is not None else '<span style="color:var(--text-muted)">N/A</span>'}</div>
      <div style="font-size:11px;color:var(--text-muted);margin-top:2px">T-Coin balance</div>
    </div>
    <div style="padding:12px 0">
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:4px">C5Game</div>
      <div style="font-size:24px;font-weight:700;color:var(--accent)">{"$" + f"{c5game_balance:.2f}" if c5game_balance is not None else '<span style="color:var(--text-muted)">N/A</span>'}</div>
      <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Account balance</div>
    </div>
    <div style="padding:12px 0">
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:4px">Spending</div>
      <div style="font-size:24px;font-weight:700;color:var(--warning)">${spent_today:.2f} <span style="font-size:13px;font-weight:400;color:var(--text-muted)">/ ${daily_limit:.0f} today</span></div>
      <div class="progress-bar" style="margin-top:6px"><div class="progress-fill" style="width:{daily_pct}%;background:linear-gradient(90deg, var(--warning), #fb923c)"></div></div>
      <div style="font-size:11px;color:var(--text-muted);margin-top:4px">All time: ${total_spent_all:.2f}</div>
    </div>
  </div>
</div>

<!-- Circuit Breaker -->
<div class="card" style="margin-bottom:20px">
  <div class="card-header"><div class="card-title">Circuit Breaker</div></div>
  <div style="display:flex;gap:24px;flex-wrap:wrap">
    <div class="metric-row" style="border:none;padding:0">
      <span class="metric-label" style="margin-right:8px">State:</span>
      <span class="badge {'badge-success' if cb_state == 'closed' else 'badge-danger' if cb_state == 'open' else 'badge-warning'}">{cb_state.upper()}</span>
    </div>
    <div><span style="color:var(--text-muted)">Failures:</span> <strong>{cb_failures} / {cb_threshold}</strong></div>
  </div>
</div>

<!-- Workers -->
<div class="card-header" style="margin-bottom:12px"><div class="card-title" style="font-size:16px">Workers</div></div>
<div class="grid grid-2" style="margin-bottom:20px">
  {workers_html}
</div>

<!-- Live Charts -->
<div class="grid grid-2" style="margin-bottom:20px">
  <div class="chart-card">
    <div class="card-header"><div class="card-title">Proxy Usage</div></div>
    <canvas id="chartProxy" height="200"></canvas>
  </div>
  <div class="chart-card">
    <div class="card-header"><div class="card-title">Queue Activity</div></div>
    <canvas id="chartQueue" height="200"></canvas>
  </div>
</div>
"""

        extra_chart_js = f"""<script>
document.addEventListener('DOMContentLoaded', function() {{
  // Proxy donut chart
  const ctxProxy = document.getElementById('chartProxy');
  if (ctxProxy) {{
    new Chart(ctxProxy, {{
      type: 'doughnut',
      data: {{
        labels: ['Active', 'Bad'],
        datasets: [{{
          data: [{proxy_active}, {proxy_total - proxy_active}],
          backgroundColor: ['#3b82f6', '#1e293b'],
          borderWidth: 0,
          borderRadius: 4,
        }}]
      }},
      options: {{
        responsive: true,
        cutout: '70%',
        plugins: {{
          legend: {{ position: 'bottom', labels: {{ color: '#94a3b8', font: {{ size: 12 }} }} }}
        }}
      }}
    }});
  }}

  // Queue bar chart
  const ctxQueue = document.getElementById('chartQueue');
  if (ctxQueue) {{
    new Chart(ctxQueue, {{
      type: 'bar',
      data: {{
        labels: ['Scanner Queue', 'Candidates', 'Enrich Queue'],
        datasets: [{{
          data: [{queue_scanner}, {candidates_count}, {enrich_queue}],
          backgroundColor: ['#3b82f6', '#22c55e', '#f59e0b'],
          borderRadius: 6,
          borderSkipped: false,
        }}]
      }},
      options: {{
        responsive: true,
        scales: {{
          y: {{ beginAtZero: true, grid: {{ color: '#1a1f2e' }}, ticks: {{ color: '#64748b' }} }},
          x: {{ grid: {{ display: false }}, ticks: {{ color: '#94a3b8' }} }}
        }},
        plugins: {{
          legend: {{ display: false }}
        }}
      }}
    }});
  }}
}});
</script>"""

        return _layout("Service Status", "/", content,
                       extra_head='<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>') + extra_chart_js

    # ─── Logs ──────────────────────────────────────────────────────────────────

    @app.get("/admin/logs", response_class=HTMLResponse)
    async def page_logs() -> str:
        """Страница SSE логов с фильтрацией по воркерам."""
        content = """
<div class="tabs">
  <div class="tab active" data-filter="all">All Logs<span class="count" id="cnt-all">0</span></div>
  <div class="tab" data-filter="scanner">Scanner<span class="count" id="cnt-scanner">0</span></div>
  <div class="tab" data-filter="updater">Updater<span class="count" id="cnt-updater">0</span></div>
  <div class="tab" data-filter="buyer">Buyer<span class="count" id="cnt-buyer">0</span></div>
  <div class="tab" data-filter="other">Other<span class="count" id="cnt-other">0</span></div>
</div>
<div class="log-status" id="status">Connecting...</div>
<div class="log-container" id="logs"></div>
"""

        extra_js = """<script>
const logsDiv = document.getElementById('logs');
const statusDiv = document.getElementById('status');
let activeFilter = 'all';
const allLines = [];
const MAX_LINES = 2000;
const counters = {all: 0, scanner: 0, updater: 0, buyer: 0, other: 0};

function getLogClass(text) {
  if (text.includes('| ERROR |')) return 'log-ERROR';
  if (text.includes('| WARNING |')) return 'log-WARNING';
  if (text.includes('| INFO |')) return 'log-INFO';
  if (text.includes('| DEBUG |')) return 'log-DEBUG';
  return '';
}

function getWorker(text) {
  if (/\\| (scanner|cs2dt_client):/.test(text) || text.includes('Scanner:')) return 'scanner';
  if (/\\| (updater|steam_client):/.test(text) || text.includes('Updater:')) return 'updater';
  if (/\\| (buyer|safety):/.test(text) || text.includes('Buyer')) return 'buyer';
  return 'other';
}

function matchesFilter(worker) {
  return activeFilter === 'all' || activeFilter === worker;
}

function updateCounters() {
  for (const k of Object.keys(counters)) {
    document.getElementById('cnt-' + k).textContent = counters[k];
  }
}

function addLine(text) {
  const worker = getWorker(text);
  const cls = getLogClass(text);
  counters.all++;
  counters[worker]++;

  const entry = {text, worker, cls};
  allLines.push(entry);
  if (allLines.length > MAX_LINES) {
    const removed = allLines.shift();
    counters.all--;
    counters[removed.worker]--;
    if (matchesFilter(removed.worker) && logsDiv.firstChild) {
      logsDiv.removeChild(logsDiv.firstChild);
    }
  }

  if (matchesFilter(worker)) {
    const div = document.createElement('div');
    div.className = 'log-line ' + cls;
    div.textContent = text;
    logsDiv.appendChild(div);
    logsDiv.scrollTop = logsDiv.scrollHeight;
  }

  updateCounters();
}

function renderFilter() {
  logsDiv.innerHTML = '';
  for (const entry of allLines) {
    if (matchesFilter(entry.worker)) {
      const div = document.createElement('div');
      div.className = 'log-line ' + entry.cls;
      div.textContent = entry.text;
      logsDiv.appendChild(div);
    }
  }
  logsDiv.scrollTop = logsDiv.scrollHeight;
}

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelector('.tab.active').classList.remove('active');
    tab.classList.add('active');
    activeFilter = tab.dataset.filter;
    renderFilter();
  });
});

function connect() {
  const es = new EventSource('/sse/logs');
  es.onopen = () => { statusDiv.textContent = 'Connected (SSE)'; };
  es.onmessage = (e) => { addLine(e.data); };
  es.onerror = () => {
    statusDiv.textContent = 'Disconnected. Reconnecting...';
    es.close();
    setTimeout(connect, 3000);
  };
}
connect();
</script>"""

        return _layout("Logs", "/admin/logs", content) + extra_js

    # ─── Catalog API ────────────────────────────────────────────────────────────

    @app.get("/api/v1/items/catalog")
    async def api_items_catalog(
        offset: int = 0,
        limit: int = 60,
        min_discount: float = 0,
        min_volume: int = 0,
        min_price: float = 0,
        max_price: float = 0,
        sort_by: str = "discount",
    ) -> JSONResponse:
        """Каталог предметов с пагинацией и серверной фильтрацией (JSON).

        Фильтры применяются на стороне сервера: discount считается
        по формуле (steam_net - price) / steam_net * 100.
        Когда фильтры активны, offset/limit работают по отфильтрованным данным.
        sort_by: discount, profit, volume, price_asc, price_desc
        """
        limit = min(limit, 200)  # Макс. 200 за запрос
        has_filters = min_discount > 0 or min_volume > 0 or min_price > 0 or (0 < max_price < 999) or sort_by != "discount"
        try:
            from fluxio.db.session import async_session_factory
            from fluxio.db.unit_of_work import UnitOfWork
            async with UnitOfWork(async_session_factory) as uow:
                # Загружаем все предметы для фильтрации дисконта (он вычисляемый)
                # При наличии фильтров нужен полный проход; без фильтров — пагинация
                if has_filters:
                    all_items = await uow.items.get_all()
                    from fluxio.config import FeesConfig
                    filtered: list[dict] = []
                    for item in all_items:
                        p = float(item.price_usd) if item.price_usd else None
                        sp = float(item.steam_price_usd) if item.steam_price_usd else None
                        d_val = 0.0
                        if p and sp and p > 0:
                            net = FeesConfig.calc_net_steam(sp)
                            d_val = (net - p) / p * 100 if net > 0 else 0
                        vol = item.steam_volume_24h or 0
                        price_val = p or 0
                        # Применяем фильтры
                        if d_val < min_discount:
                            continue
                        if vol < min_volume:
                            continue
                        if min_price > 0 and price_val < min_price:
                            continue
                        if 0 < max_price < 999 and price_val > max_price:
                            continue
                        profit_val = (net - p) if (p and sp and net > 0) else 0
                        filtered.append({
                            "name": item.market_hash_name,
                            "price": p,
                            "steam": sp,
                            "discount": round(d_val, 2),
                            "profit": round(profit_val, 4),
                            "volume": vol,
                            "image_url": item.image_url or "",
                            "app_id": item.app_id,
                            "updated": item.steam_updated_at.strftime("%H:%M") if hasattr(item, "steam_updated_at") and item.steam_updated_at else "",
                        })
                    # Сортировка
                    sort_keys = {
                        "discount": lambda x: x["discount"],
                        "profit": lambda x: x["profit"],
                        "volume": lambda x: x["volume"],
                        "price_asc": lambda x: x["price"] or 0,
                        "price_desc": lambda x: x["price"] or 0,
                    }
                    sk = sort_keys.get(sort_by, sort_keys["discount"])
                    reverse = sort_by != "price_asc"
                    filtered.sort(key=sk, reverse=reverse)
                    total = len(filtered)
                    result = filtered[offset:offset + limit]
                else:
                    items = await uow.items.get_all_paginated(offset=offset, limit=limit)
                    total = await uow.items.count()
                    from fluxio.config import FeesConfig
                    result = []
                    for item in items:
                        p = float(item.price_usd) if item.price_usd else None
                        sp = float(item.steam_price_usd) if item.steam_price_usd else None
                        d_val = 0.0
                        profit_val = 0.0
                        if p and sp and p > 0:
                            net = FeesConfig.calc_net_steam(sp)
                            d_val = (net - p) / p * 100 if net > 0 else 0
                            profit_val = net - p
                        result.append({
                            "name": item.market_hash_name,
                            "price": p,
                            "steam": sp,
                            "discount": round(d_val, 2),
                            "profit": round(profit_val, 4),
                            "volume": item.steam_volume_24h or 0,
                            "image_url": item.image_url or "",
                            "app_id": item.app_id,
                            "updated": item.steam_updated_at.strftime("%H:%M") if hasattr(item, "steam_updated_at") and item.steam_updated_at else "",
                        })
            return JSONResponse({"items": result, "total": total, "offset": offset, "limit": limit})
        except Exception as e:
            logger.exception("Dashboard: ошибка API каталога")
            return JSONResponse({"error": str(e), "items": [], "total": 0}, status_code=500)

    # ─── Catalog (/items) ──────────────────────────────────────────────────────

    @app.get("/items", response_class=HTMLResponse)
    async def page_catalog() -> str:
        """Каталог предметов из БД — карточки с фильтрацией и ленивой подгрузкой."""
        total = 0
        try:
            from fluxio.db.session import async_session_factory
            from fluxio.db.unit_of_work import UnitOfWork
            async with UnitOfWork(async_session_factory) as uow:
                total = await uow.items.count()
        except Exception:
            logger.exception("Dashboard: ошибка подсчёта предметов")

        min_discount = config.trading.min_discount_percent

        # Стиль для числовых input в фильтрах
        input_style = (
            "width:100%;padding:6px 8px;background:var(--bg-card);border:1px solid var(--border);"
            "border-radius:6px;color:var(--text);font-size:13px;outline:none"
        )

        content = f"""
<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:8px">
  <div style="color:var(--text-muted);font-size:13px">
    Total: <strong style="color:var(--text)" id="totalCount">{total}</strong> &middot;
    Loaded: <strong style="color:var(--text)" id="loadedCount">0</strong>
    <span id="filteredInfo" style="display:none"> &middot; Matched: <strong style="color:var(--accent)" id="matchedCount">0</strong></span>
  </div>
  <div style="display:flex;gap:8px">
    <button class="btn btn-ghost" onclick="toggleFilter()" id="filterBtn">
      <i data-lucide="sliders-horizontal" style="width:14px;height:14px"></i> Filter
    </button>
  </div>
</div>

<div class="catalog-layout" style="display:grid;grid-template-columns:220px 1fr;gap:16px">
  <!-- Filter Panel -->
  <div class="filter-panel" id="filterPanel">
    <h3>Filters</h3>
    <div class="filter-group">
      <div class="filter-label"><span>Min Discount %</span></div>
      <input type="number" min="0" max="99" value="0" step="1" id="filterDiscount"
        style="{input_style}" onchange="onFilterChange()" onkeyup="onFilterKeyup(event)">
    </div>
    <div class="filter-group">
      <div class="filter-label"><span>Min Volume (24h)</span></div>
      <input type="number" min="0" max="100000" value="0" step="1" id="filterVolume"
        style="{input_style}" onchange="onFilterChange()" onkeyup="onFilterKeyup(event)">
    </div>
    <div class="filter-group">
      <div class="filter-label"><span>Min Price $</span></div>
      <input type="number" min="0" max="100000" value="0" step="0.01" id="filterMinPrice"
        style="{input_style}" placeholder="0 = no limit" onchange="onFilterChange()" onkeyup="onFilterKeyup(event)">
    </div>
    <div class="filter-group">
      <div class="filter-label"><span>Max Price $</span></div>
      <input type="number" min="0" max="100000" value="0" step="0.01" id="filterPrice"
        style="{input_style}" placeholder="0 = no limit" onchange="onFilterChange()" onkeyup="onFilterKeyup(event)">
    </div>
    <div class="filter-group">
      <div class="filter-label"><span>Sort By</span></div>
      <select id="filterSort" style="{input_style}" onchange="onFilterChange()">
        <option value="discount">Discount ↓</option>
        <option value="profit">Profit ↓</option>
        <option value="volume">Volume ↓</option>
        <option value="price_asc">Price ↑</option>
        <option value="price_desc">Price ↓</option>
      </select>
    </div>
    <button class="btn btn-ghost" style="width:100%;margin-top:8px" onclick="resetFilters()">Reset</button>
  </div>

  <!-- Items Grid -->
  <div class="items-grid" id="itemsGrid" style="display:grid;grid-template-columns:repeat(auto-fill, minmax(220px, 1fr));gap:14px;align-content:start">
    <div class="empty" style="grid-column:1/-1" id="loadingMsg"><p>Loading...</p></div>
  </div>
</div>
<div style="text-align:center;margin-top:20px" id="loadMoreWrap">
  <button class="btn btn-ghost" id="loadMoreBtn" onclick="loadMore()" style="display:none">
    Show More
  </button>
</div>"""

        extra_js = f"""<script>
const BATCH = 60;
const MIN_DISCOUNT_CFG = {min_discount};
const TOTAL_ALL = {total};
let catalogOffset = 0;
let catalogLoading = false;
let serverTotal = TOTAL_ALL;  // Обновляется из API (с учётом фильтров)
let filterTimer = null;

function escHtml(s) {{
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}}

function getFilters() {{
  return {{
    minD: parseFloat(document.getElementById('filterDiscount').value) || 0,
    minV: parseInt(document.getElementById('filterVolume').value) || 0,
    minP: parseFloat(document.getElementById('filterMinPrice').value) || 0,
    maxP: parseFloat(document.getElementById('filterPrice').value) || 0,
    sortBy: document.getElementById('filterSort').value || 'discount',
  }};
}}

function hasActiveFilters() {{
  const f = getFilters();
  return f.minD > 0 || f.minV > 0 || f.minP > 0 || (f.maxP > 0 && f.maxP < 999) || f.sortBy !== 'discount';
}}

function makeCard(it) {{
  const priceStr = it.price != null ? '$' + it.price.toFixed(2) : '—';
  const steamStr = it.steam != null ? '$' + it.steam.toFixed(2) : '—';
  let badge = '';
  if (it.discount >= MIN_DISCOUNT_CFG) {{
    badge = '<span class="badge badge-success">' + it.discount.toFixed(1) + '%</span>';
  }} else if (it.discount > 0) {{
    badge = '<span class="badge badge-info">' + it.discount.toFixed(1) + '%</span>';
  }}
  const imgHtml = it.image_url
    ? '<img src="' + escHtml(it.image_url) + '" alt="" loading="lazy" referrerpolicy="no-referrer" style="max-width:100%;max-height:100%;object-fit:contain" onerror="this.style.display=\\\'none\\\'">'
    : '<i data-lucide="package" style="width:48px;height:48px;color:var(--text-muted)"></i>';
  const buyBg = it.discount >= MIN_DISCOUNT_CFG ? 'background:var(--success)' : 'background:var(--accent)';
  const steamUrl = 'https://steamcommunity.com/market/listings/' + (it.app_id || 570) + '/' + encodeURIComponent(it.name);

  const div = document.createElement('div');
  div.className = 'item-card';
  div.innerHTML = '<div class="item-img">' + imgHtml + '</div>'
    + '<div class="item-name" title="' + escHtml(it.name) + '">' + escHtml(it.name) + '</div>'
    + '<div class="item-rarity">Vol: ' + it.volume + ' ' + badge + '</div>'
    + '<div class="item-prices" style="display:flex;justify-content:space-between;align-items:center;padding:4px 10px 0;font-size:12px;color:var(--text-muted)">'
    + '<span>CS2DT: <strong style="color:var(--text)">' + priceStr + '</strong></span>'
    + '<span>Steam: <strong style="color:var(--accent)">' + steamStr + '</strong></span>'
    + '</div>'
    + '<div class="item-footer">'
    + '<a href="' + steamUrl + '" target="_blank" rel="noopener" title="Open on Steam Market" class="steam-icon-link">'
    + '<svg width="18" height="18" viewBox="0 0 256 259" fill="#c6d4df" xmlns="http://www.w3.org/2000/svg">'
    + '<path d="M127.8 0C60.2 0 5.2 52 .5 118.6l68.6 28.3c5.8-4 12.9-6.3 20.5-6.3l.3 0 2-.1 30.6-44.3v-.6c0-26.3 21.4-47.6 47.6-47.6s47.6 21.4 47.6 47.6-21.4 47.6-47.6 47.6h-1.1l-43.6 31.1c0 .5 0 1.1 0 1.6 0 19.7-16 35.7-35.7 35.7-17.6 0-32.2-12.7-35.2-29.4L4.1 160C20.8 214.4 71.1 253.5 130.6 253.5c71.5 0 129.4-57.9 129.4-129.4C260 52.7 199.2 0 127.8 0zM80.3 196.3l-15.6-6.4c2.8 5.7 7.4 10.6 13.4 13.6 13 6.5 28.9 1 35.4-12.1 3.1-6.3 3.4-13.3.7-19.7-2.7-6.4-7.7-11.3-14.1-14.5-6.2-3.1-13-3.5-19.2-1.5l16.1 6.7c9.6 4 14.2 15 10.2 24.6-4 9.6-15 14.2-24.6 10.2l-.3-.2zm110.6-96.6c0-17.5-14.2-31.7-31.7-31.7s-31.7 14.2-31.7 31.7 14.2 31.7 31.7 31.7 31.7-14.2 31.7-31.7zm-55.6.1c0-13.2 10.7-24 24-24s24 10.7 24 24-10.7 24-24 24-24-10.7-24-24z"/>'
    + '</svg></a>'
    + '<span class="btn-buy" style="' + buyBg + '">Buy</span></div>';
  return div;
}}

function buildUrl() {{
  const f = getFilters();
  let url = '/api/v1/items/catalog?offset=' + catalogOffset + '&limit=' + BATCH;
  if (f.minD > 0) url += '&min_discount=' + f.minD;
  if (f.minV > 0) url += '&min_volume=' + f.minV;
  if (f.minP > 0) url += '&min_price=' + f.minP;
  if (f.maxP > 0 && f.maxP < 999) url += '&max_price=' + f.maxP;
  if (f.sortBy && f.sortBy !== 'discount') url += '&sort_by=' + f.sortBy;
  return url;
}}

async function loadMore() {{
  if (catalogLoading) return;
  catalogLoading = true;
  const btn = document.getElementById('loadMoreBtn');
  btn.textContent = 'Loading...';
  btn.disabled = true;

  try {{
    const resp = await fetch(buildUrl());
    const data = await resp.json();
    const grid = document.getElementById('itemsGrid');

    // Убрать сообщение Loading при первой загрузке
    const loadingMsg = document.getElementById('loadingMsg');
    if (loadingMsg) loadingMsg.remove();

    const items = data.items || [];
    items.forEach(it => {{
      grid.appendChild(makeCard(it));
    }});

    catalogOffset += items.length;
    serverTotal = data.total || 0;

    document.getElementById('loadedCount').textContent = catalogOffset;

    // Показать инфо о фильтрации
    const fInfo = document.getElementById('filteredInfo');
    const mCount = document.getElementById('matchedCount');
    if (hasActiveFilters()) {{
      fInfo.style.display = '';
      mCount.textContent = serverTotal;
    }} else {{
      fInfo.style.display = 'none';
    }}

    // Переинициализировать иконки Lucide для новых карточек
    if (typeof lucide !== 'undefined') lucide.createIcons();

    const remaining = serverTotal - catalogOffset;
    if (remaining <= 0 || items.length < BATCH) {{
      btn.style.display = 'none';
    }} else {{
      btn.style.display = '';
      btn.textContent = 'Show More (' + remaining + ' remaining)';
      btn.disabled = false;
    }}
  }} catch (e) {{
    console.error('Ошибка загрузки:', e);
    btn.textContent = 'Error — retry';
    btn.disabled = false;
  }}
  catalogLoading = false;
}}

function toggleFilter() {{
  document.getElementById('filterPanel').classList.toggle('open');
}}

function onFilterKeyup(e) {{
  // Применить фильтр по Enter или с задержкой 600мс
  if (e.key === 'Enter') {{
    if (filterTimer) clearTimeout(filterTimer);
    onFilterChange();
  }} else {{
    if (filterTimer) clearTimeout(filterTimer);
    filterTimer = setTimeout(onFilterChange, 600);
  }}
}}

function onFilterChange() {{
  // Сброс и перезагрузка с новыми фильтрами
  catalogOffset = 0;
  const grid = document.getElementById('itemsGrid');
  grid.innerHTML = '<div class="empty" style="grid-column:1/-1" id="loadingMsg"><p>Loading...</p></div>';
  loadMore();
}}

function resetFilters() {{
  document.getElementById('filterDiscount').value = 0;
  document.getElementById('filterVolume').value = 0;
  document.getElementById('filterMinPrice').value = 0;
  document.getElementById('filterPrice').value = 0;
  document.getElementById('filterSort').value = 'discount';
  onFilterChange();
}}

// Загрузить первый батч при открытии страницы
loadMore();
</script>"""

        return _layout("Catalog", "/items", content) + extra_js

    # ─── Candidates ────────────────────────────────────────────────────────────

    @app.post("/api/v1/candidates/clear")
    async def api_clear_candidates() -> JSONResponse:
        """Очистить список кандидатов в Redis."""
        try:
            from fluxio.utils.redis_client import KEY_CANDIDATES, get_redis
            redis = await get_redis()
            count = await redis.scard(KEY_CANDIDATES)
            await redis.delete(KEY_CANDIDATES)
            logger.info(f"Список кандидатов очищен ({count} шт.)")
            return JSONResponse({"cleared": count})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/candidates", response_class=HTMLResponse)
    async def page_candidates() -> str:
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
                    candidate_rows: list[tuple[float, float, str]] = []  # (discount, profit, html)
                    for name in names:
                        item = await uow.items.get_by_name(name)
                        if not item:
                            continue
                        p = float(item.price_usd) if item.price_usd else 0.0
                        sp = float(item.steam_price_usd) if item.steam_price_usd else 0.0
                        d_val = 0.0
                        profit_val = 0.0
                        discount = ""
                        profit = ""
                        vol = item.steam_volume_24h or 0
                        if p > 0 and sp > 0:
                            from fluxio.config import FeesConfig
                            net = FeesConfig.calc_net_steam(sp)
                            d_val = (net - p) / p * 100 if net > 0 else 0
                            profit_val = net - p
                            discount = f"{d_val:.1f}%"
                            profit = f"${profit_val:.4f}"
                        profit_color = "var(--success)" if profit_val > 0 else "var(--danger)"
                        img_tag = (
                            f'<img src="{_esc(item.image_url)}" alt="" '
                            f'style="width:32px;height:32px;object-fit:contain;vertical-align:middle;margin-right:6px">'
                            if item.image_url else ""
                        )
                        from urllib.parse import quote as _quote
                        steam_link = f"https://steamcommunity.com/market/listings/570/{_quote(name)}"
                        row = (
                            f'<tr data-discount="{d_val:.4f}" '
                            f'data-profit="{profit_val:.6f}" '
                            f'data-volume="{vol}">'
                            f'<td style="white-space:nowrap">{img_tag}{_esc(name)}</td>'
                            f"<td>${p:.4f}</td>"
                            f"<td>${sp:.4f}</td>"
                            f'<td><span style="color:var(--success);font-weight:600">{discount}</span></td>'
                            f'<td><span style="color:{profit_color}">{profit}</span></td>'
                            f"<td>{vol or '—'}</td>"
                            f'<td><a href="{steam_link}" target="_blank" '
                            f'style="color:var(--accent);font-size:12px">Steam</a></td>'
                            f"</tr>"
                        )
                        candidate_rows.append((d_val, profit_val, row))
                    candidate_rows.sort(key=lambda x: x[0], reverse=True)  # по discount
                    rows_html = "".join(r for _, _, r in candidate_rows)
        except Exception as e:
            rows_html = f"<tr><td colspan='7'>Error: {_esc(e)}</td></tr>"

        if not rows_html:
            rows_html = '<tr><td colspan="7" style="text-align:center;color:var(--text-muted);padding:32px">No candidates yet. Run Scanner + Updater.</td></tr>'

        content = f"""
<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:8px">
  <div style="display:flex;gap:16px;align-items:center">
    <span style="color:var(--text-muted)">Candidates: <strong style="color:var(--success)">{total}</strong></span>
    <span style="color:var(--text-muted)">Update Queue: <strong>{queue_len}</strong></span>
  </div>
  <button class="btn btn-danger" onclick="clearCandidates()">
    <i data-lucide="trash-2" style="width:14px;height:14px"></i> Clear
  </button>
</div>

<div class="table-wrap" style="max-height:70vh;overflow:auto"><div class="table-scroll">
<table id="candidates-table">
<thead style="position:sticky;top:0;z-index:1"><tr>
  <th>Item</th><th>CS2DT Price</th><th>Steam Median</th>
  <th class="sortable sort-desc" data-key="discount">Discount</th>
  <th class="sortable" data-key="profit">Profit</th>
  <th class="sortable" data-key="volume">Vol 24h</th>
  <th>Link</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>
</div></div>"""

        extra_js = """<script>
async function clearCandidates() {
  if (!confirm('Clear all candidates? New ones will appear as prices update.')) return;
  const resp = await fetch('/api/v1/candidates/clear', {method: 'POST', headers: {'X-Fluxio-CSRF': '1'}});
  const data = await resp.json();
  if (data.cleared !== undefined) {
    alert('Cleared: ' + data.cleared + ' candidates');
    location.reload();
  } else {
    alert('Error: ' + (data.error || 'unknown'));
  }
}

document.querySelectorAll('th.sortable').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.key;
    const tbody = document.querySelector('#candidates-table tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
    const isDesc = th.classList.contains('sort-desc');
    const newDir = isDesc ? 'asc' : 'desc';
    document.querySelectorAll('th.sortable').forEach(h => {
      h.classList.remove('sort-asc', 'sort-desc');
    });
    th.classList.add('sort-' + newDir);
    rows.sort((a, b) => {
      const va = parseFloat(a.dataset[key]) || 0;
      const vb = parseFloat(b.dataset[key]) || 0;
      return newDir === 'desc' ? vb - va : va - vb;
    });
    rows.forEach(r => tbody.appendChild(r));
  });
});
</script>"""

        return _layout("Candidates", "/candidates", content) + extra_js

    # ─── Purchases ─────────────────────────────────────────────────────────────

    @app.get("/api/v1/purchases")
    async def api_purchases() -> JSONResponse:
        """История покупок (JSON)."""
        try:
            from fluxio.db.session import async_session_factory
            from fluxio.db.unit_of_work import UnitOfWork
            async with UnitOfWork(async_session_factory) as uow:
                purchases = await uow.purchases.get_all(limit=100_000)
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

    @app.post("/api/v1/purchases/clear")
    async def api_clear_purchases() -> JSONResponse:
        """Удалить все покупки из PostgreSQL."""
        try:
            from sqlalchemy import delete

            from fluxio.db.models import Purchase
            from fluxio.db.session import async_session_factory
            from fluxio.db.unit_of_work import UnitOfWork
            async with UnitOfWork(async_session_factory) as uow:
                result = await uow.session.execute(delete(Purchase))
                count = result.rowcount
                await uow.commit()
            logger.info(f"Все покупки удалены из БД ({count} шт.)")
            return JSONResponse({"cleared": count})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/purchases", response_class=HTMLResponse)
    async def page_purchases() -> str:
        """Страница истории покупок."""
        rows_html = ""
        total = 0
        total_spent = 0.0
        total_profit = 0.0
        dry_count = 0
        live_count = 0
        filtered_count = 0
        total_median_profit = 0.0
        rows: list[str] = []
        # Данные для графиков
        by_day: dict[str, dict] = {}   # "2026-03-15" -> {count, spent, profit}
        by_hour: dict[int, dict] = {}  # 0..23 -> {count, spent, profit}

        try:
            from urllib.parse import quote

            from sqlalchemy import func as sa_func, select as sa_select

            from fluxio.db.models import Purchase
            from fluxio.db.session import async_session_factory
            from fluxio.db.unit_of_work import UnitOfWork
            async with UnitOfWork(async_session_factory) as uow:
                all_purchases = await uow.purchases.get_all(limit=100_000)
                total = len(all_purchases)
                from fluxio.config import FeesConfig

                items_cache: dict[str, object] = {}
                for p in all_purchases:
                    if p.market_hash_name not in items_cache:
                        items_cache[p.market_hash_name] = await uow.items.get_by_name(
                            p.market_hash_name,
                        )

                # Кэш картинок: market_hash_name → image_url
                img_cache: dict[str, str] = {}
                for name, obj in items_cache.items():
                    if obj and getattr(obj, "image_url", None):
                        img_cache[name] = obj.image_url

                for p in all_purchases:
                    price = float(p.price_usd) if p.price_usd else 0
                    steam = float(p.steam_price_usd) if p.steam_price_usd else 0
                    if p.status == "filtered":
                        filtered_count += 1
                        continue
                    if p.status in ("success", "pending"):
                        total_spent += price
                        p_profit = 0.0
                        if steam > 0 and price > 0:
                            p_profit = FeesConfig.calc_net_steam(steam) - price
                            total_profit += p_profit
                        item_obj = items_cache.get(p.market_hash_name)
                        median = float(item_obj.steam_median_30d) if item_obj and item_obj.steam_median_30d else 0
                        if median > 0 and price > 0:
                            total_median_profit += FeesConfig.calc_net_steam(median) - price
                        if p.dry_run:
                            dry_count += 1
                        else:
                            live_count += 1

                        # Агрегация для графиков
                        if p.purchased_at:
                            day_key = p.purchased_at.strftime("%Y-%m-%d")
                            hour_key = p.purchased_at.hour
                            if day_key not in by_day:
                                by_day[day_key] = {"count": 0, "spent": 0.0, "profit": 0.0}
                            by_day[day_key]["count"] += 1
                            by_day[day_key]["spent"] += price
                            by_day[day_key]["profit"] += p_profit
                            if hour_key not in by_hour:
                                by_hour[hour_key] = {"count": 0, "spent": 0.0, "profit": 0.0}
                            by_hour[hour_key]["count"] += 1
                            by_hour[hour_key]["spent"] += price
                            by_hour[hour_key]["profit"] += p_profit

                rows = []
                for p in all_purchases:
                    price = float(p.price_usd) if p.price_usd else 0
                    steam = float(p.steam_price_usd) if p.steam_price_usd else 0
                    disc = f"{float(p.discount_percent):.1f}%" if p.discount_percent else "—"
                    ts = p.purchased_at.strftime("%Y-%m-%d %H:%M") if p.purchased_at else "—"

                    item_obj = items_cache.get(p.market_hash_name)
                    vol = item_obj.steam_volume_24h if item_obj and item_obj.steam_volume_24h else 0
                    median = float(item_obj.steam_median_30d) if item_obj and item_obj.steam_median_30d else 0

                    ratio = steam / median if median > 0 and steam > 0 else 0
                    ratio_str = f"{ratio:.1f}x" if ratio > 0 else "—"
                    ratio_color = "var(--text)"
                    if ratio > 2.0:
                        ratio_color = "var(--danger)"
                    elif ratio > 1.5:
                        ratio_color = "var(--warning)"

                    median_str = f"${median:.4f}" if median > 0 else "—"

                    profit = 0.0
                    profit_str = "—"
                    if steam > 0 and price > 0:
                        net_steam = FeesConfig.calc_net_steam(steam)
                        profit = net_steam - price
                        color_p = "var(--success)" if profit > 0 else "var(--danger)"
                        profit_str = f'<span style="color:{color_p}">${profit:.4f}</span>'

                    median_profit = 0.0
                    median_profit_str = "—"
                    if median > 0 and price > 0:
                        net_median = FeesConfig.calc_net_steam(median)
                        median_profit = net_median - price
                        mp_color = "var(--success)" if median_profit > 0 else "var(--danger)"
                        median_profit_str = f'<span style="color:{mp_color}">${median_profit:.4f}</span>'

                    status_map = {
                        "success": "badge-success",
                        "pending": "badge-warning",
                        "failed": "badge-danger",
                        "cancelled": "badge-muted",
                        "filtered": "badge-danger",
                    }
                    status_badge = status_map.get(p.status, "badge-muted")
                    dry_badge = ' <span class="badge badge-warning" style="font-size:10px">DRY</span>' if p.dry_run else ""
                    is_filtered = p.status == "filtered"
                    row_style = ' style="opacity:0.45"' if is_filtered else ""
                    filter_note = ""
                    if is_filtered and p.notes:
                        filter_note = (
                            f'<div style="font-size:10px;color:var(--danger);'
                            f'max-width:250px;overflow:hidden;text-overflow:ellipsis;'
                            f'white-space:nowrap" title="{_esc(p.notes)}">{_esc(p.notes)}</div>'
                        )

                    steam_url = f"https://steamcommunity.com/market/listings/570/{quote(p.market_hash_name)}"
                    pricemap_url = f"/price-map?item={quote(p.market_hash_name)}"
                    p_img = img_cache.get(p.market_hash_name, "")
                    p_img_tag = (
                        f'<img src="{_esc(p_img)}" alt="" '
                        f'style="width:28px;height:28px;object-fit:contain;vertical-align:middle;'
                        f'margin-right:8px;border-radius:4px">'
                        if p_img else ""
                    )

                    # Кнопка ручной покупки — только для dry_run с успешным статусом
                    buy_btn = ""
                    if p.dry_run and p.status in ("success", "pending"):
                        buy_btn = (
                            f'<button class="btn-manual-buy" '
                            f'data-name="{_esc(p.market_hash_name)}" '
                            f'data-price="{price:.4f}" '
                            f'onclick="openBuyDialog(this)">'
                            f'Купить</button>'
                        )

                    rows.append(
                        f'<tr class="p-row" data-profit="{profit:.6f}" data-volume="{vol}" '
                        f'data-discount="{float(p.discount_percent) if p.discount_percent else 0:.4f}" '
                        f'data-median-profit="{median_profit:.6f}" data-ratio="{ratio:.4f}" '
                        f'data-status="{_esc(p.status)}"'
                        f'{row_style}>'
                        f'<td style="white-space:nowrap;color:var(--text-muted);font-size:12px">{ts}</td>'
                        f'<td style="white-space:nowrap">{p_img_tag}'
                        f'<a href="{steam_url}" target="_blank" style="color:var(--text)">'
                        f'{_esc(p.market_hash_name)}</a>'
                        f' <a href="{pricemap_url}" target="_blank" title="Price Map" '
                        f'style="color:var(--accent);font-size:11px;text-decoration:none;'
                        f'opacity:0.7;margin-left:6px">📊</a>'
                        f'{filter_note}</td>'
                        f"<td>{buy_btn}</td>"
                        f"<td>${price:.4f}</td>"
                        f"<td>${steam:.4f}</td>"
                        f"<td>{median_str}</td>"
                        f'<td style="color:{ratio_color}">{ratio_str}</td>'
                        f"<td>{disc}</td>"
                        f"<td>{profit_str}</td>"
                        f"<td>{median_profit_str}</td>"
                        f"<td>{vol or '—'}</td>"
                        f'<td><span class="badge {status_badge}">{_esc(p.status)}</span>{dry_badge}</td>'
                        f"</tr>"
                    )
                rows_html = "".join(rows)
        except Exception as e:
            logger.exception("Dashboard: ошибка на странице покупок")
            rows_html = f"<tr><td colspan='13'>Error: {_esc(e)}</td></tr>"

        if not rows_html:
            rows_html = '<tr><td colspan="13" style="text-align:center;color:var(--text-muted);padding:32px">No purchases yet.</td></tr>'

        # Сериализация данных для графиков
        import json as _json
        sorted_days = sorted(by_day.keys())
        chart_day_labels = _json.dumps(sorted_days)
        chart_day_counts = _json.dumps([by_day[d]["count"] for d in sorted_days])
        chart_day_spent = _json.dumps([round(by_day[d]["spent"], 2) for d in sorted_days])
        chart_day_profit = _json.dumps([round(by_day[d]["profit"], 2) for d in sorted_days])

        chart_hour_labels = _json.dumps([f"{h:02d}:00" for h in range(24)])
        chart_hour_counts = _json.dumps([by_hour.get(h, {}).get("count", 0) for h in range(24)])
        chart_hour_spent = _json.dumps([round(by_hour.get(h, {}).get("spent", 0), 2) for h in range(24)])
        chart_hour_profit = _json.dumps([round(by_hour.get(h, {}).get("profit", 0), 2) for h in range(24)])

        profit_color = "var(--success)" if total_profit >= 0 else "var(--danger)"
        median_profit_color = "var(--success)" if total_median_profit >= 0 else "var(--danger)"

        content = f"""
<!-- Stats -->
<div class="grid grid-auto" style="margin-bottom:20px">
  <div class="stat-card">
    <div class="stat-label">Purchased</div>
    <div class="stat-value" style="color:var(--accent)">{dry_count + live_count}</div>
    <div class="stat-sub">dry: {dry_count} &middot; live: {live_count}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Filtered Out</div>
    <div class="stat-value" style="color:var(--danger)">{filtered_count}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Spent</div>
    <div class="stat-value" style="color:var(--warning)">${total_spent:.2f}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Profit (Listing)</div>
    <div class="stat-value" style="color:{profit_color}">${total_profit:.2f}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Profit (Median 30d)</div>
    <div class="stat-value" style="color:{median_profit_color}">${total_median_profit:.2f}</div>
  </div>
</div>

<!-- Графики покупок -->
<div class="grid" style="grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">
  <div class="chart-container">
    <div class="chart-title">Покупки по дням</div>
    <canvas id="chartByDay" height="220"></canvas>
  </div>
  <div class="chart-container">
    <div class="chart-title">Активность по часам</div>
    <canvas id="chartByHour" height="220"></canvas>
  </div>
</div>

<!-- Фильтры -->
<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap;
  padding:12px 16px;background:var(--bg-elevated);border-radius:var(--radius-sm);border:1px solid var(--border)">
  <label style="display:flex;align-items:center;gap:6px;color:var(--text-secondary);font-size:13px;cursor:pointer">
    <input type="checkbox" id="fHideFiltered" checked onchange="applyFilters()"
      style="accent-color:var(--accent)"> Скрыть filtered
  </label>
  <label style="display:flex;align-items:center;gap:6px;color:var(--text-secondary);font-size:13px">
    Min ROI %
    <input type="number" id="fMinRoi" value="0" step="5" style="width:64px;padding:4px 8px;
      background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--radius-xs);
      color:var(--text);font-size:13px" onchange="applyFilters()">
  </label>
  <label style="display:flex;align-items:center;gap:6px;color:var(--text-secondary);font-size:13px">
    Min Vol
    <input type="number" id="fMinVol" value="0" step="10" style="width:64px;padding:4px 8px;
      background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--radius-xs);
      color:var(--text);font-size:13px" onchange="applyFilters()">
  </label>
  <span style="color:var(--text-muted);font-size:12px;margin-left:auto" id="filterCounter">
    Total: {total}
  </span>
  <button class="btn btn-danger" style="padding:6px 12px;font-size:12px" onclick="clearPurchases()">
    <i data-lucide="trash-2" style="width:13px;height:13px"></i> Clear All
  </button>
</div>

<div class="table-wrap" style="max-height:70vh;overflow:auto"><div class="table-scroll">
<table id="purchases-table">
<thead style="position:sticky;top:0;z-index:1"><tr>
  <th>Date</th><th>Item</th><th></th><th>Price</th><th>Steam</th>
  <th>Median 30d</th>
  <th class="sortable" data-key="ratio">Ratio</th>
  <th class="sortable" data-key="discount">ROI</th>
  <th class="sortable" data-key="profit">Profit</th>
  <th class="sortable" data-key="median-profit">By Median</th>
  <th class="sortable" data-key="volume">Vol 24h</th>
  <th>Status</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>
</div></div>"""

        extra_js = """<script>
function saveScrollAndReload() {
  const wrap = document.querySelector('.table-wrap');
  if (wrap) sessionStorage.setItem('purchasesScrollTop', wrap.scrollTop);
  sessionStorage.setItem('purchasesPageScroll', window.scrollY);
  location.reload();
}
document.addEventListener('DOMContentLoaded', () => {
  const wrap = document.querySelector('.table-wrap');
  const st = sessionStorage.getItem('purchasesScrollTop');
  const ps = sessionStorage.getItem('purchasesPageScroll');
  if (st !== null && wrap) { wrap.scrollTop = parseInt(st); sessionStorage.removeItem('purchasesScrollTop'); }
  if (ps !== null) { window.scrollTo(0, parseInt(ps)); sessionStorage.removeItem('purchasesPageScroll'); }
});

function applyFilters() {
  const hideFiltered = document.getElementById('fHideFiltered').checked;
  const minRoi = parseFloat(document.getElementById('fMinRoi').value) || 0;
  const minVol = parseInt(document.getElementById('fMinVol').value) || 0;
  const rows = document.querySelectorAll('#purchases-table tbody tr.p-row');
  let shown = 0;
  rows.forEach(row => {
    const status = row.dataset.status || '';
    const discount = parseFloat(row.dataset.discount) || 0;
    const vol = parseInt(row.dataset.volume) || 0;
    let hide = false;
    if (hideFiltered && status === 'filtered') hide = true;
    if (discount < minRoi) hide = true;
    if (vol < minVol) hide = true;
    row.style.display = hide ? 'none' : '';
    if (!hide) shown++;
  });
  document.getElementById('filterCounter').textContent = shown + ' / ' + rows.length;
}
// Применить фильтры при загрузке (скрыть filtered по умолчанию)
document.addEventListener('DOMContentLoaded', applyFilters);

async function clearPurchases() {
  if (!confirm('Delete ALL purchases from database? This cannot be undone.')) return;
  const resp = await fetch('/api/v1/purchases/clear', {method: 'POST', headers: {'X-Fluxio-CSRF': '1'}});
  const data = await resp.json();
  if (data.cleared !== undefined) {
    alert('Deleted: ' + data.cleared + ' purchases');
    location.reload();
  } else {
    alert('Error: ' + (data.error || 'unknown'));
  }
}

document.querySelectorAll('th.sortable').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.key;
    const tbody = document.querySelector('#purchases-table tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
    const isDesc = th.classList.contains('sort-desc');
    const newDir = isDesc ? 'asc' : 'desc';
    document.querySelectorAll('th.sortable').forEach(h => {
      h.classList.remove('sort-asc', 'sort-desc');
    });
    th.classList.add('sort-' + newDir);
    const camelKey = key.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    rows.sort((a, b) => {
      const va = parseFloat(a.dataset[camelKey]) || 0;
      const vb = parseFloat(b.dataset[camelKey]) || 0;
      return newDir === 'desc' ? vb - va : va - vb;
    });
    rows.forEach(r => tbody.appendChild(r));
  });
});

// Сортировка по дисконту по умолчанию (убывание)
{
  const tbody = document.querySelector('#purchases-table tbody');
  if (tbody) {
    const rows = Array.from(tbody.querySelectorAll('tr'));
    rows.sort((a, b) => (parseFloat(b.dataset.discount) || 0) - (parseFloat(a.dataset.discount) || 0));
    rows.forEach(r => tbody.appendChild(r));
    const th = document.querySelector('th[data-key="discount"]');
    if (th) th.classList.add('sort-desc');
  }
}

// ── Графики покупок ──
const chartDefaults = {
  color: '#94a3b8',
  borderColor: 'rgba(255,255,255,0.06)',
};
Chart.defaults.color = chartDefaults.color;
Chart.defaults.borderColor = chartDefaults.borderColor;

const tooltipStyle = {
  backgroundColor: '#1e2538',
  titleColor: '#e2e8f0',
  bodyColor: '#e2e8f0',
  borderColor: '#2a3348',
  borderWidth: 1,
  padding: 10,
  cornerRadius: 8,
};

// График по дням
const dayLabels = """ + chart_day_labels + """;
const dayCounts = """ + chart_day_counts + """;
const daySpent  = """ + chart_day_spent + """;
const dayProfit = """ + chart_day_profit + """;

if (dayLabels.length > 0) {
  new Chart(document.getElementById('chartByDay'), {
    type: 'bar',
    data: {
      labels: dayLabels.map(d => { const parts = d.split('-'); return parts[2] + '.' + parts[1]; }),
      datasets: [
        {
          label: 'Кол-во',
          data: dayCounts,
          backgroundColor: 'rgba(99, 102, 241, 0.7)',
          borderRadius: 4,
          yAxisID: 'y',
          order: 2,
        },
        {
          label: 'Потрачено $',
          data: daySpent,
          type: 'line',
          borderColor: '#f59e0b',
          backgroundColor: 'rgba(245, 158, 11, 0.1)',
          borderWidth: 2,
          pointRadius: 3,
          pointBackgroundColor: '#f59e0b',
          fill: true,
          tension: 0.3,
          yAxisID: 'y1',
          order: 1,
        },
        {
          label: 'Профит $',
          data: dayProfit,
          type: 'line',
          borderColor: '#22c55e',
          borderWidth: 2,
          pointRadius: 3,
          pointBackgroundColor: '#22c55e',
          borderDash: [4, 3],
          tension: 0.3,
          yAxisID: 'y1',
          order: 0,
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'bottom', labels: { boxWidth: 12, padding: 16, usePointStyle: true } },
        tooltip: tooltipStyle,
      },
      scales: {
        x: { grid: { display: false } },
        y:  { position: 'left', title: { display: true, text: 'Кол-во' }, beginAtZero: true, ticks: { stepSize: 1 } },
        y1: { position: 'right', title: { display: true, text: '$' }, beginAtZero: true, grid: { drawOnChartArea: false } },
      }
    }
  });
}

// График по часам
const hourLabels  = """ + chart_hour_labels + """;
const hourCounts  = """ + chart_hour_counts + """;
const hourSpent   = """ + chart_hour_spent + """;
const hourProfit  = """ + chart_hour_profit + """;

new Chart(document.getElementById('chartByHour'), {
  type: 'bar',
  data: {
    labels: hourLabels,
    datasets: [
      {
        label: 'Кол-во',
        data: hourCounts,
        backgroundColor: hourCounts.map((v, i) => {
          const max = Math.max(...hourCounts);
          const alpha = max > 0 ? 0.3 + 0.7 * (v / max) : 0.3;
          return 'rgba(99, 102, 241, ' + alpha + ')';
        }),
        borderRadius: 4,
        yAxisID: 'y',
        order: 2,
      },
      {
        label: 'Потрачено $',
        data: hourSpent,
        type: 'line',
        borderColor: '#f59e0b',
        backgroundColor: 'rgba(245, 158, 11, 0.1)',
        borderWidth: 2,
        pointRadius: 2,
        pointBackgroundColor: '#f59e0b',
        fill: true,
        tension: 0.3,
        yAxisID: 'y1',
        order: 1,
      },
      {
        label: 'Профит $',
        data: hourProfit,
        type: 'line',
        borderColor: '#22c55e',
        borderWidth: 2,
        pointRadius: 2,
        pointBackgroundColor: '#22c55e',
        borderDash: [4, 3],
        tension: 0.3,
        yAxisID: 'y1',
        order: 0,
      }
    ]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { position: 'bottom', labels: { boxWidth: 12, padding: 16, usePointStyle: true } },
      tooltip: tooltipStyle,
    },
    scales: {
      x: { grid: { display: false } },
      y:  { position: 'left', title: { display: true, text: 'Кол-во' }, beginAtZero: true, ticks: { stepSize: 1 } },
      y1: { position: 'right', title: { display: true, text: '$' }, beginAtZero: true, grid: { drawOnChartArea: false } },
    }
  }
});
// ── Manual Buy ──
let buyInProgress = false;

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function modalLoading() {
  return '<div class="modal-state"><div class="modal-spinner"></div><div class="modal-state-text">Загрузка...</div></div>';
}

function modalError(msg, closeFn) {
  closeFn = closeFn || 'closeBuyModal()';
  return '<div class="modal-state">'
    + '<div class="modal-icon modal-icon-error">!</div>'
    + '<div class="modal-state-title">Ошибка</div>'
    + '<div class="modal-state-text">' + escHtml(msg) + '</div>'
    + '<button class="modal-btn modal-btn-secondary" onclick="' + closeFn + '">Закрыть</button>'
    + '</div>';
}

async function openBuyDialog(btn) {
  if (buyInProgress) return;
  const name = btn.dataset.name;
  const modal = document.getElementById('buyModal');
  const body = document.getElementById('buyModalBody');
  modal.classList.add('active');
  body.innerHTML = modalLoading();

  try {
    const resp = await fetch('/api/v1/manual-buy/preflight?market_hash_name=' + encodeURIComponent(name));
    const data = await resp.json();
    if (!data.available) {
      body.innerHTML = modalError(data.error || 'Предмет недоступен');
      return;
    }
    const profitColor = data.expected_profit_usd >= 0 ? 'var(--success)' : 'var(--danger)';
    const imgTag = data.image_url
      ? '<img src="' + escHtml(data.image_url) + '" class="modal-item-img" referrerpolicy="no-referrer">' : '';
    const steamUrl = 'https://steamcommunity.com/market/listings/570/' + encodeURIComponent(data.market_hash_name);

    body.innerHTML = ''
      + '<div class="modal-header">'
      +   imgTag
      +   '<a href="' + steamUrl + '" target="_blank" class="modal-item-name">' + escHtml(data.market_hash_name) + ' <span class="modal-external">&#8599;</span></a>'
      + '</div>'
      + '<div class="modal-divider"></div>'
      + '<div class="modal-grid">'
      +   '<div class="modal-row"><span class="modal-label">Цена CS2DT</span><span class="modal-value" style="color:var(--accent);font-weight:700">$' + data.cheapest_price_usd.toFixed(4) + '</span></div>'
      +   '<div class="modal-row"><span class="modal-label">Цена Steam</span><span class="modal-value">$' + data.steam_price_usd.toFixed(4) + '</span></div>'
      +   '<div class="modal-row"><span class="modal-label">Net Steam</span><span class="modal-value">$' + data.net_steam_usd.toFixed(4) + '</span></div>'
      + '</div>'
      + '<div class="modal-highlight">'
      +   '<div class="modal-highlight-item"><div class="modal-highlight-label">ROI</div><div class="modal-highlight-value" style="color:' + profitColor + '">' + data.discount_percent.toFixed(1) + '%</div></div>'
      +   '<div class="modal-highlight-item"><div class="modal-highlight-label">Профит</div><div class="modal-highlight-value" style="color:' + profitColor + '">$' + (data.expected_profit_usd || 0).toFixed(4) + '</div></div>'
      + '</div>'
      + '<div class="modal-divider"></div>'
      + '<div class="modal-grid">'
      +   '<div class="modal-row"><span class="modal-label">Баланс</span><span class="modal-value">$' + data.balance_usd.toFixed(2) + '</span></div>'
      +   '<div class="modal-row"><span class="modal-label">После покупки</span><span class="modal-value" style="font-weight:600">$' + data.balance_after_usd.toFixed(2) + '</span></div>'
      +   '<div class="modal-row"><span class="modal-label">Объём 24ч</span><span class="modal-value">' + data.volume_24h + '</span></div>'
      +   '<div class="modal-row"><span class="modal-label">Лотов</span><span class="modal-value">' + data.listings_count + '</span></div>'
      + '</div>'
      + '<div class="modal-actions">'
      +   '<button class="modal-btn modal-btn-secondary" onclick="closeBuyModal()">Отмена</button>'
      +   '<button class="modal-btn modal-btn-primary" id="confirmBuyBtn" data-name="' + escHtml(data.market_hash_name) + '" data-price="' + data.cheapest_price_usd + '" onclick="confirmBuy(this.dataset.name, parseFloat(this.dataset.price))">Купить</button>'
      + '</div>';
  } catch (e) {
    body.innerHTML = modalError(e.message);
  }
}

async function confirmBuy(name, maxPrice) {
  if (buyInProgress) return;
  buyInProgress = true;
  const btn = document.getElementById('confirmBuyBtn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="modal-spinner modal-spinner-sm"></span> Покупка...'; }

  try {
    const resp = await fetch('/api/v1/manual-buy', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Fluxio-CSRF': '1'},
      body: JSON.stringify({market_hash_name: name, max_price_usd: maxPrice})
    });
    const data = await resp.json();
    const body = document.getElementById('buyModalBody');
    if (data.ok) {
      body.innerHTML = '<div class="modal-state">'
        + '<div class="modal-icon modal-icon-success">&#10003;</div>'
        + '<div class="modal-state-title">Куплено!</div>'
        + '<div class="modal-state-details">'
        +   '<span>' + escHtml(name) + '</span>'
        +   '<span class="modal-state-meta">$' + data.buy_price.toFixed(4) + ' &middot; ' + (data.delivery === 2 ? 'Авто' : 'P2P') + '</span>'
        +   '<span class="modal-state-meta">Ордер: ' + escHtml(data.order_id) + '</span>'
        + '</div>'
        + '<button class="modal-btn modal-btn-secondary" onclick="closeBuyModal();saveScrollAndReload()">Закрыть</button>'
        + '</div>';
    } else {
      body.innerHTML = modalError(data.error || 'Неизвестная ошибка');
    }
  } catch (e) {
    document.getElementById('buyModalBody').innerHTML = modalError('Сетевая ошибка: ' + e.message);
  } finally {
    buyInProgress = false;
  }
}

function closeBuyModal() {
  document.getElementById('buyModal').classList.remove('active');
}
</script>"""

        # Модальное окно покупки
        buy_modal_html = """
<div id="buyModal" class="modal-overlay" onclick="if(event.target===this)closeBuyModal()">
  <div class="modal-card">
    <div id="buyModalBody"></div>
  </div>
</div>

<style>
/* ── Modal Overlay ── */
.modal-overlay {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 9999;
  background: rgba(4, 6, 14, 0.6);
  align-items: center;
  justify-content: center;
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  opacity: 0;
  transition: opacity 0.2s ease;
}
.modal-overlay.active {
  display: flex;
  opacity: 1;
}

/* ── Modal Card ── */
.modal-card {
  background: var(--bg-elevated);
  border: 1px solid var(--border-light);
  border-radius: 16px;
  padding: 0;
  max-width: 400px;
  width: 92%;
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.6), 0 0 0 1px rgba(255,255,255,0.03) inset;
  transform: translateY(8px);
  animation: modalSlideUp 0.2s ease forwards;
}
@keyframes modalSlideUp {
  to { transform: translateY(0); }
}

/* ── Header ── */
.modal-header {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 24px 24px 16px;
  gap: 10px;
}
.modal-item-img {
  width: 56px;
  height: 56px;
  object-fit: contain;
  border-radius: 10px;
  background: var(--bg-surface);
  padding: 4px;
}
.modal-item-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  text-decoration: none;
  text-align: center;
  line-height: 1.4;
  transition: color 0.15s;
}
.modal-item-name:hover { color: var(--accent); }
.modal-external { font-size: 11px; opacity: 0.4; }

/* ── Divider ── */
.modal-divider {
  height: 1px;
  background: var(--border);
  margin: 0 20px;
}

/* ── Grid Rows ── */
.modal-grid { padding: 12px 24px; }
.modal-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 7px 0;
}
.modal-label {
  font-size: 13px;
  color: var(--text-muted);
}
.modal-value {
  font-size: 13px;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}

/* ── Highlight Block (ROI + Profit) ── */
.modal-highlight {
  display: flex;
  gap: 12px;
  padding: 12px 24px;
}
.modal-highlight-item {
  flex: 1;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px;
  text-align: center;
}
.modal-highlight-label {
  font-size: 11px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}
.modal-highlight-value {
  font-size: 20px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

/* ── Actions ── */
.modal-actions {
  display: flex;
  gap: 10px;
  padding: 16px 24px 24px;
}
.modal-btn {
  flex: 1;
  padding: 10px 16px;
  font-size: 13px;
  font-weight: 600;
  border: none;
  border-radius: 10px;
  cursor: pointer;
  transition: all 0.15s ease;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}
.modal-btn-primary {
  background: var(--success);
  color: #fff;
  box-shadow: 0 2px 8px rgba(34, 197, 94, 0.25);
}
.modal-btn-primary:hover {
  background: #16a34a;
  box-shadow: 0 4px 16px rgba(34, 197, 94, 0.35);
  transform: translateY(-1px);
}
.modal-btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  transform: none;
  box-shadow: none;
}
.modal-btn-secondary {
  background: var(--bg-surface);
  color: var(--text-secondary);
  border: 1px solid var(--border);
}
.modal-btn-secondary:hover {
  background: var(--bg-hover);
  color: var(--text);
}

/* ── State Screens (loading, success, error) ── */
.modal-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 40px 24px 32px;
  gap: 12px;
  text-align: center;
}
.modal-state-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--text);
}
.modal-state-text {
  font-size: 13px;
  color: var(--text-muted);
  max-width: 280px;
}
.modal-state-details {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
  color: var(--text-secondary);
}
.modal-state-meta {
  font-size: 12px;
  color: var(--text-muted);
}

/* ── Icons ── */
.modal-icon {
  width: 48px;
  height: 48px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 22px;
  font-weight: 700;
  margin-bottom: 4px;
}
.modal-icon-success {
  background: rgba(34, 197, 94, 0.12);
  color: var(--success);
  border: 2px solid rgba(34, 197, 94, 0.25);
}
.modal-icon-error {
  background: rgba(239, 68, 68, 0.12);
  color: var(--danger);
  border: 2px solid rgba(239, 68, 68, 0.25);
}

/* ── Spinner ── */
.modal-spinner {
  width: 32px;
  height: 32px;
  border: 3px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: modalSpin 0.7s linear infinite;
}
.modal-spinner-sm {
  width: 14px;
  height: 14px;
  border-width: 2px;
  display: inline-block;
}
@keyframes modalSpin { to { transform: rotate(360deg); } }

/* ── Table Buy Button ── */
.btn-manual-buy {
  padding: 4px 12px;
  font-size: 12px;
  font-weight: 600;
  border: 1px solid rgba(34, 197, 94, 0.3);
  border-radius: 6px;
  cursor: pointer;
  background: rgba(34, 197, 94, 0.1);
  color: var(--success);
  transition: all 0.15s;
  white-space: nowrap;
}
.btn-manual-buy:hover {
  background: rgba(34, 197, 94, 0.2);
  border-color: var(--success);
}

/* ── Scrollbar for modal ── */
.modal-card::-webkit-scrollbar { width: 4px; }
.modal-card::-webkit-scrollbar-track { background: transparent; }
.modal-card::-webkit-scrollbar-thumb { background: var(--border-light); border-radius: 4px; }
</style>
"""

        return _layout("Purchases", "/purchases", content + buy_modal_html,
                       extra_head='<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>') + extra_js

    # ─── Audit ─────────────────────────────────────────────────────────────────

    @app.get("/audit", response_class=HTMLResponse)
    async def page_audit() -> str:
        """Аудит покупок по правилам buy order."""
        from urllib.parse import quote

        from fluxio.config import FeesConfig
        from fluxio.db.session import async_session_factory
        from fluxio.db.unit_of_work import UnitOfWork

        am = config.anti_manipulation
        passed_rows: list[str] = []
        failed_rows: list[str] = []
        stats = {
            "total": 0, "passed": 0, "failed": 0,
            "spent_passed": 0.0, "spent_failed": 0.0,
            "fail_reasons": {},
        }

        try:
            async with UnitOfWork(async_session_factory) as uow:
                all_purchases = await uow.purchases.get_all(limit=100_000)

                items_cache: dict[str, object] = {}
                for p in all_purchases:
                    if p.market_hash_name not in items_cache:
                        items_cache[p.market_hash_name] = await uow.items.get_by_name(
                            p.market_hash_name,
                        )

                for p in all_purchases:
                    if p.status not in ("success", "pending"):
                        continue

                    price = float(p.price_usd) if p.price_usd else 0
                    steam = float(p.steam_price_usd) if p.steam_price_usd else 0
                    item = items_cache.get(p.market_hash_name)

                    buy_order = float(item.steam_buy_order_price) if item and item.steam_buy_order_price else 0
                    median_30d = float(item.steam_median_30d) if item and item.steam_median_30d else 0
                    listings = int(item.steam_sell_listings) if item and item.steam_sell_listings else 0
                    cv = float(item.price_stability_cv) if item and item.price_stability_cv else 0
                    spike = float(item.price_spike_ratio) if item and item.price_spike_ratio else 0
                    vol_7d = int(item.steam_volume_7d) if item and item.steam_volume_7d else 0
                    img_url = item.image_url if item and getattr(item, "image_url", None) else ""

                    # ── Проверки buy order ──
                    verdict = "PASS"
                    reason = ""

                    if buy_order <= 0:
                        verdict = "FAIL"
                        reason = "Нет buy orders"
                    elif steam > 0 and buy_order > 0:
                        spread_ratio = steam / buy_order
                        is_cheap = steam <= am.cheap_price_threshold
                        max_spread = am.max_spread_ratio_cheap if is_cheap else am.max_spread_ratio_normal
                        if spread_ratio > max_spread:
                            verdict = "FAIL"
                            reason = f"Spread {spread_ratio:.2f}x > {max_spread}x"
                        elif not is_cheap:
                            net_bo = FeesConfig.calc_net_steam(
                                buy_order,
                                config.fees.steam_valve_fee_percent,
                                config.fees.steam_game_fee_percent,
                            )
                            if net_bo < price:
                                verdict = "FAIL"
                                reason = f"Убыток по BO: net=${net_bo:.4f} < cs2dt=${price:.4f}"

                    # Прибыль
                    net_steam = FeesConfig.calc_net_steam(steam) if steam > 0 else 0
                    profit = net_steam - price if steam > 0 and price > 0 else 0
                    net_bo_val = FeesConfig.calc_net_steam(buy_order) if buy_order > 0 else 0
                    bo_profit = net_bo_val - price if buy_order > 0 and price > 0 else 0

                    spread_str = f"{steam / buy_order:.2f}x" if buy_order > 0 and steam > 0 else "—"

                    stats["total"] += 1
                    disc = float(p.discount_percent) if p.discount_percent else 0
                    ts = p.purchased_at.strftime("%m-%d %H:%M") if p.purchased_at else "—"
                    steam_url = f"https://steamcommunity.com/market/listings/570/{quote(p.market_hash_name)}"

                    img_tag = (
                        f'<img src="{_esc(img_url)}" alt="" '
                        f'style="width:32px;height:32px;object-fit:contain;border-radius:4px;vertical-align:middle">'
                        if img_url else '<div style="width:32px;height:32px;background:var(--bg-hover);border-radius:4px;display:inline-block;vertical-align:middle"></div>'
                    )

                    profit_color = "var(--success)" if profit >= 0 else "var(--danger)"
                    bo_profit_color = "var(--success)" if bo_profit >= 0 else "var(--danger)"

                    row = f"""<tr>
<td style="white-space:nowrap">{ts}</td>
<td>
  <div style="display:flex;align-items:center;gap:8px">
    {img_tag}
    <a href="{_esc(steam_url)}" target="_blank" rel="noopener"
       style="color:var(--accent);text-decoration:none;font-size:13px"
       title="{_esc(p.market_hash_name)}">{_esc(p.market_hash_name[:40])}</a>
  </div>
</td>
<td>${price:.4f}</td>
<td>${steam:.4f}</td>
<td>${buy_order:.4f}</td>
<td>{spread_str}</td>
<td>${median_30d:.4f}</td>
<td>{disc:.1f}%</td>
<td style="color:{profit_color}">${profit:.4f}</td>
<td style="color:{bo_profit_color}">${bo_profit:.4f}</td>
<td>{vol_7d}</td>
"""

                    if verdict == "PASS":
                        stats["passed"] += 1
                        stats["spent_passed"] += price
                        row += "</tr>"
                        passed_rows.append(row)
                    else:
                        stats["failed"] += 1
                        stats["spent_failed"] += price
                        stats["fail_reasons"][reason] = stats["fail_reasons"].get(reason, 0) + 1
                        row += f'<td><span class="badge badge-danger" style="font-size:10px;white-space:nowrap">{_esc(reason)}</span></td></tr>'
                        failed_rows.append(row)

        except Exception as e:
            logger.error(f"Audit page error: {e}")
            return _layout("Audit", "/audit", f'<div class="stat-card"><p style="color:var(--danger)">Ошибка: {_esc(str(e))}</p></div>')

        # ── Карточки статистики ──
        fail_pct = (stats["failed"] / stats["total"] * 100) if stats["total"] else 0
        reasons_html = ""
        for r, cnt in sorted(stats["fail_reasons"].items(), key=lambda x: -x[1]):
            reasons_html += f'<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)"><span style="font-size:12px">{_esc(r)}</span><span class="badge badge-danger" style="font-size:11px">{cnt}</span></div>'

        summary = f"""
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:24px">
  <div class="stat-card">
    <div class="stat-label">Всего покупок</div>
    <div class="stat-value">{stats['total']}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Прошли аудит</div>
    <div class="stat-value" style="color:var(--success)">{stats['passed']}</div>
    <div class="stat-label">${stats['spent_passed']:.2f}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Не прошли</div>
    <div class="stat-value" style="color:var(--danger)">{stats['failed']} ({fail_pct:.1f}%)</div>
    <div class="stat-label">${stats['spent_failed']:.2f}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Причины отклонения</div>
    {reasons_html if reasons_html else '<div class="stat-label">—</div>'}
  </div>
</div>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:24px;padding:12px;background:var(--bg-surface);border-radius:var(--radius-sm);border:1px solid var(--border)">
  <div><span style="color:var(--text-muted);font-size:12px">Spread (дешёвые ≤${am.cheap_price_threshold})</span><br><strong>max {am.max_spread_ratio_cheap}x</strong></div>
  <div><span style="color:var(--text-muted);font-size:12px">Spread (обычные)</span><br><strong>max {am.max_spread_ratio_normal}x</strong></div>
  <div><span style="color:var(--text-muted);font-size:12px">Buy orders обязательны</span><br><strong>{'Да' if am.require_buy_orders else 'Нет'}</strong></div>
</div>"""

        th = """<tr>
<th>Дата</th><th>Предмет</th><th>CS2DT</th><th>Steam</th>
<th>Buy Order</th><th>Spread</th><th>Медиана 30д</th><th>Дисконт</th>
<th>Прибыль</th><th>Прибыль (BO)</th><th>Vol 7д</th>"""

        failed_section = ""
        if failed_rows:
            failed_section = f"""
<h2 style="color:var(--danger);margin:24px 0 12px;font-size:18px">
  <i data-lucide="x-circle" style="width:20px;height:20px;vertical-align:middle"></i>
  Не прошли ({stats['failed']})
</h2>
<div class="table-wrap">
<table class="data-table">
<thead>{th}<th>Причина</th></tr></thead>
<tbody>{''.join(failed_rows)}</tbody>
</table>
</div>"""

        passed_section = f"""
<h2 style="color:var(--success);margin:24px 0 12px;font-size:18px">
  <i data-lucide="check-circle" style="width:20px;height:20px;vertical-align:middle"></i>
  Прошли аудит ({stats['passed']})
</h2>
<div class="table-wrap">
<table class="data-table">
<thead>{th}</tr></thead>
<tbody>{''.join(passed_rows)}</tbody>
</table>
</div>"""

        extra_css = """
.table-wrap { overflow-x:auto; margin-bottom:24px; border:1px solid var(--border); border-radius:var(--radius-sm); }
.data-table { width:100%; border-collapse:collapse; font-size:13px; }
.data-table th { position:sticky; top:0; background:var(--bg-elevated); padding:10px 12px; text-align:left;
  font-size:11px; text-transform:uppercase; color:var(--text-muted); letter-spacing:0.05em;
  border-bottom:1px solid var(--border); white-space:nowrap; }
.data-table td { padding:8px 12px; border-bottom:1px solid var(--border); white-space:nowrap; color:var(--text-secondary); }
.data-table tbody tr:hover { background:var(--bg-hover); }
"""

        content = summary + failed_section + passed_section

        return _layout("Audit — Buy Order", "/audit", content, extra_css=extra_css)

    # ─── Price Map — тепловая карта продаж ────────────────────────────────────

    @app.get("/price-map", response_class=HTMLResponse)
    async def page_price_map(request: Request) -> str:
        """Тепловая карта продаж предмета на Steam Market по ценовым точкам."""
        from collections import Counter
        from datetime import timedelta
        from urllib.parse import quote

        item_name = request.query_params.get("item", "").strip()

        # ── Форма поиска ──
        search_form = f"""
<div class="stat-card" style="margin-bottom:20px;">
  <form method="get" action="/price-map" style="display:flex; gap:12px; align-items:end; flex-wrap:wrap;">
    <div style="flex:1; min-width:300px;">
      <label style="color:var(--text-secondary); font-size:12px; display:block; margin-bottom:4px;">
        Market Hash Name предмета
      </label>
      <input type="text" name="item" value="{_esc(item_name)}"
        placeholder="Abdomen of the Glutton's Larder"
        style="width:100%; padding:10px 14px; background:var(--bg); border:1px solid var(--border);
               border-radius:8px; color:var(--text); font-size:14px; outline:none;"
        onfocus="this.style.borderColor='var(--accent)'"
        onblur="this.style.borderColor='var(--border)'"
      >
    </div>
    <button type="submit" style="padding:10px 24px; background:var(--accent); color:#fff;
            border:none; border-radius:8px; cursor:pointer; font-size:14px; font-weight:600;
            height:42px;">
      Анализ
    </button>
  </form>
</div>
"""

        if not item_name:
            return _layout("Price Map", "/price-map", search_form)

        # ── Получить Steam клиент ──
        steam = None
        try:
            if _container:
                from fluxio.api.steam_client import SteamClient
                if _container.is_initialized(SteamClient):
                    steam = await _container.get(SteamClient)
        except Exception:
            pass

        if not steam:
            err = '<div class="stat-card"><p style="color:var(--danger);">Steam клиент недоступен. Запустите бота.</p></div>'
            return _layout("Price Map", "/price-map", search_form + err)

        # ── Запросить историю ──
        try:
            history = await steam.get_price_history(item_name, app_id=570)
        except Exception as e:
            err = f'<div class="stat-card"><p style="color:var(--danger);">Ошибка API: {_esc(str(e))}</p></div>'
            return _layout("Price Map", "/price-map", search_form + err)

        if not history:
            err = '<div class="stat-card"><p style="color:var(--warning);">Нет данных. Возможно предмет не найден или куки протухли.</p></div>'
            return _layout("Price Map", "/price-map", search_form + err)

        # ── Парсинг и агрегация ──
        cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)
        cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)

        # Продажи по цене: {price_cents: total_volume}
        sales_by_price_30d: Counter[int] = Counter()
        sales_by_price_7d: Counter[int] = Counter()
        all_prices: list[float] = []
        daily_volume: Counter[str] = Counter()  # "YYYY-MM-DD" → volume

        for entry in history:
            if len(entry) < 3:
                continue
            date_str, price, volume_str = entry[0], entry[1], entry[2]
            try:
                clean = date_str.split(": +")[0].strip()
                dt = datetime.strptime(clean, "%b %d %Y %H")
                dt = dt.replace(tzinfo=timezone.utc)
            except (ValueError, IndexError):
                continue

            vol = int(str(volume_str).replace(",", "")) if volume_str else 0
            price_f = float(price)
            price_cents = round(price_f * 100)  # в центах для точной группировки

            if dt >= cutoff_30d:
                sales_by_price_30d[price_cents] += vol
                all_prices.extend([price_f] * vol)
                day_key = dt.strftime("%Y-%m-%d")
                daily_volume[day_key] += vol

            if dt >= cutoff_7d:
                sales_by_price_7d[price_cents] += vol

        if not all_prices:
            err = '<div class="stat-card"><p style="color:var(--warning);">Нет продаж за последние 30 дней.</p></div>'
            return _layout("Price Map", "/price-map", search_form + err)

        import statistics as stat_mod

        total_sales = len(all_prices)
        median_price = stat_mod.median(all_prices)
        mean_price = stat_mod.mean(all_prices)
        min_price = min(all_prices)
        max_price = max(all_prices)

        # ── Данные для гистограммы (отсортировано по цене) ──
        sorted_prices_30d = sorted(sales_by_price_30d.items())
        max_volume = max(sales_by_price_30d.values()) if sales_by_price_30d else 1

        # ── Steam ссылка ──
        steam_url = f"https://steamcommunity.com/market/listings/570/{quote(item_name, safe='')}"

        # ── Информация из БД (если есть) ──
        db_info = ""
        try:
            from fluxio.db.session import async_session_factory
            from fluxio.db.unit_of_work import UnitOfWork
            async with UnitOfWork(async_session_factory) as uow:
                db_item = await uow.items.get_by_name(item_name)
                if db_item:
                    cs2dt_p = float(db_item.price_usd) if db_item.price_usd else 0
                    steam_p = float(db_item.steam_price_usd) if db_item.steam_price_usd else 0
                    buy_order = float(db_item.steam_buy_order_price) if db_item.steam_buy_order_price else 0
                    median_30d = float(db_item.steam_median_30d) if db_item.steam_median_30d else 0
                    listings = int(db_item.steam_sell_listings) if db_item.steam_sell_listings else 0
                    vol_7d = int(db_item.steam_volume_7d) if db_item.steam_volume_7d else 0

                    # Рассчитать net
                    from fluxio.config import FeesConfig
                    net_listing = FeesConfig.calc_net_steam(steam_p) if steam_p else 0
                    net_buy = FeesConfig.calc_net_steam(buy_order) if buy_order else 0
                    roi_listing = ((net_listing - cs2dt_p) / cs2dt_p * 100) if cs2dt_p > 0 and net_listing > 0 else 0
                    roi_buy = ((net_buy - cs2dt_p) / cs2dt_p * 100) if cs2dt_p > 0 and net_buy > 0 else 0

                    db_info = f"""
<div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:20px;">
  <div class="stat-card">
    <div class="stat-label">CS2DT цена</div>
    <div class="stat-value" style="color:var(--accent);">${cs2dt_p:.2f}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Steam листинг</div>
    <div class="stat-value">${steam_p:.2f}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Buy Order</div>
    <div class="stat-value" style="color:{'var(--danger)' if buy_order <= 0 else 'var(--success)'};">${buy_order:.2f}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Медиана 30д (БД)</div>
    <div class="stat-value">${median_30d:.2f}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Листингов</div>
    <div class="stat-value">{listings}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Объём 7д</div>
    <div class="stat-value">{vol_7d}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">ROI (листинг)</div>
    <div class="stat-value" style="color:{'var(--success)' if roi_listing > 0 else 'var(--danger)'};">{roi_listing:+.1f}%</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">ROI (buy order)</div>
    <div class="stat-value" style="color:{'var(--success)' if roi_buy > 0 else 'var(--danger)'};">{roi_buy:+.1f}%</div>
  </div>
</div>
"""
        except Exception:
            pass

        # ── Сводка ──
        summary_html = f"""
<div style="display:flex; align-items:center; gap:16px; margin-bottom:20px; flex-wrap:wrap;">
  <h2 style="margin:0; color:var(--text);">{_esc(item_name)}</h2>
  <a href="{steam_url}" target="_blank" rel="noopener"
     style="padding:6px 16px; background:#1b2838; color:#66c0f4; border-radius:6px;
            text-decoration:none; font-size:13px; font-weight:500; border:1px solid #2a475e;">
    Steam Market &rarr;
  </a>
</div>
{db_info}
<div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:12px; margin-bottom:24px;">
  <div class="stat-card">
    <div class="stat-label">Продаж (30д)</div>
    <div class="stat-value">{total_sales:,}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Медиана</div>
    <div class="stat-value">${median_price:.2f}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Средняя</div>
    <div class="stat-value">${mean_price:.2f}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Мин / Макс</div>
    <div class="stat-value" style="font-size:16px;">${min_price:.2f} — ${max_price:.2f}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Ценовых точек</div>
    <div class="stat-value">{len(sales_by_price_30d)}</div>
  </div>
</div>
"""

        # ── Гистограмма (CSS bars) ──
        bars_html = ""
        # Топ-цены для подсветки
        top_5_prices = set(p for p, _ in sales_by_price_30d.most_common(5))

        for price_cents, vol in sorted_prices_30d:
            price_usd = price_cents / 100
            pct = vol / max_volume * 100
            is_top = price_cents in top_5_prices
            vol_7d = sales_by_price_7d.get(price_cents, 0)

            # Цвет: тепловая карта от синего (мало) к красному (много)
            intensity = vol / max_volume
            if intensity > 0.8:
                color = "#ef4444"  # красный — пик продаж
            elif intensity > 0.5:
                color = "#f97316"  # оранжевый
            elif intensity > 0.25:
                color = "#eab308"  # жёлтый
            elif intensity > 0.1:
                color = "#22c55e"  # зелёный
            else:
                color = "#3b82f6"  # синий — мало продаж

            label_style = "font-weight:700; color:var(--text);" if is_top else "color:var(--text-secondary);"
            bars_html += f"""
<div class="price-row" style="{'background:rgba(255,255,255,0.03);' if is_top else ''}">
  <div class="price-label" style="{label_style}">${price_usd:.2f}</div>
  <div class="bar-cell">
    <div class="bar" style="width:{max(pct, 0.5):.1f}%; background:{color};"></div>
  </div>
  <div class="vol-label">{vol:,}</div>
  <div class="vol-7d-label">{vol_7d if vol_7d else '—'}</div>
</div>
"""

        chart_html = f"""
<div class="stat-card" style="padding:0; overflow:hidden;">
  <div style="padding:16px 20px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center;">
    <h3 style="margin:0; font-size:15px; color:var(--text);">Распределение продаж по цене (30 дней)</h3>
    <div style="display:flex; gap:16px; font-size:11px; color:var(--text-secondary);">
      <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#ef4444;margin-right:4px;"></span>Пик</span>
      <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#f97316;margin-right:4px;"></span>Высокий</span>
      <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#eab308;margin-right:4px;"></span>Средний</span>
      <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#22c55e;margin-right:4px;"></span>Умеренный</span>
      <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#3b82f6;margin-right:4px;"></span>Мало</span>
    </div>
  </div>
  <div style="padding:8px 16px;">
    <div class="price-row" style="border-bottom:1px solid var(--border); padding-bottom:6px; margin-bottom:6px;">
      <div class="price-label" style="font-weight:600; color:var(--text-secondary); font-size:11px;">ЦЕНА</div>
      <div class="bar-cell" style="font-weight:600; color:var(--text-secondary); font-size:11px;">ОБЪЁМ ПРОДАЖ</div>
      <div class="vol-label" style="font-weight:600; color:var(--text-secondary); font-size:11px;">30Д</div>
      <div class="vol-7d-label" style="font-weight:600; color:var(--text-secondary); font-size:11px;">7Д</div>
    </div>
    {bars_html}
  </div>
</div>
"""

        # ── Дневной объём (Chart.js) ──
        sorted_days = sorted(daily_volume.items())
        day_labels = [d for d, _ in sorted_days]
        day_values = [v for _, v in sorted_days]

        daily_chart_html = f"""
<div class="stat-card" style="margin-top:20px;">
  <h3 style="margin:0 0 16px; font-size:15px; color:var(--text);">Дневной объём продаж</h3>
  <canvas id="dailyVolumeChart" height="200"></canvas>
</div>
<script>
document.addEventListener('DOMContentLoaded', function() {{
  const ctx = document.getElementById('dailyVolumeChart');
  if (ctx && typeof Chart !== 'undefined') {{
    new Chart(ctx, {{
      type: 'bar',
      data: {{
        labels: {day_labels},
        datasets: [{{
          label: 'Продажи',
          data: {day_values},
          backgroundColor: 'rgba(59,130,246,0.6)',
          borderColor: 'rgba(59,130,246,1)',
          borderWidth: 1,
          borderRadius: 3,
        }}]
      }},
      options: {{
        responsive: true,
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
          x: {{
            ticks: {{ color: '#888', maxRotation: 45 }},
            grid: {{ color: 'rgba(255,255,255,0.05)' }}
          }},
          y: {{
            ticks: {{ color: '#888' }},
            grid: {{ color: 'rgba(255,255,255,0.05)' }}
          }}
        }}
      }}
    }});
  }}
}});
</script>
"""

        # ── CSS для гистограммы ──
        extra_css = """
.price-row { display:flex; align-items:center; gap:8px; padding:3px 0; }
.price-label { width:60px; text-align:right; font-size:13px; font-family:'Courier New',monospace; flex-shrink:0; }
.bar-cell { flex:1; min-width:0; }
.bar { height:20px; border-radius:3px; transition:width 0.3s; min-width:2px; }
.vol-label { width:55px; text-align:right; font-size:12px; color:var(--text-secondary); font-family:'Courier New',monospace; flex-shrink:0; }
.vol-7d-label { width:45px; text-align:right; font-size:12px; color:var(--text-secondary); font-family:'Courier New',monospace; flex-shrink:0; }
.price-row:hover { background:rgba(255,255,255,0.05) !important; }
.price-row:hover .bar { opacity:0.85; }
"""

        content = search_form + summary_html + chart_html + daily_chart_html

        return _layout(
            "Price Map", "/price-map", content,
            extra_head='<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>',
            extra_css=extra_css,
        )

    return app


# Обратная совместимость — app для uvicorn без контейнера
app = create_app()
