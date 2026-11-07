# Crypto Arbitrage Intelligence & Trading System: Project Specification

## 1. Project Overview & System Scope
The Crypto Arbitrage Intelligence & Trading System is an institutional-grade, modular monolithic trading engine written in Python. It detects, evaluates, risk-manages, simulates, and executes two core quantitative arbitrage strategies across **Binance Spot** and **Kraken Spot**:

1. **Strategy A: Cross-Exchange Spot Arbitrage (Pre-Funded Inventory Model)**
   - Simultaneously buy on the lower-ask exchange and sell on the higher-bid exchange.
   - Zero-Transfer Assumption: Execution relies strictly on pre-funded asset balances on both venues.
   - Enforces L2 VWAP depth walking, exchange maker/taker fee tiers, minimum notional constraints, lot size floor truncation, and latency freshness gates.
2. **Strategy B: Single-Exchange Triangular Arbitrage**
   - Evaluates directed 3-leg cycles within a single exchange (e.g., $USDT \to BTC \to ETH \to USDT$).
   - Re-evaluates affected cycles upon L2 updates.
   - Accurately accounts for fee compounding, tick size, and lot size truncation at each intermediate step.

---

## 2. Exchange Technical Specifications

### 2.1 Binance Spot Specification
- **REST API Base URL:** `https://api.binance.com/api/v3`
- **Testnet REST Base URL:** `https://testnet.binance.vision/api/v3`
- **Public WebSocket:** `wss://stream.binance.com:9443/ws`
- **Testnet WebSocket:** `wss://testnet.binance.vision/ws`
- **UserData Stream:** Requires REST `POST /api/v3/userDataStream` to acquire `listenKey` with keep-alive every 30 minutes (`PUT /api/v3/userDataStream`).
- **Depth Stream:** `<symbol>@depth@100ms`
- **REST Depth Snapshot:** `GET /api/v3/depth?symbol={symbol}&limit=1000`
- **Rate Limit Weight:** 1200 request weight per minute per IP. Order rate limit: 100 orders per 10s, 200,000 per 24 hours.
- **Base Fee Tier (VIP 0):** Maker 0.10%, Taker 0.10% (0.075% with BNB deduction).
- **Symbol Precision Rules (`/api/v3/exchangeInfo`):**
  - `PRICE_FILTER`: `tickSize`
  - `LOT_SIZE`: `stepSize`, `minQty`, `maxQty`
  - `NOTIONAL` / `MIN_NOTIONAL`: `minNotional`

#### Binance L2 Sync State Machine
```
[Start]
  │
  ├── 1. Connect to WebSocket stream: <symbol>@depth@100ms
  ├── 2. Buffer all incoming depth messages into local memory
  ├── 3. Fetch REST snapshot: GET /api/v3/depth?symbol=<symbol>&limit=1000
  │      -> Returns snapshot with lastUpdateId
  ├── 4. Drop buffered events where u <= lastUpdateId
  ├── 5. First processed event must satisfy:
  │      U <= lastUpdateId + 1  AND  u >= lastUpdateId + 1
  ├── 6. For all subsequent events:
  │      U_new == u_prev + 1
  └── 7. If gap detected (U_new > u_prev + 1):
         -> Trigger Desynchronization Alert
         -> Purge local book, re-initiate hydration sequence
```

### 2.2 Kraken Spot Specification
- **REST API Base URL:** `https://api.kraken.com/0/`
- **Public WebSocket v2:** `wss://ws.kraken.com/v2`
- **Private WebSocket v2:** `wss://ws-auth.kraken.com/v2` (requires authentication token obtained via private REST `GetWebSocketsToken`).
- **Depth Channel:** `book` with `depth: 100`
- **Rate Limit Model:** Call counter system (15–20 max counter, decaying 0.33 to 1.0 calls/second).
- **Base Fee Tier:** Maker 0.16%–0.25%, Taker 0.26%–0.40%.
- **Symbol Precision Rules (`/0/public/AssetPairs`):**
  - `tick_size`: Minimum price variation.
  - `lot_decimals`: Minimum amount decimal precision.
  - `ordermin`: Minimum order quantity.
  - `costmin`: Minimum order cost.

#### Kraken L2 CRC32 Checksum Engine
Kraken v2 provides a CRC32 checksum in every incremental book message.
The checksum is calculated over the top 10 bids and top 10 asks:
1. Iterate asks from index 0 to 9 (lowest price to highest):
   - Format: `f"{price_formatted}{qty_formatted}"`
2. Iterate bids from index 0 to 9 (highest price to lowest):
   - Format: `f"{price_formatted}{qty_formatted}"`
3. Concatenate all 20 strings.
4. Calculate standard IEEE 802.3 CRC32 integer:
   `crc = zlib.crc32(concatenated_bytes) & 0xFFFFFFFF`
5. Compare with received `checksum`. If mismatch occurs:
   - Flag order book `is_stale = True`.
   - Re-subscribe to `book` channel to receive a fresh snapshot.

---

## 3. Mathematical Execution Formulations

### 3.1 Order Book VWAP Walking
Given an order book side with sorted levels $(P_i, Q_i)$:
To execute quantity $Q$:
$$C = \sum_{i=1}^{k} P_i \cdot \min(q_{rem, i}, Q_i) \quad \text{where} \quad \sum \min(q_{rem, i}, Q_i) = Q$$
$$\text{VWAP} = \frac{C}{Q}$$

### 3.2 Strategy A: Cross-Exchange Net Edge & Fee Accounting

#### Conservative Fee Baseline & BNB Discount Rules
1. **Conservative Fee Tier:** The default fee model assumes standard VIP 0 rates of **0.10% (10 bps)** Maker and **0.10% (10 bps)** Taker for both exchanges (Binance and Kraken default).
2. **BNB Fee Discount Gate:** A reduced fee of **0.075% (7.5 bps)** on Binance is applied **only if** `enable_bnb_discount = True` AND verified account `BNB` balance $\ge 0.1\text{ BNB}$.
3. **Base-Asset vs. Quote-Asset Fee Deduction Impact:**
   - On Binance Spot, when BNB fee deduction is **disabled**, fees on `BUY` orders are deducted directly from the **received base asset** (e.g., buying BTC with USDT delivers $Q \cdot (1 - f_{taker})$ in BTC).
   - **Base Inventory Invariant:** Inventory tracking must credit the account with net base asset $Q_{net} = Q_{fill} \cdot (1 - f_{taker})$. Any concurrent or subsequent sell leg must not attempt to sell more than $Q_{net}$ unless pre-funded surplus exists.
   - **Quote Profit Valuation:** In quote PnL accounting, the base-denominated fee is valued at the execution buy price: $\text{Fee}_{quote} = (Q_{fill} \cdot f_{taker}) \cdot P_{vwap\_buy}$.
   - For `SELL` orders, fees are deducted in quote currency ($USDT$), directly reducing cash proceeds.

#### Net Profit & Edge Formula
$$\Pi_{net} = \left(Q_{exec} \cdot P_{vwap\_sell}\right) \cdot (1 - f_{taker, sell}) - \left(Q_{exec} \cdot P_{vwap\_buy}\right) \cdot (1 + f_{taker, buy})$$
$$\text{Edge}_{bps} = \frac{\Pi_{net}}{Q_{exec} \cdot P_{vwap\_buy}} \times 10,000$$

### 3.3 Strategy B: Triangular Arbitrage Compound Return
Given an initial balance $K_0$ in currency $C_0$:
$$K_1 = \text{ExecuteLeg}(K_0, \text{Leg}_1, f_1)$$
$$K_2 = \text{ExecuteLeg}(K_1, \text{Leg}_2, f_2)$$
$$K_{final} = \text{ExecuteLeg}(K_2, \text{Leg}_3, f_3)$$
$$\text{Return}_{bps} = \frac{K_{final} - K_0}{K_0} \times 10,000$$
At each leg:
- `quantity` is truncated via `floor(quantity, lot_size)`.
- Base-asset fee deductions are accounted for before passing intermediate balance to the next hop.
- If `quantity * price < min_notional`, the cycle is aborted.

---

## 4. Emergency Unwind & Directional Hedging Invariants
When multi-leg execution experiences partial fills, rejected subsequent legs, or timeouts:
1. **Directional Aggressiveness Rules:**
   - **Long Unwind (Flattening an acquired Long leg):** Submit an immediate `IOC` limit SELL crossing the touch at:
     $$P_{unwind\_sell} = P_{best\_bid} \cdot (1 - 0.0020) \quad (-20\text{ bps})$$
   - **Short Unwind (Covering an acquired Short leg):** Submit an immediate `IOC` limit BUY crossing the touch at:
     $$P_{unwind\_buy} = P_{best\_ask} \cdot (1 + 0.0020) \quad (+20\text{ bps})$$
2. **Escalation Policy:**
   - If the `IOC` order does not completely fill within **300ms**, escalate immediately to an aggressive **MARKET** order.
3. **Circuit Breaker Halt:**
   - If realized unwind slippage exceeds **50 bps** ($0.50\%$) or market unwind rejects, trigger `EMERGENCY_HALT` and freeze automated order routing across all strategies.

---

## 4. Opportunity Lifecycle Pipeline

```
┌──────────────────────────────┐
│   THEORETICAL_OPPORTUNITY    │  Top of book spread > 0
└──────────────┬───────────────┘
               │ VWAP Depth Walk & Fee Deductions
               ▼
┌──────────────────────────────┐
│    EXECUTABLE_OPPORTUNITY    │  Net edge >= min_net_edge_bps & balances verified
└──────────────┬───────────────┘
               │ Execution Mode Selection
         ┌─────┴─────────────────────┐
         ▼                           ▼
┌─────────────────┐         ┌─────────────────┐
│ SIMULATED_FILL  │ (Paper) │DEMO_TESTNET_FILL│ (Testnet)
└─────────────────┘         └────────┬────────┘
                                     │ Production Override
                                     ▼
                            ┌─────────────────┐
                            │    LIVE_FILL    │ (Strictly disabled by default)
                            └─────────────────┘
```

---

## 5. System Invariants
1. **Zero Floats:** Fixed-point or exact `Decimal` arithmetic everywhere in the core engine.
2. **Freshness Gate:** Any order book older than 250ms is discarded.
3. **Emergency Unwind:** Failed multi-leg sequences automatically trigger market hedges within 300ms.
4. **Non-Blocking Persistence:** Database operations are asynchronous and isolated from the trading event loop.
