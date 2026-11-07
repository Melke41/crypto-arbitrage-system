# Development Status & Multi-Agent Task Tracker

## 1. Project Phase Matrix

| Phase | Milestone | Assigned Agent | Branch | Status | Acceptance Gate |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Phase 0** | **Architecture, Specification & Core Contracts** | **Lead Agent** | `phase0-arch-spec` | **COMPLETED & MERGED** | 11/11 Core Unit Tests Passing (0 warnings) |
| **Phase 0.1** | **4-Strategy Core Architecture Expansion** | **Lead Agent** | `phase0-v2-four-strategies` | **COMPLETED & MERGED** | All 4 Strategies Domain Contracts Tested |
| **Phase 1** | **Exchange Connectivity & 4-Strategy Data Engine** | Agent 1 (Data) | `agent1/phase1-connectivity` | **COMPLETED & MERGED** | Spot + Perp WS feeds & TimeSeriesBuffer |
| **Phase 2 (Market Data)** | **Verified L2 Book & Mark-Price Engine** | Agent 1 (Data) | `agent1/phase1-connectivity` | **COMPLETED & MERGED** | U/u sequence tracker & Kraken CRC32 verified |
| **Phase 2 (Persistence)** | **L2 Snapshot Database Partitioning** | Agent 3 (Risk/Exec) | `agent3/phase7-persistence` | PENDING | Scoped with Phase 7 async persistence worker |
| **Phase 3** | **4 Quantitative Strategy Engines & Opportunity Evaluator** | Agent 2 (Quant) | `agent2/phase3-strategies` | **COMPLETED & MERGED** | All 4 Strategies & Evaluator Tested (37 passing) |
| **Phase 4** | **Pre-Trade Risk & Inventory Balancer** | Agent 3 (Risk/Exec) | `agent3/phase4-risk` | **IN_PROGRESS** | 100% of out-of-bounds orders rejected |
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

---

## 3. Phase 3 Work Package: Agent 2 (Quantitative Strategy Engineer) — COMPLETED

### Completed Deliverables
1. `src/opportunity/fee_calculator.py`: VIP 0 vs. BNB fee calculation, base vs quote fee deductions, round-trip fee compounding.
2. `src/opportunity/profitability.py`: Multi-level VWAP depth walk, lot floor truncation, and net edge calculation for all 4 strategies (Cross-Exchange, Triangular, Funding/Basis Carry, and Cointegrated Pairs StatArb).
3. `src/opportunity/evaluator.py`: Freshness validation (< 250ms), crossed book detection, lifecycle state gating (`THEORETICAL_OPPORTUNITY` vs `EXECUTABLE_OPPORTUNITY`), and event dispatch.
4. `src/strategies/base_strategy.py`: Abstract `StrategyEngine` base class.
5. `src/strategies/cross_exchange.py`: Strategy 1 (Binance vs Kraken Spot in both directions).
6. `src/strategies/triangular.py`: Strategy 2 (Single-exchange 3-leg pre-computed cycles indexed by symbol).
7. `src/strategies/funding_basis.py`: Strategy 3 (Delta-neutral Spot + Short Perp carry, basis tracking, break-even holding period, and exit conditions).
8. `src/strategies/stat_arb.py`: Strategy 4 (Cointegrated pairs mean-reversion, rolling Z-score triggers $|Z| \ge 2.2$, exit $|Z| \le 0.4$, stop loss $|Z| \ge 4.0$).
9. `tests/unit/test_opportunity.py` and `tests/unit/test_strategies.py`: 37 passing unit & integration tests.

---

## 4. Phase 4–7 Work Package: Agent 3 (Risk, Execution, Portfolio & Persistence Engineer)

### Scope & Mission
Implement institutional pre-trade risk controls, portfolio & balance management (including derivatives positions and delta-neutral monitoring), paper/testnet execution state machines with automated IOC unwind, and high-throughput dual-queue asynchronous persistence.

### Assigned Branch & File Locks
- **Branch:** `agent3/phase4-risk`
- **Allowed Write Directories:**
  - `src/risk/*`
  - `src/portfolio/*`
  - `src/execution/*`
  - `src/persistence/*`
  - `tests/unit/test_risk.py`
  - `tests/unit/test_portfolio.py`
  - `tests/unit/test_execution.py`
  - `tests/unit/test_persistence.py`
- **Strictly Read-Only:**
  - `src/core/*` (Do NOT touch `models.py`, `interfaces.py`, `events.py`, or `exceptions.py` without Lead authorization)
  - `src/market_data/*`
  - `src/strategies/*`
  - `src/opportunity/*`
  - `config/*`

