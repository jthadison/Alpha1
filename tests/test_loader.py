import pandas as pd
import pytest
from pathlib import Path
from alpha1.data.loader import load_csv, resample_multi_tf

def test_load_csv_and_resample(tmp_path):
    csv_file = tmp_path / "test_data.csv"
    
    # Create some dummy 1min data
    times = pd.date_range("2024-01-01 08:00:00", periods=60, freq="1min", tz="UTC")
    df = pd.DataFrame({
        "datetime": times,
        "open": range(60),
        "high": range(1, 61),
        "low": range(-1, 59),
        "close": range(60),
        "volume": [100] * 60
    })
    df.to_csv(csv_file, index=False)
    
    # Test load
    loaded = load_csv(csv_file)
    assert len(loaded) == 60
    assert "open" in loaded.columns
    
    # Test resample
    resampled = resample_multi_tf(loaded)
    assert "5min" in resampled
    assert "15min" in resampled
    assert "1h" in resampled
    assert "4h" in resampled
    
    # 60 1min bars = 12 5min bars
    assert len(resampled["5min"]) == 12
