from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd


class FVGType(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"

@dataclass
class FVG:
    fvg_type: FVGType
    top: float
    bottom: float
    midpoint: float
    formation_bar_index: int  # index of candle 3 (i+2), the bar that confirmed the FVG
    mitigated_bar_index: int | None = None
    ob_extreme: float | None = None
    @property
    def is_mitigated(self) -> bool:
        return self.mitigated_bar_index is not None

def detect_fvgs(df: pd.DataFrame,
                atr_series: pd.Series,
                min_gap_atr_ratio: float = 0.25,
                body_multiplier: float = 1.5) -> list[FVG]:
    """
    Detects and tracks mitigation of Fair Value Gaps (FVGs).
    Returns a list of FVG objects.
    """
    fvgs: list[FVG] = []

    highs = df['high'].values
    lows = df['low'].values
    opens = df['open'].values
    closes = df['close'].values

    bodies = np.abs(closes - opens)
    # Use rolling mean for average body size
    avg_bodies = pd.Series(bodies).rolling(window=14, min_periods=1).mean().values

    n = len(df)

    # We need 3 bars to form an FVG, so loop from i=0 to n-3
    # Actually loop up to n-3, and current formation bar is i+2
    for i in range(n - 2):
        formation_bar = i + 2

        c1_high = highs[i]
        c1_low = lows[i]
        c3_high = highs[formation_bar]
        c3_low = lows[formation_bar]

        c2_body = bodies[i + 1]
        c2_avg_body = avg_bodies[i + 1]

        atr = atr_series.iloc[formation_bar] if not np.isnan(atr_series.iloc[formation_bar]) else 0.0

        # Bullish FVG
        if c3_low > c1_high:
            gap = c3_low - c1_high
            if gap >= atr * min_gap_atr_ratio and c2_body >= body_multiplier * c2_avg_body:
                ob_extreme = None
                for j in range(i, max(-1, i-10), -1):
                    if closes[j] < opens[j]:
                        ob_extreme = lows[j]
                        break
                fvgs.append(FVG(
                    fvg_type=FVGType.BULLISH,
                    top=c3_low,
                    bottom=c1_high,
                    midpoint=(c3_low + c1_high) / 2.0,
                    formation_bar_index=formation_bar,
                    ob_extreme=ob_extreme
                ))

        # Bearish FVG
        elif c3_high < c1_low:
            gap = c1_low - c3_high
            if gap >= atr * min_gap_atr_ratio and c2_body >= body_multiplier * c2_avg_body:
                ob_extreme = None
                for j in range(i, max(-1, i-10), -1):
                    if closes[j] > opens[j]:
                        ob_extreme = highs[j]
                        break
                fvgs.append(FVG(
                    fvg_type=FVGType.BEARISH,
                    top=c1_low,
                    bottom=c3_high,
                    midpoint=(c1_low + c3_high) / 2.0,
                    formation_bar_index=formation_bar,
                    ob_extreme=ob_extreme
                ))
    # Now simulate mitigation forward
    # For each FVG, find the first bar after its formation where it is fully mitigated
    # Mitigation means price reaches the full FVG or just touches it?
    # Usually "mitigation" means price returns into it.
    # Plan says: "mark FVG as filled when price returns through it" (through meaning closes the gap completely or touches?)
    # "unmitigated (price hasn't returned to fill it yet)... Price retraces into the FVG zone. Entry at FVG midpoint or on a confirmation candle close within the FVG."
    # So if price goes completely through it, it's fully mitigated and invalidated.
    # Let's mark it mitigated if price touches the opposite side of the FVG, OR just touches the FVG?
    # Usually, a FVG is "filled" when price reaches the opposite end (e.g. for Bullish, price drops to bottom = c1_high).
    # Let's track when price touches the midpoint as an entry trigger, but it's "mitigated" (invalidated)
    # if price drops below the bottom (for bullish) or above the top (for bearish).
    # Actually, a common rule is: if it's completely filled, it's mitigated.
    # Let's define mitigated = completely filled.
    # Bullish mitigated: low < bottom. Bearish mitigated: high > top.

    for fvg in fvgs:
        for j in range(fvg.formation_bar_index + 1, n):
            if fvg.fvg_type == FVGType.BULLISH:
                if lows[j] <= fvg.bottom:
                    fvg.mitigated_bar_index = j
                    break
            else:
                if highs[j] >= fvg.top:
                    fvg.mitigated_bar_index = j
                    break

    return fvgs
