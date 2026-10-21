from types import SimpleNamespace

from dashboard import render_history
from src.database import initialize_db, save_snapshot, save_offers
from src.monitor_registry import MonitorProfile, save_monitors
from src.providers import Segment, Offer, _leg_from_segments


def _seg(code, fn, fr, to, dep, arr, name=None):
    return Segment(code, fn, fr, to, dep, arr, airline_name=name)


def _offer(code, name, price, checked="INCLUDED"):
    out = _leg_from_segments([_seg(code, f"{code} 100", "EZE", "MIA", "2027-12-10T10:00:00", "2027-12-10T19:00:00", name)])
    ret = _leg_from_segments([_seg(code, f"{code} 101", "MIA", "EZE", "2027-12-17T21:00:00", "2027-12-18T07:00:00", name)])
    o = Offer("EZE", "MIA", "2027-12-10", "2027-12-17", price, "USD", "ECONOMY", out, ret, adults=2, children=1)
    o.baggage = {
        "status": "VERIFIED",
        "source": "Google Flights",
        "outbound": {"personal_item":"UNKNOWN","carry_on":"INCLUDED","checked_bag":checked},
        "inbound": {"personal_item":"UNKNOWN","carry_on":"INCLUDED","checked_bag":checked},
    }
    return o


def _setup(tmp_path):
    db = str(tmp_path / "ui235.db")
    mons = str(tmp_path / "monitors.yaml")
    initialize_db(db)
    save_monitors([MonitorProfile("miami", "Miami", "2027-12-10", "2027-12-17", adults=2, children_ages=(8,))], mons)
    offers = [_offer("AA", "American Airlines", 3600, "PAID"), _offer("AR", "Aerolíneas Argentinas", 4100)]
    run_id = save_snapshot(db, origin="EZE", destination="MIA", date_out="2027-12-10", date_ret="2027-12-17", currency="USD", best_price=3600, adults=2, children=1, offers_found=2, monitor_id="miami", monitor_name="Miami", provider="serpapi", provider_mode="real", outbound_detected=2, outbound_explored=2, queries_used=3, coverage_status="COMPLETE")
    save_offers(db, run_id, offers)
    return SimpleNamespace(DB_PATH=db, PRICE_PROVIDER="serpapi", SERPAPI_API_KEY="x"), mons


def test_v235_history_is_comparison_first_and_details_collapsed(tmp_path):
    cfg, mons = _setup(tmp_path)
    body = render_history(cfg, mons, "miami")
    assert "Comparación de ofertas" in body
    assert 'id="offersGrid"' in body
    assert 'class="quick-compare"' not in body
    assert "Carry-on:" in body and "Bodega:" in body
    assert '<details class="offer-details best">' in body
    assert "Ver itinerario y equipaje completo" in body
    assert 'tab=history' in body
    assert "Histórico" in body
    # Los detalles completos siguen presentes para abrirlos bajo demanda.
    assert "Equipaje de esta tarifa" in body
    assert "AA 100" in body and "AR 100" in body


def test_v235_show_all_uses_collapsed_rows_not_expanded_cards(tmp_path):
    cfg, mons = _setup(tmp_path)
    body = render_history(cfg, mons, "miami", show_all=True)
    assert "Todas las ofertas" in body
    assert body.count('class="offer-details') == 2
    assert "Ver resumen" in body
    # No se duplica una segunda tabla de comparación para las 53+ opciones.
    assert 'class="quick-compare"' not in body
