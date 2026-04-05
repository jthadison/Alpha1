import json
from pathlib import Path
from typing import Any

import pandas as pd


def generate_report(portfolio: Any, metrics: dict[str, Any], out_dir: str = "output"):
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # 1. Print Summary
    print("=" * 50)
    print("ALPHA1 BACKTEST SUMMARY")
    print("=" * 50)
    print(f"Total Trades:      {metrics.get('total_trades', 0)}")
    print(f"Total Return:      {metrics.get('total_return_pct', 0.0):.2f}%")
    print(f"Win Rate:          {metrics.get('win_rate', 0.0):.2f}%")
    print(f"Profit Factor:     {metrics.get('profit_factor', 0.0):.2f}")
    print(f"Expectancy (R):    {metrics.get('expectancy_r', 0.0):.2f}")
    print(f"Max Drawdown:      {metrics.get('max_drawdown_pct', 0.0):.2f}%")
    print(f"Sharpe Ratio:      {metrics.get('sharpe_ratio', 0.0):.2f}")
    print(f"Sortino Ratio:     {metrics.get('sortino_ratio', 0.0):.2f}")
    print("-" * 50)
    print("Averages:")
    print(f"  Win ($):         ${metrics.get('avg_win_dlr', 0.0):.2f}")
    print(f"  Loss ($):        ${metrics.get('avg_loss_dlr', 0.0):.2f}")
    print(f"  Bars Held:       {metrics.get('avg_bars_held', 0.0):.1f}")
    print("-" * 50)
    print("Exit Reasons:")
    reasons = metrics.get('exit_reasons', {})
    for reason, count in reasons.items():
        print(f"  {reason}: {count}")
    print("=" * 50)

    # 2. Export Trade Log
    if portfolio.trades:
        trade_data = []
        for t in portfolio.trades:
            trade_data.append({
                "direction": t.direction,
                "entry_time": t.entry_time,
                "entry_price": t.entry_price_raw,
                "entry_price_adjusted": t.entry_price_adjusted,
                "stop_price": t.stop_price,
                "target_price": t.target_price,
                "size": t.size,
                "exit_time": t.exit_time,
                "exit_price": t.exit_price_raw,
                "exit_price_adjusted": t.exit_price_adjusted,
                "exit_reason": t.exit_reason.value if t.exit_reason else None,
                "pnl": t.pnl,
                "r_multiple": t.r_multiple,
                "bars_held": t.bars_held
            })

        df_trades = pd.DataFrame(trade_data)
        trades_csv = out_path / "trade_log.csv"
        df_trades.to_csv(trades_csv, index=False)
        print(f"Saved trade log to {trades_csv}")

    # 3. Export Equity Curve
    if portfolio.equity_dates:
        df_eq = pd.DataFrame({
            "datetime": portfolio.equity_dates,
            "equity": portfolio.equity_curve[1:]  # shift by 1 as initial_equity is at index 0 without date
        })
        # Wait, the first equity is just initial_equity.
        # It's better to align dates properly.
        # portfolio.equity_dates has length N, portfolio.equity_curve has length N+1.
        # Let's insert the start date for the first point or just drop it.
        # If we drop the initial equity, it's fine.

        eq_csv = out_path / "equity_curve.csv"
        df_eq.to_csv(eq_csv, index=False)
        print(f"Saved equity curve to {eq_csv}")

    # 4. Save Metrics JSON
    import numpy as np
    class NumpyEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, np.integer):
                return int(obj)
            if isinstance(obj, np.floating):
                return float(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            return super().default(obj)

    metrics_json = out_path / "metrics.json"
    with open(metrics_json, "w") as f:
        json.dump(metrics, f, indent=4, cls=NumpyEncoder)
    print(f"Saved metrics to {metrics_json}")
