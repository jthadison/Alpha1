import pandas as pd
import pytest

from alpha1.backtest.metrics import calculate_metrics
from alpha1.backtest.portfolio import ExitReason, Trade


def _make_trade(direction, entry_time, raw, adj, stop, init_stop, target, size, pnl, r, reason):
    t = Trade(direction, entry_time, raw, adj, stop, init_stop, target, size, exit_reason=reason)
    t.pnl = pnl
    t.r_multiple = r
    return t


def test_calculate_metrics_basic():
    t1 = _make_trade("LONG",  pd.Timestamp("2024-01-01"), 1.0, 1.0, 0.9, 0.9, 1.2, 1.0,
                     pnl=100.0, r=3.0,  reason=ExitReason.TARGET)
    t2 = _make_trade("SHORT", pd.Timestamp("2024-01-02"), 1.0, 1.0, 1.1, 1.1, 0.8, 1.0,
                     pnl=-50.0, r=-1.0, reason=ExitReason.STOP_LOSS)

    m = calculate_metrics([t1, t2],
                          [1000.0, 1100.0, 1050.0],
                          [pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-02")])

    assert m["total_trades"] == 2
    assert m["win_rate"] == 50.0
    assert m["profit_factor"] == pytest.approx(2.0)
    assert m["expectancy_r"] == pytest.approx(1.0)
    assert m["total_return_pct"] == pytest.approx(5.0)

    # New metrics present and typed
    assert m["sqn"] > 0
    assert isinstance(m["calmar_ratio"], (float, int))
    assert isinstance(m["omega_ratio"], (float, int))
    assert isinstance(m["ulcer_index"], float)
    assert isinstance(m["k_ratio"], float)
    assert isinstance(m["var_95_pct"], float)
    assert isinstance(m["cvar_95_pct"], float)
    assert "SL" not in str(m["exit_reasons"])   # keys are the enum values
    assert ExitReason.TARGET.value in m["exit_reasons"]
    assert m["max_drawdown_pct"] >= 0.0
    assert m["payoff_ratio"] > 0


def test_calculate_metrics_empty():
    m = calculate_metrics([], [1000.0], [pd.Timestamp("2024-01-01")])
    assert m["total_trades"] == 0
    assert m["win_rate"] == 0.0
    assert m["total_return_pct"] == 0.0


def test_all_wins():
    trades = [
        _make_trade("LONG", pd.Timestamp(f"2024-01-{i:02d}"), 1.0, 1.0, 0.9, 0.9, 1.2, 1.0,
                    pnl=100.0, r=1.5, reason=ExitReason.TARGET)
        for i in range(1, 6)
    ]
    equity = [1000.0] + [1000.0 + 100.0 * i for i in range(1, 6)]
    dates  = [pd.Timestamp(f"2024-01-{i:02d}") for i in range(1, 6)]

    m = calculate_metrics(trades, equity, dates)
    assert m["win_rate"] == pytest.approx(100.0)
    assert m["profit_factor"] == float("inf")
    # SQN is undefined (0/0) when all R multiples are identical -- accept 0
    assert m["sqn"] >= 0
    assert m["max_drawdown_pct"] == pytest.approx(0.0, abs=1e-6)
