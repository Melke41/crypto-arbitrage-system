# Development Status & Multi-Agent Task Tracker

## 1. Project Phase Matrix

| Phase | Milestone | Assigned Agent | Branch | Status | Acceptance Gate |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Phase 0** | **Architecture, Specification & Core Contracts** | **Lead Agent** | `phase0-arch-spec` | **COMPLETED & MERGED** | 11/11 Core Unit Tests Passing (0 warnings) |
| **Phase 1** | **Exchange Connectivity & Normalization** | Agent 1 (Data) | `agent1/phase1-connectivity` | **IN_PROGRESS** | Pending |
| **Phase 2** | **Verified L2 Order Book Engine** | Agent 1 (Data) | `agent1/phase2-market-data` | PENDING | Zero desync in 10-min Binance/Kraken diff playback |
| **Phase 3** | **Strategy Engines (Cross & Triangular)** | Agent 2 (Quant) | `agent2/phase3-strategies` | PENDING | VWAP depth walker & directed cycle unit tests |
| **Phase 4** | **Pre-Trade Risk & Inventory Balancer** | Agent 3 (Risk/Exec) | `agent3/phase4-risk` | PENDING | 100% of out-of-bounds orders rejected |
| **Phase 5** | **Paper & Testnet Execution Engine** | Agent 3 (Risk/Exec) | `agent3/phase5-paper` | PENDING | Simulated latency decay & testnet fill verification |
| **Phase 6** | **Multi-Leg State Machine & Unwind Engine** | Agent 3 (Risk/Exec) | `agent3/phase6-execution` | PENDING | Automated unwind flattens position on simulated leg failure |
| **Phase 7** | **Non-Blocking Persistence & Async DB Writer** | Agent 3 (Risk/Exec) | `agent3/phase7-persistence` | PENDING | Ingest 10k events/sec with zero event loop lag |
| **Phase 8** | **Quantitative Analytics & Performance** | Agent 4 (QA/Val) | `agent4/phase8-analytics` | PENDING | Slippage attribution & PnL reporting automated |
| **Phase 9** | **Operational Dashboard & Hardening** | Agent 4 (QA/Val) | `agent4/phase9-dashboard` | PENDING | 2-hour end-to-end stress run with zero crashes |

---

## 2. Phase 1 Work Package: Agent 1 (Exchange & Market Data Engineer)

### Scope & Mission
Implement asynchronous WebSocket streams and REST adapters for Binance Spot and Kraken Spot, adhering strictly to the `ExchangeAdapter` protocol defined in `src/core/interfaces.py`.

### Assigned Branch & File Locks
- **Branch:** `agent1/phase1-connectivity` (branched from `main` after Phase 0 sign-off)
- **Allowed Write Directories:**
  - `src/exchanges/base_exchange.py`
  - `src/exchanges/binance.py`
  - `src/exchanges/kraken.py`
  - `src/market_data/normalizer.py`
  - `src/market_data/market_registry.py`
  - `tests/unit/test_exchanges_binance.py`
  - `tests/unit/test_exchanges_kraken.py`
  - `tests/unit/test_market_registry.py`
- **Strictly Read-Only:**
  - `src/core/*` (Do NOT touch `models.py`, `interfaces.py`, `events.py`, or `exceptions.py`)
  - `config/*`
  - `docs/*`

### Required Implementation Deliverables
1. `src/exchanges/base_exchange.py`: Abstract base class implementing common reconnection loops, exponential backoff, and heartbeat tracking.
2. `src/exchanges/binance.py`:
   - Async WebSocket connection to `wss://stream.binance.com:9443/ws` (and testnet `wss://testnet.binance.vision/ws`).
   - Subscription management for `<symbol>@depth@100ms`.
   - REST client fetching `GET /api/v3/depth` and `GET /api/v3/exchangeInfo`.
3. `src/exchanges/kraken.py`:
   - Async WebSocket connection to `wss://ws.kraken.com/v2`.
   - Subscription management for `book` channel with `depth: 100`.
   - REST client fetching asset pairs and snapshot depths.
4. `src/market_data/market_registry.py`:
   - Cache `SymbolMetadata` (tickSize, lotSize, minNotional, taker/maker fee rates) for active symbols.
5. `src/market_data/normalizer.py`:
   - Normalize Binance raw depth JSON and Kraken v2 book JSON into standard `OrderBookLevel` and `NormalizedOrderBook`.

### Pytest Acceptance Gates
- `tests/unit/test_exchanges_binance.py`: Verifies mock connection, subscription, ping/pong, and error handling.
- `tests/unit/test_exchanges_kraken.py`: Verifies mock v2 book subscription and message parsing.
- `tests/unit/test_market_registry.py`: Verifies ticker normalization and metadata retrieval.
- All tests must pass with 100% type safety and zero unhandled exceptions.
