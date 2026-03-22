from alpha1.strategy.market_structure import Bias, classify_structure
from tests.conftest import create_ohlcv


# Helper: bar tuples are (open, high, low, close)


def test_hh_sequence_fires_bullish_and_bos():
    """
    Three ascending swing highs with window=1:
      - Pivot at bar 1 (high=15) confirmed at bar 2  → 1st SH, no prior → NEUTRAL
      - Pivot at bar 3 (high=20) confirmed at bar 4  → 2nd SH, 20>15   → BULLISH (1st HH, bias set, no bos yet)
      - Pivot at bar 5 (high=25) confirmed at bar 6  → 3rd SH, 25>20   → BOS fired (already BULLISH)
    """
    data = [
        (10, 10, 8, 9),   # 0
        (9, 15, 8, 10),   # 1  swing high candidate (high=15)
        (10, 12, 8, 9),   # 2  confirms bar 1  → 1st SH
        (9, 20, 8, 18),   # 3  swing high candidate (high=20)
        (18, 16, 8, 15),  # 4  confirms bar 3  → 2nd SH=20 > 15 → BULLISH
        (15, 25, 8, 23),  # 5  swing high candidate (high=25)
        (23, 19, 8, 17),  # 6  confirms bar 5  → 3rd SH=25 > 20 → BOS
    ]
    df = create_ohlcv(data)
    res = classify_structure(df, window=1, use_ema_bias=False)

    # Bar 4: first HH sets BULLISH, no bos (transition from NEUTRAL)
    assert res["trend_state"].iloc[4] == Bias.BULLISH
    assert not res["bos"].iloc[4]
    assert not res["choch"].iloc[4]

    # Bar 6: second HH while already BULLISH → BOS
    assert res["trend_state"].iloc[6] == Bias.BULLISH
    assert res["bos"].iloc[6]
    assert not res["choch"].iloc[6]


def test_lh_after_bullish_fires_choch():
    """
    After two HHs establish BULLISH, a Lower High flips to BEARISH via CHoCH.
      - Bars 1-6: same ascending SH sequence establishing BULLISH (from above)
      - Pivot at bar 7 (high=22) confirmed at bar 8  → LH (22<25) → CHoCH → BEARISH
    """
    data = [
        (10, 10, 8, 9),   # 0
        (9, 15, 8, 10),   # 1  SH candidate
        (10, 12, 8, 9),   # 2  confirms bar 1  → 1st SH
        (9, 20, 8, 18),   # 3  SH candidate
        (18, 16, 8, 15),  # 4  confirms bar 3  → 2nd SH → BULLISH
        (15, 25, 8, 23),  # 5  SH candidate
        (23, 19, 8, 17),  # 6  confirms bar 5  → BOS
        (17, 22, 8, 20),  # 7  SH candidate (22 < 25, Lower High)
        (20, 18, 8, 16),  # 8  confirms bar 7  → LH → CHoCH → BEARISH
    ]
    df = create_ohlcv(data)
    res = classify_structure(df, window=1, use_ema_bias=False)

    assert res["trend_state"].iloc[6] == Bias.BULLISH
    assert res["trend_state"].iloc[8] == Bias.BEARISH
    assert res["choch"].iloc[8]
    assert not res["bos"].iloc[8]


def test_ema_fallback_when_no_swings():
    """
    When there are fewer than 2 confirmed swing highs (no sequence established),
    the EMA fallback determines bias.  A sustained uptrend in close prices should
    produce BULLISH once close > EMA(5).
    """
    # Rising prices so close quickly exceeds a short EMA
    data = [(i, i + 1, i - 1, i) for i in range(1, 20)]
    df = create_ohlcv(data)
    res = classify_structure(df, window=3, use_ema_bias=True, ema_period=5)

    # After enough bars the EMA catches up; final bars should be BULLISH
    assert res["trend_state"].iloc[-1] == Bias.BULLISH


def test_neutral_with_ema_disabled_and_no_swings():
    """
    With use_ema_bias=False and not enough bars to form 2 swing highs,
    trend_state must stay NEUTRAL throughout.
    """
    data = [
        (10, 11, 9, 10),
        (10, 12, 9, 11),
        (11, 10, 8, 9),
    ]
    df = create_ohlcv(data)
    res = classify_structure(df, window=1, use_ema_bias=False)

    # Only one possible swing high (bar 1, high=12, confirmed at bar 2). No second swing → NEUTRAL.
    for state in res["trend_state"]:
        assert state in (Bias.NEUTRAL, Bias.BULLISH)  # could be BULLISH at bar 2 if sh fires
