# Arbitrage Bot Audit Report

**Repository:** `prediction-markets/arb_bot.py` (v6.0)
**Audit Date:** 2026-02-25
**Scope:** Logic correctness, execution risks, API conformance, missing edge cases

---

## Critical Logic Flaws

### 1. Polymarket Fee Formula Is Wrong for Most Fee-Enabled Markets

**Severity: CRITICAL**

The repo uses a single fee formula for Polymarket:

```python
fee = ceil_penny(POLY_FEE_RATE × C × P × (1 − P))
```

This mirrors the Kalshi shape and assumes exponent = 1. However, per the [official Polymarket fee documentation](https://docs.polymarket.com/trading/fees), the fee formula is:

```
fee = C × feeRate × (p × (1 − p))^exponent
```

There are **two distinct fee regimes** on Polymarket:

| Market Type | Fee Rate | Exponent | Max Effective Rate |
|---|---|---|---|
| 5-min and 15-min Crypto | 0.0175 | 1 | ~0.44% at p=0.50 |
| Sports — NCAAB & Serie A | 0.25 | **2** | ~1.56% at p=0.50 |

The bot hardcodes `POLY_FEE_RATE = 0` by default and suggests setting it to `~0.0624` for fee-enabled markets. This is incorrect:

- For **crypto** markets: the correct rate is `0.0175` with exponent 1 — the repo's formula shape is correct, but the suggested rate (`0.0624`) is wrong.
- For **sports** (NCAAB/Serie A) markets: the correct rate is `0.25` with exponent **2** — the repo's formula raises `P*(1-P)` to the first power, not the second. The fee at P=0.50 would be `0.25 × 0.25 = 0.0625` per contract with exponent 2, but the repo's formula yields `0.25 × 0.25 = 0.0625` (coincidentally equal at P=0.5). However, at other prices (e.g., P=0.20: correct = `0.25 × 0.16^2 = 0.0064`; repo = `0.25 × 0.16 = 0.04`), the repo **dramatically overstates fees** at non-midpoint prices, causing it to reject genuinely profitable arbs, or if the rate is tuned to match at P=0.5, to understate fees at other prices.

**Impact:** The bot will either (a) miss profitable opportunities by overestimating fees, or (b) execute unprofitable trades by underestimating fees, depending on the market type and the price level. Since the repo is focused on sports markets (NCAAB), this is directly relevant.

**Recommendation:** Add an `exponent` parameter to `poly_taker_fee()` and dynamically fetch the fee rate per market via `GET /fee-rate?token_id=...` as Polymarket's own documentation recommends. Never hardcode a fee rate.

### 2. Polymarket Fee Rounding Uses `ceil_penny` (Wrong Precision)

**Severity: HIGH**

The repo rounds Polymarket fees using `_ceil_penny()` (rounds up to $0.01). Per official docs, Polymarket fees are "rounded to 4 decimal places" and "the smallest fee charged is 0.0001 USDC." Using 2-decimal-place ceiling rounds creates a systematic upward bias in fee estimation, especially on small trades. At 1 contract × $0.30, the real fee on a sports market is ~$0.0004, but `ceil_penny` rounds this up to $0.01 — a 25× overstatement.

### 3. Leg Ordering Mismatch Between Comments and Code

**Severity: HIGH**

The `_build_legs()` method and surrounding comments contradict each other about execution order:

- The docstring on `_build_legs()` (line ~3024) states: *"Polymarket FOK is executed first because it is less reliable... Kalshi IOC (Leg 2) is more reliable and supports partial fills, making it the safer second leg."*
- The `execute()` method labels `leg1` as the first submitted and `leg2` as the second.
- `_build_legs()` assigns `leg1 = polymarket` and `leg2 = kalshi`.

So the actual execution order is: **Polymarket first, Kalshi second** — which *matches* the docstring. This is architecturally sound: FOK-first minimizes legging risk. **However**, the unwind logic assumes Leg 1 was on Polymarket (see `_unwind()`), and the unwind fee calculation always calls `kalshi_taker_fee()` for both the original leg and the unwind sell:

```python
leg1_fee = kalshi_taker_fee(leg1.price, contracts_to_unwind)
unwind_fee = kalshi_taker_fee(unwind_sell.price, contracts_to_unwind)
```

When Leg 1 is a Polymarket order, the unwind sell also happens on Polymarket — but the fee is calculated using the **Kalshi fee formula**. This is incorrect and underestimates the realized unwind loss for all trades where Leg 1 is on Polymarket (which is every trade, per the current execution order).

### 4. `events.json` Ships with `FILL_ME` Placeholder Tokens

**Severity: HIGH (Operational)**

Both events in `events.json` have `"poly_yes_token": "FILL_ME"`. The code correctly validates against this at startup and refuses to run, which is good defensive design. However, shipping production config with placeholder values increases the risk of a copy-paste deployment error. There are no instructions in `README.md` explaining that `ticker_return.py` must be run first to populate these fields.

### 5. Sports Markets: 3-Second Matching Delay Not Handled

**Severity: HIGH**

Per Polymarket's [order lifecycle documentation](https://docs.polymarket.com/concepts/order-lifecycle), sports markets have a **3-second matching delay** — marketable orders receive `"status": "delayed"` and sit for 3 seconds before matching or being placed on the book as `"unmatched"`. The bot handles the `"delayed"` status in `_submit_poly_buy()` with a polling loop, which is good. However:

- The 3-second delay means the quoted price may have moved by the time the match occurs.
- There is no re-verification of the spread after the delay.
- During the 3-second delay window, the bot has already committed Leg 1 (Polymarket) as filled (FOK returned `"delayed"`, bot enters poll loop). If the Poly FOK ultimately returns `"unmatched"`, the bot correctly treats it as a failure. But this creates a 3-second window where the bot believes it has a pending position when it may not.

---

## Execution & Architecture Risks

### 1. Race Condition: `asyncio.create_task(execution.submit(opp))` Fire-and-Forget

**Severity: HIGH**

In `_process_updates()` (line ~4644), arb submissions are dispatched as fire-and-forget tasks:

```python
asyncio.create_task(execution.submit(opp))
```

If two WS ticks arrive for the same event within microseconds (e.g., Kalshi and Poly both update), two `create_task` calls race. The `_traded` set provides protection (checked at the top of `submit()`), and the key is added before `await`. However, there is a subtle window: between the `if key in self._traded` check and the `self._traded.add(key)` call, two concurrent tasks could both pass the check before either adds the key. In practice this is mitigated because `self._traded.add(key)` occurs before `await`, but the entire `submit()` method should be wrapped in an `asyncio.Lock` per event to guarantee mutual exclusion.

### 2. Sequential Leg Execution Creates Latency Exposure

**Severity: MEDIUM**

The bot executes Leg 1 (Polymarket FOK) → waits for confirmation → executes Leg 2 (Kalshi IOC) sequentially. Total latency is at least:

- Polymarket FOK: ~100-500ms REST round-trip + potential 3s sports delay
- Kalshi IOC: ~50-200ms REST round-trip

During this 150ms-3.5s window, the Kalshi price may have moved. The bot submits Kalshi at the *snapshot* price, not the *current* price. If the Kalshi ask has moved up by even 1 cent, the IOC may partially fill or not fill at all, triggering an unwind.

**Recommendation:** After Leg 1 fills, re-fetch the Kalshi orderbook to verify the price hasn't moved before submitting Leg 2. Alternatively, add a configurable price tolerance (e.g., accept prices up to 2 cents worse than snapshot).

### 3. CSV File I/O is Synchronous and Blocking

**Severity: MEDIUM**

All CSV writes (`PaperTrader.log()`, `OrderLogger.log()`, `PaperPortfolio._write_row()`, `UnwindLogger.log()`) use synchronous `open()` + `csv.DictWriter()` inside the async event loop. Under load, file I/O can block for milliseconds, stalling WS message processing and introducing latency in the arb detection path.

**Recommendation:** Use `asyncio.to_thread()` for CSV writes, or migrate to SQLite with WAL mode (as noted in the scope.md Phase 6 roadmap).

### 4. No Atomic Lock Between Legs — Partial Capital Deduction

**Severity: MEDIUM**

Capital is only deducted when `portfolio.open_position()` is called after *both* legs fill. If the bot crashes between Leg 1 filling and Leg 2 completing, the position is not recorded in the portfolio. On restart, capital appears available but a real position exists on the exchange. There is no reconciliation mechanism to detect orphaned positions on either platform at startup.

### 5. Unbounded Reconnect Recursion

**Severity: LOW-MEDIUM**

Both `KalshiWS._schedule_reconnect()` and `PolymarketWS._schedule_reconnect()` call `_do_connect()`, which on failure calls `_schedule_reconnect()` again. This is recursive via `await`. In a sustained outage (hours), the reconnect attempt counter grows without bound but the delay caps at `WS_RECONNECT_MAX` (30s). While this won't overflow the stack (each call awaits and returns), it does create a task chain that can accumulate if background tasks are not properly cancelled. A maximum reconnect count before entering a "degraded" state would be prudent.

### 6. `py_clob_client` Is Synchronous — Thread-Safety Concern

**Severity: MEDIUM**

The `PolymarketOrderClient` wraps `py_clob_client` (synchronous `requests`-based) in `asyncio.to_thread()`. The `_order_lock` serializes `create_order + post_order` to prevent nonce collisions. However, `get_order()` and `cancel_order()` are NOT serialized with this lock. If a get or cancel runs concurrently with a create on the same thread pool, `py_clob_client`'s internal state (session, nonce tracking) may not be thread-safe.

---

## API Discrepancies

### 1. Kalshi Rate Limits: Repo Is Conservative But Documents Outdated Tier Names

**Severity: LOW**

The repo sets `KALSHI_MAX_RPS = 5.0` and comments "Basic tier: 20 reads/sec". Per [current Kalshi documentation](https://docs.kalshi.com/getting_started/rate_limits):

| Tier | Read Limit | Write Limit |
|---|---|---|
| Basic | 20/sec | 10/sec |
| Advanced | 30/sec | 30/sec |
| Premier | 100/sec | 100/sec |
| Prime | 400/sec | 400/sec |

The bot's 5 RPS for reads is safely under even Basic tier (20/sec). For writes (order placement), the bot's 5 RPS is under the Basic write limit (10/sec). This is conservative and correct. No issue here.

### 2. Polymarket Rate Limits: Dramatically Under-Utilized

**Severity: LOW**

The repo sets `POLY_MAX_RPS = 10.0` and comments "Polymarket /price: 150 reads/sec". Per [current Polymarket documentation](https://docs.polymarket.com/api-reference/rate-limits):

| Endpoint | Limit |
|---|---|
| CLOB General | 9,000 req/10s (900/s) |
| /book | 1,500 req/10s (150/s) |
| /price | 1,500 req/10s (150/s) |
| POST /order | 3,500 req/10s (350/s) |

The bot's 10 RPS is extremely conservative. This is fine for safety but means the bot cannot react quickly if it needs to burst multiple REST calls (e.g., fetching snapshots for 10+ markets after a reconnect). The comment should note the actual limit is 150/s for /book and /price.

### 3. Kalshi Orderbook API: `depth` Parameter Semantics

**Severity: LOW**

The bot passes `{"depth": 5}` to `GET /markets/{ticker}/orderbook`. Per Kalshi's API, the orderbook endpoint returns `yes` and `no` arrays of `[price_cents, quantity]`. The depth parameter limits the number of price levels. Using depth=5 is reasonable but limits visibility into true available liquidity. For arb sizing, this means `max_contracts` may be underestimated if liquidity is spread across more than 5 price levels.

### 4. Kalshi Fee: `fee_type: "quadratic"` and `fee_multiplier` Are Dynamic

**Severity: MEDIUM**

The repo hardcodes `KALSHI_FEE_RATE = 0.07` and references a static PDF. However, Kalshi exposes a dynamic `GET /series/fee_changes` endpoint that returns `fee_type` and `fee_multiplier` per series. The fee rate can change over time per series. Hardcoding 7% may be incorrect for series with different or recently changed rates.

**Recommendation:** Query `GET /series/fee_changes?series_ticker=...` at startup (or per-market) and use the returned `fee_multiplier` dynamically rather than a global constant.

### 5. Polymarket `signature_type` Not Specified

**Severity: MEDIUM**

The `PolymarketOrderClient` initializes `ClobClient` without specifying `signature_type` or `funder`:

```python
self._client = ClobClient(
    host=CLOB_BASE,
    key=key,
    chain_id=POLY_CHAIN_ID,
    creds=creds,
)
```

Per [py_clob_client documentation](https://github.com/Polymarket/py-clob-client), if the private key holder is not the same address that holds funds (e.g., email/Magic wallets or proxy wallets), `signature_type` and `funder` must be specified. The default (`signature_type=0`, no funder) works only for EOA wallets that directly hold funds. If the user has a proxy wallet setup, orders will silently fail signature validation.

### 6. Polymarket Sports Market: Auto-Cancellation at Game Start

**Severity: MEDIUM**

Per [Polymarket docs](https://docs.polymarket.com/concepts/markets-events): *"Specifically for sports markets, outstanding limit orders are automatically cancelled once the game begins, clearing the order book at the official start time."*

The bot does not detect or handle this scenario. If the bot places a GTC or resting order near game start, it may be auto-cancelled. More critically, if the bot detects an arb opportunity just before game start, the quoted depth may vanish within seconds. The 3-second sports matching delay exacerbates this: an order placed 2 seconds before game start could be auto-cancelled during the delay window.

---

## Missing Edge Cases

### 1. No Withdrawal Fee / Transfer Cost Accounting

The bot calculates per-trade P&L as `$1.00 - cost - fees`. But it does not account for:
- **USDC transfer costs** to/from each platform
- **Polygon gas fees** for Polymarket settlement (even if minimal, they exist for token approvals, and failed transactions still consume gas)
- **Kalshi withdrawal fees** (if any, for moving profits out)
- **Cross-platform capital rebalancing**: if all profits accumulate on one platform and capital runs out on the other, the bot cannot trade until funds are manually moved. There is no alerts or detection for this imbalance.

### 2. No Liquidity Floor / Minimum Depth Threshold

The FOK depth check (`_fillable_contracts()`) correctly ensures `min(kalshi_depth, poly_depth) >= desired_contracts`. However, there is no minimum absolute depth threshold. If both sides show 1 contract of depth, the bot will attempt a 1-contract trade. At `$0.50` per side, the gross profit on a 5% spread is `$0.05` on 1 contract. After fees (~$0.02-0.04), the net profit is effectively zero or negative. A `MIN_DEPTH` threshold (e.g., 10 contracts) would prevent economically pointless trades.

### 3. No Price Staleness Guard Per-Leg

The bot has a global stale-data watchdog (reconnect if no WS update in 30s). However, it does not check staleness *per market*. Market A may be actively updating while Market B's orderbook hasn't changed in 25 seconds. The bot would still attempt to arb Market B using potentially stale prices that are just under the global timeout. A per-market staleness check (e.g., reject arbs where either leg's last update is > 5 seconds old) would be more robust.

### 4. Resolution Discrepancy Between Platforms

The bot assumes a `$1.00` payout per contract on resolution, which is correct *if both platforms resolve the same way*. However:
- Kalshi and Polymarket use **different resolution sources and criteria**. A game outcome could be disputed or delayed on one platform but not the other.
- If one platform resolves YES and the other resolves NO (edge case: controversial call, rule difference), the bot loses both legs instead of winning one.
- There is no monitoring for resolution disputes, delayed resolutions, or voided/cancelled markets where the payout is refund at cost rather than $1.00.

### 5. No Circuit Breaker for Consecutive Unwinds

The `DailyPnLTracker` trips on cumulative daily losses, but there is no **consecutive unwind** circuit breaker. If 5 trades in a row all trigger unwinds (suggesting a systematic issue like stale data, connectivity problems, or exchange outage on one side), the bot continues attempting trades. A "consecutive failure" breaker (e.g., halt after 3 unwinds in 5 minutes) would catch systematic issues faster.

### 6. Token Allowance Not Verified at Startup

For Polymarket live trading, the ERC-20 allowance for USDC and conditional tokens must be set for the Exchange contracts. The bot does not verify allowances at startup. If allowances are insufficient, the first trade will fail with an opaque on-chain error, and the unwind path may also fail for the same reason, leaving the Kalshi leg unreversed.

### 7. Kalshi Exchange Hours / Maintenance Windows

Kalshi has defined trading hours and maintenance windows (accessible via `GET /exchange/schedule`). The bot does not check whether the exchange is open before attempting trades. Orders placed during maintenance or outside trading hours will be rejected, potentially triggering spurious unwind logic.

### 8. No Position-Level P&L Tracking After Fill

Once a position is opened, the bot does not track its mark-to-market P&L. If prices move drastically against the arb (e.g., one leg's market is halted while the other continues), there is no alerting or exit mechanism. The position sits until resolution, which could be days for non-sports events.

### 9. Single-Event Queue Flooding

The `asyncio.Queue` (maxsize=10,000) can be flooded by a single highly-active market. Every WS price update (bid/ask change at any level) triggers a queue entry. In a fast-moving sports game, hundreds of updates per second are common. The bot processes one event at a time from the queue. By the time it processes an update for Market B, the data may be stale because Market B's entry was queued behind 500 Market A entries. An event-coalescing mechanism (e.g., per-event latest-snapshot, not FIFO queue) would reduce this risk.

---

## Community Insights

### Landscape Overview

A GitHub search reveals **112+ repositories** for "polymarket kalshi arbitrage." The ecosystem ranges from:
- **Unified API wrappers** like `pmxt-dev/pmxt` (830 stars) — a CCXT-style library for prediction markets
- **Full arb bots** like `ImMike/polymarket-arbitrage` (51 stars, 10K+ markets), `CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot` (196 stars)
- **Market-making** hybrids like `solship/Polymarket-Kalshi-Arbitrage-Trading-Bot` (172 stars)

### This Repo vs. Community Standard

| Feature | This Repo | Community Standard |
|---|---|---|
| Architecture | Event-driven WS + asyncio | Most use REST polling loops |
| Precision | Decimal arithmetic | Rare — most use floats |
| Fee handling | Hardcoded global rate | Best repos query per-market |
| Unwind / rollback | Full sequential + rollback | Rare — most accept legging risk |
| Market discovery | Manual (events.json) | Some auto-discover via fuzzy matching |
| Execution | Polymarket-first (FOK), Kalshi-second (IOC) | Varies; some do simultaneous |
| Risk management | Daily limits, kill switch, rate limiting | Rare in open-source bots |

### Strengths Relative to Community

1. **Decimal precision** — the use of `decimal.Decimal` throughout is rare and prevents the `0.1 + 0.2 ≠ 0.3` class of bugs that plague float-based bots.
2. **Event-driven architecture** — most community bots use `sleep()`-based polling, adding hundreds of milliseconds of latency. This repo reacts on every WS tick.
3. **Unwind manager** — the sequential two-leg execution with automatic rollback is the most sophisticated open-source implementation found. Most repos either accept legging risk or don't implement live execution.
4. **Explicit token mapping** — requiring `poly_yes_token` / `poly_no_token` in `events.json` (not runtime API resolution) prevents the catastrophic "inverted trade" bug where YES/NO tokens are swapped.
5. **Kill switch, daily loss limits, stuck position monitor** — production-grade risk management that is absent from virtually all community implementations.

### Weaknesses Relative to Community

1. **No auto-discovery** — many community bots scan hundreds or thousands of markets. This repo requires manual event config via `ticker_return.py`. The scope.md Phase 5 roadmap addresses this.
2. **No multi-level orderbook walk** — the bot only checks best-ask depth for sizing. More advanced implementations walk multiple price levels to find the optimal trade size that maximizes total profit.
3. **No database backend** — CSV storage limits queryability and concurrent access. SQLite or PostgreSQL is standard for production bots (roadmap Phase 6).
4. **No latency optimization** — some Rust-based community bots (e.g., `TopTrenDev/polymarket-kalshi-arbitrage-bot`) achieve sub-millisecond processing. Python + asyncio adds overhead.
5. **Fee-per-market querying** — as noted above, per-market fee rates should be fetched dynamically per Polymarket's recommendation.

### Notable Community Pattern: Simultaneous Execution

Some community implementations submit both legs simultaneously rather than sequentially. This eliminates the latency exposure between legs but introduces a different risk: both legs may partially fill, leaving two half-positions that are hard to unwind atomically. The sequential approach in this repo is the more conservative and generally recommended pattern for arb bots.

---

## Summary of Recommendations (Priority Order)

| Priority | Finding | Action |
|---|---|---|
| P0 | Polymarket fee formula wrong (exponent, rate) | Implement per-market fee fetching via `/fee-rate` endpoint; support exponent parameter |
| P0 | Unwind fee calculation uses wrong platform formula | Use platform-appropriate fee function based on `leg.platform` |
| P1 | Sports 3-second delay + auto-cancel at game start | Add game-start detection; avoid arbs within N seconds of scheduled start |
| P1 | No startup reconciliation for orphaned positions | On startup, query open positions on both platforms and reconcile with portfolio state |
| P1 | Token allowance not verified | Check ERC-20 allowances at startup in live mode |
| P2 | CSV I/O blocks event loop | Migrate to `asyncio.to_thread()` or SQLite WAL |
| P2 | Per-market staleness check | Track last-update timestamp per market, reject arbs with stale legs |
| P2 | Consecutive unwind circuit breaker | Halt after N unwinds in M minutes |
| P2 | Kalshi fee rate hardcoded | Query `/series/fee_changes` dynamically |
| P3 | Queue flooding from active markets | Implement per-event coalescing (latest snapshot, not FIFO) |
| P3 | No minimum depth threshold | Add `MIN_DEPTH` config to avoid sub-economic trades |
| P3 | No cross-platform capital balance monitoring | Alert when one platform's balance drops below threshold |
