"""
Narrative Generator
=====================
Generates the Market Context page's written narrative (see
database.get_market_narrative()/save_market_narrative()) using the
Claude API's server-side web search tool, instead of it being pasted in
by hand from an outside chat session. The "Stock Highlights" section is
grounded in the SAME real numbers market_context.compute_context()
already computes for that page's Shortlist/Open Positions table (21
EMA, RS vs QQQ, ADR%, distribution days) - the model is only ever asked
to comment on those, never to invent its own - while everything else
(index closes, sector performance, market-moving news) comes from the
model's own live web search, since this app has no other source for
that and training data goes stale for "today" specifically.

Costs real API spend every time it runs (several web searches plus one
model call) - see pages/8_Market_Context.py for how that's guarded
against (generated once per day, cached in market_narratives, with a
manual "Regenerate" button for later in the day).
"""

import anthropic

import market_context
import timeutil
from report_utils import get_secret

MODEL = "claude-opus-5"
MAX_WEB_SEARCHES = 10
MAX_TOKENS = 4096


def _format_context_for_prompt(context):
    """
    Turns market_context.compute_context()'s output into a compact,
    plain-text table for the prompt - the REAL numbers the "Stock
    Highlights" section must be grounded in, not invented. A context
    with nothing tracked yet (empty Shortlist and no open positions)
    returns a one-line note instead of an empty table, so the model
    doesn't have to guess why there's no data.
    """
    if not context["rows"]:
        return "No tickers currently on the Shortlist or held as open positions - nothing to report here."

    lines = [
        f"Market gate (QQQ vs its own 21 EMA): {'OPEN' if context['gate_open'] else 'CLOSED'} "
        f"({context['gate_pct']:+.2f}% from 21 EMA), as of {context['asof']}.",
        "",
        "Ticker | List | Close | 21 EMA | RS Excess vs QQQ (pts) | ADR% | % Off 6-Mo High | Today",
    ]
    for row in context["rows"]:
        above = "above" if row["above_ema21"] else "below"
        today_flag = {
            "distribution": "Distribution day", "accumulation": "Accumulation day",
        }.get(row["day_type"], "-")
        adr = f"{row['adr_pct']:.2f}%" if row["adr_pct"] is not None else "n/a"
        lines.append(
            f"{row['symbol']} | {row['source']} | ${row['close']:.2f} | "
            f"${row['ema21']:.2f} ({above}) | {row['rs_excess_pct']:+.1f} | "
            f"{adr} | {row['pct_off_6mo_high']:.1f}% | {today_flag}"
        )
    return "\n".join(lines)


def generate_narrative(conn):
    """
    Builds today's Market Context narrative and returns (success,
    result) - result is the generated Markdown on success, or a plain-
    language error message on failure. Never raises - same (success,
    message) contract daily_report.generate_and_send_report() already
    uses, so callers handle both the same simple way.

    Does NOT save anything to the database - see pages/8_Market_Context.py
    for the save step, kept separate so a caller could preview the
    result before committing it (not currently done, but keeps this
    function a plain "go build one" rather than also owning persistence).
    """
    try:
        api_key = get_secret("ANTHROPIC_API_KEY")
    except RuntimeError as exc:
        return False, str(exc)

    context = market_context.compute_context(conn)
    context_block = _format_context_for_prompt(context)
    today = timeutil.today_eastern()

    system_prompt = f"""
You write the daily "Market Context" narrative for a personal trading
journal app. The user reads this every morning before trading - be
factual and specific, not generic. Use web search for anything you
don't already know for certain from today specifically (index levels,
sector moves, news) - these change daily and your training data is
stale for them.

Write the report in Markdown, in exactly this structure:

## Market Snapshot
S&P 500, Nasdaq Composite, Dow Jones Industrial Average, Russell 2000,
and QQQ: each one's closing level and % change for today (search for
these - do not estimate or use stale figures).

## Sector Performance
Which sectors led and lagged today. ALWAYS mention AI/semiconductor/
software/IT specifically, regardless of where they ranked - call it
out plainly even when it was flat or weak, not just when it led.

## Stock Highlights
Comment on the tracked tickers below using the IBD/Minervini framework
(21 EMA holds/breaks, distribution days, relative strength vs QQQ).
Use ONLY the numbers given to you in the "Tracked tickers" data below -
they're computed directly from real price data, not your own estimate.
Do not invent or restate a number you weren't given. You may web-search
for ticker-specific news to explain WHY a name moved, but the technical
numbers themselves come only from the data provided. Comment on both
individual names and the group collectively (e.g. how many are above
their 21 EMA, any distribution-day cluster).

## Market-Moving News
Fed/interest-rate news, geopolitical events, notable earnings
surprises, and any company-specific or sector-moving catalyst from
today - search for these.

## Next-Day Watch List
Which tracked tickers look like continuation candidates vs. which show
distribution warnings, based on the data provided. Label this section
clearly as pattern observations, not predictions - trading decisions
are the user's own.

Today's date is {today:%A, %B %d, %Y}. Keep the whole report readable
in a few minutes - specific and grounded, not padded.
""".strip()

    user_message = (
        "Write today's Market Context narrative.\n\n"
        "Tracked tickers (Shortlist + Open Positions), computed from real "
        "price data - use ONLY these numbers for anything you say about "
        "these specific tickers:\n\n"
        f"{context_block}"
    )

    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_WEB_SEARCHES}]
    request_kwargs = dict(
        model=MODEL, max_tokens=MAX_TOKENS, system=system_prompt, tools=tools,
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            messages=[{"role": "user", "content": user_message}], **request_kwargs)

        # pause_turn: the server-side web-search loop hit its own
        # iteration cap mid-turn - re-send the same messages plus the
        # paused assistant turn so it can pick up where it left off
        # (the documented pattern - NOT a new "continue" message). Capped
        # at a couple retries so a genuinely stuck request can't loop
        # forever paying for searches.
        attempts = 0
        while response.stop_reason == "pause_turn" and attempts < 2:
            response = client.messages.create(
                messages=[
                    {"role": "user", "content": user_message},
                    {"role": "assistant", "content": response.content},
                ],
                **request_kwargs,
            )
            attempts += 1
    except anthropic.AuthenticationError:
        return False, "The Anthropic API key was rejected - check ANTHROPIC_API_KEY in secrets.toml."
    except anthropic.RateLimitError:
        return False, "Anthropic API is rate-limiting requests right now - try Regenerate again in a minute."
    except anthropic.APIStatusError as exc:
        return False, f"Anthropic API error ({exc.status_code}): {exc.message}"
    except anthropic.APIConnectionError:
        return False, "Couldn't reach the Anthropic API - check your internet connection."

    if response.stop_reason == "refusal":
        return False, "Claude declined to generate today's narrative - use the paste-in box instead."

    narrative = "".join(block.text for block in response.content if block.type == "text").strip()
    if not narrative:
        return False, "Claude returned an empty response - try Regenerate, or use the paste-in box."

    return True, narrative
