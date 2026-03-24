"""
Multi-instrument portfolio backtest engine with pending limit-order support.

Mirrors the single-instrument engine (backtest/engine.py) but runs across a
pool of instruments sharing a single equity curve.

Entry mechanic: same limit-order logic as engine.py.
  - Signals are pre-generated per instrument as pending limit orders.
  - Each bar, for each instrument: check limit filled or cancelled.
  - Global max_concurrent cap prevents overleveraging when multiple setups
    fire simultaneously.
"""
import logging
from zoneinfo import ZoneInfo

import pandas as pd

from alpha1.backtest.portfolio import ExitReason, Portfolio, Trade
from alpha1.config.instruments import InstrumentSpec
from alpha1.config.settings import StrategyConfig
from alpha1.strategy.session import SessionDetector, SessionType
from alpha1.strategy.signals import generate_signals

log = logging.getLogger("alpha1.backtest.portfolio_multi")


def run_portfolio_backtest(
    instruments_data: dict[str, dict],
    config: StrategyConfig,
    max_concurrent: int = 3,
) -> Portfolio:
    """
    Multi-instrument portfolio backtest.

    Parameters
    ----------
    instruments_data : {symbol: {'data_dict': dict, 'instrument': InstrumentSpec}}
    config           : shared StrategyConfig
    max_concurrent   : max simultaneous open positions across the portfolio
    """
    portfolio = Portfolio(config.backtest.initial_equity)
    tz = ZoneInfo(config.session.timezone)

    # ── Pre-generate signals (pending limits) per instrument ─────────────────
    sig_by_symbol: dict[str, dict[int, list]] = {}   # {sym: {bar_idx: [Signal]}}
    df_1h_by_symbol: dict[str, pd.DataFrame] = {}
    sess_types_by_symbol: dict[str, list] = {}

    for sym, data in instruments_data.items():
        dd = data["data_dict"]
        df_1h = dd["1h"]
        df_1h_by_symbol[sym] = df_1h

        log.info("Generating signals for %s (%d 1H bars)", sym, len(df_1h))
        signals = generate_signals(
            {"4h": dd["4h"], "1h": dd["1h"], "5min": dd["1h"]}, config
        )
        grouped: dict[int, list] = {}
        for s in signals:
            grouped.setdefault(s.bar_index, []).append(s)
        sig_by_symbol[sym] = grouped

        detector = SessionDetector(df_1h, config.session)
        sess_types_by_symbol[sym] = list(detector.session_types)

    # ── Unified 1H timeline ───────────────────────────────────────────────────
    all_ts: list[pd.Timestamp] = sorted(
        set().union(*(set(df.index) for df in df_1h_by_symbol.values()))
    )

    def _session_end_mins(sess: SessionType) -> int:
        key = "london_end" if sess == SessionType.LONDON else "ny_end"
        t = config.session.get_time(key)
        return t.hour * 60 + t.minute

    # ── Per-instrument state ──────────────────────────────────────────────────
    open_positions: dict[str, Trade] = {}
    pending_limits: dict[str, list] = {sym: [] for sym in instruments_data}  # {sym: [{signal, formed_bar}]}
    session_trade_count: dict[str, int] = {}
    session_type_cache: dict[str, SessionType] = {}

    # ── Bar-by-bar loop ───────────────────────────────────────────────────────
    for ts in all_ts:

        # ── 1. Process exits for all open positions ───────────────────────────
        for sym in list(open_positions.keys()):
            df = df_1h_by_symbol[sym]
            if ts not in df.index:
                continue
            trade = open_positions[sym]
            inst: InstrumentSpec = instruments_data[sym]["instrument"]
            bar_idx = df.index.get_loc(ts)
            bar = df.iloc[bar_idx]
            trade.bars_held += 1

            sess = sess_types_by_symbol[sym][bar_idx]
            if config.exit.close_at_session_end and sess == SessionType.OFF_HOURS:
                portfolio.close_trade(trade, ts, bar["open"], ExitReason.TIME_EXIT, trade.bars_held, inst)
                del open_positions[sym]
                continue

            sl_hit = tp_hit = False
            if trade.direction == "LONG":
                if bar["low"] <= trade.stop_price:
                    sl_hit = True
                if bar["high"] >= trade.target_price:
                    tp_hit = True
            else:
                if bar["high"] >= trade.stop_price:
                    sl_hit = True
                if bar["low"] <= trade.target_price:
                    tp_hit = True

            if sl_hit:
                exit_px = (
                    min(trade.stop_price, bar["open"])
                    if trade.direction == "LONG"
                    else max(trade.stop_price, bar["open"])
                )
                reason = (
                    ExitReason.BREAKEVEN
                    if trade.stop_price == trade.entry_price_raw
                    else ExitReason.STOP_LOSS
                )
                portfolio.close_trade(trade, ts, exit_px, reason, trade.bars_held, inst)
                del open_positions[sym]
            elif tp_hit:
                exit_px = (
                    max(trade.target_price, bar["open"])
                    if trade.direction == "LONG"
                    else min(trade.target_price, bar["open"])
                )
                portfolio.close_trade(trade, ts, exit_px, ExitReason.TARGET, trade.bars_held, inst)
                del open_positions[sym]

        # ── 2. Queue new pending limits & process all per-instrument ─────────
        for sym, sig_map in sig_by_symbol.items():
            df = df_1h_by_symbol[sym]
            if ts not in df.index:
                continue

            bar_idx = df.index.get_loc(ts)
            if bar_idx == 0:
                continue

            prev_bar_idx = bar_idx - 1

            # Queue signals that fired on the previous bar
            for sig in sig_map.get(prev_bar_idx, []):
                pending_limits[sym].append({"signal": sig, "formed_bar": bar_idx})

            # Skip entry processing if already have a position or portfolio full
            if sym in open_positions or len(open_positions) >= max_concurrent:
                # Still prune expired limits
                pending_limits[sym] = [
                    p for p in pending_limits[sym]
                    if (bar_idx - p["formed_bar"]) <= config.entry.limit_order_timeout_bars
                ]
                continue

            sess = sess_types_by_symbol[sym][bar_idx]

            # Reset per-session counter on session change
            if session_type_cache.get(sym) != sess:
                session_type_cache[sym] = sess
                session_trade_count[sym] = 0

            if sess not in (SessionType.LONDON, SessionType.NEWYORK):
                pending_limits[sym] = [
                    p for p in pending_limits[sym]
                    if (bar_idx - p["formed_bar"]) <= config.entry.limit_order_timeout_bars
                ]
                continue

            if session_trade_count.get(sym, 0) >= config.risk.max_trades_per_session:
                continue

            # Entry cutoff
            local_ts = ts.astimezone(tz)
            cur_mins = local_ts.hour * 60 + local_ts.minute
            end_mins = _session_end_mins(sess)
            within_cutoff = (end_mins - cur_mins) <= config.entry.entry_cutoff_minutes_before_close

            bar = df.iloc[bar_idx]
            inst: InstrumentSpec = instruments_data[sym]["instrument"]

            still_pending = []
            filled_this_bar = False

            for p in pending_limits[sym]:
                sig = p["signal"]
                bars_pending = bar_idx - p["formed_bar"]

                if bars_pending > config.entry.limit_order_timeout_bars:
                    continue

                # Cancel if FVG breached
                if sig.direction == "LONG"  and bar["low"]  < sig.cancel_price:
                    continue
                if sig.direction == "SHORT" and bar["high"] > sig.cancel_price:
                    continue

                if filled_this_bar or within_cutoff:
                    still_pending.append(p)
                    continue

                limit_reached = (
                    (sig.direction == "LONG"  and bar["low"]  <= sig.entry_price) or
                    (sig.direction == "SHORT" and bar["high"] >= sig.entry_price)
                )
                if not limit_reached:
                    still_pending.append(p)
                    continue

                fill_price = sig.entry_price
                buffer_price = config.exit.stop_buffer_ticks * inst.tick_size
                if sig.direction == "LONG":
                    actual_stop = sig.stop_price - buffer_price
                    dist = fill_price - actual_stop
                else:
                    actual_stop = sig.stop_price + buffer_price
                    dist = actual_stop - fill_price

                if dist <= 0:
                    continue

                size = portfolio.calculate_position_size(
                    config.risk.risk_per_trade_pct, dist, inst
                )
                if size <= 0:
                    continue

                trade = portfolio.open_trade(
                    direction=sig.direction,
                    time=ts,
                    price=fill_price,
                    stop=actual_stop,
                    target=sig.target_price,
                    size=size,
                    instrument=inst,
                )
                trade.symbol = sym
                open_positions[sym] = trade
                session_trade_count[sym] = session_trade_count.get(sym, 0) + 1
                filled_this_bar = True
                log.debug("Portfolio limit filled: %s %s @ %.5f", sym, sig.direction, fill_price)

            pending_limits[sym] = still_pending

    # ── Close any open positions at end of data ───────────────────────────────
    for sym, trade in open_positions.items():
        df = df_1h_by_symbol[sym]
        inst = instruments_data[sym]["instrument"]
        portfolio.close_trade(
            trade, df.index[-1], df["close"].iloc[-1],
            ExitReason.TIME_EXIT, trade.bars_held, inst,
        )

    return portfolio
