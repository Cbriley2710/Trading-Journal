"""
Goals
=====================
The trading-statistic goal-tracking system (see pages/6_Goals.py). Two
layers, deliberately kept separate so adding a new goal later never
means touching how tracked goals get displayed or evaluated:

  - GOAL_LIBRARY: the catalog of available goals - name, category (all
    "Statistical" for now - see its own note below), which timeframes
    make sense for it, which metric actually computes it, a plain-
    language description of where its numbers come from, and a
    suggested (not enforced) comparison direction. Adding a goal is
    exactly one more entry here.

  - METRIC_FUNCS: one function per underlying calculation (win rate,
    net P/L, profit factor, ...), each taking a `window` (what slice of
    time/trades to look at - see resolve_window()) and a `context`
    (the data it needs - see build_context()) and returning a single
    number, or None if it can't be computed (no trades in the window,
    no losers to divide by for a ratio, no Jan 1 baseline set, ...). A
    tracked goal's Current Value is always METRIC_FUNCS[goal["metric"]]
    (window, context) - never a per-row formula - which is what makes
    "add a row to GOAL_LIBRARY" the only thing a new goal ever needs.

Deliberately scoped to STATISTICAL (trades-table-derived) goals only
for now. The user's own plan is to add "process/discipline" goals
(e.g. "followed my trading plan") and "manual-input" goals (numbers
typed in by hand, not derived from trades at all) later - those would
be more GOAL_LIBRARY entries with a different `category` and a
METRIC_FUNCS entry that doesn't necessarily need `context["trades"]`
at all (a manual-input goal's "metric" might just read a value the
user typed in elsewhere, for instance). Nothing here assumes
Statistical is the only category that will ever exist - `category` is
already a real field on every entry, just uniform today.
"""

from datetime import timedelta

import charting
import database
import timeutil
from analyze_trades import trade_stats

# Every timeframe any goal can use. A goal's own "timeframes" list (see
# GOAL_LIBRARY) is a subset of this - e.g. a streak doesn't make sense
# "Daily", and Net P/L isn't tracked "Rolling" (it's not per-trade), so
# each goal only exposes what its comparison actually holds together.
TIMEFRAMES = ["Daily", "Weekly", "Monthly", "Rolling 10", "Rolling 20", "Rolling 30", "All-Time"]

# Timeframes with a real calendar period that's still "open" (more
# trades could still happen before it's over) - see status() below for
# why that's what separates "In Progress" from "Not Met".
_OPEN_PERIOD_TIMEFRAMES = {"Daily", "Weekly", "Monthly"}


GOAL_LIBRARY = [
    {
        "key": "win_rate", "name": "Win Rate", "category": "Statistical",
        "timeframes": ["Daily", "Weekly", "Monthly", "Rolling 10", "Rolling 20", "Rolling 30"],
        "metric": "win_rate", "unit": "%",
        "data_source": "Winning trades ÷ total trades closed in the period (Trade Analyzer's closed trades)",
        "suggested_comparison": ">=",
    },
    {
        "key": "net_pl", "name": "Net P/L", "category": "Statistical",
        "timeframes": ["Daily", "Weekly", "Monthly"],
        "metric": "net_pl", "unit": "$",
        "data_source": "Sum of profit_loss for trades closed in the period",
        "suggested_comparison": ">=",
    },
    {
        "key": "profit_factor", "name": "Profit Factor", "category": "Statistical",
        "timeframes": ["Weekly", "Monthly"],
        "metric": "profit_factor", "unit": "x",
        "data_source": "Gross winning $ ÷ gross losing $ (absolute value) for the period",
        "suggested_comparison": ">=",
    },
    {
        "key": "avg_winner_dollar", "name": "Average Winner ($)", "category": "Statistical",
        "timeframes": ["Weekly", "Monthly"],
        "metric": "avg_winner_dollar", "unit": "$",
        "data_source": "Average profit_loss across winning trades in the period",
        "suggested_comparison": ">=",
    },
    {
        "key": "avg_winner_pct", "name": "Average Winner (%)", "category": "Statistical",
        "timeframes": ["Weekly", "Monthly"],
        "metric": "avg_winner_pct", "unit": "%",
        "data_source": "Average % change across winning trades in the period (analyze_trades.trade_stats())",
        "suggested_comparison": ">=",
    },
    {
        "key": "avg_loser_dollar", "name": "Average Loser ($)", "category": "Statistical",
        "timeframes": ["Weekly", "Monthly"],
        "metric": "avg_loser_dollar", "unit": "$",
        "data_source": "Average profit_loss across losing trades in the period (comes out negative)",
        "suggested_comparison": ">=",
    },
    {
        "key": "avg_loser_pct", "name": "Average Loser (%)", "category": "Statistical",
        "timeframes": ["Weekly", "Monthly"],
        "metric": "avg_loser_pct", "unit": "%",
        "data_source": "Average % change across losing trades in the period (comes out negative)",
        "suggested_comparison": ">=",
    },
    {
        "key": "largest_win", "name": "Largest Win ($)", "category": "Statistical",
        "timeframes": ["Monthly"],
        "metric": "largest_win", "unit": "$",
        "data_source": "Highest single profit_loss among winning trades in the period",
        "suggested_comparison": ">=",
    },
    {
        "key": "largest_loss", "name": "Largest Loss ($)", "category": "Statistical",
        "timeframes": ["Monthly"],
        "metric": "largest_loss", "unit": "$",
        "data_source": "Lowest single profit_loss among losing trades in the period (comes out negative)",
        "suggested_comparison": ">=",
    },
    {
        "key": "reward_risk", "name": "Reward:Risk Ratio", "category": "Statistical",
        "timeframes": ["Rolling 10", "Rolling 20", "Rolling 30"],
        "metric": "reward_risk", "unit": "x",
        "data_source": "Average winner % ÷ average loser % (absolute value) over the window",
        "suggested_comparison": ">=",
    },
    {
        "key": "current_win_streak", "name": "Current Win Streak", "category": "Statistical",
        "timeframes": ["Rolling 10", "Rolling 20", "Rolling 30", "All-Time"],
        "metric": "current_win_streak", "unit": "trades",
        "data_source": "Consecutive winning trades counting back from the most recent trade",
        "suggested_comparison": ">=",
    },
    {
        "key": "current_loss_streak", "name": "Current Loss Streak", "category": "Statistical",
        "timeframes": ["Rolling 10", "Rolling 20", "Rolling 30", "All-Time"],
        "metric": "current_loss_streak", "unit": "trades",
        "data_source": "Consecutive losing trades counting back from the most recent trade",
        "suggested_comparison": "<=",
    },
    {
        "key": "max_drawdown", "name": "Max Drawdown", "category": "Statistical",
        "timeframes": ["Monthly", "All-Time"],
        "metric": "max_drawdown", "unit": "$",
        "data_source": "Largest peak-to-trough decline in cumulative P/L over the period (comes out negative)",
        "suggested_comparison": ">=",
    },
    {
        "key": "account_growth", "name": "Account Growth", "category": "Statistical",
        "timeframes": ["Monthly", "All-Time"],
        "metric": "account_growth", "unit": "% (Monthly) / $ (All-Time)",
        "data_source": (
            "Monthly: this month's realized P/L as a % of the Jan 1 baseline (Settings page). "
            "All-Time: today's fully calculated account value in dollars (a milestone figure)"
        ),
        "suggested_comparison": ">=",
    },
]

GOAL_BY_KEY = {g["key"]: g for g in GOAL_LIBRARY}


def build_context(conn):
    """
    Everything a metric function might need, fetched once per page
    render rather than once per tracked goal - trades (oldest first,
    including SHORT trades, unlike the Excel-era tracker which left
    those out), the Jan 1 baseline, and `conn` itself for the one goal
    (Account Growth) that needs a live database read of its own.
    """
    trades = sorted(database.get_trades(conn), key=lambda t: t["date"])
    return {
        "trades": trades,
        "jan1_balance": database.get_account_value(conn),
        "conn": conn,
    }


def resolve_window(timeframe):
    """
    Turns a timeframe string into a `window` dict describing what
    slice of trades it means:
      - {"kind": "range", "start": date, "end": date} - a calendar
        period, inclusive, always ending today (Daily/Weekly/Monthly).
      - {"kind": "rolling", "n": int} - the most recent N trades,
        regardless of date.
      - {"kind": "all"} - every trade ever.
    """
    today = timeutil.today_eastern()
    if timeframe == "Daily":
        return {"kind": "range", "start": today, "end": today}
    if timeframe == "Weekly":
        monday = today - timedelta(days=today.weekday())
        return {"kind": "range", "start": monday, "end": today}
    if timeframe == "Monthly":
        return {"kind": "range", "start": today.replace(day=1), "end": today}
    if timeframe.startswith("Rolling "):
        return {"kind": "rolling", "n": int(timeframe.split()[1])}
    if timeframe == "All-Time":
        return {"kind": "all"}
    raise ValueError(f"Unknown timeframe: {timeframe!r}")


def _trades_in_window(trades, window):
    """`trades` must already be sorted oldest-first by exit date (see
    build_context()). Returns the subset the window selects."""
    if window["kind"] == "range":
        return [t for t in trades if window["start"] <= t["date"].date() <= window["end"]]
    if window["kind"] == "rolling":
        return trades[-window["n"]:]
    return list(trades)


def _pct_change(trade):
    """A trade's % change, direction-aware - reuses analyze_trades.
    trade_stats() (the same shared math Trade Analyzer/Logbook/the PDF
    report use) rather than a second copy of the entry/exit pairing
    logic for shorts."""
    stats = trade_stats(
        trade["direction"], trade["buy_price"], trade["sell_price"], trade["quantity"],
        trade["profit_loss"], trade["entry_date"].date(), trade["date"].date(),
    )
    return stats["pct_change"]


def _win_rate(window, context):
    trades = _trades_in_window(context["trades"], window)
    if not trades:
        return None
    wins = sum(1 for t in trades if t["profit_loss"] > 0)
    return wins / len(trades) * 100


def _net_pl(window, context):
    trades = _trades_in_window(context["trades"], window)
    return sum(t["profit_loss"] for t in trades)


def _profit_factor(window, context):
    trades = _trades_in_window(context["trades"], window)
    gross_win = sum(t["profit_loss"] for t in trades if t["profit_loss"] > 0)
    gross_loss = sum(t["profit_loss"] for t in trades if t["profit_loss"] < 0)
    if gross_loss == 0:
        return None  # no losers yet - a ratio against zero isn't meaningful
    return gross_win / abs(gross_loss)


def _avg_winner_dollar(window, context):
    winners = [t["profit_loss"] for t in _trades_in_window(context["trades"], window) if t["profit_loss"] > 0]
    return sum(winners) / len(winners) if winners else None


def _avg_loser_dollar(window, context):
    losers = [t["profit_loss"] for t in _trades_in_window(context["trades"], window) if t["profit_loss"] < 0]
    return sum(losers) / len(losers) if losers else None


def _avg_winner_pct(window, context):
    winners = [_pct_change(t) for t in _trades_in_window(context["trades"], window) if t["profit_loss"] > 0]
    return sum(winners) / len(winners) if winners else None


def _avg_loser_pct(window, context):
    losers = [_pct_change(t) for t in _trades_in_window(context["trades"], window) if t["profit_loss"] < 0]
    return sum(losers) / len(losers) if losers else None


def _largest_win(window, context):
    wins = [t["profit_loss"] for t in _trades_in_window(context["trades"], window) if t["profit_loss"] > 0]
    return max(wins) if wins else None


def _largest_loss(window, context):
    losses = [t["profit_loss"] for t in _trades_in_window(context["trades"], window) if t["profit_loss"] < 0]
    return min(losses) if losses else None


def _reward_risk(window, context):
    avg_win = _avg_winner_pct(window, context)
    avg_loss = _avg_loser_pct(window, context)
    if not avg_win or not avg_loss:
        return None
    return avg_win / abs(avg_loss)


def _streak(window, context):
    """Shared by current_win_streak/current_loss_streak - (streak_type,
    length) for the tail of the window's trades: 'W' or 'L' matching
    whichever the MOST RECENT trade was, and how many trades in a row
    at the end share that same outcome."""
    trades = _trades_in_window(context["trades"], window)
    if not trades:
        return None, 0
    last_is_win = trades[-1]["profit_loss"] > 0
    length = 0
    for t in reversed(trades):
        if (t["profit_loss"] > 0) != last_is_win:
            break
        length += 1
    return ("W" if last_is_win else "L"), length


def _current_win_streak(window, context):
    kind, length = _streak(window, context)
    return length if kind == "W" else 0


def _current_loss_streak(window, context):
    kind, length = _streak(window, context)
    return length if kind == "L" else 0


def _max_drawdown(window, context):
    """Largest peak-to-trough decline in cumulative P/L across the
    window's trades, in dollars - a fresh cumulative curve starting at
    0 for the window (not the account's own running equity), so
    "Monthly" measures the worst dip THAT MONTH specifically, not one
    carried over from an unrelated earlier month. Always <= 0."""
    trades = _trades_in_window(context["trades"], window)
    if not trades:
        return None
    cumulative = peak = max_dd = 0.0
    for t in trades:
        cumulative += t["profit_loss"]
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)
    return max_dd


def _account_growth(window, context):
    """Monthly: this month's realized P/L as a % of the Jan 1 baseline -
    same convention as the Dashboard's own Account Performance tiles
    (period P/L ÷ Jan 1 baseline), so this number means the same thing
    there and here. All-Time: today's fully calculated account value in
    dollars (charting.get_calculated_account_value()) - an absolute
    milestone figure, e.g. a target of ">= 100000" for "reach $100k"."""
    jan1_balance = context.get("jan1_balance")
    if not jan1_balance:
        return None
    if window["kind"] == "all":
        return charting.get_calculated_account_value(context["conn"])
    month_pl = database.get_realized_pl_since(context["conn"], window["start"])
    return month_pl / jan1_balance * 100


METRIC_FUNCS = {
    "win_rate": _win_rate,
    "net_pl": _net_pl,
    "profit_factor": _profit_factor,
    "avg_winner_dollar": _avg_winner_dollar,
    "avg_winner_pct": _avg_winner_pct,
    "avg_loser_dollar": _avg_loser_dollar,
    "avg_loser_pct": _avg_loser_pct,
    "largest_win": _largest_win,
    "largest_loss": _largest_loss,
    "reward_risk": _reward_risk,
    "current_win_streak": _current_win_streak,
    "current_loss_streak": _current_loss_streak,
    "max_drawdown": _max_drawdown,
    "account_growth": _account_growth,
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


COMPARISONS = {
    ">=": lambda current, target: current >= target,
    "<=": lambda current, target: current <= target,
    "=": lambda current, target: abs(current - target) < 0.005,
}


def status(current, target, comparison, timeframe):
    """
    "Met" / "Not Met" / "In Progress" for one tracked goal. "In
    Progress" only applies to a timeframe with a real calendar period
    that's still open (Daily/Weekly/Monthly - see
    _OPEN_PERIOD_TIMEFRAMES) - there's always more of today/this week/
    this month left for the number to still move, so falling short
    isn't a final result yet the way it is for a Rolling window or
    All-Time (always fully "current", nothing left pending). Returns
    None (not a string) if `current` is None - nothing to compare yet,
    e.g. no trades at all in the period.
    """
    if current is None:
        return None
    met = COMPARISONS[comparison](current, target)
    if met:
        return "Met"
    if timeframe in _OPEN_PERIOD_TIMEFRAMES:
        return "In Progress"
    return "Not Met"
