"""
alpha1.live — Interactive Brokers live trading package.

Install with: pip install -e ".[live]"

Components:
  contracts  — IBKR contract factory (XAUUSD, MYM, MNQ)
  state      — SQLite-backed persistence (orders, positions, trade history)
  broker     — IBKRBroker: connect, place/cancel/modify orders, equity query
  feed       — LiveFeed: 5M bar streaming + rolling multi-TF DataFrames
  engine     — LiveEngine: signal loop, order lifecycle, breakeven, session exit
  server     — FastAPI dashboard: REST endpoints + WebSocket + HTML templates
"""

from alpha1.live.contracts import ContractSpec, get_what_to_show, is_forex, resolve_contract
from alpha1.live.state import (
    OpenPositionRecord,
    PendingOrderRecord,
    StateManager,
    TradeRecord,
)

__all__ = [
    "ContractSpec",
    "OpenPositionRecord",
    "PendingOrderRecord",
    "StateManager",
    "TradeRecord",
    "get_what_to_show",
    "is_forex",
    "resolve_contract",
]
