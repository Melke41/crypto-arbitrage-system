"""Abstract Base Exchange Adapter.

Provides standardized connection management, exponential backoff with jitter,
heartbeat tracking, and common REST/WebSocket lifecycle methods.
"""

from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, Optional

import aiohttp

from src.core.interfaces import ExchangeAdapter
from src.core.models import (
    BalanceRecord,
    ExchangeId,
    ExecutionLegState,
    ExecutionOrder,
    NormalizedOrderBook,
    SymbolMetadata,
)

logger = logging.getLogger(__name__)


class BaseExchange(ABC):
    """Abstract base class implementing ExchangeAdapter protocol."""

    def __init__(self, exchange_id: ExchangeId) -> None:
        self._exchange_id = exchange_id
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._ws_connection: Optional[Any] = None
        self._is_connected: bool = False
        self._subscribed_symbols: set[str] = set()

    @property
    def exchange_id(self) -> ExchangeId:
        """Return the unique exchange identifier."""
        return self._exchange_id

    @property
    def is_connected(self) -> bool:
        """Return whether WebSocket stream is currently active."""
        return self._is_connected

    async def get_http_session(self) -> aiohttp.ClientSession:
        """Get or lazily create aiohttp ClientSession."""
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(total=10.0, connect=3.0)
            self._http_session = aiohttp.ClientSession(timeout=timeout)
        return self._http_session

    async def backoff_sleep(self, attempt: int, base_delay: float = 0.5, max_delay: float = 10.0) -> None:
        """Exponential backoff with full jitter to avoid thundering herd."""
        delay = min(max_delay, base_delay * (2 ** attempt))
        jittered_delay = random.uniform(0.1, delay)
        await asyncio.sleep(jittered_delay)

    async def close_http_session(self) -> None:
        """Close HTTP session gracefully."""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

    @abstractmethod
    async def connect(self) -> None:
        """Establish WebSocket streams and initialize exchange sessions."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully disconnect WebSocket connections and clean up resources."""
        pass

    @abstractmethod
    async def subscribe_orderbook(self, symbol: str) -> None:
        """Subscribe to real-time L2 depth stream for a symbol."""
        pass

    @abstractmethod
    async def fetch_orderbook_snapshot(self, symbol: str, limit: int = 1000) -> NormalizedOrderBook:
        """Fetch REST depth snapshot for initial order book hydration."""
        pass

    @abstractmethod
    async def fetch_balances(self) -> dict[str, BalanceRecord]:
        """Fetch current account balances across all registered assets."""
        pass

    @abstractmethod
    async def submit_order(self, order: ExecutionOrder) -> ExecutionLegState:
        """Submit a single trade order (e.g. IOC limit or market) to exchange."""
        pass

    @abstractmethod
    async def cancel_order(self, symbol: str, exchange_order_id: str) -> bool:
        """Cancel an open order on the exchange."""
        pass

    @abstractmethod
    async def get_order_status(self, symbol: str, exchange_order_id: str) -> ExecutionLegState:
        """Query live execution status of an active or completed order."""
        pass

    @abstractmethod
    def get_symbol_metadata(self, symbol: str) -> Optional[SymbolMetadata]:
        """Retrieve cached filter limits and precision rules for a symbol."""
        pass
