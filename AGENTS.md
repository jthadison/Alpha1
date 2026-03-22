# Repository Guidelines

## Project Overview

Alpha1 is an intraday multi-timeframe FVG backtesting system. It tests whether a hybrid strategy -- using 4H market structure for directional bias, 1H session context for timing, and 15M/5M Fair Value Gap entries -- produces a statistical edge across micro futures, gold, and forex majors during London and New York sessions.

**Current status**: Greenfield. Backtest-only -- no live execution, no broker integration.

## Project Workflow & Agents

### Adversarial Reviewer (MANDATORY)
You **MUST** always act as or invoke an adversarial reviewer that challenges the implementation and logic before finalizing any changes or proceeding to a new phase. This is a mandatory step, no exceptions! The adversarial reviewer should actively try to find flaws, edge cases, lookahead biases, or structural weaknesses in the code.

## Architecture & Data Flow

```
CLI (__main__.py)
        |
        v
  backtest/engine.py  (orchestrator -- bar-by-bar loop)
        |
        +-- data/loader.py           load CSV, validate, resample to multi-TF
        +-- strategy/session.py      DST-aware session boundaries per day
        +-- strategy/swings.py       swing high/low detection (confirmed only)
        +-- strategy/market_structure.py  HH/HL/LL/LH, BOS, CHoCH from swings
        +-- strategy/fvg.py          FVG detection, filtering, mitigation tracking
        +-- strategy/signals.py      multi-layer signal orchestration
        +-- backtest/portfolio.py    cost adjustment, position sizing, PnL (ONLY cost point)
        |
        v
  BacktestResult (equity_curve, trades, summary)
        |
        +-- backtest/metrics.py     Sharpe, Sortino, drawdown, PF, expectancy
        +-- backtest/report.py      summary table, trade log CSV, equity CSV
        +-- analysis/dashboard.py   matplotlib charts
```

### Multi-Timeframe Cascade

```
4H bars  ->  swings.py + market_structure.py  ->  bias (BULLISH/BEARISH/NEUTRAL)
1H bars  ->  session.py (Asian range, sweep)   ->  session_bias (LONG/SHORT/NO_TRADE)
15M/5M   ->  fvg.py (detection + filtering)    ->  entry signals (with R:R validation)
```

All timeframes are resampled from the base 5M data in `data/loader.py`. No separate data files per timeframe.

### Critical Invariant: No Lookahead

- Swing highs/lows are confirmed only after N bars have completed after the pivot. `swings.py` is the single source.
- FVGs are valid only after the 3rd candle closes. `fvg.py` is the single source.
- All resampled bars use `closed='left', label='left'` to prevent future data leaking into current bars.
- Entry signals generated on bar close -> fills happen on next bar open. `engine.py` enforces this.
- No other module performs lookahead-sensitive operations independently.

### Cost Model

`backtest/portfolio.py` is the **single point** where costs are applied:
- Entry: price + spread + slippage (longs), price - slippage (shorts)
- Exit: price - slippage (longs), price + spread + slippage (shorts)
- Commission: per-trade flat fee
- No other module adjusts prices for costs

## Key Directories

| Directory | Purpose |
|---|---|
| `config/` | Dataclass-based strategy configuration (`StrategyConfig` hierarchy), instrument specs with tick sizes and point values |
| `data/` | CSV loader with schema validation, multi-timeframe resampling, OHLCV type definitions |
| `strategy/` | Swing detection, market structure (BOS/CHoCH), session timing (DST via zoneinfo), FVG detection, signal orchestration |
| `backtest/` | Engine loop, portfolio/cost model, metrics (Sharpe/Sortino/drawdown/PF), report generators |
| `analysis/` | matplotlib dashboard: equity curve, drawdown, monthly heatmap, R-multiple distribution |
| `tests/` | Deterministic pytest suite with handcrafted synthetic data, no mocks |
| `configs/` | JSON config files. `default.json` is tracked; user configs are gitignored |

## Development Commands

```bash
# Install
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run tests with coverage
pytest tests/ --cov --cov-report=term-missing

# Lint
python -m ruff check .

# Format
python -m ruff format .

# Lint + format + test (pre-commit check)
python -m ruff check . && python -m ruff format --check . && pytest tests/ -q
```

### CLI Commands

```bash
# Run backtest
python -m alpha1 backtest --config configs/default.json --data path/to/data.csv --instrument EURUSD

# Show help
python -m alpha1 backtest --help
```

## Code Conventions & Common Patterns

### Data Types

- **Configuration**: `@dataclass` classes in `config/settings.py`. JSON-serializable via `to_json()`/`from_json()`. All strategy parameters live here -- no magic numbers in logic.
- **Frozen dataclasses** for immutable domain objects: `Signal`, `FVG`, `SwingPoint`, `SessionBoundaries`, `InstrumentSpec`
- **Mutable dataclass** for `Portfolio` (tracks equity, trades) and `Trade` (populated during execution)

### Naming

- Modules: `snake_case.py`
- Classes: `PascalCase`
- Functions: `snake_case`
- Constants: `UPPER_SNAKE_CASE` (e.g., `INSTRUMENT_REGISTRY`, `LONDON_TZ`, `MIN_BAR_COUNT`)
- Private helpers: `_prefixed` (e.g., `_compute_buffer`, `_resample_ohlcv`)
- Enums: `PascalCase` members in `UPPER_SNAKE_CASE` (e.g., `Bias.BULLISH`, `ExitReason.STOP_LOSS`)
- Test classes: `class Test<Feature>` with methods `test_<behavior>`

### Logging

Every module uses `logging.getLogger("alpha1.<package>.<module>")`. Signal decisions, fill calculations, and exit logic at DEBUG. Backtest summaries and data operations at INFO.

### Error Handling

- `ValueError` for bad configuration or unsupported parameters
- `FileNotFoundError` with actionable message when data file is missing
- `RuntimeError` for data pipeline failures (empty date range, insufficient bars for resampling)
- Functions return `None` for "no result" cases (no signal, no range, weekend) rather than raising

### DataFrame Convention

All OHLCV DataFrames follow a strict schema enforced by `data/types.py`:
- UTC-aware `DatetimeIndex` named `"datetime"`
- Columns: `open`, `high`, `low`, `close`, `volume` (lowercase)
- No duplicate timestamps, no NaN in OHLC, monotonically increasing index

### Resampling Convention

All resampling from 5M base to higher timeframes uses:
- `closed='left', label='left'` -- prevents lookahead
- OHLC aggregation: `{'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}`
- Done once in `data/loader.py` -- no other module resamples

### Session Times

Stored as `datetime.time` in London local time (`Europe/London`). `strategy/session.py` converts to UTC per trading date using `zoneinfo.ZoneInfo("Europe/London")`. Returns `None` for weekends.

- Asian: 00:00-07:00 London
- London: 08:00-12:00 London
- New York: 13:00-17:00 London

## Important Files

| File | Role |
|---|---|
| `config/settings.py` | `StrategyConfig` and all sub-configs. Single source of truth for every parameter. |
| `config/instruments.py` | `InstrumentSpec` and `INSTRUMENT_REGISTRY`. Tick sizes, point values, cost defaults per instrument. |
| `data/loader.py` | CSV ingestion, schema validation, multi-timeframe resampling. Only place resampling happens. |
| `data/types.py` | OHLCV schema definition and `validate_ohlcv()`. |
| `strategy/swings.py` | `detect_swing_highs()` / `detect_swing_lows()`. Only place swing detection happens. |
| `strategy/market_structure.py` | `classify_structure()` -- HH/HL/LL/LH, BOS, CHoCH from swing sequence. |
| `strategy/session.py` | `SessionDetector` -- DST-critical session boundaries. |
| `strategy/fvg.py` | `detect_fvgs()` -- FVG detection, filtering, mitigation tracking. |
| `strategy/signals.py` | `generate_signals()` -- multi-layer orchestration producing entry signals. |
| `backtest/engine.py` | `run_backtest()` -- bar-by-bar simulation. Main entry point. |
| `backtest/portfolio.py` | Position sizing, cost-adjusted fills, equity tracking. Only place costs are applied. |
| `backtest/metrics.py` | `calculate_metrics()` -- Sharpe, Sortino, PF, drawdown, win rate. |
| `configs/default.json` | Reference configuration with all default parameter values. |

## Runtime & Tooling

- **Python**: 3.12+ required
- **Package manager**: pip. `pip install -e ".[dev]"` for development.
- **Linter/formatter**: ruff. Config in `pyproject.toml`. Line length 120. Run via `python -m ruff` if not on PATH.
- **Type hints**: Used throughout. Dataclass fields are fully typed.
- **Build**: setuptools with `build_meta` backend.

### Dependencies

| Package | Purpose | Required? |
|---|---|---|
| pandas (>=2.0) | Data manipulation, resampling | Yes |
| numpy (>=1.24) | Numerical operations | Yes |
| matplotlib (>=3.7) | Charts and visualization | Yes |
| pytest (>=7.0) | Testing | Dev only (`[dev]`) |
| ruff (>=0.1) | Lint + format | Dev only (`[dev]`) |

## Testing & QA

### Framework

pytest with fixtures in `tests/conftest.py`. Deterministic synthetic data, no mocks.

### Test File Mapping

| File | Tests |
|---|---|
| `test_swings.py` | Known zigzag patterns, flat prices, window edge cases, confirmation delay |
| `test_market_structure.py` | HH/HL/LL/LH classification, BOS detection, CHoCH detection |
| `test_session.py` | DST transitions (BST start/end), Asian range, sweep detection, weekend skip |
| `test_fvg.py` | 3-bar FVG patterns, ATR filter, displacement filter, mitigation tracking |
| `test_signals.py` | End-to-end signal generation, R:R filter validation, bias alignment |
| `test_engine.py` | Full backtest on synthetic data: entry timing, breakeven, time exit, max trades, ambiguous bar |
| `test_metrics.py` | Known trade list -> exact Sharpe, drawdown, PF, win rate values |

### Patterns

- Handcrafted synthetic OHLCV data (not random) for deterministic assertions
- `pytest.approx` for all float comparisons
- No mocks -- tests use real objects with synthetic data
- Each test constructs a known price sequence and asserts exact outputs

### Coverage

Source: `config, data, strategy, backtest`; omit: `analysis/*, tests/*`; fail_under: 70%.
