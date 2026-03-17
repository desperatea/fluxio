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
from loguru import logger

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
    const res = await fetch(`/api/v1/workers/${{name}}/${{action}}`, {{method:'POST'}});
    if (res.ok) location.reload();
    else alert('Ошибка: ' + (await res.json()).error);
  }} catch(e) {{ alert('Ошибка: ' + e.message); }}
}}
</script>
</body>
</html>"""


# ─── App Factory ─────────────────────────────────────────────────────────────


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
                total_items = (await uow._session.execute(
                    select(sa_func.count()).select_from(Item)
                )).scalar() or 0
                enriched_count = (await uow._session.execute(
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
                result = await uow._session.execute(
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
                        err_html = f'<div style="color:var(--danger);font-size:11px;margin-top:4px">{st.last_error}</div>'
                    if st.paused:
                        btn = f'<button class="worker-btn" onclick="workerAction(\'{st.name}\',\'resume\')">&#9654; Resume</button>'
                    else:
                        btn = f'<button class="worker-btn" onclick="workerAction(\'{st.name}\',\'pause\')">&#9646;&#9646; Pause</button>'
                    workers_html += f"""<div class="worker-card">
                      <div class="worker-dot {dot_class}"></div>
                      <div class="worker-info">
                        <div class="worker-name">{st.name}</div>
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

    # ─── Catalog (/items) ──────────────────────────────────────────────────────

    @app.get("/items", response_class=HTMLResponse)
    async def page_catalog() -> str:
        """Каталог предметов из БД — карточки с фильтрацией."""
        items_data: list[dict[str, Any]] = []
        total = 0

        try:
            from fluxio.db.session import async_session_factory
            from fluxio.db.unit_of_work import UnitOfWork
            async with UnitOfWork(async_session_factory) as uow:
                all_items = await uow.items.get_all()
                total = len(all_items)
                fee = config.fees.steam_fee_percent / 100
                for item in all_items[:200]:
                    p = float(item.price_usd) if item.price_usd else None
                    sp = float(item.steam_price_usd) if item.steam_price_usd else None
                    d_val = 0.0
                    if p and sp and sp > 0:
                        net = sp * (1 - fee)
                        d_val = (net - p) / net * 100 if net > 0 else 0
                    items_data.append({
                        "name": item.market_hash_name,
                        "price": p,
                        "steam": sp,
                        "discount": d_val,
                        "volume": item.steam_volume_24h or 0,
                        "updated": item.steam_updated_at.strftime("%H:%M") if hasattr(item, "steam_updated_at") and item.steam_updated_at else "",
                    })
        except Exception as e:
            logger.exception("Dashboard: ошибка на странице каталога")

        # Генерируем карточки (первые 40 видимы, остальные скрыты)
        PAGE_SIZE = 40
        cards_html = ""
        for idx, it in enumerate(items_data):
            price_str = f"${it['price']:.2f}" if it["price"] else "—"
            steam_str = f"${it['steam']:.2f}" if it["steam"] else "—"
            discount_badge = ""
            if it["discount"] >= config.trading.min_discount_percent:
                discount_badge = f'<span class="badge badge-success">{it["discount"]:.1f}%</span>'
            elif it["discount"] > 0:
                discount_badge = f'<span class="badge badge-info">{it["discount"]:.1f}%</span>'

            hidden = ' style="display:none"' if idx >= PAGE_SIZE else ""
            cards_html += f"""<div class="item-card" data-discount="{it['discount']:.2f}" data-volume="{it['volume']}" data-price="{it['price'] or 0}"{hidden}>
  <div class="item-img">
    <i data-lucide="package" style="width:40px;height:40px;color:var(--text-muted)"></i>
  </div>
  <div class="item-name" title="{it['name']}">{it['name']}</div>
  <div class="item-rarity">Vol: {it['volume']} {discount_badge}</div>
  <div class="item-footer">
    <div class="item-price">{price_str}</div>
    <div style="font-size:11px;color:var(--text-muted)">Steam: {steam_str}</div>
  </div>
</div>"""

        if not cards_html:
            cards_html = '<div class="empty" style="grid-column:1/-1"><p>No items found</p></div>'

        content = f"""
<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:8px">
  <div style="color:var(--text-muted);font-size:13px">
    Total: <strong style="color:var(--text)">{total}</strong> &middot; Showing: {min(total, 200)}
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
      <div class="filter-label"><span>Min Discount</span><span id="discountVal">0%</span></div>
      <input type="range" min="0" max="50" value="0" id="filterDiscount" oninput="applyFilters()">
    </div>
    <div class="filter-group">
      <div class="filter-label"><span>Min Volume</span><span id="volumeVal">0</span></div>
      <input type="range" min="0" max="1000" value="0" step="10" id="filterVolume" oninput="applyFilters()">
    </div>
    <div class="filter-group">
      <div class="filter-label"><span>Max Price</span><span id="priceVal">$1000</span></div>
      <input type="range" min="1" max="1000" value="1000" id="filterPrice" oninput="applyFilters()">
    </div>
  </div>

  <!-- Items Grid -->
  <div class="items-grid" id="itemsGrid" style="display:grid;grid-template-columns:repeat(auto-fill, minmax(220px, 1fr));gap:14px;align-content:start">
    {cards_html}
  </div>
</div>
<div style="text-align:center;margin-top:20px" id="loadMoreWrap">
  <button class="btn btn-ghost" id="loadMoreBtn" onclick="showMore()" {'style="display:none"' if len(items_data) <= PAGE_SIZE else ''}>
    Show More ({len(items_data) - PAGE_SIZE} remaining)
  </button>
</div>"""

        extra_js = """<script>
let visibleCount = 40;
function showMore() {
  const cards = document.querySelectorAll('.item-card');
  const newLimit = visibleCount + 40;
  cards.forEach((card, i) => {
    if (i < newLimit) card.style.display = '';
  });
  visibleCount = newLimit;
  if (visibleCount >= cards.length) {
    document.getElementById('loadMoreBtn').style.display = 'none';
  } else {
    document.getElementById('loadMoreBtn').textContent = 'Show More (' + (cards.length - visibleCount) + ' remaining)';
  }
}

function toggleFilter() {
  document.getElementById('filterPanel').classList.toggle('open');
}

function applyFilters() {
  const minD = parseFloat(document.getElementById('filterDiscount').value);
  const minV = parseFloat(document.getElementById('filterVolume').value);
  const maxP = parseFloat(document.getElementById('filterPrice').value);

  document.getElementById('discountVal').textContent = minD + '%';
  document.getElementById('volumeVal').textContent = minV;
  document.getElementById('priceVal').textContent = '$' + maxP;

  document.querySelectorAll('.item-card').forEach(card => {
    const d = parseFloat(card.dataset.discount) || 0;
    const v = parseFloat(card.dataset.volume) || 0;
    const p = parseFloat(card.dataset.price) || 0;
    const show = d >= minD && v >= minV && p <= maxP;
    card.style.display = show ? '' : 'none';
  });
}
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
                    candidate_rows: list[tuple[float, str]] = []
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
                            fee = config.fees.steam_fee_percent / 100
                            net = sp * (1 - fee)
                            d_val = (net - p) / net * 100 if net > 0 else 0
                            profit_val = net - p
                            discount = f"{d_val:.1f}%"
                            profit = f"${profit_val:.4f}"
                        profit_color = "var(--success)" if profit_val > 0 else "var(--danger)"
                        row = (
                            f'<tr data-discount="{d_val:.4f}" '
                            f'data-profit="{profit_val:.6f}" '
                            f'data-volume="{vol}">'
                            f"<td>{name}</td>"
                            f"<td>${p:.4f}</td>"
                            f"<td>${sp:.4f}</td>"
                            f'<td><span style="color:var(--success);font-weight:600">{discount}</span></td>'
                            f'<td><span style="color:{profit_color}">{profit}</span></td>'
                            f"<td>{vol or '—'}</td>"
                            f"</tr>"
                        )
                        candidate_rows.append((d_val, row))
                    candidate_rows.sort(key=lambda x: x[0], reverse=True)
                    rows_html = "".join(r for _, r in candidate_rows)
        except Exception as e:
            rows_html = f"<tr><td colspan='6'>Error: {e}</td></tr>"

        if not rows_html:
            rows_html = '<tr><td colspan="6" style="text-align:center;color:var(--text-muted);padding:32px">No candidates yet. Run Scanner + Updater.</td></tr>'

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
</tr></thead>
<tbody>{rows_html}</tbody>
</table>
</div></div>"""

        extra_js = """<script>
async function clearCandidates() {
  if (!confirm('Clear all candidates? New ones will appear as prices update.')) return;
  const resp = await fetch('/api/v1/candidates/clear', {method: 'POST'});
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

    @app.post("/api/v1/purchases/clear")
    async def api_clear_purchases() -> JSONResponse:
        """Удалить все покупки из PostgreSQL."""
        try:
            from sqlalchemy import delete

            from fluxio.db.models import Purchase
            from fluxio.db.session import async_session_factory
            from fluxio.db.unit_of_work import UnitOfWork
            async with UnitOfWork(async_session_factory) as uow:
                result = await uow._session.execute(delete(Purchase))
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
                fee = config.fees.steam_fee_percent / 100

                items_cache: dict[str, object] = {}
                for p in all_purchases:
                    if p.market_hash_name not in items_cache:
                        items_cache[p.market_hash_name] = await uow.items.get_by_name(
                            p.market_hash_name,
                        )

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
                            p_profit = steam * (1 - fee) - price
                            total_profit += p_profit
                        item_obj = items_cache.get(p.market_hash_name)
                        median = float(item_obj.steam_median_30d) if item_obj and item_obj.steam_median_30d else 0
                        if median > 0 and price > 0:
                            total_median_profit += median * (1 - fee) - price
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

                purchases = all_purchases[:200]
                rows = []
                for p in purchases:
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
                        net_steam = steam * (1 - fee)
                        profit = net_steam - price
                        color_p = "var(--success)" if profit > 0 else "var(--danger)"
                        profit_str = f'<span style="color:{color_p}">${profit:.4f}</span>'

                    median_profit = 0.0
                    median_profit_str = "—"
                    if median > 0 and price > 0:
                        net_median = median * (1 - fee)
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
                            f'white-space:nowrap" title="{p.notes}">{p.notes}</div>'
                        )

                    steam_url = f"https://steamcommunity.com/market/listings/570/{quote(p.market_hash_name)}"

                    rows.append(
                        f'<tr data-profit="{profit:.6f}" data-volume="{vol}" '
                        f'data-median-profit="{median_profit:.6f}" data-ratio="{ratio:.4f}"'
                        f'{row_style}>'
                        f'<td><a href="{steam_url}" target="_blank" '
                        f'style="color:var(--accent)">{p.market_hash_name}</a>'
                        f'{filter_note}</td>'
                        f"<td>${price:.4f}</td>"
                        f"<td>${steam:.4f}</td>"
                        f"<td>{median_str}</td>"
                        f'<td style="color:{ratio_color}">{ratio_str}</td>'
                        f"<td>{disc}</td>"
                        f"<td>{profit_str}</td>"
                        f"<td>{median_profit_str}</td>"
                        f"<td>{vol or '—'}</td>"
                        f'<td><span class="badge {status_badge}">{p.status}</span>{dry_badge}</td>'
                        f"<td>{ts}</td>"
                        f"</tr>"
                    )
                rows_html = "".join(rows)
        except Exception as e:
            logger.exception("Dashboard: ошибка на странице покупок")
            rows_html = f"<tr><td colspan='11'>Error: {e}</td></tr>"

        if not rows_html:
            rows_html = '<tr><td colspan="11" style="text-align:center;color:var(--text-muted);padding:32px">No purchases yet.</td></tr>'

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

<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
  <span style="color:var(--text-muted)">Showing: {min(total, 200)} of {total}</span>
  <button class="btn btn-danger" onclick="clearPurchases()">
    <i data-lucide="trash-2" style="width:14px;height:14px"></i> Clear All
  </button>
</div>

<div class="table-wrap" style="max-height:70vh;overflow:auto"><div class="table-scroll">
<table id="purchases-table">
<thead style="position:sticky;top:0;z-index:1"><tr>
  <th>Item</th><th>Price</th><th>Steam</th>
  <th>Median 30d</th>
  <th class="sortable" data-key="ratio">Ratio</th>
  <th>Discount</th>
  <th class="sortable" data-key="profit">Profit</th>
  <th class="sortable" data-key="median-profit">By Median</th>
  <th class="sortable" data-key="volume">Vol 24h</th>
  <th>Status</th><th>Date</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>
</div></div>"""

        extra_js = """<script>
async function clearPurchases() {
  if (!confirm('Delete ALL purchases from database? This cannot be undone.')) return;
  const resp = await fetch('/api/v1/purchases/clear', {method: 'POST'});
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
</script>"""

        return _layout("Purchases", "/purchases", content,
                       extra_head='<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>') + extra_js

    return app


# Обратная совместимость — app для uvicorn без контейнера
app = create_app()
