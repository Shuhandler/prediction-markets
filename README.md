# Prediction Market Arbitrage Bot

A fully automated cross-platform arbitrage bot that monitors **Kalshi** and **Polymarket** prediction markets in real time, detects risk-free arbitrage opportunities, and executes two-leg trades with automatic unwind protection. Supports both **paper trading** (simulated) and **live trading** (real orders).

---

## Table of Contents

- [How It Works](#how-it-works)
- [Arbitrage Strategy](#arbitrage-strategy)
- [Architecture](#architecture)
- [Trading Modes](#trading-modes)
- [Event-Driven Pipeline](#event-driven-pipeline)
- [WebSocket Connections](#websocket-connections)
- [Orderbook Management](#orderbook-management)
- [Market Data Fetching](#market-data-fetching)
- [Opportunity Detection (ArbEngine)](#opportunity-detection-arbengine)
- [Execution Pipeline (ExecutionEngine)](#execution-pipeline-executionengine)
- [Two-Leg Execution & Unwind (UnwindManager)](#two-leg-execution--unwind-unwindmanager)
- [Live Order Clients](#live-order-clients)
- [Risk Management](#risk-management)
- [Stuck Position Monitor](#stuck-position-monitor)
- [Resolution Checker](#resolution-checker)
- [Portfolio & Position Tracking](#portfolio--position-tracking)
- [CSV Logging & Data Files](#csv-logging--data-files)
- [Fee Computation](#fee-computation)
- [Configuration Reference](#configuration-reference)
- [Events File (events.json)](#events-file-eventsjson)
- [Environment Variables](#environment-variables)
- [CLI Usage](#cli-usage)
- [Dependencies](#dependencies)
- [Project Structure](#project-structure)

---

## How It Works

Binary prediction markets price YES and NO contracts that pay $1.00 if they win. On a single platform, `YES + NO = $1.00`. Across platforms, pricing discrepancies create arbitrage: if buying YES on Kalshi + NO on Polymarket totals less than $1.00, the difference is risk-free profit, because exactly one side always pays out $1.00 regardless of the outcome.

The bot continuously monitors both platforms via WebSocket feeds, detects these discrepancies, validates them through a multi-layer risk pipeline, and executes the trade as a sequential two-leg order with automatic rollback if any leg fails.

---

## Arbitrage Strategy

The bot evaluates two directions for every monitored market:

| Direction | Leg 1 (Polymarket) | Leg 2 (Kalshi)     |
| --------- | ------------------ | ------------------- |
| **A**     | Buy NO             | Buy YES             |
| **B**     | Buy YES            | Buy NO              |

**Profit formula** (per contract):

$$\text{Gross Profit} = \$1.00 - (\text{Kalshi Ask} + \text{Polymarket Ask})$$

$$\text{Net Profit} = \text{Gross Profit} - \text{Kalshi Fee} - \text{Polymarket Fee}$$

An opportunity qualifies only when `Gross Profit >= MIN_PROFIT_MARGIN` (default 5 cents). After fees are subtracted, the net profit must still be positive for the trade to execute.

**Position sizing** is the minimum of:
- `POSITION_SIZE / cost_per_contract` (how many contracts the capital allocation allows)
- Best-ask depth on Kalshi (contracts available at the top-of-book price)
- Best-ask depth on Polymarket (contracts available at the top-of-book price)

The bot uses **Fill or Kill** semantics: if either leg lacks sufficient orderbook depth for the desired contract count, the trade is silently rejected.

---

## Architecture

The bot is structured as a set of specialized classes connected via an `asyncio.Queue`:

```
┌─────────────┐   ┌──────────────┐              ┌───────────┐
│ PolymarketWS│──▶│              │              │ ArbEngine │
│  (WS feed)  │   │  asyncio     │──event_name─▶│ (strategy)│
└─────────────┘   │  .Queue      │              └─────┬─────┘
                  │  (10,000 cap)│                    │ opportunities
┌─────────────┐   │              │              ┌─────▼──────────┐
│  KalshiWS   │──▶│              │              │ExecutionEngine │
│  (WS feed)  │   └──────────────┘              │  (OMS + risk)  │
└─────────────┘                                 └─────┬──────────┘
                                                      │ validated trade
                                                ┌─────▼──────────┐
                                                │ UnwindManager  │
                                                │ (two-leg exec) │
                                                └─────┬──────────┘
                                                      │
                                         ┌────────────┼────────────┐
                                         ▼            ▼            ▼
                                   PaperPortfolio  CSV Logs   RiskManager
```

**Key design principles:**

- **All arithmetic uses `decimal.Decimal`** with 12-digit precision and `ROUND_HALF_UP`. No floating-point anywhere in monetary or probability math.
- **Strategy is separated from execution.** `ArbEngine` only detects opportunities; `ExecutionEngine` validates and routes; `UnwindManager` handles placement and rollback.
- **Event-driven, not polling.** WebSocket messages trigger immediate arb checks. No `sleep()` loop.
- **Graceful shutdown** via `SIGTERM`/`SIGINT` signal handlers that set a shared `asyncio.Event`.

---

## Trading Modes

### Paper Mode (default)

```bash
python arb_bot.py
```

- No real orders are placed.
- Fills are simulated as instant and complete.
- Unwind sells are simulated at zero slippage.
- Portfolio, orders, and P&L are tracked in CSV exactly as in live mode.
- Requires only Kalshi API credentials (for WebSocket read-only access). Polymarket WS requires no auth.

### Live Mode

```bash
python arb_bot.py --mode live
```

- **Real orders** are submitted to both platforms.
- Kalshi: IOC (Immediate-or-Cancel) limit orders via authenticated REST, with WS fill channel for confirmation.
- Polymarket: FOK (Fill-or-Kill) orders via `py_clob_client` with EIP-712 signatures on Polygon (chain ID 137).
- Requires credentials for both platforms (see [Environment Variables](#environment-variables)).
- Validates all credentials at startup; exits with an error if any are missing.

---

## Event-Driven Pipeline

The main processing loop (`_process_updates`) runs as follows:

1. **Wait** for an `event_name` string from the shared `asyncio.Queue`.
2. **Look up** the event configuration (Kalshi ticker, Polymarket condition ID, token IDs).
3. **Read snapshots** from both `KalshiWS.get_snapshot()` and `PolymarketWS.get_snapshot()`. These are local in-memory orderbook states — no network call.
4. **Skip** if either snapshot is `None` (no data from that platform yet).
5. **Run `ArbEngine.check()`** to evaluate both directions (A and B).
6. For each qualifying opportunity, **spawn `asyncio.create_task(execution.submit(opp))`**.

The queue has a capacity of 10,000 entries. If it fills (unlikely under normal conditions), updates are dropped with a warning log. The 5-second `wait_for` timeout ensures the loop periodically checks the `stop_event` for graceful shutdown.

---

## WebSocket Connections

### PolymarketWS

- **URL:** `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- **Auth:** None required (read-only).
- **Subscription:** Sends `{"assets_ids": [...], "type": "market"}` for each YES and NO token ID.
- **Message types handled:**
  - `book` — Full orderbook snapshot. Overwrites the local `_LocalOrderbook`.
  - `price_change` — Incremental delta. Updates specific price levels.
- **PING/PONG:** Sends `"PING"` every 10 seconds. Ignores `"PONG"` replies.
- **Stale watchdog:** If no message arrives for `STALE_TIMEOUT_SECONDS` (default 30s), the connection is force-closed. The listen loop detects the close and triggers reconnection.
- **Reconnection:** Exponential backoff from 1s to 30s. On every reconnect:
  1. Re-subscribes to all previously subscribed assets.
  2. Pulls a **fresh REST snapshot** via `MarketFetcher` to avoid trading on stale delta-only data.
  3. Resets the stale watchdog timer.
- **Data format:** Can receive a single JSON object or an array of objects.
- **Queue notification:** On every book/price update, puts the corresponding `event_name` onto the shared queue via `put_nowait()` (non-blocking).

### KalshiWS

- **URL:** `wss://api.elections.kalshi.com/trade-api/ws/v2`
- **Auth:** RSA-PSS signature in WebSocket handshake headers (`KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`). Payload: `{timestamp_ms}GET/trade-api/ws/v2`.
- **Subscription channels:**
  - `orderbook_delta` — Book snapshots and incremental deltas.
  - `fill` (live mode only) — Real-time fill notifications for placed orders. Subscribed via `subscribe_fills()` after initial `connect()`.
- **Message types handled:**
  - `orderbook_snapshot` — Full snapshot with `yes` and `no` bid arrays. Overwrites local books.
  - `orderbook_delta` — Single price+delta update to a specific side.
  - `fill` — Fill notification with `order_id` and `count`. Resolves pending `asyncio.Future` waiters registered by `UnwindManager`. Accumulates partial fills.
  - `error` — Logs the error code and message.
- **Implied asks:** Kalshi only sends bid-side data. Asks are derived: `Ask(YES) = 1.0 - Bid(NO)`, `Ask(NO) = 1.0 - Bid(YES)`.
- **Stale watchdog:** Same behavior as Polymarket — force-reconnect after `STALE_TIMEOUT_SECONDS`.
- **Reconnection:** Same exponential backoff. Re-subscribes to both `orderbook_delta` and `fill` channels. Pulls fresh REST snapshots.
- **Fill tracking (live mode):**
  - `register_fill_waiter(order_id)` — Returns an `asyncio.Future` that resolves when fill(s) arrive.
  - `cancel_fill_waiter(order_id)` — Cancels and cleans up a waiter.
  - Partial fills are accumulated in `_fill_counts[order_id]`.

### _LocalOrderbook

A lightweight in-memory orderbook (dict of `{price: size}` for bids and asks) maintained by both WS classes. Supports:

- `apply_snapshot_poly(data)` — Overwrites from Polymarket book event.
- `apply_price_change_poly(change)` — Applies an incremental Polymarket delta.
- `apply_snapshot_kalshi(levels)` — Overwrites from Kalshi snapshot.
- `apply_delta_kalshi(price, delta)` — Applies a Kalshi delta (removes level if size drops to 0).
- `to_tob()` — Converts to `TopOfBook` (best bid/ask + depth levels).

---

## Market Data Fetching

`MarketFetcher` handles all REST-based market data requests. It is used:
- For initial snapshots after every WS (re)connect.
- To check market resolution status.
- As a fallback data source (Polymarket `/book` → `/price` fallback).

### Key methods

| Method | Purpose |
| ------ | ------- |
| `fetch_kalshi(ticker)` | Fetches Kalshi REST orderbook. Parses the binary format — YES bids + NO bids, with implied asks via `1 - price`. |
| `fetch_polymarket(condition_id)` | Tries `/book` endpoint first (real orderbook with depth). Falls back to `/price` (indicative midpoint, no depth). Remembers per-condition which endpoint works. |
| `fetch_both(ticker, condition_id)` | Concurrent fetch of both platforms via `asyncio.gather`. |
| `check_kalshi_active(ticker)` | Checks if a Kalshi market is still active by inspecting `status` and `result` fields (does NOT use `close_time`). |
| `check_poly_active(condition_id)` | Checks Polymarket `closed` and `active` flags. |

### Token resolution

Polymarket token IDs (YES/NO) are **not resolved at runtime** from APIs. They are loaded from `events.json` and cached in `_poly_token_cache`. The `_resolve_poly_tokens()` method is cache-only — it never makes network calls.

### HTTP resilience

All REST calls go through `_request_with_retry()`:
- Retries on **429 (rate limit)**, **5xx (server error)**, and **timeouts**.
- Does **not** retry on **4xx client errors** (except 429).
- Exponential backoff: `1s × 2^attempt`, up to `MAX_RETRIES` (default 3).
- Each platform has its own `RateLimiter` (token-bucket) to stay under documented rate limits.

---

## Opportunity Detection (ArbEngine)

`ArbEngine` is purely analytical — it never places orders.

For both directions (A and B):

1. Read best-ask prices from the Kalshi and Polymarket snapshots.
2. Compute `total_cost = kalshi_ask + poly_ask`.
3. Compute `gross_profit = 1.00 - total_cost`.
4. If `gross_profit < MIN_PROFIT_MARGIN` — skip.
5. Compute per-contract fees for both platforms.
6. Compute `net_profit = 1.00 - total_cost - kalshi_fee - poly_fee`.
7. Compute `max_contracts = min(kalshi_depth_at_best_ask, poly_depth_at_best_ask)`.
8. Return an `ArbOpportunity` dataclass with all computed values.

The engine only considers top-of-book prices (best ask). It does not walk the orderbook for deeper levels.

---

## Execution Pipeline (ExecutionEngine)

The `ExecutionEngine` implements a **two-phase validation pipeline** designed to minimize CSV log spam while preserving instant retry:

### Silent Pre-Trade Filters (no Order created, no CSV written, no rate-limit consumed)

These checks return `None` silently on every WS tick that doesn't qualify:

1. **Duplicate / in-flight check** — Rejects if `(event_name, direction)` is already traded or has an in-flight order. The key is held in `self._traded` and released if the trade fails/unwinds (allowing immediate retry on the next tick).
2. **Risk management gate** — Delegates to `RiskManager.check()` (see [Risk Management](#risk-management)). Covers kill switch, daily loss limit, exposure cap, position limit, and spread sanity — but NOT the rate limiter.
3. **Net profitability** — Rejects if `net_profit <= 0` after fees.
4. **Capital & sizing** — Computes `desired_contracts = min(POSITION_SIZE, available_capital) / cost_per_contract`. Rejects if < 1.
5. **FOK depth check** — Rejects if either platform's orderbook depth at best ask is less than `desired_contracts`.

### Post-Trade Gate (Order created, CSV logged)

6. **Order rate limiter** — Sliding-window rate limiter (`MAX_ORDERS_PER_MINUTE`). Only consumed when all silent checks pass. If rate-limited, an Order is created with `REJECTED` status and logged to CSV for audit.
7. **Fill** — Routes to `UnwindManager.execute()` for sequential two-leg execution.

### Order outcomes

| Status | Meaning |
| ------ | ------- |
| `FILLED` | Both legs completed. Position opened in portfolio. |
| `UNWOUND` | Leg 1 filled but Leg 2 failed; Leg 1 was sold back. Loss recorded in portfolio. |
| `CANCELLED` | Leg 1 never filled, or an unexpected error occurred. No capital impact. |
| `REJECTED` | Rate limit exceeded (the only rejection that creates a CSV row). |

On any non-`COMPLETE` outcome from `UnwindManager`, the `(event, direction)` key is removed from `self._traded` so the bot can retry on the next tick.

---

## Two-Leg Execution & Unwind (UnwindManager)

### Execution order

**Polymarket FOK is always Leg 1** (executed first). Rationale: FOK is all-or-nothing, and Polymarket is less reliable. If Poly FOK fails, no Kalshi order is placed — zero legging risk. Kalshi IOC (Leg 2) is more reliable and supports partial fills, making it the safer second leg.

### Execution flow

```
┌──────────────────────────────────────────────────────────────┐
│ 1. Build Leg 1 (Polymarket FOK) and Leg 2 (Kalshi IOC)      │
│ 2. Submit Leg 1 (Polymarket FOK)                             │
│    ├─ FAILED  → mark FAILED, return                          │
│    ├─ 0 fills → mark FAILED, return                          │
│    └─ FILLED (possibly partial) → continue                   │
│ 3. If Leg 1 partial fill → adjust Leg 2 size to match       │
│ 4. Submit Leg 2 (Kalshi IOC)                                 │
│    ├─ FAILED / 0 fills → UNWIND Leg 1 (sell back)            │
│    ├─ Partial fill → UNWIND excess contracts from Leg 1      │
│    └─ Full fill → COMPLETE                                   │
└──────────────────────────────────────────────────────────────┘
```

### Paper mode fills

Both `_submit_leg()` and `_submit_unwind()` simulate instant, complete fills with zero slippage. An `await asyncio.sleep(0)` yields to the event loop.

### Live mode fills

**Kalshi buy (`_submit_kalshi_buy`):**
1. Convert Decimal price to integer cents.
2. POST an IOC limit order with a `client_order_id` (UUID) for reconciliation.
3. If the HTTP POST **times out**, reconcile by querying recent orders on that ticker matching the `client_order_id`. The exchange may have accepted and filled the order despite no HTTP response.
4. Check REST response: if `remaining_count == 0` or `status == "executed"/"filled"` → done.
5. If partial fill → report partial fill count.
6. If ambiguous → register a WS fill waiter (`asyncio.Future`) and `await` with timeout.
7. Final REST poll if WS waiter times out.
8. If no fill at all → IOC was fully cancelled.

**Polymarket buy (`_submit_poly_buy`):**
1. Resolve the specific `token_id` for the condition + side from cache.
2. Submit FOK buy order via `PolymarketOrderClient.place_order()`.
3. Check response `status`:
   - `"matched"` → fully filled (FOK guarantees no partials).
   - `"delayed"` → rare; poll repeatedly until `"matched"` or `"unmatched"`.
   - Anything else → killed/unmatched.

### Unwind process

When Leg 2 fails and Leg 1 is filled, the bot sells Leg 1 back:

- **Builds a sell order** on the same platform/side as Leg 1.
- **Slippage-protected pricing:** Sell limit = `max(buy_price × UNWIND_MIN_PRICE_RATIO, UNWIND_ABSOLUTE_MIN_PRICE)`. Defaults: 50% of buy price, minimum $0.05. Prevents selling at $0.01 what was bought at $0.55.

**Kalshi sell (`_sell_kalshi`):**
- IOC sell at the floor price.
- Updates `leg.price` to the actual execution price from the REST response (for accurate slippage calculation).

**Polymarket sell (`_sell_poly`) — Smart-sized FOK:**
1. Fetch the live orderbook for the token via REST.
2. Sum total resting bid quantity across all price levels.
3. Cap FOK size to `min(contracts_to_sell, total_bids)` to avoid certain rejection for insufficient counterparty depth.
4. If no bids exist, retry up to 3 times (0.5s apart).
5. Send FOK at the slippage-protected floor price.
6. If the capped size is less than the full amount, mark as `FAILED` so the `StuckPositionMonitor` can handle the remainder.

### Unwind loss calculation

$$\text{Unwind Loss} = \text{Leg 1 Fee} + \text{Unwind Fee} + \text{Slippage}$$

$$\text{Slippage} = (\text{Buy Price} - \text{Sell Price}) \times \text{Contracts}$$

In paper mode slippage is always zero. In live mode it reflects the actual fill price.

The loss is:
- Deducted from portfolio capital.
- Accumulated in `total_unwind_losses`.
- Fed to the daily P&L tracker as negative P&L (may trip the circuit breaker).
- Logged to `data/unwinds.csv`.
- Written to `data/portfolio.csv` as an `UNWIND` row.

### Partial unwind

If Leg 2 partially fills, only the **excess** (Leg 1 fills minus Leg 2 fills) is unwound. The successfully paired contracts become a valid position.

---

## Live Order Clients

### KalshiOrderClient

- **Authentication:** RSA-PSS signatures on every REST request.
  - Payload: `{timestamp_ms}{METHOD}{path}` where path starts from `/trade-api/v2/...`.
  - Hash: SHA-256, padding: PSS with MGF1(SHA-256), salt_length: MAX_LENGTH.
  - Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`.
- **Methods:**
  - `place_order(ticker, action, side, count, price_cents, time_in_force, client_order_id)` — Places an order. Returns response dict.
  - `get_order(order_id)` — Gets a single order by ID.
  - `get_orders(**params)` — Queries orders with filters (used for timeout reconciliation).
  - `cancel_order(order_id)` — Cancels an order.
  - `close()` — Closes the aiohttp session.
- **Price format:** Integer cents (1–99).

### PolymarketOrderClient

- **Authentication:** EIP-712 signed orders on Polygon (chain ID 137) via `py_clob_client.ClobClient`.
- **Credentials:** `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_PASSPHRASE`, plus an Ethereum private key read from `POLY_PRIVATE_KEY_PATH`.
- **Nonce safety:** All order operations are serialized through `asyncio.Lock()` to prevent nonce races (the synchronous `py_clob_client` is wrapped in `asyncio.to_thread()`).
- **Methods:**
  - `place_order(token_id, side, price, size, order_type)` — Creates and posts an order. Returns response dict.
  - `resolve_token_id(condition_id, side)` — Cache-only lookup of YES/NO token IDs.
  - `get_order(order_id)` — Gets order status.
  - `cancel_order(order_id)` — Cancels an order.
  - `close()` — No-op (synchronous client has no session to close).
- **Client initialization:** Lazy via `_ensure_client()`. Called at startup in live mode to catch credential errors early.
- **Token cache:** Shared with `MarketFetcher._poly_token_cache` so token resolution happens once.

---

## Risk Management

`RiskManager` aggregates six checks into a single gate called before every order:

| # | Check | Rejection Reason | Description |
|---|-------|-----------------|-------------|
| 1 | **Kill switch** | `KILL_SWITCH` | If a file named `KILL` (configurable) exists in the working directory, all orders are rejected. Delete the file to resume. |
| 2 | **Daily loss limit** | `DAILY_LOSS_LIMIT` | `DailyPnLTracker` sums realized P&L for the current UTC day. If cumulative loss exceeds `DAILY_LOSS_LIMIT` (default $100), the circuit breaker trips and blocks all new orders until midnight UTC. Resets automatically on day change. |
| 3 | **Spread sanity** | `SPREAD_TOO_WIDE` | Rejects opportunities with `gross_profit > MAX_SPREAD_THRESHOLD` (default 20%). Unreasonably large spreads indicate stale/erroneous data, not real arbitrage. |
| 4 | **Max concurrent positions** | `MAX_POSITIONS` | Caps the number of simultaneously open positions (default 10). |
| 5 | **Max total exposure** | `MAX_EXPOSURE` | Caps the total dollar outlay across all open positions (default $500). |
| 6 | **Order rate limiter** | `ORDER_RATE_LIMIT` | Sliding-window limiter: max `MAX_ORDERS_PER_MINUTE` (default 10) order submissions in any 60-second window. Applied as a post-filter in `ExecutionEngine` (only consumed when all other checks pass). |

### All RejectReason values

```
NONE, DUPLICATE, NO_PROFIT, NO_CAPITAL, DEPTH_KALSHI, DEPTH_POLY,
DEPTH_BOTH, ORDER_RATE_LIMIT, DAILY_LOSS_LIMIT, MAX_EXPOSURE,
MAX_POSITIONS, SPREAD_TOO_WIDE, KILL_SWITCH
```

---

## Stuck Position Monitor

`StuckPositionMonitor` is a background `asyncio` task that runs every `STUCK_POSITION_CHECK_INTERVAL` seconds (default 10s). It detects half-filled arbs that are stuck in intermediate states:

- `LEG1_SUBMITTED`
- `LEG1_FILLED`
- `LEG2_SUBMITTED`

If an `UnwindOrder` has been in one of these states for longer than `STUCK_POSITION_MAX_AGE` seconds (default 30s), the monitor triggers `UnwindManager.force_unwind()` to sell back the filled leg.

---

## Resolution Checker

A background task (`_resolution_checker`) runs every `RESOLUTION_CHECK_INTERVAL` seconds (default 60s). For each active event:

1. Concurrently checks `check_kalshi_active(ticker)` and `check_poly_active(condition_id)`.
2. If either market has resolved → closes all open positions for that event (credits $1.00/contract payout) and removes the event from the active list.
3. If all events have resolved → sets `stop_event` to trigger graceful shutdown.

---

## Portfolio & Position Tracking

### PaperPortfolio

Tracks simulated capital and open positions:

- `open_position(pos)` — Deducts `total_outlay` from capital, adds to open positions list, writes an `OPEN` row to CSV.
- `close_positions_for_event(event_name)` — Settles all positions for a resolved event. Credits `$1.00 × contracts` as payout. Computes P&L and writes `CLOSE` rows.
- `record_unwind_loss(event_name, direction, contracts, loss_amount)` — Debits the portfolio for a realized unwind loss. Writes an `UNWIND` row.
- `summary()` — Multi-line human-readable status: capital, open positions, realized P&L, ROI.

### Position dataclass

Each open position records: `event_name`, `direction`, `contracts`, `cost_per_contract`, `fees_per_contract`, `total_outlay`, `opened_at`.

---

## CSV Logging & Data Files

All data files are created in `data/` with headers on first run.

| File | Logger Class | Contents |
| ---- | ------------ | -------- |
| `data/trade_ledger.csv` | `PaperTrader` | Every detected opportunity (logged regardless of execution outcome). Columns: timestamp, event, direction, prices, fees, gross/net profit, depth, latency. |
| `data/orders.csv` | `OrderLogger` | Full order lifecycle. Columns: order_id, timestamp, status (FILLED/REJECTED/UNWOUND/CANCELLED), event, direction, desired/filled contracts, prices, fees, profits, outlay, reject_reason, depth. |
| `data/portfolio.csv` | `PaperPortfolio` | Position opens, closes, and unwinds. Columns: action (OPEN/CLOSE/UNWIND), timestamp, event, direction, contracts, cost/fees/outlay, payout, P&L, capital_remaining. |
| `data/unwinds.csv` | `UnwindLogger` | Unwind order lifecycle. Columns: unwind_id, timestamp, event, direction, status, contracts, net_profit, leg1/leg2/unwind leg details (platform, side, price, status, filled), error_message. |

In-memory order/unwind lists are capped at `MAX_ORDERS_IN_MEMORY` (default 1000) to prevent unbounded growth. CSV persistence is immediate and uncapped.

---

## Fee Computation

Both platforms use the same fee formula:

$$\text{Fee} = \lceil \text{Rate} \times C \times P \times (1 - P) \rceil_{\text{penny}}$$

Where:
- $\text{Rate}$ = `KALSHI_FEE_RATE` (default 0.07 = 7%) or `POLY_FEE_RATE` (default 0)
- $C$ = number of contracts
- $P$ = probability (price as a decimal, e.g., 0.65)
- $\lceil \cdot \rceil_{\text{penny}}$ = ceiling to the nearest cent (`ROUND_CEILING` to 2 decimal places)

The fee is maximized at $P = 0.5$ and approaches zero near $P = 0$ or $P = 1$. Kalshi's max fee per contract at 7% rate is $0.02 (at $P = 0.50$).

Polymarket currently charges 0% fees on most markets. Set `POLY_FEE_RATE` to `0.0624` for fee-enabled markets.

---

## Configuration Reference

All configuration is read from environment variables with defaults. Set them via a `.env` file (loaded by `python-dotenv`) or export directly.

### Trading Parameters

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `MIN_PROFIT_MARGIN` | `0.05` | Minimum gross profit (dollars) per contract to qualify as an opportunity. |
| `STARTING_CAPITAL` | `1000.00` | Initial paper portfolio capital. |
| `POSITION_SIZE` | `50.00` | Maximum dollar allocation per trade. |
| `KALSHI_FEE_RATE` | `0.07` | Kalshi taker fee rate (7%). |
| `POLY_FEE_RATE` | `0` | Polymarket taker fee rate (0% for most markets). |

### Risk Management

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `DAILY_LOSS_LIMIT` | `100.00` | Max cumulative daily loss before circuit breaker trips. |
| `MAX_TOTAL_EXPOSURE` | `500.00` | Max total dollar outlay across all open positions. |
| `MAX_CONCURRENT_POSITIONS` | `10` | Max number of simultaneously open positions. |
| `MAX_SPREAD_THRESHOLD` | `0.20` | Max gross profit ratio (rejects likely-stale data). |
| `MAX_ORDERS_PER_MINUTE` | `10` | Sliding-window order rate limit. |
| `KILL_FILE` | `KILL` | File whose existence halts all trading. |

### Unwind Settings

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `UNWIND_TIMEOUT_SECONDS` | `5` | Timeout for each leg during execution. |
| `STUCK_POSITION_MAX_AGE` | `30` | Seconds before a stuck position triggers auto-unwind. |
| `STUCK_POSITION_CHECK_INTERVAL` | `10` | Seconds between stuck position monitor checks. |
| `UNWIND_MIN_PRICE_RATIO` | `0.50` | Minimum sell price as fraction of buy price during unwind. |
| `UNWIND_ABSOLUTE_MIN_PRICE` | `0.05` | Absolute minimum sell price during unwind ($0.05). |

### Network & WebSocket

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `STALE_TIMEOUT_SECONDS` | `30` | Seconds without WS data before force-reconnect. |
| `KALSHI_BASE` | `https://api.elections.kalshi.com/trade-api/v2` | Kalshi REST API base URL. |
| `KALSHI_WS_URL` | `wss://api.elections.kalshi.com/trade-api/ws/v2` | Kalshi WebSocket URL. |
| `CLOB_BASE` | `https://clob.polymarket.com` | Polymarket CLOB REST base URL. |
| `POLY_WS_URL` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Polymarket WebSocket URL. |
| `MAX_ORDERS_IN_MEMORY` | `1000` | In-memory order list cap (CSV is always uncapped). |
| `KALSHI_FILL_POLL_MS` | `250` | Poll interval for Kalshi/Poly order status checks. |
| `RESOLUTION_CHECK_INTERVAL` | `60` | Seconds between market resolution checks. |

---

## Events File (events.json)

The bot loads market pairs from a JSON file. Each entry requires exactly 5 fields:

```json
[
  {
    "name": "Will BTC hit $100k by Dec 2025?",
    "kalshi_ticker": "KXBTC-100K-DEC25",
    "poly_condition_id": "0xabc123...",
    "poly_yes_token": "12345678901234...",
    "poly_no_token": "98765432109876..."
  }
]
```

| Field | Description |
| ----- | ----------- |
| `name` | Human-readable event name (used as the key in logging, queue, and dedup). |
| `kalshi_ticker` | Kalshi market ticker string. |
| `poly_condition_id` | Polymarket condition ID (hex string). |
| `poly_yes_token` | Polymarket YES token ID (numeric string). |
| `poly_no_token` | Polymarket NO token ID (numeric string). |

**Validation at startup:**
- All 5 fields must be present and non-empty.
- Values of `"FILL_ME"` are rejected (placeholder detection).
- Duplicate event names are not explicitly checked but will cause the second to overwrite the first in `name_to_event`.

Use `ticker_return.py` (interactive helper) to generate entries with explicit token IDs.

---

## Environment Variables

### Paper mode (minimum)

```bash
KALSHI_API_KEY=your_kalshi_api_key_id
KALSHI_PRIVATE_KEY_PATH=keys/kalshi_private.txt
```

### Live mode (all required)

```bash
# Kalshi
KALSHI_API_KEY=your_kalshi_api_key_id
KALSHI_PRIVATE_KEY_PATH=keys/kalshi_private.txt

# Polymarket
POLY_API_KEY=your_poly_clob_api_key
POLY_API_SECRET=your_poly_clob_api_secret
POLY_PASSPHRASE=your_poly_clob_passphrase
POLY_PRIVATE_KEY_PATH=keys/poly_private.txt
```

The Kalshi private key is an RSA PEM file. The Polymarket private key is a hex-encoded Ethereum private key file.

---

## CLI Usage

```bash
python arb_bot.py [OPTIONS]
```

| Flag | Default | Description |
| ---- | ------- | ----------- |
| `--events-file PATH` | `events.json` | Path to the events JSON configuration file. |
| `--log-level LEVEL` | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. Also settable via `LOG_LEVEL` env var. |
| `--dry-run` | off | Print startup banner and configuration, then exit without trading. |
| `--mode {paper,live}` | `paper` | Trading mode. `paper` simulates; `live` places real orders. |

### Examples

```bash
# Paper trading with default config
python arb_bot.py

# Live trading with debug logging
python arb_bot.py --mode live --log-level DEBUG

# Validate config and exit
python arb_bot.py --dry-run

# Custom events file
python arb_bot.py --events-file my_events.json
```

---

## Dependencies

```
aiohttp>=3.9.0
cryptography>=41.0.0
python-dotenv>=1.0.0
requests>=2.31.0
py-clob-client>=0.13.0
```

Install with:

```bash
pip install -r requirements.txt
```

Python 3.10+ required (for `match`/`case`, `dict | None` syntax, and `asyncio` features).

---

## Project Structure

```
prediction-markets/
├── arb_bot.py               # Main bot (4942 lines, 21 classes, 136 methods)
├── ticker_return.py         # Interactive config generator for events.json
├── events.json              # Market pair configuration
├── requirements.txt         # Python dependencies
├── scope.md                 # Development scope / phase checklist
├── README.md                # This file
├── .env                     # Environment variables (not committed)
├── keys/
│   ├── kalshi_private.csv   # RSA private key for Kalshi auth
│   └── poly_private.txt     # Hex-encoded Ethereum private key for Polymarket auth
├── data/
│   ├── trade_ledger.csv     # All detected opportunities
│   ├── orders.csv           # Order lifecycle log
│   ├── portfolio.csv        # Position opens/closes/unwinds
│   └── unwinds.csv          # Unwind order lifecycle log
└── __pycache__/
```

---

## Graceful Shutdown

The bot handles `SIGTERM` and `SIGINT` (Ctrl+C) by setting a shared `asyncio.Event`. This triggers:

1. The main processor loop exits.
2. All background tasks (resolution checker, stuck monitor) are cancelled.
3. WebSocket connections are closed on both platforms.
4. REST sessions are closed.
5. Order clients are closed (live mode).
6. A summary is printed: portfolio status, order counts, daily P&L, unwind statistics, and trading mode.
