from src.normalizer import AIRLINE_CATALOG, airline_info, normalize_airline
from src.providers import Segment, Leg, Offer
from src import providers_serpapi as ps
import dashboard


def _seg(code='AA', name='American Airlines', fn='AA 1', dep='EZE', arr='MIA', dt='2026-12-10T10:00:00', at='2026-12-10T19:00:00'):
    return Segment(code, fn, dep, arr, dt, at, airline_name=name)


def _offer(code='AA', name='American Airlines', price=1000, booking='tok'):
    out = Leg([_seg(code,name,f'{code} 1')], [])
    inn = Leg([_seg(code,name,f'{code} 2', 'MIA','EZE','2026-12-17T10:00:00','2026-12-17T19:00:00')], [])
    return Offer('EZE','MIA','2026-12-10','2026-12-17',price,'USD','ECONOMY',out,inn,2,1,
                 raw={'outbound':{}, 'inbound':{'booking_token':booking}})


def test_airline_catalog_is_broad_and_current_overrides():
    assert len(AIRLINE_CATALOG) >= 900
    assert airline_info('AA')['name'] == 'American Airlines'
    assert airline_info('AR')['name'] == 'Aerolíneas Argentinas'
    assert airline_info('CM')['name'] == 'Copa Airlines'
    assert airline_info('DL')['name'] == 'Delta Air Lines'
    assert airline_info('LA')['name'] == 'LATAM Airlines'
    assert normalize_airline('ZZ') == 'ZZ'


def test_serpapi_provider_airline_name_is_preserved():
    seg = ps._parse_flight_segment({
        'departure_airport': {'id':'EZE','time':'2026-12-10 10:00'},
        'arrival_airport': {'id':'MIA','time':'2026-12-10 19:00'},
        'flight_number': 'CM 278',
        'airline': 'Copa Airlines',
        'extensions': [],
    }, 'ECONOMY')
    assert seg.carrier == 'CM'
    assert seg.airline_name == 'Copa Airlines'
    assert Leg([seg], []).to_dict()['segments'][0]['airline_name'] == 'Copa Airlines'


def test_baggage_parser_structures_carry_on_and_checked_bag_price():
    info = ps._interpret_baggage_lines(['1 free carry-on', '1st checked bag: 30'], 'USD')
    assert info['carry_on'] == 'INCLUDED'
    assert info['checked_bag'] == 'PAID'
    assert info['checked_bag_price'] == 30
    assert info['personal_item'] == 'UNKNOWN'


def test_baggage_parser_spanish():
    info = ps._interpret_baggage_lines(['1 equipaje de mano incluido', 'Equipaje facturado con cargo'], 'USD')
    assert info['carry_on'] == 'INCLUDED'
    assert info['checked_bag'] == 'PAID'


def test_baggage_booking_options_enrichment(monkeypatch):
    offer = _offer()
    monkeypatch.setattr(ps, '_serpapi_get', lambda params, timeout=30: {
        'baggage_prices': {
            'departing': ['1 free carry-on', '1st checked bag: 30'],
            'returning': ['1 free carry-on', 'No checked bags'],
        }
    })
    summary, q = ps.enrich_offer_baggage(offer, api_key='x')
    assert q == 1
    assert summary['status'] == 'VERIFIED'
    assert summary['outbound']['carry_on'] == 'INCLUDED'
    assert summary['outbound']['checked_bag'] == 'PAID'
    assert summary['outbound']['checked_bag_price'] == 30
    assert summary['inbound']['checked_bag'] == 'NOT_INCLUDED'


def test_offer_presentation_prefers_provider_name():
    offer = _offer('LA', 'LATAM Airlines Brasil', 1200)
    data = offer.to_dict()
    assert data['presentation']['airlines'] == ['LATAM Airlines Brasil']


def test_dashboard_highlights_cover_secondary_airlines():
    aa = _offer('AA','American Airlines',1000).to_dict()
    la = _offer('LA','LATAM Airlines',1200).to_dict()
    # mixed DL + LA itinerary, DL is the new carrier that should be surfaced
    mixed = _offer('LA','LATAM Airlines',1300)
    mixed.outbound.segments.append(_seg('DL','Delta Air Lines','DL 10','MIA','ATL','2026-12-10T20:00:00','2026-12-10T21:00:00'))
    mixed.inbound.segments.append(_seg('DL','Delta Air Lines','DL 11','ATL','MIA','2026-12-17T08:00:00','2026-12-17T09:00:00'))
    rows = [
        {'rank':1,'offer':aa},
        {'rank':2,'offer':la},
        {'rank':3,'offer':mixed.to_dict()},
    ]
    selected = dashboard._select_highlights(rows)
    covered = set()
    for row,_ in selected:
        covered.update(row['offer']['airlines'])
    assert {'AA','LA','DL'} <= covered


def test_dashboard_renders_baggage_fields():
    offer = _offer().to_dict()
    offer['baggage'] = {
        'status':'VERIFIED','source':'Google Flights · Booking Options (SerpApi)',
        'outbound': {'personal_item':'UNKNOWN','carry_on':'INCLUDED','checked_bag':'PAID','checked_bag_price':30,'raw':['1 free carry-on','1st checked bag: 30']},
        'inbound': {'personal_item':'UNKNOWN','carry_on':'INCLUDED','checked_bag':'NOT_INCLUDED','checked_bag_price':None,'raw':['1 free carry-on','No checked bags']},
    }
    html = dashboard._render_baggage(offer,'USD')
    assert 'Equipaje de esta tarifa' in html
    assert 'Artículo personal' in html
    assert 'Carry-on / cabina' in html
    assert 'Equipaje en bodega' in html
    assert 'USD 30.00' in html


def test_baggage_v234_uses_selected_flights_json(monkeypatch):
    offer = _offer()
    captured = {}
    def fake_get(params, timeout=30):
        captured.update(params)
        return {'baggage_prices': {'together': ['1 free carry-on', 'No checked bags']}}
    monkeypatch.setattr(ps, '_serpapi_get', fake_get)
    summary, q = ps.enrich_offer_baggage(offer, api_key='x')
    assert q == 1
    assert summary['status'] == 'VERIFIED'
    assert 'selected_flights_json' in captured
    assert 'booking_token' not in captured
    import json
    selected = json.loads(captured['selected_flights_json'])
    assert selected['outbound'][0] == {
        'flight_number': 'AA1', 'departure_id': 'EZE', 'arrival_id': 'MIA', 'date': '2026-12-10'
    }
    assert selected['return'][0] == {
        'flight_number': 'AA2', 'departure_id': 'MIA', 'arrival_id': 'EZE', 'date': '2026-12-17'
    }
    assert captured['departure_id'] == 'EZE'
    assert captured['arrival_id'] == 'MIA'
    assert captured['outbound_date'] == '2026-12-10'
    assert captured['return_date'] == '2026-12-17'
    assert captured['adults'] == '2'
    assert captured['children'] == '1'


def test_baggage_v234_does_not_spend_query_if_itinerary_cannot_be_pinned(monkeypatch):
    offer = _offer()
    offer.outbound.segments[0].flight_number = ''
    called = {'n': 0}
    def fake_get(params, timeout=30):
        called['n'] += 1
        return {}
    monkeypatch.setattr(ps, '_serpapi_get', fake_get)
    summary, q = ps.enrich_offer_baggage(offer, api_key='x')
    assert q == 0
    assert called['n'] == 0
    assert summary['status'] in {'UNAVAILABLE', 'PARTIAL'}
