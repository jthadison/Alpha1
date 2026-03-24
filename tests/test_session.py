import pandas as pd

from alpha1.config.settings import SessionConfig
from alpha1.strategy.session import SessionBias, SessionDetector, SessionType


def test_session_detection():
    # London timezone
    config = SessionConfig()

    # 00:00 London time -> ASIAN
    # 08:00 London time -> LONDON
    # 13:00 London time -> NEWYORK
    # 18:00 London time -> OFF_HOURS

    # In winter, London is UTC. In summer, London is UTC+1.
    # Let's test a winter date (Jan 2024, UTC == London)
    times = [
        "2024-01-01 00:00:00",
        "2024-01-01 08:00:00",
        "2024-01-01 13:00:00",
        "2024-01-01 18:00:00"
    ]
    df = pd.DataFrame({
        'open': [1, 1, 1, 1], 'high': [2, 2, 2, 2], 'low': [0, 0, 0, 0],
        'close': [1, 1, 1, 1], 'volume': [100, 100, 100, 100]
    })
    df['datetime'] = pd.to_datetime(times, utc=True)
    df = df.set_index('datetime')

    detector = SessionDetector(df, config)
    st = detector.session_types

    assert st[0] == SessionType.ASIAN
    assert st[1] == SessionType.LONDON
    assert st[2] == SessionType.NEWYORK
    assert st[3] == SessionType.OFF_HOURS

def test_dst_handling():
    config = SessionConfig()
    # August 2024, London is UTC+1 (BST)
    # 07:00 UTC = 08:00 London (LONDON session)
    times = [
        "2024-08-01 07:00:00",
    ]
    df = pd.DataFrame({'open': [1], 'high': [2], 'low': [0], 'close': [1], 'volume': [100]})
    df['datetime'] = pd.to_datetime(times, utc=True)
    df = df.set_index('datetime')

    detector = SessionDetector(df, config)
    assert detector.session_types[0] == SessionType.LONDON

def test_asian_sweep():
    config = SessionConfig()
    data = [
        (10.0, 15.0,  5.0, 10.0), # 00:00 UTC (ASIAN)
        (10.0, 12.0,  8.0, 11.0), # 04:00 UTC (ASIAN) -> Asian High=15, Low=5
        (10.0, 16.0,  8.0, 11.0), # 08:00 UTC (LONDON) -> Sweeps high (16 > 15) -> SHORT bias
    ]
    times = ["2024-01-01 00:00:00", "2024-01-01 04:00:00", "2024-01-01 08:00:00"]
    df = pd.DataFrame(data, columns=["open", "high", "low", "close"])
    df["volume"] = 100
    df["datetime"] = pd.to_datetime(times, utc=True)
    df = df.set_index("datetime")

    detector = SessionDetector(df, config)
    bias = detector.get_session_bias()

    assert detector.asian_high[1] == 15.0
    assert bias.iloc[2] == SessionBias.SHORT.value
