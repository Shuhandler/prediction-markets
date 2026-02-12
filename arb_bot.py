#!/usr/bin/env python3
"""
==========================================================================
  Prediction Market Arbitrage — Paper-Trading Bot  (v3.0)
  Monitors Kalshi and Polymarket for cross-platform arbitrage opportunities.

  PAPER TRADING ONLY — no real orders are ever placed.
==========================================================================

  Dependencies (run once):
      pip install aiohttp

  Usage:
      1. Edit the EVENTS list at the bottom of the config section.
      2. Run:  python arb_bot.py
      3. Check data/trade_ledger.csv for logged opportunities.
      4. Check data/portfolio.csv for simulated positions and P&L.
"""

import asyncio
import csv
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp

# ====================================================================
# CONFIGURATION — edit these values to suit your needs
# ====================================================================

CHECK_INTERVAL: int = 5        # Seconds between each polling cycle
MIN_PROFIT_MARGIN: float = 0.05  # Minimum gross spread to log
DATA_DIR: str = "data"
LEDGER_FILE: str = os.path.join(DATA_DIR, "trade_ledger.csv")
PORTFOLIO_FILE: str = os.path.join(DATA_DIR, "portfolio.csv")
REQUEST_TIMEOUT: int = 10        # HTTP timeout in seconds

# How many consecutive cycles with no usable data before marking an
# event as "stale" and removing it from the active list.  This catches
# the common case where the game has ended but the APIs haven't
# officially settled the market yet.
STALE_CYCLE_THRESHOLD: int = 3

# ---- Fee rates ----
# Kalshi taker fee formula: round_up(rate × C × P × (1−P))
# Source: https://kalshi.com/docs/kalshi-fee-schedule.pdf
KALSHI_FEE_RATE: float = 0.07

# Polymarket taker fee — currently 0 for most markets.
# NCAAB/Serie A markets created after Feb 18 2026 will have fees.
# When active, the formula is the same shape: rate × C × P × (1−P)
# Set to ~0.0624 when trading fee-enabled markets (1.56% max at P=0.5).
POLY_FEE_RATE: float = 0.0

# ---- Paper portfolio ----
STARTING_CAPITAL: float = 1000.0  # Dollars to simulate with
POSITION_SIZE: float = 50.0       # Max dollars per trade (both legs)

# ---- Retry / backoff settings ----
MAX_RETRIES: int = 3
RETRY_BASE_DELAY: float = 1.0
RETRY_BACKOFF_FACTOR: float = 2.0

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
    {
        "name": "Bayern Munich vs Maccabi Tel-Aviv Winner?",
        "kalshi_ticker": "KXEUROLEAGUEGAME-26FEB121405BAYMTA-MTA",
        "poly_condition_id": "0x5fac1f28689e979b3be20c2d2a864d9e7b951833f35c28870a15a6b6f29bdfb5",
    },
    # Add more pairs here:
    # {
    #     "name": "...",
    #     "kalshi_ticker": "...",
    #     "poly_condition_id": "...",
    # },
]


# ====================================================================
# HELPERS
# ====================================================================

def _parse_iso(s: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string into an aware datetime (UTC).

    Handles the common formats returned by Kalshi and Polymarket,
    including the trailing ``Z`` that ``datetime.fromisoformat`` doesn't
    support until Python 3.11.
    """
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ====================================================================
# FEE COMPUTATION
# ====================================================================

def kalshi_taker_fee(price: float, contracts: int = 1) -> float:
    """
    Kalshi taker fee per the published formula:
        fee = round_up(RATE × C × P × (1 − P))
    where round_up = ceiling to the next cent.

    Returns the fee in dollars.
    """
    if KALSHI_FEE_RATE <= 0 or contracts <= 0:
        return 0.0
    raw = KALSHI_FEE_RATE * contracts * price * (1 - price)
    return math.ceil(raw * 100) / 100


def poly_taker_fee(price: float, contracts: int = 1) -> float:
    """
    Polymarket taker fee (same formula shape as Kalshi).
    Currently 0 for most markets.  Set POLY_FEE_RATE when trading
    fee-enabled markets.
    """
    if POLY_FEE_RATE <= 0 or contracts <= 0:
        return 0.0
    raw = POLY_FEE_RATE * contracts * price * (1 - price)
    return math.ceil(raw * 100) / 100


# ====================================================================
# DATA CLASSES
# ====================================================================

@dataclass
class TopOfBook:
    """Best bid and ask for one side (Yes or No) of a binary market."""
    best_bid: Optional[float] = None   # highest resting buy price
    best_ask: Optional[float] = None   # lowest resting sell price
    best_bid_size: Optional[float] = None  # depth at best bid
    best_ask_size: Optional[float] = None  # depth at best ask


@dataclass
class MarketSnapshot:
    """Top-of-book snapshot for both sides of a binary market."""
    yes: TopOfBook = field(default_factory=TopOfBook)
    no: TopOfBook = field(default_factory=TopOfBook)
    source: str = ""           # "kalshi" or "polymarket"
    price_source: str = ""     # "book" (real orderbook) or "indicative" (/price)


@dataclass
class ArbOpportunity:
    """A single detected arbitrage opportunity (per-contract values)."""
    timestamp: str
    event_name: str
    direction: str
    kalshi_price: float
    poly_price: float
    total_cost: float          # kalshi_price + poly_price (pre-fee)
    kalshi_fee: float          # Kalshi taker fee for 1 contract
    poly_fee: float            # Polymarket taker fee for 1 contract
    gross_profit: float        # 1.0 − total_cost
    net_profit: float          # 1.0 − total_cost − fees
    fetch_latency_ms: int      # wall-clock ms to fetch both platforms


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

                if resp.status >= 500:
                    delay = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** (attempt - 1))
                    logging.warning(
                        "[%s] Server error %d, retry in %.1fs (%d/%d)",
                        label, resp.status, delay, attempt, MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue

                if resp.status >= 400:
                    logging.error(
                        "[%s] Client error %d for %s — not retrying",
                        label, resp.status, url,
                    )
                    return None

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
    - Polymarket:  CLOB /book for real orderbook with depth (preferred),
                   falls back to CLOB /price if /book 404s.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._poly_token_cache: dict[str, tuple[str, str]] = {}
        self._kalshi_limiter = RateLimiter(KALSHI_MAX_RPS)
        self._poly_limiter = RateLimiter(POLY_MAX_RPS)
        # Track whether /book works to avoid repeated 404 probes
        self._poly_book_available: dict[str, bool] = {}

    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Accept": "application/json",
                    "User-Agent": "PredictionArbBot/3.0 (paper-trading)",
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
        """Return True if the Kalshi market is still open for trading.

        Checks three signals:
        1. Market ``status`` in a set of terminal states.
        2. ``result`` field is populated (outcome known).
        3. ``close_time`` / ``expiration_time`` is in the past.
        """
        await self._ensure_session()
        data = await _request_with_retry(
            self._session,
            f"{KALSHI_BASE}/markets/{ticker}",
            self._kalshi_limiter,
            label="Kalshi-status",
        )
        if data is None:
            return True  # assume active on transient error

        market = data.get("market", data)
        status = str(market.get("status", "")).lower()
        result = market.get("result", "")

        logging.debug(
            "[Kalshi] %s status=%r, result=%r, close_time=%r",
            ticker, status, result,
            market.get("close_time", market.get("expiration_time", "")),
        )

        # 1) Terminal status
        if status in (
            "settled", "closed", "finalized", "determined",
            "ceased_trading", "complete",
        ) or result:
            logging.info(
                "[Kalshi] Market %s resolved (status=%s, result=%s)",
                ticker, status, result,
            )
            return False

        # 2) Past close / expiration time
        for time_field in ("close_time", "expiration_time"):
            raw = market.get(time_field, "")
            close_dt = _parse_iso(str(raw)) if raw else None
            if close_dt and close_dt <= _utc_now():
                logging.info(
                    "[Kalshi] Market %s past %s (%s). Treating as resolved.",
                    ticker, time_field, raw,
                )
                return False

        return True

    async def check_poly_active(self, condition_id: str) -> bool:
        """Return True if the Polymarket market is still active.

        Checks three signals:
        1. ``closed`` is True or ``active`` is False.
        2. ``end_date_iso`` is in the past.
        3. Opportunistically caches token IDs from the response.
        """
        await self._ensure_session()
        data = await _request_with_retry(
            self._session,
            f"{CLOB_BASE}/markets/{condition_id}",
            self._poly_limiter,
            label="Poly-status",
        )
        if data is None:
            return True

        logging.debug(
            "[Polymarket] %s… closed=%r, active=%r, end_date_iso=%r",
            condition_id[:16],
            data.get("closed"), data.get("active"),
            data.get("end_date_iso", ""),
        )

        # 1) Explicit closed / inactive flag
        if data.get("closed") is True or data.get("active") is False:
            logging.info(
                "[Polymarket] Market %s… resolved (closed=%s, active=%s)",
                condition_id[:16], data.get("closed"), data.get("active"),
            )
            return False

        # 2) Past end date
        end_raw = data.get("end_date_iso", "")
        end_dt = _parse_iso(str(end_raw)) if end_raw else None
        if end_dt and end_dt <= _utc_now():
            logging.info(
                "[Polymarket] Market %s… past end_date_iso (%s). Treating as resolved.",
                condition_id[:16], end_raw,
            )
            return False

        # 3) Cache token IDs
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
            best_yes = max(yes_bids_raw, key=lambda lvl: lvl[0])
            yes.best_bid = best_yes[0] / 100.0
            yes.best_bid_size = best_yes[1]
            no.best_ask = (100 - best_yes[0]) / 100.0
            no.best_ask_size = best_yes[1]  # depth is the same (implied)

        if no_bids_raw:
            best_no = max(no_bids_raw, key=lambda lvl: lvl[0])
            no.best_bid = best_no[0] / 100.0
            no.best_bid_size = best_no[1]
            yes.best_ask = (100 - best_no[0]) / 100.0
            yes.best_ask_size = best_no[1]

        return MarketSnapshot(yes=yes, no=no, source="kalshi", price_source="book")

    # ----------------------------------------------------------------
    #  Polymarket — token resolution (cached)
    # ----------------------------------------------------------------

    async def _resolve_poly_tokens(self, condition_id: str) -> Optional[tuple[str, str]]:
        """
        Resolve a condition_id into (yes_token_id, no_token_id).
        Returns cached tokens if available.
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
    #  Polymarket — /book endpoint (preferred, real orderbook)
    # ----------------------------------------------------------------

    async def _fetch_poly_book(self, token_id: str) -> Optional[dict]:
        """
        Try GET /book?token_id=… for real orderbook data with depth.
        Returns the JSON response or None if 404 / error.
        Does NOT log errors for 404 (expected for some markets).
        """
        await self._ensure_session()
        await self._poly_limiter.acquire()
        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            async with self._session.get(
                f"{CLOB_BASE}/book",
                params={"token_id": token_id},
                timeout=timeout,
            ) as resp:
                if resp.status == 404:
                    return None  # expected for some markets
                if resp.status >= 400:
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    @staticmethod
    def _parse_book_tob(book: dict) -> TopOfBook:
        """Parse a /book response into a TopOfBook."""
        tob = TopOfBook()
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if bids:
            # Bids sorted desc by price — first is best
            best = max(bids, key=lambda lvl: float(lvl.get("price", 0)))
            tob.best_bid = float(best["price"])
            tob.best_bid_size = float(best.get("size", 0))

        if asks:
            # Asks sorted asc by price — first is best (lowest)
            best = min(asks, key=lambda lvl: float(lvl.get("price", 999)))
            tob.best_ask = float(best["price"])
            tob.best_ask_size = float(best.get("size", 0))

        return tob

    # ----------------------------------------------------------------
    #  Polymarket — /price endpoint (fallback, indicative)
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
    #  Polymarket — full snapshot (tries /book, falls back to /price)
    # ----------------------------------------------------------------

    async def fetch_polymarket(self, condition_id: str) -> Optional[MarketSnapshot]:
        """
        Fetch top-of-book from Polymarket for both tokens.

        Tries /book first for real orderbook data with depth.
        Falls back to /price (indicative, no depth) if /book 404s.
        Remembers whether /book works to skip futile probes.
        """
        tokens = await self._resolve_poly_tokens(condition_id)
        if tokens is None:
            return None

        yes_token, no_token = tokens

        # ── Try /book if we haven't already determined it's unavailable ──
        if self._poly_book_available.get(condition_id, True):
            yes_book, no_book = await asyncio.gather(
                self._fetch_poly_book(yes_token),
                self._fetch_poly_book(no_token),
            )

            if yes_book is not None and no_book is not None:
                self._poly_book_available[condition_id] = True
                yes_tob = self._parse_book_tob(yes_book)
                no_tob = self._parse_book_tob(no_book)
                return MarketSnapshot(
                    yes=yes_tob, no=no_tob,
                    source="polymarket", price_source="book",
                )

            # /book not available for this market — remember for future cycles
            self._poly_book_available[condition_id] = False
            logging.info(
                "[Polymarket] /book unavailable for %s…, using /price fallback",
                condition_id[:16],
            )

        # ── Fall back to /price (4 concurrent calls) ──
        yes_buy, yes_sell, no_buy, no_sell = await asyncio.gather(
            self._fetch_poly_price(yes_token, "BUY"),
            self._fetch_poly_price(yes_token, "SELL"),
            self._fetch_poly_price(no_token, "BUY"),
            self._fetch_poly_price(no_token, "SELL"),
        )

        yes_tob = TopOfBook(best_ask=yes_buy, best_bid=yes_sell)
        no_tob = TopOfBook(best_ask=no_buy, best_bid=no_sell)

        return MarketSnapshot(
            yes=yes_tob, no=no_tob,
            source="polymarket", price_source="indicative",
        )

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
    opportunities whose gross spread exceeds the configured minimum.

    Strategy (guaranteed $1.00 payout regardless of outcome):
      Direction A:  Buy YES on Kalshi  +  Buy NO on Polymarket
      Direction B:  Buy NO on Kalshi   +  Buy YES on Polymarket

    If total cost + fees < $1.00, the difference is risk-free profit.
    """

    def __init__(self, min_margin: float = MIN_PROFIT_MARGIN):
        self.min_margin = min_margin

    def check(
        self,
        event_name: str,
        kalshi: MarketSnapshot,
        poly: MarketSnapshot,
        fetch_latency_ms: int = 0,
    ) -> list[ArbOpportunity]:
        """Return a list of ArbOpportunity for every qualifying spread."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        opportunities: list[ArbOpportunity] = []

        # Direction A: Buy YES @ Kalshi  +  Buy NO @ Polymarket
        if kalshi.yes.best_ask is not None and poly.no.best_ask is not None:
            opp = self._evaluate(
                now, event_name,
                "Buy YES@Kalshi + Buy NO@Polymarket",
                kalshi.yes.best_ask, poly.no.best_ask,
                fetch_latency_ms,
            )
            if opp is not None:
                opportunities.append(opp)

        # Direction B: Buy NO @ Kalshi  +  Buy YES @ Polymarket
        if kalshi.no.best_ask is not None and poly.yes.best_ask is not None:
            opp = self._evaluate(
                now, event_name,
                "Buy NO@Kalshi + Buy YES@Polymarket",
                kalshi.no.best_ask, poly.yes.best_ask,
                fetch_latency_ms,
            )
            if opp is not None:
                opportunities.append(opp)

        return opportunities

    def _evaluate(
        self,
        timestamp: str,
        event_name: str,
        direction: str,
        k_price: float,
        p_price: float,
        fetch_ms: int,
    ) -> Optional[ArbOpportunity]:
        """Evaluate one direction, applying fees. Return opp or None."""
        cost = round(k_price + p_price, 6)
        gross_profit = round(1.0 - cost, 6)

        if gross_profit < self.min_margin:
            return None

        k_fee = kalshi_taker_fee(k_price, 1)
        p_fee = poly_taker_fee(p_price, 1)
        net_profit = round(1.0 - cost - k_fee - p_fee, 6)

        return ArbOpportunity(
            timestamp=timestamp,
            event_name=event_name,
            direction=direction,
            kalshi_price=k_price,
            poly_price=p_price,
            total_cost=cost,
            kalshi_fee=k_fee,
            poly_fee=p_fee,
            gross_profit=gross_profit,
            net_profit=net_profit,
            fetch_latency_ms=fetch_ms,
        )


# ====================================================================
# PaperTrader — CSV ledger for detected opportunities
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
        "kalshi_fee",
        "poly_fee",
        "gross_profit",
        "net_profit",
        "net_profit_pct",
        "fetch_ms",
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
            logging.info("Created trade ledger → %s", self.path)

    def log(self, opp: ArbOpportunity) -> None:
        """Append one row to the ledger CSV."""
        row = {
            "timestamp": opp.timestamp,
            "event_name": opp.event_name,
            "direction": opp.direction,
            "kalshi_price": f"{opp.kalshi_price:.4f}",
            "poly_price": f"{opp.poly_price:.4f}",
            "total_cost": f"{opp.total_cost:.4f}",
            "kalshi_fee": f"{opp.kalshi_fee:.4f}",
            "poly_fee": f"{opp.poly_fee:.4f}",
            "gross_profit": f"{opp.gross_profit:.4f}",
            "net_profit": f"{opp.net_profit:.4f}",
            "net_profit_pct": f"{opp.net_profit * 100:.2f}%",
            "fetch_ms": opp.fetch_latency_ms,
        }
        with open(self.path, mode="a", newline="") as fh:
            csv.DictWriter(fh, fieldnames=self.COLUMNS).writerow(row)


# ====================================================================
# PaperPortfolio — simulated capital & position management
# ====================================================================

@dataclass
class Position:
    """An open simulated position (both arb legs combined)."""
    event_name: str
    direction: str
    contracts: int
    cost_per_contract: float   # total_cost (pre-fee) per contract
    fees_per_contract: float   # kalshi_fee + poly_fee per contract
    total_outlay: float        # contracts × (cost + fees)
    opened_at: str


class PaperPortfolio:
    """
    Simulates a paper portfolio that opens positions when arb
    opportunities are detected and closes them when markets resolve.

    Each arb trade guarantees a $1.00 payout per contract (one leg
    always wins).  Profit = payout − cost − fees.
    """

    PORT_COLUMNS = [
        "action", "timestamp", "event_name", "direction", "contracts",
        "cost_per_contract", "fees_per_contract", "total_outlay",
        "payout", "pnl", "capital_remaining",
    ]

    def __init__(
        self,
        starting_capital: float = STARTING_CAPITAL,
        position_size: float = POSITION_SIZE,
        path: str = PORTFOLIO_FILE,
    ):
        self.starting_capital = starting_capital
        self.capital = starting_capital
        self.position_size = position_size
        self.path = path
        self.open_positions: list[Position] = []
        self.total_realized_pnl: float = 0.0
        self.closed_count: int = 0
        self._ensure_file()
        # Track which event+direction combos we've already traded
        self._traded: set[tuple[str, str]] = set()

    def _ensure_file(self) -> None:
        dir_path = os.path.dirname(self.path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, mode="w", newline="") as fh:
                csv.DictWriter(fh, fieldnames=self.PORT_COLUMNS).writeheader()
            logging.info("Created portfolio ledger → %s", self.path)

    def _write_row(self, row: dict) -> None:
        with open(self.path, mode="a", newline="") as fh:
            csv.DictWriter(fh, fieldnames=self.PORT_COLUMNS).writerow(row)

    def try_open(self, opp: ArbOpportunity) -> Optional[Position]:
        """
        Attempt to open a position for the given opportunity.

        Returns the Position if opened, None if skipped (already traded,
        insufficient capital, or net-unprofitable after fees).
        """
        key = (opp.event_name, opp.direction)

        # Don't double-trade the same event+direction
        if key in self._traded:
            return None

        # Only paper-trade if profitable after fees
        if opp.net_profit <= 0:
            return None

        cost_plus_fees = opp.total_cost + opp.kalshi_fee + opp.poly_fee
        if cost_plus_fees <= 0:
            return None

        # Size the trade
        max_contracts = int(min(self.position_size, self.capital) / cost_plus_fees)
        if max_contracts < 1:
            return None

        total_outlay = round(max_contracts * cost_plus_fees, 4)
        self.capital -= total_outlay
        self._traded.add(key)

        pos = Position(
            event_name=opp.event_name,
            direction=opp.direction,
            contracts=max_contracts,
            cost_per_contract=opp.total_cost,
            fees_per_contract=opp.kalshi_fee + opp.poly_fee,
            total_outlay=total_outlay,
            opened_at=opp.timestamp,
        )
        self.open_positions.append(pos)

        self._write_row({
            "action": "OPEN",
            "timestamp": opp.timestamp,
            "event_name": pos.event_name,
            "direction": pos.direction,
            "contracts": pos.contracts,
            "cost_per_contract": f"{pos.cost_per_contract:.4f}",
            "fees_per_contract": f"{pos.fees_per_contract:.4f}",
            "total_outlay": f"{pos.total_outlay:.4f}",
            "payout": "",
            "pnl": "",
            "capital_remaining": f"{self.capital:.2f}",
        })

        logging.info(
            "  PORTFOLIO: Opened %d contracts [%s] %s — outlay $%.2f, capital $%.2f",
            pos.contracts, pos.event_name, pos.direction,
            pos.total_outlay, self.capital,
        )
        return pos

    def close_positions_for_event(self, event_name: str) -> None:
        """
        Close all open positions for a resolved event.
        Arb guarantees $1.00 payout per contract.
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        still_open: list[Position] = []

        for pos in self.open_positions:
            if pos.event_name == event_name:
                payout = pos.contracts * 1.0
                pnl = round(payout - pos.total_outlay, 4)
                self.capital += payout
                self.total_realized_pnl += pnl
                self.closed_count += 1

                self._write_row({
                    "action": "CLOSE",
                    "timestamp": now,
                    "event_name": pos.event_name,
                    "direction": pos.direction,
                    "contracts": pos.contracts,
                    "cost_per_contract": f"{pos.cost_per_contract:.4f}",
                    "fees_per_contract": f"{pos.fees_per_contract:.4f}",
                    "total_outlay": f"{pos.total_outlay:.4f}",
                    "payout": f"{payout:.4f}",
                    "pnl": f"{pnl:.4f}",
                    "capital_remaining": f"{self.capital:.2f}",
                })

                logging.info(
                    "  PORTFOLIO: Closed %d contracts [%s] %s — "
                    "payout $%.2f, P&L $%.4f, capital $%.2f",
                    pos.contracts, pos.event_name, pos.direction,
                    payout, pnl, self.capital,
                )
            else:
                still_open.append(pos)

        self.open_positions = still_open

    def summary(self) -> str:
        """Return a multi-line summary string."""
        open_outlay = sum(p.total_outlay for p in self.open_positions)
        lines = [
            "─── PORTFOLIO SUMMARY ───",
            f"  Starting capital  : ${self.starting_capital:.2f}",
            f"  Current capital   : ${self.capital:.2f}",
            f"  Open positions    : {len(self.open_positions)} "
            f"(${open_outlay:.2f} locked)",
            f"  Closed trades     : {self.closed_count}",
            f"  Realized P&L      : ${self.total_realized_pnl:.4f}",
        ]
        if self.closed_count > 0:
            roi = (self.total_realized_pnl / self.starting_capital) * 100
            lines.append(f"  ROI               : {roi:.2f}%")
        return "\n".join(lines)


# ====================================================================
# Helpers
# ====================================================================

def _fmt_price(price: Optional[float]) -> str:
    """Format a price for logging, handling None gracefully."""
    return f"${price:.4f}" if price is not None else "  n/a  "


def _print_banner(active_events: list[dict]) -> None:
    """Print a startup banner with current configuration."""
    logging.info("=" * 62)
    logging.info("  ARBITRAGE PAPER-TRADING BOT  v3.0")
    logging.info("  NO REAL TRADES WILL BE EXECUTED")
    logging.info("=" * 62)
    logging.info("  Poll interval   : %ds", CHECK_INTERVAL)
    logging.info("  Min gross margin: %.2f%%", MIN_PROFIT_MARGIN * 100)
    logging.info("  Kalshi fee rate : %.2f%%", KALSHI_FEE_RATE * 100)
    logging.info("  Poly fee rate   : %.2f%%", POLY_FEE_RATE * 100)
    logging.info("  Starting capital: $%.2f", STARTING_CAPITAL)
    logging.info("  Position size   : $%.2f", POSITION_SIZE)
    logging.info("  Ledger file     : %s", LEDGER_FILE)
    logging.info("  Portfolio file  : %s", PORTFOLIO_FILE)
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

    active_events: list[dict] = list(EVENTS)

    fetcher = MarketFetcher()
    engine = ArbEngine(min_margin=MIN_PROFIT_MARGIN)
    trader = PaperTrader(path=LEDGER_FILE)
    portfolio = PaperPortfolio(
        starting_capital=STARTING_CAPITAL,
        position_size=POSITION_SIZE,
        path=PORTFOLIO_FILE,
    )

    _print_banner(active_events)

    cycle = 0
    total_opps = 0
    # Track consecutive cycles where we get no usable price data per event.
    # Once we exceed STALE_CYCLE_THRESHOLD, treat as resolved (the game
    # likely ended but the APIs haven't officially settled yet).
    stale_counts: dict[str, int] = {ev["name"]: 0 for ev in active_events}

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
                    portfolio.close_positions_for_event(name)
                    resolved_indices.append(idx)
                    continue

                # ── Fetch prices concurrently + measure latency ──
                loop = asyncio.get_running_loop()
                t0 = loop.time()
                kalshi_snap, poly_snap = await fetcher.fetch_both(ticker, cond_id)
                fetch_ms = int((loop.time() - t0) * 1000)

                if kalshi_snap is None or poly_snap is None:
                    stale_counts[name] = stale_counts.get(name, 0) + 1
                    logging.warning(
                        "[%s] Skipped — could not fetch data from one or both "
                        "platforms. (stale %d/%d)",
                        name, stale_counts[name], STALE_CYCLE_THRESHOLD,
                    )
                    if stale_counts[name] >= STALE_CYCLE_THRESHOLD:
                        logging.info(
                            "[%s] No data for %d consecutive cycles — "
                            "treating as resolved (game likely over).",
                            name, stale_counts[name],
                        )
                        portfolio.close_positions_for_event(name)
                        resolved_indices.append(idx)
                    continue

                # ── Check for empty orderbooks (both sides have no ask) ──
                kalshi_has_data = (
                    kalshi_snap.yes.best_ask is not None
                    or kalshi_snap.no.best_ask is not None
                )
                poly_has_data = (
                    poly_snap.yes.best_ask is not None
                    or poly_snap.no.best_ask is not None
                )
                if not kalshi_has_data and not poly_has_data:
                    stale_counts[name] = stale_counts.get(name, 0) + 1
                    logging.warning(
                        "[%s] Both orderbooks empty — no prices available. "
                        "(stale %d/%d)",
                        name, stale_counts[name], STALE_CYCLE_THRESHOLD,
                    )
                    if stale_counts[name] >= STALE_CYCLE_THRESHOLD:
                        logging.info(
                            "[%s] Empty orderbooks for %d consecutive cycles — "
                            "treating as resolved (game likely over).",
                            name, stale_counts[name],
                        )
                        portfolio.close_positions_for_event(name)
                        resolved_indices.append(idx)
                    continue

                # Got real data — reset the stale counter
                stale_counts[name] = 0

                # ── Log current top-of-book ──────────────────────
                logging.info(
                    "[%s] Kalshi   Yes Ask=%s  No Ask=%s",
                    name,
                    _fmt_price(kalshi_snap.yes.best_ask),
                    _fmt_price(kalshi_snap.no.best_ask),
                )
                poly_src = f"({poly_snap.price_source})"
                logging.info(
                    "[%s] Poly %s Yes Ask=%s  No Ask=%s  [%dms]",
                    name, poly_src,
                    _fmt_price(poly_snap.yes.best_ask),
                    _fmt_price(poly_snap.no.best_ask),
                    fetch_ms,
                )

                # ── Check for arbitrage ──────────────────────────
                opps = engine.check(name, kalshi_snap, poly_snap, fetch_ms)

                if opps:
                    for opp in opps:
                        total_opps += 1
                        net_label = (
                            f"Net: ${opp.net_profit:.4f}"
                            if opp.net_profit > 0
                            else f"Net: -${abs(opp.net_profit):.4f} (fees eat profit)"
                        )
                        logging.info(
                            "*** OPP [%s] %s │ "
                            "Gross: %.2f%% │ %s │ Fees: $%.4f ***",
                            opp.event_name,
                            opp.direction,
                            opp.gross_profit * 100,
                            net_label,
                            opp.kalshi_fee + opp.poly_fee,
                        )
                        trader.log(opp)
                        portfolio.try_open(opp)
                else:
                    logging.info("[%s] No arb this cycle.", name)

            # ── Remove resolved events ───────────────────────────
            for idx in reversed(resolved_indices):
                removed = active_events.pop(idx)
                stale_counts.pop(removed["name"], None)
                logging.info("Removed resolved event: %s", removed["name"])

            if not active_events:
                logging.info("All events have resolved. Exiting.")
                break

            logging.info(
                "Cycle %d complete — %d opps logged. Sleeping %ds…",
                cycle, total_opps, CHECK_INTERVAL,
            )
            await asyncio.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        logging.info("")
        logging.info(
            "Shutting down (Ctrl+C). %d opportunities logged.", total_opps
        )
    finally:
        await fetcher.close()
        # Print portfolio summary
        logging.info("")
        for line in portfolio.summary().split("\n"):
            logging.info(line)

    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
