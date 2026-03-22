import pandas as pd

from alpha1.strategy.fvg import FVGType, detect_fvgs


def test_detect_fvgs_bullish(sample_fvg_data):
    df = sample_fvg_data
    atr = pd.Series([1.0] * len(df), index=df.index)

    fvgs = detect_fvgs(df, atr, min_gap_atr_ratio=0.1, body_multiplier=0.1)

    # Should detect 1 Bullish FVG and 2 Bearish FVGs
    assert len(fvgs) == 3

    bull_fvg = fvgs[0]
    assert bull_fvg.fvg_type == FVGType.BULLISH
    assert bull_fvg.bottom == 11.0
    assert bull_fvg.top == 12.0
    assert bull_fvg.formation_bar_index == 2

    bear_fvg = fvgs[2]
    assert bear_fvg.fvg_type == FVGType.BEARISH
    assert bear_fvg.top == 10.0
    assert bear_fvg.bottom == 8.0
    assert bear_fvg.formation_bar_index == 6

def test_fvg_mitigation(sample_fvg_data):
    df = sample_fvg_data
    atr = pd.Series([1.0] * len(df), index=df.index)

    fvgs = detect_fvgs(df, atr, min_gap_atr_ratio=0.1, body_multiplier=0.1)

    bull_fvg = fvgs[0]
    # Retraces into FVG zone at bar 3 (low is 11.5), then completely fills it at bar 4 (low is 10.0).
    # Our mitigation rule says mitigated when low <= bottom (11.0).
    assert bull_fvg.is_mitigated
    assert bull_fvg.mitigated_bar_index == 4
