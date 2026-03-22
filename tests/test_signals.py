import pandas as pd

from alpha1.config.settings import StrategyConfig
from alpha1.strategy.signals import broadcast_htf_to_ltf, generate_signals
from tests.conftest import create_ohlcv


def test_broadcast_htf_to_ltf():
    # HTF 1H bars
    htf_times = pd.date_range("2024-01-01 08:00:00", periods=2, freq="1h", tz="UTC")
    htf_series = pd.Series([10, 20], index=htf_times)

    # LTF 5M bars
    ltf_times = pd.date_range("2024-01-01 08:00:00", periods=24, freq="5min", tz="UTC")

    # At 08:05, the 08:00 1H bar hasn't closed yet. So it should be NaN.
    # At 09:00, the 08:00 1H bar closes and becomes available.
    res = broadcast_htf_to_ltf(ltf_times, htf_series, htf_duration=pd.Timedelta(hours=1))

    # Index 11 is 08:55
    assert pd.isna(res.iloc[11])
    # Index 12 is 09:00
    assert res.iloc[12] == 10
    assert res.iloc[-1] == 10

def test_generate_signals_empty():
    config = StrategyConfig()

    df_4h = create_ohlcv([(10, 15, 5, 10)] * 5, freq="4h")
    df_1h = create_ohlcv([(10, 15, 5, 10)] * 20, freq="1h")
    df_5m = create_ohlcv([(10, 15, 5, 10)] * 240, freq="5min")

    data_dict = {'4h': df_4h, '1h': df_1h, '5min': df_5m}

    signals = generate_signals(data_dict, config)
    assert len(signals) == 0
