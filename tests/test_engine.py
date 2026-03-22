
from alpha1.backtest.engine import run_backtest
from alpha1.config.settings import StrategyConfig
from tests.conftest import create_ohlcv


def test_run_backtest_empty():
    config = StrategyConfig()
    from alpha1.config.instruments import INSTRUMENT_REGISTRY
    instrument = INSTRUMENT_REGISTRY["EURUSD"]

    # Just check it runs without crashing
    df_4h = create_ohlcv([(10, 15, 5, 10)] * 5, freq="4h")
    df_1h = create_ohlcv([(10, 15, 5, 10)] * 20, freq="1h")
    df_5m = create_ohlcv([(10, 15, 5, 10)] * 240, freq="5min")

    data_dict = {'4h': df_4h, '1h': df_1h, '5min': df_5m}

    portfolio = run_backtest(data_dict, config, instrument)

    assert len(portfolio.trades) == 0
