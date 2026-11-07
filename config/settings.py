"""System configuration loader using Pydantic and PyYAML.

Loads environment variables, exchange definitions, and strategy parameters.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
import yaml
from pydantic import BaseModel, Field


CONFIG_DIR = Path(__file__).parent


class ExchangeConfig(BaseModel):
    exchange_id: str
    rest_url: str
    testnet_rest_url: Optional[str] = None
    ws_url: str
    testnet_ws_url: Optional[str] = None
    depth_stream_suffix: Optional[str] = None
    default_maker_fee_bps: Decimal = Decimal("10.0")
    default_taker_fee_bps: Decimal = Decimal("10.0")
    enable_bnb_discount: bool = False
    bnb_discount_maker_fee_bps: Decimal = Decimal("7.5")
    bnb_discount_taker_fee_bps: Decimal = Decimal("7.5")
    min_bnb_balance_for_discount: Decimal = Decimal("0.1")
    rate_limits: dict[str, Any] = Field(default_factory=dict)


class CrossExchangeStrategyConfig(BaseModel):
    name: str = "CROSS_EXCHANGE_SPOT"
    initial_pairs: list[str] = Field(
        default_factory=lambda: ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"]
    )
    min_net_edge_bps: Decimal = Decimal("15.0")
    max_notional_usdt: Decimal = Decimal("1000.0")
    unwind_cross_touch_bps: Decimal = Decimal("20.0")
    unwind_escalate_ms: int = 300
    max_unwind_slippage_bps: Decimal = Decimal("50.0")


class TriangularStrategyConfig(BaseModel):
    name: str = "TRIANGULAR_SPOT"
    initial_cycles: list[list[str]] = Field(
        default_factory=lambda: [
            ["USDT", "BTC", "ETH", "USDT"],
            ["USDT", "ETH", "BTC", "USDT"],
            ["USDT", "SOL", "BTC", "USDT"],
        ]
    )
    min_net_edge_bps: Decimal = Decimal("10.0")
    max_notional_usdt: Decimal = Decimal("1000.0")


class RiskLimitsConfig(BaseModel):
    max_daily_loss_usdt: Decimal = Decimal("100.0")
    max_open_plans: int = 3
    circuit_breaker_cooldown_sec: int = 300


class PersistenceConfig(BaseModel):
    critical_queue_maxsize: int = 0
    telemetry_queue_maxsize: int = 10000


class SystemSettings(BaseModel):
    """Unified system configuration."""

    execution_mode: str = "PAPER"
    live_trading_enabled: bool = False
    database_url: str = "postgresql+asyncpg://arb_user:arb_pass@localhost:5432/crypto_arb"

    # API Credentials
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_use_testnet: bool = True

    kraken_api_key: str = ""
    kraken_api_secret: str = ""

    # Latency & Feature Flags
    max_data_age_ms: int = 250
    enable_synthetic_fx_basis: bool = False

    # Sub-configurations loaded from YAML
    exchanges: dict[str, ExchangeConfig] = Field(default_factory=dict)
    strategy_a: CrossExchangeStrategyConfig = Field(default_factory=CrossExchangeStrategyConfig)
    strategy_b: TriangularStrategyConfig = Field(default_factory=TriangularStrategyConfig)
    risk_limits: RiskLimitsConfig = Field(default_factory=RiskLimitsConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> SystemSettings:
        """Load settings from environment variables and YAML files."""
        base_dir = config_path or CONFIG_DIR

        # Load environment variables
        exec_mode = os.getenv("EXECUTION_MODE", "PAPER")
        live_enabled = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
        db_url = os.getenv(
            "DATABASE_URL", "postgresql+asyncpg://arb_user:arb_pass@localhost:5432/crypto_arb"
        )
        b_key = os.getenv("BINANCE_API_KEY", "")
        b_sec = os.getenv("BINANCE_API_SECRET", "")
        b_testnet = os.getenv("BINANCE_USE_TESTNET", "true").lower() == "true"
        k_key = os.getenv("KRAKEN_API_KEY", "")
        k_sec = os.getenv("KRAKEN_API_SECRET", "")

        # Load exchanges.yaml
        exchanges_data: dict[str, ExchangeConfig] = {}
        exchanges_file = base_dir / "exchanges.yaml"
        if exchanges_file.exists():
            with open(exchanges_file, "r") as f:
                raw_exchanges = yaml.safe_load(f) or {}
                for k, v in raw_exchanges.items():
                    exchanges_data[k] = ExchangeConfig(**v)

        # Load strategy.yaml
        strategy_a = CrossExchangeStrategyConfig()
        strategy_b = TriangularStrategyConfig()
        risk_limits = RiskLimitsConfig()
        persistence = PersistenceConfig()
        max_data_age = 250
        enable_synthetic_fx = False

        strategy_file = base_dir / "strategy.yaml"
        if strategy_file.exists():
            with open(strategy_file, "r") as f:
                raw_strat = yaml.safe_load(f) or {}
                if "common" in raw_strat:
                    max_data_age = raw_strat["common"].get("max_data_age_ms", 250)
                    enable_synthetic_fx = raw_strat["common"].get("enable_synthetic_fx_basis", False)
                if "strategy_a_cross_exchange" in raw_strat:
                    strategy_a = CrossExchangeStrategyConfig(**raw_strat["strategy_a_cross_exchange"])
                if "strategy_b_triangular" in raw_strat:
                    strategy_b = TriangularStrategyConfig(**raw_strat["strategy_b_triangular"])
                if "risk_limits" in raw_strat:
                    risk_limits = RiskLimitsConfig(**raw_strat["risk_limits"])
                if "persistence" in raw_strat:
                    persistence = PersistenceConfig(**raw_strat["persistence"])

        return cls(
            execution_mode=exec_mode,
            live_trading_enabled=live_enabled,
            database_url=db_url,
            binance_api_key=b_key,
            binance_api_secret=b_sec,
            binance_use_testnet=b_testnet,
            kraken_api_key=k_key,
            kraken_api_secret=k_sec,
            max_data_age_ms=max_data_age,
            enable_synthetic_fx_basis=enable_synthetic_fx,
            exchanges=exchanges_data,
            strategy_a=strategy_a,
            strategy_b=strategy_b,
            risk_limits=risk_limits,
            persistence=persistence,
        )


settings = SystemSettings.load()
