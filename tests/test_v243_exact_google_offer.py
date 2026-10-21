from src.providers import Offer, Segment, Leg
from src import providers_serpapi
from src.database import initialize_db, get_connection, get_offer_by_id, update_offer_exact_google_url


def _offer():
    out=Leg(segments=[Segment('AA','AA 982','EZE','MIA','2026-12-10T21:40:00','2026-12-11T05:00:00',airline_name='American Airlines')])
    inn=Leg(segments=[Segment('AA','AA 931','MIA','EZE','2026-12-17T21:15:00','2026-12-18T07:25:00',airline_name='American Airlines')])
    return Offer('EZE','MIA','2026-12-10','2026-12-17',3655,'USD','ECONOMY',out,inn,2,1,raw={'outbound':{},'inbound':{}},baggage={})


def test_baggage_enrichment_captures_exact_google_url(monkeypatch):
    o=_offer(); exact='https://www.google.com/travel/flights/search?tfs=EXACT123&curr=USD'
    monkeypatch.setattr(providers_serpapi,'_serpapi_get',lambda params,timeout=30:{'search_metadata':{'google_flights_url':exact},'baggage_prices':['1 free carry-on','1st checked bag: 30']})
    summary,q=providers_serpapi.enrich_offer_baggage(o,api_key='x')
    assert q==1 and summary['google_flights_exact_url']==exact
    d=o.to_dict(); assert d['links']['google_flights']==exact and d['links']['google_flights_scope']=='EXACT'


def test_resolver_pins_aa982_and_aa931(monkeypatch):
    o=_offer().to_dict(); seen={}; exact='https://www.google.com/travel/flights/search?tfs=PINNED&curr=USD'
    def fake(params,timeout=30): seen.update(params); return {'search_metadata':{'google_flights_url':exact}}
    monkeypatch.setattr(providers_serpapi,'_serpapi_get',fake)
    assert providers_serpapi.resolve_exact_google_flights_url(o,api_key='secret')==exact
    assert 'AA982' in seen['selected_flights_json'] and 'AA931' in seen['selected_flights_json']


def test_persist_exact_link(tmp_path):
    import json
    db=str(tmp_path/'x.db'); initialize_db(db); offer=_offer().to_dict()
    with get_connection(db) as c:
        c.execute("INSERT INTO snapshots(run_at,monitor_id,monitor_name,origin,destination,date_out,date_ret,currency,best_price,adults,children,offers_found,provider,provider_mode) VALUES('2026-08-12T00:00:00','miami','Miami','EZE','MIA','2026-12-10','2026-12-17','USD',3655,2,1,1,'serpapi','REAL')")
        rid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute("INSERT INTO offers(run_id,content_hash,rank,origin,destination,date_out,date_ret,price,currency,cabin_class,airlines,total_duration_min,stops_outbound,stops_inbound,total_stops,total_layover_hours,outbound_dep,inbound_dep,itinerary,itinerary_hash,offer_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(rid,'h',1,'EZE','MIA','2026-12-10','2026-12-17',3655,'USD','ECONOMY','[\"AA\"]',1000,0,0,0,0,'','',offer['itinerary_str'],offer['itinerary_hash'],json.dumps(offer)))
        oid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    exact='https://www.google.com/travel/flights/search?tfs=PINNED'
    update_offer_exact_google_url(db,oid,exact)
    row=get_offer_by_id(db,oid); assert row['offer']['links']['google_flights_exact']==exact and row['offer']['links']['google_flights_scope']=='EXACT'
