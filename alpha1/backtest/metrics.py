from typing import Any

import numpy as np
import pandas as pd


def calculate_metrics(trades: list[Any], equity_curve: list[float], equity_dates: list[pd.Timestamp]) -> dict[str, Any]:
    if not trades:
        return {
            "total_trades": 0,
            "total_return_pct": 0.0,
            "win_rate": 0.0
        }

    df_trades = pd.DataFrame([
        {
            "pnl": t.pnl,
            "r_multiple": t.r_multiple,
            "bars_held": t.bars_held,
            "exit_reason": t.exit_reason.value if t.exit_reason else "UNKNOWN"
        } for t in trades
    ])

    initial_equity = equity_curve[0]
    final_equity = equity_curve[-1]
    total_return_pct = ((final_equity / initial_equity) - 1.0) * 100

    wins = df_trades[df_trades['pnl'] > 0]
    losses = df_trades[df_trades['pnl'] <= 0]

    win_rate = (len(wins) / len(df_trades)) * 100

    avg_win_dlr = wins['pnl'].mean() if not wins.empty else 0.0
    avg_loss_dlr = losses['pnl'].mean() if not losses.empty else 0.0

    avg_win_r = wins['r_multiple'].mean() if not wins.empty else 0.0
    avg_loss_r = losses['r_multiple'].mean() if not losses.empty else 0.0

    gross_profit = wins['pnl'].sum() if not wins.empty else 0.0
    gross_loss = abs(losses['pnl'].sum()) if not losses.empty else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')

    expectancy_r = df_trades['r_multiple'].mean()

    # Drawdown
    s_eq = pd.Series(equity_curve)
    rolling_max = s_eq.cummax()
    drawdowns = (s_eq - rolling_max) / rolling_max
    max_drawdown_pct = abs(drawdowns.min()) * 100

    # Ratios (simplified assuming risk free rate = 0, using trade returns or equity returns)
    # Typically calculated on daily equity
    if len(equity_dates) > 1:
        pd.Series(equity_curve, index=[equity_dates[0], *equity_dates]) # first date could be start of backtest
        # Let's resample to daily
        daily_eq = pd.Series(equity_curve[1:], index=equity_dates).resample('D').last().dropna()
        if not daily_eq.empty:
            daily_returns = daily_eq.pct_change().dropna()
            mean_ret = daily_returns.mean()
            std_ret = daily_returns.std()
            sharpe = (mean_ret / std_ret * np.sqrt(252)) if std_ret > 0 else 0.0

            neg_ret = daily_returns[daily_returns < 0]
            sortino_std = neg_ret.std()
            sortino = (mean_ret / sortino_std * np.sqrt(252)) if sortino_std > 0 else 0.0
        else:
            sharpe, sortino = 0.0, 0.0
    else:
        sharpe, sortino = 0.0, 0.0

    avg_bars_held = df_trades['bars_held'].mean()
    max_bars_held = df_trades['bars_held'].max()

    exit_reasons = df_trades['exit_reason'].value_counts().to_dict()

    return {
        "total_trades": len(trades),
        "total_return_pct": total_return_pct,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy_r": expectancy_r,
        "avg_win_dlr": avg_win_dlr,
        "avg_loss_dlr": avg_loss_dlr,
        "avg_win_r": avg_win_r,
        "avg_loss_r": avg_loss_r,
        "max_drawdown_pct": max_drawdown_pct,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "avg_bars_held": avg_bars_held,
        "max_bars_held": max_bars_held,
        "exit_reasons": exit_reasons
    }
