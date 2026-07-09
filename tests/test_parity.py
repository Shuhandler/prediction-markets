"""Paper/live parity: the two modes must share the entire pipeline and
diverge only at the documented seams.  Guards the promotion path — if
paper behavior stops predicting live behavior, paper testing is worthless.
"""
import ast
from decimal import Decimal as D
from pathlib import Path

import arb_bot as ab
from conftest import build_stack, make_opp, order_outlay, snap
from mocks import MockFetcher, MockKalshiOrderClient, MockPolyOrderClient

# The complete, intentional set of places allowed to branch on live mode:
#   UnwindManager.__init__      — stores the flag
#   UnwindManager._submit_leg   — paper fill vs real order
#   UnwindManager._submit_unwind— paper sell vs real sell
#   UnwindManager._execute_locked — live-only spread re-verification
#   main                        — wiring/validation of clients
ALLOWED_LIVE_REFS = {
    "UnwindManager.__init__",
    "UnwindManager._submit_leg",
    "UnwindManager._submit_unwind",
    "UnwindManager._execute_locked",
    "main",
}


class _LiveRefVisitor(ast.NodeVisitor):
    def __init__(self):
        self.stack: list[str] = []
        self.hits: set[str] = set()

    def visit_ClassDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self._in_func(node)

    def visit_AsyncFunctionDef(self, node):
        self._in_func(node)

    def _in_func(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _record(self):
        self.hits.add(".".join(self.stack) if self.stack else "<module>")

    def visit_Attribute(self, node):
        if node.attr == "_live_mode":
            self._record()
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id == "live_mode":
            self._record()
        self.generic_visit(node)


def test_live_mode_branching_is_confined():
    source = Path(ab.__file__).read_text(encoding="utf-8")
    visitor = _LiveRefVisitor()
    visitor.visit(ast.parse(source))
    unexpected = visitor.hits - ALLOWED_LIVE_REFS
    assert not unexpected, (
        f"live/paper branching leaked into {sorted(unexpected)} — "
        "strategy, risk, and execution-validation code must be mode-agnostic"
    )


async def test_paper_and_perfect_live_produce_identical_orders(tmp_path):
    """With a perfectly-behaved mock exchange, the live path must produce
    the same order economics as the paper path."""
    opp = make_opp("evt-parity", k_depth=5, p_depth=5)

    paper = build_stack(tmp_path / "paper")
    paper_order = await paper.ex.submit(opp)

    poly = MockPolyOrderClient()
    poly.place_responses = [{"orderID": "P1", "status": "matched"}]
    kalshi = MockKalshiOrderClient()
    kalshi.place_responses = [
        {"order_id": "K1", "status": "executed", "remaining_count": 0},
    ]
    fetcher = MockFetcher(
        kalshi_snapshot=snap(yes={"ask": "0.40", "ask_size": "50"}),
    )
    live = build_stack(
        tmp_path / "live", live_mode=True,
        kalshi_client=kalshi, poly_client=poly, fetcher=fetcher,
    )
    live_order = await live.ex.submit(opp)

    assert paper_order.status == live_order.status == ab.OrderStatus.FILLED
    assert paper_order.filled_contracts == live_order.filled_contracts == 5
    assert paper_order.total_outlay == live_order.total_outlay
    assert paper_order.total_outlay == order_outlay(opp, 5)
    assert paper.portfolio.capital == live.portfolio.capital
    assert paper.pnl.daily_pnl == live.pnl.daily_pnl
