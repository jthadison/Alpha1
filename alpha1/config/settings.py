import json
from dataclasses import asdict, dataclass, field
from datetime import time
from typing import Any


@dataclass
class SessionConfig:
    asian_start: str = "00:00"
    asian_end: str = "07:00"
    london_start: str = "08:00"
    london_end: str = "12:00"
    ny_start: str = "13:00"
    ny_end: str = "17:00"
    timezone: str = "Europe/London"

    def get_time(self, attr: str) -> time:
        val = getattr(self, attr)
        h, m = map(int, val.split(":"))
        return time(hour=h, minute=m)


@dataclass
class EntryConfig:
    min_gap_atr_ratio: float = 0.25
    displacement_body_multiplier: float = 1.5
    atr_period: int = 14
    swing_lookback: int = 3
    entry_cutoff_minutes_before_close: int = 90
    # EMA fallback bias: when the swing-based structure is NEUTRAL (not enough confirmed
    # swings yet, or ambiguous sequence), fall back to 4H EMA direction.
    # Fully lookahead-free: we use the previous bar's EMA value.
    use_ema_bias: bool = True
    ema_period: int = 50
    # Pending limit order: cancel after this many bars if not filled.
    limit_order_timeout_bars: int = 50


@dataclass
class ExitConfig:
    stop_buffer_ticks: int = 8
    breakeven_at_r: float = 1.0
    target_min_rr: float = 3.0
    close_at_session_end: bool = True


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 1.0
    max_trades_per_session: int = 2


@dataclass
class LiveConfig:
    """
    Configuration for the live trading engine and IBKR connection.

    Default port 4002 = IB Gateway paper trading.
    Port 4001 = IB Gateway live.  Port 7497/7496 = TWS paper/live.
    paper=True by default: a loud warning is printed at startup if paper=False.
    """

    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 1
    paper: bool = True
    instruments: list[str] = field(default_factory=lambda: ["XAUUSD", "MYM", "MNQ"])
    db_path: str = "alpha1_live.db"
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    history_bars: int = 2000


@dataclass
class BacktestConfig:
    start_date: str = "2020-01-01"
    end_date: str = "2025-01-01"
    initial_equity: float = 100000.0


@dataclass
class StrategyConfig:
    session: SessionConfig = field(default_factory=SessionConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    live: LiveConfig = field(default_factory=LiveConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StrategyConfig":
        return cls(
            session=SessionConfig(**data.get("session", {})),
            entry=EntryConfig(**data.get("entry", {})),
            exit=ExitConfig(**data.get("exit", {})),
            risk=RiskConfig(**data.get("risk", {})),
            backtest=BacktestConfig(**data.get("backtest", {})),
            live=LiveConfig(**data.get("live", {})),
        )

    @classmethod
    def from_json(cls, path: str) -> "StrategyConfig":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    def to_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=4)
