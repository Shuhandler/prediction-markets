# Deployment Guide

Operational reference for running the bot 24/7 on a VPS. **Run in paper
mode first** — it is the staging environment and generates the
`data/trade_ledger.csv` history that the Phase 7 economics analysis needs.

---

## VPS selection

- **Region: US-East.** Kalshi and the Polymarket CLOB are both US-hosted;
  an EU region adds ~80–100ms per REST round-trip, which directly widens
  the window between Leg 1 and Leg 2 fills (legging risk). DigitalOcean
  NYC or Hetzner Ashburn are fine; 1 vCPU / 1–2 GB RAM is sufficient.
- **Clock sync:** RSA request signatures carry timestamps. Verify NTP is
  active: `timedatectl` should show `System clock synchronized: yes`.
- Long-lived websockets rule out Lambda / Cloud Run — use a VM.

## Secrets

- `.env` holds API credentials; `keys/` holds the Kalshi RSA PEM and the
  Polymarket Ethereum key. Permissions: `chmod 600 .env; chmod -R 400 keys/`.
  Neither is baked into the Docker image (`.dockerignore`), both are
  mounted at runtime.
- **The Ethereum key controls real funds.** Use a dedicated hot wallet
  holding working capital only. In live mode the bot checks the USDC
  exchange allowance at startup (advisory — it logs, it does not block);
  approve the exchange contract before first live trade and keep
  approvals bounded.

## Run it

```bash
# one-time
cp .env.example .env         # fill in credentials; chmod 600
mkdir -p data keys           # keys/ populated manually

docker compose up -d --build # paper mode (default)
docker compose logs -f
docker compose down          # graceful: drains in-flight legs first
```

Live mode: add `command: ["python", "arb_bot.py", "--mode", "live"]` to
the service in `docker-compose.yml` — deliberately not the default.

A systemd alternative for bare-VPS installs is in
[deploy/arb-bot.service](deploy/arb-bot.service).

## State, restarts, and the run lock

- `data/state.db` (SQLite, WAL) is the **state of record**: capital, open
  positions, orphaned unwind exposure, run lock. Restarts restore from it;
  the CSVs in `data/` are append-only audit logs, not state.
  Deleting `state.db` resets the paper portfolio to `STARTING_CAPITAL`.
- **Startup reconciliation (live):** persisted positions are compared
  against the Kalshi account at boot; mismatches alert via log + Discord
  and require manual review (the bot never auto-trades on a mismatch).
- **Run lock:** a second instance sharing the same `data/` will wait up
  to ~2 minutes for a live holder, then exit. A crashed instance's lock
  expires on its own (`RUN_LOCK_STALE_SECONDS`, default 90s), so
  `restart: unless-stopped` recovers unattended.
- **Shutdown order matters:** SIGTERM → stop detection → drain in-flight
  two-leg executions (`DRAIN_TIMEOUT_SECONDS`, default 20s) → close
  clients → release lock. `stop_grace_period` (45s) and systemd
  `TimeoutStopSec` are set above the drain timeout; don't lower them.

## Health endpoint

`GET http://127.0.0.1:8080/health` (configurable via `HEALTH_HOST` /
`HEALTH_PORT`; `HEALTH_PORT=0` disables — also disable the Docker
healthcheck if you do). Returns **200** when both websockets are fresh,
**503** when degraded. Body includes: per-platform WS staleness, queue
depth, capital, open positions, daily P&L, circuit-breaker state,
orphaned-unwind count, order counts, last order time. The Docker
healthcheck keys off the status code; point external uptime monitoring at
it too (keep the port bound to localhost and tunnel, or bind carefully).

## Kill switch

`KILL_FILE` is set to `/app/data/KILL` in compose, so from the host:

```bash
touch data/KILL   # all new orders rejected immediately (open positions untouched)
rm data/KILL      # resume
```

## Logging

- `LOG_FORMAT=json` for structured one-line-per-event logs (shipping to
  Loki/CloudWatch); default is human-readable text.
- `LOG_FILE=/app/data/arb_bot.log` enables a 10 MB × 5 rotating file in
  addition to stdout. Docker's own json-file driver also applies; cap it
  with `logging: options: max-size` if disk is tight.

## Instrumentation (Phase 7 data capture)

Both are **on by default** — the collection run is the point of deploying:

- `data/observations.csv` — every spread evaluation, both directions, no
  margin filter. Throttled: 1 row/s per event+direction while a positive
  spread is live (`OBS_HOT_INTERVAL_SECONDS`), one baseline sample per
  60s otherwise (`OBS_BASELINE_INTERVAL_SECONDS`). This file — not
  `trade_ledger.csv` — is what the economics analysis reads. Expect a few
  MB/day per handful of events. Disable with `OBSERVATIONS_FILE=""`.
- `data/ws_capture/{platform}-{YYYYMMDD}.jsonl.gz` — raw WS messages for
  later replay/backtest. Each line is `{"ts": epoch, "raw": "<verbatim>"}`.
  Files are always-valid multi-member gzip: a crash loses at most
  `WS_CAPTURE_FLUSH_SECONDS` (5s) of buffer, never the file. Expect
  ~10–100 MB/day per platform during active games; prune or sync old days
  off-box. Disable with `WS_CAPTURE_DIR=""`.

Confirm collection is live via the health endpoint:
`observations_logged` and `ws_capture_messages` should be climbing.

## Backups

`data/` is the only stateful directory. Nightly cron on the host:

```bash
sqlite3 data/state.db ".backup data/state.backup.db" && \
  tar czf backups/arb-data-$(date +%F).tar.gz data/*.csv data/state.backup.db
```

## Dependency pinning

The image freezes exact resolved versions at build time; extract the lock
for audits or exact rebuilds:

```bash
docker compose run --rm arb-bot cat /requirements.lock.txt > requirements.lock.txt
```

`py_clob_client` has a history of breaking changes — review its diff
before rebuilding the image with an updated resolution.

## Before live capital — checklist

1. ≥2 weeks of clean 24/7 paper operation (no unexplained unwinds,
   reconciliation clean after every restart).
2. Phase 7 economics gate passed (see scope.md) — the edge must clear
   ~3.3¢/contract in fees at executable depth.
3. Confirm platform eligibility/ToS on both venues **and personal-trading
   compliance clearance** (event-contract accounts may fall under the
   firm's personal-trading policy even though they are not securities
   accounts — ask Compliance, don't assume).
4. Test against Kalshi's demo environment first where possible; start
   with `POSITION_SIZE` at the minimum and a tight `DAILY_LOSS_LIMIT`.
5. Verify USDC allowance log line at startup and fund the hot wallet with
   working capital only.
