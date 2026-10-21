from __future__ import annotations

from pathlib import Path

from cloud_dashboard import render
from cloud_manage_monitor import profile_from_values
from src.cloud_state import append_event, empty_state, ensure_monitor, sync_monitors
from src.monitor_registry import MonitorProfile


def _profile(monitor_id: str = "miami", name: str = "Miami diciembre") -> MonitorProfile:
    return MonitorProfile(
        id=monitor_id,
        name=name,
        date_outbound="2027-12-10",
        date_return="2027-12-17",
        enabled=True,
        adults=2,
        children_ages=(8,),
        cabin_class="ECONOMY",
        currency="USD",
    )


def test_cloud_monitor_form_accepts_ddmmyyyy():
    p = profile_from_values({
        "MONITOR_ID": "miami-navidad",
        "MONITOR_NAME": "Miami Navidad",
        "MONITOR_DATE_OUTBOUND": "10/12/2027",
        "MONITOR_DATE_RETURN": "17/12/2027",
        "MONITOR_ADULTS": "2",
        "MONITOR_CHILDREN_AGES": "8,10",
        "MONITOR_CABIN_CLASS": "ECONOMY",
        "MONITOR_CURRENCY": "USD",
        "MONITOR_ENABLED": "true",
    })
    assert p.date_outbound == "2027-12-10"
    assert p.date_return == "2027-12-17"
    assert p.children_ages == (8, 10)
    assert p.origins == ["EZE", "AEP"]
    assert p.destination == "MIA"
    assert p.max_stops == 1
    assert p.max_layover_hours == 2.0


def test_sync_monitors_archives_and_restores_history():
    state = empty_state()
    p = _profile()
    node = ensure_monitor(state, p)
    node["history"] = [{"mode": "WATCH", "status": "OK"}]
    sync_monitors(state, [], prune=True)
    assert "miami" not in state["monitors"]
    assert state["archived_monitors"]["miami"]["history"]

    sync_monitors(state, [p], prune=True)
    assert "miami" in state["monitors"]
    assert state["monitors"]["miami"]["history"] == [{"mode": "WATCH", "status": "OK"}]
    assert "miami" not in state["archived_monitors"]


def test_history_compacts_full_offer_collections():
    node = {"history": []}
    full = {
        "search_price": 4000,
        "currency": "USD",
        "itinerary": "EZE→MIA(AR 1304)",
        "booking": {"very": "large"},
        "outbound": [{"departure_iata": "EZE"}],
    }
    append_event(node, {
        "mode": "WATCH",
        "status": "OK",
        "watch": {"offers": [full] * 50, "best": full},
        "verified": [full],
    })
    saved = node["history"][0]
    assert "offers" not in saved["watch"]
    assert saved["watch"]["best"]["search_price"] == 4000
    assert "booking" not in saved["watch"]["best"]
    assert "outbound" not in saved["verified"][0]


def test_cloud_full_dashboard_uses_github_command_bridge_and_rich_tabs():
    p = _profile()
    state = empty_state()
    node = ensure_monitor(state, p)
    node["last_watch"] = {
        "status": "OK",
        "offers_found": 2,
        "airlines": ["AR", "AA"],
        "direct_airlines": ["AR", "AA"],
        "best": {
            "search_price": 3931.0,
            "currency": "USD",
            "airlines": ["AR"],
            "itinerary": "EZE→MIA(AR 1304) // MIA→EZE(AR 1305)",
            "booking_status": "NOT_CHECKED",
        },
        "offers": [],
    }
    node["last_run"] = {"mode": "WATCH", "status": "OK", "at": "2026-08-13T17:53:09Z"}
    html = render(state)
    for text in (
        "Nuevo monitor",
        "Editar monitor",
        "Comparación",
        "Detalles y cobertura",
        "Histórico",
        "Actualizar precios",
        "WATCH rápido",
        "10/12/2027",
        "17/12/2027",
    ):
        assert text in html
    assert "FLIGHT_ACTION:'save_monitor'" in html
    assert "/issues/new" in html
    assert "FLIGHT_SENTINEL_COMMAND_V1" in html
    assert "Create issue" in html
    assert "/-/pipelines/new" not in html
    assert "CI_JOB_TOKEN" not in html


def test_github_workflow_uses_durable_state_branch_and_cloud_orchestrator():
    root = Path(__file__).resolve().parents[1]
    wf = (root / ".github/workflows/flight-sentinel.yml").read_text(encoding="utf-8")
    git_sync = (root / "cloud_git_sync.py").read_text(encoding="utf-8")
    assert "cloud_git_sync.py state-pull" in wf
    assert "cloud_pipeline.py" in wf
    assert "cloud_dashboard.py" in wf
    assert "cloud_git_sync.py state-push" in wf
    assert "FLIGHT_STATE_BRANCH: flight-state" in wf
    assert "GITHUB_TOKEN" in git_sync
    assert "CI_JOB_TOKEN" not in git_sync
    assert "flight-state" in git_sync


def test_github_cloud_job_uses_python_and_pages_actions():
    root = Path(__file__).resolve().parents[1]
    wf = (root / ".github/workflows/flight-sentinel.yml").read_text(encoding="utf-8")
    assert "actions/setup-python@v5" in wf
    assert "python-version: '3.13'" in wf
    assert "actions/configure-pages@v5" in wf
    assert "actions/upload-pages-artifact@v4" in wf
    assert "actions/deploy-pages@v4" in wf
