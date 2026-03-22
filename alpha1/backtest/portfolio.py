from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

from alpha1.config.instruments import InstrumentSpec


class ExitReason(Enum):
    STOP_LOSS = "STOP_LOSS"
    TARGET = "TARGET"
    BREAKEVEN = "BREAKEVEN"
    TIME_EXIT = "TIME_EXIT"

@dataclass
class Trade:
    direction: str  # "LONG" or "SHORT"
    entry_time: pd.Timestamp
    entry_price_raw: float
    entry_price_adjusted: float
    stop_price: float
    initial_stop_price: float
    target_price: float
    size: float

    exit_time: pd.Timestamp | None = None
    exit_price_raw: float | None = None
    exit_price_adjusted: float | None = None
    exit_reason: ExitReason | None = None

    pnl: float = 0.0
    r_multiple: float = 0.0
    bars_held: int = 0
    symbol: str = field(default="", init=False)

class Portfolio:
    def __init__(self, initial_equity: float):
        self.initial_equity = initial_equity
        self.equity = initial_equity
        self.trades: list[Trade] = []
        self.equity_curve: list[float] = [initial_equity]
        self.equity_dates: list[pd.Timestamp] = []

    def calculate_position_size(self, risk_pct: float, stop_distance_price: float, instrument: InstrumentSpec) -> float:
        """
        Calculates position size based on risk percentage.
        stop_distance_price is the absolute difference in price between entry and stop.
        """
        if stop_distance_price <= 0:
            return 0.0

        risk_amount = self.equity * (risk_pct / 100.0)

        # How much we lose per 1 unit of size for this stop distance
        # Actually point_value is the value of 1.0 price move
        loss_per_contract = stop_distance_price * instrument.point_value

        if loss_per_contract <= 0:
            return 0.0

        # For forex it could be micro lots etc. We can just use fractional sizes.
        size = risk_amount / loss_per_contract

        # Keep it simple: allow fractional sizes to abstract away from contract rounding for now,
        # unless it's strictly futures. The plan doesn't specify rounding rules.
        return round(size, 2)

    def apply_costs(self, price: float, direction: str, is_entry: bool, instrument: InstrumentSpec) -> float:
        """
        Applies spread and slippage to price.
        Assumes price in data is the Bid price.
        Long Entry (Buy) = price + spread + slippage
        Short Entry (Sell) = price - slippage
        Long Exit (Sell) = price - slippage
        Short Exit (Buy) = price + spread + slippage
        """
        spread_price = instrument.default_spread_ticks * instrument.tick_size
        slippage_price = instrument.default_slippage_ticks * instrument.tick_size

        if direction == "LONG":
            if is_entry:
                return price + spread_price + slippage_price
            else:
                return price - slippage_price
        else: # SHORT
            if is_entry:
                return price - slippage_price
            else:
                return price + spread_price + slippage_price

    def open_trade(self, direction: str, time: pd.Timestamp, price: float, stop: float, target: float, size: float, instrument: InstrumentSpec) -> Trade:
        adjusted_price = self.apply_costs(price, direction, is_entry=True, instrument=instrument)

        trade = Trade(
            direction=direction,
            entry_time=time,
            entry_price_raw=price,
            entry_price_adjusted=adjusted_price,
            stop_price=stop,
            initial_stop_price=stop,
            target_price=target,
            size=size
        )
        return trade

    def close_trade(self, trade: Trade, time: pd.Timestamp, price: float, reason: ExitReason, bars_held: int, instrument: InstrumentSpec):
        adjusted_price = self.apply_costs(price, trade.direction, is_entry=False, instrument=instrument)

        trade.exit_time = time
        trade.exit_price_raw = price
        trade.exit_price_adjusted = adjusted_price
        trade.exit_reason = reason
        trade.bars_held = bars_held

        # Calculate PnL
        if trade.direction == "LONG":
            price_diff = trade.exit_price_adjusted - trade.entry_price_adjusted
        else:
            price_diff = trade.entry_price_adjusted - trade.exit_price_adjusted

        trade.pnl = (price_diff * instrument.point_value * trade.size) - instrument.commission_per_trade

        # Calculate R-multiple based on initial risk (including entry costs, but stop price usually doesn't have costs baked in until exit)
        # We define initial risk based on entry_price_adjusted and stop_price.
        if trade.direction == "LONG":
            risk_price = trade.entry_price_adjusted - trade.initial_stop_price
        else:
            risk_price = trade.initial_stop_price - trade.entry_price_adjusted

        risk_amount = risk_price * instrument.point_value * trade.size

        if risk_amount > 0:
            trade.r_multiple = trade.pnl / risk_amount
        else:
            trade.r_multiple = 0.0

        self.trades.append(trade)
        self.equity += trade.pnl
        self.equity_curve.append(self.equity)
        self.equity_dates.append(time)
