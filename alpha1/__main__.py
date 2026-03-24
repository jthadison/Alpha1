import argparse
import asyncio
import logging
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

    live_parser = subparsers.add_parser("live", help="Run live trading engine + web dashboard")
    live_parser.add_argument("--config", type=str, default="configs/live.json", help="Path to strategy config JSON")
    live_parser.add_argument("--host", type=str, default=None, help="IBKR host override")
    live_parser.add_argument("--port", type=int, default=None, help="IBKR port override")
    live_parser.add_argument("--client-id", type=int, default=None, dest="client_id", help="IBKR client ID override")
    live_parser.add_argument("--paper", action="store_true", default=None, help="Force paper trading mode")
    live_parser.add_argument(
        "--live-account",
        action="store_true",
        dest="live_account",
        help="Disable paper mode (USE WITH CAUTION)",
    )
    live_parser.add_argument("--web-port", type=int, default=None, dest="web_port", help="Web dashboard port")
    live_parser.add_argument("--web-host", type=str, default=None, dest="web_host", help="Web dashboard bind address (default 127.0.0.1; use 0.0.0.0 to expose on all interfaces)")
    live_parser.add_argument("--instruments", type=str, nargs="+", default=None, help="Override instrument list")

    args = parser.parse_args()

    if args.command == "backtest":
        run_backtest(args.config, args.data, args.instrument, args.out)
    elif args.command == "portfolio":
        run_portfolio(args.pairs, args.max_concurrent, args.out)
    elif args.command == "live":
        asyncio.run(_run_live(args))


async def _run_live(args) -> None:
    """
    Async entry point for the live trading engine.

    Single asyncio.run() call ensures ib_async and uvicorn share the same event
    loop.  Do NOT call ib.run() — let asyncio manage the loop.
    (Adversarial Finding #9)
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("alpha1.live")

    # Load config and apply CLI overrides
    try:
        config = StrategyConfig.from_json(args.config)
    except FileNotFoundError:
        print(f"Config not found: {args.config}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error loading config: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.host:
        config.live.host = args.host
    if args.port:
        config.live.port = args.port
    if args.client_id:
        config.live.client_id = args.client_id
    if args.instruments:
        config.live.instruments = args.instruments
    if args.web_port:
        config.live.web_port = args.web_port
    if args.web_host:
        config.live.web_host = args.web_host
    if getattr(args, "live_account", False):
        config.live.paper = False
    elif args.paper:
        config.live.paper = True

    if not config.live.paper:
        log.warning("=" * 60)
        log.warning("LIVE ACCOUNT MODE — real money at risk!")
        log.warning("IBKR port: %d", config.live.port)
        log.warning("=" * 60)
    else:
        log.info("Paper trading mode (port %d).", config.live.port)

    # Deferred import so base install doesn't require live deps
    try:
        import uvicorn

        from alpha1.live.broker import IBKRBroker
        from alpha1.live.engine import LiveEngine
        from alpha1.live.feed import LiveFeed
        from alpha1.live.server import create_app
        from alpha1.live.state import StateManager
    except ImportError as exc:
        print(
            f"Live trading dependencies not installed: {exc}\nInstall with: pip install -e '.[live]'",
            file=sys.stderr,
        )
        sys.exit(1)

    state = StateManager()
    broker = IBKRBroker(config.live)
    feed = LiveFeed(broker, config)
    engine = LiveEngine(broker, feed, state, config)
    app = create_app(state, broker, engine)

    try:
        await engine.start()
    except Exception as exc:
        log.exception("Engine startup failed: %s", exc)
        sys.exit(1)

    server_cfg = uvicorn.Config(
        app,
        host=config.live.web_host,
        port=config.live.web_port,
        log_level="info",
    )
    server = uvicorn.Server(server_cfg)

    log.info(
        "Dashboard: http://%s:%d",
        config.live.web_host,
        config.live.web_port,
    )

    try:
        await asyncio.gather(server.serve(), engine.run_forever())
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutdown signal received.")
    finally:
        await engine.stop()


if __name__ == "__main__":
    main()
