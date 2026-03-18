"""Анализ выгодности предметов.

Критерии покупки (SPEC.md раздел 5.1):
1. Предмет имеет эталонную цену Steam
2. Дисконт >= min_discount_percent
3. Ликвидность >= min_sales_volume_7d
4. Цена в допустимом диапазоне
5. Не в чёрном списке
6. Антиманипуляция: не на искусственном бусте
7. Whitelist (если включён)

Все цены и сравнения — в USD.
CS2DT — основная площадка, цены уже в USD.
"""

from __future__ import annotations

from loguru import logger

from fluxio.config import config
from fluxio.core.monitor import MarketItem


class ProfitAnalyzer:
    """Анализ дисконта, ликвидности, антиманипуляции."""

    def __init__(self) -> None:
        self._blacklist: set[str] = set(config.blacklist.items)
        self._whitelist_enabled = config.whitelist.enabled
        self._whitelist_names: dict[str, float] = {}
        if self._whitelist_enabled:
            for entry in config.whitelist.items:
                name = entry.get("name", "")
                max_price = entry.get("max_price_usd", config.trading.max_price_usd)
                if name:
                    self._whitelist_names[name] = max_price

    def reload_lists(self) -> None:
        """Перечитать blacklist/whitelist из конфига."""
        self._blacklist = set(config.blacklist.items)
        self._whitelist_enabled = config.whitelist.enabled
        self._whitelist_names.clear()
        if self._whitelist_enabled:
            for entry in config.whitelist.items:
                name = entry.get("name", "")
                max_price = entry.get("max_price_usd", config.trading.max_price_usd)
                if name:
                    self._whitelist_names[name] = max_price

    def analyze(self, item: MarketItem) -> AnalysisResult:
        """Проверить предмет на выгодность.

        Returns:
            AnalysisResult с решением и причиной.
        """
        name = item.market_hash_name
        item_price_usd = item.price_usd

        # 1. Есть ли эталонная цена Steam?
        if item.steam_price is None:
            return AnalysisResult(False, "нет цены Steam", item)

        steam_price_usd = item.steam_price.median_price_usd
        if not steam_price_usd or steam_price_usd <= 0:
            return AnalysisResult(False, "цена Steam <= 0", item)

        # 2. ROI (всё в USD, с учётом поцентовой комиссии Steam)
        from fluxio.config import FeesConfig
        net_steam = FeesConfig.calc_net_steam(
            steam_price_usd,
            config.fees.steam_valve_fee_percent,
            config.fees.steam_game_fee_percent,
        )
        discount = (net_steam - item_price_usd) / item_price_usd * 100 if item_price_usd > 0 else 0
        min_discount = config.trading.min_discount_percent

        if discount < min_discount:
            return AnalysisResult(
                False,
                f"дисконт {discount:.1f}% < {min_discount}%",
                item,
                discount_percent=discount,
                steam_price_usd=steam_price_usd,
                item_price_usd=item_price_usd,
            )

        # 3. Ликвидность
        sales = item.steam_price.sales_count
        min_sales = config.trading.min_sales_volume_7d

        # pricehistory возвращает продажи за 30 дней, делим на ~4 для 7-дневной оценки
        if item.steam_price.source == "pricehistory":
            estimated_7d_sales = sales / 4
        else:
            # priceoverview: volume за 24ч, умножаем на 7
            estimated_7d_sales = sales * 7

        if estimated_7d_sales < min_sales:
            return AnalysisResult(
                False,
                f"ликвидность {estimated_7d_sales:.0f} < {min_sales} (7д)",
                item,
                discount_percent=discount,
                steam_price_usd=steam_price_usd,
                item_price_usd=item_price_usd,
            )

        # 4. Диапазон цены (USD)
        if not (config.trading.min_price_usd <= item_price_usd <= config.trading.max_price_usd):
            return AnalysisResult(
                False,
                f"цена ${item_price_usd:.2f} вне диапазона "
                f"${config.trading.min_price_usd}–${config.trading.max_price_usd}",
                item,
                discount_percent=discount,
                steam_price_usd=steam_price_usd,
                item_price_usd=item_price_usd,
            )

        # 5. Чёрный список
        if name in self._blacklist:
            return AnalysisResult(
                False,
                "в чёрном списке",
                item,
                discount_percent=discount,
                steam_price_usd=steam_price_usd,
                item_price_usd=item_price_usd,
            )

        # 6. Whitelist (если включён)
        if self._whitelist_enabled:
            if name not in self._whitelist_names:
                return AnalysisResult(
                    False,
                    "не в whitelist",
                    item,
                    discount_percent=discount,
                    steam_price_usd=steam_price_usd,
                    item_price_usd=item_price_usd,
                )
            wl_max_price = self._whitelist_names[name]
            if item_price_usd > wl_max_price:
                return AnalysisResult(
                    False,
                    f"цена ${item_price_usd:.2f} > whitelist лимит ${wl_max_price:.2f}",
                    item,
                    discount_percent=discount,
                    steam_price_usd=steam_price_usd,
                    item_price_usd=item_price_usd,
                )

        # 7. Антиманипуляция (упрощённая — без исторических данных)
        # Полная проверка требует price_history в БД (Фаза 3+)

        # Все проверки пройдены
        return AnalysisResult(
            True,
            f"ВЫГОДНО: дисконт {discount:.1f}%, продаж ~{estimated_7d_sales:.0f}/7д",
            item,
            discount_percent=discount,
            steam_price_usd=steam_price_usd,
            item_price_usd=item_price_usd,
        )

    def analyze_batch(self, items: list[MarketItem]) -> list[AnalysisResult]:
        """Проанализировать список предметов.

        Returns:
            Список результатов, отсортированный по дисконту (лучшие первые).
        """
        results = [self.analyze(item) for item in items]

        profitable = [r for r in results if r.is_profitable]
        rejected = [r for r in results if not r.is_profitable]

        if profitable:
            profitable.sort(key=lambda r: r.discount_percent or 0, reverse=True)
            logger.info(
                f"Анализ: {len(profitable)} выгодных из {len(items)} предметов"
            )
            for r in profitable[:5]:
                logger.info(
                    f"  {r.item.market_hash_name}: "
                    f"покупка=${r.item_price_usd:.2f}, "
                    f"steam=${r.steam_price_usd:.2f}, "
                    f"дисконт={r.discount_percent:.1f}%"
                )
        else:
            logger.info(f"Анализ: 0 выгодных из {len(items)} предметов")

        # Логируем причины отказа (DEBUG)
        reason_counts: dict[str, int] = {}
        for r in rejected:
            key = r.reason.split(":")[0].split(" <")[0]
            reason_counts[key] = reason_counts.get(key, 0) + 1
        if reason_counts:
            logger.debug(f"Причины отказа: {reason_counts}")

        return profitable


class AnalysisResult:
    """Результат анализа выгодности предмета."""

    __slots__ = (
        "is_profitable",
        "reason",
        "item",
        "discount_percent",
        "steam_price_usd",
        "item_price_usd",
    )

    def __init__(
        self,
        is_profitable: bool,
        reason: str,
        item: MarketItem,
        discount_percent: float | None = None,
        steam_price_usd: float | None = None,
        item_price_usd: float | None = None,
    ) -> None:
        self.is_profitable = is_profitable
        self.reason = reason
        self.item = item
        self.discount_percent = discount_percent
        self.steam_price_usd = steam_price_usd
        self.item_price_usd = item_price_usd

    def __repr__(self) -> str:
        status = "OK" if self.is_profitable else "SKIP"
        return f"AnalysisResult({status}: {self.reason})"
