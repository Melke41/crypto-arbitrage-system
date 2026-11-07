# Institutional Crypto Arbitrage Intelligence & Autonomous Trading System

An institutional-grade, event-driven, modular quantitative arbitrage research and execution engine in Python integrating **Binance Spot**, **Binance USDⓈ-M Perpetual Futures**, and **Kraken Spot**.

[![Release](https://img.shields.io/badge/release-v1.0.0-blue.svg)](https://github.com/Melke41/crypto-arbitrage-system)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.14-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-77%20passed%20%7C%201%20skipped-brightgreen.svg)]()

---

## 1. Architecture & Strategy Overview

The system runs on an event-driven, tick-to-trade pipeline engineered for ultra-low latency, deterministic execution, and zero floating-point rounding errors (`Decimal` arithmetic strictly enforced across all balances, orders, and pricing).

```
                      +---------------------------------------+
                      |       Exchange Market Data Feeds       |
                      |   Binance Spot / Perps & Kraken Spot   |
                      +-------------------+-------------------+
                                          | WebSocket / REST
                                          v
                      +---------------------------------------+
                      |       Market Data & Registry Engine   |
                      |   L2 Sequence Sync / Kraken CRC32     |
                      +-------------------+-------------------+
                                          | Normalized Order Books
                                          v
   +-------------------------------------------------------------------------+
   |                        4 Quantitative Strategy Engines                  |
   |                                                                         |
   |  [1] Cross-Exchange Spot     [2] Single-Exchange Triangular             |
   |  (Binance vs Kraken Spot)    (Binance 3-Leg Sequential Cycles)          |
   |                                                                         |
   |  [3] Delta-Neutral Carry     [4] Statistical Arbitrage Pairs            |
   |  (Spot Buy + Perp Short)     (Cointegration β, Rolling Z-Score)         |
   +--------------------------------------+----------------------------------+
                                          | Raw Opportunities
                                          v
                      +---------------------------------------+
                      |     Profitability & Evaluator Gate    |
                      |   L2 Depth Walk, Base/Quote Fees,     |
                      |   Freshness (<250ms), Crossed Books   |
                      +-------------------+-------------------+
                                          | EXECUTABLE_OPPORTUNITY
                                          v
                      +---------------------------------------+
                      |     Central Pre-Trade Risk Manager    |
                      |   8 Institutional Pre-Trade Gates     |
                      |   Kill Switch, Drawdown & Loss Limits |
                      +-------------------+-------------------+
                                          | Approved Execution Plans
                                          v
                      +---------------------------------------+
                      |       Multi-Leg Execution Engine      |
                      |   Simultaneous / Sequential Runners   |
                      |   Directional ±20 bps IOC Unwind      |
                      |   50 bps Slippage Circuit Breaker     |
                      +---------+-------------------+---------+
                                |                   |
             Telemetry & Events |                   | Critical Records
                                v                   v
    +-----------------------------------------------------------------------+
    |               Non-Blocking Dual-Queue Persistence Engine              |
    |   Unbounded Critical Queue (Orders/Plans) | Droppable Telemetry Queue |
    |                   PostgreSQL / SQLite Storage Engine                  |
    +-----------------------------------------------------------------------+
```

### The 4 Supported Quantitative Strategies

1. **Strategy 1: Cross-Exchange Spot Arbitrage (`src/strategies/cross_exchange.py`)**
   - **Model:** Pre-funded inventory model (zero cross-exchange transfer assumption during execution).
   - **Mechanics:** Simultaneously buys on the lower-ask exchange and sells on the higher-bid exchange (`Binance` vs. `Kraken`).
   - **Pricing:** Multi-level L2 depth walking computes volume-weighted average price (VWAP) across order books.
   - **Fees:** Deducts exact maker/taker fee schedules including VIP tiers and Binance base-asset fee deduction adjustments.

2. **Strategy 2: Single-Exchange Triangular Arbitrage (`src/strategies/triangular.py`)**
   - **Model:** Intra-exchange sequential synthetic triangular cycles on Binance (e.g., `USDT` $\rightarrow$ `BTC` $\rightarrow$ `ETH` $\rightarrow$ `USDT`).
   - **Mechanics:** Sequential chaining where lot-truncated output quantity of Leg $N$ feeds into Leg $N+1$.
   - **Inventory:** Zero cross-currency inventory risk; begins in quote currency (`USDT`) and finishes in `USDT` within milliseconds.

3. **Strategy 3: Delta-Neutral Funding-Rate & Basis Carry (`src/strategies/funding_basis.py`)**
   - **Model:** Long Spot + Short USDⓈ-M Perpetual Futures.
   - **Mechanics:** Captures positive 8-hour funding payments while hedging out underlying directional price risk.
   - **Basis Convergence:** Evaluates annualized net carry yields, entry basis divergence, and break-even holding periods; exits automatically when basis converges or funding turns adverse.

4. **Strategy 4: Statistical Arbitrage Cointegrated Pairs (`src/strategies/stat_arb.py`)**
   - **Model:** Engle-Granger / OLS cointegrated pairs spread modeling (e.g., `ETH/USDT` vs. `BTC/USDT`).
   - **Mechanics:** Maintains rolling time-series ring buffers, dynamic Ornstein-Uhlenbeck half-life estimates, and rolling spread Z-scores.
   - **Signals:** Enters mean-reversion positions at $|Z| \ge 2.2$, exits at $|Z| \le 0.4$, and enforces hard stop-loss exits at $|Z| \ge 4.0$.

---

## 2. Environment Setup

### Prerequisites
- Python 3.11+ (Tested on Python 3.11, 3.12, 3.14)
- PostgreSQL (Production) or SQLite (Default / Local Testing)
- Git

### Installation
```bash
# 1. Clone repository
git clone https://github.com/Melke41/crypto-arbitrage-system.git
cd cypto-arbitrage-system

# 2. Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env
```

### Environment Configuration (`.env`)
```ini
# Execution Mode: PAPER (default), TESTNET, or LIVE
EXECUTION_MODE=PAPER

# API Keys & Endpoints
BINANCE_API_KEY=your_binance_key
BINANCE_API_SECRET=your_binance_secret
KRAKEN_API_KEY=your_kraken_key
KRAKEN_API_SECRET=your_kraken_secret

# Database Configuration (PostgreSQL in production; SQLite supported)
DATABASE_URL=sqlite+aiosqlite:///arbitrage_system.db

# Pre-Trade Risk Limits (Exact Decimals)
MAX_NOTIONAL_PER_TRADE_USDT=500.0
MAX_DAILY_LOSS_USDT=100.0
MAX_DRAWDOWN_PCT=5.0
MAX_CONCURRENT_PLANS=3
MAX_DATA_AGE_MS=250

# Fast API Control Server
API_HOST=127.0.0.1
API_PORT=8000
API_SECRET_KEY=generate_a_secure_token_here
```

---

## 3. Operator Runbook: Running the System

### Stage 1: Run Full Test & Chaos Simulation Suite
Before starting any live or paper processes, verify all 77 automated unit, integration, and failure scenario tests pass:

```bash
.venv/bin/pytest -v
```

The test suite validates:
- **Unit Contracts:** Domain models, lot size truncation, L2 sequence synchronization, Kraken CRC32 checksums, and fee calculators.
- **Pre-Trade Risk:** 8 sequential pre-trade gates, balance locking, and daily loss breakers.
- **Execution State Machine:** Simultaneous/sequential order execution and directional ±20 bps emergency unwinding.
- **Multi-Strategy Simulations:** End-to-end execution across all 4 quantitative engines.
- **Chaos Scenarios:** Unprofitable spread traps, stale book rejections, partial leg drops, catastrophic unwind slippage, and automated kill-switch triggers.

---

### Stage 2: Launch Autonomous Engine & Control Dashboard

Open two terminal tabs:

#### Terminal 1: Autonomous Trading Pipeline & FastAPI Control Server
```bash
.venv/bin/python -m src.main
```
*Initializes market registry, balance manager, WebSocket adapters, 4 strategy engines, risk manager, and launches the FastAPI control API at `http://127.0.0.1:8000`.*

Key endpoints:
- `GET /health`: Engine status, uptime, active execution mode.
- `GET /portfolio`: Real-time free/locked balances and open positions across Binance and Kraken.
- `GET /opportunities`: Live opportunity stream with gross/net edge in bps and lifecycle state.
- `GET /executions`: Chronological execution log with fill prices, slippage, and realized PnL.
- `POST /risk/kill-switch`: Instant manual or automated trading halt.

#### Terminal 2: Streamlit Real-Time Monitoring & Kill-Switch UI
```bash
.venv/bin/streamlit run dashboard/app.py
```
*Launches the institutional web dashboard at `http://localhost:8501` featuring real-time telemetry, portfolio distribution, opportunity funnel, execution logs, and an emergency Kill Switch trigger.*

---

## 4. Promotion Protocol: PAPER $\rightarrow$ TESTNET $\rightarrow$ LIVE

The system employs a strict 3-tier promotion progression to ensure quantitative edge, execution safety, and zero unhedged exposure.

```
+--------------------+      48-72h Burn-In      +--------------------+      Validation Pass      +--------------------+
|     PAPER MODE     |  =====================>  |    TESTNET MODE    |  =====================>  |     LIVE MODE      |
| Real WS Market Data|    Profit Factor > 1.3   | Real Exchange API  |    Multi-Factor Audit    | Real Capital Risk  |
| Simulated Fills    |    0 Unhandled Errors    | Sandbox Matching   |    Manual Sign-Off       | Strict Risk Limits |
+--------------------+                          +--------------------+                          +--------------------+
```

### Stage 1: Paper Burn-In (48–72 Hours)
1. Set `EXECUTION_MODE=PAPER` in `.env`.
2. Connect to live Binance and Kraken public WebSocket feeds.
3. Allow the system to ingest real market data, walk live order book depths, and record theoretical vs. executable opportunities.
4. **Acceptance Criteria for Promotion:**
   - Minimum 48 hours of uninterrupted execution.
   - Zero unhandled crash loops or database persistence bottlenecks.
   - Net realized PnL across paper executions is positive with a **Profit Factor $\ge 1.3$** and win rate $\ge 85\%$.

### Stage 2: Testnet Validation (Binance Testnet)
1. Generate testnet API credentials:
   - Binance Spot Testnet: `https://testnet.binance.vision`
   - Binance Futures Testnet: `https://testnet.binancefuture.com`
2. Update `.env`:
   ```ini
   EXECUTION_MODE=TESTNET
   BINANCE_API_KEY=your_testnet_key
   BINANCE_API_SECRET=your_testnet_secret
   ```
3. Run the orchestrator. Order routing will place authenticated testnet orders on Binance while pairing with our high-precision internal paper-matching engine for Kraken.
4. **Acceptance Criteria for Promotion:**
   - Successful HMAC SHA256 authentication and signature validation.
   - Successful round-trip order placement, fill polling, and balance deduction.
   - Verified automated emergency unwind on simulated leg rejection.

### Stage 3: Strict Multi-Factor Safety Checklist for LIVE Mode
> [!CAUTION]
> **LIVE TRADING COMMITS REAL FINANCIAL CAPITAL.**
> Live mode (`EXECUTION_MODE=LIVE`) is disabled by default in configuration. Do not enable live execution without completing every item below.

- [ ] **Dual Exchange Inventory Funded:** Pre-funded balances deposited on both Binance and Kraken in target quote (`USDT`) and base assets (`BTC`, `ETH`, `SOL`, `XRP`).
- [ ] **IP Whitelisting Configured:** API keys restricted to the static production server IP on both Binance and Kraken security dashboards.
- [ ] **Withdrawal Permissions Disabled:** API keys strictly configured with **Trade Only** permissions; **Withdrawal permissions must be disabled**.
- [ ] **BNB Fee Discount Active:** Binance account holds at least $50 worth of BNB with "Use BNB for Fees" enabled to secure lower taker fee rates.
- [ ] **Pre-Trade Risk Limits Confirmed:** `MAX_NOTIONAL_PER_TRADE_USDT` set to a safe percentage of inventory ($\le 5\%$), and `MAX_DAILY_LOSS_USDT` configured to acceptable tolerance.
- [ ] **FastAPI Authentication Key Rotated:** `API_SECRET_KEY` set to a cryptographically secure 256-bit token in `.env`.
- [ ] **Operator Dashboard Monitoring Ready:** Streamlit dashboard open and monitored by the operations team with immediate access to the manual **Emergency Kill Switch**.

---

## 5. Repository Structure

```
cypto-arbitrage-system/
├── config/
│   ├── exchanges.yaml           # Exchange WebSocket/REST endpoints & fee structures
│   ├── settings.py              # Pydantic v2 validated settings loader
│   └── strategy.yaml            # Strategy parameters, thresholds, and cycle definitions
├── dashboard/
│   └── app.py                   # Streamlit real-time monitoring & kill-switch dashboard
├── docs/
│   ├── ARCHITECTURE.md          # Complete technical architecture specification
│   ├── DEVELOPMENT_STATUS.md    # Multi-agent development matrix & status tracker
│   └── PROJECT_SPECIFICATION.md # Institutional functional specification
├── src/
│   ├── analytics/               # Performance analysis, PnL attribution & backtester
│   ├── api/                     # FastAPI control endpoints & health checks
│   ├── core/                    # Domain models, protocols, events & custom exceptions
│   ├── exchanges/               # Async WebSocket & REST exchange adapters
│   ├── execution/               # Multi-leg execution engines & automated unwind logic
│   ├── market_data/             # L2 order book synchronizer, registry & ring buffers
│   ├── opportunity/             # Profitability calculator, fee engine & opportunity evaluator
│   ├── persistence/             # Dual-queue async SQLite/PostgreSQL persistence engine
│   ├── portfolio/               # Balance manager, inventory balancer & position tracking
│   ├── risk/                    # Pre-trade risk manager, exposure tracker & kill switch
│   ├── strategies/              # 4 quantitative strategy engines
│   └── main.py                  # Autonomous trading pipeline orchestrator & CLI entrypoint
├── tests/
│   ├── integration/             # Exchange connectivity & live network tests
│   ├── simulation/              # Deterministic multi-strategy & chaos simulation suites
│   └── unit/                    # 60+ modular unit tests covering all system components
├── pyproject.toml               # Project configuration, dependencies & pytest settings
├── requirements.txt             # Production & development dependencies
└── README.md                    # Institutional operator runbook
```

---

## 6. License & Disclaimer

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

**Disclaimer:** Cryptocurrency trading involves substantial financial risk. This software is provided for quantitative research, simulation, and educational purposes. Authors and contributors accept no liability for financial losses incurred through live operation.
