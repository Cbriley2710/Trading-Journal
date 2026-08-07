"""
Tests that chart_component/index.html's hardcoded JS THEME literal
matches charting._COMPONENT_THEME_COLORS.

Correction to how this was originally described: the interactive chart
does NOT actually run on hardcoded JS colors day to day -
charting.py's own applyTheme() payload (_COMPONENT_THEME_COLORS) is
pushed to the component and overwrites the JS literal on every single
render, so Python is already the real source of truth there. The JS
literal only matters for the brief moment before that first
applyTheme() call - a stale value there would show as a flash of the
wrong color rather than a chart that's permanently wrong. Still worth
catching automatically rather than by eye.
"""
import re
from pathlib import Path

import charting

COMPONENT_HTML_PATH = Path(__file__).parent.parent / "chart_component" / "index.html"


def _parse_js_theme():
    """Extracts the var THEME = {...}; object literal's key/hex-value
    pairs via regex - good enough for this one specific, simple object
    literal without needing a real JS parser."""
    html = COMPONENT_HTML_PATH.read_text(encoding="utf-8")
    match = re.search(r"var THEME\s*=\s*\{(.*?)\};", html, re.DOTALL)
    assert match is not None, "Could not find \"var THEME = {...};\" in chart_component/index.html"
    return dict(re.findall(r'(\w+):\s*"(#[0-9a-fA-F]{6})"', match.group(1)))


def test_js_theme_fallback_matches_python_source_of_truth():
    js_theme = _parse_js_theme()
    assert js_theme == charting._COMPONENT_THEME_COLORS
