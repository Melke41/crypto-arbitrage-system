"""Domain models and data structures for the Crypto Arbitrage Intelligence & Trading System.

All numerical financial computations use exact Decimal representations to prevent
floating-point rounding errors. Float types are prohibited for balance accounting,
order sizing, and fee computation.
"""

from __future__ import annotations

import enum
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ==============================================================================
# Enumerations
# ==============================================================================


class ExchangeId(str, enum.Enum):
    """Supported exchange identifiers."""

    BINANCE = "BINANCE"
    KRAKEN = "KRAKEN"


class OrderSide(str, enum.Enum):
    """Trading order sides."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, enum.Enum):
    """Supported order types."""

    LIMIT = "LIMIT"
    MARKET = "MARKET"
    LIMIT_MAKER = "LIMIT_MAKER"
    IMMEDIATE_OR_CANCEL = "IOC"


class TimeInForce(str, enum.Enum):
    """Time in force policies."""

    IOC = "IOC"  # Immediate or Cancel (default for arbitrage)
    FOK = "FOK"  # Fill or Kill
    GTC = "GTC"  # Good 'Til Cancelled


class InstrumentType(str, enum.Enum):
    """Financial instrument classification."""

    SPOT = "SPOT"
    LINEAR_PERP = "LINEAR_PERP"      # USDⓈ-M linear perpetual futures (settled in USDT/USDC)
    INVERSE_PERP = "INVERSE_PERP"    # Coin-margined inverse perpetual futures


class StrategyType(str, enum.Enum):
    """Four quantitative arbitrage & alpha strategy archetypes."""

    CROSS_EXCHANGE = "CROSS_EXCHANGE"
    CROSS_EXCHANGE_SPOT = "CROSS_EXCHANGE_SPOT"  # Backward compatibility alias
    TRIANGULAR = "TRIANGULAR"
    TRIANGULAR_SPOT = "TRIANGULAR_SPOT"          # Backward compatibility alias
    FUNDING_BASIS = "FUNDING_BASIS"              # Delta-neutral Spot vs. Linear Perp Carry
    STAT_ARB = "STAT_ARB"                        # Cointegrated pairs / mean-reverting spread alpha


class StatArbAction(str, enum.Enum):
    """Statistical arbitrage signals."""

    ENTER_LONG_SPREAD = "ENTER_LONG_SPREAD"    # Buy Asset A, Sell Asset B * beta
    ENTER_SHORT_SPREAD = "ENTER_SHORT_SPREAD"  # Sell Asset A, Buy Asset B * beta
    EXIT_SPREAD = "EXIT_SPREAD"                # Mean reversion achieved (take profit)
    STOP_LOSS_SPREAD = "STOP_LOSS_SPREAD"      # Structural break / Z-score stop breached
    HOLD = "HOLD"


class HedgedPositionStatus(str, enum.Enum):
    """Lifecycle status of a stateful hedged position."""

    OPENING = "OPENING"
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    EMERGENCY_UNWIND = "EMERGENCY_UNWIND"


class OpportunityLifecycleState(str, enum.Enum):
    """Five mandatory lifecycle stages tracking opportunity maturity."""

    THEORETICAL_OPPORTUNITY = "THEORETICAL_OPPORTUNITY"  # Top-of-book gross spread > 0
    EXECUTABLE_OPPORTUNITY = "EXECUTABLE_OPPORTUNITY"    # Depth VWAP net of fees & min notional > edge threshold
    SIMULATED_FILL = "SIMULATED_FILL"                    # Paper-traded with simulated latency/queue decay
    DEMO_TESTNET_FILL = "DEMO_TESTNET_FILL"              # Executed against Binance Spot Testnet / sandbox
    LIVE_FILL = "LIVE_FILL"                              # Executed on live production exchange


class ExecutionLegStatus(str, enum.Enum):
    """Lifecycle state of an individual leg within a multi-leg execution."""

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNWOUND = "UNWOUND"


class ExecutionPlanStatus(str, enum.Enum):
    """Lifecycle state of an entire multi-leg arbitrage execution plan."""

    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    PARTIALLY_FAILED = "PARTIALLY_FAILED"
    UNWINDING = "UNWINDING"
    UNWOUND = "UNWOUND"
    FAILED = "FAILED"


# ==============================================================================
# Market Data Models
# ==============================================================================


class OrderBookLevel(BaseModel):
    """Single price level in an L2 order book."""

    model_config = ConfigDict(frozen=True)

    price: Decimal = Field(..., description="Level price")
    quantity: Decimal = Field(..., description="Level quantity available")

    @field_validator("price", "quantity", mode="before")
    @classmethod
    def parse_decimal(cls, v: Any) -> Decimal:
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))


class NormalizedOrderBook(BaseModel):
    """Normalized, sequence-checked L2 order book representation."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    exchange: ExchangeId
    symbol: str = Field(..., description="Normalized symbol, e.g. BTC/USDT")
    bids: list[OrderBookLevel] = Field(default_factory=list, description="Bids sorted descending by price")
    asks: list[OrderBookLevel] = Field(default_factory=list, description="Asks sorted ascending by price")
    sequence_id: int = Field(default=0, description="Sequence identifier or lastUpdateId")
    exchange_timestamp_ms: int = Field(default=0, description="Exchange server matching timestamp")
    local_receipt_timestamp_ms: int = Field(default=0, description="Local machine packet receipt timestamp")
    checksum: Optional[int] = Field(default=None, description="CRC32 checksum (Kraken book v2)")
    is_stale: bool = Field(default=False, description="Flagged true if receipt age exceeds threshold or desync")

    @property
    def best_bid(self) -> Optional[OrderBookLevel]:
        """Return highest bid level or None if book empty."""
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[OrderBookLevel]:
        """Return lowest ask level or None if book empty."""
        return self.asks[0] if self.asks else None

    @property
    def mid_price(self) -> Optional[Decimal]:
        """Compute mid price between best bid and best ask."""
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].price + self.asks[0].price) / Decimal("2")

    @property
    def spread(self) -> Optional[Decimal]:
        """Compute absolute spread (best_ask - best_bid)."""
        if not self.bids or not self.asks:
            return None
        return self.asks[0].price - self.bids[0].price

    @property
    def spread_bps(self) -> Optional[Decimal]:
        """Compute spread in basis points relative to mid price."""
        mid = self.mid_price
        spr = self.spread
        if mid is None or spr is None or mid == Decimal("0"):
            return None
        return (spr / mid) * Decimal("10000")

    def is_valid_age(self, max_data_age_ms: int, current_timestamp_ms: Optional[int] = None) -> bool:
        """Check if order book timestamp is within acceptable latency freshness window."""
        now_ms = current_timestamp_ms or int(time.time() * 1000)
        age = now_ms - self.local_receipt_timestamp_ms
        return age <= max_data_age_ms and not self.is_stale

    def vwap_ask(self, target_qty: Decimal) -> tuple[Decimal, Decimal]:
        """Compute volume-weighted average price (VWAP) to BUY target_qty by walking asks.

        Returns:
            tuple[Decimal, Decimal]: (vwap_price, total_fill_qty).
            If available depth < target_qty, returns fillable VWAP and filled qty.
        """
        if target_qty <= Decimal("0") or not self.asks:
            return Decimal("0"), Decimal("0")

        remaining = target_qty
        total_cost = Decimal("0")
        filled_qty = Decimal("0")

        for level in self.asks:
            fill = min(remaining, level.quantity)
            total_cost += fill * level.price
            filled_qty += fill
            remaining -= fill
            if remaining <= Decimal("0"):
                break

        if filled_qty == Decimal("0"):
            return Decimal("0"), Decimal("0")

        vwap = total_cost / filled_qty
        return vwap, filled_qty

    def vwap_bid(self, target_qty: Decimal) -> tuple[Decimal, Decimal]:
        """Compute volume-weighted average price (VWAP) to SELL target_qty by walking bids.

        Returns:
            tuple[Decimal, Decimal]: (vwap_price, total_fill_qty).
            If available depth < target_qty, returns fillable VWAP and filled qty.
        """
        if target_qty <= Decimal("0") or not self.bids:
            return Decimal("0"), Decimal("0")

        remaining = target_qty
        total_proceeds = Decimal("0")
        filled_qty = Decimal("0")

        for level in self.bids:
            fill = min(remaining, level.quantity)
            total_proceeds += fill * level.price
            filled_qty += fill
            remaining -= fill
            if remaining <= Decimal("0"):
                break

        if filled_qty == Decimal("0"):
            return Decimal("0"), Decimal("0")

        vwap = total_proceeds / filled_qty
        return vwap, filled_qty


class SymbolMetadata(BaseModel):
    """Exchange market filters, precision constraints, and fee tiers for Spot & Perpetuals."""

    model_config = ConfigDict(frozen=True)

    exchange: ExchangeId
    symbol: str = Field(..., description="Canonical unified symbol, e.g. BTC/USDT")
    native_symbol: str = Field(..., description="Exchange native ticker, e.g. BTCUSDT or XBTUSDT")
    base_asset: str = Field(..., description="Base currency, e.g. BTC")
    quote_asset: str = Field(..., description="Quote currency, e.g. USDT")
    instrument_type: InstrumentType = Field(default=InstrumentType.SPOT, description="Spot or Perpetual derivative")
    tick_size: Decimal = Field(..., description="Minimum price increment (PRICE_FILTER)")
    lot_size: Decimal = Field(..., description="Minimum quantity step size (LOT_SIZE)")
    min_qty: Decimal = Field(..., description="Minimum order quantity")
    max_qty: Optional[Decimal] = Field(default=None, description="Maximum order quantity")
    min_notional: Decimal = Field(..., description="Minimum order cost in quote currency")
    maker_fee_rate: Decimal = Field(default=Decimal("0.001"), description="Maker fee rate as fraction")
    taker_fee_rate: Decimal = Field(default=Decimal("0.001"), description="Taker fee rate as fraction")
    contract_size: Decimal = Field(default=Decimal("1"), description="Contract multiplier for derivatives")
    max_leverage: int = Field(default=1, description="Maximum allowable leverage on derivative instrument")
    maintenance_margin_rate: Decimal = Field(default=Decimal("0.0"), description="Maintenance margin fraction (e.g. 0.005 for 0.5%)")
    funding_interval_hours: int = Field(default=8, description="Funding rate settlement interval (typically 8h or 4h)")

    @field_validator(
        "tick_size", "lot_size", "min_qty", "min_notional", "maker_fee_rate", "taker_fee_rate",
        "contract_size", "maintenance_margin_rate",
        mode="before"
    )
    @classmethod
    def parse_decimal(cls, v: Any) -> Decimal:
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))

    def truncate_amount(self, quantity: Decimal) -> Decimal:
        """Floor truncate order quantity to exchange lot_size step-size multiple: (quantity // lot_size) * lot_size."""
        if self.lot_size <= Decimal("0"):
            return quantity
        steps = (quantity // self.lot_size)
        return steps * self.lot_size

    def truncate_price(self, price: Decimal) -> Decimal:
        """Floor truncate price to exchange tick_size increment: (price // tick_size) * tick_size."""
        if self.tick_size <= Decimal("0"):
            return price
        steps = (price // self.tick_size)
        return steps * self.tick_size

    def round_price(self, price: Decimal) -> Decimal:
        """Round price to nearest tick_size increment (half even)."""
        if self.tick_size <= Decimal("0"):
            return price
        steps = (price / self.tick_size).to_integral_value(rounding=ROUND_HALF_EVEN)
        return steps * self.tick_size

    def validate_order(self, price: Decimal, quantity: Decimal) -> tuple[bool, Optional[str]]:
        """Validate an order against tick_size, lot_size, min_qty, and min_notional filters."""
        if quantity < self.min_qty:
            return False, f"Quantity {quantity} below min_qty {self.min_qty}"
        if self.max_qty and quantity > self.max_qty:
            return False, f"Quantity {quantity} exceeds max_qty {self.max_qty}"
        notional = price * quantity
        if notional < self.min_notional:
            return False, f"Notional {notional} below min_notional {self.min_notional}"
        return True, None

    @staticmethod
    def match_cross_exchange_quantity(
        meta_buy: SymbolMetadata,
        meta_sell: SymbolMetadata,
        target_quantity: Decimal,
        buy_price: Decimal,
        sell_price: Decimal,
    ) -> tuple[bool, Decimal, Optional[str]]:
        """Validate and harmonize executable quantity across two exchanges.

        Floor-truncates quantity to satisfy both exchanges' step_size (lot_size) rules
        and verifies that resulting quantity and prices satisfy min_qty, max_qty,
        and min_notional on both venues simultaneously.
        """
        # Sequential floor truncation ensures step conformity across both exchange grids
        qty = meta_buy.truncate_amount(target_quantity)
        qty = meta_sell.truncate_amount(qty)
        qty = meta_buy.truncate_amount(qty)

        if qty <= Decimal("0"):
            return False, Decimal("0"), "Quantity truncated to zero due to lot_size incompatibility"

        # Validate on buy venue
        valid_buy, err_buy = meta_buy.validate_order(buy_price, qty)
        if not valid_buy:
            return False, Decimal("0"), f"Buy exchange ({meta_buy.exchange.value}) constraint failed: {err_buy}"

        # Validate on sell venue
        valid_sell, err_sell = meta_sell.validate_order(sell_price, qty)
        if not valid_sell:
            return False, Decimal("0"), f"Sell exchange ({meta_sell.exchange.value}) constraint failed: {err_sell}"

        return True, qty, None


# ==============================================================================
# Opportunity Models
# ==============================================================================


class ArbitrageLeg(BaseModel):
    """Specification of an individual trade leg in an arbitrage opportunity."""

    model_config = ConfigDict(frozen=True)

    leg_index: int = Field(..., ge=0, description="Zero-based sequence index")
    exchange: ExchangeId
    symbol: str
    side: OrderSide
    expected_price: Decimal
    expected_qty: Decimal
    expected_fee: Decimal
    fee_asset: str

    @field_validator("expected_price", "expected_qty", "expected_fee", mode="before")
    @classmethod
    def parse_decimal(cls, v: Any) -> Decimal:
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))


class ArbitrageOpportunity(BaseModel):
    """Complete specification of a detected arbitrage opportunity."""

    id: str = Field(..., description="Unique opportunity UUID or timestamp-hash")
    strategy_type: StrategyType
    lifecycle_state: OpportunityLifecycleState = OpportunityLifecycleState.THEORETICAL_OPPORTUNITY
    symbol_legs: list[str] = Field(..., description="Ordered list of symbols participating in cycle")
    legs: list[ArbitrageLeg] = Field(..., description="Detailed execution legs")
    gross_edge_bps: Decimal = Field(..., description="Gross theoretical edge in basis points")
    net_edge_bps: Decimal = Field(..., description="Net executable edge in basis points after fees and slippage")
    target_notional: Decimal = Field(..., description="Total capital notional allocated")
    expected_net_profit: Decimal = Field(..., description="Net profit denominated in profit_asset")
    profit_asset: str = Field(default="USDT", description="Currency of expected profit")
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    metadata: dict[str, Any] = Field(default_factory=dict, description="Strategy-specific diagnostic details")

    @field_validator("gross_edge_bps", "net_edge_bps", "target_notional", "expected_net_profit", mode="before")
    @classmethod
    def parse_decimal(cls, v: Any) -> Decimal:
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))


# ==============================================================================
# Risk Models
# ==============================================================================


class RiskDecision(BaseModel):
    """Output decision from pre-trade risk evaluation."""

    is_approved: bool = Field(..., description="True if opportunity passes all pre-trade risk gates")
    reason: str = Field(default="Approved", description="Decision reason or rejection explanation")
    adjusted_notional: Optional[Decimal] = Field(default=None, description="Clamped capital notional if reduced by risk limits")
    rejection_code: Optional[str] = Field(default=None, description="Structured error code if rejected")
    checked_at_ms: int = Field(default_factory=lambda: int(time.time() * 1000))

    @field_validator("adjusted_notional", mode="before")
    @classmethod
    def parse_decimal(cls, v: Any) -> Optional[Decimal]:
        if v is None or isinstance(v, Decimal):
            return v
        return Decimal(str(v))


# ==============================================================================
# Execution Models
# ==============================================================================


class ExecutionOrder(BaseModel):
    """Order parameters prepared for exchange submission."""

    client_order_id: str
    exchange: ExchangeId
    symbol: str
    side: OrderSide
    order_type: OrderType = OrderType.IMMEDIATE_OR_CANCEL
    time_in_force: TimeInForce = TimeInForce.IOC
    price: Optional[Decimal] = None
    quantity: Decimal
    created_at_ms: int = Field(default_factory=lambda: int(time.time() * 1000))

    @field_validator("price", "quantity", mode="before")
    @classmethod
    def parse_decimal(cls, v: Any) -> Any:
        if v is None or isinstance(v, Decimal):
            return v
        return Decimal(str(v))


class ExecutionLegState(BaseModel):
    """Live state tracking for an individual execution leg."""

    leg_index: int
    order: ExecutionOrder
    status: ExecutionLegStatus = ExecutionLegStatus.PENDING
    exchange_order_id: Optional[str] = None
    filled_qty: Decimal = Field(default=Decimal("0"))
    average_fill_price: Optional[Decimal] = None
    fee_paid: Decimal = Field(default=Decimal("0"))
    fee_asset: Optional[str] = None
    error_message: Optional[str] = None
    submission_time_ms: Optional[int] = None
    completion_time_ms: Optional[int] = None

    @field_validator("filled_qty", "average_fill_price", "fee_paid", mode="before")
    @classmethod
    def parse_decimal(cls, v: Any) -> Any:
        if v is None or isinstance(v, Decimal):
            return v
        return Decimal(str(v))


class ExecutionPlan(BaseModel):
    """Multi-leg execution orchestration state machine."""

    plan_id: str
    opportunity_id: str
    strategy_type: StrategyType
    status: ExecutionPlanStatus = ExecutionPlanStatus.PENDING
    legs: list[ExecutionLegState] = Field(default_factory=list)
    target_notional: Decimal
    realized_pnl: Decimal = Field(default=Decimal("0"))
    pnl_asset: str = "USDT"
    created_at_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    updated_at_ms: int = Field(default_factory=lambda: int(time.time() * 1000))

    @field_validator("target_notional", "realized_pnl", mode="before")
    @classmethod
    def parse_decimal(cls, v: Any) -> Decimal:
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))


# ==============================================================================
# Portfolio & Balance Models
# ==============================================================================


class BalanceRecord(BaseModel):
    """Account inventory snapshot for a specific asset on an exchange."""

    exchange: ExchangeId
    asset: str
    free: Decimal
    locked: Decimal
    total: Decimal
    updated_at_ms: int = Field(default_factory=lambda: int(time.time() * 1000))

    @field_validator("free", "locked", "total", mode="before")
    @classmethod
    def parse_decimal(cls, v: Any) -> Decimal:
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))


# ==============================================================================
# Derivatives & Stateful Position Models (Strategies 3 & 4)
# ==============================================================================


class FundingRateSnapshot(BaseModel):
    """Snapshot of live funding rate, mark price, and basis for Perpetual Futures."""

    exchange: ExchangeId
    symbol: str = Field(..., description="Perpetual symbol, e.g. BTC/USDT:USDT")
    mark_price: Decimal
    index_price: Decimal
    spot_mid_price: Optional[Decimal] = None
    current_funding_rate: Decimal = Field(..., description="8h or 4h funding rate fraction, e.g. 0.0001 = 1 bp")
    predicted_funding_rate: Optional[Decimal] = None
    next_funding_timestamp_ms: int = 0
    funding_interval_hours: int = 8
    local_receipt_timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    is_stale: bool = False

    @field_validator(
        "mark_price", "index_price", "spot_mid_price", "current_funding_rate", "predicted_funding_rate",
        mode="before"
    )
    @classmethod
    def parse_decimal(cls, v: Any) -> Any:
        if v is None or isinstance(v, Decimal):
            return v
        return Decimal(str(v))

    @property
    def basis_bps(self) -> Optional[Decimal]:
        """Compute spot-futures basis in basis points: ((mark - spot) / spot) * 10,000."""
        if self.spot_mid_price is None or self.spot_mid_price == Decimal("0"):
            return None
        return ((self.mark_price - self.spot_mid_price) / self.spot_mid_price) * Decimal("10000")

    @property
    def annualized_funding_yield_pct(self) -> Decimal:
        """Annualized funding yield as a percentage (e.g. 15.0%)."""
        periods_per_year = Decimal(str(int(365 * 24 / self.funding_interval_hours)))
        return self.current_funding_rate * periods_per_year * Decimal("100")

    @property
    def annualized_funding_yield_bps(self) -> Decimal:
        """Annualized funding yield in basis points."""
        periods_per_year = Decimal(str(int(365 * 24 / self.funding_interval_hours)))
        return self.current_funding_rate * periods_per_year * Decimal("10000")


class StatArbSignal(BaseModel):
    """Signal parameters emitted by Strategy 4 (Statistical Arbitrage)."""

    pair_id: str = Field(..., description="Identifier for cointegrated pair, e.g. ETH_BTC_SPREAD")
    symbol_a: str
    symbol_b: str
    hedge_ratio_beta: Decimal = Field(..., description="Cointegration hedge ratio (beta)")
    spread_value: Decimal = Field(..., description="Current spread S_t = ln(P_a) - beta * ln(P_b)")
    spread_mean: Decimal = Field(..., description="Rolling spread mean")
    spread_std: Decimal = Field(..., description="Rolling spread standard deviation")
    z_score: Decimal = Field(..., description="Z-Score = (spread - mean) / std")
    half_life_seconds: Optional[Decimal] = Field(default=None, description="Ornstein-Uhlenbeck mean-reversion half-life")
    action: StatArbAction = StatArbAction.HOLD
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))

    @field_validator(
        "hedge_ratio_beta", "spread_value", "spread_mean", "spread_std", "z_score", "half_life_seconds",
        mode="before"
    )
    @classmethod
    def parse_decimal(cls, v: Any) -> Any:
        if v is None or isinstance(v, Decimal):
            return v
        return Decimal(str(v))


class PositionLeg(BaseModel):
    """Single leg of a stateful hedged position."""

    symbol: str
    exchange: ExchangeId
    instrument_type: InstrumentType
    side: OrderSide
    quantity: Decimal
    entry_price: Decimal
    current_price: Decimal
    unrealized_pnl: Decimal = Field(default=Decimal("0"))

    @field_validator("quantity", "entry_price", "current_price", "unrealized_pnl", mode="before")
    @classmethod
    def parse_decimal(cls, v: Any) -> Decimal:
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))


class HedgedPosition(BaseModel):
    """Open stateful position tracking multi-leg market-neutral portfolios."""

    position_id: str
    strategy_type: StrategyType
    status: HedgedPositionStatus = HedgedPositionStatus.OPEN
    leg_a: PositionLeg = Field(..., description="Long Spot or Asset A")
    leg_b: PositionLeg = Field(..., description="Short Perpetual or Asset B")
    net_delta_usd: Decimal = Field(default=Decimal("0"), description="Net directional exposure in USD")
    entry_spread_or_basis_bps: Decimal = Field(default=Decimal("0"), description="Entry basis or spread in bps")
    current_unrealized_pnl_quote: Decimal = Field(default=Decimal("0"))
    realized_funding_pnl_quote: Decimal = Field(default=Decimal("0"), description="Cumulative accrued funding cash flows")
    margin_ratio: Optional[Decimal] = Field(default=None, description="Current margin utilization ratio (maintenance/margin)")
    liquidation_price_leg_b: Optional[Decimal] = Field(default=None, description="Estimated liquidation price of perpetual leg")
    opened_at_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    closed_at_ms: Optional[int] = None

    @field_validator(
        "net_delta_usd", "entry_spread_or_basis_bps", "current_unrealized_pnl_quote",
        "realized_funding_pnl_quote", "margin_ratio", "liquidation_price_leg_b",
        mode="before"
    )
    @classmethod
    def parse_decimal(cls, v: Any) -> Any:
        if v is None or isinstance(v, Decimal):
            return v
        return Decimal(str(v))

    def is_delta_neutral(self, max_drift_pct: Decimal = Decimal("1.0")) -> bool:
        """Check if position's net delta is within allowable drift percentage (default 1.0%)."""
        notional_a = self.leg_a.quantity * self.leg_a.current_price
        if notional_a == Decimal("0"):
            return False
        notional_b = self.leg_b.quantity * self.leg_b.current_price
        drift = (abs(notional_a - notional_b) / notional_a) * Decimal("100")
        return drift <= max_drift_pct

    def liquidation_distance_pct(self, current_mark_price: Optional[Decimal] = None) -> Optional[Decimal]:
        """Compute percentage distance from current mark price to perpetual leg liquidation price."""
        if self.liquidation_price_leg_b is None:
            return None
        price = current_mark_price or self.leg_b.current_price
        if price == Decimal("0"):
            return None
        return (abs(price - self.liquidation_price_leg_b) / price) * Decimal("100")

    @property
    def total_pnl(self) -> Decimal:
        """Total realized + unrealized profit denominated in quote currency."""
        return self.current_unrealized_pnl_quote + self.realized_funding_pnl_quote
