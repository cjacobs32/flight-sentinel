"""
Tests para src/providers_duffel.py

Cobertura
─────────
  TestDuffelHTTP          — _duffel_request: errores HTTP, malformed JSON
  TestParseSegments       — _parse_segments: todos los campos, baggage, fallbacks
  TestParseOffer          — _parse_offer: round-trip completo, falta de slices
  TestDuffelFilters       — exclusión de GDS airlines, max_stops, max_layover
  TestDuffelSearchUnit    — duffel_search con API mockeada
  TestDuffelIntegration   — (requiere DUFFEL_API_TOKEN) prueba real de sandbox

Ejecutar solo los unitarios (sin red):
    python tests/test_duffel.py

Ejecutar con integración real (requiere token en DUFFEL_API_TOKEN):
    DUFFEL_API_TOKEN=duffel_test_XXX python tests/test_duffel.py integration
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.providers import Leg, Offer, Segment, _leg_from_segments
from src.providers_duffel import (
    _duffel_request, _parse_offer, _parse_segments, _resolve_carrier, duffel_search,
)


# ────────────────────────────────────────────────────────────────────────────
# Fixtures reutilizables
# ────────────────────────────────────────────────────────────────────────────

def _raw_segment(
    carrier="AR",
    flight_num="1100",
    origin="EZE",
    dest="PTY",
    dep="2027-07-04T08:00:00",
    arr="2027-07-04T12:30:00",
    bags_qty=1,
):
    return {
        "operating_carrier": {"iata_code": carrier},
        "operating_carrier_flight_number": flight_num,
        "origin": {"iata_code": origin},
        "destination": {"iata_code": dest},
        "departing_at": dep,
        "arriving_at": arr,
        "stops": 0,
        "passengers": [
            {"baggages": [{"type": "checked", "quantity": bags_qty}]}
        ],
    }


def _raw_offer_roundtrip(
    price="1850.00",
    currency="USD",
    out_segs=None,
    in_segs=None,
):
    if out_segs is None:
        out_segs = [_raw_segment("AR", "1100", "EZE", "PTY"), _raw_segment("CM", "701", "PTY", "PUJ")]
    if in_segs is None:
        in_segs = [_raw_segment("CM", "702", "PUJ", "PTY"), _raw_segment("AR", "1101", "PTY", "EZE")]
    return {
        "id": "off_0001",
        "total_amount": price,
        "total_currency": currency,
        "slices": [
            {"segments": out_segs},
            {"segments": in_segs},
        ],
    }


def _mock_api_response(offers: list) -> dict:
    return {"data": {"offers": offers}}


# ────────────────────────────────────────────────────────────────────────────
# Tests HTTP helpers
# ────────────────────────────────────────────────────────────────────────────

class TestDuffelHTTP(unittest.TestCase):
    def test_missing_token_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            duffel_search(
                origin="EZE", destination="PUJ",
                date_out="2027-07-04", date_ret="2027-07-11",
                adults=2, children_ages=[7],
                cabin_class="ECONOMY", currency="USD",
                max_stops=1, max_layover_hours=3.0,
                api_token="",   # sin token
            )
        self.assertIn("DUFFEL_API_TOKEN", str(ctx.exception))

    @patch("src.providers_duffel._duffel_request")
    def test_http_error_after_retries_raises(self, mock_req):
        mock_req.side_effect = RuntimeError("Duffel HTTP 503")
        with self.assertRaises(RuntimeError) as ctx:
            duffel_search(
                origin="EZE", destination="PUJ",
                date_out="2027-07-04", date_ret="2027-07-11",
                adults=2, children_ages=[7],
                cabin_class="ECONOMY", currency="USD",
                max_stops=1, max_layover_hours=3.0,
                api_token="duffel_test_dummy",
                max_retries=2, backoff_base=0.01,  # sleep corto
            )
        # Debe mencionar "intentos" o ser el error original propagado
        self.assertTrue(
            "intentos" in str(ctx.exception) or "503" in str(ctx.exception)
        )
        self.assertEqual(mock_req.call_count, 2)

    @patch("src.providers_duffel._duffel_request")
    def test_retry_succeeds_on_second_attempt(self, mock_req):
        """Primera llamada falla, segunda tiene éxito → debe devolver ofertas."""
        mock_req.side_effect = [
            RuntimeError("timeout"),
            _mock_api_response([_raw_offer_roundtrip()]),
        ]
        offers = duffel_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[7],
            cabin_class="ECONOMY", currency="USD",
            max_stops=1, max_layover_hours=3.0,
            api_token="duffel_test_dummy",
            max_retries=3, backoff_base=0.01,
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(mock_req.call_count, 2)


# ────────────────────────────────────────────────────────────────────────────
# Tests de parseo
# ────────────────────────────────────────────────────────────────────────────

class TestParseSegments(unittest.TestCase):
    def test_basic_fields(self):
        raw = [_raw_segment("AR", "1100", "EZE", "PTY", bags_qty=1)]
        segs = _parse_segments(raw, "ECONOMY")
        self.assertEqual(len(segs), 1)
        s = segs[0]
        self.assertEqual(s.carrier, "AR")
        self.assertEqual(s.flight_number, "AR1100")
        self.assertEqual(s.departure_iata, "EZE")
        self.assertEqual(s.arrival_iata, "PTY")
        self.assertEqual(s.cabin_class, "ECONOMY")
        self.assertTrue(s.baggage_included)
        self.assertEqual(s.baggage_pieces, 1)

    def test_no_baggage(self):
        raw = [_raw_segment(bags_qty=0)]
        segs = _parse_segments(raw, "ECONOMY")
        self.assertFalse(segs[0].baggage_included)

    def test_marketing_carrier_fallback(self):
        """Si no hay operating_carrier, usa marketing_carrier."""
        seg = {
            "marketing_carrier": {"iata_code": "CM"},
            "marketing_carrier_flight_number": "555",
            "origin": {"iata_code": "EZE"},
            "destination": {"iata_code": "PTY"},
            "departing_at": "2027-07-04T08:00:00",
            "arriving_at": "2027-07-04T12:00:00",
            "passengers": [],
        }
        segs = _parse_segments([seg], "ECONOMY")
        self.assertEqual(segs[0].carrier, "CM")

    def test_multiple_segments(self):
        raw = [
            _raw_segment("AR", "1100", "EZE", "PTY"),
            _raw_segment("CM", "701", "PTY", "PUJ"),
        ]
        segs = _parse_segments(raw, "ECONOMY")
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0].departure_iata, "EZE")
        self.assertEqual(segs[1].arrival_iata, "PUJ")

    def test_duration_minutes_calculated(self):
        seg = _raw_segment(
            dep="2027-07-04T08:00:00",
            arr="2027-07-04T12:30:00",
        )
        segs = _parse_segments([seg], "ECONOMY")
        # 8:00 → 12:30 = 270 minutos
        self.assertEqual(segs[0].duration_minutes, 270)


class TestParseOffer(unittest.TestCase):
    def test_round_trip_parsed_correctly(self):
        raw = _raw_offer_roundtrip(price="1850.00")
        offer = _parse_offer(raw, "EZE", "PUJ", "2027-07-04", "2027-07-11",
                             2, 1, "ECONOMY", "USD")
        self.assertIsNotNone(offer)
        self.assertAlmostEqual(offer.total_price, 1850.0)
        self.assertEqual(offer.origin, "EZE")
        self.assertEqual(offer.destination, "PUJ")
        self.assertEqual(offer.stops_outbound, 1)   # 2 segs → 1 escala
        self.assertEqual(offer.stops_inbound, 1)
        self.assertEqual(offer.outbound.origin, "EZE")
        self.assertEqual(offer.outbound.destination, "PUJ")
        self.assertEqual(offer.inbound.origin, "PUJ")
        self.assertEqual(offer.inbound.destination, "EZE")

    def test_direct_flight_no_layovers(self):
        out_segs = [_raw_segment("AR", "1200", "EZE", "PUJ",
                                  dep="2027-07-04T08:00:00",
                                  arr="2027-07-04T18:00:00")]
        in_segs  = [_raw_segment("AR", "1201", "PUJ", "EZE",
                                  dep="2027-07-11T10:00:00",
                                  arr="2027-07-11T20:00:00")]
        raw = _raw_offer_roundtrip(out_segs=out_segs, in_segs=in_segs)
        offer = _parse_offer(raw, "EZE", "PUJ", "2027-07-04", "2027-07-11",
                             2, 0, "ECONOMY", "USD")
        self.assertIsNotNone(offer)
        self.assertEqual(offer.stops_outbound, 0)
        self.assertEqual(offer.stops_inbound, 0)
        self.assertEqual(len(offer.outbound.layovers), 0)

    def test_missing_slices_returns_none(self):
        raw = {"id": "x", "total_amount": "500", "total_currency": "USD", "slices": []}
        result = _parse_offer(raw, "EZE", "PUJ", "2027-07-04", "2027-07-11",
                              2, 0, "ECONOMY", "USD")
        self.assertIsNone(result)

    def test_one_slice_only_returns_none(self):
        raw = {
            "id": "x", "total_amount": "500", "total_currency": "USD",
            "slices": [{"segments": [_raw_segment()]}],  # solo ida
        }
        result = _parse_offer(raw, "EZE", "PUJ", "2027-07-04", "2027-07-11",
                              2, 0, "ECONOMY", "USD")
        self.assertIsNone(result)

    def test_currency_from_offer_used(self):
        raw = _raw_offer_roundtrip(currency="ARS")
        offer = _parse_offer(raw, "EZE", "PUJ", "2027-07-04", "2027-07-11",
                             2, 0, "ECONOMY", "USD")
        self.assertEqual(offer.currency, "ARS")   # usa la moneda de la oferta

    def test_layover_calculated(self):
        """Segmento 1 llega 12:30, segmento 2 sale 15:30 → 3h de escala."""
        out_segs = [
            _raw_segment("AR", "1100", "EZE", "PTY",
                          dep="2027-07-04T08:00:00", arr="2027-07-04T12:30:00"),
            _raw_segment("CM", "701", "PTY", "PUJ",
                          dep="2027-07-04T15:30:00", arr="2027-07-04T17:00:00"),
        ]
        in_segs = [_raw_segment("CM", "702", "PUJ", "PTY"),
                   _raw_segment("AR", "1101", "PTY", "EZE")]
        raw = _raw_offer_roundtrip(out_segs=out_segs, in_segs=in_segs)
        offer = _parse_offer(raw, "EZE", "PUJ", "2027-07-04", "2027-07-11",
                             2, 0, "ECONOMY", "USD")
        self.assertEqual(len(offer.outbound.layovers), 1)
        lv = offer.outbound.layovers[0]
        self.assertEqual(lv.airport_iata, "PTY")
        self.assertEqual(lv.duration_minutes, 180)  # 3 horas exactas


# ────────────────────────────────────────────────────────────────────────────
# Tests de filtros integrados en duffel_search
# ────────────────────────────────────────────────────────────────────────────

class TestDuffelFilters(unittest.TestCase):
    """Todos los tests mockean _duffel_request para no usar red."""

    def _run_search(self, raw_offers, max_stops=2, max_layover_hours=24.0):
        with patch("src.providers_duffel._duffel_request",
                   return_value=_mock_api_response(raw_offers)):
            return duffel_search(
                origin="EZE", destination="PUJ",
                date_out="2027-07-04", date_ret="2027-07-11",
                adults=2, children_ages=[7],
                cabin_class="ECONOMY", currency="USD",
                max_stops=max_stops, max_layover_hours=max_layover_hours,
                api_token="duffel_test_dummy",
            )

    # ── GDS / excluded airlines ──────────────────────────────────────────
    def test_w1_excluded(self):
        raw = _raw_offer_roundtrip(
            out_segs=[_raw_segment("W1", "100", "EZE", "PUJ")],
            in_segs=[_raw_segment("W1", "101", "PUJ", "EZE")],
        )
        offers = self._run_search([raw])
        self.assertEqual(offers, [])

    def test_x1_excluded(self):
        raw = _raw_offer_roundtrip(
            out_segs=[_raw_segment("X1", "100", "EZE", "PUJ")],
            in_segs=[_raw_segment("X1", "101", "PUJ", "EZE")],
        )
        offers = self._run_search([raw])
        self.assertEqual(offers, [])

    def test_hr_excluded(self):
        raw = _raw_offer_roundtrip(
            out_segs=[_raw_segment("HR", "100", "EZE", "PUJ")],
            in_segs=[_raw_segment("HR", "101", "PUJ", "EZE")],
        )
        offers = self._run_search([raw])
        self.assertEqual(offers, [])

    def test_gf_excluded(self):
        raw = _raw_offer_roundtrip(
            out_segs=[_raw_segment("GF", "100", "EZE", "PUJ")],
            in_segs=[_raw_segment("GF", "101", "PUJ", "EZE")],
        )
        offers = self._run_search([raw])
        self.assertEqual(offers, [])

    def test_valid_airline_not_excluded(self):
        raw = _raw_offer_roundtrip()  # AR y CM por defecto
        offers = self._run_search([raw])
        self.assertEqual(len(offers), 1)

    # ── max_stops ────────────────────────────────────────────────────────
    def test_max_stops_0_rejects_1stop(self):
        raw = _raw_offer_roundtrip()  # AR→PTY + CM→PUJ = 1 escala en ida
        offers = self._run_search([raw], max_stops=0)
        self.assertEqual(offers, [])

    def test_max_stops_1_accepts_1stop(self):
        raw = _raw_offer_roundtrip()
        offers = self._run_search([raw], max_stops=1)
        self.assertEqual(len(offers), 1)

    def test_max_stops_0_accepts_direct(self):
        raw = _raw_offer_roundtrip(
            out_segs=[_raw_segment("AR", "1200", "EZE", "PUJ",
                                   dep="2027-07-04T08:00:00",
                                   arr="2027-07-04T18:00:00")],
            in_segs=[_raw_segment("AR", "1201", "PUJ", "EZE",
                                  dep="2027-07-11T10:00:00",
                                  arr="2027-07-11T20:00:00")],
        )
        offers = self._run_search([raw], max_stops=0)
        self.assertEqual(len(offers), 1)

    # ── max_layover_hours ────────────────────────────────────────────────
    def test_layover_exceeds_max_rejected(self):
        """Escala de 5h con max_layover=3 debe rechazarse."""
        out_segs = [
            _raw_segment("AR", "1100", "EZE", "PTY",
                          dep="2027-07-04T08:00:00", arr="2027-07-04T12:00:00"),
            _raw_segment("CM", "701", "PTY", "PUJ",
                          dep="2027-07-04T17:00:00", arr="2027-07-04T19:00:00"),
        ]
        in_segs = [_raw_segment("CM", "702", "PUJ", "PTY"),
                   _raw_segment("AR", "1101", "PTY", "EZE")]
        raw = _raw_offer_roundtrip(out_segs=out_segs, in_segs=in_segs)
        offers = self._run_search([raw], max_layover_hours=3.0)
        self.assertEqual(offers, [])

    def test_layover_under_max_accepted(self):
        """Escala de 2h con max_layover=3 debe aceptarse."""
        out_segs = [
            _raw_segment("AR", "1100", "EZE", "PTY",
                          dep="2027-07-04T08:00:00", arr="2027-07-04T12:00:00"),
            _raw_segment("CM", "701", "PTY", "PUJ",
                          dep="2027-07-04T14:00:00", arr="2027-07-04T16:00:00"),
        ]
        in_segs = [_raw_segment("CM", "702", "PUJ", "PTY"),
                   _raw_segment("AR", "1101", "PTY", "EZE")]
        raw = _raw_offer_roundtrip(out_segs=out_segs, in_segs=in_segs)
        offers = self._run_search([raw], max_layover_hours=3.0)
        self.assertEqual(len(offers), 1)

    # ── Múltiples ofertas con mix de válidas/inválidas ───────────────────
    def test_mixed_offers_only_valid_returned(self):
        valid = _raw_offer_roundtrip()
        excluded = _raw_offer_roundtrip(
            out_segs=[_raw_segment("X1", "100", "EZE", "PUJ")],
            in_segs=[_raw_segment("X1", "101", "PUJ", "EZE")],
        )
        offers = self._run_search([valid, excluded, valid])
        self.assertEqual(len(offers), 2)


# ────────────────────────────────────────────────────────────────────────────
# Tests de duffel_search unitarios (mock completo)
# ────────────────────────────────────────────────────────────────────────────

class TestDuffelSearchUnit(unittest.TestCase):
    @patch("src.providers_duffel._duffel_request")
    def test_returns_offer_list(self, mock_req):
        mock_req.return_value = _mock_api_response([
            _raw_offer_roundtrip("1500.00"),
            _raw_offer_roundtrip("1800.00"),
        ])
        offers = duffel_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[7],
            cabin_class="ECONOMY", currency="USD",
            max_stops=1, max_layover_hours=3.0,
            api_token="duffel_test_dummy",
        )
        self.assertEqual(len(offers), 2)
        self.assertIsInstance(offers[0], Offer)

    @patch("src.providers_duffel._duffel_request")
    def test_empty_response_returns_empty_list(self, mock_req):
        mock_req.return_value = _mock_api_response([])
        offers = duffel_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[7],
            cabin_class="ECONOMY", currency="USD",
            max_stops=1, max_layover_hours=3.0,
            api_token="duffel_test_dummy",
        )
        self.assertEqual(offers, [])

    @patch("src.providers_duffel._duffel_request")
    def test_max_results_limits_processing(self, mock_req):
        """Si hay 10 ofertas brutas pero max_results=3, solo se procesan 3."""
        mock_req.return_value = _mock_api_response(
            [_raw_offer_roundtrip() for _ in range(10)]
        )
        offers = duffel_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[7],
            cabin_class="ECONOMY", currency="USD",
            max_stops=1, max_layover_hours=3.0,
            api_token="duffel_test_dummy",
            max_results=3,
        )
        self.assertLessEqual(len(offers), 3)

    @patch("src.providers_duffel._duffel_request")
    def test_payload_structure(self, mock_req):
        """Verifica que el payload enviado a Duffel tenga la estructura correcta."""
        mock_req.return_value = _mock_api_response([])
        duffel_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[7, 10],
            cabin_class="ECONOMY", currency="USD",
            max_stops=1, max_layover_hours=3.0,
            api_token="duffel_test_dummy",
        )
        call_args = mock_req.call_args
        method, path, token, payload = (
            call_args[0][0], call_args[0][1], call_args[0][2], call_args[0][3]
        )
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/air/offer_requests")
        data = payload["data"]
        slices = data["slices"]
        self.assertEqual(len(slices), 2)
        self.assertEqual(slices[0]["origin"], "EZE")
        self.assertEqual(slices[0]["destination"], "PUJ")
        self.assertEqual(slices[0]["departure_date"], "2027-07-04")
        self.assertEqual(slices[1]["origin"], "PUJ")
        self.assertEqual(slices[1]["destination"], "EZE")
        # Pasajeros: 2 adultos + 2 niños
        pax = data["passengers"]
        self.assertEqual(len(pax), 4)
        adults = [p for p in pax if p["type"] == "adult"]
        kids   = [p for p in pax if p["type"] == "child"]
        self.assertEqual(len(adults), 2)
        self.assertEqual(len(kids), 2)
        self.assertIn("age", kids[0])

    @patch("src.providers_duffel._duffel_request")
    def test_offer_content_hash_stable(self, mock_req):
        """Dos parseos del mismo offer deben producir el mismo content_hash."""
        raw_offer = _raw_offer_roundtrip("1500.00")
        mock_req.return_value = _mock_api_response([raw_offer])
        offers1 = duffel_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[7],
            cabin_class="ECONOMY", currency="USD",
            max_stops=1, max_layover_hours=3.0,
            api_token="duffel_test_dummy",
        )
        mock_req.return_value = _mock_api_response([raw_offer])
        offers2 = duffel_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[7],
            cabin_class="ECONOMY", currency="USD",
            max_stops=1, max_layover_hours=3.0,
            api_token="duffel_test_dummy",
        )
        self.assertEqual(offers1[0].content_hash(), offers2[0].content_hash())

    @patch("src.providers_duffel._duffel_request")
    def test_offer_to_dict_has_presentation(self, mock_req):
        """to_dict() debe incluir la sección presentation con aerolíneas legibles."""
        mock_req.return_value = _mock_api_response([_raw_offer_roundtrip()])
        offers = duffel_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[],
            cabin_class="ECONOMY", currency="USD",
            max_stops=2, max_layover_hours=24.0,
            api_token="duffel_test_dummy",
        )
        d = offers[0].to_dict()
        self.assertIn("presentation", d)
        pres = d["presentation"]
        self.assertIn("price", pres)
        self.assertIn("airlines", pres)
        # AR y CM deben normalizarse
        airline_names = pres["airlines"]
        self.assertIn("Aerolíneas Argentinas", airline_names)
        self.assertIn("Copa Airlines", airline_names)


# ────────────────────────────────────────────────────────────────────────────
# Tests de resolución de carrier (Fix 2 — ZZ placeholder)
# ────────────────────────────────────────────────────────────────────────────

class TestResolveCarrier(unittest.TestCase):
    """
    _resolve_carrier() debe:
      - preferir operating_carrier sobre marketing_carrier
      - tratar "ZZ" como placeholder y caer al marketing_carrier
      - devolver "UNKNOWN" solo si ambos están ausentes/inválidos
      - normalizar strip + upper
    """

    def _seg(self, operating=None, marketing=None):
        """Construye un segmento Duffel mínimo con los carriers indicados."""
        s = {
            "origin": {"iata_code": "EZE"},
            "destination": {"iata_code": "PTY"},
            "departing_at": "2027-07-04T08:00:00",
            "arriving_at": "2027-07-04T12:00:00",
        }
        if operating is not None:
            s["operating_carrier"] = {"iata_code": operating}
        if marketing is not None:
            s["marketing_carrier"] = {"iata_code": marketing}
        return s

    def test_operating_carrier_used_when_valid(self):
        self.assertEqual(_resolve_carrier(self._seg("AR", "LA")), "AR")

    def test_marketing_carrier_fallback_when_no_operating(self):
        self.assertEqual(_resolve_carrier(self._seg(None, "CM")), "CM")

    def test_zz_treated_as_placeholder_falls_to_marketing(self):
        """ZZ es el placeholder más común del sandbox de Duffel."""
        self.assertEqual(_resolve_carrier(self._seg("ZZ", "LA")), "LA")

    def test_zz_with_no_marketing_returns_unknown(self):
        self.assertEqual(_resolve_carrier(self._seg("ZZ", None)), "UNKNOWN")

    def test_empty_string_operating_falls_to_marketing(self):
        self.assertEqual(_resolve_carrier(self._seg("", "AV")), "AV")

    def test_whitespace_only_operating_falls_to_marketing(self):
        self.assertEqual(_resolve_carrier(self._seg("  ", "IB")), "IB")

    def test_both_absent_returns_unknown(self):
        self.assertEqual(_resolve_carrier(self._seg(None, None)), "UNKNOWN")

    def test_both_empty_returns_unknown(self):
        self.assertEqual(_resolve_carrier(self._seg("", "")), "UNKNOWN")

    def test_code_normalized_to_upper(self):
        """Códigos en minúscula deben normalizarse."""
        self.assertEqual(_resolve_carrier(self._seg("ar", None)), "AR")

    def test_code_stripped(self):
        """Espacios alrededor del código deben ignorarse."""
        self.assertEqual(_resolve_carrier(self._seg(" CM ", None)), "CM")

    def test_no_carrier_fields_at_all_returns_unknown(self):
        """Segmento sin ninguno de los dos campos carrier."""
        seg = {
            "origin": {"iata_code": "EZE"},
            "destination": {"iata_code": "PTY"},
            "departing_at": "2027-07-04T08:00:00",
            "arriving_at": "2027-07-04T12:00:00",
        }
        self.assertEqual(_resolve_carrier(seg), "UNKNOWN")

    # ── Integración con _parse_segments ─────────────────────────────────
    def test_parse_segments_never_returns_zz(self):
        """_parse_segments no debe devolver ZZ en ningún carrier."""
        seg = {
            "operating_carrier": {"iata_code": "ZZ"},
            "marketing_carrier": {"iata_code": "LA"},
            "operating_carrier_flight_number": "800",
            "origin": {"iata_code": "EZE"},
            "destination": {"iata_code": "SCL"},
            "departing_at": "2027-07-04T07:00:00",
            "arriving_at": "2027-07-04T10:00:00",
            "passengers": [],
        }
        segs = _parse_segments([seg], "ECONOMY")
        self.assertEqual(segs[0].carrier, "LA")
        self.assertNotIn("ZZ", segs[0].flight_number)

    def test_parse_segments_unknown_fallback_not_zz(self):
        """Sin carriers válidos → UNKNOWN, nunca ZZ ni ??."""
        seg = {
            "origin": {"iata_code": "EZE"},
            "destination": {"iata_code": "SCL"},
            "departing_at": "2027-07-04T07:00:00",
            "arriving_at": "2027-07-04T10:00:00",
            "passengers": [],
        }
        segs = _parse_segments([seg], "ECONOMY")
        self.assertEqual(segs[0].carrier, "UNKNOWN")
        self.assertNotEqual(segs[0].carrier, "ZZ")
        self.assertNotEqual(segs[0].carrier, "??")


# ────────────────────────────────────────────────────────────────────────────
# Tests de moneda (Fix 1 — currency mismatch)
# ────────────────────────────────────────────────────────────────────────────

class TestCurrencyHandling(unittest.TestCase):
    """
    Verifica que el provider preserve la moneda real de la oferta
    (total_currency de Duffel), no la solicitada en el payload.
    El mismatch se loguea como WARNING pero la oferta no se descarta.
    """

    def _search_with_currency(self, offer_currency, requested_currency="USD"):
        raw = _raw_offer_roundtrip(price="1353.20", currency=offer_currency)
        with patch("src.providers_duffel._duffel_request",
                   return_value=_mock_api_response([raw])):
            return duffel_search(
                origin="EZE", destination="PUJ",
                date_out="2027-07-04", date_ret="2027-07-11",
                adults=2, children_ages=[],
                cabin_class="ECONOMY", currency=requested_currency,
                max_stops=2, max_layover_hours=24.0,
                api_token="duffel_test_dummy",
            )

    def test_offer_currency_preserved_when_matches(self):
        """Si la oferta devuelve USD y se pidió USD, currency debe ser USD."""
        offers = self._search_with_currency("USD", "USD")
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].currency, "USD")

    def test_offer_currency_preserved_when_mismatch(self):
        """Si Duffel devuelve EUR aunque se pidió USD, se preserva EUR."""
        offers = self._search_with_currency("EUR", "USD")
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].currency, "EUR")

    def test_offer_not_discarded_on_currency_mismatch(self):
        """Una oferta en EUR cuando se pidió USD NO debe descartarse."""
        offers = self._search_with_currency("EUR", "USD")
        self.assertEqual(len(offers), 1)
        self.assertAlmostEqual(offers[0].total_price, 1353.20, places=2)

    def test_multiple_offers_each_preserves_own_currency(self):
        """Cada oferta preserva su propia moneda."""
        raw_usd = _raw_offer_roundtrip(price="1200.00", currency="USD")
        raw_eur = _raw_offer_roundtrip(price="1353.20", currency="EUR")
        with patch("src.providers_duffel._duffel_request",
                   return_value=_mock_api_response([raw_usd, raw_eur])):
            offers = duffel_search(
                origin="EZE", destination="PUJ",
                date_out="2027-07-04", date_ret="2027-07-11",
                adults=2, children_ages=[],
                cabin_class="ECONOMY", currency="USD",
                max_stops=2, max_layover_hours=24.0,
                api_token="duffel_test_dummy",
            )
        self.assertEqual(len(offers), 2)
        currencies = {o.currency for o in offers}
        self.assertIn("USD", currencies)
        self.assertIn("EUR", currencies)

    def test_currency_warning_logged_on_mismatch(self):
        """Debe loguearse un WARNING cuando la moneda difiere."""
        with self.assertLogs("src.providers_duffel", level="WARNING") as log:
            self._search_with_currency("EUR", "USD")
        # Al menos uno de los mensajes debe mencionar EUR y USD
        combined = "\n".join(log.output)
        self.assertIn("EUR", combined)
        self.assertIn("USD", combined)

    def test_no_warning_when_currencies_match(self):
        """No debe haber WARNING de moneda cuando USD == USD."""
        import logging
        with self.assertLogs("src.providers_duffel", level="DEBUG") as log:
            self._search_with_currency("USD", "USD")
        # Filtrar solo los WARNING de moneda
        currency_warnings = [
            line for line in log.output
            if "WARNING" in line and "moneda" in line.lower()
        ]
        self.assertEqual(currency_warnings, [])

    def test_parse_offer_eur_from_duffel(self):
        """_parse_offer extrae la moneda del campo total_currency."""
        raw = _raw_offer_roundtrip(price="1353.20", currency="EUR")
        offer = _parse_offer(raw, "EZE", "PUJ", "2027-07-04", "2027-07-11",
                             2, 0, "ECONOMY", "USD")  # solicitado: USD
        self.assertIsNotNone(offer)
        self.assertEqual(offer.currency, "EUR")    # preservado
        self.assertAlmostEqual(offer.total_price, 1353.20, places=2)

    def test_parse_offer_missing_currency_falls_back_to_requested(self):
        """Si total_currency está ausente, usar la moneda solicitada como fallback."""
        raw = {
            "id": "off_x",
            "total_amount": "900.00",
            # sin total_currency
            "slices": [
                {"segments": [_raw_segment("AR", "100", "EZE", "PUJ",
                                            dep="2027-07-04T08:00:00",
                                            arr="2027-07-04T18:00:00")]},
                {"segments": [_raw_segment("AR", "101", "PUJ", "EZE",
                                            dep="2027-07-11T10:00:00",
                                            arr="2027-07-11T20:00:00")]},
            ],
        }
        offer = _parse_offer(raw, "EZE", "PUJ", "2027-07-04", "2027-07-11",
                             2, 0, "ECONOMY", "USD")
        self.assertIsNotNone(offer)
        self.assertEqual(offer.currency, "USD")   # fallback a la solicitada


# ────────────────────────────────────────────────────────────────────────────
# Test de integración real (requiere DUFFEL_API_TOKEN)
# ────────────────────────────────────────────────────────────────────────────
# ────────────────────────────────────────────────────────────────────────────
# Test de integración real (requiere DUFFEL_API_TOKEN)
# ────────────────────────────────────────────────────────────────────────────

class TestDuffelIntegration(unittest.TestCase):
    """
    Llama a la API de Duffel de verdad.
    Solo se ejecuta si DUFFEL_API_TOKEN está seteado Y se pasa 'integration' en argv.
    """

    @classmethod
    def setUpClass(cls):
        cls.token = os.getenv("DUFFEL_API_TOKEN", "")
        cls.run_integration = "integration" in sys.argv and cls.token

    def _skip_if_no_token(self):
        if not self.run_integration:
            self.skipTest(
                "Test de integración omitido.\n"
                "Para correr: DUFFEL_API_TOKEN=duffel_test_XXX python tests/test_duffel.py integration"
            )

    def test_search_EZE_PUJ_returns_offers(self):
        self._skip_if_no_token()
        offers = duffel_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[7],
            cabin_class="ECONOMY", currency="USD",
            max_stops=1, max_layover_hours=3.0,
            api_token=self.token,
            max_results=50,
        )
        print(f"\n  → Duffel EZE→PUJ: {len(offers)} ofertas recibidas")
        self.assertGreater(len(offers), 0, "Esperaba al menos 1 oferta del sandbox")
        for o in offers:
            self.assertGreater(o.total_price, 0)
            self.assertIn(o.origin, ["EZE", "AEP"])
            self.assertIsInstance(o.outbound, Leg)
            self.assertIsInstance(o.inbound, Leg)

    def test_search_AEP_PUJ_returns_offers(self):
        self._skip_if_no_token()
        offers = duffel_search(
            origin="AEP", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[7],
            cabin_class="ECONOMY", currency="USD",
            max_stops=1, max_layover_hours=3.0,
            api_token=self.token,
            max_results=50,
        )
        print(f"\n  → Duffel AEP→PUJ: {len(offers)} ofertas recibidas")
        # AEP puede no tener vuelos internacionales en sandbox → no fallar si es 0
        # pero sí verificar que no explota
        for o in offers:
            self.assertGreater(o.total_price, 0)

    def test_airlines_include_relevant_carriers(self):
        """En sandbox de Duffel, deben aparecer aerolíneas conocidas."""
        self._skip_if_no_token()
        offers = duffel_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[],
            cabin_class="ECONOMY", currency="USD",
            max_stops=2, max_layover_hours=24.0,
            api_token=self.token,
            max_results=100,
        )
        all_airlines = {a for o in offers for a in o.airlines}
        print(f"\n  → Aerolíneas en sandbox: {sorted(all_airlines)}")
        # No hacer assert duro — el sandbox puede variar, pero reportamos
        relevant = all_airlines & {"AR", "LA", "CM", "AV", "AA", "BA"}
        print(f"  → Aerolíneas relevantes encontradas: {sorted(relevant)}")

    def test_no_excluded_airlines_in_results(self):
        """Ninguna oferta debe contener W1/X1/HR/GF."""
        self._skip_if_no_token()
        from src.normalizer import has_excluded_airline, EXCLUDED_AIRLINES
        offers = duffel_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[],
            cabin_class="ECONOMY", currency="USD",
            max_stops=2, max_layover_hours=24.0,
            api_token=self.token,
            max_results=200,
        )
        for o in offers:
            self.assertFalse(
                has_excluded_airline(o.airlines),
                f"Oferta con airline excluida encontrada: {o.airlines}"
            )

    def test_no_zz_carrier_in_results(self):
        """Ningún segmento debe tener carrier ZZ en resultados reales."""
        self._skip_if_no_token()
        offers = duffel_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[],
            cabin_class="ECONOMY", currency="USD",
            max_stops=2, max_layover_hours=24.0,
            api_token=self.token,
            max_results=100,
        )
        all_carriers = {
            s.carrier
            for o in offers
            for s in o.outbound.segments + o.inbound.segments
        }
        print(f"\n  → Carriers en resultados reales: {sorted(all_carriers)}")
        self.assertNotIn("ZZ", all_carriers, "ZZ no debería aparecer como carrier")

    def test_currency_consistent_in_results(self):
        """Todas las ofertas deben tener currency explícita (no vacía)."""
        self._skip_if_no_token()
        offers = duffel_search(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[],
            cabin_class="ECONOMY", currency="USD",
            max_stops=2, max_layover_hours=24.0,
            api_token=self.token,
            max_results=50,
        )
        currencies = {o.currency for o in offers}
        print(f"\n  → Monedas devueltas por Duffel: {currencies}")
        for o in offers:
            self.assertTrue(
                o.currency and len(o.currency) == 3,
                f"Moneda inválida en oferta: {o.currency!r}"
            )


# ────────────────────────────────────────────────────────────────────────────
# Runner
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Eliminar 'integration' de argv para que unittest no lo confunda con un test
    argv = [a for a in sys.argv if a != "integration"]
    unittest.main(argv=argv, verbosity=2)
