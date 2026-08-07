"""
Session Keys
=====================
Named constants for every st.session_state key that's read/written
from more than one place - a typo in a hand-typed string (e.g.
"journal_seession") wouldn't raise an error, it would just silently
create a brand-new, disconnected session_state entry, breaking
whatever cross-rerun flow depended on the two spots agreeing. Using a
constant instead means a typo becomes a real NameError/AttributeError
at import time, not a silent bug found later by clicking around.

A handful of these (PENDING_TWEET's "session_key" field,
REVIEW_REPORT_PICKER as a widget key= as well as a session_state
entry) are shared between a page's own session_state usage and a
value stored INSIDE another dict - kept as the same constant rather
than two separate ones, since they're the same underlying concept
("which session is this") even though they're read back two different
ways.

Single-use keys (never read back anywhere but where they're set, or
dynamically built per-ticker like f"{key_prefix}_{symbol}_notes") are
deliberately NOT listed here - centralizing those wouldn't prevent any
real bug, just add a layer of indirection for no benefit.
"""

# Login gate (auth.py)
AUTHENTICATED = "authenticated"

# Shared between the Journal Session (pages/2_Shortlist.py) and Trade
# Review Session (pages/1_Trade_Analyzer.py) - see twitter_post.py's
# render_tweet_preview()/post_tweet(). JOURNAL_SESSION/REVIEW_SESSION
# below double as the "session_key" tag stored inside the pending_tweet
# dict itself, so a caller can tell which session a pending tweet
# belongs to.
PENDING_TWEET = "pending_tweet"

# Shortlist page - Journal Session
WATCHLIST_SELECTED = "watchlist_selected"
WATCHLIST_MESSAGE = "watchlist_message"
SCROLL_TO_SESSION_ANCHOR = "_scroll_to_session_anchor"
JOURNAL_SESSION = "journal_session"
JOURNAL_SELECTING = "journal_selecting"

# Trade Analyzer page - Trade Review Session
SCROLL_TO_REVIEW_ANCHOR = "_scroll_to_review_anchor"
REVIEW_SESSION = "review_session"
REVIEW_SELECTING = "review_selecting"
APPLIED_WEEK_CHOICE = "_applied_week_choice"
APPLIED_MONTH_CHOICE = "_applied_month_choice"
REVIEW_DATE_RANGE = "review_date_range"
REVIEW_PRECHECK = "_review_precheck"

# Open Positions page
SCROLL_TO_POSITIONS_ANCHOR = "_scroll_to_positions_anchor"

# Logbook page - also used as a widget key= (the report picker
# selectbox), not just a plain session_state entry - see this module's
# own docstring.
REVIEW_REPORT_PICKER = "review_report_picker"
REVIEW_REPORT_MESSAGE = "review_report_message"
