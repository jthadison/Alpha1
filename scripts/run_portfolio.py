from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from alpha1.backtest.metrics import calculate_metrics
from alpha1.backtest.portfolio_multi import run_portfolio_backtest
from alpha1.backtest.report import generate_report
from alpha1.config.instruments import INSTRUMENT_REGISTRY
from alpha1.config.settings import StrategyConfig
from alpha1.data.loader import load_csv, resample_multi_tf

FOREX_PAIRS: list[tuple[str, str]] = [
    ("EURUSD", "data/EURUSD_M1_formatted.csv"),
    ("AUDUSD", "data/AUDUSD_M1_formatted.csv"),
    ("GBPUSD", "data/GBPUSD_M1_formatted.csv"),
    ("NZDUSD", "data/NZDUSD_M1_formatted.csv"),
    ("USDCAD", "data/USDCAD_M1_formatted.csv"),
    ("USDCHF", "data/USDCHF_M1_formatted.csv"),
    ("USDJPY", "data/USDJPY_M1_formatted.csv"),
    ("GBPJPY", "data/GBPJPY_M1_formatted.csv"),
]

FULL_PAIRS: list[tuple[str, str]] = [
    ("GBPJPY", "data/GBPJPY_M1_full.csv"),
    ("USDCAD", "data/USDCAD_M1_full.csv"),
]

TRADE_COLUMNS = [
    "symbol",
    "direction",
    "entry_time",
    "entry_price",
    "entry_price_adjusted",
    "stop_price",
    "target_price",
    "size",
    "exit_time",
    "exit_price",
    "exit_price_adjusted",
    "exit_reason",
    "pnl",
    "r_multiple",
    "bars_held",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Alpha1 multi-instrument portfolio backtest")
    parser.add_argument(
        "--pairs",
        choices=["recent", "full", "all"],
        default="recent",
        help="Pair set to run: recent=8 formatted pairs, full=GBPJPY+USDCAD full history, all=same as recent",
    )
    parser.add_argument("--max-concurrent", type=int, default=3, help="Max open trades across portfolio")
    parser.add_argument("--out", default="output", help="Output directory")
    return parser.parse_args()


def select_pairs(mode: str) -> list[tuple[str, str]]:
    if mode == "full":
        return FULL_PAIRS
    return FOREX_PAIRS


def build_default_config() -> StrategyConfig:
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


def load_instruments(pair_defs: list[tuple[str, str]]) -> dict[str, dict]:
    instruments_data: dict[str, dict] = {}
    for symbol, csv_path in pair_defs:
        if symbol not in INSTRUMENT_REGISTRY:
            raise KeyError(f"Instrument '{symbol}' not found in INSTRUMENT_REGISTRY")

        print(f"Loading {symbol}: {csv_path}")
        df = load_csv(csv_path)
        data_dict = resample_multi_tf(df)
        instruments_data[symbol] = {
            "data_dict": data_dict,
            "instrument": INSTRUMENT_REGISTRY[symbol],
        }
    return instruments_data


def trades_to_dataframe(trades: list) -> pd.DataFrame:
    rows = []
    for trade in trades:
        rows.append(
            {
                "symbol": getattr(trade, "symbol", "") or "UNKNOWN",
                "direction": trade.direction,
                "entry_time": trade.entry_time,
                "entry_price": trade.entry_price_raw,
                "entry_price_adjusted": trade.entry_price_adjusted,
                "stop_price": trade.stop_price,
                "target_price": trade.target_price,
                "size": trade.size,
                "exit_time": trade.exit_time,
                "exit_price": trade.exit_price_raw,
                "exit_price_adjusted": trade.exit_price_adjusted,
                "exit_reason": trade.exit_reason.value if trade.exit_reason else None,
                "pnl": trade.pnl,
                "r_multiple": trade.r_multiple,
                "bars_held": trade.bars_held,
            }
        )

    return pd.DataFrame(rows, columns=TRADE_COLUMNS)


def print_combined_metrics(metrics: dict) -> None:
    combined = pd.DataFrame(
        [
            {
                "return %": metrics.get("total_return_pct", 0.0),
                "WR %": metrics.get("win_rate", 0.0),
                "trades": int(metrics.get("total_trades", 0)),
                "PF": metrics.get("profit_factor", 0.0),
                "Sharpe": metrics.get("sharpe_ratio", 0.0),
                "Sortino": metrics.get("sortino_ratio", 0.0),
                "MaxDD %": metrics.get("max_drawdown_pct", 0.0),
            }
        ]
    )

    print("\n=== Combined Metrics ===")
    print(
        combined.to_string(
            index=False,
            formatters={
                "return %": "{:.2f}".format,
                "WR %": "{:.2f}".format,
                "PF": "{:.2f}".format,
                "Sharpe": "{:.2f}".format,
                "Sortino": "{:.2f}".format,
                "MaxDD %": "{:.2f}".format,
            },
        )
    )


def print_trade_breakdown(trades_df: pd.DataFrame) -> None:
    print("\n=== Per-Instrument Trade Breakdown ===")
    if trades_df.empty:
        print("No trades")
        return

    rows = []
    for symbol, group in trades_df.groupby("symbol", sort=True):
        wins = group[group["pnl"] > 0]
        losses = group[group["pnl"] <= 0]
        total = len(group)
        rows.append(
            {
                "symbol": symbol,
                "trades": total,
                "WR%": (len(wins) / total) * 100.0 if total else 0.0,
                "avg_win$": wins["pnl"].mean() if not wins.empty else 0.0,
                "avg_loss$": losses["pnl"].mean() if not losses.empty else 0.0,
            }
        )

    breakdown = pd.DataFrame(rows).sort_values("symbol")
    print(
        breakdown.to_string(
            index=False,
            formatters={
                "WR%": "{:.2f}".format,
                "avg_win$": "{:.2f}".format,
                "avg_loss$": "{:.2f}".format,
            },
        )
    )


def print_annual_returns(initial_equity: float, equity_curve: list[float], equity_dates: list[pd.Timestamp]) -> None:
    print("\n=== Year-by-Year Equity Returns ===")
    if not equity_dates:
        print("No equity points")
        return

    eq_df = pd.DataFrame(
        {
            "date": pd.to_datetime(equity_dates),
            "equity": equity_curve[1:],
        }
    ).sort_values("date")

    year_end = eq_df.groupby(eq_df["date"].dt.year, sort=True)["equity"].last()

    rows = []
    start_equity = initial_equity
    for year, end_equity in year_end.items():
        annual_return = ((end_equity / start_equity) - 1.0) * 100.0 if start_equity else 0.0
        rows.append({"year": int(year), "annual_return %": annual_return})
        start_equity = end_equity

    annual_df = pd.DataFrame(rows)
    print(annual_df.to_string(index=False, formatters={"annual_return %": "{:.2f}".format}))


def main() -> None:
    args = parse_args()

    pair_defs = select_pairs(args.pairs)
    instruments_data = load_instruments(pair_defs)

    config = build_default_config()

    portfolio = run_portfolio_backtest(
        instruments_data,
        config,
        max_concurrent=args.max_concurrent,
    )

    metrics = calculate_metrics(
        portfolio.trades,
        portfolio.equity_curve,
        portfolio.equity_dates,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    trades_df = trades_to_dataframe(portfolio.trades)
    trades_csv = out_dir / "portfolio_trades.csv"
    trades_df.to_csv(trades_csv, index=False)
    print(f"Saved portfolio trades to {trades_csv}")

    generate_report(portfolio, metrics, out_dir=args.out)

    print_combined_metrics(metrics)
    print_trade_breakdown(trades_df)
    print_annual_returns(portfolio.initial_equity, portfolio.equity_curve, portfolio.equity_dates)


if __name__ == "__main__":
    main()
