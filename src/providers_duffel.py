"""
Provider Duffel — Flight Offers Search via Duffel API v2.

Duffel es una API REST moderna que conecta directamente a GDS, NDC y LCC.
Cobertura relevante para EZE/AEP → MIA:
  • AR — Aerolíneas Argentinas  (conexión directa NDC)
  • LA — LATAM                   (integración completa)
  • CM — Copa Airlines           (primer partner NDC de Duffel)
  • AV — Avianca                 (vía GDS)
  • AA — American Airlines       (NDC)
  • BA — British Airways         (NDC)
  • y +300 más

Documentación: https://duffel.com/docs/api/flights

Registro gratuito: https://app.duffel.com/join
  → Crear organización → Developers → Access tokens → New token
  → Tipo: duffel_test_XXX  (sandbox, sin costo)
        ó duffel_live_XXX  (producción, requiere activación)

Variables de entorno:
  DUFFEL_API_TOKEN   — token Bearer (duffel_test_XXX o duffel_live_XXX)

Flujo de la API:
  1. POST /air/offer_requests  →  crea búsqueda, devuelve offer_request.id + offers[]
  2. (opcional) GET /air/offers/{id}  →  refresca precio de oferta individual

Modelo interno:
  Offer  (precio, aerolíneas, duración, escalas)
    └── Leg      (ida / vuelta)
         └── Segment  (segmento de vuelo individual)
              └── LayoverInfo  (escala calculada entre segmentos)
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import List, Optional

from src.normalizer import has_excluded_airline
from src.providers import (
    Leg, LayoverInfo, Offer, Segment,
    _leg_from_segments, _passes_filters,
)

logger = logging.getLogger(__name__)

DUFFEL_API_BASE = "https://api.duffel.com"
# La versión va SOLO en el header Duffel-Version, NO en la URL.
# URL correcta: https://api.duffel.com/air/offer_requests  (sin prefijo /v2/)
DUFFEL_API_VERSION = "v2"


# ---------------------------------------------------------------------------
# Helpers HTTP (sin dependencias externas)
# ---------------------------------------------------------------------------

def _decompress_if_gzip(data: bytes) -> bytes:
    """Descomprime si los primeros 2 bytes son el magic number gzip \x1f\x8b.
    Funciona tanto para respuestas de éxito como para cuerpos de error."""
    if data[:2] == b"\x1f\x8b":
        import gzip
        return gzip.decompress(data)
    return data


def _duffel_request(
    method: str,
    path: str,
    token: str,
    payload: Optional[dict] = None,
    timeout: int = 30,
) -> dict:
    """
    Realiza una llamada HTTP a la API de Duffel.
    Usa sólo stdlib (urllib) — no requiere requests ni httpx.

    Bugs corregidos:
      • URL sin prefijo de versión: https://api.duffel.com/air/offer_requests
        (antes era https://api.duffel.com/v2/air/offer_requests → 404)
      • Gzip decodificado por magic bytes en éxito Y en error
        (antes: Content-Encoding solo verificado en éxito; error devolvía bytes crudos)

    Raises:
        RuntimeError si la respuesta HTTP es un error (4xx / 5xx).
        urllib.error.URLError en problemas de red.
    """
    # URL: base + path directo, sin prefijo de versión
    url = f"{DUFFEL_API_BASE}{path}"
    headers = {
        "Authorization":   f"Bearer {token}",
        "Duffel-Version":  DUFFEL_API_VERSION,   # versión solo en header
        "Accept":          "application/json",
        "Accept-Encoding": "gzip",
        "Content-Type":    "application/json",
    }
    body = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = _decompress_if_gzip(resp.read())
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Los errores también pueden venir comprimidos — siempre descomprimir
        body_bytes = _decompress_if_gzip(exc.read())
        try:
            err_json = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            err_json = {"raw": body_bytes.decode("utf-8", errors="replace")}
        raise RuntimeError(
            f"Duffel HTTP {exc.code} en {method} {path}: {err_json}"
        ) from exc


# ---------------------------------------------------------------------------
# Conversión de la respuesta Duffel → modelo interno
# ---------------------------------------------------------------------------

_CABIN_MAP = {
    "economy":        "ECONOMY",
    "premium_economy": "PREMIUM_ECONOMY",
    "business":       "BUSINESS",
    "first":          "FIRST",
}

_CABIN_MAP_REVERSE = {v: k for k, v in _CABIN_MAP.items()}


# Códigos IATA que Duffel usa como placeholder cuando el operating carrier
# no está disponible. Deben tratarse como ausentes y caer al marketing carrier.
# "ZZ" es el más común en el sandbox de Duffel (no representa una aerolínea real).
_DUFFEL_PLACEHOLDER_CARRIERS = frozenset({"ZZ", "??"})


def _resolve_carrier(raw_segment: dict) -> str:
    """
    Determina el código IATA del carrier para un segmento Duffel.

    Prioridad:
      1. operating_carrier.iata_code  (si existe y no es placeholder)
      2. marketing_carrier.iata_code  (fallback)
      3. "UNKNOWN"                    (ninguno disponible)

    Normalización: strip + upper. Strings vacíos o solo espacios → ausente.
    """
    def _clean(code) -> Optional[str]:
        if not code:
            return None
        c = str(code).strip().upper()
        if not c or c in _DUFFEL_PLACEHOLDER_CARRIERS:
            return None
        return c

    operating = _clean(
        raw_segment.get("operating_carrier", {}).get("iata_code")
    )
    marketing = _clean(
        raw_segment.get("marketing_carrier", {}).get("iata_code")
    )

    carrier = operating or marketing
    if carrier is None:
        logger.debug(
            "Duffel: carrier no resolvible en segmento %s→%s — usando UNKNOWN",
            raw_segment.get("origin", {}).get("iata_code", "?"),
            raw_segment.get("destination", {}).get("iata_code", "?"),
        )
        return "UNKNOWN"
    return carrier


def _parse_segments(raw_segments: list, cabin_class: str) -> List[Segment]:
    """
    Convierte la lista 'segments' de una slice Duffel al modelo Segment.

    Carrier resolution (ver _resolve_carrier):
      - operating_carrier tiene prioridad sobre marketing_carrier
      - "ZZ" y otros placeholders de Duffel se tratan como ausentes
      - fallback final: "UNKNOWN" (nunca "??" ni "ZZ" en el output)

    Campos Duffel utilizados por segmento:
      operating/marketing_carrier.iata_code
      operating/marketing_carrier_flight_number
      origin/destination.iata_code
      departing_at / arriving_at
      passengers[0].baggages[].{type, quantity}
    """
    segs: List[Segment] = []
    for s in raw_segments:
        carrier = _resolve_carrier(s)
        flight_num = (
            s.get("operating_carrier_flight_number")
            or s.get("marketing_carrier_flight_number", "")
        )
        full_flight_num = f"{carrier}{flight_num}" if flight_num else carrier

        # Equipaje: primer pasajero, primer segmento
        bags_included = False
        bags_count = 0
        passengers = s.get("passengers", [])
        if passengers:
            baggages = passengers[0].get("baggages", [])
            for b in baggages:
                if b.get("type") == "checked":
                    qty = b.get("quantity", 0)
                    if qty > 0:
                        bags_included = True
                        bags_count = qty
                        break

        segs.append(Segment(
            carrier=carrier,
            flight_number=full_flight_num,
            departure_iata=s.get("origin", {}).get("iata_code", "???"),
            arrival_iata=s.get("destination", {}).get("iata_code", "???"),
            departure_time=s.get("departing_at", ""),
            arrival_time=s.get("arriving_at", ""),
            stops=s.get("stops", 0),
            cabin_class=cabin_class,
            baggage_included=bags_included,
            baggage_pieces=bags_count,
        ))
    return segs


def _parse_offer(raw: dict, origin: str, destination: str,
                 date_out: str, date_ret: str, adults: int, children: int,
                 cabin_class: str, currency: str) -> Optional[Offer]:
    """
    Convierte un offer Duffel al modelo Offer interno.
    Devuelve None si no se puede parsear o si le faltan slices.

    Moneda: se usa total_currency del offer (fuente de verdad de Duffel).
    Si difiere de la moneda solicitada se loguea WARNING — el caller
    (duffel_search) decide si descartar o no la oferta.
    """
    try:
        # Precio — usar total_currency de la oferta, no la solicitada
        price = float(raw.get("total_amount", 0))
        offer_currency = raw.get("total_currency") or currency
        if offer_currency != currency:
            logger.warning(
                "Duffel: moneda de oferta (%s) difiere de la solicitada (%s) "
                "— se preserva la moneda real de la oferta",
                offer_currency, currency,
            )

        slices = raw.get("slices", [])
        if len(slices) < 2:
            logger.debug("Duffel: oferta ignorada — menos de 2 slices (%d)", len(slices))
            return None

        out_segs = _parse_segments(slices[0].get("segments", []), cabin_class)
        in_segs  = _parse_segments(slices[1].get("segments", []), cabin_class)

        if not out_segs or not in_segs:
            logger.debug("Duffel: oferta ignorada — sin segmentos válidos")
            return None

        return Offer(
            origin=origin, destination=destination,
            date_out=date_out, date_ret=date_ret,
            total_price=price, currency=offer_currency,
            cabin_class=cabin_class,
            outbound=_leg_from_segments(out_segs),
            inbound=_leg_from_segments(in_segs),
            adults=adults, children=children,
            raw=raw,
        )
    except (KeyError, ValueError, IndexError, TypeError) as exc:
        logger.warning("Duffel: no se pudo parsear oferta — %s", exc,
                       extra={"parse_error": str(exc)})
        return None


# ---------------------------------------------------------------------------
# Búsqueda principal
# ---------------------------------------------------------------------------

def duffel_search(
    *,
    origin: str,
    destination: str,
    date_out: str,
    date_ret: str,
    adults: int,
    children_ages: List[int],
    cabin_class: str,
    currency: str,
    max_stops: Optional[int],
    max_layover_hours: Optional[float],
    api_token: str,
    max_results: int = 200,
    max_retries: int = 3,
    backoff_base: float = 2.0,
) -> List[Offer]:
    """
    Busca vuelos round-trip vía Duffel API.

    Parámetros:
        api_token     — Bearer token (duffel_test_XXX o duffel_live_XXX)
        max_results   — máximo de ofertas a procesar (Duffel puede devolver cientos)
        max_retries   — reintentos ante errores transitorios
        backoff_base  — base para backoff exponencial (wait = base^intento segundos)

    Devuelve lista de Offer filtrada y con exclusión de GDS carriers.
    """
    if not api_token:
        raise RuntimeError(
            "Duffel API token no configurado.\n"
            "Registrarse en https://app.duffel.com/join (gratis)\n"
            "y setear: DUFFEL_API_TOKEN=duffel_test_XXX en .env o config.yaml"
        )

    # Construir pasajeros
    passengers: List[dict] = [{"type": "adult"} for _ in range(adults)]
    for age in children_ages:
        passengers.append({"type": "child", "age": age})

    # Mapear cabin class al formato Duffel
    duffel_cabin = _CABIN_MAP_REVERSE.get(cabin_class.upper(), "economy")

    # Payload para crear el offer_request
    payload = {
        "data": {
            "slices": [
                {
                    "origin":          origin,
                    "destination":     destination,
                    "departure_date":  date_out,
                    "cabin_class":     duffel_cabin,
                },
                {
                    "origin":          destination,
                    "destination":     origin,
                    "departure_date":  date_ret,
                    "cabin_class":     duffel_cabin,
                },
            ],
            "passengers": passengers,
            "currency": currency,
            "return_offers": True,
        }
    }

    # Llamada con retry + backoff exponencial
    raw_response: Optional[dict] = None
    last_exc: Exception = RuntimeError("no attempt made")
    for attempt in range(1, max_retries + 1):
        try:
            raw_response = _duffel_request(
                "POST", "/air/offer_requests", api_token, payload
            )
            break
        except Exception as exc:
            last_exc = exc
            if attempt == max_retries:
                logger.error(
                    "Duffel: %d intentos fallidos — %s", max_retries, exc,
                    extra={"duffel_error": str(exc)},
                )
                raise RuntimeError(
                    f"Duffel API no disponible tras {max_retries} intentos: {exc}"
                ) from exc
            wait = backoff_base ** attempt
            logger.warning(
                "Duffel: intento %d/%d falló (%s). Reintentando en %.0fs…",
                attempt, max_retries, exc, wait,
                extra={"attempt": attempt, "wait_seconds": wait},
            )
            time.sleep(wait)

    assert raw_response is not None  # type narrowing

    raw_offers = raw_response.get("data", {}).get("offers", [])
    logger.info("Duffel: %d ofertas brutas recibidas para %s→%s", len(raw_offers), origin, destination)

    # Limitar procesamiento
    raw_offers = raw_offers[:max_results]

    offers: List[Offer] = []
    excluded_count = 0
    filtered_count = 0

    for raw in raw_offers:
        offer = _parse_offer(
            raw, origin=origin, destination=destination,
            date_out=date_out, date_ret=date_ret,
            adults=adults, children=len(children_ages),
            cabin_class=cabin_class, currency=currency,
        )
        if offer is None:
            continue

        # Filtrar GDS carriers / pseudoaerolíneas
        if has_excluded_airline(offer.airlines):
            excluded_count += 1
            logger.debug("Duffel: descartada por airline excluida — %s", offer.airlines)
            continue

        # Filtrar por escalas y layover
        ok, reason = _passes_filters(offer, max_stops, max_layover_hours)
        if not ok:
            filtered_count += 1
            logger.debug("Duffel: descartada — %s", reason)
            continue

        offers.append(offer)

    logger.info(
        "Duffel: %d oferta(s) aceptadas (%d excluidas por airline, %d por filtros)",
        len(offers), excluded_count, filtered_count,
    )
    return offers
