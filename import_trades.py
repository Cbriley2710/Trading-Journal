"""
Import Trades
=====================
This is the script you run day to day, whenever you have new trading
activity to record. It replaces running build_trade_tracker.py
directly - that script still exists, but now it just updates Excel
from trading.db; this script is what feeds trading.db in the first
place.

WHAT IT DOES, IN ORDER:
  1. Finds your newest Fidelity CSV export in Downloads (reusing
     find_csv_file() from analyze_trades.py).
  2. Imports any transactions from it that aren't already stored in
     trading.db (see database.py - already-stored ones are skipped
     automatically, so re-running this with the same or an overlapping
     CSV never creates duplicates).
  3. Recalculates the full list of completed trades from everything
     now in the database.
  4. Updates Trade Tracker Template 2026.xlsx with any trades that
     aren't in it yet (reusing build_trade_tracker.py's
     update_excel_tracker()).
"""

import build_trade_tracker
import database
from analyze_trades import find_csv_file, find_csv_file_schwab


def find_newest_export():
    """
    Looks in Downloads for BOTH brokerages' export files (Fidelity's
    "History_for_Account_*.csv" and Schwab's "*_Transactions_*.csv")
    and returns whichever matching file is newest overall - so this
    script picks up whichever brokerage you exported from most
    recently, same as the web importer's auto-detection.
    """
    candidates = []
    for finder in (find_csv_file, find_csv_file_schwab):
        try:
            candidates.append(finder())
        except FileNotFoundError:
            pass  # no exports from this brokerage - fine, try the other

    if not candidates:
        raise FileNotFoundError(
            "No Fidelity or Schwab trade-history export found in your "
            "Downloads folder. Export one from your brokerage and try again."
        )

    return max(candidates, key=lambda f: f.stat().st_mtime)


def main():
    csv_path = find_newest_export()
    print(f"Reading trades from: {csv_path.name}")

    conn = database.get_connection()

    new_transaction_count = database.import_transactions(conn, csv_path)
    print(f"Imported {new_transaction_count} new transaction row(s).")

    trade_count = database.rebuild_trades(conn)
    print(f"Recalculated {trade_count} completed stock trade(s) total.")

    all_trades = database.get_trades(conn)
    build_trade_tracker.update_excel_tracker(all_trades)


if __name__ == "__main__":
    main()
