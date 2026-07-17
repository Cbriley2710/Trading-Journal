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


def match_trades_fifo(transactions):
    """
    Matches each SELL to the oldest available BUY shares for that same
    ticker (FIFO), and calculates the profit or loss for each matched
    portion.

    Returns a list of "closed trades", each a dictionary like:
        {"symbol": "AAPL", "buy_price": 150.0, "sell_price": 155.0,
         "quantity": 100, "profit_loss": 500.0, "date": datetime}
    """
    # First, combine any partial fills (see merge_partial_fills above)
    # so a single order doesn't get counted as several tiny trades.
    transactions = merge_partial_fills(transactions)

    # Sort transactions oldest-first so we process them in the order
    # they actually happened. Python's sort keeps same-day rows in
    # their original order, which in this file is already
    # chronological (buys before sells on the same day).
    transactions = sorted(transactions, key=lambda t: t["date"])

    # For each ticker, we keep a "line" (list) of purchase batches that
    # haven't been fully sold yet. Each batch remembers its price and
    # how many shares are left in it.
    open_lots = {}  # example: {"AAPL": [{"price": 150.0, "quantity": 100}]}

    closed_trades = []

    for t in transactions:
        symbol = t["symbol"]
        open_lots.setdefault(symbol, [])

        if t["action"] == "BUY":
            open_lots[symbol].append({
                "price": t["price"],
                "quantity": t["quantity"],
                "date": t["date"],
            })

        else:  # SELL
            shares_to_sell = t["quantity"]

            while shares_to_sell > 0:
                if not open_lots[symbol]:
                    # There's a sell with no matching buy on record
                    # (e.g. a short sale, or a trade from before this
                    # file's date range). We can't calculate a
                    # profit/loss for it, so we skip it and move on.
                    print(f"Note: skipping unmatched sell of {shares_to_sell} "
                          f"{symbol} shares on {t['date'].date()} (no buy found).")
                    break

                oldest_lot = open_lots[symbol][0]
                matched_quantity = min(oldest_lot["quantity"], shares_to_sell)

                profit_loss = (t["price"] - oldest_lot["price"]) * matched_quantity

                closed_trades.append({
                    "symbol": symbol,
                    "buy_price": oldest_lot["price"],
                    "sell_price": t["price"],
                    "quantity": matched_quantity,
                    "profit_loss": profit_loss,
                    "entry_date": oldest_lot["date"],
                    "date": t["date"],
                })

                oldest_lot["quantity"] -= matched_quantity
                shares_to_sell -= matched_quantity

                if oldest_lot["quantity"] == 0:
                    open_lots[symbol].pop(0)

    return closed_trades, open_lots


def build_report(closed_trades, open_lots):
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

    # Let the user know about any shares still being held (bought but
    # not yet sold), since those aren't included in the P/L above.
    still_open = {symbol: lots for symbol, lots in open_lots.items() if lots}
    if still_open:
        print("\nPositions still open (not included in profit/loss above):")
        for symbol, lots in still_open.items():
            total_shares = sum(lot["quantity"] for lot in lots)
            print(f"  {symbol:<6} {total_shares:.0f} shares still held")

    print("\nNote: this analysis uses share price only and does not")
    print("subtract small trading fees/commissions, so real totals may")
    print("differ by a few dollars.")
    print("=" * 60)


def main():
    csv_path = find_csv_file()
    print(f"Reading trades from: {csv_path.name}\n")
    transactions = load_transactions(csv_path)
    closed_trades, open_lots = match_trades_fifo(transactions)
    build_report(closed_trades, open_lots)


if __name__ == "__main__":
    main()
