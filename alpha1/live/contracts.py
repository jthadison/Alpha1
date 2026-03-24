"""
IBKR contract factory for Alpha1 live trading instruments.

Maps Alpha1 symbol names to qualified ib_async Contract objects.

Key design decisions:
  - Futures use ContFuture so IBKR resolves the current front-month automatically.
    Re-resolve at midnight UTC to catch quarterly rolls (Mar/Jun/Sep/Dec).
  - XAUUSD uses Forex (secType='CASH', exchange='IDEALPRO'). Some US retail
    accounts require secType='CMDTY' — qualification failure surfaces the issue
    with an actionable error message.
  - whatToShow differs by instrument type: MIDPOINT for forex/metals, TRADES for
    futures. Using TRADES for forex returns wider bid/ask spreads in historical
    data, which overstates costs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("alpha1.live.contracts")

# ---------------------------------------------------------------------------
# Instrument metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractSpec:
    """Metadata needed by the feed and broker for a given instrument."""

    what_to_show: str  # MIDPOINT (forex/metals) or TRADES (futures)
    is_forex: bool  # True → bar columns use MIDPOINT semantics


_CONTRACT_SPECS: dict[str, ContractSpec] = {
    "XAUUSD": ContractSpec(what_to_show="MIDPOINT", is_forex=True),
    "MYM": ContractSpec(what_to_show="TRADES", is_forex=False),
    "MNQ": ContractSpec(what_to_show="TRADES", is_forex=False),
}


def is_forex(symbol: str) -> bool:
    """Return True if symbol is a forex/metals instrument (not a futures contract)."""
    spec = _CONTRACT_SPECS.get(symbol)
    if spec is None:
        raise ValueError(f"Unknown live trading symbol: {symbol!r}")
    return spec.is_forex


def get_what_to_show(symbol: str) -> str:
    """Return the IBKR whatToShow value for historical/realtime bars."""
    spec = _CONTRACT_SPECS.get(symbol)
    if spec is None:
        raise ValueError(f"No contract spec for symbol {symbol!r}")
    return spec.what_to_show


def get_contract_spec(symbol: str) -> ContractSpec:
    """Return the full ContractSpec for a symbol."""
    spec = _CONTRACT_SPECS.get(symbol)
    if spec is None:
        raise ValueError(f"No contract spec for symbol {symbol!r}")
    return spec


# ---------------------------------------------------------------------------
# Contract construction
# ---------------------------------------------------------------------------


def _build_unqualified(symbol: str):  # returns ib_async Contract subclass
    """
    Build an unqualified contract object for the given symbol.

    The returned object is not yet validated against IBKR — callers must pass it
    through qualifyContractsAsync before use.
    """
    # Import deferred so the module can be imported without ib_async installed
    # (needed for test_live_contracts.py which tests definitions independently).
    try:
        from ib_async import ContFuture, Forex
    except ImportError as exc:
        raise ImportError("ib_async is required for live trading. Install with: pip install -e '.[live]'") from exc

    if symbol == "XAUUSD":
        # Spot gold: XAU/USD, secType='CASH', exchange='IDEALPRO'.
        # Forex("XAUUSD") is ib_async shorthand for Forex(pair='XAUUSD').
        return Forex("XAUUSD")

    elif symbol == "MYM":
        # Micro Dow Jones: CBOT, continuous front-month.
        return ContFuture("MYM", exchange="CBOT")

    elif symbol == "MNQ":
        # Micro Nasdaq-100: CME, continuous front-month.
        return ContFuture("MNQ", exchange="CME")

    else:
        raise ValueError(
            f"No IBKR contract definition for symbol {symbol!r}. Add it to contracts.py to trade this instrument live."
        )


async def resolve_contract(ib, symbol: str):  # ib: IB
    """
    Resolve and qualify the IBKR contract for the given symbol.

    For futures (MYM, MNQ):
        ContFuture triggers IBKR to resolve to the currently active front-month.
        The returned contract carries the concrete conId and expiry date.
        Call this again at midnight UTC to catch quarterly rolls.

    For forex/metals (XAUUSD):
        Forex contract is qualified directly; no expiry resolution needed.

    Raises:
        RuntimeError — if IBKR returns no qualified contract.  This happens when
            the account does not have permissions for the product or the exchange
            is unavailable.  The error message includes account-specific advice.
    """
    contract = _build_unqualified(symbol)
    qualified = await ib.qualifyContractsAsync(contract)

    # ib_async returns [None] (not []) when qualification fails — guard both cases.
    if not qualified or qualified[0] is None or not getattr(qualified[0], "conId", None):
        account_note = (
            "XAUUSD: some US retail accounts require secType='CMDTY' — "
            "contact IBKR support to enable spot gold trading."
            if symbol == "XAUUSD"
            else f"{symbol}: verify market data subscriptions and trading permissions."
        )
        raise RuntimeError(f"Could not qualify IBKR contract for {symbol!r}. {account_note}")

    resolved = qualified[0]
    expiry = getattr(resolved, "lastTradeDateOrContractMonth", "N/A")
    log.info(
        "Resolved contract: %s -> conId=%s expiry=%s exchange=%s",
        symbol,
        resolved.conId,
        expiry,
        getattr(resolved, "exchange", "?"),
    )
    return resolved
