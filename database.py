"""
Database
=====================
This is the "source of truth" for your trade history. It used to be a
SQLite file (a database that lives in one file on this computer) - now
it's a hosted Postgres database (via Neon), so both this computer's
scripts AND the hosted dashboard can read/write the same data from
anywhere. Postgres is a database that runs on a server rather than
living in a single file - "hosted Postgres" just means someone else
(Neon, for free, on their small tier) runs that server for you.

WHERE THE CONNECTION INFO COMES FROM: `st.secrets["DATABASE_URL"]`.
This is Streamlit's built-in way of keeping secrets (passwords,
connection strings) OUT of your actual code - locally, it reads a
`.streamlit/secrets.toml` file (which is deliberately excluded from
Git via .gitignore, so it's never uploaded anywhere); once deployed,
Streamlit Community Cloud has its own secrets page you paste the same
value into. This works even in scripts like import_trades.py that
aren't run with `streamlit run` - `st.secrets` just reads the file, no
running app required.

WHAT'S STORED, IN THREE TABLES (a "table" is just a grid of rows and
columns, like a spreadsheet sheet, but one a database can search
through quickly):

  - `transactions`: one row per raw buy or sell, either from a
    Fidelity/Schwab CSV export or pulled automatically from SnapTrade
    (same normalized shape either way - see `load_transactions()` in
    analyze_trades.py and `fetch_activities()` in snaptrade_sync.py).
    Every import, from either source, only adds genuinely new rows -
    see `_insert_transactions()` below for how that's shared and kept
    duplicate-safe.

  - `trades`: one row per completed (matched buy+sell) trade - the
    result of running FIFO matching (see analyze_trades.py) over
    everything in `transactions`. Unlike `transactions`, this table is
    just wiped and recalculated fresh every time (see
    `rebuild_trades()`), since matching is quick and this way there's
    never a risk of it getting out of sync with `transactions`.

  - `logbook_entries`: one row per (symbol, calendar day) - the daily
    journal + archived chart image behind the Shortlist and Logbook
    pages. See `upsert_logbook_entry()` below for how the "still being
    written today" and "archived overnight" cases share one row.

  - `watchlist`: tickers you've manually added to track on the
    Shortlist page, independent of whether you actually hold a
    position in them. A ticker stays here (and keeps getting archived
    every night) until you remove it - see `add_to_watchlist()` /
    `remove_from_watchlist()` below.

  - `chart_preferences`: a single saved row remembering which moving
    averages you've added to the chart (periods + their colors), so
    they're still there the next time you open the app, on any device -
    see `get_chart_preferences()` / `save_chart_preferences()` below.
"""

import os
import time
from datetime import datetime, timedelta

import psycopg2
from psycopg2.extras import Json, execute_values
import streamlit as st

import snaptrade_sync
import timeutil
from analyze_trades import (
    detect_csv_source,
    load_transactions,
    load_transactions_schwab,
    match_trades_lifo,
)


def _get_database_url():
    try:
        return st.secrets["DATABASE_URL"]
    except Exception:
        pass

    # Falls back to a plain environment variable when st.secrets has
    # nothing to read from (e.g. .streamlit/secrets.toml doesn't exist) -
    # this is how nightly_archive.py gets its connection string when
    # GitHub Actions runs it, since there's no Streamlit secrets file
    # in that environment, just a repository secret set as an env var.
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url

    raise RuntimeError(
        "No DATABASE_URL found. Copy .streamlit/secrets.toml.example to "
        ".streamlit/secrets.toml and fill in your Neon connection string "
        "(or set a DATABASE_URL environment variable)."
    )


# Whether this process has already made sure every table/column exists.
# A page render opens several separate connections (one per section),
# and running the schema setup on every single one caused a real
# deadlock: an ALTER TABLE needs an exclusive lock on its table, and an
# earlier connection from the SAME page that had merely SELECTed from
# that table was still holding a read lock open - so the second
# connection waited forever. Running init_db() once per process (the
# first connection) avoids that entirely, and makes every later
# connection faster too.
_schema_ready = False


def get_connection():
    """Opens a connection to the hosted Postgres database - the first
    call in this process also makes sure every table exists."""
    global _schema_ready
    conn = _connect_with_retry()
    if not _schema_ready:
        init_db(conn)
        _schema_ready = True
    return conn


def _connect_with_retry():
    """
    Connects to Neon with a 10-second connect_timeout (instead of no
    timeout at all, which let an unreachable/very-slow-to-wake database
    hang the entire page rather than fail visibly) and one retry after
    a short pause. Neon's free tier suspends an idle database and takes
    a few seconds to wake back up on the next connection - the first
    attempt after a while away sometimes lands right in that window.
    A second, still-failing attempt is treated as a real problem (bad
    credentials, Neon actually down) and raises normally rather than
    retrying forever.
    """
    try:
        return psycopg2.connect(_get_database_url(), connect_timeout=10)
    except psycopg2.OperationalError:
        time.sleep(3)
        return psycopg2.connect(_get_database_url(), connect_timeout=10)


def _column_exists(cur, table, column):
    """Whether a column already exists - a plain read, used so the
    ALTER TABLE migrations below (which need an exclusive lock on their
    table) only actually run the one time they have real work to do."""
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


def _column_type(cur, table, column):
    """The Postgres data type of an existing column (e.g. "date",
    "timestamp without time zone") - same idea as _column_exists above,
    but for the ALTER COLUMN TYPE migrations below, which need to know
    what a column currently is rather than just whether it exists."""
    cur.execute(
        "SELECT data_type FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    row = cur.fetchone()
    return row[0] if row else None


def init_db(conn):
    """
    Creates the `transactions` and `trades` tables if they don't
    already exist. Safe to call every time the script runs - it only
    creates them the first time.
    """
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            date TIMESTAMP NOT NULL,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            price DOUBLE PRECISION NOT NULL,
            quantity DOUBLE PRECISION NOT NULL,
            UNIQUE (date, symbol, action, price, quantity)
        )
    """)
    # Which importer produced this row ("csv" or "snaptrade") - added
    # after `transactions` already existed, so CREATE TABLE IF NOT
    # EXISTS above won't retroactively add it (guarded like the other
    # migrations here). Existing rows stay NULL ("unknown origin") -
    # see _insert_transactions()'s is_duplicate() for why that's the
    # safe default rather than guessing.
    if not _column_exists(cur, "transactions", "source"):
        cur.execute("""
            ALTER TABLE transactions ADD COLUMN source TEXT
        """)
    # Widened from DATE to TIMESTAMP so a transaction pulled from
    # SnapTrade can keep its real execution time-of-day instead of
    # being flattened to midnight - see snaptrade_sync.fetch_
    # activities()'s own notes on where that time already comes from.
    # A DATE->TIMESTAMP widening is safe: every existing row just
    # becomes a midnight timestamp, the same moment it already meant.
    # Fidelity/Schwab CSV exports never had a time of day to begin
    # with (see analyze_trades.load_transactions()), so CSV-imported
    # rows stay at midnight regardless of this column's type.
    if _column_type(cur, "transactions", "date") == "date":
        cur.execute("ALTER TABLE transactions ALTER COLUMN date TYPE TIMESTAMP")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            entry_date TIMESTAMP NOT NULL,
            buy_price DOUBLE PRECISION NOT NULL,
            quantity DOUBLE PRECISION NOT NULL,
            exit_date TIMESTAMP NOT NULL,
            sell_price DOUBLE PRECISION NOT NULL,
            profit_loss DOUBLE PRECISION NOT NULL
        )
    """)
    # `trades` already existed before short-position support was added,
    # so CREATE TABLE IF NOT EXISTS above won't retroactively add this
    # column to it - this ADD COLUMN does. Existing rows all get 'LONG'
    # (correct, since every trade imported before this was long-only),
    # and get fully recalculated with real directions the next time
    # rebuild_trades() runs. Guarded by _column_exists so the exclusive
    # table lock ALTER needs is only ever taken the one time there's
    # real work to do.
    if not _column_exists(cur, "trades", "direction"):
        cur.execute("""
            ALTER TABLE trades ADD COLUMN direction TEXT NOT NULL DEFAULT 'LONG'
        """)
    # Same DATE->TIMESTAMP widening as transactions.date above, and for
    # the same reason - rebuild_trades() carries entry_date/exit_date
    # straight through from a transaction's own timestamp, so `trades`
    # needs to be able to hold real time-of-day too, not just flatten
    # it back to midnight on the way in.
    if _column_type(cur, "trades", "entry_date") == "date":
        cur.execute("ALTER TABLE trades ALTER COLUMN entry_date TYPE TIMESTAMP")
    if _column_type(cur, "trades", "exit_date") == "date":
        cur.execute("ALTER TABLE trades ALTER COLUMN exit_date TYPE TIMESTAMP")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS logbook_entries (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            entry_date DATE NOT NULL,
            notes TEXT,
            chart_image BYTEA,
            archived_at TIMESTAMP,
            UNIQUE (symbol, entry_date)
        )
    """)
    # A watchlist ticker's "trading plan for next time" - the entry/stop
    # prices typed into the Plan Entry/Plan Stop boxes on the Shortlist
    # page (see pages/2_Shortlist.py's render_journal_box()), NULL when
    # no plan's been set for that day. Only ever set for watchlist
    # tickers, never open positions - see render_journal_box()'s own
    # `on_watchlist` gating for why.
    if not _column_exists(cur, "logbook_entries", "plan_entry_price"):
        cur.execute("ALTER TABLE logbook_entries ADD COLUMN plan_entry_price DOUBLE PRECISION")
    if not _column_exists(cur, "logbook_entries", "plan_stop_price"):
        cur.execute("ALTER TABLE logbook_entries ADD COLUMN plan_stop_price DOUBLE PRECISION")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL UNIQUE,
            added_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    # `watchlist` predates having five separate lists, so CREATE TABLE
    # IF NOT EXISTS alone won't add this column to the existing table -
    # this does (guarded like the trades migration above). Existing
    # tickers land in list 1. A ticker still lives in exactly ONE list
    # (the UNIQUE symbol constraint above stays) - the journal/Logbook
    # is keyed per symbol per day, so the same ticker in two lists
    # would share one journal anyway and just get archived twice a
    # night.
    if not _column_exists(cur, "watchlist", "list_id"):
        cur.execute("""
            ALTER TABLE watchlist ADD COLUMN list_id INTEGER NOT NULL DEFAULT 1
        """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_names (
            list_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        )
    """)
    # Tracks WHEN a symbol was last removed from every watchlist -
    # `watchlist` itself only ever holds currently-tracked tickers (a
    # removal is a hard DELETE, no history kept there), so this is the
    # only record of "it's been gone for N days" that
    # symbol_archive_report.archive_and_delete_stale_watchlist_symbols()
    # needs. remove_from_watchlist() writes a row here; add_to_watchlist()
    # deletes it (re-adding resets the countdown, same as if it had
    # never been removed).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_removals (
            symbol TEXT PRIMARY KEY,
            removed_at TIMESTAMP NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chart_preferences (
            id INTEGER PRIMARY KEY DEFAULT 1,
            ma_periods TEXT NOT NULL DEFAULT '',
            ma_colors JSONB NOT NULL DEFAULT '{}',
            CONSTRAINT single_row CHECK (id = 1)
        )
    """)
    # `chart_preferences` predates the SMA/EMA toggle - existing rows all
    # get 'SMA' (correct, since a plain rolling average is what every
    # moving average on this app's charts was before this existed).
    if not _column_exists(cur, "chart_preferences", "ma_type"):
        cur.execute("ALTER TABLE chart_preferences ADD COLUMN ma_type TEXT NOT NULL DEFAULT 'SMA'")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS position_stops (
            symbol TEXT PRIMARY KEY,
            stop_loss DOUBLE PRECISION,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    # stop_loss used to be NOT NULL, back when a row only ever existed
    # for a symbol that had a real dollar stop set. Now a row can also
    # exist purely to hold this symbol's MA Stop Rule settings (see the
    # ma_* columns below) before any dollar stop has been set - DROP
    # NOT NULL is safe to run every time (a no-op once already
    # dropped), unlike ADD COLUMN, so no _column_exists guard is
    # needed here.
    cur.execute("ALTER TABLE position_stops ALTER COLUMN stop_loss DROP NOT NULL")
    # The MA Stop Rule (see ma_strategy.py): an open position can opt
    # into tracking itself against a moving average, either as an
    # informational signal ("manual") or as something that actively
    # trails the stop-loss up to the MA once it's cleared cost basis
    # by enough ("auto") - see pages/4_Open_Positions.py. Every ma_*
    # column except mode is nullable on purpose - NULL means "use the
    # global default from strategy_settings instead of a per-position
    # override" (see database.get_position_ma_settings()).
    if not _column_exists(cur, "position_stops", "ma_stop_mode"):
        cur.execute("ALTER TABLE position_stops ADD COLUMN ma_stop_mode TEXT NOT NULL DEFAULT 'off'")
    if not _column_exists(cur, "position_stops", "ma_period"):
        cur.execute("ALTER TABLE position_stops ADD COLUMN ma_period INTEGER")
    if not _column_exists(cur, "position_stops", "ma_closes_threshold"):
        cur.execute("ALTER TABLE position_stops ADD COLUMN ma_closes_threshold INTEGER")
    if not _column_exists(cur, "position_stops", "ma_unlock_pct"):
        cur.execute("ALTER TABLE position_stops ADD COLUMN ma_unlock_pct DOUBLE PRECISION")
    if not _column_exists(cur, "position_stops", "ma_approach_pct"):
        cur.execute("ALTER TABLE position_stops ADD COLUMN ma_approach_pct DOUBLE PRECISION")
    if not _column_exists(cur, "position_stops", "ma_extended_pct"):
        cur.execute("ALTER TABLE position_stops ADD COLUMN ma_extended_pct DOUBLE PRECISION")
    # An append-only log of every time set_stop_loss() below actually
    # sets a real stop - position_stops itself only ever holds the
    # CURRENT stop per symbol (overwritten on every edit, see
    # set_stop_loss()'s own note), so there was no way to answer "did
    # THIS closed trade ever have a stop set during its life" once the
    # position closed or a later trade reused the same symbol. Used by
    # goals.py's Stop-Loss Usage % goal - a trade only counts as
    # "protected" if a row here falls between its entry and exit date.
    # Starts empty, so only trades closed AFTER this table existed can
    # ever be scored - a known, accepted gap (nothing before this can
    # be retroactively known).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stop_loss_events (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            stop_loss DOUBLE PRECISION NOT NULL,
            set_at TIMESTAMP NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS account_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            account_value DOUBLE PRECISION,
            updated_at TIMESTAMP,
            CONSTRAINT single_row CHECK (id = 1)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_reports (
            report_date DATE PRIMARY KEY,
            generated_at TIMESTAMP NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deposits (
            id SERIAL PRIMARY KEY,
            deposit_date DATE NOT NULL,
            amount DOUBLE PRECISION NOT NULL
        )
    """)
    # One row per goal the user is actually tracking (see goals.py and
    # pages/6_Goals.py) - `goal_key` is a GOAL_LIBRARY key (e.g.
    # "win_rate"), not a foreign key into any table, since the library
    # itself lives in Python code, not the database. A goal_key/
    # timeframe no longer present in GOAL_LIBRARY (the code changed
    # since this row was added) is handled at read time, not enforced
    # here - see get_tracked_goals()'s own note.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tracked_goals (
            id SERIAL PRIMARY KEY,
            goal_key TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            warning_level DOUBLE PRECISION,
            alert_level DOUBLE PRECISION,
            created_at TIMESTAMP NOT NULL
        )
    """)
    # Replaced the old Target Value/Comparison (>=/<=/=) model with a
    # Warning Level/Alert Level pair (see goals.status_zone()) - a
    # three-way Good/Warning/Alert read instead of a binary Met/Not Met,
    # nullable until the Settings table's per-goal inputs are filled in.
    cur.execute("ALTER TABLE tracked_goals DROP COLUMN IF EXISTS target_value")
    cur.execute("ALTER TABLE tracked_goals DROP COLUMN IF EXISTS comparison")
    if not _column_exists(cur, "tracked_goals", "warning_level"):
        cur.execute("ALTER TABLE tracked_goals ADD COLUMN warning_level DOUBLE PRECISION")
    if not _column_exists(cur, "tracked_goals", "alert_level"):
        cur.execute("ALTER TABLE tracked_goals ADD COLUMN alert_level DOUBLE PRECISION")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chart_drawings (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            shape_type TEXT NOT NULL,
            x0 TEXT NOT NULL,
            y0 DOUBLE PRECISION NOT NULL,
            x1 TEXT,
            y1 DOUBLE PRECISION,
            color TEXT NOT NULL,
            width DOUBLE PRECISION NOT NULL,
            opacity DOUBLE PRECISION NOT NULL
        )
    """)
    # Global defaults for the MA Stop Rule (see ma_strategy.py) - one
    # row, edited on the Settings page. Any individual open position
    # can override any of these (see position_stops.ma_* columns
    # above); a position that hasn't customized a field just uses
    # whatever's saved here.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS strategy_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            ma_period INTEGER NOT NULL DEFAULT 21,
            closes_threshold INTEGER NOT NULL DEFAULT 2,
            unlock_pct DOUBLE PRECISION NOT NULL DEFAULT 5.0,
            approach_pct DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            extended_pct DOUBLE PRECISION NOT NULL DEFAULT 10.0,
            CONSTRAINT single_row CHECK (id = 1)
        )
    """)
    # Adjustable layout preferences - starting with how wide each
    # column of the Open Positions table is (see get_open_positions_
    # column_widths() below). One JSONB blob rather than a column per
    # setting, since this is just relative UI proportions, not
    # structured data anything else needs to query.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ui_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            open_positions_column_widths JSONB NOT NULL DEFAULT '{}',
            CONSTRAINT single_row CHECK (id = 1)
        )
    """)
    # Which of the Dashboard's chart sections to show - a JSON list of
    # section names (see get_dashboard_visible_sections() below). NULL
    # (nothing saved yet) means "show everything," so this stays a no-op
    # until the Dashboard's own Filters sidebar is actually used.
    if not _column_exists(cur, "ui_settings", "dashboard_visible_sections"):
        cur.execute("ALTER TABLE ui_settings ADD COLUMN dashboard_visible_sections JSONB")
    # A custom app-wide background image, uploaded on the Settings page
    # (see get_background_image()/save_background_image() below) and
    # applied via CSS injected in nav.render_top_nav() - which every
    # page calls - so this shows up everywhere, not just Settings.
    # `background_image_mime` (e.g. "image/png") is what was actually
    # uploaded, needed to build a correct data: URI - guessing wrong
    # here still often displays fine (browsers are lenient), but there's
    # no reason to guess when the upload already told us.
    if not _column_exists(cur, "ui_settings", "background_image"):
        cur.execute("ALTER TABLE ui_settings ADD COLUMN background_image BYTEA")
    if not _column_exists(cur, "ui_settings", "background_image_mime"):
        cur.execute("ALTER TABLE ui_settings ADD COLUMN background_image_mime TEXT")
    # A customizable accent color for the interactive chart's toolbar
    # (active tool highlight, hover tooltip border - see charting.py's
    # _component_theme_colors()), set on the Settings page. NULL (never
    # saved, or reset) means "use the app's built-in default," same
    # NULL-means-default convention as dashboard_visible_sections above.
    if not _column_exists(cur, "ui_settings", "accent_color"):
        cur.execute("ALTER TABLE ui_settings ADD COLUMN accent_color TEXT")
    # Customizable win/loss colors - the same NULL-means-default
    # convention as accent_color above, but for the green/red pair used
    # everywhere a stat tile, chart marker, or bar needs to show a gain
    # vs a loss (see charting.py's GOOD_COLOR/CRITICAL_COLOR/
    # win_loss_color()).
    if not _column_exists(cur, "ui_settings", "good_color"):
        cur.execute("ALTER TABLE ui_settings ADD COLUMN good_color TEXT")
    if not _column_exists(cur, "ui_settings", "critical_color"):
        cur.execute("ALTER TABLE ui_settings ADD COLUMN critical_color TEXT")
    # Custom text for the top nav bar's page buttons (see nav.py's
    # PAGES/render_top_nav()), keyed by each page's stable id - a page
    # missing from this dict just uses PAGES' own built-in default
    # label, the same JSONB-blob-of-named-values approach as
    # open_positions_column_widths above (there's no reason for seven
    # separate TEXT columns when this is really one related group of
    # settings).
    if not _column_exists(cur, "ui_settings", "nav_labels"):
        cur.execute("ALTER TABLE ui_settings ADD COLUMN nav_labels JSONB NOT NULL DEFAULT '{}'")
    # One general, not-tied-to-any-ticker journal entry per day - shown
    # as the first step of the guided Journal Session (see
    # pages/2_Shortlist.py's render_journal_session()) before it moves
    # into reviewing individual tickers, and again on the Daily Report's
    # cover page (see daily_report.py's build_report_pdf()) under
    # "Today's Thoughts."
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_journal_notes (
            entry_date DATE PRIMARY KEY,
            notes TEXT NOT NULL
        )
    """)
    # One written market-context narrative per day (index/sector/RS/news
    # summary plus watchlist-specific commentary) - see market_context.py
    # and narrative_generator.py. Auto-generated by the Market Context
    # page (pages/8_Market_Context.py) via the Claude API's web search
    # tool the first time that page loads with nothing saved for today,
    # with a manual paste-in box as a fallback for whenever the API call
    # fails or isn't configured. Same one-row-per-day, ON CONFLICT-upsert
    # shape as daily_journal_notes just above, for the same reason - a
    # day can be regenerated/overwritten (by auto-generation, a manual
    # Regenerate, or a hand-pasted override), never duplicated.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_narratives (
            entry_date DATE PRIMARY KEY,
            narrative_markdown TEXT NOT NULL,
            generated_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    # The guided Journal Session's progress (see pages/2_Shortlist.py's
    # render_journal_session()) - which tickers were in the queue and how
    # far you'd gotten - saved here (not just st.session_state) so
    # closing the tab, or Streamlit Cloud putting the app to sleep from
    # inactivity, doesn't lose your place partway through a session.
    # `queue` stores plain {"symbol", "source"} pairs, not the full
    # entry_point/position data - that's always re-derived fresh from
    # the live positions/watchlist on resume (see
    # pages/2_Shortlist.py's build_journal_queue()).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS journal_session_progress (
            id INTEGER PRIMARY KEY DEFAULT 1,
            queue JSONB NOT NULL,
            current_index INTEGER NOT NULL,
            started_at TIMESTAMP NOT NULL DEFAULT NOW(),
            CONSTRAINT single_row CHECK (id = 1)
        )
    """)
    # A persistent (not just st.cache_data - see charting.fetch_history())
    # cache of each tracked symbol's daily price history, one row per
    # symbol. Warmed shortly after market close by warm_price_cache.py
    # (a scheduled job, separate from and earlier than nightly_archive.py's
    # own near-midnight run - see that script's own docstring for why) and
    # immediately whenever a new ticker is added to a watchlist (see
    # pages/2_Shortlist.py) - so the FIRST time a ticker's chart is
    # actually opened each day, often during a Journal Session, doesn't
    # pay for a live Yahoo Finance fetch. `history` stores parallel
    # arrays (dates/open/high/low/close/volume), not a DataFrame directly -
    # trivial to rebuild one from, and keeps this a plain JSON blob.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_cache (
            symbol TEXT PRIMARY KEY,
            history JSONB NOT NULL,
            fetched_for_date DATE NOT NULL
        )
    """)
    # A persistent cache of each tracked symbol's next earnings date,
    # warmed by the same once-a-day job as price_cache above (see
    # warm_price_cache.py/charting.warm_earnings_cache_for_symbol()).
    # Feeds the Journal Session's "earnings within 10 days" badge (see
    # charting.build_figure()) with a plain date-math check instead of
    # a live Yahoo Finance call during the session itself.
    # `next_earnings_date` is NULL when Yahoo Finance has no known
    # upcoming date for the symbol - that correctly means "no badge"
    # downstream, not "not cached yet" (see get_cached_next_earnings_date()).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS earnings_cache (
            symbol TEXT PRIMARY KEY,
            next_earnings_date DATE,
            fetched_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    # A Trade Review Report: one batch of CLOSED trades reviewed together
    # in one guided session (see pages/1_Trade_Analyzer.py's Review
    # Session) - `range_start`/`range_end` are just the date-range filter
    # the session was started with, for display; `sent_at` mirrors
    # daily_reports.generated_at (see get_review_report_sent_status()/
    # mark_review_report_sent() below), NULL until the PDF's actually
    # been generated and emailed.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_review_reports (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            range_start DATE,
            range_end DATE,
            sent_at TIMESTAMP
        )
    """)
    # A journal entry before the first trade ("how do you feel about
    # this session, what are your predictions?") and one after the
    # last ("Reflections") - see render_review_session()'s intro/outro
    # screens. NULL means "not written yet", which is exactly the
    # signal that function uses to decide whether to show that prompt
    # at all (so resuming a session, or re-opening a finished report
    # on the Logbook page, doesn't ask again).
    if not _column_exists(cur, "trade_review_reports", "predictions_notes"):
        cur.execute("ALTER TABLE trade_review_reports ADD COLUMN predictions_notes TEXT")
    if not _column_exists(cur, "trade_review_reports", "reflections_notes"):
        cur.execute("ALTER TABLE trade_review_reports ADD COLUMN reflections_notes TEXT")
    # One reviewed trade within a report. Deliberately NOT a foreign key
    # into `trades.id` - rebuild_trades() (run after every import) does a
    # full DELETE + re-insert of that whole table, regenerating fresh
    # ids every time, so an id saved here today could point at a
    # completely different trade (or nothing) after the next import. The
    # natural fields below (symbol/entry_date/exit_date/direction) are
    # exactly what rebuild_trades() recomputes identically from the same
    # transaction history, so they stay valid across a rebuild the same
    # way logbook_entries staying keyed by (symbol, entry_date) does.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_reviews (
            id SERIAL PRIMARY KEY,
            report_id INTEGER NOT NULL REFERENCES trade_review_reports(id) ON DELETE CASCADE,
            symbol TEXT NOT NULL,
            entry_date DATE NOT NULL,
            exit_date DATE NOT NULL,
            direction TEXT NOT NULL,
            notes TEXT,
            reviewed_at TIMESTAMP NOT NULL
        )
    """)
    # The trade's own numbers, saved alongside the review rather than
    # re-looked-up later - lets the Trade Review Report PDF and the
    # Logbook's Trade Reviews section show entry/exit price, shares,
    # P/L, and days held (see analyze_trades.trade_stats()) without
    # depending on database.get_trades() still having a matching row
    # later (rebuild_trades() could have changed things since).
    if not _column_exists(cur, "trade_reviews", "buy_price"):
        cur.execute("ALTER TABLE trade_reviews ADD COLUMN buy_price DOUBLE PRECISION")
    if not _column_exists(cur, "trade_reviews", "sell_price"):
        cur.execute("ALTER TABLE trade_reviews ADD COLUMN sell_price DOUBLE PRECISION")
    if not _column_exists(cur, "trade_reviews", "quantity"):
        cur.execute("ALTER TABLE trade_reviews ADD COLUMN quantity DOUBLE PRECISION")
    if not _column_exists(cur, "trade_reviews", "profit_loss"):
        cur.execute("ALTER TABLE trade_reviews ADD COLUMN profit_loss DOUBLE PRECISION")
    # A chart snapshot captured during a trade's review, at whichever
    # timeframe was on screen when "Save this timeframe" was clicked -
    # one trade can have several (Hourly, Daily, Weekly, ...), which is
    # why this is its own one-row-per-image table rather than a single
    # column, the same "many rows per parent" shape chart_drawings
    # already uses instead of a JSON blob.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_review_snapshots (
            id SERIAL PRIMARY KEY,
            review_id INTEGER NOT NULL REFERENCES trade_reviews(id) ON DELETE CASCADE,
            timeframe TEXT NOT NULL,
            chart_image BYTEA NOT NULL
        )
    """)
    # The guided Review Session's progress - same idea and shape as
    # journal_session_progress above, just for a queue of closed trades
    # instead of positions/watchlist tickers. `queue` stores each
    # trade's natural-key fields (symbol/entry_date/exit_date/direction),
    # for the same rebuild_trades()-stability reason trade_reviews above
    # doesn't use a trades.id foreign key.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_review_session_progress (
            id INTEGER PRIMARY KEY DEFAULT 1,
            report_id INTEGER NOT NULL,
            queue JSONB NOT NULL,
            current_index INTEGER NOT NULL,
            CONSTRAINT single_row CHECK (id = 1)
        )
    """)
    # The Screener page's signal log (see screener/engine.py and
    # pages/7_Screener.py) - one row per (signal_date, ticker), for
    # EVERY baseline-passing candidate, not just the strict ones shown
    # by default or the ones the user actually acted on. That's the
    # whole point of logging it at all: reviewing later whether the
    # strict filter is hiding winners the baseline tier would have
    # caught, and whether the user's own skips are helping or costing
    # money - neither question is answerable from a log that only
    # keeps what was shown/taken. `trigger_price`/`close_price` (not
    # `trigger`/`close`) sidestep `trigger` being a reserved SQL
    # keyword and `close` reading oddly unquoted.
    #
    # triggered/trigger_date/outcome_*/exit_reason/reconciled_at start
    # NULL and are filled in later by screener.reconcile - see that
    # module's own docstring for the three exit conditions it
    # simulates. user_action/user_note are the only fields this app
    # itself ever sets directly on an already-logged row (see
    # update_signal_user_action() below) - everything else about a
    # signal is immutable once logged, same "append-only" idea
    # daily_screen.py's own append_log() already uses for its CSV.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signal_log (
            id SERIAL PRIMARY KEY,
            signal_date DATE NOT NULL,
            ticker TEXT NOT NULL,
            tier TEXT NOT NULL,
            trigger_price DOUBLE PRECISION NOT NULL,
            stop_price DOUBLE PRECISION NOT NULL,
            risk_pct DOUBLE PRECISION NOT NULL,
            shares INTEGER NOT NULL,
            position_usd DOUBLE PRECISION NOT NULL,
            risk_usd DOUBLE PRECISION NOT NULL,
            adr DOUBLE PRECISION NOT NULL,
            rs_excess DOUBLE PRECISION NOT NULL,
            rsline_at_high BOOLEAN NOT NULL,
            nr7_2 BOOLEAN NOT NULL,
            pct_off_high DOUBLE PRECISION NOT NULL,
            close_price DOUBLE PRECISION NOT NULL,
            gate_pct DOUBLE PRECISION,
            expires DATE NOT NULL,
            triggered BOOLEAN,
            trigger_date DATE,
            user_action TEXT,
            user_note TEXT,
            outcome_return DOUBLE PRECISION,
            outcome_r DOUBLE PRECISION,
            outcome_bars INTEGER,
            exit_reason TEXT,
            reconciled_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE (signal_date, ticker)
        )
    """)
    conn.commit()


def _insert_transactions(conn, transactions):
    """
    Adds any of these normalized transaction dicts ({"date", "symbol",
    "action", "price", "quantity", "source"} - see analyze_trades.load_
    transactions()) that aren't already stored. Shared by every import
    path (CSV upload, SnapTrade sync) so there's exactly one place
    that knows how a transaction gets deduplicated and inserted.

    A row gets skipped as a duplicate two ways:
      1. The `UNIQUE` constraint on the transactions table (set up in
         init_db above) catches an EXACT match on date/symbol/action/
         price/quantity, via "ON CONFLICT DO NOTHING" below.
      2. The SELECT check just above that: SnapTrade's API reports MORE
         decimal precision than a plain Fidelity/Schwab CSV export does
         for the SAME real fill - e.g. "239.63" from a CSV vs
         "239.6296" from SnapTrade - close enough to defeat #1's exact
         match, and would otherwise double-count that trade (inflating
         a position's share count) every time it shows up from a
         second source. So a row is ALSO treated as a duplicate when an
         existing row involving SnapTrade on either side already
         matches on date/symbol/action/quantity and its price is within
         a cent (or 0.2%, whichever is bigger, for pricier stocks).

    That fuzzy price check in #2 is deliberately narrow - SnapTrade
    specifically, not just "any two different sources" - see
    same_fill() below. Two broader (and both wrong) versions of this
    check have each caused REAL data loss in production, in opposite
    directions:
      - Too broad (comparing ANY mismatched sources, including a
        legacy None-source row against a plain "csv" one): two
        GENUINELY SEPARATE fills from the same multi-part order (e.g. a
        750-share sell that filled in 4 pieces, two of them
        coincidentally both 100 shares a few cents apart) got merged -
        a fresh CSV re-import of a real missing fill was silently
        rejected as "already have this one," permanently losing that
        fill's own P/L (confirmed for real: CON, a 100-share fill at
        $31.96 dropped as a false match against an unrelated 100-share
        fill at $31.98 from the same order).
      - Too narrow (requiring BOTH sources to be truthy/known before
        allowing the tolerance at all): a legacy pre-source-column row
        (source=None) re-synced later via SnapTrade never counted as
        "different enough" to get the tolerance, so 46 real SnapTrade-
        vs-legacy duplicates went uncaught and inflated open positions
        (see this project's own commit history around both fixes).
      - Also too narrow, in a different way (fixed alongside): requiring
        a bit-for-bit exact price match for the non-SnapTrade case
        above rejected a legacy row's occasional extra digit of raw
        precision (e.g. "1250.775") against a fresh CSV re-import's
        cleanly-rounded cent price ("1250.78") for the SAME real fill -
        confirmed for real (SNDK, AMRX), each inflating a phantom open
        position by the fill's full share count. Now compared at cent
        precision (round to 2 decimals) instead of exact equality.
      - A fourth gap, same root cause as the SnapTrade-precision one
        above: SnapTrade's synced action is always plain "BUY"/"SELL",
        never "SELL_SHORT" (its feed doesn't expose that distinction
        for this account - see snaptrade_sync.py), so a real short sale
        already correctly recorded as SELL_SHORT (from a CSV import or
        legacy row) got a mislabeled SnapTrade "SELL" duplicate that
        same_fill()'s exact-action requirement couldn't recognize as
        the same fill. Confirmed for real (NBIS: 4 duplicated rows).
        Now allowed to match across that specific action pair, but only
        when SnapTrade is involved - see same_fill() below.
    The actual right line: the tolerance is ONLY ever needed because of
    SnapTrade's own extra precision, so it should only fire when
    SnapTrade is actually one of the two sources being compared - a CSV
    import and a legacy (None) row are BOTH CSV-precision data, so two
    of those reporting the same real fill would carry the same price to
    the cent, even if a stray extra decimal digit differs.

    Since a CSV export always contains your FULL history, and a
    SnapTrade sync re-fetches a recent overlapping window every time
    (see snaptrade_sync.py), both of these together are what let
    either source run repeatedly, or alongside the other, without
    creating duplicates.

    Does the fuzzy dedup check (#2 above) once in Python against every
    existing row, instead of one SELECT per candidate row, then adds
    every genuinely-new row in a single batched INSERT - a full-history
    CSV or a year-wide SnapTrade sync used to mean two round-trips to
    Neon PER transaction; this is two round-trips total. The exact-match
    UNIQUE constraint (#1 above) is kept as an "ON CONFLICT DO NOTHING"
    backstop on the batched insert, for the unlikely case something else
    wrote the same exact row between this function's read and write.

    Returns how many new rows were actually added.
    """
    cur = conn.cursor()
    cur.execute("SELECT date, symbol, action, price, quantity, source FROM transactions")
    existing = [
        {"date": row[0], "symbol": row[1], "action": row[2], "price": row[3],
         "quantity": row[4], "source": row[5]}
        for row in cur.fetchall()
    ]

    def as_date(d):
        # `existing` (straight from Postgres) holds plain `date`
        # objects; `t`/`to_insert` (straight from the CSV/SnapTrade
        # loaders) hold `datetime`s - a datetime and a date on the same
        # calendar day compare UNEQUAL with plain `==`, so both sides
        # have to be normalized the same way before comparing.
        return d.date() if isinstance(d, datetime) else d

    def same_fill(t, e):
        t_date = as_date(t["date"])
        if as_date(e["date"]) != t_date or e["symbol"] != t["symbol"] or e["quantity"] != t["quantity"]:
            return False

        involves_snaptrade = "snaptrade" in (t["source"], e["source"])

        if e["action"] != t["action"]:
            # SnapTrade's synced action is always plain "BUY"/"SELL" -
            # it has no way to tell us a sell was actually a short-sale
            # open (see snaptrade_sync.py's fetch_activities() docstring
            # for why - this account's feed doesn't expose that). A CSV
            # import or legacy row DOES know this (see analyze_trades.
            # load_transactions()'s "SOLD SHORT SALE" detection), so a
            # SnapTrade "SELL" landing on the same date/symbol/quantity/
            # price as an existing "SELL_SHORT" from one of those is the
            # SAME real short sale, just mislabeled - not a separate
            # transaction. Confirmed for real (NBIS): 4 SnapTrade rows
            # duplicated 4 already-correct SELL_SHORT rows this way.
            # Restricted to SnapTrade specifically (not any two
            # mismatched actions) so a genuinely different real SELL and
            # SELL_SHORT that happen to share date/symbol/quantity/price
            # between two CSV/legacy rows still stay separate.
            if not (involves_snaptrade and {t["action"], e["action"]} == {"SELL", "SELL_SHORT"}):
                return False

        # The fuzzy price tolerance below exists for exactly ONE reason:
        # SnapTrade's API reports more decimal precision than a plain
        # Fidelity/Schwab CSV export does for the SAME real execution
        # (e.g. "239.6296" vs a CSV's "239.63") - see this function's
        # own module docstring. A CSV import and a legacy pre-source-
        # column row are BOTH CSV-precision data (the legacy rows ARE
        # old CSV imports, just from before this column existed), so
        # two of THOSE reporting the same real fill would carry the
        # identical price, not just a close one. Only reach for the
        # tolerance when SnapTrade is actually one of the two sources -
        # confirmed the hard way: treating ANY source mismatch
        # (including None vs "csv") as "maybe the same fill, allow a
        # few cents" wrongly merged two GENUINELY SEPARATE fills from a
        # multi-part order (a 750-share sell that filled in 4 pieces,
        # two of them coincidentally both 100 shares a few cents apart)
        # - a fresh CSV re-import of the missing 4th fill got silently
        # rejected as "already have this one," permanently losing that
        # fill's own P/L, because it happened to land within a few
        # cents of an unrelated EXISTING 100-share fill from the same
        # order.
        if t["source"] == e["source"] or not involves_snaptrade:
            # Not a bit-for-bit comparison: a legacy (source=None) row
            # sometimes carries an extra digit or two of raw precision
            # (e.g. an averaged fill price like "1250.775" or "17.9327")
            # that a fresh CSV export - always quoted to the cent -
            # rounds cleanly ("1250.78", "17.93"). Confirmed for real
            # (SNDK, AMRX): the SAME 100-share fill, reported once as a
            # legacy row and once via a fresh CSV re-import, failed this
            # exact-match check by less than a cent and got double-
            # counted as a phantom open position. Comparing at cent
            # precision (what a broker actually quotes prices at) still
            # keeps genuinely separate fills apart - the CON case this
            # exact-match branch exists to protect (two real fills at
            # $31.96 and $31.98) differ by a whole 2 cents, which
            # survives rounding either way.
            return round(e["price"], 2) == round(t["price"], 2)

        return abs(e["price"] - t["price"]) <= max(0.01, e["price"] * 0.002)

    def is_duplicate(t, others):
        return any(same_fill(t, e) for e in others)

    to_insert = []
    for t in transactions:
        # Checked against `existing` AND every row already accepted
        # earlier in THIS SAME batch - a CSV/sync response can itself
        # contain near-duplicates of a row it also contains, and the
        # original per-row loop caught that too (each INSERT was
        # visible to the next row's SELECT within the same open,
        # not-yet-committed transaction).
        if is_duplicate(t, existing) or is_duplicate(t, to_insert):
            continue
        to_insert.append(t)

    if not to_insert:
        return 0

    inserted = execute_values(
        cur,
        """
        INSERT INTO transactions (date, symbol, action, price, quantity, source)
        VALUES %s
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        # The full datetime, not t["date"].date() - transactions.date is
        # a TIMESTAMP now (see init_db()'s migration note) specifically
        # so a SnapTrade-sourced row's real execution time-of-day
        # survives being stored, not just its calendar date.
        [(t["date"], t["symbol"], t["action"], t["price"], t["quantity"], t["source"])
         for t in to_insert],
        fetch=True,
    )
    conn.commit()
    return len(inserted)


def import_transactions(conn, csv_path):
    """
    Reads every buy/sell (and short-sale) row out of the CSV and adds
    any that aren't already stored. Auto-detects whether the file is a
    Fidelity or Schwab export (see analyze_trades.detect_csv_source())
    and calls the matching parser - you never have to say which one it
    is, just drop the file in.

    Returns how many new rows were actually added.
    """
    source = detect_csv_source(csv_path)
    transactions = load_transactions_schwab(csv_path) if source == "schwab" else load_transactions(csv_path)
    return _insert_transactions(conn, transactions)


def import_transactions_snaptrade(conn, start_date, end_date):
    """
    Pulls activity from your connected Fidelity account (via
    snaptrade_sync.py) for the given date range and adds any
    transactions that aren't already stored - the SnapTrade
    equivalent of import_transactions() above, sharing the same
    dedup-safe insert. See pages/5_Settings.py (manual "Sync
    Now") and snaptrade_daily_sync.py (the automatic daily version)
    for where this gets called from.

    Returns how many new rows were actually added.
    """
    transactions = snaptrade_sync.fetch_activities(start_date, end_date)
    return _insert_transactions(conn, transactions)


def clear_trade_cache():
    """
    Clears the cached get_trades()/get_open_positions() reads (see
    their own docstrings for why they're cached). Called right after
    rebuild_trades() below so the current session sees newly-imported
    trades immediately instead of waiting out the cache's TTL - the
    same clear-on-write pattern nav.clear_background_cache() already
    uses for the background image cache.
    """
    get_trades.clear()
    get_open_positions.clear()


def rebuild_trades(conn):
    """
    Recalculates the entire `trades` table from scratch, using every
    row currently in `transactions`.

    This reuses match_trades_lifo() from analyze_trades.py (the same
    LIFO buy/sell matching logic the other scripts use), so a "trade"
    here means exactly the same thing everywhere else in this project.
    Options contracts (tickers starting with "-") are left out, since
    this tracker is for stock trades only.

    We don't try to only add "new" trades here - we just clear the
    table and write the fresh results every time. That's simpler than
    figuring out which trades changed, and since this table is derived
    entirely from `transactions` (which IS carefully deduplicated),
    the result is always correct.

    Returns how many trades were computed.
    """
    cur = conn.cursor()
    cur.execute("SELECT date, symbol, action, price, quantity FROM transactions")
    # `date` is a real datetime already (see init_db()'s DATE->TIMESTAMP
    # migration note) - no need to reconstruct one the way this used to
    # when the column only ever held a plain date, since that would
    # silently flatten a SnapTrade-sourced row's real time-of-day back
    # to midnight right before match_trades_lifo() ever saw it.
    transactions = [
        {
            "date": row[0],
            "symbol": row[1],
            "action": row[2],
            "price": row[3],
            "quantity": row[4],
        }
        for row in cur.fetchall()
    ]

    closed_trades, _open_long_lots, _open_short_lots = match_trades_lifo(transactions)
    stock_trades = [t for t in closed_trades if not t["symbol"].strip().startswith("-")]

    cur.execute("DELETE FROM trades")
    cur.executemany(
        """
        INSERT INTO trades (symbol, entry_date, buy_price, quantity, exit_date, sell_price, profit_loss, direction)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                t["symbol"],
                t["entry_date"],
                t["buy_price"],
                t["quantity"],
                t["date"],
                t["sell_price"],
                t["profit_loss"],
                t["direction"],
            )
            for t in stock_trades
        ],
    )
    conn.commit()
    clear_trade_cache()

    return len(stock_trades)


@st.cache_data(ttl=60, show_spinner=False)
def get_trades(_conn):
    """
    Returns every completed trade from the `trades` table, oldest
    first, in the same dictionary shape build_trade_tracker.py and
    dashboard.py already expect.

    Cached (see clear_trade_cache()) - this and get_open_positions()
    below were the two most expensive reads in the app, each re-run on
    every single Streamlit rerun (any widget click, not just an actual
    data change) by several pages at once. `_conn` (leading underscore)
    tells st.cache_data to key the cache on nothing but the function's
    other arguments - there are none here, so effectively one shared
    cache entry for the whole process - rather than trying to hash the
    connection object itself, which it can't do anyway.
    """
    cur = _conn.cursor()
    cur.execute("""
        SELECT symbol, entry_date, buy_price, quantity, exit_date, sell_price, profit_loss, direction
        FROM trades
        ORDER BY entry_date
    """)

    # entry_date/exit_date come back as real datetimes already (see
    # init_db()'s DATE->TIMESTAMP migration note) - reconstructing them
    # via datetime.combine(row, datetime.min.time()) the way this used
    # to would silently flatten a SnapTrade-sourced trade's real
    # execution time back to midnight on every single read.
    return [
        {
            "symbol": row[0],
            "entry_date": row[1],
            "buy_price": row[2],
            "quantity": row[3],
            "date": row[4],
            "sell_price": row[5],
            "profit_loss": row[6],
            "direction": row[7],
        }
        for row in cur.fetchall()
    ]


def _aggregate_open_lots(open_lots, direction):
    """
    Turns match_trades_lifo()'s per-symbol list of still-open lots into
    one row per symbol (total shares, a quantity-weighted average
    price, and the earliest entry date among them), tagged with
    `direction` ("LONG" or "SHORT") - shared by get_open_positions()
    for both its long and short lots, since the aggregation math is
    identical for either.
    """
    positions = []
    for symbol, lots in open_lots.items():
        if not lots or symbol.strip().startswith("-"):
            continue  # no open shares, or an options contract (not tracked here)
        total_quantity = sum(lot["quantity"] for lot in lots)
        total_cost = sum(lot["quantity"] * lot["price"] for lot in lots)
        positions.append({
            "symbol": symbol,
            "direction": direction,
            "quantity": total_quantity,
            "avg_price": total_cost / total_quantity,
            "entry_date": min(lot["date"] for lot in lots),
        })
    return positions


@st.cache_data(ttl=60, show_spinner=False)
def get_open_positions(_conn):
    """
    Returns currently-open positions (bought but not yet sold, or sold
    short but not yet covered), computed fresh from match_trades_lifo()'s
    open lots - the other side of the same FIFO matching that produces
    `trades`. This is derived data, not something separately tracked, so
    it's always consistent with whatever is currently in `transactions` -
    no separate "position opened" event needs to be recorded anywhere.

    One row per symbol per direction (see _aggregate_open_lots() above) -
    a symbol could in principle appear twice, once "LONG" and once
    "SHORT", if it somehow has both open at once; they aren't netted
    against each other. Sorted oldest entry first.

    Cached (see get_trades() above for why, and clear_trade_cache()
    below for how it's kept fresh after an import).
    """
    cur = _conn.cursor()
    cur.execute("SELECT date, symbol, action, price, quantity FROM transactions")
    # `date` is a real datetime already - see rebuild_trades()'s own
    # note on this same pattern for why no datetime.combine() is needed
    # (or wanted) here anymore.
    transactions = [
        {
            "date": row[0],
            "symbol": row[1],
            "action": row[2],
            "price": row[3],
            "quantity": row[4],
        }
        for row in cur.fetchall()
    ]

    _closed_trades, open_long_lots, open_short_lots = match_trades_lifo(transactions)

    positions = (
        _aggregate_open_lots(open_long_lots, "LONG")
        + _aggregate_open_lots(open_short_lots, "SHORT")
    )

    return sorted(positions, key=lambda p: p["entry_date"])


def upsert_logbook_entry(conn, symbol, entry_date, notes=None, chart_image=None, archived_at=None):
    """
    Adds or updates one day's logbook row for a symbol. Only the fields
    actually passed in get overwritten - COALESCE keeps whatever was
    already stored for anything left as None - so the daytime "save my
    journal notes" action (from the Shortlist page) and the nightly
    "save the chart image" action (from nightly_archive.py) can both call
    this without erasing each other's field.
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO logbook_entries (symbol, entry_date, notes, chart_image, archived_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (symbol, entry_date) DO UPDATE SET
            notes = COALESCE(EXCLUDED.notes, logbook_entries.notes),
            chart_image = COALESCE(EXCLUDED.chart_image, logbook_entries.chart_image),
            archived_at = COALESCE(EXCLUDED.archived_at, logbook_entries.archived_at)
        """,
        (symbol, entry_date, notes, chart_image, archived_at),
    )
    conn.commit()


def save_journal_plan(conn, symbol, entry_date, plan_entry_price, plan_stop_price):
    """
    Saves (or clears) a watchlist ticker's trading plan for one day -
    unlike upsert_logbook_entry()'s notes/chart_image/archived_at, this
    always OVERWRITES both fields with exactly what's passed, including
    None - the same "no COALESCE" convention position_stops already
    uses (see set_stop_loss()/delete_stop_loss()), since the Plan
    Entry/Plan Stop boxes being left blank on a later save means "clear
    this plan," not "leave whatever was there before." The journal
    form's own Save action is the only caller. Creates the day's
    logbook_entries row if it doesn't exist yet - a plan can be the
    first thing saved for a ticker today, before any notes.
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO logbook_entries (symbol, entry_date, plan_entry_price, plan_stop_price)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (symbol, entry_date) DO UPDATE SET
            plan_entry_price = EXCLUDED.plan_entry_price,
            plan_stop_price = EXCLUDED.plan_stop_price
        """,
        (symbol, entry_date, plan_entry_price, plan_stop_price),
    )
    conn.commit()


def get_logbook_entry(conn, symbol, entry_date):
    """Returns one day's logbook row for a symbol, or None if it doesn't exist yet."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT notes, chart_image, archived_at, plan_entry_price, plan_stop_price
        FROM logbook_entries WHERE symbol = %s AND entry_date = %s
        """,
        (symbol, entry_date),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "notes": row[0],
        "chart_image": bytes(row[1]) if row[1] is not None else None,
        "archived_at": row[2],
        "plan_entry_price": row[3],
        "plan_stop_price": row[4],
    }


def get_logbook_entries_for_symbol(conn, symbol):
    """
    Returns every logbook row ever recorded for `symbol`, oldest first,
    as a list of {entry_date, notes, chart_image, archived_at,
    plan_entry_price, plan_stop_price} dicts - the "one symbol, every
    date" counterpart to get_logbook_entries_for_date()'s "one date,
    every symbol". Used by symbol_archive_report.py to build one PDF
    covering a watchlist-only ticker's whole journal history before
    it's deleted.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT entry_date, notes, chart_image, archived_at, plan_entry_price, plan_stop_price
        FROM logbook_entries WHERE symbol = %s ORDER BY entry_date
        """,
        (symbol,),
    )
    return [
        {
            "entry_date": row[0],
            "notes": row[1],
            "chart_image": bytes(row[2]) if row[2] is not None else None,
            "archived_at": row[3],
            "plan_entry_price": row[4],
            "plan_stop_price": row[5],
        }
        for row in cur.fetchall()
    ]


def has_trade_history(conn, symbol):
    """
    Whether `symbol` appears anywhere in the `trades` table (an open OR
    closed position, ever) - symbol_archive_report.py's line between
    "this was just a watchlist idea that never went anywhere" (eligible
    for auto-archive-and-delete once removed long enough) and "this was
    a real trade" (Logbook kept forever, regardless of watchlist
    status). Checking `trades` rather than get_open_positions() also
    catches a symbol that WAS traded and has since fully closed out -
    still real trade history, not just a watchlist idea.
    """
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM trades WHERE symbol = %s LIMIT 1", (symbol,))
    return cur.fetchone() is not None


def get_stale_watchlist_removals(conn, older_than_days):
    """
    Returns the symbols in watchlist_removals whose removed_at is more
    than `older_than_days` days ago - candidates for
    symbol_archive_report.py's auto-archive-and-delete (still subject
    to that function's own has_trade_history() exemption check on top
    of this).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT symbol FROM watchlist_removals WHERE removed_at < %s",
        (timeutil.now_eastern() - timedelta(days=older_than_days),),
    )
    return [row[0] for row in cur.fetchall()]


def delete_symbol_logbook_history(conn, symbol):
    """
    Permanently deletes EVERYTHING keyed by `symbol` except `trades`/
    `transactions` (real trade history is never touched by this) -
    logbook_entries (notes + chart images), chart_drawings,
    position_stops, price_cache, and its own watchlist_removals
    tracking row. Called by symbol_archive_report.py only AFTER the
    archive PDF has been successfully emailed - never before, since
    this has no undo.
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM logbook_entries WHERE symbol = %s", (symbol,))
    cur.execute("DELETE FROM chart_drawings WHERE symbol = %s", (symbol,))
    cur.execute("DELETE FROM position_stops WHERE symbol = %s", (symbol,))
    cur.execute("DELETE FROM price_cache WHERE symbol = %s", (symbol,))
    cur.execute("DELETE FROM watchlist_removals WHERE symbol = %s", (symbol,))
    conn.commit()


def has_logbook_chart_image(conn, symbol, entry_date):
    """
    Whether a symbol's logbook row for entry_date already has a chart
    image saved - a lightweight existence check (no chart_image BYTEA
    pulled over the wire, unlike get_logbook_entry()) for
    archiving.archive_all()'s skip_if_already_archived option, which
    calls this once per ticker on every nightly run.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT chart_image IS NOT NULL FROM logbook_entries WHERE symbol = %s AND entry_date = %s",
        (symbol, entry_date),
    )
    row = cur.fetchone()
    return row is not None and row[0]


def get_logbook_entries_for_date(conn, entry_date):
    """
    Returns every symbol's logbook row for `entry_date` as {symbol: {...}}
    (same per-entry shape as get_logbook_entry()) - one query instead of
    one per symbol. Used by daily_report.build_report_pdf(), which used
    to call get_logbook_entry() once per watchlist ticker plus once per
    open position (a real N+1 query pattern on both the "Generate & Email
    Report" button and the nightly report fallback). A symbol with
    nothing archived for this date simply won't be a key here - look it
    up with `.get(symbol)`, same None-if-missing behavior as before.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT symbol, notes, chart_image, archived_at, plan_entry_price, plan_stop_price
        FROM logbook_entries WHERE entry_date = %s
        """,
        (entry_date,),
    )
    return {
        row[0]: {
            "notes": row[1],
            "chart_image": bytes(row[2]) if row[2] is not None else None,
            "archived_at": row[3],
            "plan_entry_price": row[4],
            "plan_stop_price": row[5],
        }
        for row in cur.fetchall()
    }


def get_daily_journal_note(conn, entry_date):
    """
    Returns the general "Today's Thoughts" journal entry for `entry_date`
    (see daily_journal_notes' own docstring in init_db()), or None if
    nothing's been written for that day yet. An empty string (submitted
    on purpose, with nothing typed) is NOT the same as None - it still
    counts as "handled today," so render_journal_session() won't prompt
    for it again for the rest of that day.
    """
    cur = conn.cursor()
    cur.execute("SELECT notes FROM daily_journal_notes WHERE entry_date = %s", (entry_date,))
    row = cur.fetchone()
    return row[0] if row else None


def save_daily_journal_note(conn, entry_date, notes):
    """Saves (or overwrites) the general journal entry for `entry_date` - see get_daily_journal_note()."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO daily_journal_notes (entry_date, notes)
        VALUES (%s, %s)
        ON CONFLICT (entry_date) DO UPDATE SET notes = EXCLUDED.notes
        """,
        (entry_date, notes),
    )
    conn.commit()


def get_market_narrative(conn, entry_date):
    """
    Returns the saved Market Context narrative for `entry_date` as
    {"narrative_markdown": str, "generated_at": datetime}, or None if
    nothing's been written for that day yet (e.g. it's a fresh morning
    and today's page load hasn't triggered auto-generation yet, or
    entry_date is a day nobody ever generated/pasted one for - see
    market_narratives' own docstring in init_db()).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT narrative_markdown, generated_at FROM market_narratives WHERE entry_date = %s",
        (entry_date,),
    )
    row = cur.fetchone()
    return {"narrative_markdown": row[0], "generated_at": row[1]} if row else None


def save_market_narrative(conn, entry_date, narrative_markdown):
    """Saves (or overwrites) the Market Context narrative for
    `entry_date` - see get_market_narrative(). Called from
    pages/8_Market_Context.py, both after narrative_generator.py builds
    one automatically and when the manual paste-in box's Save button is
    used - one plain, documented function for either path rather than
    each needing to know the table's raw column names."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO market_narratives (entry_date, narrative_markdown, generated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (entry_date) DO UPDATE SET
            narrative_markdown = EXCLUDED.narrative_markdown,
            generated_at = NOW()
        """,
        (entry_date, narrative_markdown),
    )
    conn.commit()


def get_journal_note_dates(conn, start_date, end_date):
    """Every date in [start_date, end_date] (inclusive) that has a
    daily_journal_notes row, as a set of dates - used by goals.py's
    Daily Journal % goal, which only needs to know WHICH days were
    journaled, not the note text itself. A row existing at all counts,
    even an empty string (see get_daily_journal_note()'s own note on
    why)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT entry_date FROM daily_journal_notes WHERE entry_date BETWEEN %s AND %s",
        (start_date, end_date),
    )
    return {row[0] for row in cur.fetchall()}


def get_logbook_entries(conn, symbol):
    """Returns every logbook row for a symbol, oldest day first."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT entry_date, notes, chart_image, archived_at
        FROM logbook_entries WHERE symbol = %s ORDER BY entry_date
        """,
        (symbol,),
    )
    return [
        {
            "entry_date": row[0],
            "notes": row[1],
            "chart_image": bytes(row[2]) if row[2] is not None else None,
            "archived_at": row[3],
        }
        for row in cur.fetchall()
    ]


def get_logbook_symbols(conn):
    """Returns every symbol that has at least one logbook entry, alphabetically."""
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT symbol FROM logbook_entries ORDER BY symbol")
    return [row[0] for row in cur.fetchall()]


def get_logbook_summary(conn):
    """
    Returns one row per symbol with at least one logbook entry - how
    many entries it has, and its earliest/latest entry_date - without
    pulling every entry's full notes/chart_image just to build a ticker
    list. This is what the Logbook page's date-range filter checks a
    symbol against (does [first_entry, last_entry] overlap the selected
    range at all) before it bothers fetching that symbol's actual
    day-by-day entries.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, COUNT(*), MIN(entry_date), MAX(entry_date)
        FROM logbook_entries
        GROUP BY symbol
        ORDER BY symbol
    """)
    return [
        {"symbol": row[0], "entry_count": row[1], "first_entry": row[2], "last_entry": row[3]}
        for row in cur.fetchall()
    ]


def search_logbook_notes(conn, keyword):
    """Returns the set of symbols with at least one logbook entry whose
    notes contain `keyword` (case-insensitive) - used by the Logbook
    page's keyword filter to narrow the ticker list down to ones worth
    looking at, before you pick one to actually read."""
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT symbol FROM logbook_entries WHERE notes ILIKE %s",
        (f"%{keyword}%",),
    )
    return {row[0] for row in cur.fetchall()}


def get_watchlist(conn):
    """Returns every manually-tracked ticker across all five lists,
    oldest added first - each row says which list it belongs to."""
    cur = conn.cursor()
    cur.execute("SELECT symbol, added_at, list_id FROM watchlist ORDER BY added_at")
    return [{"symbol": row[0], "added_at": row[1], "list_id": row[2]} for row in cur.fetchall()]


def add_to_watchlist(conn, symbol, list_id=1):
    """
    Adds a ticker to one of the five watchlists, or MOVES it there if
    it's already tracked in a different one - a ticker only ever lives
    in ONE list at a time (see the UNIQUE constraint on watchlist.
    symbol), so "add to List 2" when it's already sitting in List 1
    means move it, not reject the add.

    Returns a (status, previous_list_id) pair:
      - ("added", None): a brand-new row was created.
      - ("moved", previous_list_id): it was already tracked in a
        different list and has now been moved to `list_id`.
      - ("unchanged", None): it was already exactly in `list_id` -
        nothing to do.

    `added_at` is only set on a genuinely new row, passed explicitly
    (US Eastern - see timeutil.py) instead of relying on the table's
    own DEFAULT NOW() - Postgres's NOW() reflects the database server's
    own timezone (UTC on Neon), which would make a ticker added in the
    evening show as "added" tomorrow. Moving a ticker between lists
    doesn't touch added_at - it doesn't reset how long it's been
    tracked, only which list it currently shows up in.
    """
    cur = conn.cursor()
    cur.execute("SELECT list_id FROM watchlist WHERE symbol = %s", (symbol,))
    existing = cur.fetchone()

    if existing is None:
        cur.execute(
            "INSERT INTO watchlist (symbol, list_id, added_at) VALUES (%s, %s, %s)",
            (symbol, list_id, timeutil.now_eastern()),
        )
        # Being (re-)added means it's no longer "removed" - clears any
        # stale-removal countdown symbol_archive_report.py is tracking,
        # same as if it had never been removed at all.
        cur.execute("DELETE FROM watchlist_removals WHERE symbol = %s", (symbol,))
        conn.commit()
        return "added", None

    previous_list_id = existing[0]
    if previous_list_id == list_id:
        return "unchanged", None

    cur.execute("UPDATE watchlist SET list_id = %s WHERE symbol = %s", (list_id, symbol))
    conn.commit()
    return "moved", previous_list_id


def remove_from_watchlist(conn, symbol):
    """
    Removes a ticker from whichever watchlist it's in - it stops being
    archived going forward, but its existing logbook_entries history is
    untouched, same as a closed trade's logbook staying permanently
    archived (unless it's later auto-archived-and-deleted - see
    watchlist_removals below and symbol_archive_report.py).

    Records WHEN this happened in watchlist_removals - the starting
    point for that 1-week countdown. ON CONFLICT (re-removing a symbol
    that already had a stale, uncleaned-up row for some reason) resets
    removed_at to now rather than leaving the older timestamp.
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM watchlist WHERE symbol = %s", (symbol,))
    cur.execute(
        """
        INSERT INTO watchlist_removals (symbol, removed_at) VALUES (%s, %s)
        ON CONFLICT (symbol) DO UPDATE SET removed_at = EXCLUDED.removed_at
        """,
        (symbol, timeutil.now_eastern()),
    )
    conn.commit()


def get_watchlist_names(conn):
    """
    Returns the display name of each of the five watchlists as
    {1: "List 1", ..., 5: "List 5"} - falling back to those defaults
    for any list whose name has never been edited.
    """
    cur = conn.cursor()
    cur.execute("SELECT list_id, name FROM watchlist_names")
    saved = dict(cur.fetchall())
    return {list_id: saved.get(list_id, f"List {list_id}") for list_id in range(1, 6)}


def set_watchlist_name(conn, list_id, name):
    """Saves (or updates) one watchlist's display name - same upsert
    pattern as set_stop_loss()."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO watchlist_names (list_id, name)
        VALUES (%s, %s)
        ON CONFLICT (list_id) DO UPDATE SET name = EXCLUDED.name
        """,
        (list_id, name),
    )
    conn.commit()


def get_chart_preferences(conn):
    """
    Returns the saved moving-average preference (there's only ever one,
    since this app has a single user): {"ma_text": "20,50", "ma_colors":
    {"20": "#2375f4"}, "ma_type": "SMA"}. Defaults to no moving averages
    (plain SMA) if nothing has been saved yet. `ma_type` is "SMA" (a
    plain rolling average) or "EMA" (an exponential moving average,
    which weights recent closes more heavily) - one global choice
    applied to every moving average on every chart, not a per-period
    setting, matching how ma_colors already works.
    """
    cur = conn.cursor()
    cur.execute("SELECT ma_periods, ma_colors, ma_type FROM chart_preferences WHERE id = 1")
    row = cur.fetchone()
    if row is None:
        return {"ma_text": "", "ma_colors": {}, "ma_type": "SMA"}
    return {"ma_text": row[0], "ma_colors": row[1], "ma_type": row[2]}


def save_chart_preferences(conn, ma_text, ma_colors, ma_type):
    """
    Saves the moving-average text (e.g. "20,50"), each period's color,
    and the SMA/EMA type as the one persistent chart preference -
    overwriting whatever was saved before, so the chart looks the same
    next time you open the app, on any device, until you change it again.
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO chart_preferences (id, ma_periods, ma_colors, ma_type)
        VALUES (1, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            ma_periods = EXCLUDED.ma_periods,
            ma_colors = EXCLUDED.ma_colors,
            ma_type = EXCLUDED.ma_type
        """,
        (ma_text, Json({str(k): v for k, v in ma_colors.items()}), ma_type),
    )
    conn.commit()


def save_journal_session_progress(conn, queue_symbols, current_index):
    """
    Persists the guided Journal Session's progress: `queue_symbols` is a
    list of {"symbol": ..., "source": "position"/"watchlist"} pairs (the
    order the session is working through), `current_index` is how far
    into it you'd gotten. Overwrites whatever was saved before - there's
    only ever one in-progress session. Called every time the session
    advances (Save & Next, or Skip), not just at the start, so a lost
    connection partway through still resumes from the last ticker you
    actually finished, not the first one.
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO journal_session_progress (id, queue, current_index)
        VALUES (1, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            queue = EXCLUDED.queue,
            current_index = EXCLUDED.current_index
        """,
        (Json(queue_symbols), current_index),
    )
    conn.commit()


def get_journal_session_progress(conn):
    """
    Returns the saved Journal Session progress as
    {"queue": [{"symbol", "source"}, ...], "current_index": N}, or None
    if there's no session in progress right now.
    """
    cur = conn.cursor()
    cur.execute("SELECT queue, current_index FROM journal_session_progress WHERE id = 1")
    row = cur.fetchone()
    if row is None:
        return None
    return {"queue": row[0], "current_index": row[1]}


def clear_journal_session_progress(conn):
    """Deletes the saved Journal Session progress - called once a
    session finishes normally (every ticker in it got journaled or
    skipped), since there's nothing left to offer to resume."""
    cur = conn.cursor()
    cur.execute("DELETE FROM journal_session_progress WHERE id = 1")
    conn.commit()


def create_review_report(conn, range_start, range_end):
    """Starts a new Trade Review Report - called once, at the moment a
    Review Session begins (see pages/1_Trade_Analyzer.py), before any
    individual trade has actually been reviewed yet. Returns the new
    report's id, which the session then threads through every
    save_trade_review() call and the resumable progress row below.
    `range_start`/`range_end` are just the date-range filter the
    session was started with - for display only, since the actual
    trades reviewed are whatever got checked off, not necessarily every
    trade in that range."""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO trade_review_reports (created_at, range_start, range_end) VALUES (%s, %s, %s) RETURNING id",
        (timeutil.now_eastern(), range_start, range_end),
    )
    report_id = cur.fetchone()[0]
    conn.commit()
    return report_id


def save_review_predictions_notes(conn, report_id, notes):
    """Saves the Review Session's opening journal entry ("how do you
    feel about this session, what are your predictions?") - written
    once, before the first trade. See render_review_session()'s intro
    screen."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE trade_review_reports SET predictions_notes = %s WHERE id = %s",
        (notes, report_id),
    )
    conn.commit()


def save_review_reflections_notes(conn, report_id, notes):
    """Saves the Review Session's closing journal entry ("Reflections") -
    written once, after the last trade. See render_review_session()'s
    outro screen."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE trade_review_reports SET reflections_notes = %s WHERE id = %s",
        (notes, report_id),
    )
    conn.commit()


def delete_review_report(conn, report_id):
    """Deletes a Trade Review Report and everything under it (cascades
    to its trade_reviews and their trade_review_snapshots) - used when a
    session ends with nothing actually saved (every trade in it was
    Skipped), so an empty report doesn't clutter the Logbook's review
    list."""
    cur = conn.cursor()
    cur.execute("DELETE FROM trade_review_reports WHERE id = %s", (report_id,))
    conn.commit()


def save_trade_review(conn, report_id, trade, notes, snapshots):
    """
    Saves one reviewed trade into a report: `trade` is a dict from
    get_trades() (its symbol/entry_date/date/direction identify WHICH
    closed trade this is - see trade_reviews' own schema comment for
    why that's natural-key fields, not a trades.id foreign key),
    `notes` is the free-text review, and `snapshots` is
    {timeframe_label: png_bytes} for whichever timeframes were captured
    with "Save this timeframe" during the review (can be empty - a note
    with no snapshots is still a valid review).

    buy_price/sell_price/quantity/profit_loss are saved too (not just
    the identifying fields) so the PDF/Logbook display (see
    analyze_trades.trade_stats()) never has to re-look-up this trade
    from database.get_trades() later - which might not even find a
    matching row anymore if trades were re-imported since.
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO trade_reviews
            (report_id, symbol, entry_date, exit_date, direction, notes, reviewed_at,
             buy_price, sell_price, quantity, profit_loss)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (report_id, trade["symbol"], trade["entry_date"].date(), trade["date"].date(),
         trade["direction"], notes, timeutil.now_eastern(),
         trade["buy_price"], trade["sell_price"], trade["quantity"], trade["profit_loss"]),
    )
    review_id = cur.fetchone()[0]
    for timeframe, chart_image in snapshots.items():
        cur.execute(
            "INSERT INTO trade_review_snapshots (review_id, timeframe, chart_image) VALUES (%s, %s, %s)",
            (review_id, timeframe, chart_image),
        )
    conn.commit()


def get_review_for_trade(conn, report_id, trade):
    """
    Returns the already-saved review for this exact trade within this
    report - {"id", "notes", "snapshots": {timeframe: bytes}} - or None
    if this trade hasn't been reviewed yet in this report. Matched by
    the same natural-key fields as trade_reviews' own schema (symbol/
    entry_date/exit_date/direction/quantity/buy_price/sell_price - see
    pages/1_Trade_Analyzer.py's _trade_key(), which this mirrors).

    Used by the Review Session when revisiting a trade via the Previous
    button or the trade-jump dropdown - lets that render prefill the
    existing notes instead of a blank box, and tells the caller whether
    "Save & Next" should update this row (see update_trade_review()
    below) instead of inserting a duplicate.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, notes FROM trade_reviews
        WHERE report_id = %s AND symbol = %s AND entry_date = %s AND exit_date = %s
              AND direction = %s AND quantity = %s AND buy_price = %s AND sell_price = %s
        """,
        (report_id, trade["symbol"], trade["entry_date"].date(), trade["date"].date(),
         trade["direction"], trade["quantity"], trade["buy_price"], trade["sell_price"]),
    )
    row = cur.fetchone()
    if row is None:
        return None
    review_id, notes = row
    cur.execute(
        "SELECT timeframe, chart_image FROM trade_review_snapshots WHERE review_id = %s", (review_id,))
    snapshots = {timeframe: bytes(chart_image) for timeframe, chart_image in cur.fetchall()}
    return {"id": review_id, "notes": notes, "snapshots": snapshots}


def update_trade_review(conn, review_id, notes, new_snapshots):
    """
    Updates an already-saved review in place, for the Review Session's
    Save & Next when revisiting a trade via Previous or the trade-jump
    dropdown (see get_review_for_trade()) - notes are overwritten with
    whatever's currently in the box (even blank; clearing a note on a
    re-review is a legitimate way to remove it, not something to
    silently keep the old value for). Any newly captured snapshot in
    `new_snapshots` replaces an existing one for that SAME timeframe
    (capturing "Daily" again means "update this snapshot", not "keep
    both") and adds a new row for any timeframe not already saved.
    """
    cur = conn.cursor()
    cur.execute(
        "UPDATE trade_reviews SET notes = %s, reviewed_at = %s WHERE id = %s",
        (notes, timeutil.now_eastern(), review_id),
    )
    for timeframe, chart_image in new_snapshots.items():
        cur.execute(
            "DELETE FROM trade_review_snapshots WHERE review_id = %s AND timeframe = %s",
            (review_id, timeframe),
        )
        cur.execute(
            "INSERT INTO trade_review_snapshots (review_id, timeframe, chart_image) VALUES (%s, %s, %s)",
            (review_id, timeframe, chart_image),
        )
    conn.commit()


def get_reviewed_trade_keys(conn):
    """
    Every reviewed trade across EVERY report, flat (not scoped to one
    report_id the way get_review_report() is) - used by goals.py's
    Monthly Review Credit % goal, which needs to check "has this trade
    been reviewed, and was that review's report actually finished (has
    reflections) and finished by when." Returns a list of
    {"symbol", "entry_date", "exit_date", "direction", "quantity",
    "buy_price", "sell_price", "report_created_at", "reflections_notes"}
    - the first seven fields are the same natural key trade_reviews
    itself is matched by everywhere else (see pages/1_Trade_Analyzer.py's
    _trade_key()), not trades.id.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT tr.symbol, tr.entry_date, tr.exit_date, tr.direction, tr.quantity,
               tr.buy_price, tr.sell_price, r.created_at, r.reflections_notes
        FROM trade_reviews tr
        JOIN trade_review_reports r ON r.id = tr.report_id
    """)
    return [
        {
            "symbol": row[0], "entry_date": row[1], "exit_date": row[2], "direction": row[3],
            "quantity": row[4], "buy_price": row[5], "sell_price": row[6],
            "report_created_at": row[7], "reflections_notes": row[8],
        }
        for row in cur.fetchall()
    ]


def get_review_reports(conn):
    """Returns every Trade Review Report, newest first, as
    {"id", "created_at", "range_start", "range_end", "sent_at",
    "trade_count"} - trade_count is how many trades were actually
    reviewed and saved in it (0 shouldn't normally happen - see
    delete_review_report() - but is possible if a report was created
    and the session was abandoned before this cleanup ran)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT r.id, r.created_at, r.range_start, r.range_end, r.sent_at, COUNT(tr.id)
        FROM trade_review_reports r
        LEFT JOIN trade_reviews tr ON tr.report_id = r.id
        GROUP BY r.id
        ORDER BY r.created_at DESC
    """)
    return [
        {
            "id": row[0], "created_at": row[1], "range_start": row[2],
            "range_end": row[3], "sent_at": row[4], "trade_count": row[5],
        }
        for row in cur.fetchall()
    ]


def get_review_report(conn, report_id):
    """
    Returns one Trade Review Report's full detail:
    {"id", "created_at", "range_start", "range_end", "sent_at",
    "predictions_notes", "reflections_notes",
    "reviews": [{"symbol", "entry_date", "exit_date", "direction",
    "notes", "reviewed_at", "buy_price", "sell_price", "quantity",
    "profit_loss", "snapshots": {timeframe: chart_image}}, ...]}
    or None if this report doesn't exist. Two queries (reviews, then
    every snapshot for those reviews in one batch), not one join per
    review - same "batch it, don't N+1" reasoning as
    get_logbook_entries_for_date().
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, created_at, range_start, range_end, sent_at, predictions_notes, reflections_notes
        FROM trade_review_reports WHERE id = %s
        """,
        (report_id,),
    )
    report_row = cur.fetchone()
    if report_row is None:
        return None

    cur.execute(
        """
        SELECT id, symbol, entry_date, exit_date, direction, notes, reviewed_at,
               buy_price, sell_price, quantity, profit_loss
        FROM trade_reviews WHERE report_id = %s ORDER BY reviewed_at
        """,
        (report_id,),
    )
    review_rows = cur.fetchall()
    review_ids = [row[0] for row in review_rows]

    snapshots_by_review = {review_id: {} for review_id in review_ids}
    if review_ids:
        cur.execute(
            "SELECT review_id, timeframe, chart_image FROM trade_review_snapshots WHERE review_id = ANY(%s)",
            (review_ids,),
        )
        for review_id, timeframe, chart_image in cur.fetchall():
            snapshots_by_review[review_id][timeframe] = bytes(chart_image)

    return {
        "id": report_row[0], "created_at": report_row[1], "range_start": report_row[2],
        "range_end": report_row[3], "sent_at": report_row[4],
        "predictions_notes": report_row[5], "reflections_notes": report_row[6],
        "reviews": [
            {
                "symbol": row[1], "entry_date": row[2], "exit_date": row[3], "direction": row[4],
                "notes": row[5], "reviewed_at": row[6],
                "buy_price": row[7], "sell_price": row[8], "quantity": row[9], "profit_loss": row[10],
                "snapshots": snapshots_by_review[row[0]],
            }
            for row in review_rows
        ],
    }


def get_review_report_sent_status(conn, report_id):
    """Returns when a Trade Review Report's PDF was generated/emailed,
    or None if it hasn't been yet - same idea as get_daily_report_status()."""
    cur = conn.cursor()
    cur.execute("SELECT sent_at FROM trade_review_reports WHERE id = %s", (report_id,))
    row = cur.fetchone()
    return row[0] if row else None


def mark_review_report_sent(conn, report_id):
    """Records that a Trade Review Report's PDF has been generated and
    emailed - same idea as mark_daily_report_generated(), just keyed by
    report id instead of a calendar date since a review report isn't
    one-per-day."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE trade_review_reports SET sent_at = %s WHERE id = %s",
        (timeutil.now_eastern(), report_id),
    )
    conn.commit()


def save_review_session_progress(conn, report_id, queue, current_index):
    """Persists the guided Review Session's progress - same shape and
    purpose as save_journal_session_progress(), just for a queue of
    closed trades. `queue` is a list of {"symbol", "entry_date",
    "exit_date", "direction"} dicts (a trade's natural-key fields, not
    a trades.id - see trade_reviews' own schema comment for why),
    `current_index` is how far into it you'd gotten."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO trade_review_session_progress (id, report_id, queue, current_index)
        VALUES (1, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            report_id = EXCLUDED.report_id,
            queue = EXCLUDED.queue,
            current_index = EXCLUDED.current_index
        """,
        (report_id, Json(queue), current_index),
    )
    conn.commit()


def get_review_session_progress(conn):
    """Returns the saved Review Session progress as {"report_id",
    "queue": [{"symbol", "entry_date", "exit_date", "direction"}, ...],
    "current_index": N}, or None if there's no session in progress."""
    cur = conn.cursor()
    cur.execute("SELECT report_id, queue, current_index FROM trade_review_session_progress WHERE id = 1")
    row = cur.fetchone()
    if row is None:
        return None
    return {"report_id": row[0], "queue": row[1], "current_index": row[2]}


def clear_review_session_progress(conn):
    """Deletes the saved Review Session progress - called once a
    session finishes normally (every trade in it got reviewed or
    skipped)."""
    cur = conn.cursor()
    cur.execute("DELETE FROM trade_review_session_progress WHERE id = 1")
    conn.commit()


def get_tracked_symbols(conn):
    """
    Every symbol currently worth pre-warming a price cache for: every
    open position plus every watchlist ticker, deduplicated - the same
    universe archiving.archive_all() already iterates for the nightly
    Logbook snapshot, just symbols only (no entry dates/directions,
    which warming a plain price cache doesn't need).
    """
    positions = {p["symbol"] for p in get_open_positions(conn)}
    watchlist = {w["symbol"] for w in get_watchlist(conn)}
    return positions | watchlist


def get_cached_price_history(conn, symbol):
    """
    Returns {"history": {...parallel arrays...}, "fetched_for_date": date}
    for `symbol` from the persistent price cache (see
    charting.warm_price_cache_for_symbol()/fetch_history()), or None if
    nothing's cached for it yet.
    """
    cur = conn.cursor()
    cur.execute("SELECT history, fetched_for_date FROM price_cache WHERE symbol = %s", (symbol,))
    row = cur.fetchone()
    if row is None:
        return None
    return {"history": row[0], "fetched_for_date": row[1]}


def save_cached_price_history(conn, symbol, history_dict, fetched_for_date):
    """Saves (or overwrites) `symbol`'s cached daily price history -
    called by the after-close warming job and by the "add to watchlist"
    action, never by an interactive chart view itself."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO price_cache (symbol, history, fetched_for_date)
        VALUES (%s, %s, %s)
        ON CONFLICT (symbol) DO UPDATE SET
            history = EXCLUDED.history,
            fetched_for_date = EXCLUDED.fetched_for_date
        """,
        (symbol, Json(history_dict), fetched_for_date),
    )
    conn.commit()


def get_cached_next_earnings_date(conn, symbol):
    """
    Returns `symbol`'s cached next earnings date (a plain date), or None
    if either nothing's cached for it yet OR Yahoo Finance has no known
    upcoming date - both cases mean "don't show the earnings badge",
    which is why this doesn't distinguish the two.
    """
    cur = conn.cursor()
    cur.execute("SELECT next_earnings_date FROM earnings_cache WHERE symbol = %s", (symbol,))
    row = cur.fetchone()
    return row[0] if row else None


def save_cached_next_earnings_date(conn, symbol, next_earnings_date):
    """Saves (or overwrites) `symbol`'s cached next earnings date -
    `next_earnings_date` may be None (Yahoo Finance has no known
    upcoming date for this symbol). Called by the after-close warming
    job, never by an interactive chart view itself."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO earnings_cache (symbol, next_earnings_date, fetched_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (symbol) DO UPDATE SET
            next_earnings_date = EXCLUDED.next_earnings_date,
            fetched_at = NOW()
        """,
        (symbol, next_earnings_date),
    )
    conn.commit()


def clear_stale_price_cache(conn, expected_day):
    """
    Deletes any cached price history not tagged with `expected_day` -
    called by nightly_archive.py once its own (near-midnight) archiving
    is done, which is the "discard after midnight" half of the price
    cache's lifecycle. Covers both an ordinary day's now-stale cache AND
    a symbol that's no longer tracked at all (removed from every
    watchlist, position closed) - either way, nothing refreshed it for
    `expected_day`, so it's safe to drop; a symbol that IS still tracked
    just gets a fresh row again at the next warming run.

    `expected_day` should be timeutil.expected_last_trading_day(), NOT
    timeutil.today_eastern() - passing the literal calendar day here
    used to delete a perfectly valid weekend cache (tagged with
    Friday's trading day) the moment this ran past Friday itself, since
    a plain calendar "today" never equals "Friday" again after the
    weekend starts.
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM price_cache WHERE fetched_for_date != %s", (expected_day,))
    conn.commit()


def get_drawings(conn, symbol):
    """
    Returns every saved drawing (line/rect/arrow_up/arrow_down) for a
    symbol's chart - see charting.render_interactive_chart(). Keyed by
    symbol only, not by which page or trade you happened to be looking
    at when you drew it, since a support/resistance line drawn on a
    ticker's chart is just as relevant wherever else that ticker's
    chart shows up (Shortlist, Trade Analyzer, an archived snapshot).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT shape_type, x0, y0, x1, y1, color, width, opacity FROM chart_drawings WHERE symbol = %s",
        (symbol,),
    )
    return [
        {
            "type": row[0], "x0": row[1], "y0": row[2], "x1": row[3], "y1": row[4],
            "color": row[5], "width": row[6], "opacity": row[7],
        }
        for row in cur.fetchall()
    ]


def save_drawings(conn, symbol, drawings):
    """
    Replaces every saved drawing for a symbol with `drawings` - the
    chart component's current full list, sent back whenever something
    is added, moved, resized, or erased. Simplest to just wipe and
    rewrite the whole set rather than track individual edits, the same
    "recalculate from scratch" pattern already used for the trades
    table (see rebuild_trades()).
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM chart_drawings WHERE symbol = %s", (symbol,))
    if drawings:
        cur.executemany(
            """
            INSERT INTO chart_drawings (symbol, shape_type, x0, y0, x1, y1, color, width, opacity)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (symbol, d["type"], d["x0"], d["y0"], d.get("x1"), d.get("y1"),
                 d["color"], d["width"], d["opacity"])
                for d in drawings
            ],
        )
    conn.commit()


def get_stop_loss(conn, symbol):
    """Returns the saved stop-loss price for a symbol, or None if one
    hasn't been set yet."""
    cur = conn.cursor()
    cur.execute("SELECT stop_loss FROM position_stops WHERE symbol = %s", (symbol,))
    row = cur.fetchone()
    return row[0] if row else None


def get_all_stop_losses(conn):
    """Returns every saved stop-loss as {symbol: stop_loss} - used by the
    Open Positions page to look them all up in one query instead of one
    per position."""
    cur = conn.cursor()
    cur.execute("SELECT symbol, stop_loss FROM position_stops")
    return {row[0]: row[1] for row in cur.fetchall()}


def set_stop_loss(conn, symbol, stop_loss):
    """Saves (or updates) the stop-loss price for a symbol - overwriting
    whatever was saved before, since a stop is something you move over
    the life of a trade (e.g. trailing it up as the position works).
    `updated_at` is computed in Python (US Eastern - see timeutil.py)
    rather than SQL's NOW(), which reflects the database server's own
    timezone (UTC on Neon), not yours.

    Also appends a stop_loss_events row every time this runs (manual
    edit or ma_strategy.py's auto-trailing alike, since both call this
    same function) - position_stops itself only ever holds the CURRENT
    value, so this is the only place that ever finds out a stop was set
    at all, let alone when. See that table's own note in init_db()."""
    cur = conn.cursor()
    now = timeutil.now_eastern()
    cur.execute(
        """
        INSERT INTO position_stops (symbol, stop_loss, updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (symbol) DO UPDATE SET
            stop_loss = EXCLUDED.stop_loss,
            updated_at = EXCLUDED.updated_at
        """,
        (symbol, stop_loss, now),
    )
    cur.execute(
        "INSERT INTO stop_loss_events (symbol, stop_loss, set_at) VALUES (%s, %s, %s)",
        (symbol, stop_loss, now),
    )
    conn.commit()


def get_stop_loss_events(conn):
    """Every stop_loss_events row ever logged, as {"symbol", "stop_loss",
    "set_at"} dicts - used by goals.py's Stop-Loss Usage % goal to check
    whether a closed trade had a stop set at some point during its
    life."""
    cur = conn.cursor()
    cur.execute("SELECT symbol, stop_loss, set_at FROM stop_loss_events")
    return [{"symbol": row[0], "stop_loss": row[1], "set_at": row[2]} for row in cur.fetchall()]


def delete_stop_loss(conn, symbol):
    """
    Clears the saved stop-loss for a symbol - used when a stop of $0 is
    "saved" on the Shortlist page, which means "I don't have a stop for
    this anymore," not "my stop is literally zero dollars." (Storing an
    actual $0 stop would make the Open Positions page count nearly the
    whole position's value as heat.) Clearing a stop that was never set
    is a harmless no-op.

    Only clears the stop_loss column (UPDATE, not DELETE) - the row can
    also hold this symbol's MA Stop Rule settings now (see
    save_position_ma_settings()), which should survive clearing just
    the dollar stop.
    """
    cur = conn.cursor()
    cur.execute("UPDATE position_stops SET stop_loss = NULL WHERE symbol = %s", (symbol,))
    conn.commit()


# The built-in fallback for the MA Stop Rule's global settings (see
# ma_strategy.py) until you've saved your own on the Settings page -
# a 21-day MA, 2 closes on the wrong side of it to trigger, the MA
# needing to clear cost basis by 5% before a trailing stop takes over,
# and 1%/10% distance thresholds for the "approaching"/"extended"
# badges.
_DEFAULT_STRATEGY_SETTINGS = {
    "ma_period": 21, "closes_threshold": 2, "unlock_pct": 5.0,
    "approach_pct": 1.0, "extended_pct": 10.0,
}


def get_strategy_settings(conn):
    """
    Returns the saved global defaults for the MA Stop Rule (see
    ma_strategy.py) - one row, since this app has a single user.
    Falls back to _DEFAULT_STRATEGY_SETTINGS until you've saved your
    own on the Settings page.
    """
    cur = conn.cursor()
    cur.execute("SELECT ma_period, closes_threshold, unlock_pct, approach_pct, extended_pct FROM strategy_settings WHERE id = 1")
    row = cur.fetchone()
    if row is None:
        return dict(_DEFAULT_STRATEGY_SETTINGS)
    return {
        "ma_period": row[0], "closes_threshold": row[1], "unlock_pct": row[2],
        "approach_pct": row[3], "extended_pct": row[4],
    }


def save_strategy_settings(conn, ma_period, closes_threshold, unlock_pct, approach_pct, extended_pct):
    """Saves the global default MA Stop Rule settings - see get_strategy_settings()."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO strategy_settings (id, ma_period, closes_threshold, unlock_pct, approach_pct, extended_pct)
        VALUES (1, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            ma_period = EXCLUDED.ma_period,
            closes_threshold = EXCLUDED.closes_threshold,
            unlock_pct = EXCLUDED.unlock_pct,
            approach_pct = EXCLUDED.approach_pct,
            extended_pct = EXCLUDED.extended_pct
        """,
        (ma_period, closes_threshold, unlock_pct, approach_pct, extended_pct),
    )
    conn.commit()


def _merge_ma_settings(row, defaults):
    """Shared by get_position_ma_settings()/get_all_position_ma_settings()
    below - turns one position_stops row's MA columns (any of which may
    be NULL, meaning "use the global default") into a complete settings
    dict. `row` is (ma_stop_mode, ma_period, ma_closes_threshold,
    ma_unlock_pct, ma_approach_pct, ma_extended_pct)."""
    mode, ma_period, closes_threshold, unlock_pct, approach_pct, extended_pct = row
    return {
        "mode": mode or "off",
        "ma_period": ma_period if ma_period is not None else defaults["ma_period"],
        "closes_threshold": closes_threshold if closes_threshold is not None else defaults["closes_threshold"],
        "unlock_pct": unlock_pct if unlock_pct is not None else defaults["unlock_pct"],
        "approach_pct": approach_pct if approach_pct is not None else defaults["approach_pct"],
        "extended_pct": extended_pct if extended_pct is not None else defaults["extended_pct"],
    }


def get_position_ma_settings(conn, symbol, defaults):
    """
    Returns this position's MA Stop Rule settings, merged with the
    global `defaults` (see get_strategy_settings()) for any field this
    position hasn't customized - a position that's never been touched
    gets {"mode": "off", ...all the global defaults...}. `mode` is
    "off"/"manual"/"auto" (see pages/4_Open_Positions.py).

    Looks up one symbol - see get_all_position_ma_settings() below for
    the batched version the Open Positions page actually uses now, to
    avoid one round-trip per open position.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ma_stop_mode, ma_period, ma_closes_threshold, ma_unlock_pct, ma_approach_pct, ma_extended_pct
        FROM position_stops WHERE symbol = %s
        """,
        (symbol,),
    )
    row = cur.fetchone()
    if row is None:
        return {"mode": "off", **defaults}
    return _merge_ma_settings(row, defaults)


def get_all_position_ma_settings(conn, defaults):
    """
    Returns every position's MA Stop Rule settings as {symbol: settings},
    each already merged with the global `defaults` exactly like
    get_position_ma_settings() - one query instead of one per open
    position (the same batching get_all_stop_losses() already does for
    dollar stops). A symbol with no position_stops row at all (never
    touched) simply won't be a key here - look it up with
    `.get(symbol, {"mode": "off", **defaults})`.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT symbol, ma_stop_mode, ma_period, ma_closes_threshold, ma_unlock_pct, ma_approach_pct, ma_extended_pct
        FROM position_stops
        """
    )
    return {row[0]: _merge_ma_settings(row[1:], defaults) for row in cur.fetchall()}


def save_position_ma_settings(conn, symbol, mode, ma_period, closes_threshold, unlock_pct, approach_pct, extended_pct):
    """
    Saves this position's MA Stop Rule mode and any per-position
    overrides - each of ma_period/closes_threshold/unlock_pct/
    approach_pct/extended_pct can be None to mean "use the global
    default instead" (see get_position_ma_settings()). Creates the
    position_stops row if it doesn't exist yet (e.g. no dollar
    stop-loss has ever been set for this symbol) - stop_loss is left
    untouched either way (not part of this INSERT/UPDATE at all), so
    this never affects whatever dollar stop is separately set via
    set_stop_loss().
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO position_stops
            (symbol, ma_stop_mode, ma_period, ma_closes_threshold, ma_unlock_pct, ma_approach_pct, ma_extended_pct, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (symbol) DO UPDATE SET
            ma_stop_mode = EXCLUDED.ma_stop_mode,
            ma_period = EXCLUDED.ma_period,
            ma_closes_threshold = EXCLUDED.ma_closes_threshold,
            ma_unlock_pct = EXCLUDED.ma_unlock_pct,
            ma_approach_pct = EXCLUDED.ma_approach_pct,
            ma_extended_pct = EXCLUDED.ma_extended_pct,
            updated_at = EXCLUDED.updated_at
        """,
        (symbol, mode, ma_period, closes_threshold, unlock_pct, approach_pct, extended_pct, timeutil.now_eastern()),
    )
    conn.commit()


# Built-in fallback widths (relative proportions, not pixels - same
# units st.columns() takes) for the Open Positions table, until you've
# saved your own on the Settings page. "mode" is shared by all three
# O/M/A buttons - they're always the same width as each other, so
# there's no reason to adjust them individually.
_DEFAULT_OPEN_POSITIONS_COLUMN_WIDTHS = {
    "ticker": 0.7, "entry_date": 0.9, "shares": 0.6, "avg_price": 0.8,
    "current_price": 0.8, "unrealized_pl": 0.9, "stop_loss": 0.9,
    "mode": 0.3, "ma_signal": 3.5,
}


def get_open_positions_column_widths(conn):
    """
    Returns the saved relative column widths for the Open Positions
    table (see pages/4_Open_Positions.py) - a dict keyed by column
    name, merged with _DEFAULT_OPEN_POSITIONS_COLUMN_WIDTHS for
    anything not yet customized on the Settings page.
    """
    cur = conn.cursor()
    cur.execute("SELECT open_positions_column_widths FROM ui_settings WHERE id = 1")
    row = cur.fetchone()
    saved = row[0] if row else {}
    return {**_DEFAULT_OPEN_POSITIONS_COLUMN_WIDTHS, **saved}


def save_open_positions_column_widths(conn, widths):
    """Saves the Open Positions table's column widths - see get_open_positions_column_widths()."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ui_settings (id, open_positions_column_widths)
        VALUES (1, %s)
        ON CONFLICT (id) DO UPDATE SET open_positions_column_widths = EXCLUDED.open_positions_column_widths
        """,
        (Json(widths),),
    )
    conn.commit()


def get_dashboard_visible_sections(conn, all_sections):
    """
    Returns the saved list of which Dashboard chart sections to show
    (see the Filters sidebar on dashboard.py), or `all_sections` itself
    (everything shown) if nothing's been customized yet - so this is a
    no-op for anyone who's never touched that control.
    """
    cur = conn.cursor()
    cur.execute("SELECT dashboard_visible_sections FROM ui_settings WHERE id = 1")
    row = cur.fetchone()
    if row is None or row[0] is None:
        return list(all_sections)
    return row[0]


def save_dashboard_visible_sections(conn, visible_sections):
    """Saves which Dashboard chart sections to show - see get_dashboard_visible_sections()."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ui_settings (id, dashboard_visible_sections)
        VALUES (1, %s)
        ON CONFLICT (id) DO UPDATE SET dashboard_visible_sections = EXCLUDED.dashboard_visible_sections
        """,
        (Json(visible_sections),),
    )
    conn.commit()


def get_background_image(conn):
    """
    Returns the saved custom background image as {"bytes": ..., "mime":
    "image/png"}, or None if nothing's been uploaded (the default look
    applies). See nav.render_top_nav() for where this actually gets
    applied - every page calls that, so this shows up everywhere.
    """
    cur = conn.cursor()
    cur.execute("SELECT background_image, background_image_mime FROM ui_settings WHERE id = 1")
    row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return {"bytes": bytes(row[0]), "mime": row[1]}


def save_background_image(conn, image_bytes, mime_type):
    """Saves a custom background image - see get_background_image()."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ui_settings (id, background_image, background_image_mime)
        VALUES (1, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            background_image = EXCLUDED.background_image,
            background_image_mime = EXCLUDED.background_image_mime
        """,
        (image_bytes, mime_type),
    )
    conn.commit()


def clear_background_image(conn):
    """Removes the custom background image, reverting to the default look."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE ui_settings SET background_image = NULL, background_image_mime = NULL WHERE id = 1"
    )
    conn.commit()


def get_accent_color(conn):
    """Returns the saved chart accent color override (a "#rrggbb"
    string), or None if it's never been set/has been reset - see
    charting.py's _component_theme_colors() for where None falls back
    to the app's built-in default instead."""
    cur = conn.cursor()
    cur.execute("SELECT accent_color FROM ui_settings WHERE id = 1")
    row = cur.fetchone()
    return row[0] if row else None


def save_accent_color(conn, color):
    """Saves a custom chart accent color - see get_accent_color()."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ui_settings (id, accent_color) VALUES (1, %s)
        ON CONFLICT (id) DO UPDATE SET accent_color = EXCLUDED.accent_color
        """,
        (color,),
    )
    conn.commit()


def clear_accent_color(conn):
    """Resets the chart accent color back to the app's built-in default."""
    cur = conn.cursor()
    cur.execute("UPDATE ui_settings SET accent_color = NULL WHERE id = 1")
    conn.commit()


def get_trade_colors(conn):
    """Returns the saved win/loss color override as {"good": ...,
    "critical": ...} - either key can be None (not saved/reset), meaning
    "use the app's built-in default" for that one - see charting.py's
    win_loss_color(). Returns {"good": None, "critical": None} if
    neither has ever been saved."""
    cur = conn.cursor()
    cur.execute("SELECT good_color, critical_color FROM ui_settings WHERE id = 1")
    row = cur.fetchone()
    if row is None:
        return {"good": None, "critical": None}
    return {"good": row[0], "critical": row[1]}


def save_trade_colors(conn, good_color, critical_color):
    """Saves custom win/loss colors - see get_trade_colors()."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ui_settings (id, good_color, critical_color) VALUES (1, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            good_color = EXCLUDED.good_color,
            critical_color = EXCLUDED.critical_color
        """,
        (good_color, critical_color),
    )
    conn.commit()


def clear_trade_colors(conn):
    """Resets both win/loss colors back to the app's built-in defaults."""
    cur = conn.cursor()
    cur.execute("UPDATE ui_settings SET good_color = NULL, critical_color = NULL WHERE id = 1")
    conn.commit()


def get_nav_labels(conn):
    """Returns the saved nav-bar label overrides as {page_id: custom
    label} - a page_id missing from this dict means "use nav.py's own
    built-in default label for it," see nav.py's PAGES/render_top_nav()."""
    cur = conn.cursor()
    cur.execute("SELECT nav_labels FROM ui_settings WHERE id = 1")
    row = cur.fetchone()
    return row[0] if row and row[0] else {}


def save_nav_labels(conn, labels):
    """Saves the nav-bar label overrides - see get_nav_labels()."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ui_settings (id, nav_labels) VALUES (1, %s)
        ON CONFLICT (id) DO UPDATE SET nav_labels = EXCLUDED.nav_labels
        """,
        (Json(labels),),
    )
    conn.commit()


def clear_nav_labels(conn):
    """Resets every nav-bar label back to its built-in default."""
    cur = conn.cursor()
    cur.execute("UPDATE ui_settings SET nav_labels = '{}' WHERE id = 1")
    conn.commit()


def get_account_value(conn):
    """Returns the saved Jan 1 (start-of-year) account value baseline,
    or None if it hasn't been set yet. This is a fixed starting point,
    not today's value - the Dashboard adds this year's deposits and
    profit/loss on top of it to arrive at today's calculated account
    value, so this number only needs to be set once a year instead of
    kept manually up to date."""
    cur = conn.cursor()
    cur.execute("SELECT account_value FROM account_settings WHERE id = 1")
    row = cur.fetchone()
    return row[0] if row else None


def set_account_value(conn, account_value):
    """Saves (or updates) the Jan 1 account value baseline - overwriting
    whatever was saved before. Meant to be set once at the start of each
    year, not adjusted day to day (deposits and trading P/L already
    account for everything since then). `updated_at` is computed in
    Python (US Eastern - see timeutil.py) rather than SQL's NOW(),
    which reflects the database server's own timezone (UTC on Neon),
    not yours."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO account_settings (id, account_value, updated_at)
        VALUES (1, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            account_value = EXCLUDED.account_value,
            updated_at = EXCLUDED.updated_at
        """,
        (account_value, timeutil.now_eastern()),
    )
    conn.commit()


def get_deposits(conn):
    """Returns every deposit/withdrawal ever recorded, oldest first, as
    {"id", "deposit_date", "amount"} dictionaries. A withdrawal is
    stored as a negative amount - summing this column is all the
    calculated account value needs, so there's no separate table or
    sign-flipping logic for the two."""
    cur = conn.cursor()
    cur.execute("SELECT id, deposit_date, amount FROM deposits ORDER BY deposit_date")
    return [{"id": row[0], "deposit_date": row[1], "amount": row[2]} for row in cur.fetchall()]


def add_deposit(conn, deposit_date, amount):
    """Records one deposit (positive amount) or withdrawal (negative
    amount) for the trading account."""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO deposits (deposit_date, amount) VALUES (%s, %s)",
        (deposit_date, amount),
    )
    conn.commit()


def delete_deposit(conn, deposit_id):
    """Removes one deposit/withdrawal (e.g. one entered by mistake)."""
    cur = conn.cursor()
    cur.execute("DELETE FROM deposits WHERE id = %s", (deposit_id,))
    conn.commit()


def get_realized_pl_since(conn, since_date):
    """Total profit/loss of every closed trade exited on or after
    since_date - used to build up this year's calculated account value
    on top of the Jan 1 baseline, without double-counting trades from
    before then (those are already baked into that baseline)."""
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(profit_loss), 0) FROM trades WHERE exit_date >= %s", (since_date,))
    return cur.fetchone()[0]


def get_daily_report_status(conn, report_date):
    """Returns when the daily PDF report was generated/emailed for a
    given date, or None if it hasn't been yet - used both by the
    Logbook page's status caption and by nightly_archive.py to decide
    whether it needs to generate the report itself as a fallback."""
    cur = conn.cursor()
    cur.execute("SELECT generated_at FROM daily_reports WHERE report_date = %s", (report_date,))
    row = cur.fetchone()
    return row[0] if row else None


def mark_daily_report_generated(conn, report_date):
    """Records that the daily report for a date has been generated and
    emailed - overwriting any earlier timestamp, since re-generating on
    purpose (the button can be clicked more than once a day) should
    always count as the latest "done" marker. `generated_at` is
    computed in Python (US Eastern - see timeutil.py) rather than
    SQL's NOW(), which reflects the database server's own timezone
    (UTC on Neon) - this value is shown directly on the Logbook page
    ("generated and emailed... at H:MM AM/PM"), so it needs to be your
    time, not the server's."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO daily_reports (report_date, generated_at)
        VALUES (%s, %s)
        ON CONFLICT (report_date) DO UPDATE SET generated_at = EXCLUDED.generated_at
        """,
        (report_date, timeutil.now_eastern()),
    )
    conn.commit()


def get_tracked_goals(conn):
    """
    Every goal currently being tracked (see pages/6_Goals.py), oldest
    first, as {"id", "goal_key", "timeframe", "warning_level",
    "alert_level"} dictionaries. Not joined against goals.GOAL_LIBRARY
    here - a row whose goal_key/timeframe no longer matches anything in
    the library (the code changed since it was added) is still
    returned as-is; it's the PAGE's job to notice that and show it as
    stale rather than this function silently hiding it.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT id, goal_key, timeframe, warning_level, alert_level FROM tracked_goals ORDER BY created_at"
    )
    return [
        {"id": row[0], "goal_key": row[1], "timeframe": row[2], "warning_level": row[3], "alert_level": row[4]}
        for row in cur.fetchall()
    ]


def add_tracked_goal(conn, goal_key, timeframe, warning_level=None, alert_level=None):
    """Starts tracking one goal - see get_tracked_goals() for the shape
    this later comes back as. Warning/Alert Level start unset (checking
    a goal on in the Available Goals table doesn't ask for them up
    front) - set them afterward via update_tracked_goal() in the
    Actively Tracked table."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO tracked_goals (goal_key, timeframe, warning_level, alert_level, created_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (goal_key, timeframe, warning_level, alert_level, timeutil.now_eastern()),
    )
    conn.commit()


def update_tracked_goal(conn, goal_id, warning_level, alert_level):
    """Changes an already-tracked goal's Warning/Alert Level in place -
    used when you want to move the bar on a goal without losing its
    history of being tracked (re-adding it would look identical today,
    but this is clearer about what actually happened)."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE tracked_goals SET warning_level = %s, alert_level = %s WHERE id = %s",
        (warning_level, alert_level, goal_id),
    )
    conn.commit()


def delete_tracked_goal(conn, goal_id):
    """Stops tracking one goal - permanent, no undo (nothing else in
    the database references a tracked goal, so there's nothing else to
    clean up alongside it)."""
    cur = conn.cursor()
    cur.execute("DELETE FROM tracked_goals WHERE id = %s", (goal_id,))
    conn.commit()


# The Screener page's signal log (see screener/engine.py's own module
# docstring and the signal_log table in init_db() above) - column order
# here is the one source of truth _signal_row_to_dict() and every
# SELECT below rely on, so a new column always gets added to BOTH the
# CREATE TABLE and this list together.
_SIGNAL_LOG_COLUMNS = [
    "signal_date", "ticker", "tier", "trigger_price", "stop_price", "risk_pct",
    "shares", "position_usd", "risk_usd", "adr", "rs_excess", "rsline_at_high",
    "nr7_2", "pct_off_high", "close_price", "gate_pct", "expires",
    "triggered", "trigger_date", "user_action", "user_note",
    "outcome_return", "outcome_r", "outcome_bars", "exit_reason", "reconciled_at",
]


def _signal_row_to_dict(row):
    return dict(zip(_SIGNAL_LOG_COLUMNS, row))


def save_signals(conn, watchlist):
    """
    Bulk-logs every row in `watchlist` (a screener.engine.ScreenResult.
    watchlist-shaped DataFrame, or anything with the same column names -
    see screener/engine.py's make_orders()) into signal_log. Dedupes on
    (signal_date, ticker) via ON CONFLICT DO NOTHING - the same "first
    logged wins, permanently" semantics daily_screen.py's own
    append_log() uses for its CSV, so a signal's logged numbers never
    change even if it gets re-screened (carried forward at a different
    age) on a later run.

    Deliberately logs EVERY row passed in, strict and baseline alike -
    the caller (pages/7_Screener.py) is responsible for passing the
    FULL watchlist here regardless of which tier it's currently
    displaying, since an unlogged baseline candidate can never be
    reviewed later (see this module's own note on signal_log above).

    Returns how many rows were actually NEW (not already logged).
    """
    if watchlist.empty:
        return 0
    cur = conn.cursor()
    inserted = 0
    for _, s in watchlist.iterrows():
        cur.execute(
            """
            INSERT INTO signal_log
                (signal_date, ticker, tier, trigger_price, stop_price, risk_pct,
                 shares, position_usd, risk_usd, adr, rs_excess, rsline_at_high,
                 nr7_2, pct_off_high, close_price, gate_pct, expires)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (signal_date, ticker) DO NOTHING
            """,
            (s.signal_date, s.ticker, s.tier, s.trigger, s.stop, s.risk_pct,
             int(s.shares), s.position, s.risk_usd, s.adr, s.rs_excess, bool(s.rsline_at_high),
             bool(s.nr7_2), s.pct_off_high, s.close, s.gate_pct, s.expires),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def get_unreconciled_signals(conn):
    """
    Every signal screener.reconcile hasn't finished resolving yet -
    reconciled_at IS NULL covers both a brand-new signal (nothing
    checked at all) and a triggered-but-still-open one (see
    screener.reconcile.reconcile_signal()'s `resolved` flag) - both
    need re-checking against fresh price data on the next reconciliation
    run.
    """
    cur = conn.cursor()
    cur.execute(f"""
        SELECT {", ".join(_SIGNAL_LOG_COLUMNS)} FROM signal_log
        WHERE reconciled_at IS NULL
        ORDER BY signal_date, ticker
    """)
    return [_signal_row_to_dict(row) for row in cur.fetchall()]


def update_signal_reconciliation(conn, signal_date, ticker, triggered=None, trigger_date=None,
                                  outcome_return=None, outcome_r=None, outcome_bars=None,
                                  exit_reason=None, mark_reconciled=False):
    """
    Writes whatever screener.reconcile.reconcile_signal() figured out
    for one signal. `mark_reconciled` sets reconciled_at to NOW() (a
    FINAL answer - never checked again) only when True; a
    `triggered=True, mark_reconciled=False` call (the "still open, no
    exit yet" case) writes the partial info without closing the row
    out, so get_unreconciled_signals() keeps surfacing it for the next
    run. COALESCE keeps any field not passed here unchanged, the same
    "only overwrite what's actually given" convention
    upsert_logbook_entry() already uses elsewhere in this file.
    """
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE signal_log SET
            triggered = COALESCE(%s, triggered),
            trigger_date = COALESCE(%s, trigger_date),
            outcome_return = COALESCE(%s, outcome_return),
            outcome_r = COALESCE(%s, outcome_r),
            outcome_bars = COALESCE(%s, outcome_bars),
            exit_reason = COALESCE(%s, exit_reason),
            reconciled_at = CASE WHEN %s THEN NOW() ELSE reconciled_at END
        WHERE signal_date = %s AND ticker = %s
        """,
        (triggered, trigger_date, outcome_return, outcome_r, outcome_bars, exit_reason,
         mark_reconciled, signal_date, ticker),
    )
    conn.commit()


def update_signal_user_action(conn, signal_date, ticker, user_action, user_note=None):
    """Records what the user actually did with a logged signal (taken/
    skipped/missed) - the only fields on an already-logged signal_log
    row this app ever lets the user edit directly (see the History tab
    on pages/7_Screener.py). Without this, the signal log can log
    candidates but never answer whether a discretionary skip helped or
    cost money - see screener/engine.py's own module docstring."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE signal_log SET user_action = %s, user_note = %s WHERE signal_date = %s AND ticker = %s",
        (user_action, user_note, signal_date, ticker),
    )
    conn.commit()


def get_signals_for_history(conn, start_date, end_date, tier=None):
    """
    Every logged signal (strict AND baseline) between `start_date` and
    `end_date` inclusive, newest first - the History tab's own table
    and summary stats (see pages/7_Screener.py) are both built from
    this. `tier`, if given, restricts to just "strict" or "baseline".
    """
    cur = conn.cursor()
    if tier:
        cur.execute(f"""
            SELECT {", ".join(_SIGNAL_LOG_COLUMNS)} FROM signal_log
            WHERE signal_date BETWEEN %s AND %s AND tier = %s
            ORDER BY signal_date DESC, ticker
        """, (start_date, end_date, tier))
    else:
        cur.execute(f"""
            SELECT {", ".join(_SIGNAL_LOG_COLUMNS)} FROM signal_log
            WHERE signal_date BETWEEN %s AND %s
            ORDER BY signal_date DESC, ticker
        """, (start_date, end_date))
    return [_signal_row_to_dict(row) for row in cur.fetchall()]
