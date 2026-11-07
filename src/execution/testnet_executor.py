"""Binance Spot/Futures Testnet executor with hybrid Kraken paper simulation.

Routes Binance legs to Binance Spot/Futures Testnets via signed REST endpoints,
while routing Kraken legs to internal L2 paper-matching engine over live Kraken data.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from decimal import Decimal
from typing import Optional
import aiohttp

from src.core.models import (
    ExchangeId,
    ExecutionLegState,
    ExecutionLegStatus,
    ExecutionOrder,
    ExecutionPlan,
    ExecutionPlanStatus,
    OrderSide,
    OrderType,
    StrategyType,
)
from src.core.interfaces import OrderBookEngine
from src.execution.base_executor import BaseExecutor
from src.execution.paper_executor import PaperExecutor
from src.execution.state_machine import ExecutionStateMachine
from src.market_data.market_registry import MarketRegistry
from src.portfolio.balances import BalanceManager
from src.risk.risk_manager import CentralRiskManager


class TestnetExecutor(BaseExecutor):
    """Hybrid execution engine testing live Binance testnet with local Kraken paper matching."""

    def __init__(
        self,
        balance_manager: BalanceManager,
        risk_manager: CentralRiskManager,
        binance_api_key: str = "",
        binance_api_secret: str = "",
        binance_futures_api_key: Optional[str] = None,
        binance_futures_api_secret: Optional[str] = None,
        order_book_engine: Optional[OrderBookEngine] = None,
        market_registry: Optional[MarketRegistry] = None,
        paper_fallback: Optional[PaperExecutor] = None,
    ) -> None:
        super().__init__(
            balance_manager=balance_manager,
            risk_manager=risk_manager,
            order_book_engine=order_book_engine,
            market_registry=market_registry,
        )
        self.binance_api_key = binance_api_key
        self.binance_api_secret = binance_api_secret
        self.binance_futures_api_key = binance_futures_api_key or binance_api_key
        self.binance_futures_api_secret = binance_futures_api_secret or binance_api_secret
        self.paper_fallback = paper_fallback or PaperExecutor(
            balance_manager=balance_manager,
            risk_manager=risk_manager,
            order_book_engine=order_book_engine,
            market_registry=market_registry,
        )
        self.binance_spot_testnet_url = "https://testnet.binance.vision"
        self.binance_futures_testnet_url = "https://testnet.binancefuture.com"
        self._spot_clock_offset_ms: int = 0
        self._futures_clock_offset_ms: int = 0

    async def sync_clock_offsets(self) -> tuple[int, int]:
        """Fetch server times from Spot and Futures testnets and calibrate clock offsets in ms.

        offset = server_time - (local_time + round_trip / 2)
        """
        async with aiohttp.ClientSession() as session:
            # 1. Spot Testnet Time
            try:
                t0 = int(time.time() * 1000)
                async with session.get(f"{self.binance_spot_testnet_url}/api/v3/time", timeout=3) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        t1 = int(time.time() * 1000)
                        server_time = int(data.get("serverTime", t1))
                        self._spot_clock_offset_ms = server_time - ((t0 + t1) // 2)
            except Exception:
                pass

            # 2. Futures Testnet Time
            try:
                t0 = int(time.time() * 1000)
                async with session.get(f"{self.binance_futures_testnet_url}/fapi/v1/time", timeout=3) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        t1 = int(time.time() * 1000)
                        server_time = int(data.get("serverTime", t1))
                        self._futures_clock_offset_ms = server_time - ((t0 + t1) // 2)
            except Exception:
                pass

        return self._spot_clock_offset_ms, self._futures_clock_offset_ms

    async def execute_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        """Route Binance legs to testnet REST and Kraken legs to paper engine."""
        # Check if keys exist for the target plan legs
        has_keys = bool(self.binance_api_key and self.binance_api_secret)
        if not has_keys:
            return await self.paper_fallback.execute_plan(plan)

        ExecutionStateMachine.transition_plan(plan, ExecutionPlanStatus.EXECUTING)

        tasks = []
        for leg in plan.legs:
            if leg.order.exchange == ExchangeId.BINANCE:
                tasks.append(self._submit_binance_testnet_order(leg))
            else:
                tasks.append(self.paper_fallback._simulate_order_fill(leg))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_filled = all(
            leg.status == ExecutionLegStatus.FILLED for leg in plan.legs
        )

        if all_filled:
            self.paper_fallback._compute_plan_pnl(plan)
            ExecutionStateMachine.transition_plan(plan, ExecutionPlanStatus.COMPLETED)
        else:
            failed_idx = next(
                (i for i, leg in enumerate(plan.legs) if leg.status != ExecutionLegStatus.FILLED),
                0,
            )
            await self.emergency_unwind(plan, failed_leg_index=failed_idx)

        return plan

    async def _submit_binance_testnet_order(self, leg_state: ExecutionLegState) -> ExecutionLegState:
        """Submit signed testnet order to Binance Spot or Futures testnet."""
        order = leg_state.order
        ExecutionStateMachine.transition_leg(leg_state, ExecutionLegStatus.SUBMITTED)

        is_perp = ":" in order.symbol
        base_url = self.binance_futures_testnet_url if is_perp else self.binance_spot_testnet_url
        endpoint = "/fapi/v1/order" if is_perp else "/api/v3/order"
        key = self.binance_futures_api_key if is_perp else self.binance_api_key
        sec = self.binance_futures_api_secret if is_perp else self.binance_api_secret
        offset = self._futures_clock_offset_ms if is_perp else self._spot_clock_offset_ms

        symbol_clean = order.symbol.split(":")[0].replace("/", "")
        timestamp = int(time.time() * 1000) + offset

        params: dict[str, str] = {
            "symbol": symbol_clean,
            "side": order.side.value,
            "type": "MARKET" if order.order_type == OrderType.MARKET else "LIMIT",
            "quantity": str(order.quantity),
            "recvWindow": "5000",
            "timestamp": str(timestamp),
        }
        if order.price and order.order_type != OrderType.MARKET:
            params["price"] = str(order.price)
            params["timeInForce"] = order.time_in_force.value

        query_string = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        signature = hmac.new(
            sec.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = signature

        headers = {"X-MBX-APIKEY": key}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base_url}{endpoint}", params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    data = await resp.json()
                    if resp.status == 200:
                        leg_state.exchange_order_id = str(data.get("orderId", ""))
                        fill_qty = Decimal(str(data.get("executedQty", order.quantity)))
                        cum_quote = Decimal(str(data.get("cummulativeQuoteQty", "0")))
                        avg_price = cum_quote / fill_qty if fill_qty > Decimal("0") else (order.price or Decimal("0"))

                        leg_state.filled_qty = fill_qty
                        leg_state.average_fill_price = avg_price
                        ExecutionStateMachine.transition_leg(leg_state, ExecutionLegStatus.FILLED)
                    else:
                        ExecutionStateMachine.transition_leg(
                            leg_state,
                            ExecutionLegStatus.FAILED,
                            error_message=f"Testnet reject: {data.get('msg', 'Unknown error')}",
                        )
        except Exception as exc:
            ExecutionStateMachine.transition_leg(
                leg_state,
                ExecutionLegStatus.FAILED,
                error_message=f"Connection failure to testnet: {exc}",
            )

        return leg_state
