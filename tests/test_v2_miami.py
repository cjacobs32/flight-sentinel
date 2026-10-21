from pathlib import Path
from unittest.mock import patch

import pytest

from config.settings import load_config
from src.monitor_registry import (
    MonitorError, MonitorProfile, apply_monitor, load_monitors, save_monitors,
)
from src.providers import _passes_filters, Offer, Segment, _leg_from_segments
from src.providers_serpapi import serpapi_search
from src.database import initialize_db, save_snapshot, get_min_price, get_monitor_history


def _seg(origin, dest, dep, arr, fn="AA 1"):
    return Segment("AA", fn, origin, dest, dep, arr)


def _offer(layover_minutes=120, stops=1):
    if stops == 0:
        out = _leg_from_segments([_seg("EZE", "MIA", "2027-07-04T10:00:00", "2027-07-04T19:00:00")])
        ret = _leg_from_segments([_seg("MIA", "EZE", "2027-07-11T10:00:00", "2027-07-11T19:00:00")])
    else:
        out = _leg_from_segments([
            _seg("EZE", "PTY", "2027-07-04T08:00:00", "2027-07-04T14:00:00", "CM 1"),
            _seg("PTY", "MIA", f"2027-07-04T{14 + layover_minutes // 60:02d}:{layover_minutes % 60:02d}:00", "2027-07-04T21:00:00", "CM 2"),
        ])
        ret = _leg_from_segments([
            _seg("MIA", "PTY", "2027-07-11T08:00:00", "2027-07-11T11:00:00", "CM 3"),
            _seg("PTY", "EZE", "2027-07-11T12:30:00", "2027-07-11T20:30:00", "CM 4"),
        ])
    return Offer("EZE", "MIA", "2027-07-04", "2027-07-11", 1000, "USD", "ECONOMY", out, ret)


def _raw_flight(origin, destination, dep, arr, flight_number="AA 1"):
    return {
        "departure_airport": {"id": origin, "time": dep},
        "arrival_airport": {"id": destination, "time": arr},
        "flight_number": flight_number,
        "airline": "American Airlines",
        "extensions": [],
    }


def test_policy_accepts_direct():
    ok, _ = _passes_filters(_offer(stops=0), 1, 2.0)
    assert ok


def test_policy_accepts_exactly_two_hour_layover():
    ok, _ = _passes_filters(_offer(layover_minutes=120), 1, 2.0)
    assert ok


def test_policy_rejects_two_hours_one_minute():
    ok, reason = _passes_filters(_offer(layover_minutes=121), 1, 2.0)
    assert not ok
    assert "máx: 2.0h" in reason


def test_serpapi_sends_native_one_stop_filter():
    outbound = {
        "best_flights": [{
            "flights": [_raw_flight("EZE", "MIA", "2027-07-04 10:00", "2027-07-04 19:00")],
            "layovers": [], "price": 1000, "departure_token": "tok"
        }], "other_flights": []
    }
    inbound = {
        "best_flights": [{
            "flights": [_raw_flight("MIA", "EZE", "2027-07-11 10:00", "2027-07-11 19:00")],
            "layovers": [], "price": 1000
        }], "other_flights": []
    }
    seen = []
    def fake_get(params):
        seen.append(dict(params))
        return outbound if len(seen) == 1 else inbound
    with patch("src.providers_serpapi._serpapi_get", side_effect=fake_get):
        offers = serpapi_search(
            origin="EZE,AEP", destination="MIA", date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[7], cabin_class="ECONOMY", currency="USD",
            max_stops=1, max_layover_hours=2.0, api_key="x", max_outbound_tokens=1,
        )
    assert offers
    assert seen[0]["stops"] == "2"  # SerpApi: 2 == 1 stop or fewer
    assert seen[0]["layover_duration"] == "1,120"
    assert seen[0]["sort_by"] == "2"
    assert seen[0]["departure_id"] == "EZE,AEP"


def test_serpapi_no_partial_fallback_when_return_unavailable():
    outbound = {
        "best_flights": [{
            "flights": [_raw_flight("EZE", "MIA", "2027-07-04 10:00", "2027-07-04 19:00")],
            "layovers": [], "price": 900, "departure_token": "tok"
        }], "other_flights": []
    }
    inbound = {"best_flights": [], "other_flights": []}
    with patch("src.providers_serpapi._serpapi_get", side_effect=[outbound, inbound]):
        offers = serpapi_search(
            origin="EZE", destination="MIA", date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[], cabin_class="ECONOMY", currency="USD",
            max_stops=1, max_layover_hours=2.0, api_key="x", max_outbound_tokens=1,
        )
    assert offers == []


def test_monitor_profile_forces_miami_policy(tmp_path):
    profile = MonitorProfile("miami-test", "Miami test", "2027-07-04", "2027-07-11", children_ages=(7,))
    path = tmp_path / "monitors.yaml"
    save_monitors([profile], str(path))
    loaded = load_monitors(str(path))[0]
    assert loaded.origins == ["EZE", "AEP"]
    assert loaded.destination == "MIA"
    assert loaded.max_stops == 1
    assert loaded.max_layover_hours == 2.0


def test_monitor_profile_rejects_past_date():
    with pytest.raises(MonitorError):
        MonitorProfile("miami-old", "Old", "2020-01-01", "2020-01-10").validate()


def test_monitor_history_is_scoped_by_id(tmp_path):
    db = str(tmp_path / "v2.db")
    initialize_db(db)
    common = dict(origin="EZE", destination="MIA", date_out="2027-07-04", date_ret="2027-07-11",
                  currency="USD", adults=2, children=1, offers_found=1)
    save_snapshot(db, best_price=1000, monitor_id="m1", monitor_name="uno", **common)
    save_snapshot(db, best_price=800, monitor_id="m2", monitor_name="dos", **common)
    assert get_min_price(db, destination="MIA", date_out="2027-07-04", date_ret="2027-07-11", currency="USD", monitor_id="m1") == 1000
    assert len(get_monitor_history(db, "m1")) == 1


def test_apply_monitor_overrides_legacy_route(monkeypatch):
    monkeypatch.delenv("DESTINATION", raising=False)
    base = load_config(cli_overrides={"DATE_OUTBOUND":"2027-08-01","DATE_RETURN":"2027-08-10","PRICE_PROVIDER":"mock"}, yaml_path="missing.yaml")
    p = MonitorProfile("miami-aug", "Agosto", "2027-08-02", "2027-08-12")
    cfg = apply_monitor(base, p)
    assert cfg.DESTINATION == "MIA"
    assert cfg.ORIGINS == ["EZE", "AEP"]
    assert cfg.MAX_STOPS == 1 and cfg.MAX_LAYOVER_HOURS == 2.0


def test_config_accepts_configurable_destination(monkeypatch):
    monkeypatch.delenv("DESTINATION", raising=False)
    cfg = load_config(
        cli_overrides={"DATE_OUTBOUND":"2027-09-01","DATE_RETURN":"2027-09-10","DESTINATION":"PUJ"},
        yaml_path="missing.yaml",
    )
    assert cfg.DESTINATION == "PUJ"


def test_config_rejects_invalid_destination_code(monkeypatch):
    monkeypatch.delenv("DESTINATION", raising=False)
    with pytest.raises(Exception, match="código IATA de 3 letras"):
        load_config(
            cli_overrides={"DATE_OUTBOUND":"2027-09-01","DATE_RETURN":"2027-09-10","DESTINATION":"ARUBA"},
            yaml_path="missing.yaml",
        )


def test_config_rejects_more_than_one_stop(monkeypatch):
    with pytest.raises(Exception, match="MAX_STOPS no puede superar 1"):
        load_config(cli_overrides={"DATE_OUTBOUND":"2027-09-01","DATE_RETURN":"2027-09-10","MAX_STOPS":2}, yaml_path="missing.yaml")


def test_config_rejects_layover_over_two_hours(monkeypatch):
    with pytest.raises(Exception, match="MAX_LAYOVER_HOURS no puede superar 2.0"):
        load_config(cli_overrides={"DATE_OUTBOUND":"2027-09-01","DATE_RETURN":"2027-09-10","MAX_LAYOVER_HOURS":2.01}, yaml_path="missing.yaml")
