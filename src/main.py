"""Central Autonomous Orchestrator and CLI entrypoint for Crypto Arbitrage.

Integrates all layers: Market Data -> Strategies -> Evaluator -> Risk -> Executor -> Persistence.
Includes FastAPI control server and graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from collections import deque
from decimal import Decimal
from typing import Any, Optional

from config.settings import SystemSettings, settings as default_settings
from src.core.models import (
    ArbitrageOpportunity,
    ExchangeId,
    ExecutionLegState,
    ExecutionOrder,
    ExecutionPlan,
    ExecutionPlanStatus,
    FundingRateSnapshot,
    HedgedPositionStatus,
    InstrumentType,
    NormalizedOrderBook,
    OpportunityLifecycleState,
    OrderSide,
    OrderType,
    StrategyType,
    SymbolMetadata,
)
from src.core.interfaces import OrderBookEngine
from src.execution.base_executor import BaseExecutor
from src.execution.live_executor import LiveExecutor
from src.execution.paper_executor import PaperExecutor
from src.execution.testnet_executor import TestnetExecutor
from src.market_data.market_registry import MarketRegistry
from src.opportunity.evaluator import OpportunityEvaluator
from src.opportunity.fee_calculator import FeeCalculator
from src.persistence.database import create_db_engine, get_session_factory, init_db
from src.persistence.writer_worker import DualQueuePersistenceWriter
from src.portfolio.balances import BalanceManager
from src.portfolio.inventory import InventoryBalancer
from src.portfolio.positions import HedgedPositionManager
from src.risk.exposure import ExposureTracker
from src.risk.limits import RiskLimits
from src.risk.risk_manager import CentralRiskManager
from src.strategies.cross_exchange import CrossExchangeStrategy
from src.strategies.funding_basis import FundingBasisStrategy
from src.strategies.stat_arb import StatArbStrategy
from src.strategies.triangular import TriangularStrategy

logger = logging.getLogger("orchestrator")


class ArbitrageSystemOrchestrator:
    """Master event-driven orchestrator executing the full tick-to-trade lifecycle."""

    def __init__(
        self,
        settings: Optional[SystemSettings] = None,
        market_registry: Optional[MarketRegistry] = None,
        balance_manager: Optional[BalanceManager] = None,
        inventory_balancer: Optional[InventoryBalancer] = None,
        position_manager: Optional[HedgedPositionManager] = None,
        risk_manager: Optional[CentralRiskManager] = None,
        opportunity_evaluator: Optional[OpportunityEvaluator] = None,
        cross_exchange_strategy: Optional[CrossExchangeStrategy] = None,
        triangular_strategy: Optional[TriangularStrategy] = None,
        funding_basis_strategy: Optional[FundingBasisStrategy] = None,
        stat_arb_strategy: Optional[StatArbStrategy] = None,
        executor: Optional[BaseExecutor] = None,
        persistence_writer: Optional[DualQueuePersistenceWriter] = None,
        order_book_engine: Optional[OrderBookEngine] = None,
    ) -> None:
        self.settings = settings or default_settings
        self.market_registry = market_registry or MarketRegistry()
        self.balance_manager = balance_manager or BalanceManager()
        self.inventory_balancer = inventory_balancer or InventoryBalancer(self.balance_manager)
        self.position_manager = position_manager or HedgedPositionManager()
        self.exposure_tracker = ExposureTracker()

        risk_limits = RiskLimits(
            max_notional_per_trade=self.settings.strategy_1.max_notional_usdt,
            max_daily_loss_usdt=self.settings.risk_limits.max_daily_loss_usdt,
            max_open_plans=self.settings.risk_limits.max_open_plans,
            max_data_age_ms=self.settings.max_data_age_ms,
        )
        self.risk_manager = risk_manager or CentralRiskManager(
            balance_manager=self.balance_manager,
            limits=risk_limits,
            exposure_tracker=self.exposure_tracker,
        )

        self.fee_calculator = FeeCalculator()
        self.opportunity_evaluator = opportunity_evaluator or OpportunityEvaluator(
            max_data_age_ms=self.settings.max_data_age_ms,
            min_net_edge_cross_bps=self.settings.strategy_1.min_net_edge_bps,
            min_net_edge_tri_bps=self.settings.strategy_2.min_net_edge_bps,
        )
        self.order_book_engine = order_book_engine

        # Strategy Engines
        self.cross_exchange_strategy = cross_exchange_strategy or CrossExchangeStrategy(
            market_registry=self.market_registry,
            min_net_edge_bps=self.settings.strategy_1.min_net_edge_bps,
            max_notional_per_trade=self.settings.strategy_1.max_notional_usdt,
        )
        self.triangular_strategy = triangular_strategy or TriangularStrategy(
            exchange=ExchangeId.BINANCE,
            market_registry=self.market_registry,
            min_net_edge_bps=self.settings.strategy_2.min_net_edge_bps,
            max_notional_per_trade=self.settings.strategy_2.max_notional_usdt,
            custom_cycles=self.settings.strategy_2.initial_cycles,
        )
        self.funding_basis_strategy = funding_basis_strategy or FundingBasisStrategy(
            market_registry=self.market_registry,
            min_funding_bps_per_8h=self.settings.strategy_3.min_funding_rate_bps_per_8h,
            min_annualized_yield_pct=self.settings.strategy_3.min_annualized_yield_pct,
            max_notional_per_trade=self.settings.strategy_1.max_notional_usdt,
        )
        self.stat_arb_strategy = stat_arb_strategy or StatArbStrategy(
            market_registry=self.market_registry,
            notional_per_leg=self.settings.strategy_4.max_notional_per_leg_usdt,
        )

        # Executor selection
        if executor:
            self.executor = executor
        elif self.settings.execution_mode == "LIVE":
            self.executor = LiveExecutor(
                balance_manager=self.balance_manager,
                risk_manager=self.risk_manager,
                order_book_engine=self.order_book_engine,
                market_registry=self.market_registry,
            )
        elif self.settings.execution_mode == "TESTNET":
            self.executor = TestnetExecutor(
                balance_manager=self.balance_manager,
                risk_manager=self.risk_manager,
                binance_api_key=self.settings.binance_api_key,
                binance_api_secret=self.settings.binance_api_secret,
                order_book_engine=self.order_book_engine,
                market_registry=self.market_registry,
            )
        else:
            self.executor = PaperExecutor(
                balance_manager=self.balance_manager,
                risk_manager=self.risk_manager,
                order_book_engine=self.order_book_engine,
                market_registry=self.market_registry,
                simulated_latency_ms=0,
            )

        self.persistence_writer = persistence_writer or DualQueuePersistenceWriter(
            telemetry_queue_maxsize=self.settings.persistence.telemetry_queue_maxsize,
        )

        # Operational telemetry buffers
        self._recent_opportunities: deque[ArbitrageOpportunity] = deque(maxlen=500)
        self._recent_executions: deque[ExecutionPlan] = deque(maxlen=500)
        self._active_connections: int = 0
        self._running: bool = False

        # Register standard default instruments and seed initial balances
        self._seed_default_symbols()
        if self.settings.execution_mode == "PAPER":
            self._seed_paper_balances()

    # --------------------------------------------------------------------------
    # Initialization Helpers
    # --------------------------------------------------------------------------

    def _seed_default_symbols(self) -> None:
        """Register default canonical trading pairs."""
        pairs = [
            (ExchangeId.BINANCE, "BTC/USDT", "BTCUSDT", "BTC", "USDT", Decimal("0.01"), Decimal("0.00001"), Decimal("0.0001"), Decimal("5.0"), Decimal("0.001")),
            (ExchangeId.BINANCE, "ETH/USDT", "ETHUSDT", "ETH", "USDT", Decimal("0.01"), Decimal("0.0001"), Decimal("0.001"), Decimal("5.0"), Decimal("0.001")),
            (ExchangeId.BINANCE, "SOL/USDT", "SOLUSDT", "SOL", "USDT", Decimal("0.01"), Decimal("0.01"), Decimal("0.01"), Decimal("5.0"), Decimal("0.001")),
            (ExchangeId.BINANCE, "XRP/USDT", "XRPUSDT", "XRP", "USDT", Decimal("0.0001"), Decimal("0.1"), Decimal("1.0"), Decimal("5.0"), Decimal("0.001")),
            (ExchangeId.BINANCE, "ETH/BTC", "ETHBTC", "ETH", "BTC", Decimal("0.00001"), Decimal("0.001"), Decimal("0.001"), Decimal("0.0001"), Decimal("0.001")),
            (ExchangeId.KRAKEN, "BTC/USDT", "XBTUSDT", "BTC", "USDT", Decimal("0.1"), Decimal("0.0001"), Decimal("0.0001"), Decimal("5.0"), Decimal("0.004")),
            (ExchangeId.KRAKEN, "ETH/USDT", "ETHUSDT", "ETH", "USDT", Decimal("0.01"), Decimal("0.001"), Decimal("0.001"), Decimal("5.0"), Decimal("0.004")),
            (ExchangeId.KRAKEN, "SOL/USDT", "SOLUSDT", "SOL", "USDT", Decimal("0.01"), Decimal("0.01"), Decimal("0.01"), Decimal("5.0"), Decimal("0.004")),
            (ExchangeId.KRAKEN, "XRP/USDT", "XRPUSDT", "XRP", "USDT", Decimal("0.0001"), Decimal("1.0"), Decimal("1.0"), Decimal("5.0"), Decimal("0.004")),
        ]
        for ex, sym, nat, base, quote, tick, lot, min_q, min_not, fee in pairs:
            self.market_registry.register(
                SymbolMetadata(
                    exchange=ex,
                    symbol=sym,
                    native_symbol=nat,
                    base_asset=base,
                    quote_asset=quote,
                    tick_size=tick,
                    lot_size=lot,
                    min_qty=min_q,
                    min_notional=min_not,
                    taker_fee_rate=fee,
                )
            )

        # Register Binance BTC/USDT Perpetual
        self.market_registry.register(
            SymbolMetadata(
                exchange=ExchangeId.BINANCE,
                symbol="BTC/USDT:USDT",
                native_symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                instrument_type=InstrumentType.LINEAR_PERP,
                tick_size=Decimal("0.10"),
                lot_size=Decimal("0.001"),
                min_qty=Decimal("0.001"),
                min_notional=Decimal("5.0"),
                taker_fee_rate=Decimal("0.0005"),
                max_leverage=2,
            )
        )

    def _seed_paper_balances(self) -> None:
        """Seed initial inventory for paper trading mode."""
        self.balance_manager.set_balance(ExchangeId.BINANCE, "USDT", free=Decimal("10000.0"))
        self.balance_manager.set_balance(ExchangeId.BINANCE, "BTC", free=Decimal("1.0"))
        self.balance_manager.set_balance(ExchangeId.BINANCE, "ETH", free=Decimal("10.0"))
        self.balance_manager.set_balance(ExchangeId.BINANCE, "SOL", free=Decimal("50.0"))
        self.balance_manager.set_balance(ExchangeId.BINANCE, "XRP", free=Decimal("5000.0"))
        self.balance_manager.set_balance(ExchangeId.BINANCE, "BNB", free=Decimal("10.0"))

        self.balance_manager.set_balance(ExchangeId.KRAKEN, "USDT", free=Decimal("10000.0"))
        self.balance_manager.set_balance(ExchangeId.KRAKEN, "BTC", free=Decimal("1.0"))
        self.balance_manager.set_balance(ExchangeId.KRAKEN, "ETH", free=Decimal("10.0"))
        self.balance_manager.set_balance(ExchangeId.KRAKEN, "SOL", free=Decimal("50.0"))
        self.balance_manager.set_balance(ExchangeId.KRAKEN, "XRP", free=Decimal("5000.0"))

    # --------------------------------------------------------------------------
    # Tick-to-Trade Core Pipeline
    # --------------------------------------------------------------------------

    async def handle_orderbook_update(self, book: NormalizedOrderBook) -> list[ExecutionPlan]:
        """Core non-blocking tick-to-trade dispatch on L2 orderbook update."""
        # 1. Enqueue order book snapshot to telemetry queue
        await self.persistence_writer.enqueue_order_book_snapshot(book)

        executed_plans: list[ExecutionPlan] = []
        raw_opportunities: list[ArbitrageOpportunity] = []

        # 2. Evaluate Strategies
        # Strategy 1: Cross-Exchange Spot
        opps_cross = await self.cross_exchange_strategy.process_orderbook_update(book)
        raw_opportunities.extend(opps_cross)

        # Strategy 2: Triangular
        if book.exchange == ExchangeId.BINANCE:
            opps_tri = await self.triangular_strategy.process_orderbook_update(book)
            raw_opportunities.extend(opps_tri)

        # Strategy 3: Funding / Basis Carry
        if ":" in book.symbol:
            self.funding_basis_strategy.update_perp_book(book)
        else:
            self.funding_basis_strategy.update_spot_book(book)
        opps_fb = await self.funding_basis_strategy.process_orderbook_update(book)
        raw_opportunities.extend(opps_fb)

        # Strategy 4: Statistical Arbitrage
        opps_stat = await self.stat_arb_strategy.process_orderbook_update(book)
        raw_opportunities.extend(opps_stat)

        # 3. Process each detected opportunity through Evaluator -> Risk -> Execution
        for opp in raw_opportunities:
            evaluated_opp = await self.opportunity_evaluator.evaluate_and_emit(opp, [book])
            if evaluated_opp is None:
                continue
            self._recent_opportunities.append(evaluated_opp)
            await self.persistence_writer.enqueue_opportunity(evaluated_opp)

            if evaluated_opp.lifecycle_state != OpportunityLifecycleState.EXECUTABLE_OPPORTUNITY:
                continue

            # Pre-trade Risk Gating
            decision = await self.risk_manager.evaluate_opportunity(evaluated_opp)
            if not decision.is_approved:
                logger.info(f"Opportunity {evaluated_opp.id} rejected by RiskManager: {decision.reason}")
                continue

            # Build and dispatch ExecutionPlan
            plan = self._build_execution_plan(evaluated_opp)
            await self.exposure_tracker.add_open_plan(plan.plan_id, plan.target_notional)

            try:
                result_plan = await self.executor.execute_plan(plan)
            finally:
                await self.exposure_tracker.remove_open_plan(plan.plan_id)

            self._recent_executions.append(result_plan)
            await self.persistence_writer.enqueue_execution_plan(result_plan)

            # Record realized PnL in exposure tracker
            if result_plan.realized_pnl != Decimal("0"):
                await self.exposure_tracker.record_realized_pnl(result_plan.realized_pnl)

            # Register stateful positions (Strategies 3 & 4) upon completion
            if (
                result_plan.status == ExecutionPlanStatus.COMPLETED
                and result_plan.strategy_type in (StrategyType.FUNDING_BASIS, StrategyType.STAT_ARB)
            ):
                await self.position_manager.open_hedged_position(result_plan)

            executed_plans.append(result_plan)

        return executed_plans

    async def handle_funding_update(self, snapshot: FundingRateSnapshot) -> None:
        """Handle incoming derivatives mark-price and funding rate snapshot."""
        await self.persistence_writer.enqueue_funding_snapshot(snapshot)
        self.funding_basis_strategy.update_funding_snapshot(snapshot)
        await self.position_manager.accrue_funding(snapshot)

    def _build_execution_plan(self, opp: ArbitrageOpportunity) -> ExecutionPlan:
        """Transform ArbitrageOpportunity into an executable multi-leg ExecutionPlan."""
        plan_id = f"plan_{opp.id}_{int(time.time() * 1000)}"
        legs: list[ExecutionLegState] = []

        for leg in opp.legs:
            order = ExecutionOrder(
                client_order_id=f"ord_{plan_id}_{leg.leg_index}",
                exchange=leg.exchange,
                symbol=leg.symbol,
                side=leg.side,
                order_type=OrderType.IMMEDIATE_OR_CANCEL,
                price=leg.expected_price,
                quantity=leg.expected_qty,
            )
            legs.append(ExecutionLegState(leg_index=leg.leg_index, order=order))

        return ExecutionPlan(
            plan_id=plan_id,
            opportunity_id=opp.id,
            strategy_type=opp.strategy_type,
            status=ExecutionPlanStatus.PENDING,
            legs=legs,
            target_notional=opp.target_notional,
        )

    # --------------------------------------------------------------------------
    # API & Telemetry State Inspection
    # --------------------------------------------------------------------------

    def get_health_status(self) -> dict[str, Any]:
        """System health and operational metrics."""
        is_halted = self.risk_manager.is_kill_switch_active()
        return {
            "status": "HALTED" if is_halted else "HEALTHY",
            "execution_mode": self.settings.execution_mode,
            "kill_switch_active": is_halted,
            "kill_switch_reason": self.risk_manager.kill_switch_reason,
            "active_connections": self._active_connections,
            "persistence_queue_depth": {
                "critical_queue": self.persistence_writer.critical_queue.qsize(),
                "telemetry_queue": self.persistence_writer.telemetry_queue.qsize(),
                "dropped_telemetry_count": self.persistence_writer.dropped_telemetry_count,
            },
        }

    def get_portfolio_summary(self) -> dict[str, Any]:
        """Current account balances, inventory skew, and open hedged positions."""
        balances = [b.model_dump() for b in self.balance_manager.get_all_balances()]
        assets = ["USDT", "BTC", "ETH", "SOL", "XRP"]
        skew = {
            asset: {ex.value: float(v) for ex, v in self.inventory_balancer.calculate_skew(asset).items()}
            for asset in assets
        }
        open_positions = [p.model_dump() for p in self.position_manager.get_open_positions()]
        return {
            "balances": balances,
            "skew": skew,
            "open_positions": open_positions,
            "daily_realized_pnl_usdt": str(self.exposure_tracker.daily_realized_pnl),
            "high_water_mark_usdt": str(self.exposure_tracker.high_water_mark),
        }

    def get_recent_opportunities(
        self, limit: int = 50, state: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Return cached recent opportunities."""
        opps = list(self._recent_opportunities)
        if state:
            opps = [o for o in opps if o.lifecycle_state.value == state.upper()]
        return [o.model_dump() for o in reversed(opps[-limit:])]

    def get_recent_executions(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return cached recent execution plans."""
        plans = list(self._recent_executions)
        return [p.model_dump() for p in reversed(plans[-limit:])]

    def set_kill_switch(self, active: bool, reason: str = "Manual operator action") -> None:
        """Manually engage or disengage the global kill switch."""
        if active:
            self.risk_manager.activate_kill_switch(reason)
        else:
            self.risk_manager.deactivate_kill_switch()

    # --------------------------------------------------------------------------
    # Lifecycle Management
    # --------------------------------------------------------------------------

    async def start(self) -> None:
        """Start background persistence writer and background workers."""
        if self._running:
            return
        self._running = True
        await self.persistence_writer.start()
        self._active_connections = 2  # Binance and Kraken
        logger.info(f"ArbitrageSystemOrchestrator started in {self.settings.execution_mode} mode")

    async def stop(self) -> None:
        """Gracefully stop system and drain queues."""
        if not self._running:
            return
        self._running = False
        await self.persistence_writer.stop()
        self._active_connections = 0
        logger.info("ArbitrageSystemOrchestrator stopped gracefully")


# ==============================================================================
# CLI Entrypoint
# ==============================================================================

async def main() -> None:
    """Launch orchestrator and FastAPI monitoring server."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    orchestrator = ArbitrageSystemOrchestrator()
    await orchestrator.start()

    from src.api.routes import create_app
    import uvicorn

    app = create_app(orchestrator)
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    server_task = asyncio.create_task(server.serve())

    try:
        await stop_event.wait()
    finally:
        server.should_exit = True
        await server_task
        await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())
