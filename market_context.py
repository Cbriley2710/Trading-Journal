"""
Market Context
=====================
The numbers behind the new "Market Context" page (pages/8_Market_Context.py):
for every ticker currently on your Shortlist (Lists 1-4) or held as an
Open Position, where does it stand today against the same IBD/Minervini-
style framework the Screener page already uses - 21 EMA, 6-month
relative strength vs QQQ, ADR%, distance off its 6-month high, and
volume behavior - plus a plain-English "collective" roll-up (how many
names are above their 21 EMA right now, average RS excess, how many
flagged a distribution or accumulation day today).

Deliberately reuses screener.data.fetch_universe() and
screener.engine.build()/market_ok() rather than recomputing any of this
from scratch - those are the same tested functions the Screener page's
own numbers come from (see screener/engine.py's own docstring/tests),
so a ticker's "21 EMA" here is guaranteed to mean the exact same thing
it means there. This module adds nothing new mathematically - it just
looks at your CURRENT watchlist/positions instead of a pasted list, and
adds one thing engine.py doesn't already compute: a plain up/down-on-
volume "distribution day" / "accumulation day" flag for today, the
classic IBD definition (a distribution day is a decline on higher
volume than the prior session; an accumulation day is the mirror -
a gain on higher volume).

This module is READ-ONLY as far as your database goes - it fetches live
prices from Yahoo Finance (via screener.data) and reads your watchlist/
positions, but writes nothing back. The separate written narrative
(news, Fed, sector commentary) shown on the same page is a different
system - see narrative_generator.py - which calls compute_context()
here to ground its "Stock Highlights" section in these same real
numbers rather than inventing its own.
"""

import pandas as pd

import database
import screener.data as screener_data
import screener.engine as engine


def _symbol_labels(conn):
    """
    Every symbol currently worth showing on the Market Context page,
    mapped to a short label saying where it comes from - a Shortlist
    ticker shows its list's own (possibly renamed) name, an open
    position always shows "Open Positions" regardless of which list (if
    any) it also happens to sit in, since a position matters here
    because you own it, not because of which shortlist it's parked in.
    Deliberately a plain {symbol: label} dict, not a list - a symbol
    that's BOTH an open position and on a shortlist only ever needs to
    appear once on this page.
    """
    names = database.get_watchlist_names(conn)
    labels = {w["symbol"]: names[w["list_id"]] for w in database.get_watchlist(conn)}
    for position in database.get_open_positions(conn):
        labels[position["symbol"]] = "Open Positions"
    return labels


def _day_type(recent_two_rows):
    """
    Today's plain distribution-day / accumulation-day read, the classic
    IBD definition: a DOWN day (today's close below yesterday's) on
    HIGHER volume than yesterday is a distribution day (institutional
    selling); an UP day on higher volume is an accumulation day.
    Anything else (a down day on lighter volume, an up day on lighter
    volume, or a day that's flat) is "neutral" - still informative
    price action, just not the higher-conviction signal a volume spike
    in either direction represents.

    `recent_two_rows` is that ticker's last two rows from the engine's
    own panel (already sorted oldest-to-newest), each with .close/.volume -
    returns None if there isn't a prior day to compare against yet
    (shouldn't happen given screener.data's 2-year fetch, but a brand
    new/thinly-traded symbol could in principle have gaps).
    """
    if len(recent_two_rows) < 2:
        return None
    yesterday, today = recent_two_rows.iloc[-2], recent_two_rows.iloc[-1]
    higher_volume = today.volume > yesterday.volume
    if not higher_volume:
        return "neutral"
    if today.close < yesterday.close:
        return "distribution"
    if today.close > yesterday.close:
        return "accumulation"
    return "neutral"


def compute_context(conn):
    """
    The one function pages/8_Market_Context.py calls. Returns a dict:
        {
            "asof": date,                 # trading day everything below is as-of
            "gate_open": bool,             # QQQ above its own rising 21 EMA
            "gate_pct": float,              # QQQ's %  distance from its 21 EMA
            "rows": [ {...one per symbol...} ],
            "summary": {...collective roll-up...},
            "failed": [...symbols Yahoo Finance had nothing for...],
            "insufficient": {...symbol: bars fetched, too little history...},
        }
    or, if there's nothing to show yet (an empty Shortlist and no open
    positions), a dict with "rows": [] and every other field set to a
    safe empty/None value - the page checks for that and shows a plain
    message instead of an error.

    Each row in "rows" is:
        {
            "symbol": str, "source": str,       # e.g. "List 1" / "Open Positions"
            "close": float, "ema21": float,
            "above_ema21": bool, "dist_from_ema21_pct": float,  # always positive
            "rs_excess_pct": float,              # 6-mo return minus QQQ's, in points
            "rsline_at_high": bool,              # RS line itself at/near a new high
            "adr_pct": float, "pct_off_6mo_high": float,        # always <= 0
            "nr7": bool, "nr7_2": bool,           # today/yesterday both narrowest-range-7
            "vol_contracting": bool,              # 20-day avg volume below 50-day (base-building)
            "day_type": str or None,              # "distribution" / "accumulation" / "neutral"
        }
    """
    labels = _symbol_labels(conn)
    empty = {
        "asof": None, "gate_open": None, "gate_pct": None,
        "rows": [], "summary": None, "failed": [], "insufficient": {},
    }
    if not labels:
        return empty

    symbols = sorted(labels)
    fetch_result = screener_data.fetch_universe(symbols, benchmark="QQQ")
    if fetch_result.prices.empty:
        return {**empty, "failed": fetch_result.failed, "insufficient": fetch_result.insufficient}

    panel, bm, bm_label = engine.build(fetch_result.prices, "QQQ")
    asof_ts = panel.date.max()
    gate_open, gate_pct = engine.market_ok(bm, bm_label, asof_ts)

    rows = []
    for symbol in symbols:
        if symbol in fetch_result.failed or symbol in fetch_result.insufficient:
            continue
        ticker_rows = panel[panel.ticker == symbol].sort_values("date")
        if ticker_rows.empty:
            continue
        today = ticker_rows.iloc[-1]
        # A brand-new fetch can have NaN indicators for the very latest
        # bar if not enough history has rolled in yet (same reasoning as
        # ma_strategy.compute_signal()'s own .dropna() before reading a
        # value) - skip rather than show a broken row.
        if pd.isna(today.ema21) or pd.isna(today.hi6m) or pd.isna(today.rs_excess):
            continue

        rows.append({
            "symbol": symbol,
            "source": labels[symbol],
            "close": float(today.close),
            "ema21": float(today.ema21),
            "above_ema21": bool(today.close > today.ema21),
            "dist_from_ema21_pct": abs(today.close - today.ema21) / today.ema21 * 100,
            "rs_excess_pct": float(today.rs_excess * 100),
            "rsline_at_high": bool(today.rsline_at_high),
            "adr_pct": float(today.adr) if pd.notna(today.adr) else None,
            "pct_off_6mo_high": (today.close / today.hi6m - 1) * 100,
            "nr7": bool(today.nr7),
            "nr7_2": bool(today.nr7_2),
            "vol_contracting": bool(today.vol20 < today.vol50) if pd.notna(today.vol20) and pd.notna(today.vol50) else None,
            "day_type": _day_type(ticker_rows.tail(2)),
        })

    if not rows:
        return {
            **empty, "asof": asof_ts.date(), "gate_open": gate_open, "gate_pct": gate_pct,
            "failed": fetch_result.failed, "insufficient": fetch_result.insufficient,
        }

    above_count = sum(1 for r in rows if r["above_ema21"])
    summary = {
        "n_total": len(rows),
        "n_above_ema21": above_count,
        "pct_above_ema21": above_count / len(rows) * 100,
        "avg_rs_excess_pct": sum(r["rs_excess_pct"] for r in rows) / len(rows),
        "n_distribution_today": sum(1 for r in rows if r["day_type"] == "distribution"),
        "n_accumulation_today": sum(1 for r in rows if r["day_type"] == "accumulation"),
        "n_rsline_at_high": sum(1 for r in rows if r["rsline_at_high"]),
        "n_vol_contracting": sum(1 for r in rows if r["vol_contracting"]),
    }

    return {
        "asof": asof_ts.date(),
        "gate_open": gate_open,
        "gate_pct": gate_pct,
        "rows": rows,
        "summary": summary,
        "failed": fetch_result.failed,
        "insufficient": fetch_result.insufficient,
    }
