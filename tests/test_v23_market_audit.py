from types import SimpleNamespace
from unittest.mock import patch

from src.market_audit_despegar import (
    build_despegar_url, parse_despegar_card, reconcile_market,
)
from src.providers import Offer, Segment, _leg_from_segments
from src.database import initialize_db, save_snapshot, get_connection, get_market_audit
from src.monitor_registry import MonitorProfile, save_monitors
from dashboard import render_history


def _seg(carrier, fn, fr, to, dep, arr):
    return Segment(carrier, fn, fr, to, dep, arr)


def _offer(carrier="AA", price=1000):
    out = _leg_from_segments([_seg(carrier, f"{carrier} 100", "EZE", "MIA", "2026-12-10T10:00:00", "2026-12-10T19:00:00")])
    ret = _leg_from_segments([_seg(carrier, f"{carrier} 101", "MIA", "EZE", "2026-12-17T21:00:00", "2026-12-18T07:00:00")])
    return Offer("EZE", "MIA", "2026-12-10", "2026-12-17", price, "USD", "ECONOMY", out, ret, adults=2, children=1)


def test_despegar_url_uses_bue_mia_and_passengers():
    url = build_despegar_url(date_out="2026-12-10", date_ret="2026-12-17", adults=2, children=1, currency="USD")
    assert "/roundtrip/BUE/MIA/2026-12-10/2026-12-17/2/1/0" in url
    assert "currency=USD" in url


def test_parse_despegar_direct_card():
    card = parse_despegar_card("American Airlines EZE 10:10 MIA 17:20 Directo 9 h 10 m US$ 1.034")
    assert card is not None
    assert card.airline_code == "AA"
    assert card.stops == 0
    assert card.price_per_person == 1034.0


def test_parse_despegar_one_stop_card():
    card = parse_despegar_card("Copa Airlines EZE 02:30 MIA 13:40 1 escala US$ 980")
    assert card is not None
    assert card.airline_code == "CM"
    assert card.stops == 1


def test_reconciliation_detects_airline_missing_from_google():
    d = {
        "status": "OK",
        "airlines": ["AA", "AR", "CM"],
        "direct_airlines": ["AA", "AR", "CM"],
    }
    rec = reconcile_market([_offer("AA"), _offer("AR", 1200)], d)
    assert rec["status"] == "DISCREPANCY"
    assert rec["missing_in_google"] == ["CM"]
    assert rec["missing_direct_in_google"] == ["CM"]


def test_reconciliation_consistent_does_not_claim_complete_market():
    d = {"status": "OK", "airlines": ["AA", "AR"], "direct_airlines": ["AA"]}
    rec = reconcile_market([_offer("AA"), _offer("AR", 1200)], d)
    assert rec["status"] == "CONSISTENT"
    assert "no garantiza" in rec["note"].lower()


def test_snapshot_persists_market_audit(tmp_path):
    db = str(tmp_path / "m.db")
    initialize_db(db)
    audit = {"reconciliation": {"status": "DISCREPANCY", "missing_in_google": ["CM"]}, "despegar": {"status": "OK"}}
    rid = save_snapshot(
        db, origin="EZE", destination="MIA", date_out="2026-12-10", date_ret="2026-12-17",
        currency="USD", best_price=1000, adults=2, children=1, offers_found=1,
        market_coverage_status="DISCREPANCY", market_audit=audit,
    )
    with get_connection(db) as conn:
        row = conn.execute("select * from snapshots where id=?", (rid,)).fetchone()
    assert row["market_coverage_status"] == "DISCREPANCY"
    assert get_market_audit(row)["reconciliation"]["missing_in_google"] == ["CM"]


def test_dashboard_shows_google_vs_despegar_discrepancy(tmp_path):
    from src.database import save_offers
    db = str(tmp_path / "ui.db")
    mons = str(tmp_path / "monitors.yaml")
    initialize_db(db)
    save_monitors([MonitorProfile("miami", "Miami", "2026-12-10", "2026-12-17", adults=2, children_ages=(8,))], mons)
    audit = {
        "reconciliation": {
            "status": "DISCREPANCY", "google_airlines": ["AA", "AR"],
            "despegar_airlines": ["AA", "AR", "CM"], "missing_in_google": ["CM"],
            "missing_direct_in_google": ["CM"], "only_google": [],
            "note": "Despegar detectó aerolíneas que no aparecen en Google.",
        },
        "despegar": {"status": "OK", "cards_seen": 20, "direct_cards": 12, "one_stop_cards": 8},
    }
    rid = save_snapshot(
        db, origin="EZE", destination="MIA", date_out="2026-12-10", date_ret="2026-12-17",
        currency="USD", best_price=1000, adults=2, children=1, offers_found=2,
        monitor_id="miami", provider="serpapi", provider_mode="real",
        outbound_detected=12, outbound_explored=12, queries_used=13, coverage_status="COMPLETE",
        market_coverage_status="DISCREPANCY", market_audit=audit,
    )
    save_offers(db, rid, [_offer("AA"), _offer("AR", 1200)])
    cfg = SimpleNamespace(DB_PATH=db, PRICE_PROVIDER="serpapi", SERPAPI_API_KEY="x")
    body = render_history(cfg, mons, "miami", tab="details")
    assert "Cobertura Google" in body
    assert "Cobertura mercado" in body
    assert "DISCREPANCIA" in body
    assert "Sólo en Despegar" in body
    assert "CM" in body
    assert "Auditar Despegar gratis" in body


def test_one_stop_without_layover_duration_is_not_declared_discrepancy():
    d = {
        "status": "OK",
        "airlines": ["AA", "CM"],
        "direct_airlines": ["AA"],
        "eligible_airlines": ["AA"],
        "unverified_one_stop_airlines": ["CM"],
    }
    rec = reconcile_market([_offer("AA")], d)
    assert rec["status"] == "PARTIAL"
    assert rec["missing_in_google"] == []
    assert rec["unresolved_one_stop"] == ["CM"]
