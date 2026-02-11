#!/usr/bin/env python3
"""
==========================================================================
  Prediction Market Arbitrage — Paper-Trading Bot
  Monitors Kalshi and Polymarket for cross-platform arbitrage opportunities.

  PAPER TRADING ONLY — no real orders are ever placed.
==========================================================================

  Dependencies (run once):
      pip install requests

  Usage:
      1. Edit the EVENTS list at the bottom of the config section.
      2. Run:  python arb_bot.py
      3. Check trade_ledger.csv for logged opportunities.
"""

import csv
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

# ====================================================================
# CONFIGURATION — edit these values to suit your needs
# ====================================================================

CHECK_INTERVAL: int = 10        # Seconds between each polling cycle
MIN_PROFIT_MARGIN: float = 0.02 # Minimum spread (2%) to log a paper trade
LEDGER_FILE: str = "trade_ledger.csv"
REQUEST_TIMEOUT: int = 10       # HTTP timeout in seconds

# ---- API base URLs (no trailing slash) ----
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# ====================================================================
# EVENTS TO MONITOR — add your own pairs here
# ====================================================================
# Each entry maps the same real-world event across both platforms.
#   name            : a human-readable label (for logs / CSV)
#   kalshi_ticker   : the Kalshi market ticker string
#   poly_condition_id : the Polymarket condition_id (hex string)

EVENTS: list[dict] = [
    # ---- Fordham Rams vs Saint Louis Billikens (Women's CBB, Feb 11) ----
    # Kalshi: https://kalshi.com/markets/kxncaawbgame/college-basketball-womens-game/kxncaawbgame-26feb11forslu
    #   Kalshi splits this into two markets (one per team). We use the
    #   Fordham ticker so that "Yes" = "Fordham wins", aligning with
    #   Polymarket outcome[0] = "Fordham Rams".
    # Polymarket: https://polymarket.com/sports/cwbb/cwbb-fordm-stlou-2026-02-11
    {
        "name": "WCBB: Fordham vs Saint Louis (Feb 11)",
        "kalshi_ticker": "KXNCAAWBGAME-26FEB11FORSLU-FOR",
        "poly_condition_id": "0x7cfb79cb050bb34ca08509bd3dfd0d566a8a9876ee9e2a7e227ee4ee8ba05c35",
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
    theoretical_profit: float
    margin: float


# ====================================================================
# MarketFetcher — all API communication lives here
# ====================================================================

class MarketFetcher:
    """
    Retrieves top-of-book data from Kalshi and Polymarket.

    - Kalshi:      REST orderbook endpoint (public, no auth required).
    - Polymarket:  Gamma API to resolve condition_id → CLOB token IDs,
                   then CLOB /book endpoint for the orderbook.
    """

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "PredictionArbBot/1.0 (paper-trading)",
        })
        # Cache so we only resolve condition_id → tokens once per run
        self._poly_token_cache: dict[str, tuple[str, str]] = {}

    # ----------------------------------------------------------------
    #  Kalshi
    # ----------------------------------------------------------------

    def fetch_kalshi(self, ticker: str) -> Optional[MarketSnapshot]:
        """
        Fetch the Kalshi orderbook for *ticker* and return a MarketSnapshot.

        Kalshi's binary orderbook returns YES bids and NO bids only.
        Asks are implied:
            Best Ask(Yes) = 1.00 − Best Bid(No)
            Best Ask(No)  = 1.00 − Best Bid(Yes)

        Prices in the legacy "yes"/"no" arrays are in CENTS (0-99).
        Each entry is [price_cents, quantity].
        """
        url = f"{KALSHI_BASE}/markets/{ticker}/orderbook"
        try:
            resp = self._session.get(url, params={"depth": 5}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logging.error("[Kalshi] Orderbook request failed for %s: %s", ticker, exc)
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
    #  Polymarket — helper: resolve condition_id to CLOB token IDs
    # ----------------------------------------------------------------

    def _resolve_poly_tokens(self, condition_id: str) -> Optional[tuple[str, str]]:
        """
        Resolve a condition_id into the pair of CLOB token IDs:
        (yes_token_id, no_token_id).

        Uses the CLOB's own GET /markets/{condition_id} endpoint, which
        returns the canonical token IDs that the CLOB recognises.

        NOTE: The Gamma API's condition_id query param does NOT filter
        correctly (it returns unrelated markets), so we must use the
        CLOB endpoint instead.

        Results are cached for the lifetime of this MarketFetcher instance.
        """
        if condition_id in self._poly_token_cache:
            return self._poly_token_cache[condition_id]

        url = f"{CLOB_BASE}/markets/{condition_id}"
        try:
            resp = self._session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            market = resp.json()
        except requests.RequestException as exc:
            logging.error(
                "[Polymarket] CLOB market lookup failed for %s…: %s",
                condition_id[:16], exc,
            )
            return None

        tokens = market.get("tokens", [])
        if len(tokens) < 2:
            logging.error(
                "[Polymarket] Expected 2 tokens, got %d for %s…",
                len(tokens), condition_id[:16],
            )
            return None

        # tokens[0] = first outcome (Yes / Team A), tokens[1] = second outcome
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
    #  Polymarket — helper: fetch price for a single token
    # ----------------------------------------------------------------

    def _fetch_poly_price(self, token_id: str, side: str) -> Optional[float]:
        """
        Fetch a single price via CLOB GET /price?token_id=…&side=BUY|SELL.
        Returns the price as a float, or None on failure.

        The /price endpoint is more reliable than /book for sports markets.
        """
        url = f"{CLOB_BASE}/price"
        try:
            resp = self._session.get(
                url,
                params={"token_id": token_id, "side": side},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            price = float(resp.json().get("price", 0))
            return price if price > 0 else None
        except (requests.RequestException, ValueError, TypeError) as exc:
            logging.error(
                "[Polymarket] Price request failed for token=%s… side=%s: %s",
                token_id[:16], side, exc,
            )
            return None

    def _build_tob(self, token_id: str) -> TopOfBook:
        """
        Build a TopOfBook for one token using the /price endpoint.
        BUY price = best ask (what you'd pay to buy).
        SELL price = best bid (what you'd receive to sell).
        """
        tob = TopOfBook()
        tob.best_ask = self._fetch_poly_price(token_id, "BUY")
        tob.best_bid = self._fetch_poly_price(token_id, "SELL")
        return tob

    # ----------------------------------------------------------------
    #  Polymarket — public method
    # ----------------------------------------------------------------

    def fetch_polymarket(self, condition_id: str) -> Optional[MarketSnapshot]:
        """
        Fetch top-of-book from Polymarket for both the Yes and No tokens
        associated with *condition_id*.

        Uses CLOB /price (not /book) because the /book endpoint returns
        404 for many sports markets.
        """
        tokens = self._resolve_poly_tokens(condition_id)
        if tokens is None:
            return None

        yes_token, no_token = tokens
        yes_tob = self._build_tob(yes_token)
        no_tob = self._build_tob(no_token)

        return MarketSnapshot(yes=yes_tob, no=no_tob, source="polymarket")


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
                        theoretical_profit=profit,
                        margin=profit,
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
                        theoretical_profit=profit,
                        margin=profit,
                    ))

        return opportunities


# ====================================================================
# PaperTrader — CSV ledger for hypothetical trades
# ====================================================================

class PaperTrader:
    """
    Writes every detected opportunity to a CSV file.
    Creates the file with headers on first run.
    """

    COLUMNS = [
        "timestamp",
        "event_name",
        "direction",
        "kalshi_price",
        "poly_price",
        "total_cost",
        "theoretical_profit",
        "margin",
    ]

    def __init__(self, path: str = LEDGER_FILE):
        self.path = path
        self._ensure_header()

    def _ensure_header(self):
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
            "theoretical_profit": f"{opp.theoretical_profit:.4f}",
            "margin": f"{opp.margin:.2%}",
        }
        with open(self.path, mode="a", newline="") as fh:
            csv.DictWriter(fh, fieldnames=self.COLUMNS).writerow(row)


# ====================================================================
# Helpers
# ====================================================================

def _fmt_price(price: Optional[float]) -> str:
    """Format a price for logging, handling None gracefully."""
    return f"${price:.4f}" if price is not None else "  n/a  "


def _print_banner() -> None:
    """Print a startup banner with current configuration."""
    logging.info("=" * 62)
    logging.info("  ARBITRAGE PAPER-TRADING BOT")
    logging.info("  NO REAL TRADES WILL BE EXECUTED")
    logging.info("=" * 62)
    logging.info("  Poll interval   : %ds", CHECK_INTERVAL)
    logging.info("  Min margin      : %.2f%%", MIN_PROFIT_MARGIN * 100)
    logging.info("  Ledger file     : %s", LEDGER_FILE)
    logging.info("  Events tracked  : %d", len(EVENTS))
    for ev in EVENTS:
        logging.info("    • %s", ev["name"])
    logging.info("=" * 62)


# ====================================================================
# Main loop
# ====================================================================

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not EVENTS:
        logging.error("No events configured. Add at least one entry to the EVENTS list.")
        sys.exit(1)

    fetcher = MarketFetcher()
    engine = ArbEngine(min_margin=MIN_PROFIT_MARGIN)
    trader = PaperTrader(path=LEDGER_FILE)

    _print_banner()

    cycle = 0
    total_opps = 0

    try:
        while True:
            cycle += 1
            logging.info("─── Cycle %d ───", cycle)

            for event_cfg in EVENTS:
                name = event_cfg["name"]
                ticker = event_cfg["kalshi_ticker"]
                cond_id = event_cfg["poly_condition_id"]

                # ---- Fetch both sides ----
                kalshi_snap = fetcher.fetch_kalshi(ticker)
                poly_snap = fetcher.fetch_polymarket(cond_id)

                if kalshi_snap is None or poly_snap is None:
                    logging.warning(
                        "[%s] Skipped — could not fetch data from one or both platforms.", name
                    )
                    continue

                # ---- Log current top-of-book ----
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

                # ---- Check arbitrage ----
                opps = engine.check(name, kalshi_snap, poly_snap)

                if opps:
                    for opp in opps:
                        total_opps += 1
                        logging.info(
                            "*** OPPORTUNITY FOUND! [%s] %s │ "
                            "Spread: %.2f%% │ Cost: $%.4f │ Profit: $%.4f ***",
                            opp.event_name,
                            opp.direction,
                            opp.margin * 100,
                            opp.total_cost,
                            opp.theoretical_profit,
                        )
                        trader.log(opp)
                else:
                    logging.info("[%s] No arb this cycle.", name)

            logging.info(
                "Cycle %d complete — %d total opportunities logged. "
                "Sleeping %ds…",
                cycle, total_opps, CHECK_INTERVAL,
            )
            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        logging.info("")
        logging.info("Shutting down gracefully (Ctrl+C). %d opportunities logged.", total_opps)
        sys.exit(0)


if __name__ == "__main__":
    main()
