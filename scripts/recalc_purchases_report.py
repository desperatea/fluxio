"""Пересчёт покупок по новым анти-манипуляционным фильтрам.

Генерирует HTML-отчёт с разбивкой:
 - какие предметы отсеялись
 - какие остались
 - как изменился дисконт и ожидаемая прибыль

Запуск:
    python scripts/recalc_purchases_report.py
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# Корень проекта
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from sqlalchemy import func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fluxio.db.models import Base, Item, Purchase, SteamSalesHistory

import os

PG_USER = os.getenv("POSTGRES_USER", "bot")
PG_PASS = os.getenv("POSTGRES_PASSWORD", "secret")
PG_HOST = os.getenv("POSTGRES_HOST", "postgres")
PG_PORT = os.getenv("POSTGRES_PORT", "5432")
PG_DB = os.getenv("POSTGRES_DB", "c5game_bot")

DATABASE_URL = f"postgresql+asyncpg://{PG_USER}:{PG_PASS}@{PG_HOST}:{PG_PORT}/{PG_DB}"
engine = create_async_engine(DATABASE_URL, echo=False, pool_size=5, max_overflow=10)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ── Конфиг фильтров (из config.yaml) ──
STEAM_FEE = 0.13
MIN_DISCOUNT = 45.0
MAX_PRICE_TO_MEDIAN_RATIO = 2.0
REFERENCE_BLEND_THRESHOLD = 1.5
MAX_SPIKE_RATIO = 2.0
MAX_PRICE_CV = 0.5
MIN_SALES_AT_PRICE = 5
MIN_VOLUME_7D = 5
APP_ID = 570  # Dota 2


@dataclass
class RecalcResult:
    """Результат пересчёта одной покупки."""

    # Данные покупки
    market_hash_name: str = ""
    purchase_price: float = 0.0
    old_steam_price: float = 0.0
    old_discount: float = 0.0
    old_profit: float = 0.0
    purchased_at: str = ""
    status: str = ""
    dry_run: bool = False
    product_id: str = ""

    # Данные из items (обогащение)
    median_30d: float | None = None
    volume_7d: int | None = None
    price_cv: float | None = None
    spike_ratio: float | None = None
    sales_at_price_enriched: int | None = None

    # Новые расчёты
    price_to_median_ratio: float | None = None
    reference_price: float = 0.0
    new_discount: float = 0.0
    new_profit: float = 0.0
    sales_at_current_price_fresh: int | None = None
    median_profit: float | None = None  # прибыль по медиане

    # Фильтры
    rejected: bool = False
    reject_reasons: list[str] = field(default_factory=list)
    filter_details: dict = field(default_factory=dict)


async def recalc_sales_at_price(
    session: AsyncSession, hash_name: str, target_price: float,
) -> int:
    """Пересчитать sales@price из steam_sales_history."""
    low = target_price * 0.8
    high = target_price * 1.2
    stmt = (
        select(sa_func.coalesce(sa_func.sum(SteamSalesHistory.volume), 0))
        .where(SteamSalesHistory.hash_name == hash_name)
        .where(SteamSalesHistory.price_usd >= low)
        .where(SteamSalesHistory.price_usd <= high)
    )
    result = await session.execute(stmt)
    return result.scalar() or 0


async def run_recalc() -> list[RecalcResult]:
    """Пересчитать все покупки по новым фильтрам."""
    results: list[RecalcResult] = []

    async with async_session() as session:
        # Получить все покупки
        stmt = select(Purchase).order_by(Purchase.purchased_at.desc())
        rows = await session.execute(stmt)
        purchases = rows.scalars().all()

        # Кеш items
        items_cache: dict[str, Item | None] = {}

        for p in purchases:
            r = RecalcResult()
            r.market_hash_name = p.market_hash_name
            r.purchase_price = float(p.price_usd or 0)
            r.old_steam_price = float(p.steam_price_usd or 0)
            r.old_discount = float(p.discount_percent or 0)
            r.old_profit = r.old_steam_price * (1 - STEAM_FEE) - r.purchase_price
            r.purchased_at = (
                p.purchased_at.strftime("%Y-%m-%d %H:%M") if p.purchased_at else "—"
            )
            r.status = p.status or "unknown"
            r.dry_run = bool(p.dry_run)
            r.product_id = p.product_id or ""

            # Получить item из БД
            if p.market_hash_name not in items_cache:
                item_stmt = select(Item).where(
                    Item.market_hash_name == p.market_hash_name,
                )
                item_row = await session.execute(item_stmt)
                items_cache[p.market_hash_name] = item_row.scalar_one_or_none()

            item = items_cache.get(p.market_hash_name)

            if item is None:
                r.rejected = True
                r.reject_reasons.append("предмет не найден в БД items")
                r.reference_price = r.old_steam_price
                r.new_discount = r.old_discount
                r.new_profit = r.old_profit
                results.append(r)
                continue

            r.median_30d = float(item.steam_median_30d) if item.steam_median_30d else None
            r.volume_7d = item.steam_volume_7d
            r.price_cv = item.price_stability_cv
            r.spike_ratio = item.price_spike_ratio
            r.sales_at_price_enriched = item.sales_at_current_price

            steam_current = r.old_steam_price

            # ── Фильтр 1: Ratio check ──
            if r.median_30d and r.median_30d > 0 and steam_current > 0:
                r.price_to_median_ratio = steam_current / r.median_30d
                if r.price_to_median_ratio > MAX_PRICE_TO_MEDIAN_RATIO:
                    r.rejected = True
                    r.reject_reasons.append(
                        f"ratio={r.price_to_median_ratio:.1f}x > {MAX_PRICE_TO_MEDIAN_RATIO}x",
                    )
                    r.filter_details["ratio_check"] = True
            elif r.median_30d is None:
                # Нет медианы — не можем проверить ratio
                r.filter_details["no_median"] = True

            # ── Фильтр 5: Скорректированная эталонная цена ──
            if (
                r.median_30d
                and r.median_30d > 0
                and steam_current > 0
                and steam_current / r.median_30d > REFERENCE_BLEND_THRESHOLD
            ):
                r.reference_price = steam_current * 0.7 + r.median_30d * 0.3
            else:
                r.reference_price = steam_current

            # Новый дисконт
            net_steam = r.reference_price * (1 - STEAM_FEE)
            r.new_discount = (
                (net_steam - r.purchase_price) / net_steam * 100
                if net_steam > 0
                else 0
            )
            r.new_profit = net_steam - r.purchase_price

            # ── Фильтр 2: Пересчёт sales@price ──
            if (
                r.median_30d
                and r.median_30d > 0
                and steam_current > 0
                and steam_current / r.median_30d > REFERENCE_BLEND_THRESHOLD
            ):
                r.sales_at_current_price_fresh = await recalc_sales_at_price(
                    session, p.market_hash_name, steam_current,
                )
                if r.sales_at_current_price_fresh < MIN_SALES_AT_PRICE:
                    if not r.rejected:
                        r.rejected = True
                    r.reject_reasons.append(
                        f"sales@price(fresh)={r.sales_at_current_price_fresh} "
                        f"< {MIN_SALES_AT_PRICE}",
                    )
                    r.filter_details["sales_at_price_fresh"] = True

            # ── Фильтр 3: spike_ratio ──
            if r.spike_ratio is not None and r.spike_ratio > MAX_SPIKE_RATIO:
                if not r.rejected:
                    r.rejected = True
                r.reject_reasons.append(
                    f"spike={r.spike_ratio:.2f} > {MAX_SPIKE_RATIO}",
                )
                r.filter_details["spike"] = True

            # ── Фильтр 3: CV ──
            if r.price_cv is not None and r.price_cv > MAX_PRICE_CV:
                if not r.rejected:
                    r.rejected = True
                r.reject_reasons.append(
                    f"CV={r.price_cv:.3f} > {MAX_PRICE_CV}",
                )
                r.filter_details["cv"] = True

            # ── Фильтр 2 (enriched): sales_at_current_price ──
            if r.sales_at_current_price_fresh is None:
                # Не пересчитывали — используем enriched
                if (
                    r.sales_at_price_enriched is not None
                    and r.sales_at_price_enriched < MIN_SALES_AT_PRICE
                ):
                    if not r.rejected:
                        r.rejected = True
                    r.reject_reasons.append(
                        f"sales@price(enriched)={r.sales_at_price_enriched} "
                        f"< {MIN_SALES_AT_PRICE}",
                    )
                    r.filter_details["sales_enriched"] = True

            # ── Фильтр: дисконт ──
            if r.new_discount < MIN_DISCOUNT and not r.rejected:
                r.rejected = True
                r.reject_reasons.append(
                    f"new_discount={r.new_discount:.1f}% < {MIN_DISCOUNT}%",
                )
                r.filter_details["discount"] = True

            # ── Фильтр 4: Safety net — убыток по медиане ──
            if r.median_30d and r.median_30d > 0:
                net_median = r.median_30d * (1 - STEAM_FEE)
                r.median_profit = net_median - r.purchase_price
                if net_median < r.purchase_price:
                    if not r.rejected:
                        r.rejected = True
                    r.reject_reasons.append(
                        f"убыток по медиане: ${r.median_profit:.4f}",
                    )
                    r.filter_details["safety_net"] = True

            # ── Фильтр: ликвидность ──
            if r.volume_7d is not None and r.volume_7d < MIN_VOLUME_7D:
                if not r.rejected:
                    r.rejected = True
                r.reject_reasons.append(
                    f"volume_7d={r.volume_7d} < {MIN_VOLUME_7D}",
                )
                r.filter_details["volume"] = True

            results.append(r)

    return results


def generate_html(results: list[RecalcResult]) -> str:
    """Сгенерировать HTML-отчёт."""
    rejected = [r for r in results if r.rejected]
    passed = [r for r in results if not r.rejected]

    total_old_profit = sum(r.old_profit for r in results)
    total_new_profit_passed = sum(r.new_profit for r in passed)
    total_new_profit_all = sum(r.new_profit for r in results)
    total_spent = sum(r.purchase_price for r in results)
    total_spent_rejected = sum(r.purchase_price for r in rejected)

    # Посчитаем прибыль по медиане для passed
    total_median_profit_passed = sum(
        r.median_profit for r in passed if r.median_profit is not None
    )

    # Подсчёт причин отклонения
    reason_counts: dict[str, int] = {}
    for r in rejected:
        for key in r.filter_details:
            reason_counts[key] = reason_counts.get(key, 0) + 1

    reason_labels = {
        "ratio_check": "Ratio цена/медиана",
        "sales_at_price_fresh": "Sales@price (пересчёт)",
        "sales_enriched": "Sales@price (enriched)",
        "spike": "Spike ratio",
        "cv": "Price CV",
        "safety_net": "Safety net (убыток по медиане)",
        "discount": "Дисконт ниже порога",
        "volume": "Низкая ликвидность",
        "no_median": "Нет медианы",
    }

    def _row(r: RecalcResult, show_reasons: bool = False) -> str:
        steam_url = (
            f"https://steamcommunity.com/market/listings/{APP_ID}/"
            f"{quote(r.market_hash_name)}"
        )

        old_profit_color = "#4caf50" if r.old_profit > 0 else "#f44336"
        new_profit_color = "#4caf50" if r.new_profit > 0 else "#f44336"
        median_str = f"${r.median_30d:.4f}" if r.median_30d else "—"
        ratio_str = f"{r.price_to_median_ratio:.1f}x" if r.price_to_median_ratio else "—"

        ratio_color = "#c7d5e0"
        if r.price_to_median_ratio:
            if r.price_to_median_ratio > MAX_PRICE_TO_MEDIAN_RATIO:
                ratio_color = "#f44336"
            elif r.price_to_median_ratio > REFERENCE_BLEND_THRESHOLD:
                ratio_color = "#ff9800"

        median_profit_str = "—"
        if r.median_profit is not None:
            mp_color = "#4caf50" if r.median_profit > 0 else "#f44336"
            median_profit_str = (
                f'<span style="color:{mp_color}">${r.median_profit:.4f}</span>'
            )

        reasons_html = ""
        if show_reasons and r.reject_reasons:
            reasons_html = (
                '<div style="font-size:11px;color:#ff6b6b;margin-top:2px">'
                + "; ".join(r.reject_reasons)
                + "</div>"
            )

        dry_badge = (
            ' <span style="background:#3d2e00;color:#ff9800;padding:1px 6px;'
            'border-radius:3px;font-size:11px">DRY</span>'
            if r.dry_run
            else ""
        )

        ref_str = (
            f"${r.reference_price:.4f}"
            if abs(r.reference_price - r.old_steam_price) > 0.001
            else ""
        )
        ref_cell = f'<div style="font-size:11px;color:#ff9800">→ ref: {ref_str}</div>' if ref_str else ""

        return (
            f"<tr>"
            f'<td><a href="{steam_url}" target="_blank" '
            f'style="color:#66c0f4;text-decoration:none">'
            f"{r.market_hash_name}</a>{dry_badge}{reasons_html}</td>"
            f"<td>${r.purchase_price:.4f}</td>"
            f"<td>${r.old_steam_price:.4f}{ref_cell}</td>"
            f"<td>{median_str}</td>"
            f'<td style="color:{ratio_color}">{ratio_str}</td>'
            f"<td>{r.old_discount:.1f}%</td>"
            f"<td>{r.new_discount:.1f}%</td>"
            f'<td><span style="color:{old_profit_color}">${r.old_profit:.4f}</span></td>'
            f'<td><span style="color:{new_profit_color}">${r.new_profit:.4f}</span></td>'
            f"<td>{median_profit_str}</td>"
            f"<td>{r.purchased_at}</td>"
            f"</tr>"
        )

    # Статистика по фильтрам
    filter_stats_html = ""
    for key, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        label = reason_labels.get(key, key)
        filter_stats_html += (
            f'<div class="stat-card">'
            f'<div class="lbl">{label}</div>'
            f'<div class="val" style="color:#f44336">{count}</div>'
            f"</div>"
        )

    rejected_rows = "".join(_row(r, show_reasons=True) for r in rejected)
    passed_rows = "".join(_row(r) for r in passed)

    if not rejected_rows:
        rejected_rows = (
            "<tr><td colspan='11' style='text-align:center;color:#8f98a0'>"
            "Все покупки прошли фильтры</td></tr>"
        )
    if not passed_rows:
        passed_rows = (
            "<tr><td colspan='11' style='text-align:center;color:#8f98a0'>"
            "Ни одна покупка не прошла фильтры</td></tr>"
        )

    old_profit_color = "#4caf50" if total_old_profit >= 0 else "#f44336"
    new_profit_color = "#4caf50" if total_new_profit_passed >= 0 else "#f44336"
    median_total_color = "#4caf50" if total_median_profit_passed >= 0 else "#f44336"
    savings_color = "#4caf50" if total_spent_rejected > 0 else "#8f98a0"

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Fluxio — Пересчёт покупок по новым фильтрам</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: #0f1923; color: #c7d5e0;
         margin: 0; padding: 20px; }}
  h1 {{ color: #66c0f4; margin-bottom: 4px; }}
  h2 {{ color: #66c0f4; margin-top: 32px; }}
  .subtitle {{ color: #8f98a0; font-size: 14px; margin-bottom: 20px; }}
  .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
                 gap: 12px; margin-bottom: 20px; }}
  .stat-card {{ background: #1b2838; border-radius: 8px; padding: 14px 18px;
                border: 1px solid #2a475e; }}
  .stat-card .val {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
  .stat-card .lbl {{ color: #8f98a0; font-size: 13px; }}
  table {{ width: 100%; border-collapse: collapse; background: #1b2838;
           border-radius: 8px; overflow: hidden; margin-bottom: 24px; }}
  th {{ background: #2a475e; color: #66c0f4; padding: 10px 12px; text-align: left;
        font-size: 13px; white-space: nowrap; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #2a475e; color: #c7d5e0;
        font-size: 13px; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #243547; }}
  .section-rejected {{ border-left: 3px solid #f44336; padding-left: 12px; }}
  .section-passed {{ border-left: 3px solid #4caf50; padding-left: 12px; }}
  .filter-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
                   gap: 10px; margin-bottom: 20px; }}
  a {{ color: #66c0f4; }}
  th.sortable {{ cursor: pointer; user-select: none; }}
  th.sortable:hover {{ color: #fff; }}
  th.sortable::after {{ content: ' ⇅'; opacity: 0.4; }}
  th.sort-desc::after {{ content: ' ↓'; opacity: 1; }}
  th.sort-asc::after {{ content: ' ↑'; opacity: 1; }}
</style>
</head>
<body>
<h1>Пересчёт покупок по новым фильтрам</h1>
<div class="subtitle">Сгенерировано: {now} | Фильтры: ratio≤{MAX_PRICE_TO_MEDIAN_RATIO}x,
  min_discount≥{MIN_DISCOUNT}%, CV≤{MAX_PRICE_CV}, spike≤{MAX_SPIKE_RATIO},
  sales@price≥{MIN_SALES_AT_PRICE}, volume_7d≥{MIN_VOLUME_7D}</div>

<div class="stats-grid">
  <div class="stat-card">
    <div class="lbl">Всего покупок</div>
    <div class="val" style="color:#66c0f4">{len(results)}</div>
  </div>
  <div class="stat-card">
    <div class="lbl">Отсеяно новыми фильтрами</div>
    <div class="val" style="color:#f44336">{len(rejected)}</div>
    <div class="lbl">{len(rejected)/len(results)*100:.0f}% от всех</div>
  </div>
  <div class="stat-card">
    <div class="lbl">Осталось</div>
    <div class="val" style="color:#4caf50">{len(passed)}</div>
  </div>
  <div class="stat-card">
    <div class="lbl">Потрачено всего</div>
    <div class="val" style="color:#ff9800">${total_spent:.2f}</div>
  </div>
  <div class="stat-card">
    <div class="lbl">Сэкономлено (отсеяно)</div>
    <div class="val" style="color:{savings_color}">${total_spent_rejected:.2f}</div>
  </div>
  <div class="stat-card">
    <div class="lbl">Старая прибыль (все)</div>
    <div class="val" style="color:{old_profit_color}">${total_old_profit:.2f}</div>
  </div>
  <div class="stat-card">
    <div class="lbl">Новая прибыль (прошедшие)</div>
    <div class="val" style="color:{new_profit_color}">${total_new_profit_passed:.2f}</div>
  </div>
  <div class="stat-card">
    <div class="lbl">Прибыль по медиане (прошедшие)</div>
    <div class="val" style="color:{median_total_color}">${total_median_profit_passed:.2f}</div>
  </div>
</div>

<h2>Причины отклонения</h2>
<div class="filter-grid">
{filter_stats_html}
</div>

<div class="section-rejected">
<h2 style="color:#f44336">Отсеянные предметы ({len(rejected)})</h2>
<table>
<thead><tr>
  <th>Предмет</th><th>Цена</th><th>Steam</th><th>Медиана 30д</th>
  <th>Ratio</th><th>Старый дисконт</th><th>Новый дисконт</th>
  <th>Старая прибыль</th><th>Новая прибыль</th><th>Прибыль по медиане</th><th>Дата</th>
</tr></thead>
<tbody>{rejected_rows}</tbody>
</table>
</div>

<div class="section-passed">
<h2 style="color:#4caf50">Прошедшие предметы ({len(passed)})</h2>
<table id="passed-table">
<thead><tr>
  <th>Предмет</th><th>Цена</th><th>Steam</th><th>Медиана 30д</th>
  <th>Ratio</th><th>Старый дисконт</th><th class="sortable" data-col="6">Новый дисконт</th>
  <th>Старая прибыль</th><th class="sortable" data-col="8">Новая прибыль</th>
  <th class="sortable" data-col="9">Прибыль по медиане</th><th>Дата</th>
</tr></thead>
<tbody>{passed_rows}</tbody>
</table>
</div>

<script>
document.querySelectorAll('th.sortable').forEach(th => {{
  th.addEventListener('click', () => {{
    const table = th.closest('table');
    const tbody = table.querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
    const col = parseInt(th.dataset.col);
    const isDesc = th.classList.contains('sort-desc');

    table.querySelectorAll('th.sortable').forEach(h => {{
      h.classList.remove('sort-desc', 'sort-asc');
    }});
    th.classList.add(isDesc ? 'sort-asc' : 'sort-desc');
    const dir = isDesc ? 1 : -1;

    rows.sort((a, b) => {{
      const aText = a.cells[col]?.textContent?.replace(/[^\\d.\\-]/g, '') || '0';
      const bText = b.cells[col]?.textContent?.replace(/[^\\d.\\-]/g, '') || '0';
      return (parseFloat(bText) - parseFloat(aText)) * dir;
    }});
    rows.forEach(row => tbody.appendChild(row));
  }});
}});
</script>
</body>
</html>"""


async def update_db(results: list[RecalcResult]) -> int:
    """Обновить статус отсеянных покупок в БД.

    Отсеянным ставим status='filtered', notes с причиной.
    Прошедшим — обновляем notes с новыми метриками.
    Возвращает количество обновлённых записей.
    """
    updated = 0
    async with async_session() as session:
        for r in results:
            if not r.product_id:
                continue

            stmt = select(Purchase).where(Purchase.product_id == r.product_id)
            row = await session.execute(stmt)
            p = row.scalar_one_or_none()
            if p is None:
                continue

            if r.rejected:
                p.status = "filtered"
                p.notes = "; ".join(r.reject_reasons)
                updated += 1
            else:
                # Для прошедших — сохранить актуальные метрики в notes
                parts = []
                if r.median_30d:
                    parts.append(f"median_30d=${r.median_30d:.4f}")
                if r.price_to_median_ratio:
                    parts.append(f"ratio={r.price_to_median_ratio:.1f}x")
                if r.median_profit is not None:
                    parts.append(f"median_profit=${r.median_profit:.4f}")
                if parts:
                    p.notes = "; ".join(parts)

        await session.commit()
    return updated


async def main() -> None:
    """Основная функция."""
    print("Пересчитываю покупки по новым фильтрам...")
    results = await run_recalc()

    rejected = [r for r in results if r.rejected]
    passed = [r for r in results if not r.rejected]
    print(f"Всего: {len(results)}, отсеяно: {len(rejected)}, осталось: {len(passed)}")

    # Обновить БД
    updated = await update_db(results)
    print(f"Обновлено в БД: {updated} записей (status='filtered')")

    html = generate_html(results)

    # В Docker /app, локально — рядом с PROJECT_ROOT
    app_dir = Path("/app") if Path("/app/fluxio").exists() else PROJECT_ROOT
    output_dir = app_dir / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "recalc_purchases.html"
    output_path.write_text(html, encoding="utf-8")
    print(f"Отчёт сохранён: {output_path}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
