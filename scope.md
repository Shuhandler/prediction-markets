# Production Readiness Roadmap — Prediction Market Arb Bot

> **Current state**: v5.0 paper-trading bot with event-driven WS architecture,
> Decimal precision, Fill-or-Kill execution, OMS, and stale-data watchdog.
>
> **Goal**: Transform into a production-ready live-trading system.
>
> **Execution order (revised 2026-07-09)**: Phases 0–4 are done. The remaining
> phases were reordered to **5 Testing → 6 Deployment → 7 Economics Gate →
> 8 Auto Discovery** (testing and deployment were previously last, discovery
> first). Rationale: discovery multiplies any latent bug across hundreds of
> markets, 24/7 paper deployment generates the ledger data the go/no-go
> analysis needs, and discovery is only worth building if that analysis says
> the edge exists.

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

_Do first — protects everything that follows._

- [ ] Add `tests/` directory with `pytest` + `pytest-asyncio`
- [ ] **Unit tests (critical)**:
  - `ArbEngine.check()` / `_evaluate()` — core arb detection logic
  - Fee computation (`kalshi_taker_fee`, `poly_taker_fee`) — order-level
    rounding, both Poly regimes (quadratic sports / linear crypto)
  - `ExecutionEngine.submit()` — all silent-reject paths, size-down-to-depth,
    capital reservation, precise exposure check, fill path
  - `_LocalOrderbook` snapshot/delta application
- [ ] **Regression tests for the 2026-07 fix batch** (port the existing smoke
      script into pytest): fill-waiter race (fill arrives before register),
      orphan-registry retry, `yes_price`/`no_price` side selection, iterative
      reconnect, spread re-verification after Leg 1
- [ ] **Failure-injection tests (highest value)** — mock exchange clients that
      simulate: POST timeout → `client_order_id` reconciliation, delayed FOK →
      cancel-on-exhaustion, 429s, partial unwind sells, unwind-sell failure →
      orphan registration.  The unwind paths only run when things go wrong;
      they must be exercised somewhere other than production
- [ ] **Clock-controlled tests** (freezegun or injected clock):
      `DailyPnLTracker` midnight reset, pre-midnight summary timing,
      stuck-position aging
- [ ] **Property-based tests** (`hypothesis`): fee/rounding invariants;
      random delta stream applied to a book == rebuild from final snapshot
- [ ] **Paper/live parity guard**: assert the pipelines diverge only inside
      `_submit_leg()` / `_submit_unwind()` so paper→live promotion stays valid
- [ ] **Integration tests**:
  - End-to-end: mock WS update → queue → arb detection → order fill
  - REST fetcher with mocked `aiohttp` responses
  - Unwind manager with simulated partial fills
- [ ] Add `pytest`, `pytest-asyncio` (and `hypothesis`) as dev dependencies
- [ ] CI: GitHub Actions workflow running tests on push

---

## Phase 6 — Cloud Deployment

_Deploy 24/7 in **paper mode** first — it is the staging environment and
generates the ledger data Phase 7 needs._

- [ ] Create `Dockerfile` + `docker-compose.yml` with volume mounts for `data/` and `keys/`
- [ ] Target: US-East VPS (Kalshi and the Polymarket CLOB are US-hosted; an EU
      region adds ~100ms per leg, directly widening the legging window).
      Long-running WS connections rule out Lambda/Cloud Run
- [ ] `systemd` service file or Docker `restart: unless-stopped` policy
- [ ] Migrate CSV storage → SQLite (atomic writes, concurrent safety, bounded growth, queryable)
- [ ] **State persistence, not just logs**: positions, capital, and orphaned
      exposure in SQLite — today the portfolio is in-memory and every restart
      resets capital and forgets open positions
- [ ] **Startup reconciliation**: query open positions on both exchanges at
      boot and reconcile against persisted state (also catches legs orphaned
      by a crash mid-trade)
- [ ] **Graceful shutdown drains in-flight executions**: `execution.submit()`
      tasks are fire-and-forget — track them and await (with timeout) before
      closing order clients; set the Docker stop grace period above that
- [ ] **Run lock** (SQLite/PID): a deploy overlap must not produce two
      instances trading simultaneously
- [ ] Cross-platform balance monitoring: alert when either platform's
      available balance drops below a threshold (profits pool on one side)
- [ ] Add structured JSON logging with log rotation (`logging.handlers.RotatingFileHandler`)
- [ ] Secrets management: Docker secrets or `.env` file with restricted
      permissions; hot wallet holds working capital only, ERC-20 allowances
      capped and verified at startup
- [ ] Pin dependencies (exact versions / lockfile) — `py_clob_client`
      breaking changes are a live risk; ensure NTP is active (RSA signature
      timestamps)
- [ ] Health-check endpoint: minimal HTTP server on localhost reporting WS
      status/staleness per platform, queue depth, last trade time, daily P&L,
      circuit-breaker state, orphan count
- [ ] Document the container kill-switch path (the `KILL` file check is
      CWD-relative); back up the data volume
- [ ] Before live capital: confirm platform eligibility/ToS on both venues
      and personal-trading compliance clearance
- [ ] Optional: Prometheus metrics export for Grafana dashboards

---

## Phase 7 — Economics Gate  _(new — explicit go/no-go before building discovery)_

_The roadmap previously never asked whether the edge exists.  Fees alone are
~3.3¢/contract at p=0.5 (Kalshi ~1.75¢ + Poly sports ~1.56¢), so opportunities
must clear that at executable depth, against competitors without a 3-second
sports matching delay._

- [ ] Analysis tooling over `data/trade_ledger.csv` from the 24/7 paper
      deployment: opportunity frequency, size-weighted net edge at fillable
      depth, persistence/half-life, breakdown by market/category/time-of-day
- [ ] WS record/replay harness: record raw WS messages to disk; replay through
      the pipeline for regression tests, performance benchmarks, and backtests
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
| SQLite over Postgres | Simpler for single-instance bot; no separate DB process needed |
| VPS over serverless | WebSocket connections are long-lived; Lambda/Cloud Run would reconnect constantly |
| Sequential leg execution (not parallel) | Safer — guarantees Leg 1 fill status is known before committing Leg 2 |
| `py_clob_client` for Polymarket over raw REST | Handles EIP-712 signing complexity |
| Testing in parallel with Phases 1–3 | Tests should exist before going live, but shouldn't block risk/unwind design work |