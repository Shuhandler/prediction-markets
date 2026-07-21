"""ExecutionEngine.submit(): every silent-reject path, size-down-to-depth,
order-level outlay, capital reservation, precise exposure check, rate-limit
audit row, dedup semantics, and error recovery."""
import asyncio
from decimal import Decimal as D

import arb_bot as ab
from conftest import build_stack, make_opp, order_outlay


# ── Fill path ────────────────────────────────────────────────────────

async def test_paper_fill_sizes_down_to_depth(stack):
    opp = make_opp("evt-fill", k_depth=7, p_depth=9)  # capital allows ~53
    order = await stack.ex.submit(opp)
    assert order.status == ab.OrderStatus.FILLED
    assert order.filled_contracts == 7
    assert order.total_outlay == order_outlay(opp, 7)
    assert stack.portfolio.capital == D("1000") - order_outlay(opp, 7)
    assert stack.ex._reserved == ab.ZERO


async def test_capital_binds_when_depth_is_deep(stack):
    opp = make_opp("evt-deep", k_depth=500, p_depth=500)
    order = await stack.ex.submit(opp)
    # position_size $50 / ~0.9357 per contract = 53
    assert order.filled_contracts == 53
    assert order.total_outlay <= D("50")


async def test_expected_pnl_recorded(stack):
    opp = make_opp("evt-pnl", k_depth=5, p_depth=5)
    await stack.ex.submit(opp)
    assert stack.pnl.daily_pnl == (opp.net_profit * 5).quantize(ab.Q4)


# ── Silent reject paths (no Order, no CSV row) ───────────────────────

async def test_duplicate_allowed_when_unlimited(stack):
    opp = make_opp("evt-dup")
    assert (await stack.ex.submit(opp)).status == ab.OrderStatus.FILLED
    # Default MAX_TRADES_PER_EVENT=0 (unlimited) — second trade goes through
    order2 = await stack.ex.submit(opp)
    assert order2 is not None
    assert order2.status == ab.OrderStatus.FILLED


async def test_duplicate_rejected_when_capped(tmp_path):
    stack = build_stack(tmp_path)
    stack.ex._max_trades_per_event = 1  # allow only 1 trade per event+direction
    opp = make_opp("evt-dup-cap")
    assert (await stack.ex.submit(opp)).status == ab.OrderStatus.FILLED
    assert await stack.ex.submit(opp) is None


async def test_zero_depth_rejected(stack):
    assert await stack.ex.submit(make_opp("evt-z", k_depth=0)) is None


async def test_net_unprofitable_rejected(stack):
    opp = make_opp("evt-neg", net_profit=D("-0.01"))
    assert await stack.ex.submit(opp) is None


async def test_kill_switch_rejected(tmp_path):
    stack = build_stack(tmp_path)
    (tmp_path / "KILL").write_text("stop")
    assert await stack.ex.submit(make_opp("evt-kill")) is None


async def test_circuit_breaker_rejected(stack):
    stack.pnl.record(D("-101"), 1)  # trip the $100 daily limit
    assert stack.pnl.is_breached
    assert await stack.ex.submit(make_opp("evt-cb")) is None


async def test_spread_sanity_rejected(stack):
    # 30% gross spread > 20% threshold -> stale data, not arbitrage
    opp = make_opp("evt-wide", k_price="0.30", p_price="0.40")
    assert opp.gross_profit == D("0.3000")
    assert await stack.ex.submit(opp) is None


async def test_max_positions_rejected(stack):
    for i in range(10):
        stack.portfolio.open_positions.append(ab.Position(
            event_name=f"pos-{i}", direction="d", contracts=1,
            cost_per_contract=D("0.9"), fees_per_contract=D("0.01"),
            total_outlay=D("0.91"), opened_at=ab._utc_iso(),
        ))
    assert await stack.ex.submit(make_opp("evt-maxpos")) is None


async def test_no_capital_rejected(tmp_path):
    stack = build_stack(tmp_path, starting_capital=D("0.50"))
    assert await stack.ex.submit(make_opp("evt-poor")) is None


async def test_precise_exposure_check_rejects(tmp_path):
    # Open positions worth $460 of a $500 cap; a ~$47 trade must be
    # rejected even though the coarse check ($460 < $500) passes.
    stack = build_stack(tmp_path, starting_capital=D("10000"))
    stack.portfolio.open_positions.append(ab.Position(
        event_name="big", direction="d", contracts=500,
        cost_per_contract=D("0.90"), fees_per_contract=D("0.02"),
        total_outlay=D("460"), opened_at=ab._utc_iso(),
    ))
    opp = make_opp("evt-exp", k_depth=500, p_depth=500)  # plans ~$47
    assert await stack.ex.submit(opp) is None

    # A small trade that fits the $40 headroom still goes through
    small = make_opp("evt-exp-small", k_depth=20, p_depth=20)  # ~$19
    order = await stack.ex.submit(small)
    assert order.status == ab.OrderStatus.FILLED


# ── Rate limiter (the only rejection that writes a CSV row) ──────────

async def test_rate_limit_produces_rejected_order(tmp_path):
    stack = build_stack(tmp_path, max_per_minute=0)
    order = await stack.ex.submit(make_opp("evt-rate"))
    assert order.status == ab.OrderStatus.REJECTED
    assert order.reject_reason == ab.RejectReason.ORDER_RATE_LIMIT
    assert stack.ex._reserved == ab.ZERO


def test_order_rate_limiter_window():
    limiter = ab.OrderRateLimiter(max_per_minute=2)
    assert limiter.try_acquire() and limiter.try_acquire()
    assert not limiter.try_acquire()
    assert limiter.recent_count == 2


# ── Concurrency: reservation + per-event lock ────────────────────────

async def test_inflight_reservation_and_dedup(stack):
    release = asyncio.Event()
    orig = stack.um.execute

    async def slow_execute(opp, desired):
        await release.wait()
        return await orig(opp, desired)

    stack.um.execute = slow_execute
    opp = make_opp("evt-conc")
    t1 = asyncio.create_task(stack.ex.submit(opp))
    await asyncio.sleep(0.05)
    # capital is reserved while the first submission is in flight
    assert stack.ex._reserved > ab.ZERO
    t2 = asyncio.create_task(stack.ex.submit(opp))
    await asyncio.sleep(0.05)
    release.set()
    r1, r2 = await asyncio.gather(t1, t2)

    statuses = {r.status if r else None for r in (r1, r2)}
    # With unlimited trades, both should fill (serialised by the per-event lock)
    assert statuses == {ab.OrderStatus.FILLED}
    assert stack.ex._reserved == ab.ZERO


# ── Failure recovery ─────────────────────────────────────────────────

async def test_execution_error_releases_key_and_reservation(stack):
    async def boom(opp, desired):
        raise RuntimeError("exchange exploded")

    orig = stack.um.execute
    stack.um.execute = boom
    opp = make_opp("evt-boom")
    order = await stack.ex.submit(opp)
    assert order.status == ab.OrderStatus.CANCELLED
    assert stack.ex._reserved == ab.ZERO

    stack.um.execute = orig
    retry = await stack.ex.submit(opp)
    assert retry.status == ab.OrderStatus.FILLED


async def test_unwound_outcome_releases_key(stack):
    orig = stack.um._submit_leg

    async def fail_kalshi(leg):
        if leg.platform == "kalshi":
            leg.status = ab.LegStatus.FAILED
            leg.filled_contracts = 0
            return False
        return await orig(leg)

    stack.um._submit_leg = fail_kalshi
    opp = make_opp("evt-unw", k_depth=5, p_depth=5)
    order = await stack.ex.submit(opp)
    assert order.status == ab.OrderStatus.UNWOUND
    # loss = poly fee on buy + poly fee on sell-back, zero slippage in paper
    expected_loss = (ab.poly_taker_fee(opp.poly_price, 5) * 2).quantize(ab.Q4)
    assert order.total_outlay == expected_loss
    assert stack.pnl.daily_pnl == -expected_loss

    stack.um._submit_leg = orig
    retry = await stack.ex.submit(opp)
    assert retry.status == ab.OrderStatus.FILLED
