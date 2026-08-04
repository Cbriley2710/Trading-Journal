"""
Screener Engine
=====================
The setups-with-buy-points-in-the-next-1-3-sessions logic, refactored
out of the original `daily_screen.py` CLI script (still at the repo
root, now a thin wrapper around this module - see its own docstring)
into an importable module the Screener page (pages/7_Screener.py) can
call directly instead of shelling out to a script and parsing a CSV.

indicators()/benchmark_series()/build()/market_ok()/screen()/
make_orders() below are the SAME functions, unchanged logic - this is a
move, not a rewrite (see tests/test_screener_engine.py, which locks in
their exact behavior against the original script BEFORE this refactor
and continues to pass unchanged against this module after it - only
the import line at the top of that test file changes).

screen_tickers() is new: it's the orchestration daily_screen.py's own
main() used to do inline (the TRIGGER_DAYS lookback loop, dedupe,
market gate, benchmark return) plus a funnel breakdown main() never
had, packaged into one call with a typed ScreenResult return instead of
printing straight to stdout - what a UI needs to actually render this
instead of a terminal.

TWO TIERS
  baseline  liquidity + trend + NR7 + volume contraction + beats QQQ over 6mo
  strict    baseline AND RS line at a 6-month high AND within 8% of the 6-month high
"""
import sys
from dataclasses import dataclass
from datetime import date as date_type

import numpy as np
import pandas as pd

# Kept intact and exposed exactly as in daily_screen.py - every filter
# threshold and sizing rule this engine uses lives here, nowhere else,
# so the Advanced panel on the Screener page can read/override these
# same names directly instead of a shadow copy that could drift.
P = dict(
    EMA=21, ADR_LEN=20, RS_LEN=126,
    MIN_PRICE=10.0, MIN_ADV=20e6, MIN_ADR=3.0,
    RS_EXCESS=0.0,           # 6-mo return must beat the benchmark by this much
    NEAR_HIGH_BASE=0.15,     # baseline: within 15% of the 6-month high
    NEAR_HIGH_STRICT=0.08,   # strict:   within 8%
    RSLINE_TOL=0.98,         # RS line within 2% of its 6-month high
    STOP_ADR_MULT=1.0,       # stop = NR7 low - 1.0 x ADR%
    MAX_RISK_PCT=8.0,
    TRIGGER_DAYS=3,
    RISK_PER_TRADE=0.01,
    MAX_POS_PCT=0.25,
)

# The funnel stages screen_tickers() reports counts for, in the order a
# ticker is actually eliminated (see _funnel_counts() below) - matches
# the order requested for the Screener page's funnel breakdown.
# "insufficient_history" isn't computed here: a ticker with too few
# bars is excluded from `prices` before it ever reaches this module
# (see screener/data.py) - the page folds that count in as the funnel's
# first row, ahead of these.
FUNNEL_STAGES = [
    "below_liquidity_floor",
    "below_price_floor",
    "adr_too_low",
    "below_moving_averages",
    "not_near_high",
    "no_nr7",
    "volume_not_contracting",
    "did_not_beat_benchmark",
    "risk_too_wide",
]


class ScreenError(Exception):
    """Raised for conditions the caller should stop and show cleanly -
    a missing benchmark, or no trading data on/before the requested
    as-of date. Deliberately NOT a bare sys.exit() (what the original
    CLI script uses) - a Streamlit page needs a catchable exception,
    not something that tries to terminate the whole process."""


@dataclass
class ScreenResult:
    """Everything the Screener page needs to render one run. `watchlist`
    already covers the TRIGGER_DAYS (3-session) lookback and is deduped
    by ticker (see screen_tickers()) - it's the same shape whether zero,
    one, or many signals came back. `funnel` counts start from whatever
    `panel` already contains for `asof` (see FUNNEL_STAGES) - the page
    prepends its own "insufficient history" count ahead of these, since
    that exclusion happens before this module ever sees the data.
    """
    asof: date_type
    gate_open: bool
    gate_pct: float
    benchmark_label: str
    benchmark_return_pct: float | None
    n_screened: int
    watchlist: pd.DataFrame
    funnel: dict
    panel: pd.DataFrame

    @property
    def strict(self) -> pd.DataFrame:
        if self.watchlist.empty:
            return self.watchlist
        return self.watchlist[self.watchlist.tier == "strict"]

    @property
    def baseline_only(self) -> pd.DataFrame:
        """Baseline passers that did NOT also clear strict - the
        "show baseline too" toggle adds these to the strict list."""
        if self.watchlist.empty:
            return self.watchlist
        return self.watchlist[self.watchlist.tier == "baseline"]


def indicators(g, params=None):
    """
    `params`, if given, overrides the module-level P dict for this call
    only (defaults to P itself when omitted - every existing caller
    that doesn't pass it behaves exactly as before). This is how the
    Screener page's Advanced panel applies per-run threshold overrides
    WITHOUT mutating the shared P dict in place - P is a module-level
    global, and this module could in principle be handling more than
    one user's concurrent run in the same process (a Streamlit
    deployment isn't single-user just because this app usually is) - a
    temporary "mutate P, screen, restore P" approach would be a real
    race condition waiting to happen. Passing a plain per-call dict
    instead has no shared state to race on.
    """
    params = P if params is None else params
    g = g.sort_values("date").copy()
    g["ema21"] = g.close.ewm(span=params["EMA"], adjust=False).mean()
    g["sma50"] = g.close.rolling(50).mean()
    g["adr"] = 100 * (g.high / g.low).rolling(params["ADR_LEN"]).mean() - 100
    g["advd"] = (g.close * g.volume).rolling(20).mean()
    rng = g.high - g.low
    g["nr7"] = rng == rng.rolling(7).min()
    g["nr7_2"] = g.nr7 & g.nr7.shift(1)
    g["hi6m"] = g.high.rolling(params["RS_LEN"]).max()
    g["rs"] = g.close / g.close.shift(params["RS_LEN"]) - 1
    g["vol20"] = g.volume.rolling(20).mean()
    g["vol50"] = g.volume.rolling(50).mean()
    return g


def benchmark_series(df, benchmark, strict_bm=False):
    """
    Unchanged from daily_screen.py, `strict_bm` included - see
    tests/test_screener_engine.py's own test for this exact sys.exit()
    path. Nothing in this module's actual call graph ever passes
    strict_bm=True (build() below always uses the default), so this
    branch is dead in practice here - the real "no benchmark, no
    screen" enforcement for the Screener page lives in
    screen_tickers()'s own `require_benchmark` check instead, which
    raises a catchable ScreenError rather than trying to exit the
    whole process the way a Streamlit page can't cleanly recover from.
    Kept exactly as-is anyway rather than diverging from the reference
    implementation in a code path nothing here actually exercises.
    """
    if benchmark and benchmark in set(df.ticker):
        return df[df.ticker == benchmark].set_index("date").close, benchmark
    if strict_bm:
        sys.exit(f"ERROR: benchmark {benchmark} not in the price file. "
                 f"Add it to the data pull — the RS filter is meaningless without it.")
    px = df.pivot_table(index="date", columns="ticker", values="close")
    full = px.columns[px.notna().sum() == px.notna().sum().max()]
    s = px[full].pct_change().mean(axis=1).add(1).cumprod()
    return s, f"equal-weight universe (WARNING: {benchmark} not in data)"


def build(df, benchmark, params=None):
    params = P if params is None else params
    df = df.sort_values(["ticker", "date"])
    out = pd.concat([indicators(g, params).assign(ticker=t) for t, g in df.groupby("ticker")])
    bm, label = benchmark_series(df, benchmark)
    out["bm_rs"] = out.date.map(bm / bm.shift(params["RS_LEN"]) - 1)
    out["rs_excess"] = out.rs - out.bm_rs
    out["rsline"] = out.close / out.date.map(bm)
    out["rsl_hi"] = out.groupby("ticker").rsline.transform(
        lambda s: s.rolling(params["RS_LEN"]).max())
    out["rsline_at_high"] = out.rsline >= out.rsl_hi * params["RSLINE_TOL"]
    return out.reset_index(drop=True), bm, label


def market_ok(bm, label, asof, params=None):
    params = P if params is None else params
    ema = bm.ewm(span=params["EMA"], adjust=False).mean()
    if asof not in bm.index:
        return False, np.nan
    ok = bool(bm.loc[asof] > ema.loc[asof] and ema.loc[asof] > ema.shift(5).loc[asof])
    return ok, (bm.loc[asof] / ema.loc[asof] - 1) * 100


def screen(panel, asof, params=None):
    """Returns baseline passes, tagged with whether they also clear strict."""
    params = P if params is None else params
    p = panel[panel.date == asof]
    base = p[(p.nr7) & (p.close > params["MIN_PRICE"]) & (p.advd > params["MIN_ADV"]) &
             (p.adr >= params["MIN_ADR"]) & (p.rs_excess > params["RS_EXCESS"]) &
             (p.close > p.ema21) & (p.close > p.sma50) &
             (p.close >= p.hi6m * (1 - params["NEAR_HIGH_BASE"])) &
             (p.vol20 < p.vol50)].copy()
    base["strict"] = (base.rsline_at_high &
                      (base.close >= base.hi6m * (1 - params["NEAR_HIGH_STRICT"])))
    return base


def make_orders(sig, equity, sig_date, expiry, gate_pct, params=None):
    params = P if params is None else params
    rows = []
    for _, s in sig.iterrows():
        trigger = round(s.high + 0.01, 2)
        stop = round(s.low * (1 - params["STOP_ADR_MULT"] * s.adr / 100), 2)
        if trigger <= stop:
            continue
        risk_pct = (trigger - stop) / trigger * 100
        if risk_pct > params["MAX_RISK_PCT"]:
            continue
        shares = int(min(equity * params["RISK_PER_TRADE"] / (trigger - stop),
                         equity * params["MAX_POS_PCT"] / trigger))
        if shares < 1:
            continue
        rows.append(dict(
            signal_date=str(pd.Timestamp(sig_date).date()), ticker=s.ticker,
            tier="strict" if s.strict else "baseline",
            trigger=trigger, stop=stop, risk_pct=round(risk_pct, 2),
            shares=shares, position=round(shares * trigger),
            risk_usd=round(shares * (trigger - stop)),
            adr=round(s.adr, 2), rs_excess=round(s.rs_excess * 100, 1),
            rsline_at_high=bool(s.rsline_at_high), nr7_2=bool(s.nr7_2),
            pct_off_high=round((s.close / s.hi6m - 1) * 100, 1),
            close=round(s.close, 2), gate_pct=round(gate_pct, 2),
            expires=str(pd.Timestamp(expiry).date())))
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["rs_excess", "risk_pct"],
                                          ascending=[False, True])


def _funnel_counts(panel, asof, params=None):
    """
    Sequentially applies screen()'s own conditions one at a time (in the
    order requested for the Screener page's funnel breakdown) instead of
    screen()'s single combined boolean mask, counting how many tickers
    fall out at EACH stage. `risk_too_wide` is evaluated last, via
    make_orders() itself, since that's where the trigger<=stop and
    MAX_RISK_PCT skips actually live.

    Returns (counts_dict, baseline_survivors) - `baseline_survivors` is
    asserted (see tests/test_screener_engine.py) to be exactly
    screen(panel, asof)'s own output, so this can never silently
    diverge from the real filter.
    """
    params = P if params is None else params
    p = panel[panel.date == asof]
    counts = {}
    remaining = p

    def cut(stage, mask):
        nonlocal remaining
        counts[stage] = int((~mask).sum())
        remaining = remaining[mask]

    cut("below_liquidity_floor", remaining.advd > params["MIN_ADV"])
    cut("below_price_floor", remaining.close > params["MIN_PRICE"])
    cut("adr_too_low", remaining.adr >= params["MIN_ADR"])
    cut("below_moving_averages", (remaining.close > remaining.ema21) & (remaining.close > remaining.sma50))
    cut("not_near_high", remaining.close >= remaining.hi6m * (1 - params["NEAR_HIGH_BASE"]))
    cut("no_nr7", remaining.nr7)
    cut("volume_not_contracting", remaining.vol20 < remaining.vol50)
    cut("did_not_beat_benchmark", remaining.rs_excess > params["RS_EXCESS"])

    baseline_survivors = remaining.copy()
    baseline_survivors["strict"] = (
        baseline_survivors.rsline_at_high &
        (baseline_survivors.close >= baseline_survivors.hi6m * (1 - params["NEAR_HIGH_STRICT"]))
    )

    if len(baseline_survivors):
        orders_today = make_orders(baseline_survivors, 10_000, asof, asof, 0.0, params)
    else:
        orders_today = pd.DataFrame()
    counts["risk_too_wide"] = len(baseline_survivors) - len(orders_today)

    return counts, baseline_survivors


def screen_tickers(prices, asof, equity, benchmark="QQQ", require_benchmark=True, params=None):
    """
    The single entry point pages/7_Screener.py (and daily_screen.py's
    CLI wrapper) calls. `prices` is a ticker,date,open,high,low,close,
    volume DataFrame already covering enough history (see
    screener/data.py - this function does no fetching itself).

    Unlike the original CLI's main(), this does NOT skip screening when
    the market gate is closed - it always screens and returns real
    numbers; "gate closed, so treat this as informational" is a display
    decision the page makes with ScreenResult.gate_open, not something
    this function decides for the caller.

    `require_benchmark`, when True (the default, and what the Screener
    page always uses), raises ScreenError immediately if `benchmark`
    isn't in `prices` - no equal-weight fallback. The CLI wrapper passes
    False to keep its own historical convenience-fallback behavior (see
    benchmark_series()) - that difference is the ONLY place this engine
    behaves differently for the two callers.

    `params`, if given, overrides P for this call only - see
    indicators()'s own note on why this is a per-call dict rather than
    a temporary mutation of the shared P global. The Screener page's
    Advanced panel builds `{**P, **overrides}` and passes it here.
    """
    params = P if params is None else params
    if require_benchmark and benchmark not in set(prices["ticker"]):
        raise ScreenError(
            f"{benchmark} not found in the fetched price data - cannot screen "
            f"without it (the RS filter is meaningless against a substitute)."
        )

    panel, bm, label = build(prices, benchmark, params)
    dates = sorted(panel.date.unique())
    if not dates:
        raise ScreenError("No trading data available to screen.")

    asof_ts = pd.Timestamp(asof)
    if asof_ts not in dates:
        candidates = [d for d in dates if d <= asof_ts]
        if not candidates:
            raise ScreenError(
                f"No trading data on or before {asof_ts.date()} - earliest "
                f"available date is {pd.Timestamp(dates[0]).date()}."
            )
        asof_ts = max(candidates)

    ok, gate_pct = market_ok(bm, label, asof_ts, params)
    n_screened = int(panel[panel.date == asof_ts].ticker.nunique())
    bmr = panel[panel.date == asof_ts].bm_rs.dropna()
    bm_return_pct = float(bmr.iloc[0] * 100) if len(bmr) else None

    i = dates.index(asof_ts)
    frames = []
    for back in range(params["TRIGGER_DAYS"]):
        j = i - back
        if j < 0:
            break
        sd = dates[j]
        exp = dates[min(j + params["TRIGGER_DAYS"], len(dates) - 1)]
        s = screen(panel, sd, params)
        if len(s):
            o = make_orders(s, equity, sd, exp, gate_pct, params)
            if len(o):
                o["age"] = back
                frames.append(o)
    watchlist = pd.concat(frames).drop_duplicates("ticker", keep="last") if frames else pd.DataFrame()

    funnel, _ = _funnel_counts(panel, asof_ts, params)

    return ScreenResult(
        asof=asof_ts.date(),
        gate_open=ok,
        gate_pct=gate_pct,
        benchmark_label=label,
        benchmark_return_pct=bm_return_pct,
        n_screened=n_screened,
        watchlist=watchlist,
        funnel=funnel,
        panel=panel,
    )
