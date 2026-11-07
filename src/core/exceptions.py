"""Domain exceptions for the Crypto Arbitrage Intelligence & Trading System.

All specialized exceptions inherit from ArbitrageSystemError to allow
unified catching at boundary layers while maintaining granular diagnosis.
"""

from typing import Any, Optional


class ArbitrageSystemError(Exception):
    """Base exception for all domain-specific errors in the system."""

    def __init__(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | Details: {self.details}"
        return self.message


# ==============================================================================
# Market Data & Order Book Exceptions
# ==============================================================================


class MarketDataError(ArbitrageSystemError):
    """Base exception for market data ingestion and normalization errors."""


class OrderBookError(MarketDataError):
    """Base exception for order book state management."""


class OrderBookDesyncError(OrderBookError):
    """Raised when sequence continuity fails (e.g., Binance U/u sequence gap)."""


class ChecksumMismatchError(OrderBookError):
    """Raised when CRC32 checksum verification fails (e.g., Kraken book drift)."""


class StaleDataError(MarketDataError):
    """Raised when an order book or price feed exceeds the maximum allowable latency age."""


# ==============================================================================
# Exchange & Transport Exceptions
# ==============================================================================


class ExchangeConnectionError(ArbitrageSystemError):
    """Raised on WebSocket or REST connection drop, failure, or handshake reject."""


class ExchangeRateLimitError(ArbitrageSystemError):
    """Raised when exchange rate limit / call counter is breached."""


class SymbolNotFoundError(ArbitrageSystemError):
    """Raised when a requested symbol is not registered in the exchange market registry."""


# ==============================================================================
# Risk Management Exceptions
# ==============================================================================


class RiskViolationError(ArbitrageSystemError):
    """Base exception for pre-trade or post-trade risk check rejections."""


class InsufficientBalanceError(RiskViolationError):
    """Raised when pre-funded inventory on an exchange is below executable order requirements."""


class PreTradeLimitBreachedError(RiskViolationError):
    """Raised when notional size, maximum slippage, or max open legs limit is violated."""


class CircuitBreakerTriggeredError(RiskViolationError):
    """Raised when systemic volatility or daily max drawdown halt is active."""


# ==============================================================================
# Execution & State Machine Exceptions
# ==============================================================================


class ExecutionError(ArbitrageSystemError):
    """Base exception for multi-leg trade execution failures."""


class LeggingRiskError(ExecutionError):
    """Raised when Leg 1 fills but subsequent legs fail, creating unhedged inventory risk."""


class EmergencyUnwindError(ExecutionError):
    """Raised when the automated unwinding/hedging state machine fails to flatten exposure."""


class OrderTimeoutError(ExecutionError):
    """Raised when an IOC or market order submission does not receive an exchange ack within tolerance."""


# ==============================================================================
# Configuration & Infrastructure Exceptions
# ==============================================================================


class ConfigurationError(ArbitrageSystemError):
    """Raised when environment variables, exchange credentials, or strategy configs are invalid."""


class PersistenceError(ArbitrageSystemError):
    """Raised when the asynchronous database write-behind buffer or worker encounters an error."""
