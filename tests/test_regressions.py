"""Regression tests for the 2026-07 fix batch.  Each test pins a bug that
existed in production code: if it fails, a fixed bug has come back."""
import asyncio
from decimal import Decimal as D

import arb_bot as ab
from conftest import build_stack, make_opp, snap
from mocks import MockFetcher, MockKalshiOrderClient, MockPolyOrderClient


# ── Fill-waiter race: fills can beat the REST response ───────────────

async def test_fill_before_registration_resolves_immediately():
    ws = ab.KalshiWS()
    ws._handle_message({"type": "fill", "msg": {"order_id": "o1", "count": 3}})
    ws._handle_message({"type": "fill", "msg": {"order_id": "o1", "count": 2}})
    fut = ws.register_fill_waiter("o1")
    assert fut.done()
    assert fut.result()["filled_count"] == 5  # accumulated, not reset to 0
    ws.cancel_fill_waiter("o1")


async def test_fill_after_registration_still_works():
    ws = ab.KalshiWS()
    fut = ws.register_fill_waiter("o2")
    assert not fut.done()
    ws._handle_message({"type": "fill", "msg": {"order_id": "o2", "count": 4}})
    assert fut.done() and fut.result()["filled_count"] == 4
    ws.cancel_fill_waiter("o2")


# ── Kalshi sell price: select by side, not `yes_price or no_price` ───

async def test_sell_kalshi_uses_no_price_for_no_side(tmp_path):
    kalshi = MockKalshiOrderClient()
    # Kalshi order objects carry BOTH prices as complements.  The old
    # `yes_price or no_price` picked 0.72 here, booking a phantom gain.
    kalshi.place_responses = [
        {"order_id": "K1", "remaining_count": 0, "yes_price": 72, "no_price": 28},
    ]
    stack = build_stack(tmp_path, live_mode=True, kalshi_client=kalshi)

    leg = ab.LegOrder(
        leg_id="L", platform="kalshi", side="no",
        price=D("0.55"), contracts=5, market_id="TICK",
    )
    ok = await stack.um._sell_kalshi(leg)
    assert ok
    assert leg.price == D("0.28")  # floor price for the NO side, not 0.72
    # And the floor itself: max(0.55 * 0.50, 0.05) -> 0.28 cents sent
    assert kalshi.place_calls[0]["price_cents"] == 28


# ── Iterative reconnect: bounded attempts, no recursion ──────────────

async def test_reconnect_loops_until_success(monkeypatch):
    ws = ab.PolymarketWS()
    outcomes = [False, False, True]
    delays = []

    async def fake_connect():
        return outcomes.pop(0)

    async def fake_sleep(seconds):
        delays.append(seconds)

    ws._do_connect = fake_connect
    monkeypatch.setattr(ab.asyncio, "sleep", fake_sleep)
    await ws._schedule_reconnect()
    assert ws._reconnect_attempts == 3
    assert delays == [1.0, 2.0, 4.0]  # exponential backoff


async def test_reconnect_delay_caps_at_max(monkeypatch):
    ws = ab.KalshiWS()
    ws._reconnect_attempts = 20
    delays = []

    async def fake_connect():
        return True

    async def fake_sleep(seconds):
        delays.append(seconds)

    ws._do_connect = fake_connect
    monkeypatch.setattr(ab.asyncio, "sleep", fake_sleep)
    await ws._schedule_reconnect()
    assert delays == [ab.WS_RECONNECT_MAX]


# ── Spread re-verification after Leg 1 (live mode) ───────────────────

def _live_stack(tmp_path, fresh_yes_ask):
    poly = MockPolyOrderClient()
    poly.place_responses = [{"orderID": "P1", "status": "matched"}]
    kalshi = MockKalshiOrderClient()
    kalshi.place_responses = [
        {"order_id": "K1", "status": "executed", "remaining_count": 0},
    ]
    fetcher = MockFetcher(
        kalshi_snapshot=snap(yes={"ask": fresh_yes_ask, "ask_size": "50"}),
        poly_bids=[{"price": "0.50", "size": "100"}],
    )
    stack = build_stack(
        tmp_path, live_mode=True,
        kalshi_client=kalshi, poly_client=poly, fetcher=fetcher,
    )
    return stack, kalshi, poly


async def test_spread_evaporated_unwinds_without_leg2(tmp_path):
    # Fresh Kalshi ask 0.70: cost 0.50 + 0.70 = 1.20 -> net < 0 -> unwind
    stack, kalshi, poly = _live_stack(tmp_path, "0.70")
    poly.place_responses.append({"orderID": "P2", "status": "matched"})  # sell
    uw = await stack.um.execute(make_opp("evt-gone", k_depth=5, p_depth=5), 5)
    assert uw.status == ab.UnwindStatus.UNWOUND
    assert kalshi.place_calls == []  # Leg 2 never submitted
    sell = poly.place_calls[-1]
    assert sell["side"] == "sell"


async def test_spread_still_good_updates_leg2_price(tmp_path):
    # Fresh ask 0.41 (was 0.40): still profitable, Leg 2 repriced
    stack, kalshi, poly = _live_stack(tmp_path, "0.41")
    uw = await stack.um.execute(make_opp("evt-ok", k_depth=5, p_depth=5), 5)
    assert uw.status == ab.UnwindStatus.COMPLETE
    assert kalshi.place_calls[0]["price_cents"] == 41
    fresh_net = (
        ab.ONE - D("0.91")
        - ab.kalshi_taker_fee(D("0.41"), 1)
        - ab.poly_taker_fee(D("0.50"), 1)
    ).quantize(ab.Q4)
    assert uw.net_profit_per_contract == fresh_net


# ── /book downgrade: transient errors must not be permanent ──────────

async def test_book_hard_404_backs_off_five_minutes():
    fetcher = ab.MarketFetcher()
    fetcher._poly_token_cache["cid"] = ("Y", "N")
    calls = {"book": 0, "price": 0}

    async def fake_book(token_id):
        calls["book"] += 1
        return None, True  # hard 404

    async def fake_price(token_id, side):
        calls["price"] += 1
        return D("0.50")

    fetcher._fetch_poly_book = fake_book
    fetcher._fetch_poly_price = fake_price

    first = await fetcher.fetch_polymarket("cid")
    assert first.price_source == "indicative"
    assert calls["book"] == 2  # both tokens probed once

    second = await fetcher.fetch_polymarket("cid")
    assert second.price_source == "indicative"
    assert calls["book"] == 2  # inside the 5-minute back-off window


async def test_book_transient_failure_reprobes_next_cycle():
    fetcher = ab.MarketFetcher()
    fetcher._poly_token_cache["cid"] = ("Y", "N")
    calls = {"book": 0}

    async def flaky_book(token_id):
        calls["book"] += 1
        return None, False  # timeout / 5xx

    async def fake_price(token_id, side):
        return D("0.50")

    fetcher._fetch_poly_book = flaky_book
    fetcher._fetch_poly_price = fake_price

    await fetcher.fetch_polymarket("cid")
    assert calls["book"] == 2
    await fetcher.fetch_polymarket("cid")
    assert calls["book"] == 4  # re-probed, not permanently downgraded


async def test_book_recovers_after_transient_failure():
    fetcher = ab.MarketFetcher()
    fetcher._poly_token_cache["cid"] = ("Y", "N")
    good_book = {
        "bids": [{"price": "0.45", "size": "10"}],
        "asks": [{"price": "0.55", "size": "10"}],
    }
    responses = [(None, False), (None, False), (good_book, False), (good_book, False)]

    async def scripted_book(token_id):
        return responses.pop(0)

    async def fake_price(token_id, side):
        return D("0.50")

    fetcher._fetch_poly_book = scripted_book
    fetcher._fetch_poly_price = fake_price

    first = await fetcher.fetch_polymarket("cid")
    assert first.price_source == "indicative"
    second = await fetcher.fetch_polymarket("cid")
    assert second.price_source == "book"
    assert second.yes.best_bid == D("0.45")
