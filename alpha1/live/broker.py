"""
IBKR broker adapter for Alpha1 live trading.

Wraps ib_async.IB with Alpha1-specific order management:
  - bracket order placement (parent limit + TP + SL)
  - stop-loss modification for breakeven management
  - market order close (session-end exit)
  - account equity query

All methods are async or sync depending on ib_async's API.  ib_async callbacks
run synchronously in the asyncio event loop, so handlers must not block.

Bracket order transmit sequence (Adversarial Finding #10):
  IBKR requires the parent order to be submitted first, then children.  ib_async's
  bracketOrder() sets transmit=False on parent and first child, transmit=True on
  the stop-loss (last child).  Placing orders in the sequence parent → tp → sl
  triggers IBKR to process all three atomically.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alpha1.config.settings import LiveConfig

log = logging.getLogger("alpha1.live.broker")


class IBKRBroker:
    """
    Thin adapter over ib_async.IB for Alpha1's order management needs.

    Resolved contracts are cached after connect() so the engine never has to
    wait for contract resolution on the hot path.
    """

    def __init__(self, config: LiveConfig) -> None:
        from ib_async import IB

        self.config = config
        self.ib = IB()
        # symbol → qualified Contract; populated by connect()
        self._contracts: dict[str, object] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """
        Connect to TWS/IB Gateway and resolve contracts for all configured instruments.

        Raises RuntimeError if connection fails or any contract cannot be qualified.
        """
        from alpha1.live.contracts import resolve_contract

        log.info(
            "Connecting to IBKR at %s:%d (clientId=%d, paper=%s)",
            self.config.host,
            self.config.port,
            self.config.client_id,
            self.config.paper,
        )
        await self.ib.connectAsync(
            self.config.host,
            self.config.port,
            clientId=self.config.client_id,
        )
        log.info("Connected to IBKR.")

        # Resolve and cache all configured instrument contracts up-front.
        for symbol in self.config.instruments:
            self._contracts[symbol] = await resolve_contract(self.ib, symbol)
        log.info("All contracts resolved: %s", list(self._contracts.keys()))

    def disconnect(self) -> None:
        self.ib.disconnect()
        log.info("Disconnected from IBKR.")

    def get_contract(self, symbol: str):
        """Return the qualified contract for a symbol.  Must be called after connect()."""
        try:
            return self._contracts[symbol]
        except KeyError as exc:
            raise KeyError(
                f"Contract for {symbol!r} not resolved. Ensure connect() completed successfully before trading."
            ) from exc

    async def re_resolve_contract(self, symbol: str) -> None:
        """
        Re-resolve a futures contract to pick up front-month rolls.

        Call at midnight UTC for MYM / MNQ.  No-op for forex.
        """
        from alpha1.live.contracts import is_forex, resolve_contract

        if is_forex(symbol):
            return  # forex contracts don't roll
        self._contracts[symbol] = await resolve_contract(self.ib, symbol)
        log.info("Re-resolved contract for %s after roll check.", symbol)

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def place_bracket(
        self,
        symbol: str,
        direction: str,
        qty: float,
        limit_price: float,
        tp_price: float,
        sl_price: float,
    ) -> tuple:
        """
        Place a bracket order (limit entry + take-profit + stop-loss).

        Returns (parent_trade, tp_trade, sl_trade) — ib_async Trade objects that
        reflect order status and fill prices as IBKR updates them.

        Transmit sequence: parent (transmit=False) → tp (transmit=False) → sl
        (transmit=True).  The final sl.transmit=True triggers IBKR to process all
        three atomically.  This is handled by ib_async.IB.bracketOrder().
        """
        contract = self.get_contract(symbol)
        action = "BUY" if direction == "LONG" else "SELL"

        # bracketOrder returns BracketOrder(parent, takeProfit, stopLoss)
        bracket = self.ib.bracketOrder(action, qty, limit_price, tp_price, sl_price)

        # Place in order: parent first, then tp, then sl (transmit sequence).
        parent_trade = self.ib.placeOrder(contract, bracket.parent)
        tp_trade = self.ib.placeOrder(contract, bracket.takeProfit)
        sl_trade = self.ib.placeOrder(contract, bracket.stopLoss)

        log.info(
            "Placed bracket: %s %s %g @ %.5f  SL=%.5f  TP=%.5f  ordIds=(%d, %d, %d)",
            action,
            symbol,
            qty,
            limit_price,
            sl_price,
            tp_price,
            bracket.parent.orderId,
            bracket.takeProfit.orderId,
            bracket.stopLoss.orderId,
        )
        return parent_trade, tp_trade, sl_trade

    def cancel_order(self, trade) -> None:
        """Cancel a single order if it's not already terminal."""

        status = trade.orderStatus.status
        if status not in ("Filled", "Cancelled", "Inactive"):
            self.ib.cancelOrder(trade.order)
            log.debug("Cancelled order %d (was %s)", trade.order.orderId, status)

    def cancel_bracket(self, parent_trade, tp_trade, sl_trade) -> None:
        """Cancel all three legs of a bracket order."""
        for trade in (parent_trade, tp_trade, sl_trade):
            self.cancel_order(trade)

    def modify_stop(self, sl_trade, new_stop_price: float) -> None:
        """
        Move the stop-loss price (breakeven management).

        ib_async modifies the existing order in-place by re-submitting it with
        the updated auxPrice.  IBKR accepts the modification as long as the order
        is still active.
        """
        sl_trade.order.auxPrice = new_stop_price
        self.ib.placeOrder(sl_trade.contract, sl_trade.order)
        log.info(
            "Modified SL orderId=%d  new stop=%.5f",
            sl_trade.order.orderId,
            new_stop_price,
        )

    def close_position_market(self, symbol: str, direction: str, qty: float):
        """
        Close position with a market order (session-end exit).

        Returns a Trade object.  Caller must poll or await isDone().
        """
        from ib_async import MarketOrder

        contract = self.get_contract(symbol)
        action = "SELL" if direction == "LONG" else "BUY"
        order = MarketOrder(action, qty)
        trade = self.ib.placeOrder(contract, order)
        log.info("Market close: %s %s %g", action, symbol, qty)
        return trade

    # ------------------------------------------------------------------
    # Account / position queries
    # ------------------------------------------------------------------

    def get_equity(self) -> float:
        """
        Return current account net liquidation value in USD.

        Returns 0.0 if not available (e.g., called before first account update).
        IBKR pushes account updates periodically; the cached value may lag by a
        few seconds.
        """
        for v in self.ib.accountValues():
            if v.tag == "NetLiquidation" and v.currency == "USD":
                return float(v.value)
        log.warning("NetLiquidation not found in account values — returning 0.0")
        return 0.0

    def get_positions(self) -> list:
        """Return IBKR Position objects for the connected account."""
        return list(self.ib.positions())

    def get_open_orders(self) -> list:
        """Return currently open/pending orders from IBKR."""
        return list(self.ib.openOrders())

    async def request_current_time(self) -> float:
        """
        Fetch IBKR server time (Unix seconds).

        Use this instead of local time() for session boundary calculations to
        avoid clock drift issues.
        """
        return await self.ib.reqCurrentTimeAsync()

    # ------------------------------------------------------------------
    # Event subscriptions (engine subscribes to these)
    # ------------------------------------------------------------------

    @property
    def order_status_event(self):
        """Event fired when an order status changes.  Handler signature: (Trade) -> None."""
        return self.ib.orderStatusEvent

    @property
    def position_event(self):
        """Event fired when a position changes.  Handler signature: (Position) -> None."""
        return self.ib.positionEvent

    @property
    def error_event(self):
        """Event fired on IBKR errors.  Handler signature: (reqId, errorCode, msg, ...) -> None."""
        return self.ib.errorEvent
