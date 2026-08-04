#!/usr/bin/env python3
"""
daily_screen.py — setups with buy points in the next 1-3 sessions.

    python daily_screen.py --prices prices.csv
    python daily_screen.py --prices prices.csv --equity 25000 --loose
    python daily_screen.py --prices prices.csv --date 2025-01-16 --log signals.csv

Input CSV: ticker,date,open,high,low,close,volume  (QQQ included)

TWO TIERS
  baseline  liquidity + trend + NR7 + volume contraction + beats QQQ over 6mo
  strict    baseline AND RS line at a 6-month high AND within 8% of the 6-month high

Strict is shown by default (~0.8 signals/week, 0.91 avg R over 2022-2026 testing).
Baseline is always LOGGED even when not displayed, so filter choices stay reviewable.

A thin CLI now - the actual screening logic (indicators, the two
tiers, position sizing, the TRIGGER_DAYS lookback) lives in
screener/engine.py, which pages/7_Screener.py (the in-app Screener)
calls directly. This script exists for command-line/offline use with a
plain price CSV and its own append-only signal_log.csv - the app logs
to Postgres instead (see database.py's signal_log table). Both call
the exact same screen_tickers(), so a filter tweak in screener/engine.py
never needs to happen twice. append_log() below is the one piece of
real logic that stays here rather than in screener/engine.py - it's
CSV file I/O specific to this script, not part of the screening rules
themselves.

Unlike the app, this CLI keeps its original convenience behavior of
falling back to an equal-weight benchmark if QQQ isn't in the price
file (require_benchmark=False below) - see screener/engine.py's own
docstring on why the app itself never does that.
"""
import argparse, os, sys
import pandas as pd

from screener.engine import P, ScreenError, screen_tickers


def append_log(path, w):
    """Append-only signal log. Dedupes on (signal_date, ticker)."""
    cols = ["signal_date", "ticker", "tier", "trigger", "stop", "risk_pct", "shares",
            "position", "risk_usd", "adr", "rs_excess", "rsline_at_high", "nr7_2",
            "pct_off_high", "close", "gate_pct", "expires"]
    new = w[cols]
    if os.path.exists(path):
        old = pd.read_csv(path)
        merged = pd.concat([old, new]).drop_duplicates(["signal_date", "ticker"], keep="first")
    else:
        merged = new
    merged.to_csv(path, index=False)
    return len(merged) - (len(pd.read_csv(path)) if False else 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", required=True)
    ap.add_argument("--date", default=None)
    ap.add_argument("--equity", type=float, default=10000)
    ap.add_argument("--benchmark", default="QQQ")
    ap.add_argument("--loose", action="store_true", help="show baseline tier too")
    ap.add_argument("--log", default="signal_log.csv", help="append-only signal log")
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--csv", default=None, help="write today's watchlist here")
    ap.add_argument("--force", action="store_true", help="screen even if the gate is closed")
    a = ap.parse_args()

    df = pd.read_csv(a.prices, parse_dates=["date"])
    need = {"ticker", "date", "open", "high", "low", "close", "volume"}
    if not need.issubset(df.columns):
        sys.exit(f"missing columns: {need - set(df.columns)}")

    try:
        result = screen_tickers(
            df, a.date or df.date.max(), a.equity, benchmark=a.benchmark,
            require_benchmark=False)
    except ScreenError as exc:
        sys.exit(f"ERROR: {exc}")

    print(f"\n{'='*80}\nAS OF {result.asof}   |   equity ${a.equity:,.0f}   |   "
          f"{result.n_screened} tickers")
    print(f"MARKET GATE: {'OPEN' if result.gate_open else 'CLOSED'}  "
          f"({result.benchmark_label}, {result.gate_pct:+.2f}% vs 21 EMA)")
    if result.benchmark_return_pct is not None:
        print(f"BENCHMARK 6-MO RETURN: {result.benchmark_return_pct:+.1f}%   "
              f"(candidates must beat this)")
    if not result.gate_open and not a.force:
        print("\nGate closed — no new entries indicated. --force to screen anyway.")
        return

    w = result.watchlist
    if w.empty:
        print("\nNo qualifying setups.")
        return

    if not a.no_log:
        append_log(a.log, w)

    strict, loose = result.strict, result.baseline_only
    show = w if a.loose else strict
    cols = ["ticker", "trigger", "stop", "risk_pct", "shares", "position", "risk_usd",
            "adr", "rs_excess", "pct_off_high", "age", "expires"]

    print(f"\nSTRICT: {len(strict)}   |   baseline also passing: {len(loose)} "
          f"(logged, not shown — use --loose)")
    if len(show) == 0:
        print("\nNothing at the strict tier today.")
    else:
        if a.loose:
            cols = ["tier"] + cols
        print(f"\nWATCHLIST — {len(show)} pending trigger(s)\n")
        print(show[cols].to_string(index=False))
        flags = show[show.nr7_2]
        if len(flags):
            print(f"\n  NR7-2 (two narrow bars, higher odds of immediate expansion): "
                  f"{', '.join(flags.ticker)}")
        print(f"\n  Total risk if all fill: ${show.risk_usd.sum():,.0f} "
              f"({100*show.risk_usd.sum()/a.equity:.1f}% of equity)")
    print("\n  age 0 = flagged today, 1-2 = carried from a prior session.")
    print("  rs_excess = 6-month return minus the benchmark's, in points.")
    print("  Buy stop at trigger; initial stop as shown. Exit: 2 consecutive closes")
    print("  below 21 EMA x (1 - 0.25 x ADR%), or a decline > 3 x ADR% intraday.")
    if not a.no_log:
        print(f"\n  logged to {a.log} (baseline + strict, for later review)")
    if a.csv:
        show[cols].to_csv(a.csv, index=False)
        print(f"  watchlist written to {a.csv}")


if __name__ == "__main__":
    main()
