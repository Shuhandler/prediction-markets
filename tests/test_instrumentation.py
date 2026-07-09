"""Phase 7 data capture: the observation ledger (full spread distribution,
throttled, independent of execution filters) and the raw WS recorder
(gzip JSONL, daily rotation, restart-safe append)."""
import asyncio
import csv
import gzip
import json
import time
from decimal import Decimal as D
from types import SimpleNamespace

import aiohttp

import arb_bot as ab
from conftest import build_stack, make_opp, snap


# ── ArbEngine.observe(): the margin-free evaluation path ─────────────

def test_observe_includes_below_margin_and_negative_gross():
    engine = ab.ArbEngine(min_margin=D("0.05"))
    kalshi = snap(yes={"ask": "0.50", "ask_size": "10"})
    poly = snap(no={"ask": "0.53", "ask_size": "10"}, source="polymarket")
    # cost 1.03: negative gross — invisible to check(), visible to observe()
    assert engine.check("evt", kalshi, poly) == []
    obs = engine.observe("evt", kalshi, poly)
    assert len(obs) == 1
    assert obs[0].gross_profit == D("-0.0300")
    assert obs[0].net_profit < obs[0].gross_profit  # fees still applied


def test_check_equals_observe_filtered():
    """check() must be exactly observe() + margin filter — detection and
    observation share one code path and cannot drift."""
    engine = ab.ArbEngine(min_margin=D("0.05"))
    kalshi = snap(yes={"ask": "0.40"}, no={"ask": "0.58"})
    poly = snap(yes={"ask": "0.44"}, no={"ask": "0.50"}, source="polymarket")
    # A: 0.40+0.50=0.90 (gross 0.10, qualifies); B: 0.58+0.44=1.02 (no)
    obs = engine.observe("evt", kalshi, poly)
    checked = engine.check("evt", kalshi, poly)
    assert len(obs) == 2
    assert [o.direction for o in checked] == [
        o.direction for o in obs if o.gross_profit >= engine.min_margin
    ]


# ── ObservationLogger throttling ─────────────────────────────────────

def obs_logger(tmp_path, hot=0.05, baseline=10.0):
    return ab.ObservationLogger(
        path=str(tmp_path / "observations.csv"),
        hot_interval=hot, baseline_interval=baseline,
    )


def read_rows(tmp_path):
    with open(tmp_path / "observations.csv", newline="") as fh:
        return list(csv.DictReader(fh))


def test_hot_spread_sampled_at_hot_interval(tmp_path):
    logger = obs_logger(tmp_path, hot=0.05, baseline=10.0)
    hot = make_opp("evt-hot")  # gross 0.10 > 0
    assert logger.maybe_log([hot]) == 1
    assert logger.maybe_log([hot]) == 0     # inside hot interval
    time.sleep(0.06)
    assert logger.maybe_log([hot]) == 1     # hot interval elapsed
    logger.close()
    rows = read_rows(tmp_path)
    assert len(rows) == 2
    assert rows[0]["gross_profit"] == "0.1000"
    assert rows[0]["event_name"] == "evt-hot"


def test_baseline_spread_sampled_coarsely(tmp_path):
    logger = obs_logger(tmp_path, hot=0.05, baseline=10.0)
    cold = make_opp("evt-cold", k_price="0.55", p_price="0.55")  # gross < 0
    assert logger.maybe_log([cold]) == 1
    time.sleep(0.06)  # past the HOT interval, well inside baseline
    assert logger.maybe_log([cold]) == 0
    logger.close()
    assert len(read_rows(tmp_path)) == 1


def test_throttle_is_per_event_and_direction(tmp_path):
    logger = obs_logger(tmp_path)
    a = make_opp("evt-1")
    b = make_opp("evt-2")
    c = make_opp("evt-1", direction="Buy NO@Kalshi + Buy YES@Polymarket")
    assert logger.maybe_log([a, b, c]) == 3  # distinct keys, no interference
    logger.close()


async def test_observation_row_written_even_when_execution_skips(tmp_path, stack):
    """The whole point: rejected/duplicate/below-margin evaluations still
    land in the observation ledger."""
    logger = obs_logger(tmp_path)
    kalshi_ws = SimpleNamespace(
        get_snapshot=lambda t: snap(yes={"ask": "0.47", "ask_size": "10"}),
    )
    poly_ws = SimpleNamespace(
        get_snapshot=lambda c: snap(
            no={"ask": "0.51", "ask_size": "10"}, source="polymarket",
        ),
    )
    cfg = {"name": "evt", "kalshi_ticker": "T", "poly_condition_id": "0xc"}
    # cost 0.98: gross 0.02 < margin -> execution skips it entirely
    found = ab._check_event(
        "evt", [cfg], {"evt": cfg}, kalshi_ws, poly_ws,
        ab.ArbEngine(min_margin=D("0.05")), stack.ex, None, logger,
    )
    logger.close()
    assert found == 0
    rows = read_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["gross_profit"] == "0.0200"
    assert stack.ex.filled_count == 0


def test_rows_written_counter(tmp_path):
    logger = obs_logger(tmp_path)
    logger.maybe_log([make_opp("e1"), make_opp("e2")])
    assert logger.rows_written == 2
    logger.close()


# ── WSRecorder ───────────────────────────────────────────────────────

def read_capture(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def test_recorder_roundtrip(tmp_path):
    rec = ab.WSRecorder(directory=str(tmp_path / "cap"), flush_interval=0)
    raw1 = '{"event_type":"book","asset_id":"111","buys":[]}'
    rec.record("polymarket", raw1)
    rec.record("polymarket", "PONG")
    rec.record("kalshi", '{"type":"orderbook_delta"}')
    rec.close()

    day = time.strftime("%Y%m%d", time.gmtime())
    poly_rows = read_capture(tmp_path / "cap" / f"polymarket-{day}.jsonl.gz")
    assert [r["raw"] for r in poly_rows] == [raw1, "PONG"]
    assert json.loads(poly_rows[0]["raw"])["asset_id"] == "111"  # verbatim
    assert all(isinstance(r["ts"], float) for r in poly_rows)

    kalshi_rows = read_capture(tmp_path / "cap" / f"kalshi-{day}.jsonl.gz")
    assert len(kalshi_rows) == 1
    assert rec.counts == {"polymarket": 2, "kalshi": 1}


def test_recorder_append_after_restart(tmp_path):
    """Appending creates a second gzip member — readers must see both."""
    cap = str(tmp_path / "cap")
    rec1 = ab.WSRecorder(directory=cap, flush_interval=0)
    rec1.record("kalshi", "before-restart")
    rec1.close()
    rec2 = ab.WSRecorder(directory=cap, flush_interval=0)
    rec2.record("kalshi", "after-restart")
    rec2.close()

    day = time.strftime("%Y%m%d", time.gmtime())
    rows = read_capture(tmp_path / "cap" / f"kalshi-{day}.jsonl.gz")
    assert [r["raw"] for r in rows] == ["before-restart", "after-restart"]


def test_recorder_daily_rotation(tmp_path, monkeypatch):
    rec = ab.WSRecorder(directory=str(tmp_path / "cap"), flush_interval=0)
    day1 = time.mktime((2026, 7, 9, 12, 0, 0, 0, 0, 0))
    day2 = time.mktime((2026, 7, 10, 12, 0, 0, 0, 0, 0))
    clock = [day1]
    monkeypatch.setattr(ab.time, "time", lambda: clock[0])
    rec.record("kalshi", "day-one")
    clock[0] = day2
    rec.record("kalshi", "day-two")
    rec.close()
    monkeypatch.undo()

    files = sorted(p.name for p in (tmp_path / "cap").iterdir())
    assert len(files) == 2
    assert [r["raw"] for r in read_capture(tmp_path / "cap" / files[0])] == ["day-one"]
    assert [r["raw"] for r in read_capture(tmp_path / "cap" / files[1])] == ["day-two"]


def test_recorder_failure_disables_not_raises(tmp_path):
    rec = ab.WSRecorder(directory=str(tmp_path / "cap"), flush_interval=0)
    rec.record("kalshi", "ok")
    rec._files["kalshi"][1].close()  # sabotage the handle
    rec.record("kalshi", "boom")     # must not raise
    assert rec._failed
    rec.record("kalshi", "ignored")  # no-op after failure
    # "boom" was counted (seen) but its flush failed; "ignored" was not
    assert rec.counts["kalshi"] == 2


def test_recorder_file_readable_after_hard_kill(tmp_path):
    """Regression (found in live validation): each flush must be a
    complete gzip member so a SIGKILL'd process — close() never runs —
    still leaves a strictly-readable file."""
    rec = ab.WSRecorder(directory=str(tmp_path / "cap"), flush_interval=0)
    rec.record("kalshi", "one")
    rec.record("kalshi", "two")
    # simulate hard kill: no close(), no final flush
    day = time.strftime("%Y%m%d", time.gmtime())
    rows = read_capture(tmp_path / "cap" / f"kalshi-{day}.jsonl.gz")
    assert [r["raw"] for r in rows] == ["one", "two"]


# ── Listener wiring: raw messages reach the recorder ─────────────────

class FakeWSMessages:
    def __init__(self, messages):
        self._msgs = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._msgs:
            raise StopAsyncIteration
        return self._msgs[0] if False else self._msgs.pop(0)


def text_msg(data):
    return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=data)


async def test_poly_listener_records_raw(tmp_path):
    rec = ab.WSRecorder(directory=str(tmp_path / "cap"), flush_interval=0)
    ws = ab.PolymarketWS()
    ws.set_recorder(rec)
    ws._ws = FakeWSMessages([
        text_msg("PONG"),
        text_msg('{"event_type":"book","asset_id":"X","buys":[],"sells":[]}'),
    ])

    async def no_reconnect():
        pass

    ws._schedule_reconnect = no_reconnect
    await ws._listen_loop()
    rec.close()
    assert rec.counts["polymarket"] == 2  # PONG captured too (full fidelity)


async def test_kalshi_listener_records_raw(tmp_path):
    rec = ab.WSRecorder(directory=str(tmp_path / "cap"), flush_interval=0)
    ws = ab.KalshiWS()
    ws.set_recorder(rec)
    ws._ws = FakeWSMessages([
        text_msg('{"type":"orderbook_snapshot","msg":{"market_ticker":"T","yes":[],"no":[]}}'),
    ])

    async def no_reconnect():
        pass

    ws._schedule_reconnect = no_reconnect
    await ws._listen_loop()
    rec.close()
    assert rec.counts["kalshi"] == 1


# ── Health endpoint exposes capture counters ─────────────────────────

def test_health_payload_includes_instrumentation(tmp_path, stack):
    logger = obs_logger(tmp_path)
    logger.maybe_log([make_opp("evt-h")])
    rec = ab.WSRecorder(directory=str(tmp_path / "cap"), flush_interval=0)
    rec.record("polymarket", "x")

    server = ab.HealthServer(
        "127.0.0.1", 0, mode="PAPER (test)",
        update_queue=asyncio.Queue(),
        kalshi_ws=None, poly_ws=None,
        portfolio=stack.portfolio, pnl_tracker=stack.pnl,
        unwind_manager=stack.um, execution=stack.ex,
        active_events=[], obs_logger=logger, recorder=rec,
    )
    body, _ = server.payload()
    assert body["observations_logged"] == 1
    assert body["ws_capture_messages"] == {"polymarket": 1}
    logger.close()
    rec.close()
