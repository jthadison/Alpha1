"""
Bar-by-bar backtest engine with pending limit-order support.

Entry mechanic (the critical change vs the old implementation):
  - Signals are generated at FVG formation bar N with entry_price = FVG midpoint.
  - The engine queues each signal as a *pending limit order*.
  - On every subsequent bar, the engine checks whether the low (longs) or high
    (shorts) touched the limit price and fills at that exact price.
  - If price breaches the cancel_price (FVG bottom for longs / top for shorts)
    before filling, the limit is cancelled — the imbalance is considered dead.
  - Limits that remain unfilled after config.entry.limit_order_timeout_bars bars
    are cancelled automatically.

This replaces the old close-based retrace trigger which was proven to destroy
the 62 % win rate present at the FVG midpoint.
"""
import logging
from typing import Optional

import pandas as pd

from alpha1.backtest.portfolio import ExitReason, Portfolio, Trade
from alpha1.config.instruments import InstrumentSpec
from alpha1.config.settings import StrategyConfig
from alpha1.strategy.session import SessionDetector, SessionType
from alpha1.strategy.signals import Signal, generate_signals

log = logging.getLogger("alpha1.backtest.engine")


def run_backtest(
    data_dict: dict[str, pd.DataFrame],
    config: StrategyConfig,
    instrument: InstrumentSpec,
) -> Portfolio:
    """
    Runs a single-instrument bar-by-bar backtest.

    data_dict must contain '4h', '1h', and '5min' keys.
    For a 1H entry strategy, pass data_dict['1h'] as both '1h' and '5min'.
    """
    df = data_dict["5min"]
    n = len(df)

    portfolio = Portfolio(config.backtest.initial_equity)
    signals = generate_signals(data_dict, config)

    # Group signals by their formation bar index.
    signals_by_bar: dict[int, list[Signal]] = {}
    for s in signals:
        signals_by_bar.setdefault(s.bar_index, []).append(s)

    detector = SessionDetector(df, config.session)
    session_types = detector.session_types

    opens  = df["open"].values
    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values
    times  = df.index

    # State
    open_trade: Optional[Trade] = None
    pending_limits: list[dict] = []   # [{"signal": Signal, "formed_bar": int}, ...]
    trades_this_session: int = 0
    current_session: SessionType = session_types[0]

    for i in range(1, n):
        sess = session_types[i]

        # Reset per-session trade counter when session type changes
        if sess != current_session:
            current_session = sess
            trades_this_session = 0

        # ── 1. Manage open trade ───────────────────────────────────────────
        if open_trade:
            open_trade.bars_held += 1

            # Time exit at session boundary
            if config.exit.close_at_session_end and sess == SessionType.OFF_HOURS:
                portfolio.close_trade(
                    open_trade, times[i], opens[i],
                    ExitReason.TIME_EXIT, open_trade.bars_held, instrument,
                )
                open_trade = None
                continue

            sl_hit = tp_hit = False
            if open_trade.direction == "LONG":
                if lows[i]  <= open_trade.stop_price:   sl_hit = True
                if highs[i] >= open_trade.target_price: tp_hit = True
            else:
                if highs[i] >= open_trade.stop_price:   sl_hit = True
                if lows[i]  <= open_trade.target_price: tp_hit = True

            # Ambiguous bar (both hit): SL wins — conservative
            if sl_hit:
                exit_px = (
                    min(open_trade.stop_price, opens[i])
                    if open_trade.direction == "LONG"
                    else max(open_trade.stop_price, opens[i])
                )
                reason = (
                    ExitReason.BREAKEVEN
                    if open_trade.stop_price == open_trade.entry_price_raw
                    else ExitReason.STOP_LOSS
                )
                portfolio.close_trade(open_trade, times[i], exit_px, reason, open_trade.bars_held, instrument)
                open_trade = None
            elif tp_hit:
                exit_px = (
                    max(open_trade.target_price, opens[i])
                    if open_trade.direction == "LONG"
                    else min(open_trade.target_price, opens[i])
                )
                portfolio.close_trade(open_trade, times[i], exit_px, ExitReason.TARGET, open_trade.bars_held, instrument)
                open_trade = None

            # Breakeven management
            if open_trade and config.exit.breakeven_at_r > 0:
                if open_trade.direction == "LONG":
                    risk = open_trade.entry_price_raw - open_trade.initial_stop_price
                    if risk > 0:
                        current_r = (highs[i] - open_trade.entry_price_raw) / risk
                        if current_r >= config.exit.breakeven_at_r and open_trade.stop_price < open_trade.entry_price_raw:
                            open_trade.stop_price = open_trade.entry_price_raw
                else:
                    risk = open_trade.initial_stop_price - open_trade.entry_price_raw
                    if risk > 0:
                        current_r = (open_trade.entry_price_raw - lows[i]) / risk
                        if current_r >= config.exit.breakeven_at_r and open_trade.stop_price > open_trade.entry_price_raw:
                            open_trade.stop_price = open_trade.entry_price_raw

        # ── 2. Queue new pending limits from signals formed on bar i-1 ────
        for sig in signals_by_bar.get(i - 1, []):
            pending_limits.append({"signal": sig, "formed_bar": i})

        # ── 3. Process pending limits ──────────────────────────────────────
        if open_trade:
            continue  # single position per instrument

        # Session gate — only fill limits inside active sessions
        if sess not in (SessionType.LONDON, SessionType.NEWYORK):
            # Still age/cancel expired limits even outside sessions
            pending_limits = [
                p for p in pending_limits
                if (i - p["formed_bar"]) <= config.entry.limit_order_timeout_bars
            ]
            continue

        # Entry cutoff: don't open trades too close to session end
        local_ts = times[i].astimezone(__import__("zoneinfo").ZoneInfo(config.session.timezone))
        end_time = config.session.get_time(
            "london_end" if sess == SessionType.LONDON else "ny_end"
        )
        cur_mins = local_ts.hour * 60 + local_ts.minute
        end_mins = end_time.hour * 60 + end_time.minute
        within_cutoff = (end_mins - cur_mins) <= config.entry.entry_cutoff_minutes_before_close

        still_pending = []
        filled = False

        for p in pending_limits:
            sig = p["signal"]
            bars_pending = i - p["formed_bar"]

            # Expire old limits
            if bars_pending > config.entry.limit_order_timeout_bars:
                continue

            # Cancel if FVG has been breached (imbalance destroyed)
            if sig.direction == "LONG"  and lows[i]  < sig.cancel_price:
                continue
            if sig.direction == "SHORT" and highs[i] > sig.cancel_price:
                continue

            # Skip fill if session limit reached or inside cutoff window
            if filled or within_cutoff or trades_this_session >= config.risk.max_trades_per_session:
                still_pending.append(p)
                continue

            # Check limit fill
            limit_reached = (
                (sig.direction == "LONG"  and lows[i]  <= sig.entry_price) or
                (sig.direction == "SHORT" and highs[i] >= sig.entry_price)
            )
            if not limit_reached:
                still_pending.append(p)
                continue

            # Fill at the limit price
            fill_price = sig.entry_price
            buffer_price = config.exit.stop_buffer_ticks * instrument.tick_size
            if sig.direction == "LONG":
                actual_stop = sig.stop_price - buffer_price
                dist = fill_price - actual_stop
            else:
                actual_stop = sig.stop_price + buffer_price
                dist = actual_stop - fill_price

            if dist <= 0:
                continue

            size = portfolio.calculate_position_size(
                config.risk.risk_per_trade_pct, dist, instrument
            )
            if size <= 0:
                continue

            open_trade = portfolio.open_trade(
                direction=sig.direction,
                time=times[i],
                price=fill_price,
                stop=actual_stop,
                target=sig.target_price,
                size=size,
                instrument=instrument,
            )
            trades_this_session += 1
            filled = True
            log.debug("Limit filled: %s %s @ %.5f  bar=%d", sig.direction, instrument.symbol, fill_price, i)

        pending_limits = still_pending

    # End-of-data: close any open trade
    if open_trade:
        portfolio.close_trade(
            open_trade, times[-1], closes[-1],
            ExitReason.TIME_EXIT, open_trade.bars_held, instrument,
        )

    return portfolio
