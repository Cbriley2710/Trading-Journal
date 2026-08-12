"""
Tests for narrative_generator.py - the Market Context page's
auto-generated Daily Narrative (see pages/8_Market_Context.py). The
Anthropic client and market_context.compute_context() are both faked
out (no real network/database access) so these run as plain unit
tests, matching this project's tests/test_charting.py pattern for
another module that talks to an external service.
"""
from datetime import date

import anthropic
import httpx
import pytest

import narrative_generator


def _fake_context(rows=None):
    return {
        "asof": date(2026, 8, 12), "gate_open": True, "gate_pct": 1.5,
        "rows": rows or [], "summary": None, "failed": [], "insufficient": {},
    }


def _fake_row(symbol="AAPL", source="List 1"):
    return {
        "symbol": symbol, "source": source, "close": 150.0, "ema21": 148.0,
        "above_ema21": True, "dist_from_ema21_pct": 1.35, "rs_excess_pct": 12.3,
        "rsline_at_high": True, "adr_pct": 2.1, "pct_off_6mo_high": -3.2,
        "nr7": False, "nr7_2": False, "vol_contracting": True, "day_type": "distribution",
    }


def _text_block(text):
    return type("TextBlock", (), {"type": "text", "text": text})()


def _fake_response(text, stop_reason="end_turn"):
    return type("FakeResponse", (), {
        "content": [_text_block(text)] if text else [], "stop_reason": stop_reason,
    })()


class _FakeMessages:
    """Records every create() call and returns responses from a queue,
    one per call - lets a test script a pause_turn retry sequence."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses, api_key=None):
        self.messages = _FakeMessages(responses)


def _patch_client(monkeypatch, responses):
    fake_client = _FakeClient(responses)
    monkeypatch.setattr(narrative_generator.anthropic, "Anthropic", lambda api_key: fake_client)
    return fake_client


def _api_error_response(status_code):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx.Response(status_code, request=request)


# --- _format_context_for_prompt ---

def test_format_context_for_prompt_empty_rows():
    result = narrative_generator._format_context_for_prompt(_fake_context())
    assert "nothing to report" in result.lower()


def test_format_context_for_prompt_includes_row_data():
    context = _fake_context(rows=[_fake_row()])
    result = narrative_generator._format_context_for_prompt(context)
    assert "AAPL" in result
    assert "List 1" in result
    assert "$150.00" in result
    assert "+12.3" in result
    assert "Distribution day" in result


# --- generate_narrative ---

def test_generate_narrative_fails_without_api_key(monkeypatch):
    monkeypatch.setattr(narrative_generator, "get_secret", lambda key: (_ for _ in ()).throw(RuntimeError("no key")))
    monkeypatch.setattr(narrative_generator.market_context, "compute_context", lambda conn: _fake_context())
    success, result = narrative_generator.generate_narrative(conn=None)
    assert success is False
    assert result == "no key"


def test_generate_narrative_succeeds(monkeypatch):
    monkeypatch.setattr(narrative_generator, "get_secret", lambda key: "sk-ant-fake")
    monkeypatch.setattr(narrative_generator.market_context, "compute_context", lambda conn: _fake_context())
    fake_client = _patch_client(monkeypatch, [_fake_response("## Market Snapshot\nUp today.")])

    success, result = narrative_generator.generate_narrative(conn=None)

    assert success is True
    assert result == "## Market Snapshot\nUp today."
    assert len(fake_client.messages.calls) == 1
    assert fake_client.messages.calls[0]["model"] == narrative_generator.MODEL
    assert fake_client.messages.calls[0]["tools"][0]["type"] == "web_search_20260209"
    assert fake_client.messages.calls[0]["tools"][0]["max_uses"] == narrative_generator.MAX_WEB_SEARCHES


def test_generate_narrative_resumes_after_pause_turn(monkeypatch):
    """The server-side web-search loop hit its own iteration cap
    mid-turn - narrative_generator must resend and pick up the final
    text from the SECOND response, not give up after the first."""
    monkeypatch.setattr(narrative_generator, "get_secret", lambda key: "sk-ant-fake")
    monkeypatch.setattr(narrative_generator.market_context, "compute_context", lambda conn: _fake_context())
    fake_client = _patch_client(monkeypatch, [
        _fake_response("partial...", stop_reason="pause_turn"),
        _fake_response("## Market Snapshot\nFinished after resuming."),
    ])

    success, result = narrative_generator.generate_narrative(conn=None)

    assert success is True
    assert result == "## Market Snapshot\nFinished after resuming."
    assert len(fake_client.messages.calls) == 2
    # The resend carries the paused assistant turn, not a new "continue" message.
    resend_messages = fake_client.messages.calls[1]["messages"]
    assert resend_messages[-1]["role"] == "assistant"


def test_generate_narrative_none_when_refused(monkeypatch):
    monkeypatch.setattr(narrative_generator, "get_secret", lambda key: "sk-ant-fake")
    monkeypatch.setattr(narrative_generator.market_context, "compute_context", lambda conn: _fake_context())
    _patch_client(monkeypatch, [_fake_response("", stop_reason="refusal")])

    success, result = narrative_generator.generate_narrative(conn=None)

    assert success is False
    assert "declined" in result.lower()


def test_generate_narrative_empty_text_is_a_failure(monkeypatch):
    monkeypatch.setattr(narrative_generator, "get_secret", lambda key: "sk-ant-fake")
    monkeypatch.setattr(narrative_generator.market_context, "compute_context", lambda conn: _fake_context())
    _patch_client(monkeypatch, [_fake_response("", stop_reason="end_turn")])

    success, result = narrative_generator.generate_narrative(conn=None)

    assert success is False
    assert "empty" in result.lower()


@pytest.mark.parametrize("exc_factory,expected_snippet", [
    (lambda: anthropic.AuthenticationError("bad key", response=_api_error_response(401), body=None), "rejected"),
    (lambda: anthropic.RateLimitError("rate limited", response=_api_error_response(429), body=None), "rate-limiting"),
    (lambda: anthropic.APIStatusError("boom", response=_api_error_response(500), body=None), "500"),
])
def test_generate_narrative_handles_api_errors(monkeypatch, exc_factory, expected_snippet):
    monkeypatch.setattr(narrative_generator, "get_secret", lambda key: "sk-ant-fake")
    monkeypatch.setattr(narrative_generator.market_context, "compute_context", lambda conn: _fake_context())

    class _RaisingMessages:
        def create(self, **kwargs):
            raise exc_factory()

    class _RaisingClient:
        def __init__(self, api_key=None):
            self.messages = _RaisingMessages()

    monkeypatch.setattr(narrative_generator.anthropic, "Anthropic", _RaisingClient)

    success, result = narrative_generator.generate_narrative(conn=None)

    assert success is False
    assert expected_snippet in result


def test_generate_narrative_handles_connection_error(monkeypatch):
    monkeypatch.setattr(narrative_generator, "get_secret", lambda key: "sk-ant-fake")
    monkeypatch.setattr(narrative_generator.market_context, "compute_context", lambda conn: _fake_context())

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    class _RaisingMessages:
        def create(self, **kwargs):
            raise anthropic.APIConnectionError(request=request)

    class _RaisingClient:
        def __init__(self, api_key=None):
            self.messages = _RaisingMessages()

    monkeypatch.setattr(narrative_generator.anthropic, "Anthropic", _RaisingClient)

    success, result = narrative_generator.generate_narrative(conn=None)

    assert success is False
    assert "internet connection" in result.lower()
