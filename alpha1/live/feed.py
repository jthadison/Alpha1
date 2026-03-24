"""
Real-time 5M bar feed for Alpha1 live trading.

Why polling instead of keepUpToDate=True
-----------------------------------------
ib_async's keepUpToDate=True subscription requires a live (real-time) IBKR
market data subscription for the instrument.  Paper trading accounts typically
only have delayed data, which silently starves the live update callbacks.
Historical data (durationStr='60 D', keepUpToDate=False) works on all account
levels because it's served from IBKR's archive (HMDS).

The polling approach:
  1. Load full history on subscribe() — 60 days, same as before.
  2. Start a per-instrument asyncio poll task that sleeps until the next 5M bar
     close (aligned to clock, +5 s buffer), then re-requests the last 30 minutes
     of history.
  3. Compare the last bar timestamp against what we already have.  If a new bar
     is present, rebuild the data_dict and fire callbacks.

Trade-off vs keepUpToDate:
  - Slightly less precise timing (~5-15 s after bar close vs. ~1 s).
  - No dependency on live data subscription.
  - Robust to connection drop / nightly IBKR restart — each poll is a fresh
    IBKR request that will succeed once the connection is restored.
  - Memory is still bounded (trimmed to config.live.history_bars).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from alpha1.config.settings import StrategyConfig
    from alpha1.live.broker import IBKRBroker

log = logging.getLogger("alpha1.live.feed")

# How many seconds after bar close to wait before polling.
# Gives IBKR time to commit the completed bar to HMDS.
_POLL_BUFFER_SECS = 5

# Bar size in seconds (5 minutes)
_BAR_SECS = 300


class LiveFeed:
    """
    Polls IBKR for 5M bars and maintains rolling multi-TF DataFrames.

    Usage:
        feed = LiveFeed(broker, config)
        feed.on_bar_close(my_callback)   # register callback
        await feed.subscribe("MYM")      # load history + start poll loop
        # my_callback(symbol, data_dict) fires ~5-15 s after each 5M bar close
    """

    def __init__(self, broker: IBKRBroker, config: StrategyConfig) -> None:
        self.broker = broker
        self.config = config
        # Most recent multi-TF data_dict per instrument
        self._data_dicts: dict[str, dict[str, pd.DataFrame]] = {}
        # Timestamp of the last bar we processed per instrument
        self._last_bar_ts: dict[str, pd.Timestamp] = {}
        # Active poll tasks
        self._poll_tasks: dict[str, asyncio.Task] = {}
        # Registered bar-close callbacks
        self._callbacks: list[Callable[[str, dict[str, pd.DataFrame]], None]] = []
        self._running = True
        self._subscribe_count: int = 0  # tracks subscription order for poll staggering

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_bar_close(
        self,
        callback: Callable[[str, dict[str, pd.DataFrame]], None],
    ) -> None:
        """Register a callback invoked on every completed 5M bar."""
        self._callbacks.append(callback)

    async def subscribe(self, symbol: str) -> None:
        """
        Load 60 days of 5M history for an instrument and start polling for new bars.

        The initial load may take 15-45 seconds (IBKR HMDS throttling).
        """
        from alpha1.live.contracts import get_what_to_show

        contract = self.broker.get_contract(symbol)
        what = get_what_to_show(symbol)

        log.info("Loading 5-day history for %s (whatToShow=%s)…", symbol, what)
        df = await self._fetch_history(contract, what, duration="5 D")  # ~1440 bars; enough for ATR, swings, FVG

        if df is None or df.empty:
            log.error("%s: failed to load history — no bars returned.", symbol)
            return

        self._update_data_dict(symbol, df)
        if not df.empty:
            self._last_bar_ts[symbol] = df.index[-1]

        log.info(
            "%s: loaded %d historical bars, streaming active. Last bar: %s",
            symbol,
            len(df),
            self._last_bar_ts.get(symbol),
        )

        # Stagger poll starts: 2-second offset per instrument to avoid simultaneous
        # reqHistoricalDataAsync calls through the single IB socket.
        offset = self._subscribe_count * 2
        self._subscribe_count += 1
        task = asyncio.create_task(self._poll_loop(symbol, contract, what, offset_secs=offset))
        self._poll_tasks[symbol] = task

    def get_data_dict(self, symbol: str) -> dict[str, pd.DataFrame] | None:
        return self._data_dicts.get(symbol)

    def unsubscribe(self, symbol: str) -> None:
        task = self._poll_tasks.pop(symbol, None)
        if task and not task.done():
            task.cancel()
        self._data_dicts.pop(symbol, None)
        self._last_bar_ts.pop(symbol, None)
        log.info("%s: poll loop cancelled.", symbol)

    def stop(self) -> None:
        self._running = False
        for task in list(self._poll_tasks.values()):
            if not task.done():
                task.cancel()

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self, symbol: str, contract, what: str, offset_secs: float = 0.0) -> None:
        """
        Sleep until each 5M bar close + buffer, then fetch the latest bars.

        Aligns to 5-minute boundaries on the UTC clock (00:00, 00:05, 00:10…)
        so we don't drift relative to IBKR's bar close times.
        """
        if offset_secs > 0:
            log.debug("%s: poll start staggered by %.0f s.", symbol, offset_secs)
            await asyncio.sleep(offset_secs)
        log.info("%s: poll loop started.", symbol)
        while self._running:
            try:
                sleep_secs = self._secs_until_next_bar_close()
                log.debug(
                    "%s: next poll in %.0f s (at next 5M boundary + %d s buffer).",
                    symbol,
                    sleep_secs,
                    _POLL_BUFFER_SECS,
                )
                await asyncio.sleep(sleep_secs)

                if not self._running:
                    break

                await self._poll_once(symbol, contract, what)

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("%s: error in poll loop — retrying after 30 s.", symbol)
                await asyncio.sleep(30)

        log.info("%s: poll loop stopped.", symbol)

    def _secs_until_next_bar_close(self) -> float:
        """
        Seconds until the next 5M bar close on the UTC clock, plus buffer.

        IBKR aligns 5M bars to 00:00 UTC (00:00, 00:05, 00:10, …).
        We add _POLL_BUFFER_SECS so the bar is committed to HMDS before we query.
        """
        now = datetime.now(UTC).timestamp()
        secs_into_bar = now % _BAR_SECS
        secs_to_close = _BAR_SECS - secs_into_bar + _POLL_BUFFER_SECS
        # Minimum 5 seconds to avoid hammering IBKR immediately
        return max(5.0, secs_to_close)

    async def _poll_once(self, symbol: str, contract, what: str) -> None:
        """Fetch the last 30 minutes of bars and fire callbacks for new ones."""
        log.info("%s: polling for new bars (last_known=%s)…", symbol, self._last_bar_ts.get(symbol))
        df = await self._fetch_history(contract, what, duration="3600 S")  # 1 hour = 12 bars
        if df is None or df.empty:
            log.info("%s: poll returned no bars (fetch failed or empty).", symbol)
            return

        last_known = self._last_bar_ts.get(symbol)

        # Find bars that are newer than what we already have
        if last_known is not None:
            # Exclude the most recent bar — it may still be in progress.
            # generate_signals() also guards this, but being explicit here
            # prevents duplicate signals if the final completed bar's timestamp
            # matches the in-progress bar.
            completed = df[df.index < df.index[-1]]
            new_bars = completed[completed.index > last_known]
        else:
            # No prior state: treat all but the last as completed
            new_bars = df.iloc[:-1]

        if new_bars.empty:
            log.info(
                "%s: poll OK — no new bars beyond last_known=%s (latest in fetch=%s).", symbol, last_known, df.index[-1]
            )
            return

        log.info(
            "%s: %d new bar(s) detected. Latest: %s  close=%.4f",
            symbol,
            len(new_bars),
            new_bars.index[-1],
            new_bars.iloc[-1]["close"],
        )

        # Merge new bars into our rolling dataset and update data_dict
        self._merge_new_bars(symbol, df)
        self._last_bar_ts[symbol] = new_bars.index[-1]
        data_dict = self._data_dicts[symbol]

        # Fire one callback per new bar, in order
        for _, _bar_row in new_bars.iterrows():
            for cb in self._callbacks:
                result = cb(symbol, data_dict)
                if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)  # noqa: RUF006

    # ------------------------------------------------------------------
    # IBKR fetch helpers
    # ------------------------------------------------------------------

    async def _fetch_history(self, contract, what: str, duration: str) -> pd.DataFrame | None:
        """
        Fetch historical bars from IBKR HMDS.  Returns a UTC-indexed DataFrame
        or None on failure.

        keepUpToDate=False: works on all account levels including delayed-only.
        """

        try:
            bars = await self.broker.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting="5 mins",
                whatToShow=what,
                useRTH=False,
                keepUpToDate=False,
                timeout=120,  # HMDS farms can take >60 s to respond after going dormant
            )
        except Exception:
            log.exception("reqHistoricalDataAsync failed for %s.", contract.symbol)
            return None

        if not bars:
            return None

        return self._bars_to_df(bars)

    def _bars_to_df(self, bars) -> pd.DataFrame | None:
        """Convert ib_async BarDataList to a clean UTC-indexed OHLCV DataFrame."""
        from ib_async import util

        df = util.df(bars)
        if df is None or df.empty:
            return None

        df = df.rename(columns={"date": "datetime"})
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
        df = df.set_index("datetime")

        for col in ("open", "high", "low", "close", "volume"):
            if col not in df.columns:
                df[col] = 0.0 if col == "volume" else None

        return df[["open", "high", "low", "close", "volume"]].copy()

    # ------------------------------------------------------------------
    # Data management
    # ------------------------------------------------------------------

    def _update_data_dict(self, symbol: str, df: pd.DataFrame) -> None:
        """Rebuild the multi-TF data_dict from a full bar DataFrame."""
        from alpha1.data.loader import resample_multi_tf

        df = self._trim(df)
        if not df.index.is_monotonic_increasing:
            df = df.sort_index()

        self._data_dicts[symbol] = resample_multi_tf(df)

    def _merge_new_bars(self, symbol: str, fresh_df: pd.DataFrame) -> None:
        """
        Merge freshly fetched bars into the existing rolling dataset.

        Takes the most recent bars from fresh_df and appends any timestamps
        not already present.  This is simpler and safer than a full merge:
        historical bars don't change, so we only need to append.
        """
        from alpha1.data.loader import resample_multi_tf

        existing_dict = self._data_dicts.get(symbol)
        if existing_dict is None:
            self._update_data_dict(symbol, fresh_df)
            return

        existing_5m = existing_dict.get("5min", pd.DataFrame())
        last_known = existing_5m.index[-1] if not existing_5m.empty else None

        new_rows = fresh_df[fresh_df.index > last_known] if last_known is not None else fresh_df

        if new_rows.empty:
            return

        combined = pd.concat([existing_5m, new_rows]).sort_index()
        combined = self._trim(combined)
        self._data_dicts[symbol] = resample_multi_tf(combined)

    def _trim(self, df: pd.DataFrame) -> pd.DataFrame:
        """Trim to config.live.history_bars to bound memory."""
        max_bars = self.config.live.history_bars
        if len(df) > max_bars:
            return df.iloc[-max_bars:]
        return df
