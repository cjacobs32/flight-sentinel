from types import SimpleNamespace
from unittest.mock import patch

from dashboard import render_form, page, _parse_ui_date
from src.market_audit_despegar import _blocked_reason, reconcile_market
from src.agent import run_market_audit


def _cfg():
    return SimpleNamespace(
        PRICE_PROVIDER="serpapi", SERPAPI_API_KEY="x", DB_PATH=":memory:",
        MONITOR_ID="miami", DATE_OUTBOUND="2026-12-10", DATE_RETURN="2026-12-17",
        ADULTS=2, CHILDREN_AGES=(8,), CURRENCY="USD",
        MARKET_AUDIT_ENABLED=True, DESPEGAR_HEADLESS=True,
        DESPEGAR_TIMEOUT_SECONDS=1, DESPEGAR_MAX_SCROLLS=1,
    )


def test_date_form_pattern_renders_real_quantifiers():
    html = render_form(_cfg())
    assert 'pattern="[0-9]{2}/[0-9]{2}/[0-9]{4}"' in html
    assert 'pattern="[0-9]2/[0-9]2/[0-9]4"' not in html
    assert 'class="input date-input"' in html
    assert _parse_ui_date("10/11/2027") == "2027-11-10"


def test_long_action_overlay_is_present():
    html = page("x", '<form class="js-long-action"></form>', cfg=_cfg())
    assert 'id="loadingOverlay"' in html
    assert 'Procesando búsqueda' in html
    assert 's transcurridos' in html


def test_despegar_antibot_messages_are_detected():
    body = "Verification Required Slide right to secure your access. Automated (bot) activity on your network"
    reason = _blocked_reason(body)
    assert "bloqueó" in reason
    assert "no intenta evadir" in reason


def test_reconciliation_marks_site_block_as_unverified():
    rec = reconcile_market([], {"status": "BLOCKED_BY_SITE", "warning": "blocked"})
    assert rec["status"] == "AUDIT_BLOCKED"
    assert rec["missing_in_google"] == []


def test_unexpected_auditor_bug_never_breaks_google_run_path():
    with patch("src.agent.audit_despegar", side_effect=TypeError("boom")):
        result = run_market_audit(_cfg(), [])
    assert result["despegar"]["status"] == "AUDIT_FAILED"
    assert result["reconciliation"]["status"] == "AUDIT_FAILED"
