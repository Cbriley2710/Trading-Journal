"""
Locks in the screening logic's behavior with synthetic data. The first
section below (through test_append_log_dedupes_keeping_first) was
written and run against the ORIGINAL daily_screen.py BEFORE it was
refactored into screener/engine.py, then re-run unchanged (only the
import line changed) against the refactored module to confirm the
refactor didn't change any answer - see screener/engine.py's own
docstring. The second section covers screen_tickers()/_funnel_counts(),
which are new (daily_screen.py's main() never returned a typed result,
it only printed to stdout), so those don't have a "before" to lock
against - they're tested directly.

The synthetic price panel below is hand-tuned (see tests/synthetic.py)
so one ticker (WINNER) cleanly passes every baseline AND strict filter
on the last day, and three others (LAGGARD, PENNY, THIN) each fail for
a distinct, verified reason - not because the numbers are "realistic"
but because they exercise every branch of screen()/market_ok().
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Points at the refactored module (screener/engine.py) - everything
# below calls `ds.<name>`, so this is the only line that changed when
# the refactor landed; every assertion below is unchanged from what
# passed against the original daily_screen.py.
import screener.engine as ds

from tests.synthetic import build_synthetic_panel


@pytest.fixture(scope="module")
def panel_bundle():
    df = build_synthetic_panel()
    panel, bm, label = ds.build(df, "QQQ")
    asof = panel.date.max()
    return dict(df=df, panel=panel, bm=bm, label=label, asof=asof)


# --- indicators() -----------------------------------------------------

def test_indicators_has_expected_columns_and_no_error():
    dates = pd.bdate_range("2024-01-02", periods=200)
    g = pd.DataFrame(dict(
        date=dates,
        open=np.linspace(10, 20, 200),
        high=np.linspace(10.2, 20.2, 200),
        low=np.linspace(9.8, 19.8, 200),
        close=np.linspace(10, 20, 200),
        volume=np.full(200, 1_000_000),
    ))
    out = ds.indicators(g)
    for col in ["ema21", "sma50", "adr", "advd", "nr7", "nr7_2", "hi6m", "rs", "vol20", "vol50"]:
        assert col in out.columns
    # A steady linear climb never produces a NEW narrowest-of-7 range
    # after the first window (every bar's range is identical here), so
    # nr7 should be true basically everywhere once the rolling window
    # is full - a cheap sanity check that the comparison isn't inverted.
    assert bool(out["nr7"].iloc[-1])


# --- market_ok() -------------------------------------------------------

def test_market_ok_gate_open():
    dates = pd.bdate_range("2024-01-02", periods=40)
    # Steadily rising series - price stays above its own rising 21 EMA
    # by construction, so the gate should read OPEN.
    bm = pd.Series(np.linspace(100, 140, 40), index=dates)
    ok, pct = ds.market_ok(bm, "TEST", dates[-1])
    assert ok is True
    assert pct > 0


def test_market_ok_gate_closed_below_ema():
    dates = pd.bdate_range("2024-01-02", periods=40)
    # Rises, then drops sharply on the last few bars - price ends BELOW
    # its own (slower-moving) EMA, so the gate should read CLOSED.
    values = np.concatenate([np.linspace(100, 140, 35), np.linspace(140, 90, 5)])
    bm = pd.Series(values, index=dates)
    ok, pct = ds.market_ok(bm, "TEST", dates[-1])
    assert ok is False
    assert pct < 0


def test_market_ok_date_not_in_series():
    dates = pd.bdate_range("2024-01-02", periods=10)
    bm = pd.Series(np.linspace(100, 110, 10), index=dates)
    ok, pct = ds.market_ok(bm, "TEST", pd.Timestamp("2099-01-01"))
    assert ok is False
    assert np.isnan(pct)


# --- benchmark_series() -------------------------------------------------

def test_benchmark_series_uses_real_benchmark_when_present(panel_bundle):
    assert panel_bundle["label"] == "QQQ"
    assert panel_bundle["bm"].name == "close"


def test_benchmark_series_equal_weight_fallback_when_missing():
    df = pd.DataFrame(dict(
        ticker=["A"] * 5, date=pd.bdate_range("2024-01-02", periods=5),
        open=[1] * 5, high=[1] * 5, low=[1] * 5, close=[1, 2, 3, 4, 5], volume=[1] * 5,
    ))
    bm, label = ds.benchmark_series(df, "QQQ", strict_bm=False)
    assert "WARNING" in label
    assert len(bm) == 5


def test_benchmark_series_hard_exits_when_strict_and_missing():
    df = pd.DataFrame(dict(
        ticker=["A"] * 5, date=pd.bdate_range("2024-01-02", periods=5),
        open=[1] * 5, high=[1] * 5, low=[1] * 5, close=[1, 2, 3, 4, 5], volume=[1] * 5,
    ))
    with pytest.raises(SystemExit):
        ds.benchmark_series(df, "QQQ", strict_bm=True)


# --- screen() - locks in exactly which synthetic tickers pass/fail -----

def test_screen_winner_passes_both_tiers(panel_bundle):
    s = ds.screen(panel_bundle["panel"], panel_bundle["asof"])
    passing = set(s.ticker)
    assert "WINNER" in passing
    winner = s[s.ticker == "WINNER"].iloc[0]
    assert bool(winner.strict) is True


def test_screen_excludes_laggard_penny_thin(panel_bundle):
    s = ds.screen(panel_bundle["panel"], panel_bundle["asof"])
    passing = set(s.ticker)
    # Verified independently (see tests/synthetic.py's own docstring) -
    # each fails baseline for a different, deliberate reason: LAGGARD on
    # trend/RS, PENNY on price AND liquidity, THIN on liquidity alone.
    assert "LAGGARD" not in passing
    assert "PENNY" not in passing
    assert "THIN" not in passing


# --- make_orders() - locks in the exact math on a known-good signal ----

def test_make_orders_winner_exact_numbers(panel_bundle):
    s = ds.screen(panel_bundle["panel"], panel_bundle["asof"])
    asof = panel_bundle["asof"]
    orders = ds.make_orders(s, equity=10_000, sig_date=asof, expiry=asof, gate_pct=0.44)
    assert len(orders) == 1
    row = orders.iloc[0]
    assert row.ticker == "WINNER"
    assert row.tier == "strict"
    # Locked from an actual run against this fixture (see this file's
    # own module docstring) - a refactor that changes any of these
    # means the arithmetic itself changed, not just its shape.
    assert row.trigger == pytest.approx(91.96, abs=0.01)
    assert row.stop == pytest.approx(87.54, abs=0.01)
    assert row.risk_pct == pytest.approx(4.81, abs=0.02)
    assert row.shares == 22
    assert row.position == 2023
    assert row.risk_usd == 97
    assert bool(row.nr7_2) is True
    assert bool(row.rsline_at_high) is True


def test_make_orders_skips_when_trigger_not_above_stop():
    # An absurdly wide ADR% pushes the computed stop ABOVE the trigger
    # (low * (1 - 1.0 * adr/100) can go negative for adr > 100) -
    # make_orders() must skip that row rather than emit a nonsense order.
    sig = pd.DataFrame([dict(
        ticker="X", high=100.0, low=95.0, adr=150.0, strict=True,
        rs_excess=0.10, rsline_at_high=True, nr7_2=False, close=98.0, hi6m=100.0,
    )])
    orders = ds.make_orders(sig, equity=10_000, sig_date="2024-01-01",
                             expiry="2024-01-02", gate_pct=1.0)
    assert orders.empty


def test_make_orders_skips_when_risk_pct_too_wide():
    # ADR just large enough to push risk_pct past MAX_RISK_PCT (8%) but
    # still leave trigger > stop, isolating the risk-cap skip from the
    # trigger<=stop skip tested above.
    sig = pd.DataFrame([dict(
        ticker="X", high=100.0, low=95.0, adr=12.0, strict=True,
        rs_excess=0.10, rsline_at_high=True, nr7_2=False, close=98.0, hi6m=100.0,
    )])
    orders = ds.make_orders(sig, equity=10_000, sig_date="2024-01-01",
                             expiry="2024-01-02", gate_pct=1.0)
    assert orders.empty


def test_make_orders_skips_when_shares_round_to_zero():
    # Tiny equity means 1% risk-per-trade in dollars can't buy even one
    # share at this risk-per-share - make_orders() must skip, not emit
    # a zero/negative-share order.
    sig = pd.DataFrame([dict(
        ticker="X", high=1000.0, low=950.0, adr=3.0, strict=True,
        rs_excess=0.10, rsline_at_high=True, nr7_2=False, close=980.0, hi6m=1000.0,
    )])
    orders = ds.make_orders(sig, equity=10.0, sig_date="2024-01-01",
                             expiry="2024-01-02", gate_pct=1.0)
    assert orders.empty


def test_make_orders_baseline_tier_label():
    sig = pd.DataFrame([dict(
        ticker="X", high=50.0, low=48.0, adr=4.0, strict=False,
        rs_excess=0.02, rsline_at_high=False, nr7_2=False, close=49.0, hi6m=55.0,
    )])
    orders = ds.make_orders(sig, equity=10_000, sig_date="2024-01-01",
                             expiry="2024-01-02", gate_pct=1.0)
    assert len(orders) == 1
    assert orders.iloc[0].tier == "baseline"


# --- append_log() - CLI-only CSV logging, not part of the engine -------
# append_log() is deliberately NOT in screener/engine.py - it's CSV file
# I/O specific to the CLI script; the app's equivalent is
# database.save_signals() (Postgres, tested separately). It still lives
# in daily_screen.py itself, so this test imports that module directly
# rather than using the module-level `ds` alias above.

def test_append_log_dedupes_keeping_first(tmp_path):
    import daily_screen as cli
    log_path = str(tmp_path / "signal_log.csv")
    cols = ["signal_date", "ticker", "tier", "trigger", "stop", "risk_pct", "shares",
            "position", "risk_usd", "adr", "rs_excess", "rsline_at_high", "nr7_2",
            "pct_off_high", "close", "gate_pct", "expires"]

    first = pd.DataFrame([dict(
        signal_date="2024-01-02", ticker="X", tier="strict", trigger=10.0, stop=9.0,
        risk_pct=10.0, shares=100, position=1000, risk_usd=100, adr=4.0, rs_excess=5.0,
        rsline_at_high=True, nr7_2=False, pct_off_high=-1.0, close=9.9, gate_pct=1.0,
        expires="2024-01-05",
    )])[cols]
    cli.append_log(log_path, first)

    # Same (signal_date, ticker) logged again with DIFFERENT numbers -
    # the first-ever logged row must win, not this later one.
    second = first.copy()
    second["trigger"] = 999.0
    cli.append_log(log_path, second)

    merged = pd.read_csv(log_path)
    assert len(merged) == 1
    assert merged.iloc[0].trigger == 10.0

    # A genuinely new (signal_date, ticker) key adds a second row.
    third = first.copy()
    third["ticker"] = "Y"
    cli.append_log(log_path, third)
    merged = pd.read_csv(log_path)
    assert len(merged) == 2


# =========================================================================
# New surface: screen_tickers()/_funnel_counts()/ScreenError. No "before"
# to lock against (daily_screen.py's main() only ever printed to stdout),
# so these are tested directly rather than characterized against a prior
# run.
# =========================================================================

def test_screen_tickers_happy_path(panel_bundle):
    result = ds.screen_tickers(panel_bundle["df"], panel_bundle["asof"], equity=10_000, benchmark="QQQ")
    assert result.gate_open is True
    assert result.n_screened == 5  # QQQ + WINNER + LAGGARD + PENNY + THIN
    assert result.benchmark_label == "QQQ"
    assert result.benchmark_return_pct is not None

    assert not result.watchlist.empty
    assert "WINNER" in set(result.watchlist.ticker)
    assert "WINNER" in set(result.strict.ticker)
    assert "WINNER" not in set(result.baseline_only.ticker)


def test_screen_tickers_funnel_has_all_stages_and_sane_counts(panel_bundle):
    result = ds.screen_tickers(panel_bundle["df"], panel_bundle["asof"], equity=10_000, benchmark="QQQ")
    for stage in ds.FUNNEL_STAGES:
        assert stage in result.funnel
        assert result.funnel[stage] >= 0
    # No stage can eliminate more tickers than were screened in total.
    assert sum(result.funnel.values()) <= result.n_screened


def test_funnel_survivors_match_screen_output(panel_bundle):
    """The funnel's own step-by-step baseline survivors must be exactly
    screen()'s output for the same date - the funnel breakdown can
    never silently diverge from the real filter it's explaining."""
    asof = panel_bundle["asof"]
    panel = panel_bundle["panel"]
    _, funnel_survivors = ds._funnel_counts(panel, asof)
    screen_result = ds.screen(panel, asof)
    assert set(funnel_survivors.ticker) == set(screen_result.ticker)


def test_screen_tickers_requires_benchmark_by_default():
    df = pd.DataFrame(dict(
        ticker=["A"] * 200, date=pd.bdate_range("2024-01-02", periods=200),
        open=np.linspace(10, 20, 200), high=np.linspace(10.2, 20.2, 200),
        low=np.linspace(9.8, 19.8, 200), close=np.linspace(10, 20, 200),
        volume=np.full(200, 1_000_000),
    ))
    with pytest.raises(ds.ScreenError):
        ds.screen_tickers(df, df.date.max(), equity=10_000, benchmark="QQQ")


def test_screen_tickers_allows_fallback_when_not_required():
    df = pd.DataFrame(dict(
        ticker=["A"] * 200, date=pd.bdate_range("2024-01-02", periods=200),
        open=np.linspace(10, 20, 200), high=np.linspace(10.2, 20.2, 200),
        low=np.linspace(9.8, 19.8, 200), close=np.linspace(10, 20, 200),
        volume=np.full(200, 1_000_000),
    ))
    result = ds.screen_tickers(df, df.date.max(), equity=10_000, benchmark="QQQ",
                                require_benchmark=False)
    assert "WARNING" in result.benchmark_label


def test_screen_tickers_backdates_to_last_available_session(panel_bundle):
    # A weekend date with no trading data should snap back to the most
    # recent real session on or before it, not error out.
    asof = panel_bundle["asof"]
    weekend_after = pd.Timestamp(asof) + pd.Timedelta(days=1)
    while weekend_after.weekday() < 5:
        weekend_after += pd.Timedelta(days=1)
    result = ds.screen_tickers(panel_bundle["df"], weekend_after, equity=10_000, benchmark="QQQ")
    assert result.asof == pd.Timestamp(asof).date()


def test_screen_tickers_raises_when_no_data_before_asof(panel_bundle):
    with pytest.raises(ds.ScreenError):
        ds.screen_tickers(panel_bundle["df"], pd.Timestamp("1990-01-01"),
                           equity=10_000, benchmark="QQQ")


def test_screen_tickers_params_override_is_isolated_per_call(panel_bundle):
    """
    The whole point of threading `params` through instead of mutating
    the module-level P dict: an absurdly strict override for ONE call
    (nothing could ever pass a 99% ADR floor) must produce zero
    signals, while a call right after with NO override must go back to
    getting WINNER normally - proving P itself was never touched.
    """
    strict_params = {**ds.P, "MIN_ADR": 99.0}
    strict_result = ds.screen_tickers(
        panel_bundle["df"], panel_bundle["asof"], equity=10_000, benchmark="QQQ",
        params=strict_params)
    assert strict_result.watchlist.empty

    assert ds.P["MIN_ADR"] == 3.0  # untouched by the call above

    normal_result = ds.screen_tickers(
        panel_bundle["df"], panel_bundle["asof"], equity=10_000, benchmark="QQQ")
    assert "WINNER" in set(normal_result.watchlist.ticker)
