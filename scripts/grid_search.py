import itertools

import pandas as pd

from alpha1.backtest.engine import run_backtest
from alpha1.backtest.metrics import calculate_metrics
from alpha1.config.instruments import INSTRUMENT_REGISTRY
from alpha1.config.settings import StrategyConfig
from alpha1.data.loader import load_csv, resample_multi_tf


def main():
    print("Loading data...")
    df = load_csv("data/EURUSD_M1_formatted.csv", start_date="2024-01-01")
    data_dict = resample_multi_tf(df)
    instrument = INSTRUMENT_REGISTRY["EURUSD"]

    # Parameters to test
    stop_buffers = [8, 20, 30] # ticks (0.8 pips, 2.0 pips, 3.0 pips)
    breakeven_rs = [0, 1.0, 2.0] # 0 means off
    # Note: Target is strictly defined by 4H structure in the code. We can change the target_min_rr to filter.
    target_min_rrs = [1.5, 3.0] # test taking setups with closer targets

    results = []

    combinations = list(itertools.product(stop_buffers, breakeven_rs, target_min_rrs))
    total = len(combinations)

    print(f"Running {total} combinations...")

    for i, (buffer, be_r, rr) in enumerate(combinations, 1):
        config = StrategyConfig()
        config.exit.stop_buffer_ticks = buffer
        config.exit.breakeven_at_r = be_r
        config.exit.target_min_rr = rr

        portfolio = run_backtest(data_dict, config, instrument)
        metrics = calculate_metrics(portfolio.trades, portfolio.equity_curve, portfolio.equity_dates)

        results.append({
            "stop_buffer": buffer,
            "breakeven_r": be_r,
            "min_rr": rr,
            "trades": metrics["total_trades"],
            "win_rate": metrics["win_rate"],
            "return_pct": metrics["total_return_pct"],
            "profit_factor": metrics["profit_factor"],
            "expectancy_r": metrics["expectancy_r"],
            "sharpe": metrics["sharpe_ratio"],
            "drawdown_pct": metrics["max_drawdown_pct"]
        })

        print(
            f"[{i}/{total}] buffer={buffer}, be_r={be_r}, min_rr={rr} -> "
            f"Return: {metrics['total_return_pct']:.2f}% | "
            f"WR: {metrics['win_rate']:.2f}% | "
            f"Trades: {metrics['total_trades']}"
        )

    df_results = pd.DataFrame(results)
    df_results = df_results.sort_values("return_pct", ascending=False)

    print("\nTop 5 Results:")
    print(df_results.head(5).to_string(index=False))

    df_results.to_csv("output/grid_search_results.csv", index=False)
    print("\nFull results saved to output/grid_search_results.csv")

if __name__ == "__main__":
    main()
