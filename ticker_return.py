#!/usr/bin/env python3
"""
Event Config Generator for arb_bot.py

Takes Kalshi and Polymarket URLs for the same event and outputs
the EVENTS dict entry needed for arb_bot.py.

Usage:
    python ticker_return.py
    python ticker_return.py <kalshi_url> <polymarket_url>

Dependencies:
    pip install requests
"""

import sys
import requests
from urllib.parse import urlparse

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
TIMEOUT = 15


# ====================================================================
# HTTP helper
# ====================================================================

def get_json(url, params=None):
    """GET *url*, return parsed JSON or None on any failure."""
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT,
                            headers={"Accept": "application/json"})
        if resp.status_code >= 400:
            return None
        return resp.json()
    except Exception:
        return None


# ====================================================================
# Kalshi resolution
# ====================================================================

def resolve_kalshi(url):
    """
    Parse a Kalshi URL and return (event_ticker, [market_dicts]).

    URL structure: /markets/{series_ticker}/{slug}/{event_ticker}
    Example:       /markets/kxncaawbgame/college-basketball-womens-game/kxncaawbgame-26feb11forslu

    The last segment is the EVENT ticker (not a market ticker).
    Individual market tickers have a team suffix, e.g. …-FOR, …-SLU.
    """
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    raw = parts[-1].upper() if parts else ""

    if not raw:
        return raw, []

    # 1. Try as event_ticker — returns only markets for this specific game
    data = get_json(
        f"{KALSHI_BASE}/markets",
        params={"event_ticker": raw, "limit": 20},
    )
    markets = (data or {}).get("markets", [])
    if markets:
        return raw, markets

    # 2. Maybe the URL already had a full market ticker (with team suffix)
    data = get_json(f"{KALSHI_BASE}/markets/{raw}")
    if data and "market" in data:
        market = data["market"]
        event_ticker = market.get("event_ticker", raw)
        # Fetch sibling markets for the same event
        data2 = get_json(
            f"{KALSHI_BASE}/markets",
            params={"event_ticker": event_ticker, "limit": 20},
        )
        siblings = (data2 or {}).get("markets", [])
        return event_ticker, siblings if siblings else [market]

    return raw, []


# ====================================================================
# Polymarket resolution
# ====================================================================

def resolve_polymarket(url):
    """
    Parse a Polymarket URL and return a list of market dicts.

    Tries multiple Gamma API strategies:
      1. GET /events?slug={slug}
      2. GET /events/slug/{slug}
      3. Text search via GET /events?_q=…
    """
    path = urlparse(url).path.strip("/")
    segments = [s for s in path.split("/") if s]
    slug = segments[-1] if segments else ""

    if not slug:
        return slug, []

    # Strategy 1: filter events by slug query param
    data = get_json(f"{GAMMA_BASE}/events", params={"slug": slug})
    if data and isinstance(data, list) and data:
        markets = data[0].get("markets", [])
        if markets:
            return slug, markets

    # Strategy 2: direct slug lookup endpoint
    data = get_json(f"{GAMMA_BASE}/events/slug/{slug}")
    if data and isinstance(data, dict):
        markets = data.get("markets", [])
        if markets:
            return slug, markets

    # Strategy 3: text search using slug keywords
    search = slug.replace("-", " ")
    data = get_json(
        f"{GAMMA_BASE}/events",
        params={"_q": search, "closed": "false", "_limit": 5},
    )
    if data and isinstance(data, list) and data:
        markets = data[0].get("markets", [])
        if markets:
            return slug, markets

    return slug, []


def verify_clob_outcomes(condition_id):
    """Fetch token outcome names from the CLOB API."""
    data = get_json(f"{CLOB_BASE}/markets/{condition_id}")
    if data:
        return data.get("tokens", [])
    return []


# ====================================================================
# Interactive helpers
# ====================================================================

def prompt_choice(prompt, max_val):
    """Prompt the user for an integer in [1, max_val]."""
    while True:
        raw = input(prompt).strip()
        try:
            val = int(raw)
            if 1 <= val <= max_val:
                return val
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {max_val}.")


# ====================================================================
# Main
# ====================================================================

def main():
    print()
    print("=" * 62)
    print("  Event Config Generator for arb_bot.py")
    print("=" * 62)
    print()

    # ── Get URLs from args or interactive input ──
    if len(sys.argv) >= 3:
        k_url = sys.argv[1]
        p_url = sys.argv[2]
        print(f"  Kalshi URL:      {k_url}")
        print(f"  Polymarket URL:  {p_url}")
        print()
    else:
        k_url = input("  Kalshi URL: ").strip()
        p_url = input("  Polymarket URL: ").strip()
        print()

    # ── Kalshi ──────────────────────────────────────────────────────
    print("  Fetching Kalshi data...")
    event_ticker, k_markets = resolve_kalshi(k_url)

    if not k_markets:
        print(f"  ERROR: No markets found for '{event_ticker}'.")
        print("  Check the URL and try again.")
        sys.exit(1)

    print(f"  Event: {event_ticker}")
    print(f"  Found {len(k_markets)} market(s):\n")

    for i, m in enumerate(k_markets, 1):
        ticker = m.get("ticker", "?")
        title = m.get("title", m.get("subtitle", ""))
        yes_sub = m.get("yes_sub_title", "")
        no_sub = m.get("no_sub_title", "")
        print(f"    {i}. {ticker}")
        if title:
            print(f"       {title}")
        if yes_sub:
            print(f"       Yes = {yes_sub}  |  No = {no_sub}")
        print()

    # ── Polymarket ──────────────────────────────────────────────────
    print("  Fetching Polymarket data...")
    slug, p_markets = resolve_polymarket(p_url)

    condition_id = ""

    if not p_markets:
        print(f"  Could not auto-resolve slug '{slug}'.")
        condition_id = input("  Enter condition_id manually: ").strip()
    elif len(p_markets) == 1:
        m = p_markets[0]
        condition_id = m.get("conditionId", m.get("condition_id", ""))
        title = m.get("question", m.get("groupItemTitle", "?"))
        print(f"  Found: {title}")
        print(f"  Condition ID: {condition_id}")
    else:
        print(f"  Found {len(p_markets)} market(s):\n")
        for i, m in enumerate(p_markets, 1):
            title = m.get("question", m.get("groupItemTitle", "?"))
            cid = m.get("conditionId", m.get("condition_id", "?"))
            print(f"    {i}. {title}")
            print(f"       condition_id: {cid}")
        print()
        choice = prompt_choice(
            f"  Which market? [1-{len(p_markets)}]: ", len(p_markets),
        )
        selected = p_markets[choice - 1]
        condition_id = selected.get("conditionId", selected.get("condition_id", ""))

    if not condition_id:
        print("  ERROR: No condition_id obtained.")
        sys.exit(1)

    # ── Verify outcomes via CLOB ────────────────────────────────────
    print()
    print("  Verifying outcomes via CLOB...")
    tokens = verify_clob_outcomes(condition_id)

    if tokens and len(tokens) >= 2:
        print("  Token outcomes:")
        for i, t in enumerate(tokens):
            print(f"    [{i}] {t.get('outcome', '?')}")
    else:
        print("  WARNING: Could not verify token outcomes via CLOB.")
        print("  Double-check alignment manually.")

    # ── Outcome alignment ───────────────────────────────────────────
    print()
    print("  " + "-" * 58)
    print("  OUTCOME ALIGNMENT")
    print("  " + "-" * 58)
    print()
    print("  The Kalshi ticker's 'Yes' must match Polymarket's")
    print("  outcome[0] (first token) for the arb logic to work.")
    print("  If they don't match, arb_bot will be inverted")
    print("  (guaranteed LOSS instead of profit).")
    print()

    if len(k_markets) > 1:
        print("  Which Kalshi ticker has Yes = Polymarket outcome[0]?")
        print()
        for i, m in enumerate(k_markets, 1):
            ticker = m.get("ticker", "?")
            yes_sub = m.get("yes_sub_title", m.get("title", "?"))
            print(f"    {i}. {ticker}  (Yes = {yes_sub})")
        print()
        choice = prompt_choice(
            f"  Choice [1-{len(k_markets)}]: ", len(k_markets),
        )
        selected_ticker = k_markets[choice - 1].get("ticker", "")
    else:
        selected_ticker = k_markets[0].get("ticker", "")
        yes_sub = k_markets[0].get("yes_sub_title", "")
        print(f"  Only one ticker available: {selected_ticker}")
        if yes_sub:
            print(f"  (Yes = {yes_sub})")

    # ── Event name ──────────────────────────────────────────────────
    suggested = ""
    if k_markets:
        suggested = k_markets[0].get("title", k_markets[0].get("subtitle", ""))
    if not suggested:
        suggested = event_ticker

    print()
    name = input(f"  Event name [{suggested}]: ").strip() or suggested

    # ── Output ──────────────────────────────────────────────────────
    print()
    print("  " + "=" * 58)
    print("  Add this to the EVENTS list in arb_bot.py:")
    print("  " + "=" * 58)
    print()
    print("    {")
    print(f'        "name": "{name}",')
    print(f'        "kalshi_ticker": "{selected_ticker}",')
    print(f'        "poly_condition_id": "{condition_id}"')
    print("    }")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Cancelled.")
        sys.exit(0)
