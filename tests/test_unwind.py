"""UnwindManager: leg ordering invariant, partial fills, orphan registry,
stuck-position detection, and the execute/force_unwind lock."""
import asyncio
from datetime import timedelta
from decimal import Decimal as D

import arb_bot as ab
from conftest import make_opp


async def test_paper_complete(stack):
    opp = make_opp("evt-ok", k_depth=5, p_depth=5)
    uw = await stack.um.execute(opp, 5)
    assert uw.status == ab.UnwindStatus.COMPLETE
    assert uw.leg1.platform == "polymarket"
    assert uw.leg2.platform == "kalshi"
    assert uw.total_contracts == 5
    assert stack.um.active_count == 0


async def test_direction_b_leg_sides(stack):
    opp = make_opp(
        "evt-b", direction="Buy NO@Kalshi + Buy YES@Polymarket",
        k_depth=5, p_depth=5,
    )
    uw = await stack.um.execute(opp, 5)
    assert (uw.leg1.platform, uw.leg1.side) == ("polymarket", "yes")
    assert (uw.leg2.platform, uw.leg2.side) == ("kalshi", "no")


async def test_leg1_failure_never_places_kalshi_order(stack):
    """Zero legging risk: if the Polymarket FOK fails, no Kalshi order."""
    submitted = []

    async def failing_poly(leg):
        submitted.append(leg.platform)
        leg.status = ab.LegStatus.FAILED
        leg.filled_contracts = 0
        return False

    stack.um._submit_leg = failing_poly
    uw = await stack.um.execute(make_opp("evt-l1"), 5)
    assert uw.status == ab.UnwindStatus.FAILED
    assert submitted == ["polymarket"]
    # No exposure -> nothing unwound, nothing orphaned, no loss
    assert uw.unwind_leg is None
    assert stack.um.orphan_count == 0
    assert stack.portfolio.total_unwind_losses == ab.ZERO


async def test_leg2_partial_fill_unwinds_excess(stack):
    """Leg 2 fills 3/5: the 3 paired contracts become the position, the
    2 excess Leg 1 contracts are sold back with the loss booked."""
    orig = stack.um._submit_leg

    async def partial_kalshi(leg):
        if leg.platform == "kalshi":
            leg.status = ab.LegStatus.PARTIAL
            leg.filled_contracts = 3
            leg.filled_at = ab._utc_iso()
            return True
        return await orig(leg)

    stack.um._submit_leg = partial_kalshi
    opp = make_opp("evt-part", k_depth=5, p_depth=5)
    uw = await stack.um.execute(opp, 5)

    assert uw.status == ab.UnwindStatus.COMPLETE
    assert uw.total_contracts == 3
    assert uw.unwind_leg is not None
    assert uw.unwind_leg.contracts == 2
    # paper sell-back: poly fee both ways on 2 contracts, zero slippage
    expected = (ab.poly_taker_fee(opp.poly_price, 2) * 2).quantize(ab.Q4)
    assert stack.portfolio.total_unwind_losses == expected


async def test_failed_unwind_registers_orphan_and_retry_flattens(stack):
    orig_leg = stack.um._submit_leg
    orig_unwind = stack.um._submit_unwind

    async def fail_kalshi(leg):
        if leg.platform == "kalshi":
            leg.status = ab.LegStatus.FAILED
            leg.filled_contracts = 0
            return False
        return await orig_leg(leg)

    async def fail_unwind(leg):
        leg.status = ab.LegStatus.FAILED
        leg.filled_contracts = 0
        return False

    stack.um._submit_leg = fail_kalshi
    stack.um._submit_unwind = fail_unwind
    opp = make_opp("evt-orph", k_depth=4, p_depth=4)
    uw = await stack.um.execute(opp, 4)

    assert uw.status == ab.UnwindStatus.FAILED
    assert stack.um.orphan_count == 1
    assert stack.portfolio.total_unwind_losses == ab.ZERO  # nothing sold yet

    # StuckPositionMonitor's retry path, with the sell now succeeding
    stack.um._submit_unwind = orig_unwind
    await stack.um.retry_orphaned_unwinds()
    assert stack.um.orphan_count == 0
    expected = (ab.poly_taker_fee(opp.poly_price, 4) * 2).quantize(ab.Q4)
    assert stack.portfolio.total_unwind_losses == expected


async def test_partially_filled_unwind_books_loss_and_orphans_rest(stack):
    """Unwind sell fills 1 of 4: loss booked for the 1 sold, orphan holds
    the remaining 3 (previously the sold portion was never accounted)."""
    orig_leg = stack.um._submit_leg

    async def fail_kalshi(leg):
        if leg.platform == "kalshi":
            leg.status = ab.LegStatus.FAILED
            leg.filled_contracts = 0
            return False
        return await orig_leg(leg)

    async def partial_unwind(leg):
        leg.status = ab.LegStatus.FAILED
        leg.filled_contracts = 1
        return False

    stack.um._submit_leg = fail_kalshi
    stack.um._submit_unwind = partial_unwind
    opp = make_opp("evt-porph", k_depth=4, p_depth=4)
    uw = await stack.um.execute(opp, 4)

    assert uw.status == ab.UnwindStatus.FAILED
    expected = (ab.poly_taker_fee(opp.poly_price, 1) * 2).quantize(ab.Q4)
    assert stack.portfolio.total_unwind_losses == expected
    assert stack.um.orphan_count == 1
    assert stack.um._orphans[uw.unwind_id]["contracts"] == 3


async def test_force_unwind_waits_for_inflight_execute(stack):
    """The monitor must not sell Leg 1 while execute() is mid-flight; it
    waits on the per-order lock and re-checks."""
    gate = asyncio.Event()
    orig = stack.um._submit_leg

    async def gated_kalshi(leg):
        if leg.platform == "kalshi":
            await gate.wait()
        return await orig(leg)

    stack.um._submit_leg = gated_kalshi
    task = asyncio.create_task(stack.um.execute(make_opp("evt-lock"), 5))
    await asyncio.sleep(0.05)

    (uw,) = stack.um._active.values()
    assert uw.status == ab.UnwindStatus.LEG2_SUBMITTED
    force = asyncio.create_task(stack.um.force_unwind(uw))
    await asyncio.sleep(0.05)
    assert not force.done()  # blocked on the order lock

    gate.set()
    await asyncio.gather(task, force)
    # execute() finished cleanly; force_unwind saw a resolved order and
    # did nothing
    assert uw.status == ab.UnwindStatus.COMPLETE
    assert uw.unwind_leg is None
    assert stack.portfolio.total_unwind_losses == ab.ZERO


async def test_get_stuck_positions_by_age(stack):
    old = ab.UnwindOrder(
        unwind_id="old", event_name="e", direction="d",
        status=ab.UnwindStatus.LEG1_FILLED,
        created_at=(ab._utc_now() - timedelta(seconds=60)).isoformat(),
    )
    fresh = ab.UnwindOrder(
        unwind_id="new", event_name="e", direction="d",
        status=ab.UnwindStatus.LEG1_FILLED,
        created_at=ab._utc_now().isoformat(),
    )
    done = ab.UnwindOrder(
        unwind_id="done", event_name="e", direction="d",
        status=ab.UnwindStatus.COMPLETE,
        created_at=(ab._utc_now() - timedelta(seconds=60)).isoformat(),
    )
    stack.um._active = {u.unwind_id: u for u in (old, fresh, done)}
    stuck = stack.um.get_stuck_positions(max_age_seconds=30)
    assert [u.unwind_id for u in stuck] == ["old"]
