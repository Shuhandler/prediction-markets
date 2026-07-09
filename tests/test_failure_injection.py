"""Live-mode failure injection: the code paths that only run when the
exchange misbehaves — timeouts, delayed FOKs, partial IOC fills, HTTP
errors.  These paths are the highest-risk code in the bot and must be
exercised somewhere other than production."""
import asyncio
from decimal import Decimal as D

import arb_bot as ab
from conftest import build_stack, make_opp, snap
from mocks import (
    FakeResponse,
    FakeSession,
    MockFetcher,
    MockKalshiOrderClient,
    MockPolyOrderClient,
)


def kalshi_leg(contracts=5, side="yes", price="0.40"):
    return ab.LegOrder(
        leg_id="L", platform="kalshi", side=side,
        price=D(price), contracts=contracts, market_id="TICK",
    )


# ── Kalshi POST timeout → client_order_id reconciliation ─────────────

async def test_timeout_reconciliation_finds_filled_order(tmp_path):
    kalshi = MockKalshiOrderClient()
    kalshi.place_responses = [{"error": True, "detail": "TimeoutError()"}]
    stack = build_stack(tmp_path, live_mode=True, kalshi_client=kalshi)

    async def scripted_orders(**params):
        kalshi.orders_calls.append(params)
        cid = kalshi.place_calls[0]["client_order_id"]
        if params.get("status") == "executed":
            return [{"client_order_id": cid, "order_id": "K7",
                     "remaining_count": 0}]
        return []

    kalshi.get_orders = scripted_orders

    leg = kalshi_leg(contracts=5)
    ok = await stack.um._submit_kalshi_buy(leg)
    assert ok
    assert leg.status == ab.LegStatus.FILLED
    assert leg.filled_contracts == 5
    assert leg.exchange_order_id == "K7"


async def test_timeout_reconciliation_finds_nothing(tmp_path):
    kalshi = MockKalshiOrderClient()
    kalshi.place_responses = [{"error": True, "detail": "TimeoutError()"}]
    kalshi.orders_responses = [[], []]
    stack = build_stack(tmp_path, live_mode=True, kalshi_client=kalshi)

    leg = kalshi_leg()
    ok = await stack.um._submit_kalshi_buy(leg)
    assert not ok
    assert leg.status == ab.LegStatus.FAILED
    assert len(kalshi.orders_calls) == 2  # executed + canceled queries


# ── Kalshi IOC partial fill ──────────────────────────────────────────

async def test_kalshi_ioc_partial_fill_reported(tmp_path):
    kalshi = MockKalshiOrderClient()
    kalshi.place_responses = [
        {"order_id": "K1", "status": "canceled", "remaining_count": 2},
    ]
    stack = build_stack(tmp_path, live_mode=True, kalshi_client=kalshi)

    leg = kalshi_leg(contracts=5)
    ok = await stack.um._submit_kalshi_buy(leg)
    assert ok
    assert leg.status == ab.LegStatus.PARTIAL
    assert leg.filled_contracts == 3


# ── Delayed Poly FOK ─────────────────────────────────────────────────

def poly_leg(contracts=5):
    return ab.LegOrder(
        leg_id="P", platform="polymarket", side="no",
        price=D("0.50"), contracts=contracts, market_id="0xcond",
    )


async def test_delayed_fok_matched_during_poll(tmp_path):
    poly = MockPolyOrderClient()
    poly.place_responses = [{"orderID": "P1", "status": "delayed"}]
    poly.get_responses = [{"status": "delayed"}, {"status": "matched"}]
    stack = build_stack(tmp_path, live_mode=True, poly_client=poly)

    leg = poly_leg()
    ok = await stack.um._submit_poly_buy(leg)
    assert ok and leg.status == ab.LegStatus.FILLED
    assert poly.cancel_calls == []


async def test_delayed_fok_cancelled_on_exhaustion(tmp_path):
    poly = MockPolyOrderClient()
    poly.place_responses = [{"orderID": "P1", "status": "delayed"}]
    poly.get_responses = [{"status": "delayed"}]  # steady state: never matches
    stack = build_stack(tmp_path, live_mode=True, poly_client=poly,
                        unwind_timeout=1)

    leg = poly_leg()
    ok = await stack.um._submit_poly_buy(leg)
    assert not ok
    assert leg.status == ab.LegStatus.FAILED
    # The order must be cancelled, not abandoned live on the exchange
    assert poly.cancel_calls == ["P1"]


async def test_delayed_fok_matched_while_cancelling(tmp_path):
    poly = MockPolyOrderClient()
    poly.place_responses = [{"orderID": "P1", "status": "delayed"}]
    # 4 polls see "delayed"; the final post-cancel check sees "matched"
    poly.get_responses = [
        {"status": "delayed"}, {"status": "delayed"},
        {"status": "delayed"}, {"status": "delayed"},
        {"status": "matched"},
    ]
    stack = build_stack(tmp_path, live_mode=True, poly_client=poly,
                        unwind_timeout=1)

    leg = poly_leg()
    ok = await stack.um._submit_poly_buy(leg)
    assert ok and leg.status == ab.LegStatus.FILLED
    assert poly.cancel_calls == ["P1"]


# ── Full live pipeline: Leg 2 partial → excess unwound via smart FOK ─

async def test_live_partial_pairing_and_unwind(tmp_path):
    poly = MockPolyOrderClient()
    poly.place_responses = [
        {"orderID": "P1", "status": "matched"},   # leg 1 buy: 5 filled
        {"orderID": "P2", "status": "matched"},   # unwind sell: 2 sold
    ]
    kalshi = MockKalshiOrderClient()
    kalshi.place_responses = [
        {"order_id": "K1", "status": "canceled", "remaining_count": 2},  # 3/5
    ]
    fetcher = MockFetcher(
        kalshi_snapshot=snap(yes={"ask": "0.40", "ask_size": "50"}),
        poly_bids=[{"price": "0.50", "size": "100"}],
    )
    stack = build_stack(
        tmp_path, live_mode=True,
        kalshi_client=kalshi, poly_client=poly, fetcher=fetcher,
    )

    opp = make_opp("evt-live-part", k_depth=5, p_depth=5)
    uw = await stack.um.execute(opp, 5)

    assert uw.status == ab.UnwindStatus.COMPLETE
    assert uw.total_contracts == 3       # paired position
    assert uw.unwind_leg.contracts == 2  # excess sold back
    sell_call = poly.place_calls[-1]
    assert sell_call["side"] == "sell"
    assert sell_call["size"] == 2.0
    # Slippage-protected floor: max(0.50 * 0.50, 0.05) = 0.25
    assert sell_call["price"] == 0.25
    assert stack.portfolio.total_unwind_losses > ab.ZERO


# ── Smart-sized FOK: only bids at/above the floor count ──────────────

async def test_unwind_sizing_ignores_bids_below_floor(tmp_path):
    poly = MockPolyOrderClient()
    poly.place_responses = [{"orderID": "P9", "status": "matched"}]
    # 100 contracts bid at 0.02 (below the 0.25 floor), only 3 at 0.30
    fetcher = MockFetcher(poly_bids=[
        {"price": "0.02", "size": "100"},
        {"price": "0.30", "size": "3"},
    ])
    stack = build_stack(tmp_path, live_mode=True, poly_client=poly,
                        fetcher=fetcher)

    leg = poly_leg(contracts=5)
    ok = await stack.um._submit_unwind(leg)
    assert not ok  # only 3 of 5 could be sold -> remainder still exposed
    assert leg.filled_contracts == 3
    assert poly.place_calls[0]["size"] == 3.0


async def test_unwind_fails_cleanly_when_no_fillable_bids(tmp_path):
    poly = MockPolyOrderClient()
    fetcher = MockFetcher(poly_bids=[{"price": "0.02", "size": "500"}])
    stack = build_stack(tmp_path, live_mode=True, poly_client=poly,
                        fetcher=fetcher)

    leg = poly_leg(contracts=5)
    ok = await stack.um._submit_unwind(leg)
    assert not ok
    assert leg.filled_contracts == 0
    assert poly.place_calls == []  # no doomed FOK sent


# ── HTTP retry behavior (429 / 5xx / 4xx / timeouts) ─────────────────

async def test_retry_on_429_then_success(monkeypatch):
    monkeypatch.setattr(ab, "RETRY_BASE_DELAY", 0.001)
    session = FakeSession([
        FakeResponse(429, {}, headers={"Retry-After": "0.001"}),
        FakeResponse(200, {"ok": 1}),
    ])
    result = await ab._request_with_retry(
        session, "http://x/y", ab.RateLimiter(10_000), label="t",
    )
    assert result == {"ok": 1}
    assert len(session.calls) == 2


async def test_retry_on_5xx_then_success(monkeypatch):
    monkeypatch.setattr(ab, "RETRY_BASE_DELAY", 0.001)
    session = FakeSession([
        FakeResponse(503, {}),
        FakeResponse(200, {"ok": 2}),
    ])
    result = await ab._request_with_retry(
        session, "http://x/y", ab.RateLimiter(10_000), label="t",
    )
    assert result == {"ok": 2}


async def test_no_retry_on_4xx(monkeypatch):
    monkeypatch.setattr(ab, "RETRY_BASE_DELAY", 0.001)
    session = FakeSession([FakeResponse(404, {})])
    result = await ab._request_with_retry(
        session, "http://x/y", ab.RateLimiter(10_000), label="t",
    )
    assert result is None
    assert len(session.calls) == 1


async def test_timeouts_exhaust_retries(monkeypatch):
    monkeypatch.setattr(ab, "RETRY_BASE_DELAY", 0.001)
    session = FakeSession([
        asyncio.TimeoutError(),
        asyncio.TimeoutError(),
        asyncio.TimeoutError(),
    ])
    result = await ab._request_with_retry(
        session, "http://x/y", ab.RateLimiter(10_000), label="t",
    )
    assert result is None
    assert len(session.calls) == ab.MAX_RETRIES
