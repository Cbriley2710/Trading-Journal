"""
Screener Data
=====================
Fetches the daily OHLCV price panel the Screener page hands to
screener.engine.screen_tickers() - concurrent yfinance fetches (one
worker pool, a small cap so this doesn't read as a burst of hundreds of
simultaneous requests to Yahoo Finance and trip its rate limiting - see
warm_price_cache.py's own note on that), split-adjusted but NOT
dividend-adjusted (see _fetch_one_raw()'s own note on why auto_adjust=
False already gives exactly that), cached per ticker per trading
session so re-running the same list twice in one session is instant.

QQQ is always fetched alongside whatever the user typed in, and its
failure is NOT treated like any other ticker's - see fetch_universe().
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import pandas as pd
import streamlit as st
import yfinance as yf
from yfinance.exceptions import YFRateLimitError

import timeutil

# 300 trading days is screen_tickers()'s own floor (126-bar RS lookback
# + 50-bar SMA + rolling-window warmup) - fetching "2y" of daily bars
# (~500 trading days) comfortably clears that with room to spare rather
# than fetching exactly 300 and having a handful of holidays/gaps push
# a real ticker just under the line.
FETCH_PERIOD = "2y"
MIN_TRADING_DAYS = 300
MAX_WORKERS = 8


class DataFetchError(Exception):
    """Raised when the benchmark itself couldn't be fetched or doesn't
    have enough history - a hard stop for the Screener page (see this
    module's own docstring and screener/engine.py's require_benchmark),
    never something to silently substitute around."""


@dataclass
class FetchResult:
    prices: pd.DataFrame           # ticker,date,open,high,low,close,volume
    insufficient: dict = field(default_factory=dict)   # ticker -> bar count fetched
    failed: list = field(default_factory=list)          # tickers that never returned data
    session_date: object = None    # the trading day this fetch/cache is keyed to


def _fetch_one_raw(ticker, session_date, period):
    """
    The actual network call - `session_date` is only ever used as a
    cache key (see the @st.cache_data wrapper below), not read inside
    this function; it's what makes a new trading session automatically
    invalidate yesterday's cached fetch without a manual TTL.

    auto_adjust=False here does NOT mean "raw, unadjusted prices" the
    way it sounds - Yahoo Finance's underlying historical series is
    already split-adjusted at the source (a stock split is applied
    retroactively to the whole history there, not something the
    auto_adjust flag controls) - auto_adjust=True only adds DIVIDEND
    adjustment on top of that. So auto_adjust=False is exactly
    "split-adjusted, not dividend-adjusted," with no extra math needed
    (verified directly against a real split - see this project's own
    dev notes if this ever needs re-checking).

    Retries up to 3 times with a short backoff on a rate limit or any
    other fetch error - returns None (not an exception) if every
    attempt fails, so the caller can tell "this ticker has no data" (a
    typo/delisted symbol) apart from "the whole run should stop" (the
    benchmark itself failing - see fetch_universe()).
    """
    for attempt in range(3):
        try:
            history = yf.Ticker(ticker).history(
                period=period, interval="1d", auto_adjust=False, actions=False)
            if history.empty:
                return None
            history.index = history.index.tz_localize(None)
            return history
        except YFRateLimitError:
            time.sleep(2 ** attempt)
        except Exception:
            if attempt == 2:
                return None
            time.sleep(1)
    return None


@st.cache_data(show_spinner=False)
def _cached_fetch_one(ticker, session_date, period=FETCH_PERIOD):
    return _fetch_one_raw(ticker, session_date, period)


def to_price_frame(ticker, history):
    """
    Converts one ticker's raw yfinance history (as returned by
    _fetch_one_raw()/_cached_fetch_one() - a DatetimeIndex'd DataFrame
    with Open/High/Low/Close/Volume) into the flat ticker,date,open,
    high,low,close,volume shape screener.engine and screener.reconcile
    both expect. Shared by fetch_universe() below and
    screener.reconcile's own per-ticker refetch, so there's exactly one
    place that knows this column mapping.
    """
    return pd.DataFrame({
        "ticker": ticker,
        "date": history.index.normalize(),
        "open": history["Open"].to_numpy(),
        "high": history["High"].to_numpy(),
        "low": history["Low"].to_numpy(),
        "close": history["Close"].to_numpy(),
        "volume": history["Volume"].to_numpy(),
    })


def fetch_universe(tickers, benchmark="QQQ", progress_callback=None):
    """
    Fetches `tickers` PLUS `benchmark` (always, regardless of whether
    it's already in `tickers`) concurrently, worker-capped at
    MAX_WORKERS. `progress_callback(done, total, ticker)`, if given, is
    called after each fetch completes - the page uses this to drive a
    visible st.progress bar.

    Raises DataFetchError immediately if the benchmark itself couldn't
    be fetched or came back with fewer than MIN_TRADING_DAYS bars - no
    equal-weight or other substitute, ever, for the app (see this
    module's own docstring). Every OTHER ticker's failure is instead
    collected into the returned FetchResult (`failed`/`insufficient`)
    so one bad symbol never fails the whole run.
    """
    all_tickers = list(dict.fromkeys([benchmark] + [t for t in tickers if t]))
    session_date = timeutil.expected_last_trading_day()

    histories = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_cached_fetch_one, ticker, session_date): ticker
            for ticker in all_tickers
        }
        done = 0
        for future in as_completed(futures):
            ticker = futures[future]
            histories[ticker] = future.result()
            done += 1
            if progress_callback:
                progress_callback(done, len(all_tickers), ticker)

    bm_history = histories.get(benchmark)
    if bm_history is None:
        raise DataFetchError(
            f"Could not fetch {benchmark} (the benchmark) from Yahoo Finance after "
            f"retrying - the RS filter is meaningless without it, so the run has "
            f"stopped rather than screening against a substitute. This is usually "
            f"transient (rate limiting) - try again in a minute."
        )
    if len(bm_history) < MIN_TRADING_DAYS:
        raise DataFetchError(
            f"{benchmark} only returned {len(bm_history)} trading days (need at "
            f"least {MIN_TRADING_DAYS}) - can't reliably compute the 6-month "
            f"relative-strength window against it."
        )

    frames = []
    insufficient = {}
    failed = []
    for ticker, history in histories.items():
        if history is None:
            if ticker != benchmark:
                failed.append(ticker)
            continue
        if len(history) < MIN_TRADING_DAYS:
            insufficient[ticker] = len(history)
            continue
        frames.append(to_price_frame(ticker, history))

    prices = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["ticker", "date", "open", "high", "low", "close", "volume"])

    return FetchResult(
        prices=prices, insufficient=insufficient, failed=failed, session_date=session_date)
