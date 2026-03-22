import pandas as pd

import pytest
from alpha1.backtest.metrics import calculate_metrics
from alpha1.backtest.portfolio import ExitReason, Trade


def test_calculate_metrics():
    t1 = Trade("LONG", pd.Timestamp("2024-01-01"), 1.0, 1.0, 0.9, 0.9, 1.2, 1.0, exit_reason=ExitReason.TARGET)
    t1.pnl = 100.0
    t1.r_multiple = 3.0
    
    t2 = Trade("SHORT", pd.Timestamp("2024-01-02"), 1.0, 1.0, 1.1, 1.1, 0.8, 1.0, exit_reason=ExitReason.STOP_LOSS)
    t2.pnl = -50.0
    t2.r_multiple = -1.0

    trades = [t1, t2]

    equity_curve = [1000.0, 1100.0, 1050.0]
    equity_dates = [pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-02")]

    metrics = calculate_metrics(trades, equity_curve, equity_dates)

    assert metrics["total_trades"] == 2
    assert metrics["win_rate"] == 50.0
    assert metrics["profit_factor"] == 2.0
    assert metrics["expectancy_r"] == 1.0
    assert metrics["total_return_pct"] == pytest.approx(5.0)

def test_calculate_metrics_empty():
    metrics = calculate_metrics([], [1000.0], [pd.Timestamp("2024-01-01")])
    assert metrics["total_trades"] == 0
    assert metrics["win_rate"] == 0.0
    assert metrics["total_return_pct"] == 0.0