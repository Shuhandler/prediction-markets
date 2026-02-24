#!/usr/bin/env python3
"""
==========================================================================
  Prediction Market Arbitrage — Paper-Trading Bot  (v5.0)
  Monitors Kalshi and Polymarket for cross-platform arbitrage opportunities.

  PAPER TRADING ONLY — no real orders are ever placed.
==========================================================================

  Architecture (v5.0 — four major improvements over v4.0):

    1. EVENT-DRIVEN: WebSocket updates trigger immediate arb checks via
       an asyncio.Queue. No more sleep-based polling loop. The bot reacts
       the instant a price update arrives. A stale-data watchdog forces
       reconnect (with fresh REST snapshot) if the WS goes silent.

    2. HIGH-PRECISION (Decimal): All monetary / probability calculations
       use decimal.Decimal to avoid float rounding errors like
       0.1 + 0.2 != 0.3. Eliminates false-positive arb detections.

    3. FILL OR KILL (FOK): Before executing a paper trade, the bot
       verifies that BOTH legs have orderbook depth >= desired trade size.
       If not, the entire order is Killed (rejected). No partial fills.

    4. ORDER MANAGEMENT SYSTEM (OMS): Full order lifecycle tracking
       (PENDING → FILLED / REJECTED). Strategy (ArbEngine) is separated
       from Execution (ExecutionEngine). Each order records its state,
       reject reason, and sizing details.

  Dependencies (run once):
      pip install aiohttp cryptography python-dotenv

  Environment variables (for Kalshi websocket — optional):
      KALSHI_API_KEY          — your Kalshi API key ID
      KALSHI_PRIVATE_KEY_PATH — path to your RSA private key PEM file

  If Kalshi credentials are not set, the Kalshi WS will be unavailable.
  Polymarket websocket requires no authentication.

  Usage:
      1. Edit events.json (or specify --events-file path).
      2. Run:  python arb_bot.py [--log-level DEBUG] [--dry-run]
      3. Check data/trade_ledger.csv for logged opportunities.
      4. Check data/orders.csv for full order lifecycle.
      5. Check data/portfolio.csv for simulated positions and P&L.
"""

import argparse
import asyncio
import base64
import csv
import decimal
import enum
import json
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

import aiohttp
from dotenv import load_dotenv

# Load the .env file into the environment
load_dotenv()

# ====================================================================
# DECIMAL CONTEXT — exact arithmetic for all monetary calculations
# ====================================================================
# 12 significant digits of precision: ample headroom for 4-decimal-place
# prices through intermediate multiplication, division, and fee formulas.
decimal.getcontext().prec = 12
decimal.getcontext().rounding = ROUND_HALF_UP

# Shorthand and constants
D = Decimal
ZERO = D("0")
ONE = D("1")
HUNDRED = D("100")
Q4 = D("0.0001")   # quantize to 4 decimal places (price precision)
Q2 = D("0.01")     # quantize to 2 decimal places (dollar amounts)

# ====================================================================
# CONFIGURATION — edit these values to suit your needs
# ====================================================================

# All tunable params read from environment (set via .env file) with
# sensible defaults.  CLI flags can override at runtime — see _parse_args().

MIN_PROFIT_MARGIN: Decimal = D(os.environ.get("MIN_PROFIT_MARGIN", "0.05"))
DATA_DIR: str = "data"
LEDGER_FILE: str = os.path.join(DATA_DIR, "trade_ledger.csv")
PORTFOLIO_FILE: str = os.path.join(DATA_DIR, "portfolio.csv")
ORDER_FILE: str = os.path.join(DATA_DIR, "orders.csv")
REQUEST_TIMEOUT: int = 10        # HTTP timeout in seconds

# How many seconds without any WS update before we force-reconnect.
STALE_TIMEOUT_SECONDS: int = int(os.environ.get("STALE_TIMEOUT_SECONDS", "30"))

# ---- Fee rates (Decimal) ----
# Kalshi taker fee formula: round_up(rate × C × P × (1−P))
# Source: https://kalshi.com/docs/kalshi-fee-schedule.pdf
KALSHI_FEE_RATE: Decimal = D(os.environ.get("KALSHI_FEE_RATE", "0.07"))

# Polymarket taker fee — currently 0 for most markets.
# NCAAB/Serie A markets created after Feb 18 2026 will have fees.
# When active, the formula is the same shape: rate × C × P × (1−P)
# Set to ~0.0624 when trading fee-enabled markets (1.56% max at P=0.5).
POLY_FEE_RATE: Decimal = D(os.environ.get("POLY_FEE_RATE", "0"))

# ---- Paper portfolio ----
STARTING_CAPITAL: Decimal = D(os.environ.get("STARTING_CAPITAL", "1000.00"))
POSITION_SIZE: Decimal = D(os.environ.get("POSITION_SIZE", "50.00"))

# ---- Retry / backoff settings ----
MAX_RETRIES: int = 3
RETRY_BASE_DELAY: float = 1.0
RETRY_BACKOFF_FACTOR: float = 2.0

# ---- Rate limits (conservative — well under documented limits) ----
# Kalshi Basic tier: 20 reads/sec;  Polymarket /price: 150 reads/sec
KALSHI_MAX_RPS: float = 5.0
POLY_MAX_RPS: float = 10.0

# ---- API base URLs (no trailing slash) ----
KALSHI_BASE = os.environ.get("KALSHI_BASE", "https://api.elections.kalshi.com/trade-api/v2")
KALSHI_WS_URL = os.environ.get("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2")
CLOB_BASE = os.environ.get("CLOB_BASE", "https://clob.polymarket.com")
POLY_WS_URL = os.environ.get("POLY_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market")

# ---- Kalshi API credentials (env vars) ----
KALSHI_API_KEY: str = os.environ.get("KALSHI_API_KEY", "")
KALSHI_PRIVATE_KEY_PATH: str = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

# ---- Websocket settings ----
WS_PING_INTERVAL: int = 10       # Seconds between heartbeat pings
WS_RECONNECT_BASE: float = 1.0   # Base delay for reconnection backoff
WS_RECONNECT_MAX: float = 30.0   # Max reconnection delay

# ---- Resolution check ----
RESOLUTION_CHECK_INTERVAL: int = 60  # Seconds between market-alive checks
# Hours after end_date_iso before treating a Polymarket market as resolved.
# Accounts for overtime, delayed settlement, and clock skew.
RESOLUTION_GRACE_HOURS: int = int(os.environ.get("RESOLUTION_GRACE_HOURS", "4"))

# ---- In-memory order cap ----
# Orders are always persisted to CSV immediately.  This cap limits the
# in-memory list to avoid unbounded growth during long-running sessions.
MAX_ORDERS_IN_MEMORY: int = int(os.environ.get("MAX_ORDERS_IN_MEMORY", "1000"))

# ---- Events file ----
DEFAULT_EVENTS_FILE: str = os.environ.get("EVENTS_FILE", "events.json")

# ====================================================================
# EVENT LOADING — reads from events.json (or --events-file path)
# ====================================================================

def _load_events(path: str) -> list[dict]:
    """Load events from a JSON file.

    The JSON must be a list of objects, each with:
      - name              : human-readable label
      - kalshi_ticker     : Kalshi market ticker
      - poly_condition_id : Polymarket condition_id (hex)
    """
    p = Path(path)
    if not p.exists():
        logging.error(
            "Events file '%s' not found. Create it or use --events-file.",
            path,
        )
        sys.exit(1)

    with open(p) as f:
        events = json.load(f)

    if not isinstance(events, list) or not events:
        logging.error(
            "Events file '%s' must contain a non-empty JSON array.", path,
        )
        sys.exit(1)

    required = {"name", "kalshi_ticker", "poly_condition_id"}
    for i, ev in enumerate(events):
        missing = required - set(ev.keys())
        if missing:
            logging.error(
                "Event #%d in '%s' missing keys: %s", i, path, missing,
            )
            sys.exit(1)

    logging.info("Loaded %d events from %s", len(events), path)
    return events


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


def _utc_iso() -> str:
    """Current UTC time as a compact ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ceil_penny(amount: Decimal) -> Decimal:
    """Round up to the next cent (ceiling)."""
    return amount.quantize(Q2, rounding=ROUND_CEILING)


# ====================================================================
# FEE COMPUTATION (Decimal-precise)
# ====================================================================

def kalshi_taker_fee(price: Decimal, contracts: int = 1) -> Decimal:
    """
    Kalshi taker fee per the published formula:
        fee = ceil_penny(RATE × C × P × (1 − P))

    Returns the fee in dollars (Decimal).
    """
    if KALSHI_FEE_RATE <= ZERO or contracts <= 0:
        return ZERO
    raw = KALSHI_FEE_RATE * D(contracts) * price * (ONE - price)
    return _ceil_penny(raw)


def poly_taker_fee(price: Decimal, contracts: int = 1) -> Decimal:
    """
    Polymarket taker fee (same formula shape as Kalshi).
    Currently 0 for most markets.  Set POLY_FEE_RATE when trading
    fee-enabled markets.
    """
    if POLY_FEE_RATE <= ZERO or contracts <= 0:
        return ZERO
    raw = POLY_FEE_RATE * D(contracts) * price * (ONE - price)
    return _ceil_penny(raw)


# ====================================================================
# DATA CLASSES (Decimal-based)
# ====================================================================

@dataclass
class OrderbookLevel:
    """A single price level in an orderbook."""
    price: Decimal   # 0.0000–1.0000
    size: Decimal    # number of contracts


@dataclass
class TopOfBook:
    """Best bid and ask for one side (Yes or No) of a binary market."""
    best_bid: Optional[Decimal] = None   # highest resting buy price
    best_ask: Optional[Decimal] = None   # lowest resting sell price
    best_bid_size: Optional[Decimal] = None  # depth at best bid
    best_ask_size: Optional[Decimal] = None  # depth at best ask
    # Full depth: sorted lists (bids descending, asks ascending)
    bid_levels: list[OrderbookLevel] = field(default_factory=list)
    ask_levels: list[OrderbookLevel] = field(default_factory=list)


@dataclass
class MarketSnapshot:
    """Top-of-book snapshot for both sides of a binary market."""
    yes: TopOfBook = field(default_factory=TopOfBook)
    no: TopOfBook = field(default_factory=TopOfBook)
    source: str = ""           # "kalshi" or "polymarket"
    price_source: str = ""     # "book" (real orderbook) or "indicative" (/price)


@dataclass
class ArbOpportunity:
    """A single detected arbitrage opportunity (per-contract values, Decimal)."""
    timestamp: str
    event_name: str
    direction: str
    kalshi_price: Decimal
    poly_price: Decimal
    total_cost: Decimal        # kalshi_price + poly_price (pre-fee)
    kalshi_fee: Decimal        # Kalshi taker fee for 1 contract
    poly_fee: Decimal          # Polymarket taker fee for 1 contract
    gross_profit: Decimal      # 1.0 − total_cost
    net_profit: Decimal        # 1.0 − total_cost − fees
    fetch_latency_ms: int = 0  # wall-clock ms to fetch both platforms
    max_contracts: int = 0     # max fillable contracts at these prices
    kalshi_depth: int = 0      # contracts available on Kalshi side
    poly_depth: int = 0        # contracts available on Polymarket side


# ====================================================================
# ORDER MANAGEMENT SYSTEM — data types
# ====================================================================

class OrderStatus(enum.Enum):
    """Lifecycle states for a simulated order."""
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class RejectReason(enum.Enum):
    """Reasons an order may be rejected."""
    NONE = ""
    INSUFFICIENT_CAPITAL = "INSUFFICIENT_CAPITAL"
    FOK_DEPTH_KALSHI = "FOK_DEPTH_KALSHI"       # Kalshi depth < desired qty
    FOK_DEPTH_POLY = "FOK_DEPTH_POLY"           # Poly depth < desired qty
    FOK_DEPTH_BOTH = "FOK_DEPTH_BOTH"           # Both sides insufficient
    DUPLICATE_TRADE = "DUPLICATE_TRADE"
    NET_UNPROFITABLE = "NET_UNPROFITABLE"
    ZERO_CONTRACTS = "ZERO_CONTRACTS"


@dataclass
class Order:
    """
    Tracks the full lifecycle of a simulated order.

    In production, this would map to actual exchange order IDs and
    include fill confirmations, partial fills (if supported), etc.
    """
    order_id: str
    timestamp: str
    event_name: str
    direction: str
    status: OrderStatus
    desired_contracts: int       # contracts we wanted to fill
    filled_contracts: int        # contracts actually filled (0 or desired for FOK)
    kalshi_price: Decimal
    poly_price: Decimal
    total_cost: Decimal
    kalshi_fee: Decimal
    poly_fee: Decimal
    gross_profit: Decimal
    net_profit: Decimal
    total_outlay: Decimal = ZERO
    reject_reason: RejectReason = RejectReason.NONE
    kalshi_depth: int = 0
    poly_depth: int = 0


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
# MarketFetcher — async REST API communication (Decimal-precise)
# ====================================================================

class MarketFetcher:
    """
    Retrieves top-of-book data from Kalshi and Polymarket via REST.

    Used as a fallback when WebSocket connections are unavailable, and
    for market resolution checks (which are always REST-based).

    All HTTP calls use retry + exponential backoff and respect per-host
    rate limiters.  Prices are parsed into Decimal for exact arithmetic.
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
                    "User-Agent": "PredictionArbBot/5.0 (paper-trading)",
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

        # 2) Past end date — with grace period to account for overtime,
        #    delayed settlement, or clock skew.
        end_raw = data.get("end_date_iso", "")
        end_dt = _parse_iso(str(end_raw)) if end_raw else None
        if end_dt and (end_dt + timedelta(hours=RESOLUTION_GRACE_HOURS)) <= _utc_now():
            logging.info(
                "[Polymarket] Market %s… past end_date_iso (%s) + %dh grace. "
                "Treating as resolved.",
                condition_id[:16], end_raw, RESOLUTION_GRACE_HOURS,
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
    #  Kalshi — orderbook (Decimal)
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
            sorted_yes = sorted(yes_bids_raw, key=lambda lvl: lvl[0], reverse=True)
            best_yes = sorted_yes[0]
            yes.best_bid = D(best_yes[0]) / HUNDRED
            yes.best_bid_size = D(str(best_yes[1]))
            yes.bid_levels = [
                OrderbookLevel(price=D(lvl[0]) / HUNDRED, size=D(str(lvl[1])))
                for lvl in sorted_yes
            ]
            # Implied No asks (ascending price = 1 - descending yes bid)
            no.best_ask = (HUNDRED - D(best_yes[0])) / HUNDRED
            no.best_ask_size = D(str(best_yes[1]))
            no.ask_levels = [
                OrderbookLevel(
                    price=(HUNDRED - D(lvl[0])) / HUNDRED,
                    size=D(str(lvl[1])),
                )
                for lvl in sorted_yes
            ]

        if no_bids_raw:
            sorted_no = sorted(no_bids_raw, key=lambda lvl: lvl[0], reverse=True)
            best_no = sorted_no[0]
            no.best_bid = D(best_no[0]) / HUNDRED
            no.best_bid_size = D(str(best_no[1]))
            no.bid_levels = [
                OrderbookLevel(price=D(lvl[0]) / HUNDRED, size=D(str(lvl[1])))
                for lvl in sorted_no
            ]
            # Implied Yes asks
            yes.best_ask = (HUNDRED - D(best_no[0])) / HUNDRED
            yes.best_ask_size = D(str(best_no[1]))
            yes.ask_levels = [
                OrderbookLevel(
                    price=(HUNDRED - D(lvl[0])) / HUNDRED,
                    size=D(str(lvl[1])),
                )
                for lvl in sorted_no
            ]

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
        """Parse a /book response into a TopOfBook with full depth (Decimal)."""
        tob = TopOfBook()
        bids = book.get("bids", []) or book.get("buys", [])
        asks = book.get("asks", []) or book.get("sells", [])

        if bids:
            sorted_bids = sorted(
                bids, key=lambda lvl: float(lvl.get("price", 0)), reverse=True,
            )
            tob.best_bid = D(str(sorted_bids[0]["price"]))
            tob.best_bid_size = D(str(sorted_bids[0].get("size", 0)))
            tob.bid_levels = [
                OrderbookLevel(
                    price=D(str(lvl["price"])),
                    size=D(str(lvl.get("size", 0))),
                )
                for lvl in sorted_bids
            ]

        if asks:
            sorted_asks = sorted(
                asks, key=lambda lvl: float(lvl.get("price", 999)),
            )
            tob.best_ask = D(str(sorted_asks[0]["price"]))
            tob.best_ask_size = D(str(sorted_asks[0].get("size", 0)))
            tob.ask_levels = [
                OrderbookLevel(
                    price=D(str(lvl["price"])),
                    size=D(str(lvl.get("size", 0))),
                )
                for lvl in sorted_asks
            ]

        return tob

    # ----------------------------------------------------------------
    #  Polymarket — /price endpoint (fallback, indicative)
    # ----------------------------------------------------------------

    async def _fetch_poly_price(self, token_id: str, side: str) -> Optional[Decimal]:
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
            price = D(str(data.get("price", 0)))
            return price if price > ZERO else None
        except (ValueError, TypeError, decimal.InvalidOperation):
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
    #  Fetch both platforms concurrently (REST)
    # ----------------------------------------------------------------

    async def fetch_both(
        self, ticker: str, condition_id: str,
    ) -> tuple[Optional[MarketSnapshot], Optional[MarketSnapshot]]:
        """Fetch Kalshi and Polymarket snapshots concurrently via REST."""
        return await asyncio.gather(
            self.fetch_kalshi(ticker),
            self.fetch_polymarket(condition_id),
        )


# ====================================================================
# WebSocket Managers — real-time orderbook streaming (Decimal)
# ====================================================================

class _LocalOrderbook:
    """Maintains a local orderbook from snapshots and deltas (Decimal).

    All prices and sizes are stored as Decimal for exact arithmetic.
    """

    def __init__(self) -> None:
        # {Decimal_price: Decimal_size} for bids and asks
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}

    def apply_snapshot_poly(self, data: dict) -> None:
        """Apply a Polymarket 'book' event (full snapshot)."""
        self.bids.clear()
        self.asks.clear()
        for lvl in data.get("buys", []):
            p, s = D(str(lvl["price"])), D(str(lvl["size"]))
            if s > ZERO:
                self.bids[p] = s
        for lvl in data.get("sells", []):
            p, s = D(str(lvl["price"])), D(str(lvl["size"]))
            if s > ZERO:
                self.asks[p] = s

    def apply_price_change_poly(self, change: dict) -> None:
        """Apply a Polymarket 'price_change' event (absolute size update)."""
        price = D(str(change["price"]))
        size = D(str(change["size"]))
        side = change["side"]  # "BUY" or "SELL"
        book = self.bids if side == "BUY" else self.asks
        if size > ZERO:
            book[price] = size
        else:
            book.pop(price, None)

    def apply_snapshot_kalshi(self, bids_raw: list) -> None:
        """Apply a Kalshi orderbook_snapshot (one side: 'yes' or 'no').
        bids_raw = [[price_cents, qty], ...]
        """
        self.bids.clear()
        for lvl in bids_raw:
            p = D(str(lvl[0])) / HUNDRED
            s = D(str(lvl[1]))
            if s > ZERO:
                self.bids[p] = s

    def apply_delta_kalshi(self, price_cents: int, delta: int) -> None:
        """Apply a Kalshi orderbook_delta."""
        price = D(str(price_cents)) / HUNDRED
        current = self.bids.get(price, ZERO)
        new_size = current + D(str(delta))
        if new_size > ZERO:
            self.bids[price] = new_size
        else:
            self.bids.pop(price, None)

    def to_tob(self) -> TopOfBook:
        """Convert current state to a TopOfBook (Decimal)."""
        tob = TopOfBook()
        if self.bids:
            sorted_bids = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)
            tob.best_bid = sorted_bids[0][0]
            tob.best_bid_size = sorted_bids[0][1]
            tob.bid_levels = [
                OrderbookLevel(price=p, size=s) for p, s in sorted_bids
            ]
        if self.asks:
            sorted_asks = sorted(self.asks.items(), key=lambda x: x[0])
            tob.best_ask = sorted_asks[0][0]
            tob.best_ask_size = sorted_asks[0][1]
            tob.ask_levels = [
                OrderbookLevel(price=p, size=s) for p, s in sorted_asks
            ]
        return tob


class PolymarketWS:
    """Manages a websocket connection to Polymarket's market channel.

    Maintains local orderbooks for subscribed tokens and provides
    snapshot access via get_snapshot().
    No authentication required.

    EVENT-DRIVEN: On every book update, puts the event_name onto the
    shared asyncio.Queue so the processor reacts immediately.  The
    put_nowait() call is non-blocking and never stalls the WS listener.
    """

    def __init__(self) -> None:
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        # token_id -> _LocalOrderbook
        self._books: dict[str, _LocalOrderbook] = {}
        self._connected = False
        self._subscribed_assets: list[str] = []
        self._ping_task: Optional[asyncio.Task] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._stale_task: Optional[asyncio.Task] = None
        self._reconnect_attempts = 0
        # condition_id -> (yes_token_id, no_token_id)
        self._token_map: dict[str, tuple[str, str]] = {}
        # condition_id -> event_name (for queue notifications)
        self._condition_to_event: dict[str, str] = {}
        # Shared queue for event-driven processing
        self._update_queue: Optional[asyncio.Queue] = None
        # Stale-data watchdog: updated on every incoming message
        self.last_update_time: float = 0.0
        # MarketFetcher reference — used to pull a fresh REST snapshot
        # after every reconnect so we never trade on stale delta-only data.
        self._fetcher: Optional["MarketFetcher"] = None

    def set_update_queue(self, queue: asyncio.Queue) -> None:
        """Attach the shared update queue for event-driven processing."""
        self._update_queue = queue

    def set_fetcher(self, fetcher: "MarketFetcher") -> None:
        """Attach MarketFetcher for post-reconnect REST snapshots."""
        self._fetcher = fetcher

    async def connect(self) -> None:
        """Open the websocket connection."""
        self._session = aiohttp.ClientSession()
        await self._do_connect()

    async def _do_connect(self) -> None:
        try:
            self._ws = await self._session.ws_connect(
                POLY_WS_URL,
                heartbeat=30,
                timeout=aiohttp.ClientWSTimeout(ws_close=15),
            )
            self._connected = True
            self._reconnect_attempts = 0
            logging.info("[Poly-WS] Connected to %s", POLY_WS_URL)

            # Re-subscribe if reconnecting
            if self._subscribed_assets:
                msg = {
                    "assets_ids": self._subscribed_assets,
                    "type": "market",
                }
                await self._ws.send_json(msg)
                logging.info(
                    "[Poly-WS] Re-subscribed to %d assets",
                    len(self._subscribed_assets),
                )

            # Start background tasks
            self._ping_task = asyncio.create_task(self._ping_loop())
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._stale_task = asyncio.create_task(self._stale_watchdog())

            # After (re)connect, pull a fresh REST snapshot so we aren't
            # relying solely on deltas that may have arrived out of order.
            await self._fetch_initial_snapshots()

        except Exception as exc:
            logging.error("[Poly-WS] Connection failed: %s", exc)
            self._connected = False
            await self._schedule_reconnect()

    async def _ping_loop(self) -> None:
        """Send PING every WS_PING_INTERVAL seconds."""
        try:
            while self._connected and self._ws and not self._ws.closed:
                await self._ws.send_str("PING")
                await asyncio.sleep(WS_PING_INTERVAL)
        except Exception:
            pass  # reconnect handled by listen loop

    async def _stale_watchdog(self) -> None:
        """Kill the WS if no data arrives within STALE_TIMEOUT_SECONDS.

        This replaces the old REST-polling fallback.  Instead of trading
        on potentially stale REST data, we force a reconnect so the next
        cycle starts with a fresh snapshot.
        """
        try:
            while self._connected and self._ws and not self._ws.closed:
                await asyncio.sleep(STALE_TIMEOUT_SECONDS)
                if self.last_update_time == 0.0:
                    continue  # haven't received first message yet
                elapsed = time.time() - self.last_update_time
                if elapsed > STALE_TIMEOUT_SECONDS:
                    logging.warning(
                        "[Poly-WS] Stale data — no update for %.0fs, "
                        "forcing reconnect…",
                        elapsed,
                    )
                    if self._ws and not self._ws.closed:
                        await self._ws.close()
                    break  # listen_loop will detect close → reconnect
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logging.warning("[Poly-WS] Stale watchdog error: %s", exc)

    async def _fetch_initial_snapshots(self) -> None:
        """Pull a full REST snapshot after every (re)connect.

        Ensures local books start from a known-good state rather than
        relying on WS deltas that may reference an unknown base.
        """
        if not self._fetcher:
            return
        for cid, (yes_tok, no_tok) in self._token_map.items():
            snap = await self._fetcher.fetch_polymarket(cid)
            if snap is None:
                logging.warning(
                    "[Poly-WS] REST snapshot unavailable for %s… after reconnect",
                    cid[:16],
                )
                continue
            # Overwrite local orderbooks with the fresh REST data
            if yes_tok in self._books:
                self._books[yes_tok] = _LocalOrderbook()
                for lvl in snap.yes.bid_levels:
                    self._books[yes_tok].bids[lvl.price] = lvl.size
                for lvl in snap.yes.ask_levels:
                    self._books[yes_tok].asks[lvl.price] = lvl.size
            if no_tok in self._books:
                self._books[no_tok] = _LocalOrderbook()
                for lvl in snap.no.bid_levels:
                    self._books[no_tok].bids[lvl.price] = lvl.size
                for lvl in snap.no.ask_levels:
                    self._books[no_tok].asks[lvl.price] = lvl.size
            logging.info(
                "[Poly-WS] Loaded REST snapshot for %s… after reconnect",
                cid[:16],
            )
        self.last_update_time = time.time()

    async def _listen_loop(self) -> None:
        """Process incoming websocket messages.

        This runs in its own asyncio task.  Message handling is
        synchronous and fast (just dict updates), so it never blocks
        the event loop.  The arb-check work is offloaded to the
        processor task via the queue.
        """
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if msg.data == "PONG":
                        continue
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    self.last_update_time = time.time()
                    # Poly WS can send a single object or an array of events
                    if isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict):
                                self._handle_message(item)
                    elif isinstance(data, dict):
                        self._handle_message(data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except Exception as exc:
            logging.warning("[Poly-WS] Listen error: %s", exc)

        self._connected = False
        logging.warning("[Poly-WS] Disconnected, scheduling reconnect…")
        await self._schedule_reconnect()

    def _handle_message(self, data: dict) -> None:
        event_type = data.get("event_type", "")

        if event_type == "book":
            asset_id = data.get("asset_id", "")
            if asset_id not in self._books:
                self._books[asset_id] = _LocalOrderbook()
            self._books[asset_id].apply_snapshot_poly(data)
            self._notify_update(asset_id)

        elif event_type == "price_change":
            for change in data.get("price_changes", []):
                asset_id = change.get("asset_id", "")
                if asset_id in self._books:
                    self._books[asset_id].apply_price_change_poly(change)
                    self._notify_update(asset_id)

    def _notify_update(self, token_id: str) -> None:
        """Non-blocking: put event_name on queue so the processor wakes up.

        Uses put_nowait() to ensure we never block the WS listener task.
        """
        if self._update_queue is None:
            return
        for cid, (yes_tok, no_tok) in self._token_map.items():
            if token_id in (yes_tok, no_tok):
                event_name = self._condition_to_event.get(cid)
                if event_name:
                    try:
                        self._update_queue.put_nowait(event_name)
                    except asyncio.QueueFull:
                        logging.warning(
                            "[Poly-WS] Update queue full, dropping update for %s",
                            event_name,
                        )
                break

    async def _schedule_reconnect(self) -> None:
        self._reconnect_attempts += 1
        delay = min(
            WS_RECONNECT_BASE * (2 ** (self._reconnect_attempts - 1)),
            WS_RECONNECT_MAX,
        )
        logging.info(
            "[Poly-WS] Reconnecting in %.1fs (attempt %d)…",
            delay, self._reconnect_attempts,
        )
        await asyncio.sleep(delay)
        await self._do_connect()

    async def subscribe(
        self,
        condition_id: str,
        yes_token: str,
        no_token: str,
        event_name: str,
    ) -> None:
        """Subscribe to orderbook updates for a market's tokens."""
        self._token_map[condition_id] = (yes_token, no_token)
        self._condition_to_event[condition_id] = event_name
        new_assets = []
        for tok in (yes_token, no_token):
            if tok not in self._books:
                self._books[tok] = _LocalOrderbook()
                new_assets.append(tok)
                self._subscribed_assets.append(tok)

        if new_assets and self._connected and self._ws and not self._ws.closed:
            msg = {"assets_ids": new_assets, "type": "market"}
            await self._ws.send_json(msg)
            logging.info(
                "[Poly-WS] Subscribed to %d new assets for %s…",
                len(new_assets), condition_id[:16],
            )

    def get_snapshot(self, condition_id: str) -> Optional[MarketSnapshot]:
        """Build a MarketSnapshot from local orderbook state (Decimal)."""
        tokens = self._token_map.get(condition_id)
        if tokens is None:
            return None
        yes_tok, no_tok = tokens
        yes_book = self._books.get(yes_tok)
        no_book = self._books.get(no_tok)
        if yes_book is None or no_book is None:
            return None

        yes_tob = yes_book.to_tob()
        no_tob = no_book.to_tob()

        # Only return if we have at least some data
        if (yes_tob.best_bid is None and yes_tob.best_ask is None
                and no_tob.best_bid is None and no_tob.best_ask is None):
            return None

        return MarketSnapshot(
            yes=yes_tob, no=no_tob,
            source="polymarket", price_source="book",
        )

    async def close(self) -> None:
        self._connected = False
        if self._ping_task:
            self._ping_task.cancel()
        if self._listen_task:
            self._listen_task.cancel()
        if self._stale_task:
            self._stale_task.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        logging.info("[Poly-WS] Closed.")


class KalshiWS:
    """Manages an authenticated websocket connection to Kalshi.

    Requires KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH env vars.
    Maintains local orderbooks via orderbook_snapshot + orderbook_delta.

    EVENT-DRIVEN: On every book update, puts the event_name onto the
    shared asyncio.Queue so the processor reacts immediately.
    """

    def __init__(self) -> None:
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        # ticker -> {side: _LocalOrderbook} where side is "yes" or "no"
        self._books: dict[str, dict[str, _LocalOrderbook]] = {}
        self._connected = False
        self._subscribed_tickers: list[str] = []
        self._listen_task: Optional[asyncio.Task] = None
        self._stale_task: Optional[asyncio.Task] = None
        self._reconnect_attempts = 0
        self._msg_id = 0
        self._private_key = None
        # ticker -> event_name (for queue notifications)
        self._ticker_to_event: dict[str, str] = {}
        # Shared queue for event-driven processing
        self._update_queue: Optional[asyncio.Queue] = None
        # Stale-data watchdog: updated on every incoming message
        self.last_update_time: float = 0.0
        # MarketFetcher reference — used for post-reconnect REST snapshots
        self._fetcher: Optional["MarketFetcher"] = None

    def set_update_queue(self, queue: asyncio.Queue) -> None:
        """Attach the shared update queue for event-driven processing."""
        self._update_queue = queue

    def set_fetcher(self, fetcher: "MarketFetcher") -> None:
        """Attach MarketFetcher for post-reconnect REST snapshots."""
        self._fetcher = fetcher

    def _load_private_key(self):
        """Load RSA private key for Kalshi auth."""
        if self._private_key is not None:
            return self._private_key

        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        key_path = Path(KALSHI_PRIVATE_KEY_PATH).expanduser()
        if not key_path.exists():
            raise FileNotFoundError(
                f"Kalshi private key not found at {key_path}. "
                "Set KALSHI_PRIVATE_KEY_PATH env var."
            )
        self._private_key = load_pem_private_key(
            key_path.read_bytes(), password=None,
        )
        return self._private_key

    def _sign_ws_request(self) -> dict[str, str]:
        """Generate auth headers for the Kalshi WS handshake."""
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives import hashes

        if not KALSHI_API_KEY:
            raise ValueError("KALSHI_API_KEY env var is required for websocket auth.")

        key = self._load_private_key()
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}GET/trade-api/ws/v2"
        signature = key.sign(
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(signature).decode()

        return {
            "KALSHI-ACCESS-KEY": KALSHI_API_KEY,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }

    async def connect(self) -> None:
        """Open the authenticated websocket connection."""
        self._session = aiohttp.ClientSession()
        await self._do_connect()

    async def _do_connect(self) -> None:
        try:
            headers = self._sign_ws_request()
            self._ws = await self._session.ws_connect(
                KALSHI_WS_URL,
                headers=headers,
                heartbeat=30,
                timeout=aiohttp.ClientWSTimeout(ws_close=15),
            )
            self._connected = True
            self._reconnect_attempts = 0
            logging.info("[Kalshi-WS] Connected to %s", KALSHI_WS_URL)

            # Re-subscribe if reconnecting
            if self._subscribed_tickers:
                self._msg_id += 1
                msg = {
                    "id": self._msg_id,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta"],
                        "market_tickers": self._subscribed_tickers,
                    },
                }
                await self._ws.send_json(msg)
                logging.info(
                    "[Kalshi-WS] Re-subscribed to %d tickers",
                    len(self._subscribed_tickers),
                )

            self._listen_task = asyncio.create_task(self._listen_loop())
            self._stale_task = asyncio.create_task(self._stale_watchdog())

            # After (re)connect, pull a fresh REST snapshot
            await self._fetch_initial_snapshots()

        except Exception as exc:
            logging.error("[Kalshi-WS] Connection failed: %s", exc)
            self._connected = False
            await self._schedule_reconnect()

    async def _stale_watchdog(self) -> None:
        """Kill the WS if no data arrives within STALE_TIMEOUT_SECONDS."""
        try:
            while self._connected and self._ws and not self._ws.closed:
                await asyncio.sleep(STALE_TIMEOUT_SECONDS)
                if self.last_update_time == 0.0:
                    continue
                elapsed = time.time() - self.last_update_time
                if elapsed > STALE_TIMEOUT_SECONDS:
                    logging.warning(
                        "[Kalshi-WS] Stale data — no update for %.0fs, "
                        "forcing reconnect…",
                        elapsed,
                    )
                    if self._ws and not self._ws.closed:
                        await self._ws.close()
                    break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logging.warning("[Kalshi-WS] Stale watchdog error: %s", exc)

    async def _fetch_initial_snapshots(self) -> None:
        """Pull a full REST snapshot after every (re)connect.

        Clears local books and repopulates from REST so we never trade
        on stale delta-only data.
        """
        if not self._fetcher:
            return
        for ticker in self._subscribed_tickers:
            snap = await self._fetcher.fetch_kalshi(ticker)
            if snap is None:
                logging.warning(
                    "[Kalshi-WS] REST snapshot unavailable for %s after reconnect",
                    ticker,
                )
                continue
            # Rebuild local books from fresh REST data
            self._books[ticker] = {
                "yes": _LocalOrderbook(),
                "no": _LocalOrderbook(),
            }
            for lvl in snap.yes.bid_levels:
                self._books[ticker]["yes"].bids[lvl.price] = lvl.size
            for lvl in snap.no.bid_levels:
                self._books[ticker]["no"].bids[lvl.price] = lvl.size
            logging.info(
                "[Kalshi-WS] Loaded REST snapshot for %s after reconnect",
                ticker,
            )
        self.last_update_time = time.time()

    async def _listen_loop(self) -> None:
        """Process incoming websocket messages.

        Runs in its own asyncio task.  Dict updates are fast and
        synchronous; the arb-check work is offloaded to the processor
        task via the queue.
        """
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    self.last_update_time = time.time()
                    self._handle_message(data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except Exception as exc:
            logging.warning("[Kalshi-WS] Listen error: %s", exc)

        self._connected = False
        logging.warning("[Kalshi-WS] Disconnected, scheduling reconnect…")
        await self._schedule_reconnect()

    def _handle_message(self, data: dict) -> None:
        msg_type = data.get("type", "")

        if msg_type == "orderbook_snapshot":
            inner = data.get("msg", {})
            ticker = inner.get("market_ticker", "")
            if ticker not in self._books:
                self._books[ticker] = {
                    "yes": _LocalOrderbook(),
                    "no": _LocalOrderbook(),
                }
            self._books[ticker]["yes"].apply_snapshot_kalshi(
                inner.get("yes", []),
            )
            self._books[ticker]["no"].apply_snapshot_kalshi(
                inner.get("no", []),
            )
            self._notify_update(ticker)

        elif msg_type == "orderbook_delta":
            inner = data.get("msg", {})
            ticker = inner.get("market_ticker", "")
            side = inner.get("side", "")
            if ticker in self._books and side in self._books[ticker]:
                self._books[ticker][side].apply_delta_kalshi(
                    inner.get("price", 0), inner.get("delta", 0),
                )
                self._notify_update(ticker)

        elif msg_type == "error":
            logging.error(
                "[Kalshi-WS] Error code=%s: %s",
                data.get("code"), data.get("msg"),
            )

    def _notify_update(self, ticker: str) -> None:
        """Non-blocking: put event_name on queue so the processor wakes up."""
        if self._update_queue is None:
            return
        event_name = self._ticker_to_event.get(ticker)
        if event_name:
            try:
                self._update_queue.put_nowait(event_name)
            except asyncio.QueueFull:
                logging.warning(
                    "[Kalshi-WS] Update queue full, dropping update for %s",
                    event_name,
                )

    async def _schedule_reconnect(self) -> None:
        self._reconnect_attempts += 1
        delay = min(
            WS_RECONNECT_BASE * (2 ** (self._reconnect_attempts - 1)),
            WS_RECONNECT_MAX,
        )
        logging.info(
            "[Kalshi-WS] Reconnecting in %.1fs (attempt %d)…",
            delay, self._reconnect_attempts,
        )
        await asyncio.sleep(delay)
        await self._do_connect()

    async def subscribe(self, ticker: str, event_name: str) -> None:
        """Subscribe to orderbook updates for a ticker."""
        self._ticker_to_event[ticker] = event_name
        if ticker not in self._books:
            self._books[ticker] = {
                "yes": _LocalOrderbook(),
                "no": _LocalOrderbook(),
            }
        if ticker not in self._subscribed_tickers:
            self._subscribed_tickers.append(ticker)

        if self._connected and self._ws and not self._ws.closed:
            self._msg_id += 1
            msg = {
                "id": self._msg_id,
                "cmd": "subscribe",
                "params": {
                    "channels": ["orderbook_delta"],
                    "market_tickers": [ticker],
                },
            }
            await self._ws.send_json(msg)
            logging.info("[Kalshi-WS] Subscribed to %s", ticker)

    def get_snapshot(self, ticker: str) -> Optional[MarketSnapshot]:
        """Build a MarketSnapshot from local orderbook state (Decimal).

        Kalshi books store bids per side. Asks are implied:
            Ask(Yes) = 1.0 - Bid(No)
            Ask(No)  = 1.0 - Bid(Yes)
        """
        sides = self._books.get(ticker)
        if sides is None:
            return None

        yes_bids_tob = sides["yes"].to_tob()
        no_bids_tob = sides["no"].to_tob()

        yes = TopOfBook()
        no = TopOfBook()

        # Yes bids -> Yes bid, implied No ask
        if yes_bids_tob.best_bid is not None:
            yes.best_bid = yes_bids_tob.best_bid
            yes.best_bid_size = yes_bids_tob.best_bid_size
            yes.bid_levels = yes_bids_tob.bid_levels
            no.best_ask = (ONE - yes_bids_tob.best_bid).quantize(Q4)
            no.best_ask_size = yes_bids_tob.best_bid_size
            no.ask_levels = [
                OrderbookLevel(
                    price=(ONE - lvl.price).quantize(Q4), size=lvl.size,
                )
                for lvl in yes_bids_tob.bid_levels
            ]

        # No bids -> No bid, implied Yes ask
        if no_bids_tob.best_bid is not None:
            no.best_bid = no_bids_tob.best_bid
            no.best_bid_size = no_bids_tob.best_bid_size
            no.bid_levels = no_bids_tob.bid_levels
            yes.best_ask = (ONE - no_bids_tob.best_bid).quantize(Q4)
            yes.best_ask_size = no_bids_tob.best_bid_size
            yes.ask_levels = [
                OrderbookLevel(
                    price=(ONE - lvl.price).quantize(Q4), size=lvl.size,
                )
                for lvl in no_bids_tob.bid_levels
            ]

        if (yes.best_bid is None and yes.best_ask is None
                and no.best_bid is None and no.best_ask is None):
            return None

        return MarketSnapshot(
            yes=yes, no=no, source="kalshi", price_source="book",
        )

    async def close(self) -> None:
        self._connected = False
        if self._listen_task:
            self._listen_task.cancel()
        if self._stale_task:
            self._stale_task.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        logging.info("[Kalshi-WS] Closed.")


# ====================================================================
# ArbEngine — STRATEGY: price comparison & opportunity detection
# ====================================================================
# This class is purely analytical — it finds arb opportunities but does
# NOT execute trades.  Execution is handled by ExecutionEngine (below).

class ArbEngine:
    """
    Compares Kalshi and Polymarket snapshots and returns any arbitrage
    opportunities whose gross spread exceeds the configured minimum.

    Strategy (guaranteed $1.00 payout regardless of outcome):
      Direction A:  Buy YES on Kalshi  +  Buy NO on Polymarket
      Direction B:  Buy NO on Kalshi   +  Buy YES on Polymarket

    If total cost + fees < $1.00, the difference is risk-free profit.

    Position sizing is based on actual orderbook depth — the max
    fillable contracts is the minimum of available depth on both legs.

    All arithmetic uses Decimal for exact precision.
    """

    def __init__(self, min_margin: Decimal = MIN_PROFIT_MARGIN):
        self.min_margin = min_margin

    @staticmethod
    def _fillable_contracts(
        k_ask_levels: list[OrderbookLevel],
        p_ask_levels: list[OrderbookLevel],
    ) -> tuple[int, int, int]:
        """Walk both orderbooks to find how many contracts can be filled.

        For the arb to work at the top-of-book price, we can only fill
        as many contracts as are available at the best ask on BOTH sides.

        Returns (max_contracts, kalshi_depth_at_best, poly_depth_at_best).
        """
        k_depth = int(k_ask_levels[0].size) if k_ask_levels else 0
        p_depth = int(p_ask_levels[0].size) if p_ask_levels else 0
        fillable = min(k_depth, p_depth)
        return fillable, k_depth, p_depth

    def check(
        self,
        event_name: str,
        kalshi: MarketSnapshot,
        poly: MarketSnapshot,
        fetch_latency_ms: int = 0,
    ) -> list[ArbOpportunity]:
        """Return a list of ArbOpportunity for every qualifying spread."""
        now = _utc_iso()
        opportunities: list[ArbOpportunity] = []

        # Direction A: Buy YES @ Kalshi  +  Buy NO @ Polymarket
        if kalshi.yes.best_ask is not None and poly.no.best_ask is not None:
            opp = self._evaluate(
                now, event_name,
                "Buy YES@Kalshi + Buy NO@Polymarket",
                kalshi.yes.best_ask, poly.no.best_ask,
                kalshi.yes.ask_levels, poly.no.ask_levels,
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
                kalshi.no.ask_levels, poly.yes.ask_levels,
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
        k_price: Decimal,
        p_price: Decimal,
        k_ask_levels: list[OrderbookLevel],
        p_ask_levels: list[OrderbookLevel],
        fetch_ms: int,
    ) -> Optional[ArbOpportunity]:
        """Evaluate one direction, applying fees and depth. Return opp or None."""
        cost = (k_price + p_price).quantize(Q4)
        gross_profit = (ONE - cost).quantize(Q4)

        if gross_profit < self.min_margin:
            return None

        k_fee = kalshi_taker_fee(k_price, 1)
        p_fee = poly_taker_fee(p_price, 1)
        net_profit = (ONE - cost - k_fee - p_fee).quantize(Q4)

        max_contracts, k_depth, p_depth = self._fillable_contracts(
            k_ask_levels, p_ask_levels,
        )

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
            max_contracts=max_contracts,
            kalshi_depth=k_depth,
            poly_depth=p_depth,
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
        "max_contracts",
        "kalshi_depth",
        "poly_depth",
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
            "kalshi_price": f"{opp.kalshi_price.quantize(Q4)}",
            "poly_price": f"{opp.poly_price.quantize(Q4)}",
            "total_cost": f"{opp.total_cost.quantize(Q4)}",
            "kalshi_fee": f"{opp.kalshi_fee.quantize(Q4)}",
            "poly_fee": f"{opp.poly_fee.quantize(Q4)}",
            "gross_profit": f"{opp.gross_profit.quantize(Q4)}",
            "net_profit": f"{opp.net_profit.quantize(Q4)}",
            "net_profit_pct": f"{(opp.net_profit * HUNDRED).quantize(Q2)}%",
            "max_contracts": opp.max_contracts,
            "kalshi_depth": opp.kalshi_depth,
            "poly_depth": opp.poly_depth,
            "fetch_ms": opp.fetch_latency_ms,
        }
        with open(self.path, mode="a", newline="") as fh:
            csv.DictWriter(fh, fieldnames=self.COLUMNS).writerow(row)


# ====================================================================
# Position — simulated open position
# ====================================================================

@dataclass
class Position:
    """An open simulated position (both arb legs combined, Decimal)."""
    event_name: str
    direction: str
    contracts: int
    cost_per_contract: Decimal   # total_cost (pre-fee) per contract
    fees_per_contract: Decimal   # kalshi_fee + poly_fee per contract
    total_outlay: Decimal        # contracts × (cost + fees)
    opened_at: str


# ====================================================================
# PaperPortfolio — simulated capital & position management
# ====================================================================

class PaperPortfolio:
    """
    Simulates a paper portfolio that tracks positions and capital.

    Position opening is driven by the ExecutionEngine (not directly
    by the portfolio).  The portfolio exposes methods for:
      - open_position(): add a position, deduct capital, log to CSV
      - close_positions_for_event(): settle positions, credit payout
      - summary(): human-readable status

    Each arb trade guarantees a $1.00 payout per contract (one leg
    always wins).  Profit = payout - cost - fees.
    """

    PORT_COLUMNS = [
        "action", "timestamp", "event_name", "direction", "contracts",
        "cost_per_contract", "fees_per_contract", "total_outlay",
        "payout", "pnl", "capital_remaining",
    ]

    def __init__(
        self,
        starting_capital: Decimal = STARTING_CAPITAL,
        position_size: Decimal = POSITION_SIZE,
        path: str = PORTFOLIO_FILE,
    ):
        self.starting_capital = starting_capital
        self.capital = starting_capital
        self.position_size = position_size
        self.path = path
        self.open_positions: list[Position] = []
        self.total_realized_pnl: Decimal = ZERO
        self.closed_count: int = 0
        self._ensure_file()

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

    def open_position(self, pos: Position) -> None:
        """Add a position and deduct capital.  Called by ExecutionEngine."""
        self.capital -= pos.total_outlay
        self.open_positions.append(pos)

        self._write_row({
            "action": "OPEN",
            "timestamp": pos.opened_at,
            "event_name": pos.event_name,
            "direction": pos.direction,
            "contracts": pos.contracts,
            "cost_per_contract": f"{pos.cost_per_contract.quantize(Q4)}",
            "fees_per_contract": f"{pos.fees_per_contract.quantize(Q4)}",
            "total_outlay": f"{pos.total_outlay.quantize(Q4)}",
            "payout": "",
            "pnl": "",
            "capital_remaining": f"{self.capital.quantize(Q2)}",
        })

        logging.info(
            "  PORTFOLIO: Opened %d contracts [%s] %s — outlay $%s, capital $%s",
            pos.contracts, pos.event_name, pos.direction,
            pos.total_outlay.quantize(Q2), self.capital.quantize(Q2),
        )

    def close_positions_for_event(self, event_name: str) -> None:
        """
        Close all open positions for a resolved event.
        Arb guarantees $1.00 payout per contract.
        """
        now = _utc_iso()
        still_open: list[Position] = []

        for pos in self.open_positions:
            if pos.event_name == event_name:
                payout = D(pos.contracts) * ONE
                pnl = (payout - pos.total_outlay).quantize(Q4)
                self.capital += payout
                self.total_realized_pnl += pnl
                self.closed_count += 1

                self._write_row({
                    "action": "CLOSE",
                    "timestamp": now,
                    "event_name": pos.event_name,
                    "direction": pos.direction,
                    "contracts": pos.contracts,
                    "cost_per_contract": f"{pos.cost_per_contract.quantize(Q4)}",
                    "fees_per_contract": f"{pos.fees_per_contract.quantize(Q4)}",
                    "total_outlay": f"{pos.total_outlay.quantize(Q4)}",
                    "payout": f"{payout.quantize(Q4)}",
                    "pnl": f"{pnl.quantize(Q4)}",
                    "capital_remaining": f"{self.capital.quantize(Q2)}",
                })

                logging.info(
                    "  PORTFOLIO: Closed %d contracts [%s] %s — "
                    "payout $%s, P&L $%s, capital $%s",
                    pos.contracts, pos.event_name, pos.direction,
                    payout.quantize(Q2), pnl.quantize(Q4),
                    self.capital.quantize(Q2),
                )
            else:
                still_open.append(pos)

        self.open_positions = still_open

    def summary(self) -> str:
        """Return a multi-line summary string."""
        open_outlay = sum(
            (p.total_outlay for p in self.open_positions), ZERO,
        )
        lines = [
            "─── PORTFOLIO SUMMARY ───",
            f"  Starting capital  : ${self.starting_capital.quantize(Q2)}",
            f"  Current capital   : ${self.capital.quantize(Q2)}",
            f"  Open positions    : {len(self.open_positions)} "
            f"(${open_outlay.quantize(Q2)} locked)",
            f"  Closed trades     : {self.closed_count}",
            f"  Realized P&L      : ${self.total_realized_pnl.quantize(Q4)}",
        ]
        if self.closed_count > 0:
            roi = (self.total_realized_pnl / self.starting_capital * HUNDRED)
            lines.append(f"  ROI               : {roi.quantize(Q2)}%")
        return "\n".join(lines)


# ====================================================================
# OrderLogger — CSV logger for order lifecycle
# ====================================================================

class OrderLogger:
    """Writes every Order (FILLED or REJECTED) to a CSV file."""

    COLUMNS = [
        "order_id", "timestamp", "status", "event_name", "direction",
        "desired_contracts", "filled_contracts",
        "kalshi_price", "poly_price", "total_cost",
        "kalshi_fee", "poly_fee", "gross_profit", "net_profit",
        "total_outlay", "reject_reason",
        "kalshi_depth", "poly_depth",
    ]

    def __init__(self, path: str = ORDER_FILE):
        self.path = path
        self._ensure()

    def _ensure(self) -> None:
        dir_path = os.path.dirname(self.path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, mode="w", newline="") as fh:
                csv.DictWriter(fh, fieldnames=self.COLUMNS).writeheader()
            logging.info("Created order log → %s", self.path)

    def log(self, order: Order) -> None:
        """Append one order to the CSV."""
        row = {
            "order_id": order.order_id,
            "timestamp": order.timestamp,
            "status": order.status.value,
            "event_name": order.event_name,
            "direction": order.direction,
            "desired_contracts": order.desired_contracts,
            "filled_contracts": order.filled_contracts,
            "kalshi_price": f"{order.kalshi_price.quantize(Q4)}",
            "poly_price": f"{order.poly_price.quantize(Q4)}",
            "total_cost": f"{order.total_cost.quantize(Q4)}",
            "kalshi_fee": f"{order.kalshi_fee.quantize(Q4)}",
            "poly_fee": f"{order.poly_fee.quantize(Q4)}",
            "gross_profit": f"{order.gross_profit.quantize(Q4)}",
            "net_profit": f"{order.net_profit.quantize(Q4)}",
            "total_outlay": f"{order.total_outlay.quantize(Q4)}",
            "reject_reason": order.reject_reason.value,
            "kalshi_depth": order.kalshi_depth,
            "poly_depth": order.poly_depth,
        }
        with open(self.path, mode="a", newline="") as fh:
            csv.DictWriter(fh, fieldnames=self.COLUMNS).writerow(row)


# ====================================================================
# ExecutionEngine — EXECUTION: OMS + Fill-or-Kill validation
# ====================================================================
# Separates the "should we trade?" question (ArbEngine, the Strategy)
# from the "can we trade?" and "execute the trade" steps (this class).
#
# PRODUCTION TODO: Replace the paper-trading fills below with real
# API calls:
#   Kalshi:      POST /trade-api/v2/portfolio/orders
#   Polymarket:  POST signed CLOB order via py_clob_client
#
# The validation pipeline (duplicate, profitability, capital, FOK)
# stays the same; only step 5 (fill) changes for live trading.

class ExecutionEngine:
    """
    Validates and executes (simulated) trades.

    Responsibilities:
      1. Duplicate check — don't re-trade the same event+direction.
      2. Net-profitability gate — skip if fees eat the spread.
      3. Capital check — ensure we have enough to cover the outlay.
      4. FOK depth check — BOTH legs must have depth >= desired qty.
         If not, the entire order is Killed (rejected). No partial fills.
      5. Fill — deduct capital, create Position, log Order.

    Every order (filled or rejected) is logged to data/orders.csv for
    post-session analysis.
    """

    def __init__(
        self,
        portfolio: PaperPortfolio,
        trader: PaperTrader,
        order_logger: Optional[OrderLogger] = None,
    ):
        self._portfolio = portfolio
        self._trader = trader
        self._order_logger = order_logger or OrderLogger()
        self._orders: list[Order] = []
        # Track which event+direction combos we've already traded
        self._traded: set[tuple[str, str]] = set()

    def submit(self, opp: ArbOpportunity) -> Order:
        """
        Submit a candidate arb trade for execution.

        Creates an Order in PENDING state, runs the validation pipeline,
        and transitions to FILLED or REJECTED.  Returns the finalized Order.

        The opportunity is always logged to the trade_ledger CSV regardless
        of whether the order fills.
        """
        # Log all detected opportunities regardless of fill outcome
        self._trader.log(opp)

        # Create pending order
        order = Order(
            order_id=str(uuid.uuid4())[:8],
            timestamp=opp.timestamp,
            event_name=opp.event_name,
            direction=opp.direction,
            status=OrderStatus.PENDING,
            desired_contracts=0,
            filled_contracts=0,
            kalshi_price=opp.kalshi_price,
            poly_price=opp.poly_price,
            total_cost=opp.total_cost,
            kalshi_fee=opp.kalshi_fee,
            poly_fee=opp.poly_fee,
            gross_profit=opp.gross_profit,
            net_profit=opp.net_profit,
            kalshi_depth=opp.kalshi_depth,
            poly_depth=opp.poly_depth,
        )

        # ── Validation pipeline ──

        # 1. Duplicate check
        key = (opp.event_name, opp.direction)
        if key in self._traded:
            order.status = OrderStatus.REJECTED
            order.reject_reason = RejectReason.DUPLICATE_TRADE
            self._finalize(order)
            return order

        # 2. Net profitability after fees
        if opp.net_profit <= ZERO:
            order.status = OrderStatus.REJECTED
            order.reject_reason = RejectReason.NET_UNPROFITABLE
            self._finalize(order)
            return order

        # 3. Compute desired contracts from capital and position size
        cost_plus_fees = opp.total_cost + opp.kalshi_fee + opp.poly_fee
        if cost_plus_fees <= ZERO:
            order.status = OrderStatus.REJECTED
            order.reject_reason = RejectReason.ZERO_CONTRACTS
            self._finalize(order)
            return order

        available = min(self._portfolio.position_size, self._portfolio.capital)
        desired = int(available / cost_plus_fees)
        order.desired_contracts = desired

        if desired < 1:
            order.status = OrderStatus.REJECTED
            order.reject_reason = RejectReason.INSUFFICIENT_CAPITAL
            self._finalize(order)
            return order

        # 4. Fill or Kill — BOTH legs must have depth >= desired qty
        #    If either side can't fill the full desired quantity at the
        #    stated price, reject the entire order.
        k_short = opp.kalshi_depth < desired
        p_short = opp.poly_depth < desired
        if k_short and p_short:
            order.status = OrderStatus.REJECTED
            order.reject_reason = RejectReason.FOK_DEPTH_BOTH
            self._finalize(order)
            return order
        if k_short:
            order.status = OrderStatus.REJECTED
            order.reject_reason = RejectReason.FOK_DEPTH_KALSHI
            self._finalize(order)
            return order
        if p_short:
            order.status = OrderStatus.REJECTED
            order.reject_reason = RejectReason.FOK_DEPTH_POLY
            self._finalize(order)
            return order

        # ── 5. All validations passed — FILL the order ──
        #
        # PRODUCTION TODO: Replace this block with real API calls:
        #
        #   # Kalshi leg
        #   kalshi_order = await kalshi_client.create_order(
        #       ticker=..., action="buy", side="yes"|"no",
        #       count=desired, type="limit", yes_price=...,
        #   )
        #
        #   # Polymarket leg
        #   poly_order = await poly_clob_client.create_and_post_order(
        #       OrderArgs(token_id=..., price=..., size=desired, side=BUY),
        #   )
        #
        #   # Wait for fill confirmations from both platforms.
        #   # Handle partial fills / rejections and unwind the other leg.

        total_outlay = (D(desired) * cost_plus_fees).quantize(Q4)
        order.filled_contracts = desired
        order.total_outlay = total_outlay
        order.status = OrderStatus.FILLED

        # Record in portfolio
        self._traded.add(key)
        pos = Position(
            event_name=opp.event_name,
            direction=opp.direction,
            contracts=desired,
            cost_per_contract=opp.total_cost,
            fees_per_contract=opp.kalshi_fee + opp.poly_fee,
            total_outlay=total_outlay,
            opened_at=opp.timestamp,
        )
        self._portfolio.open_position(pos)

        self._finalize(order)
        return order

    def _finalize(self, order: Order) -> None:
        """Log the order to CSV and store in memory for inspection.

        Orders are always persisted to CSV immediately.  The in-memory
        list is capped at MAX_ORDERS_IN_MEMORY to prevent unbounded
        growth during long-running sessions.
        """
        self._orders.append(order)
        self._order_logger.log(order)

        if len(self._orders) > MAX_ORDERS_IN_MEMORY:
            self._orders = self._orders[-MAX_ORDERS_IN_MEMORY:]

        if order.status == OrderStatus.FILLED:
            logging.info(
                "  ORDER %s FILLED: %d contracts [%s] %s — outlay $%s",
                order.order_id, order.filled_contracts,
                order.event_name, order.direction,
                order.total_outlay.quantize(Q2),
            )
        elif order.status == OrderStatus.REJECTED:
            logging.info(
                "  ORDER %s REJECTED: [%s] %s — %s "
                "(wanted %d, depth K=%d P=%d)",
                order.order_id,
                order.event_name, order.direction,
                order.reject_reason.value,
                order.desired_contracts,
                order.kalshi_depth, order.poly_depth,
            )

    @property
    def orders(self) -> list[Order]:
        """All orders submitted to this engine."""
        return list(self._orders)

    @property
    def filled_count(self) -> int:
        return sum(1 for o in self._orders if o.status == OrderStatus.FILLED)

    @property
    def rejected_count(self) -> int:
        return sum(1 for o in self._orders if o.status == OrderStatus.REJECTED)


# ====================================================================
# Helpers
# ====================================================================

def _fmt_price(price: Optional[Decimal]) -> str:
    """Format a price for logging, handling None gracefully."""
    return f"${price.quantize(Q4)}" if price is not None else "  n/a  "


# Module-level variable set by main() so _print_banner can display it.
_events_file_path: str = DEFAULT_EVENTS_FILE


def _print_banner(active_events: list[dict], mode: str = "event-driven") -> None:
    """Print a startup banner with current configuration."""
    logging.info("=" * 62)
    logging.info("  ARBITRAGE PAPER-TRADING BOT  v5.0")
    logging.info("  NO REAL TRADES WILL BE EXECUTED")
    logging.info("=" * 62)
    logging.info("  Data mode       : %s", mode)
    logging.info("  Execution model : event-driven (WS only, stale watchdog)")
    logging.info("  Order type      : Fill or Kill (FOK)")
    logging.info("  Precision       : decimal.Decimal (12 sig digits)")
    logging.info(
        "  Min gross margin: %s%%",
        (MIN_PROFIT_MARGIN * HUNDRED).quantize(Q2),
    )
    logging.info(
        "  Kalshi fee rate : %s%%",
        (KALSHI_FEE_RATE * HUNDRED).quantize(Q2),
    )
    logging.info(
        "  Poly fee rate   : %s%%",
        (POLY_FEE_RATE * HUNDRED).quantize(Q2),
    )
    logging.info("  Starting capital: $%s", STARTING_CAPITAL.quantize(Q2))
    logging.info("  Position size   : $%s", POSITION_SIZE.quantize(Q2))
    logging.info("  Stale timeout   : %ds", STALE_TIMEOUT_SECONDS)
    logging.info("  Ledger file     : %s", LEDGER_FILE)
    logging.info("  Order file      : %s", ORDER_FILE)
    logging.info("  Portfolio file  : %s", PORTFOLIO_FILE)
    logging.info("  Events file     : %s", _events_file_path)
    logging.info("  Events tracked  : %d", len(active_events))
    for ev in active_events:
        logging.info("    - %s", ev["name"])
    logging.info("=" * 62)


def _log_opportunity(opp: ArbOpportunity) -> None:
    """Log a detected arb opportunity with depth info."""
    net_label = (
        f"Net: ${opp.net_profit.quantize(Q4)}"
        if opp.net_profit > ZERO
        else f"Net: -${abs(opp.net_profit).quantize(Q4)} (fees eat profit)"
    )
    logging.info(
        "*** OPP [%s] %s | "
        "Gross: %s%% | %s | Fees: $%s | "
        "Depth: K=%d P=%d Fill=%d ***",
        opp.event_name,
        opp.direction,
        (opp.gross_profit * HUNDRED).quantize(Q2),
        net_label,
        (opp.kalshi_fee + opp.poly_fee).quantize(Q4),
        opp.kalshi_depth,
        opp.poly_depth,
        opp.max_contracts,
    )


# ====================================================================
# Event-driven main loop
# ====================================================================

async def _setup_websockets(
    active_events: list[dict],
    fetcher: MarketFetcher,
    update_queue: asyncio.Queue,
) -> tuple[Optional[KalshiWS], Optional[PolymarketWS]]:
    """Connect websockets, subscribe to all active events, and attach
    the shared update queue.

    Returns (kalshi_ws, poly_ws). Either may be None if connection fails.
    Without a WS connection the bot will NOT fall back to REST polling;
    it will simply have no data for that platform until WS reconnects.
    """
    kalshi_ws: Optional[KalshiWS] = None
    poly_ws: Optional[PolymarketWS] = None

    # ── Kalshi websocket (requires auth) ──
    if KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH:
        try:
            kalshi_ws = KalshiWS()
            kalshi_ws.set_update_queue(update_queue)
            kalshi_ws.set_fetcher(fetcher)
            await kalshi_ws.connect()
            for ev in active_events:
                await kalshi_ws.subscribe(ev["kalshi_ticker"], ev["name"])
            logging.info("[Kalshi-WS] Subscribed to %d markets.", len(active_events))
        except Exception as exc:
            logging.warning(
                "[Kalshi-WS] Failed to connect (%s). Will retry via reconnect logic.",
                exc,
            )
            kalshi_ws = None
    else:
        logging.warning(
            "[Kalshi] No API credentials found (KALSHI_API_KEY / "
            "KALSHI_PRIVATE_KEY_PATH). Kalshi data UNAVAILABLE."
        )

    # ── Polymarket websocket (no auth) ──
    try:
        poly_ws = PolymarketWS()
        poly_ws.set_update_queue(update_queue)
        poly_ws.set_fetcher(fetcher)
        await poly_ws.connect()

        # Resolve token IDs for each event, then subscribe
        for ev in active_events:
            cond_id = ev["poly_condition_id"]
            tokens = await fetcher._resolve_poly_tokens(cond_id)
            if tokens:
                await poly_ws.subscribe(cond_id, tokens[0], tokens[1], ev["name"])

        logging.info("[Poly-WS] Subscribed to %d markets.", len(active_events))
    except Exception as exc:
        logging.warning(
            "[Poly-WS] Failed to connect (%s). Will retry via reconnect logic.",
            exc,
        )
        poly_ws = None

    return kalshi_ws, poly_ws


async def _resolution_checker(
    active_events: list[dict],
    name_to_event: dict[str, dict],
    fetcher: MarketFetcher,
    portfolio: PaperPortfolio,
    stop_event: asyncio.Event,
) -> None:
    """Periodically check if any active markets have resolved.

    Runs every RESOLUTION_CHECK_INTERVAL seconds.  When a market
    resolves, closes open positions and removes the event from
    the active list.
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=RESOLUTION_CHECK_INTERVAL,
            )
            return  # stop_event was set
        except asyncio.TimeoutError:
            pass  # time to check

        if not active_events:
            return

        to_remove: list[dict] = []
        for ev in list(active_events):
            k_active, p_active = await asyncio.gather(
                fetcher.check_kalshi_active(ev["kalshi_ticker"]),
                fetcher.check_poly_active(ev["poly_condition_id"]),
            )
            if not k_active or not p_active:
                logging.info("[%s] Market resolved — removing.", ev["name"])
                portfolio.close_positions_for_event(ev["name"])
                to_remove.append(ev)

        for ev in to_remove:
            active_events.remove(ev)
            name_to_event.pop(ev["name"], None)
            logging.info("Removed resolved event: %s", ev["name"])

        if not active_events:
            logging.info("All events resolved.")
            stop_event.set()


async def _process_updates(
    queue: asyncio.Queue,
    active_events: list[dict],
    name_to_event: dict[str, dict],
    kalshi_ws: Optional[KalshiWS],
    poly_ws: Optional[PolymarketWS],
    engine: ArbEngine,
    execution: ExecutionEngine,
    stop_event: asyncio.Event,
) -> int:
    """Main event-driven processor.

    Reads event_name strings from the queue.  For each:
      1. Look up the event config.
      2. Get latest snapshots from both platforms (WS only).
      3. Run ArbEngine to detect opportunities.
      4. Submit any opportunities to ExecutionEngine for FOK validation & fill.

    Returns total number of opportunities detected.
    """
    total_opps = 0

    while not stop_event.is_set():
        # Wait for an update from a WS callback.
        # Timeout so we can check stop_event periodically.
        try:
            event_name = await asyncio.wait_for(queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            continue

        # Look up the event config
        event_cfg = name_to_event.get(event_name)
        if event_cfg is None or event_cfg not in active_events:
            continue

        ticker = event_cfg["kalshi_ticker"]
        cond_id = event_cfg["poly_condition_id"]

        try:
            # ── Get snapshots (WS only — no REST fallback) ──
            loop = asyncio.get_running_loop()
            t0 = loop.time()

            kalshi_snap: Optional[MarketSnapshot] = None
            if kalshi_ws:
                kalshi_snap = kalshi_ws.get_snapshot(ticker)

            poly_snap: Optional[MarketSnapshot] = None
            if poly_ws:
                poly_snap = poly_ws.get_snapshot(cond_id)

            fetch_ms = int((loop.time() - t0) * 1000)

            if kalshi_snap is None or poly_snap is None:
                continue

            # ── Log top-of-book for this update ──
            logging.debug(
                "[%s] K(WS) Yes=%s No=%s | P(WS) Yes=%s No=%s [%dms]",
                event_name,
                _fmt_price(kalshi_snap.yes.best_ask),
                _fmt_price(kalshi_snap.no.best_ask),
                _fmt_price(poly_snap.yes.best_ask),
                _fmt_price(poly_snap.no.best_ask),
                fetch_ms,
            )

            # ── Check for arbitrage ──
            opps = engine.check(event_name, kalshi_snap, poly_snap, fetch_ms)

            if opps:
                for opp in opps:
                    total_opps += 1
                    _log_opportunity(opp)
                    execution.submit(opp)

        except Exception as exc:
            logging.error(
                "Error processing update for %s: %s", event_name, exc,
                exc_info=True,
            )

    return total_opps


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments (Phase 0 — config overrides)."""
    parser = argparse.ArgumentParser(
        description="Prediction Market Arbitrage Bot",
    )
    parser.add_argument(
        "--events-file",
        default=DEFAULT_EVENTS_FILE,
        help="Path to events JSON file (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print configuration and exit without trading",
    )
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="Trading mode (default: %(default)s). "
             "'live' is not yet implemented — reserved for Phase 3.",
    )
    return parser.parse_args()


async def main() -> None:
    # ── Parse CLI args first (before configuring logging) ──
    args = _parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.mode == "live":
        logging.error("Live trading is not yet implemented (see scope.md Phase 3).")
        sys.exit(1)

    # ── Load events from JSON ──
    global _events_file_path
    _events_file_path = args.events_file
    events = _load_events(args.events_file)

    # ── Build event lookup structures ──
    active_events: list[dict] = list(events)
    name_to_event: dict[str, dict] = {ev["name"]: ev for ev in active_events}

    # ── Shared components ──
    update_queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
    stop_event = asyncio.Event()

    fetcher = MarketFetcher()
    engine = ArbEngine(min_margin=MIN_PROFIT_MARGIN)
    trader = PaperTrader(path=LEDGER_FILE)
    portfolio = PaperPortfolio(
        starting_capital=STARTING_CAPITAL,
        position_size=POSITION_SIZE,
        path=PORTFOLIO_FILE,
    )
    execution = ExecutionEngine(
        portfolio=portfolio,
        trader=trader,
    )

    # ── Connect WebSockets ──
    kalshi_ws, poly_ws = await _setup_websockets(
        active_events, fetcher, update_queue,
    )

    mode_parts = []
    if kalshi_ws:
        mode_parts.append("Kalshi=WS")
    else:
        mode_parts.append("Kalshi=NONE")
    if poly_ws:
        mode_parts.append("Poly=WS")
    else:
        mode_parts.append("Poly=NONE")
    mode_str = ", ".join(mode_parts)

    _print_banner(active_events, mode=mode_str)

    # ── Dry-run: print config and exit ──
    if args.dry_run:
        logging.info("--dry-run flag set. Exiting without trading.")
        if kalshi_ws:
            await kalshi_ws.close()
        if poly_ws:
            await poly_ws.close()
        await fetcher.close()
        sys.exit(0)

    # ── SIGTERM / SIGINT signal handlers for graceful shutdown ──
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    # ── Start background tasks ──
    bg_tasks: list[asyncio.Task] = []

    # Resolution checker: periodic background task
    bg_tasks.append(asyncio.create_task(
        _resolution_checker(
            active_events, name_to_event, fetcher, portfolio, stop_event,
        ),
        name="resolution-check",
    ))

    # Give websockets a moment to receive initial snapshots
    if kalshi_ws or poly_ws:
        logging.info("Waiting 3s for initial WS snapshots…")
        await asyncio.sleep(3)

    # ── Run the main event-driven processor ──
    total_opps = 0
    try:
        total_opps = await _process_updates(
            update_queue, active_events, name_to_event,
            kalshi_ws, poly_ws,
            engine, execution, stop_event,
        )
    finally:
        logging.info("")
        logging.info(
            "Shutting down. %d opportunities detected.", total_opps,
        )
        stop_event.set()

        # Cancel background tasks
        for task in bg_tasks:
            task.cancel()
        await asyncio.gather(*bg_tasks, return_exceptions=True)

        # Close connections
        if kalshi_ws:
            await kalshi_ws.close()
        if poly_ws:
            await poly_ws.close()
        await fetcher.close()

        # Print summary
        logging.info("")
        for line in portfolio.summary().split("\n"):
            logging.info(line)
        logging.info(
            "  Orders filed     : %d (%d filled, %d rejected)",
            len(execution.orders),
            execution.filled_count,
            execution.rejected_count,
        )

    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
