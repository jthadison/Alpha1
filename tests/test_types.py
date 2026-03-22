import pandas as pd
import pytest
from alpha1.data.types import validate_ohlcv

def test_validate_ohlcv_missing_cols():
    df = pd.DataFrame({"open": [1]})
    df.index = pd.DatetimeIndex(["2024-01-01"], tz="UTC")
    with pytest.raises(ValueError, match="Missing required columns"):
        validate_ohlcv(df)

def test_validate_ohlcv_no_tz():
    df = pd.DataFrame({"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]})
    df.index = pd.DatetimeIndex(["2024-01-01"])  # No tz
    with pytest.raises(ValueError, match="timezone-aware"):
        validate_ohlcv(df)
        
def test_validate_ohlcv_nan():
    df = pd.DataFrame({"open": [1], "high": [float('nan')], "low": [1], "close": [1], "volume": [1]})
    df.index = pd.DatetimeIndex(["2024-01-01"], tz="UTC")
    with pytest.raises(ValueError, match="NaN values"):
        validate_ohlcv(df)

def test_validate_ohlcv_monotonic():
    df = pd.DataFrame({"open": [1, 1], "high": [1, 1], "low": [1, 1], "close": [1, 1], "volume": [1, 1]})
    # Decreasing dates
    df.index = pd.DatetimeIndex(["2024-01-02", "2024-01-01"], tz="UTC")
    with pytest.raises(ValueError, match="strictly monotonic"):
        validate_ohlcv(df)

def test_validate_ohlcv_duplicates():
    df = pd.DataFrame({"open": [1, 1], "high": [1, 1], "low": [1, 1], "close": [1, 1], "volume": [1, 1]})
    df.index = pd.DatetimeIndex(["2024-01-01", "2024-01-01"], tz="UTC")
    with pytest.raises(ValueError, match="duplicate"):
        validate_ohlcv(df)
