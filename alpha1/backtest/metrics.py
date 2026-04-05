"""
Performance metrics for backtested trade streams.

Metrics are computed at two levels:
  1. Trade-level  — R-multiples, expectancy, win/loss distribution, SQN
  2. Equity-curve — daily returns used for Sharpe, Sortino, Calmar, Omega,
                    Ulcer Index, K-Ratio, VaR/CVaR, skewness, kurtosis

All ratios are annualised assuming 252 trading days.
Risk-free rate is set to 0 throughout (absolute return strategy assumption).
"""
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

# ── helpers ───────────────────────────────────────────────────────────────────

def _daily_returns(equity_curve: list[float], equity_dates: list[pd.Timestamp]) -> pd.Series:
    """Resample equity curve to business-day returns.  Empty → empty Series."""
    if len(equity_dates) < 2:
        return pd.Series(dtype=float)
    s = pd.Series(equity_curve[1:], index=equity_dates)
    daily = s.resample("B").last().dropna()
    return daily.pct_change().dropna()


def _drawdown_series(equity_curve: list[float]) -> pd.Series:
    """Per-observation drawdown from peak, as a negative fraction."""
    s = pd.Series(equity_curve)
    return (s - s.cummax()) / s.cummax()


# ── main function ─────────────────────────────────────────────────────────────

def calculate_metrics(
    trades: list[Any],
    equity_curve: list[float],
    equity_dates: list[pd.Timestamp],
) -> dict[str, Any]:
    """
    Compute the full performance metric suite for a completed backtest.

    Parameters
    ----------
    trades       : list of Trade objects (must have .pnl, .r_multiple, .bars_held, .exit_reason)
    equity_curve : equity value at each trade close, starting with initial_equity at index 0
    equity_dates : timestamps corresponding to equity_curve[1:]

    Returns
    -------
    dict with all metrics described below.
    """
    if not trades:
        return {
            "total_trades": 0,
            "total_return_pct": 0.0,
            "win_rate": 0.0,
        }

    # ── trade-level frame ─────────────────────────────────────────────────────
    df = pd.DataFrame([
        {
            "pnl": t.pnl,
            "r": t.r_multiple,
            "bars": t.bars_held,
            "exit": t.exit_reason.value if t.exit_reason else "UNKNOWN",
        }
        for t in trades
    ])

    # ── basic return ──────────────────────────────────────────────────────────
    initial_equity = equity_curve[0]
    final_equity   = equity_curve[-1]
    total_return   = (final_equity / initial_equity - 1.0) * 100

    # ── win / loss split ──────────────────────────────────────────────────────
    wins   = df[df["pnl"] > 0]
    losses = df[df["pnl"] <= 0]

    win_rate    = len(wins) / len(df) * 100
    avg_win_dlr = wins["pnl"].mean()   if not wins.empty   else 0.0
    avg_los_dlr = losses["pnl"].mean() if not losses.empty else 0.0
    avg_win_r   = wins["r"].mean()     if not wins.empty   else 0.0
    avg_los_r   = losses["r"].mean()   if not losses.empty else 0.0
    payoff_ratio = abs(avg_win_r / avg_los_r) if avg_los_r != 0 else float("inf")

    gross_profit = wins["pnl"].sum()      if not wins.empty   else 0.0
    gross_loss   = abs(losses["pnl"].sum()) if not losses.empty else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # ── expectancy & SQN ─────────────────────────────────────────────────────
    # Expectancy: average R gained per trade
    expectancy_r = df["r"].mean()

    # SQN — System Quality Number (Van Tharp)
     # sqrt(n) x mean_R / std_R
    # Interpretation: >2 Good, >3 Excellent, >5 World-class
    r_std = df["r"].std()
    sqn   = np.sqrt(len(df)) * expectancy_r / r_std if r_std > 0 else 0.0

    # ── R-multiple distribution ───────────────────────────────────────────────
    r_skew = float(scipy_stats.skew(df["r"]))
    r_kurt = float(scipy_stats.kurtosis(df["r"]))   # excess kurtosis (normal = 0)

    # ── drawdown series ───────────────────────────────────────────────────────
    dd_series        = _drawdown_series(equity_curve)
    max_drawdown_pct = abs(dd_series.min()) * 100

    # Average drawdown depth (only underwater observations)
    underwater       = dd_series[dd_series < 0]
    avg_drawdown_pct = abs(underwater.mean()) * 100 if len(underwater) > 0 else 0.0

    # Drawdown duration — longest consecutive run below a previous peak
    is_dd           = (dd_series < 0).astype(int)
    dd_durations    = []
    cur_len         = 0
    for v in is_dd:
        if v:
            cur_len += 1
        elif cur_len > 0:
            dd_durations.append(cur_len)
            cur_len = 0
    if cur_len > 0:
        dd_durations.append(cur_len)
    max_dd_duration  = max(dd_durations) if dd_durations else 0   # in trades
    avg_dd_duration  = float(np.mean(dd_durations)) if dd_durations else 0.0

    # Ulcer Index — square-root of mean squared drawdown percentage
    # Captures both depth and duration of drawdowns.  Lower is better.
    # Rule of thumb: UI < 5 is good, < 10 is acceptable for equity strategies.
    ulcer_index = float(np.sqrt(np.mean(dd_series.values ** 2)) * 100)

    # ── daily-return based ratios ─────────────────────────────────────────────
    dr = _daily_returns(equity_curve, equity_dates)

    if len(dr) >= 2:
        mean_dr = dr.mean()
        std_dr  = dr.std()

        # Sharpe — annualised, risk-free = 0
        sharpe = mean_dr / std_dr * np.sqrt(252) if std_dr > 0 else 0.0

        # Sortino — only downside deviation in denominator
        neg_dr     = dr[dr < 0]
        sortino_sd = neg_dr.std()
        sortino    = mean_dr / sortino_sd * np.sqrt(252) if sortino_sd > 0 else 0.0

        # Omega Ratio — probability-weighted gain above 0 / loss below 0
        # Omega > 1 means more probability-weighted upside than downside
        gains = dr[dr > 0].sum()
        ls    = abs(dr[dr < 0].sum())
        omega = gains / ls if ls > 0 else float("inf")

        # VaR and CVaR (Expected Shortfall) at 95% confidence on daily returns
        var_95  = float(np.percentile(dr, 5)) * 100    # 5th percentile → daily VaR
        cvar_95 = float(dr[dr <= np.percentile(dr, 5)].mean()) * 100  # avg of worst 5%

        # Return skewness and kurtosis of daily returns
        dr_skew = float(scipy_stats.skew(dr))
        dr_kurt = float(scipy_stats.kurtosis(dr))

        # K-Ratio — measures consistency of equity curve growth.
         # Regresses ln(equity) on time; K = slope / (std_error x sqrt(n)).
        # Higher is better: >1.0 is considered acceptable, >2.0 is strong.
        ln_eq      = np.log(pd.Series(equity_curve[1:]).values)
        x          = np.arange(len(ln_eq))
        slope, _, _, _, se = scipy_stats.linregress(x, ln_eq)
        k_ratio    = slope / (se * np.sqrt(len(ln_eq))) if se > 0 else 0.0

    else:
        sharpe = sortino = omega = var_95 = cvar_95 = 0.0
        dr_skew = dr_kurt = k_ratio = 0.0

    # ── Calmar Ratio — CAGR / MaxDD ──────────────────────────────────────────
    # Requires dates to compute CAGR.
    if equity_dates:
        years = (equity_dates[-1] - equity_dates[0]).days / 365.25
        cagr  = ((final_equity / initial_equity) ** (1 / years) - 1) * 100 if years > 0 else 0.0
    else:
        cagr  = 0.0
    calmar = cagr / max_drawdown_pct if max_drawdown_pct > 0 else float("inf")

    # Recovery Factor — total return / max drawdown (simpler than Calmar)
    recovery_factor = total_return / max_drawdown_pct if max_drawdown_pct > 0 else float("inf")

    # ── holding period ────────────────────────────────────────────────────────
    avg_bars = float(df["bars"].mean())
    max_bars = int(df["bars"].max())

    return {
        # ── overview ──────────────────────────────────────────────────────────
        "total_trades":       len(trades),
        "total_return_pct":   total_return,
        "cagr_pct":           cagr,

        # ── win / loss ────────────────────────────────────────────────────────
        "win_rate":           win_rate,
        "profit_factor":      profit_factor,
        "payoff_ratio":       payoff_ratio,       # avg_win_R / avg_loss_R
        "expectancy_r":       expectancy_r,
        "avg_win_dlr":        avg_win_dlr,
        "avg_loss_dlr":       avg_los_dlr,
        "avg_win_r":          avg_win_r,
        "avg_loss_r":         avg_los_r,

        # ── trade quality ─────────────────────────────────────────────────────
        "sqn":                sqn,                # System Quality Number
        "r_skewness":         r_skew,             # positive = fat right tail (good)
        "r_kurtosis":         r_kurt,             # excess kurtosis of R distribution

        # ── drawdown ──────────────────────────────────────────────────────────
        "max_drawdown_pct":   max_drawdown_pct,
        "avg_drawdown_pct":   avg_drawdown_pct,
        "max_dd_duration":    max_dd_duration,    # in trades
        "avg_dd_duration":    avg_dd_duration,    # in trades
        "ulcer_index":        ulcer_index,        # lower is better

        # ── risk-adjusted returns ─────────────────────────────────────────────
        "sharpe_ratio":       sharpe,
        "sortino_ratio":      sortino,
        "calmar_ratio":       calmar,             # CAGR / MaxDD
        "omega_ratio":        omega,              # prob-weighted gain/loss
        "recovery_factor":    recovery_factor,   # total_return / MaxDD
        "k_ratio":            k_ratio,            # equity curve linearity

        # ── daily return distribution ─────────────────────────────────────────
        "var_95_pct":         var_95,             # daily 95% VaR
        "cvar_95_pct":        cvar_95,            # daily CVaR / Expected Shortfall
        "daily_skewness":     dr_skew,
        "daily_kurtosis":     dr_kurt,

        # ── holding period ────────────────────────────────────────────────────
        "avg_bars_held":      avg_bars,
        "max_bars_held":      max_bars,

        # ── exit breakdown ────────────────────────────────────────────────────
        "exit_reasons":       df["exit"].value_counts().to_dict(),
    }
