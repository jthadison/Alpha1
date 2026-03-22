import pandas as pd
from alpha1.config.settings import StrategyConfig
from alpha1.config.instruments import INSTRUMENT_REGISTRY
from alpha1.data.loader import load_csv, resample_multi_tf
from alpha1.backtest.engine import run_backtest
from alpha1.backtest.metrics import calculate_metrics

def main():
    df = load_csv("data/EURUSD_M1_formatted.csv", start_date="2024-01-01")
    data_dict = resample_multi_tf(df)
    instrument = INSTRUMENT_REGISTRY["EURUSD"]

    config = StrategyConfig()
    config.exit.stop_buffer_ticks = 30
    config.exit.breakeven_at_r = 2.0
    config.exit.target_min_rr = 3.0

    portfolio = run_backtest(data_dict, config, instrument)
    metrics = calculate_metrics(portfolio.trades, portfolio.equity_curve, portfolio.equity_dates)

    print("Total Trades:", metrics["total_trades"])
    print("Return:", f"{metrics['total_return_pct']:.2f}%")
    print("Win Rate:", f"{metrics['win_rate']:.2f}%")
    print("Avg Win ($):", metrics["avg_win_dlr"])
    print("Avg Loss ($):", metrics["avg_loss_dlr"])
    print("Exit Reasons:")
    for reason, count in metrics["exit_reasons"].items():
        print(f"  {reason}: {count}")

if __name__ == "__main__":
    main()
