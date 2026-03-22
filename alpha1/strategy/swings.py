import pandas as pd


def detect_swing_highs(df: pd.DataFrame, window: int) -> pd.Series:
    """
    Detects swing highs.
    A bar 'i' is a swing high if its high is the maximum of the window [i-window, i+window].
    Returns a boolean Series where True indicates a confirmed swing high at that bar.
    The last 'window' bars will always be False as they cannot be confirmed yet.
    """
    # rolling max with center=True puts the window max at 'i'.
    # For a window of N, window_size = 2N + 1
    window_size = 2 * window + 1

    # We need min_periods=window+1 to allow calculation at the start of the dataframe,
    # but strictly speaking, to be a true swing high, it needs N bars before and N bars after.
    # Let's require full N bars before and after.
    roll_max = df['high'].rolling(window=window_size, center=True, min_periods=window_size).max()

    # It's a swing high if the high equals the rolling max
    is_swing_high = (df['high'] == roll_max)

    # The last 'window' bars cannot be confirmed yet, because the rolling window in pandas
    # with center=True will compute with fewer than window_size elements at the end if min_periods allows,
    # but we required min_periods=window_size so they are NaN anyway.
    # We explicitly fillna(False).
    is_swing_high = is_swing_high.fillna(False).astype(bool)

    return is_swing_high

def detect_swing_lows(df: pd.DataFrame, window: int) -> pd.Series:
    """
    Detects swing lows.
    A bar 'i' is a swing low if its low is the minimum of the window [i-window, i+window].
    Returns a boolean Series where True indicates a confirmed swing low at that bar.
    The last 'window' bars will always be False as they cannot be confirmed yet.
    """
    window_size = 2 * window + 1
    roll_min = df['low'].rolling(window=window_size, center=True, min_periods=window_size).min()

    is_swing_low = (df['low'] == roll_min)
    is_swing_low = is_swing_low.fillna(False).astype(bool)

    return is_swing_low
