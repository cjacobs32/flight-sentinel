from types import SimpleNamespace

from dashboard import render_history
from src.database import initialize_db, save_snapshot, save_offers
from src.monitor_registry import MonitorProfile, save_monitors
from src.providers import Segment, Offer, _leg_from_segments


def _offer(code, name, price):
    out = _leg_from_segments([Segment(code, f"{code} 100", "EZE", "MIA", "2027-12-10T10:00:00", "2027-12-10T19:00:00", airline_name=name)])
    ret = _leg_from_segments([Segment(code, f"{code} 200", "MIA", "EZE", "2027-12-17T21:00:00", "2027-12-18T07:00:00", airline_name=name)])
    o = Offer("EZE", "MIA", "2027-12-10", "2027-12-17", price, "USD", "ECONOMY", out, ret, adults=2, children=1)
    o.baggage = {
        "status": "VERIFIED", "source": "Google Flights",
        "outbound": {"personal_item":"UNKNOWN","carry_on":"INCLUDED","checked_bag":"PAID","checked_bag_price":35},
        "inbound": {"personal_item":"UNKNOWN","carry_on":"INCLUDED","checked_bag":"PAID","checked_bag_price":35},
    }
    return o


def _base(tmp_path):
    db = str(tmp_path / "v241.db")
    mons = str(tmp_path / "monitors.yaml")
    initialize_db(db)
    save_monitors([MonitorProfile("miami", "Miami", "2027-12-10", "2027-12-17", adults=2, children_ages=(8,))], mons)
    cfg = SimpleNamespace(DB_PATH=db, PRICE_PROVIDER="serpapi", SERPAPI_API_KEY="x")
    return cfg, mons, db


def _snapshot(db, price, offers):
    rid = save_snapshot(
        db, origin="EZE", destination="MIA", date_out="2027-12-10", date_ret="2027-12-17",
        currency="USD", best_price=price, adults=2, children=1, offers_found=len(offers),
        monitor_id="miami", monitor_name="Miami", provider="serpapi", provider_mode="real",
        outbound_detected=len(offers), outbound_explored=len(offers), queries_used=4, coverage_status="COMPLETE",
    )
    save_offers(db, rid, offers)
    return rid


def test_v241_offer_cards_are_compact_and_have_strong_cta(tmp_path):
    cfg, mons, db = _base(tmp_path)
    _snapshot(db, 3655, [_offer("AA", "American Airlines", 3655), _offer("AR", "Aerolíneas Argentinas", 4150)])
    body = render_history(cfg, mons, "miami")
    assert 'class="btn buy small"' in body
    assert "Ver oferta en Google" in body
    assert 'data-offer-details' in body
    assert "Ver itinerario y equipaje completo" in body
    assert "--success:#27B067" in body and "--info:#378ADD" in body


def test_v241_shows_change_vs_previous_comparable_airline(tmp_path):
    cfg, mons, db = _base(tmp_path)
    _snapshot(db, 3755, [_offer("AA", "American Airlines", 3755), _offer("AR", "Aerolíneas Argentinas", 4250)])
    _snapshot(db, 3655, [_offer("AA", "American Airlines", 3655), _offer("AR", "Aerolíneas Argentinas", 4150)])
    body = render_history(cfg, mons, "miami")
    assert "Vs. mejor oferta comparable de la corrida anterior" in body
    assert "↓ USD 100.00" in body


def test_v241_timeline_collapses_after_five_events(tmp_path):
    cfg, mons, db = _base(tmp_path)
    for i in range(8):
        p = 4200 - i * 50
        _snapshot(db, p, [_offer("AA", "American Airlines", p)])
    body = render_history(cfg, mons, "miami", tab="history", period="all")
    assert 'class="timeline-more"' in body
    assert "cambio(s) más" in body


def test_v241_badge_semantics_are_standardized(tmp_path):
    cfg, mons, db = _base(tmp_path)
    _snapshot(db, 3655, [_offer("AA", "American Airlines", 3655)])
    body = render_history(cfg, mons, "miami")
    assert ".badge.ok,.badge.real,.badge.success,.badge.direct" in body
    assert ".badge.blue,.badge.info,.badge.stop" in body
    assert ".badge.warn,.badge.warning" in body
    assert ".badge.bad,.badge.critical" in body
