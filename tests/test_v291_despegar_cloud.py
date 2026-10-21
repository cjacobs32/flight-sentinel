from __future__ import annotations

import sys

from cloud_despegar_patch import JS_DESPEGAR, patch_html
from github_issue_command import MAGIC, parse_body
from src.cloud_state import empty_state, ensure_monitor, load_state, save_state
from src.monitor_registry import MonitorProfile, save_monitors


def _profile() -> MonitorProfile:
    return MonitorProfile(
        id="aruba-pipeline", name="Aruba pipeline", destination="AUA",
        date_outbound="2027-01-01", date_return="2027-01-15", flex_days=0,
        adults=2, children_ages=(8,), cabin_class="ECONOMY", currency="USD",
    )


def test_issue_command_accepts_despegar_audit():
    values = parse_body(f"{MAGIC}\nFLIGHT_ACTION=despegar_audit\nFLIGHT_MONITOR_ID=aruba-pipeline\n")
    assert values["FLIGHT_ACTION"] == "despegar_audit"
    assert values["FLIGHT_MONITOR_ID"] == "aruba-pipeline"


def test_pages_patch_restores_despegar_links_and_manual_audit(tmp_path):
    assert "www.despegar.com.ar/shop/flights/results/roundtrip/BUE/" in JS_DESPEGAR
    assert "FLIGHT_ACTION:'despegar_audit'" in JS_DESPEGAR
    assert "Ver en Despegar" in JS_DESPEGAR
    assert "Auditar Despegar gratis" in JS_DESPEGAR
    html = tmp_path / "index.html"
    html.write_text("<html><body><main>ok</main></body></html>", encoding="utf-8")
    patch_html(str(html))
    rendered = html.read_text(encoding="utf-8")
    assert "data-flight-despegar-v291" in rendered
    assert "Auditar Despegar gratis" in rendered
    patch_html(str(html))
    assert html.read_text(encoding="utf-8").count("data-flight-despegar-v291") == 1


def test_workflow_prepares_despegar_runtime():
    workflow = open(".github/workflows/flight-sentinel.yml", encoding="utf-8").read()
    deps = open("requirements-cloud.txt", encoding="utf-8").read()
    assert "- despegar_audit" in workflow
    assert "Prepare browser for Despegar audit" in workflow
    assert "cloud_despegar_patch.py" in workflow
    assert "playwright" in deps.lower()


def test_cloud_pipeline_despegar_audit_reuses_saved_google_snapshot(tmp_path, monkeypatch):
    import cloud_pipeline
    import src.agent as agent

    profile = _profile()
    monitors = tmp_path / "monitors.yaml"
    save_monitors([profile], str(monitors))
    state_path = tmp_path / "state.json"
    state = empty_state()
    node = ensure_monitor(state, profile)
    node["last_audit"] = {
        "offers": [{
            "origin": "EZE", "destination": "AUA", "date_out": "2027-01-01", "date_ret": "2027-01-15",
            "search_price": 900.0, "currency": "USD", "cabin_class": "ECONOMY", "adults": 2, "children": 1,
            "outbound": [{"carrier": "AR", "flight_number": "AR 1", "departure_iata": "EZE", "arrival_iata": "AUA",
                          "departure_time": "2027-01-01T08:00:00", "arrival_time": "2027-01-01T14:00:00"}],
            "inbound": [{"carrier": "AR", "flight_number": "AR 2", "departure_iata": "AUA", "arrival_iata": "EZE",
                         "departure_time": "2027-01-15T15:00:00", "arrival_time": "2027-01-15T23:00:00"}],
            "booking": {}, "baggage": {},
        }],
        "offers_found": 1,
    }
    save_state(str(state_path), state)
    calls = []

    def fake_market(cfg, offers):
        calls.append((cfg.DESTINATION, len(offers)))
        return {
            "reconciliation": {"status": "CONSISTENT", "google_airlines": ["AR"], "despegar_airlines": ["AR"],
                               "missing_in_google": [], "missing_direct_in_google": [], "note": "ok"},
            "despegar": {"status": "OK", "url": "https://despegar.test", "cards_seen": 1,
                         "direct_cards": 1, "one_stop_cards": 0, "warning": ""},
        }

    monkeypatch.setattr(agent, "run_market_audit", fake_market)
    monkeypatch.setenv("FLIGHT_ACTION", "despegar_audit")
    monkeypatch.setenv("FLIGHT_MONITOR_ID", profile.id)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setattr(sys, "argv", ["cloud_pipeline.py", "--state", str(state_path), "--monitors-file", str(monitors)])

    assert cloud_pipeline.main() == 0
    saved = load_state(str(state_path))["monitors"][profile.id]
    assert calls == [("AUA", 1)]
    assert saved["last_market_audit"]["reconciliation"]["status"] == "CONSISTENT"
    assert saved["history"][-1]["mode"] == "DESPEGAR_AUDIT"
