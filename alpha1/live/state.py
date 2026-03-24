"""
SQLite-backed state persistence for live trading.

Stores pending limit orders, open positions, and trade history.  Survives process
crashes: on restart, engine.py calls _recover_state() which loads this data and
reconciles it against IBKR's actual positions/orders.

Design choices:
  - aiosqlite for non-blocking I/O (lives in the same asyncio loop as ib_async).
  - Timestamps stored as ISO 8601 strings (UTC).  SQLite has no native datetime
    type; strings sort correctly and survive JSON serialisation unchanged.
  - Records are @dataclass objects — typed, easy to construct in tests, and
    straightforward to convert to/from SQLite rows via dataclasses.asdict().
  - init_db accepts a path argument so tests can pass ':memory:'.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("alpha1.live.state")

# ---------------------------------------------------------------------------
# Record dataclasses (mirrors SQL schema; used both for writes and query results)
# ---------------------------------------------------------------------------


@dataclass
class PendingOrderRecord:
    id: str  # UUID string
    instrument: str
    direction: str  # LONG or SHORT
    limit_price: float
    stop_price: float
    target_price: float
    cancel_price: float
    signal_timestamp: str  # ISO 8601 UTC
    placed_at: str  # ISO 8601 UTC
    formed_bar_time: str  # bar when signal formed (ISO 8601 UTC)
    status: str = "PENDING"  # PENDING | SUBMITTED | FILLED | CANCELLED | EXPIRED
    ibkr_parent_id: int | None = None
    ibkr_tp_id: int | None = None
    ibkr_sl_id: int | None = None


@dataclass
class OpenPositionRecord:
    id: str
    instrument: str
    direction: str
    entry_price: float
    entry_time: str  # ISO 8601 UTC
    stop_price: float
    initial_stop_price: float
    target_price: float
    size: float
    ibkr_sl_id: int | None = None
    ibkr_tp_id: int | None = None
    session_end_time: str | None = None


@dataclass
class TradeRecord:
    instrument: str
    direction: str
    entry_time: str | None
    entry_price_limit: float | None  # what we asked for
    entry_price_filled: float | None  # what IBKR gave us
    exit_time: str | None
    exit_price_expected: float | None  # SL/TP/session-end price
    exit_price_filled: float | None  # what IBKR gave us
    exit_reason: str | None
    pnl: float
    r_multiple: float
    fill_slippage_entry: float  # filled - limit (positive = worse)
    fill_slippage_exit: float  # signed slippage on exit
    signal_timestamp: str | None = None
    id: int | None = field(default=None)  # set by DB on insert


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS pending_orders (
    id                TEXT PRIMARY KEY,
    instrument        TEXT NOT NULL,
    direction         TEXT NOT NULL,
    limit_price       REAL NOT NULL,
    stop_price        REAL NOT NULL,
    target_price      REAL NOT NULL,
    cancel_price      REAL NOT NULL,
    signal_timestamp  TEXT NOT NULL,
    placed_at         TEXT NOT NULL,
    formed_bar_time   TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'PENDING',
    ibkr_parent_id    INTEGER,
    ibkr_tp_id        INTEGER,
    ibkr_sl_id        INTEGER
);

CREATE TABLE IF NOT EXISTS open_positions (
    id                  TEXT PRIMARY KEY,
    instrument          TEXT NOT NULL,
    direction           TEXT NOT NULL,
    entry_price         REAL NOT NULL,
    entry_time          TEXT NOT NULL,
    stop_price          REAL NOT NULL,
    initial_stop_price  REAL NOT NULL,
    target_price        REAL NOT NULL,
    size                REAL NOT NULL,
    ibkr_sl_id          INTEGER,
    ibkr_tp_id          INTEGER,
    session_end_time    TEXT
);

CREATE TABLE IF NOT EXISTS trade_history (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument            TEXT NOT NULL,
    direction             TEXT NOT NULL,
    signal_timestamp      TEXT,
    entry_time            TEXT,
    entry_price_limit     REAL,
    entry_price_filled    REAL,
    exit_time             TEXT,
    exit_price_expected   REAL,
    exit_price_filled     REAL,
    exit_reason           TEXT,
    pnl                   REAL,
    r_multiple            REAL,
    fill_slippage_entry   REAL,
    fill_slippage_exit    REAL,
    created_at            TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------


class StateManager:
    """
    Async SQLite persistence for live trading state.

    Usage:
        sm = StateManager()
        await sm.init_db()          # production: 'alpha1_live.db'
        await sm.init_db(':memory:') # tests
    """

    def __init__(self) -> None:
        self._db = None  # aiosqlite.Connection, set by init_db

    async def init_db(self, db_path: str = "alpha1_live.db") -> None:
        """Open (or create) the SQLite database and run DDL."""
        import aiosqlite

        self._db = await aiosqlite.connect(db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_DDL)
        await self._db.commit()
        log.info("State DB ready: %s", db_path)

    def _require_db(self) -> Any:
        if self._db is None:
            raise RuntimeError("StateManager.init_db() must be called before use")
        return self._db

    # ------------------------------------------------------------------
    # Pending orders
    # ------------------------------------------------------------------

    async def save_pending_order(self, order: PendingOrderRecord) -> None:
        db = self._require_db()
        await db.execute(
            """
            INSERT INTO pending_orders
                (id, instrument, direction, limit_price, stop_price, target_price,
                 cancel_price, signal_timestamp, placed_at, formed_bar_time, status,
                 ibkr_parent_id, ibkr_tp_id, ibkr_sl_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.id,
                order.instrument,
                order.direction,
                order.limit_price,
                order.stop_price,
                order.target_price,
                order.cancel_price,
                order.signal_timestamp,
                order.placed_at,
                order.formed_bar_time,
                order.status,
                order.ibkr_parent_id,
                order.ibkr_tp_id,
                order.ibkr_sl_id,
            ),
        )
        await db.commit()

    async def update_pending_order_status(
        self,
        order_id: str,
        status: str,
        ibkr_ids: dict[str, int] | None = None,
    ) -> None:
        """Update status and optionally set IBKR order IDs (after submission)."""
        db = self._require_db()
        if ibkr_ids:
            await db.execute(
                """
                UPDATE pending_orders
                SET status = ?, ibkr_parent_id = ?, ibkr_tp_id = ?, ibkr_sl_id = ?
                WHERE id = ?
                """,
                (
                    status,
                    ibkr_ids.get("parent_id"),
                    ibkr_ids.get("tp_id"),
                    ibkr_ids.get("sl_id"),
                    order_id,
                ),
            )
        else:
            await db.execute(
                "UPDATE pending_orders SET status = ? WHERE id = ?",
                (status, order_id),
            )
        await db.commit()

    async def remove_pending_order(self, order_id: str) -> None:
        db = self._require_db()
        await db.execute("DELETE FROM pending_orders WHERE id = ?", (order_id,))
        await db.commit()

    async def get_pending_orders(self) -> list[PendingOrderRecord]:
        db = self._require_db()
        async with db.execute("SELECT * FROM pending_orders") as cursor:
            rows = await cursor.fetchall()
        return [_row_to_pending(r) for r in rows]

    # ------------------------------------------------------------------
    # Open positions
    # ------------------------------------------------------------------

    async def save_open_position(self, position: OpenPositionRecord) -> None:
        db = self._require_db()
        await db.execute(
            """
            INSERT INTO open_positions
                (id, instrument, direction, entry_price, entry_time, stop_price,
                 initial_stop_price, target_price, size, ibkr_sl_id, ibkr_tp_id,
                 session_end_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position.id,
                position.instrument,
                position.direction,
                position.entry_price,
                position.entry_time,
                position.stop_price,
                position.initial_stop_price,
                position.target_price,
                position.size,
                position.ibkr_sl_id,
                position.ibkr_tp_id,
                position.session_end_time,
            ),
        )
        await db.commit()

    async def update_stop_price(self, position_id: str, new_stop: float) -> None:
        db = self._require_db()
        await db.execute(
            "UPDATE open_positions SET stop_price = ? WHERE id = ?",
            (new_stop, position_id),
        )
        await db.commit()

    async def remove_open_position(self, position_id: str) -> None:
        db = self._require_db()
        await db.execute("DELETE FROM open_positions WHERE id = ?", (position_id,))
        await db.commit()

    async def get_open_positions(self) -> list[OpenPositionRecord]:
        db = self._require_db()
        async with db.execute("SELECT * FROM open_positions") as cursor:
            rows = await cursor.fetchall()
        return [_row_to_position(r) for r in rows]

    # ------------------------------------------------------------------
    # Trade history
    # ------------------------------------------------------------------

    async def save_trade(self, trade: TradeRecord) -> None:
        db = self._require_db()
        await db.execute(
            """
            INSERT INTO trade_history
                (instrument, direction, signal_timestamp, entry_time, entry_price_limit,
                 entry_price_filled, exit_time, exit_price_expected, exit_price_filled,
                 exit_reason, pnl, r_multiple, fill_slippage_entry, fill_slippage_exit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.instrument,
                trade.direction,
                trade.signal_timestamp,
                trade.entry_time,
                trade.entry_price_limit,
                trade.entry_price_filled,
                trade.exit_time,
                trade.exit_price_expected,
                trade.exit_price_filled,
                trade.exit_reason,
                trade.pnl,
                trade.r_multiple,
                trade.fill_slippage_entry,
                trade.fill_slippage_exit,
            ),
        )
        await db.commit()

    async def get_trade_history(
        self,
        instrument: str | None = None,
        limit: int = 100,
    ) -> list[TradeRecord]:
        db = self._require_db()
        if instrument:
            query = "SELECT * FROM trade_history WHERE instrument = ? ORDER BY id DESC LIMIT ?"
            params = (instrument, limit)
        else:
            query = "SELECT * FROM trade_history ORDER BY id DESC LIMIT ?"
            params = (limit,)
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_trade(r) for r in rows]

    async def get_fill_quality_summary(self) -> dict[str, Any]:
        """Aggregate fill quality metrics for the dashboard."""
        db = self._require_db()
        async with db.execute(
            """
            SELECT
                COUNT(*)                          AS total_trades,
                AVG(fill_slippage_entry)          AS avg_entry_slippage,
                AVG(ABS(fill_slippage_exit))      AS avg_exit_slippage,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS winners,
                AVG(r_multiple)                   AS avg_r,
                SUM(pnl)                          AS total_pnl
            FROM trade_history
            """
        ) as cursor:
            row = await cursor.fetchone()

        total = row["total_trades"] or 0
        winners = row["winners"] or 0
        return {
            "total_trades": total,
            "win_rate": round(winners / total * 100, 2) if total > 0 else 0.0,
            "avg_entry_slippage": round(row["avg_entry_slippage"] or 0.0, 5),
            "avg_exit_slippage": round(row["avg_exit_slippage"] or 0.0, 5),
            "avg_r_multiple": round(row["avg_r"] or 0.0, 3),
            "total_pnl": round(row["total_pnl"] or 0.0, 2),
        }

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None


# ---------------------------------------------------------------------------
# Row → record converters
# ---------------------------------------------------------------------------


def _row_to_pending(row) -> PendingOrderRecord:
    return PendingOrderRecord(
        id=row["id"],
        instrument=row["instrument"],
        direction=row["direction"],
        limit_price=row["limit_price"],
        stop_price=row["stop_price"],
        target_price=row["target_price"],
        cancel_price=row["cancel_price"],
        signal_timestamp=row["signal_timestamp"],
        placed_at=row["placed_at"],
        formed_bar_time=row["formed_bar_time"],
        status=row["status"],
        ibkr_parent_id=row["ibkr_parent_id"],
        ibkr_tp_id=row["ibkr_tp_id"],
        ibkr_sl_id=row["ibkr_sl_id"],
    )


def _row_to_position(row) -> OpenPositionRecord:
    return OpenPositionRecord(
        id=row["id"],
        instrument=row["instrument"],
        direction=row["direction"],
        entry_price=row["entry_price"],
        entry_time=row["entry_time"],
        stop_price=row["stop_price"],
        initial_stop_price=row["initial_stop_price"],
        target_price=row["target_price"],
        size=row["size"],
        ibkr_sl_id=row["ibkr_sl_id"],
        ibkr_tp_id=row["ibkr_tp_id"],
        session_end_time=row["session_end_time"],
    )


def _row_to_trade(row) -> TradeRecord:
    return TradeRecord(
        id=row["id"],
        instrument=row["instrument"],
        direction=row["direction"],
        signal_timestamp=row["signal_timestamp"],
        entry_time=row["entry_time"],
        entry_price_limit=row["entry_price_limit"],
        entry_price_filled=row["entry_price_filled"],
        exit_time=row["exit_time"],
        exit_price_expected=row["exit_price_expected"],
        exit_price_filled=row["exit_price_filled"],
        exit_reason=row["exit_reason"],
        pnl=row["pnl"],
        r_multiple=row["r_multiple"],
        fill_slippage_entry=row["fill_slippage_entry"],
        fill_slippage_exit=row["fill_slippage_exit"],
    )
