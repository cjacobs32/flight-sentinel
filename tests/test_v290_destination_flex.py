from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

import cloud_dashboard
from cloud_manage_monitor import profile_from_values
from github_issue_command import MAGIC, parse_body
from src.flex_engine import run_flex_monitor
from src.market_audit_despegar import build_despegar_url, parse_despegar_card
from src.monitor_registry import MonitorError, MonitorProfile, load_monitors, save_monitors
from src import watch_provider_fli


def _profile(**kw):
    base = dict(
        id="aruba-enero", name="Aruba enero", destination="AUA",
        date_outbound="2027-01-01", date_return="2027-01-15",
        flex_days=3, adults=2, children_ages=(8,), cabin_class="ECONOMY", currency="USD",
    )
    base.update(kw)
    return MonitorProfile(**base)


def test_monitor_destination_and_flex_round_trip_yaml(tmp_path):
    p = _profile()
    p.validate()
    path = tmp_path / "monitors.yaml"
    save_monitors([p], str(path))
    loaded = load_monitors(str(path))[0]
    assert loaded.destination == "AUA"
    assert loaded.flex_days == 3
    assert loaded.route_label == "EZE/AEP → AUA"


def test_old_monitor_defaults_to_mia_exact_dates(tmp_path):
    path = tmp_path / "monitors.yaml"
    path.write_text(
        "monitors:\n  - id: legacy\n    name: Legacy\n    date_outbound: '2027-01-01'\n    date_return: '2027-01-15'\n",
        encoding="utf-8",
    )
    loaded = load_monitors(str(path))[0]
    assert loaded.destination == "MIA"
    assert loaded.flex_days == 0


@pytest.mark.parametrize("destination", ["AU", "ARUBA", "12A", "EZE", "AEP"])
def test_invalid_destinations_are_rejected(destination):
    with pytest.raises(MonitorError):
        _profile(destination=destination).validate()


@pytest.mark.parametrize("flex", [-1, 4, 99])
def test_invalid_flex_range_is_rejected(flex):
    with pytest.raises(MonitorError):
        _profile(flex_days=flex).validate()


def test_cloud_monitor_form_accepts_aruba_and_flex():
    p = profile_from_values({
        "MONITOR_ID": "aruba-enero", "MONITOR_NAME": "Aruba enero",
        "MONITOR_DESTINATION": "AUA", "MONITOR_DATE_OUTBOUND": "01/01/2027",
        "MONITOR_DATE_RETURN": "15/01/2027", "MONITOR_FLEX_DAYS": "3",
        "MONITOR_ADULTS": "2", "MONITOR_CHILDREN_AGES": "8",
        "MONITOR_CABIN_CLASS": "ECONOMY", "MONITOR_CURRENCY": "USD",
        "MONITOR_ENABLED": "true",
    })
    assert p.destination == "AUA" and p.flex_days == 3


def test_github_issue_accepts_flex_action_and_save_fields():
    flex = parse_body(f"{MAGIC}\nFLIGHT_ACTION=flex\nFLIGHT_MONITOR_ID=aruba-enero\n")
    assert flex["FLIGHT_ACTION"] == "flex"
    body = "\n".join([
        MAGIC, "FLIGHT_ACTION=save_monitor", "MONITOR_ID=aruba-enero", "MONITOR_NAME=Aruba enero",
        "MONITOR_DESTINATION=AUA", "MONITOR_DATE_OUTBOUND=01/01/2027", "MONITOR_DATE_RETURN=15/01/2027",
        "MONITOR_FLEX_DAYS=3", "MONITOR_ADULTS=2", "MONITOR_CHILDREN_AGES=8",
        "MONITOR_CABIN_CLASS=ECONOMY", "MONITOR_CURRENCY=USD", "MONITOR_ENABLED=true",
    ])
    values = parse_body(body)
    assert values["MONITOR_DESTINATION"] == "AUA"
    assert values["MONITOR_FLEX_DAYS"] == "3"


def test_despegar_audit_is_destination_agnostic():
    url = build_despegar_url(date_out="2027-01-01", date_ret="2027-01-15", adults=2, children=1, destination="AUA")
    assert "/BUE/AUA/2027-01-01/2027-01-15/" in url
    card = parse_despegar_card(
        "EZE → AUA Directo Aerolíneas Argentinas USD 900", destination="AUA"
    )
    assert card is not None and card.stops == 0 and card.airline_code == "AR"
    assert parse_despegar_card("EZE → MIA Directo American Airlines USD 900", destination="AUA") is None


def test_fli_flexible_dates_support_independent_outbound_and_return_shifts(monkeypatch):
    class Airport:
        EZE = "EZE"; AEP = "AEP"; AUA = "AUA"
    class FlightSegment:
        def __init__(self, **kw): self.__dict__.update(kw)
    class DateSearchFilters:
        def __init__(self, **kw): self.__dict__.update(kw)
    class PassengerInfo:
        def __init__(self, **kw): self.__dict__.update(kw)
    class LayoverRestrictions:
        def __init__(self, **kw): self.__dict__.update(kw)
    class TripType: ROUND_TRIP = "ROUND_TRIP"
    class MaxStops: ONE_STOP_OR_FEWER = "ONE_STOP_OR_FEWER"
    class SeatType: ECONOMY = "ECONOMY"
    class Item:
        def __init__(self, out, ret, price):
            self.date = (datetime.fromisoformat(out), datetime.fromisoformat(ret))
            self.price = price; self.currency = "USD"
    class SearchDates:
        def search(self, filters, **kw):
            # base duration 14. These exercise independent shifts.
            if filters.duration == 15:
                return [Item("2027-01-01", "2027-01-16", 800)]  # return +1 only
            if filters.duration == 13:
                return [Item("2027-01-02", "2027-01-15", 780)]  # outbound +1 only
            if filters.duration == 14:
                return [Item("2027-01-01", "2027-01-15", 850)]
            return []
    monkeypatch.setattr(watch_provider_fli, "_imports", lambda: {
        "Airport": Airport, "FlightSegment": FlightSegment, "DateSearchFilters": DateSearchFilters,
        "PassengerInfo": PassengerInfo, "LayoverRestrictions": LayoverRestrictions,
        "TripType": TripType, "MaxStops": MaxStops, "SeatType": SeatType, "SearchDates": SearchDates,
    })
    cfg = SimpleNamespace(
        DATE_OUTBOUND="2027-01-01", DATE_RETURN="2027-01-15", ORIGINS=["EZE", "AEP"],
        DESTINATION="AUA", CHILDREN_AGES=[8], ADULTS=2, MAX_LAYOVER_HOURS=2.0,
        CABIN_CLASS="ECONOMY", CURRENCY="USD",
    )
    rows = watch_provider_fli.search_fli_flexible_dates(cfg, 1)
    pairs = {(r["date_out"], r["date_ret"], r["out_shift"], r["ret_shift"]) for r in rows}
    assert ("2027-01-01", "2027-01-16", 0, 1) in pairs
    assert ("2027-01-02", "2027-01-15", 1, 0) in pairs


def test_flex_engine_verifies_only_top_candidates(monkeypatch):
    rows = [
        {"date_out":"2027-01-02","date_ret":"2027-01-15","price":700,"currency":"USD","origin":"EZE","trip_days":13,"out_shift":1,"ret_shift":0,"verified":False},
        {"date_out":"2027-01-01","date_ret":"2027-01-16","price":720,"currency":"USD","origin":"EZE","trip_days":15,"out_shift":0,"ret_shift":1,"verified":False},
        {"date_out":"2027-01-01","date_ret":"2027-01-15","price":800,"currency":"USD","origin":"EZE","trip_days":14,"out_shift":0,"ret_shift":0,"verified":False},
    ]
    monkeypatch.setattr("src.flex_engine.search_fli_flexible_dates", lambda cfg, days: rows)
    budget = SimpleNamespace(ok=True, to_dict=lambda: {"ok": True})
    monkeypatch.setattr("src.flex_engine.check_budget", lambda *a, **k: budget)
    calls=[]
    def verify(cfg,row,**kw):
        calls.append((row["date_out"],row["date_ret"]))
        return ({"search_price":row["price"],"bookable_price":row["price"]+5,"currency":"USD","booking_status":"VERIFIED","exact_google_url":"https://google.test"}, 3, {"coverage_status":"PARTIAL","outbound_explored":1,"outbound_detected":4})
    monkeypatch.setattr("src.flex_engine._verify_candidate", verify)
    cfg = SimpleNamespace(SERPAPI_API_KEY="x", SERPAPI_AUTO_MONTHLY_CAP=220, SERPAPI_MANUAL_RESERVE=30, FLEX_VERIFY_TOP=2, FLEX_MAX_OUTBOUND_TOKENS=1)
    state={}
    event=run_flex_monitor(cfg,_profile(flex_days=3),state)
    assert len(calls)==2
    assert event["flex"]["combinations_found"]==3
    assert event["flex"]["queries_used"]==6
    assert state["monitors"]["aruba-enero"]["last_flex"]["best"]["verified"] is True


def test_dashboard_v29_contains_destination_and_flexible_controls():
    src = cloud_dashboard.JS_V29
    assert "MONITOR_DESTINATION" in src
    assert "MONITOR_FLEX_DAYS" in src
    assert "FLIGHT_ACTION:'flex'" in src
    assert "Fechas flexibles" in src
    assert "Aruba" in cloud_dashboard.prepare_state({"monitors": {}}, "missing-monitors.yaml")["airport_aliases"]["AUA"]


def test_github_workflow_exposes_flex_action():
    text = open(".github/workflows/flight-sentinel.yml", encoding="utf-8").read()
    assert "- flex" in text


def test_dashboard_v29_renames_reused_v28_functions_before_override():
    js = cloud_dashboard._runtime_js()
    assert "function bindCommonV28(){" in js
    assert "function renderMonitorV28(n,tab){" in js
    assert "var bindCommonV28=bindCommon" not in js
    assert "var renderMonitorV28=renderMonitor" not in js
    assert "b.classList.toggle('active',b.dataset.tab===tab)" in js
    assert js.rstrip().endswith("render();")
