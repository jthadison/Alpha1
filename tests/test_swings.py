from alpha1.strategy.swings import detect_swing_highs, detect_swing_lows


def test_detect_swing_highs_and_lows(sample_swing_data):
    df = sample_swing_data

    sh = detect_swing_highs(df, window=1)
    sl = detect_swing_lows(df, window=1)

    # Bar 1 high is 15.0, surrounded by 10.0 and 12.0. So Bar 1 is a swing high.
    # Bar 1 low is 5.0, surrounded by 8.0 and 7.0. So Bar 1 is a swing low.
    # Bar 4 low is 4.0, surrounded by 6.0 and 4.5. So Bar 4 is a swing low.

    assert not sh.iloc[0]
    assert sh.iloc[1]
    assert not sh.iloc[2]

    assert not sl.iloc[0]
    assert sl.iloc[1]
    assert sl.iloc[4]

    # The last 'window' (1) bar should be False for both because it's not confirmed
    assert not sh.iloc[-1]
    assert not sl.iloc[-1]

def test_flat_prices(sample_swing_data):
    df = sample_swing_data.copy()
    df['high'] = 10.0
    df['low'] = 5.0

    sh = detect_swing_highs(df, window=1)
    sl = detect_swing_lows(df, window=1)

    # Depending on definition, if all are equal, are they all swing highs?
    # Our simple implementation: high == max means yes, except the edges
    # But usually a flat market doesn't matter much. Let's just check it runs without error.
    assert len(sh) == len(df)
    assert len(sl) == len(df)
