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

WHAT'S STORED, IN TWO TABLES (a "table" is just a grid of rows and
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
"""

from datetime import datetime

import psycopg2
import streamlit as st

from analyze_trades import load_transactions, match_trades_fifo


def _get_database_url():
    try:
        return st.secrets["DATABASE_URL"]
    except Exception:
        raise RuntimeError(
            "No DATABASE_URL found. Copy .streamlit/secrets.toml.example to "
            ".streamlit/secrets.toml and fill in your Neon connection string."
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
