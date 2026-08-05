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
            "Days this month with a \"Today's Thoughts\" journal entry ÷ days so far "
            "this month, excluding Friday and Saturday (Friday's session gets journaled Sunday)"
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
]

GOAL_BY_KEY = {g["key"]: g for g in GOAL_LIBRARY}


def build_context(conn):
    """
    Everything a metric function might need, fetched once per page
    render rather than once per tracked goal.
    """
    today = timeutil.today_eastern()
    year_start = today.replace(month=1, day=1)
    return {
        "trades": sorted(database.get_trades(conn), key=lambda t: t["date"]),
        "journaled_dates": database.get_journal_note_dates(conn, year_start, today),
        "reviewed_trade_keys": database.get_reviewed_trade_keys(conn),
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
    there's nothing to write on Friday or Saturday itself). None if the
    window has no countable days at all (only possible if today is the
    1st of the month and it's a Friday or Saturday)."""
    journaled_dates = context["journaled_dates"]
    total = done = 0
    d = window["start"]
    while d <= window["end"]:
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


METRIC_FUNCS = {
    "daily_journal_pct": _daily_journal_pct,
    "monthly_review_pct": _monthly_review_pct,
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
