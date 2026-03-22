import argparse
import sys

from alpha1.analysis.dashboard import plot_dashboard
from alpha1.backtest.engine import run_backtest as execute_backtest
from alpha1.backtest.metrics import calculate_metrics
from alpha1.backtest.portfolio_multi import run_portfolio_backtest
from alpha1.backtest.report import generate_report
from alpha1.config.instruments import INSTRUMENT_REGISTRY
from alpha1.config.settings import StrategyConfig
from alpha1.data.loader import load_csv, resample_multi_tf

FOREX_PAIRS = [
    ("EURUSD", "data/EURUSD_M1_formatted.csv"),
    ("AUDUSD", "data/AUDUSD_M1_formatted.csv"),
    ("GBPUSD", "data/GBPUSD_M1_formatted.csv"),
    ("NZDUSD", "data/NZDUSD_M1_formatted.csv"),
    ("USDCAD", "data/USDCAD_M1_formatted.csv"),
    ("USDCHF", "data/USDCHF_M1_formatted.csv"),
    ("USDJPY", "data/USDJPY_M1_formatted.csv"),
    ("GBPJPY", "data/GBPJPY_M1_formatted.csv"),
]
FULL_PAIRS = [
    ("GBPJPY", "data/GBPJPY_M1_full.csv"),
    ("USDCAD", "data/USDCAD_M1_full.csv"),
]
PAIR_PRESETS = {
    "forex": FOREX_PAIRS,
    "full": FULL_PAIRS,
}


def run_backtest(config_path: str, data_path: str, instrument: str, out_dir: str):
    print(f"Loading config from {config_path}...")
    try:
        config = StrategyConfig.from_json(config_path)
    except Exception as e:
        print(f"Error loading config: {e}", file=sys.stderr)
        sys.exit(1)

    if instrument not in INSTRUMENT_REGISTRY:
        print(f"Error: Unknown instrument '{instrument}'", file=sys.stderr)
        sys.exit(1)

    inst_spec = INSTRUMENT_REGISTRY[instrument]

    print(f"Loading data from {data_path}...")
    try:
        df = load_csv(data_path, start_date=config.backtest.start_date, end_date=config.backtest.end_date)
    except Exception as e:
        print(f"Error loading data: {e}", file=sys.stderr)
        sys.exit(1)

    print("Resampling timeframes...")
    data_dict = resample_multi_tf(df)

    print(f"Starting backtest for {instrument}...")
    portfolio = execute_backtest(data_dict, config, inst_spec)

    print("Calculating metrics...")
    metrics = calculate_metrics(portfolio.trades, portfolio.equity_curve, portfolio.equity_dates)

    print(f"Generating report in '{out_dir}'...")
    generate_report(portfolio, metrics, out_dir=out_dir)

    print("Generating dashboard charts...")
    plot_dashboard(portfolio.trades, portfolio.equity_curve, portfolio.equity_dates, out_dir=out_dir)

    print("Backtest completed successfully.")


def build_portfolio_config() -> StrategyConfig:
    config = StrategyConfig()
    config.exit.stop_buffer_ticks = 5
    config.exit.breakeven_at_r = 0.0
    config.exit.target_min_rr = 2.0
    config.exit.close_at_session_end = False
    config.entry.entry_cutoff_minutes_before_close = 120
    config.entry.swing_lookback = 2
    config.entry.min_gap_atr_ratio = 1.0
    config.entry.displacement_body_multiplier = 3.0
    config.entry.use_ema_bias = True
    config.entry.ema_period = 50
    return config


def run_portfolio(pairs: str, max_concurrent: int, out_dir: str):
    if pairs not in PAIR_PRESETS:
        print(f"Error: Unknown pair preset '{pairs}'", file=sys.stderr)
        sys.exit(1)

    config = build_portfolio_config()
    instruments_data: dict[str, dict] = {}

    for symbol, csv_path in PAIR_PRESETS[pairs]:
        print(f"Loading {symbol} from {csv_path}...")
        try:
            df = load_csv(
                csv_path,
                start_date=config.backtest.start_date,
                end_date=config.backtest.end_date,
            )
        except Exception as e:
            print(f"Error loading {symbol} data: {e}", file=sys.stderr)
            sys.exit(1)

        if symbol not in INSTRUMENT_REGISTRY:
            print(f"Error: Unknown instrument '{symbol}'", file=sys.stderr)
            sys.exit(1)

        instruments_data[symbol] = {
            "data_dict": resample_multi_tf(df),
            "instrument": INSTRUMENT_REGISTRY[symbol],
        }

    print("Starting portfolio backtest...")
    portfolio = run_portfolio_backtest(
        instruments_data=instruments_data,
        config=config,
        max_concurrent=max_concurrent,
    )

    print("Calculating metrics...")
    metrics = calculate_metrics(portfolio.trades, portfolio.equity_curve, portfolio.equity_dates)
    print("Total Trades:", metrics.get("total_trades", 0))
    print("Return:", f"{metrics.get('total_return_pct', 0.0):.2f}%")
    print("Win Rate:", f"{metrics.get('win_rate', 0.0):.2f}%")

    print(f"Generating report in '{out_dir}'...")
    generate_report(portfolio, metrics, out_dir=out_dir)
    print("Portfolio backtest completed successfully.")


def main():
    parser = argparse.ArgumentParser(description="Alpha1 Intraday FVG Backtester")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backtest_parser = subparsers.add_parser("backtest", help="Run backtest")
    backtest_parser.add_argument("--config", type=str, default="configs/default.json", help="Path to config JSON")
    backtest_parser.add_argument("--data", type=str, required=True, help="Path to OHLCV CSV")
    backtest_parser.add_argument("--instrument", type=str, required=True, help="Instrument symbol (e.g. EURUSD, MES)")
    backtest_parser.add_argument("--out", type=str, default="output", help="Output directory")

    portfolio_parser = subparsers.add_parser("portfolio", help="Run portfolio backtest")
    portfolio_parser.add_argument(
        "--pairs",
        type=str,
        choices=sorted(PAIR_PRESETS.keys()),
        default="forex",
        help="Pair set to run (forex or full)",
    )
    portfolio_parser.add_argument(
        "--max-concurrent",
        type=int,
        default=3,
        help="Max simultaneous open positions",
    )
    portfolio_parser.add_argument(
        "--out",
        type=str,
        default="output/portfolio",
        help="Output directory",
    )

    args = parser.parse_args()

    if args.command == "backtest":
        run_backtest(args.config, args.data, args.instrument, args.out)
    elif args.command == "portfolio":
        run_portfolio(args.pairs, args.max_concurrent, args.out)


if __name__ == "__main__":
    main()
