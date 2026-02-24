# Production Readiness Roadmap — Prediction Market Arb Bot

> **Current state**: v5.0 paper-trading bot with event-driven WS architecture,
> Decimal precision, Fill-or-Kill execution, OMS, and stale-data watchdog.
>
> **Goal**: Transform into a production-ready live-trading system across 8 phases.

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

- [ ] Add `DailyPnLTracker` class: tracks realized + unrealized P&L per calendar day, persists to CSV/SQLite
- [ ] Add `DAILY_LOSS_LIMIT` config param (e.g. `$100`); circuit-breaker halts all new orders when breached
- [ ] Add `MAX_TOTAL_EXPOSURE` cap across all open positions
- [ ] Add `MAX_CONCURRENT_POSITIONS` limit
- [ ] Add spread sanity check in `ArbEngine.check()`: reject spreads > 20% as likely stale/bad data
- [ ] Add order submission rate limiter (max N orders/minute) to prevent runaway execution
- [ ] Add file-based kill switch (`touch KILL` to halt) for emergency remote stop

---

## Phase 2 — Unwind Module

_Critical for live trading — builds on Phase 1 risk management._

- [ ] New `UnwindManager` class with sequential execution + rollback:
  - Submit Leg 1 → await fill confirmation → Submit Leg 2 → await fill confirmation
  - If Leg 2 fails/times out: immediately sell Leg 1 at market to unwind
  - If Leg 1 partially fills: adjust Leg 2 size to match, or unwind partial
- [ ] Add `UNWIND_TIMEOUT_SECONDS` config (e.g. 5s) — if no fill confirmation within timeout, cancel + unwind
- [ ] Add `StuckPositionMonitor`: background task that detects half-filled arbs older than N seconds and auto-unwinds
- [ ] Integrate with Discord alerts (Phase 4) — every unwind triggers a notification
- [ ] Add `UnwindOrder` type to OMS with its own lifecycle tracking

---

## Phase 3 — Live Order Execution

- [ ] **Kalshi**: Build authenticated REST order client using existing RSA signing (lines 1307–1329). Endpoint: `POST /trade-api/v2/portfolio/orders`
- [ ] **Polymarket**: Add `py_clob_client` dependency. Initialize `ClobClient` with Ethereum private key + API credentials. Handle EIP-712 order signing on Polygon
- [ ] Make `ExecutionEngine.submit()` async — replace paper fill block (lines 2150–2180) with real API calls routed through `UnwindManager`
- [ ] Update `_process_updates()` call site (line 2480) to `await` execution
- [ ] Add `--dry-run` flag that preserves current paper-trading behavior as safety net
- [ ] Track real order IDs from both platforms; poll/WS-subscribe for fill status updates
- [ ] Add `--paper` / `--live` mode flag with confirmation prompt for live mode

---

## Phase 4 — Discord Notifications

- [ ] Add `DiscordNotifier` class: async webhook POST via `aiohttp`
- [ ] Config: `DISCORD_WEBHOOK_URL` in `.env`
- [ ] Notification triggers:
  - Order filled (direction, contracts, prices, expected profit)
  - Order rejected (reject reason)
  - Unwind triggered (details)
  - Circuit breaker activated (daily loss limit hit)
  - WS reconnection events
  - Error-level log messages
  - Daily summary (total P&L, positions, fills, rejects)
- [ ] Rate-limit Discord POSTs (max 5/sec per Discord API limits)
- [ ] Embed formatting with color coding: green = fill, red = error, yellow = warning

---

## Phase 5 — Auto Market Discovery

- [ ] Extend `MarketFetcher` with discovery endpoints:
  - Kalshi: `GET /trade-api/v2/events?series_ticker=KXNCAAMBGAME&status=open`
  - Polymarket: Gamma API `GET /events` with category/text search
- [ ] Build `EventMatcher` class: fuzzy-match Kalshi events to Polymarket events by name/date
- [ ] Automated outcome alignment verification (Kalshi YES == Poly token[0]) — port logic from `ticker_return.py` lines 259–285
- [ ] Hot-subscribe: add new events to running WS connections without restart
- [ ] `DiscoveryScheduler`: runs every N minutes, discovers new events, validates alignment, adds to active set
- [ ] Safety: new events start in "observe-only" mode for M minutes before trading is enabled

---

## Phase 6 — Cloud Deployment

- [ ] Create `Dockerfile` + `docker-compose.yml` with volume mounts for `data/` and `keys/`
- [ ] Target: DigitalOcean or Hetzner VPS (long-running WS connections rule out Lambda/Cloud Run)
- [ ] `systemd` service file or Docker `restart: unless-stopped` policy
- [ ] Migrate CSV storage → SQLite (atomic writes, concurrent safety, bounded growth, queryable)
- [ ] Add structured JSON logging with log rotation (`logging.handlers.RotatingFileHandler`)
- [ ] Secrets management: Docker secrets or `.env` file with restricted permissions
- [ ] Health-check endpoint: minimal HTTP server on localhost reporting WS status, last trade time, daily P&L
- [ ] Optional: Prometheus metrics export for Grafana dashboards

---

## Phase 7 — Testing

_Run in parallel with Phases 1–3._

- [ ] Add `tests/` directory with `pytest` + `pytest-asyncio`
- [ ] **Unit tests (critical)**:
  - `ArbEngine.check()` / `_evaluate()` — core arb detection logic
  - Fee computation (`kalshi_taker_fee`, `poly_taker_fee`) — wrong fees = false arbs
  - `ExecutionEngine.submit()` — all 5 reject paths + fill path
  - `_LocalOrderbook` snapshot/delta application
- [ ] **Integration tests**:
  - End-to-end: mock WS update → queue → arb detection → order fill
  - REST fetcher with mocked `aiohttp` responses
  - Unwind manager with simulated partial fills
- [ ] Add `pytest` and `pytest-asyncio` to `requirements.txt`
- [ ] CI: GitHub Actions workflow running tests on push

---

## Verification Checklist

| Phase | How to verify |
|-------|--------------|
| 0 | `python -c "import arb_bot"` passes; `python arb_bot.py --dry-run` starts and halts cleanly; `.env` values override defaults |
| 1 | Unit tests for `DailyPnLTracker` circuit breaker; set loss limit to $0 → bot refuses all trades |
| 2 | Integration test: mock Leg 2 failure → verify Leg 1 is unwound; stuck position monitor fires after timeout |
| 3 | Test against Kalshi demo/sandbox first; verify order IDs returned and fill status tracked |
| 4 | Send test webhook to Discord; verify all notification types render correctly |
| 5 | Run discovery against live APIs; verify matched events have correct outcome alignment |
| 6 | `docker-compose up` starts bot; `docker-compose down` triggers graceful shutdown; SQLite data persists across restarts |
| 7 | `pytest tests/ -v` — all green; GitHub Actions badge shows passing |

---

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| Phase order: config → risk → unwind → live → alerts → discovery → deploy | Dependency chain — can't go live without unwind; can't trust live without risk controls |
| SQLite over Postgres | Simpler for single-instance bot; no separate DB process needed |
| VPS over serverless | WebSocket connections are long-lived; Lambda/Cloud Run would reconnect constantly |
| Sequential leg execution (not parallel) | Safer — guarantees Leg 1 fill status is known before committing Leg 2 |
| `py_clob_client` for Polymarket over raw REST | Handles EIP-712 signing complexity |
| Testing in parallel with Phases 1–3 | Tests should exist before going live, but shouldn't block risk/unwind design work |