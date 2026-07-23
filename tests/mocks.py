"""Mock exchange clients, fetchers, and aiohttp doubles for failure injection.

Response scripting convention: `*_responses` lists are consumed with pop(0);
the final entry is reused once the list is down to one element, so a single
scripted response acts as a steady state.
"""
import asyncio
from decimal import Decimal as D


def _pop(responses, default=None):
    if not responses:
        return default if default is not None else {}
    if len(responses) == 1:
        return responses[0]
    return responses.pop(0)


class MockKalshiOrderClient:
    """Scriptable stand-in for KalshiOrderClient."""

    def __init__(self):
        self.place_calls: list[dict] = []
        self.place_responses: list[dict] = []
        self.get_responses: list[dict] = []
        self.orders_calls: list[dict] = []
        self.orders_responses: list[list] = []
        self.cancel_calls: list[str] = []
        self.cancel_responses: list[dict] = []

    async def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return _pop(self.place_responses)

    async def get_order(self, order_id):
        return _pop(self.get_responses)

    async def get_orders(self, **params):
        self.orders_calls.append(params)
        return _pop(self.orders_responses, default=[])

    async def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        return _pop(self.cancel_responses, default={})


class MockPolyOrderClient:
    """Scriptable stand-in for PolymarketOrderClient."""

    def __init__(self, token_id="TOK"):
        self._token = token_id
        self.place_calls: list[dict] = []
        self.place_responses: list[dict] = []
        self.get_responses: list[dict] = []
        self.cancel_calls: list[str] = []
        self.token_balance = 0.0

    def resolve_token_id(self, condition_id, side):
        return self._token

    async def get_token_balance(self, token_id):
        # 0.0 mirrors the real client's "unknown" result: callers treat
        # a non-positive balance as advisory and skip capping.
        return self.token_balance

    async def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return _pop(self.place_responses)

    async def get_order(self, order_id):
        return _pop(self.get_responses)

    async def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        return {}


class MockFetcher:
    """Stand-in for MarketFetcher inside UnwindManager.

    kalshi_snapshot: returned by fetch_kalshi (unused by the execute
        path since Leg 2 reservation pricing removed the refetch;
        kalshi_calls lets tests assert it stays that way).
    poly_bids: bid levels returned by _fetch_poly_book (unwind sizing).
    """

    def __init__(self, kalshi_snapshot=None, poly_bids=None):
        self.kalshi_snapshot = kalshi_snapshot
        self.poly_bids = poly_bids if poly_bids is not None else []
        self.kalshi_calls: list[tuple] = []

    async def fetch_kalshi(self, ticker, **kwargs):
        self.kalshi_calls.append((ticker, kwargs))
        return self.kalshi_snapshot

    async def _fetch_poly_book(self, token_id):
        return {"bids": self.poly_bids}, False


class FakeResponse:
    """aiohttp response double usable as an async context manager."""

    def __init__(self, status, json_data=None, headers=None):
        self.status = status
        self._json = json_data
        self.headers = headers or {}

    async def json(self, content_type=None):
        return self._json

    async def text(self):
        return str(self._json)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class RaisingCM:
    """Context manager that raises on entry (connection error / timeout)."""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """aiohttp.ClientSession double: serves scripted responses in order."""

    closed = False

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            return RaisingCM(item)
        return item
