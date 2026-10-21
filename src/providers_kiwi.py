"""
Provider: Kiwi Tequila API v2
──────────────────────────────────────────────────────────────────────────────
API REST gratuita (requiere key de registro gratuito en tequila.kiwi.com).

Por qué Kiwi y no otras opciones
──────────────────────────────────
  Google Flights scraping → JS dinámico + Cloudflare anti-bot. Necesita
      Playwright/Selenium. Frágil, lento, TOS violation. Inviable.

  Travelpayouts → caché histórico, no tiempo real. Requiere white-label
      partner agreement. No sirve para monitoreo de precio actual.

  Duffel sandbox → datos ficticios generados. No refleja disponibilidad real.

  Aviationstack / AviationEdge → estado operativo de vuelos (delays, gates),
      NO búsqueda de disponibilidad ni precios futuros.

  SkyScanner unofficial → endpoints privados sin documentar que cambian
      constantemente. TOS violation. No viable.

  Kiwi Tequila API v2 →
    ✅ REST con documentación oficial completa
    ✅ Key gratuita, sin tarjeta de crédito
    ✅ Devuelve segmentos con IATA, carrier, horarios, precio en divisa solicitada
    ✅ Cubre AR, LA, CM, AV, AA en rutas EZE/AEP→MIA
    ✅ Sin dependencias externas (solo urllib de stdlib)
    ✅ Campo route[].return = 0/1 separa ida/vuelta sin heurísticas frágiles

Cómo obtener la API key (gratuita, sin tarjeta)
─────────────────────────────────────────────────
  1. Ir a https://tequila.kiwi.com/
  2. Registrarse con nombre + email (no requiere tarjeta)
  3. Crear un "Solution" → copiar la API key del dashboard
  4. Agregar a .env:      KIWI_API_KEY=tu_clave_aqui
     O a config.yaml:     kiwi_api_key: "tu_clave_aqui"
  5. Cambiar el provider: PRICE_PROVIDER=kiwi

Rate limits (tier gratuito)
─────────────────────────────
  ~10 req/s sin límite mensual documentado para búsqueda.
  El agente hace 1 request por origen × corrida → muy por debajo del límite.

Documentación oficial de la API
─────────────────────────────────
  https://tequila.kiwi.com/portal/docs/tequila-api/search_api
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import List, Optional

from src.providers import (
    Offer,
    Segment,
    _leg_from_segments,
    _passes_filters,
)
from src.normalizer import has_excluded_airline

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

KIWI_SEARCH_URL = "https://api.tequila.kiwi.com/v2/search"

# Kiwi usa códigos de cabina distintos a los de Amadeus / nuestro sistema
_CABIN_MAP: dict[str, str] = {
    "ECONOMY":         "M",
    "PREMIUM_ECONOMY": "W",
    "BUSINESS":        "C",
    "FIRST":           "F",
}


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _clean_dt(raw: str) -> str:
    """
    Normaliza cualquier timestamp de Kiwi a ISO-8601 sin zona horaria.

    Formatos que devuelve Kiwi:
      "2026-07-04T10:30:00.000Z"  →  "2026-07-04T10:30:00"
      "2026-07-04T10:30:00Z"      →  "2026-07-04T10:30:00"
      "2026-07-04T10:30:00"       →  "2026-07-04T10:30:00"
    """
    s = raw.split("Z")[0].split(".")[0]
    return s[:19]


def _parse_segments(route_items: list, cabin_class: str) -> list[Segment]:
    """
    Convierte items de ruta de la respuesta Kiwi en objetos Segment.

    Campos relevantes de cada item de ruta:
      airline         → código IATA del operador (ej: "AR", "LA", "CM")
      flight_no       → número sin carrier prefix (ej: "1400")
      flyFrom         → IATA aeropuerto origen del segmento
      flyTo           → IATA aeropuerto destino del segmento
      local_departure → datetime de salida en hora local del aeropuerto
      local_arrival   → datetime de llegada en hora local del aeropuerto
    """
    segs: list[Segment] = []
    for item in route_items:
        carrier   = item.get("airline", "")
        flight_no = str(item.get("flight_no", ""))
        full_fn   = f"{carrier}{flight_no}" if flight_no else carrier

        dep = _clean_dt(item.get("local_departure", "1970-01-01T00:00:00"))
        arr = _clean_dt(item.get("local_arrival",   "1970-01-01T00:00:00"))

        segs.append(Segment(
            carrier=carrier,
            flight_number=full_fn,
            departure_iata=item.get("flyFrom", ""),
            arrival_iata=item.get("flyTo",   ""),
            departure_time=dep,
            arrival_time=arr,
            cabin_class=cabin_class,
            # Kiwi v2/search no devuelve piezas de equipaje por segmento
            baggage_included=False,
            baggage_pieces=0,
        ))
    return segs


def _split_legs(route: list) -> tuple[list, list]:
    """
    Separa la lista plana de segmentos Kiwi en tramo de ida y tramo de vuelta.

    Estrategia primaria: el campo 'return' (0 = ida, 1 = vuelta) que Kiwi
    incluye en cada item de ruta.

    Fallback (cuando 'return' no está): detectar el corte buscando el primer
    lugar donde el flyFrom del siguiente segmento no continúa del flyTo del
    anterior (es decir, comienza un nuevo tramo desde el destino).
    """
    # Estrategia primaria: campo 'return'
    if route and "return" in route[0]:
        outbound = [r for r in route if r.get("return", 0) == 0]
        inbound  = [r for r in route if r.get("return", 0) == 1]
        if outbound and inbound:
            return outbound, inbound

    # Fallback: detectar corte por discontinuidad de flyFrom/flyTo
    cut = len(route) // 2  # default: mitad
    for i in range(1, len(route)):
        if route[i].get("flyFrom") != route[i - 1].get("flyTo"):
            cut = i
            break

    outbound = route[:cut]
    inbound  = route[cut:]
    return outbound, inbound


def _http_get(
    url: str,
    headers: dict,
    params: dict,
    max_retries: int,
    backoff_base: float,
) -> dict:
    """
    HTTP GET con urllib (stdlib, sin dependencias externas).

    Comportamiento por código de respuesta:
      200 → devuelve dict parseado del JSON
      401 → falla inmediata con mensaje claro (API key inválida)
      400 → falla inmediata con detalle del error (params inválidos)
      429 → retry con backoff (rate limit)
      5xx → retry con backoff (error transitorio del servidor)
      red  → retry con backoff (timeout, DNS, etc.)
    """
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full_url, headers=headers)
    last_exc: Exception = RuntimeError("sin intentos realizados")

    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)

        except urllib.error.HTTPError as exc:
            last_exc = exc
            status = exc.code

            if status == 401:
                body = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    "Kiwi API: clave inválida (HTTP 401).\n"
                    "  1. Registrarse en https://tequila.kiwi.com/ (gratuito, sin tarjeta)\n"
                    "  2. Crear un 'Solution' y copiar la API key\n"
                    "  3. Agregar al .env:  KIWI_API_KEY=<tu_clave>\n"
                    f"  Respuesta del servidor: {body[:300]}"
                ) from exc

            if status == 400:
                body = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Kiwi API: parámetros inválidos (HTTP 400).\n"
                    f"  Detalle: {body[:400]}"
                ) from exc

            # 429 rate-limit o 5xx transitorios → retry
            if status == 429 or status >= 500:
                if attempt == max_retries:
                    logger.error(
                        "Kiwi: %d intentos fallidos con HTTP %d",
                        max_retries, status,
                        extra={"http_status": status},
                    )
                    raise
                wait = backoff_base ** attempt
                logger.warning(
                    "Kiwi: HTTP %d en intento %d/%d — reintentando en %.0fs…",
                    status, attempt, max_retries, wait,
                )
                time.sleep(wait)
                continue

            # Cualquier otro 4xx es definitivo
            raise

        except OSError as exc:
            # Errores de red: timeout, DNS failure, connection refused, etc.
            last_exc = exc
            if attempt == max_retries:
                logger.error(
                    "Kiwi: %d intentos fallidos — %s",
                    max_retries, exc,
                    extra={"kiwi_network_error": str(exc)},
                )
                raise
            wait = backoff_base ** attempt
            logger.warning(
                "Kiwi: error de red en intento %d/%d (%s) — reintentando en %.0fs…",
                attempt, max_retries, exc, wait,
            )
            time.sleep(wait)

    raise last_exc


# ---------------------------------------------------------------------------
# Función pública de búsqueda
# ---------------------------------------------------------------------------

def kiwi_search(
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
    api_key: str,
    max_results: int = 50,
    max_retries: int = 3,
    backoff_base: float = 2.0,
) -> List[Offer]:
    """
    Busca vuelos de ida y vuelta usando Kiwi Tequila API v2.

    Devuelve List[Offer] ya filtrados (escalas, layover, carriers excluidos)
    y en orden de precio ascendente (el ranking global lo hace rank_offers
    en agent.py después de consolidar todos los orígenes).

    Notas sobre precio
    ──────────────────
    Kiwi devuelve el precio POR PERSONA. Multiplicamos por pax_total
    (adults + children) para obtener el precio total del grupo.
    """
    if not api_key:
        raise RuntimeError(
            "KIWI_API_KEY no configurado.\n"
            "  1. Registrarse en https://tequila.kiwi.com/ (gratuito, sin tarjeta)\n"
            "  2. Crear un 'Solution' y copiar la API key del dashboard\n"
            "  3. Agregar al .env:      KIWI_API_KEY=<tu_clave>\n"
            "     O en config.yaml:     kiwi_api_key: \"<tu_clave>\"\n"
            "  4. Cambiar el provider:  PRICE_PROVIDER=kiwi"
        )

    def to_kiwi_date(iso_date: str) -> str:
        """Convierte YYYY-MM-DD al formato DD/MM/YYYY que acepta Kiwi."""
        return datetime.fromisoformat(iso_date).strftime("%d/%m/%Y")

    params: dict = {
        "fly_from":        origin,
        "fly_to":          destination,
        "date_from":       to_kiwi_date(date_out),
        "date_to":         to_kiwi_date(date_out),    # fecha exacta
        "return_from":     to_kiwi_date(date_ret),
        "return_to":       to_kiwi_date(date_ret),    # fecha exacta
        "flight_type":     "round",
        "adults":          adults,
        "curr":            currency,
        "selected_cabins": _CABIN_MAP.get(cabin_class, "M"),
        "sort":            "price",
        "limit":           max_results,
        "one_for_city":    0,    # incluye todas las combinaciones horarias
    }
    if children_ages:
        params["children"] = len(children_ages)
    if max_stops is not None:
        # Kiwi filtra escalas del itinerario completo (ida+vuelta).
        # Nuestra semántica MAX_STOPS es por tramo → multiplicar ×2
        # para dar el margen equivalente sin sobre-filtrar.
        params["max_stopovers"] = max_stops * 2

    headers = {
        "apikey":       api_key,
        "Content-Type": "application/json",
        "User-Agent":   "flight-monitor/2.0",
    }

    logger.info(
        "Kiwi: %s → %s | %s / %s | cabin=%s | curr=%s | limit=%d",
        origin, destination, date_out, date_ret,
        cabin_class, currency, max_results,
    )

    data = _http_get(
        KIWI_SEARCH_URL, headers, params,
        max_retries=max_retries, backoff_base=backoff_base,
    )

    raw_itins  = data.get("data", [])
    pax_total  = adults + len(children_ages)
    logger.info("Kiwi: %d itinerarios crudos recibidos", len(raw_itins))

    offers:     List[Offer] = []
    excluded_n  = 0
    parse_err_n = 0

    for raw in raw_itins:
        try:
            # Precio por persona → precio total del grupo
            price_per_pax = float(raw.get("price", 0))
            price_total   = round(price_per_pax * pax_total, 2)

            route = raw.get("route", [])
            if not route:
                logger.debug("Kiwi: itinerario sin ruta, descartado")
                continue

            out_items, in_items = _split_legs(route)
            if not out_items or not in_items:
                logger.debug("Kiwi: no se pudo separar ida/vuelta, descartado")
                continue

            out_segs = _parse_segments(out_items, cabin_class)
            in_segs  = _parse_segments(in_items,  cabin_class)

            if not out_segs or not in_segs:
                logger.debug("Kiwi: segmentos vacíos tras parseo, descartado")
                continue

            offer = Offer(
                origin=origin,
                destination=destination,
                date_out=date_out,
                date_ret=date_ret,
                total_price=price_total,
                currency=currency,
                cabin_class=cabin_class,
                outbound=_leg_from_segments(out_segs),
                inbound=_leg_from_segments(in_segs),
                adults=adults,
                children=len(children_ages),
                raw=raw,
            )

            # Filtro 1: carriers excluidos (GDS / ticketing pseudo-carriers)
            if has_excluded_airline(offer.airlines):
                excluded_n += 1
                logger.debug("Kiwi: excluida — carrier excluido %s", offer.airlines)
                continue

            # Filtro 2: escalas por tramo y tiempo de layover
            ok, reason = _passes_filters(offer, max_stops, max_layover_hours)
            if ok:
                offers.append(offer)
            else:
                logger.debug("Kiwi: descartada — %s", reason)

        except (KeyError, ValueError, TypeError, IndexError) as exc:
            parse_err_n += 1
            logger.warning(
                "Kiwi: error parseando itinerario: %s", exc,
                extra={"parse_error": str(exc)},
            )

    if excluded_n:
        logger.info("Kiwi: %d oferta(s) excluida(s) por carrier filter", excluded_n)
    if parse_err_n:
        logger.warning("Kiwi: %d itinerario(s) con error de parseo", parse_err_n)
    logger.info("Kiwi: %d oferta(s) válidas tras filtros", len(offers))

    return offers
