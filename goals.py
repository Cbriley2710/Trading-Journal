"""
Goals
=====================
The goal-tracking system (see pages/6_Goals.py). Two layers,
deliberately kept separate so adding a new goal later never means
touching how tracked goals get displayed or evaluated:

  - GOAL_LIBRARY: the catalog of available goals - name, category, which
    timeframe(s) make sense for it, which metric actually computes it, a
    plain-language description of where its numbers come from, and
    which "direction" counts as good (see status_zone() below).

  - METRIC_FUNCS: one function per underlying calculation, each taking a
    `window` (what slice of time to look at - see resolve_window()) and
    a `context` (the data it needs - see build_context()) and returning
    a single number, or None if it can't be computed yet. A tracked
    goal's Current Value is always METRIC_FUNCS[goal["metric"]](window,
    context) - never a per-row formula - which is what makes "add a row
    to GOAL_LIBRARY" the only thing a new goal ever needs.

Started fresh (2026-08) with two "Process" goals (did you actually do
the habit, not a trade-performance number) instead of the earlier set
of 13 Statistical (trades-table-derived) goals - those were removed
wholesale rather than carried forward; see git history if any of them
are wanted back later, one at a time.

Every tracked goal now uses a Warning Level / Alert Level pair instead
of the old Target Value + Comparison - see status_zone() below for why
that's a three-way read (Good/Warning/Alert) instead of a binary
Met/Not Met.
"""

import calendar
from datetime import timedelta

import charting
import database
import timeutil

# Every timeframe any goal can use. A goal's own "timeframes" list (see
# GOAL_LIBRARY) is a subset of this.
TIMEFRAMES = ["Daily", "Weekly", "Monthly", "Yearly", "Rolling 10", "Rolling 20", "Rolling 30", "All-Time"]

# Timeframes with a real calendar period that's still "open" (more days
# could still happen before it's over) - see status_zone() callers for
# why that matters: a shortfall here isn't final yet the way it is for
# a Rolling window or All-Time.
_OPEN_PERIOD_TIMEFRAMES = {"Daily", "Weekly", "Monthly", "Yearly"}


GOAL_LIBRARY = [
    {
        "key": "daily_journal_pct", "name": "Daily Journal %", "category": "Process",
        "timeframes": ["Monthly"], "metric": "daily_journal_pct", "unit": "%",
        "direction": "higher_is_better",
        "data_source": (
            "Days this month with a \"Today's Thoughts\" journal entry ÷ days completed so far "
            "this month, excluding Friday and Saturday (Friday's session gets journaled Sunday) "
            "and today itself (that entry isn't due until tonight)"
        ),
    },
    {
        "key": "monthly_review_pct", "name": "Monthly Review Credit %", "category": "Process",
        "timeframes": ["Yearly"], "metric": "monthly_review_pct", "unit": "%",
        "direction": "higher_is_better",
        "data_source": (
            "Months this year whose closed trades were ALL reviewed (with reflections written) "
            "by the end of the following month, as a % of months whose deadline has already passed"
        ),
    },
    {
        "key": "stop_loss_coverage_pct", "name": "Stop-Loss Coverage %", "category": "Process",
        "timeframes": ["All-Time"], "metric": "stop_loss_coverage_pct", "unit": "%",
        "direction": "higher_is_better",
        "data_source": (
            "Currently open positions with a stop-loss set (Open Positions page) ÷ all "
            "currently open positions - a live snapshot, not a date-windowed period"
        ),
    },
    {
        "key": "stop_loss_usage_pct", "name": "Stop-Loss Usage % (Closed Trades)", "category": "Process",
        "timeframes": ["Monthly", "Yearly"], "metric": "stop_loss_usage_pct", "unit": "%",
        "direction": "higher_is_better",
        "data_source": (
            "Closed trades in the period that had a stop-loss set at some point between entry "
            "and exit ÷ all closed trades in the period. Only counts a stop set via the Open "
            "Positions/Shortlist pages AFTER this goal was added (2026-08) - stop-loss history "
            "isn't tracked before that, so trades closed earlier can't be scored"
        ),
    },
    {
        "key": "equity_growth_pct", "name": "Equity Growth %", "category": "Statistical",
        "timeframes": ["Monthly", "Yearly"], "metric": "equity_growth_pct", "unit": "%",
        "direction": "higher_is_better",
        "data_source": (
            "Realized P/L closed since the start of the period ÷ Jan 1 account value baseline "
            "(Settings page) - same convention as the Dashboard's own Account Performance tiles"
        ),
    },
    {
        "key": "equity_growth_vs_spy", "name": "Equity Growth vs SPY", "category": "Statistical",
        "timeframes": ["Monthly", "Yearly"], "metric": "equity_growth_vs_spy", "unit": "%",
        "direction": "higher_is_better",
        "data_source": "Equity Growth % minus SPY's own % price change over the same period (excess return)",
    },
    {
        "key": "equity_growth_vs_qqq", "name": "Equity Growth vs QQQ", "category": "Statistical",
        "timeframes": ["Monthly", "Yearly"], "metric": "equity_growth_vs_qqq", "unit": "%",
        "direction": "higher_is_better",
        "data_source": "Equity Growth % minus QQQ's own % price change over the same period (excess return)",
    },
]

GOAL_BY_KEY = {g["key"]: g for g in GOAL_LIBRARY}


def build_context(conn):
    """
    Everything a metric function might need, fetched once per page
    render rather than once per tracked goal.

    "fetch_period_return_pct" is stored as a function reference (not
    called here) rather than pre-fetching every benchmark/timeframe
    combination up front - most page renders won't have every Equity
    Growth vs SPY/QQQ goal tracked at once, so this only pays for a
    live price fetch for whichever ones actually are. Injected this way
    (not called directly from charting.py inside the metric functions
    below) so tests can swap in a fake without touching the network -
    see tests/test_goals.py.
    """
    today = timeutil.today_eastern()
    year_start = today.replace(month=1, day=1)
    return {
        "trades": sorted(database.get_trades(conn), key=lambda t: t["date"]),
        "journaled_dates": database.get_journal_note_dates(conn, year_start, today),
        "reviewed_trade_keys": database.get_reviewed_trade_keys(conn),
        "jan1_balance": database.get_account_value(conn),
        "conn": conn,
        "fetch_period_return_pct": charting.fetch_period_return_pct,
        "open_positions": database.get_open_positions(conn),
        "stop_losses": database.get_all_stop_losses(conn),
        "stop_loss_events": database.get_stop_loss_events(conn),
    }


def resolve_window(timeframe):
    """
    Turns a timeframe string into a `window` dict describing what
    slice of time it means:
      - {"kind": "range", "start": date, "end": date} - a calendar
        period, inclusive, always ending today.
      - {"kind": "rolling", "n": int} - the most recent N trades,
        regardless of date.
      - {"kind": "all"} - everything.
    """
    today = timeutil.today_eastern()
    if timeframe == "Daily":
        return {"kind": "range", "start": today, "end": today}
    if timeframe == "Weekly":
        monday = today - timedelta(days=today.weekday())
        return {"kind": "range", "start": monday, "end": today}
    if timeframe == "Monthly":
        return {"kind": "range", "start": today.replace(day=1), "end": today}
    if timeframe == "Yearly":
        return {"kind": "range", "start": today.replace(month=1, day=1), "end": today}
    if timeframe.startswith("Rolling "):
        return {"kind": "rolling", "n": int(timeframe.split()[1])}
    if timeframe == "All-Time":
        return {"kind": "all"}
    raise ValueError(f"Unknown timeframe: {timeframe!r}")


def _last_day_of_month(any_date_in_month):
    """The date of the last day of any_date_in_month's calendar month."""
    last_day = calendar.monthrange(any_date_in_month.year, any_date_in_month.month)[1]
    return any_date_in_month.replace(day=last_day)


def _first_of_next_month(any_date_in_month):
    """The 1st of the calendar month right after any_date_in_month's."""
    last_day = _last_day_of_month(any_date_in_month)
    return last_day + timedelta(days=1)


def _daily_journal_pct(window, context):
    """% of days in the window with a Today's Thoughts journal entry -
    Friday and Saturday are excluded from both the numerator and the
    denominator (the user journals Friday's market action on Sunday, so
    there's nothing to write on Friday or Saturday itself). Today
    itself is ALSO excluded - the journal entry for today gets written
    tonight, so counting today as a miss before the day is even over
    would unfairly drag the % down; window["end"] is always today (see
    resolve_window()'s "Monthly" case), so window["end"] - 1 day is
    "yesterday" without this function needing its own clock read. None
    if the window has no countable days at all (e.g. it's the 1st of
    the month, or the whole window is Friday/Saturday)."""
    journaled_dates = context["journaled_dates"]
    total = done = 0
    d = window["start"]
    last_countable_day = window["end"] - timedelta(days=1)
    while d <= last_countable_day:
        if d.weekday() not in (4, 5):  # Friday=4, Saturday=5
            total += 1
            if d in journaled_dates:
                done += 1
        d += timedelta(days=1)
    if total == 0:
        return None
    return done / total * 100


def _month_is_credited(month_start, context, today):
    """True/False/None for whether month_start's calendar month earned
    "Monthly Review" credit - None means "not decided yet" (either no
    trades closed that month, so there's nothing to review, or the
    deadline - the end of the FOLLOWING month - hasn't passed yet, so a
    shortfall isn't final). True/False only once the deadline has
    passed, based on whether every trade that closed that month is
    covered by a reviewed_trade_keys entry with reflections written, on
    or before that deadline."""
    month_end = _last_day_of_month(month_start)
    trades_in_month = [
        t for t in context["trades"]
        if month_start <= t["date"].date() <= month_end
    ]
    if not trades_in_month:
        return None

    deadline = _last_day_of_month(_first_of_next_month(month_start))

    covered_keys = {
        (r["symbol"], r["entry_date"].isoformat(), r["exit_date"].isoformat(), r["direction"],
         r["quantity"], r["buy_price"], r["sell_price"])
        for r in context["reviewed_trade_keys"]
        if r["reflections_notes"] and r["report_created_at"].date() <= deadline
    }
    trade_keys = {
        (t["symbol"], t["entry_date"].date().isoformat(), t["date"].date().isoformat(), t["direction"],
         t["quantity"], t["buy_price"], t["sell_price"])
        for t in trades_in_month
    }

    if trade_keys.issubset(covered_keys):
        return True
    if today > deadline:
        return False
    return None  # deadline hasn't passed yet - still pending


def _monthly_review_pct(window, context):
    """% of this year's "decided" months (deadline already passed, see
    _month_is_credited()) that earned Monthly Review credit. A month
    that's still pending (deadline not passed) or had no trades at all
    is left out of both the numerator and denominator - it isn't a
    failure yet, or there was nothing to review. None if no month has
    been decided yet."""
    today = timeutil.today_eastern()
    decided = []
    month_start = window["start"].replace(day=1)
    while month_start <= today:
        result = _month_is_credited(month_start, context, today)
        if result is not None:
            decided.append(result)
        month_start = _first_of_next_month(month_start)
    if not decided:
        return None
    return sum(decided) / len(decided) * 100


def _stop_loss_coverage_pct(window, context):
    """% of currently open positions that have a stop-loss set - a live
    snapshot (ignores `window` entirely, unlike every other goal here),
    since there's no meaningful "this month's" version of "do my
    CURRENT positions have stops right now." None if there are no open
    positions at all (nothing to have a % of)."""
    positions = context["open_positions"]
    if not positions:
        return None
    stop_losses = context["stop_losses"]
    covered = sum(1 for p in positions if stop_losses.get(p["symbol"]) is not None)
    return covered / len(positions) * 100


def _trade_had_stop_loss(trade, stop_loss_events):
    """Whether a stop_loss_events row exists for this trade's symbol
    with set_at falling between its entry and exit date - i.e. a stop
    was actively set at SOME point while the trade was open, even if it
    was later cleared or the trade closed before hitting it. Doesn't
    matter what the stop's actual price was, just that one existed."""
    entry = trade["entry_date"].date()
    exit_date = trade["date"].date()
    return any(
        e["symbol"] == trade["symbol"] and entry <= e["set_at"].date() <= exit_date
        for e in stop_loss_events
    )


def _stop_loss_usage_pct(window, context):
    """% of closed trades in the period that had a stop-loss set at
    some point between entry and exit - see stop_loss_events' own note
    in init_db() for why this can only ever cover trades closed after
    that table started logging (nothing before it can be retroactively
    known). None if there are no closed trades in the period."""
    trades = [t for t in context["trades"] if window["start"] <= t["date"].date() <= window["end"]]
    if not trades:
        return None
    events = context["stop_loss_events"]
    protected = sum(1 for t in trades if _trade_had_stop_loss(t, events))
    return protected / len(trades) * 100


def _equity_growth_pct(window, context):
    """Realized P/L closed since the start of the period, as a % of the
    Jan 1 account value baseline - the SAME convention (period P/L ÷
    Jan1 baseline, not the fully-calculated current value) already used
    by the Dashboard's own Account Performance tiles, deliberately kept
    consistent rather than re-deriving a different "growth" number here
    (dividing by current value instead of Jan1 baseline was a real bug
    there once - it self-inflates as the year goes on). None if no Jan
    1 baseline has been set (Dashboard's Account Settings)."""
    jan1_balance = context.get("jan1_balance")
    if not jan1_balance:
        return None
    period_pl = database.get_realized_pl_since(context["conn"], window["start"])
    return period_pl / jan1_balance * 100


def _equity_growth_vs_benchmark(window, context, symbol):
    """Equity Growth % minus a benchmark symbol's own % price change
    over the same period (start of the window through today) - a
    positive number means beating that benchmark, negative means
    lagging it. None if either side can't be computed (no Jan 1
    baseline, or the benchmark's price data isn't available right now -
    see charting.fetch_period_return_pct())."""
    equity_pct = _equity_growth_pct(window, context)
    if equity_pct is None:
        return None
    benchmark_pct = context["fetch_period_return_pct"](symbol, window["start"])
    if benchmark_pct is None:
        return None
    return equity_pct - benchmark_pct


def _equity_growth_vs_spy(window, context):
    return _equity_growth_vs_benchmark(window, context, "SPY")


def _equity_growth_vs_qqq(window, context):
    return _equity_growth_vs_benchmark(window, context, "QQQ")


METRIC_FUNCS = {
    "daily_journal_pct": _daily_journal_pct,
    "monthly_review_pct": _monthly_review_pct,
    "stop_loss_coverage_pct": _stop_loss_coverage_pct,
    "stop_loss_usage_pct": _stop_loss_usage_pct,
    "equity_growth_pct": _equity_growth_pct,
    "equity_growth_vs_spy": _equity_growth_vs_spy,
    "equity_growth_vs_qqq": _equity_growth_vs_qqq,
}


def current_value(goal, timeframe, context):
    """The one dispatch point every tracked goal's Current Value comes
    from - looks up `goal`'s metric function and runs it against
    whichever window `timeframe` resolves to. This is the entire reason
    adding a new goal never means writing new UI/evaluation code: as
    long as GOAL_LIBRARY names a `metric` that's a real key in
    METRIC_FUNCS, this already knows how to compute it."""
    window = resolve_window(timeframe)
    return METRIC_FUNCS[goal["metric"]](window, context)


def status_zone(value, warning_level, alert_level, direction):
    """
    "Good" / "Warning" / "Alert" for one tracked goal - None if there's
    nothing to compare yet (no current value, or Warning/Alert Level
    hasn't been set in the Settings table). Unlike the old binary Met/
    Not Met model, this is always a live read of where the CURRENT
    value sits relative to both thresholds - there's no separate
    "In Progress" state; a goal on an open-period timeframe (see
    _OPEN_PERIOD_TIMEFRAMES) simply keeps recomputing as the period
    goes, the same way its Current Value already does.
    """
    if value is None or warning_level is None or alert_level is None:
        return None
    if direction == "higher_is_better":
        if value < alert_level:
            return "Alert"
        if value < warning_level:
            return "Warning"
        return "Good"
    if value > alert_level:
        return "Alert"
    if value > warning_level:
        return "Warning"
    return "Good"
