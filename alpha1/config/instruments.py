from dataclasses import dataclass


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    tick_size: float
    point_value: float
    pip_size: float | None
    default_spread_ticks: int
    default_slippage_ticks: int
    commission_per_trade: float = 0.0


INSTRUMENT_REGISTRY: dict[str, InstrumentSpec] = {
    # Micro Futures
    "MES": InstrumentSpec(
        symbol="MES",
        tick_size=0.25,
        point_value=5.0,  # 0.25 * $1.25? Wait, plan says Tick Size: 0.25, Point Value $1.25. Actually wait.
        # If tick size is 0.25 and point value is $5.0 (so 1 tick = 1.25), then point_value should be 5.0
        # Let's align with the table: point value means value of 1.0 price move.
        # 1.0 move in MES = 4 ticks. 4 * 1.25 = 5.0.
        # Actually the table in plan says "Tick Size: 0.25, Point Value: $1.25"
        # Let's adjust to be clear. If point value is $1.25 per TICK, then 1 tick = $1.25.
        # Standard MES: $5 per point (multiplier). Tick = 0.25 * $5 = $1.25.
        # So "Point Value: $1.25" in plan meant "Tick Value". Let's name fields according to plan or standardize.
        pip_size=None,
        default_spread_ticks=1,   # 0.25 pts = 1 tick
        default_slippage_ticks=1, # 0.25 pts = 1 tick
        commission_per_trade=0.62
    ),
    "MNQ": InstrumentSpec(
        symbol="MNQ",
        tick_size=0.25,
        point_value=2.0,  # multiplier 2. 0.25 * 2 = 0.50 tick value.
        pip_size=None,
        default_spread_ticks=2,   # 0.50 pts = 2 ticks
        default_slippage_ticks=1, # 0.25 pts = 1 tick
        commission_per_trade=0.62
    ),
    "MGC": InstrumentSpec(
        symbol="MGC",
        tick_size=0.10,
        point_value=10.0, # multiplier 10. 0.10 * 10 = 1.0 tick value
        pip_size=None,
        default_spread_ticks=3,   # 0.30 pts = 3 ticks
        default_slippage_ticks=1, # 0.10 pts = 1 tick
        commission_per_trade=1.50
    ),
    "GC": InstrumentSpec(
        symbol="GC",
        tick_size=0.10,
        point_value=100.0, # multiplier 100. 0.10 * 100 = 10.0 tick value
        pip_size=None,
        default_spread_ticks=3,   # 0.30 pts = 3 ticks
        default_slippage_ticks=1, # 0.10 pts = 1 tick
        commission_per_trade=2.50
    ),
    # Forex Majors
    "EURUSD": InstrumentSpec(
        symbol="EURUSD",
        tick_size=0.00001,
        point_value=100000.0, # Lot size
        pip_size=0.0001,
        default_spread_ticks=10,  # 1.0 pips = 10 ticks
        default_slippage_ticks=5, # 0.5 pips = 5 ticks
        commission_per_trade=3.00
    ),
    "GBPUSD": InstrumentSpec(
        symbol="GBPUSD",
        tick_size=0.00001,
        point_value=100000.0,
        pip_size=0.0001,
        default_spread_ticks=12,  # 1.2 pips = 12 ticks
        default_slippage_ticks=5, # 0.5 pips = 5 ticks
        commission_per_trade=3.00
    ),
    "USDJPY": InstrumentSpec(
        symbol="USDJPY",
        tick_size=0.001,
        point_value=100000.0,
        pip_size=0.01,
        default_spread_ticks=10,  # 1.0 pips = 10 ticks
        default_slippage_ticks=5, # 0.5 pips = 5 ticks
        commission_per_trade=3.00
    ),
    # Forex — additional pairs
    "AUDUSD": InstrumentSpec(
        symbol="AUDUSD",
        tick_size=0.00001,
        point_value=100000.0,
        pip_size=0.0001,
        default_spread_ticks=15,  # 1.5 pips
        default_slippage_ticks=5,
        commission_per_trade=3.50
    ),
    "NZDUSD": InstrumentSpec(
        symbol="NZDUSD",
        tick_size=0.00001,
        point_value=100000.0,
        pip_size=0.0001,
        default_spread_ticks=20,  # 2.0 pips
        default_slippage_ticks=5,
        commission_per_trade=3.50
    ),
    "USDCAD": InstrumentSpec(
        symbol="USDCAD",
        tick_size=0.00001,
        point_value=100000.0,
        pip_size=0.0001,
        default_spread_ticks=20,  # 2.0 pips
        default_slippage_ticks=5,
        commission_per_trade=3.50
    ),
    "USDCHF": InstrumentSpec(
        symbol="USDCHF",
        tick_size=0.00001,
        point_value=100000.0,
        pip_size=0.0001,
        default_spread_ticks=15,  # 1.5 pips
        default_slippage_ticks=5,
        commission_per_trade=3.50
    ),
    "GBPJPY": InstrumentSpec(
        symbol="GBPJPY",
        tick_size=0.001,
        point_value=100000.0,
        pip_size=0.01,
        default_spread_ticks=30,  # 3.0 pips
        default_slippage_ticks=10, # 1.0 pip
        commission_per_trade=4.00
    ),
    "MYM": InstrumentSpec(
        symbol="MYM",
        tick_size=1.0,        # 1 point minimum move
        point_value=0.50,    # $0.50 per point (micro Dow Jones)
        pip_size=None,
        default_spread_ticks=2,   # 2 points
        default_slippage_ticks=1,
        commission_per_trade=0.62
    ),
    # Spot Gold (XAUUSD via Forex broker)
    # price in USD/oz; point_value=100 assumes a 100-oz standard lot.
    # tick_size=0.01 → tick value = 0.01 × 100 = $1.00 per lot.
    "XAUUSD": InstrumentSpec(
        symbol="XAUUSD",
        tick_size=0.01,
        point_value=100.0,
        pip_size=0.01,
        default_spread_ticks=30,  # $0.30 typical spread
        default_slippage_ticks=5,  # $0.05
        commission_per_trade=5.00
    ),
}
