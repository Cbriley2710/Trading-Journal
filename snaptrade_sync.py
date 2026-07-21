"""
SnapTrade Sync
=====================
Pulls your Fidelity trade history straight from SnapTrade (a service
that connects to your brokerage on your behalf) instead of you having
to manually export and upload a CSV every time. See:
  - pages/0_Import_Trades.py for the one-time "Connect Fidelity"
    link and the "Sync Now" button.
  - snaptrade_daily_sync.py for the automatic version of that same
    sync, run once a day after market close.

HOW THE CONNECTION WORKS: you connect your Fidelity account ONCE,
through SnapTrade's own "Connection Portal" - a webpage hosted by
SnapTrade, not this app. You log into Fidelity directly on Fidelity's
own site from there, so your Fidelity password never passes through
SnapTrade or this app at all - only a read-only access token does
(see get_connection_portal_url() below for why it's read-only).

WHERE CREDENTIALS LIVE: this app's own SnapTrade API key
(SNAPTRADE_CLIENT_ID / SNAPTRADE_CONSUMER_KEY in st.secrets, same
gitignored-secrets pattern as DATABASE_URL - see database.py) is what
identifies THIS APP to SnapTrade. It's a "Personal" API key (see
docs.snaptrade.com/docs/terminology), meaning it represents you
directly - unlike a commercial integration managing many separate
end-users, there's no separate userId/userSecret to register or store
on top of it, so every call below just omits those.
"""

import os
from datetime import datetime

import streamlit as st
from snaptrade_client import SnapTrade

import timeutil


def _get_secret(key):
    """
    Same fallback pattern as database.py's _get_database_url(): reads
    st.secrets first (how the deployed app, and any `streamlit run`
    command, reads .streamlit/secrets.toml), then a plain environment
    variable (how snaptrade_daily_sync.py's GitHub Actions job reads
    it - there's no secrets.toml file in that environment, just a
    repository secret set as an env var).
    """
    try:
        return st.secrets[key]
    except Exception:
        pass

    value = os.environ.get(key)
    if value:
        return value

    raise RuntimeError(
        f"No {key} found. Copy .streamlit/secrets.toml.example to "
        ".streamlit/secrets.toml and fill in your SnapTrade Personal "
        f"API key (or set a {key} environment variable)."
    )


def _get_client():
    """Builds the SnapTrade API client from your secrets."""
    return SnapTrade(
        client_id=_get_secret("SNAPTRADE_CLIENT_ID"),
        consumer_key=_get_secret("SNAPTRADE_CONSUMER_KEY"),
    )


def _find_broker_slug(name_contains):
    """
    Looks up a brokerage's SnapTrade "slug" (the short code the
    Connection Portal expects, e.g. for Fidelity) by matching against
    its display name - rather than hardcoding a guessed slug string
    here, which could quietly be wrong. Returns None (falls back to
    the portal's own brokerage picker) if nothing matches.
    """
    client = _get_client()
    response = client.reference_data.list_all_brokerages()
    for brokerage in response.body:
        display_name = (brokerage.get("display_name") or brokerage.get("name") or "")
        if name_contains.lower() in display_name.lower():
            return brokerage.get("slug")
    return None


def get_connection_portal_url(broker="Fidelity", reconnect=None):
    """
    Returns a one-time link to SnapTrade's Connection Portal - open it
    in a browser to connect (or, if `reconnect` is a connection ID,
    reconnect) a brokerage account.

    Read-only on purpose (connection_type="read"): this app only ever
    reads trade history, it never places trades, so there's no reason
    to grant more access than that. A read-only connection means that
    even in a worst case - SnapTrade compromised, or this app's own
    API key leaked - whatever's exposed can't be used to move money or
    place orders.

    `broker` is looked up by name via _find_broker_slug() so the
    portal jumps straight to Fidelity's login step instead of showing
    the full brokerage picker; pass None to always show the picker.
    """
    slug = _find_broker_slug(broker) if broker else None
    client = _get_client()
    response = client.authentication.login_snap_trade_user(
        broker=slug, connection_type="read", reconnect=reconnect,
    )
    return response.body["redirectURI"]


def list_connected_accounts():
    """
    Returns every brokerage account currently connected - each a dict
    with at least "id", "name", and "institution_name". Used to find
    the account_id(s) fetch_activities() needs, and to show what's
    already connected on the Import Trades page.
    """
    client = _get_client()
    response = client.account_information.list_user_accounts()
    return list(response.body)


def fetch_activities(start_date, end_date):
    """
    Pulls every BUY/SELL activity across all connected accounts
    between start_date and end_date (inclusive, both plain `date`
    objects), already normalized into the exact same shape
    analyze_trades.py's load_transactions()/load_transactions_schwab()
    produce:
        {"date": datetime, "symbol": "AAPL", "action": "BUY",
         "price": 150.0, "quantity": 100}
    so the result can go straight into database.py's shared insert
    helper alongside CSV-imported rows, with the exact same
    duplicate-safe behavior (see database._insert_transactions()).

    Option activity is skipped (option_symbol is only set on an option
    trade) - this project tracks stocks only, the same exclusion the
    CSV importers already apply for symbols starting with "-".

    Note: SnapTrade's transaction data is cached and refreshed once a
    day, about a day behind - a trade made today typically won't show
    up here until tomorrow, same as the old manual CSV-export
    workflow already was.
    """
    client = _get_client()
    transactions = []

    for account in list_connected_accounts():
        offset = 0
        while True:
            response = client.account_information.get_account_activities(
                account_id=account["id"], start_date=start_date, end_date=end_date,
                type="BUY,SELL", offset=offset, limit=1000,
            )
            rows = list(response.body.get("data", []))

            for row in rows:
                if row.get("option_symbol"):
                    continue  # an option trade, not a stock - not tracked here

                symbol = row.get("symbol")
                if not symbol or not symbol.get("symbol"):
                    continue  # no ticker attached - not a trade we can match

                trade_date = row.get("trade_date")
                if not trade_date:
                    continue
                if isinstance(trade_date, str):
                    trade_date = datetime.fromisoformat(trade_date.replace("Z", "+00:00"))
                # SnapTrade timestamps come back in UTC - converted to
                # Eastern (like everything else in this app, see
                # timeutil.py) before dropping the time-of-day, so a
                # trade late in the evening ET doesn't get filed under
                # the wrong calendar day.
                if trade_date.tzinfo is not None:
                    trade_date = trade_date.astimezone(timeutil.EASTERN).replace(tzinfo=None)

                transactions.append({
                    "date": trade_date,
                    "symbol": symbol["symbol"],
                    "action": row["type"],  # already "BUY" or "SELL", matching the CSV importers
                    "price": float(row["price"]),
                    "quantity": abs(float(row["units"])),
                })

            if len(rows) < 1000:
                break  # last page for this account
            offset += 1000

    return transactions
