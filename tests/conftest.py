import pandas as pd
import pytest


@pytest.fixture
def base_config():
    from alpha1.config.settings import StrategyConfig
    return StrategyConfig()

@pytest.fixture
def eurusd_spec():
    from alpha1.config.instruments import INSTRUMENT_REGISTRY
    return INSTRUMENT_REGISTRY["EURUSD"]

@pytest.fixture
def mes_spec():
    from alpha1.config.instruments import INSTRUMENT_REGISTRY
    return INSTRUMENT_REGISTRY["MES"]

def create_ohlcv(data_list, start_time="2024-01-01 08:00:00", freq="5min"):
    """
    Helper to create deterministic OHLCV DataFrame.
    data_list: List of tuples (open, high, low, close)
    """
    times = pd.date_range(start=start_time, periods=len(data_list), freq=freq, tz="UTC")

    df = pd.DataFrame(data_list, columns=["open", "high", "low", "close"])
    df["volume"] = 100
    df["datetime"] = times
    df.set_index("datetime", inplace=True)
    return df

@pytest.fixture
def sample_swing_data():
    """
    A specific sequence that guarantees a swing high and swing low with window=1.
    """
    # Highs: 10, 15, 12 -> swing high at 15
    # Lows:   8,  5,  7 -> swing low at 5
    data = [
        (9.0, 10.0,  8.0,  9.5),  # 0
        (9.5, 15.0,  5.0, 10.0),  # 1 (Swing High and Swing Low)
        (10.0, 12.0,  7.0, 11.0), # 2
        (11.0, 11.5,  6.0,  8.0), # 3
        (8.0,   9.0,  4.0,  5.0), # 4 (Swing Low at 4)
        (5.0,   6.0,  4.5,  5.5), # 5
    ]
    return create_ohlcv(data)

@pytest.fixture
def sample_fvg_data():
    """
    A sequence that contains a bullish and bearish FVG.
    Bullish FVG at bars 1-3. c1_high < c3_low.
    """
    data = [
        # Bullish FVG
        (10.0, 11.0,  9.0, 10.5), # 0 (c1) high=11
        (10.5, 15.0, 10.0, 14.5), # 1 (c2) big displacement
        (14.5, 16.0, 12.0, 15.5), # 2 (c3) low=12. Gap = 12 - 11 = 1

        # Consolidation / retracement into FVG
        (15.5, 15.8, 11.5, 14.0), # 3 Retraces into 11-12 gap!

        # Bearish FVG
        (14.0, 14.5, 10.0, 10.5), # 4 (c1) low=10
        (10.5, 11.0,  5.0,  5.5), # 5 (c2) big displacement
        (5.5,   8.0,  4.0,  6.0), # 6 (c3) high=8. Gap = 10 - 8 = 2
    ]
    return create_ohlcv(data)
