from types import SimpleNamespace

from dashboard import _all_airline_codes, _job_lock, page
from src.market_audit_despegar import reconcile_market


def _cfg():
    return SimpleNamespace(PRICE_PROVIDER='serpapi', SERPAPI_API_KEY='x')


def test_duplicate_submit_guard_present_in_dashboard_js():
    html = page('x', '<form class="js-long-action"><button>Go</button></form>', cfg=_cfg())
    assert "form.dataset.submitting==='1'" in html
    assert "form.dataset.submitting='1'" in html


def test_monitor_lock_rejects_second_concurrent_job():
    lock = _job_lock('miami-test-lock')
    assert lock.acquire(blocking=False) is True
    try:
        assert lock.acquire(blocking=False) is False
    finally:
        lock.release()
    assert lock.acquire(blocking=False) is True
    lock.release()


def test_no_results_parsed_is_inconclusive_not_partial_market_evidence():
    rec = reconcile_market([], {
        'status': 'NO_RESULTS_PARSED',
        'warning': 'No cards',
        'airlines': [],
    })
    assert rec['status'] == 'AUDIT_INCONCLUSIVE'
    assert rec['missing_in_google'] == []


def test_airline_metric_counts_secondary_carriers_too():
    rows = [
        {'offer': {'airlines': ['DL', 'LA']}},
        {'offer': {'airlines': ['AA']}},
        {'offer': {'airlines': ['LA', 'CM']}},
    ]
    assert _all_airline_codes(rows) == {'DL', 'LA', 'AA', 'CM'}
