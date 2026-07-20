"""
Stock Trade Analyzer
=====================
This script reads your Fidelity trading history CSV file and tells you:
  - How many trades were wins vs. losses
  - Your total profit/loss for the year
  - Profit/loss broken down by ticker (stock symbol)
  - Your single biggest winning trade and biggest losing trade

HOW IT WORKS (plain-language overview):
Your CSV file lists every buy and sell as a separate line, not as
matched "trades." For example, it might say you bought 100 shares of
AAPL on Monday, then sold 100 shares of AAPL on Wednesday. To know if
that was a win or a loss, we need to match the buy to the sell
ourselves.

We do this using a method called FIFO (First In, First Out) - the same
method accountants use. Imagine a line at a grocery store: the first
person in line is the first one served. Here, the first shares you
bought are treated as the first shares you sold. So if you buy shares
in two separate batches and then sell some, we match the sale against
your OLDEST batch first.
"""

import csv
from datetime import datetime
from pathlib import Path

# The folder this script lives in - some other files (like the Excel
# template) still live alongside the script.
FOLDER = Path(__file__).parent

# Your web browser saves Fidelity's export here automatically. Fidelity
# names the single-account export "History_for_Account_<number>.csv"
# (a different export, "Accounts_History.csv", covers ALL your
# accounts combined and has extra columns this script doesn't expect -
# so we specifically look for the single-account name, not just any CSV).
DOWNLOADS_FOLDER = Path.home() / "Downloads"
CSV_NAME_PATTERN = "History_for_Account_*.csv"

# Schwab names its "Transaction History" export
# "<account>_Transactions_<timestamp>.csv".
CSV_NAME_PATTERN_SCHWAB = "*_Transactions_*.csv"


def find_csv_file():
    """
    Looks in your Downloads folder for your Fidelity trading history
    export, so you don't have to move the file anywhere - just export
    it from Fidelity (it'll land in Downloads) and run the script.

    Downloads builds up old exports over time, so if it finds more than
    one matching file, it just uses the most recently downloaded one
    rather than stopping to ask.
    """
    csv_files = list(DOWNLOADS_FOLDER.glob(CSV_NAME_PATTERN))

    if not csv_files:
        raise FileNotFoundError(
            f"No file matching '{CSV_NAME_PATTERN}' found in "
            f"{DOWNLOADS_FOLDER}. Export your trading history from "
            "Fidelity (the single-account export) and try again."
        )

    return max(csv_files, key=lambda f: f.stat().st_mtime)


def load_transactions(filename):
    """
    Reads the CSV file and pulls out only the rows that are actual
    stock buys or sells. Dividends, interest charges, and other
    non-trade rows are skipped.

    Returns a list of dictionaries, one per buy/sell, like:
        {"date": datetime, "symbol": "AAPL", "action": "BUY",
         "price": 150.0, "quantity": 100}
    """
    transactions = []

    with open(filename, newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.reader(csv_file)

        # The real column headers ("Run Date,Action,Symbol,...") aren't
        # on the very first line of this file - Fidelity adds a couple
        # of blank lines before it. So we skip rows until we find the
        # one that starts with "Run Date".
        for row in reader:
            if row and row[0] == "Run Date":
                break

        # Now `reader` is positioned right after the header row, so we
        # can read the actual data rows using their column position.
        for row in reader:
            if len(row) < 7:
                continue  # skip blank/short rows

            run_date, action, symbol, description, row_type, price, quantity = row[:7]

            # We only care about rows where you actually bought or sold
            # a stock. Everything else (dividends, margin interest,
            # money market "reinvestment" rows, etc.) gets skipped.
            if action.startswith("YOU BOUGHT"):
                trade_action = "BUY"
            elif action.startswith("YOU SOLD"):
                trade_action = "SELL"
            else:
                continue

            if not symbol or not price:
                continue  # skip rows with no ticker or no price

            transactions.append({
                "date": datetime.strptime(run_date, "%m/%d/%Y"),
                "symbol": symbol,
                "action": trade_action,
                "price": float(price),
                "quantity": abs(float(quantity)),
            })

    return transactions


def find_csv_file_schwab():
    """
    Looks in your Downloads folder for a Schwab transaction history
    export - same "just use the newest matching one" behavior as
    find_csv_file() above, just a different filename pattern.
    """
    csv_files = list(DOWNLOADS_FOLDER.glob(CSV_NAME_PATTERN_SCHWAB))

    if not csv_files:
        raise FileNotFoundError(
            f"No file matching '{CSV_NAME_PATTERN_SCHWAB}' found in "
            f"{DOWNLOADS_FOLDER}. Export your transaction history from "
            "Schwab and try again."
        )

    return max(csv_files, key=lambda f: f.stat().st_mtime)


def detect_csv_source(filename):
    """
    Looks at a CSV's header row to figure out which brokerage exported
    it, so you never have to say which one it is - just drop the file
    in and it's handled. Fidelity's file has a couple of blank lines
    before a "Run Date" header; Schwab's starts right away with "Date"
    as its first column. Checks the first several rows (not just the
    very first) since Fidelity's blank lines mean the real header isn't
    always on line 1.
    """
    with open(filename, newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.reader(csv_file)
        for _ in range(10):
            try:
                row = next(reader)
            except StopIteration:
                break
            if not row:
                continue
            first_cell = row[0].strip()
            if first_cell == "Run Date":
                return "fidelity"
            if first_cell == "Date":
                return "schwab"

    raise ValueError(
        f"Could not tell whether {filename} is a Fidelity or Schwab "
        "export - no recognizable header row found in the first 10 lines."
    )


# Schwab labels a short sale distinctly ("Sell Short"), but labels
# covering that short (buying it back) with the exact same plain "Buy"
# used for opening a brand new long position - match_trades_lifo() is
# what actually tells those two apart (by checking whether a short
# position is already open for that symbol), not this mapping.
SCHWAB_ACTION_MAP = {"Buy": "BUY", "Sell": "SELL", "Sell Short": "SELL_SHORT"}


def load_transactions_schwab(filename):
    """
    Reads a Schwab "Transaction History" CSV export and pulls out only
    the rows that are actual stock buys, sells, or short sales -
    dividends, margin interest, internal transfers ("Journal"), and
    other non-trade rows are skipped, the same idea as
    load_transactions() above but for Schwab's different column layout
    and action labels.

    Returns the same shape as load_transactions(): a list of
    {"date": datetime, "symbol": ..., "action": "BUY"/"SELL"/"SELL_SHORT",
     "price": float, "quantity": float} dictionaries - "SELL_SHORT" is
    the one new action Fidelity's file never produces.
    """
    transactions = []

    with open(filename, newline="", encoding="utf-8-sig") as csv_file:
        for row in csv.DictReader(csv_file):
            trade_action = SCHWAB_ACTION_MAP.get(row.get("Action", "").strip())
            if trade_action is None:
                continue  # not a buy/sell/short-sale row - skip it

            symbol = row.get("Symbol", "").strip()
            price = row.get("Price", "").strip()
            quantity = row.get("Quantity", "").strip()
            if not symbol or not price or not quantity:
                continue  # skip rows with no ticker or no price/quantity

            transactions.append({
                "date": datetime.strptime(row["Date"].strip(), "%m/%d/%Y"),
                "symbol": symbol,
                "action": trade_action,
                "price": float(price.replace("$", "").replace(",", "")),
                "quantity": abs(float(quantity.replace(",", ""))),
            })

    return transactions


# When your broker fills a big order, it often splits it into several
# smaller "partial fills" at slightly different prices instead of one
# clean transaction. This is how many cents apart two fills can be and
# still count as pieces of the same order. Raise this number if you
# still see an order split into multiple rows; lower it if unrelated
# trades are getting merged together.
PARTIAL_FILL_THRESHOLD = 0.15


def merge_partial_fills(transactions, threshold=PARTIAL_FILL_THRESHOLD):
    """
    Combines partial fills into a single transaction. A "partial fill"
    is a buy (or sell) of the same stock, on the same day, in a chain
    of prices no more than `threshold` apart from one neighbor to the
    next - for example five separate sells of AAPL on the same day at
    $150.01, $150.03, and $150.06 are really just one sell order that
    got filled in pieces.

    The combined transaction's price is the volume-weighted average
    price (bigger fills count more toward the average), and its
    quantity is the total of all the pieces.
    """
    groups = {}
    for t in transactions:
        key = (t["date"], t["symbol"], t["action"])
        groups.setdefault(key, []).append(t)

    merged = []
    for fills in groups.values():
        # Sort by price so nearby prices end up next to each other.
        fills = sorted(fills, key=lambda t: t["price"])

        cluster = [fills[0]]
        for fill in fills[1:]:
            if fill["price"] - cluster[-1]["price"] <= threshold:
                cluster.append(fill)
            else:
                merged.append(_combine_fills(cluster))
                cluster = [fill]
        merged.append(_combine_fills(cluster))

    return merged


def _combine_fills(fills):
    """Averages a cluster of partial fills into one transaction."""
    total_quantity = sum(f["quantity"] for f in fills)
    total_cost = sum(f["price"] * f["quantity"] for f in fills)
    return {
        "date": fills[0]["date"],
        "symbol": fills[0]["symbol"],
        "action": fills[0]["action"],
        "price": total_cost / total_quantity,
        "quantity": total_quantity,
    }


def match_trades_lifo(transactions):
    """
    Matches each closing transaction to the most recently opened lot
    for that same ticker (LIFO - Last In, First Out) and calculates the
    profit or loss for each matched portion - for both long trades (buy,
    then later sell) and short trades (sell short, then later buy back
    to cover).

    LIFO (rather than oldest-lot-first FIFO) matches how adding to a
    position actually tends to get traded in practice: an add usually
    carries its own tighter exit (a stop or a target) while the
    original entry keeps riding - so a sell shortly after an add is far
    more often closing that add than reaching all the way back to close
    the original entry instead.

    A plain BUY first covers any already-open short position for that
    symbol (most recently opened short lot first) before any leftover
    opens a new long lot - this is how a short's "cover" is recognized,
    since brokers (Schwab included) don't always give it its own
    distinct action label the way they do for opening a short ("Sell
    Short"). A plain SELL only ever closes an open long lot - it never
    opens a new short; only an explicit "SELL_SHORT" action does that.

    Returns (closed_trades, open_long_lots, open_short_lots). Each
    closed trade is a dictionary like:
        {"symbol": "AAPL", "direction": "LONG", "buy_price": 150.0,
         "sell_price": 155.0, "quantity": 100, "profit_loss": 500.0,
         "entry_date": datetime, "date": datetime}
    For a "SHORT" trade, "sell_price"/"entry_date" describe the short
    sale (the opening event) and "buy_price"/"date" describe the cover
    (the closing event) - keeping the same (sell_price - buy_price) *
    quantity profit formula correct for both directions, even though
    for a short the "sell" chronologically happens before the "buy".
    """
    # First, combine any partial fills (see merge_partial_fills above)
    # so a single order doesn't get counted as several tiny trades.
    transactions = merge_partial_fills(transactions)

    # Sort transactions oldest-first so we process them in the order
    # they actually happened. There's no time-of-day in this data, only
    # a date, so same-day trades are also sorted with BUY/SELL_SHORT
    # before SELL - a same-day round trip almost always opens before it
    # closes, and some brokers don't reliably label a same-day short
    # sale as "Sell Short", so without this tiebreaker a same-day
    # SELL-then-BUY pair (in whatever order the export happened to list
    # them) can get processed as "sell with nothing to close" (silently
    # dropped) followed by a same-day BUY that wrongly opens a brand
    # new, never-closed position instead of the two matching each other.
    transactions = sorted(transactions, key=lambda t: (t["date"], t["action"] == "SELL"))

    # For each ticker, we keep a "line" (list) of batches that haven't
    # been fully closed yet - one line for long positions (opened by a
    # BUY), one for short positions (opened by a SELL_SHORT).
    open_long_lots = {}  # example: {"AAPL": [{"price": 150.0, "quantity": 100}]}
    open_short_lots = {}

    closed_trades = []

    for t in transactions:
        symbol = t["symbol"]
        open_long_lots.setdefault(symbol, [])
        open_short_lots.setdefault(symbol, [])

        if t["action"] == "SELL_SHORT":
            open_short_lots[symbol].append({
                "price": t["price"],
                "quantity": t["quantity"],
                "date": t["date"],
            })

        elif t["action"] == "BUY":
            shares_to_buy = t["quantity"]

            # This buy first covers any already-open short position
            # for this symbol (most recently opened short lot first) ...
            while shares_to_buy > 0 and open_short_lots[symbol]:
                most_recent_short = open_short_lots[symbol][-1]
                matched_quantity = min(most_recent_short["quantity"], shares_to_buy)

                profit_loss = (most_recent_short["price"] - t["price"]) * matched_quantity

                closed_trades.append({
                    "symbol": symbol,
                    "direction": "SHORT",
                    "buy_price": t["price"],
                    "sell_price": most_recent_short["price"],
                    "quantity": matched_quantity,
                    "profit_loss": profit_loss,
                    "entry_date": most_recent_short["date"],
                    "date": t["date"],
                })

                most_recent_short["quantity"] -= matched_quantity
                shares_to_buy -= matched_quantity

                if most_recent_short["quantity"] == 0:
                    open_short_lots[symbol].pop()

            # ... and any leftover quantity (past what was needed to
            # cover a short) opens a new long lot, same as before.
            if shares_to_buy > 0:
                open_long_lots[symbol].append({
                    "price": t["price"],
                    "quantity": shares_to_buy,
                    "date": t["date"],
                })

        else:  # SELL - only ever closes an open long lot
            shares_to_sell = t["quantity"]

            while shares_to_sell > 0:
                if not open_long_lots[symbol]:
                    # There's a sell with no matching buy on record
                    # (e.g. a trade from before this file's date
                    # range). We can't calculate a profit/loss for it,
                    # so we skip it and move on.
                    print(f"Note: skipping unmatched sell of {shares_to_sell} "
                          f"{symbol} shares on {t['date'].date()} (no buy found).")
                    break

                most_recent_lot = open_long_lots[symbol][-1]
                matched_quantity = min(most_recent_lot["quantity"], shares_to_sell)

                profit_loss = (t["price"] - most_recent_lot["price"]) * matched_quantity

                closed_trades.append({
                    "symbol": symbol,
                    "direction": "LONG",
                    "buy_price": most_recent_lot["price"],
                    "sell_price": t["price"],
                    "quantity": matched_quantity,
                    "profit_loss": profit_loss,
                    "entry_date": most_recent_lot["date"],
                    "date": t["date"],
                })

                most_recent_lot["quantity"] -= matched_quantity
                shares_to_sell -= matched_quantity

                if most_recent_lot["quantity"] == 0:
                    open_long_lots[symbol].pop()

    return closed_trades, open_long_lots, open_short_lots


def build_report(closed_trades, open_long_lots, open_short_lots):
    """
    Takes the list of closed (matched) trades and prints a summary
    report to the screen.
    """
    if not closed_trades:
        print("No completed (matched buy+sell) trades were found.")
        return

    wins = [t for t in closed_trades if t["profit_loss"] > 0]
    losses = [t for t in closed_trades if t["profit_loss"] < 0]
    breakeven = [t for t in closed_trades if t["profit_loss"] == 0]

    total_profit_loss = sum(t["profit_loss"] for t in closed_trades)

    # Add up profit/loss per ticker symbol.
    profit_by_symbol = {}
    for t in closed_trades:
        profit_by_symbol[t["symbol"]] = profit_by_symbol.get(t["symbol"], 0) + t["profit_loss"]

    biggest_win = max(closed_trades, key=lambda t: t["profit_loss"])
    biggest_loss = min(closed_trades, key=lambda t: t["profit_loss"])

    print("=" * 60)
    print("STOCK TRADE ANALYSIS")
    print("=" * 60)

    print(f"\nTotal completed trades: {len(closed_trades)}")
    print(f"Wins:   {len(wins)}")
    print(f"Losses: {len(losses)}")
    if breakeven:
        print(f"Breakeven: {len(breakeven)}")
    win_rate = (len(wins) / len(closed_trades)) * 100
    print(f"Win rate: {win_rate:.1f}%")

    print(f"\nTotal profit/loss: ${total_profit_loss:,.2f}")

    print("\nProfit/loss by ticker (highest to lowest):")
    for symbol, pl in sorted(profit_by_symbol.items(), key=lambda item: item[1], reverse=True):
        print(f"  {symbol:<6} ${pl:,.2f}")

    print(f"\nBiggest win:  {biggest_win['symbol']} on {biggest_win['date'].date()} "
          f"-> ${biggest_win['profit_loss']:,.2f} "
          f"({biggest_win['quantity']:.0f} shares, bought ${biggest_win['buy_price']:.2f}, "
          f"sold ${biggest_win['sell_price']:.2f})")

    print(f"Biggest loss: {biggest_loss['symbol']} on {biggest_loss['date'].date()} "
          f"-> ${biggest_loss['profit_loss']:,.2f} "
          f"({biggest_loss['quantity']:.0f} shares, bought ${biggest_loss['buy_price']:.2f}, "
          f"sold ${biggest_loss['sell_price']:.2f})")

    # Let the user know about any positions still open (bought or sold
    # short but not yet closed), since those aren't included in the
    # P/L above.
    still_open_long = {symbol: lots for symbol, lots in open_long_lots.items() if lots}
    if still_open_long:
        print("\nLong positions still open (not included in profit/loss above):")
        for symbol, lots in still_open_long.items():
            total_shares = sum(lot["quantity"] for lot in lots)
            print(f"  {symbol:<6} {total_shares:.0f} shares still held")

    still_open_short = {symbol: lots for symbol, lots in open_short_lots.items() if lots}
    if still_open_short:
        print("\nShort positions still open (not included in profit/loss above):")
        for symbol, lots in still_open_short.items():
            total_shares = sum(lot["quantity"] for lot in lots)
            print(f"  {symbol:<6} {total_shares:.0f} shares still short")

    print("\nNote: this analysis uses share price only and does not")
    print("subtract small trading fees/commissions, so real totals may")
    print("differ by a few dollars.")
    print("=" * 60)


def main():
    csv_path = find_csv_file()
    print(f"Reading trades from: {csv_path.name}\n")
    transactions = load_transactions(csv_path)
    closed_trades, open_long_lots, open_short_lots = match_trades_lifo(transactions)
    build_report(closed_trades, open_long_lots, open_short_lots)


if __name__ == "__main__":
    main()
