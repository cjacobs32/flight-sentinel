from __future__ import annotations

from types import SimpleNamespace

import cloud_dashboard
from config.settings import load_config
from src.audit_engine import _verification_targets
from src.cloud_state import empty_state, ensure_monitor
from src.monitor_registry import MonitorProfile, save_monitors


def _profile():
    return MonitorProfile(
        id="miami", name="Miami diciembre", date_outbound="2027-12-10", date_return="2027-12-17",
        enabled=True, adults=2, children_ages=(8,), cabin_class="ECONOMY", currency="USD",
    )


def _offer(price=3000, airline="AR", h="a", verified=False):
    booking = {
        "status": "VERIFIED" if verified else "NOT_CHECKED",
        "lowest": {"seller": "Proveedor", "price": price - 100} if verified else None,
        "airline_direct": {"seller": "Aerolineas Argentinas", "price": price + 200} if verified else None,
        "options": [{"seller": "Proveedor", "price": price - 100}] if verified else [],
    }
    return {
        "origin": "EZE", "destination": "MIA", "date_out": "2027-12-10", "date_ret": "2027-12-17",
        "search_price": float(price), "currency": "USD", "cabin_class": "ECONOMY", "adults": 2,
        "children": 1, "itinerary_hash": h, "itinerary": f"EZE→MIA({airline} 100) // MIA→EZE({airline} 101)",
        "airlines": [airline], "stops_outbound": 0, "stops_inbound": 0, "max_layover_hours": 0,
        "outbound": [{"carrier": airline, "flight_number": f"{airline} 100", "departure_iata": "EZE", "arrival_iata": "MIA", "departure_time": "2027-12-10T10:00:00", "arrival_time": "2027-12-10T19:00:00"}],
        "inbound": [{"carrier": airline, "flight_number": f"{airline} 101", "departure_iata": "MIA", "arrival_iata": "EZE", "departure_time": "2027-12-17T20:00:00", "arrival_time": "2027-12-18T05:00:00"}],
        "total_duration": "18h 00m", "total_duration_minutes": 1080,
        "booking_status": "VERIFIED" if verified else "NOT_CHECKED",
        "bookable_price": float(price - 100) if verified else None, "seller": "Proveedor" if verified else "",
        "booking": booking,
        "baggage": {"status": "VERIFIED" if verified else "NOT_CHECKED", "outbound": {"carry_on": "INCLUDED", "checked_bag": "UNKNOWN", "personal_item": "UNKNOWN"}, "inbound": {"carry_on": "INCLUDED", "checked_bag": "UNKNOWN", "personal_item": "UNKNOWN"}},
        "exact_google_url": "https://www.google.com/travel/flights/example" if verified else None,
        "airline_url": "https://www.aerolineas.com/" if airline == "AR" else "",
    }


def test_v27_cloud_restores_rich_v244_card_language_and_manual_full_query():
    state = empty_state(); node = ensure_monitor(state, _profile())
    ar = _offer(3000, "AR", "ar", True); aa = _offer(3200, "AA", "aa", False)
    node["last_audit"] = {"at": "2026-08-13T18:00:00Z", "offers_found": 2, "airlines": ["AR", "AA"], "offers": [ar, aa], "best": ar, "coverage_status": "COMPLETE", "outbound_detected": 4, "outbound_explored": 4}
    node["previous_audit"] = {"offers": [_offer(3300, "AR", "old-ar", True)]}
    node["last_verified"] = {"offers": [ar]}
    html = cloud_dashboard.render(state)
    for text in (
        "MEJOR ITINERARIO DE", "Ver todas las ofertas", "PRECIO RESERVABLE VERIFICADO",
        "Precio observado en búsqueda", "Ver itinerario, reserva y equipaje completo",
        "Web aerolínea", "Actualizar precios", "WATCH rápido", "Consulta completa",
    ):
        assert text in html
    assert "FLIGHT_ACTION:'audit'" in html
    assert "FLIGHT_ACTION:'watch'" in html
    assert "const defaultKey=sets.audit.offers.length?'audit':'watch'" in html
    assert "FLIGHT_RUN_AFTER_SAVE:$('#fRun').checked?'audit':''" in html


def test_v27_previous_snapshots_are_prepared_for_comparable_card_deltas(tmp_path):
    state = empty_state(); node = ensure_monitor(state, _profile())
    node["previous_watch"] = {"offers": [_offer(3500, "AR", "w-old")]}
    node["previous_audit"] = {"offers": [_offer(3400, "AR", "a-old")]}
    monitors = tmp_path / "monitors.yaml"
    save_monitors([_profile()], str(monitors))
    out = cloud_dashboard.prepare_state(state, str(monitors))
    saved = out["monitors"]["miami"]
    assert saved["previous_watch"]["offers"][0]["search_price"] == 3500
    assert saved["previous_audit"]["offers"][0]["search_price"] == 3400


def test_v27_audit_verification_targets_best_plus_new_airlines():
    def o(h, airlines):
        return SimpleNamespace(itinerary_hash=h, airlines=airlines)
    rows = [o("best", ["AR"]), o("same", ["AR"]), o("aa", ["AA"]), o("mix", ["AR", "LA"]), o("av", ["AV"])]
    selected = _verification_targets(rows, 4)
    assert [x.itinerary_hash for x in selected] == ["best", "aa", "mix", "av"]


def test_v27_config_defaults_to_four_representative_verifications(monkeypatch):
    monkeypatch.delenv("AUDIT_VERIFY_MAX_OFFERS", raising=False)
    cfg = load_config(yaml_path="/definitely/missing/config.yaml")
    assert cfg.AUDIT_VERIFY_MAX_OFFERS == 4


def test_v27_smtp_re_ehlo_after_starttls(monkeypatch):
    import src.scheduled_notifier as mod
    calls = []
    class FakeSMTP:
        def __init__(self, *a, **k): calls.append("connect")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): calls.append("ehlo")
        def starttls(self): calls.append("starttls")
        def login(self, u, p): calls.append("login")
        def sendmail(self, f, t, m): calls.append("sendmail")
    monkeypatch.setattr(mod.smtplib, "SMTP", FakeSMTP)
    cfg = SimpleNamespace(EMAIL_TO=["to@example.com"], SMTP_HOST="smtp.gmail.com", SMTP_PORT=587,
                          SMTP_USE_TLS=True, SMTP_USER="u@example.com", SMTP_PASSWORD="x",
                          EMAIL_FROM="u@example.com", EMAIL_SUBJECT_PREFIX="[Flight Sentinel]")
    assert mod.send_scheduled_email(cfg, subject="Test", html="<b>ok</b>") is True
    assert calls == ["connect", "ehlo", "starttls", "ehlo", "login", "sendmail"]


def test_v27_full_query_verifies_four_representative_airlines_and_keeps_previous(monkeypatch):
    import src.audit_engine as ae
    from src.providers import Offer, Leg, Segment
    from src.serpapi_budget import BudgetStatus

    def mk(carrier, price, fn):
        out = Leg([Segment(carrier, f"{carrier} {fn}", "EZE", "MIA", "2027-12-10T10:00:00", "2027-12-10T19:00:00")])
        inn = Leg([Segment(carrier, f"{carrier} {fn+1}", "MIA", "EZE", "2027-12-17T20:00:00", "2027-12-18T05:00:00")])
        return Offer("EZE", "MIA", "2027-12-10", "2027-12-17", float(price), "USD", "ECONOMY", out, inn, 2, 1)

    offers = [mk("AR", 3000, 100), mk("AR", 3050, 110), mk("AA", 3200, 200), mk("AV", 3400, 300), mk("CM", 3500, 400), mk("LA", 3600, 500)]
    from dataclasses import replace
    cfg = replace(load_config(yaml_path="/definitely/missing/config.yaml"),
        AUDIT_VERIFY_MAX_OFFERS=4, SERPAPI_MAX_OUTBOUND_TOKENS=15,
        SERPAPI_API_KEY="x", SERPAPI_AUTO_MONTHLY_CAP=220, SERPAPI_MANUAL_RESERVE=30,
        SERPAPI_GL="ar", SERPAPI_HL="es", EMAIL_TO=[], SMTP_HOST="")
    state = empty_state(); profile = _profile(); node = ensure_monitor(state, profile)
    node["last_audit"] = {"best": {"search_price": 3100}, "airlines": ["AR"], "offers": []}
    requested = []
    verified = []
    monkeypatch.setattr(ae, "check_budget", lambda *a, **k: (requested.append(k["requested"]) or BudgetStatus(ok=True, requested=k["requested"], used=10, monthly_limit=250, searches_left=240, auto_cap=220, auto_left=210, reserve=30)))
    monkeypatch.setattr(ae, "fetch_all_offers", lambda cfg: (offers, {"coverage_status": "COMPLETE", "outbound_detected": 6, "outbound_explored": 6, "queries_used": 7}))
    def enrich(o, **kwargs):
        verified.append(o.airlines[0])
        o.booking = {"status": "VERIFIED", "lowest": {"seller": "Proveedor", "price": o.total_price - 50}, "options": []}
        return o.baggage, 1
    monkeypatch.setattr(ae, "enrich_offer_baggage", enrich)
    event = ae.run_audit_monitor(cfg, profile, state)
    assert event["status"] == "OK"
    assert requested == [20]
    assert verified == ["AR", "AA", "AV", "CM"]
    assert len(event["verified"]) == 4
    assert node["previous_audit"]["best"]["search_price"] == 3100
    assert node["last_audit"]["offers_found"] == 6
    assert node["last_audit"]["queries_used"] == 11
