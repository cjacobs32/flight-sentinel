import base64
from urllib.parse import parse_qs, urlparse

from dashboard import _best_offer_google_link
from src.google_flights_link import (
    build_google_flights_url,
    normalize_google_flights_url,
)
from src.providers import Offer, Segment, _leg_from_segments


def _offer():
    out = _leg_from_segments([Segment('AA', 'AA 982', 'EZE', 'MIA', '2026-12-10T10:10:00', '2026-12-10T17:20:00', airline_name='American Airlines')])
    ret = _leg_from_segments([Segment('AA', 'AA 931', 'MIA', 'EZE', '2026-12-17T20:15:00', '2026-12-18T07:25:00', airline_name='American Airlines')])
    return Offer('EZE', 'MIA', '2026-12-10', '2026-12-17', 3655, 'USD', 'ECONOMY', out, ret, adults=2, children=1)


def test_v242_structured_url_uses_tfs_and_encodes_search_data():
    url = build_google_flights_url(
        origin='EZE', destination='MIA', date_out='2026-12-10', date_ret='2026-12-17',
        adults=2, children=1, cabin_class='ECONOMY', currency='USD', max_stops=1,
    )
    qs = parse_qs(urlparse(url).query)
    assert 'tfs' in qs
    assert 'q' not in qs
    raw = base64.urlsafe_b64decode(qs['tfs'][0] + '=' * (-len(qs['tfs'][0]) % 4))
    assert b'EZE' in raw and b'MIA' in raw
    assert b'2026-12-10' in raw and b'2026-12-17' in raw
    assert qs['curr'] == ['USD'] and qs['hl'] == ['es'] and qs['gl'] == ['ar']


def test_v242_encoder_matches_known_google_flights_tfs_shape():
    # Known round-trip deep-link payload shape; validates byte-level encoding.
    url = build_google_flights_url(
        origin='HEL', destination='LPA', date_out='2026-03-01', date_ret='2026-03-08',
        adults=1, children=0, cabin_class='ECONOMY', currency='EUR', language='en',
        country='fi', max_stops=None,
    )
    tfs = parse_qs(urlparse(url).query)['tfs'][0]
    assert tfs == 'GhoSCjIwMjYtMDMtMDFqBRIDSEVMcgUSA0xQQRoaEgoyMDI2LTAzLTA4agUSA0xQQXIFEgNIRUxCAQFIAZgBAQ'


def test_v242_legacy_q_link_is_rebuilt_without_new_search():
    offer = _offer().to_dict()
    legacy = 'https://www.google.com/travel/flights/search?q=Flights+EZE+to+MIA+2026-12-10+returning+2026-12-17&curr=USD'
    fixed = normalize_google_flights_url(offer, legacy)
    assert 'tfs=' in fixed
    assert 'q=Flights' not in fixed


def test_v242_offer_prefers_serpapi_google_flights_url():
    o = _offer()
    official = 'https://www.google.com/travel/flights?hl=es&gl=ar&curr=USD&tfs=ABC123'
    o.raw = {'google_flights_url': official}
    assert o.google_flights_url == official


def test_v242_dashboard_migrates_historical_legacy_link():
    offer = _offer().to_dict()
    offer['links']['google_flights'] = 'https://www.google.com/travel/flights/search?q=Flights+EZE+to+MIA+2026-12-10+returning+2026-12-17&curr=USD'
    url = _best_offer_google_link({'offer': offer})
    assert 'tfs=' in url
    assert 'q=Flights' not in url
