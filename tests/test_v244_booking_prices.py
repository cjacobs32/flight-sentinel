from types import SimpleNamespace

from src.providers import Offer, Segment, Leg
from src import providers_serpapi, agent
from src.database import (
    initialize_db, save_snapshot, save_offers,
    get_cached_verified_enrichment, get_offer_by_id, update_offer_google_details,
)
from dashboard import _offer_summary_values, _render_offer_card


def _offer(price=3971):
    out = Leg([Segment('AR','AR 1304','EZE','MIA','2026-12-10T08:00:00','2026-12-10T15:15:00',airline_name='Aerolíneas Argentinas')], [])
    inn = Leg([Segment('AR','AR 1305','MIA','EZE','2026-12-17T08:10:00','2026-12-17T19:15:00',airline_name='Aerolíneas Argentinas')], [])
    return Offer('EZE','MIA','2026-12-10','2026-12-17',price,'USD','ECONOMY',out,inn,2,1)


def _booking_response():
    return {
        'search_metadata': {'google_flights_url': 'https://www.google.com/travel/flights?tfs=PINNED'},
        'baggage_prices': {'together': ['1 free carry-on', '1st checked bag available for a fee']},
        'booking_options': [
            {'together': {'book_with':'Aerolíneas Argentinas','airline':True,'price':4118,'option_title':'Economy','baggage_prices':['1 free carry-on','1st checked bag available for a fee']}},
            {'together': {'book_with':'Despegar','airline':False,'price':3236}},
            {'together': {'book_with':'kiss&fly','airline':False,'price':2882}},
        ]
    }


def test_booking_summary_separates_search_lowest_and_airline_direct():
    b = providers_serpapi._booking_summary(_booking_response(), 'USD', 3971)
    assert b['status'] == 'VERIFIED'
    assert b['search_price'] == 3971
    assert b['lowest']['seller'] == 'kiss&fly'
    assert b['lowest']['price'] == 2882
    assert b['airline_direct']['seller'] == 'Aerolíneas Argentinas'
    assert b['airline_direct']['price'] == 4118


def test_enrichment_persists_booking_and_paid_bag_without_invented_amount(monkeypatch):
    o = _offer()
    monkeypatch.setattr(providers_serpapi, '_serpapi_get', lambda params, timeout=30: _booking_response())
    bag, q = providers_serpapi.enrich_offer_baggage(o, api_key='x')
    assert q == 1
    assert o.booking['lowest']['price'] == 2882
    assert o.booking['airline_direct']['price'] == 4118
    assert bag['outbound']['carry_on'] == 'INCLUDED'
    assert bag['outbound']['checked_bag'] == 'PAID'
    assert bag['outbound']['checked_bag_price'] is None
    d = o.to_dict()
    assert d['booking']['lowest']['seller'] == 'kiss&fly'


def test_dashboard_uses_bookable_price_but_keeps_observed_context():
    o = _offer(); o.booking = providers_serpapi._booking_summary(_booking_response(),'USD',3971)
    o.baggage = {'status':'VERIFIED','outbound':{'carry_on':'INCLUDED','checked_bag':'PAID'},'inbound':{'carry_on':'INCLUDED','checked_bag':'PAID'}}
    row = {'id':1,'rank':2,'price':3971,'currency':'USD','total_duration_min':1100,'offer':o.to_dict()}
    snap = {'currency':'USD','adults':2,'children':1}
    v = _offer_summary_values(row,snap,3655)
    assert v['display_total'] == 2882
    html = _render_offer_card(row,snap,3655,'MEJOR ITINERARIO DE AEROLÍNEAS ARGENTINAS',4,4154)
    assert 'USD 2,882.00' in html
    assert 'Reservable con kiss&amp;fly' in html
    assert 'Precio observado en búsqueda: USD 3,971.00' in html
    assert 'Directo aerolínea: USD 4,118.00' in html
    assert 'Con cargo · importe no informado' in html


def test_booking_enrichment_cache_reused_with_baggage(tmp_path, monkeypatch):
    db = str(tmp_path/'x.db'); initialize_db(db)
    prev = _offer(); prev.booking = providers_serpapi._booking_summary(_booking_response(),'USD',3971)
    prev.baggage = {'status':'VERIFIED','outbound':{'carry_on':'INCLUDED'},'inbound':{'carry_on':'INCLUDED'},'google_flights_exact_url':'https://www.google.com/travel/flights?tfs=PINNED'}
    prev.raw = {'exact_google_flights_url':'https://www.google.com/travel/flights?tfs=PINNED'}
    run = save_snapshot(db, origin='EZE', destination='MIA', date_out='2026-12-10', date_ret='2026-12-17', currency='USD', best_price=3971, adults=2, children=1)
    save_offers(db, run, [prev])
    cached = get_cached_verified_enrichment(db, prev.content_hash())
    assert cached['booking']['lowest']['price'] == 2882
    current = _offer()
    monkeypatch.setattr(agent, 'enrich_offer_baggage', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no debe llamar SerpApi')))
    cfg = SimpleNamespace(PRICE_PROVIDER='serpapi',SERPAPI_BAGGAGE_ENRICH=True,SERPAPI_BAGGAGE_MAX_OFFERS=6,SERPAPI_API_KEY='x',SERPAPI_GL='ar',SERPAPI_HL='es',DB_PATH=db)
    diag={'queries_used':11}
    agent.enrich_featured_baggage(cfg,[current],diag)
    assert current.booking['lowest']['seller']=='kiss&fly'
    assert diag['queries_used']==11


def test_update_offer_google_details_persists_booking(tmp_path):
    db=str(tmp_path/'x.db'); initialize_db(db); o=_offer()
    rid=save_snapshot(db, origin='EZE', destination='MIA', date_out='2026-12-10', date_ret='2026-12-17', currency='USD', best_price=3971, adults=2, children=1)
    save_offers(db,rid,[o])
    oid=get_offer_by_id(db,1)['id']
    booking=providers_serpapi._booking_summary(_booking_response(),'USD',3971)
    update_offer_google_details(db,oid,exact_url='https://www.google.com/travel/flights?tfs=PINNED',booking=booking)
    row=get_offer_by_id(db,oid)
    assert row['offer']['booking']['lowest']['price']==2882
    assert row['offer']['links']['google_flights_scope']=='EXACT'
