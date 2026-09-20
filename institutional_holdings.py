"""
Institutional Holdings
=====================
Fetches SEC Form 13F filings for the curated list of hedge funds in the
`hedge_funds` table (see database.py) and figures out, for any ticker
you type in on pages/8_Institutional_Holdings.py, which of those funds
hold it - the data behind the "9 of 15 funds hold this" checklist.

WHAT A 13F ACTUALLY IS: every institutional manager with over $100M in
US equities must file one every quarter, listing their long stock
positions AS OF the quarter-end date (Mar/Jun/Sep/Dec 31) - filed within
45 days after. It's a snapshot, not a list of trades: there's no
purchase date, no short positions, and no trades that happened and
reversed within the same quarter. "This fund added shares" always means
"their share count went up between two quarterly snapshots," never "on
this specific day."

A 13F identifies each stock by CUSIP (a 9-character security id), never
by ticker - so a second step (resolve_tickers(), via the free OpenFIGI
API) is needed to turn "037833100" into "AAPL". Results are cached in
the cusip_ticker_map table so the same CUSIP is never looked up twice.

ONLY EACH FUND'S TOP _MAX_POSITIONS_PER_FUND POSITIONS (BY DOLLAR VALUE)
ARE KEPT - a real, measured constraint discovered while building this:
a systematic/quant multi-strat fund's 13F can list TENS OF THOUSANDS of
tiny positions (AQR Capital Management's latest info table alone is
13MB - tens of thousands of rows), because 13F rules require listing
every distinct security a fund holds even a few shares of. Resolving
every one of those through a free, rate-limited ticker-lookup API would
take hours per fund and would likely get the whole app blocked. Capping
to each fund's biggest positions is also the RIGHT behavior, not just a
workaround: a $4,000 position buried in a 30,000-line quant basket was
never a real "conviction" signal anyway - the checklist is meant to
answer "does this fund meaningfully hold this stock," and that's
answered by its largest positions, not its smallest. The trade-off is
explicit: a fund holding a stock ONLY as a sub-$-threshold position
outside its top {N} will show as "not held" here even though a full,
unfiltered 13F would show a tiny position - see the caption on
pages/8_Institutional_Holdings.py, which says this out loud.

SEC's servers require a descriptive User-Agent header on every request
(not optional - undescriptive requests get blocked) - see _HEADERS
below.
"""

import time
from datetime import datetime

import requests
from lxml import etree

import database

_HEADERS = {"User-Agent": "Stock Data Analyzer contact:connorbriley1@gmail.com"}

_INFOTABLE_NS = "http://www.sec.gov/edgar/document/thirteenf/informationtable"

# Only each fund's biggest positions (by dollar value) are kept - see the
# module docstring above for why. 300 comfortably covers anything a
# fund actually has real conviction in, while keeping the total number
# of CUSIPs needing a ticker lookup manageable across 20 funds.
_MAX_POSITIONS_PER_FUND = 300


def find_cik_candidates(name):
    """
    Looks up candidate SEC filers for a fund name, for the "Manage
    Tracked Funds" add form. Uses SEC's full-text search (which matches
    against actual filed documents) rather than the older company-name
    search, because a fund's SEC-registered filer name doesn't always
    match its public brand name - e.g. Balyasny Asset Management files
    its 13F under "Longaeva Partners L.P.", which a plain name search
    for "Balyasny" never finds.

    Returns a list of {cik, display_name} dicts, most relevant first,
    deduplicated by CIK (a fund shows up once per past filing otherwise).
    """
    response = requests.get(
        "https://efts.sec.gov/LATEST/search-index",
        params={"q": name, "forms": "13F-HR"},
        headers=_HEADERS,
        timeout=15,
    )
    response.raise_for_status()
    hits = response.json().get("hits", {}).get("hits", [])

    seen_ciks = set()
    candidates = []
    for hit in hits:
        source = hit["_source"]
        cik = source["ciks"][0]
        if cik in seen_ciks:
            continue
        seen_ciks.add(cik)
        # display_names looks like "Longaeva Partners L.P.  (CIK 0002054122)"
        display_name = source["display_names"][0].split("  (CIK")[0].strip()
        # cik as stored by SEC always has leading zeros to 10 digits -
        # keep it that way, it's what the submissions API expects.
        candidates.append({"cik": cik.zfill(10), "display_name": display_name})
    return candidates


def _recent_13f_filings(cik, limit=8):
    """
    Returns up to `limit` (accession_number, quarter_end, filed_date)
    tuples for a fund's most recent 13F-HR ("holdings report") filings,
    newest quarter first - the last two years' worth by default (13F is
    quarterly, so 8 filings = ~2 years of history to actually compare
    trends against, not just one quarter's snapshot).

    Deliberately skips 13F-NT ("notice") filings - those are filed by a
    related entity that reports its holdings through a DIFFERENT filer
    instead (see D. E. Shaw's setup: two of its three SEC entities file
    13F-NT pointing back to the one that actually lists holdings) - an
    NT filing has no positions in it at all. Also skips 13F-HR/A
    (amendments) for simplicity - the rare correction isn't worth the
    added complexity of reconciling it against the original.

    SEC's submissions.json only holds a filer's most recent ~1000
    filings of ANY type in `filings.recent` - for a fund that also
    files many non-13F disclosures, its 13F-HR filings could in theory
    fall outside that window sooner than 2 years back, in which case
    this simply returns fewer than `limit`.
    """
    response = requests.get(
        f"https://data.sec.gov/submissions/CIK{cik}.json",
        headers=_HEADERS,
        timeout=15,
    )
    response.raise_for_status()
    recent = response.json()["filings"]["recent"]

    filings = []
    for form, accession, period, filed in zip(
        recent["form"], recent["accessionNumber"], recent["reportDate"], recent["filingDate"]
    ):
        if form != "13F-HR":
            continue
        filings.append((
            accession,
            datetime.strptime(period, "%Y-%m-%d").date(),
            datetime.strptime(filed, "%Y-%m-%d").date(),
        ))
        if len(filings) == limit:
            break
    return filings


def _fetch_infotable(cik, accession):
    """
    Downloads and parses the actual holdings list (the "information
    table") for one 13F-HR filing, found via the filing's index.json
    rather than assuming an exact filename.

    A REAL BUG lived here: this used to look for "infotable" in the
    filename, which works for most filers (form13fInfoTable.xml) but
    NOT all - Millennium's filings are named e.g.
    "MLP_Filing_20260630.xml", named by whatever filing-agent software
    that manager's law firm/admin uses, which varies by filer AND can
    even vary by year for the SAME filer. That mismatch made this
    silently return [] for Millennium, Renaissance, Viking, Balyasny,
    and Baupost's filings, and for older filings of a few others
    (Citadel, Elliott, Two Sigma) - which then got saved as a
    quarter with zero holdings and reported as a successful fetch, with
    no error anywhere. Caught by manually sanity-checking stored
    quarter counts after a backfill, not by any exception.

    The fix: every 13F-HR filing has exactly ONE other XML document
    besides primary_doc.xml (the cover page, always tiny, a few KB) -
    that's the actual holdings list, and it is always by far the
    largest file in the filing regardless of what it's named. Picking
    the biggest non-cover-page XML is robust to any naming convention.
    """
    accession_nodash = accession.replace("-", "")
    cik_int = int(cik)
    index = requests.get(
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/index.json",
        headers=_HEADERS,
        timeout=15,
    )
    index.raise_for_status()
    items = index.json()["directory"]["item"]
    candidate_items = [
        item for item in items
        if item["name"].lower().endswith(".xml") and item["name"].lower() != "primary_doc.xml"
    ]
    if not candidate_items:
        return []
    infotable_name = max(candidate_items, key=lambda item: int(item.get("size") or 0))["name"]

    xml_response = requests.get(
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{infotable_name}",
        headers=_HEADERS,
        timeout=15,
    )
    xml_response.raise_for_status()
    return parse_infotable_xml(xml_response.content)


def parse_infotable_xml(xml_bytes):
    """
    Parses a 13F information-table XML document (the actual holdings
    list, separate from the filing's primary_doc.xml cover page) into a
    list of {cusip, issuer_name, shares, value_usd} dicts. Split out
    from _fetch_infotable() so a test can feed this a saved sample file
    without needing a live SEC request.
    """
    root = etree.fromstring(xml_bytes)
    ns = {"n": _INFOTABLE_NS}

    holdings = []
    for row in root.findall(".//n:infoTable", ns):
        cusip = row.findtext("n:cusip", namespaces=ns)
        issuer_name = row.findtext("n:nameOfIssuer", namespaces=ns)
        shares = row.findtext("n:shrsOrPrnAmt/n:sshPrnamt", namespaces=ns)
        value_dollars = row.findtext("n:value", namespaces=ns)
        if not cusip or shares is None or value_dollars is None:
            continue
        holdings.append({
            "cusip": cusip,
            "issuer_name": issuer_name or cusip,
            "shares": int(shares),
            # SEC's pre-2023 13F schema reported this in THOUSANDS of
            # dollars - a real bug here originally assumed that and
            # multiplied by 1000, which was only caught by sanity-
            # checking an implied fund total against real-world scale
            # (it came out to hundreds of TRILLIONS of dollars, more
            # than the entire global stock market). Every filing this
            # app fetches is current (this XML schema), which reports
            # whole dollars directly - no conversion needed.
            "value_usd": int(value_dollars),
        })
    return holdings


def _combine_duplicate_cusips(holdings):
    """
    A fund can legally report the SAME CUSIP as more than one row in a
    13F (e.g. split by voting authority - sole/shared/none) - without
    combining those first, whichever row happened to be inserted last
    would silently overwrite the others (hedge_fund_holdings only keeps
    one row per fund+cusip+quarter), understating that position's true
    share count.
    """
    combined = {}
    for h in holdings:
        if h["cusip"] in combined:
            combined[h["cusip"]]["shares"] += h["shares"]
            combined[h["cusip"]]["value_usd"] += h["value_usd"]
        else:
            combined[h["cusip"]] = dict(h)
    return list(combined.values())


def resolve_tickers(conn, cusips):
    """
    Looks up whichever of these CUSIPs haven't been resolved before via
    OpenFIGI's free public mapping API (no key needed at this volume -
    a handful of batches per refresh), and caches every result
    (including "no match found") in cusip_ticker_map. Returns nothing -
    callers re-read tickers from the cache via database.get_cusip_tickers().

    A network error or a non-200 response is skipped WITHOUT caching a
    None for that CUSIP, so a transient OpenFIGI hiccup gets retried on
    the next refresh instead of being remembered as "no ticker exists."
    """
    already_known = database.get_cusip_tickers(conn, cusips)
    unresolved = [c for c in cusips if c not in already_known]
    if not unresolved:
        return

    # OpenFIGI's public (no API key) tier is rate-limited and caps each
    # request's job list - batching in small groups with a short pause
    # keeps this comfortably under that limit for the couple hundred
    # CUSIPs 20 funds' worth of large-cap holdings realistically produce.
    batch_size = 10
    for i in range(0, len(unresolved), batch_size):
        batch = unresolved[i : i + batch_size]
        try:
            response = requests.post(
                "https://api.openfigi.com/v3/mapping",
                json=[{"idType": "ID_CUSIP", "idValue": cusip} for cusip in batch],
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            response.raise_for_status()
            results = response.json()
        except requests.RequestException:
            continue

        resolved_batch = {}
        for cusip, result in zip(batch, results):
            data = result.get("data") or []
            # Several rows come back per CUSIP (one per exchange listing)
            # - prefer the US composite listing, since that's the plain
            # ticker (e.g. "EQIX") used everywhere else in this app.
            us_match = next((d for d in data if d.get("exchCode") == "US"), None)
            match = us_match or (data[0] if data else None)
            resolved_batch[cusip] = match["ticker"] if match else None
        database.save_cusip_tickers(conn, resolved_batch)
        time.sleep(0.3)


def _quarter_label(quarter_end):
    return f"Q{(quarter_end.month - 1) // 3 + 1} {quarter_end.year}"


def _fetch_and_save_one_quarter(conn, fund, accession, quarter_end, filed_date):
    """The actual fetch-parse-cap-resolve-save pipeline for ONE 13F
    filing, shared by refresh_fund()'s backfill loop below."""
    holdings = _fetch_infotable(fund["sec_cik"], accession)
    if not holdings:
        # A real, once-tracked-companies-sized fund reporting ZERO
        # positions is not a real outcome - it means _fetch_infotable
        # couldn't find/parse the actual holdings document (see its
        # docstring for the exact bug this caught). Raising here turns
        # that into a visible "error" status instead of a silently
        # "successful" empty quarter - see database.save_fund_holdings()
        # call site below, which would otherwise happily save nothing
        # and report success.
        raise ValueError(f"parsed zero holdings for accession {accession} - likely a file-detection failure")
    holdings = _combine_duplicate_cusips(holdings)
    # The TRUE total across every position this fund reported - computed
    # BEFORE trimming to the top 300, so "% of portfolio" stays accurate
    # even for a position that (rightly) got cut for being too small to
    # individually track.
    fund_total_value_usd = sum(h["value_usd"] for h in holdings)
    # Keep only the biggest positions - see _MAX_POSITIONS_PER_FUND's
    # docstring note above for why this is necessary, not just a shortcut.
    holdings.sort(key=lambda h: h["value_usd"], reverse=True)
    holdings = holdings[:_MAX_POSITIONS_PER_FUND]

    resolve_tickers(conn, [h["cusip"] for h in holdings])
    ticker_map = database.get_cusip_tickers(conn, [h["cusip"] for h in holdings])
    for h in holdings:
        h["ticker"] = ticker_map.get(h["cusip"])

    database.save_fund_holdings(conn, fund["id"], quarter_end, filed_date, holdings, fund_total_value_usd)


def refresh_fund(conn, fund, quarters_to_keep=8):
    """
    Brings one fund's stored history up to its `quarters_to_keep` most
    recent 13F-HR filings (2 years' worth by default) - fetching
    whichever of those quarters aren't already stored, and resolving
    any newly-seen CUSIPs to tickers along the way. Already-stored
    quarters are never re-fetched, so a routine refresh (everything
    already backfilled, just checking for a newly-filed quarter) only
    ever does the one new quarter's worth of work, not all 8 again.

    Returns a short status string for the refresh summary shown on the
    page: "up to date", "fetched N quarter(s): Q<n> <year>, ...", or
    "no 13F-HR filing found" / "error: <message>".
    """
    try:
        filings = _recent_13f_filings(fund["sec_cik"], limit=quarters_to_keep)
    except requests.RequestException as e:
        return f"error: {e}"

    if not filings:
        return "no 13F-HR filing found"

    already_have = set(database.get_fund_quarters(conn, fund["id"], limit=quarters_to_keep))
    fetched = []
    for accession, quarter_end, filed_date in filings:
        if quarter_end in already_have:
            continue
        try:
            _fetch_and_save_one_quarter(conn, fund, accession, quarter_end, filed_date)
        except (requests.RequestException, ValueError) as e:
            # Report what DID get fetched before the failure, rather than
            # losing that progress from the status message entirely.
            done = ", ".join(_quarter_label(q) for q in fetched) or "none"
            return f"error fetching {_quarter_label(quarter_end)} (fetched so far: {done}): {e}"
        fetched.append(quarter_end)

    if not fetched:
        return "up to date"
    return f"fetched {len(fetched)} quarter(s): " + ", ".join(_quarter_label(q) for q in sorted(fetched))


def refresh_all_funds(conn):
    """
    Refreshes every active tracked fund - called from the "Refresh
    Holdings Data" button. Returns {fund_display_name: status} so the
    page can show exactly what happened per fund (most funds will say
    "up to date" outside of the ~2-week window after each quarter's
    filings come in). A short pause between funds keeps this well
    within SEC's request-rate expectations.
    """
    results = {}
    for fund in database.get_hedge_funds(conn, active_only=True):
        results[fund["display_name"]] = refresh_fund(conn, fund)
        time.sleep(0.2)
    return results


def _classify_change(current, previous):
    """
    Compares one position's current-quarter row to its previous-quarter
    row (either may be None) and returns (change, change_pct):
      - current only            -> ("New", None) - can't quantify a % increase from zero
      - current and previous    -> ("Increased"/"Decreased"/"Unchanged", the real %change in shares)
      - previous only           -> ("Closed", -100.0) - the whole position is gone
      - neither                 -> (None, None)
    """
    if current is not None and previous is None:
        return "New", None
    if current is not None and previous is not None:
        if current["shares"] > previous["shares"]:
            change = "Increased"
        elif current["shares"] < previous["shares"]:
            change = "Decreased"
        else:
            change = "Unchanged"
        change_pct = (
            (current["shares"] - previous["shares"]) / previous["shares"] * 100
            if previous["shares"] else None
        )
        return change, change_pct
    if current is None and previous is not None:
        return "Closed", -100.0
    return None, None


def _portfolio_pct(holding):
    """What % of the fund's TOTAL 13F value this one position is - None
    if the fund's total isn't known (only true for data fetched before
    this was tracked)."""
    if holding is None or not holding.get("fund_total_value_usd"):
        return None
    return holding["value_usd"] / holding["fund_total_value_usd"] * 100


def get_snapshot_for_ticker(conn, ticker):
    """
    For every active tracked fund, works out whether it holds `ticker`
    as of its most recently fetched quarter, and how that compares to
    the quarter before. Returns a list of dicts, one per fund:

        {fund, held, shares, value_usd, portfolio_pct, quarter_end,
         change, change_pct, stale}

    `change`/`change_pct` come from _classify_change() above. `stale`
    is True when this fund's latest fetched quarter is older than the
    newest quarter ANY tracked fund has reported - i.e. this fund
    hasn't filed its most recent 13F yet (or filed late), so its row
    here may be a quarter behind everyone else's.
    """
    ticker = ticker.strip().upper()
    newest_quarter = database.get_newest_quarter_across_funds(conn)
    snapshot = []
    for fund in database.get_hedge_funds(conn, active_only=True):
        quarters = database.get_fund_quarters(conn, fund["id"], limit=2)
        row = {
            "fund": fund["display_name"], "held": False, "shares": None,
            "value_usd": None, "portfolio_pct": None,
            "quarter_end": quarters[0] if quarters else None,
            "change": None, "change_pct": None,
            "stale": bool(quarters and newest_quarter and quarters[0] < newest_quarter),
        }
        if not quarters:
            snapshot.append(row)
            continue

        # Targeted query - only ever fetches the (at most 2) rows where
        # THIS fund held THIS ticker, instead of its whole ~300-position
        # list per quarter (get_fund_holdings()) filtered down in Python.
        matches = database.get_fund_holding_for_ticker_in_quarters(conn, fund["id"], ticker, quarters)
        current = matches.get(quarters[0])
        previous_match = matches.get(quarters[1]) if len(quarters) > 1 else None

        if current is not None:
            row.update(held=True, shares=current["shares"], value_usd=current["value_usd"],
                       portfolio_pct=_portfolio_pct(current))
        row["change"], row["change_pct"] = _classify_change(current, previous_match)

        snapshot.append(row)
    return snapshot


def get_recent_moves(conn):
    """
    Across every active tracked fund, every position whose share count
    changed between that fund's two most recently fetched quarters -
    the data behind the "Recent Moves" discovery tab (as opposed to
    looking up one ticker you already have in mind). A fund with only
    ONE fetched quarter so far contributes nothing here - "New" would
    be trivially true for every single position on a first-ever fetch,
    which isn't a real signal, just an empty baseline.

    Returns a list of dicts: {fund, ticker, issuer_name, shares,
    value_usd, portfolio_pct, change, change_pct, quarter_end, stale},
    one row per position that changed - "Unchanged" positions are left
    out entirely (there'd be thousands of them, and "still holds it" is
    not a move). Unsorted - the page sorts/filters for display.
    """
    newest_quarter = database.get_newest_quarter_across_funds(conn)
    moves = []
    for fund in database.get_hedge_funds(conn, active_only=True):
        quarters = database.get_fund_quarters(conn, fund["id"], limit=2)
        if len(quarters) < 2:
            continue

        latest_holdings = {h["cusip"]: h for h in database.get_fund_holdings(conn, fund["id"], quarters[0])}
        previous_holdings = {h["cusip"]: h for h in database.get_fund_holdings(conn, fund["id"], quarters[1])}
        stale = bool(newest_quarter and quarters[0] < newest_quarter)

        for cusip in set(latest_holdings) | set(previous_holdings):
            current = latest_holdings.get(cusip)
            previous = previous_holdings.get(cusip)
            change, change_pct = _classify_change(current, previous)
            if change in (None, "Unchanged"):
                continue

            reference = current or previous  # Closed positions have no `current` row to read issuer/ticker from
            moves.append({
                "fund": fund["display_name"],
                "ticker": reference["ticker"],
                "issuer_name": reference["issuer_name"],
                "shares": current["shares"] if current else 0,
                "value_usd": current["value_usd"] if current else 0,
                "portfolio_pct": _portfolio_pct(current) if current else _portfolio_pct(previous),
                "change": change,
                "change_pct": change_pct,
                "quarter_end": quarters[0],
                "stale": stale,
            })
    return moves


def get_ticker_history(conn, ticker):
    """
    The full multi-quarter trend behind the History tab: for `ticker`,
    every active fund that held it in at least one of its stored
    quarters (up to 8), with its % of portfolio for each such quarter -
    the deeper complement to get_snapshot_for_ticker()'s latest-vs-
    previous-quarter-only view.

    Returns (quarters, rows):
      - quarters: every distinct quarter_end across all tracked funds,
        oldest first - the grid's column headers. A fund with a
        shorter real filing history (e.g. Balyasny) just won't have an
        entry for an earlier quarter.
      - rows: [{fund, values: {quarter_end: portfolio_pct}}] - a fund
        that never held this ticker in any stored quarter is left out
        entirely, not shown with a row of dashes.
    """
    ticker = ticker.strip().upper()
    quarters = sorted(database.get_all_recent_quarters(conn))
    rows = []
    for fund in database.get_hedge_funds(conn, active_only=True):
        values = database.get_fund_ticker_history(conn, fund["id"], ticker)
        if values:
            rows.append({"fund": fund["display_name"], "values": values})
    return quarters, rows
