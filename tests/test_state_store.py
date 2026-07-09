"""StateStore: Decimal-exact persistence of portfolio state, orphaned
exposure, and run-lock semantics (fresh holder blocks, stale expires)."""
import time
from decimal import Decimal as D

import arb_bot as ab


def store(tmp_path):
    return ab.StateStore(str(tmp_path / "state.db"))


def make_pos(name="evt", contracts=7):
    return ab.Position(
        event_name=name, direction="Buy YES@Kalshi + Buy NO@Polymarket",
        contracts=contracts, cost_per_contract=D("0.90"),
        fees_per_contract=D("0.0357"), total_outlay=D("6.5499"),
        opened_at="2026-07-09T12:00:00+00:00",
    )


class FakePortfolio:
    """Just the fields StateStore persists."""
    capital = D("993.4501")
    total_realized_pnl = D("1.2345")
    total_unwind_losses = D("0.0782")
    closed_count = 3


def test_meta_roundtrip_preserves_decimals(tmp_path):
    s = store(tmp_path)
    assert s.load_meta() is None  # first run
    s.init_meta(D("1000.00"))
    s.persist_meta(FakePortfolio())
    meta = s.load_meta()
    assert meta["capital"] == D("993.4501")
    assert meta["realized_pnl"] == D("1.2345")
    assert meta["unwind_losses"] == D("0.0782")
    assert meta["closed_count"] == 3
    assert isinstance(meta["capital"], D)  # exact Decimal, not float
    s.close()


def test_positions_roundtrip(tmp_path):
    s = store(tmp_path)
    s.init_meta(D("1000"))
    s.persist_open(make_pos("evt-a"), FakePortfolio())
    s.persist_open(make_pos("evt-b", contracts=3), FakePortfolio())

    restored = s.load_positions()
    assert [p.event_name for p in restored] == ["evt-a", "evt-b"]
    assert restored[0].contracts == 7
    assert restored[0].total_outlay == D("6.5499")

    s.persist_close("evt-a", FakePortfolio())
    assert [p.event_name for p in s.load_positions()] == ["evt-b"]
    s.close()


def test_orphan_lifecycle(tmp_path):
    s = store(tmp_path)
    info = {
        "event_name": "evt", "direction": "d", "platform": "polymarket",
        "side": "no", "market_id": "0xc", "buy_price": D("0.50"),
        "contracts": 4, "attempts": 0,
    }
    s.save_orphan("uw1", info)
    loaded = s.load_orphans()
    assert loaded["uw1"]["buy_price"] == D("0.50")
    assert loaded["uw1"]["contracts"] == 4

    s.update_orphan("uw1", contracts=2, attempts=3)
    loaded = s.load_orphans()
    assert (loaded["uw1"]["contracts"], loaded["uw1"]["attempts"]) == (2, 3)

    s.delete_orphan("uw1")
    assert s.load_orphans() == {}
    s.close()


def test_run_lock_blocks_second_live_instance(tmp_path):
    s = store(tmp_path)
    assert s.try_acquire_run_lock("host:1", stale_after=90)
    assert not s.try_acquire_run_lock("host:2", stale_after=90)
    # the holder itself can re-acquire (idempotent heartbeat path)
    assert s.try_acquire_run_lock("host:1", stale_after=90)
    s.close()


def test_run_lock_stale_holder_expires(tmp_path):
    s = store(tmp_path)
    assert s.try_acquire_run_lock("host:1", stale_after=0.05)
    time.sleep(0.1)
    # crashed predecessor: heartbeat is stale -> takeover allowed
    assert s.try_acquire_run_lock("host:2", stale_after=0.05)
    s.close()


def test_run_lock_heartbeat_and_release(tmp_path):
    s = store(tmp_path)
    assert s.try_acquire_run_lock("host:1", stale_after=0.2)
    time.sleep(0.1)
    s.heartbeat_run_lock("host:1")  # refresh before expiry
    time.sleep(0.15)
    # 0.25s since acquire but only 0.15s since heartbeat -> still live
    assert not s.try_acquire_run_lock("host:2", stale_after=0.2)

    s.release_run_lock("host:1")
    assert s.try_acquire_run_lock("host:2", stale_after=0.2)
    s.close()


def test_release_ignores_non_holder(tmp_path):
    s = store(tmp_path)
    assert s.try_acquire_run_lock("host:1", stale_after=90)
    s.release_run_lock("host:2")  # not the holder — must be a no-op
    assert not s.try_acquire_run_lock("host:3", stale_after=90)
    s.close()
