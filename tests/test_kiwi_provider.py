"""
tests/test_kiwi_provider.py
─────────────────────────────────────────────────────────────────────────────
Tests del provider Kiwi Tequila API v2.

Estructura de tests
────────────────────
1. Parseo de respuesta Kiwi (_clean_dt, _parse_segments, _split_legs)
2. Construcción de Offer desde itinerario Kiwi
3. Filtros aplicados (carriers excluidos, escalas, layover)
4. Manejo de errores HTTP (401, 400, 429, red)
5. Integración con search_offers() (dispatch correcto)
6. Configuración: KIWI_API_KEY en settings
7. Regresión: mock y amadeus siguen funcionando tras agregar kiwi

Todos los tests son offline (no llaman a la API real).
Los tests de HTTP mockean urllib con unittest.mock.
"""
from __future__ import annotations

import json
import sys
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import MagicMock, patch

# Asegurar que el directorio raíz del proyecto esté en sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.providers_kiwi import (
    _clean_dt,
    _parse_segments,
    _split_legs,
    _http_get,
    kiwi_search,
    KIWI_SEARCH_URL,
)
from src.providers import (
    Offer, Segment, Leg, LayoverInfo,
    _leg_from_segments, _passes_filters, search_offers,
)
from src.normalizer import has_excluded_airline


# ===========================================================================
# Fixtures: datos de respuesta Kiwi sintéticos
# ===========================================================================

def _make_route_item(
    carrier: str,
    flight_no: str,
    fly_from: str,
    fly_to: str,
    dep: str,
    arr: str,
    is_return: int = 0,
) -> dict:
    return {
        "airline":         carrier,
        "flight_no":       flight_no,
        "flyFrom":         fly_from,
        "flyTo":           fly_to,
        "local_departure": dep,
        "local_arrival":   arr,
        "return":          is_return,
    }


def _make_kiwi_response(
    price: float = 800.0,
    out_carrier: str = "AR",
    in_carrier: str = "AR",
    n_stops_out: int = 0,
    n_stops_in: int = 0,
    hub: str = "EZE",   # hub para escalas
) -> dict:
    """Construye una respuesta Kiwi con 1 itinerario."""

    # Tramo de ida
    out_items = []
    if n_stops_out == 0:
        out_items = [
            _make_route_item(out_carrier, "1400", "EZE", "PUJ",
                             "2027-07-04T07:00:00", "2027-07-04T15:00:00", 0),
        ]
    else:
        out_items = [
            _make_route_item(out_carrier, "1400", "EZE", hub,
                             "2027-07-04T07:00:00", "2027-07-04T10:00:00", 0),
            _make_route_item(out_carrier, "1401", hub, "PUJ",
                             "2027-07-04T12:00:00", "2027-07-04T15:00:00", 0),
        ]

    # Tramo de vuelta
    in_items = []
    if n_stops_in == 0:
        in_items = [
            _make_route_item(in_carrier, "1402", "PUJ", "EZE",
                             "2027-07-11T09:00:00", "2027-07-11T18:00:00", 1),
        ]
    else:
        in_items = [
            _make_route_item(in_carrier, "1402", "PUJ", hub,
                             "2027-07-11T09:00:00", "2027-07-11T12:00:00", 1),
            _make_route_item(in_carrier, "1403", hub, "EZE",
                             "2027-07-11T14:00:00", "2027-07-11T18:00:00", 1),
        ]

    return {
        "data": [
            {
                "price": price,
                "route": out_items + in_items,
            }
        ]
    }


def _make_mock_urlopen(response_dict: dict, status: int = 200):
    """
    Devuelve un context manager que simula urllib.request.urlopen
    con una respuesta JSON dada.
    """
    body = json.dumps(response_dict).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=mock_resp)


# ===========================================================================
# 1. Helpers de parseo
# ===========================================================================

class TestCleanDt(unittest.TestCase):
    def test_iso_with_millis_and_z(self):
        self.assertEqual(_clean_dt("2027-07-04T10:30:00.000Z"), "2027-07-04T10:30:00")

    def test_iso_with_z_no_millis(self):
        self.assertEqual(_clean_dt("2027-07-04T10:30:00Z"), "2027-07-04T10:30:00")

    def test_iso_plain(self):
        self.assertEqual(_clean_dt("2027-07-04T10:30:00"), "2027-07-04T10:30:00")

    def test_truncates_to_19_chars(self):
        result = _clean_dt("2027-07-04T10:30:00.999Z")
        self.assertEqual(len(result), 19)


class TestParseSegments(unittest.TestCase):
    def _item(self, carrier="AR", fn="1400", frm="EZE", to="PUJ",
              dep="2027-07-04T07:00:00", arr="2027-07-04T15:00:00"):
        return {
            "airline": carrier, "flight_no": fn,
            "flyFrom": frm, "flyTo": to,
            "local_departure": dep, "local_arrival": arr,
        }

    def test_single_segment_fields(self):
        segs = _parse_segments([self._item()], "ECONOMY")
        self.assertEqual(len(segs), 1)
        s = segs[0]
        self.assertEqual(s.carrier, "AR")
        self.assertEqual(s.flight_number, "AR1400")
        self.assertEqual(s.departure_iata, "EZE")
        self.assertEqual(s.arrival_iata, "PUJ")
        self.assertEqual(s.departure_time, "2027-07-04T07:00:00")
        self.assertEqual(s.arrival_time, "2027-07-04T15:00:00")
        self.assertEqual(s.cabin_class, "ECONOMY")

    def test_flight_number_concatenation(self):
        segs = _parse_segments([self._item(carrier="CM", fn="0701")], "ECONOMY")
        self.assertEqual(segs[0].flight_number, "CM0701")

    def test_empty_flight_no_uses_carrier(self):
        item = self._item(fn="")
        segs = _parse_segments([item], "ECONOMY")
        self.assertEqual(segs[0].flight_number, "AR")

    def test_datetime_normalization(self):
        item = self._item(dep="2027-07-04T07:00:00.000Z", arr="2027-07-04T15:00:00Z")
        segs = _parse_segments([item], "ECONOMY")
        self.assertEqual(segs[0].departure_time, "2027-07-04T07:00:00")
        self.assertEqual(segs[0].arrival_time, "2027-07-04T15:00:00")

    def test_duration_calculated_correctly(self):
        segs = _parse_segments([self._item(
            dep="2027-07-04T07:00:00", arr="2027-07-04T15:00:00"
        )], "ECONOMY")
        self.assertEqual(segs[0].duration_minutes, 480)

    def test_multiple_segments(self):
        items = [
            self._item(carrier="AR", fn="1400", frm="EZE", to="PTY",
                       dep="2027-07-04T07:00:00", arr="2027-07-04T12:00:00"),
            self._item(carrier="CM", fn="701",  frm="PTY", to="PUJ",
                       dep="2027-07-04T14:00:00", arr="2027-07-04T16:00:00"),
        ]
        segs = _parse_segments(items, "ECONOMY")
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0].carrier, "AR")
        self.assertEqual(segs[1].carrier, "CM")


class TestSplitLegs(unittest.TestCase):
    def _item(self, frm, to, dep, arr, ret=0):
        return {
            "airline": "AR", "flight_no": "100",
            "flyFrom": frm, "flyTo": to,
            "local_departure": dep, "local_arrival": arr,
            "return": ret,
        }

    def test_split_by_return_field_direct(self):
        route = [
            self._item("EZE","PUJ","2027-07-04T07:00:00","2027-07-04T15:00:00", ret=0),
            self._item("PUJ","EZE","2027-07-11T09:00:00","2027-07-11T18:00:00", ret=1),
        ]
        out, inn = _split_legs(route)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(inn), 1)
        self.assertEqual(out[0]["flyFrom"], "EZE")
        self.assertEqual(inn[0]["flyFrom"], "PUJ")

    def test_split_by_return_field_with_stops(self):
        route = [
            self._item("EZE","GRU","2027-07-04T06:00:00","2027-07-04T08:00:00", ret=0),
            self._item("GRU","PUJ","2027-07-04T10:00:00","2027-07-04T15:00:00", ret=0),
            self._item("PUJ","GRU","2027-07-11T09:00:00","2027-07-11T14:00:00", ret=1),
            self._item("GRU","EZE","2027-07-11T16:00:00","2027-07-11T18:00:00", ret=1),
        ]
        out, inn = _split_legs(route)
        self.assertEqual(len(out), 2)
        self.assertEqual(len(inn), 2)

    def test_fallback_without_return_field(self):
        # Sin campo 'return': detección por flyFrom/flyTo
        route = [
            {"airline":"AR","flight_no":"1","flyFrom":"EZE","flyTo":"GRU",
             "local_departure":"2027-07-04T07:00:00","local_arrival":"2027-07-04T10:00:00"},
            {"airline":"AR","flight_no":"2","flyFrom":"PUJ","flyTo":"EZE",
             "local_departure":"2027-07-11T09:00:00","local_arrival":"2027-07-11T18:00:00"},
        ]
        out, inn = _split_legs(route)
        # Debe detectar el corte porque PUJ != GRU (flyFrom ≠ flyTo anterior)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(inn), 1)
        self.assertEqual(out[0]["flyFrom"], "EZE")
        self.assertEqual(inn[0]["flyFrom"], "PUJ")

    def test_fallback_halves_when_no_discontinuity(self):
        # Si no hay discontinuidad detectable, parte a la mitad
        route = [
            {"airline":"AR","flight_no":"1","flyFrom":"EZE","flyTo":"PTY",
             "local_departure":"2027-07-04T07:00:00","local_arrival":"2027-07-04T10:00:00"},
            {"airline":"AR","flight_no":"2","flyFrom":"PTY","flyTo":"PUJ",
             "local_departure":"2027-07-04T11:00:00","local_arrival":"2027-07-04T14:00:00"},
            {"airline":"AR","flight_no":"3","flyFrom":"PUJ","flyTo":"PTY",
             "local_departure":"2027-07-11T09:00:00","local_arrival":"2027-07-11T12:00:00"},
            {"airline":"AR","flight_no":"4","flyFrom":"PTY","flyTo":"EZE",
             "local_departure":"2027-07-11T14:00:00","local_arrival":"2027-07-11T17:00:00"},
        ]
        out, inn = _split_legs(route)
        self.assertGreater(len(out), 0)
        self.assertGreater(len(inn), 0)
        self.assertEqual(len(out) + len(inn), 4)


# ===========================================================================
# 2. Construcción de Offer desde respuesta Kiwi
# ===========================================================================

class TestKiwiSearchOfferBuilding(unittest.TestCase):

    @patch("urllib.request.urlopen")
    def test_direct_flight_offer(self, mock_urlopen):
        """Vuelo directo EZE→PUJ: 1 segmento ida, 1 vuelta, precio correcto."""
        resp = _make_kiwi_response(price=800.0, out_carrier="AR", in_carrier="AR")
        mock_urlopen.side_effect = _make_mock_urlopen(resp)

        offers = kiwi_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[7],
            cabin_class="ECONOMY", currency="USD",
            max_stops=None, max_layover_hours=None,
            api_key="test_key",
        )
        self.assertEqual(len(offers), 1)
        o = offers[0]
        # precio × 3 pasajeros (2 adultos + 1 niño)
        self.assertAlmostEqual(o.total_price, 2400.0, places=2)
        self.assertEqual(o.origin, "EZE")
        self.assertEqual(o.destination, "PUJ")
        self.assertEqual(o.currency, "USD")
        self.assertEqual(o.outbound.stops, 0)
        self.assertEqual(o.inbound.stops, 0)
        self.assertEqual(o.stops_outbound, 0)
        self.assertEqual(o.stops_inbound, 0)
        self.assertIn("AR", o.airlines)

    @patch("urllib.request.urlopen")
    def test_price_multiplied_by_pax(self, mock_urlopen):
        """Precio Kiwi (por persona) × total pasajeros."""
        resp = _make_kiwi_response(price=1000.0)
        mock_urlopen.side_effect = _make_mock_urlopen(resp)

        # 1 adulto, sin niños → precio × 1
        offers = kiwi_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=1, children_ages=[],
            cabin_class="ECONOMY", currency="USD",
            max_stops=None, max_layover_hours=None,
            api_key="test_key",
        )
        self.assertAlmostEqual(offers[0].total_price, 1000.0, places=2)

    @patch("urllib.request.urlopen")
    def test_1stop_offer(self, mock_urlopen):
        """Vuelo con 1 escala: 2 segmentos ida, layover calculado."""
        resp = _make_kiwi_response(
            price=700.0, out_carrier="CM", in_carrier="CM",
            n_stops_out=1, n_stops_in=1, hub="PTY",
        )
        mock_urlopen.side_effect = _make_mock_urlopen(resp)

        offers = kiwi_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[],
            cabin_class="ECONOMY", currency="USD",
            max_stops=1, max_layover_hours=None,
            api_key="test_key",
        )
        self.assertEqual(len(offers), 1)
        o = offers[0]
        self.assertEqual(o.outbound.stops, 1)
        self.assertEqual(o.inbound.stops, 1)
        # La escala debe estar en PTY con 2h de espera
        self.assertEqual(len(o.outbound.layovers), 1)
        self.assertEqual(o.outbound.layovers[0].airport_iata, "PTY")
        self.assertAlmostEqual(o.outbound.layovers[0].duration_hours, 2.0, places=1)

    @patch("urllib.request.urlopen")
    def test_cabin_class_preserved(self, mock_urlopen):
        resp = _make_kiwi_response()
        mock_urlopen.side_effect = _make_mock_urlopen(resp)

        offers = kiwi_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=1, children_ages=[],
            cabin_class="BUSINESS", currency="USD",
            max_stops=None, max_layover_hours=None,
            api_key="test_key",
        )
        self.assertEqual(offers[0].cabin_class, "BUSINESS")
        self.assertEqual(offers[0].outbound.segments[0].cabin_class, "BUSINESS")

    @patch("urllib.request.urlopen")
    def test_content_hash_stable(self, mock_urlopen):
        """El mismo itinerario siempre produce el mismo content_hash."""
        resp = _make_kiwi_response(price=800.0)
        mock_urlopen.side_effect = _make_mock_urlopen(resp)
        offers1 = kiwi_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[7],
            cabin_class="ECONOMY", currency="USD",
            max_stops=None, max_layover_hours=None,
            api_key="test_key",
        )
        mock_urlopen.side_effect = _make_mock_urlopen(resp)
        offers2 = kiwi_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[7],
            cabin_class="ECONOMY", currency="USD",
            max_stops=None, max_layover_hours=None,
            api_key="test_key",
        )
        self.assertEqual(offers1[0].content_hash(), offers2[0].content_hash())

    @patch("urllib.request.urlopen")
    def test_empty_route_skipped(self, mock_urlopen):
        """Itinerario sin route[] es descartado silenciosamente."""
        resp = {"data": [{"price": 500.0, "route": []}]}
        mock_urlopen.side_effect = _make_mock_urlopen(resp)
        offers = kiwi_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=1, children_ages=[],
            cabin_class="ECONOMY", currency="USD",
            max_stops=None, max_layover_hours=None,
            api_key="test_key",
        )
        self.assertEqual(len(offers), 0)

    @patch("urllib.request.urlopen")
    def test_multiple_itineraries_all_parsed(self, mock_urlopen):
        """Múltiples itinerarios en data[] → todos parseados."""
        itin = _make_kiwi_response(price=800.0)["data"][0]
        resp = {"data": [itin, dict(itin, price=900.0), dict(itin, price=1000.0)]}
        mock_urlopen.side_effect = _make_mock_urlopen(resp)
        offers = kiwi_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=1, children_ages=[],
            cabin_class="ECONOMY", currency="USD",
            max_stops=None, max_layover_hours=None,
            api_key="test_key",
        )
        self.assertEqual(len(offers), 3)


# ===========================================================================
# 3. Filtros aplicados por kiwi_search
# ===========================================================================

class TestKiwiFilters(unittest.TestCase):

    def _search(self, mock_urlopen, resp, **kwargs):
        mock_urlopen.side_effect = _make_mock_urlopen(resp)
        defaults = dict(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=1, children_ages=[],
            cabin_class="ECONOMY", currency="USD",
            api_key="test_key",
        )
        defaults.update(kwargs)
        return kiwi_search(**defaults)

    @patch("urllib.request.urlopen")
    def test_excluded_carrier_w1_filtered(self, mock_urlopen):
        """Oferta con W1 (World Ticket) debe ser excluida."""
        resp = _make_kiwi_response(price=500.0, out_carrier="W1", in_carrier="W1")
        offers = self._search(mock_urlopen, resp, max_stops=None, max_layover_hours=None)
        self.assertEqual(len(offers), 0)

    @patch("urllib.request.urlopen")
    def test_excluded_carrier_x1_filtered(self, mock_urlopen):
        resp = _make_kiwi_response(price=500.0, out_carrier="X1", in_carrier="AR")
        offers = self._search(mock_urlopen, resp, max_stops=None, max_layover_hours=None)
        self.assertEqual(len(offers), 0)

    @patch("urllib.request.urlopen")
    def test_excluded_carrier_hr_filtered(self, mock_urlopen):
        resp = _make_kiwi_response(price=500.0, out_carrier="HR", in_carrier="HR")
        offers = self._search(mock_urlopen, resp, max_stops=None, max_layover_hours=None)
        self.assertEqual(len(offers), 0)

    @patch("urllib.request.urlopen")
    def test_real_carrier_ar_not_filtered(self, mock_urlopen):
        """AR (Aerolíneas Argentinas) no debe ser excluida."""
        resp = _make_kiwi_response(price=800.0, out_carrier="AR", in_carrier="AR")
        offers = self._search(mock_urlopen, resp, max_stops=None, max_layover_hours=None)
        self.assertEqual(len(offers), 1)

    @patch("urllib.request.urlopen")
    def test_max_stops_0_filters_1stop(self, mock_urlopen):
        """max_stops=0 filtra oferta con 1 escala."""
        resp = _make_kiwi_response(n_stops_out=1, n_stops_in=0, hub="PTY")
        offers = self._search(mock_urlopen, resp, max_stops=0, max_layover_hours=None)
        self.assertEqual(len(offers), 0)

    @patch("urllib.request.urlopen")
    def test_max_stops_1_passes_1stop(self, mock_urlopen):
        """max_stops=1 acepta oferta con 1 escala."""
        resp = _make_kiwi_response(n_stops_out=1, n_stops_in=1, hub="PTY")
        offers = self._search(mock_urlopen, resp, max_stops=1, max_layover_hours=None)
        self.assertEqual(len(offers), 1)

    @patch("urllib.request.urlopen")
    def test_max_layover_2h_filters_3h_layover(self, mock_urlopen):
        """max_layover_hours=2 filtra oferta con escala de 2h (hub PTY, 12:00→14:00 = 2h exactas, pasa)
        y escala de 3h (12:00→15:00, falla si max=2)."""
        # El fixture _make_kiwi_response con n_stops=1 tiene 2h de escala en PTY
        # (arr 10:00, dep 12:00 → 2h). Con max_layover=1.5h debe ser filtrada.
        resp = _make_kiwi_response(n_stops_out=1, n_stops_in=0, hub="PTY")
        offers = self._search(mock_urlopen, resp, max_stops=1, max_layover_hours=1.5)
        self.assertEqual(len(offers), 0)

    @patch("urllib.request.urlopen")
    def test_max_layover_3h_passes_2h_layover(self, mock_urlopen):
        """max_layover_hours=3 acepta escala de 2h."""
        resp = _make_kiwi_response(n_stops_out=1, n_stops_in=1, hub="PTY")
        offers = self._search(mock_urlopen, resp, max_stops=1, max_layover_hours=3.0)
        self.assertEqual(len(offers), 1)

    @patch("urllib.request.urlopen")
    def test_no_api_key_raises(self, mock_urlopen):
        """Sin API key debe lanzar RuntimeError antes de hacer la request."""
        with self.assertRaises(RuntimeError) as ctx:
            kiwi_search(
                origin="EZE", destination="PUJ",
                date_out="2027-07-04", date_ret="2027-07-11",
                adults=1, children_ages=[],
                cabin_class="ECONOMY", currency="USD",
                max_stops=None, max_layover_hours=None,
                api_key="",
            )
        self.assertIn("KIWI_API_KEY", str(ctx.exception))
        mock_urlopen.assert_not_called()


# ===========================================================================
# 4. Manejo de errores HTTP
# ===========================================================================

def _make_http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.tequila.kiwi.com/v2/search",
        code=code,
        msg=f"HTTP {code}",
        hdrs=None,
        fp=BytesIO(body.encode("utf-8")),
    )


class TestKiwiHTTPErrors(unittest.TestCase):

    def _kiwi_call(self, api_key="key"):
        return kiwi_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=1, children_ages=[],
            cabin_class="ECONOMY", currency="USD",
            max_stops=None, max_layover_hours=None,
            api_key=api_key,
            max_retries=1, backoff_base=0.0,
        )

    @patch("urllib.request.urlopen")
    def test_401_raises_clear_message(self, mock_urlopen):
        mock_urlopen.side_effect = _make_http_error(401, '{"error":"Unauthorized"}')
        with self.assertRaises(RuntimeError) as ctx:
            self._kiwi_call()
        self.assertIn("401", str(ctx.exception))
        self.assertIn("tequila.kiwi.com", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_400_raises_with_detail(self, mock_urlopen):
        mock_urlopen.side_effect = _make_http_error(400, '{"error":"Bad date format"}')
        with self.assertRaises(RuntimeError) as ctx:
            self._kiwi_call()
        self.assertIn("400", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_429_retries_and_raises(self, mock_urlopen):
        """HTTP 429 → retries y finalmente propaga el error."""
        mock_urlopen.side_effect = _make_http_error(429, "rate limited")
        with self.assertRaises(urllib.error.HTTPError):
            self._kiwi_call()
        # Con max_retries=1, urlopen debe haberse llamado exactamente 1 vez
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("urllib.request.urlopen")
    def test_500_retries(self, mock_urlopen):
        """HTTP 500 → retry. Con max_retries=2 hace 2 intentos antes de fallar."""
        mock_urlopen.side_effect = _make_http_error(500, "server error")
        with self.assertRaises(urllib.error.HTTPError):
            kiwi_search(
                origin="EZE", destination="PUJ",
                date_out="2027-07-04", date_ret="2027-07-11",
                adults=1, children_ages=[],
                cabin_class="ECONOMY", currency="USD",
                max_stops=None, max_layover_hours=None,
                api_key="key",
                max_retries=2, backoff_base=0.0,
            )
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("urllib.request.urlopen")
    def test_network_error_retries(self, mock_urlopen):
        """Error de red (OSError) → retry."""
        mock_urlopen.side_effect = OSError("Connection refused")
        with self.assertRaises(OSError):
            kiwi_search(
                origin="EZE", destination="PUJ",
                date_out="2027-07-04", date_ret="2027-07-11",
                adults=1, children_ages=[],
                cabin_class="ECONOMY", currency="USD",
                max_stops=None, max_layover_hours=None,
                api_key="key",
                max_retries=2, backoff_base=0.0,
            )
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("urllib.request.urlopen")
    def test_success_after_retry(self, mock_urlopen):
        """Falla la primera llamada (429), la segunda tiene éxito."""
        resp = _make_kiwi_response(price=800.0)
        body = json.dumps(resp).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [
            _make_http_error(429, "rate limited"),
            mock_resp,
        ]
        offers = kiwi_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=1, children_ages=[],
            cabin_class="ECONOMY", currency="USD",
            max_stops=None, max_layover_hours=None,
            api_key="key",
            max_retries=2, backoff_base=0.0,
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(mock_urlopen.call_count, 2)


# ===========================================================================
# 5. Integración con search_offers() (dispatch correcto)
# ===========================================================================

class TestSearchOffersDispatch(unittest.TestCase):

    @patch("urllib.request.urlopen")
    def test_kiwi_provider_dispatched(self, mock_urlopen):
        """search_offers(provider='kiwi') llama a kiwi_search."""
        resp = _make_kiwi_response(price=800.0)
        mock_urlopen.side_effect = _make_mock_urlopen(resp)

        offers = search_offers(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=1, children_ages=[],
            cabin_class="ECONOMY", currency="USD",
            max_stops=None, max_layover_hours=None,
            provider="kiwi",
            api_key="test_key",
        )
        self.assertEqual(len(offers), 1)
        mock_urlopen.assert_called_once()

    def test_mock_provider_still_works(self):
        """provider='mock' sigue funcionando sin cambios."""
        offers = search_offers(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=1, children_ages=[],
            cabin_class="ECONOMY", currency="USD",
            max_stops=1, max_layover_hours=4.0,
            provider="mock",
        )
        # Mock devuelve resultados (puede ser 0 si random los filtra, OK)
        self.assertIsInstance(offers, list)

    def test_unknown_provider_raises(self):
        with self.assertRaises(ValueError) as ctx:
            search_offers(
                origin="EZE", destination="PUJ",
                date_out="2027-07-04", date_ret="2027-07-11",
                adults=1, children_ages=[],
                cabin_class="ECONOMY", currency="USD",
                max_stops=None, max_layover_hours=None,
                provider="fakeapi",
            )
        self.assertIn("fakeapi", str(ctx.exception))
        self.assertIn("kiwi", str(ctx.exception))


# ===========================================================================
# 6. Configuración: KIWI_API_KEY y KIWI_MAX_RESULTS en settings
# ===========================================================================

class TestKiwiSettings(unittest.TestCase):

    def _load(self, overrides: dict):
        from config.settings import load_config
        return load_config(cli_overrides={
            "DATE_OUTBOUND": "2027-07-04",
            "DATE_RETURN":   "2027-07-11",
            **overrides,
        })

    def test_kiwi_api_key_from_cli(self):
        cfg = self._load({"KIWI_API_KEY": "abc123"})
        self.assertEqual(cfg.KIWI_API_KEY, "abc123")

    def test_kiwi_max_results_from_cli(self):
        cfg = self._load({"KIWI_MAX_RESULTS": 25})
        self.assertEqual(cfg.KIWI_MAX_RESULTS, 25)

    def test_kiwi_max_results_default_50(self):
        cfg = self._load({})
        self.assertEqual(cfg.KIWI_MAX_RESULTS, 50)

    def test_kiwi_api_key_default_empty(self):
        cfg = self._load({})
        self.assertEqual(cfg.KIWI_API_KEY, "")

    def test_price_provider_kiwi_accepted(self):
        cfg = self._load({"PRICE_PROVIDER": "kiwi"})
        self.assertEqual(cfg.PRICE_PROVIDER, "kiwi")

    def test_kiwi_from_yaml(self):
        import yaml
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({
                "kiwi_api_key":    "yaml_key_xyz",
                "kiwi_max_results": 30,
                "price_provider":   "kiwi",
            }, f)
            yaml_path = f.name
        try:
            from config.settings import load_config
            cfg = load_config(
                yaml_path=yaml_path,
                cli_overrides={"DATE_OUTBOUND": "2027-07-04", "DATE_RETURN": "2027-07-11"},
            )
            self.assertEqual(cfg.KIWI_API_KEY, "yaml_key_xyz")
            self.assertEqual(cfg.KIWI_MAX_RESULTS, 30)
            self.assertEqual(cfg.PRICE_PROVIDER, "kiwi")
        finally:
            os.unlink(yaml_path)


# ===========================================================================
# 7. Regresión: la arquitectura existente no fue rota
# ===========================================================================

class TestArchitectureRegression(unittest.TestCase):
    """Verifica que los cambios para Kiwi no rompieron nada del código existente."""

    def test_offer_dataclass_intact(self):
        from src.providers import Offer, Leg, Segment, _leg_from_segments
        dep = datetime(2026, 7, 4, 8, 0)
        fmt = "%Y-%m-%dT%H:%M:%S"
        seg = Segment(
            carrier="AR", flight_number="AR1400",
            departure_iata="EZE", arrival_iata="PUJ",
            departure_time=dep.strftime(fmt),
            arrival_time=(dep + timedelta(hours=8)).strftime(fmt),
            cabin_class="ECONOMY",
        )
        leg = _leg_from_segments([seg])
        offer = Offer(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            total_price=1500.0, currency="USD", cabin_class="ECONOMY",
            outbound=leg, inbound=leg, adults=2, children=1,
        )
        d = offer.to_dict()
        self.assertIn("presentation", d)
        self.assertEqual(d["presentation"]["price"], 1500.0)
        self.assertIn("links", d)
        self.assertIsInstance(d["airlines"], list)

    def test_has_excluded_airline_intact(self):
        self.assertTrue(has_excluded_airline(["W1"]))
        self.assertTrue(has_excluded_airline(["AR", "X1"]))
        self.assertFalse(has_excluded_airline(["AR"]))
        self.assertFalse(has_excluded_airline(["LA", "CM"]))
        self.assertFalse(has_excluded_airline([]))

    def test_passes_filters_intact(self):
        from src.providers import Offer, Segment, _leg_from_segments
        dep = datetime(2026, 7, 4, 8, 0)
        fmt = "%Y-%m-%dT%H:%M:%S"
        def seg(frm, to, dep_dt, hrs):
            return Segment(
                carrier="AR", flight_number="AR1",
                departure_iata=frm, arrival_iata=to,
                departure_time=dep_dt.strftime(fmt),
                arrival_time=(dep_dt + timedelta(hours=hrs)).strftime(fmt),
            )
        s1 = seg("EZE", "PTY", dep, 4)
        s2 = seg("PTY", "PUJ", dep + timedelta(hours=6), 3)
        leg1 = _leg_from_segments([s1, s2])
        offer = Offer("EZE","PUJ","2027-07-04","2027-07-11",1000.,"USD","ECONOMY",
                      outbound=leg1, inbound=leg1, adults=1, children=0)

        ok, _ = _passes_filters(offer, max_stops=1, max_layover_hours=None)
        self.assertTrue(ok)
        ok, _ = _passes_filters(offer, max_stops=0, max_layover_hours=None)
        self.assertFalse(ok)
        ok, _ = _passes_filters(offer, max_stops=1, max_layover_hours=1.0)
        self.assertFalse(ok)  # layover es 2h, max 1h

    def test_normalizer_intact(self):
        from src.normalizer import normalize_airline, normalize_airlines, has_excluded_airline
        self.assertEqual(normalize_airline("AR"), "Aerolíneas Argentinas")
        self.assertEqual(normalize_airline("CM"), "Copa Airlines")
        self.assertEqual(normalize_airline("ZZ"), "ZZ")
        self.assertEqual(normalize_airlines(["AR", "CM"]), ["Aerolíneas Argentinas", "Copa Airlines"])

    def test_search_offers_error_on_unknown_provider(self):
        with self.assertRaises(ValueError):
            search_offers(
                origin="EZE", destination="PUJ",
                date_out="2027-07-04", date_ret="2027-07-11",
                adults=1, children_ages=[],
                cabin_class="ECONOMY", currency="USD",
                max_stops=None, max_layover_hours=None,
                provider="nonexistent",
            )


# ===========================================================================
# Runner
# ===========================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
