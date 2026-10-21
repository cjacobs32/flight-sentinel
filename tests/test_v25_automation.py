from __future__ import annotations

from types import SimpleNamespace

from src.cloud_state import empty_state, ensure_monitor, offer_from_state, offer_to_state
from src.monitor_registry import MonitorProfile
from src.providers import Offer, Segment, Leg
from src.serpapi_budget import evaluate_account, BudgetStatus
from src.watch_engine import detect_changes, run_watch_monitor
from src import watch_provider_fli
from cloud_dashboard import render


def _offer(price=1000.0, carrier="AA", out_flight="AA 982", ret_flight="AA 931"):
    out = Leg([Segment(carrier, out_flight, "EZE", "MIA", "2027-12-10T10:00:00", "2027-12-10T19:00:00")], [])
    inn = Leg([Segment(carrier, ret_flight, "MIA", "EZE", "2027-12-17T20:00:00", "2027-12-18T05:00:00")], [])
    return Offer("EZE", "MIA", "2027-12-10", "2027-12-17", price, "USD", "ECONOMY", out, inn, 2, 1)


def _profile():
    return MonitorProfile(
        id="miami", name="Miami", date_outbound="2027-12-10", date_return="2027-12-17",
        adults=2, children_ages=(8,), cabin_class="ECONOMY", currency="USD",
    )


def _cfg():
    return SimpleNamespace(
        ORIGINS=["EZE", "AEP"], DESTINATION="MIA", DATE_OUTBOUND="2027-12-10", DATE_RETURN="2027-12-17",
        ADULTS=2, CHILDREN_AGES=[8], MAX_STOPS=1, MAX_LAYOVER_HOURS=2.0, CABIN_CLASS="ECONOMY", CURRENCY="USD",
        WATCH_PRICE_ABS_CHANGE=25.0, WATCH_PRICE_PERCENT_CHANGE=1.0, WATCH_VERIFY_MAX_OFFERS=2,
        SERPAPI_API_KEY="secret", SERPAPI_AUTO_MONTHLY_CAP=220, SERPAPI_MANUAL_RESERVE=30,
        SERPAPI_GL="ar", SERPAPI_HL="es", EMAIL_TO=[], SMTP_HOST="",
    )


def test_budget_guard_preserves_manual_reserve():
    account = {"searches_per_month": 250, "this_month_usage": 100, "plan_searches_left": 150}
    ok = evaluate_account(account, requested=2, auto_cap=220, reserve=30)
    assert ok.ok is True
    assert ok.auto_cap == 220
    assert ok.auto_left == 120

    near = evaluate_account({"searches_per_month": 250, "this_month_usage": 219, "plan_searches_left": 31}, requested=2, auto_cap=220, reserve=30)
    assert near.ok is False
    assert near.auto_left == 1


def test_watch_detects_price_drop_and_new_airline():
    prev = {"best": offer_to_state(_offer(1000, "AA")), "airlines": ["AA"], "itinerary_hashes": [_offer(1000, "AA").itinerary_hash]}
    current = {"best": offer_to_state(_offer(900, "AR", "AR 1304", "AR 1305")), "airlines": ["AA", "AR"], "itinerary_hashes": [_offer(900, "AR", "AR 1304", "AR 1305").itinerary_hash]}
    changes = detect_changes(prev, current, abs_threshold=25, pct_threshold=1)
    types = {x["type"] for x in changes}
    assert "PRICE_DROP" in types
    assert "BEST_ITINERARY_CHANGED" in types
    assert "NEW_AIRLINE" in types
    assert "PREVIOUS_BEST_MISSING" in types


def test_cloud_offer_roundtrip_keeps_exact_booking_data():
    offer = _offer(1000)
    offer.booking = {"status": "VERIFIED", "lowest": {"price": 950, "seller": "American Airlines"}}
    offer.raw = {"exact_google_flights_url": "https://www.google.com/travel/flights?tfs=PINNED"}
    raw = offer_to_state(offer)
    restored = offer_from_state(raw)
    assert restored is not None
    assert restored.itinerary_hash == offer.itinerary_hash
    assert restored.booking["lowest"]["price"] == 950
    assert restored.exact_google_flights_url.endswith("tfs=PINNED")


def test_fli_adapter_round_trip_and_policy(monkeypatch):
    class E:
        def __init__(self, value): self.value = value
    class Airport:
        EZE=E("EZE"); AEP=E("AEP"); MIA=E("MIA")
    class FlightSegment:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    class PassengerInfo:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    class LayoverRestrictions:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    class FlightSearchFilters:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    class SeatType: ECONOMY="ECONOMY"
    class MaxStops: ONE_STOP_OR_FEWER="ONE"
    class SortBy: CHEAPEST="CHEAPEST"
    class TripType: ROUND_TRIP="ROUND"
    calls=[]
    class Leg:
        def __init__(self, carrier, fn, dep, arr, depdt, arrdt):
            self.airline=E(carrier); self.flight_number=fn; self.departure_airport=E(dep); self.arrival_airport=E(arr)
            self.departure_datetime=depdt; self.arrival_datetime=arrdt; self.airline_name=""
    class Result:
        def __init__(self, price, legs): self.price=price; self.legs=legs
    class SearchFlights:
        def search(self, filters, **kwargs):
            calls.append((filters, kwargs))
            origin=filters.flight_segments[0].departure_airport[0][0].value
            out=Result(1234.0, [Leg("AA", "982", origin, "MIA", "2027-12-10T10:00:00", "2027-12-10T19:00:00")])
            inn=Result(1234.0, [Leg("AA", "931", "MIA", origin, "2027-12-17T20:00:00", "2027-12-18T05:00:00")])
            return [(out, inn)]
    monkeypatch.setattr(watch_provider_fli, "_imports", lambda: {
        "Airport":Airport, "FlightSearchFilters":FlightSearchFilters, "FlightSegment":FlightSegment,
        "LayoverRestrictions":LayoverRestrictions, "MaxStops":MaxStops, "PassengerInfo":PassengerInfo,
        "SeatType":SeatType, "SortBy":SortBy, "TripType":TripType, "SearchFlights":SearchFlights,
    })
    offers=watch_provider_fli.search_fli_offers(_cfg())
    assert len(offers)==2
    assert calls[0][0].layover_restrictions.max_duration == 120
    assert calls[0][0].passenger_info.children == 1
    assert calls[0][1] == {"currency":"USD", "language":"es-AR", "country":"AR"}
    assert all(x.total_stops == 0 for x in offers)


def test_watch_only_spends_serpapi_on_change(monkeypatch):
    import src.watch_engine as we
    state=empty_state(); cfg=_cfg(); profile=_profile()
    base=_offer(1000)
    monkeypatch.setattr(we, "search_fli_offers", lambda cfg: [base])
    budget_calls=[]; enrich_calls=[]
    monkeypatch.setattr(we, "check_budget", lambda *a, **k: (budget_calls.append(k) or BudgetStatus(ok=True, requested=k["requested"], used=10, monthly_limit=250, searches_left=240, auto_cap=220, auto_left=210, reserve=30)))
    def enrich(o, **kwargs):
        enrich_calls.append(o.itinerary_hash); o.booking={"status":"VERIFIED", "lowest":{"price":o.total_price,"seller":"Google Flights"}}; return o.baggage, 1
    monkeypatch.setattr(we, "enrich_offer_baggage", enrich)
    monkeypatch.setattr(we, "send_scheduled_email", lambda *a, **k: True)
    first=run_watch_monitor(cfg, profile, state)
    assert len(enrich_calls)==1  # baseline verified once
    assert first["notify"] is False

    # Same observation: no SerpApi call.
    run_watch_monitor(cfg, profile, state)
    assert len(enrich_calls)==1

    # Meaningful drop: exactly one verification and notification.
    cheaper=_offer(900)
    monkeypatch.setattr(we, "search_fli_offers", lambda cfg: [cheaper])
    third=run_watch_monitor(cfg, profile, state)
    assert len(enrich_calls)==2
    assert third["notify"] is True


def test_cloud_dashboard_dates_are_ddmmaaaa():
    state=empty_state(); node=ensure_monitor(state,_profile())
    node["last_watch"]={"best":offer_to_state(_offer()),"offers_found":1,"airlines":["AA"]}
    node["last_run"]={"mode":"WATCH","status":"OK"}
    out=render(state)
    assert "10/12/2027" in out
    assert "17/12/2027" in out


def test_github_actions_has_schedule_pages_and_cloud_requirement():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    wf=(root/".github/workflows/flight-sentinel.yml").read_text(encoding="utf-8")
    req=(root/"requirements-cloud.txt").read_text(encoding="utf-8")
    assert "schedule:" in wf
    assert "actions/deploy-pages@v4" in wf
    assert "python-version: '3.13'" in wf
    assert "flights==0.9.0" in req

def test_fli_090_enum_name_is_used_for_iata_and_carrier():
    class E:
        def __init__(self, name, value): self.name=name; self.value=value
    leg=SimpleNamespace(
        airline=E("AR", "130"), flight_number="1302",
        departure_airport=E("EZE", "MINISTRO PISTARINI INTERNATIONAL AIRPORT"),
        arrival_airport=E("MIA", "MIAMI INTERNATIONAL AIRPORT"),
        departure_datetime="2026-12-10T22:45:00", arrival_datetime="2026-12-11T06:00:00",
        airline_name="Aerolíneas Argentinas",
    )
    seg=watch_provider_fli._segment(leg, "ECONOMY")
    assert seg.carrier == "AR"
    assert seg.flight_number == "AR 1302"
    assert seg.departure_iata == "EZE"
    assert seg.arrival_iata == "MIA"


def test_invalid_v251_watch_snapshot_is_not_compared(monkeypatch):
    import src.watch_engine as we
    state=empty_state(); cfg=_cfg(); profile=_profile()
    node=ensure_monitor(state, profile)
    node["last_watch"]={
        "best": {"outbound":[{"departure_iata":"MINISTRO PISTARINI INTERNATIONAL AIRPORT","arrival_iata":"MIAMI INTERNATIONAL AIRPORT"}],"inbound":[]},
        "airlines":["130","154"], "direct_airlines":["130"], "itinerary_hashes":["bad"]
    }
    base=_offer(1000,"AR","AR 1304","AR 1305")
    monkeypatch.setattr(we, "search_fli_offers", lambda cfg:[base])
    monkeypatch.setattr(we, "check_budget", lambda *a, **k: BudgetStatus(ok=False, requested=k["requested"], used=220, monthly_limit=250, searches_left=30, auto_cap=220, auto_left=0, reserve=30))
    result=run_watch_monitor(cfg, profile, state)
    assert result["changes"][0]["type"] == "BASELINE"
    assert not any(c["type"] in {"NEW_AIRLINE","AIRLINE_MISSING","PRICE_DROP","PRICE_RISE"} for c in result["changes"])
