# Repository Guidelines

## Project Overview

Alpha1 is an intraday FVG + Order Block backtesting system. It validates a statistical edge across forex majors, spot gold, and US index micro futures using a limit-order entry strategy built on Fair Value Gaps and Order Block structural stops.

**Current status**: Backtest-only. No live execution, no broker integration.

### Proven Results (full historical backtests)

| Instrument | History | CAGR | Sharpe | MaxDD | Win Rate | p-value |
|---|---|---|---|---|---|---|
| XAUUSD | 2009–2026 | +16.4%/yr | +3.62 | 5.5% | 58.4% | <0.001 |
| MYM | 2021–2026 | +8.7%/yr | +5.99 | 3.5% | 64.9% | <0.001 |
| MNQ | 2021–2026 | +8.6%/yr | +4.77 | 4.2% | 56.0% | <0.001 |
| USDCAD | 2000–2026 | +10.6%/yr | +2.25 | 12.8% | 53.9% | <0.001 |
| GBPJPY | 2002–2026 | +6.8%/yr | +2.16 | 8.3% | 54.8% | <0.001 |

All results use the intraday configuration: session-end close, 1.5R fixed target, limit entry at FVG midpoint.

---

## Project Workflow & Agents

### Adversarial Reviewer (MANDATORY)

You **MUST** always act as or invoke an adversarial reviewer that challenges the implementation and logic before finalising any changes or proceeding to a new phase. **This is a mandatory step, no exceptions.**

The adversarial reviewer must actively try to find:
- Lookahead bias in signal generation or data handling
- Incorrect assumptions about how price data is structured
- Wrong entry/exit mechanics that destroy an otherwise valid edge
- Statistical overfit (small sample sizes, regime-specific results)
- Structural weaknesses in the cost model, position sizing, or portfolio accounting

The adversarial reviewer **already caught** the most critical bug in this project: the original close-based retracement entry trigger destroyed the 62% win rate inherent in FVG midpoint limit entries, producing a real win rate of 30.8% — worse than random after costs. Do not let this class of mistake recur.

### Branching

> **No work is ever done directly on `main`.** Every change lives on a dedicated branch.

```bash
# Create feature branch
git checkout -b feature/<slug>

# Push and open PR
git push -u origin feature/<slug>
gh pr create --base main

# After merge, clean up
git checkout main && git pull origin main
git branch -D feature/<slug>
```

**Branch prefixes:** `fix/`, `feature/`, `chore/`

**PR flow:**
1. Implement and test locally.
2. Run adversarial review — mandatory, no exceptions.
3. Push branch, open PR.
4. Run independent reviewer on final PR state — mandatory, no exceptions.
5. Address all Critical and Significant findings.
6. Merge via GitHub (squash preferred).

---

## Strategy Logic — What Is Proven

### The Core Edge

Fair Value Gaps (FVGs) on the 1H chart have a **62% win rate at 1.5R when entered via limit order at the FVG midpoint** with an Order Block structural stop. This was verified across 25 years of GBPJPY and USDCAD data and holds across all tested instruments.

**The entry mechanic is the entire edge.** Everything else is either noise or overhead.

### What Was Disproven

The following layers were **systematically tested and found to add zero incremental win rate**:

| Layer | Tested | Result |
|---|---|---|
| 4H market structure bias (BOS/CHoCH) | Yes | 61.7% WR with-trend vs 62.8% counter-trend vs 62.2% unfiltered — no difference |
| Session sweep filter (Asian range) | Yes | Broken XOR logic, adds nothing |
| HTF trend alignment requirement | Yes | Reduces frequency 10x with zero WR improvement |

These layers remain in the codebase for research purposes (`market_structure.py`, `session.py`) but `generate_signals()` does **not** use them. Do not re-add HTF/session filters without a full adversarial review and statistical proof.

### Signal Generation (Current)

`strategy/signals.py` generates one `Signal` per detected FVG:

1. Detect FVGs on the entry timeframe using `detect_fvgs()`.
2. For each FVG, find the Order Block: last opposing candle within 10 bars before the displacement.
3. Emit a `Signal` immediately at FVG formation close:
   - `entry_price` = FVG midpoint (this is the **limit price**, not a market order price)
   - `stop_price` = OB extreme, clipped to FVG boundary
   - `cancel_price` = FVG bottom (longs) / top (shorts) — if breached, limit is cancelled
   - `target_price` = midpoint ± risk × `target_min_rr` (fixed at generation, not recalculated at fill)
4. No trend filter. No session filter. The retrace back to the midpoint is itself the quality gate.

### Entry Execution (Current)

`backtest/engine.py` and `backtest/portfolio_multi.py` maintain a **pending limit order queue**:

- Signals enter the queue as pending limits on the bar after formation.
- Each subsequent bar: check if `low <= entry_price` (longs) or `high >= entry_price` (shorts) → fill at `entry_price`.
- Cancel if price breaks `cancel_price` (FVG mitigated).
- Cancel after `limit_order_timeout_bars` bars without fill.
- Fill only during London / NY sessions, outside the `entry_cutoff_minutes_before_close` window.

**Why limit orders:** entering at the close of a bar that "closed back above the midpoint" places the entry in the top half of the FVG. The stop (OB extreme) is unchanged. Risk is proportionally larger. The same market move that would hit 1.5R from the midpoint now falls short. This was the mechanism destroying the edge in the original implementation.

### Trade Management

- **Stop**: OB extreme minus `stop_buffer_ticks` (minimal buffer, structural stop only).
- **Target**: fixed at `midpoint ± risk × target_min_rr` at signal generation. Not adjusted.
- **Breakeven**: disabled by default (`breakeven_at_r = 0`). Time stop is the risk control mechanism.
- **Session exit**: `close_at_session_end = true`. **This is not a limitation — it is the strategy.** The session close acts as a time-based stop that cuts losing trades at a partial loss (avg -0.3R) rather than holding to a full -1R stop. Winning trades resolve within the session (median 1–4H). Net effect: lower drawdown, higher Sharpe, more profitable years.

---

## Architecture & Data Flow

```
CLI (__main__.py)
        |
        +--[backtest]---> backtest/engine.py          single-instrument bar-by-bar loop
        |                       |
        +--[portfolio]--> backtest/portfolio_multi.py  multi-instrument shared equity
                                |
                +---------------+
                |
                +-- data/loader.py           CSV ingestion, multi-TF resampling
                +-- strategy/fvg.py          FVG + OB detection
                +-- strategy/signals.py      pending limit order generation
                +-- backtest/portfolio.py    position sizing, cost model, PnL
                |
                v
          Portfolio (equity_curve, trades)
                |
                +-- backtest/metrics.py      Sharpe, Sortino, PF, drawdown, binomial test
                +-- backtest/report.py       CSV export, JSON metrics
                +-- analysis/dashboard.py   matplotlib charts
```

### Entry Timeframe

All instruments use the **1H chart** as the entry timeframe. The `data_dict['5min']` key in `generate_signals()` holds whatever timeframe is the entry TF — callers pass `data_dict['1h']` as `data_dict['5min']` for 1H operation.

### Resampling

All resampling from the raw 1M base uses `closed='left', label='left'` — the only place resampling happens is `data/loader.py`. The left-label convention ensures bar N's data is only available at the bar N close timestamp, preventing lookahead.

### No Lookahead Invariants

- FVGs are only valid after the 3rd candle closes. `fvg.py` is the single source.
- Limit signals are emitted at bar N close and queued for consideration from bar N+1 onward.
- `broadcast_htf_to_ltf()` shifts HTF availability by one full bar duration — a 4H bar closing at 12:00 is available at 12:00, not at 08:00 when it opened.
- OB search looks backward only (within 10 bars before the displacement candle).

### Cost Model

`backtest/portfolio.py` is the **single point** where costs are applied:
- Limit fill: no additional slippage on entry (limit orders fill at the stated price).
- Stop exit: worst of `stop_price` and `opens[i]` (gap protection).
- Target exit: best of `target_price` and `opens[i]` (gap capture).
- Commission: flat per-trade fee from `InstrumentSpec`.
- `stop_buffer_ticks` widens the actual stop beyond the OB extreme by N ticks.

---

## Instrument Registry

| Symbol | Class | Tick | Point Value | Spread | Works? | Note |
|---|---|---|---|---|---|---|
| XAUUSD | Spot Gold | 0.01 | $100/pt (100oz) | 30 ticks | **Yes** | Best result; 18/18 years profitable |
| MYM | Micro Dow | 1.0 pt | $0.50/pt | 2 ticks | **Yes** | Best Sharpe; 6/6 years |
| MNQ | Micro NQ | 0.25 pt | $2.00/pt | 2 ticks | **Yes** | 5/5 years profitable |
| GBPJPY | Forex | 0.001 | 100k lot | 30 ticks | **Yes** | 25yr history; 20/25 years |
| USDCAD | Forex | 0.00001 | 100k lot | 20 ticks | **Yes** | 25yr history; 24/26 years |
| EURUSD | Forex | 0.00001 | 100k lot | 10 ticks | Marginal | +1% CAGR; thin edge |
| GBPUSD | Forex | 0.00001 | 100k lot | 12 ticks | No | Negative in full history |
| MES | Micro S&P | 0.25 pt | $5.00/pt | 1 tick | **No** | ATR too large; <2 FVGs/year at standard params |
| USDJPY | Forex | 0.001 | 100k lot | 10 ticks | No | 18% WR in full history |
| AUDUSD | Forex | 0.00001 | 100k lot | 15 ticks | No | 11% WR; ranging instrument |

---

## Key Directories

| Directory | Purpose |
|---|---|
| `config/` | `StrategyConfig` dataclass hierarchy, `InstrumentSpec` registry |
| `data/` | CSV loader with schema validation and multi-TF resampling |
| `strategy/` | FVG/OB detection, signal generation, session detection, market structure |
| `backtest/` | Single and multi-instrument engines, portfolio, metrics, report |
| `analysis/` | matplotlib dashboard |
| `tests/` | Deterministic pytest suite, synthetic data, no mocks |
| `configs/` | `default.json` tracked; user configs gitignored |
| `scripts/` | `run_portfolio.py` multi-instrument runner, `grid_search.py` |

---

## Important Files

| File | Role |
|---|---|
| `config/settings.py` | `StrategyConfig` — single source of truth for all parameters |
| `config/instruments.py` | `InstrumentSpec` + `INSTRUMENT_REGISTRY` |
| `data/loader.py` | CSV ingestion, validation, multi-TF resampling. Only place resampling happens. |
| `data/types.py` | OHLCV schema and `validate_ohlcv()` |
| `strategy/fvg.py` | `detect_fvgs()` — FVG + OB extreme detection. Only place FVGs are detected. |
| `strategy/signals.py` | `generate_signals()` — emits one limit-order `Signal` per FVG. No HTF/session filtering. |
| `strategy/market_structure.py` | `classify_structure()` — HH/HL/BOS/CHoCH. Available for research; not used in live signal path. |
| `strategy/session.py` | `SessionDetector` — DST-aware session boundaries, used for entry cutoff and session-close exit. |
| `backtest/engine.py` | `run_backtest()` — single-instrument bar-by-bar loop with pending limit queue. |
| `backtest/portfolio_multi.py` | `run_portfolio_backtest()` — multi-instrument shared equity, same limit logic. |
| `backtest/portfolio.py` | `Portfolio`, `Trade` — costs, position sizing. Only place costs are applied. |
| `backtest/metrics.py` | `calculate_metrics()` — Sharpe, Sortino, PF, drawdown, expectancy. |
| `scripts/run_portfolio.py` | CLI runner for multi-instrument portfolio backtests. |
| `configs/default.json` | Proven intraday config. Do not change without adversarial review + full history backtest. |

---

## Default Configuration

```json
{
    "entry": {
        "min_gap_atr_ratio": 0.5,
        "displacement_body_multiplier": 1.5,
        "limit_order_timeout_bars": 50,
        "entry_cutoff_minutes_before_close": 90
    },
    "exit": {
        "stop_buffer_ticks": 2,
        "target_min_rr": 1.5,
        "close_at_session_end": true,
        "breakeven_at_r": 0.0
    }
}
```

**Do not change `close_at_session_end` to `false` without running a full-history backtest.** The session close is the primary risk management mechanism — it functions as a time-based stop that cuts losing trades at a partial loss (~-0.3R average for time exits) rather than holding to a full -1R structural stop. Removing it degrades performance on every tested instrument.

---

## Development Commands

```bash
# Install
pip install -e ".[dev]"

# Run tests (coverage threshold: 65%)
python -m pytest tests/ -v

# Lint
python -m ruff check .

# Format
python -m ruff format .

# Pre-commit check
python -m ruff check . && python -m ruff format --check . && python -m pytest tests/ -q
```

### CLI Commands

```bash
# Single-instrument backtest
python -m alpha1 backtest --config configs/default.json --data path/to/data.csv --instrument XAUUSD

# Multi-instrument portfolio (recent 8 Forex pairs)
python scripts/run_portfolio.py --pairs recent --max-concurrent 3 --out output/

# Multi-instrument portfolio (GBPJPY + USDCAD full history)
python scripts/run_portfolio.py --pairs full --max-concurrent 3 --out output/
```

### Data Format

CSV with columns: `datetime,open,high,low,close,volume`
- `datetime`: UTC-aware ISO 8601 (`2024-01-01 08:00:00+00:00`)
- Minimum timeframe: 1M bars (resampled to 1H for entry)
- Data files are **not tracked in git** — supply your own OHLCV CSVs.

---

## Code Conventions

### Data Types
- **Config**: `@dataclass` in `config/settings.py`. All parameters live here.
- **Immutable domain objects**: `Signal`, `FVG`, `InstrumentSpec` (frozen dataclasses).
- **Mutable**: `Portfolio`, `Trade`.

### Naming
- Modules: `snake_case.py` | Classes: `PascalCase` | Functions: `snake_case`
- Constants: `UPPER_SNAKE_CASE` | Private: `_prefixed`
- Enums: `PascalCase` class, `UPPER_SNAKE_CASE` members (`Bias.BULLISH`, `ExitReason.STOP_LOSS`)

### Logging
Every module uses `logging.getLogger("alpha1.<package>.<module>")`. Signal events at DEBUG, backtest summaries at INFO.

### Error Handling
- `ValueError` for bad configuration or unsupported parameters
- `FileNotFoundError` with actionable message for missing data
- `RuntimeError` for pipeline failures (empty date range, insufficient bars)
- Return `None` for "no result" cases rather than raising

### Testing
- `pytest` with fixtures in `tests/conftest.py`
- Deterministic synthetic OHLCV data — no mocks, no random data
- `pytest.approx` for all float comparisons
- Coverage threshold: 65% (`engine.py` and `portfolio_multi.py` are integration-level; their loops only fire with real signal data)

---

## Runtime & Tooling

- **Python**: 3.12+ required
- **Package manager**: pip (`pip install -e ".[dev]"`)
- **Linter/formatter**: ruff (line length 120, configured in `pyproject.toml`)
- **Build**: setuptools with `build_meta` backend

### Dependencies

| Package | Purpose | Required? |
|---|---|---|
| pandas (>=2.0) | Data manipulation, resampling | Yes |
| numpy (>=1.24) | Numerical operations | Yes |
| matplotlib (>=3.7) | Charts | Yes |
| scipy | Statistical tests (binomial p-values in analysis scripts) | Yes |
| pytest (>=7.0) | Testing | Dev only |
| ruff (>=0.1) | Lint + format | Dev only |
