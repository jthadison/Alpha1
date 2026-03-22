"""
Signal generation for the FVG limit-order strategy.

Core insight proved by data:
  - FVG + OB stop has a 62%+ win rate at ideal midpoint entry across 25 years.
  - Trend / session filters add zero incremental win rate.
  - The entire edge comes from entering via limit order at the FVG midpoint,
    not via a market order after a close-based retrace trigger.

Therefore generate_signals:
  1. Detects all FVGs on the entry timeframe.
  2. For each FVG, immediately emits a Signal (= a pending limit order).
  3. Entry price = FVG midpoint.
  4. Stop price  = OB extreme (structural stop), clipped to FVG boundary.
  5. Target      = midpoint ± risk × config.exit.target_min_rr  (fixed at generation).
  6. Cancel price = FVG bottom (longs) / top (shorts):
       if price breaks this level the imbalance is filled and the setup is dead.

No HTF trend filter. No session sweep filter. The retrace back to the midpoint
is itself the quality gate — 15 % of FVGs are never retraced at all.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from alpha1.config.settings import StrategyConfig
from alpha1.strategy.fvg import FVGType, detect_fvgs


@dataclass
class Signal:
    direction: str  # "LONG" or "SHORT"
    entry_price: float   # limit price = FVG midpoint
    stop_price: float   # OB extreme (below FVG bottom for longs)
    target_price: float  # fixed at generation time: midpoint +/- risk * RR
    cancel_price: float  # FVG bottom (long) / top (short): if breached, cancel limit
    bar_index: int
    timestamp: pd.Timestamp


def broadcast_htf_to_ltf(
    ltf_index: pd.DatetimeIndex,
    htf_series: pd.Series,
    htf_duration: pd.Timedelta,
) -> pd.Series:
    """
    Safely broadcasts HTF data to LTF index without lookahead bias.
    Kept as a utility function; no longer used inside generate_signals.
    """
    htf_available_time = htf_series.index + htf_duration
    available_series = pd.Series(htf_series.values, index=htf_available_time).sort_index()
    return available_series.reindex(ltf_index, method="ffill")


def calculate_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = np.abs(df["high"] - df["close"].shift())
    low_close = np.abs(df["low"] - df["close"].shift())
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(window=period).mean()


def generate_signals(
    data_dict: dict[str, pd.DataFrame],
    config: StrategyConfig,
) -> list[Signal]:
    """
    Generates limit-order signals from FVG formations on the entry timeframe.

    data_dict must contain a '5min' key (or whichever timeframe is the entry TF;
    callers substitute the key before passing in).
    """
    df = data_dict["5min"]
    atr = calculate_atr(df, config.entry.atr_period)
    fvgs = detect_fvgs(
        df,
        atr,
        config.entry.min_gap_atr_ratio,
        config.entry.displacement_body_multiplier,
    )

    signals: list[Signal] = []

    for fvg in fvgs:
        i = fvg.formation_bar_index
        if i >= len(df) - 1:
            continue

        is_bull = fvg.fvg_type == FVGType.BULLISH

        # Stop: OB extreme is the structurally significant level.
        # Clip so the stop is always outside the FVG boundary.
        if is_bull:
            stop_price  = fvg.ob_extreme if fvg.ob_extreme is not None else fvg.bottom
            stop_price  = min(stop_price, fvg.bottom)   # must be at or below FVG bottom
            cancel_price = fvg.bottom                   # FVG breached downward → cancel
        else:
            stop_price  = fvg.ob_extreme if fvg.ob_extreme is not None else fvg.top
            stop_price  = max(stop_price, fvg.top)      # must be at or above FVG top
            cancel_price = fvg.top                      # FVG breached upward → cancel

        entry_price = fvg.midpoint
        risk = abs(entry_price - stop_price)
        if risk <= 0:
            continue

        target_price = (
            entry_price + risk * config.exit.target_min_rr
            if is_bull
            else entry_price - risk * config.exit.target_min_rr
        )

        signals.append(
            Signal(
                direction="LONG" if is_bull else "SHORT",
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                cancel_price=cancel_price,
                bar_index=i,
                timestamp=df.index[i],
            )
        )

    return signals
