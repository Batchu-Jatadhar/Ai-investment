"""Zerodha Kite Connect adapter - READ-ONLY market data.

This package contains no order operations and none may be added while the
project is in the market-data phase. There is no place_order, modify_order or
cancel_order anywhere in it, and a test asserts that.
"""

from app.adapters.zerodha.client import ZerodhaRestClient, ZerodhaSession
from app.adapters.zerodha.errors import (
    ZerodhaAuthError,
    ZerodhaError,
    ZerodhaNotConfiguredError,
)
from app.adapters.zerodha.provider import ReconnectPolicy, ZerodhaMarketDataProvider

__all__ = [
    "ReconnectPolicy",
    "ZerodhaAuthError",
    "ZerodhaError",
    "ZerodhaMarketDataProvider",
    "ZerodhaNotConfiguredError",
    "ZerodhaRestClient",
    "ZerodhaSession",
]
