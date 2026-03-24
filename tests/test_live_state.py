"""
Tests for alpha1.live.state.StateManager.

All tests use an in-memory SQLite database (':memory:') so they are fully
isolated, fast, and require no filesystem access.

Pattern: each test creates a fresh StateManager, initialises it, exercises
the relevant methods, and asserts the expected database content.
"""

import pytest

from alpha1.live.state import (
    OpenPositionRecord,
    PendingOrderRecord,
    StateManager,
    TradeRecord,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pending(id_: str = "order-1", status: str = "PENDING") -> PendingOrderRecord:
    return PendingOrderRecord(
        id=id_,
        instrument="XAUUSD",
        direction="LONG",
        limit_price=1950.00,
        stop_price=1940.00,
        target_price=1975.00,
        cancel_price=1945.00,
        signal_timestamp="2024-01-15T09:00:00+00:00",
        placed_at="2024-01-15T09:05:00+00:00",
        formed_bar_time="2024-01-15T09:00:00+00:00",
        status=status,
        ibkr_parent_id=1001,
        ibkr_tp_id=1002,
        ibkr_sl_id=1003,
    )


def _make_position(id_: str = "pos-1") -> OpenPositionRecord:
    return OpenPositionRecord(
        id=id_,
        instrument="MYM",
        direction="SHORT",
        entry_price=38000.0,
        entry_time="2024-01-15T14:00:00+00:00",
        stop_price=38100.0,
        initial_stop_price=38100.0,
        target_price=37700.0,
        size=2.0,
        ibkr_sl_id=2001,
        ibkr_tp_id=2002,
    )


def _make_trade() -> TradeRecord:
    return TradeRecord(
        instrument="MNQ",
        direction="LONG",
        signal_timestamp="2024-01-15T08:55:00+00:00",
        entry_time="2024-01-15T09:10:00+00:00",
        entry_price_limit=18500.0,
        entry_price_filled=18500.25,
        exit_time="2024-01-15T12:00:00+00:00",
        exit_price_expected=18600.0,
        exit_price_filled=18599.75,
        exit_reason="TARGET",
        pnl=198.44,
        r_multiple=1.48,
        fill_slippage_entry=0.25,
        fill_slippage_exit=-0.25,
    )


async def _fresh_sm() -> StateManager:
    sm = StateManager()
    await sm.init_db(":memory:")
    return sm


# ---------------------------------------------------------------------------
# Pending orders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_get_pending_order():
    sm = await _fresh_sm()
    order = _make_pending()
    await sm.save_pending_order(order)

    orders = await sm.get_pending_orders()
    assert len(orders) == 1
    o = orders[0]
    assert o.id == "order-1"
    assert o.instrument == "XAUUSD"
    assert o.direction == "LONG"
    assert o.limit_price == pytest.approx(1950.00)
    assert o.ibkr_parent_id == 1001


@pytest.mark.asyncio
async def test_update_pending_order_status():
    sm = await _fresh_sm()
    await sm.save_pending_order(_make_pending())

    await sm.update_pending_order_status("order-1", "FILLED")
    orders = await sm.get_pending_orders()
    assert orders[0].status == "FILLED"


@pytest.mark.asyncio
async def test_update_pending_order_with_ibkr_ids():
    sm = await _fresh_sm()
    await sm.save_pending_order(_make_pending(id_="o2"))

    await sm.update_pending_order_status(
        "o2",
        "SUBMITTED",
        ibkr_ids={"parent_id": 9001, "tp_id": 9002, "sl_id": 9003},
    )
    orders = await sm.get_pending_orders()
    assert orders[0].ibkr_parent_id == 9001
    assert orders[0].ibkr_tp_id == 9002
    assert orders[0].ibkr_sl_id == 9003


@pytest.mark.asyncio
async def test_remove_pending_order():
    sm = await _fresh_sm()
    await sm.save_pending_order(_make_pending("a"))
    await sm.save_pending_order(_make_pending("b"))

    await sm.remove_pending_order("a")
    orders = await sm.get_pending_orders()
    assert len(orders) == 1
    assert orders[0].id == "b"


@pytest.mark.asyncio
async def test_pending_order_multiple_instruments():
    sm = await _fresh_sm()
    o1 = _make_pending("xau")
    o1.instrument = "XAUUSD"
    o2 = _make_pending("mym")
    o2.instrument = "MYM"
    await sm.save_pending_order(o1)
    await sm.save_pending_order(o2)

    orders = await sm.get_pending_orders()
    instruments = {o.instrument for o in orders}
    assert instruments == {"XAUUSD", "MYM"}


# ---------------------------------------------------------------------------
# Open positions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_get_open_position():
    sm = await _fresh_sm()
    await sm.save_open_position(_make_position())

    positions = await sm.get_open_positions()
    assert len(positions) == 1
    p = positions[0]
    assert p.id == "pos-1"
    assert p.instrument == "MYM"
    assert p.entry_price == pytest.approx(38000.0)
    assert p.size == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_update_stop_price():
    sm = await _fresh_sm()
    await sm.save_open_position(_make_position())

    await sm.update_stop_price("pos-1", 38000.0)  # breakeven
    positions = await sm.get_open_positions()
    assert positions[0].stop_price == pytest.approx(38000.0)


@pytest.mark.asyncio
async def test_remove_open_position():
    sm = await _fresh_sm()
    await sm.save_open_position(_make_position("p1"))
    await sm.save_open_position(_make_position("p2"))

    await sm.remove_open_position("p1")
    positions = await sm.get_open_positions()
    assert len(positions) == 1
    assert positions[0].id == "p2"


# ---------------------------------------------------------------------------
# Trade history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_get_trade():
    sm = await _fresh_sm()
    await sm.save_trade(_make_trade())

    trades = await sm.get_trade_history()
    assert len(trades) == 1
    t = trades[0]
    assert t.instrument == "MNQ"
    assert t.pnl == pytest.approx(198.44)
    assert t.r_multiple == pytest.approx(1.48)
    assert t.exit_reason == "TARGET"


@pytest.mark.asyncio
async def test_trade_history_instrument_filter():
    sm = await _fresh_sm()

    t1 = _make_trade()
    t1.instrument = "MNQ"
    t2 = _make_trade()
    t2.instrument = "MYM"
    await sm.save_trade(t1)
    await sm.save_trade(t2)

    mnq_trades = await sm.get_trade_history(instrument="MNQ")
    assert len(mnq_trades) == 1
    assert mnq_trades[0].instrument == "MNQ"


@pytest.mark.asyncio
async def test_trade_history_limit():
    sm = await _fresh_sm()
    for _ in range(10):
        await sm.save_trade(_make_trade())

    trades = await sm.get_trade_history(limit=5)
    assert len(trades) == 5


@pytest.mark.asyncio
async def test_trade_history_order_newest_first():
    sm = await _fresh_sm()
    t1 = _make_trade()
    t1.pnl = 100.0
    t2 = _make_trade()
    t2.pnl = 200.0
    await sm.save_trade(t1)
    await sm.save_trade(t2)

    trades = await sm.get_trade_history(limit=2)
    # Newest first (highest auto-increment id)
    assert trades[0].pnl == pytest.approx(200.0)
    assert trades[1].pnl == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Fill quality summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fill_quality_summary_empty():
    sm = await _fresh_sm()
    summary = await sm.get_fill_quality_summary()
    assert summary["total_trades"] == 0
    assert summary["win_rate"] == 0.0


@pytest.mark.asyncio
async def test_fill_quality_summary_with_trades():
    sm = await _fresh_sm()

    win = _make_trade()
    win.pnl = 100.0
    win.r_multiple = 1.5
    loss = _make_trade()
    loss.pnl = -50.0
    loss.r_multiple = -0.5
    await sm.save_trade(win)
    await sm.save_trade(loss)

    summary = await sm.get_fill_quality_summary()
    assert summary["total_trades"] == 2
    assert summary["win_rate"] == pytest.approx(50.0)
    assert summary["total_pnl"] == pytest.approx(50.0)
    assert summary["avg_r_multiple"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Double-init guard (idempotent DDL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_db_idempotent():
    sm = StateManager()
    await sm.init_db(":memory:")
    # Second call should not raise (CREATE TABLE IF NOT EXISTS)
    await sm.init_db(":memory:")


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close():
    sm = await _fresh_sm()
    await sm.close()
    # Second close is a no-op
    await sm.close()


# ---------------------------------------------------------------------------
# Error: use before init
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_use_before_init_raises():
    sm = StateManager()
    with pytest.raises(RuntimeError, match="init_db"):
        await sm.get_pending_orders()


@pytest.mark.asyncio
async def test_init_db_twice_closes_first_connection():
    """Second init_db call closes the first connection and leaves the DB usable."""
    sm = StateManager()
    await sm.init_db(":memory:")
    # Call a second time -- must not raise and must not leak the first connection
    await sm.init_db(":memory:")
    # DB must be fully functional after the second init
    order = _make_pending(id_="leak-check")
    await sm.save_pending_order(order)
    orders = await sm.get_pending_orders()
    assert len(orders) == 1
    assert orders[0].id == "leak-check"


# ---------------------------------------------------------------------------
# Fill quality: TIME_EXIT entry slippage is included; exit slippage is excluded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fill_quality_summary_time_exit_entry_slippage_included():
    """Entry slippage for TIME_EXIT trades must be included in the average.

    fill_slippage_entry is set at entry time; the exit reason is irrelevant to
    whether that fill was good or bad.  Filtering it out would bias the metric.
    """
    sm = await _fresh_sm()

    stop_trade = _make_trade()
    stop_trade.exit_reason = "STOP_LOSS"
    stop_trade.fill_slippage_entry = 5.0

    time_trade = _make_trade()
    time_trade.exit_reason = "TIME_EXIT"
    time_trade.fill_slippage_entry = 3.0

    await sm.save_trade(stop_trade)
    await sm.save_trade(time_trade)

    summary = await sm.get_fill_quality_summary()
    # Both trades contribute: (5.0 + 3.0) / 2 == 4.0
    assert summary["avg_entry_slippage"] == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_fill_quality_summary_time_exit_exit_slippage_excluded():
    """Exit slippage for TIME_EXIT trades must be excluded from the average.

    TIME_EXIT sets fill_slippage_exit to 0.0 as a sentinel (no real measurement).
    Including it would dilute the metric with a fictitious zero.
    """
    sm = await _fresh_sm()

    stop_trade = _make_trade()
    stop_trade.exit_reason = "STOP_LOSS"
    stop_trade.fill_slippage_exit = 8.0

    time_trade = _make_trade()
    time_trade.exit_reason = "TIME_EXIT"
    time_trade.fill_slippage_exit = 0.0  # sentinel, not a real measurement

    await sm.save_trade(stop_trade)
    await sm.save_trade(time_trade)

    summary = await sm.get_fill_quality_summary()
    # Only STOP_LOSS contributes: ABS(8.0) == 8.0
    assert summary["avg_exit_slippage"] == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# Journal fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_trade_with_journal_fields():
    sm = await _fresh_sm()
    t = _make_trade()
    t.mfe_price = 18620.0
    t.mae_price = 18490.0
    t.mfe_r = 2.1
    t.mae_r = -0.5
    t.time_in_trade_minutes = 47.5
    t.session_label = "London"
    t.notes = "clean BOS entry"
    t.tags = "bos,fvg"
    await sm.save_trade(t)

    trades = await sm.get_trade_history()
    assert len(trades) == 1
    r = trades[0]
    assert r.mfe_r == pytest.approx(2.1)
    assert r.mae_r == pytest.approx(-0.5)
    assert r.session_label == "London"
    assert r.time_in_trade_minutes == pytest.approx(47.5)
    assert r.notes == "clean BOS entry"
    assert r.tags == "bos,fvg"


@pytest.mark.asyncio
async def test_update_trade_notes():
    sm = await _fresh_sm()
    await sm.save_trade(_make_trade())

    trades = await sm.get_trade_history()
    trade_id = trades[0].id
    assert trade_id is not None

    await sm.update_trade_notes(trade_id, "great setup", "fvg_bos,london")

    updated = await sm.get_trade_history()
    assert updated[0].notes == "great setup"
    assert updated[0].tags == "fvg_bos,london"


@pytest.mark.asyncio
async def test_migration_idempotent():
    """Running init_db twice on the same StateManager must not raise."""
    sm = StateManager()
    await sm.init_db(":memory:")
    # Second call: DDL uses CREATE TABLE IF NOT EXISTS; migrations hit existing
    # columns and are silently suppressed via contextlib.suppress.
    await sm.init_db(":memory:")
    # DB must still be usable
    await sm.save_trade(_make_trade())
    trades = await sm.get_trade_history()
    assert len(trades) == 1


@pytest.mark.asyncio
async def test_fill_quality_includes_mfe_mae():
    sm = await _fresh_sm()

    t1 = _make_trade()
    t1.mfe_r = 2.0
    t1.mae_r = -0.4
    t2 = _make_trade()
    t2.mfe_r = 1.0
    t2.mae_r = -0.8
    await sm.save_trade(t1)
    await sm.save_trade(t2)

    summary = await sm.get_fill_quality_summary()
    assert summary["avg_mfe_r"] == pytest.approx(1.5)
    assert summary["avg_mae_r"] == pytest.approx(-0.6)
