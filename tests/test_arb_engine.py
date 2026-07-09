"""ArbEngine.check()/_evaluate(): detection thresholds, fee-aware net
profit, direction handling, and depth extraction."""
from decimal import Decimal as D

import arb_bot as ab
from conftest import snap


def engine():
    return ab.ArbEngine(min_margin=D("0.05"))


def test_direction_a_detected():
    opps = engine().check(
        "evt",
        kalshi=snap(yes={"ask": "0.40", "ask_size": "10"}),
        poly=snap(no={"ask": "0.50", "ask_size": "8"}, source="polymarket"),
    )
    assert len(opps) == 1
    opp = opps[0]
    assert opp.direction == "Buy YES@Kalshi + Buy NO@Polymarket"
    assert opp.total_cost == D("0.9000")
    assert opp.gross_profit == D("0.1000")
    # net = 1 - 0.90 - kalshi_fee(0.40) - poly_fee(0.50) = 1 - 0.90 - 0.02 - 0.0157
    assert opp.net_profit == D("0.0643")
    assert (opp.kalshi_depth, opp.poly_depth, opp.max_contracts) == (10, 8, 8)


def test_margin_boundary_inclusive():
    # gross exactly at the margin qualifies (>=)
    at_margin = engine().check(
        "evt",
        kalshi=snap(yes={"ask": "0.45"}),
        poly=snap(no={"ask": "0.50"}, source="polymarket"),
    )
    assert len(at_margin) == 1

    below = engine().check(
        "evt",
        kalshi=snap(yes={"ask": "0.46"}),
        poly=snap(no={"ask": "0.50"}, source="polymarket"),
    )
    assert below == []


def test_both_directions_detected():
    opps = engine().check(
        "evt",
        kalshi=snap(yes={"ask": "0.40"}, no={"ask": "0.45"}),
        poly=snap(yes={"ask": "0.45"}, no={"ask": "0.50"}, source="polymarket"),
    )
    directions = {o.direction for o in opps}
    assert directions == {
        "Buy YES@Kalshi + Buy NO@Polymarket",
        "Buy NO@Kalshi + Buy YES@Polymarket",
    }


def test_missing_asks_skip_directions():
    # No poly NO ask -> direction A impossible; no kalshi NO ask -> B impossible
    opps = engine().check(
        "evt",
        kalshi=snap(yes={"ask": "0.40"}),
        poly=snap(yes={"ask": "0.40"}, source="polymarket"),
    )
    assert opps == []


def test_no_arb_when_costs_sum_above_one():
    opps = engine().check(
        "evt",
        kalshi=snap(yes={"ask": "0.55"}),
        poly=snap(no={"ask": "0.55"}, source="polymarket"),
    )
    assert opps == []


def test_depth_zero_when_no_levels():
    # best_ask present but ask_levels empty (indicative /price fallback)
    kalshi = snap(yes={"ask": "0.40", "ask_size": "10"})
    poly = ab.MarketSnapshot(source="polymarket", price_source="indicative")
    poly.no.best_ask = D("0.50")  # no ask_levels
    opps = engine().check("evt", kalshi=kalshi, poly=poly)
    assert len(opps) == 1
    assert opps[0].poly_depth == 0
    assert opps[0].max_contracts == 0
