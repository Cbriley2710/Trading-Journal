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
from datetime import datetime

import psycopg2
from psycopg2.extras import Json
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
    conn = psycopg2.connect(_get_database_url())
    if not _schema_ready:
        init_db(conn)
        _schema_ready = True
    return conn


def _column_exists(cur, table, column):
    """Whether a column already exists - a plain read, used so the
    ALTER TABLE migrations below (which need an exclusive lock on their
    table) only actually run the one time they have real work to do."""
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


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
            date DATE NOT NULL,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            price DOUBLE PRECISION NOT NULL,
            quantity DOUBLE PRECISION NOT NULL,
            UNIQUE (date, symbol, action, price, quantity)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            entry_date DATE NOT NULL,
            buy_price DOUBLE PRECISION NOT NULL,
            quantity DOUBLE PRECISION NOT NULL,
            exit_date DATE NOT NULL,
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
    conn.commit()


def _insert_transactions(conn, transactions):
    """
    Adds any of these normalized transaction dicts ({"date", "symbol",
    "action", "price", "quantity"} - see analyze_trades.load_
    transactions()) that aren't already stored. Shared by every import
    path (CSV upload, SnapTrade sync) so there's exactly one place
    that knows how a transaction gets deduplicated and inserted.

    A row gets skipped as a duplicate two ways:
      1. The `UNIQUE` constraint on the transactions table (set up in
         init_db above) catches an EXACT match on date/symbol/action/
         price/quantity, via "ON CONFLICT DO NOTHING" below.
      2. The SELECT check just above that: Fidelity/Schwab CSV exports
         and SnapTrade can both report the same real fill with a
         slightly different price - e.g. "239.63" from a CSV vs
         "239.6296" from SnapTrade, probably just different rounding
         of the same execution - which is close enough to defeat #1's
         exact match, and would otherwise double-count that trade
         (inflating a position's share count) every time it shows up
         from a second source. So a row is also treated as a duplicate
         whenever an existing one already matches on date/symbol/
         action/quantity and its price is within a cent (or 0.2%,
         whichever is bigger, for pricier stocks).

    Since a CSV export always contains your FULL history, and a
    SnapTrade sync re-fetches a recent overlapping window every time
    (see snaptrade_sync.py), both of these together are what let
    either source run repeatedly, or alongside the other, without
    creating duplicates.

    Returns how many new rows were actually added.
    """
    cur = conn.cursor()

    new_count = 0
    for t in transactions:
        cur.execute(
            """
            SELECT 1 FROM transactions
            WHERE date = %s AND symbol = %s AND action = %s AND quantity = %s
              AND abs(price - %s) <= greatest(0.01, price * 0.002)
            LIMIT 1
            """,
            (t["date"].date(), t["symbol"], t["action"], t["quantity"], t["price"]),
        )
        if cur.fetchone():
            continue

        cur.execute(
            """
            INSERT INTO transactions (date, symbol, action, price, quantity)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (t["date"].date(), t["symbol"], t["action"], t["price"], t["quantity"]),
        )
        if cur.rowcount == 1:
            new_count += 1

    conn.commit()
    return new_count


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
    dedup-safe insert. See pages/0_Import_Trades.py (manual "Sync
    Now") and snaptrade_daily_sync.py (the automatic daily version)
    for where this gets called from.

    Returns how many new rows were actually added.
    """
    transactions = snaptrade_sync.fetch_activities(start_date, end_date)
    return _insert_transactions(conn, transactions)


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
    transactions = [
        {
            "date": datetime.combine(row[0], datetime.min.time()),
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
                t["entry_date"].date(),
                t["buy_price"],
                t["quantity"],
                t["date"].date(),
                t["sell_price"],
                t["profit_loss"],
                t["direction"],
            )
            for t in stock_trades
        ],
    )
    conn.commit()

    return len(stock_trades)


def get_trades(conn):
    """
    Returns every completed trade from the `trades` table, oldest
    first, in the same dictionary shape build_trade_tracker.py and
    dashboard.py already expect.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, entry_date, buy_price, quantity, exit_date, sell_price, profit_loss, direction
        FROM trades
        ORDER BY entry_date
    """)

    return [
        {
            "symbol": row[0],
            "entry_date": datetime.combine(row[1], datetime.min.time()),
            "buy_price": row[2],
            "quantity": row[3],
            "date": datetime.combine(row[4], datetime.min.time()),
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


def get_open_positions(conn):
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
    """
    cur = conn.cursor()
    cur.execute("SELECT date, symbol, action, price, quantity FROM transactions")
    transactions = [
        {
            "date": datetime.combine(row[0], datetime.min.time()),
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


def get_logbook_entry(conn, symbol, entry_date):
    """Returns one day's logbook row for a symbol, or None if it doesn't exist yet."""
    cur = conn.cursor()
    cur.execute(
        "SELECT notes, chart_image, archived_at FROM logbook_entries WHERE symbol = %s AND entry_date = %s",
        (symbol, entry_date),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "notes": row[0],
        "chart_image": bytes(row[1]) if row[1] is not None else None,
        "archived_at": row[2],
    }


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
    Adds a ticker to one of the five watchlists. A ticker can only live
    in ONE list at a time - if it's already somewhere (this list or
    another), nothing changes and this returns False so the page can
    say where it already is. Returns True when it was actually added.

    `added_at` is passed explicitly (US Eastern - see timeutil.py)
    instead of relying on the table's own DEFAULT NOW() - Postgres's
    NOW() reflects the database server's own timezone (UTC on Neon),
    which would make a ticker added in the evening show as "added"
    tomorrow.
    """
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO watchlist (symbol, list_id, added_at) VALUES (%s, %s, %s) ON CONFLICT (symbol) DO NOTHING",
        (symbol, list_id, timeutil.now_eastern()),
    )
    conn.commit()
    return cur.rowcount == 1


def remove_from_watchlist(conn, symbol):
    """
    Removes a ticker from whichever watchlist it's in - it stops being
    archived going forward, but its existing logbook_entries history is
    untouched, same as a closed trade's logbook staying permanently
    archived.
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM watchlist WHERE symbol = %s", (symbol,))
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
    timezone (UTC on Neon), not yours."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO position_stops (symbol, stop_loss, updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (symbol) DO UPDATE SET
            stop_loss = EXCLUDED.stop_loss,
            updated_at = EXCLUDED.updated_at
        """,
        (symbol, stop_loss, timeutil.now_eastern()),
    )
    conn.commit()


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


def get_position_ma_settings(conn, symbol, defaults):
    """
    Returns this position's MA Stop Rule settings, merged with the
    global `defaults` (see get_strategy_settings()) for any field this
    position hasn't customized - a position that's never been touched
    gets {"mode": "off", ...all the global defaults...}. `mode` is
    "off"/"manual"/"auto" (see pages/4_Open_Positions.py).
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
    mode, ma_period, closes_threshold, unlock_pct, approach_pct, extended_pct = row
    return {
        "mode": mode or "off",
        "ma_period": ma_period if ma_period is not None else defaults["ma_period"],
        "closes_threshold": closes_threshold if closes_threshold is not None else defaults["closes_threshold"],
        "unlock_pct": unlock_pct if unlock_pct is not None else defaults["unlock_pct"],
        "approach_pct": approach_pct if approach_pct is not None else defaults["approach_pct"],
        "extended_pct": extended_pct if extended_pct is not None else defaults["extended_pct"],
    }


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
