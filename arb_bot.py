#!/usr/bin/env python3
"""
==========================================================================
  Prediction Market Arbitrage — Paper-Trading Bot  (v2.0)
  Monitors Kalshi and Polymarket for cross-platform arbitrage opportunities.

  PAPER TRADING ONLY — no real orders are ever placed.
==========================================================================

  Dependencies (run once):
      pip install aiohttp

  Usage:
      1. Edit the EVENTS list at the bottom of the config section.
      2. Run:  python arb_bot.py
      3. Check data/trade_ledger.csv for logged opportunities.
"""

import asyncio
import csv
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp

# ====================================================================
# CONFIGURATION — edit these values to suit your needs
# ====================================================================

CHECK_INTERVAL: int = 60         # Seconds between each polling cycle
MIN_PROFIT_MARGIN: float = 0.02  # Minimum spread (2%) to log a paper trade
DATA_DIR: str = "data"
LEDGER_FILE: str = os.path.join(DATA_DIR, "trade_ledger.csv")
REQUEST_TIMEOUT: int = 10        # HTTP timeout in seconds

# ---- Retry / backoff settings ----
MAX_RETRIES: int = 3             # Number of retry attempts per request
RETRY_BASE_DELAY: float = 1.0    # Initial backoff delay in seconds
RETRY_BACKOFF_FACTOR: float = 2.0  # Multiply delay by this each retry

# ---- Rate limits (conservative — well under documented limits) ----
# Kalshi Basic tier: 20 reads/sec;  Polymarket /price: 150 reads/sec
KALSHI_MAX_RPS: float = 5.0
POLY_MAX_RPS: float = 10.0

# ---- API base URLs (no trailing slash) ----
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
CLOB_BASE = "https://clob.polymarket.com"

# ====================================================================
# EVENTS TO MONITOR — add your own pairs here
# ====================================================================
# Each entry maps the same real-world event across both platforms.
#   name              : a human-readable label (for logs / CSV)
#   kalshi_ticker     : the Kalshi market ticker string
#   poly_condition_id : the Polymarket condition_id (hex string)

EVENTS: list[dict] = [
    # ---- Fordham Rams vs Saint Louis Billikens (Women's CBB, Feb 11) ----
    # Kalshi: https://kalshi.com/markets/kxncaawbgame/college-basketball-womens-game/kxncaawbgame-26feb11forslu
    #   Kalshi splits this into two markets (one per team). We use the
    #   Fordham ticker so that "Yes" = "Fordham wins", aligning with
    #   Polymarket outcome[0] = "Fordham Rams".
    # Polymarket: https://polymarket.com/sports/cwbb/cwbb-fordm-stlou-2026-02-11
    {
        "name": "WCBB: Coastal Carolina vs Old Dominion",
        "kalshi_ticker": "KXNCAAWBGAME-26FEB11CCARODU-CCAR",
        "poly_condition_id": "0xf9db0e36f512695d5a5a49f35c4b8415bd9785318cec5b274e0ade4d12892ef0",
    },
    # Add more pairs here:
    # {
    #     "name": "...",
    #     "kalshi_ticker": "...",
    #     "poly_condition_id": "...",
    # },
]


# ====================================================================
# DATA CLASSES
# ====================================================================

@dataclass
class TopOfBook:
    """Best bid and ask for one side (Yes or No) of a binary market."""
    best_bid: Optional[float] = None  # highest resting buy price
    best_ask: Optional[float] = None  # lowest resting sell price


@dataclass
class MarketSnapshot:
    """Top-of-book snapshot for both sides of a binary market."""
    yes: TopOfBook = field(default_factory=TopOfBook)
    no: TopOfBook = field(default_factory=TopOfBook)
    source: str = ""


@dataclass
class ArbOpportunity:
    """A single detected arbitrage opportunity."""
    timestamp: str
    event_name: str
    direction: str
    kalshi_price: float
    poly_price: float
    total_cost: float
    profit: float          # absolute dollar profit per $1 contract


# ====================================================================
# RateLimiter — simple token-bucket per host
# ====================================================================

class RateLimiter:
    """Async rate limiter ensuring requests don't exceed *max_rps*."""

    def __init__(self, max_rps: float) -> None:
        self._min_interval = 1.0 / max_rps
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = asyncio.get_running_loop().time()
            wait = self._last + self._min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = asyncio.get_running_loop().time()


# ====================================================================
# Retry helper — exponential backoff for HTTP requests
# ====================================================================

async def _request_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    limiter: RateLimiter,
    *,
    params: dict | None = None,
    label: str = "",
) -> dict | None:
    """
    GET *url* with retry + exponential backoff.

    Retries on:  429 (rate-limit), 5xx (server error), connection errors,
                 timeouts.
    Does NOT retry on:  other 4xx client errors.

    Returns parsed JSON on success, None on permanent failure.
    """
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

    for attempt in range(1, MAX_RETRIES + 1):
        await limiter.acquire()
        try:
            async with session.get(url, params=params, timeout=timeout) as resp:
                # ── Rate-limited: honour Retry-After if present ──
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After")
                    delay = (
                        float(retry_after)
                        if retry_after
                        else RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** (attempt - 1))
                    )
                    logging.warning(
                        "[%s] Rate limited (429), retry in %.1fs (%d/%d)",
                        label, delay, attempt, MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue

                # ── Server error: retry with backoff ──
                if resp.status >= 500:
                    delay = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** (attempt - 1))
                    logging.warning(
                        "[%s] Server error %d, retry in %.1fs (%d/%d)",
                        label, resp.status, delay, attempt, MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue

                # ── Other 4xx: permanent client error, don't retry ──
                if resp.status >= 400:
                    logging.error(
                        "[%s] Client error %d for %s — not retrying",
                        label, resp.status, url,
                    )
                    return None

                # ── Success: parse JSON ──
                try:
                    return await resp.json(content_type=None)
                except Exception as exc:
                    logging.error("[%s] Invalid JSON from %s: %s", label, url, exc)
                    return None

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            delay = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** (attempt - 1))
            logging.warning(
                "[%s] %s — retry in %.1fs (%d/%d)",
                label, exc, delay, attempt, MAX_RETRIES,
            )
            await asyncio.sleep(delay)

    logging.error("[%s] All %d retries exhausted for %s", label, MAX_RETRIES, url)
    return None


# ====================================================================
# MarketFetcher — async API communication
# ====================================================================

class MarketFetcher:
    """
    Retrieves top-of-book data from Kalshi and Polymarket concurrently.

    All HTTP calls use retry + exponential backoff and respect per-host
    rate limiters.

    - Kalshi:      REST orderbook endpoint (public, no auth required).
    - Polymarket:  CLOB /markets to resolve condition_id → token IDs,
                   then CLOB /price for each token+side.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._poly_token_cache: dict[str, tuple[str, str]] = {}
        self._kalshi_limiter = RateLimiter(KALSHI_MAX_RPS)
        self._poly_limiter = RateLimiter(POLY_MAX_RPS)

    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Accept": "application/json",
                    "User-Agent": "PredictionArbBot/2.0 (paper-trading)",
                },
            )

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    # ----------------------------------------------------------------
    #  Market expiration / resolution checks
    # ----------------------------------------------------------------

    async def check_kalshi_active(self, ticker: str) -> bool:
        """Return True if the Kalshi market is still open for trading."""
        await self._ensure_session()
        data = await _request_with_retry(
            self._session,
            f"{KALSHI_BASE}/markets/{ticker}",
            self._kalshi_limiter,
            label="Kalshi-status",
        )
        if data is None:
            # Network error — assume active to avoid premature removal.
            return True

        market = data.get("market", data)
        status = str(market.get("status", "")).lower()
        result = market.get("result", "")

        if status in ("settled", "closed", "finalized") or result:
            logging.info(
                "[Kalshi] Market %s resolved (status=%s, result=%s)",
                ticker, status, result,
            )
            return False
        return True

    async def check_poly_active(self, condition_id: str) -> bool:
        """
        Return True if the Polymarket market is still active.

        Also opportunistically caches the token IDs from the same
        response, so the later price-fetch step can skip a round-trip.
        """
        await self._ensure_session()
        data = await _request_with_retry(
            self._session,
            f"{CLOB_BASE}/markets/{condition_id}",
            self._poly_limiter,
            label="Poly-status",
        )
        if data is None:
            return True  # assume active on error

        if data.get("closed") is True or data.get("active") is False:
            logging.info(
                "[Polymarket] Market %s… resolved (closed=%s, active=%s)",
                condition_id[:16], data.get("closed"), data.get("active"),
            )
            return False

        # Cache tokens from this response so _resolve_poly_tokens is free
        tokens = data.get("tokens", [])
        if len(tokens) >= 2 and condition_id not in self._poly_token_cache:
            pair = (str(tokens[0]["token_id"]), str(tokens[1]["token_id"]))
            self._poly_token_cache[condition_id] = pair
            logging.info(
                "[Polymarket] Resolved %s… → %s (%s…)  %s (%s…)",
                condition_id[:16],
                tokens[0].get("outcome", "?"), pair[0][:16],
                tokens[1].get("outcome", "?"), pair[1][:16],
            )

        return True

    # ----------------------------------------------------------------
    #  Kalshi — orderbook
    # ----------------------------------------------------------------

    async def fetch_kalshi(self, ticker: str) -> Optional[MarketSnapshot]:
        """
        Fetch the Kalshi orderbook for *ticker* and return a MarketSnapshot.

        Kalshi's binary orderbook returns YES bids and NO bids only.
        Asks are implied:
            Best Ask(Yes) = 1.00 − Best Bid(No)
            Best Ask(No)  = 1.00 − Best Bid(Yes)

        Prices in the "yes"/"no" arrays are in CENTS (0-99).
        Each entry is [price_cents, quantity].
        """
        await self._ensure_session()
        data = await _request_with_retry(
            self._session,
            f"{KALSHI_BASE}/markets/{ticker}/orderbook",
            self._kalshi_limiter,
            params={"depth": 5},
            label="Kalshi",
        )
        if data is None:
            return None

        orderbook = data.get("orderbook", {})
        yes_bids_raw = orderbook.get("yes", [])  # [[cents, qty], ...]
        no_bids_raw = orderbook.get("no", [])     # [[cents, qty], ...]

        yes = TopOfBook()
        no = TopOfBook()

        if yes_bids_raw:
            highest_yes_bid_cents = max(lvl[0] for lvl in yes_bids_raw)
            yes.best_bid = highest_yes_bid_cents / 100.0
            # Yes bid @ X¢  ⟹  No ask @ (100−X)¢
            no.best_ask = (100 - highest_yes_bid_cents) / 100.0

        if no_bids_raw:
            highest_no_bid_cents = max(lvl[0] for lvl in no_bids_raw)
            no.best_bid = highest_no_bid_cents / 100.0
            # No bid @ X¢  ⟹  Yes ask @ (100−X)¢
            yes.best_ask = (100 - highest_no_bid_cents) / 100.0

        return MarketSnapshot(yes=yes, no=no, source="kalshi")

    # ----------------------------------------------------------------
    #  Polymarket — token resolution (cached)
    # ----------------------------------------------------------------

    async def _resolve_poly_tokens(self, condition_id: str) -> Optional[tuple[str, str]]:
        """
        Resolve a condition_id into (yes_token_id, no_token_id).

        Returns cached tokens if available (typically populated by
        check_poly_active); otherwise hits the CLOB /markets endpoint.
        """
        if condition_id in self._poly_token_cache:
            return self._poly_token_cache[condition_id]

        await self._ensure_session()
        data = await _request_with_retry(
            self._session,
            f"{CLOB_BASE}/markets/{condition_id}",
            self._poly_limiter,
            label="Poly-resolve",
        )
        if data is None:
            return None

        tokens = data.get("tokens", [])
        if len(tokens) < 2:
            logging.error(
                "[Polymarket] Expected 2 tokens, got %d for %s…",
                len(tokens), condition_id[:16],
            )
            return None

        pair = (str(tokens[0]["token_id"]), str(tokens[1]["token_id"]))
        self._poly_token_cache[condition_id] = pair
        logging.info(
            "[Polymarket] Resolved %s… → %s (%s…)  %s (%s…)",
            condition_id[:16],
            tokens[0].get("outcome", "?"), pair[0][:16],
            tokens[1].get("outcome", "?"), pair[1][:16],
        )
        return pair

    # ----------------------------------------------------------------
    #  Polymarket — single price fetch
    # ----------------------------------------------------------------

    async def _fetch_poly_price(self, token_id: str, side: str) -> Optional[float]:
        """
        Fetch a single price via CLOB GET /price.
        BUY price = best ask (what you'd pay to buy).
        SELL price = best bid (what you'd receive to sell).
        """
        await self._ensure_session()
        data = await _request_with_retry(
            self._session,
            f"{CLOB_BASE}/price",
            self._poly_limiter,
            params={"token_id": token_id, "side": side},
            label=f"Poly-{side}",
        )
        if data is None:
            return None
        try:
            price = float(data.get("price", 0))
            return price if price > 0 else None
        except (ValueError, TypeError):
            return None

    # ----------------------------------------------------------------
    #  Polymarket — full snapshot (4 prices concurrently)
    # ----------------------------------------------------------------

    async def fetch_polymarket(self, condition_id: str) -> Optional[MarketSnapshot]:
        """
        Fetch top-of-book from Polymarket for both the Yes and No tokens
        associated with *condition_id*.

        Fires all four /price requests concurrently to minimise latency
        and reduce stale-price risk.
        """
        tokens = await self._resolve_poly_tokens(condition_id)
        if tokens is None:
            return None

        yes_token, no_token = tokens

        # All four price calls in parallel
        yes_buy, yes_sell, no_buy, no_sell = await asyncio.gather(
            self._fetch_poly_price(yes_token, "BUY"),
            self._fetch_poly_price(yes_token, "SELL"),
            self._fetch_poly_price(no_token, "BUY"),
            self._fetch_poly_price(no_token, "SELL"),
        )

        yes_tob = TopOfBook(best_ask=yes_buy, best_bid=yes_sell)
        no_tob = TopOfBook(best_ask=no_buy, best_bid=no_sell)

        return MarketSnapshot(yes=yes_tob, no=no_tob, source="polymarket")

    # ----------------------------------------------------------------
    #  Fetch both platforms concurrently for one event
    # ----------------------------------------------------------------

    async def fetch_both(
        self, ticker: str, condition_id: str,
    ) -> tuple[Optional[MarketSnapshot], Optional[MarketSnapshot]]:
        """Fetch Kalshi and Polymarket snapshots concurrently."""
        return await asyncio.gather(
            self.fetch_kalshi(ticker),
            self.fetch_polymarket(condition_id),
        )


# ====================================================================
# ArbEngine — price comparison & opportunity detection
# ====================================================================

class ArbEngine:
    """
    Compares Kalshi and Polymarket snapshots and returns any arbitrage
    opportunities whose spread exceeds the configured minimum margin.

    Strategy (guaranteed $1.00 payout regardless of outcome):
      Direction A:  Buy YES on Kalshi  +  Buy NO on Polymarket
      Direction B:  Buy NO on Kalshi   +  Buy YES on Polymarket

    If total cost < $1.00, the difference is risk-free profit.
    """

    def __init__(self, min_margin: float = MIN_PROFIT_MARGIN):
        self.min_margin = min_margin

    def check(
        self,
        event_name: str,
        kalshi: MarketSnapshot,
        poly: MarketSnapshot,
    ) -> list[ArbOpportunity]:
        """Return a list of ArbOpportunity for every qualifying spread."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        opportunities: list[ArbOpportunity] = []

        # Direction A: Buy YES @ Kalshi  +  Buy NO @ Polymarket
        if kalshi.yes.best_ask is not None and poly.no.best_ask is not None:
            cost = round(kalshi.yes.best_ask + poly.no.best_ask, 6)
            if cost < 1.0:
                profit = round(1.0 - cost, 6)
                if profit >= self.min_margin:
                    opportunities.append(ArbOpportunity(
                        timestamp=now,
                        event_name=event_name,
                        direction="Buy YES@Kalshi + Buy NO@Polymarket",
                        kalshi_price=kalshi.yes.best_ask,
                        poly_price=poly.no.best_ask,
                        total_cost=cost,
                        profit=profit,
                    ))

        # Direction B: Buy NO @ Kalshi  +  Buy YES @ Polymarket
        if kalshi.no.best_ask is not None and poly.yes.best_ask is not None:
            cost = round(kalshi.no.best_ask + poly.yes.best_ask, 6)
            if cost < 1.0:
                profit = round(1.0 - cost, 6)
                if profit >= self.min_margin:
                    opportunities.append(ArbOpportunity(
                        timestamp=now,
                        event_name=event_name,
                        direction="Buy NO@Kalshi + Buy YES@Polymarket",
                        kalshi_price=kalshi.no.best_ask,
                        poly_price=poly.yes.best_ask,
                        total_cost=cost,
                        profit=profit,
                    ))

        return opportunities


# ====================================================================
# PaperTrader — CSV ledger for hypothetical trades
# ====================================================================

class PaperTrader:
    """
    Writes every detected opportunity to a CSV file.
    Creates the data directory and file with headers on first run.
    """

    COLUMNS = [
        "timestamp",
        "event_name",
        "direction",
        "kalshi_price",
        "poly_price",
        "total_cost",
        "profit",
        "profit_pct",
    ]

    def __init__(self, path: str = LEDGER_FILE):
        self.path = path
        self._ensure_dir()
        self._ensure_header()

    def _ensure_dir(self) -> None:
        dir_path = os.path.dirname(self.path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

    def _ensure_header(self) -> None:
        if not os.path.exists(self.path):
            with open(self.path, mode="w", newline="") as fh:
                csv.DictWriter(fh, fieldnames=self.COLUMNS).writeheader()
            logging.info("Created new trade ledger → %s", self.path)

    def log(self, opp: ArbOpportunity) -> None:
        """Append one row to the ledger CSV."""
        row = {
            "timestamp": opp.timestamp,
            "event_name": opp.event_name,
            "direction": opp.direction,
            "kalshi_price": f"{opp.kalshi_price:.4f}",
            "poly_price": f"{opp.poly_price:.4f}",
            "total_cost": f"{opp.total_cost:.4f}",
            "profit": f"{opp.profit:.4f}",
            "profit_pct": f"{opp.profit * 100:.2f}%",
        }
        with open(self.path, mode="a", newline="") as fh:
            csv.DictWriter(fh, fieldnames=self.COLUMNS).writerow(row)


# ====================================================================
# Helpers
# ====================================================================

def _fmt_price(price: Optional[float]) -> str:
    """Format a price for logging, handling None gracefully."""
    return f"${price:.4f}" if price is not None else "  n/a  "


def _print_banner(active_events: list[dict]) -> None:
    """Print a startup banner with current configuration."""
    logging.info("=" * 62)
    logging.info("  ARBITRAGE PAPER-TRADING BOT  v2.0")
    logging.info("  NO REAL TRADES WILL BE EXECUTED")
    logging.info("=" * 62)
    logging.info("  Poll interval   : %ds", CHECK_INTERVAL)
    logging.info("  Min margin      : %.2f%%", MIN_PROFIT_MARGIN * 100)
    logging.info("  Ledger file     : %s", LEDGER_FILE)
    logging.info("  Max retries     : %d (backoff: %.0fx)", MAX_RETRIES, RETRY_BACKOFF_FACTOR)
    logging.info("  Events tracked  : %d", len(active_events))
    for ev in active_events:
        logging.info("    • %s", ev["name"])
    logging.info("=" * 62)


# ====================================================================
# Main loop
# ====================================================================

async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not EVENTS:
        logging.error("No events configured. Add at least one entry to the EVENTS list.")
        sys.exit(1)

    # Working copy so we can remove resolved events without touching the constant
    active_events: list[dict] = list(EVENTS)

    fetcher = MarketFetcher()
    engine = ArbEngine(min_margin=MIN_PROFIT_MARGIN)
    trader = PaperTrader(path=LEDGER_FILE)

    _print_banner(active_events)

    cycle = 0
    total_opps = 0

    try:
        while active_events:
            cycle += 1
            logging.info(
                "─── Cycle %d (%d active events) ───", cycle, len(active_events)
            )

            resolved_indices: list[int] = []

            for idx, event_cfg in enumerate(active_events):
                name = event_cfg["name"]
                ticker = event_cfg["kalshi_ticker"]
                cond_id = event_cfg["poly_condition_id"]

                # ── Check if either market has resolved ──────────
                kalshi_live, poly_live = await asyncio.gather(
                    fetcher.check_kalshi_active(ticker),
                    fetcher.check_poly_active(cond_id),
                )

                if not kalshi_live or not poly_live:
                    logging.info(
                        "[%s] Market resolved — will remove from active list.",
                        name,
                    )
                    resolved_indices.append(idx)
                    continue

                # ── Fetch prices concurrently ────────────────────
                kalshi_snap, poly_snap = await fetcher.fetch_both(ticker, cond_id)

                if kalshi_snap is None or poly_snap is None:
                    logging.warning(
                        "[%s] Skipped — could not fetch data from one or both platforms.",
                        name,
                    )
                    continue

                # ── Log current top-of-book ──────────────────────
                logging.info(
                    "[%s] Kalshi   Yes Ask=%s  No Ask=%s",
                    name,
                    _fmt_price(kalshi_snap.yes.best_ask),
                    _fmt_price(kalshi_snap.no.best_ask),
                )
                logging.info(
                    "[%s] Poly     Yes Ask=%s  No Ask=%s",
                    name,
                    _fmt_price(poly_snap.yes.best_ask),
                    _fmt_price(poly_snap.no.best_ask),
                )

                # ── Check for arbitrage ──────────────────────────
                opps = engine.check(name, kalshi_snap, poly_snap)

                if opps:
                    for opp in opps:
                        total_opps += 1
                        logging.info(
                            "*** OPPORTUNITY! [%s] %s │ "
                            "Spread: %.2f%% │ Cost: $%.4f │ Profit: $%.4f ***",
                            opp.event_name,
                            opp.direction,
                            opp.profit * 100,
                            opp.total_cost,
                            opp.profit,
                        )
                        trader.log(opp)
                else:
                    logging.info("[%s] No arb this cycle.", name)

            # ── Remove resolved events (reverse to keep indices valid) ──
            for idx in reversed(resolved_indices):
                removed = active_events.pop(idx)
                logging.info("Removed resolved event: %s", removed["name"])

            if not active_events:
                logging.info("All events have resolved. Exiting.")
                break

            logging.info(
                "Cycle %d complete — %d total opportunities logged. Sleeping %ds…",
                cycle, total_opps, CHECK_INTERVAL,
            )
            await asyncio.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        logging.info("")
        logging.info(
            "Shutting down gracefully (Ctrl+C). %d opportunities logged.", total_opps
        )
    finally:
        await fetcher.close()

    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
