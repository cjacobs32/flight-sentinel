from types import SimpleNamespace

from dashboard import render_history, _provider_info_from_cfg
from src.database import initialize_db, save_snapshot, save_offers
from src.monitor_registry import MonitorProfile, save_monitors
from src.providers import Offer, Segment, _leg_from_segments


def _seg(carrier, fn, fr, to, dep, arr):
    return Segment(carrier, fn, fr, to, dep, arr)


def test_dashboard_marks_mock_as_simulation():
    name, mode, css = _provider_info_from_cfg(SimpleNamespace(PRICE_PROVIDER="mock"))
    assert "sintéticos" in name
    assert "NO ES PRECIO REAL" in mode
    assert css == "simulation"


def test_history_shows_airline_per_passenger_and_stop_detail(tmp_path):
    db = str(tmp_path / "ui.db")
    mons = str(tmp_path / "monitors.yaml")
    initialize_db(db)
    m = MonitorProfile("miami", "Miami", "2027-12-09", "2027-12-16", adults=2, children_ages=(8,))
    save_monitors([m], mons)
    out = _leg_from_segments([
        _seg("CM", "CM 278", "EZE", "PTY", "2027-12-09T10:00:00", "2027-12-09T16:00:00"),
        _seg("CM", "CM 419", "PTY", "MIA", "2027-12-09T17:30:00", "2027-12-09T20:30:00"),
    ])
    ret = _leg_from_segments([
        _seg("AA", "AA 900", "MIA", "EZE", "2027-12-16T21:00:00", "2027-12-17T07:00:00"),
    ])
    offer = Offer("EZE", "MIA", "2027-12-09", "2027-12-16", 2400.0, "USD", "ECONOMY", out, ret, adults=2, children=1)
    run_id = save_snapshot(db, origin="EZE", destination="MIA", date_out="2027-12-09", date_ret="2027-12-16", currency="USD", best_price=2400, adults=2, children=1, offers_found=1, monitor_id="miami", monitor_name="Miami", provider="serpapi", provider_mode="real")
    save_offers(db, run_id, [offer])
    cfg = SimpleNamespace(DB_PATH=db, PRICE_PROVIDER="serpapi")
    body = render_history(cfg, mons, "miami")
    assert "Copa Airlines" in body
    assert "American Airlines" in body
    assert "USD 800.00" in body
    assert "promedio por pasajero" in body
    assert "1 ESCALA · PTY 1h 30m" in body
    assert "DIRECTO" in body
    assert "CM 278" in body
    assert "AA 900" in body


def test_snapshot_provider_is_persisted(tmp_path):
    db = str(tmp_path / "provider.db")
    initialize_db(db)
    run_id = save_snapshot(db, origin="EZE", destination="MIA", date_out="2027-12-09", date_ret="2027-12-16", currency="USD", best_price=1000, adults=1, children=0, offers_found=1, provider="serpapi", provider_mode="real")
    from src.database import get_connection
    with get_connection(db) as conn:
        row = conn.execute("SELECT provider, provider_mode FROM snapshots WHERE id=?", (run_id,)).fetchone()
    assert row["provider"] == "serpapi"
    assert row["provider_mode"] == "real"


def test_dashboard_does_not_mark_serpapi_real_without_key():
    name, mode, css = _provider_info_from_cfg(SimpleNamespace(PRICE_PROVIDER="serpapi", SERPAPI_API_KEY=""))
    assert "Google Flights" in name
    assert "FALTA API KEY" in mode
    assert css == "sandbox"


def test_monitor_form_uses_ddmmyyyy_and_converts_to_iso():
    from dashboard import render_form, _profile_from_form
    cfg = SimpleNamespace(PRICE_PROVIDER="mock")
    m = MonitorProfile("miami", "Miami", "2027-12-09", "2027-12-16", adults=2)
    html = render_form(cfg, m)
    assert 'value="09/12/2027"' in html
    assert 'value="16/12/2027"' in html
    assert 'placeholder="DD/MM/AAAA"' in html
    assert 'type="date"' not in html

    profile = _profile_from_form({
        "name": ["Miami"],
        "date_outbound": ["10/12/2027"],
        "date_return": ["17/12/2027"],
        "adults": ["2"],
        "children_ages": ["8"],
        "cabin_class": ["ECONOMY"],
        "enabled": ["on"],
    }, existing_id="miami")
    assert profile.date_outbound == "2027-12-10"
    assert profile.date_return == "2027-12-17"


def test_history_shows_coverage_and_smart_airline_summary(tmp_path):
    db = str(tmp_path / "coverage.db")
    mons = str(tmp_path / "monitors.yaml")
    initialize_db(db)
    m = MonitorProfile("miami", "Miami", "2027-12-10", "2027-12-17", adults=2, children_ages=(8,))
    save_monitors([m], mons)

    def direct(carrier, fn1, fn2, price):
        out = _leg_from_segments([_seg(carrier, fn1, "EZE", "MIA", "2027-12-10T10:00:00", "2027-12-10T19:00:00")])
        ret = _leg_from_segments([_seg(carrier, fn2, "MIA", "EZE", "2027-12-17T21:00:00", "2027-12-18T07:00:00")])
        return Offer("EZE", "MIA", "2027-12-10", "2027-12-17", price, "USD", "ECONOMY", out, ret, adults=2, children=1)

    offers = [
        direct("AA", "AA 982", "AA 931", 3655),
        direct("AA", "AA 900", "AA 931", 3970),
        direct("AR", "AR 1304", "AR 1305", 4157),
    ]
    run_id = save_snapshot(
        db, origin="EZE", destination="MIA", date_out="2027-12-10", date_ret="2027-12-17",
        currency="USD", best_price=3655, adults=2, children=1, offers_found=3,
        monitor_id="miami", monitor_name="Miami", provider="serpapi", provider_mode="real",
        outbound_detected=9, outbound_explored=9, queries_used=10, coverage_status="COMPLETE",
    )
    save_offers(db, run_id, offers)
    cfg = SimpleNamespace(DB_PATH=db, PRICE_PROVIDER="serpapi", SERPAPI_API_KEY="x")
    body = render_history(cfg, mons, "miami")
    assert "10/12/2027 → 17/12/2027" in body
    assert "COMPLETA" in body
    assert "9/9 salidas exploradas" in body
    assert "10" in body and "Consultas SerpApi" in body
    assert "Ver todas las ofertas (3)" in body
    # En resumen sólo debe renderizarse la mejor AA y la mejor AR.
    assert "AA 982" in body
    assert "AA 900" not in body
    assert "AR 1304" in body
    assert "+USD 502.00" in body


def test_history_show_all_restores_every_valid_offer(tmp_path):
    db = str(tmp_path / "all.db")
    mons = str(tmp_path / "monitors.yaml")
    initialize_db(db)
    m = MonitorProfile("miami", "Miami", "2027-12-10", "2027-12-17", adults=1)
    save_monitors([m], mons)
    offers = []
    for idx, price in enumerate((1000, 1100, 1200), start=1):
        out = _leg_from_segments([_seg("AA", f"AA {900+idx}", "EZE", "MIA", "2027-12-10T10:00:00", "2027-12-10T19:00:00")])
        ret = _leg_from_segments([_seg("AA", f"AA {930+idx}", "MIA", "EZE", "2027-12-17T21:00:00", "2027-12-18T07:00:00")])
        offers.append(Offer("EZE", "MIA", "2027-12-10", "2027-12-17", price, "USD", "ECONOMY", out, ret, adults=1, children=0))
    run_id = save_snapshot(db, origin="EZE", destination="MIA", date_out="2027-12-10", date_ret="2027-12-17", currency="USD", best_price=1000, adults=1, children=0, offers_found=3, monitor_id="miami", provider="serpapi", provider_mode="real")
    save_offers(db, run_id, offers)
    cfg = SimpleNamespace(DB_PATH=db, PRICE_PROVIDER="serpapi", SERPAPI_API_KEY="x")
    body = render_history(cfg, mons, "miami", show_all=True)
    assert "Todas las ofertas" in body
    assert "AA 901" in body and "AA 902" in body and "AA 903" in body
    assert "Ver resumen" in body


def test_snapshot_persists_search_coverage(tmp_path):
    db = str(tmp_path / "coverage_persist.db")
    initialize_db(db)
    run_id = save_snapshot(
        db, origin="EZE", destination="MIA", date_out="2027-12-10", date_ret="2027-12-17",
        currency="USD", best_price=1000, adults=1, children=0, offers_found=5,
        provider="serpapi", provider_mode="real", outbound_detected=9,
        outbound_explored=4, queries_used=5, coverage_status="PARTIAL",
    )
    from src.database import get_connection
    with get_connection(db) as conn:
        row = conn.execute("SELECT outbound_detected,outbound_explored,queries_used,coverage_status FROM snapshots WHERE id=?", (run_id,)).fetchone()
    assert row["outbound_detected"] == 9
    assert row["outbound_explored"] == 4
    assert row["queries_used"] == 5
    assert row["coverage_status"] == "PARTIAL"
