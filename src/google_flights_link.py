"""Construcción y normalización de deep-links de Google Flights.

Google Flights codifica la búsqueda estructurada en el parámetro ``tfs``.
Este módulo implementa sólo el subconjunto mínimo que necesita Flight Sentinel:
ida y vuelta, aeropuertos IATA, fechas, pasajeros y cabina.

Cuando SerpApi devuelve ``search_metadata.google_flights_url`` se prioriza esa
URL porque refleja exactamente la búsqueda realizada. Para snapshots antiguos
que guardaron el enlace legacy ``?q=Flights+...`` se genera localmente un deep
link equivalente, sin consumir ninguna consulta externa.
"""
from __future__ import annotations

import base64
from urllib.parse import urlencode, urlparse, parse_qs


def _varint(value: int) -> bytes:
    value = int(value)
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field: int, wire_type: int) -> bytes:
    return _varint((int(field) << 3) | int(wire_type))


def _string(field: int, value: str) -> bytes:
    raw = str(value).encode("utf-8")
    return _tag(field, 2) + _varint(len(raw)) + raw


def _submessage(field: int, payload: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(payload)) + payload


def _airport(code: str) -> bytes:
    return _string(2, str(code).strip().upper())


def _leg(date: str, origin: str, destination: str, max_stops: int | None = 1) -> bytes:
    payload = bytearray()
    payload += _string(2, date)
    if max_stops is not None:
        payload += _tag(5, 0) + _varint(max(0, int(max_stops)))
    payload += _submessage(13, _airport(origin))
    payload += _submessage(14, _airport(destination))
    return bytes(payload)


def _seat_value(cabin_class: str) -> int:
    key = str(cabin_class or "ECONOMY").upper().replace("-", "_").replace(" ", "_")
    return {
        "ECONOMY": 1,
        "PREMIUM_ECONOMY": 2,
        "BUSINESS": 3,
        "FIRST": 4,
        "FIRST_CLASS": 4,
    }.get(key, 1)


def build_google_flights_url(
    *,
    origin: str,
    destination: str,
    date_out: str,
    date_ret: str,
    adults: int = 1,
    children: int = 0,
    cabin_class: str = "ECONOMY",
    currency: str = "USD",
    language: str = "es",
    country: str = "ar",
    max_stops: int | None = 1,
) -> str:
    """Genera un deep-link estructurado de Google Flights para ida y vuelta."""
    origin = str(origin or "").strip().upper()
    destination = str(destination or "").strip().upper()
    if len(origin) != 3 or len(destination) != 3 or not date_out or not date_ret:
        return "https://www.google.com/travel/flights"

    payload = bytearray()
    payload += _submessage(3, _leg(date_out, origin, destination, max_stops))
    payload += _submessage(3, _leg(date_ret, destination, origin, max_stops))

    # Packed enum: 1=adult, 2=child. Flight Sentinel no modela bebés aquí.
    pax = bytes([1] * max(1, int(adults or 1)) + [2] * max(0, int(children or 0)))
    payload += _tag(8, 2) + _varint(len(pax)) + pax

    payload += _tag(9, 0) + _varint(_seat_value(cabin_class))
    payload += _tag(19, 0) + _varint(1)  # round trip

    tfs = base64.urlsafe_b64encode(bytes(payload)).decode("ascii").rstrip("=")
    params = {
        "tfs": tfs,
        "tfu": "EgYIABAAGAA",
        "curr": str(currency or "USD").upper(),
        "hl": str(language or "es").lower(),
        "gl": str(country or "ar").lower(),
    }
    return "https://www.google.com/travel/flights/search?" + urlencode(params)


def build_google_flights_url_from_offer(offer: dict) -> str:
    offer = offer or {}
    return build_google_flights_url(
        origin=offer.get("origin") or "",
        destination=offer.get("destination") or "",
        date_out=offer.get("date_out") or "",
        date_ret=offer.get("date_ret") or "",
        adults=int(offer.get("adults") or 1),
        children=int(offer.get("children") or 0),
        cabin_class=offer.get("cabin_class") or "ECONOMY",
        currency=offer.get("currency") or "USD",
        max_stops=1,
    )


def is_structured_google_flights_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(str(url))
        if "google." not in parsed.netloc.lower() or "/travel/flights" not in parsed.path:
            return False
        return bool(parse_qs(parsed.query).get("tfs"))
    except Exception:
        return False


def normalize_google_flights_url(offer: dict, existing_url: str | None = None) -> str:
    """Usa un deep-link real si existe; migra en memoria los links legacy."""
    if is_structured_google_flights_url(existing_url):
        return str(existing_url)
    return build_google_flights_url_from_offer(offer)
