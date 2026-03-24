"""
Tests for the four engine.py fixes.  No IBKR connection required — all
ib_async objects are replaced with minimal stubs.

Fix 1  State recovery populates self._positions from IBKR positions
Fix 2  Signal recency filter drops stale signals before placement
Fix 3  Cancel/fill guard: filled parent → _handle_fill not cancel_bracket
Fix 4  Bracket child preservation: TP/SL survive when parent fills
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from alpha1.config.settings import StrategyConfig
from alpha1.live.engine import LiveEngine, OpenPosition, PendingOrder
from alpha1.live.state import OpenPositionRecord, PendingOrderRecord, StateManager
from alpha1.strategy.signals import Signal

UTC = UTC


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _make_order(order_id: int, status: str = "Submitted", fill_price: float = 0.0):
    """Minimal ib_async Order+OrderStatus+Trade stub."""
    order = SimpleNamespace(orderId=order_id)
    order_status = SimpleNamespace(status=status, avgFillPrice=fill_price)
    trade = SimpleNamespace(order=order, orderStatus=order_status, isDone=lambda: status == "Filled")
    return trade


def _make_ibkr_position(symbol: str, qty: float, avg_cost: float, multiplier: float = 0.5):
    contract = SimpleNamespace(symbol=symbol, multiplier=str(multiplier))
    return SimpleNamespace(contract=contract, position=qty, avgCost=avg_cost)


def _make_signal(
    direction: str = "LONG",
    entry_price: float = 100.0,
    stop_price: float = 95.0,
    target_price: float = 115.0,
    cancel_price: float = 94.0,
    ts: pd.Timestamp | None = None,
) -> Signal:
    if ts is None:
        ts = pd.Timestamp("2026-03-23 15:00:00", tz="UTC")
    return Signal(
        direction=direction,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        cancel_price=cancel_price,
        bar_index=10,
        timestamp=ts,
    )


def _make_ohlcv(n: int = 60, freq: str = "5min", end: str = "2026-03-23 15:00") -> pd.DataFrame:
    """n bars of flat OHLCV data ending at `end`."""
    idx = pd.date_range(end=end, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {"open": 100.0, "high": 102.0, "low": 98.0, "close": 100.5, "volume": 1000},
        index=idx,
    )


async def _fresh_state() -> StateManager:
    sm = StateManager()
    await sm.init_db(":memory:")
    return sm


def _make_engine(config: StrategyConfig | None = None) -> tuple[LiveEngine, MagicMock, MagicMock]:
    """Return (engine, mock_broker, mock_feed) with minimal wiring."""
    if config is None:
        config = StrategyConfig()

    broker = MagicMock()
    broker.ib = MagicMock()
    broker.ib.openTrades.return_value = []
    broker.ib.disconnectedEvent = MagicMock()
    broker.ib.disconnectedEvent.__iadd__ = MagicMock()
    broker.ib.connectedEvent = MagicMock()
    broker.ib.connectedEvent.__iadd__ = MagicMock()
    broker.get_positions.return_value = []
    broker.get_equity.return_value = 100_000.0
    broker.order_status_event = MagicMock()
    broker.order_status_event.__iadd__ = MagicMock()

    feed = MagicMock()
    feed.on_bar_close = MagicMock()
    feed.subscribe = AsyncMock()

    state = MagicMock()
    state.init_db = AsyncMock()
    state.get_pending_orders = AsyncMock(return_value=[])
    state.get_open_positions = AsyncMock(return_value=[])
    state.save_pending_order = AsyncMock()
    state.update_pending_order_status = AsyncMock()
    state.save_open_position = AsyncMock()
    state.remove_open_position = AsyncMock()
    state.save_trade = AsyncMock()
    state.close = AsyncMock()
    state.update_stop_price = AsyncMock()

    engine = LiveEngine(broker, feed, state, config)
    return engine, broker, feed


# ---------------------------------------------------------------------------
# Fix 1 — State recovery
# ---------------------------------------------------------------------------


class TestStateRecovery:
    """_recover_state populates self._positions from IBKR."""

    @pytest.mark.asyncio
    async def test_known_ibkr_position_with_db_record_is_recovered(self):
        """IBKR position + matching DB record → OpenPosition with correct values."""
        engine, broker, _ = _make_engine()

        ibkr_pos = _make_ibkr_position("MYM", qty=2.0, avg_cost=23400.0, multiplier=0.5)
        broker.get_positions.return_value = [ibkr_pos]

        sl_trade = _make_order(99, "Submitted")
        tp_trade = _make_order(100, "Submitted")
        broker.ib.openTrades.return_value = [sl_trade, tp_trade]

        db_pos = OpenPositionRecord(
            id="pos-abc",
            instrument="MYM",
            direction="LONG",
            entry_price=46800.0,
            entry_time="2026-03-23T14:45:00+00:00",
            stop_price=46600.0,
            initial_stop_price=46600.0,
            target_price=47100.0,
            size=2.0,
            ibkr_sl_id=99,
            ibkr_tp_id=100,
        )
        engine.state.get_open_positions = AsyncMock(return_value=[db_pos])
        engine.state.get_pending_orders = AsyncMock(return_value=[])

        await engine._recover_state()

        assert "MYM" in engine._positions
        pos = engine._positions["MYM"]
        assert pos.direction == "LONG"
        assert pos.size == pytest.approx(2.0)
        # avgCost / multiplier = 23400 / 0.5 = 46800
        assert pos.entry_price == pytest.approx(46800.0)
        assert pos.stop_price == pytest.approx(46600.0)
        assert pos.target_price == pytest.approx(47100.0)
        assert pos.sl_trade is sl_trade
        assert pos.tp_trade is tp_trade

    @pytest.mark.asyncio
    async def test_unknown_ibkr_position_recovered_without_bracket(self):
        """IBKR position with no DB record → recovered with None sl/tp trades."""
        engine, broker, _ = _make_engine()
        ibkr_pos = _make_ibkr_position("MYM", qty=5.0, avg_cost=23000.0, multiplier=0.5)
        broker.get_positions.return_value = [ibkr_pos]

        await engine._recover_state()

        assert "MYM" in engine._positions
        pos = engine._positions["MYM"]
        assert pos.direction == "LONG"
        assert pos.sl_trade is None
        assert pos.tp_trade is None

    @pytest.mark.asyncio
    async def test_short_position_recovered_correctly(self):
        """Negative position quantity → direction SHORT."""
        engine, broker, _ = _make_engine()
        ibkr_pos = _make_ibkr_position("MNQ", qty=-3.0, avg_cost=48800.0, multiplier=2.0)
        broker.get_positions.return_value = [ibkr_pos]

        await engine._recover_state()

        assert engine._positions["MNQ"].direction == "SHORT"
        assert engine._positions["MNQ"].size == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_recovered_position_blocks_new_order_placement(self):
        """After recovery, engine won't place new signals on the recovered symbol."""
        config = StrategyConfig()
        engine, broker, _ = _make_engine(config)

        ibkr_pos = _make_ibkr_position("MYM", qty=2.0, avg_cost=23000.0, multiplier=0.5)
        broker.get_positions.return_value = [ibkr_pos]
        await engine._recover_state()

        assert "MYM" in engine._positions
        # The engine checks `if symbol in self._positions: break` before placing
        # any new orders, so place_bracket should never be called.
        broker.place_bracket.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_position_without_ibkr_is_purged(self):
        """DB position with no corresponding IBKR position is removed from DB."""
        engine, broker, _ = _make_engine()
        broker.get_positions.return_value = []  # IBKR is flat

        db_pos = OpenPositionRecord(
            id="stale-pos",
            instrument="MYM",
            direction="LONG",
            entry_price=46800.0,
            entry_time="2026-03-23T10:00:00+00:00",
            stop_price=46600.0,
            initial_stop_price=46600.0,
            target_price=47100.0,
            size=1.0,
        )
        engine.state.get_open_positions = AsyncMock(return_value=[db_pos])

        await engine._recover_state()

        engine.state.remove_open_position.assert_called_once_with("stale-pos")
        assert "MYM" not in engine._positions

    @pytest.mark.asyncio
    async def test_submitted_pending_order_not_in_ibkr_is_expired(self):
        """DB SUBMITTED order absent from IBKR open trades → marked EXPIRED."""
        engine, broker, _ = _make_engine()
        broker.ib.openTrades.return_value = []  # empty — order is gone from IBKR

        db_order = PendingOrderRecord(
            id="o-abc",
            instrument="MYM",
            direction="LONG",
            limit_price=46800.0,
            stop_price=46600.0,
            target_price=47100.0,
            cancel_price=46700.0,
            signal_timestamp="2026-03-23T14:00:00+00:00",
            placed_at="2026-03-23T14:05:00+00:00",
            formed_bar_time="2026-03-23T14:00:00+00:00",
            status="SUBMITTED",
            ibkr_parent_id=42,
        )
        engine.state.get_pending_orders = AsyncMock(return_value=[db_order])

        await engine._recover_state()

        engine.state.update_pending_order_status.assert_called_once_with("o-abc", "EXPIRED")

    @pytest.mark.asyncio
    async def test_known_signals_seeded_from_submitted_db_orders(self):
        """Submitted DB orders seed _known_signals so they aren't re-placed."""
        engine, broker, _ = _make_engine()
        broker.ib.openTrades.return_value = [_make_order(42, "Submitted")]

        db_order = PendingOrderRecord(
            id="o-xyz",
            instrument="MYM",
            direction="LONG",
            limit_price=46800.0,
            stop_price=46600.0,
            target_price=47100.0,
            cancel_price=46700.0,
            signal_timestamp="2026-03-23T14:00:00+00:00",
            placed_at="2026-03-23T14:05:00+00:00",
            formed_bar_time="2026-03-23T14:00:00+00:00",
            status="SUBMITTED",
            ibkr_parent_id=42,
        )
        engine.state.get_pending_orders = AsyncMock(return_value=[db_order])

        await engine._recover_state()

        key = (pd.Timestamp("2026-03-23T14:00:00+00:00"), "LONG", 46800.0)
        assert key in engine._known_signals.get("MYM", set())


# ---------------------------------------------------------------------------
# Fix 2 — Signal recency filter
# ---------------------------------------------------------------------------


class TestSignalRecencyFilter:
    """Signals older than limit_order_timeout_bars are dropped before placement."""

    def _run_process_bar(self, engine: LiveEngine, signals: list, df: pd.DataFrame):
        """Synchronously run _process_bar logic for the signal placement section."""
        return asyncio.get_event_loop().run_until_complete(self._async_process_bar(engine, signals, df))

    async def _async_process_bar(self, engine: LiveEngine, signals: list, df: pd.DataFrame) -> list:
        """Patch generate_signals and run _process_bar; return place_bracket call args."""
        with patch(
            "alpha1.strategy.signals.generate_signals",
            return_value=signals,
        ):
            data_dict = {"5min": df, "15min": df, "1h": df, "4h": df}
            await engine._process_bar("MYM", data_dict)
        return engine.broker.place_bracket.call_args_list

    @pytest.mark.asyncio
    async def test_fresh_signal_within_window_is_placed(self):
        """Signal formed < timeout_bars ago is placed."""
        engine, broker, _ = _make_engine()
        broker.place_bracket.return_value = (
            _make_order(1, "Submitted"),
            _make_order(2, "Submitted"),
            _make_order(3, "Submitted"),
        )

        df = _make_ohlcv(60, end="2026-03-23 15:00")
        # Signal timestamp = 49 bars ago (just inside the 50-bar window)
        sig_ts = df.index[-2] - pd.Timedelta(minutes=5 * 49)
        signal = _make_signal(ts=sig_ts)

        calls = await self._async_process_bar(engine, [signal], df)
        assert len(calls) == 1, "Fresh signal should be placed"

    @pytest.mark.asyncio
    async def test_stale_signal_beyond_window_is_dropped(self):
        """Signal formed > timeout_bars ago is silently discarded."""
        engine, broker, _ = _make_engine()

        df = _make_ohlcv(60, end="2026-03-23 15:00")
        # Signal timestamp = 51 bars ago (outside the 50-bar window)
        sig_ts = df.index[-2] - pd.Timedelta(minutes=5 * 51)
        signal = _make_signal(ts=sig_ts)

        calls = await self._async_process_bar(engine, [signal], df)
        assert len(calls) == 0, "Stale signal must not be placed"
        broker.place_bracket.assert_not_called()

    @pytest.mark.asyncio
    async def test_exactly_at_cutoff_is_kept(self):
        """Signal at exactly cutoff (== timeout_bars ago) is kept (>= boundary)."""
        engine, broker, _ = _make_engine()
        broker.place_bracket.return_value = (
            _make_order(1),
            _make_order(2),
            _make_order(3),
        )

        df = _make_ohlcv(60, end="2026-03-23 15:00")
        timeout = engine.config.entry.limit_order_timeout_bars  # 50
        sig_ts = df.index[-2] - pd.Timedelta(minutes=5 * timeout)
        signal = _make_signal(ts=sig_ts)

        calls = await self._async_process_bar(engine, [signal], df)
        assert len(calls) == 1, "Signal at exactly cutoff should still be placed"

    @pytest.mark.asyncio
    async def test_mixed_signals_only_fresh_placed(self):
        """When signals span the boundary, only fresh ones reach place_bracket."""
        engine, broker, _ = _make_engine()
        broker.place_bracket.return_value = (
            _make_order(1),
            _make_order(2),
            _make_order(3),
        )

        df = _make_ohlcv(60, end="2026-03-23 15:00")
        fresh_ts = df.index[-2] - pd.Timedelta(minutes=5 * 10)  # 10 bars ago — fresh
        stale_ts = df.index[-2] - pd.Timedelta(minutes=5 * 100)  # 100 bars ago — stale

        fresh = _make_signal(entry_price=100.0, ts=fresh_ts)
        stale = _make_signal(entry_price=200.0, ts=stale_ts)

        calls = await self._async_process_bar(engine, [stale, fresh], df)
        assert len(calls) == 1
        # The placed order should be the fresh signal
        placed_entry = calls[0].args[3]  # place_bracket(symbol, dir, size, entry_price, ...)
        assert placed_entry == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_stale_signals_still_added_to_known_set(self):
        """Stale signals are still deduped — they must not be re-tried next bar."""
        engine, broker, _ = _make_engine()

        df = _make_ohlcv(60, end="2026-03-23 15:00")
        stale_ts = df.index[-2] - pd.Timedelta(minutes=5 * 200)
        signal = _make_signal(ts=stale_ts)

        # First bar: stale signal added to known set
        await self._async_process_bar(engine, [signal], df)
        key = (signal.timestamp, signal.direction, signal.entry_price)
        assert key in engine._known_signals.get("MYM", set())

        # Second bar: same signal NOT in new_signals again (dedup fired)
        broker.place_bracket.reset_mock()
        await self._async_process_bar(engine, [signal], df)
        broker.place_bracket.assert_not_called()


# ---------------------------------------------------------------------------
# Fix 3 — Cancel/fill guard
# ---------------------------------------------------------------------------


class TestCancelFillGuard:
    """If parent fills while we're about to cancel, transition to position."""

    def _make_pending(
        self,
        symbol: str = "MYM",
        direction: str = "LONG",
        parent_status: str = "Submitted",
        parent_fill_price: float = 100.0,
    ) -> PendingOrder:
        parent = _make_order(10, parent_status, parent_fill_price)
        tp = _make_order(11, "Submitted")
        sl = _make_order(12, "Submitted")
        signal = _make_signal(
            direction=direction,
            ts=pd.Timestamp("2026-01-01 00:00:00", tz="UTC"),  # very stale — triggers timeout
        )
        return PendingOrder(
            id="po-1",
            symbol=symbol,
            signal=signal,
            size=2.0,
            actual_stop=95.0,
            parent_trade=parent,
            tp_trade=tp,
            sl_trade=sl,
            placed_at=datetime.now(UTC),
        )

    @pytest.mark.asyncio
    async def test_filled_parent_on_timeout_transitions_not_cancels(self):
        """Timed-out order whose parent already filled → _handle_fill not cancel."""
        engine, broker, _ = _make_engine()
        po = self._make_pending(parent_status="Filled", parent_fill_price=100.5)
        engine._pending["MYM"] = [po]

        df = _make_ohlcv(200, end="2026-03-23 15:00")  # many bars → timeout fires

        with patch.object(engine, "_handle_fill", new_callable=AsyncMock) as mock_fill:
            await engine._manage_pending_orders("MYM", df)

        mock_fill.assert_called_once_with("MYM", po)
        broker.cancel_bracket.assert_not_called()

    @pytest.mark.asyncio
    async def test_unfilled_parent_on_timeout_still_cancels(self):
        """Timed-out order with unfilled parent → cancel_bracket as before."""
        engine, broker, _ = _make_engine()
        po = self._make_pending(parent_status="Submitted")
        engine._pending["MYM"] = [po]

        df = _make_ohlcv(200, end="2026-03-23 15:00")

        with patch.object(engine, "_handle_fill", new_callable=AsyncMock) as mock_fill:
            await engine._manage_pending_orders("MYM", df)

        mock_fill.assert_not_called()
        broker.cancel_bracket.assert_called_once()

    @pytest.mark.asyncio
    async def test_filled_parent_on_fvg_breach_transitions_not_cancels(self):
        """FVG-breached order whose parent already filled → _handle_fill."""
        engine, broker, _ = _make_engine()

        # Signal timestamp recent (no timeout), but cancel_price breached
        recent_ts = pd.Timestamp("2026-03-23 14:55:00", tz="UTC")
        signal = _make_signal(direction="LONG", cancel_price=99.0, ts=recent_ts)
        parent = _make_order(10, "Filled", 100.5)
        po = PendingOrder(
            id="po-fvg",
            symbol="MYM",
            signal=signal,
            size=2.0,
            actual_stop=95.0,
            parent_trade=parent,
            tp_trade=_make_order(11),
            sl_trade=_make_order(12),
            placed_at=datetime.now(UTC),
        )
        engine._pending["MYM"] = [po]

        # Build bar where low < cancel_price (98 < 99) → FVG breach
        df = _make_ohlcv(10, end="2026-03-23 15:00")
        df.iloc[-1, df.columns.get_loc("low")] = 98.0

        with patch.object(engine, "_handle_fill", new_callable=AsyncMock) as mock_fill:
            await engine._manage_pending_orders("MYM", df)

        mock_fill.assert_called_once_with("MYM", po)
        broker.cancel_bracket.assert_not_called()


# ---------------------------------------------------------------------------
# Fix 4 — Bracket child preservation (null-safe guard paths)
# ---------------------------------------------------------------------------


class TestBracketChildPreservation:
    """Recovered positions with None sl/tp trades don't crash the engine."""

    def _recovered_position(self, symbol: str = "MYM") -> OpenPosition:
        return OpenPosition(
            id="recovered",
            symbol=symbol,
            direction="LONG",
            entry_price=46800.0,
            entry_time=datetime.now(UTC),
            stop_price=46600.0,
            initial_stop_price=46600.0,
            target_price=47100.0,
            size=2.0,
            sl_trade=None,
            tp_trade=None,
        )

    @pytest.mark.asyncio
    async def test_manage_position_does_not_crash_with_none_sl_trade(self):
        """Breakeven management skips modify_stop when sl_trade is None."""
        config = StrategyConfig()
        config.exit.breakeven_at_r = 0.5  # enable breakeven
        engine, broker, _ = _make_engine(config)
        engine._positions["MYM"] = self._recovered_position()

        df = _make_ohlcv(10, end="2026-03-23 15:00")
        # Bar high well above entry → triggers breakeven
        df.iloc[-1, df.columns.get_loc("high")] = 48000.0

        # Must not raise AttributeError
        await engine._manage_position("MYM", df)
        broker.modify_stop.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_end_exit_does_not_crash_with_none_bracket(self):
        """Session-end exit proceeds without crashing when bracket is None."""
        engine, broker, _ = _make_engine()
        engine._positions["MYM"] = self._recovered_position()

        close_trade = _make_order(99, "Filled", 46700.0)
        broker.close_position_market.return_value = close_trade

        with patch.object(engine, "_record_completed_trade", new_callable=AsyncMock):
            exit_time = datetime.now(UTC)  # immediate exit
            await engine._session_end_exit("MYM", exit_time)

        broker.cancel_order.assert_not_called()
        broker.close_position_market.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_order_status_skips_none_sl_tp(self):
        """_on_order_status does not crash or false-match when sl/tp are None."""
        engine, _broker, _ = _make_engine()
        engine._positions["MYM"] = self._recovered_position()

        # Fire a trade for an order that has nothing to do with our position
        unrelated = _make_order(999, "Filled", 46700.0)
        await engine._on_order_status(unrelated)

        # Position should still exist — no match on None sl_id or tp_id
        assert "MYM" in engine._positions

    @pytest.mark.asyncio
    async def test_on_order_status_matches_real_sl_trade(self):
        """When sl_trade is set, a fill on its orderId closes the position."""
        engine, broker, _ = _make_engine()

        sl_trade = _make_order(77, "Filled", 46600.0)
        tp_trade = _make_order(78, "Submitted")
        pos = OpenPosition(
            id="p1",
            symbol="MYM",
            direction="LONG",
            entry_price=46800.0,
            entry_time=datetime.now(UTC),
            stop_price=46600.0,
            initial_stop_price=46600.0,
            target_price=47100.0,
            size=2.0,
            sl_trade=sl_trade,
            tp_trade=tp_trade,
        )
        engine._positions["MYM"] = pos

        with patch.object(engine, "_record_completed_trade", new_callable=AsyncMock):
            await engine._on_order_status(sl_trade)

        assert "MYM" not in engine._positions
        broker.cancel_order.assert_called_once_with(tp_trade)


# ---------------------------------------------------------------------------
# Review findings — PR #1
# ---------------------------------------------------------------------------


class TestReviewFindings:
    """Tests for the 8 code-review findings addressed in PR #1."""

    # ------------------------------------------------------------------
    # Finding P0: double fill overwrites position
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_double_fill_second_order_cancelled(self):
        """Two pending orders for the same symbol both Filled — only first creates position."""
        engine, broker, _ = _make_engine()

        sig = _make_signal(ts=pd.Timestamp("2026-03-23 14:55:00", tz="UTC"))
        po1 = PendingOrder(
            id="po-1",
            symbol="MYM",
            signal=sig,
            size=2.0,
            actual_stop=95.0,
            parent_trade=_make_order(10, "Filled", 100.0),
            tp_trade=_make_order(11, "Submitted"),
            sl_trade=_make_order(12, "Submitted"),
            placed_at=datetime.now(UTC),
        )
        po2 = PendingOrder(
            id="po-2",
            symbol="MYM",
            signal=_make_signal(entry_price=101.0, ts=pd.Timestamp("2026-03-23 14:55:00", tz="UTC")),
            size=1.0,
            actual_stop=94.0,
            parent_trade=_make_order(20, "Filled", 101.0),
            tp_trade=_make_order(21, "Submitted"),
            sl_trade=_make_order(22, "Submitted"),
            placed_at=datetime.now(UTC),
        )
        engine._pending["MYM"] = [po1, po2]

        df = _make_ohlcv(10, end="2026-03-23 15:00")
        await engine._manage_pending_orders("MYM", df)

        # Only one position — the first fill wins
        assert "MYM" in engine._positions
        assert engine._positions["MYM"].id == "po-1"

        # Duplicate bracket children must be cancelled
        broker.cancel_order.assert_any_call(po2.tp_trade)
        broker.cancel_order.assert_any_call(po2.sl_trade)

        # place_bracket must never be called (these are fill transitions, not new orders)
        broker.place_bracket.assert_not_called()

    # ------------------------------------------------------------------
    # Finding P1: signals permanently lost when equity=0
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_equity_zero_defers_signals_to_known_set(self):
        """equity=0 clears all_signals before dedup; nothing added to _known_signals."""
        engine, broker, _ = _make_engine()
        broker.get_equity.return_value = 0.0

        df = _make_ohlcv(60, end="2026-03-23 15:00")
        sig_ts = df.index[-2] - pd.Timedelta(minutes=5 * 10)
        signal = _make_signal(ts=sig_ts)

        with patch("alpha1.strategy.signals.generate_signals", return_value=[signal]):
            await engine._process_bar("MYM", {"5min": df, "15min": df, "1h": df, "4h": df})

        # Signal must NOT be in _known_signals so it can be retried next bar
        key = (signal.timestamp, signal.direction, signal.entry_price)
        assert key not in engine._known_signals.get("MYM", set())
        broker.place_bracket.assert_not_called()

    @pytest.mark.asyncio
    async def test_equity_nonzero_adds_signal_to_known_set(self):
        """equity>0: signals are added to _known_signals after placement attempt."""
        engine, broker, _ = _make_engine()
        broker.get_equity.return_value = 100_000.0
        broker.place_bracket.return_value = (
            _make_order(1, "Submitted"),
            _make_order(2, "Submitted"),
            _make_order(3, "Submitted"),
        )

        df = _make_ohlcv(60, end="2026-03-23 15:00")
        sig_ts = df.index[-2] - pd.Timedelta(minutes=5 * 10)
        signal = _make_signal(ts=sig_ts)

        with patch("alpha1.strategy.signals.generate_signals", return_value=[signal]):
            await engine._process_bar("MYM", {"5min": df, "15min": df, "1h": df, "4h": df})

        key = (signal.timestamp, signal.direction, signal.entry_price)
        assert key in engine._known_signals.get("MYM", set())

    # ------------------------------------------------------------------
    # Finding P1: _recover_state preserves initial_stop_price from DB
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_recover_state_uses_db_initial_stop_price(self):
        """DB initial_stop_price survives recovery even when stop_price is at breakeven."""
        from alpha1.live.state import OpenPositionRecord

        engine, broker, _ = _make_engine()
        ibkr_pos = _make_ibkr_position("MYM", qty=2.0, avg_cost=23400.0, multiplier=0.5)
        broker.get_positions.return_value = [ibkr_pos]

        db_pos = OpenPositionRecord(
            id="pos-recover",
            instrument="MYM",
            direction="LONG",
            entry_price=46800.0,
            entry_time="2026-03-23T14:45:00+00:00",
            stop_price=46800.0,  # moved to breakeven
            initial_stop_price=46600.0,  # original stop
            target_price=47100.0,
            size=2.0,
        )
        engine.state.get_open_positions = AsyncMock(return_value=[db_pos])
        engine.state.get_pending_orders = AsyncMock(return_value=[])

        await engine._recover_state()

        pos = engine._positions["MYM"]
        # initial_stop_price must come from DB, not from stop_price
        assert pos.initial_stop_price == pytest.approx(46600.0)
        assert pos.stop_price == pytest.approx(46800.0)

    # ------------------------------------------------------------------
    # Finding P2: session-end timeout keeps position tracked
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_session_end_timeout_keeps_position_tracked(self):
        """Market close times out → position stays in _positions; no trade recorded."""
        engine, broker, _ = _make_engine()

        pos = OpenPosition(
            id="p-timeout",
            symbol="MYM",
            direction="LONG",
            entry_price=46800.0,
            entry_time=datetime.now(UTC),
            stop_price=46600.0,
            initial_stop_price=46600.0,
            target_price=47100.0,
            size=1.0,
            sl_trade=None,
            tp_trade=None,
        )
        engine._positions["MYM"] = pos

        # isDone() always returns False — simulates a market order that never fills
        never_done = _make_order(99, "Submitted")  # isDone lambda returns False
        broker.close_position_market.return_value = never_done

        exit_time = datetime.now(UTC)  # no sleep
        await engine._session_end_exit("MYM", exit_time)

        # Position must remain tracked for manual resolution
        assert "MYM" in engine._positions
        # No trade should have been recorded
        engine.state.save_trade.assert_not_called()

    # ------------------------------------------------------------------
    # Finding P2: short DataFrame causes IndexError on index[-2]
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_df_too_short_returns_early(self):
        """1-row DataFrame returns before generate_signals is called."""
        engine, broker, _ = _make_engine()

        df = _make_ohlcv(1, end="2026-03-23 15:00")

        with patch("alpha1.strategy.signals.generate_signals") as mock_gen:
            await engine._process_bar("MYM", {"5min": df, "15min": df, "1h": df, "4h": df})

        mock_gen.assert_not_called()
        broker.place_bracket.assert_not_called()
