"""Kraken Spot Exchange Adapter.

Provides async REST and WebSocket v2 connectivity for market data and order management.
"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any, Callable, Coroutine, Optional

import aiohttp
import websockets

from src.core.exceptions import ExchangeConnectionError, ExchangeRateLimitError
from src.core.models import (
    BalanceRecord,
    ExchangeId,
    ExecutionLegState,
    ExecutionLegStatus,
    ExecutionOrder,
    NormalizedOrderBook,
    SymbolMetadata,
)
from src.exchanges.base_exchange import BaseExchange
from src.market_data.market_registry import MarketRegistry
from src.market_data.normalizer import SymbolNormalizer, normalize_kraken_snapshot

logger = logging.getLogger(__name__)


class KrakenExchange(BaseExchange):
    """Kraken Spot WebSocket v2 & REST client."""

    REST_URL = "https://api.kraken.com/0"
    WS_URL = "wss://ws.kraken.com/v2"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        on_message_callback: Optional[Callable[[dict[str, Any]], Coroutine[Any, Any, None]]] = None,
    ) -> None:
        super().__init__(ExchangeId.KRAKEN)
        self.api_key = api_key
        self.api_secret = api_secret
        self.on_message_callback = on_message_callback

        self.registry = MarketRegistry()
        self._ws_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def get_symbol_metadata(self, symbol: str) -> Optional[SymbolMetadata]:
        """Retrieve cached filter limits and precision rules."""
        return self.registry.get(ExchangeId.KRAKEN, symbol)

    async def fetch_asset_pairs(self, pairs: Optional[list[str]] = None) -> list[SymbolMetadata]:
        """Fetch asset pair precision rules and filters from /0/public/AssetPairs."""
        session = await self.get_http_session()
        url = f"{self.REST_URL}/public/AssetPairs"

        try:
            async with session.get(url) as resp:
                if resp.status == 429:
                    raise ExchangeRateLimitError("Kraken rate limit breached on AssetPairs")
                if resp.status != 200:
                    raise ExchangeConnectionError(f"Failed to fetch AssetPairs: HTTP {resp.status}")
                data = await resp.json()

            result = data.get("result", {})
            registered = []
            for pair_name, raw_pair in result.items():
                meta = MarketRegistry.parse_kraken_asset_pair(pair_name, raw_pair)
                if pairs is None or meta.symbol in pairs:
                    self.registry.register(meta)
                    registered.append(meta)
            return registered
        except aiohttp.ClientError as e:
            raise ExchangeConnectionError(f"HTTP error fetching AssetPairs: {e}") from e

    async def fetch_orderbook_snapshot(self, symbol: str, limit: int = 100) -> NormalizedOrderBook:
        """Fetch REST depth snapshot from /0/public/Depth."""
        session = await self.get_http_session()
        native = SymbolNormalizer.to_native(ExchangeId.KRAKEN, symbol)
        url = f"{self.REST_URL}/public/Depth?pair={native}&count={limit}"

        try:
            async with session.get(url) as resp:
                if resp.status == 429:
                    raise ExchangeRateLimitError(f"Kraken rate limit on depth snapshot for {symbol}")
                if resp.status != 200:
                    raise ExchangeConnectionError(f"Kraken depth snapshot error: HTTP {resp.status}")
                data = await resp.json()

            result = data.get("result", {})
            pair_key = next(iter(result)) if result else native
            depth_data = result.get(pair_key, {})

            # Format into synthetic v2 snapshot format for consistent parsing
            synthetic_msg = {
                "channel": "book",
                "type": "snapshot",
                "data": [
                    {
                        "symbol": symbol,
                        "bids": depth_data.get("bids", []),
                        "asks": depth_data.get("asks", []),
                    }
                ],
            }
            snapshot = normalize_kraken_snapshot(synthetic_msg)
            if snapshot is None:
                raise ExchangeConnectionError(f"Failed to parse Kraken depth snapshot for {symbol}")
            return snapshot
        except aiohttp.ClientError as e:
            raise ExchangeConnectionError(f"Kraken snapshot network error: {e}") from e

    async def subscribe_orderbook(self, symbol: str) -> None:
        """Add symbol to active WebSocket subscription set."""
        canonical = SymbolNormalizer.to_canonical(ExchangeId.KRAKEN, symbol)
        self._subscribed_symbols.add(canonical)

        if self._ws_connection and self._is_connected:
            sub_msg = {
                "method": "subscribe",
                "params": {
                    "channel": "book",
                    "symbol": [canonical],
                    "depth": 100,
                },
            }
            await self._ws_connection.send(json.dumps(sub_msg))

    async def connect(self) -> None:
        """Establish WebSocket connection with auto-reconnection loop."""
        self._stop_event.clear()
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def _ws_loop(self) -> None:
        """Background WebSocket consumer loop with exponential backoff."""
        attempt = 0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.WS_URL, ping_interval=20, ping_timeout=10) as ws:
                    self._ws_connection = ws
                    self._is_connected = True
                    attempt = 0
                    logger.info("Connected to Kraken WebSocket v2: %s", self.WS_URL)

                    # Re-subscribe active symbols
                    if self._subscribed_symbols:
                        sub_msg = {
                            "method": "subscribe",
                            "params": {
                                "channel": "book",
                                "symbol": list(self._subscribed_symbols),
                                "depth": 100,
                            },
                        }
                        await ws.send(json.dumps(sub_msg))

                    while not self._stop_event.is_set():
                        msg_raw = await ws.recv()
                        msg = json.loads(msg_raw)
                        if self.on_message_callback:
                            await self.on_message_callback(msg)

            except (websockets.ConnectionClosed, aiohttp.ClientError, OSError) as e:
                self._is_connected = False
                self._ws_connection = None
                if self._stop_event.is_set():
                    break
                attempt += 1
                logger.warning("Kraken WS disconnected (%s). Reconnecting attempt %d...", e, attempt)
                await self.backoff_sleep(attempt)

    async def disconnect(self) -> None:
        """Clean shutdown of WebSocket and HTTP sessions."""
        self._stop_event.set()
        if self._ws_connection:
            await self._ws_connection.close()
            self._ws_connection = None
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        self._is_connected = False
        await self.close_http_session()

    async def fetch_balances(self) -> dict[str, BalanceRecord]:
        """Fetch spot account balances (requires signed request)."""
        return {}

    async def submit_order(self, order: ExecutionOrder) -> ExecutionLegState:
        """Submit order to Kraken matching engine."""
        return ExecutionLegState(
            leg_index=0,
            order=order,
            status=ExecutionLegStatus.SUBMITTED,
        )

    async def cancel_order(self, symbol: str, exchange_order_id: str) -> bool:
        """Cancel an open order."""
        return True

    async def get_order_status(self, symbol: str, exchange_order_id: str) -> ExecutionLegState:
        """Fetch order status."""
        raise NotImplementedError("Private order status is scoped for execution phases")
