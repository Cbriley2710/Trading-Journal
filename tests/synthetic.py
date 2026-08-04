"""
A hand-tuned synthetic OHLCV panel shared by the screener test suite -
deterministic (fixed RNG seeds), so every test run sees identical
numbers. Four tickers plus QQQ, 320 trading days:

  WINNER   A strong uptrend ($50 -> ~$95) followed by a 14-day
           consolidation with monotonically shrinking daily range and
           volume - by construction this makes the final bar the
           narrowest of its trailing 7 (NR7, and NR7-2 too), with
           volume already below its 50-day average. Verified (see
           tests/test_screener_engine.py) to clear every baseline AND
           strict condition on the last day.
  LAGGARD  Drifts down over the period - fails on trend (price ends
           below its own 21 EMA/50 SMA) and relative strength (doesn't
           beat QQQ's 6-month return), and volume isn't contracting.
  PENNY    Priced under $10 - fails the price floor AND the liquidity
           floor (low dollar volume at a low share price) AND trend.
  THIN     Priced normally and genuinely trending, but at a low share
           count - fails ONLY the liquidity (ADV) floor, isolating that
           one condition from everything else.

Verified failure reasons above were confirmed by running daily_screen's
own filter conditions individually against this fixture, not assumed -
see the git history for that scratch check if this fixture is ever
re-tuned.
"""
import numpy as np
import pandas as pd

N_DAYS = 320
DATES = pd.bdate_range("2024-01-02", periods=N_DAYS)


def _make_qqq():
    rng = np.random.default_rng(42)
    drift = np.linspace(0, 0.10, N_DAYS)
    noise = rng.normal(0, 0.006, N_DAYS).cumsum() * 0.15
    close = 400 * (1 + drift + noise)
    high = close * 1.008
    low = close * 0.992
    volume = rng.integers(30_000_000, 40_000_000, N_DAYS)
    return pd.DataFrame(dict(ticker="QQQ", date=DATES, open=close, high=high,
                              low=low, close=close, volume=volume))


def _make_winner(shrink_days=14, uptrend_range=0.06, end_range=0.006):
    rng = np.random.default_rng(7)
    n_up = N_DAYS - shrink_days
    base = 50 * np.exp(np.linspace(0, np.log(95 / 50), n_up))
    noise = rng.normal(0, 0.004, n_up).cumsum() * 0.3
    up_close = base * (1 + noise)

    plateau_base = up_close[-1]
    plateau_close = plateau_base * (1 + np.linspace(0, 0.01, shrink_days))
    ranges = np.linspace(uptrend_range, end_range, shrink_days)

    close = np.concatenate([up_close, plateau_close])
    day_range_pct = np.concatenate([np.full(n_up, uptrend_range), ranges])
    high = close * (1 + day_range_pct / 2)
    low = close * (1 - day_range_pct / 2)

    vol_up = rng.integers(1_800_000, 2_200_000, n_up)
    vol_shrink = np.linspace(2_000_000, 700_000, shrink_days).astype(int)
    volume = np.concatenate([vol_up, vol_shrink])

    return pd.DataFrame(dict(ticker="WINNER", date=DATES, open=close, high=high,
                              low=low, close=close, volume=volume))


def _make_laggard():
    rng = np.random.default_rng(11)
    noise = rng.normal(0, 0.01, N_DAYS).cumsum()
    close = 60 * (1 + noise * 0.05 - np.linspace(0, 0.05, N_DAYS))
    high = close * 1.015
    low = close * 0.985
    volume = rng.integers(5_000_000, 6_000_000, N_DAYS)
    return pd.DataFrame(dict(ticker="LAGGARD", date=DATES, open=close, high=high,
                              low=low, close=close, volume=volume))


def _make_penny():
    rng = np.random.default_rng(13)
    close = np.full(N_DAYS, 4.0) + rng.normal(0, 0.05, N_DAYS).cumsum() * 0.02
    high = close * 1.03
    low = close * 0.97
    volume = rng.integers(2_000_000, 3_000_000, N_DAYS)
    return pd.DataFrame(dict(ticker="PENNY", date=DATES, open=close, high=high,
                              low=low, close=close, volume=volume))


def _make_thin():
    rng = np.random.default_rng(17)
    close = 40 * np.exp(np.linspace(0, 0.5, N_DAYS))
    high = close * 1.02
    low = close * 0.98
    volume = rng.integers(50_000, 100_000, N_DAYS)
    return pd.DataFrame(dict(ticker="THIN", date=DATES, open=close, high=high,
                              low=low, close=close, volume=volume))


def build_synthetic_panel():
    """Returns the full ticker,date,open,high,low,close,volume panel
    (QQQ + WINNER + LAGGARD + PENNY + THIN) described in this module's
    own docstring."""
    return pd.concat(
        [_make_qqq(), _make_winner(), _make_laggard(), _make_penny(), _make_thin()],
        ignore_index=True,
    )
