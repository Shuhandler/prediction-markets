"""Integration: WS message → local books → queue → detection → paper fill
→ CSV audit rows, plus REST fetcher parsing against fake HTTP responses.
No network involved anywhere."""
import asyncio
import csv
from decimal import Decimal as D

import arb_bot as ab
from conftest import build_stack
from mocks import FakeResponse, FakeSession

YES_TOK = "111"
NO_TOK = "222"
COND = "0xcond"
TICKER = "TICK"
EVENT = "Event X"


async def _pump_pipeline(tmp_path):
    """Feed one arb-able state through real WS classes and the processor."""
    stack = build_stack(tmp_path)
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    stop = asyncio.Event()

    kalshi_ws = ab.KalshiWS()
    kalshi_ws.set_update_queue(queue)
    await kalshi_ws.subscribe(TICKER, EVENT)  # offline: just wires maps

    poly_ws = ab.PolymarketWS()
    poly_ws.set_update_queue(queue)
    await poly_ws.subscribe(COND, YES_TOK, NO_TOK, EVENT)

    # Kalshi snapshot: YES bids 0.30, NO bids 0.60
    #   -> implied YES ask = 1 - 0.60 = 0.40 (size 20)
    kalshi_ws._handle_message({
        "type": "orderbook_snapshot",
        "msg": {"market_ticker": TICKER, "yes": [[30, 25]], "no": [[60, 20]]},
    })
    # Polymarket NO-token book: asks at 0.50 (size 15)
    poly_ws._handle_message({
        "event_type": "book",
        "asset_id": NO_TOK,
        "buys": [{"price": "0.45", "size": "30"}],
        "sells": [{"price": "0.50", "size": "15"}],
    })

    active = [{
        "name": EVENT, "kalshi_ticker": TICKER, "poly_condition_id": COND,
        "poly_yes_token": YES_TOK, "poly_no_token": NO_TOK,
    }]
    name_to_event = {EVENT: active[0]}
    engine = ab.ArbEngine(min_margin=D("0.05"))

    task = asyncio.create_task(ab._process_updates(
        queue, active, name_to_event, kalshi_ws, poly_ws,
        engine, stack.ex, stop,
    ))
    # Wait for the fill instead of sleeping a fixed interval
    for _ in range(100):
        if stack.ex.filled_count:
            break
        await asyncio.sleep(0.02)
    stop.set()
    queue.put_nowait("wake")  # unblock the queue.get so the loop exits
    total = await asyncio.wait_for(task, timeout=10)
    return stack, total


async def test_ws_tick_to_filled_order(tmp_path):
    stack, total = await _pump_pipeline(tmp_path)
    assert total >= 1
    assert stack.ex.filled_count == 1
    (order,) = [o for o in stack.ex.orders if o.status == ab.OrderStatus.FILLED]
    # Direction A: implied Kalshi YES ask 0.40 + Poly NO ask 0.50
    assert order.kalshi_price == D("0.40")
    assert order.poly_price == D("0.50")
    # depth-capped: min(kalshi 20, poly 15) beats the ~53 capital allows
    assert order.filled_contracts == 15


async def test_csv_audit_rows_written(tmp_path):
    stack, _ = await _pump_pipeline(tmp_path)

    with open(tmp_path / "orders.csv", newline="") as fh:
        orders = list(csv.DictReader(fh))
    assert len(orders) == 1
    assert orders[0]["status"] == "FILLED"
    assert orders[0]["event_name"] == EVENT

    with open(tmp_path / "ledger.csv", newline="") as fh:
        ledger = list(csv.DictReader(fh))
    assert len(ledger) >= 1

    with open(tmp_path / "portfolio.csv", newline="") as fh:
        portfolio = list(csv.DictReader(fh))
    assert portfolio[0]["action"] == "OPEN"
    assert portfolio[0]["contracts"] == "15"


async def test_removed_market_stops_generating_ticks():
    queue: asyncio.Queue = asyncio.Queue()
    poly_ws = ab.PolymarketWS()
    poly_ws.set_update_queue(queue)
    await poly_ws.subscribe(COND, YES_TOK, NO_TOK, EVENT)

    book_msg = {
        "event_type": "book", "asset_id": NO_TOK,
        "buys": [{"price": "0.45", "size": "30"}], "sells": [],
    }
    poly_ws._handle_message(book_msg)
    assert queue.qsize() == 1

    poly_ws.remove_market(COND)
    poly_ws._handle_message(book_msg)      # stray post-resolution tick
    assert queue.qsize() == 1              # discarded, not queued
    assert NO_TOK not in poly_ws._books    # book not resurrected
    assert poly_ws.get_snapshot(COND) is None


# ── REST fetcher parsing (mocked aiohttp) ────────────────────────────

async def test_fetch_kalshi_parses_cents_and_implied_asks():
    fetcher = ab.MarketFetcher()
    fetcher._session = FakeSession([FakeResponse(200, {
        "orderbook": {
            "yes": [[39, 5], [40, 10]],   # unsorted on purpose
            "no": [[55, 20]],
        },
    })])
    snap = await fetcher.fetch_kalshi("TICK")

    assert snap.yes.best_bid == D("0.4")          # best of 39/40
    assert snap.yes.best_bid_size == D("10")
    assert snap.no.best_ask == D("0.6")           # implied: 1 - 0.40
    assert snap.no.best_ask_size == D("10")
    assert snap.yes.best_ask == D("0.45")         # implied: 1 - 0.55
    assert snap.yes.best_ask_size == D("20")
    assert snap.price_source == "book"


async def test_fetch_kalshi_handles_empty_book():
    fetcher = ab.MarketFetcher()
    fetcher._session = FakeSession([FakeResponse(200, {"orderbook": {}})])
    snap = await fetcher.fetch_kalshi("TICK")
    assert snap is not None
    assert snap.yes.best_bid is None and snap.no.best_bid is None


async def test_queue_full_drops_updates_without_blocking():
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    kalshi_ws = ab.KalshiWS()
    kalshi_ws.set_update_queue(queue)
    await kalshi_ws.subscribe(TICKER, EVENT)
    msg = {
        "type": "orderbook_snapshot",
        "msg": {"market_ticker": TICKER, "yes": [[30, 5]], "no": []},
    }
    kalshi_ws._handle_message(msg)
    kalshi_ws._handle_message(msg)  # queue full — must not raise
    assert queue.qsize() == 1
