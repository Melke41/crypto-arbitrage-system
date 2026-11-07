"""Market Data Collector.

Orchestrates concurrent WebSocket feeds for Binance and Kraken across the configured
trading universe and synchronizes L2 order books via OrderBookEngineImpl.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine, Optional

from src.core.events import OrderBookUpdateEvent
from src.core.models import ExchangeId, NormalizedOrderBook
from src.exchanges.binance import BinanceExchange
from src.exchanges.kraken import KrakenExchange
from src.market_data.normalizer import normalize_kraken_snapshot
from src.market_data.orderbook import OrderBookEngineImpl

logger = logging.getLogger(__name__)


class MarketDataCollector:
    """Coordinates Binance and Kraken market data feeds."""

    def __init__(
        self,
        symbols: list[str],
        binance_use_testnet: bool = True,
        max_data_age_ms: int = 250,
        on_book_update: Optional[Callable[[OrderBookUpdateEvent], Coroutine[Any, Any, None]]] = None,
    ) -> None:
        self.symbols = symbols
        self.max_data_age_ms = max_data_age_ms
        self.on_book_update = on_book_update

        self.orderbook_engine = OrderBookEngineImpl(max_data_age_ms=max_data_age_ms)

        self.binance = BinanceExchange(
            use_testnet=binance_use_testnet,
            on_message_callback=self._handle_binance_message,
        )
        self.kraken = KrakenExchange(
            on_message_callback=self._handle_kraken_message,
        )

        self._is_running = False

    async def _handle_binance_message(self, msg: dict[str, Any]) -> None:
        """Handle incoming Binance diff-depth message."""
        event_type = msg.get("e")
        if event_type == "depthUpdate":
            try:
                book = self.orderbook_engine.apply_binance_diff(msg)
                if not book.is_stale and self.on_book_update:
                    await self.on_book_update(OrderBookUpdateEvent(book=book))
            except Exception as e:
                logger.error("Error processing Binance depth update: %s", e)

    async def _handle_kraken_message(self, msg: dict[str, Any]) -> None:
        """Handle incoming Kraken v2 book message."""
        channel = msg.get("channel")
        msg_type = msg.get("type")

        if channel == "book":
            try:
                if msg_type == "snapshot":
                    snapshot = normalize_kraken_snapshot(msg)
                    if snapshot:
                        self.orderbook_engine.apply_snapshot(snapshot)
                        if self.on_book_update:
                            await self.on_book_update(OrderBookUpdateEvent(book=snapshot))
                elif msg_type == "update":
                    book = self.orderbook_engine.apply_kraken_update(msg)
                    if not book.is_stale and self.on_book_update:
                        await self.on_book_update(OrderBookUpdateEvent(book=book))
            except Exception as e:
                logger.error("Error processing Kraken book update: %s", e)

    async def start(self) -> None:
        """Hydrate order books with REST snapshots and start WebSocket streams."""
        self._is_running = True

        # 1. Subscribe symbols
        for symbol in self.symbols:
            await self.binance.subscribe_orderbook(symbol)
            await self.kraken.subscribe_orderbook(symbol)

        # 2. Start WebSocket listeners
        await self.binance.connect()
        await self.kraken.connect()

        # 3. Hydrate initial snapshots via REST in background
        asyncio.create_task(self._hydrate_snapshots())

    async def _hydrate_snapshots(self) -> None:
        """Fetch REST depth snapshots for all configured symbols."""
        for symbol in self.symbols:
            # Binance snapshot
            try:
                b_snap = await self.binance.fetch_orderbook_snapshot(symbol, limit=1000)
                self.orderbook_engine.apply_snapshot(b_snap)
                logger.info("Hydrated Binance snapshot for %s (lastUpdateId=%d)", symbol, b_snap.sequence_id)
            except Exception as e:
                logger.warning("Failed to fetch initial Binance snapshot for %s: %s", symbol, e)

            # Kraken snapshot
            try:
                k_snap = await self.kraken.fetch_orderbook_snapshot(symbol, limit=100)
                self.orderbook_engine.apply_snapshot(k_snap)
                logger.info("Hydrated Kraken snapshot for %s", symbol)
            except Exception as e:
                logger.warning("Failed to fetch initial Kraken snapshot for %s: %s", symbol, e)

    async def stop(self) -> None:
        """Gracefully disconnect all exchange feeds."""
        self._is_running = False
        await self.binance.disconnect()
        await self.kraken.disconnect()

    def get_orderbook(self, exchange: ExchangeId, symbol: str) -> Optional[NormalizedOrderBook]:
        """Query current verified in-memory order book."""
        return self.orderbook_engine.get_book(exchange, symbol)
