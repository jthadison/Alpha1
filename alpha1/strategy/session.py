from enum import Enum

import numpy as np
import pandas as pd

from alpha1.config.settings import SessionConfig


class SessionType(Enum):
    ASIAN = "ASIAN"
    LONDON = "LONDON"
    NEWYORK = "NEWYORK"
    OFF_HOURS = "OFF_HOURS"

class SessionBias(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NO_TRADE = "NO_TRADE"

class SessionDetector:
    def __init__(self, df: pd.DataFrame, config: SessionConfig):
        self.df = df
        self.config = config

        # Convert index to target timezone for session timing
        self.tz_idx = df.index.tz_convert(config.timezone)

        self._classify_sessions()
        self._compute_asian_ranges()

    def _classify_sessions(self):
        """Classify each bar into a session type based on London time."""
        times = self.tz_idx.time

        a_start = self.config.get_time("asian_start")
        a_end = self.config.get_time("asian_end")

        l_start = self.config.get_time("london_start")
        l_end = self.config.get_time("london_end")

        ny_start = self.config.get_time("ny_start")
        ny_end = self.config.get_time("ny_end")

        n = len(self.df)
        self.session_types = np.full(n, SessionType.OFF_HOURS, dtype=object)

        # We assume start is inclusive, end is exclusive for simplicity,
        # but 1H bars labeled 'left' from 00:00 to 07:00 include 00:00, 01:00... up to 06:00.
        # Actually, let's use pandas between_time logic or simple time comparisons.

        for i, t in enumerate(times):
            if a_start <= t < a_end:
                self.session_types[i] = SessionType.ASIAN
            elif l_start <= t < l_end:
                self.session_types[i] = SessionType.LONDON
            elif ny_start <= t < ny_end:
                self.session_types[i] = SessionType.NEWYORK

    def _compute_asian_ranges(self):
        """Compute the high/low of the Asian session per day."""
        # Add a date column based on local time
        dates = self.tz_idx.date

        self.asian_high = np.full(len(self.df), np.nan)
        self.asian_low = np.full(len(self.df), np.nan)

        # Calculate daily Asian range
        is_asian = self.session_types == SessionType.ASIAN

        df_asian = self.df[is_asian].copy()
        df_asian['local_date'] = self.tz_idx[is_asian].date

        daily_high = df_asian.groupby('local_date')['high'].max()
        daily_low = df_asian.groupby('local_date')['low'].min()

        # Forward fill the daily values to the rest of the day
        for i, d in enumerate(dates):
            if d in daily_high.index:
                self.asian_high[i] = daily_high[d]
                self.asian_low[i] = daily_low[d]

    def get_session_bias(self) -> pd.Series:
        """
        Determines the session bias per bar.
        Sweep detection:
        - If price > asian_high, we mark it as swept_high.
        - If price < asian_low, we mark it as swept_low.
        - Reversal/bias:
          If swept_high happens, we look for Shorts (SHORT bias).
          If swept_low happens, we look for Longs (LONG bias).
        """
        n = len(self.df)
        biases = np.full(n, SessionBias.NO_TRADE.value, dtype=object)

        self.df['close'].values
        highs = self.df['high'].values
        lows = self.df['low'].values

        swept_high_today = False
        swept_low_today = False

        dates = self.tz_idx.date
        current_date = dates[0] if n > 0 else None

        for i in range(n):
            if dates[i] != current_date:
                current_date = dates[i]
                swept_high_today = False
                swept_low_today = False

            # We only evaluate sweeps during London or NY sessions
            # and if we have a valid Asian range
            sess = self.session_types[i]
            if sess in (SessionType.LONDON, SessionType.NEWYORK):
                ah = self.asian_high[i]
                al = self.asian_low[i]

                if not np.isnan(ah) and not np.isnan(al):
                    # Did we sweep it on this bar or previously today?
                    if highs[i] > ah:
                        swept_high_today = True
                    if lows[i] < al:
                        swept_low_today = True

                    # If both swept, we might be NEUTRAL or just take the last one.
                    # Standard ICT: sweep of one side targets the other.
                    # We will output LONG if we swept low, SHORT if we swept high.
                    if swept_high_today and not swept_low_today:
                        biases[i] = SessionBias.SHORT.value
                    elif swept_low_today and not swept_high_today:
                        biases[i] = SessionBias.LONG.value

        return pd.Series(biases, index=self.df.index, name="session_bias")
