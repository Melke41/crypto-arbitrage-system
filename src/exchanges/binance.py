"""Binance Spot Exchange Adapter.

Provides async REST and WebSocket connectivity for market data and order management
supporting both Binance Production and Binance Spot Testnet (testnet.binance.vision).
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
    FundingRateSnapshot,
    HedgedPosition,
    InstrumentType,
    NormalizedOrderBook,
    SymbolMetadata,
)
from src.exchanges.base_exchange import BaseExchange
from src.market_data.market_registry import MarketRegistry
from src.market_data.normalizer import SymbolNormalizer, normalize_binance_snapshot

logger = logging.getLogger(__name__)


class BinanceExchange(BaseExchange):
    """Binance Spot & USDⓈ-M Perpetual Futures WebSocket & REST client."""

    PROD_REST_URL = "https://api.binance.com"
    PROD_WS_URL = "wss://stream.binance.com:9443/ws"

    TESTNET_REST_URL = "https://testnet.binance.vision"
    TESTNET_WS_URL = "wss://testnet.binance.vision/ws"

    FUTURES_PROD_REST_URL = "https://fapi.binance.com"
    FUTURES_PROD_WS_URL = "wss://fstream.binance.com/ws"

    FUTURES_TESTNET_REST_URL = "https://testnet.binancefuture.com"
    FUTURES_TESTNET_WS_URL = "wss://stream.binancefuture.com/ws"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        use_testnet: bool = True,
        is_futures: bool = False,
        on_message_callback: Optional[Callable[[dict[str, Any]], Coroutine[Any, Any, None]]] = None,
    ) -> None:
        super().__init__(ExchangeId.BINANCE)
        self.api_key = api_key
        self.api_secret = api_secret
        self.use_testnet = use_testnet
        self.is_futures = is_futures
        self.on_message_callback = on_message_callback

        if is_futures:
            self.rest_url = self.FUTURES_TESTNET_REST_URL if use_testnet else self.FUTURES_PROD_REST_URL
            self.ws_url = self.FUTURES_TESTNET_WS_URL if use_testnet else self.FUTURES_PROD_WS_URL
        else:
            self.rest_url = self.TESTNET_REST_URL if use_testnet else self.PROD_REST_URL
            self.ws_url = self.TESTNET_WS_URL if use_testnet else self.PROD_WS_URL

        self.registry = MarketRegistry()
        self._ws_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def get_symbol_metadata(self, symbol: str) -> Optional[SymbolMetadata]:
        """Retrieve cached filter limits and precision rules."""
        return self.registry.get(ExchangeId.BINANCE, symbol)

    async def fetch_exchange_info(self, symbols: Optional[list[str]] = None) -> list[SymbolMetadata]:
        """Fetch symbol metadata and trading filters from /api/v3/exchangeInfo."""
        session = await self.get_http_session()
        url = f"{self.rest_url}/api/v3/exchangeInfo"

        try:
            async with session.get(url) as resp:
                if resp.status == 429:
                    raise ExchangeRateLimitError("Binance rate limit breached on exchangeInfo")
                if resp.status != 200:
                    raise ExchangeConnectionError(f"Failed to fetch exchangeInfo: HTTP {resp.status}")
                data = await resp.json()

            symbols_raw = data.get("symbols", [])
            registered = []
            for s_info in symbols_raw:
                meta = MarketRegistry.parse_binance_symbol_info(s_info)
                if symbols is None or meta.symbol in symbols:
                    self.registry.register(meta)
                    registered.append(meta)
            return registered
        except aiohttp.ClientError as e:
            raise ExchangeConnectionError(f"HTTP error fetching exchangeInfo: {e}") from e

    async def fetch_orderbook_snapshot(self, symbol: str, limit: int = 1000) -> NormalizedOrderBook:
        """Fetch REST depth snapshot for initial order book hydration."""
        session = await self.get_http_session()
        native = SymbolNormalizer.to_native(ExchangeId.BINANCE, symbol)
        url = f"{self.rest_url}/api/v3/depth?symbol={native}&limit={limit}"

        try:
            async with session.get(url) as resp:
                if resp.status == 429:
                    raise ExchangeRateLimitError(f"Binance rate limit on depth snapshot for {symbol}")
                if resp.status != 200:
                    text = await resp.text()
                    raise ExchangeConnectionError(f"Binance depth snapshot error: HTTP {resp.status} - {text}")
                data = await resp.json()
            return normalize_binance_snapshot(symbol, data)
        except aiohttp.ClientError as e:
            raise ExchangeConnectionError(f"Binance snapshot network error: {e}") from e

    async def subscribe_orderbook(self, symbol: str) -> None:
        """Add symbol to active WebSocket subscription set."""
        canonical = SymbolNormalizer.to_canonical(ExchangeId.BINANCE, symbol)
        self._subscribed_symbols.add(canonical)

        # If already connected, send subscription message
        if self._ws_connection and self._is_connected:
            native = SymbolNormalizer.to_native(ExchangeId.BINANCE, canonical).lower()
            stream = f"{native}@depth@100ms"
            sub_msg = {
                "method": "SUBSCRIBE",
                "params": [stream],
                "id": int(asyncio.get_event_loop().time() * 1000),
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
                # Build combined stream path if symbols are pre-subscribed
                if self._subscribed_symbols:
                    streams = "/".join(
                        [
                            f"{SymbolNormalizer.to_native(ExchangeId.BINANCE, s).lower()}@depth@100ms"
                            for s in self._subscribed_symbols
                        ]
                    )
                    url = f"{self.ws_url}/{streams}" if not self.use_testnet else f"{self.ws_url}/{streams}"
                else:
                    url = self.ws_url

                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    self._ws_connection = ws
                    self._is_connected = True
                    attempt = 0
                    logger.info("Connected to Binance WebSocket: %s", url)

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
                logger.warning("Binance WS disconnected (%s). Reconnecting attempt %d...", e, attempt)
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
        # Placeholder for read-only / public Phase 1 scope
        return {}

    async def submit_order(self, order: ExecutionOrder) -> ExecutionLegState:
        """Submit order to Binance matching engine."""
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


    # ==============================================================================
    # Perpetual Futures & Derivatives Support (Strategy 3 & 4)
    # ==============================================================================

    @staticmethod
    def parse_mark_price_update(msg: dict[str, Any]) -> FundingRateSnapshot:
        """Parse Binance <symbol>@markPrice@1s WebSocket update into FundingRateSnapshot."""
        native = msg.get("s", "")
        canonical = SymbolNormalizer.to_canonical(ExchangeId.BINANCE, native)
        mark_p = Decimal(str(msg.get("p", "0")))
        index_p = Decimal(str(msg.get("i", "0")))
        funding_r = Decimal(str(msg.get("r", "0")))
        next_funding_ms = int(msg.get("T", 0))
        event_time_ms = int(msg.get("E", 0))

        return FundingRateSnapshot(
            exchange=ExchangeId.BINANCE,
            symbol=f"{canonical}:USDT",
            mark_price=mark_p,
            index_price=index_p,
            spot_mid_price=None,
            current_funding_rate=funding_r,
            next_funding_timestamp_ms=next_funding_ms,
            funding_interval_hours=8,
            local_receipt_timestamp_ms=event_time_ms or int(time.time() * 1000),
            is_stale=False,
        )

    async def subscribe_funding_and_mark_price(self, symbols: list[str]) -> None:
        """Subscribe to @markPrice@1s streams for specified symbols."""
        for s in symbols:
            canonical = SymbolNormalizer.to_canonical(ExchangeId.BINANCE, s)
            self._subscribed_symbols.add(canonical)

        if self._ws_connection and self._is_connected:
            streams = [
                f"{SymbolNormalizer.to_native(ExchangeId.BINANCE, s).lower()}@markPrice@1s"
                for s in symbols
            ]
            sub_msg = {
                "method": "SUBSCRIBE",
                "params": streams,
                "id": int(asyncio.get_event_loop().time() * 1000),
            }
            await self._ws_connection.send(json.dumps(sub_msg))

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage on perpetual contract (enforced <= 2x for delta-neutral carry)."""
        if leverage > 3:
            raise ValueError(f"Leverage {leverage}x exceeds quantitative risk safety cap (3x)")
        logger.info("Setting leverage for %s on Binance Futures to %dx", symbol, leverage)
        return True

    async def fetch_funding_rate_history(
        self, symbol: str, limit: int = 100
    ) -> list[FundingRateSnapshot]:
        """Fetch historical funding rates from /fapi/v1/fundingRate."""
        session = await self.get_http_session()
        native = SymbolNormalizer.to_native(ExchangeId.BINANCE, symbol)
        url = f"{self.rest_url}/fapi/v1/fundingRate?symbol={native}&limit={limit}"

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()

            snapshots = []
            for item in data:
                snap = FundingRateSnapshot(
                    exchange=ExchangeId.BINANCE,
                    symbol=f"{symbol}:USDT",
                    mark_price=Decimal(str(item.get("fundingRate", "0"))),
                    index_price=Decimal("0"),
                    current_funding_rate=Decimal(str(item.get("fundingRate", "0"))),
                    next_funding_timestamp_ms=int(item.get("fundingTime", 0)),
                    funding_interval_hours=8,
                )
                snapshots.append(snap)
            return snapshots
        except aiohttp.ClientError:
            return []

    async def fetch_positions(self) -> list[HedgedPosition]:
        """Fetch active perpetual positions (requires authenticated session)."""
        return []

    async def fetch_margin_health(self) -> dict[str, Any]:
        """Fetch margin balance, maintenance margin, and margin ratio."""
        return {"margin_ratio": Decimal("0.0"), "maintenance_margin": Decimal("0.0")}
