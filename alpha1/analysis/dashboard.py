from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from alpha1.backtest.portfolio import Trade


def plot_dashboard(
    trades: list[Trade],
    equity_curve: list[float],
    equity_dates: list[pd.Timestamp],
    out_dir: str = "output",
):
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if not trades or not equity_dates:
        print("Not enough data to plot dashboard.")
        return

    df_eq = pd.DataFrame({
        "datetime": equity_dates,
        "equity": equity_curve[1:]  # shift by 1 as initial_equity is at index 0 without date
    }).set_index("datetime")

    df_trades = pd.DataFrame([{
        "entry_time": t.entry_time,
        "pnl": t.pnl,
        "r_multiple": t.r_multiple,
        "direction": t.direction
    } for t in trades])

    # Set up matplotlib style
    plt.style.use('ggplot')

    # 1. Equity Curve & Drawdown
    _fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), gridspec_kw={'height_ratios': [3, 1]})
    ax1.plot(df_eq.index, df_eq['equity'], color='blue', linewidth=1.5)
    ax1.set_title("Equity Curve")
    ax1.set_ylabel("Account Balance ($)")

    rolling_max = df_eq['equity'].cummax()
    drawdown = (df_eq['equity'] - rolling_max) / rolling_max * 100
    ax2.fill_between(drawdown.index, drawdown, 0, color='red', alpha=0.3)
    ax2.set_title("Drawdown (%)")
    ax2.set_ylabel("Drawdown (%)")

    plt.tight_layout()
    plt.savefig(out_path / "equity_drawdown.png")
    plt.close()

    # 2. R-Multiple Distribution
    plt.figure(figsize=(10, 6))
    plt.hist(df_trades['r_multiple'], bins=20, color='purple', edgecolor='black', alpha=0.7)
    plt.axvline(x=0, color='red', linestyle='dashed', linewidth=2)
    plt.title("R-Multiple Distribution")
    plt.xlabel("R-Multiple")
    plt.ylabel("Frequency")
    plt.savefig(out_path / "r_multiple_dist.png")
    plt.close()

    # 3. Monthly Returns Heatmap
    if len(df_eq) > 0:
        df_monthly = df_eq.resample('M').last().pct_change() * 100
        df_monthly['Year'] = df_monthly.index.year
        df_monthly['Month'] = df_monthly.index.month

        pivot = df_monthly.pivot(index='Year', columns='Month', values='equity')

        plt.figure(figsize=(10, 6))
        plt.imshow(pivot, cmap='RdYlGn', aspect='auto')
        plt.colorbar(label='Return (%)')
        plt.title("Monthly Returns Heatmap")
        plt.yticks(range(len(pivot.index)), pivot.index)
        months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        plt.xticks(range(len(pivot.columns)), months[:len(pivot.columns)])

        # Add text annotations
        for i in range(len(pivot.index)):
            for j in range(len(pivot.columns)):
                val = pivot.iloc[i, j]
                if not np.isnan(val):
                    plt.text(j, i, f"{val:.1f}%", ha='center', va='center', color='black')

        plt.tight_layout()
        plt.savefig(out_path / "monthly_heatmap.png")
        plt.close()

    print(f"Dashboard charts saved to {out_path}")
