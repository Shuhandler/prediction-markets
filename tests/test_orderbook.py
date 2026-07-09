"""_LocalOrderbook: snapshot/delta application and top-of-book extraction,
including the property that any delta stream equals a rebuild from the
final state (the invariant behind trusting WS deltas at all).
"""
from decimal import Decimal as D

from hypothesis import given, settings, strategies as st

import arb_bot as ab


def test_poly_snapshot_drops_zero_sizes():
    book = ab._LocalOrderbook()
    book.apply_snapshot_poly({
        "buys": [{"price": "0.40", "size": "10"}, {"price": "0.30", "size": "0"}],
        "sells": [{"price": "0.55", "size": "7"}],
    })
    assert book.bids == {D("0.40"): D("10")}
    assert book.asks == {D("0.55"): D("7")}


def test_poly_snapshot_overwrites():
    book = ab._LocalOrderbook()
    book.apply_snapshot_poly({"buys": [{"price": "0.40", "size": "10"}], "sells": []})
    book.apply_snapshot_poly({"buys": [{"price": "0.20", "size": "3"}], "sells": []})
    assert book.bids == {D("0.20"): D("3")}


def test_poly_price_change_add_update_remove():
    book = ab._LocalOrderbook()
    book.apply_price_change_poly({"side": "BUY", "price": "0.40", "size": "10"})
    book.apply_price_change_poly({"side": "BUY", "price": "0.40", "size": "4"})
    assert book.bids == {D("0.40"): D("4")}
    book.apply_price_change_poly({"side": "BUY", "price": "0.40", "size": "0"})
    assert book.bids == {}


def test_kalshi_snapshot_and_delta():
    book = ab._LocalOrderbook()
    book.apply_snapshot_kalshi([[40, 10], [39, 5]])
    assert book.bids == {D("0.4"): D("10"), D("0.39"): D("5")}
    book.apply_delta_kalshi(40, -10)
    assert D("0.4") not in book.bids
    book.apply_delta_kalshi(41, 3)
    assert book.bids[D("0.41")] == D("3")


def test_to_tob_best_levels():
    book = ab._LocalOrderbook()
    book.apply_snapshot_poly({
        "buys": [
            {"price": "0.40", "size": "10"},
            {"price": "0.45", "size": "5"},
            {"price": "0.30", "size": "99"},
        ],
        "sells": [
            {"price": "0.55", "size": "7"},
            {"price": "0.60", "size": "3"},
        ],
    })
    tob = book.to_tob()
    assert (tob.best_bid, tob.best_bid_size) == (D("0.45"), D("5"))
    assert (tob.best_ask, tob.best_ask_size) == (D("0.55"), D("7"))
    # Hot path materializes top-of-book only
    assert len(tob.bid_levels) == 1 and len(tob.ask_levels) == 1


def test_to_tob_empty_book():
    tob = ab._LocalOrderbook().to_tob()
    assert tob.best_bid is None and tob.best_ask is None
    assert tob.bid_levels == [] and tob.ask_levels == []


# ── Property tests ───────────────────────────────────────────────────

POLY_CHANGES = st.lists(
    st.tuples(
        st.sampled_from(["BUY", "SELL"]),
        st.sampled_from(["0.10", "0.25", "0.40", "0.55", "0.70"]),
        st.integers(min_value=0, max_value=20),
    ),
    max_size=60,
)


@settings(deadline=None)
@given(changes=POLY_CHANGES)
def test_poly_delta_stream_equals_final_state(changes):
    """price_change events are absolute sizes: the book must equal the
    last nonzero size per (side, price), and to_tob must be max/min."""
    book = ab._LocalOrderbook()
    mirror = {"BUY": {}, "SELL": {}}
    for side, price, size in changes:
        book.apply_price_change_poly(
            {"side": side, "price": price, "size": str(size)},
        )
        if size > 0:
            mirror[side][D(price)] = D(size)
        else:
            mirror[side].pop(D(price), None)

    assert book.bids == mirror["BUY"]
    assert book.asks == mirror["SELL"]

    tob = book.to_tob()
    assert tob.best_bid == (max(mirror["BUY"]) if mirror["BUY"] else None)
    assert tob.best_ask == (min(mirror["SELL"]) if mirror["SELL"] else None)


KALSHI_DELTAS = st.lists(
    st.tuples(
        st.integers(min_value=1, max_value=99),
        st.integers(min_value=-5, max_value=5),
    ),
    max_size=60,
)


@settings(deadline=None)
@given(deltas=KALSHI_DELTAS)
def test_kalshi_delta_stream_equals_snapshot_rebuild(deltas):
    """Applying a random delta stream must equal rebuilding from the final
    accumulated sizes via apply_snapshot_kalshi."""
    via_deltas = ab._LocalOrderbook()
    acc: dict[int, int] = {}
    for cents, delta in deltas:
        via_deltas.apply_delta_kalshi(cents, delta)
        new = acc.get(cents, 0) + delta
        if new > 0:
            acc[cents] = new
        else:
            acc.pop(cents, None)

    via_snapshot = ab._LocalOrderbook()
    via_snapshot.apply_snapshot_kalshi([[c, s] for c, s in acc.items()])
    assert via_deltas.bids == via_snapshot.bids
