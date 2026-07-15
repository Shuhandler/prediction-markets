# Production Readiness Roadmap — Prediction Market Arb Bot

> **Current state**: v6.0 — event-driven WS architecture, Decimal precision,
> Fill-or-Kill execution, OMS, live order execution with unwind protection,
> SQLite state of record, and 24/7 deployment infrastructure.
>
> **Goal**: Transform into a production-ready live-trading system.
>
> **Execution order (revised 2026-07-09)**: Phases 0–6 are done. The remaining
> phases were reordered to **7 Economics Gate → 8 Auto Discovery** (testing
> and deployment were previously last, discovery first). Rationale: discovery
> multiplies any latent bug across hundreds of markets, 24/7 paper deployment
> generates the ledger data the go/no-go analysis needs, and discovery is only
> worth building if that analysis says the edge exists.  See also the
> **Code-Review Follow-ups** section below for open correctness/hardening
> decisions from the 2026-07 reviews.
>
> **Parallel track**: ForecastEx/IBKR venue expansion (econ markets,
> near-zero fee floor, coupon carry) is planned separately in
> [IBKR_ROLLOUT.md](IBKR_ROLLOUT.md) — phases FX-A…FX-D, feeding the same
> Phase 7 economics gate.

---

## Phase 0 — Housekeeping

_Do first — unblocks everything else._

- [x] Fix `requirements.txt`: add `cryptography` and `python-dotenv` (both already imported but missing from deps)
- [x] Create `.env` file with all tunable params currently hardcoded in `arb_bot.py` lines 90–142: `MIN_PROFIT_MARGIN`, `STARTING_CAPITAL`, `POSITION_SIZE`, `KALSHI_FEE_RATE`, `POLY_FEE_RATE`, `STALE_TIMEOUT_SECONDS`, API base URLs, log level
- [x] Add `argparse` CLI overrides for `--dry-run`, `--log-level`, `--events-file`, `--paper` / `--live`
- [x] Move `EVENTS` list out of source into `events.json` or `events.yaml`
- [x] Fix Polymarket end-date resolution bug (line 563 — commented-out check falsely marking live games as resolved)
- [x] Add `SIGTERM` handler for graceful Docker shutdown alongside existing `KeyboardInterrupt` (line 2545)
- [x] Cap `_orders` list growth in `ExecutionEngine` (line 2055) — flush to CSV and clear periodically

---

## Phase 1 — Risk Management & Daily Loss Limits

- [x] Add `DailyPnLTracker` class: tracks realized + unrealized P&L per calendar day, persists to CSV/SQLite
- [x] Add `DAILY_LOSS_LIMIT` config param (e.g. `$100`); circuit-breaker halts all new orders when breached
- [x] Add `MAX_TOTAL_EXPOSURE` cap across all open positions
- [x] Add `MAX_CONCURRENT_POSITIONS` limit
- [x] Add spread sanity check in `ArbEngine.check()`: reject spreads > 20% as likely stale/bad data
- [x] Add order submission rate limiter (max N orders/minute) to prevent runaway execution
- [x] Add file-based kill switch (`touch KILL` to halt) for emergency remote stop

---

## Phase 2 — Unwind Module

_Critical for live trading — builds on Phase 1 risk management._

- [x] New `UnwindManager` class with sequential execution + rollback:
  - Submit Leg 1 → await fill confirmation → Submit Leg 2 → await fill confirmation
  - If Leg 2 fails/times out: immediately sell Leg 1 at market to unwind
  - If Leg 1 partially fills: adjust Leg 2 size to match, or unwind partial
- [x] Add `UNWIND_TIMEOUT_SECONDS` config (e.g. 5s) — if no fill confirmation within timeout, cancel + unwind
- [x] Add `StuckPositionMonitor`: background task that detects half-filled arbs older than N seconds and auto-unwinds
- [x] Integrate with Discord alerts (Phase 4) — every unwind triggers a notification
- [x] Add `UnwindOrder` type to OMS with its own lifecycle tracking

---

## Phase 3 — Live Order Execution

- [x] **Kalshi**: Build authenticated REST order client using existing RSA signing. Endpoint: `POST /trade-api/v2/portfolio/orders`
- [x] **Polymarket**: Add `py_clob_client` dependency. Initialize `ClobClient` with Ethereum private key + API credentials. Handle EIP-712 order signing on Polygon
- [x] Make `ExecutionEngine.submit()` async — replace paper fill block with real API calls routed through `UnwindManager` (live mode branches in `_submit_leg()` and `_submit_unwind()`)
- [x] Update `_process_updates()` call site to pass `kalshi_ticker` and `poly_condition_id` on each `ArbOpportunity`
- [x] Add `--dry-run` flag that preserves current paper-trading behavior as safety net (paper mode is default; `--mode live` opts in)
- [x] Track real order IDs from both platforms; WS-subscribe for Kalshi fill status, synchronous FOK confirmation for Polymarket
- [x] Add `--paper` / `--live` mode flag (`--mode paper|live`); live mode validates all credentials at startup

---

## Phase 4 — Discord Notifications

- [x] Add `DiscordNotifier` class: async webhook POST via `aiohttp`
- [x] Config: `DISCORD_WEBHOOK_URL` in `.env`
- [x] Notification triggers:
  - Order filled (direction, contracts, prices, expected profit)
  - Order rejected (reject reason)
  - Unwind triggered (details)
  - Circuit breaker activated (daily loss limit hit)
  - WS reconnection events
  - Error-level log messages
  - Daily summary (total P&L, positions, fills, rejects)
- [x] Rate-limit Discord POSTs (max 5/sec per Discord API limits)
- [x] Embed formatting with color coding: green = fill, red = error, yellow = warning

---

## Phase 5 — Testing  _(was Phase 7 — moved ahead of discovery/deployment)_

_Do first — protects everything that follows._  **✅ Completed 2026-07-09 —
87 tests, all green (`pytest tests/ -v`)._

- [x] Add `tests/` directory with `pytest` + `pytest-asyncio`
- [x] **Unit tests (critical)**:
  - `ArbEngine.check()` / `_evaluate()` — core arb detection logic
    (`test_arb_engine.py`)
  - Fee computation (`kalshi_taker_fee`, `poly_taker_fee`) — order-level
    rounding, both Poly regimes (quadratic sports / linear crypto)
    (`test_fees.py`)
  - `ExecutionEngine.submit()` — all silent-reject paths, size-down-to-depth,
    capital reservation, precise exposure check, fill path
    (`test_execution.py`)
  - `_LocalOrderbook` snapshot/delta application (`test_orderbook.py`)
- [x] **Regression tests for the 2026-07 fix batch**: fill-waiter race (fill
      arrives before register), orphan-registry retry, `yes_price`/`no_price`
      side selection, iterative reconnect, spread re-verification after
      Leg 1, `/book` transient-vs-404 back-off (`test_regressions.py`)
- [x] **Failure-injection tests** — mock exchange clients simulating: POST
      timeout → `client_order_id` reconciliation, delayed FOK →
      cancel-on-exhaustion, 429/5xx/timeout retry behavior, partial IOC and
      unwind fills, smart-FOK floor sizing (`test_failure_injection.py`,
      mocks in `tests/mocks.py`)
- [x] **Clock-controlled tests** (injected clock, no freezegun dependency):
      `DailyPnLTracker` midnight reset, pre-midnight summary slot
      (extracted `_seconds_until_daily_summary()` for testability),
      stuck-position aging (`test_clock.py`, `test_unwind.py`)
- [x] **Property-based tests** (`hypothesis`): fee/rounding invariants;
      random delta stream applied to a book == rebuild from final snapshot
      (`test_fees.py`, `test_orderbook.py`)
- [x] **Paper/live parity guard**: AST scan pins live-mode branching to the
      documented seams + behavioral test that a perfect mock exchange yields
      identical order economics to paper (`test_parity.py`)
- [x] **Integration tests**:
  - End-to-end: WS message → local books → queue → arb detection → paper
    fill → CSV audit rows (`test_integration.py`)
  - REST fetcher with mocked `aiohttp` responses
  - Unwind manager with simulated partial fills (paper and live-mocked)
- [x] Add `pytest`, `pytest-asyncio`, `hypothesis` as dev dependencies
      (`requirements-dev.txt`)
- [x] CI: GitHub Actions workflow running tests on push
      (`.github/workflows/tests.yml`, py3.10 + py3.12 matrix)

---

## Phase 6 — Cloud Deployment

_Deploy 24/7 in **paper mode** first — it is the staging environment and
generates the ledger data Phase 7 needs._  **✅ Code + infra completed
2026-07-09 (106 tests green); the actual VPS rollout and the pre-live
checklist below remain operator actions.  See [DEPLOYMENT.md](DEPLOYMENT.md)._

- [x] Create `Dockerfile` + `docker-compose.yml` with volume mounts for
      `data/` and `keys/` (non-root user, healthcheck, 45s stop grace)
- [x] Target: US-East VPS — rationale and sizing documented in DEPLOYMENT.md
      (Kalshi and the Polymarket CLOB are US-hosted; an EU region adds
      ~100ms per leg).  Long-running WS connections rule out Lambda/Cloud Run
- [x] `systemd` service file (`deploy/arb-bot.service`) and Docker
      `restart: unless-stopped` policy
- [x] SQLite as **state of record** (`StateStore`, WAL): positions, capital,
      orphaned exposure, run lock.  *Deliberate deviation from the original
      "migrate CSVs to SQLite" item:* the CSVs stay as the append-only audit
      trail — duplicating them into SQLite adds failure modes without adding
      restart safety, and the ledger stays directly readable for Phase 7
- [x] **State persistence**: `PaperPortfolio` and `UnwindManager` restore
      capital/positions/orphans from `data/state.db` at startup; every
      mutation persists atomically (`test_ops.py` restart tests)
- [x] **Startup reconciliation** (live): persisted positions vs the Kalshi
      account at boot; alert-only on mismatch.  *Limitation:* the Polymarket
      side has no authenticated position query in the current client — every
      arb position has a Kalshi leg, so pair-level drift is still detected
- [x] **Graceful shutdown drains in-flight executions**:
      `ExecutionEngine.submit_background()` tracks tasks; `drain()` runs
      before order clients close; compose stop grace (45s) >
      `DRAIN_TIMEOUT_SECONDS` (20s)
- [x] **Run lock**: heartbeat row in the state DB; a live holder blocks a
      second instance (verified against a running instance), a crashed
      holder's lock expires after `RUN_LOCK_STALE_SECONDS` so
      `restart: unless-stopped` recovers unattended
- [x] Cross-platform balance monitoring (live): Kalshi `/portfolio/balance`
      + Poly collateral via `py_clob_client` (best-effort), alert on the
      healthy→low transition below `MIN_PLATFORM_BALANCE`
- [x] Structured JSON logging (`LOG_FORMAT=json`) with optional 10MB×5
      rotation (`LOG_FILE`)
- [x] Secrets: `.env` + `keys/` volume-mounted (never in the image),
      permissions documented; USDC allowance checked at startup in live mode
      (advisory — depends on `py_clob_client` internals, verify on first
      live run)
- [x] Dependency pinning: image freezes resolved versions at build
      (`/requirements.lock.txt`, extraction documented); NTP requirement
      documented
- [x] Health endpoint (`GET /health`, 200/503): WS staleness per platform,
      queue depth, capital, positions, daily P&L, circuit breaker, orphan
      count, order counts, last order time — verified live against a real
      Polymarket WS connection
- [x] Kill-switch path in containers (`KILL_FILE=/app/data/KILL`) and data
      volume backups documented
- [ ] **Operator actions before live capital** (see DEPLOYMENT.md checklist):
      provision US-East VPS, ≥2 weeks clean paper operation, platform
      eligibility/ToS + personal-trading compliance clearance, verify the
      allowance/balance checks against the real APIs
- [ ] Optional: Prometheus metrics export for Grafana dashboards

---

## Phase 7 — Economics Gate  _(new — explicit go/no-go before building discovery)_

_The roadmap previously never asked whether the edge exists.  Fees alone are
~3.3¢/contract at p=0.5 (Kalshi ~1.75¢ + Poly sports ~1.56¢), so opportunities
must clear that at executable depth, against competitors without a 3-second
sports matching delay._

- [x] **Observation ledger** (instrumentation sprint, 2026-07-09):
      `data/observations.csv` records EVERY spread evaluation (both
      directions, no margin filter, independent of execution/dedup) with a
      two-tier throttle (1s when gross > 0, 60s baseline).
      `trade_ledger.csv` was unusable for this — it only logs executed
      trades and the dedup key suppresses recurrences.  `ArbEngine.check()`
      is now literally `observe()` + margin filter so detection and
      observation cannot drift
- [x] **WS recording half** of the record/replay harness (instrumentation
      sprint): raw messages to `data/ws_capture/{platform}-{day}.jsonl.gz`.
      Crash-safe by construction — each flush is a complete gzip member, so
      a SIGKILL'd process leaves a strictly-readable file (verified live
      with a hard kill against the real Polymarket feed)
- [ ] Replay half: feed captured files back through the pipeline for
      regression tests, performance benchmarks, and backtests
- [ ] Analysis tooling over `data/observations.csv` (+ trade_ledger for
      executed fills): opportunity frequency, size-weighted net edge at
      fillable depth, persistence/half-life, breakdown by
      market/category/time-of-day
- [ ] Fill-quality comparison once small live trades run: realized vs paper
      (slippage, partial-fill rate, unwind frequency)
- [ ] Define go/no-go criteria (e.g. expected monthly net > infra + capital
      carrying costs) and record the decision in this file before Phase 8

---

## Phase 8 — Auto Market Discovery  _(was Phase 5 — gated on the Phase 7 go decision)_

- [ ] Extend `MarketFetcher` with discovery endpoints:
  - Kalshi: `GET /trade-api/v2/events?series_ticker=KXNCAAMBGAME&status=open`
  - Polymarket: Gamma API `GET /events` with category/text search
- [ ] Build `EventMatcher` class: fuzzy-match Kalshi events to Polymarket
      events by name/date, then **hard-reject** any pair that is not strictly
      binary and complementary (soccer draws / 3-way markets — Serie A is
      fee-listed and 3-outcome on Polymarket)
- [ ] **Outcome alignment via independent automated checks** (the
      `ticker_return.py` logic is an interactive prompt and cannot be ported
      as-is):
  - outcome-name matching (Kalshi `yes_sub_title` vs Poly token `outcome`)
  - **price coherence**: aligned ⇒ Kalshi YES mid ≈ Poly YES mid; inverted ⇒
    ≈ 1 − mid.  Require coherence for the entire observe-only window
  - treat a persistent large "spread" as misalignment evidence, not
    opportunity — inverted pairs look like the best arbs, so the detector
    preferentially trades exactly the markets where matching failed
- [ ] Verify resolution-rule equivalence (OT handling, postponement/void
      policy) — a void on one platform breaks the $1.00 payout identity
- [ ] **Per-market fee fetching** (Poly `/fee-rate`, Kalshi series fee data) —
      global `POLY_FEE_RATE`/`POLY_FEE_EXPONENT` are wrong across mixed
      categories (sports 0.25/exp2, crypto 0.0175/exp1, politics zero)
- [ ] Hot-subscribe: add new events to running WS connections without restart
      — **including the fill channel** (`subscribe_fills()` snapshots the
      ticker list at call time; hot-added tickers currently get no fill
      notifications until the next reconnect)
- [ ] `DiscoveryScheduler`: runs every N minutes, discovers new events,
      validates alignment, adds to active set; discovered events persisted so
      observe-only timers survive restarts; denylist for rejected pairs
- [ ] Observe-only graduation criteria: N minutes price-coherent, zero
      spread-sanity rejections, depth above a minimum
- [ ] Scale the risk limits: per-market and per-category exposure caps;
      revisit the global order rate limit and queue/CPU headroom at hundreds
      of markets

---

## Code-Review Follow-ups  _(folded from the 2026-07-13/14 review tracker)_

_Two full review passes ran against v6.0: a module-by-module correctness
review (findings F1–F11, all fixed) and an xhigh-effort review of that fix
batch (15 verified findings; 11 repaired).  Every applied fix is pinned by
regression tests in `tests/test_review_fixes.py`.  The items below are what
remains open._

### Design decisions (need a deliberate call, not a patch)

- [ ] **Settlement payout model**: `check_kalshi_settled` treats any truthy
      `result` as settled, and `close_positions_for_event` credits a flat
      $1.00/contract — voided (refund) and scalar (`settlement_value` ≤ $1)
      outcomes overstate capital in the state of record.  Key the payout on
      the actual result; overlaps with the Phase 8 resolution-rule-equivalence
      item
- [ ] **Restart-safe settlement**: `pending_settlement` lives in the
      resolution-checker task's memory; a restart with the event pruned from
      `events.json` strands restored positions uncredited forever.  Persist
      `kalshi_ticker` on position rows in state.db and re-enroll at startup
- [ ] **Leg-2 reprice cap**: the post-Leg-1 spread re-verification accepts any
      fresh ask with net > 0, so the executed outlay can exceed the capital
      reservation (negative capital / exposure-cap breach under concurrency).
      Bound the accepted reprice or re-check headroom before submitting Leg 2
- [ ] **Paired-contract booking home**: ExecutionEngine books
      `min(leg1, leg2)` fills only on the `execute()` return path; a
      force-unwind after a cancelled `execute()` (e.g. drain timeout) bypasses
      it, leaving real two-sided exposure unbooked.  Deeper home:
      `UnwindManager._finish`
- [ ] **Circuit-breaker semantics** (deliberate design, pinned by
      `test_clock.py`): expected profits offset realized unwind losses in the
      daily tracker, weakening the loss limit on busy days.  Decide whether a
      losses-only accumulator should drive the breaker

### Live verification before the next live session

- [ ] Grep VPS `data/ws_capture/polymarket-*.jsonl.gz` for `"bids"` vs
      `"buys"` in book events — the parsers now accept both schemas; this
      confirms which one production actually sends
- [ ] Probe Kalshi with a 1-contract `immediate_or_cancel` order on the legacy
      endpoint, and plan the migration to `/portfolio/events/orders` — the
      legacy `/portfolio/orders` endpoint is past its announced deprecation
      window, and the replacement uses a different schema (fixed-point dollar
      `price`, `side: bid|ask`)

### Verified cleanup batch (safe any time)

- [ ] One Kalshi market-status classifier shared by `check_kalshi_active` /
      `check_kalshi_settled` — the two hand-maintained status tuples already
      disagree (`complete` halts trading but never satisfies settlement:
      liveness risk), and the transition cycle fetches the same ticker twice
- [ ] One Kalshi IOC lifecycle helper (place → resolve fills → defensive
      cancel) shared by `_submit_kalshi_buy` / `_sell_kalshi` — the two copies
      have already diverged once
- [ ] One order-outlay helper shared by `_submit_inner._order_outlay` and
      `ExecutionEngine._book_position` (order-level fee rounding invariant in
      two places)
- [ ] One Polymarket schema normalizer — bids/buys key tolerance is hand-rolled
      at four sites (WS book, WS price_change, REST `_parse_book_tob`,
      `_fetch_poly_bid_depth`)
- [ ] Module-level except tuple for the six `KalshiOrderClient` methods
- [ ] orders.csv non-COMPLETE rows: `total_outlay` carries only the unwind
      loss and excludes the booked paired outlay — audit rows don't reconcile
      with portfolio.csv/state.db for partial-pair trades
- [ ] Skip the step-5 REST poll when the WS waiter already confirmed a full
      fill (wasted rate-limited call on the execution hot path)
- [ ] Tests: merge `test_review_fixes.py` / `test_regressions.py` into topical
      modules; consolidate `FakeResolutionFetcher` into `tests/mocks.py`

### Still open from the pre-v6 external audit (2026-02-25, report since deleted)

- [ ] Per-market staleness check (the stale-data watchdog is per-connection
      only — one quiet market among active ones can trade on stale prices)
- [ ] Consecutive-unwind circuit breaker (halt after N unwinds in M minutes —
      catches systematic issues the daily loss limit reacts to slowly)
- [ ] Minimum-depth economic threshold (skip sub-economic 1-contract trades —
      Phase 7 analysis will quantify where the floor belongs)

---

## Verification Checklist

| Phase | How to verify |
|-------|--------------|
| 0 | `python -c "import arb_bot"` passes; `python arb_bot.py --dry-run` starts and halts cleanly; `.env` values override defaults |
| 1 | Unit tests for `DailyPnLTracker` circuit breaker; set loss limit to $0 → bot refuses all trades |
| 2 | Integration test: mock Leg 2 failure → verify Leg 1 is unwound; stuck position monitor fires after timeout |
| 3 | Test against Kalshi demo/sandbox first; verify order IDs returned and fill status tracked |
| 4 | Send test webhook to Discord; verify all notification types render correctly |
| 5 | `pytest tests/ -v` — all green; GitHub Actions badge passing; failure-injection suite covers every unwind/reconciliation branch |
| 6 | `docker-compose up` starts bot; `docker-compose down` drains in-flight legs then exits; `kill -9` + restart reconciles positions from the exchanges; a second instance refuses to start |
| 7 | Analysis report over ≥2 weeks of 24/7 paper ledger data; go/no-go decision recorded in this file |
| 8 | Discovery against live APIs; matched events pass outcome-alignment AND price-coherence checks; a deliberately inverted test pair is caught in observe-only and never trades |

---

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| Phase order: config → risk → unwind → live → alerts → discovery → deploy | Dependency chain — can't go live without unwind; can't trust live without risk controls |
| Remaining phases reordered (2026-07-09): tests → deploy → economics gate → discovery | Discovery multiplies any latent bug across N markets; 24/7 paper deployment doubles as staging and produces the ledger data the go/no-go needs |
| Economics gate before discovery | Fees are ~3.3¢/contract at p=0.5; prove that much dislocation exists at fillable depth before buying more infrastructure |
| Alignment safety = price coherence, not just name matching | Inverted/misaligned pairs read as huge "spreads" — the detector preferentially trades matching failures, and the loss is unhedged 2× outlay |
| SQLite for state, CSV retained for audit logs (2026-07-09) | Restart safety needs *state* (positions/capital/orphans/lock); duplicating append-only audit logs into SQLite adds failure modes without adding safety, and CSVs stay directly readable for Phase 7 analysis |
| SQLite over Postgres | Simpler for single-instance bot; no separate DB process needed |
| VPS over serverless | WebSocket connections are long-lived; Lambda/Cloud Run would reconnect constantly |
| Sequential leg execution (not parallel) | Safer — guarantees Leg 1 fill status is known before committing Leg 2 |
| `py_clob_client` for Polymarket over raw REST | Handles EIP-712 signing complexity |
| Testing in parallel with Phases 1–3 | Tests should exist before going live, but shouldn't block risk/unwind design work |