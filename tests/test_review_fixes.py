"""Regression tests for the 2026-07-13 correctness-review fix batch
(CODE_REVIEW_PROGRESS.md, findings F1–F11).  Each test pins a fixed bug:
if it fails, that bug has come back."""
import asyncio
from decimal import Decimal as D

import arb_bot as ab
from conftest import build_stack, make_opp
from mocks import MockKalshiOrderClient, MockPolyOrderClient


# ── F1: Polymarket WS book snapshots — current schema uses bids/asks ─

def test_poly_snapshot_accepts_current_bids_asks_schema():
    book = ab._LocalOrderbook()
    book.apply_snapshot_poly({
        "bids": [{"price": "0.45", "size": "10"}],
        "asks": [{"price": "0.55", "size": "7"}],
    })
    assert book.bids == {D("0.45"): D("10")}
    assert book.asks == {D("0.55"): D("7")}


def test_poly_snapshot_still_accepts_legacy_buys_sells_schema():
    book = ab._LocalOrderbook()
    book.apply_snapshot_poly({
        "buys": [{"price": "0.40", "size": "3"}],
        "sells": [{"price": "0.60", "size": "4"}],
    })
    assert book.bids == {D("0.40"): D("3")}
    assert book.asks == {D("0.60"): D("4")}


def test_ws_book_event_new_schema_populates_book():
    ws = ab.PolymarketWS()
    ws._token_to_event["T"] = "evt"
    ws._handle_message({
        "event_type": "book", "asset_id": "T",
        "bids": [{"price": "0.45", "size": "10"}],
        "asks": [{"price": "0.55", "size": "7"}],
    })
    assert ws._books["T"].bids == {D("0.45"): D("10")}
    assert ws._books["T"].asks == {D("0.55"): D("7")}


def test_ws_price_change_current_batched_schema():
    ws = ab.PolymarketWS()
    ws._token_to_event["T"] = "evt"
    ws._books["T"] = ab._LocalOrderbook()
    ws._handle_message({
        "event_type": "price_change",
        "price_changes": [
            {"asset_id": "T", "price": "0.40", "size": "5", "side": "BUY"},
        ],
    })
    assert ws._books["T"].bids == {D("0.40"): D("5")}


def test_ws_price_change_legacy_top_level_asset_schema():
    ws = ab.PolymarketWS()
    ws._token_to_event["T"] = "evt"
    ws._books["T"] = ab._LocalOrderbook()
    ws._handle_message({
        "event_type": "price_change", "asset_id": "T",
        "changes": [{"price": "0.40", "size": "5", "side": "BUY"}],
    })
    assert ws._books["T"].bids == {D("0.40"): D("5")}


# ── F2: Kalshi time_in_force must be a documented enum value ─────────

async def test_kalshi_buy_sends_documented_time_in_force(tmp_path):
    kalshi = MockKalshiOrderClient()
    kalshi.place_responses = [
        {"order_id": "K1", "status": "executed", "remaining_count": 0},
    ]
    stack = build_stack(tmp_path, live_mode=True, kalshi_client=kalshi)
    leg = ab.LegOrder(
        leg_id="L", platform="kalshi", side="yes",
        price=D("0.40"), contracts=5, market_id="TICK",
    )
    assert await stack.um._submit_kalshi_buy(leg)
    assert kalshi.place_calls[0]["time_in_force"] == "immediate_or_cancel"


async def test_kalshi_sell_sends_documented_time_in_force(tmp_path):
    kalshi = MockKalshiOrderClient()
    kalshi.place_responses = [
        {"order_id": "K1", "remaining_count": 0, "yes_price": 20},
    ]
    stack = build_stack(tmp_path, live_mode=True, kalshi_client=kalshi)
    leg = ab.LegOrder(
        leg_id="L", platform="kalshi", side="yes",
        price=D("0.40"), contracts=5, market_id="TICK",
    )
    assert await stack.um._sell_kalshi(leg)
    assert kalshi.place_calls[0]["time_in_force"] == "immediate_or_cancel"


async def test_unfilled_kalshi_buy_is_defensively_cancelled(tmp_path):
    # If the exchange treats the order as resting instead of IOC, a fill
    # after we report failure is an untracked position — the no-fill path
    # must issue a cancel.
    kalshi = MockKalshiOrderClient()
    kalshi.place_responses = [
        {"order_id": "K1", "status": "resting", "remaining_count": 5},
    ]
    kalshi.get_responses = [{"order_id": "K1", "remaining_count": 5}]
    stack = build_stack(tmp_path, live_mode=True, kalshi_client=kalshi)
    leg = ab.LegOrder(
        leg_id="L", platform="kalshi", side="yes",
        price=D("0.40"), contracts=5, market_id="TICK",
    )
    assert not await stack.um._submit_kalshi_buy(leg)
    assert kalshi.cancel_calls == ["K1"]


async def test_unfilled_kalshi_sell_is_defensively_cancelled(tmp_path):
    kalshi = MockKalshiOrderClient()
    kalshi.place_responses = [
        {"order_id": "K1", "status": "resting", "remaining_count": 5},
    ]
    stack = build_stack(tmp_path, live_mode=True, kalshi_client=kalshi)
    leg = ab.LegOrder(
        leg_id="L", platform="kalshi", side="yes",
        price=D("0.40"), contracts=5, market_id="TICK",
    )
    assert not await stack.um._sell_kalshi(leg)
    assert kalshi.cancel_calls == ["K1"]


# ── F3: positions booked at EXECUTED prices, not the stale snapshot ──

async def test_position_booked_at_actual_leg2_fill_price(tmp_path):
    poly = MockPolyOrderClient()
    poly.place_responses = [{"orderID": "P1", "status": "matched"}]
    kalshi = MockKalshiOrderClient()
    # The Leg 2 IOC (ceiling 0.41) filled above the observed 0.40 ask:
    # the position must be booked at the executed VWAP, not the snapshot.
    kalshi.place_responses = [
        {"order_id": "K1", "status": "executed", "remaining_count": 0,
         "average_fill_price": "0.41"},
    ]
    stack = build_stack(
        tmp_path, live_mode=True,
        kalshi_client=kalshi, poly_client=poly,
    )

    order = await stack.ex.submit(make_opp("evt-repriced"))
    assert order.status == ab.OrderStatus.FILLED

    (pos,) = stack.portfolio.open_positions
    n = pos.contracts
    executed_cost = D("0.41") + D("0.50")
    assert pos.cost_per_contract == executed_cost
    expected_outlay = (
        executed_cost * D(n)
        + ab.kalshi_taker_fee(D("0.41"), n)
        + ab.poly_taker_fee(D("0.50"), n)
    ).quantize(ab.Q4)
    assert pos.total_outlay == expected_outlay
    assert stack.portfolio.capital == D("1000") - expected_outlay


# ── F4: partial Kalshi unwind sell must NOT report success ───────────

async def test_sell_kalshi_partial_fill_reports_failure(tmp_path):
    kalshi = MockKalshiOrderClient()
    # 3 of 5 sold; IOC cancelled the remaining 2
    kalshi.place_responses = [
        {"order_id": "K1", "remaining_count": 2, "yes_price": 20},
    ]
    stack = build_stack(tmp_path, live_mode=True, kalshi_client=kalshi)
    leg = ab.LegOrder(
        leg_id="L", platform="kalshi", side="yes",
        price=D("0.40"), contracts=5, market_id="TICK",
    )
    ok = await stack.um._sell_kalshi(leg)
    assert not ok                       # remainder must be orphaned by callers
    assert leg.filled_contracts == 3    # but what sold is still recorded
    assert leg.status == ab.LegStatus.FAILED


# ── F5: REST poll is authoritative over the first WS fill message ────

async def test_ws_fill_count_is_floor_rest_poll_authoritative(tmp_path):
    kalshi = MockKalshiOrderClient()
    kalshi.place_responses = [
        {"order_id": "K1", "status": "resting", "remaining_count": 5},
    ]
    # REST poll shows the true final state: fully filled
    kalshi.get_responses = [{"order_id": "K1", "remaining_count": 0}]

    ws = ab.KalshiWS()
    ws._fills_subscribed = True
    # First fill message arrives with only 2 of the eventual 5 —
    # the waiter resolves early with the partial count.
    ws._handle_message({"type": "fill", "msg": {"order_id": "K1", "count": 2}})

    stack = build_stack(
        tmp_path, live_mode=True, kalshi_client=kalshi, kalshi_ws=ws,
    )
    leg = ab.LegOrder(
        leg_id="L", platform="kalshi", side="yes",
        price=D("0.40"), contracts=5, market_id="TICK",
    )
    assert await stack.um._submit_kalshi_buy(leg)
    assert leg.filled_contracts == 5    # REST count, not the WS floor of 2
    assert leg.status == ab.LegStatus.FILLED


async def test_ws_fill_count_used_when_rest_poll_unavailable(tmp_path):
    kalshi = MockKalshiOrderClient()
    kalshi.place_responses = [
        {"order_id": "K1", "status": "resting", "remaining_count": 5},
    ]
    kalshi.get_responses = [{"error": True}]  # poll failed → WS floor wins

    ws = ab.KalshiWS()
    ws._fills_subscribed = True
    ws._handle_message({"type": "fill", "msg": {"order_id": "K1", "count": 2}})

    stack = build_stack(
        tmp_path, live_mode=True, kalshi_client=kalshi, kalshi_ws=ws,
    )
    leg = ab.LegOrder(
        leg_id="L", platform="kalshi", side="yes",
        price=D("0.40"), contracts=5, market_id="TICK",
    )
    assert await stack.um._submit_kalshi_buy(leg)
    assert leg.filled_contracts == 2
    assert leg.status == ab.LegStatus.PARTIAL


# ── F6: payout credited only on actual Kalshi settlement ─────────────

class FakeResolutionFetcher:
    """check_kalshi_active says 'stopped trading' immediately; settlement
    confirms only on the second poll."""

    def __init__(self, settled_responses):
        self.settled_responses = list(settled_responses)
        self.settled_calls = 0

    async def check_kalshi_active(self, ticker):
        return False

    async def check_poly_active(self, condition_id):
        return True

    async def check_kalshi_settled(self, ticker):
        self.settled_calls += 1
        return self.settled_responses.pop(0)


async def test_payout_waits_for_kalshi_settlement(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "RESOLUTION_CHECK_INTERVAL", 0.01)
    stack = build_stack(tmp_path)
    await stack.portfolio.open_position(ab.Position(
        event_name="evt", direction="d", contracts=3,
        cost_per_contract=D("0.90"), fees_per_contract=D("0.01"),
        total_outlay=D("2.73"), opened_at=ab._utc_iso(),
    ))
    capital_before = stack.portfolio.capital

    fetcher = FakeResolutionFetcher(settled_responses=[False, True])
    ev = {"name": "evt", "kalshi_ticker": "TICK", "poly_condition_id": "0xc"}
    stop = asyncio.Event()
    await asyncio.wait_for(
        ab._resolution_checker([ev], {"evt": ev}, fetcher,
                               stack.portfolio, stop),
        timeout=5,
    )

    # Two settlement polls: the first (not settled) must not have paid out
    assert fetcher.settled_calls == 2
    assert stack.portfolio.open_positions == []
    assert stack.portfolio.closed_count == 1
    assert stack.portfolio.capital == capital_before + D("3")  # $1 × 3
    assert stop.is_set()


async def test_no_positions_means_no_settlement_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "RESOLUTION_CHECK_INTERVAL", 0.01)
    stack = build_stack(tmp_path)
    fetcher = FakeResolutionFetcher(settled_responses=[])
    ev = {"name": "evt", "kalshi_ticker": "TICK", "poly_condition_id": "0xc"}
    stop = asyncio.Event()
    await asyncio.wait_for(
        ab._resolution_checker([ev], {"evt": ev}, fetcher,
                               stack.portfolio, stop),
        timeout=5,
    )
    assert fetcher.settled_calls == 0  # nothing to credit, no polling
    assert stop.is_set()


# ── F7: StateStore writes get a frozen snapshot, not the live object ─

async def test_persist_receives_frozen_meta_snapshot(tmp_path):
    store = ab.StateStore(str(tmp_path / "state.db"))
    captured = []
    original = store.persist_open

    def spy(pos, meta):
        captured.append(meta)
        original(pos, meta)

    store.persist_open = spy
    portfolio = ab.PaperPortfolio(
        starting_capital=D("100"), position_size=D("50"),
        path=str(tmp_path / "portfolio.csv"), state_store=store,
    )
    await portfolio.open_position(ab.Position(
        event_name="evt", direction="d", contracts=1,
        cost_per_contract=D("0.90"), fees_per_contract=D("0.01"),
        total_outlay=D("0.91"), opened_at=ab._utc_iso(),
    ))
    (meta,) = captured
    assert isinstance(meta, ab.PortfolioMeta)
    assert meta.capital == portfolio.capital
    store.close()


# ── F8: paired contracts surviving a failed/unwound trade are booked ─

async def test_unwound_trade_books_surviving_paired_contracts(
    tmp_path, monkeypatch,
):
    stack = build_stack(tmp_path)
    opp = make_opp("evt-pair")
    fabricated = ab.UnwindOrder(
        unwind_id="u1", event_name="evt-pair", direction=opp.direction,
        status=ab.UnwindStatus.UNWOUND,
        leg1=ab.LegOrder(
            leg_id="1", platform="polymarket", side="no",
            price=opp.poly_price, contracts=5, filled_contracts=5,
        ),
        leg2=ab.LegOrder(
            leg_id="2", platform="kalshi", side="yes",
            price=opp.kalshi_price, contracts=5, filled_contracts=2,
        ),
        net_profit_per_contract=opp.net_profit,
        total_contracts=5,
    )

    async def fake_execute(o, d):
        return fabricated

    monkeypatch.setattr(stack.um, "execute", fake_execute)
    order = await stack.ex.submit(opp)

    assert order.status == ab.OrderStatus.UNWOUND
    assert order.filled_contracts == 2       # the paired portion
    (pos,) = stack.portfolio.open_positions  # ...is real, tracked exposure
    assert pos.contracts == 2
    # The event stays locked — a retry would stack exposure on top of
    # the live paired position.
    assert ("evt-pair", opp.direction) in stack.ex._traded


async def test_fully_unwound_trade_still_releases_dedup_key(
    tmp_path, monkeypatch,
):
    stack = build_stack(tmp_path)
    opp = make_opp("evt-flat")
    fabricated = ab.UnwindOrder(
        unwind_id="u2", event_name="evt-flat", direction=opp.direction,
        status=ab.UnwindStatus.UNWOUND,
        leg1=ab.LegOrder(
            leg_id="1", platform="polymarket", side="no",
            price=opp.poly_price, contracts=5, filled_contracts=5,
        ),
        leg2=ab.LegOrder(
            leg_id="2", platform="kalshi", side="yes",
            price=opp.kalshi_price, contracts=5, filled_contracts=0,
        ),
        net_profit_per_contract=opp.net_profit,
        total_contracts=5,
    )

    async def fake_execute(o, d):
        return fabricated

    monkeypatch.setattr(stack.um, "execute", fake_execute)
    order = await stack.ex.submit(opp)

    assert order.status == ab.OrderStatus.UNWOUND
    assert order.filled_contracts == 0
    assert stack.portfolio.open_positions == []
    assert ("evt-flat", opp.direction) not in stack.ex._traded


# ── F10: unwind-loss CSV pnl is sign-correct for any loss value ──────

async def test_unwind_loss_pnl_sign_is_negation_not_prefix(tmp_path):
    path = tmp_path / "portfolio.csv"
    portfolio = ab.PaperPortfolio(
        starting_capital=D("100"), position_size=D("50"), path=str(path),
    )
    await portfolio.record_unwind_loss("evt", "d", 1, D("-0.05"))
    last = path.read_text().strip().splitlines()[-1]
    assert "--" not in last          # old code produced "--0.0500"
    assert "0.0500" in last


# ── F11: force-unwinding a zero-fill order is FAILED, not COMPLETE ───

async def test_force_unwind_nothing_filled_is_failed(tmp_path):
    stack = build_stack(tmp_path)
    uw = ab.UnwindOrder(
        unwind_id="u3", event_name="evt", direction="d",
        status=ab.UnwindStatus.LEG1_SUBMITTED,
        leg1=ab.LegOrder(
            leg_id="1", platform="polymarket", side="no",
            price=D("0.50"), contracts=5,
        ),
        created_at=ab._utc_iso(),
    )
    stack.um._active[uw.unwind_id] = uw
    await stack.um.force_unwind(uw)
    assert uw.status == ab.UnwindStatus.FAILED
    assert stack.um.complete_count == 0


async def test_force_unwind_fully_paired_is_complete(tmp_path):
    stack = build_stack(tmp_path)
    uw = ab.UnwindOrder(
        unwind_id="u4", event_name="evt", direction="d",
        status=ab.UnwindStatus.LEG2_SUBMITTED,
        leg1=ab.LegOrder(
            leg_id="1", platform="polymarket", side="no",
            price=D("0.50"), contracts=3, filled_contracts=3,
            status=ab.LegStatus.FILLED,
        ),
        leg2=ab.LegOrder(
            leg_id="2", platform="kalshi", side="yes",
            price=D("0.40"), contracts=3, filled_contracts=3,
            status=ab.LegStatus.FILLED,
        ),
        created_at=ab._utc_iso(),
    )
    stack.um._active[uw.unwind_id] = uw
    await stack.um.force_unwind(uw)
    assert uw.status == ab.UnwindStatus.COMPLETE


# ═════════════════════════════════════════════════════════════════════
# Post-review repairs (2026-07-14 code review of the fix batch)
# ═════════════════════════════════════════════════════════════════════

# ── S2: live fill counter must be re-read when the REST poll fails ───

async def test_live_fill_counter_read_when_poll_fails(tmp_path):
    ws = ab.KalshiWS()
    ws._fills_subscribed = True
    # First fill message (2 of the eventual 5) resolves the waiter early
    ws._handle_message({"type": "fill", "msg": {"order_id": "K1", "count": 2}})

    class LateFillKalshi(MockKalshiOrderClient):
        async def get_order(self, order_id):
            # a further fill lands while we poll — and the poll fails
            ws._handle_message(
                {"type": "fill", "msg": {"order_id": order_id, "count": 3}},
            )
            return {"error": True}

    kalshi = LateFillKalshi()
    kalshi.place_responses = [
        {"order_id": "K1", "status": "resting", "remaining_count": 5},
    ]
    stack = build_stack(
        tmp_path, live_mode=True, kalshi_client=kalshi, kalshi_ws=ws,
    )
    leg = ab.LegOrder(
        leg_id="L", platform="kalshi", side="yes",
        price=D("0.40"), contracts=5, market_id="TICK",
    )
    assert await stack.um._submit_kalshi_buy(leg)
    assert leg.filled_contracts == 5      # freshest count, not the first 2
    assert leg.status == ab.LegStatus.FILLED


# ── C8: fills revealed by the cancel response are absorbed ───────────

async def test_cancel_response_fills_absorbed_on_no_fill_path(tmp_path):
    kalshi = MockKalshiOrderClient()
    kalshi.place_responses = [
        {"order_id": "K1", "status": "resting", "remaining_count": 5},
    ]
    kalshi.get_responses = [
        {"order_id": "K1", "status": "resting", "remaining_count": 5},
    ]
    # Order filled 3 contracts between the poll and the cancel landing
    kalshi.cancel_responses = [{"order_id": "K1", "remaining_count": 2}]
    stack = build_stack(tmp_path, live_mode=True, kalshi_client=kalshi)
    leg = ab.LegOrder(
        leg_id="L", platform="kalshi", side="yes",
        price=D("0.40"), contracts=5, market_id="TICK",
    )
    assert await stack.um._submit_kalshi_buy(leg)
    assert leg.filled_contracts == 3
    assert leg.status == ab.LegStatus.PARTIAL
    assert kalshi.cancel_calls == ["K1"]


# ── C2: partial fills on non-terminal orders cancel the remainder ────

async def test_sell_partial_non_terminal_cancels_remainder(tmp_path):
    kalshi = MockKalshiOrderClient()
    kalshi.place_responses = [
        {"order_id": "K1", "status": "resting", "remaining_count": 2,
         "yes_price": 20},
    ]
    kalshi.cancel_responses = [{"order_id": "K1", "remaining_count": 2}]
    stack = build_stack(tmp_path, live_mode=True, kalshi_client=kalshi)
    leg = ab.LegOrder(
        leg_id="L", platform="kalshi", side="yes",
        price=D("0.40"), contracts=5, market_id="TICK",
    )
    ok = await stack.um._sell_kalshi(leg)
    assert not ok                         # still partial: 3 of 5 sold
    assert leg.filled_contracts == 3
    assert kalshi.cancel_calls == ["K1"]  # resting remainder cancelled


async def test_sell_partial_terminal_status_skips_cancel(tmp_path):
    kalshi = MockKalshiOrderClient()
    kalshi.place_responses = [
        {"order_id": "K1", "status": "canceled", "remaining_count": 2,
         "yes_price": 20},
    ]
    stack = build_stack(tmp_path, live_mode=True, kalshi_client=kalshi)
    leg = ab.LegOrder(
        leg_id="L", platform="kalshi", side="yes",
        price=D("0.40"), contracts=5, market_id="TICK",
    )
    ok = await stack.um._sell_kalshi(leg)
    assert not ok and leg.filled_contracts == 3
    assert kalshi.cancel_calls == []      # genuine IOC: no wasted call


# ── S1: ambiguous POST errors reconcile; clean 4xx rejects don't ─────

async def test_ambiguous_place_error_triggers_reconciliation(tmp_path):
    kalshi = MockKalshiOrderClient()
    # Garbled body (gateway HTML): no status_code in the error dict
    kalshi.place_responses = [
        {"error": True, "detail": "Expecting value: line 1 column 1"},
    ]
    stack = build_stack(tmp_path, live_mode=True, kalshi_client=kalshi)
    leg = ab.LegOrder(
        leg_id="L", platform="kalshi", side="yes",
        price=D("0.40"), contracts=5, market_id="TICK",
    )
    assert not await stack.um._submit_kalshi_buy(leg)
    assert kalshi.orders_calls            # reconciliation attempted


async def test_clean_400_reject_skips_reconciliation(tmp_path):
    kalshi = MockKalshiOrderClient()
    kalshi.place_responses = [
        {"error": True, "status_code": 400, "detail": "insufficient balance"},
    ]
    stack = build_stack(tmp_path, live_mode=True, kalshi_client=kalshi)
    leg = ab.LegOrder(
        leg_id="L", platform="kalshi", side="yes",
        price=D("0.40"), contracts=5, market_id="TICK",
    )
    assert not await stack.um._submit_kalshi_buy(leg)
    assert kalshi.orders_calls == []      # provably never placed


# ── C11: malformed price_change payloads must not tear down the WS ───

def test_malformed_price_change_entries_do_not_crash():
    ws = ab.PolymarketWS()
    ws._token_to_event["T"] = "evt"
    ws._books["T"] = ab._LocalOrderbook()
    ws._handle_message({"event_type": "price_change", "price_changes": None})
    ws._handle_message({
        "event_type": "price_change",
        "price_changes": [None, "junk", {"asset_id": "T"}],
    })
    ws._handle_message({
        "event_type": "price_change",
        "price_changes": [
            {"asset_id": "T", "price": "0.40", "size": "5", "side": "BUY"},
        ],
    })
    assert ws._books["T"].bids == {D("0.40"): D("5")}


# ── C13: failures after booking must not release the dedup key ───────

async def test_post_booking_failure_keeps_dedup_key(tmp_path, monkeypatch):
    stack = build_stack(tmp_path)

    def boom(net, contracts):
        raise RuntimeError("tracker exploded")

    monkeypatch.setattr(stack.pnl, "record", boom)
    opp = make_opp("evt-boom")
    order = await stack.ex.submit(opp)

    assert order.status == ab.OrderStatus.FILLED
    assert order.filled_contracts > 0
    (pos,) = stack.portfolio.open_positions
    assert pos.contracts == order.filled_contracts
    # Key retained: retrying would duplicate live exposure
    assert ("evt-boom", opp.direction) in stack.ex._traded


async def test_open_position_survives_persist_failure(tmp_path):
    store = ab.StateStore(str(tmp_path / "state2.db"))

    def boom(pos, meta):
        raise RuntimeError("db locked")

    store.persist_open = boom
    portfolio = ab.PaperPortfolio(
        starting_capital=D("100"), position_size=D("50"),
        path=str(tmp_path / "p2.csv"), state_store=store,
    )
    await portfolio.open_position(ab.Position(
        event_name="evt", direction="d", contracts=1,
        cost_per_contract=D("0.90"), fees_per_contract=D("0.01"),
        total_outlay=D("0.91"), opened_at=ab._utc_iso(),
    ))
    assert len(portfolio.open_positions) == 1   # booked despite the failure
    assert portfolio.capital == D("99.09")
    store.close()


# ── C6: late-booked positions still reach settlement ─────────────────

async def test_late_booked_position_still_settled(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(ab, "RESOLUTION_CHECK_INTERVAL", 0.01)
    stack = build_stack(tmp_path)
    fetcher = FakeResolutionFetcher(settled_responses=[True])
    ev = {"name": "evt", "kalshi_ticker": "TICK", "poly_condition_id": "0xc"}
    stop = asyncio.Event()
    # An execution is mid-flight while the market closes
    inflight_fut = asyncio.get_running_loop().create_future()
    fake_exec = SimpleNamespace(_inflight={inflight_fut})

    task = asyncio.create_task(ab._resolution_checker(
        [ev], {"evt": ev}, fetcher, stack.portfolio, stop,
        execution=fake_exec,
    ))
    await asyncio.sleep(0.05)   # event removed with no positions booked yet

    # The in-flight execution books its position AFTER removal
    await stack.portfolio.open_position(ab.Position(
        event_name="evt", direction="d", contracts=3,
        cost_per_contract=D("0.90"), fees_per_contract=D("0.01"),
        total_outlay=D("2.73"), opened_at=ab._utc_iso(),
    ))
    inflight_fut.cancel()
    fake_exec._inflight = set()

    await asyncio.wait_for(task, timeout=5)
    assert stack.portfolio.closed_count == 1    # credited, not stranded
    assert stack.portfolio.open_positions == []
    assert stop.is_set()
