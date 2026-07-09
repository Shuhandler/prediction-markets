"""Phase 6 operational behavior: restart persistence end-to-end, shutdown
draining, the health endpoint, run-lock acquisition, and startup
reconciliation."""
import asyncio
from decimal import Decimal as D

import aiohttp

import arb_bot as ab
from conftest import build_stack, make_opp
from mocks import MockKalshiOrderClient


# ── Restart persistence (the reason StateStore exists) ───────────────

async def test_portfolio_survives_restart(tmp_path):
    store = ab.StateStore(str(tmp_path / "state.db"))
    p1 = ab.PaperPortfolio(
        starting_capital=D("1000"), position_size=D("50"),
        path=str(tmp_path / "p1.csv"), state_store=store,
    )
    pos = ab.Position(
        event_name="evt", direction="d", contracts=7,
        cost_per_contract=D("0.90"), fees_per_contract=D("0.0357"),
        total_outlay=D("6.5499"), opened_at=ab._utc_iso(),
    )
    await p1.open_position(pos)
    capital_after_open = p1.capital

    # "restart": new portfolio object, same store, different config —
    # persisted state wins over STARTING_CAPITAL
    p2 = ab.PaperPortfolio(
        starting_capital=D("9999"), position_size=D("50"),
        path=str(tmp_path / "p2.csv"), state_store=store,
    )
    assert p2.capital == capital_after_open
    assert p2.starting_capital == D("1000")
    assert len(p2.open_positions) == 1
    assert p2.open_positions[0].event_name == "evt"

    # resolution after the restart settles the restored position
    await p2.close_positions_for_event("evt")
    assert p2.open_positions == []
    assert p2.capital == capital_after_open + D("7")

    p3 = ab.PaperPortfolio(
        starting_capital=D("1000"), position_size=D("50"),
        path=str(tmp_path / "p3.csv"), state_store=store,
    )
    assert p3.open_positions == []
    assert p3.capital == p2.capital
    assert p3.closed_count == 1
    store.close()


async def test_unwind_loss_persists_across_restart(tmp_path):
    store = ab.StateStore(str(tmp_path / "state.db"))
    p1 = ab.PaperPortfolio(
        starting_capital=D("1000"), position_size=D("50"),
        path=str(tmp_path / "p1.csv"), state_store=store,
    )
    await p1.record_unwind_loss("evt", "d", 5, D("0.1564"))

    p2 = ab.PaperPortfolio(
        starting_capital=D("1000"), position_size=D("50"),
        path=str(tmp_path / "p2.csv"), state_store=store,
    )
    assert p2.capital == D("1000") - D("0.1564")
    assert p2.total_unwind_losses == D("0.1564")
    store.close()


async def test_orphans_survive_restart(tmp_path, stack):
    store = ab.StateStore(str(tmp_path / "state.db"))
    stack.um._state_store = store

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
    await stack.um.execute(make_opp("evt-orph", k_depth=4, p_depth=4), 4)
    assert stack.um.orphan_count == 1

    # "restart": fresh UnwindManager on the same store
    um2 = ab.UnwindManager(
        portfolio=stack.portfolio, pnl_tracker=stack.pnl,
        logger=ab.UnwindLogger(path=str(tmp_path / "u2.csv")),
        state_store=store,
    )
    assert um2.orphan_count == 0
    um2.restore_orphans()
    assert um2.orphan_count == 1

    # retry succeeds (paper unwind fills) and clears the persisted row
    await um2.retry_orphaned_unwinds()
    assert um2.orphan_count == 0
    assert store.load_orphans() == {}
    store.close()


# ── Shutdown drain ───────────────────────────────────────────────────

async def test_drain_waits_for_inflight_execution(stack):
    release = asyncio.Event()
    orig = stack.um.execute

    async def slow_execute(opp, desired):
        await release.wait()
        return await orig(opp, desired)

    stack.um.execute = slow_execute
    stack.ex.submit_background(make_opp("evt-drain"))
    await asyncio.sleep(0.05)
    assert len(stack.ex._inflight) == 1

    async def release_soon():
        await asyncio.sleep(0.1)
        release.set()

    asyncio.create_task(release_soon())
    assert await stack.ex.drain(timeout=5)
    assert stack.ex.filled_count == 1
    assert stack.ex._inflight == set()


async def test_drain_times_out_and_reports(stack):
    never = asyncio.Event()

    async def hang(opp, desired):
        await never.wait()

    stack.um.execute = hang
    task = stack.ex.submit_background(make_opp("evt-hang"))
    await asyncio.sleep(0.05)
    assert not await stack.ex.drain(timeout=0.1)
    task.cancel()


async def test_drain_noop_when_idle(stack):
    assert await stack.ex.drain(timeout=0.1)


# ── Health endpoint ──────────────────────────────────────────────────

def make_health(stack, queue, kalshi_ws, poly_ws):
    return ab.HealthServer(
        "127.0.0.1", 0,  # port 0: bind an ephemeral port for the test
        mode="PAPER (test)", update_queue=queue,
        kalshi_ws=kalshi_ws, poly_ws=poly_ws,
        portfolio=stack.portfolio, pnl_tracker=stack.pnl,
        unwind_manager=stack.um, execution=stack.ex,
        active_events=[{"name": "e1"}],
    )


async def test_health_endpoint_reports_ok(stack):
    queue: asyncio.Queue = asyncio.Queue()
    kalshi_ws = ab.KalshiWS()
    kalshi_ws._connected = True
    kalshi_ws.last_update_time = __import__("time").time()

    server = make_health(stack, queue, kalshi_ws, None)
    await server.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{server.port}/health",
            ) as resp:
                assert resp.status == 200
                body = await resp.json()
    finally:
        await server.stop()

    assert body["status"] == "ok"
    assert body["ws"]["kalshi"]["connected"] is True
    assert body["ws"]["polymarket"]["configured"] is False
    assert body["capital"] == "1000.00"
    assert body["circuit_breaker_tripped"] is False
    assert body["orphaned_unwinds"] == 0


async def test_health_endpoint_degrades_on_stale_ws(stack):
    queue: asyncio.Queue = asyncio.Queue()
    kalshi_ws = ab.KalshiWS()
    kalshi_ws._connected = False
    kalshi_ws.last_update_time = 1.0  # ancient

    server = make_health(stack, queue, kalshi_ws, None)
    await server.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{server.port}/health",
            ) as resp:
                assert resp.status == 503
                body = await resp.json()
    finally:
        await server.stop()
    assert body["status"] == "degraded"


def test_health_payload_startup_grace(stack):
    # Never-updated WS is healthy only during the startup grace window
    queue: asyncio.Queue = asyncio.Queue()
    kalshi_ws = ab.KalshiWS()  # last_update_time == 0.0
    server = make_health(stack, queue, kalshi_ws, None)
    _, healthy = server.payload()
    assert healthy  # just started
    server._started -= ab.STALE_TIMEOUT_SECONDS * 4
    _, healthy = server.payload()
    assert not healthy  # grace expired, still no data


# ── Run-lock acquisition helper ──────────────────────────────────────

async def test_acquire_run_lock_takes_over_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "RUN_LOCK_STALE_SECONDS", 0.05)
    store = ab.StateStore(str(tmp_path / "state.db"))
    store.try_acquire_run_lock("dead-instance", stale_after=0.05)
    __import__("time").sleep(0.1)
    assert await ab._acquire_run_lock(store, "new-instance")
    store.close()


# ── Startup reconciliation ───────────────────────────────────────────

def _n2e():
    return {"evt": {"name": "evt", "kalshi_ticker": "TICK",
                    "poly_condition_id": "0xc"}}


async def test_reconciliation_clean_match(stack, caplog):
    kalshi = MockKalshiOrderClient()
    stack.portfolio.open_positions.append(ab.Position(
        event_name="evt", direction="d", contracts=7,
        cost_per_contract=D("0.9"), fees_per_contract=D("0.03"),
        total_outlay=D("6.51"), opened_at=ab._utc_iso(),
    ))
    kalshi.get_positions_response = None

    async def get_positions():
        return {"market_positions": [{"ticker": "TICK", "position": 7}]}

    kalshi.get_positions = get_positions
    await ab._reconcile_startup_positions(
        kalshi, stack.portfolio, _n2e(), notifier=None,
    )
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


async def test_reconciliation_flags_mismatch(stack, caplog):
    kalshi = MockKalshiOrderClient()

    async def get_positions():
        # exchange says 4, our state says 7; plus an unknown exchange pos
        return {"market_positions": [
            {"ticker": "TICK", "position": -4},
            {"ticker": "GHOST", "position": 2},
        ]}

    kalshi.get_positions = get_positions
    stack.portfolio.open_positions.append(ab.Position(
        event_name="evt", direction="d", contracts=7,
        cost_per_contract=D("0.9"), fees_per_contract=D("0.03"),
        total_outlay=D("6.51"), opened_at=ab._utc_iso(),
    ))
    await ab._reconcile_startup_positions(
        kalshi, stack.portfolio, _n2e(), notifier=None,
    )
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("TICK" in e and "persisted=7" in e and "exchange=4" in e
               for e in errors)
    assert any("GHOST" in e for e in errors)
