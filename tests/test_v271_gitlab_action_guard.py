from pathlib import Path


def test_github_workflow_exposes_manual_action_fields():
    wf = Path('.github/workflows/flight-sentinel.yml').read_text(encoding='utf-8')
    assert 'workflow_dispatch:' in wf
    assert 'action:' in wf
    assert 'monitor_id:' in wf
    assert 'smtp_test' in wf


def test_cloud_pipeline_fails_closed_for_dispatch_without_action():
    src = Path('cloud_pipeline.py').read_text(encoding='utf-8')
    assert 'pipeline_source == "workflow_dispatch" and not action' in src
    assert 'Ejecución manual sin FLIGHT_ACTION' in src
    assert 'Entrada pipeline:' in src


def test_smtp_logs_non_secret_connection_shape():
    src = Path('src/scheduled_notifier.py').read_text(encoding='utf-8')
    needle = 'SMTP: host=%s port=%s tls=%s user_configurado=%s'
    pos = src.index(needle)
    log_region = src[max(0, pos-200):pos+350]
    assert 'SMTP_PASSWORD' not in log_region
