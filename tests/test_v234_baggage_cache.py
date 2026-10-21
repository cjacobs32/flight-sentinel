from types import SimpleNamespace
from src.database import initialize_db, save_snapshot, save_offers, get_cached_verified_baggage
from src.providers import Segment, Leg, Offer
from src import agent


def _offer(price=1000):
    out = Leg([Segment('AA','AA 1','EZE','MIA','2026-12-10T10:00:00','2026-12-10T19:00:00',airline_name='American Airlines')], [])
    inn = Leg([Segment('AA','AA 2','MIA','EZE','2026-12-17T10:00:00','2026-12-17T19:00:00',airline_name='American Airlines')], [])
    return Offer('EZE','MIA','2026-12-10','2026-12-17',price,'USD','ECONOMY',out,inn,2,1)


def test_verified_baggage_cache_is_scoped_to_same_content_hash(tmp_path):
    db = str(tmp_path/'x.db')
    initialize_db(db)
    o = _offer(1000)
    o.baggage = {
        'status':'VERIFIED','source':'Google Flights · Booking Options (SerpApi)',
        'outbound':{'carry_on':'INCLUDED'},'inbound':{'carry_on':'INCLUDED'},
        'note':'verificado'
    }
    run = save_snapshot(db, origin='EZE', destination='MIA', date_out='2026-12-10', date_ret='2026-12-17', currency='USD', best_price=1000, adults=2, children=1)
    save_offers(db, run, [o])
    hit = get_cached_verified_baggage(db, o.content_hash())
    assert hit and hit['status'] == 'VERIFIED'
    assert hit['cache_hit'] is True
    assert get_cached_verified_baggage(db, _offer(1001).content_hash()) is None


def test_agent_reuses_verified_baggage_without_serpapi_call(tmp_path, monkeypatch):
    db = str(tmp_path/'x.db')
    initialize_db(db)
    previous = _offer(1000)
    previous.baggage = {
        'status':'VERIFIED','source':'Google Flights · Booking Options (SerpApi)',
        'outbound':{'carry_on':'INCLUDED'},'inbound':{'carry_on':'INCLUDED'},
        'note':'verificado'
    }
    run = save_snapshot(db, origin='EZE', destination='MIA', date_out='2026-12-10', date_ret='2026-12-17', currency='USD', best_price=1000, adults=2, children=1)
    save_offers(db, run, [previous])
    current = _offer(1000)
    def should_not_call(*args, **kwargs):
        raise AssertionError('No debe consumir SerpApi si hay cache verificada')
    monkeypatch.setattr(agent, 'enrich_offer_baggage', should_not_call)
    cfg = SimpleNamespace(PRICE_PROVIDER='serpapi', SERPAPI_BAGGAGE_ENRICH=True, SERPAPI_BAGGAGE_MAX_OFFERS=6, SERPAPI_API_KEY='x', SERPAPI_GL='ar', SERPAPI_HL='es', DB_PATH=db)
    diag = {'queries_used': 11}
    agent.enrich_featured_baggage(cfg, [current], diag)
    assert diag['queries_used'] == 11
    assert diag['baggage_queries_used'] == 0
    assert diag['baggage_cache_hits'] == 1
    assert current.baggage['status'] == 'VERIFIED'
