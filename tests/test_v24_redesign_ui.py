from types import SimpleNamespace

from dashboard import render_history, render_form, render_home
from src.database import initialize_db, save_snapshot, save_offers
from src.monitor_registry import MonitorProfile, save_monitors
from src.providers import Segment, Offer, _leg_from_segments


def _seg(code, fn, fr, to, dep, arr, name=None):
    return Segment(code, fn, fr, to, dep, arr, airline_name=name)


def _offer(code, name, price, stops=False, checked="INCLUDED"):
    if stops:
        out = _leg_from_segments([
            _seg(code, f"{code} 100", "EZE", "BOG", "2027-12-10T07:00:00", "2027-12-10T11:00:00", name),
            _seg(code, f"{code} 101", "BOG", "MIA", "2027-12-10T12:30:00", "2027-12-10T16:30:00", name),
        ])
    else:
        out = _leg_from_segments([_seg(code, f"{code} 100", "EZE", "MIA", "2027-12-10T10:00:00", "2027-12-10T19:00:00", name)])
    ret = _leg_from_segments([_seg(code, f"{code} 200", "MIA", "EZE", "2027-12-17T21:00:00", "2027-12-18T07:00:00", name)])
    o = Offer("EZE", "MIA", "2027-12-10", "2027-12-17", price, "USD", "ECONOMY", out, ret, adults=2, children=1)
    o.baggage = {
        "status": "VERIFIED", "source": "Google Flights",
        "outbound": {"personal_item":"UNKNOWN","carry_on":"INCLUDED","checked_bag":checked},
        "inbound": {"personal_item":"UNKNOWN","carry_on":"INCLUDED","checked_bag":checked},
    }
    return o


def _setup(tmp_path):
    db = str(tmp_path / "ui24.db")
    mons = str(tmp_path / "monitors.yaml")
    initialize_db(db)
    save_monitors([MonitorProfile("miami", "Miami diciembre", "2027-12-10", "2027-12-17", adults=2, children_ages=(8,))], mons)
    offers = [
        _offer("AA", "American Airlines", 3655, checked="PAID"),
        _offer("AR", "Aerolíneas Argentinas", 4150),
        _offer("AV", "Avianca", 5000, stops=True),
    ]
    rid = save_snapshot(
        db, origin="EZE", destination="MIA", date_out="2027-12-10", date_ret="2027-12-17",
        currency="USD", best_price=3655, adults=2, children=1, offers_found=3,
        monitor_id="miami", monitor_name="Miami diciembre", provider="serpapi", provider_mode="real",
        outbound_detected=3, outbound_explored=3, queries_used=4, coverage_status="COMPLETE",
    )
    save_offers(db, rid, offers)
    cfg = SimpleNamespace(DB_PATH=db, PRICE_PROVIDER="serpapi", SERPAPI_API_KEY="x")
    return cfg, mons


def test_v24_comparison_is_card_first_and_tabbed(tmp_path):
    cfg, mons = _setup(tmp_path)
    body = render_history(cfg, mons, "miami")
    assert "Comparación de ofertas" in body
    assert 'class="tabs"' in body
    assert 'id="offersGrid"' in body
    assert body.count('class="offer-card') >= 3
    assert 'class="quick-compare"' not in body
    assert "Ver itinerario y equipaje completo" in body
    assert "Carry-on" in body and "Bodega" in body
    assert "DD/MM/AAAA" not in body  # detail page shows formatted values, not form hints


def test_v24_detail_tab_exposes_technical_coverage(tmp_path):
    cfg, mons = _setup(tmp_path)
    body = render_history(cfg, mons, "miami", tab="details")
    assert "Estado técnico y cobertura" in body
    assert "Cobertura Google" in body
    assert "Consultas SerpApi" in body
    assert "Aerolíneas encontradas por Google" in body
    assert "Auditar Despegar gratis" in body


def test_v24_history_tab_has_stats_chart_timeline_and_csv(tmp_path):
    cfg, mons = _setup(tmp_path)
    body = render_history(cfg, mons, "miami", tab="history")
    assert "Histórico de precios" in body
    assert "Precio actual" in body and "Mejor precio" in body and "Peor precio" in body
    assert "7 días" in body and "14 días" in body and "30 días" in body
    assert "Descargar CSV" in body
    assert "Cambios registrados" in body


def test_v24_edit_form_keeps_ddmmyyyy_and_fixed_policy(tmp_path):
    cfg, mons = _setup(tmp_path)
    m = MonitorProfile("miami", "Miami", "2027-12-10", "2027-12-17", adults=2, children_ages=(8,))
    body = render_form(cfg, m)
    assert 'value="10/12/2027"' in body
    assert 'value="17/12/2027"' in body
    assert 'placeholder="DD/MM/AAAA"' in body
    assert 'type="date"' not in body
    assert "EZE + AEP" in body and "MIA" in body
    assert "Directo o 1 escala" in body
    assert "Zona de peligro" in body


def test_v24_home_has_scanable_monitor_cards(tmp_path):
    cfg, mons = _setup(tmp_path)
    body = render_home(cfg, mons)
    assert "Tus monitores de vuelos" in body
    assert "Mejor tarifa" in body
    assert "Ver monitor" in body
    assert "Actualizar precios" in body
