"""Shared fixtures and factories for the arb_bot test suite.

arb_bot reads its config from the environment at import time (via .env),
so the session fixture below pins every constant the tests depend on to
canonical values — results must not depend on a developer's local .env.

Constructor defaults in arb_bot are bound at import time, so builders here
always pass config explicitly instead of relying on defaults.
"""
import sys
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import arb_bot as ab  # noqa: E402

CANONICAL = {
    "KALSHI_FEE_RATE": D("0.07"),
    "POLY_FEE_RATE": D("0.25"),
    "POLY_FEE_EXPONENT": 2,
    "MIN_PROFIT_MARGIN": D("0.05"),
    "UNWIND_MIN_PRICE_RATIO": D("0.50"),
    "UNWIND_ABSOLUTE_MIN_PRICE": D("0.05"),
    "MAX_ORDERS_IN_MEMORY": 1000,
    "KALSHI_FILL_POLL_MS": 250,
    "MAX_TRADES_PER_EVENT": 0,
    "POLY_SIGNATURE_TYPE": 0,
}


@pytest.fixture(scope="session", autouse=True)
def canonical_config():
    """Pin module-level config so tests don't depend on the local .env."""
    for key, val in CANONICAL.items():
        setattr(ab, key, val)
    yield


def build_stack(
    tmp_path,
    *,
    live_mode=False,
    kalshi_client=None,
    poly_client=None,
    kalshi_ws=None,
    fetcher=None,
    starting_capital=D("1000"),
    position_size=D("50"),
    max_exposure=D("500"),
    max_positions=10,
    max_per_minute=1000,
    unwind_timeout=1,
    max_trades_per_event=0,
    min_fill_margin=D("0.05"),
):
    """Assemble the full pipeline with CSV output under tmp_path."""
    portfolio = ab.PaperPortfolio(
        starting_capital=starting_capital,
        position_size=position_size,
        path=str(tmp_path / "portfolio.csv"),
    )
    trader = ab.PaperTrader(path=str(tmp_path / "ledger.csv"))
    pnl = ab.DailyPnLTracker(daily_limit=D("100"))
    rate = ab.OrderRateLimiter(max_per_minute=max_per_minute)
    risk = ab.RiskManager(
        pnl_tracker=pnl,
        rate_limiter=rate,
        portfolio=portfolio,
        max_exposure=max_exposure,
        max_positions=max_positions,
        max_spread=D("0.20"),
        kill_file=str(tmp_path / "KILL"),
    )
    um = ab.UnwindManager(
        portfolio=portfolio,
        pnl_tracker=pnl,
        timeout=unwind_timeout,
        logger=ab.UnwindLogger(path=str(tmp_path / "unwinds.csv")),
        live_mode=live_mode,
        kalshi_client=kalshi_client,
        poly_client=poly_client,
        kalshi_ws=kalshi_ws,
        fetcher=fetcher,
        min_fill_margin=min_fill_margin,
    )
    ex = ab.ExecutionEngine(
        portfolio=portfolio,
        trader=trader,
        order_logger=ab.OrderLogger(path=str(tmp_path / "orders.csv")),
        risk_manager=risk,
        unwind_manager=um,
        max_trades_per_event=max_trades_per_event,
        min_fill_margin=min_fill_margin,
    )
    return SimpleNamespace(
        portfolio=portfolio, trader=trader, pnl=pnl, rate=rate,
        risk=risk, um=um, ex=ex,
    )


@pytest.fixture
def stack(tmp_path):
    """Default paper-mode pipeline."""
    return build_stack(tmp_path)


def make_opp(
    name,
    k_price="0.40",
    p_price="0.50",
    k_depth=7,
    p_depth=9,
    direction="Buy YES@Kalshi + Buy NO@Polymarket",
    **overrides,
):
    """Hand-craft an ArbOpportunity with consistent derived fields."""
    k, p = D(k_price), D(p_price)
    cost = (k + p).quantize(ab.Q4)
    k_fee = ab.kalshi_taker_fee(k, 1)
    p_fee = ab.poly_taker_fee(p, 1)
    fields = dict(
        timestamp=ab._utc_iso(),
        event_name=name,
        direction=direction,
        kalshi_price=k,
        poly_price=p,
        total_cost=cost,
        kalshi_fee=k_fee,
        poly_fee=p_fee,
        gross_profit=(ab.ONE - cost).quantize(ab.Q4),
        net_profit=(ab.ONE - cost - k_fee - p_fee).quantize(ab.Q4),
        max_contracts=min(k_depth, p_depth),
        kalshi_depth=k_depth,
        poly_depth=p_depth,
        kalshi_ticker="TEST-TICKER",
        poly_condition_id="0xtest",
    )
    fields.update(overrides)
    return ab.ArbOpportunity(**fields)


def tob(bid=None, ask=None, bid_size="10", ask_size="10"):
    """Build a TopOfBook with single-level depth."""
    t = ab.TopOfBook()
    if bid is not None:
        t.best_bid = D(bid)
        t.best_bid_size = D(bid_size)
        t.bid_levels = [ab.OrderbookLevel(price=D(bid), size=D(bid_size))]
    if ask is not None:
        t.best_ask = D(ask)
        t.best_ask_size = D(ask_size)
        t.ask_levels = [ab.OrderbookLevel(price=D(ask), size=D(ask_size))]
    return t


def snap(yes=None, no=None, source="kalshi", price_source="book"):
    """Build a MarketSnapshot from TopOfBook kwargs dicts."""
    return ab.MarketSnapshot(
        yes=tob(**(yes or {})),
        no=tob(**(no or {})),
        source=source,
        price_source=price_source,
    )


def order_outlay(opp, contracts):
    """Exact order-level cost the engine should compute."""
    return (
        opp.total_cost * D(contracts)
        + ab.kalshi_taker_fee(opp.kalshi_price, contracts)
        + ab.poly_taker_fee(opp.poly_price, contracts)
    ).quantize(ab.Q4)
