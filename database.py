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

  - `transactions`: one row per raw buy or sell straight from the CSV
    (same shape as `load_transactions()` in analyze_trades.py already
    produces). Every time you import a CSV, only genuinely new rows
    get added - see `import_transactions()` below for how that works.

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

from analyze_trades import load_transactions, match_trades_fifo


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


def get_connection():
    """Opens a connection to the hosted Postgres database and makes
    sure both tables exist."""
    conn = psycopg2.connect(_get_database_url())
    init_db(conn)
    return conn


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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chart_preferences (
            id INTEGER PRIMARY KEY DEFAULT 1,
            ma_periods TEXT NOT NULL DEFAULT '',
            ma_colors JSONB NOT NULL DEFAULT '{}',
            CONSTRAINT single_row CHECK (id = 1)
        )
    """)
    conn.commit()


def import_transactions(conn, csv_path):
    """
    Reads every buy/sell row out of the CSV (reusing load_transactions()
    from analyze_trades.py) and adds any that aren't already stored.

    The `UNIQUE` constraint on the transactions table (set up in
    init_db above) means a row that's an exact match - same date,
    symbol, action, price, and quantity - as one already stored gets
    silently skipped instead of stored twice, thanks to
    "ON CONFLICT DO NOTHING" below. Since your Fidelity CSV always
    contains your FULL history, this is what lets you just re-export
    and re-import any time without creating duplicates.

    Returns how many new rows were actually added.
    """
    transactions = load_transactions(csv_path)
    cur = conn.cursor()

    new_count = 0
    for t in transactions:
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


def rebuild_trades(conn):
    """
    Recalculates the entire `trades` table from scratch, using every
    row currently in `transactions`.

    This reuses match_trades_fifo() from analyze_trades.py (the same
    FIFO buy/sell matching logic the other scripts use), so a "trade"
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

    closed_trades, _open_lots = match_trades_fifo(transactions)
    stock_trades = [t for t in closed_trades if not t["symbol"].strip().startswith("-")]

    cur.execute("DELETE FROM trades")
    cur.executemany(
        """
        INSERT INTO trades (symbol, entry_date, buy_price, quantity, exit_date, sell_price, profit_loss)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
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
        SELECT symbol, entry_date, buy_price, quantity, exit_date, sell_price, profit_loss
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
        }
        for row in cur.fetchall()
    ]


def get_open_positions(conn):
    """
    Returns currently-open positions (bought but not yet sold), computed
    fresh from match_trades_fifo()'s open_lots - the other side of the
    same FIFO matching that produces `trades`. This is derived data, not
    something separately tracked, so it's always consistent with whatever
    is currently in `transactions` - no separate "position opened" event
    needs to be recorded anywhere.

    One row per symbol, aggregating multiple still-open buy lots together
    (total shares, a quantity-weighted average entry price, and the
    earliest entry date among them). Sorted oldest entry first.
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

    _closed_trades, open_lots = match_trades_fifo(transactions)

    positions = []
    for symbol, lots in open_lots.items():
        if not lots or symbol.strip().startswith("-"):
            continue  # no open shares, or an options contract (not tracked here)
        total_quantity = sum(lot["quantity"] for lot in lots)
        total_cost = sum(lot["quantity"] * lot["price"] for lot in lots)
        positions.append({
            "symbol": symbol,
            "quantity": total_quantity,
            "avg_price": total_cost / total_quantity,
            "entry_date": min(lot["date"] for lot in lots),
        })

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


def get_watchlist(conn):
    """Returns every manually-tracked ticker, oldest added first."""
    cur = conn.cursor()
    cur.execute("SELECT symbol, added_at FROM watchlist ORDER BY added_at")
    return [{"symbol": row[0], "added_at": row[1]} for row in cur.fetchall()]


def add_to_watchlist(conn, symbol):
    """Adds a ticker to the watchlist. Re-adding one already being tracked
    is a harmless no-op - it doesn't reset its added_at date."""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO watchlist (symbol) VALUES (%s) ON CONFLICT (symbol) DO NOTHING",
        (symbol,),
    )
    conn.commit()


def remove_from_watchlist(conn, symbol):
    """
    Removes a ticker from the watchlist - it stops being archived going
    forward, but its existing logbook_entries history is untouched, same
    as a closed trade's logbook staying permanently archived.
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM watchlist WHERE symbol = %s", (symbol,))
    conn.commit()


def get_chart_preferences(conn):
    """
    Returns the saved moving-average preference (there's only ever one,
    since this app has a single user): {"ma_text": "20,50", "ma_colors":
    {"20": "#2375f4"}}. Defaults to no moving averages if nothing has
    been saved yet.
    """
    cur = conn.cursor()
    cur.execute("SELECT ma_periods, ma_colors FROM chart_preferences WHERE id = 1")
    row = cur.fetchone()
    if row is None:
        return {"ma_text": "", "ma_colors": {}}
    return {"ma_text": row[0], "ma_colors": row[1]}


def save_chart_preferences(conn, ma_text, ma_colors):
    """
    Saves the moving-average text (e.g. "20,50") and each period's color
    as the one persistent chart preference - overwriting whatever was
    saved before, so the chart looks the same next time you open the
    app, on any device, until you change it again.
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO chart_preferences (id, ma_periods, ma_colors)
        VALUES (1, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            ma_periods = EXCLUDED.ma_periods,
            ma_colors = EXCLUDED.ma_colors
        """,
        (ma_text, Json({str(k): v for k, v in ma_colors.items()})),
    )
    conn.commit()
