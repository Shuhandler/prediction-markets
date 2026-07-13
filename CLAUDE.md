# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A cross-platform arbitrage bot for binary prediction markets (Kalshi + Polymarket). It monitors both platforms over WebSocket, detects YES/NO price discrepancies where `kalshi_ask + poly_ask < $1.00`, and executes two-leg trades with automatic unwind on partial failure. Supports paper (default) and live trading. Python 3.10+.

## Commands

```bash
pip install -r requirements-dev.txt     # runtime + test dependencies

python arb_bot.py                       # paper trading (default)
python arb_bot.py --dry-run             # validate config, print banner, exit — quickest sanity check
python arb_bot.py --mode live           # live trading (real orders; validates all credentials at startup)
python arb_bot.py --events-file X.json --log-level DEBUG

python discover.py --date today [--league mlb] [--json]   # list today's games + exchange search strings (ESPN schedule, no exchange calls)
python ticker_return.py [kalshi_url] [poly_url]   # interactive helper: generate events.json entries

pytest tests/ -q                        # full test suite (~121 tests, <10s, no network)
pytest tests/test_execution.py -v       # one module
pytest tests/ -k "orphan"               # tests matching a keyword

python -c "import arb_bot"              # syntax/import check

docker compose up -d --build            # 24/7 deployment (paper mode default) — see DEPLOYMENT.md
curl http://127.0.0.1:8080/health       # runtime status (200 healthy / 503 degraded)
```

Tests are offline (mock exchange clients in `tests/mocks.py`; shared fixtures/factories in `tests/conftest.py` — `build_stack()` wires the whole pipeline with CSVs under tmp_path). Config constants are pinned by a session fixture so results don't depend on the local `.env`; constructor defaults bind at import time, so always pass config explicitly in tests. `tests/test_parity.py` enforces that live/paper branching stays confined to `UnwindManager` — extending live-only behavior elsewhere will fail CI by design; update `ALLOWED_LIVE_REFS` only with a deliberate decision.

Config comes from `.env` (loaded via python-dotenv) with defaults in arb_bot.py lines ~110–216; CLI flags override at runtime. Credentials: `KALSHI_API_KEY` + `KALSHI_PRIVATE_KEY_PATH` (RSA PEM) for paper mode; live mode additionally requires `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_PASSPHRASE`, `POLY_PRIVATE_KEY_PATH` (hex Ethereum key). Never commit `.env`, `keys/`, or `data/`.

## Architecture

Everything lives in a single file, `arb_bot.py` (~5,100 lines, ~21 classes). README.md documents each component in depth — consult it before changing behavior. `scope.md` is the phase roadmap (Phases 0–4 done; remaining, in execution order: 5 testing, 6 deployment, 7 economics gate, 8 auto-discovery — renumbered 2026-07-09). `IBKR_ROLLOUT.md` plans the parallel ForecastEx/IBKR venue-expansion track (phases FX-A…FX-D; not started).

Data flow (event-driven, no polling):

```
PolymarketWS / KalshiWS  →  asyncio.Queue (event names)  →  _process_updates()
  →  ArbEngine.check()          (pure detection, never trades)
  →  ExecutionEngine.submit()   (silent pre-trade filters + RiskManager gate + rate limiter)
  →  UnwindManager.execute()    (sequential two-leg: Polymarket FOK first, then Kalshi IOC;
                                 rollback/sell-back if Leg 2 fails)
  →  PaperPortfolio / CSV logs in data/
```

Key invariants to preserve:

- **All monetary/probability math uses `decimal.Decimal`** (12-digit precision, `ROUND_HALF_UP`; fees ceil to the penny with `ROUND_CEILING`). Never introduce floats into price, fee, or P&L calculations.
- **Strategy/execution/rollback are strictly separated**: `ArbEngine` only detects; `ExecutionEngine` only validates and routes; `UnwindManager` owns order placement and unwind.
- **Polymarket FOK is always Leg 1.** If it fails, no Kalshi order is placed — zero legging risk. Kalshi IOC (Leg 2) supports partial fills; excess Leg 1 contracts get unwound.
- **Pre-trade rejections are silent** (no Order object, no CSV row) so hot WS ticks don't spam logs; only the order rate limiter produces a `REJECTED` CSV row. On any non-complete outcome the `(event, direction)` dedup key is released so the next tick can retry.
- **Kalshi only publishes bids**; asks are implied (`Ask(YES) = 1 − Bid(NO)`), and REST prices are integer cents while Polymarket uses decimal dollars — conversion bugs here create false arbs.
- Paper and live mode share the entire pipeline; they diverge only inside `UnwindManager._submit_leg()` / `_submit_unwind()` (paper simulates instant zero-slippage fills).

Supporting mechanisms: `RiskManager` gates every order (kill-switch file `KILL`, daily loss circuit breaker, exposure/position caps, spread sanity > 20% rejected as stale data); in-flight submissions reserve capital in `ExecutionEngine._reserved` so concurrent trades can't over-commit; `StuckPositionMonitor` force-unwinds half-filled arbs older than 30s (serialized against in-flight `execute()` via a per-`UnwindOrder` lock) and retries orphaned unwind exposure each cycle; a resolution checker settles positions when markets resolve and unsubscribes dead markets from both WS trackers; both WS classes have stale-data watchdogs with exponential-backoff reconnect that re-pull REST snapshots. Order sizing caps to fillable depth (`opp.max_contracts`) rather than rejecting, and fees are computed once per order (order-level ceil), not per contract. Polymarket token IDs are never resolved at runtime — they come from `events.json` (each entry needs all 5 fields; generate with `ticker_return.py`).

Outputs land in `data/*.csv` (trade_ledger, orders, portfolio, unwinds) — append-only audit logs written immediately. Phase 7 instrumentation (on by default): `data/observations.csv` records every spread evaluation via `ArbEngine.observe()` (of which `check()` is literally the margin-filtered subset — don't reintroduce a second evaluation path), and `data/ws_capture/*.jsonl.gz` records raw WS messages where every flush is a complete gzip member (crash-safe; don't "simplify" it back to one long gzip stream — a hard kill would corrupt the file). **State of record is `data/state.db`** (`StateStore`, SQLite WAL): capital, open positions, orphaned exposure, and the single-instance run lock all survive restarts and are restored at startup — the CSVs are audit only, never read back. Deployment layer (Phase 6): Dockerfile/docker-compose/systemd unit (see DEPLOYMENT.md), a localhost `/health` endpoint (`HealthServer`), shutdown that drains in-flight executions via `ExecutionEngine.drain()` before order clients close, live-mode startup reconciliation against Kalshi positions, and a balance monitor. When touching shutdown or main() wiring, preserve the order: drain → cancel background tasks → close clients → release run lock → close state store.
