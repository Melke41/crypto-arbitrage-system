"""Strictly guarded live execution engine.

Enforces fail-safe locks preventing accidental live order submission without
explicit dual-key configuration authorization and runtime token verification.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

from src.core.models import (
    ExecutionPlan,
)
from src.core.interfaces import OrderBookEngine
from src.execution.base_executor import BaseExecutor, LiveTradingDisabledError
from src.market_data.market_registry import MarketRegistry
from src.portfolio.balances import BalanceManager
from src.risk.risk_manager import CentralRiskManager


class LiveExecutor(BaseExecutor):
    """Institutional live execution router with strict hardware/environment safeguards."""

    def __init__(
        self,
        balance_manager: BalanceManager,
        risk_manager: CentralRiskManager,
        order_book_engine: Optional[OrderBookEngine] = None,
        market_registry: Optional[MarketRegistry] = None,
        runtime_auth_token: Optional[str] = None,
    ) -> None:
        super().__init__(
            balance_manager=balance_manager,
            risk_manager=risk_manager,
            order_book_engine=order_book_engine,
            market_registry=market_registry,
        )
        self.runtime_auth_token = runtime_auth_token

    def _verify_live_authorization(self) -> None:
        """Verify that live execution is explicitly and intentionally authorized."""
        exec_mode = os.getenv("EXECUTION_MODE", "PAPER").upper()
        live_enabled = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
        expected_token = os.getenv("LIVE_AUTH_TOKEN", "")

        if exec_mode != "LIVE":
            raise LiveTradingDisabledError(
                f"Live execution rejected: EXECUTION_MODE is '{exec_mode}', must be 'LIVE'"
            )
        if not live_enabled:
            raise LiveTradingDisabledError(
                "Live execution rejected: LIVE_TRADING_ENABLED flag is False"
            )
        if not expected_token or self.runtime_auth_token != expected_token:
            raise LiveTradingDisabledError(
                "Live execution rejected: Invalid or missing runtime authorization token"
            )

    async def execute_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        """Execute live order plan after strict authorization verification."""
        self._verify_live_authorization()
        # In this sandbox/paper environment, live endpoints are not engaged
        raise LiveTradingDisabledError("Live trading endpoints not configured in sandbox.")
