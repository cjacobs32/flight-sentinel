from pathlib import Path

import pytest

from github_issue_command import CommandError, parse_body

ROOT = Path(__file__).resolve().parents[1]


def test_gitlab_ci_removed_and_github_workflows_present():
    assert not (ROOT / '.gitlab-ci.yml').exists()
    assert (ROOT / '.github/workflows/ci.yml').exists()
    assert (ROOT / '.github/workflows/flight-sentinel.yml').exists()


def test_cloud_workflow_has_schedules_timezone_pages_and_permissions():
    wf = (ROOT / '.github/workflows/flight-sentinel.yml').read_text(encoding='utf-8')
    assert "17 0,6,12,18 * * *" in wf
    assert "40 3 * * 1,4" in wf
    assert "timezone: 'America/Argentina/Buenos_Aires'" in wf
    assert 'contents: write' in wf
    assert 'issues: write' in wf
    assert 'pages: write' in wf
    assert 'id-token: write' in wf
    assert 'actions/configure-pages@v5' in wf
    assert 'actions/upload-pages-artifact@v4' in wf
    assert 'actions/deploy-pages@v4' in wf


def test_cloud_workflow_has_manual_and_issue_commands():
    wf = (ROOT / '.github/workflows/flight-sentinel.yml').read_text(encoding='utf-8')
    assert 'workflow_dispatch:' in wf
    assert 'types: [opened]' in wf
    assert "github.event.issue.user.login == github.repository_owner" in wf
    assert 'python github_issue_command.py' in wf
    assert 'render' in wf
    assert 'FLIGHT_ACTION=audit' in wf
    assert 'FLIGHT_ACTION=watch' in wf
    assert 'smtp_test' in wf


def test_git_sync_uses_github_ephemeral_token_not_gitlab_token():
    src = (ROOT / 'cloud_git_sync.py').read_text(encoding='utf-8')
    assert 'GITHUB_TOKEN' in src
    assert 'GITHUB_REPOSITORY' in src
    assert 'x-access-token' in src
    assert 'CI_JOB_TOKEN' not in src
    assert 'gitlab' not in src.lower()


def test_dashboard_uses_github_issue_bridge_without_token():
    src = (ROOT / 'cloud_dashboard.py').read_text(encoding='utf-8')
    assert "D.github||{}" in src
    assert "issues/new" in src
    assert "FLIGHT_SENTINEL_COMMAND_V1" in src
    assert "Create issue" in src
    assert "/-/pipelines/new" not in src
    assert "CI_JOB_TOKEN" not in src


def test_issue_parser_accepts_watch_and_save():
    watch = parse_body('FLIGHT_SENTINEL_COMMAND_V1\nFLIGHT_ACTION=watch\nFLIGHT_MONITOR_ID=miami')
    assert watch['FLIGHT_ACTION'] == 'watch'
    assert watch['FLIGHT_MONITOR_ID'] == 'miami'

    save = parse_body('\n'.join([
        'FLIGHT_SENTINEL_COMMAND_V1',
        'FLIGHT_ACTION=save_monitor',
        'MONITOR_ID=miami-2',
        'MONITOR_NAME=Miami julio',
        'MONITOR_DATE_OUTBOUND=04/07/2027',
        'MONITOR_DATE_RETURN=11/07/2027',
        'MONITOR_ADULTS=2',
        'MONITOR_CHILDREN_AGES=8',
        'MONITOR_CABIN_CLASS=ECONOMY',
        'MONITOR_ENABLED=true',
    ]))
    assert save['MONITOR_ID'] == 'miami-2'
    assert save['MONITOR_DATE_OUTBOUND'] == '04/07/2027'


def test_issue_parser_rejects_unknown_or_incomplete_commands():
    with pytest.raises(CommandError):
        parse_body('FLIGHT_SENTINEL_COMMAND_V1\nFLIGHT_ACTION=rm_everything')
    with pytest.raises(CommandError):
        parse_body('FLIGHT_SENTINEL_COMMAND_V1\nFLIGHT_ACTION=watch')
    with pytest.raises(CommandError):
        parse_body('FLIGHT_SENTINEL_COMMAND_V1\nFLIGHT_ACTION=render\nBAD_FIELD=x')
