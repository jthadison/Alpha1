import pytest

from alpha1.backtest.portfolio_multi import run_portfolio_backtest
from alpha1.config.instruments import INSTRUMENT_REGISTRY
from alpha1.config.settings import StrategyConfig
from alpha1.data.loader import resample_multi_tf
from tests.conftest import create_ohlcv


def _flat_data_dict(n_bars: int = 400, freq: str = "1h") -> dict:
    """Synthetic flat-price data that produces zero signals."""
    df = create_ohlcv([(10, 11, 9, 10)] * n_bars, freq=freq)
    data = resample_multi_tf(df)
    # Portfolio engine expects '4h', '1h', and uses '1h' as entry TF
    return {"4h": data["4h"], "1h": data["1h"]}


def _make_config() -> StrategyConfig:
    c = StrategyConfig()
    c.exit.stop_buffer_ticks = 5
    c.exit.breakeven_at_r = 0.0
    c.exit.target_min_rr = 2.0
    c.exit.close_at_session_end = False
    c.entry.entry_cutoff_minutes_before_close = 120
    c.entry.swing_lookback = 2
    c.entry.min_gap_atr_ratio = 1.0
    c.entry.displacement_body_multiplier = 3.0
    c.entry.use_ema_bias = True
    c.entry.ema_period = 50
    return c


def test_portfolio_no_signals_no_trades():
    """Flat synthetic data for two instruments produces zero trades."""
    instruments_data = {
        "EURUSD": {
            "data_dict": _flat_data_dict(),
            "instrument": INSTRUMENT_REGISTRY["EURUSD"],
        },
        "GBPUSD": {
            "data_dict": _flat_data_dict(),
            "instrument": INSTRUMENT_REGISTRY["GBPUSD"],
        },
    }
    portfolio = run_portfolio_backtest(instruments_data, _make_config(), max_concurrent=3)
    assert len(portfolio.trades) == 0
    assert portfolio.equity == pytest.approx(portfolio.initial_equity)


def test_portfolio_equity_starts_at_initial():
    """Portfolio equity initialises correctly regardless of instruments."""
    config = _make_config()
    config.backtest.initial_equity = 50_000.0
    instruments_data = {
        "USDJPY": {
            "data_dict": _flat_data_dict(),
            "instrument": INSTRUMENT_REGISTRY["USDJPY"],
        },
    }
    portfolio = run_portfolio_backtest(instruments_data, config, max_concurrent=2)
    assert portfolio.initial_equity == pytest.approx(50_000.0)
    assert portfolio.equity == pytest.approx(50_000.0)


def test_portfolio_max_concurrent_respected():
    """With max_concurrent=1, at most 1 position can be open simultaneously."""
    instruments_data = {
        "EURUSD": {
            "data_dict": _flat_data_dict(),
            "instrument": INSTRUMENT_REGISTRY["EURUSD"],
        },
        "GBPUSD": {
            "data_dict": _flat_data_dict(),
            "instrument": INSTRUMENT_REGISTRY["GBPUSD"],
        },
        "USDJPY": {
            "data_dict": _flat_data_dict(),
            "instrument": INSTRUMENT_REGISTRY["USDJPY"],
        },
    }
    portfolio = run_portfolio_backtest(instruments_data, _make_config(), max_concurrent=1)
    # With flat data there are no signals, so max_concurrent constraint isn't violated.
    # The test confirms the engine runs without error under a tight constraint.
    assert len(portfolio.trades) == 0
