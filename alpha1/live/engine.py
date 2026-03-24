"""
Live trading orchestrator for Alpha1.

LiveEngine receives bar-close callbacks from LiveFeed, calls generate_signals()
(the exact same function used in backtesting), places IBKR bracket orders for new
signals, and manages their lifecycle through fill, breakeven, and exit.

Design invariants:
  - One position per instrument maximum.  A second signal is ignored while a
    position is open.
  - Signal dedup key is (timestamp, direction, entry_price) — not timestamp alone.
    Two FVGs can form on the same bar (one bullish, one bearish).
    (Adversarial Finding #2)
  - Futures position size is floored to a whole number of contracts.
    (Adversarial Finding #3)
  - Session-end exit checks `if symbol not in self._positions` before placing the
    market order to guard against the SL/TP-fills-simultaneously-with-timer race.
    (Adversarial Finding #8)
  - ib_async and uvicorn share the same asyncio event loop.  All async calls use
    asyncio.ensure_future() / asyncio.create_task() rather than spawning a new
    loop.  (Adversarial Finding #9)

Post-launch fixes:
  Fix 1 — State recovery: self._positions is populated from IBKR on restart so
    the engine never stacks new orders on top of existing positions.
  Fix 2 — Signal recency filter: signals older than limit_order_timeout_bars are
    discarded before placement, preventing stale historical FVGs from being traded.
  Fix 3 — Cancel/fill guard: parent fill status is checked before cancel_bracket;
    if already filled, the pending order transitions to a position rather than the
    bracket being torn down naked.
  Fix 4 — Bracket child preservation: follows from Fix 3 — when a parent fills,
    _handle_fill is called with sl/tp still alive, so the bracket protection remains.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pandas as pd

if TYPE_CHECKING:
    from alpha1.config.settings import StrategyConfig
    from alpha1.live.broker import IBKRBroker
    from alpha1.live.feed import LiveFeed
    from alpha1.live.state import StateManager
    from alpha1.strategy.signals import Signal

log = logging.getLogger("alpha1.live.engine")

# ---------------------------------------------------------------------------
# Internal runtime-only dataclasses (not persisted directly; StateManager
# stores serialisable records instead)
# ---------------------------------------------------------------------------


@dataclass
class PendingOrder:
    """Runtime representation of a submitted bracket limit order."""

    id: str
    symbol: str
    signal: Signal
    size: float
    actual_stop: float  # stop_price + buffer
    parent_trade: object  # ib_async Trade
    tp_trade: object
    sl_trade: object
    placed_at: datetime


@dataclass
class OpenPosition:
    """Runtime representation of an open (filled) position."""

    id: str
    symbol: str
    direction: str
    entry_price: float
    entry_time: datetime
    stop_price: float  # current stop (may move to breakeven)
    initial_stop_price: float  # original stop; used for R calculation
    target_price: float
    size: float
    sl_trade: object | None  # ib_async Trade; None for recovered positions without known bracket
    tp_trade: object | None  # ib_async Trade; None for recovered positions without known bracket
    signal_timestamp: pd.Timestamp | None = None
    limit_entry_price: float = 0.0


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class LiveEngine:
    """
    Orchestrates live trading across multiple instruments.

    Call start() to connect and begin receiving bar updates.
    Call run_forever() inside an asyncio.gather() alongside the web server.
    """

    def __init__(
        self,
        broker: IBKRBroker,
        feed: LiveFeed,
        state: StateManager,
        config: StrategyConfig,
    ) -> None:
        self.broker = broker
        self.feed = feed
        self.state = state
        self.config = config

        # Dedup: set of (timestamp, direction, entry_price) tuples already processed.
        # Keyed by symbol so signals from different instruments don't collide.
        # (Adversarial Finding #2)
        self._known_signals: dict[str, set[tuple]] = {}

        # Pending limit orders per instrument (max one position, but multiple
        # pending orders allowed up to max_trades_per_session)
        self._pending: dict[str, list[PendingOrder]] = {}

        # Open positions: at most one per instrument
        self._positions: dict[str, OpenPosition] = {}

        # Asyncio tasks for scheduled session-end exits
        self._session_exit_tasks: dict[str, asyncio.Task] = {}

        # Listeners for WebSocket broadcast (set by server.py)
        self._event_callbacks: list[Callable[[dict], None]] = []

        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to IBKR, initialise state DB, subscribe to bar feeds."""
        await self.broker.connect()
        await self.state.init_db(self.config.live.db_path)
        await self._recover_state()

        # Subscribe to IBKR events.
        # Fetch the event object first; then subscribe via += on the local reference.
        # Doing `self.broker.order_status_event += handler` fails because the
        # property has no setter — Python tries to assign the result back.
        order_event = self.broker.order_status_event
        order_event += self._on_order_status_sync

        # Survive nightly IB Gateway restarts: log disconnect/reconnect but keep
        # the process alive so the web server stays up and state is preserved.
        self.broker.ib.disconnectedEvent += self._on_ibkr_disconnected
        self.broker.ib.connectedEvent += self._on_ibkr_reconnected

        # Register bar-close callback and subscribe to all instruments concurrently.
        # asyncio.gather sends all historical data requests at the same time, which
        # avoids IBKR sequential-request pacing that causes the second request to
        # stall.  return_exceptions=True means one instrument failure can't abort
        # the others.
        self.feed.on_bar_close(self._on_new_bar)
        results = await asyncio.gather(
            *[self.feed.subscribe(s) for s in self.config.live.instruments],
            return_exceptions=True,
        )
        for symbol, result in zip(self.config.live.instruments, results, strict=False):
            if isinstance(result, BaseException):
                log.exception(
                    "Failed to subscribe to %s — it will not receive bar updates.",
                    symbol,
                    exc_info=result,
                )

        self._running = True
        log.info("LiveEngine started.  Instruments: %s", self.config.live.instruments)

    async def stop(self) -> None:
        """Graceful shutdown: cancel pending tasks, disconnect."""
        self._running = False
        for task in self._session_exit_tasks.values():
            task.cancel()
        self.broker.disconnect()
        await self.state.close()
        log.info("LiveEngine stopped.")

    async def run_forever(self) -> None:
        """Keep the asyncio loop alive. Survives IBKR disconnects — never raises."""
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            await self.stop()
        except Exception:
            log.exception("Unexpected error in run_forever — shutting down engine.")
            self._running = False
            # Best-effort cleanup; don't re-raise so uvicorn sibling task stays up
            with contextlib.suppress(Exception):
                await self.stop()

    # ------------------------------------------------------------------
    # IBKR connection lifecycle events
    # ------------------------------------------------------------------

    def _on_ibkr_disconnected(self) -> None:
        """
        Fired by ib_async when the TCP connection to IB Gateway drops.

        This is expected every weeknight at ~23:45 ET for IBKR's daily server
        restart.  We log it prominently but do NOT shut down: the web server
        keeps serving, SQLite state is intact, and ib_async will reconnect
        automatically once Gateway comes back online (~5 minutes).
        """
        log.warning(
            "IBKR connection lost (nightly restart or network issue). "
            "Web dashboard remains up. Will reconnect automatically."
        )
        self._emit_event("ibkr_disconnected", {"reason": "peer_closed"})

    def _on_ibkr_reconnected(self) -> None:
        """Fired by ib_async when the API connection is re-established."""
        log.info("IBKR connection restored.")
        self._emit_event("ibkr_reconnected", {})

    def on_event(self, callback: Callable[[dict], None]) -> None:
        """Register a listener for trading events (used by WebSocket server)."""
        self._event_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Bar-close handler (entry point for each new 5M bar)
    # ------------------------------------------------------------------

    def _on_new_bar(self, symbol: str, data_dict: dict[str, pd.DataFrame]) -> None:
        """
        Synchronous callback from LiveFeed.  Dispatch async processing so the
        ib_async event loop isn't blocked.
        """
        asyncio.ensure_future(self._process_bar(symbol, data_dict))  # noqa: RUF006

    async def _process_bar(self, symbol: str, data_dict: dict[str, pd.DataFrame]) -> None:
        """Core trading logic executed on each completed 5M bar."""
        from alpha1.config.instruments import INSTRUMENT_REGISTRY
        from alpha1.strategy.signals import generate_signals

        if symbol not in INSTRUMENT_REGISTRY:
            log.error("Unknown instrument %s — skipping bar.", symbol)
            return

        instrument = INSTRUMENT_REGISTRY[symbol]
        df_5m = data_dict["5min"]
        if len(df_5m) < 2:
            return

        current_time = df_5m.index[-1]

        # 1. Generate signals using the exact same function as the backtest.
        all_signals = generate_signals(data_dict, self.config)

        # Skip signal detection if account equity is unavailable (IBKR reconnect window).
        # Don't add to _known_signals — allow retry on the next bar.
        if self.broker.get_equity() <= 0:
            log.warning(
                "%s: equity=0.0 (IBKR reconnecting?); deferring %d signal(s) to next bar.",
                symbol,
                len(all_signals),
            )
            all_signals = []

        # 2. Deduplicate: find signals not yet processed for this instrument.
        known = self._known_signals.setdefault(symbol, set())
        new_signals = []
        for s in all_signals:
            key = (s.timestamp, s.direction, s.entry_price)
            if key not in known:
                known.add(key)
                new_signals.append(s)

        # Fix 2: Recency filter — discard signals older than limit_order_timeout_bars.
        # A 5M FVG formed more than (timeout_bars x 5) minutes ago has no business
        # being placed: market structure has moved on, and the price is unlikely to
        # retrace cleanly to a stale midpoint.  This eliminates the stale-historical-
        # data fills that contaminated the first day of paper trading.
        timeout_bars = self.config.entry.limit_order_timeout_bars
        # df_5m.index[-2] is the most recently completed bar.
        cutoff = df_5m.index[-2] - pd.Timedelta(minutes=5 * timeout_bars)
        stale = [s for s in new_signals if s.timestamp < cutoff]
        if stale:
            log.info(
                "%s: dropped %d stale signal(s) (older than %d bars / %d min).",
                symbol,
                len(stale),
                timeout_bars,
                timeout_bars * 5,
            )
        new_signals = [s for s in new_signals if s.timestamp >= cutoff]

        # 3. Place bracket orders for new signals (respecting position/session limits).
        for signal in new_signals:
            # Only one open position per instrument.
            if symbol in self._positions:
                log.debug("%s: position open, skipping signal at %s", symbol, signal.timestamp)
                break

            pending_count = len(self._pending.get(symbol, []))
            if pending_count >= self.config.risk.max_trades_per_session:
                log.debug(
                    "%s: max pending orders (%d) reached, skipping signal.",
                    symbol,
                    self.config.risk.max_trades_per_session,
                )
                break

            await self._place_signal_order(symbol, signal, instrument)

        # 4. Manage pending orders (cancel on invalidation / timeout).
        await self._manage_pending_orders(symbol, df_5m)

        # 5. Manage open position (breakeven).
        if symbol in self._positions:
            await self._manage_position(symbol, df_5m)

        self._emit_event("bar_update", {"symbol": symbol, "time": str(current_time)})

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def _place_signal_order(self, symbol: str, signal: Signal, instrument) -> None:
        """Convert an Alpha1 Signal into an IBKR bracket order."""
        equity = self.broker.get_equity()
        if equity <= 0:
            log.warning("%s: account equity=%.2f, skipping order placement.", symbol, equity)
            return

        # Apply stop buffer (ticks outside the FVG boundary)
        tick = instrument.tick_size
        buffer = self.config.exit.stop_buffer_ticks * tick

        if signal.direction == "LONG":
            actual_stop = signal.stop_price - buffer
            risk_dist = signal.entry_price - actual_stop
        else:
            actual_stop = signal.stop_price + buffer
            risk_dist = actual_stop - signal.entry_price

        if risk_dist <= 0:
            log.warning("%s: non-positive risk distance %.5f, skipping signal.", symbol, risk_dist)
            return

        risk_amount = equity * (self.config.risk.risk_per_trade_pct / 100.0)
        loss_per_unit = risk_dist * instrument.point_value
        if loss_per_unit <= 0:
            return

        size = risk_amount / loss_per_unit

        # Futures require whole contracts (Adversarial Finding #3).
        # Forex/metals can use fractional lot sizes.
        if instrument.pip_size is None:
            size = int(size)  # floor to whole contracts
            if size < 1:
                log.info(
                    "%s: computed size < 1 contract (equity too small for risk), skipping.",
                    symbol,
                )
                return

        try:
            parent_trade, tp_trade, sl_trade = self.broker.place_bracket(
                symbol,
                signal.direction,
                size,
                signal.entry_price,
                signal.target_price,
                actual_stop,
            )
        except Exception:
            log.exception("%s: failed to place bracket order.", symbol)
            return

        order_id = str(uuid4())
        pending = PendingOrder(
            id=order_id,
            symbol=symbol,
            signal=signal,
            size=size,
            actual_stop=actual_stop,
            parent_trade=parent_trade,
            tp_trade=tp_trade,
            sl_trade=sl_trade,
            placed_at=datetime.now(UTC),
        )
        self._pending.setdefault(symbol, []).append(pending)

        # Persist to SQLite
        from alpha1.live.state import PendingOrderRecord

        await self.state.save_pending_order(
            PendingOrderRecord(
                id=order_id,
                instrument=symbol,
                direction=signal.direction,
                limit_price=signal.entry_price,
                stop_price=actual_stop,
                target_price=signal.target_price,
                cancel_price=signal.cancel_price,
                signal_timestamp=str(signal.timestamp),
                placed_at=pending.placed_at.isoformat(),
                formed_bar_time=str(signal.timestamp),
                status="SUBMITTED",
                ibkr_parent_id=parent_trade.order.orderId,
                ibkr_tp_id=tp_trade.order.orderId,
                ibkr_sl_id=sl_trade.order.orderId,
            )
        )

        self._emit_event(
            "order_placed",
            {
                "symbol": symbol,
                "direction": signal.direction,
                "entry": signal.entry_price,
                "stop": actual_stop,
                "target": signal.target_price,
                "size": size,
                "order_id": order_id,
            },
        )

    # ------------------------------------------------------------------
    # Pending order management (cancel conditions)
    # ------------------------------------------------------------------

    async def _manage_pending_orders(self, symbol: str, df_5m: pd.DataFrame) -> None:
        """
        Cancel pending orders that are timed out or whose FVG has been invalidated.

        Also transitions filled orders to open positions.
        """
        current_bar = df_5m.iloc[-1]
        still_pending: list[PendingOrder] = []

        for po in self._pending.get(symbol, []):
            sig = po.signal

            # -- Timeout: cancel if limit order not filled within N bars --
            bars_since = int((df_5m.index >= sig.timestamp).sum())
            if bars_since > self.config.entry.limit_order_timeout_bars:
                log.info("%s: order %s timed out after %d bars.", symbol, po.id, bars_since)
                # Fix 3/4: if the parent filled in the race window, keep the bracket
                # alive by transitioning to an open position rather than cancelling.
                if po.parent_trade.orderStatus.status == "Filled":
                    await self._handle_fill(symbol, po)
                else:
                    self.broker.cancel_bracket(po.parent_trade, po.tp_trade, po.sl_trade)
                    await asyncio.sleep(0)  # allow ib_async to process incoming fill event
                    if po.parent_trade.orderStatus.status == "Filled":
                        log.warning(
                            "%s: order %s filled during cancel window — position created WITHOUT bracket. "
                            "Add stop protection manually in IBKR.",
                            symbol,
                            po.id,
                        )
                        await self._handle_fill_naked(symbol, po)
                    else:
                        await self.state.update_pending_order_status(po.id, "EXPIRED")
                        self._emit_event("order_expired", {"symbol": symbol, "id": po.id})
                continue

            # -- FVG invalidation: cancel if price breaches the cancel level --
            if sig.direction == "LONG" and current_bar["low"] < sig.cancel_price:
                log.info(
                    "%s: order %s cancelled — FVG breached downward (low=%.5f < cancel=%.5f).",
                    symbol,
                    po.id,
                    current_bar["low"],
                    sig.cancel_price,
                )
                if po.parent_trade.orderStatus.status == "Filled":
                    await self._handle_fill(symbol, po)
                else:
                    self.broker.cancel_bracket(po.parent_trade, po.tp_trade, po.sl_trade)
                    await asyncio.sleep(0)  # allow ib_async to process incoming fill event
                    if po.parent_trade.orderStatus.status == "Filled":
                        log.warning(
                            "%s: order %s filled during cancel window — position created WITHOUT bracket. "
                            "Add stop protection manually in IBKR.",
                            symbol,
                            po.id,
                        )
                        await self._handle_fill_naked(symbol, po)
                    else:
                        await self.state.update_pending_order_status(po.id, "CANCELLED")
                        self._emit_event(
                            "order_cancelled",
                            {"symbol": symbol, "id": po.id, "reason": "FVG_BREACHED"},
                        )
                continue

            if sig.direction == "SHORT" and current_bar["high"] > sig.cancel_price:
                log.info(
                    "%s: order %s cancelled — FVG breached upward (high=%.5f > cancel=%.5f).",
                    symbol,
                    po.id,
                    current_bar["high"],
                    sig.cancel_price,
                )
                if po.parent_trade.orderStatus.status == "Filled":
                    await self._handle_fill(symbol, po)
                else:
                    self.broker.cancel_bracket(po.parent_trade, po.tp_trade, po.sl_trade)
                    await asyncio.sleep(0)  # allow ib_async to process incoming fill event
                    if po.parent_trade.orderStatus.status == "Filled":
                        log.warning(
                            "%s: order %s filled during cancel window — position created WITHOUT bracket. "
                            "Add stop protection manually in IBKR.",
                            symbol,
                            po.id,
                        )
                        await self._handle_fill_naked(symbol, po)
                    else:
                        await self.state.update_pending_order_status(po.id, "CANCELLED")
                        self._emit_event(
                            "order_cancelled",
                            {"symbol": symbol, "id": po.id, "reason": "FVG_BREACHED"},
                        )
                continue

            # -- Filled: parent limit order executed by IBKR --
            parent_status = po.parent_trade.orderStatus.status
            if parent_status == "Filled":
                await self._handle_fill(symbol, po)
                # Don't add to still_pending — it's now an open position
                continue

            still_pending.append(po)

        self._pending[symbol] = still_pending

    # ------------------------------------------------------------------
    # Fill → open position transition
    # ------------------------------------------------------------------

    async def _handle_fill(self, symbol: str, po: PendingOrder, *, naked: bool = False) -> None:
        """
        Handle limit order fill: transition from pending order to open position.

        Records fill price and schedules session-end exit if configured.
        Pass naked=True when the bracket children were already cancelled (TOCTOU race).
        """
        # Guard: if we somehow receive two fills for the same symbol, the second
        # order's contracts are now live at IBKR with no tracking.  Close them
        # immediately and record FILLED (not CANCELLED) — the order did execute.
        if symbol in self._positions:
            log.critical(
                "%s: duplicate fill on order %s (%.1f contracts) while position already tracked. "
                "Closing orphaned contracts via market order. "
                "Check IBKR account for residual exposure.",
                symbol,
                po.id,
                po.size,
            )
            # Close the orphaned filled contracts immediately.
            if not naked:
                self.broker.cancel_order(po.tp_trade)
                self.broker.cancel_order(po.sl_trade)
            self.broker.close_position_market(symbol, po.signal.direction, po.size)
            # Mark FILLED (not CANCELLED) — the parent order did execute.
            await self.state.update_pending_order_status(po.id, "FILLED")
            self._emit_event(
                "duplicate_fill_closed",
                {"symbol": symbol, "id": po.id, "size": po.size},
            )
            return

        fill_price = po.parent_trade.orderStatus.avgFillPrice
        fill_time = datetime.now(UTC)

        sl_trade = None if naked else po.sl_trade
        tp_trade = None if naked else po.tp_trade

        position = OpenPosition(
            id=po.id,
            symbol=symbol,
            direction=po.signal.direction,
            entry_price=fill_price,
            entry_time=fill_time,
            stop_price=po.actual_stop,
            initial_stop_price=po.actual_stop,
            target_price=po.signal.target_price,
            size=po.size,
            sl_trade=sl_trade,
            tp_trade=tp_trade,
            signal_timestamp=po.signal.timestamp,
            limit_entry_price=po.signal.entry_price,
        )
        self._positions[symbol] = position

        from alpha1.live.state import OpenPositionRecord

        await self.state.save_open_position(
            OpenPositionRecord(
                id=position.id,
                instrument=symbol,
                direction=position.direction,
                entry_price=fill_price,
                entry_time=fill_time.isoformat(),
                stop_price=position.stop_price,
                initial_stop_price=position.initial_stop_price,
                target_price=position.target_price,
                size=position.size,
                ibkr_sl_id=sl_trade.order.orderId if sl_trade is not None else None,
                ibkr_tp_id=tp_trade.order.orderId if tp_trade is not None else None,
            )
        )
        await self.state.update_pending_order_status(po.id, "FILLED")

        # Schedule session-end exit if configured
        if self.config.exit.close_at_session_end:
            session_end = self._next_session_end(fill_time)
            if session_end is not None:
                self._session_exit_tasks[symbol] = asyncio.create_task(self._session_end_exit(symbol, session_end))

        slippage = fill_price - po.signal.entry_price
        log.info(
            "%s: limit order filled @ %.5f (limit=%.5f, slippage=%.5f).",
            symbol,
            fill_price,
            po.signal.entry_price,
            slippage,
        )
        self._emit_event(
            "order_filled",
            {
                "symbol": symbol,
                "direction": po.signal.direction,
                "limit_price": po.signal.entry_price,
                "fill_price": fill_price,
                "slippage": slippage,
                "size": po.size,
            },
        )

    async def _handle_fill_naked(self, symbol: str, po: PendingOrder) -> None:
        """Fill handler for TOCTOU race: bracket was cancelled before fill was noticed."""
        await self._handle_fill(symbol, po, naked=True)

    # ------------------------------------------------------------------
    # Breakeven management
    # ------------------------------------------------------------------

    async def _manage_position(self, symbol: str, df_5m: pd.DataFrame) -> None:
        """Move stop to breakeven once price reaches config.exit.breakeven_at_r x R."""
        pos = self._positions[symbol]
        be_r = self.config.exit.breakeven_at_r
        if be_r <= 0:
            return  # breakeven disabled

        current_bar = df_5m.iloc[-1]
        risk = abs(pos.entry_price - pos.initial_stop_price)
        if risk <= 0:
            return

        moved = False
        if pos.direction == "LONG":
            current_r = (current_bar["high"] - pos.entry_price) / risk
            # Only move once (stop must be below entry; moving to entry is breakeven)
            if current_r >= be_r and pos.stop_price < pos.entry_price:
                pos.stop_price = pos.entry_price
                moved = True
        else:
            current_r = (pos.entry_price - current_bar["low"]) / risk
            if current_r >= be_r and pos.stop_price > pos.entry_price:
                pos.stop_price = pos.entry_price
                moved = True

        if moved:
            if pos.sl_trade is not None:
                self.broker.modify_stop(pos.sl_trade, pos.entry_price)
            await self.state.update_stop_price(pos.id, pos.entry_price)
            log.info(
                "%s: breakeven stop set @ %.5f (achieved %.2fR).",
                symbol,
                pos.entry_price,
                current_r,
            )
            self._emit_event("breakeven_set", {"symbol": symbol, "r_achieved": current_r})

    # ------------------------------------------------------------------
    # Session-end exit
    # ------------------------------------------------------------------

    def _next_session_end(self, reference_time: datetime) -> datetime | None:
        """
        Return the next London or NY session end time after reference_time.

        Uses config session times (stored in London local time) and converts
        to UTC.  Returns None if no session end is in the future today.
        """
        try:
            from zoneinfo import ZoneInfo

            london_tz = ZoneInfo("Europe/London")
            london_now = reference_time.astimezone(london_tz)
            today = london_now.date()

            session_cfg = self.config.session
            for attr in ("london_end", "ny_end"):
                t = session_cfg.get_time(attr)
                candidate = datetime(
                    today.year,
                    today.month,
                    today.day,
                    t.hour,
                    t.minute,
                    tzinfo=london_tz,
                )
                candidate_utc = candidate.astimezone(UTC)
                if candidate_utc > reference_time:
                    return candidate_utc

            return None
        except Exception:
            log.exception("Error computing next session end.")
            return None

    async def _session_end_exit(self, symbol: str, exit_time: datetime) -> None:
        """
        Sleep until session end, then close open position via market order.

        Race condition guard: check `if symbol not in self._positions` immediately
        before placing the market order — SL/TP may have fired while we slept.
        (Adversarial Finding #8)
        """
        delay = (exit_time - datetime.now(UTC)).total_seconds()
        if delay > 0:
            log.info(
                "%s: session exit scheduled in %.0f seconds at %s.",
                symbol,
                delay,
                exit_time.isoformat(),
            )
            await asyncio.sleep(delay)

        # Guard: SL or TP may have filled while we were sleeping
        if symbol not in self._positions:
            log.info("%s: session exit timer fired but position already closed.", symbol)
            return

        pos = self._positions[symbol]
        log.info("%s: session end — cancelling bracket and closing position.", symbol)

        # Cancel remaining bracket legs
        if pos.sl_trade is not None:
            self.broker.cancel_order(pos.sl_trade)
        if pos.tp_trade is not None:
            self.broker.cancel_order(pos.tp_trade)

        # Close via market order and wait for fill
        close_trade = self.broker.close_position_market(symbol, pos.direction, pos.size)
        # Poll for fill (market order should fill within seconds)
        for _ in range(60):  # 6-second timeout, then give up and log
            if close_trade.isDone():
                break
            await asyncio.sleep(0.1)

        if close_trade.isDone():
            exit_price = close_trade.orderStatus.avgFillPrice
            await self._record_completed_trade(pos, exit_price, "TIME_EXIT")
            del self._positions[symbol]
            self._emit_event(
                "position_closed",
                {"symbol": symbol, "reason": "SESSION_END", "exit_price": exit_price},
            )
        else:
            log.critical(
                "%s: market close order not filled after 6s — MANUAL INTERVENTION REQUIRED. "
                "Position remains tracked; no new orders will be placed until manually resolved.",
                symbol,
            )
            self._emit_event(
                "manual_intervention_required",
                {"symbol": symbol, "reason": "SESSION_END_TIMEOUT"},
            )

    # ------------------------------------------------------------------
    # IBKR order status handler (SL / TP fills)
    # ------------------------------------------------------------------

    def _on_order_status_sync(self, trade) -> None:
        """
        Synchronous IBKR order status event handler.

        ib_async fires events synchronously inside the asyncio loop.  We dispatch
        async work via ensure_future to avoid blocking the event loop.
        """
        asyncio.ensure_future(self._on_order_status(trade))  # noqa: RUF006

    async def _on_order_status(self, trade) -> None:
        """Handle SL or TP fill for an open position."""
        if not trade.isDone():
            return  # partial fill or status update, not terminal

        order_id = trade.order.orderId

        for symbol, pos in list(self._positions.items()):
            # Null guard: sl_trade/tp_trade are None for positions recovered without
            # a known bracket (e.g. crash recovery).  Skip the orderId comparison.
            sl_id = pos.sl_trade.order.orderId if pos.sl_trade is not None else None
            tp_id = pos.tp_trade.order.orderId if pos.tp_trade is not None else None

            if order_id == sl_id:
                exit_price = trade.orderStatus.avgFillPrice
                is_breakeven = abs(pos.stop_price - pos.entry_price) < pos.initial_stop_price * 1e-8
                reason = "BREAKEVEN" if is_breakeven else "STOP_LOSS"
                log.info("%s: SL filled @ %.5f (%s).", symbol, exit_price, reason)
                # Cancel the TP leg
                if pos.tp_trade is not None:
                    self.broker.cancel_order(pos.tp_trade)
                await self._record_completed_trade(pos, exit_price, reason)
                del self._positions[symbol]
                self._cancel_session_exit(symbol)
                self._emit_event(
                    "position_closed",
                    {"symbol": symbol, "reason": reason, "exit_price": exit_price},
                )
                break

            elif order_id == tp_id:
                exit_price = trade.orderStatus.avgFillPrice
                log.info("%s: TP filled @ %.5f.", symbol, exit_price)
                # Cancel the SL leg
                if pos.sl_trade is not None:
                    self.broker.cancel_order(pos.sl_trade)
                await self._record_completed_trade(pos, exit_price, "TARGET")
                del self._positions[symbol]
                self._cancel_session_exit(symbol)
                self._emit_event(
                    "position_closed",
                    {"symbol": symbol, "reason": "TARGET", "exit_price": exit_price},
                )
                break

    # ------------------------------------------------------------------
    # Trade record
    # ------------------------------------------------------------------

    async def _record_completed_trade(self, pos: OpenPosition, exit_price: float, reason: str) -> None:
        """Compute PnL + R and persist the completed trade to SQLite."""
        from alpha1.config.instruments import INSTRUMENT_REGISTRY
        from alpha1.live.state import TradeRecord

        instrument = INSTRUMENT_REGISTRY[pos.symbol]

        if pos.direction == "LONG":
            pnl = (exit_price - pos.entry_price) * instrument.point_value * pos.size
        else:
            pnl = (pos.entry_price - exit_price) * instrument.point_value * pos.size
        pnl -= instrument.commission_per_trade

        risk = abs(pos.entry_price - pos.initial_stop_price) * instrument.point_value * pos.size
        r_multiple = pnl / risk if risk > 0 else 0.0

        # For TIME_EXIT, there's no pre-set target price — the exit is a forced market close.
        if reason == "TIME_EXIT":
            expected_exit = exit_price
            exit_slippage = 0.0
        else:
            expected_exit = pos.target_price if reason == "TARGET" else pos.stop_price
            exit_slippage = exit_price - expected_exit
        # Positive = execution was WORSE than the limit (direction-normalised).
        # LONG  limit buy:  we want fill <= limit; positive means paid more than asked (bad).
        # SHORT limit sell: we want fill >= limit; positive means received less than asked (bad).
        if pos.direction == "LONG":
            fill_slippage_entry = pos.entry_price - pos.limit_entry_price
        else:
            fill_slippage_entry = pos.limit_entry_price - pos.entry_price
        # Recovered positions have limit_entry_price == entry_price, so slippage is 0 regardless.

        await self.state.save_trade(
            TradeRecord(
                instrument=pos.symbol,
                direction=pos.direction,
                signal_timestamp=str(pos.signal_timestamp) if pos.signal_timestamp is not None else None,
                entry_time=pos.entry_time.isoformat(),
                entry_price_limit=pos.limit_entry_price,  # actual limit price requested
                entry_price_filled=pos.entry_price,
                exit_time=datetime.now(UTC).isoformat(),
                exit_price_expected=expected_exit,
                exit_price_filled=exit_price,
                exit_reason=reason,
                pnl=pnl,
                r_multiple=r_multiple,
                fill_slippage_entry=fill_slippage_entry,
                fill_slippage_exit=exit_slippage,
            )
        )
        await self.state.remove_open_position(pos.id)

        log.info(
            "%s: trade closed. reason=%s  exit=%.5f  pnl=%.2f  R=%.2f",
            pos.symbol,
            reason,
            exit_price,
            pnl,
            r_multiple,
        )

    # ------------------------------------------------------------------
    # Session exit task management
    # ------------------------------------------------------------------

    def _cancel_session_exit(self, symbol: str) -> None:
        """Cancel the session-end exit task for a symbol if one is running."""
        task = self._session_exit_tasks.pop(symbol, None)
        if task is not None and not task.done():
            task.cancel()

    # ------------------------------------------------------------------
    # State recovery on startup
    # ------------------------------------------------------------------

    async def _recover_state(self) -> None:
        """
        Reconcile local SQLite state with IBKR's actual positions/orders on startup.

        Populates self._positions from IBKR so the engine never stacks new orders on
        top of existing ones (Fix 1).  Also pre-populates self._known_signals from
        DB pending orders to suppress re-placement of already-submitted signals.

        Cases handled:
          A) IBKR position + DB record  → recover with known stop/target/bracket trades
          B) IBKR position, no DB record → recover with approximate values (no bracket)
          C) DB record, no IBKR position → closed while offline; purge DB record
        """
        db_orders = await self.state.get_pending_orders()
        db_positions = await self.state.get_open_positions()
        ibkr_positions = self.broker.get_positions()

        # Build orderId → Trade map from all currently active IBKR orders.
        # openTrades() returns Trade objects (with orderStatus) unlike openOrders().
        open_trades = self.broker.ib.openTrades()
        trade_map: dict[int, object] = {t.order.orderId: t for t in open_trades}

        log.info(
            "State recovery: %d DB pending, %d DB positions, %d IBKR positions, %d IBKR open orders.",
            len(db_orders),
            len(db_positions),
            len(ibkr_positions),
            len(open_trades),
        )

        db_pos_by_symbol = {p.instrument: p for p in db_positions}
        ibkr_symbols: set[str] = set()

        # --- Cases A & B: recover IBKR positions into self._positions ---
        for ibkr_pos in ibkr_positions:
            symbol = ibkr_pos.contract.symbol
            ibkr_symbols.add(symbol)

            if symbol not in self.config.live.instruments:
                log.warning("IBKR has position in unconfigured instrument %s — ignored.", symbol)
                continue

            # avgCost for futures = fill_price x multiplier.
            mult = float(ibkr_pos.contract.multiplier or 1)
            entry_price = ibkr_pos.avgCost / mult if mult else ibkr_pos.avgCost
            direction = "LONG" if ibkr_pos.position > 0 else "SHORT"
            size = abs(ibkr_pos.position)

            sl_trade = tp_trade = None
            stop_price = target_price = entry_price
            position_id = str(uuid4())

            db_pos = db_pos_by_symbol.get(symbol)
            if db_pos is not None:
                position_id = db_pos.id
                stop_price = db_pos.stop_price
                target_price = db_pos.target_price
                if db_pos.ibkr_sl_id:
                    sl_trade = trade_map.get(db_pos.ibkr_sl_id)
                if db_pos.ibkr_tp_id:
                    tp_trade = trade_map.get(db_pos.ibkr_tp_id)

            self._positions[symbol] = OpenPosition(
                id=position_id,
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                entry_time=datetime.now(UTC),
                stop_price=stop_price,
                initial_stop_price=db_pos.initial_stop_price if db_pos is not None else stop_price,
                target_price=target_price,
                size=size,
                sl_trade=sl_trade,
                tp_trade=tp_trade,
                limit_entry_price=entry_price,  # unknown after crash recovery; set to fill to record 0 slippage
            )
            bracket = f"sl={'found' if sl_trade else 'NONE'}  tp={'found' if tp_trade else 'NONE'}"
            log.info(
                "Recovered %s %s %s  %.1f contracts @ %.4f  [%s]",
                "known" if db_pos else "UNKNOWN",
                symbol,
                direction,
                size,
                entry_price,
                bracket,
            )
            if sl_trade is None or tp_trade is None:
                log.warning(
                    "%s: recovered position has missing bracket protection. "
                    "Add a stop-loss manually in IBKR to limit exposure.",
                    symbol,
                )

        # --- Case C: purge DB positions absent from IBKR ---
        for db_pos in db_positions:
            if db_pos.instrument not in ibkr_symbols:
                log.info(
                    "%s: DB position %s absent from IBKR — likely closed while offline; removing from DB.",
                    db_pos.instrument,
                    db_pos.id,
                )
                await self.state.remove_open_position(db_pos.id)

        # --- Pre-populate known signals to suppress re-placement on restart ---
        # For each still-SUBMITTED DB order: if the IBKR order is gone, expire it;
        # either way add its signal key to self._known_signals so the same signal
        # is not placed again.
        for db_order in db_orders:
            if db_order.status != "SUBMITTED":
                continue
            if db_order.ibkr_parent_id and db_order.ibkr_parent_id not in trade_map:
                log.info(
                    "%s: pending order %s (ibkr#%d) absent from IBKR — marking EXPIRED.",
                    db_order.instrument,
                    db_order.id,
                    db_order.ibkr_parent_id,
                )
                await self.state.update_pending_order_status(db_order.id, "EXPIRED")
            try:
                sig_ts = pd.Timestamp(db_order.signal_timestamp)
                key = (sig_ts, db_order.direction, db_order.limit_price)
                self._known_signals.setdefault(db_order.instrument, set()).add(key)
            except Exception:
                pass  # malformed timestamp; skip dedup entry

    # ------------------------------------------------------------------
    # Event bus
    # ------------------------------------------------------------------

    def _emit_event(self, event_type: str, data: dict) -> None:
        """Broadcast a trading event to all registered listeners (WebSocket clients)."""
        event = {
            "type": event_type,
            "data": data,
            "time": datetime.now(UTC).isoformat(),
        }
        for cb in self._event_callbacks:
            try:
                result = cb(event)
                # If the callback returns a coroutine, schedule it
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)  # noqa: RUF006
            except Exception:
                log.exception("Error in event callback.")
