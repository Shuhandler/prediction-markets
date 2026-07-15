# Correctness Review — Progress Tracker

Systematic module-by-module review of the codebase for correctness bugs.
Started and completed 2026-07-13. Reviewer: Claude Code.

Focus: correctness only (logic errors, race conditions, unit/sign errors, Decimal
violations, API-contract mismatches, state-machine bugs). Not style, not performance
unless it causes wrong behavior.

Severity: **P0** = can lose money / corrupt state; **P1** = wrong behavior in realistic
scenarios; **P2** = wrong in edge cases / latent; **P3** = cosmetic / audit-trail only.

## Status

| # | Module (arb_bot.py lines) | Status | Findings |
|---|---------------------------|--------|----------|
| 1 | Config, env parsing, helpers, fees (1–450) | ✅ done | none |
| 2 | Data models & enums (452–641) | ✅ done | none |
| 3 | RateLimiter + MarketFetcher (641–1150) | ✅ done | none |
| 4 | KalshiOrderClient (1150–1434) | ✅ done | F2, F9 |
| 5 | PolymarketOrderClient (1434–1689) | ✅ done | none |
| 6 | _LocalOrderbook (1689–1773) | ✅ done | F1 |
| 7 | PolymarketWS (1773–2160) | ✅ done | F1 (impact) |
| 8 | KalshiWS (2160–2712) | ✅ done | F5 |
| 9 | ArbEngine (2712–2847) | ✅ done | none |
| 10 | PaperTrader / ObservationLogger / WSRecorder (2847–3097) | ✅ done | none |
| 11 | Position + StateStore (3097–3352) | ✅ done | F7 |
| 12 | PaperPortfolio + OrderLogger (3352–3632) | ✅ done | F7, F10 |
| 13 | DailyPnLTracker / OrderRateLimiter / RiskManager (3632–3839) | ✅ done | F12 |
| 14 | UnwindLogger + UnwindManager: legs, execute, orphans (3839–4500) | ✅ done | F3, F8, F11 |
| 15 | UnwindManager: submit/sell/reconcile paths (4500–5194) | ✅ done | F2, F4, F5 |
| 16 | DiscordNotifier + HealthServer + balance monitor (5194–5763) | ✅ done | none |
| 17 | StuckPositionMonitor + ExecutionEngine (5763–6279) | ✅ done | F3, F8 |
| 18 | main() wiring, _process_updates, resolution checker (6279–7084) | ✅ done | F6 |
| 19 | discover.py | ✅ done | none |
| 20 | ticker_return.py | ✅ done | none |

## Post-review repairs (2026-07-14)

An xhigh-effort /code-review of the F1–F11 fix batch (10 finder angles →
verification → gap sweep) produced 15 verified findings. The repair-scope
items — defects in or incompleteness of this session's own fixes — are now
implemented and pinned by 10 additional regression tests (suite: 160 passing):

| Review finding | Repair |
|---|---|
| S2: WS fill counter destroyed before REST-poll fallback | Waiter cleanup moved after the poll; live `_fill_counts` re-read so a failed poll uses the freshest count |
| C1: out-of-order PortfolioMeta commits | `PaperPortfolio._persist` — a lock serializes StateStore writes with the snapshot taken inside it (last write always carries latest meta) |
| C2: partial-fill exits lacked the defensive cancel | `_cancel_kalshi_remainder` helper applied to partial AND no-fill exits of buy/sell, gated on non-terminal order status |
| C8: cancel response discarded | Helper parses `remaining_count` from the cancel response and absorbs revealed fills into the leg |
| C9: unguarded `int(remaining_count)` at 3 sibling sites | `_int_or` helper used at all four parse sites |
| C11: malformed `price_changes` entries tore down the WS | isinstance filter on entries + hardened `apply_price_change_poly` (missing/garbage fields dropped) |
| S1: garbled-body errors skipped client_order_id reconciliation | Reconciliation now runs on ANY ambiguous error; only clean 4xx rejects skip it |
| C13: except-after-booking released the dedup key | `booked` guard: post-booking failures keep the key and report the booked quantity; `open_position` no longer propagates persist failures after mutating |
| C6: has_positions race with in-flight executions | Checker re-scans open positions every cycle against removed events; all-settled shutdown deferred while executions are in flight or stragglers exist |
| C4 (partial): no alert on never-settling entries | 4-hour stuck-pending ERROR alert per event (liveness taxonomy fix deferred, see below) |
| C12: stale dedup-key invariant docs | CLAUDE.md sentence + `submit()` docstring updated to state the paired-retention exception |

**Deferred — design decisions for the user** (behavior/architecture trade-offs):
1. **C3**: void/scalar Kalshi settlements still credit $1.00/contract — needs a
   payout model keyed on the actual `result`/settlement value.
2. **C5**: pending settlement is not restart-safe — needs `kalshi_ticker`
   persisted on position rows in state.db and startup re-enrollment.
3. **C7**: Leg-2 reprice is unbounded (only net>0) — executed outlay can exceed
   the reservation; needs a reprice cap policy.
4. **S3**: paired-contract booking lives in ExecutionEngine and misses the
   force-unwind-after-cancellation path — deeper home is UnwindManager._finish.
5. Cleanup batch (verified, unapplied): shared Kalshi status classifier,
   shared outlay helper, Poly schema normalizer, module-level except tuple,
   orders.csv paired-row outlay semantics, redundant step-5 poll on
   WS-confirmed full fills.

## Fix status (2026-07-13)

F1–F11 implemented and covered by regression tests in
`tests/test_review_fixes.py` (21 tests; full suite 150 passing, no network).
F12 left as-is: `tests/test_clock.py::test_pnl_accumulates_within_day_only`
deliberately pins the profit-offset behavior — changing it is a product
decision, not a bug fix.

| Finding | Status | Where fixed |
|---------|--------|-------------|
| F1 | ✅ fixed | `apply_snapshot_poly` accepts bids/asks AND buys/sells; `PolymarketWS._handle_message` accepts both price_change schemas |
| F2 | ✅ fixed | `time_in_force="immediate_or_cancel"` everywhere; defensive `cancel_order()` on all Kalshi no-fill paths |
| F3 | ✅ fixed | `ExecutionEngine._book_position` books at executed leg prices |
| F4 | ✅ fixed | `_sell_kalshi` returns False on partial fill (matches `_sell_poly` contract) |
| F5 | ✅ fixed | REST poll authoritative; WS fill count is a floor; waiter always cleaned up |
| F6 | ✅ fixed | `check_kalshi_settled()` + pending-settlement queue in `_resolution_checker` |
| F7 | ✅ fixed | `PortfolioMeta` frozen snapshot taken on the event loop |
| F8 | ✅ fixed | non-COMPLETE branch books paired contracts, keeps dedup key when paired > 0 |
| F9 | ✅ fixed | `ValueError` caught in all six KalshiOrderClient methods |
| F10 | ✅ fixed | pnl formatted as `(-loss).quantize(Q4)` |
| F11 | ✅ fixed | zero-fill unwind → FAILED, not COMPLETE |
| F12 | ⏸ open | deliberate design pin; needs a product decision (consider a separate losses-only breaker accumulator) |

Still requiring live verification (cannot be confirmed offline):
1. Grep a VPS `data/ws_capture/polymarket-*.jsonl.gz` for `"bids"` vs `"buys"`
   in book events — confirms which schema production actually sends (code now
   handles both either way).
2. Probe Kalshi with a 1-contract `immediate_or_cancel` order on the legacy
   endpoint before the next live session; plan migration to
   `/portfolio/events/orders` (legacy endpoint past its announced deprecation
   window).

## Findings

### F1 (P0) — Polymarket WS `book` parser reads `buys`/`sells`; current schema is `bids`/`asks`
`_LocalOrderbook.apply_snapshot_poly` (arb_bot.py:1700–1711) clears the book then
reads `data.get("buys")`/`data.get("sells")`. Per current Polymarket market-channel
docs, `book` events carry **`bids`/`asks`** — so every book snapshot **wipes the local
book and inserts nothing**. Books are then rebuilt solely from incremental
`price_change` events: resting levels that predate subscription (or never change) are
permanently invisible, and when a visible best level is consumed the book can appear
empty. Impact compounds at cold start: `_fetch_initial_snapshots` is a no-op on the
first connect (token map is populated by `subscribe()` *after* `connect()`), so the
discarded WS snapshot is the *only* initial book source. On reconnect, the
re-subscription's book event races with (and can wipe) the REST snapshot load.
Evidence the code targets the wrong schema era: the `price_change` handler
(arb_bot.py:2018–2023) matches the *current* batched schema (`price_changes` array
with nested `asset_id`) exactly. Consequences: missed/skipped arb detection AND
systematically degraded `observations.csv` — the Phase 7 economics gate is analyzing
wrong Polymarket book data. Tests can't catch it: `tests/` mocks the same
`buys`/`sells` shape.
**Verify:** inspect `data/ws_capture/polymarket-*.jsonl.gz` on the VPS for the actual keys.
**Fix:** accept both key sets (`data.get("bids") or data.get("buys")`, same for asks/sells),
mirroring what `_parse_book_tob` already does for REST.

### F2 (P0, live) — Kalshi `time_in_force: "ioc"` is not a documented enum value
`KalshiOrderClient.place_order` (arb_bot.py:1266) sends `time_in_force: "ioc"`.
Kalshi's documented enum is `fill_or_kill` / `good_till_canceled` /
`immediate_or_cancel` — `"ioc"` appears nowhere. Two failure modes: (a) API rejects
with 400 → every Leg 2 fails → constant Leg-1 buy-then-unwind churn (visible, costly);
(b) the value is ignored → the order rests as GTC, but `_submit_kalshi_buy`
(arb_bot.py:4644–4651) treats "no immediate fill" as IOC-cancelled and **never cancels
the resting order** → it can fill minutes later, creating an untracked naked position.
Same issue in `_sell_kalshi` (arb_bot.py:4875). Could not verify the legacy endpoint's
tolerance offline (its doc pages now 404). Related operational risk: the legacy
`/trade-api/v2/portfolio/orders` endpoint was slated for deprecation "no earlier than
May 6, 2026" (already past) in favor of `/portfolio/events/orders` (different schema:
single fixed-point-dollar `price`, `side: bid|ask`).
**Fix:** use `"immediate_or_cancel"`; defensively `cancel_order()` on the no-fill path;
plan the endpoint migration; verify with a 1-contract order before next live session.

### F3 (P1, live) — Position booked at stale prices after Leg-2 price refresh
The spread re-verification updates `leg2.price = fresh_ask` (arb_bot.py:4127) and Leg 2
executes at that price, but `ExecutionEngine._execute_gated` books the position with
`order_outlay(actual)` built from **`opp.total_cost` / `opp.kalshi_price`** — the
original snapshot prices (arb_bot.py:6124–6137). Actual Kalshi spend is higher on every
re-priced trade, so `state.db` capital drifts upward vs. reality. Inconsistent with the
P&L tracker, which correctly uses `uw_order.net_profit_per_contract` (re-verified).
**Fix:** recompute outlay from the executed leg prices (`leg1.price + leg2.price`).

### F4 (P1, latent) — `_sell_kalshi` treats partial unwind fill as full success
arb_bot.py:4888–4909: any `filled > 0` → `LegStatus.UNWOUND`, `return True`. Callers
treat `ok=True` as fully flat: `_unwind` (4412) sets UNWOUND without orphaning the
remainder; `retry_orphaned_unwinds` (4322) pops the orphan. Remaining exposure is
silently dropped. Contrast `_sell_poly`, which correctly returns False on partial and
lets the remainder be orphaned. **Currently unreachable** (Leg 1 is always Polymarket,
so unwind sells are always `_sell_poly`), but this is a loaded trap for the IBKR/venue
work or any leg-ordering change.
**Fix:** return False (and don't mark UNWOUND) when `filled < leg.contracts`.

### F5 (P2, live) — Kalshi WS fill-waiter can under-count multi-fill IOC orders
The fill handler resolves the waiter future on the **first** fill message with the
count at that moment (arb_bot.py:2476–2482); later fills for the same order can't
update a done future. `_submit_kalshi_buy` returns immediately when `ws_filled > 0`
(4616–4626) with **no final REST poll**. If an IOC matches multiple resting orders and
fills stream across WS messages, `leg.filled_contracts` under-counts → the engine
"unwinds excess" that isn't excess → sells back too many Poly contracts → naked Kalshi
exposure. Only reachable when the REST response was ambiguous (no immediate fill
info), but the consequence is unhedged exposure.
**Fix:** after the waiter resolves, short settle delay + `get_order()` REST confirm of
the final count (or only resolve the future when `filled_count == contracts` / on timeout).

### F6 (P2, live) — Resolution checker credits $1.00/contract when EITHER platform reports closed
`_resolution_checker` (arb_bot.py:6476–6479) closes positions when `not k_active or
not p_active`; `close_positions_for_event` pays $1/contract unconditionally. Issues:
(a) Kalshi `"closed"` means trading halted, not settled — payout credited early;
(b) void/disagreeing resolutions don't pay $1; (c) one-sided closure settles both
legs' accounting. Acceptable paper simplification, but it also runs in live mode where
`state.db` is the trusted capital record and is never reconciled against actual
exchange settlement amounts.
**Fix (live):** credit on Kalshi `settled`/`result` only, and/or reconcile against
`get_balance()` deltas.

### F7 (P2) — persist-time race: StateStore writes read the live portfolio object in a worker thread
`persist_open`/`persist_close`/`persist_meta` (arb_bot.py:3216–3242) read
`portfolio.capital` etc. at write time via `asyncio.to_thread`, while the event loop
may mutate the portfolio for a concurrent trade. A crash in the window can leave
`state.db` with capital that includes trade B's deduction but no trade B position row
(money vanishes from state on restart — conservative direction, but drift).
**Fix:** snapshot the values on the event loop and pass primitives to the thread.

### F8 (P2) — UNWOUND path assumes zero paired contracts
`_execute_gated` (arb_bot.py:6105–6111) books `filled_contracts = 0` and opens no
Position for UNWOUND trades. But `_unwind` supports a *partial* unwind
(`contracts_to_unwind = leg1.filled − leg2.filled`, arb_bot.py:4362–4365): reachable
via the generic-exception path after Leg 2 partially fills, or `force_unwind` on an
execute() cancelled mid-flight. Those paired contracts are real exposure on both
exchanges with no Position, no capital deduction, and a released dedup key.
**Fix:** in the UNWOUND branch, open a Position for `min(leg1.filled, leg2.filled)`.

### F9 (P3) — KalshiOrderClient JSON parse can raise on non-JSON error bodies
`resp.json(content_type=None)` (arb_bot.py:1283, 1321, 1346, 1372, 1395, 1419) raises
`json.JSONDecodeError` on empty/HTML bodies (gateway 502 pages); only
`aiohttp.ClientError`/`TimeoutError` are caught. Propagates into execution/
reconciliation paths where it's misclassified as an unexpected error.
**Fix:** catch `ValueError` too and return `{"error": True, ...}`.

### F10 (P3, latent) — `record_unwind_loss` CSV formats pnl as `f"-{loss}"`
arb_bot.py:3538 — a negative loss would render `"--0.05"`. Currently impossible
(price floors keep sell ≤ buy so loss ≥ fees), but fragile if floors change.

### F11 (P3) — force-unwinding an order with zero fills marks it COMPLETE
`_unwind` (arb_bot.py:4367–4370): `contracts_to_unwind <= 0` → `UnwindStatus.COMPLETE`.
A stuck order that never filled anything gets a COMPLETE row in unwinds.csv and
inflates `complete_count`. Stats/audit pollution only.

### F12 (P3, design note) — Circuit breaker offsets realized losses with unrealized expected profits
Every fill records its *expected* net as positive daily P&L immediately
(arb_bot.py:6144 → 3669). On a busy day the breaker trips only when realized unwind
losses exceed `DAILY_LOSS_LIMIT` **plus** accumulated expected profits, weakening the
intended guarantee. Deliberate per docstring, but worth a conscious re-decision.
(The old audit's "consecutive-unwind breaker" suggestion remains unimplemented and
would compensate.)

## Cross-check vs. prior audit (audit_arb_report.md, 2026-02-25)

Fixed since: Poly fee exponent/rate ✅ (configurable, quadratic), unwind fee uses
platform-correct formula ✅ (`_record_unwind_loss_for`), 4-dp poly fee rounding ✅,
sports 3s-delay handling ✅ (delayed-poll + cancel + re-check + spread re-verify),
submit race ✅ (per-event locks), CSV blocking ✅ (to_thread), startup reconciliation ✅
(alert-only), allowance check ✅ (advisory), queue flooding ✅ (burst coalescing +
dedupe), reconnect recursion ✅ (iterative), SQLite state ✅, balance monitoring ✅.

Still open from prior audit (unchanged, lower priority): per-market staleness check
(watchdog is per-connection only), consecutive-unwind circuit breaker, minimum-depth
economic threshold, resolution-source divergence between platforms (partially = F6).

## Verification notes

- Polymarket market-channel schema confirmed against docs.polymarket.com (book =
  `bids`/`asks`; price_change = `price_changes[]` with nested `asset_id`).
- Kalshi `time_in_force` enum confirmed against docs.kalshi.com CreateOrder (V2);
  legacy endpoint docs are 404 (deprecation), so F2 mode (a)-vs-(b) needs a live probe.
- Decimal thread-context concern checked: all Decimal *arithmetic* happens on the event
  loop; worker threads only do `str()` conversion — no context issue.
- Tests mock the same WS shapes as the code (tests/mocks.py, test_orderbook.py), so
  CI structurally cannot catch F1/F2-class API-contract drift.
