from enum import Enum

import numpy as np
import pandas as pd

from alpha1.strategy.swings import detect_swing_highs, detect_swing_lows


class Bias(Enum):
    NEUTRAL = "NEUTRAL"
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


def classify_structure(
    df: pd.DataFrame,
    window: int,
    use_ema_bias: bool = True,
    ema_period: int = 50,
) -> pd.DataFrame:
    """
    Classifies 4H market structure bar-by-bar using two methods in priority order:

    1. HH/HL / LH/LL sequence (primary, swing-based, lookahead-free).
       Swings are confirmed only after `window` completed bars have closed after
       the pivot.  Once two consecutive swing highs establish a Higher High (HH)
       or Lower High (LH) pattern the trend_state updates immediately on the bar
       where the second swing is confirmed -- not when price later breaks the level.
       This is a direct implementation of the plan's HH/HL/LH/LL classification.

    2. EMA(ema_period) fallback (optional, activated by `use_ema_bias=True`).
       Used only when the swing sequence is still NEUTRAL (insufficient confirmed
       swings, or most recent SH and SL sequences disagree).  We compare the
       *previous bar's* EMA to the current close so there is zero lookahead.

    Returns a DataFrame with columns:
      trend_state           : Bias  (BULLISH / BEARISH / NEUTRAL)
      bos                   : bool  (Break of Structure on this bar -- SH sequence event)
      choch                 : bool  (Change of Character on this bar -- counter-trend flip)
      swing_high_confirmed  : bool  (A swing high was confirmed on this bar)
      swing_low_confirmed   : bool  (A swing low was confirmed on this bar)
    """
    sh_series = detect_swing_highs(df, window)
    sl_series = detect_swing_lows(df, window)

    n = len(df)
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    sh_arr = sh_series.values
    sl_arr = sl_series.values

    # ── EMA (shifted one bar to avoid lookahead) ──────────────────────────────
    # ewm().mean() at bar k uses data up to and including bar k (closed bar).
    # We shift by one so that at bar k we only "know" the EMA through bar k-1.
    ema_values: np.ndarray | None = None
    if use_ema_bias:
        ema_series = df["close"].ewm(span=ema_period, adjust=False).mean()
        ema_values = ema_series.shift(1).values  # safe: previous bar's EMA only

    # ── Output arrays ─────────────────────────────────────────────────────────
    trend_state = [Bias.NEUTRAL] * n
    bos = np.zeros(n, dtype=bool)
    choch = np.zeros(n, dtype=bool)
    sh_confirmed = np.zeros(n, dtype=bool)
    sl_confirmed = np.zeros(n, dtype=bool)

    # Confirmed swing sequences tracked as (confirmation_bar_idx, price)
    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []

    # sh_sequence_bias / sl_sequence_bias hold the last directional read from
    # each independent swing sequence.  NEUTRAL means we haven't seen 2 swings yet.
    sh_sequence_bias = Bias.NEUTRAL  # derived from SH sequence (HH vs LH)
    sl_sequence_bias = Bias.NEUTRAL  # derived from SL sequence (HL vs LL)

    for k in range(n):
        # ── Confirm swings that are now fully formed ──────────────────────────
        if k >= window:
            pivot = k - window
            if sh_arr[pivot]:
                sh_confirmed[k] = True
                sh_price = highs[pivot]
                if swing_highs:
                    prev_sh = swing_highs[-1][1]
                    if sh_price > prev_sh:
                        # Higher High → bullish structure event on the SH sequence
                        if sh_sequence_bias == Bias.BULLISH:
                            bos[k] = True      # continuation BOS
                        elif sh_sequence_bias == Bias.BEARISH:
                            choch[k] = True    # counter-trend flip (CHoCH)
                        sh_sequence_bias = Bias.BULLISH
                    elif sh_price < prev_sh:
                        # Lower High → bearish structure event on the SH sequence
                        if sh_sequence_bias == Bias.BEARISH:
                            bos[k] = True
                        elif sh_sequence_bias == Bias.BULLISH:
                            choch[k] = True
                        sh_sequence_bias = Bias.BEARISH
                    # If equal: no sequence change
                swing_highs.append((k, sh_price))

            if sl_arr[pivot]:
                sl_confirmed[k] = True
                sl_price = lows[pivot]
                if swing_lows:
                    prev_sl = swing_lows[-1][1]
                    if sl_price > prev_sl:
                        # Higher Low → bullish SL sequence
                        sl_sequence_bias = Bias.BULLISH
                    elif sl_price < prev_sl:
                        # Lower Low → bearish SL sequence
                        sl_sequence_bias = Bias.BEARISH
                swing_lows.append((k, sl_price))

        # ── Derive trend_state from the two sequences ─────────────────────────
        # Agreement between SH and SL sequences is the strongest signal.
        # When they disagree (e.g. HH but LL), or either is still NEUTRAL,
        # we fall back to EMA direction rather than declaring NEUTRAL.
        if sh_sequence_bias == sl_sequence_bias and sh_sequence_bias != Bias.NEUTRAL:
            # Both sequences agree → high-confidence structural bias
            trend_state[k] = sh_sequence_bias
        elif sh_sequence_bias != Bias.NEUTRAL:
            # Only one sequence has fired; prefer it as a leading signal
            trend_state[k] = sh_sequence_bias
        elif sl_sequence_bias != Bias.NEUTRAL:
            trend_state[k] = sl_sequence_bias
        elif use_ema_bias and ema_values is not None and not np.isnan(ema_values[k]):
            # EMA fallback: no confirmed swing sequence yet
            ema_val = ema_values[k]
            if closes[k] > ema_val:
                trend_state[k] = Bias.BULLISH
            elif closes[k] < ema_val:
                trend_state[k] = Bias.BEARISH
            else:
                trend_state[k] = Bias.NEUTRAL
        else:
            trend_state[k] = Bias.NEUTRAL

    return pd.DataFrame(
        {
            "trend_state": trend_state,
            "bos": bos,
            "choch": choch,
            "swing_high_confirmed": sh_confirmed,
            "swing_low_confirmed": sl_confirmed,
        },
        index=df.index,
    )
