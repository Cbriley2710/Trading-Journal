"""
Regression test for analyze_trades.load_transactions() (the Fidelity
CSV importer) - locks in a real production bug: it used to unpack each
row by FIXED COLUMN POSITION (`run_date, action, symbol, ... =
row[:7]`), assuming a header shaped like "Run Date,Action,Symbol,
Description,Type,Price ($),Quantity,...". Fidelity's actual export
gains two extra columns ("Account", "Account Number") once an account
has more than one linked account to report, shifting every column
after that point over by two - "Action" silently started reading
"Account"'s value ("Individual"/"PRE-TAX", never "YOU BOUGHT"/"YOU
SOLD"), and the entire file parsed as zero transactions with no error
at all. Fixed to read columns BY NAME instead (csv.DictReader, the
same approach load_transactions_schwab() already used) - these tests
cover both the OLD header shape (must keep working) and the NEW one
with Account/Account Number inserted (the actual bug).
"""
import tempfile
from pathlib import Path

from analyze_trades import load_transactions

OLD_FORMAT_CSV = """

Run Date,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),Fees ($),Accrued Interest ($),Amount ($),Settlement Date
07/29/2026,YOU SOLD CONCENTRA GROUP HOLDINGS (CON) (Margin),CON,CONCENTRA GROUP HOLDINGS,Margin,31.96,"-100","",0.07,"",3195.93,07/30/2026
07/28/2026,YOU BOUGHT CONCENTRA GROUP HOLDINGS (CON) (Margin),CON,CONCENTRA GROUP HOLDINGS,Margin,32.4,750,"","","","-24300",07/29/2026
"""

NEW_FORMAT_WITH_ACCOUNT_COLUMNS_CSV = """

Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),Fees ($),Accrued Interest ($),Amount ($),Settlement Date
07/29/2026,Individual,Z28684483,YOU SOLD CONCENTRA GROUP HOLDINGS PAREN (CON) (Margin),CON,CONCENTRA GROUP HOLDINGS PAREN COMMON STOCK,Margin,31.96,"-100","",0.07,"",3195.93,07/30/2026
07/29/2026,Individual,Z28684483,YOU SOLD CONCENTRA GROUP HOLDINGS PAREN (CON) (Margin),CON,CONCENTRA GROUP HOLDINGS PAREN COMMON STOCK,Margin,31.98,"-100","",0.07,"",3197.93,07/30/2026
07/28/2026,Individual,Z28684483,YOU BOUGHT CONCENTRA GROUP HOLDINGS PAREN (CON) (Margin),CON,CONCENTRA GROUP HOLDINGS PAREN COMMON STOCK,Margin,32.4,750,"","","","-24300",07/29/2026
08/03/2026,PRE-TAX,Z40428708,YOU BOUGHT PROSHARES ULTRAPRO QQQ (TQQQ) (Cash),TQQQ,PROSHARES ULTRAPRO QQQ,Cash,66.4,400,"","","","-26558.56",08/04/2026
"""

SHORT_SALE_CSV = """

Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),Fees ($),Accrued Interest ($),Amount ($),Settlement Date
07/30/2026,Individual,Z28684483,YOU SOLD SHORT SALE NEBIUS GROUP N V COM (NBIS) (Short),NBIS,NEBIUS GROUP N V COM,Short,191,"-700","",2.76,"",133697.31,07/31/2026
07/30/2026,Individual,Z28684483,YOU BOUGHT SHORT COVER NEBIUS GROUP N V COM (NBIS) (Short),NBIS,NEBIUS GROUP N V COM,Short,189.9,600,"","","","-113940",07/31/2026
07/30/2026,Individual,Z28684483,YOU SOLD NEBIUS GROUP N V COM (NBIS) (Margin),NBIS,NEBIUS GROUP N V COM,Margin,193.92,"-100","",0.4,"",19391.6,07/31/2026
07/30/2026,Individual,Z28684483,YOU BOUGHT NEBIUS GROUP N V COM (NBIS) (Margin),NBIS,NEBIUS GROUP N V COM,Margin,188.9,500,"","","","-94450",07/31/2026
"""


def _write_temp_csv(content):
    tmp_dir = Path(tempfile.mkdtemp())
    path = tmp_dir / "test_fidelity_export.csv"
    path.write_text(content, encoding="utf-8-sig")
    return str(path)


def test_old_format_without_account_columns_still_parses():
    path = _write_temp_csv(OLD_FORMAT_CSV)
    txns = load_transactions(path)
    assert len(txns) == 2
    sell = next(t for t in txns if t["action"] == "SELL")
    assert sell["symbol"] == "CON"
    assert sell["price"] == 31.96
    assert sell["quantity"] == 100.0
    buy = next(t for t in txns if t["action"] == "BUY")
    assert buy["quantity"] == 750.0


def test_new_format_with_account_columns_parses_correctly():
    """The actual bug: Account/Account Number columns pushed everything
    else over by two positions, previously producing zero transactions
    with no error - this now must correctly parse all 4 real rows,
    including across two DIFFERENT linked accounts (Individual,
    PRE-TAX)."""
    path = _write_temp_csv(NEW_FORMAT_WITH_ACCOUNT_COLUMNS_CSV)
    txns = load_transactions(path)
    assert len(txns) == 4

    con_sells = [t for t in txns if t["symbol"] == "CON" and t["action"] == "SELL"]
    assert len(con_sells) == 2
    prices = sorted(t["price"] for t in con_sells)
    assert prices == [31.96, 31.98]

    con_buy = next(t for t in txns if t["symbol"] == "CON" and t["action"] == "BUY")
    assert con_buy["quantity"] == 750.0

    tqqq = next(t for t in txns if t["symbol"] == "TQQQ")
    assert tqqq["action"] == "BUY"
    assert tqqq["quantity"] == 400.0


def test_short_sale_maps_to_sell_short_not_plain_sell():
    """The real NBIS bug: 'YOU SOLD SHORT SALE ...' (Type "Short") must
    map to SELL_SHORT so match_trades_lifo() can actually open a short
    position with it - a plain "SELL" only ever closes an existing LONG
    lot and would either wrongly consume a real long lot or report a
    phantom unmatched sell. 'YOU BOUGHT SHORT COVER ...' stays a plain
    BUY - match_trades_lifo()'s BUY handling already covers an open
    short first before opening a new long, so that half was never
    broken. A plain "YOU SOLD"/"YOU BOUGHT" (Margin, no short) must
    keep behaving exactly as before."""
    path = _write_temp_csv(SHORT_SALE_CSV)
    txns = load_transactions(path)
    assert len(txns) == 4

    short_sale = next(t for t in txns if t["price"] == 191.0)
    assert short_sale["action"] == "SELL_SHORT"
    assert short_sale["quantity"] == 700.0

    short_cover = next(t for t in txns if t["price"] == 189.9)
    assert short_cover["action"] == "BUY"

    plain_sell = next(t for t in txns if t["price"] == 193.92)
    assert plain_sell["action"] == "SELL"

    plain_buy = next(t for t in txns if t["price"] == 188.9)
    assert plain_buy["action"] == "BUY"
