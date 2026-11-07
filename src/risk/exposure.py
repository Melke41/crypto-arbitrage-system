"""Exposure and cumulative loss tracking for circuit-breaker protection."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Optional


class ExposureTracker:
    """Tracks real-time open trade exposure, daily realized PnL, and drawdown."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.daily_realized_pnl: Decimal = Decimal("0")
        self.high_water_mark: Decimal = Decimal("0")
        self._open_plans: dict[str, Decimal] = {}  # plan_id -> notional

    @property
    def open_plans_count(self) -> int:
        return len(self._open_plans)

    @property
    def total_open_notional(self) -> Decimal:
        return sum(self._open_plans.values(), Decimal("0"))

    async def record_realized_pnl(self, pnl: Decimal) -> None:
        """Record realized profit or loss from completed or unwound trade."""
        async with self._lock:
            self.daily_realized_pnl += pnl
            if self.daily_realized_pnl > self.high_water_mark:
                self.high_water_mark = self.daily_realized_pnl

    async def add_open_plan(self, plan_id: str, notional: Decimal) -> None:
        """Register active in-flight execution plan."""
        async with self._lock:
            self._open_plans[plan_id] = notional

    async def remove_open_plan(self, plan_id: str) -> None:
        """Deregister completed or aborted execution plan."""
        async with self._lock:
            self._open_plans.pop(plan_id, None)

    def is_daily_loss_breached(self, max_daily_loss_usdt: Decimal) -> bool:
        """Return True if cumulative daily losses equal or exceed the maximum limit."""
        if self.daily_realized_pnl < Decimal("0"):
            return abs(self.daily_realized_pnl) >= max_daily_loss_usdt
        return False

    def is_drawdown_breached(self, max_drawdown_pct: Decimal) -> bool:
        """Check if peak-to-trough drawdown exceeds percentage threshold."""
        if self.high_water_mark > Decimal("0"):
            drawdown = ((self.high_water_mark - self.daily_realized_pnl) / self.high_water_mark) * Decimal("100.0")
            return drawdown >= max_drawdown_pct
        return False

    def reset_daily_metrics(self) -> None:
        """Reset daily accumulator at midnight UTC."""
        self.daily_realized_pnl = Decimal("0")
        self.high_water_mark = Decimal("0")
