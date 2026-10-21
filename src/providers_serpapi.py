"""
Provider SerpApi — Google Flights vía SerpApi.

Por qué SerpApi
───────────────
  Google Flights indexa directamente GDS + NDC + LCC para la región:
    • AR — Aerolíneas Argentinas
    • LA — LATAM
    • CM — Copa Airlines
    • AV — Avianca
    • 4M — LADE / vuelos regionales
    Y cualquier otra aerolínea que opere la ruta en la fecha dada.

  SerpApi hace el scraping de Google Flights de forma legal y estructurada,
  devolviendo JSON limpio sin que el código tenga que parsear HTML.

Plan gratuito
─────────────
  250 búsquedas/mes — sin tarjeta de crédito.
  Registro: https://serpapi.com/users/sign_up
  Obtener API key: https://serpapi.com/manage-api-key

  CONSUMO DE CUOTA:
    Cada llamada a serpapi_search() gasta 1 crédito de API por llamada HTTP real.
    Para un round-trip se hacen 2 llamadas por origen:
      • Llamada 1 — vuelos de ida  (→ devuelve departure_tokens)
      • Llamada 2 — vuelos de vuelta usando el mejor departure_token
    Total por corrida: 2 × len(ORIGINS) llamadas
    Ejemplo: 2 orígenes × 2 corridas/día × 30 días = 120/mes (dentro del free tier)

Flujo de la API
───────────────
  1. GET /search?engine=google_flights&departure_id=EZE&arrival_id=MIA
                &outbound_date=2026-07-04&return_date=2026-07-11
                &type=1&adults=2&...
     → best_flights[] + other_flights[], cada uno con:
         flights[]: segmentos individuales
         price:     precio total (combinado ida+vuelta)
         departure_token: opaco, necesario para el paso 2

  2. GET /search?...&departure_token=<token_del_vuelo_ida>
     → best_flights[] + other_flights[] de vuelta
         flights[]: segmentos de la vuelta
         price:     precio total del round-trip completo

  ESTRATEGIA DE LLAMADAS:
    No iteramos todos los tokens (eso gastaría demasiados créditos).
    Tomamos los N mejores vuelos de ida (best_flights + top other_flights),
    hacemos UNA sola llamada de vuelta con el mejor token,
    y armamos las combinaciones con los datos que tenemos.

    En la práctica Google Flights ya filtra por precio óptimo en la
    llamada de vuelta, así que 1 llamada de vuelta captura el mercado real.

Variables de entorno
────────────────────
  SERPAPI_API_KEY   — API key de SerpApi (obligatoria)
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import List, Optional, Tuple

from src.normalizer import has_excluded_airline
from src.providers import (
    Leg, LayoverInfo, Offer, Segment,
    _leg_from_segments, _passes_filters,
)

logger = logging.getLogger(__name__)

SERPAPI_BASE = "https://serpapi.com/search"

# Mapeo cabin class interno → parámetro SerpApi
_CABIN_MAP = {
    "ECONOMY":          "1",
    "PREMIUM_ECONOMY":  "2",
    "BUSINESS":         "3",
    "FIRST":            "4",
}

# ---------------------------------------------------------------------------
# HTTP helper — stdlib puro, sin dependencias extra
# ---------------------------------------------------------------------------

def _serpapi_get(params: dict, timeout: int = 30) -> dict:
    """
    Realiza una llamada GET a SerpApi con los parámetros dados.

    SerpApi devuelve JSON sin compresión por defecto, pero aceptamos
    gzip por si acaso para mayor robustez.

    Raises:
        RuntimeError  en errores HTTP (4xx/5xx) con el mensaje de error de SerpApi.
        urllib.error.URLError en problemas de red.
    """
    url = f"{SERPAPI_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Accept-Encoding": "gzip, identity"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            enc = resp.headers.get("Content-Encoding", "")
            if enc == "gzip" or raw[:2] == b"\x1f\x8b":
                import gzip
                raw = gzip.decompress(raw)
            data = json.loads(raw.decode("utf-8"))
            # SerpApi puede devolver 200 con error_message
            if "error" in data:
                raise RuntimeError(f"SerpApi error: {data['error']}")
            return data
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        if raw[:2] == b"\x1f\x8b":
            import gzip
            raw = gzip.decompress(raw)
        try:
            err = json.loads(raw.decode("utf-8"))
            msg = err.get("error", str(err))
        except Exception:
            msg = raw.decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"SerpApi HTTP {exc.code}: {msg}") from exc


# ---------------------------------------------------------------------------
# Parseo de segmentos Google Flights → modelo interno
# ---------------------------------------------------------------------------

def _parse_flight_segment(raw_flight: dict, cabin_class: str) -> Segment:
    """
    Convierte un elemento de flights[] de SerpApi al modelo Segment.

    Campos relevantes de SerpApi Google Flights:
      departure_airport.id / arrival_airport.id     IATA codes
      departure_airport.time / arrival_airport.time "YYYY-MM-DD HH:MM"
      duration    minutos del segmento (int)
      airline     nombre largo ("Aerolíneas Argentinas")
      airline_logo URL
      flight_number "AR 1101"
      extensions  ["1 carry-on bag included", "Wi-Fi for a fee", ...]
    """
    dep_ap = raw_flight.get("departure_airport", {})
    arr_ap = raw_flight.get("arrival_airport", {})

    # Carrier: extraer código IATA del flight_number ("AR 1101" → "AR")
    flight_number_raw = raw_flight.get("flight_number", "")
    parts = flight_number_raw.strip().split()
    if len(parts) >= 2:
        carrier = parts[0].upper()
        flight_num = flight_number_raw.strip()
    elif len(parts) == 1:
        carrier = parts[0].upper()
        flight_num = carrier
    else:
        # Fallback: usar nombre de aerolínea si hay
        carrier = _airline_name_to_iata(raw_flight.get("airline", ""))
        flight_num = carrier

    # Timestamps: SerpApi devuelve "2026-07-04 08:00", normalizar a ISO
    dep_time = _normalize_datetime(dep_ap.get("time", ""))
    arr_time = _normalize_datetime(arr_ap.get("time", ""))

    # Equipaje: buscar "carry-on" / "checked" en extensions
    baggage_included, baggage_pieces = _parse_baggage(raw_flight.get("extensions", []))

    return Segment(
        carrier=carrier,
        flight_number=flight_num,
        departure_iata=dep_ap.get("id", "???"),
        arrival_iata=arr_ap.get("id", "???"),
        departure_time=dep_time,
        arrival_time=arr_time,
        stops=0,          # cada elemento de flights[] es un segmento directo
        cabin_class=cabin_class,
        baggage_included=baggage_included,
        baggage_pieces=baggage_pieces,
        airline_name=str(raw_flight.get("airline") or "").strip(),
    )


def _normalize_datetime(raw: str) -> str:
    """
    Convierte "2026-07-04 08:00" al formato ISO "2026-07-04T08:00:00"
    que espera el modelo Segment para calcular duración.
    """
    if not raw:
        return ""
    raw = raw.strip()
    # Si ya tiene T, devolver tal cual
    if "T" in raw:
        return raw
    # "YYYY-MM-DD HH:MM" → "YYYY-MM-DDTHH:MM:00"
    try:
        parts = raw.split(" ")
        date_part = parts[0]
        time_part = parts[1] if len(parts) > 1 else "00:00"
        if len(time_part.split(":")) == 2:
            time_part += ":00"
        return f"{date_part}T{time_part}"
    except (IndexError, AttributeError):
        return raw


def _parse_baggage(extensions: list) -> Tuple[bool, int]:
    """
    Detecta equipaje facturado incluido en el array extensions de SerpApi.
    Ejemplos de strings que puede contener:
      "1 carry-on bag included"
      "1 checked bag included"
      "2 checked bags included"
      "No carry-on bag"
      "Carbon emissions estimate: 807 kg"
    Devuelve (baggage_included: bool, pieces: int).
    """
    for ext in extensions:
        lower = ext.lower()
        if "checked bag" in lower and "included" in lower:
            # Intentar extraer número
            pieces = 1
            for token in ext.split():
                try:
                    pieces = int(token)
                    break
                except ValueError:
                    continue
            return True, pieces
    return False, 0


def _airline_name_to_iata(name: str) -> str:
    """
    Convierte nombre largo a código IATA cuando no hay flight_number.
    Tabla mínima relevante para la ruta EZE/AEP → MIA.
    """
    _TABLE = {
        "aerolíneas argentinas": "AR",
        "aerolineas argentinas": "AR",
        "latam": "LA",
        "latam airlines": "LA",
        "copa airlines": "CM",
        "copa": "CM",
        "avianca": "AV",
        "american airlines": "AA",
        "american": "AA",
        "united airlines": "UA",
        "united": "UA",
        "arajet": "DM",
        "air europa": "UX",
        "iberia": "IB",
    }
    return _TABLE.get(name.lower().strip(), name[:2].upper() if name else "??")


def _parse_layovers(raw_layovers: list) -> List[LayoverInfo]:
    """
    Convierte layovers[] de SerpApi al modelo LayoverInfo.
    Cada elemento: {"duration": 125, "name": "...", "id": "PTY", "overnight": false}
    """
    result = []
    for lv in raw_layovers:
        airport = lv.get("id", lv.get("name", "???"))
        duration_min = int(lv.get("duration", 0))
        if duration_min > 0:
            result.append(LayoverInfo(
                airport_iata=airport,
                duration_minutes=duration_min,
            ))
    return result


def _build_leg(raw_flights: list, raw_layovers: list, cabin_class: str) -> Optional[Leg]:
    """
    Construye un Leg a partir de la lista flights[] y layovers[] de SerpApi.
    Devuelve None si no hay segmentos válidos.
    """
    if not raw_flights:
        return None
    segments = [_parse_flight_segment(f, cabin_class) for f in raw_flights]
    layovers = _parse_layovers(raw_layovers)
    return Leg(segments=segments, layovers=layovers)


# ---------------------------------------------------------------------------
# Armado de Offers desde la respuesta de SerpApi
# ---------------------------------------------------------------------------

def _extract_flight_groups(response: dict) -> List[dict]:
    """
    Extrae todos los grupos de vuelos de la respuesta SerpApi:
    best_flights[] + other_flights[].
    Cada grupo tiene: flights[], layovers[], price, departure_token, total_duration.
    """
    groups = []
    groups.extend(response.get("best_flights", []))
    groups.extend(response.get("other_flights", []))
    return groups


def _build_offer_from_pair(
    out_group: dict,
    in_group: dict,
    origin: str,
    destination: str,
    date_out: str,
    date_ret: str,
    adults: int,
    children: int,
    cabin_class: str,
    currency: str,
    price_override: Optional[float] = None,
    google_flights_url: Optional[str] = None,
) -> Optional[Offer]:
    """
    Construye un Offer a partir de un grupo de outbound y uno de inbound.
    price_override: precio del inbound group (que en SerpApi es el total combinado).
    """
    out_leg = _build_leg(
        out_group.get("flights", []),
        out_group.get("layovers", []),
        cabin_class,
    )
    in_leg = _build_leg(
        in_group.get("flights", []),
        in_group.get("layovers", []),
        cabin_class,
    )
    if not out_leg or not in_leg:
        return None

    # El precio viene del grupo de vuelta (es el total round-trip combinado)
    price = float(price_override or in_group.get("price") or out_group.get("price") or 0)
    if price <= 0:
        return None

    return Offer(
        origin=out_leg.origin or origin,
        destination=out_leg.destination or destination,
        date_out=date_out,
        date_ret=date_ret,
        total_price=price,
        currency=currency,
        cabin_class=cabin_class,
        outbound=out_leg,
        inbound=in_leg,
        adults=adults,
        children=children,
        raw={"outbound": out_group, "inbound": in_group, "google_flights_url": google_flights_url},
        baggage=_baggage_from_search_groups(out_group, in_group, currency),
    )



# ---------------------------------------------------------------------------
# Equipaje — hints de resultados + verificación por Booking Options
# ---------------------------------------------------------------------------

def _flatten_text_values(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_flatten_text_values(item))
        return out
    return []


def _interpret_baggage_lines(lines: list[str], currency: str) -> dict:
    """Interpreta sólo lo que Google/SerpApi declara; lo ausente queda UNKNOWN."""
    import re
    clean = [str(x).strip() for x in lines if str(x).strip()]
    result = {
        "personal_item": "UNKNOWN",
        "carry_on": "UNKNOWN",
        "checked_bag": "UNKNOWN",
        "checked_bag_price": None,
        "currency": currency,
        "raw": clean,
    }
    for line in clean:
        low = line.lower()
        free = any(x in low for x in ("free", "included", "gratis", "incluido", "incluida", "sin cargo"))
        no_word = any(x in low for x in ("no ", "none", "sin "))
        fee = any(x in low for x in ("for a fee", "por un cargo", "con cargo", "de pago", "por una tarifa"))

        if any(x in low for x in ("personal item", "artículo personal", "articulo personal", "objeto personal")):
            result["personal_item"] = "INCLUDED" if free else ("NOT_INCLUDED" if no_word else ("PAID" if fee else "UNKNOWN"))

        if any(x in low for x in ("carry-on", "carry on", "equipaje de mano", "maleta de mano")):
            result["carry_on"] = "INCLUDED" if free else ("NOT_INCLUDED" if no_word else ("PAID" if fee else "UNKNOWN"))

        if any(x in low for x in ("checked bag", "checked baggage", "equipaje facturado", "equipaje despachado", "equipaje en bodega", "maleta facturada")):
            if free:
                result["checked_bag"] = "INCLUDED"
            elif fee or ":" in line:
                result["checked_bag"] = "PAID"
            elif no_word:
                result["checked_bag"] = "NOT_INCLUDED"
            # SerpApi suele devolver "1st checked bag: 30".
            m = re.search(r"(?:[:$€£]|\bUSD\b|\bARS\b)\s*([0-9]+(?:[.,][0-9]{1,2})?)", line, re.I)
            if not m:
                m = re.search(r"(?:bag|baggage|facturad[oa]|bodega).*?([0-9]+(?:[.,][0-9]{1,2})?)\s*$", line, re.I)
            if m:
                try:
                    result["checked_bag_price"] = float(m.group(1).replace(",", "."))
                    if result["checked_bag"] == "UNKNOWN":
                        result["checked_bag"] = "PAID"
                except ValueError:
                    pass
    return result


def _group_baggage_extensions(group: dict) -> list[str]:
    lines = _flatten_text_values(group.get("extensions"))
    # Algunas pistas aparecen por segmento.
    for f in group.get("flights", []) or []:
        lines.extend(_flatten_text_values(f.get("extensions")))
    return [x for x in lines if any(k in x.lower() for k in ("bag", "baggage", "equipaje", "maleta", "artículo personal", "articulo personal"))]


def _baggage_from_search_groups(out_group: dict, in_group: dict, currency: str) -> dict:
    out_lines = _group_baggage_extensions(out_group)
    in_lines = _group_baggage_extensions(in_group)
    status = "PARTIAL" if (out_lines or in_lines) else "NOT_CHECKED"
    return {
        "status": status,
        "source": "Google Flights results",
        "outbound": _interpret_baggage_lines(out_lines, currency),
        "inbound": _interpret_baggage_lines(in_lines, currency),
        "note": "Información preliminar de resultados; Booking Options permite verificar la tarifa seleccionada." if status == "PARTIAL" else "Equipaje todavía no verificado para esta tarifa.",
    }


def _booking_option_rows(response: dict, currency: str, search_price: float) -> list[dict]:
    """Normaliza Booking Options de ida+vuelta del itinerario exacto.

    Sólo consideramos nodos ``together`` como precios round-trip comparables.
    Las opciones de ida/vuelta separadas no se suman automáticamente porque
    podrían corresponder a vendedores o condiciones diferentes.
    """
    rows: list[dict] = []
    for opt in response.get("booking_options", []) or []:
        node = opt.get("together")
        if not isinstance(node, dict):
            continue
        try:
            price = float(node.get("price"))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        seller = str(node.get("book_with") or node.get("seller") or "Vendedor no informado").strip()
        local_prices = []
        for lp in node.get("local_prices", []) or []:
            if not isinstance(lp, dict):
                continue
            try:
                local_prices.append({"currency": str(lp.get("currency") or ""), "price": float(lp.get("price"))})
            except (TypeError, ValueError):
                continue
        rows.append({
            "seller": seller,
            "price": price,
            "currency": currency,
            "is_airline": bool(node.get("airline")),
            "fare": str(node.get("option_title") or "").strip(),
            "extensions": _flatten_text_values(node.get("extensions")),
            "baggage_prices": _flatten_text_values(node.get("baggage_prices")),
            "local_prices": local_prices,
        })
    rows.sort(key=lambda r: (r["price"], 0 if r["is_airline"] else 1, r["seller"].lower()))
    return rows


def _booking_summary(response: dict, currency: str, search_price: float) -> dict:
    rows = _booking_option_rows(response, currency, search_price)
    if not rows:
        return {
            "status": "UNAVAILABLE",
            "source": "Google Flights · Booking Options (SerpApi)",
            "currency": currency,
            "search_price": float(search_price or 0),
            "lowest": None,
            "airline_direct": None,
            "options": [],
            "note": "Google no devolvió opciones de reserva round-trip estructuradas para este itinerario.",
        }
    lowest = rows[0]
    airline_rows = [r for r in rows if r.get("is_airline")]
    direct = min(airline_rows, key=lambda r: r["price"]) if airline_rows else None
    return {
        "status": "VERIFIED",
        "source": "Google Flights · Booking Options (SerpApi)",
        "currency": currency,
        "search_price": float(search_price or 0),
        "lowest": lowest,
        "airline_direct": direct,
        "options": rows[:12],
        "note": "Precio reservable verificado para el itinerario exacto. Puede variar entre vendedores y familias tarifarias.",
    }


def _booking_baggage_lines(response: dict, offer_price: float) -> tuple[list[str], list[str], list[str]]:
    """Devuelve (outbound, inbound, together) de Booking Options."""
    bp = response.get("baggage_prices")
    out_lines: list[str] = []
    in_lines: list[str] = []
    together: list[str] = []
    if isinstance(bp, dict):
        out_lines = _flatten_text_values(bp.get("departing") or bp.get("outbound"))
        in_lines = _flatten_text_values(bp.get("returning") or bp.get("inbound"))
        together = _flatten_text_values(bp.get("together"))
    elif isinstance(bp, list):
        together = _flatten_text_values(bp)

    if out_lines or in_lines or together:
        return out_lines, in_lines, together

    # Fallback: usar primero el equipaje de la opción round-trip reservable más barata.
    # Así el resumen de equipaje queda alineado con el precio principal mostrado.
    candidates = []
    for opt in response.get("booking_options", []) or []:
        for key in ("together", "departing", "returning"):
            node = opt.get(key)
            if not isinstance(node, dict):
                continue
            lines = _flatten_text_values(node.get("baggage_prices"))
            if not lines:
                continue
            try:
                price = float(node.get("price")) if node.get("price") is not None else 10**12
            except Exception:
                price = 10**12
            candidates.append((price, key, lines))
    for _, key, lines in sorted(candidates, key=lambda x: x[0]):
        if key == "together" and not together:
            together = lines
        elif key == "departing" and not out_lines:
            out_lines = lines
        elif key == "returning" and not in_lines:
            in_lines = lines
    return out_lines, in_lines, together


def _selected_flights_payload(offer: Offer) -> dict:
    """Construye selected_flights_json según la API oficial de SerpApi.

    Cada segmento lleva flight_number sin espacios, departure_id, arrival_id y
    la fecha local de salida YYYY-MM-DD. De esta forma Booking Options queda
    anclado al itinerario exacto y no depende de un booking_token opaco.
    """
    def leg_payload(leg) -> list[dict]:
        rows = []
        for seg in leg.segments:
            fn = "".join(str(seg.flight_number or "").split())
            dep = str(seg.departure_iata or "").strip().upper()
            arr = str(seg.arrival_iata or "").strip().upper()
            dep_time = str(seg.departure_time or "")
            date = dep_time[:10] if len(dep_time) >= 10 else ""
            if not (fn and dep and arr and date):
                return []
            rows.append({
                "flight_number": fn,
                "departure_id": dep,
                "arrival_id": arr,
                "date": date,
            })
        return rows

    outbound = leg_payload(offer.outbound)
    inbound = leg_payload(offer.inbound)
    if not outbound or not inbound:
        return {}
    return {"outbound": outbound, "return": inbound}


def enrich_offer_baggage(
    offer: Offer, *, api_key: str, gl: str = "ar", hl: str = "es",
    timeout: int = 30,
) -> tuple[dict, int]:
    """Verifica equipaje de un itinerario concreto. Retorna (resumen, consultas_usadas).

    V2.3.4: usamos selected_flights_json en vez de booking_token. SerpApi
    documenta este mecanismo para pedir Booking Options fijando exactamente
    cada segmento de ida y vuelta. Evita respuestas 400 por contexto faltante
    y es más auditable porque el request describe el itinerario completo.
    """
    raw = offer.raw or {}
    selected = _selected_flights_payload(offer)
    if not selected:
        existing = offer.baggage or _baggage_from_search_groups(raw.get("outbound") or {}, raw.get("inbound") or {}, offer.currency)
        existing = dict(existing)
        existing["status"] = "PARTIAL" if existing.get("status") != "NOT_CHECKED" else "UNAVAILABLE"
        existing["note"] = "No fue posible construir el itinerario exacto para consultar Booking Options; no se consumió una búsqueda adicional."
        offer.baggage = existing
        return existing, 0

    params = {
        "engine": "google_flights",
        "api_key": api_key,
        "selected_flights_json": json.dumps(selected, separators=(",", ":"), ensure_ascii=False),
        # Los campos superiores son redundantes pero válidos si coinciden y
        # hacen el request más explícito/tolerante ante cambios del proveedor.
        "departure_id": offer.origin,
        "arrival_id": offer.destination,
        "outbound_date": offer.date_out,
        "return_date": offer.date_ret,
        "type": "1",
        "adults": str(max(1, int(offer.adults or 1))),
        "travel_class": _CABIN_MAP.get(str(offer.cabin_class or "ECONOMY").upper(), "1"),
        "currency": offer.currency,
        "hl": (hl or "es").lower(),
        "gl": (gl or "ar").lower(),
    }
    if int(offer.children or 0) > 0:
        params["children"] = str(int(offer.children))

    try:
        response = _serpapi_get(params, timeout=timeout)
        booking = _booking_summary(response, offer.currency, offer.total_price)
        offer.booking = booking
        exact_url = ((response.get("search_metadata") or {}).get("google_flights_url"))
        if exact_url:
            if not isinstance(offer.raw, dict):
                offer.raw = {}
            offer.raw["exact_google_flights_url"] = exact_url
        out_lines, in_lines, together = _booking_baggage_lines(response, offer.total_price)
        if together:
            if not out_lines:
                out_lines = together
            if not in_lines:
                in_lines = together
        out = _interpret_baggage_lines(out_lines, offer.currency)
        inn = _interpret_baggage_lines(in_lines, offer.currency)
        status = "VERIFIED" if (out_lines or in_lines) else "UNAVAILABLE"
        summary = {
            "status": status,
            "source": "Google Flights · Booking Options (SerpApi)",
            "outbound": out,
            "inbound": inn,
            "google_flights_exact_url": exact_url,
            "note": (
                "Equipaje informado por Google para las opciones de reserva del itinerario exacto. Puede variar entre vendedores/tarifas; lo no informado queda como desconocido."
                if status == "VERIFIED" else
                "Booking Options no informó franquicia de equipaje para este itinerario."
            ),
        }
        offer.baggage = summary
        return summary, 1
    except Exception as exc:
        offer.booking = {
            "status": "ERROR",
            "source": "Google Flights · Booking Options (SerpApi)",
            "currency": offer.currency,
            "search_price": float(offer.total_price or 0),
            "lowest": None,
            "airline_direct": None,
            "options": [],
            "note": f"No se pudieron verificar opciones de reserva: {exc}",
        }
        existing = dict(offer.baggage or {})
        existing.update({
            "status": "ERROR",
            "source": "Google Flights · Booking Options (SerpApi)",
            "note": f"No se pudo verificar equipaje: {exc}",
        })
        offer.baggage = existing
        logger.warning("SerpApi equipaje: no se pudo verificar %s — %s", offer.itinerary_hash, exc)
        return existing, 1

def _selected_flights_payload_from_dict(offer: dict) -> dict:
    """Construye selected_flights_json desde una oferta serializada."""
    def leg_payload(leg: dict) -> list[dict]:
        rows = []
        for seg in (leg or {}).get("segments", []) or []:
            fn = "".join(str(seg.get("flight") or seg.get("flight_number") or "").split())
            dep = str(seg.get("from") or seg.get("departure_id") or "").strip().upper()
            arr = str(seg.get("to") or seg.get("arrival_id") or "").strip().upper()
            dep_time = str(seg.get("dep") or seg.get("departure_time") or "")
            date = dep_time[:10] if len(dep_time) >= 10 else ""
            if not (fn and dep and arr and date):
                return []
            rows.append({"flight_number": fn, "departure_id": dep, "arrival_id": arr, "date": date})
        return rows
    outbound = leg_payload(offer.get("outbound") or {})
    inbound = leg_payload(offer.get("inbound") or {})
    return {"outbound": outbound, "return": inbound} if outbound and inbound else {}


def resolve_exact_google_flights_details(
    offer: dict, *, api_key: str, gl: str = "ar", hl: str = "es", timeout: int = 30
) -> dict:
    """Resuelve URL exacta + precios reservables + equipaje del itinerario."""
    selected = _selected_flights_payload_from_dict(offer or {})
    if not selected:
        raise ValueError("No se pudo reconstruir el itinerario exacto de esta oferta.")
    currency = str((offer or {}).get("currency") or "USD")
    search_price = float((offer or {}).get("total_price") or 0)
    params = {
        "engine": "google_flights",
        "api_key": api_key,
        "selected_flights_json": json.dumps(selected, separators=(",", ":"), ensure_ascii=False),
        "type": "1",
        "adults": str(max(1, int((offer or {}).get("adults") or 1))),
        "travel_class": _CABIN_MAP.get(str((offer or {}).get("cabin_class") or "ECONOMY").upper(), "1"),
        "currency": currency,
        "hl": (hl or "es").lower(),
        "gl": (gl or "ar").lower(),
    }
    children = int((offer or {}).get("children") or 0)
    if children > 0:
        params["children"] = str(children)
    response = _serpapi_get(params, timeout=timeout)
    url = ((response.get("search_metadata") or {}).get("google_flights_url"))
    if not url:
        raise ValueError("SerpApi no devolvió un enlace de Google Flights para el itinerario seleccionado.")
    out_lines, in_lines, together = _booking_baggage_lines(response, search_price)
    if together:
        if not out_lines:
            out_lines = together
        if not in_lines:
            in_lines = together
    baggage = {
        "status": "VERIFIED" if (out_lines or in_lines) else "UNAVAILABLE",
        "source": "Google Flights · Booking Options (SerpApi)",
        "outbound": _interpret_baggage_lines(out_lines, currency),
        "inbound": _interpret_baggage_lines(in_lines, currency),
        "google_flights_exact_url": str(url),
        "note": "Equipaje informado por Google para las opciones de reserva del itinerario exacto; lo no informado queda como desconocido.",
    }
    return {
        "url": str(url),
        "booking": _booking_summary(response, currency, search_price),
        "baggage": baggage,
    }


def resolve_exact_google_flights_url(
    offer: dict, *, api_key: str, gl: str = "ar", hl: str = "es", timeout: int = 30
) -> str:
    """Compatibilidad: devuelve sólo el deep-link exacto."""
    return resolve_exact_google_flights_details(
        offer, api_key=api_key, gl=gl, hl=hl, timeout=timeout
    )["url"]


# ---------------------------------------------------------------------------
# Búsqueda principal
# ---------------------------------------------------------------------------

def serpapi_search(
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
    # Cuántos departure_tokens de vuelos de ida usar para consultar vuelta
    # 1 = 2 llamadas totales (mínimo crédito), 3 = 4 llamadas (más combinaciones)
    # Para el free tier de 250/mes recomendamos 1 (2 llamadas por origen × 2 orígenes = 4/corrida)
    max_outbound_tokens: int = 1,
    show_hidden: bool = True,
    gl: str = "ar",
    hl: str = "es",
    max_retries: int = 3,
    backoff_base: float = 2.0,
    diagnostics: Optional[dict] = None,
) -> List[Offer]:
    """
    Busca vuelos round-trip vía SerpApi Google Flights.

    Consumo de créditos por llamada a esta función:
      (1 + max_outbound_tokens) créditos

    Con max_outbound_tokens=1 (default): 2 créditos/origen/corrida.
    Ejemplo con 2 orígenes × 2 corridas/día × 30 días = 120 créditos/mes.
    El free tier otorga 250/mes → recomendamos 1 corrida/día con max_outbound_tokens=1,
    o configurar solo 1 origen en ORIGINS.

    Devuelve lista de Offer filtrada, rankeada externamente por rank_offers().
    """
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.clear()
    diagnostics.update({
        "outbound_detected": 0,
        "outbound_explored": 0,
        "queries_used": 0,
        "coverage_status": "UNKNOWN",
    })

    if not api_key:
        raise RuntimeError(
            "SerpApi API key no configurada.\n"
            "Registrarse gratis en https://serpapi.com/users/sign_up\n"
            "y setear: SERPAPI_API_KEY=<tu_key> en .env o config.yaml"
        )

    children = len(children_ages)
    cabin_num = _CABIN_MAP.get(cabin_class.upper(), "1")

    # Parámetros base para ambas llamadas
    base_params = {
        "engine":         "google_flights",
        "api_key":        api_key,
        "departure_id":   origin,
        "arrival_id":     destination,
        "outbound_date":  date_out,
        "return_date":    date_ret,
        "type":           "1",              # 1 = Round trip
        "adults":         str(adults),
        "travel_class":   cabin_num,
        "currency":       currency,
        "hl":             (hl or "es").lower(),
        "gl":             (gl or "ar").lower(),
        # deep_search=true busca reproducir la experiencia del browser.
        "deep_search":    "true",
        # Incluye lo que Google deja detrás de "Ver más vuelos".
        "show_hidden":    "true" if show_hidden else "false",
        # Priorizar precio para que los departure_tokens disponibles sean útiles.
        "sort_by":        "2",
    }

    # Filtro nativo de Google Flights/SerpApi. Nuestra semántica interna es
    # 0=directo, 1=una escala o menos; SerpApi usa 1=directo, 2=1 escala o menos.
    if max_stops is not None:
        if max_stops <= 0:
            base_params["stops"] = "1"
        elif max_stops == 1:
            base_params["stops"] = "2"
        elif max_stops == 2:
            base_params["stops"] = "3"
        # >2 no tiene filtro nativo equivalente; se valida localmente.

    # SerpApi soporta filtro nativo de duración de escala. Lo usamos para
    # reducir resultados inútiles y, aun así, mantenemos la validación local
    # como regla de seguridad.
    if max_layover_hours is not None:
        max_layover_minutes = max(1, int(round(float(max_layover_hours) * 60)))
        base_params["layover_duration"] = f"1,{max_layover_minutes}"

    if children > 0:
        base_params["children"] = str(children)

    # ── Llamada 1: vuelos de ida ─────────────────────────────────────────────
    diagnostics["queries_used"] += 1
    outbound_response = _call_with_retry(
        base_params, max_retries, backoff_base, label="outbound"
    )
    if outbound_response is None:
        return []

    outbound_google_url = ((outbound_response.get("search_metadata") or {}).get("google_flights_url"))
    outbound_groups = _extract_flight_groups(outbound_response)
    if not outbound_groups:
        logger.warning("SerpApi: sin vuelos de ida para %s→%s", origin, destination)
        return []

    diagnostics["outbound_detected"] = len(outbound_groups)
    logger.info(
        "SerpApi: %d vuelo(s) de ida recibidos para %s→%s | hidden=%s | gl=%s hl=%s",
        len(outbound_groups), origin, destination, bool(show_hidden), (gl or "ar").lower(), (hl or "es").lower(),
    )

    # Tomar los mejores tokens de ida (best_flights tienen prioridad)
    best_outbound = outbound_response.get("best_flights", [])
    other_outbound = outbound_response.get("other_flights", [])
    # Ordenar: best primero, luego other — tomamos max_outbound_tokens
    candidate_groups = (best_outbound + other_outbound)[:max_outbound_tokens]
    tokens_to_use = [
        (g, g.get("departure_token"))
        for g in candidate_groups
        if g.get("departure_token")
    ]

    diagnostics["outbound_explored"] = len(tokens_to_use)
    diagnostics["coverage_status"] = (
        "COMPLETE" if len(tokens_to_use) >= len(outbound_groups) else "PARTIAL"
    )

    logger.info(
        "SerpApi: cobertura %s — %d/%d salidas exploradas (límite=%d)",
        diagnostics["coverage_status"], len(tokens_to_use), len(outbound_groups), max_outbound_tokens,
    )

    if not tokens_to_use:
        logger.warning(
            "SerpApi: ningún vuelo de ida tiene departure_token — "
            "no se puede obtener vuelos de vuelta"
        )
        return []

    # ── Llamadas 2…N: vuelos de vuelta para cada token ───────────────────────
    all_offers: List[Offer] = []

    for out_group, dep_token in tokens_to_use:
        inbound_params = {**base_params, "departure_token": dep_token}
        diagnostics["queries_used"] += 1
        inbound_response = _call_with_retry(
            inbound_params, max_retries, backoff_base, label="inbound"
        )
        if inbound_response is None:
            continue

        inbound_google_url = ((inbound_response.get("search_metadata") or {}).get("google_flights_url")) or outbound_google_url
        inbound_groups = _extract_flight_groups(inbound_response)
        if not inbound_groups:
            logger.debug("SerpApi: sin vuelos de vuelta para token %s…", dep_token[:20])
            continue

        logger.info(
            "SerpApi: %d vuelo(s) de vuelta recibidos (token %s…)",
            len(inbound_groups), dep_token[:12],
        )

        # Combinar outbound con todos los inbound disponibles
        for in_group in inbound_groups:
            offer = _build_offer_from_pair(
                out_group=out_group,
                in_group=in_group,
                origin=origin,
                destination=destination,
                date_out=date_out,
                date_ret=date_ret,
                adults=adults,
                children=children,
                cabin_class=cabin_class,
                currency=currency,
                google_flights_url=inbound_google_url,
            )
            if offer is None:
                continue

            # Filtrar pseudo-aerolíneas
            if has_excluded_airline(offer.airlines):
                logger.debug("SerpApi: descartada por airline excluida — %s", offer.airlines)
                continue

            # Aplicar filtros de escalas y layover
            ok, reason = _passes_filters(offer, max_stops, max_layover_hours)
            if not ok:
                logger.debug("SerpApi: descartada — %s", reason)
                continue

            all_offers.append(offer)

    # V2: NO usamos ofertas parciales/fallback. Para un monitor estricto, una
    # oferta sólo es válida si ida y vuelta están detalladas y ambas cumplen
    # MAX_STOPS y MAX_LAYOVER_HOURS. Esto evita alertas sobre una vuelta no
    # verificada.
    if not all_offers:
        logger.warning(
            "SerpApi: no hubo ofertas round-trip completas que cumplan los filtros "
            "(%s escala(s) máx/tramo, %.2fh máx por escala).",
            max_stops if max_stops is not None else -1,
            max_layover_hours if max_layover_hours is not None else -1.0,
        )

    logger.info(
        "SerpApi: %d oferta(s) aceptadas para %s→%s",
        len(all_offers), origin, destination,
    )
    return all_offers


def _call_with_retry(
    params: dict,
    max_retries: int,
    backoff_base: float,
    label: str,
) -> Optional[dict]:
    """
    Ejecuta _serpapi_get con reintentos y backoff exponencial.
    Devuelve el dict de respuesta o None si todos los intentos fallaron.
    """
    for attempt in range(1, max_retries + 1):
        try:
            return _serpapi_get(params)
        except Exception as exc:
            if attempt == max_retries:
                logger.error(
                    "SerpApi [%s]: %d intentos fallidos — %s",
                    label, max_retries, exc,
                    extra={"serpapi_error": str(exc)},
                )
                return None
            wait = backoff_base ** attempt
            logger.warning(
                "SerpApi [%s]: intento %d/%d falló (%s). Reintentando en %.0fs…",
                label, attempt, max_retries, exc, wait,
                extra={"attempt": attempt, "wait_seconds": wait},
            )
            time.sleep(wait)
    return None
