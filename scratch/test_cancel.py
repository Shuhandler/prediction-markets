import inspect
from py_clob_client_v2 import ClobClient

client = ClobClient("https://clob.polymarket.com")
print("cancel_order signature:", inspect.signature(client.cancel_order))
print("cancel_all signature:", inspect.signature(client.cancel_all))
