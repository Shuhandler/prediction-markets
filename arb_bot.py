#!/usr/bin/env python3
"""
==========================================================================
  Prediction Market Arbitrage Bot  (v6.0)
  Monitors Kalshi and Polymarket for cross-platform arbitrage opportunities.

  Supports both paper trading (simulated) and live trading (real orders).
==========================================================================

  Architecture (v6.0 — live execution on top of v5.0):

    1. EVENT-DRIVEN: WebSocket updates trigger immediate arb checks via
       an asyncio.Queue. No more sleep-based polling loop. The bot reacts
       the instant a price update arrives. A stale-data watchdog forces
       reconnect (with fresh REST snapshot) if the WS goes silent.

    2. HIGH-PRECISION (Decimal): All monetary / probability calculations
       use decimal.Decimal to avoid float rounding errors like
       0.1 + 0.2 != 0.3. Eliminates false-positive arb detections.

    3. FILL OR KILL (FOK): Before executing a trade, the bot verifies
       that BOTH legs have orderbook depth >= desired trade size.
       If not, the entire order is Killed (rejected). No partial fills.

    4. ORDER MANAGEMENT SYSTEM (OMS): Full order lifecycle tracking
       (PENDING → FILLED / REJECTED). Strategy (ArbEngine) is separated
       from Execution (ExecutionEngine). Each order records its state,
       reject reason, and sizing details.

    5. LIVE EXECUTION (Phase 3): Real order placement on both platforms.
       - Kalshi: IOC (Immediate-or-Cancel) limit orders via authenticated
         REST API, with WS fill tracking for confirmation.
       - Polymarket: FOK (Fill-or-Kill) orders via py_clob_client with
         EIP-712 signed orders on Polygon.
       - Sequential two-leg execution: Kalshi IOC partial fills
         dynamically resize the subsequent Polymarket FOK order.
       - Automatic unwind: if Leg 2 fails, Leg 1 is sold back at market.

  Dependencies (run once):
      pip install aiohttp cryptography python-dotenv py-clob-client

  Environment variables (paper mode — Kalshi websocket):
      KALSHI_API_KEY          — your Kalshi API key ID
      KALSHI_PRIVATE_KEY_PATH — path to your RSA private key PEM file

  Additional env vars (live mode — required for real order placement):
      POLY_API_KEY            — Polymarket CLOB API key
      POLY_API_SECRET         — Polymarket CLOB API secret
      POLY_PASSPHRASE         — Polymarket CLOB passphrase
      POLY_PRIVATE_KEY_PATH   — path to Ethereum private key file (hex)

  If Kalshi credentials are not set, the Kalshi WS will be unavailable.
  Polymarket websocket requires no authentication (read-only).

  Usage:
      Paper mode (default — no real orders):
        python arb_bot.py [--log-level DEBUG] [--dry-run]

      Live mode (real orders on both platforms):
        python arb_bot.py --mode live [--log-level DEBUG]

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

# Polymarket taker fee.
# Sports markets (NCAAB, Serie A, etc.) use a QUADRATIC formula:
#   fee = C × feeRate × (P × (1 − P))^exponent
# Per https://docs.polymarket.com/trading/fees:
#   Sports:        feeRate = 0.25,  exponent = 2
#   5/15-min Crypto: feeRate = 0.0175, exponent = 1
# Set POLY_FEE_EXPONENT=1 for crypto markets.
POLY_FEE_RATE: Decimal = D(os.environ.get("POLY_FEE_RATE", "0.25"))
POLY_FEE_EXPONENT: int = int(os.environ.get("POLY_FEE_EXPONENT", "2"))

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

# ---- Polymarket API credentials (env vars — Phase 3) ----
# The ETH private key is read from a file (like Kalshi) for security.
# CLOB API key/secret/passphrase are obtained from Polymarket's onboarding.
POLY_API_KEY: str = os.environ.get("POLY_API_KEY", "")
POLY_API_SECRET: str = os.environ.get("POLY_API_SECRET", "")
POLY_PASSPHRASE: str = os.environ.get("POLY_PASSPHRASE", "")
POLY_PRIVATE_KEY_PATH: str = os.environ.get("POLY_PRIVATE_KEY_PATH", "")
POLY_CHAIN_ID: int = int(os.environ.get("POLY_CHAIN_ID", "137"))  # Polygon mainnet

# ---- Kalshi fill poll interval (live mode) ----
KALSHI_FILL_POLL_MS: int = int(os.environ.get("KALSHI_FILL_POLL_MS", "250"))

# ---- Websocket settings ----
WS_PING_INTERVAL: int = 10       # Seconds between heartbeat pings
WS_RECONNECT_BASE: float = 1.0   # Base delay for reconnection backoff
WS_RECONNECT_MAX: float = 30.0   # Max reconnection delay

# ---- Resolution check ----
RESOLUTION_CHECK_INTERVAL: int = 60  # Seconds between market-alive checks

# ---- In-memory order cap ----
# Orders are always persisted to CSV immediately.  This cap limits the
# in-memory list to avoid unbounded growth during long-running sessions.
MAX_ORDERS_IN_MEMORY: int = int(os.environ.get("MAX_ORDERS_IN_MEMORY", "1000"))

# ---- Risk management ----
DAILY_LOSS_LIMIT: Decimal = D(os.environ.get("DAILY_LOSS_LIMIT", "100.00"))
MAX_TOTAL_EXPOSURE: Decimal = D(os.environ.get("MAX_TOTAL_EXPOSURE", "500.00"))
MAX_CONCURRENT_POSITIONS: int = int(os.environ.get("MAX_CONCURRENT_POSITIONS", "10"))
MAX_SPREAD_THRESHOLD: Decimal = D(os.environ.get("MAX_SPREAD_THRESHOLD", "0.20"))
MAX_ORDERS_PER_MINUTE: int = int(os.environ.get("MAX_ORDERS_PER_MINUTE", "10"))
KILL_FILE: str = os.environ.get("KILL_FILE", "KILL")

# ---- Unwind module (Phase 2) ----
UNWIND_TIMEOUT_SECONDS: int = int(os.environ.get("UNWIND_TIMEOUT_SECONDS", "5"))
STUCK_POSITION_MAX_AGE: int = int(os.environ.get("STUCK_POSITION_MAX_AGE", "30"))
STUCK_POSITION_CHECK_INTERVAL: int = int(os.environ.get("STUCK_POSITION_CHECK_INTERVAL", "10"))
UNWIND_LOG_FILE: str = os.path.join(DATA_DIR, "unwinds.csv")

# ---- Unwind sell price floor (Phase 3 safety) ----
# During an emergency unwind, the sell limit price is set to
#   max(buy_price * RATIO, ABSOLUTE_MIN)
# This prevents catastrophic slippage (selling at $0.01 what was
# bought at $0.55). Set RATIO to 0 to revert to aggressive $0.01 floor.
UNWIND_MIN_PRICE_RATIO: Decimal = D(os.environ.get("UNWIND_MIN_PRICE_RATIO", "0.50"))
UNWIND_ABSOLUTE_MIN_PRICE: Decimal = D(os.environ.get("UNWIND_ABSOLUTE_MIN_PRICE", "0.05"))

# ---- Events file ----
DEFAULT_EVENTS_FILE: str = os.environ.get("EVENTS_FILE", "events.json")

# ---- Discord notifications (Phase 4) ----
DISCORD_WEBHOOK_URL: str = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_MAX_POSTS_PER_SEC: float = float(os.environ.get("DISCORD_MAX_POSTS_PER_SEC", "5"))

# ====================================================================
# EVENT LOADING — reads from events.json (or --events-file path)
# ====================================================================

def _load_events(path: str) -> list[dict]:
    """Load events from a JSON file.

    The JSON must be a list of objects, each with:
      - name              : human-readable label
      - kalshi_ticker     : Kalshi market ticker
      - poly_condition_id : Polymarket condition_id (hex)
      - poly_yes_token    : Polymarket YES token_id (hex)
      - poly_no_token     : Polymarket NO token_id (hex)

    Token IDs are explicit to avoid the catastrophic bug of blindly
    mapping tokens[0] as YES — the API can return them in any order.
    Use ticker_return.py to generate entries with correct token IDs.
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

    required = {"name", "kalshi_ticker", "poly_condition_id",
                "poly_yes_token", "poly_no_token"}
    for i, ev in enumerate(events):
        missing = required - set(ev.keys())
        if missing:
            logging.error(
                "Event #%d in '%s' missing keys: %s", i, path, missing,
            )
            sys.exit(1)
        # Sanity: reject placeholder values
        for key in ("poly_yes_token", "poly_no_token"):
            if ev[key] in ("", "FILL_ME"):
                logging.error(
                    "Event #%d in '%s': %s is a placeholder — run "
                    "ticker_return.py to populate real token IDs.",
                    i, path, key,
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
    Polymarket taker fee per the official formula:
        fee = C × feeRate × (P × (1 − P))^exponent

    Sports markets (NCAAB, Serie A): rate=0.25, exponent=2
    5/15-min Crypto markets:         rate=0.0175, exponent=1

    Rounded to 4 decimal places (smallest fee = 0.0001 USDC).
    """
    if POLY_FEE_RATE <= ZERO or contracts <= 0:
        return ZERO
    p_factor = price * (ONE - price)
    raw = POLY_FEE_RATE * D(contracts) * (p_factor ** POLY_FEE_EXPONENT)
    return raw.quantize(Q4, rounding=ROUND_CEILING)


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
    # Phase 3: market identifiers for live execution
    kalshi_ticker: str = ""    # Kalshi market ticker (e.g. KXNBAGAME-26FEB24GSWNOP-GSW)
    poly_condition_id: str = "" # Polymarket condition_id (hex)


# ====================================================================
# ORDER MANAGEMENT SYSTEM — data types
# ====================================================================

class OrderStatus(enum.Enum):
    """Lifecycle states for a simulated order."""
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNWOUND = "UNWOUND"           # Leg 1 filled then sold back (legging risk realized)


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
    # Phase 1 — risk management reject reasons
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"        # Daily loss limit breached
    MAX_EXPOSURE = "MAX_EXPOSURE"                # Total exposure cap reached
    MAX_POSITIONS = "MAX_POSITIONS"              # Too many concurrent positions
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"          # Spread exceeds sanity threshold
    ORDER_RATE_LIMIT = "ORDER_RATE_LIMIT"         # Too many orders per minute
    KILL_SWITCH = "KILL_SWITCH"                   # Kill file detected


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
# Unwind Module — OMS data types (Phase 2)
# ====================================================================

class LegStatus(enum.Enum):
    """Lifecycle states for a single leg of a two-leg arb trade."""
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNWOUND = "UNWOUND"


@dataclass
class LegOrder:
    """A single leg of a two-leg arb trade.

    In paper mode, fills are instant.  In production (Phase 3), the
    exchange_order_id will map to a real exchange order for tracking.
    """
    leg_id: str
    platform: str              # "kalshi" or "polymarket"
    side: str                  # "yes" or "no"
    price: Decimal
    contracts: int
    status: LegStatus = LegStatus.PENDING
    filled_contracts: int = 0
    submitted_at: str = ""
    filled_at: str = ""
    exchange_order_id: str = ""  # real order ID from exchange (Phase 3)
    market_id: str = ""          # Kalshi ticker or Polymarket token_id


class UnwindStatus(enum.Enum):
    """Lifecycle states for a two-leg arb execution with unwind."""
    PENDING = "PENDING"
    LEG1_SUBMITTED = "LEG1_SUBMITTED"
    LEG1_FILLED = "LEG1_FILLED"
    LEG2_SUBMITTED = "LEG2_SUBMITTED"
    COMPLETE = "COMPLETE"          # Both legs filled successfully
    UNWINDING = "UNWINDING"        # Leg 2 failed — selling Leg 1
    UNWOUND = "UNWOUND"            # Leg 1 successfully unwound
    FAILED = "FAILED"              # Unrecoverable failure


@dataclass
class UnwindOrder:
    """Tracks a two-leg arb trade through sequential execution + unwind.

    Lifecycle:
      PENDING → LEG1_SUBMITTED → LEG1_FILLED → LEG2_SUBMITTED → COMPLETE
                                                              → UNWINDING → UNWOUND
                                                              → FAILED
    """
    unwind_id: str
    event_name: str
    direction: str
    status: UnwindStatus = UnwindStatus.PENDING
    leg1: Optional[LegOrder] = None
    leg2: Optional[LegOrder] = None
    unwind_leg: Optional[LegOrder] = None  # sell order if unwind needed
    created_at: str = ""
    completed_at: str = ""
    error_message: str = ""
    net_profit_per_contract: Decimal = ZERO
    total_contracts: int = 0


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

        Relies strictly on explicit exchange status flags:
        1. Market ``status`` in a set of terminal states.
        2. ``result`` field is populated (outcome known).

        Does NOT use close_time / expiration_time — these are often
        inaccurate placeholders for live sports markets.
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
            "[Kalshi] %s status=%r, result=%r",
            ticker, status, result,
        )

        if status in (
            "settled", "closed", "finalized", "determined",
            "ceased_trading", "complete",
        ) or result:
            logging.info(
                "[Kalshi] Market %s resolved (status=%s, result=%s)",
                ticker, status, result,
            )
            return False

        return True

    async def check_poly_active(self, condition_id: str) -> bool:
        """Return True if the Polymarket market is still active.

        Relies strictly on explicit exchange status flags:
        1. ``closed`` is True or ``active`` is False.

        Does NOT use end_date_iso — these timestamps are often
        inaccurate placeholders for live sports markets.

        Opportunistically caches token IDs from the response.
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
            "[Polymarket] %s… closed=%r, active=%r",
            condition_id[:16],
            data.get("closed"), data.get("active"),
        )

        if data.get("closed") is True or data.get("active") is False:
            logging.info(
                "[Polymarket] Market %s… resolved (closed=%s, active=%s)",
                condition_id[:16], data.get("closed"), data.get("active"),
            )
            return False

        # Note: do NOT cache token IDs from API response here.
        # Token mapping comes exclusively from events.json to avoid
        # the blind tokens[0]=YES assumption bug.

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

        Returns cached tokens ONLY. Token IDs must be pre-populated in
        the cache from events.json at startup — no runtime API resolution.
        This eliminates the catastrophic bug of blindly assuming
        tokens[0] == YES based on arbitrary API ordering.
        """
        if condition_id in self._poly_token_cache:
            return self._poly_token_cache[condition_id]

        logging.error(
            "[Polymarket] No cached tokens for %s… — "
            "events.json must include poly_yes_token / poly_no_token.",
            condition_id[:16],
        )
        return None

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
                bids, key=lambda lvl: D(str(lvl.get("price", 0))), reverse=True,
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
                asks, key=lambda lvl: D(str(lvl.get("price", 999))),
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
# KalshiOrderClient — authenticated REST order submission (Phase 3)
# ====================================================================

class KalshiOrderClient:
    """Authenticated REST client for Kalshi order placement.

    Uses the same RSA-PSS signing scheme as KalshiWS but for REST endpoints.

    Signing payload:  {timestamp_ms}{METHOD}{path}
    Headers:
      - KALSHI-ACCESS-KEY:       API key ID
      - KALSHI-ACCESS-SIGNATURE: base64(RSA-PSS-sign(payload))
      - KALSHI-ACCESS-TIMESTAMP: timestamp in milliseconds

    Endpoints:
      - POST   /trade-api/v2/portfolio/orders           — place order
      - GET    /trade-api/v2/portfolio/orders/{order_id} — check status
      - DELETE /trade-api/v2/portfolio/orders/{order_id} — cancel order
    """

    ORDER_PATH = "/trade-api/v2/portfolio/orders"

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._private_key = None
        self._limiter = RateLimiter(KALSHI_MAX_RPS)

    def _load_private_key(self):
        """Load RSA private key for Kalshi auth (same key as WS)."""
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

    def _sign_request(self, method: str, path: str) -> dict[str, str]:
        """Generate auth headers for a REST API call."""
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives import hashes

        if not KALSHI_API_KEY:
            raise ValueError("KALSHI_API_KEY env var is required.")

        key = self._load_private_key()
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method}{path}"
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
            "Content-Type": "application/json",
        }

    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def place_order(
        self,
        ticker: str,
        action: str,
        side: str,
        count: int,
        price_cents: int,
        time_in_force: str = "ioc",
        client_order_id: str = "",
    ) -> dict:
        """Place an order on Kalshi.

        Args:
            ticker: Market ticker (e.g. KXNBAGAME-26FEB24GSWNOP-GSW)
            action: "buy" or "sell"
            side: "yes" or "no"
            count: Number of contracts
            price_cents: Limit price in cents (1-99)
            time_in_force: "ioc" (default) or "gtc"
            client_order_id: Optional idempotency / reconciliation key (UUID)

        Returns:
            The order dict from the API response, including:
              order_id, status, remaining_count, etc.
        """
        await self._ensure_session()
        await self._limiter.acquire()

        path = self.ORDER_PATH
        headers = self._sign_request("POST", path)

        body = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "type": "limit",
            "count": count,
            "time_in_force": time_in_force,
        }
        if client_order_id:
            body["client_order_id"] = client_order_id
        # Kalshi expects yes_price or no_price depending on side
        if side == "yes":
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents

        url = f"{KALSHI_BASE.rstrip('/trade-api/v2')}{path}"
        # Handle the case where KALSHI_BASE already has the right prefix
        if "/trade-api/v2" in KALSHI_BASE:
            base = KALSHI_BASE.split("/trade-api/v2")[0]
            url = f"{base}{path}"

        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        try:
            async with self._session.post(
                url, headers=headers, json=body, timeout=timeout,
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    logging.error(
                        "[Kalshi-Order] %s %s %s x%d @ %dc → HTTP %d: %s",
                        action.upper(), side.upper(), ticker,
                        count, price_cents, resp.status, data,
                    )
                    return {"error": True, "status_code": resp.status, "detail": data}
                order = data.get("order", data)
                logging.info(
                    "[Kalshi-Order] %s %s %s x%d @ %dc → "
                    "order_id=%s status=%s remaining=%s",
                    action.upper(), side.upper(), ticker,
                    count, price_cents,
                    order.get("order_id", "?"),
                    order.get("status", "?"),
                    order.get("remaining_count", "?"),
                )
                return order
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logging.error("[Kalshi-Order] Request failed: %s", exc)
            return {"error": True, "detail": str(exc)}

    async def get_order(self, order_id: str) -> dict:
        """Check the status of an existing order."""
        await self._ensure_session()
        await self._limiter.acquire()

        path = f"{self.ORDER_PATH}/{order_id}"
        headers = self._sign_request("GET", path)

        base = KALSHI_BASE.split("/trade-api/v2")[0] if "/trade-api/v2" in KALSHI_BASE else KALSHI_BASE
        url = f"{base}{path}"

        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        try:
            async with self._session.get(
                url, headers=headers, timeout=timeout,
            ) as resp:
                data = await resp.json(content_type=None)
                return data.get("order", data) if resp.status < 400 else data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logging.error("[Kalshi-Order] get_order failed: %s", exc)
            return {"error": True, "detail": str(exc)}

    async def get_orders(self, **params) -> list[dict]:
        """List orders with query parameters (e.g. ticker, status).

        Useful for reconciliation: pass ticker=... to find orders
        placed during a timeout when we don't have the order_id.
        """
        await self._ensure_session()
        await self._limiter.acquire()

        path = self.ORDER_PATH
        headers = self._sign_request("GET", path)

        base = KALSHI_BASE.split("/trade-api/v2")[0] if "/trade-api/v2" in KALSHI_BASE else KALSHI_BASE
        url = f"{base}{path}"

        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        try:
            async with self._session.get(
                url, headers=headers, params=params, timeout=timeout,
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    return []
                return data.get("orders", []) if isinstance(data, dict) else []
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logging.error("[Kalshi-Order] get_orders failed: %s", exc)
            return []

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel an order."""
        await self._ensure_session()
        await self._limiter.acquire()

        path = f"{self.ORDER_PATH}/{order_id}"
        headers = self._sign_request("DELETE", path)

        base = KALSHI_BASE.split("/trade-api/v2")[0] if "/trade-api/v2" in KALSHI_BASE else KALSHI_BASE
        url = f"{base}{path}"

        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        try:
            async with self._session.delete(
                url, headers=headers, timeout=timeout,
            ) as resp:
                data = await resp.json(content_type=None)
                return data.get("order", data) if resp.status < 400 else data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logging.error("[Kalshi-Order] cancel_order failed: %s", exc)
            return {"error": True, "detail": str(exc)}

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ====================================================================
# PolymarketOrderClient — order submission via py_clob_client (Phase 3)
# ====================================================================

class PolymarketOrderClient:
    """Order client for Polymarket using py_clob_client.

    Uses EIP-712 signed orders on Polygon (chain_id=137).
    The ClobClient from py_clob_client is synchronous (uses `requests`),
    so all calls are wrapped in ``asyncio.to_thread()`` to avoid blocking
    the event loop.

    Order types used:
      - FOK (Fill-or-Kill) for primary leg execution
      - FOK for unwind sells (with aggressive pricing)

    Credentials required:
      - ETH private key (hex) — read from POLY_PRIVATE_KEY_PATH
      - CLOB API key, secret, passphrase — from env vars
    """

    def __init__(self, poly_token_cache: Optional[dict] = None) -> None:
        self._client = None
        self._token_cache = poly_token_cache or {}
        self._private_key_hex: str = ""
        # Serializes create_order + post_order so two concurrent calls
        # never fetch the same nonce from py_clob_client (which would
        # cause a "nonce too low" on-chain revert).
        self._order_lock = asyncio.Lock()

    def _load_private_key(self) -> str:
        """Load the ETH private key from file."""
        if self._private_key_hex:
            return self._private_key_hex
        key_path = Path(POLY_PRIVATE_KEY_PATH).expanduser()
        if not key_path.exists():
            raise FileNotFoundError(
                f"Polymarket private key not found at {key_path}. "
                "Set POLY_PRIVATE_KEY_PATH env var."
            )
        raw = key_path.read_text().strip()
        # Accept with or without 0x prefix
        self._private_key_hex = raw if raw.startswith("0x") else f"0x{raw}"
        return self._private_key_hex

    def _ensure_client(self) -> None:
        """Lazy-initialize the ClobClient."""
        if self._client is not None:
            return
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError:
            raise ImportError(
                "py_clob_client is required for live Polymarket trading. "
                "Install with: pip install py-clob-client"
            )

        key = self._load_private_key()

        if not all([POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE]):
            raise ValueError(
                "Polymarket CLOB credentials required for live trading. "
                "Set POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE env vars."
            )

        creds = ApiCreds(
            api_key=POLY_API_KEY,
            api_secret=POLY_API_SECRET,
            api_passphrase=POLY_PASSPHRASE,
        )
        self._client = ClobClient(
            host=CLOB_BASE,
            key=key,
            chain_id=POLY_CHAIN_ID,
            creds=creds,
        )
        logging.info(
            "[Poly-Order] ClobClient initialized (chain_id=%d)", POLY_CHAIN_ID,
        )

    def resolve_token_id(self, condition_id: str, side: str) -> Optional[str]:
        """Get the token_id for a given condition_id and side.

        Args:
            condition_id: Polymarket condition_id (hex)
            side: "yes" or "no"

        Returns:
            token_id string, or None if not found in cache.
        """
        pair = self._token_cache.get(condition_id)
        if pair is None:
            return None
        yes_tok, no_tok = pair
        return yes_tok if side == "yes" else no_tok

    async def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "fok",
    ) -> dict:
        """Create, sign, and post an order to Polymarket.

        Args:
            token_id: The specific token to trade
            side: "buy" or "sell"
            price: Limit price (0.0-1.0)
            size: Number of contracts (as float for py_clob_client)
            order_type: "fok" (default) or "gtc"

        Returns:
            Response dict with orderID, status ("matched"/"unmatched"), etc.
        """
        self._ensure_client()

        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        clob_side = BUY if side.lower() == "buy" else SELL
        ot = OrderType.FOK if order_type.lower() == "fok" else OrderType.GTC

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=clob_side,
        )

        def _sync_create_and_post():
            signed = self._client.create_order(order_args)
            return self._client.post_order(signed, ot)

        try:
            async with self._order_lock:
                response = await asyncio.to_thread(_sync_create_and_post)
            order_id = response.get("orderID", "?") if isinstance(response, dict) else "?"
            status = response.get("status", "?") if isinstance(response, dict) else str(response)
            logging.info(
                "[Poly-Order] %s %s token=%s… size=%.1f @ %.4f → "
                "orderID=%s status=%s",
                side.upper(), order_type.upper(),
                token_id[:16], size, price, order_id, status,
            )
            return response if isinstance(response, dict) else {"raw": response}
        except Exception as exc:
            logging.error("[Poly-Order] place_order failed: %s", exc, exc_info=True)
            return {"error": True, "detail": str(exc)}

    async def get_order(self, order_id: str) -> dict:
        """Check the status of an existing order.

        Serialized under _order_lock to prevent concurrent access to
        py_clob_client's internal state (session, nonce tracking).
        """
        self._ensure_client()
        try:
            async with self._order_lock:
                result = await asyncio.to_thread(self._client.get_order, order_id)
            return result if isinstance(result, dict) else {"raw": result}
        except Exception as exc:
            logging.error("[Poly-Order] get_order failed: %s", exc)
            return {"error": True, "detail": str(exc)}

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel an order.

        Serialized under _order_lock to prevent concurrent access to
        py_clob_client's internal state (session, nonce tracking).
        """
        self._ensure_client()
        try:
            async with self._order_lock:
                result = await asyncio.to_thread(self._client.cancel, order_id)
            return result if isinstance(result, dict) else {"raw": result}
        except Exception as exc:
            logging.error("[Poly-Order] cancel_order failed: %s", exc)
            return {"error": True, "detail": str(exc)}

    async def close(self) -> None:
        """No persistent connections to close for py_clob_client."""
        self._client = None


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
        self._notifier: Optional["DiscordNotifier"] = None
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

            # Seed the watchdog timer so it doesn't spin if the WS
            # connects successfully but the first real message is delayed.
            self.last_update_time = time.time()

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
        except Exception as exc:
            logging.debug("[Poly-WS] Ping failed: %s", exc)

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
        # Phase 4: Discord notification on reconnect
        if self._notifier is not None:
            asyncio.ensure_future(
                self._notifier.notify_reconnect(
                    "Polymarket", self._reconnect_attempts,
                )
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
        self._notifier: Optional["DiscordNotifier"] = None
        self._private_key = None
        # ticker -> event_name (for queue notifications)
        self._ticker_to_event: dict[str, str] = {}
        # Shared queue for event-driven processing
        self._update_queue: Optional[asyncio.Queue] = None
        # Stale-data watchdog: updated on every incoming message
        self.last_update_time: float = 0.0
        # MarketFetcher reference — used for post-reconnect REST snapshots
        self._fetcher: Optional["MarketFetcher"] = None
        # Phase 3 — Fill tracking for live order execution
        # Maps order_id -> asyncio.Future that resolves with fill data
        self._fill_waiters: dict[str, asyncio.Future] = {}
        # Accumulated fills per order_id: {order_id: total_filled_count}
        self._fill_counts: dict[str, int] = {}
        # Whether we’ve subscribed to the fill channel
        self._fills_subscribed: bool = False

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

            # Re-subscribe to fill channel if was previously active (Phase 3)
            if self._fills_subscribed and self._subscribed_tickers:
                self._msg_id += 1
                fill_msg = {
                    "id": self._msg_id,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["fill"],
                        "market_tickers": list(self._subscribed_tickers),
                    },
                }
                await self._ws.send_json(fill_msg)
                logging.info("[Kalshi-WS] Re-subscribed to fill channel.")

            self._listen_task = asyncio.create_task(self._listen_loop())
            self._stale_task = asyncio.create_task(self._stale_watchdog())

            # After (re)connect, pull a fresh REST snapshot
            await self._fetch_initial_snapshots()

            # Seed the watchdog timer so it doesn't spin if the WS
            # connects successfully but the first real message is delayed.
            self.last_update_time = time.time()

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

        elif msg_type == "fill":
            # Phase 3: Fill notification from the fill channel.
            # Resolves any pending fill waiter for this order_id.
            inner = data.get("msg", {})
            order_id = inner.get("order_id", "")
            count = int(inner.get("count", 0))
            if order_id and order_id in self._fill_waiters:
                # Accumulate fill count (may receive multiple partial fills)
                prev = self._fill_counts.get(order_id, 0)
                self._fill_counts[order_id] = prev + count
                logging.info(
                    "[Kalshi-WS] Fill received: order=%s count=%d "
                    "(total filled=%d)",
                    order_id, count, self._fill_counts[order_id],
                )
                # Resolve the future with the accumulated fill data
                fut = self._fill_waiters.get(order_id)
                if fut and not fut.done():
                    fut.set_result({
                        "order_id": order_id,
                        "filled_count": self._fill_counts[order_id],
                        "last_fill": inner,
                    })
            elif order_id:
                logging.debug(
                    "[Kalshi-WS] Fill for untracked order %s (count=%d)",
                    order_id, count,
                )

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
        # Phase 4: Discord notification on reconnect
        if self._notifier is not None:
            asyncio.ensure_future(
                self._notifier.notify_reconnect(
                    "Kalshi", self._reconnect_attempts,
                )
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

    async def subscribe_fills(self) -> None:
        """Subscribe to the fill channel for live order tracking (Phase 3).

        Must be called after connect().  The fill channel delivers real-time
        notifications when our orders are matched.  Messages include:
          order_id, count (filled), side, action, yes_price, etc.

        On reconnect, _do_connect() re-subscribes automatically if
        self._fills_subscribed is True.
        """
        if self._fills_subscribed:
            return
        if not self._connected or not self._ws or self._ws.closed:
            logging.warning("[Kalshi-WS] Cannot subscribe fills — not connected.")
            return

        self._msg_id += 1
        msg = {
            "id": self._msg_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["fill"],
                "market_tickers": list(self._subscribed_tickers),
            },
        }
        await self._ws.send_json(msg)
        self._fills_subscribed = True
        logging.info(
            "[Kalshi-WS] Subscribed to fill channel for %d tickers.",
            len(self._subscribed_tickers),
        )

    def register_fill_waiter(self, order_id: str) -> asyncio.Future:
        """Register a Future that resolves when fill(s) arrive for order_id.

        Call this AFTER placing the order (once you have the order_id).
        The Future resolves with a dict:
          {"order_id": ..., "filled_count": ..., "last_fill": ...}

        Use asyncio.wait_for() with a timeout to avoid hanging forever.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._fill_waiters[order_id] = fut
        self._fill_counts[order_id] = 0
        return fut

    def cancel_fill_waiter(self, order_id: str) -> None:
        """Cancel and clean up a fill waiter."""
        fut = self._fill_waiters.pop(order_id, None)
        self._fill_counts.pop(order_id, None)
        if fut and not fut.done():
            fut.cancel()

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

    async def log(self, opp: ArbOpportunity) -> None:
        """Append one row to the ledger CSV (non-blocking)."""
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
            "max_contracts": opp.max_contracts,
            "kalshi_depth": opp.kalshi_depth,
            "poly_depth": opp.poly_depth,
            "fetch_ms": opp.fetch_latency_ms,
        }
        await asyncio.to_thread(self._write_row, row)

    def _write_row(self, row: dict) -> None:
        """Synchronous CSV write (called via to_thread)."""
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
        self.total_unwind_losses: Decimal = ZERO
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

    async def _write_row_async(self, row: dict) -> None:
        """Non-blocking CSV write (offloaded to thread)."""
        await asyncio.to_thread(self._write_row_sync, row)

    def _write_row_sync(self, row: dict) -> None:
        """Synchronous CSV write (called via to_thread or at startup)."""
        with open(self.path, mode="a", newline="") as fh:
            csv.DictWriter(fh, fieldnames=self.PORT_COLUMNS).writerow(row)

    async def open_position(self, pos: Position) -> None:
        """Add a position and deduct capital.  Called by ExecutionEngine."""
        self.capital -= pos.total_outlay
        self.open_positions.append(pos)

        await self._write_row_async({
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

    async def close_positions_for_event(self, event_name: str) -> None:
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

                await self._write_row_async({
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

    async def record_unwind_loss(
        self,
        event_name: str,
        direction: str,
        contracts: int,
        loss_amount: Decimal,
    ) -> None:
        """Debit the portfolio for a realized unwind loss.

        Called when an arb trade is unwound (Leg 1 bought then sold back).
        The loss includes:
          - Leg 1 taker fees (paid on the original buy)
          - Unwind taker fees (paid on the sell-back)
          - Slippage: (buy_price - sell_price) * contracts
            (zero in paper mode, non-zero in production)

        The loss is deducted from capital and tracked cumulatively in
        self.total_unwind_losses for reporting.
        """
        self.capital -= loss_amount
        self.total_unwind_losses += loss_amount

        await self._write_row_async({
            "action": "UNWIND",
            "timestamp": _utc_iso(),
            "event_name": event_name,
            "direction": direction,
            "contracts": contracts,
            "cost_per_contract": "",
            "fees_per_contract": "",
            "total_outlay": f"{loss_amount.quantize(Q4)}",
            "payout": "",
            "pnl": f"-{loss_amount.quantize(Q4)}",
            "capital_remaining": f"{self.capital.quantize(Q2)}",
        })

        logging.warning(
            "  PORTFOLIO: UNWIND [%s] %s — %d contracts, "
            "loss $%s, capital $%s",
            event_name, direction, contracts,
            loss_amount.quantize(Q4), self.capital.quantize(Q2),
        )

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

    async def log(self, order: Order) -> None:
        """Append one order to the CSV (non-blocking)."""
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
        await asyncio.to_thread(self._write_row, row)

    def _write_row(self, row: dict) -> None:
        """Synchronous CSV write (called via to_thread)."""
        with open(self.path, mode="a", newline="") as fh:
            csv.DictWriter(fh, fieldnames=self.COLUMNS).writerow(row)


# ====================================================================
# DailyPnLTracker — tracks realized P&L per calendar day
# ====================================================================

class DailyPnLTracker:
    """Tracks realized P&L per calendar day for circuit-breaker logic.

    Every fill's expected P&L is recorded.  When the cumulative daily
    loss exceeds DAILY_LOSS_LIMIT, the tracker trips the circuit
    breaker, blocking all new orders for the rest of the day.

    The tracker resets automatically at midnight UTC.
    """

    def __init__(self, daily_limit: Decimal = DAILY_LOSS_LIMIT) -> None:
        self._daily_limit = daily_limit
        self._today: str = self._current_day()
        self._daily_pnl: Decimal = ZERO
        self._tripped = False
        self._notifier: Optional["DiscordNotifier"] = None

    def set_notifier(self, notifier: "DiscordNotifier") -> None:
        """Attach a DiscordNotifier for circuit-breaker alerts."""
        self._notifier = notifier

    @staticmethod
    def _current_day() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _maybe_reset(self) -> None:
        """Reset if the calendar day has changed."""
        today = self._current_day()
        if today != self._today:
            logging.info(
                "[DailyPnL] New day %s — resetting. Previous day P&L: $%s",
                today, self._daily_pnl.quantize(Q4),
            )
            self._today = today
            self._daily_pnl = ZERO
            self._tripped = False

    def record(self, net_pnl: Decimal, contracts: int) -> None:
        """Record a fill's expected P&L (net_profit × contracts)."""
        self._maybe_reset()
        realized = (net_pnl * D(contracts)).quantize(Q4)
        self._daily_pnl += realized
        logging.debug(
            "[DailyPnL] Recorded $%s — daily total: $%s / -$%s limit",
            realized.quantize(Q4), self._daily_pnl.quantize(Q4),
            self._daily_limit.quantize(Q2),
        )
        if self._daily_pnl < -self._daily_limit and not self._tripped:
            self._tripped = True
            logging.warning(
                "[DailyPnL] *** CIRCUIT BREAKER TRIPPED *** "
                "Daily P&L $%s exceeds -$%s limit. Halting new orders.",
                self._daily_pnl.quantize(Q4), self._daily_limit.quantize(Q2),
            )
            # Phase 4: Discord notification
            if self._notifier is not None:
                asyncio.ensure_future(
                    self._notifier.notify_circuit_breaker(
                        self._daily_pnl, self._daily_limit,
                    )
                )

    @property
    def is_breached(self) -> bool:
        """True if the daily loss limit has been exceeded."""
        self._maybe_reset()
        return self._tripped

    @property
    def daily_pnl(self) -> Decimal:
        self._maybe_reset()
        return self._daily_pnl

    @property
    def daily_limit(self) -> Decimal:
        return self._daily_limit


# ====================================================================
# OrderRateLimiter — caps order submissions per minute
# ====================================================================

class OrderRateLimiter:
    """Sliding-window rate limiter for order submissions.

    Tracks timestamps of recent submissions and rejects new ones when
    the count in the last 60 seconds exceeds MAX_ORDERS_PER_MINUTE.
    """

    def __init__(self, max_per_minute: int = MAX_ORDERS_PER_MINUTE) -> None:
        self._max = max_per_minute
        self._timestamps: list[float] = []

    def _prune(self) -> None:
        """Remove timestamps older than 60 seconds."""
        cutoff = time.time() - 60.0
        self._timestamps = [t for t in self._timestamps if t > cutoff]

    def try_acquire(self) -> bool:
        """Return True if the order is allowed, False if rate-limited."""
        self._prune()
        if len(self._timestamps) >= self._max:
            return False
        self._timestamps.append(time.time())
        return True

    @property
    def recent_count(self) -> int:
        self._prune()
        return len(self._timestamps)


# ====================================================================
# RiskManager — aggregates all risk checks into a single gate
# ====================================================================

class RiskManager:
    """Central risk management gate checked before every order.

    Consolidates:
      - Daily loss limit (circuit breaker)
      - Max total exposure across open positions
      - Max concurrent positions
      - Spread sanity check
      - Order submission rate limit
      - File-based kill switch
    """

    def __init__(
        self,
        pnl_tracker: DailyPnLTracker,
        rate_limiter: OrderRateLimiter,
        portfolio: "PaperPortfolio",
        *,
        max_exposure: Decimal = MAX_TOTAL_EXPOSURE,
        max_positions: int = MAX_CONCURRENT_POSITIONS,
        max_spread: Decimal = MAX_SPREAD_THRESHOLD,
        kill_file: str = KILL_FILE,
    ) -> None:
        self.pnl_tracker = pnl_tracker
        self.rate_limiter = rate_limiter
        self._portfolio = portfolio
        self._max_exposure = max_exposure
        self._max_positions = max_positions
        self._max_spread = max_spread
        self._kill_file = kill_file

    def check(self, opp: "ArbOpportunity") -> Optional[RejectReason]:
        """Run all risk checks.  Returns None if OK, or a RejectReason."""
        # 1. Kill switch
        if Path(self._kill_file).exists():
            logging.warning(
                "[Risk] Kill file '%s' detected — rejecting order.",
                self._kill_file,
            )
            return RejectReason.KILL_SWITCH

        # 2. Daily loss limit
        if self.pnl_tracker.is_breached:
            return RejectReason.DAILY_LOSS_LIMIT

        # 3. Spread sanity — reject unreasonably large spreads (likely stale data)
        if opp.gross_profit > self._max_spread:
            logging.warning(
                "[Risk] Spread %.2f%% exceeds %.2f%% sanity threshold for [%s]. "
                "Likely stale data — rejecting.",
                float(opp.gross_profit * HUNDRED),
                float(self._max_spread * HUNDRED),
                opp.event_name,
            )
            return RejectReason.SPREAD_TOO_WIDE

        # 4. Max concurrent positions
        if len(self._portfolio.open_positions) >= self._max_positions:
            return RejectReason.MAX_POSITIONS

        # 5. Max total exposure
        current_exposure = sum(
            (p.total_outlay for p in self._portfolio.open_positions), ZERO,
        )
        if current_exposure >= self._max_exposure:
            return RejectReason.MAX_EXPOSURE

        # NOTE: Order rate limit moved to ExecutionEngine.submit() as a
        # post-filter gate (Phase 2 refactor).  It should only be consumed
        # when a trade actually passes all pre-trade checks.

        return None


# ====================================================================
# UnwindLogger — CSV logger for unwind lifecycle
# ====================================================================

class UnwindLogger:
    """Writes every UnwindOrder to a CSV file for post-session analysis."""

    COLUMNS = [
        "unwind_id", "timestamp", "event_name", "direction", "status",
        "total_contracts", "net_profit_per_contract",
        "leg1_platform", "leg1_side", "leg1_price", "leg1_status",
        "leg1_filled",
        "leg2_platform", "leg2_side", "leg2_price", "leg2_status",
        "leg2_filled",
        "unwind_platform", "unwind_side", "unwind_price", "unwind_status",
        "unwind_filled",
        "error_message",
    ]

    def __init__(self, path: str = UNWIND_LOG_FILE) -> None:
        self.path = path
        self._ensure()

    def _ensure(self) -> None:
        dir_path = os.path.dirname(self.path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, mode="w", newline="") as fh:
                csv.DictWriter(fh, fieldnames=self.COLUMNS).writeheader()
            logging.info("Created unwind ledger → %s", self.path)

    async def log(self, uw: UnwindOrder) -> None:
        """Append one row per unwind order lifecycle event (non-blocking)."""
        def _leg_fields(leg: Optional[LegOrder], prefix: str) -> dict:
            if leg is None:
                return {
                    f"{prefix}_platform": "", f"{prefix}_side": "",
                    f"{prefix}_price": "", f"{prefix}_status": "",
                    f"{prefix}_filled": "",
                }
            return {
                f"{prefix}_platform": leg.platform,
                f"{prefix}_side": leg.side,
                f"{prefix}_price": f"{leg.price.quantize(Q4)}",
                f"{prefix}_status": leg.status.value,
                f"{prefix}_filled": leg.filled_contracts,
            }

        row: dict = {
            "unwind_id": uw.unwind_id,
            "timestamp": uw.completed_at or uw.created_at,
            "event_name": uw.event_name,
            "direction": uw.direction,
            "status": uw.status.value,
            "total_contracts": uw.total_contracts,
            "net_profit_per_contract": f"{uw.net_profit_per_contract.quantize(Q4)}",
            "error_message": uw.error_message,
        }
        row.update(_leg_fields(uw.leg1, "leg1"))
        row.update(_leg_fields(uw.leg2, "leg2"))
        row.update(_leg_fields(uw.unwind_leg, "unwind"))
        await asyncio.to_thread(self._write_row, row)

    def _write_row(self, row: dict) -> None:
        """Synchronous CSV write (called via to_thread)."""
        with open(self.path, mode="a", newline="") as fh:
            csv.DictWriter(fh, fieldnames=self.COLUMNS).writerow(row)


# ====================================================================
# UnwindManager — sequential two-leg execution with rollback
# ====================================================================

class UnwindManager:
    """Sequential two-leg execution with automatic rollback.

    Execution flow:
      1. Submit Leg 1 (Kalshi) → await fill confirmation (with timeout)
      2. Submit Leg 2 (Polymarket) → await fill confirmation (with timeout)
      3. If Leg 2 fails/times out: cancel + sell Leg 1 at market to unwind
      4. If Leg 1 partially fills: adjust Leg 2 size to match, or unwind

    PAPER MODE: Simulates immediate fills.  No real orders placed.
    LIVE MODE (Phase 3): Uses KalshiOrderClient (IOC) and
    PolymarketOrderClient (FOK) for real API calls.
    Kalshi partial IOC fills resize the Poly FOK leg dynamically.
    """

    def __init__(
        self,
        portfolio: "PaperPortfolio",
        pnl_tracker: "DailyPnLTracker",
        timeout: int = UNWIND_TIMEOUT_SECONDS,
        logger: Optional[UnwindLogger] = None,
        *,
        live_mode: bool = False,
        kalshi_client: Optional["KalshiOrderClient"] = None,
        poly_client: Optional["PolymarketOrderClient"] = None,
        kalshi_ws: Optional["KalshiWS"] = None,
        poly_ws: Optional["PolymarketWS"] = None,
        fetcher: Optional["MarketFetcher"] = None,
    ) -> None:
        self._portfolio = portfolio
        self._pnl_tracker = pnl_tracker
        self._timeout = timeout
        self._logger = logger or UnwindLogger()
        # Active unwind orders (in-flight — not yet COMPLETE/UNWOUND/FAILED)
        self._active: dict[str, UnwindOrder] = {}
        # Completed unwind orders (capped for memory)
        self._completed: list[UnwindOrder] = []
        # Notification callback for Discord (Phase 4 stub)
        self._notify_callback: Optional[object] = None
        # Phase 3: Live execution infrastructure
        self._live_mode = live_mode
        self._kalshi_client = kalshi_client
        self._poly_client = poly_client
        self._kalshi_ws = kalshi_ws
        self._poly_ws = poly_ws
        self._fetcher = fetcher

    def set_notify_callback(self, callback) -> None:
        """Register a callback for Discord alerts (Phase 4).

        Expected signature:  async def callback(message: str, level: str)
        """
        self._notify_callback = callback

    # ── Leg construction ──────────────────────────────────────────

    @staticmethod
    def _build_legs(
        opp: ArbOpportunity, contracts: int,
    ) -> tuple[LegOrder, LegOrder]:
        """Create Leg 1 (Polymarket) and Leg 2 (Kalshi) from an opp.

        Polymarket FOK is executed first because it is less reliable
        (all-or-nothing fill on a less mature exchange).  If the Poly
        FOK fails, we avoid placing the Kalshi IOC at all — no legging
        risk.  Kalshi IOC (Leg 2) is more reliable and supports partial
        fills, making it the safer second leg.
        """
        if "YES@Kalshi" in opp.direction:
            # Direction A: Buy NO @ Polymarket + Buy YES @ Kalshi
            leg1 = LegOrder(
                leg_id=str(uuid.uuid4())[:8],
                platform="polymarket", side="no",
                price=opp.poly_price, contracts=contracts,
                market_id=opp.poly_condition_id,
            )
            leg2 = LegOrder(
                leg_id=str(uuid.uuid4())[:8],
                platform="kalshi", side="yes",
                price=opp.kalshi_price, contracts=contracts,
                market_id=opp.kalshi_ticker,
            )
        else:
            # Direction B: Buy YES @ Polymarket + Buy NO @ Kalshi
            leg1 = LegOrder(
                leg_id=str(uuid.uuid4())[:8],
                platform="polymarket", side="yes",
                price=opp.poly_price, contracts=contracts,
                market_id=opp.poly_condition_id,
            )
            leg2 = LegOrder(
                leg_id=str(uuid.uuid4())[:8],
                platform="kalshi", side="no",
                price=opp.kalshi_price, contracts=contracts,
                market_id=opp.kalshi_ticker,
            )
        return leg1, leg2

    # ── Core execution ────────────────────────────────────────────

    async def execute(
        self, opp: ArbOpportunity, desired_contracts: int,
    ) -> UnwindOrder:
        """Execute a two-leg arb trade with unwind protection.

        Returns the completed UnwindOrder (COMPLETE, UNWOUND, or FAILED).
        """
        now = _utc_iso()
        leg1, leg2 = self._build_legs(opp, desired_contracts)

        uw = UnwindOrder(
            unwind_id=str(uuid.uuid4())[:8],
            event_name=opp.event_name,
            direction=opp.direction,
            status=UnwindStatus.PENDING,
            leg1=leg1,
            leg2=leg2,
            created_at=now,
            net_profit_per_contract=opp.net_profit,
            total_contracts=desired_contracts,
        )
        self._active[uw.unwind_id] = uw

        try:
            # ── Step 1: Submit Leg 1 (Polymarket FOK — less reliable) ──
            uw.status = UnwindStatus.LEG1_SUBMITTED
            logging.info(
                "  [Unwind %s] Leg 1 SUBMITTED: %s %s @ $%s × %d on %s",
                uw.unwind_id, "BUY", leg1.side.upper(),
                leg1.price.quantize(Q4), leg1.contracts, leg1.platform,
            )

            leg1_ok = await self._submit_leg(leg1)

            if not leg1_ok:
                uw.status = UnwindStatus.FAILED
                uw.error_message = "Leg 1 submission failed"
                await self._finish(uw)
                return uw

            if leg1.filled_contracts == 0:
                uw.status = UnwindStatus.FAILED
                uw.error_message = "Leg 1 fill returned 0 contracts"
                await self._finish(uw)
                return uw

            uw.status = UnwindStatus.LEG1_FILLED
            actual_contracts = leg1.filled_contracts
            logging.info(
                "  [Unwind %s] Leg 1 FILLED: %d/%d contracts",
                uw.unwind_id, actual_contracts, leg1.contracts,
            )

            # Handle partial fill on Leg 1: adjust Leg 2 size to match
            if actual_contracts < desired_contracts:
                logging.warning(
                    "  [Unwind %s] Leg 1 partial fill (%d/%d). "
                    "Adjusting Leg 2 to %d contracts.",
                    uw.unwind_id, actual_contracts, desired_contracts,
                    actual_contracts,
                )
                leg2.contracts = actual_contracts
                uw.total_contracts = actual_contracts

            # ── Spread re-verification after Leg 1 fill ──
            # Sports markets have a 3-second matching delay.  During
            # that window the Kalshi price may have moved, making the
            # arb unprofitable.  Re-fetch the Kalshi book and verify
            # the spread still holds before committing Leg 2.
            if self._live_mode and self._fetcher is not None:
                kalshi_snap = await self._fetcher.fetch_kalshi(
                    leg2.market_id,
                )
                if kalshi_snap is not None:
                    # Pick the correct side's best ask
                    side_book = (
                        kalshi_snap.yes if leg2.side == "yes"
                        else kalshi_snap.no
                    )
                    fresh_ask = side_book.best_ask
                    if fresh_ask is not None and fresh_ask > ZERO:
                        new_cost = (leg1.price + fresh_ask).quantize(Q4)
                        k_fee = kalshi_taker_fee(fresh_ask, 1)
                        p_fee = poly_taker_fee(leg1.price, 1)
                        new_net = (ONE - new_cost - k_fee - p_fee).quantize(Q4)
                        if new_net <= ZERO:
                            logging.warning(
                                "  [Unwind %s] Spread evaporated after Leg 1 "
                                "fill (new_net=$%s). Unwinding.",
                                uw.unwind_id, new_net.quantize(Q4),
                            )
                            uw.error_message = (
                                f"Spread gone after Leg 1 fill "
                                f"(net={new_net}). Unwinding."
                            )
                            await self._unwind(uw)
                            return uw
                        # Update Leg 2 price to the fresh ask
                        leg2.price = fresh_ask
                        uw.net_profit_per_contract = new_net
                        logging.info(
                            "  [Unwind %s] Spread re-verified: "
                            "fresh Kalshi ask=$%s, new net=$%s",
                            uw.unwind_id,
                            fresh_ask.quantize(Q4),
                            new_net.quantize(Q4),
                        )

            # ── Step 2: Submit Leg 2 (Kalshi IOC — more reliable) ──
            uw.status = UnwindStatus.LEG2_SUBMITTED
            logging.info(
                "  [Unwind %s] Leg 2 SUBMITTED: %s %s @ $%s × %d on %s",
                uw.unwind_id, "BUY", leg2.side.upper(),
                leg2.price.quantize(Q4), leg2.contracts, leg2.platform,
            )

            leg2_ok = await self._submit_leg(leg2)

            if not leg2_ok or leg2.filled_contracts == 0:
                # Leg 2 failed → unwind Leg 1
                uw.error_message = (
                    f"Leg 2 failed (filled {leg2.filled_contracts}/"
                    f"{leg2.contracts}). Unwinding Leg 1."
                )
                logging.warning(
                    "  [Unwind %s] %s", uw.unwind_id, uw.error_message,
                )
                await self._unwind(uw)
                return uw

            if leg2.filled_contracts < leg2.contracts:
                # Leg 2 partial fill — unwind the excess from Leg 1
                excess = actual_contracts - leg2.filled_contracts
                logging.warning(
                    "  [Unwind %s] Leg 2 partial fill (%d/%d). "
                    "Unwinding %d excess contracts from Leg 1.",
                    uw.unwind_id, leg2.filled_contracts, leg2.contracts,
                    excess,
                )
                # Adjust total to what actually paired
                uw.total_contracts = leg2.filled_contracts
                # Unwind the unpaired Leg 1 contracts
                await self._unwind_partial(uw, excess)

            # ── Both legs filled ──
            uw.status = UnwindStatus.COMPLETE
            logging.info(
                "  [Unwind %s] COMPLETE: %d contracts, "
                "expected net $%s/contract",
                uw.unwind_id, uw.total_contracts,
                uw.net_profit_per_contract.quantize(Q4),
            )
            self._notify(
                f"[Unwind {uw.unwind_id}] COMPLETE: {uw.total_contracts} "
                f"contracts [{uw.event_name}] {uw.direction} — "
                f"expected net ${uw.net_profit_per_contract.quantize(Q4)}/c",
            )
            await self._finish(uw)
            return uw

        except asyncio.TimeoutError:
            uw.error_message = (
                f"Timeout ({self._timeout}s) during execution"
            )
            logging.error(
                "  [Unwind %s] TIMEOUT: %s", uw.unwind_id, uw.error_message,
            )
            # If Leg 1 is filled but Leg 2 never completed, unwind
            if leg1.status == LegStatus.FILLED and leg1.filled_contracts > 0:
                await self._unwind(uw)
            else:
                uw.status = UnwindStatus.FAILED
                await self._finish(uw)
            return uw

        except Exception as exc:
            uw.error_message = f"Unexpected error: {exc}"
            uw.status = UnwindStatus.FAILED
            logging.error(
                "  [Unwind %s] ERROR: %s",
                uw.unwind_id, uw.error_message, exc_info=True,
            )
            # Best-effort unwind if Leg 1 was filled
            if (
                leg1.status == LegStatus.FILLED
                and leg1.filled_contracts > 0
                and uw.status != UnwindStatus.UNWOUND
            ):
                try:
                    await self._unwind(uw)
                except Exception:
                    logging.error(
                        "  [Unwind %s] Unwind also failed!",
                        uw.unwind_id, exc_info=True,
                    )
            await self._finish(uw)
            return uw

    # ── Unwind helpers ────────────────────────────────────────────

    async def _unwind(self, uw: UnwindOrder) -> None:
        """Sell Leg 1 at market to fully unwind the position."""
        uw.status = UnwindStatus.UNWINDING
        leg1 = uw.leg1
        assert leg1 is not None

        contracts_to_unwind = leg1.filled_contracts
        if uw.leg2 and uw.leg2.filled_contracts > 0:
            # Some of Leg 2 filled — only unwind the unpaired excess
            contracts_to_unwind = leg1.filled_contracts - uw.leg2.filled_contracts

        if contracts_to_unwind <= 0:
            uw.status = UnwindStatus.COMPLETE
            await self._finish(uw)
            return

        # Build unwind sell order (opposite of Leg 1 buy)
        # In live mode, _submit_unwind() handles slippage-protected pricing.
        # The actual fill price updates leg.price for slippage calculation.
        # In paper mode, sell_price == buy_price (zero slippage).
        unwind_sell = LegOrder(
            leg_id=str(uuid.uuid4())[:8],
            platform=leg1.platform,
            side=leg1.side,  # selling what we bought
            price=leg1.price,  # starting price (live mode updates after fill)
            contracts=contracts_to_unwind,
            market_id=leg1.market_id,
        )
        uw.unwind_leg = unwind_sell

        logging.warning(
            "  [Unwind %s] UNWINDING: SELL %s %s @ $%s × %d on %s",
            uw.unwind_id, unwind_sell.side.upper(),
            unwind_sell.platform, unwind_sell.price.quantize(Q4),
            unwind_sell.contracts, unwind_sell.platform,
        )

        ok = await self._submit_unwind(unwind_sell)
        if ok:
            uw.status = UnwindStatus.UNWOUND

            # ── Calculate realized unwind loss ──
            # Loss = Leg1 fees + Unwind fees + Slippage
            #   Leg 1 fee: Kalshi taker fee on the original buy
            #   Unwind fee: Kalshi taker fee on the sell-back
            #   Slippage: (buy_price - sell_price) * contracts
            #     (zero in paper mode; non-zero in production)
            # Use the correct fee function for the platform Leg 1 is on
            _fee_fn = (
                poly_taker_fee if leg1.platform == "polymarket"
                else kalshi_taker_fee
            )
            leg1_fee = _fee_fn(leg1.price, contracts_to_unwind)
            unwind_fee = _fee_fn(unwind_sell.price, contracts_to_unwind)
            slippage = (
                (leg1.price - unwind_sell.price)
                * D(contracts_to_unwind)
            ).quantize(Q4)
            unwind_loss = (leg1_fee + unwind_fee + slippage).quantize(Q4)

            logging.info(
                "  [Unwind %s] UNWOUND: sold %d contracts — "
                "loss $%s (fees $%s + $%s, slippage $%s)",
                uw.unwind_id, unwind_sell.filled_contracts,
                unwind_loss.quantize(Q4),
                leg1_fee.quantize(Q4), unwind_fee.quantize(Q4),
                slippage.quantize(Q4),
            )

            # Record loss in portfolio and circuit breaker
            await self._portfolio.record_unwind_loss(
                event_name=uw.event_name,
                direction=uw.direction,
                contracts=contracts_to_unwind,
                loss_amount=unwind_loss,
            )
            # Feed negative P&L to circuit breaker (loss is negative)
            self._pnl_tracker.record(-unwind_loss, 1)

            # Stash loss on the UnwindOrder for ExecutionEngine to read
            uw._unwind_loss = unwind_loss
        else:
            uw.status = UnwindStatus.FAILED
            uw.error_message += " | Unwind sell also failed!"
            logging.error(
                "  [Unwind %s] UNWIND FAILED — MANUAL INTERVENTION NEEDED",
                uw.unwind_id,
            )

        self._notify(
            f"[Unwind {uw.unwind_id}] {uw.status.value}: "
            f"{uw.event_name} — {uw.error_message}",
            level="warning",
        )
        await self._finish(uw)

    async def _unwind_partial(self, uw: UnwindOrder, excess: int) -> None:
        """Unwind excess contracts when Leg 2 partially fills."""
        leg1 = uw.leg1
        assert leg1 is not None

        # In live mode, _submit_unwind() handles aggressive pricing.
        unwind_sell = LegOrder(
            leg_id=str(uuid.uuid4())[:8],
            platform=leg1.platform,
            side=leg1.side,
            price=leg1.price,  # starting price (live mode updates after fill)
            contracts=excess,
            market_id=leg1.market_id,
        )
        uw.unwind_leg = unwind_sell

        logging.warning(
            "  [Unwind %s] Partial unwind: SELL %d excess contracts on %s",
            uw.unwind_id, excess, leg1.platform,
        )

        ok = await self._submit_unwind(unwind_sell)
        if ok:
            # Calculate realized loss on the partial unwind
            # Use the correct fee function for the platform Leg 1 is on
            _fee_fn = (
                poly_taker_fee if leg1.platform == "polymarket"
                else kalshi_taker_fee
            )
            leg1_fee = _fee_fn(leg1.price, excess)
            unwind_fee = _fee_fn(unwind_sell.price, excess)
            slippage = (
                (leg1.price - unwind_sell.price) * D(excess)
            ).quantize(Q4)
            partial_loss = (leg1_fee + unwind_fee + slippage).quantize(Q4)

            logging.info(
                "  [Unwind %s] Partial unwind OK: sold %d — "
                "loss $%s (fees $%s + $%s, slippage $%s)",
                uw.unwind_id, excess,
                partial_loss.quantize(Q4),
                leg1_fee.quantize(Q4), unwind_fee.quantize(Q4),
                slippage.quantize(Q4),
            )

            await self._portfolio.record_unwind_loss(
                event_name=uw.event_name,
                direction=uw.direction,
                contracts=excess,
                loss_amount=partial_loss,
            )
            self._pnl_tracker.record(-partial_loss, 1)

            # Accumulate on the UnwindOrder for ExecutionEngine
            existing = getattr(uw, '_unwind_loss', ZERO)
            uw._unwind_loss = existing + partial_loss
        else:
            uw.error_message += f" | Partial unwind of {excess} contracts failed!"
            logging.error(
                "  [Unwind %s] Partial unwind FAILED", uw.unwind_id,
            )

    # ── Leg submission (paper + live modes) ─────────────────────

    async def _submit_leg(self, leg: LegOrder) -> bool:
        """Submit a leg order.

        Paper mode: simulate immediate full fill.
        Live mode:  Kalshi IOC via REST + WS fill tracking.
                    Polymarket FOK via py_clob_client.
        """
        leg.submitted_at = _utc_iso()
        leg.status = LegStatus.SUBMITTED

        if not self._live_mode:
            # Paper mode: instant full fill
            await asyncio.sleep(0)  # yield to event loop
            leg.status = LegStatus.FILLED
            leg.filled_contracts = leg.contracts
            leg.filled_at = _utc_iso()
            return True

        # ── LIVE MODE ──
        if leg.platform == "kalshi":
            return await self._submit_kalshi_buy(leg)
        else:
            return await self._submit_poly_buy(leg)

    async def _submit_kalshi_buy(self, leg: LegOrder) -> bool:
        """Kalshi IOC buy order with WS fill tracking.

        Flow:
          1. Register fill waiter on KalshiWS (if available).
          2. POST IOC limit order via KalshiOrderClient.
          3. Check REST response for immediate fill status.
          4. If REST shows full fill → done.
          5. If partial or ambiguous → await WS fill waiter with timeout.
          6. If timeout → check order status via REST poll.

        IOC on Kalshi executes immediately and cancels the remainder,
        so the REST response typically contains the final state.
        WS fill subscription serves as confirmation and handles edge cases.
        """
        if self._kalshi_client is None:
            logging.error("[Unwind] Live mode but no KalshiOrderClient configured.")
            leg.status = LegStatus.FAILED
            return False

        ticker = leg.market_id
        if not ticker:
            logging.error("[Unwind] Kalshi leg has no market_id (ticker).")
            leg.status = LegStatus.FAILED
            return False

        # Convert Decimal price to cents (Kalshi uses integer cents)
        price_cents = int((leg.price * HUNDRED).quantize(D("1")))

        # Step 1: Register WS fill waiter (if WS available)
        fill_waiter: Optional[asyncio.Future] = None
        if self._kalshi_ws is not None and self._kalshi_ws._fills_subscribed:
            # We don't have the order_id yet, so we'll register after POST.
            pass

        # Step 2: Submit the IOC order
        # Use a client_order_id so we can reconcile on timeout.
        client_order_id = str(uuid.uuid4())
        result = await self._kalshi_client.place_order(
            ticker=ticker,
            action="buy",
            side=leg.side,
            count=leg.contracts,
            price_cents=price_cents,
            time_in_force="ioc",
            client_order_id=client_order_id,
        )

        if result.get("error"):
            detail = str(result.get("detail", ""))
            is_timeout = ("timeout" in detail.lower()
                          or "TimeoutError" in detail)
            if is_timeout:
                # ── TIMEOUT RECONCILIATION ──
                # The POST may have succeeded on the exchange even though
                # we never received the HTTP response.  Poll by order_id
                # or, if unavailable, the logs will capture this.
                logging.warning(
                    "[Unwind] Kalshi order POST timed out (client_order_id=%s). "
                    "Reconciling via REST poll…",
                    client_order_id,
                )
                # Give the exchange a moment to settle the order
                await asyncio.sleep(0.5)
                reconciled = await self._reconcile_kalshi_order(
                    leg, client_order_id,
                )
                if reconciled:
                    return True
                # Fall through to FAILED if reconciliation found nothing
            logging.error(
                "[Unwind] Kalshi order failed: %s", result.get("detail"),
            )
            leg.status = LegStatus.FAILED
            return False

        # Extract order info from REST response
        order_id = result.get("order_id", "")
        leg.exchange_order_id = order_id
        status = result.get("status", "")
        remaining = int(result.get("remaining_count", leg.contracts))
        filled = leg.contracts - remaining

        logging.info(
            "[Unwind] Kalshi order %s: status=%s filled=%d/%d remaining=%d",
            order_id, status, filled, leg.contracts, remaining,
        )

        # Step 3: Check if IOC already completed (typical case)
        if remaining == 0 or status in ("executed", "filled"):
            leg.status = LegStatus.FILLED
            leg.filled_contracts = leg.contracts
            leg.filled_at = _utc_iso()
            return True

        if filled > 0:
            # Partial fill — IOC cancelled the rest
            leg.status = LegStatus.PARTIAL if status != "canceled" else LegStatus.FILLED
            leg.filled_contracts = filled
            leg.filled_at = _utc_iso()
            logging.info(
                "[Unwind] Kalshi IOC partial fill: %d/%d contracts.",
                filled, leg.contracts,
            )
            return True

        # Step 4: No immediate fills — try WS waiter as fallback
        if order_id and self._kalshi_ws is not None and self._kalshi_ws._fills_subscribed:
            fill_waiter = self._kalshi_ws.register_fill_waiter(order_id)
            try:
                fill_data = await asyncio.wait_for(
                    fill_waiter, timeout=self._timeout,
                )
                ws_filled = fill_data.get("filled_count", 0)
                if ws_filled > 0:
                    leg.status = LegStatus.FILLED
                    leg.filled_contracts = ws_filled
                    leg.filled_at = _utc_iso()
                    return True
            except asyncio.TimeoutError:
                logging.warning(
                    "[Unwind] Kalshi WS fill timeout for order %s", order_id,
                )
                self._kalshi_ws.cancel_fill_waiter(order_id)

        # Step 5: Final REST poll
        if order_id:
            poll_result = await self._kalshi_client.get_order(order_id)
            poll_remaining = int(poll_result.get("remaining_count", leg.contracts))
            poll_filled = leg.contracts - poll_remaining
            if poll_filled > 0:
                leg.status = LegStatus.FILLED if poll_remaining == 0 else LegStatus.PARTIAL
                leg.filled_contracts = poll_filled
                leg.filled_at = _utc_iso()
                return True

        # No fill at all — IOC was fully cancelled
        leg.status = LegStatus.FAILED
        leg.filled_contracts = 0
        logging.warning(
            "[Unwind] Kalshi IOC order %s: no fill — cancelled.",
            order_id,
        )
        return False

    async def _reconcile_kalshi_order(
        self, leg: LegOrder, client_order_id: str,
    ) -> bool:
        """Reconcile a Kalshi order after a POST timeout.

        The HTTP POST timed out, so we never received the response.
        However, the exchange may have accepted and filled the order.
        Query recent orders by ticker to find a matching order and
        determine its true state.

        Returns True if the order was found and had fills.
        """
        if self._kalshi_client is None:
            return False

        ticker = leg.market_id
        try:
            orders = await self._kalshi_client.get_orders(
                ticker=ticker, status="executed",
            )
            # Also check canceled (IOC partial fills show as canceled)
            orders += await self._kalshi_client.get_orders(
                ticker=ticker, status="canceled",
            )
        except Exception as exc:
            logging.error(
                "[Unwind] Kalshi reconciliation query failed: %s", exc,
            )
            return False

        # Find the order matching our client_order_id
        for order in orders:
            oid_match = order.get("client_order_id", "") == client_order_id
            if not oid_match:
                continue

            order_id = order.get("order_id", "")
            leg.exchange_order_id = order_id
            remaining = int(order.get("remaining_count", leg.contracts))
            filled = leg.contracts - remaining

            logging.info(
                "[Unwind] Reconciled Kalshi order %s "
                "(client_order_id=%s): filled=%d/%d",
                order_id, client_order_id, filled, leg.contracts,
            )

            if filled > 0:
                leg.status = LegStatus.FILLED if remaining == 0 else LegStatus.PARTIAL
                leg.filled_contracts = filled
                leg.filled_at = _utc_iso()
                return True

            # Order was found but 0 fills
            return False

        logging.warning(
            "[Unwind] Kalshi reconciliation: no order found for "
            "client_order_id=%s on %s.",
            client_order_id, ticker,
        )
        return False

    async def _submit_poly_buy(self, leg: LegOrder) -> bool:
        """Polymarket FOK buy order via py_clob_client.

        Flow:
          1. Resolve token_id from condition_id + side.
          2. Create and post FOK order via PolymarketOrderClient.
          3. Check response status: "matched" = filled, "unmatched" = killed.

        FOK orders on Polymarket either fill completely or are killed.
        No partial fills.
        """
        if self._poly_client is None:
            logging.error("[Unwind] Live mode but no PolymarketOrderClient configured.")
            leg.status = LegStatus.FAILED
            return False

        condition_id = leg.market_id
        if not condition_id:
            logging.error("[Unwind] Poly leg has no market_id (condition_id).")
            leg.status = LegStatus.FAILED
            return False

        # Resolve the specific token_id for this side
        token_id = self._poly_client.resolve_token_id(condition_id, leg.side)
        if not token_id:
            logging.error(
                "[Unwind] Cannot resolve token_id for %s side=%s.",
                condition_id[:16], leg.side,
            )
            leg.status = LegStatus.FAILED
            return False

        # Submit FOK buy order
        result = await self._poly_client.place_order(
            token_id=token_id,
            side="buy",
            price=float(leg.price),
            size=float(leg.contracts),
            order_type="fok",
        )

        if result.get("error"):
            logging.error("[Unwind] Poly order failed: %s", result.get("detail"))
            leg.status = LegStatus.FAILED
            return False

        order_id = result.get("orderID", "")
        leg.exchange_order_id = order_id
        status = str(result.get("status", "")).lower()

        if status == "matched":
            leg.status = LegStatus.FILLED
            leg.filled_contracts = leg.contracts
            leg.filled_at = _utc_iso()
            logging.info(
                "[Unwind] Poly FOK matched: orderID=%s, %d contracts.",
                order_id, leg.contracts,
            )
            return True

        if status == "delayed":
            # Rare: order is still being processed.  Poll for result.
            logging.info("[Unwind] Poly FOK delayed — polling for result…")
            for _ in range(int(self._timeout * 1000 / KALSHI_FILL_POLL_MS)):
                await asyncio.sleep(KALSHI_FILL_POLL_MS / 1000)
                poll = await self._poly_client.get_order(order_id)
                poll_status = str(poll.get("status", "")).lower()
                if poll_status == "matched":
                    leg.status = LegStatus.FILLED
                    leg.filled_contracts = leg.contracts
                    leg.filled_at = _utc_iso()
                    return True
                if poll_status in ("unmatched", "canceled", "cancelled"):
                    break

        # FOK was killed (unmatched) or timed out
        leg.status = LegStatus.FAILED
        leg.filled_contracts = 0
        logging.warning(
            "[Unwind] Poly FOK unmatched/killed: orderID=%s status=%s",
            order_id, status,
        )
        return False

    async def _submit_unwind(self, leg: LegOrder) -> bool:
        """Submit an unwind (sell) order.

        Paper mode: instant fill.
        Live mode:  Kalshi IOC sell / Polymarket FOK sell with aggressive pricing.
        """
        leg.submitted_at = _utc_iso()
        leg.status = LegStatus.SUBMITTED

        if not self._live_mode:
            # Paper mode: instant sell
            await asyncio.sleep(0)
            leg.status = LegStatus.UNWOUND
            leg.filled_contracts = leg.contracts
            leg.filled_at = _utc_iso()
            return True

        # ── LIVE MODE UNWIND ──
        if leg.platform == "kalshi":
            return await self._sell_kalshi(leg)
        else:
            return await self._sell_poly(leg)

    async def _sell_kalshi(self, leg: LegOrder) -> bool:
        """Kalshi IOC sell order for unwind.

        Uses a slippage-protected price floor: the higher of
        (buy_price × UNWIND_MIN_PRICE_RATIO) or UNWIND_ABSOLUTE_MIN_PRICE.
        IOC cancels any unfilled remainder.
        """
        if self._kalshi_client is None:
            leg.status = LegStatus.FAILED
            return False

        ticker = leg.market_id
        if not ticker:
            leg.status = LegStatus.FAILED
            return False

        # Slippage-protected sell price (in cents)
        floor_price = max(
            leg.price * UNWIND_MIN_PRICE_RATIO,
            UNWIND_ABSOLUTE_MIN_PRICE,
        ).quantize(Q2)
        price_cents = max(1, int((floor_price * HUNDRED).quantize(D("1"))))

        logging.info(
            "[Unwind] Kalshi sell: buy_price=$%s → floor=$%s (%dc)",
            leg.price.quantize(Q4), floor_price.quantize(Q4), price_cents,
        )

        result = await self._kalshi_client.place_order(
            ticker=ticker,
            action="sell",
            side=leg.side,
            count=leg.contracts,
            price_cents=price_cents,
            time_in_force="ioc",
        )

        if result.get("error"):
            logging.error("[Unwind] Kalshi sell failed: %s", result.get("detail"))
            leg.status = LegStatus.FAILED
            return False

        order_id = result.get("order_id", "")
        leg.exchange_order_id = order_id
        remaining = int(result.get("remaining_count", leg.contracts))
        filled = leg.contracts - remaining

        if filled > 0:
            leg.status = LegStatus.UNWOUND
            leg.filled_contracts = filled
            leg.filled_at = _utc_iso()

            # Update the sell price to the actual execution price
            # (for slippage calculation)
            actual_price_cents = result.get("yes_price") or result.get("no_price")
            if actual_price_cents is not None:
                leg.price = D(str(actual_price_cents)) / HUNDRED

            logging.info(
                "[Unwind] Kalshi sell filled: %d/%d contracts @ %dc.",
                filled, leg.contracts, actual_price_cents or price_cents,
            )
            return True

        leg.status = LegStatus.FAILED
        leg.filled_contracts = 0
        logging.warning("[Unwind] Kalshi sell: no fill — order %s.", order_id)
        return False

    async def _fetch_poly_bid_depth(
        self, token_id: str,
    ) -> tuple[int, list[dict]]:
        """Fetch Polymarket orderbook and return (total_bid_qty, bid_levels).

        Uses MarketFetcher._fetch_poly_book() for a fresh REST snapshot.
        Returns (0, []) if the book is unavailable.
        """
        if self._fetcher is None:
            return 0, []
        book = await self._fetcher._fetch_poly_book(token_id)
        if book is None:
            return 0, []
        bids = book.get("bids", []) or book.get("buys", [])
        if not bids:
            return 0, []
        # Sort bids descending by price so we consume best prices first
        sorted_bids = sorted(
            bids, key=lambda lvl: D(str(lvl.get("price", 0))), reverse=True,
        )
        total_qty = sum(int(D(str(lvl.get("size", 0)))) for lvl in sorted_bids)
        return total_qty, sorted_bids

    async def _sell_poly(self, leg: LegOrder) -> bool:
        """Polymarket smart-sized FOK sell order for unwind.

        Smart Sizing approach:
          1. Fetch the live orderbook for the token.
          2. Sum total resting bid quantity across all price levels.
          3. Cap FOK size to min(leg.contracts, total_bids) so the FOK
             never fails due to insufficient counterparty depth.
          4. If no bids exist, retry up to 3 times (0.5s apart).
          5. Send FOK at slippage-protected price floor (not $0.01).
          6. If unwind_size < leg.contracts, mark as FAILED so the
             StuckPositionMonitor can clean up the remainder — but we
             dumped what we could immediately.
        """
        if self._poly_client is None:
            leg.status = LegStatus.FAILED
            return False

        condition_id = leg.market_id
        token_id = self._poly_client.resolve_token_id(condition_id, leg.side)
        if not token_id:
            logging.error("[Unwind] Cannot resolve token for Poly unwind sell.")
            leg.status = LegStatus.FAILED
            return False

        # ── Step 1-3: Fetch book and determine available depth ──
        max_book_retries = 3
        total_bids = 0
        for attempt in range(max_book_retries):
            total_bids, _ = await self._fetch_poly_bid_depth(token_id)
            if total_bids > 0:
                break
            if attempt < max_book_retries - 1:
                logging.warning(
                    "[Unwind] Poly sell: no bids on book (attempt %d/%d), "
                    "retrying in 0.5s…",
                    attempt + 1, max_book_retries,
                )
                await asyncio.sleep(0.5)

        if total_bids == 0:
            logging.error(
                "[Unwind] Poly sell: no bids after %d retries — cannot unwind.",
                max_book_retries,
            )
            leg.status = LegStatus.FAILED
            leg.filled_contracts = 0
            return False

        # Cap FOK size to available depth
        unwind_size = min(leg.contracts, total_bids)
        if unwind_size < leg.contracts:
            logging.warning(
                "[Unwind] Poly sell: capping FOK to %d/%d contracts "
                "(book depth = %d).",
                unwind_size, leg.contracts, total_bids,
            )

        # ── Step 4: Send FOK at slippage-protected price ──
        floor_price = max(
            leg.price * UNWIND_MIN_PRICE_RATIO,
            UNWIND_ABSOLUTE_MIN_PRICE,
        ).quantize(Q4)
        sell_price = float(floor_price)
        logging.info(
            "[Unwind] Poly sell: buy_price=$%s → floor=$%.4f",
            leg.price.quantize(Q4), sell_price,
        )
        result = await self._poly_client.place_order(
            token_id=token_id,
            side="sell",
            price=sell_price,
            size=float(unwind_size),
            order_type="fok",
        )

        if result.get("error"):
            logging.error(
                "[Unwind] Poly sell API error: %s", result.get("error"),
            )
            leg.status = LegStatus.FAILED
            leg.filled_contracts = 0
            return False

        status = str(result.get("status", "")).lower()
        if status == "matched":
            leg.exchange_order_id = result.get("orderID", "")
            leg.filled_contracts = unwind_size
            leg.filled_at = _utc_iso()
            leg.price = D(str(sell_price))
            logging.info(
                "[Unwind] Poly sell matched at $%.4f, %d/%d contracts.",
                sell_price, unwind_size, leg.contracts,
            )
            if unwind_size >= leg.contracts:
                leg.status = LegStatus.UNWOUND
                return True
            else:
                # Partial unwind — dumped what was available, remainder
                # will be handled by StuckPositionMonitor on next pass.
                leg.status = LegStatus.FAILED
                logging.warning(
                    "[Unwind] Poly sell partial: %d contracts remain exposed.",
                    leg.contracts - unwind_size,
                )
                return False

        # FOK was not matched even at capped size — book moved
        logging.warning(
            "[Unwind] Poly sell: FOK for %d contracts not matched "
            "(book may have moved).",
            unwind_size,
        )
        leg.status = LegStatus.FAILED
        leg.filled_contracts = 0
        return False

    # ── Stuck position detection ──────────────────────────────────

    def get_stuck_positions(self, max_age_seconds: int) -> list[UnwindOrder]:
        """Return active unwinds that have been stuck for > max_age_seconds.

        An unwind is 'stuck' if it's in LEG1_FILLED or LEG2_SUBMITTED
        state for too long (Leg 2 hasn't completed).
        """
        stuck: list[UnwindOrder] = []
        now = _utc_now()
        stuck_states = {
            UnwindStatus.LEG1_FILLED,
            UnwindStatus.LEG2_SUBMITTED,
            UnwindStatus.LEG1_SUBMITTED,
        }
        for uw in list(self._active.values()):
            if uw.status not in stuck_states:
                continue
            created = _parse_iso(uw.created_at)
            if created and (now - created).total_seconds() > max_age_seconds:
                stuck.append(uw)
        return stuck

    async def force_unwind(self, uw: UnwindOrder) -> None:
        """Force-unwind a stuck position.  Called by StuckPositionMonitor."""
        logging.warning(
            "  [Unwind %s] FORCE UNWIND triggered by StuckPositionMonitor",
            uw.unwind_id,
        )
        self._notify(
            f"[StuckMonitor] Force-unwinding {uw.unwind_id} "
            f"[{uw.event_name}] — stuck in {uw.status.value}",
            level="warning",
        )
        await self._unwind(uw)

    # ── Lifecycle management ──────────────────────────────────────

    async def _finish(self, uw: UnwindOrder) -> None:
        """Move an unwind order from active to completed."""
        uw.completed_at = _utc_iso()
        self._active.pop(uw.unwind_id, None)
        self._completed.append(uw)
        # Cap completed list
        if len(self._completed) > MAX_ORDERS_IN_MEMORY:
            self._completed = self._completed[-MAX_ORDERS_IN_MEMORY:]
        await self._logger.log(uw)

    def _notify(self, message: str, level: str = "info") -> None:
        """Fire Discord alert via DiscordNotifier (Phase 4).

        Also logs the message at the appropriate level.
        Errors in notification delivery are silently caught to avoid
        disrupting trade execution.
        """
        log_level = getattr(logging, level.upper(), logging.INFO)
        logging.log(log_level, message)
        if self._notify_callback is not None:
            try:
                asyncio.ensure_future(
                    self._notify_callback.notify_unwind(
                        # Find the most recent active/completed order to send
                        self._completed[-1] if self._completed else UnwindOrder(
                            unwind_id="unknown", event_name="unknown",
                            direction="unknown", error_message=message,
                        )
                    )
                )
            except Exception:
                pass  # never disrupt execution for notification failures

    # ── Accessors ──────────────────────────────────────────────────

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def completed_count(self) -> int:
        return len(self._completed)

    @property
    def complete_count(self) -> int:
        return sum(
            1 for uw in self._completed
            if uw.status == UnwindStatus.COMPLETE
        )

    @property
    def unwound_count(self) -> int:
        return sum(
            1 for uw in self._completed
            if uw.status in (UnwindStatus.UNWOUND, UnwindStatus.FAILED)
        )


# ====================================================================
# DiscordNotifier — async webhook notifications (Phase 4)
# ====================================================================

class DiscordNotifier:
    """Async Discord webhook notifier with rate limiting and embed formatting.

    Sends rich embeds for all critical bot events:
      - Order filled (green)
      - Order rejected (red)
      - Unwind triggered (yellow)
      - Circuit breaker activated (red)
      - WS reconnection (yellow)
      - Error-level log messages (red)
      - Daily summary (green/red)

    Rate-limited to DISCORD_MAX_POSTS_PER_SEC (default 5) per Discord
    API limits.  If no webhook URL is configured, all methods are no-ops.
    """

    # Discord embed color codes
    COLOR_GREEN = 0x2ECC71   # fills, success
    COLOR_RED = 0xE74C3C     # errors, rejects, circuit breaker
    COLOR_YELLOW = 0xF1C40F  # warnings, unwinds, reconnects

    def __init__(
        self,
        webhook_url: str = "",
        max_posts_per_sec: float = DISCORD_MAX_POSTS_PER_SEC,
    ) -> None:
        self._url = webhook_url.strip()
        self._enabled = bool(self._url)
        self._session: Optional[aiohttp.ClientSession] = None
        # Token-bucket rate limiter
        self._min_interval = 1.0 / max_posts_per_sec if max_posts_per_sec > 0 else 0.2
        self._last_post: float = 0.0
        self._lock = asyncio.Lock()

    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Core send method ──────────────────────────────────────────

    async def send(
        self,
        title: str,
        description: str = "",
        color: int = COLOR_GREEN,
        fields: list[dict] | None = None,
    ) -> None:
        """Send a Discord embed via webhook POST.

        Rate-limited and error-tolerant — failures are logged but never
        propagated to avoid disrupting bot operation.
        """
        if not self._enabled:
            return

        # Rate limit
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._last_post + self._min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_post = asyncio.get_event_loop().time()

        embed: dict = {
            "title": title[:256],  # Discord limit
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if description:
            embed["description"] = description[:4096]  # Discord limit
        if fields:
            embed["fields"] = [
                {
                    "name": f.get("name", "")[:256],
                    "value": f.get("value", "")[:1024],
                    "inline": f.get("inline", True),
                }
                for f in fields[:25]  # Discord max 25 fields
            ]

        payload = {"embeds": [embed]}

        try:
            await self._ensure_session()
            async with self._session.post(
                self._url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 429:
                    # Discord rate limit hit — back off
                    retry_after = (await resp.json()).get("retry_after", 1.0)
                    logging.warning(
                        "[Discord] Rate limited, retry after %.1fs",
                        retry_after,
                    )
                    await asyncio.sleep(float(retry_after))
                    # Retry once
                    async with self._session.post(
                        self._url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as retry_resp:
                        if retry_resp.status >= 400:
                            logging.warning(
                                "[Discord] Retry failed: %d", retry_resp.status,
                            )
                elif resp.status >= 400:
                    body = await resp.text()
                    logging.warning(
                        "[Discord] Webhook POST failed: %d — %s",
                        resp.status, body[:200],
                    )
        except Exception as exc:
            logging.warning("[Discord] Webhook POST error: %s", exc)

    # ── Convenience notification methods ──────────────────────────

    async def notify_fill(self, order: "Order") -> None:
        """Notify: order filled (green embed)."""
        await self.send(
            title="✅ Order Filled",
            color=self.COLOR_GREEN,
            fields=[
                {"name": "Event", "value": order.event_name, "inline": True},
                {"name": "Direction", "value": order.direction, "inline": True},
                {"name": "Contracts", "value": str(order.filled_contracts), "inline": True},
                {"name": "Kalshi Price", "value": f"${order.kalshi_price.quantize(Q4)}", "inline": True},
                {"name": "Poly Price", "value": f"${order.poly_price.quantize(Q4)}", "inline": True},
                {"name": "Net Profit/c", "value": f"${order.net_profit.quantize(Q4)}", "inline": True},
                {"name": "Total Outlay", "value": f"${order.total_outlay.quantize(Q2)}", "inline": True},
            ],
        )

    async def notify_reject(self, order: "Order") -> None:
        """Notify: order rejected (red embed)."""
        await self.send(
            title="❌ Order Rejected",
            color=self.COLOR_RED,
            fields=[
                {"name": "Event", "value": order.event_name, "inline": True},
                {"name": "Direction", "value": order.direction, "inline": True},
                {"name": "Reason", "value": order.reject_reason.value, "inline": True},
                {"name": "Desired Qty", "value": str(order.desired_contracts), "inline": True},
                {"name": "Depth K/P", "value": f"{order.kalshi_depth}/{order.poly_depth}", "inline": True},
            ],
        )

    async def notify_unwind(self, uw: "UnwindOrder") -> None:
        """Notify: unwind triggered (yellow embed)."""
        await self.send(
            title="⚠️ Unwind Triggered",
            color=self.COLOR_YELLOW,
            fields=[
                {"name": "Unwind ID", "value": uw.unwind_id, "inline": True},
                {"name": "Event", "value": uw.event_name, "inline": True},
                {"name": "Direction", "value": uw.direction, "inline": True},
                {"name": "Status", "value": uw.status.value, "inline": True},
                {"name": "Contracts", "value": str(uw.total_contracts), "inline": True},
                {"name": "Details", "value": uw.error_message or "n/a", "inline": False},
            ],
        )

    async def notify_circuit_breaker(
        self, daily_pnl: Decimal, limit: Decimal,
    ) -> None:
        """Notify: circuit breaker activated (red embed)."""
        await self.send(
            title="🚨 Circuit Breaker Tripped",
            description=(
                f"Daily P&L **${daily_pnl.quantize(Q4)}** "
                f"exceeds **-${limit.quantize(Q2)}** limit.\n"
                "All new orders are blocked for the rest of the day."
            ),
            color=self.COLOR_RED,
        )

    async def notify_reconnect(
        self, platform: str, attempt: int,
    ) -> None:
        """Notify: WS reconnection event (yellow embed)."""
        await self.send(
            title=f"🔌 {platform} WS Reconnecting",
            description=f"Connection lost.  Reconnect attempt **#{attempt}**.",
            color=self.COLOR_YELLOW,
        )

    async def notify_error(self, message: str) -> None:
        """Notify: error-level log message (red embed)."""
        await self.send(
            title="🔴 Error",
            description=message[:4096],
            color=self.COLOR_RED,
        )

    async def send_daily_summary(
        self,
        portfolio: "PaperPortfolio",
        pnl_tracker: "DailyPnLTracker",
        execution: "ExecutionEngine",
        unwind_mgr: "UnwindManager",
    ) -> None:
        """Send end-of-day summary embed."""
        pnl = pnl_tracker.daily_pnl
        color = self.COLOR_GREEN if pnl >= ZERO else self.COLOR_RED
        await self.send(
            title="📊 Daily Summary",
            color=color,
            fields=[
                {"name": "Daily P&L", "value": f"${pnl.quantize(Q4)}", "inline": True},
                {"name": "Loss Limit", "value": f"-${pnl_tracker.daily_limit.quantize(Q2)}", "inline": True},
                {"name": "Capital", "value": f"${portfolio.capital.quantize(Q2)}", "inline": True},
                {"name": "Open Positions", "value": str(len(portfolio.open_positions)), "inline": True},
                {"name": "Orders", "value": f"{execution.filled_count} filled, {execution.rejected_count} rejected", "inline": True},
                {"name": "Unwinds", "value": f"{unwind_mgr.complete_count} ok, {unwind_mgr.unwound_count} unwound", "inline": True},
            ],
        )


async def _daily_summary_task(
    notifier: DiscordNotifier,
    portfolio: "PaperPortfolio",
    pnl_tracker: "DailyPnLTracker",
    execution: "ExecutionEngine",
    unwind_mgr: "UnwindManager",
    stop_event: asyncio.Event,
) -> None:
    """Background task: sends a daily summary at midnight UTC."""
    while not stop_event.is_set():
        # Calculate seconds until next midnight UTC
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=5, microsecond=0,
        )
        wait_seconds = (tomorrow - now).total_seconds()

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
            return  # stop_event was set
        except asyncio.TimeoutError:
            pass  # midnight — time to send

        try:
            await notifier.send_daily_summary(
                portfolio, pnl_tracker, execution, unwind_mgr,
            )
            logging.info("[Discord] Daily summary sent.")
        except Exception as exc:
            logging.warning("[Discord] Failed to send daily summary: %s", exc)


# ====================================================================
# StuckPositionMonitor — detects half-filled arbs & auto-unwinds
# ====================================================================

class StuckPositionMonitor:
    """Background task that detects half-filled arbs and auto-unwinds.

    Runs every STUCK_POSITION_CHECK_INTERVAL seconds.  When an
    UnwindOrder has been in an intermediate state (LEG1_FILLED,
    LEG2_SUBMITTED) for longer than STUCK_POSITION_MAX_AGE seconds,
    triggers an automatic unwind via UnwindManager.force_unwind().

    Integrates with Discord alerts (Phase 4) — every unwind triggers
    a notification.
    """

    def __init__(
        self,
        unwind_manager: UnwindManager,
        max_age: int = STUCK_POSITION_MAX_AGE,
        check_interval: int = STUCK_POSITION_CHECK_INTERVAL,
    ) -> None:
        self._manager = unwind_manager
        self._max_age = max_age
        self._check_interval = check_interval

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main loop — runs until stop_event is set."""
        logging.info(
            "[StuckMonitor] Started (check every %ds, max age %ds)",
            self._check_interval, self._max_age,
        )
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._check_interval,
                )
                return  # stop_event was set
            except asyncio.TimeoutError:
                pass  # time to check

            stuck = self._manager.get_stuck_positions(self._max_age)
            if stuck:
                logging.warning(
                    "[StuckMonitor] Found %d stuck position(s)", len(stuck),
                )
            for uw in stuck:
                try:
                    await self._manager.force_unwind(uw)
                except Exception as exc:
                    logging.error(
                        "[StuckMonitor] Failed to unwind %s: %s",
                        uw.unwind_id, exc, exc_info=True,
                    )


# ====================================================================
# ExecutionEngine — EXECUTION: OMS + Fill-or-Kill validation
# ====================================================================
# Separates the "should we trade?" question (ArbEngine, the Strategy)
# from the "can we trade?" and "execute the trade" steps (this class).
#
# The validation pipeline (duplicate, profitability, capital, FOK)
# stays the same.  The fill step routes through UnwindManager for
# sequential two-leg execution with automatic rollback.
#
# PRODUCTION (Phase 3): Replace UnwindManager's paper-mode fills
# with real API calls to Kalshi and Polymarket.

class ExecutionEngine:
    """
    Validates and executes (simulated) trades.

    Architecture (Phase 2 refactor — pre-trade vs. post-trade filter):

      SILENT PRE-TRADE FILTERS (no Order created, no CSV, no rate-limit):
        1. Duplicate check — event+direction already in self._traded
        2. Risk management — kill switch, daily loss, exposure, positions,
           spread sanity (but NOT rate limit — that's post-filter)
        3. Net-profitability — fees eat the spread
        4. Capital + sizing — can't afford even 1 contract
        5. FOK depth — orderbook too thin on either/both legs

      POST-TRADE GATE (Order created, CSV logged):
        6. Rate limiter — only consumed when all silent checks pass
        7. Fill — routed through UnwindManager (sequential two-leg)

    This eliminates CSV/log spam from repeated rejections on every
    WS tick while preserving instant retry when liquidity refills.
    """

    def __init__(
        self,
        portfolio: PaperPortfolio,
        trader: PaperTrader,
        order_logger: Optional[OrderLogger] = None,
        risk_manager: Optional[RiskManager] = None,
        unwind_manager: Optional[UnwindManager] = None,
        notifier: Optional[DiscordNotifier] = None,
    ):
        self._portfolio = portfolio
        self._trader = trader
        self._order_logger = order_logger or OrderLogger()
        self._risk_manager = risk_manager
        self._unwind_manager = unwind_manager
        self._notifier = notifier
        self._orders: list[Order] = []
        # Track which event+direction combos we've already traded or
        # are currently in-flight (prevents concurrency races).
        self._traded: set[tuple[str, str]] = set()
        # Per-event locks to guarantee mutual exclusion for concurrent
        # WS ticks on the same event (prevents the TOCTOU race between
        # the `if key in self._traded` check and `self._traded.add(key)`).
        self._event_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def submit(self, opp: ArbOpportunity) -> Optional[Order]:
        """
        Submit a candidate arb trade for execution.

        Pre-trade filters silently return None (no Order, no CSV, no
        rate-limit consumed).  Only trades that pass all silent checks
        hit the rate limiter, create an Order, and write to CSV.

        If UnwindManager fails/unwinds, the (event, direction) key is
        removed from self._traded so the bot can retry on the next tick.
        """
        key = (opp.event_name, opp.direction)

        # Acquire per-event lock to prevent TOCTOU race between
        # the duplicate check and the `self._traded.add(key)` call
        # when two WS ticks for the same event arrive simultaneously.
        if key not in self._event_locks:
            self._event_locks[key] = asyncio.Lock()
        async with self._event_locks[key]:
            return await self._submit_inner(opp, key)

    async def _submit_inner(
        self, opp: ArbOpportunity, key: tuple[str, str],
    ) -> Optional[Order]:
        """Inner submit logic, called while holding the per-event lock."""

        # ── SILENT PRE-TRADE FILTERS ──
        # These return None without creating an Order, writing CSV,
        # or consuming a rate-limit slot.

        # 1. Duplicate / in-flight check
        if key in self._traded:
            logging.debug(
                "[Exec] Skipped [%s] %s — already traded or in-flight",
                opp.event_name, opp.direction,
            )
            return None

        # 2. Risk management gate (excluding rate limit)
        if self._risk_manager is not None:
            risk_reject = self._risk_manager.check(opp)
            if risk_reject is not None:
                logging.debug(
                    "[Exec] Skipped [%s] %s — %s",
                    opp.event_name, opp.direction, risk_reject.value,
                )
                return None

        # 3. Net profitability after fees
        if opp.net_profit <= ZERO:
            logging.debug(
                "[Exec] Skipped [%s] %s — net unprofitable",
                opp.event_name, opp.direction,
            )
            return None

        # 4. Compute desired contracts from capital and position size
        cost_plus_fees = opp.total_cost + opp.kalshi_fee + opp.poly_fee
        if cost_plus_fees <= ZERO:
            return None

        available = min(self._portfolio.position_size, self._portfolio.capital)
        desired = int(available / cost_plus_fees)
        if desired < 1:
            logging.debug(
                "[Exec] Skipped [%s] %s — insufficient capital for 1 contract",
                opp.event_name, opp.direction,
            )
            return None

        # 5. Fill or Kill depth check
        k_short = opp.kalshi_depth < desired
        p_short = opp.poly_depth < desired
        if k_short or p_short:
            reason = (
                "both" if (k_short and p_short)
                else ("kalshi" if k_short else "poly")
            )
            logging.debug(
                "[Exec] Skipped [%s] %s — FOK depth %s "
                "(wanted %d, K=%d P=%d)",
                opp.event_name, opp.direction, reason,
                desired, opp.kalshi_depth, opp.poly_depth,
            )
            return None

        # ── POST-TRADE GATE ──
        # All silent checks passed.  Now consume a rate-limit slot.

        if self._risk_manager is not None:
            if not self._risk_manager.rate_limiter.try_acquire():
                # Rate limit hit — log this one to CSV as a real rejection
                # so we have an audit trail, but only 1 row per event.
                order = self._make_order(opp, desired)
                order.status = OrderStatus.REJECTED
                order.reject_reason = RejectReason.ORDER_RATE_LIMIT
                await self._finalize(order)
                return order

        # ── Log the opportunity (console + CSV) ──
        _log_opportunity(opp)
        await self._trader.log(opp)

        # ── CREATE ORDER + EXECUTE ──
        order = self._make_order(opp, desired)

        # Mark in-flight BEFORE awaiting to prevent duplicate submissions
        # from concurrent WS ticks hitting submit() while we're awaiting.
        self._traded.add(key)

        try:
            if self._unwind_manager is not None:
                uw_order = await self._unwind_manager.execute(opp, desired)

                if uw_order.status != UnwindStatus.COMPLETE:
                    # Unwind occurred or execution failed — unlock the key
                    # so the bot can retry on the next tick.
                    self._traded.discard(key)

                    if uw_order.status == UnwindStatus.UNWOUND:
                        # Trade was unwound — record the real cost
                        unwind_loss = getattr(uw_order, '_unwind_loss', ZERO)
                        order.status = OrderStatus.UNWOUND
                        order.reject_reason = RejectReason.NONE
                        order.filled_contracts = 0
                        order.total_outlay = unwind_loss
                    else:
                        # Pure failure (Leg 1 never filled, etc.)
                        order.status = OrderStatus.CANCELLED
                        order.reject_reason = RejectReason.NONE
                        order.filled_contracts = 0
                        order.total_outlay = ZERO

                    await self._finalize(order)
                    return order

                # Both legs filled — use actual filled quantity
                actual = uw_order.total_contracts
                total_outlay = (D(actual) * cost_plus_fees).quantize(Q4)
                order.filled_contracts = actual
                order.total_outlay = total_outlay
                order.status = OrderStatus.FILLED

                pos = Position(
                    event_name=opp.event_name,
                    direction=opp.direction,
                    contracts=actual,
                    cost_per_contract=opp.total_cost,
                    fees_per_contract=opp.kalshi_fee + opp.poly_fee,
                    total_outlay=total_outlay,
                    opened_at=opp.timestamp,
                )
                await self._portfolio.open_position(pos)

                if self._risk_manager is not None:
                    self._risk_manager.pnl_tracker.record(opp.net_profit, actual)
            else:
                # Legacy path (no unwind manager) — instant paper fill
                total_outlay = (D(desired) * cost_plus_fees).quantize(Q4)
                order.filled_contracts = desired
                order.total_outlay = total_outlay
                order.status = OrderStatus.FILLED

                pos = Position(
                    event_name=opp.event_name,
                    direction=opp.direction,
                    contracts=desired,
                    cost_per_contract=opp.total_cost,
                    fees_per_contract=opp.kalshi_fee + opp.poly_fee,
                    total_outlay=total_outlay,
                    opened_at=opp.timestamp,
                )
                await self._portfolio.open_position(pos)

                if self._risk_manager is not None:
                    self._risk_manager.pnl_tracker.record(opp.net_profit, desired)

        except Exception as exc:
            # On any unexpected error during execution, unlock the key
            # so the bot can retry.  Do NOT re-raise — this method runs
            # via asyncio.create_task, so an unhandled exception would
            # be silently swallowed (or produce noisy GC warnings).
            self._traded.discard(key)
            logging.error(
                "[Exec] Execution failed for [%s] %s: %s",
                opp.event_name, opp.direction, exc, exc_info=True,
            )
            order.status = OrderStatus.CANCELLED
            order.reject_reason = RejectReason.NONE
            order.filled_contracts = 0
            order.total_outlay = ZERO
            await self._finalize(order)
            return order

        await self._finalize(order)
        return order

    def _make_order(self, opp: ArbOpportunity, desired: int) -> Order:
        """Instantiate an Order dataclass from an opp."""
        return Order(
            order_id=str(uuid.uuid4())[:8],
            timestamp=opp.timestamp,
            event_name=opp.event_name,
            direction=opp.direction,
            status=OrderStatus.PENDING,
            desired_contracts=desired,
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

    async def _finalize(self, order: Order) -> None:
        """Log the order to CSV and store in memory for inspection.

        Orders are always persisted to CSV immediately.  The in-memory
        list is capped at MAX_ORDERS_IN_MEMORY to prevent unbounded
        growth during long-running sessions.
        """
        self._orders.append(order)
        await self._order_logger.log(order)

        if len(self._orders) > MAX_ORDERS_IN_MEMORY:
            self._orders = self._orders[-MAX_ORDERS_IN_MEMORY:]

        if order.status == OrderStatus.FILLED:
            logging.info(
                "  ORDER %s FILLED: %d contracts [%s] %s — outlay $%s",
                order.order_id, order.filled_contracts,
                order.event_name, order.direction,
                order.total_outlay.quantize(Q2),
            )
            # Phase 4: Discord notification
            if self._notifier is not None:
                asyncio.ensure_future(self._notifier.notify_fill(order))
        elif order.status == OrderStatus.UNWOUND:
            logging.warning(
                "  ORDER %s UNWOUND: [%s] %s — "
                "loss $%s (wanted %d contracts)",
                order.order_id,
                order.event_name, order.direction,
                order.total_outlay.quantize(Q4),
                order.desired_contracts,
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
            # Phase 4: Discord notification
            if self._notifier is not None:
                asyncio.ensure_future(self._notifier.notify_reject(order))

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
    is_live = "LIVE" in mode.upper()
    logging.info("=" * 62)
    if is_live:
        logging.info("  ARBITRAGE BOT  v5.0 — LIVE TRADING")
        logging.info("  *** REAL ORDERS WILL BE PLACED ***")
    else:
        logging.info("  ARBITRAGE BOT  v5.0 — PAPER TRADING")
        logging.info("  No real trades will be executed.")
    logging.info("=" * 62)
    logging.info("  Trading mode    : %s", mode)
    logging.info("  Execution model : event-driven (WS only, stale watchdog)")
    if is_live:
        logging.info("  Kalshi orders   : IOC (Immediate-or-Cancel)")
        logging.info("  Poly orders     : FOK (Fill-or-Kill)")
    else:
        logging.info("  Order type      : Fill or Kill (FOK) — simulated")
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
    logging.info("  Daily loss limit: $%s", DAILY_LOSS_LIMIT.quantize(Q2))
    logging.info("  Max exposure    : $%s", MAX_TOTAL_EXPOSURE.quantize(Q2))
    logging.info("  Max positions   : %d", MAX_CONCURRENT_POSITIONS)
    logging.info("  Max spread      : %s%%", (MAX_SPREAD_THRESHOLD * HUNDRED).quantize(Q2))
    logging.info("  Order rate limit: %d/min", MAX_ORDERS_PER_MINUTE)
    logging.info("  Kill file       : %s", KILL_FILE)
    logging.info("  Unwind timeout  : %ds", UNWIND_TIMEOUT_SECONDS)
    logging.info("  Stuck pos age   : %ds", STUCK_POSITION_MAX_AGE)
    logging.info("  Stuck check int : %ds", STUCK_POSITION_CHECK_INTERVAL)
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

        # Use explicit token IDs from events.json (no runtime resolution)
        for ev in active_events:
            cond_id = ev["poly_condition_id"]
            yes_tok = ev["poly_yes_token"]
            no_tok = ev["poly_no_token"]
            # Pre-populate the token cache so all downstream code works
            fetcher._poly_token_cache[cond_id] = (yes_tok, no_tok)
            await poly_ws.subscribe(cond_id, yes_tok, no_tok, ev["name"])

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
                await portfolio.close_positions_for_event(ev["name"])
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
    notifier: Optional[DiscordNotifier] = None,
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
                    # Phase 3: Attach market identifiers for live execution
                    opp.kalshi_ticker = ticker
                    opp.poly_condition_id = cond_id
                    total_opps += 1
                    asyncio.create_task(execution.submit(opp))

        except Exception as exc:
            logging.error(
                "Error processing update for %s: %s", event_name, exc,
                exc_info=True,
            )
            # Phase 4: Discord notification for error-level messages
            if notifier is not None:
                asyncio.ensure_future(
                    notifier.notify_error(
                        f"Error processing update for {event_name}: {exc}"
                    )
                )

    return total_opps


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
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
             "'paper' simulates trades; 'live' places real orders "
             "on Kalshi (IOC) and Polymarket (FOK).",
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

    live_mode = (args.mode == "live")

    # ── Validate live-mode prerequisites ──
    if live_mode:
        missing: list[str] = []
        if not KALSHI_API_KEY:
            missing.append("KALSHI_API_KEY")
        if not KALSHI_PRIVATE_KEY_PATH:
            missing.append("KALSHI_PRIVATE_KEY_PATH")
        if not POLY_API_KEY:
            missing.append("POLY_API_KEY")
        if not POLY_API_SECRET:
            missing.append("POLY_API_SECRET")
        if not POLY_PASSPHRASE:
            missing.append("POLY_PASSPHRASE")
        if not POLY_PRIVATE_KEY_PATH:
            missing.append("POLY_PRIVATE_KEY_PATH")
        if missing:
            logging.error(
                "Live mode requires these env vars: %s",
                ", ".join(missing),
            )
            sys.exit(1)
        logging.warning("=" * 62)
        logging.warning("  *** LIVE TRADING MODE ***")
        logging.warning("  Real orders WILL be placed on Kalshi and Polymarket.")
        logging.warning("=" * 62)

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

    # ── Risk management (Phase 1) ──
    pnl_tracker = DailyPnLTracker(daily_limit=DAILY_LOSS_LIMIT)
    order_rate_limiter = OrderRateLimiter(max_per_minute=MAX_ORDERS_PER_MINUTE)
    risk_manager = RiskManager(
        pnl_tracker=pnl_tracker,
        rate_limiter=order_rate_limiter,
        portfolio=portfolio,
        max_exposure=MAX_TOTAL_EXPOSURE,
        max_positions=MAX_CONCURRENT_POSITIONS,
        max_spread=MAX_SPREAD_THRESHOLD,
        kill_file=KILL_FILE,
    )

    # ── Discord notifications (Phase 4) ──
    notifier = DiscordNotifier(webhook_url=DISCORD_WEBHOOK_URL)
    if notifier._enabled:
        logging.info("[Phase 4] Discord notifications ENABLED.")
    else:
        logging.info("[Phase 4] Discord notifications disabled (no DISCORD_WEBHOOK_URL).")
    # Wire notifier into circuit-breaker tracker
    pnl_tracker.set_notifier(notifier)

    # ── Connect WebSockets ──
    kalshi_ws, poly_ws = await _setup_websockets(
        active_events, fetcher, update_queue,
    )

    # Phase 4: Attach notifier to WS classes for reconnect alerts
    if kalshi_ws:
        kalshi_ws._notifier = notifier
    if poly_ws:
        poly_ws._notifier = notifier

    # ── Phase 3: Order clients (live mode only) ──
    kalshi_client: Optional[KalshiOrderClient] = None
    poly_client: Optional[PolymarketOrderClient] = None

    if live_mode:
        kalshi_client = KalshiOrderClient()
        # Share the token cache between MarketFetcher and PolymarketOrderClient
        # so token resolution (condition_id → token_ids) is done once.
        poly_client = PolymarketOrderClient(
            poly_token_cache=fetcher._poly_token_cache,
        )
        # Pre-initialize the Poly client to catch credential errors early
        try:
            poly_client._ensure_client()
        except Exception as exc:
            logging.error("Failed to initialize Polymarket client: %s", exc)
            if kalshi_ws:
                await kalshi_ws.close()
            if poly_ws:
                await poly_ws.close()
            await fetcher.close()
            await notifier.close()
            sys.exit(1)

        # Subscribe to Kalshi fill channel for live order tracking
        if kalshi_ws:
            await kalshi_ws.subscribe_fills()
            logging.info("[Phase 3] Kalshi WS fill channel active.")

        logging.info("[Phase 3] Order clients initialized for live trading.")

    # ── Unwind module (Phase 2 + Phase 3 live integration) ──
    unwind_logger = UnwindLogger(path=UNWIND_LOG_FILE)
    unwind_manager = UnwindManager(
        portfolio=portfolio,
        pnl_tracker=pnl_tracker,
        timeout=UNWIND_TIMEOUT_SECONDS,
        logger=unwind_logger,
        live_mode=live_mode,
        kalshi_client=kalshi_client,
        poly_client=poly_client,
        kalshi_ws=kalshi_ws,
        poly_ws=poly_ws,
        fetcher=fetcher,
    )
    # Phase 4: Wire notifier into UnwindManager
    unwind_manager.set_notify_callback(notifier)

    execution = ExecutionEngine(
        portfolio=portfolio,
        trader=trader,
        risk_manager=risk_manager,
        unwind_manager=unwind_manager,
        notifier=notifier,
    )

    mode_parts = []
    trade_mode = "LIVE" if live_mode else "PAPER"
    if kalshi_ws:
        mode_parts.append("Kalshi=WS")
    else:
        mode_parts.append("Kalshi=NONE")
    if poly_ws:
        mode_parts.append("Poly=WS")
    else:
        mode_parts.append("Poly=NONE")
    mode_str = f"{trade_mode} ({', '.join(mode_parts)})"

    _print_banner(active_events, mode=mode_str)

    # Phase 4: Startup notification
    await notifier.send(
        title="🟢 Bot Started",
        description=f"Mode: **{mode_str}** — tracking **{len(active_events)}** events.",
        color=DiscordNotifier.COLOR_GREEN,
    )

    # ── Dry-run: print config and exit ──
    if args.dry_run:
        logging.info("--dry-run flag set. Exiting without trading.")
        if kalshi_ws:
            await kalshi_ws.close()
        if poly_ws:
            await poly_ws.close()
        await fetcher.close()
        await notifier.close()
        if kalshi_client:
            await kalshi_client.close()
        if poly_client:
            await poly_client.close()
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

    # Stuck position monitor (Phase 2): auto-unwinds half-filled arbs
    stuck_monitor = StuckPositionMonitor(
        unwind_manager=unwind_manager,
        max_age=STUCK_POSITION_MAX_AGE,
        check_interval=STUCK_POSITION_CHECK_INTERVAL,
    )
    bg_tasks.append(asyncio.create_task(
        stuck_monitor.run(stop_event),
        name="stuck-monitor",
    ))

    # Daily summary task (Phase 4): sends P&L summary at midnight UTC
    bg_tasks.append(asyncio.create_task(
        _daily_summary_task(
            notifier, portfolio, pnl_tracker,
            execution, unwind_manager, stop_event,
        ),
        name="daily-summary",
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
            notifier=notifier,
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
        await notifier.close()
        if kalshi_client:
            await kalshi_client.close()
        if poly_client:
            await poly_client.close()

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
        logging.info(
            "  Daily P&L        : $%s (limit: -$%s)",
            pnl_tracker.daily_pnl.quantize(Q4),
            pnl_tracker.daily_limit.quantize(Q2),
        )
        logging.info(
            "  Unwind orders    : %d complete, %d unwound, %d active",
            unwind_manager.complete_count,
            unwind_manager.unwound_count,
            unwind_manager.active_count,
        )
        if live_mode:
            logging.info("  Trading mode     : LIVE")
        else:
            logging.info("  Trading mode     : PAPER")

        # Phase 4: Shutdown notification
        await notifier.send(
            title="🔴 Bot Stopped",
            description=(
                f"Detected **{total_opps}** opportunities.\n"
                f"Daily P&L: **${pnl_tracker.daily_pnl.quantize(Q4)}**"
            ),
            color=DiscordNotifier.COLOR_RED,
        )
        await notifier.close()

    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
