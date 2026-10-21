"""WATCH gratuito basado en Fli (paquete ``flights``).

Fli interactúa con Google Flights mediante ingeniería inversa y no requiere API
key. En V2.5 se usa sólo como sensor frecuente; los cambios importantes se
confirman con SerpApi antes de notificarse como verificados.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import Any, List

from src.providers import Offer, Segment, _leg_from_segments, _passes_filters, rank_offers

logger = logging.getLogger(__name__)


class FliUnavailable(RuntimeError):
    pass


def _imports():
    try:
        from fli.models import (
            Airport, DateSearchFilters, FlightSearchFilters, FlightSegment, LayoverRestrictions,
            MaxStops, PassengerInfo, SeatType, SortBy, TripType,
        )
        from fli.search import SearchDates, SearchFlights
        return {
            "Airport": Airport, "DateSearchFilters": DateSearchFilters,
            "FlightSearchFilters": FlightSearchFilters,
            "FlightSegment": FlightSegment, "LayoverRestrictions": LayoverRestrictions,
            "MaxStops": MaxStops, "PassengerInfo": PassengerInfo, "SeatType": SeatType,
            "SortBy": SortBy, "TripType": TripType, "SearchFlights": SearchFlights,
            "SearchDates": SearchDates,
        }
    except Exception as exc:
        raise FliUnavailable(
            "Motor WATCH gratuito no instalado. En GitHub Actions se instala con requirements-cloud.txt (paquete flights==0.9.0)."
        ) from exc


def _enum_value(v: Any) -> str:
    return str(getattr(v, "value", v) or "")


def _enum_name(v: Any) -> str:
    """Nombre simbólico de Enum (p.ej. EZE, AR) si existe."""
    return str(getattr(v, "name", "") or "").strip().upper()


def _airport_iata(v: Any) -> str:
    """Normaliza Airport de Fli 0.9.x a código IATA de 3 letras.

    En Fli 0.9.0 ``Airport.EZE.name`` es EZE mientras ``.value`` puede ser
    el nombre largo del aeropuerto. No debemos enviar ese nombre a SerpApi.
    """
    name = _enum_name(v)
    if len(name) == 3 and name.isalpha():
        return name
    raw = _enum_value(v).strip().upper()
    if len(raw) == 3 and raw.isalpha():
        return raw
    aliases = {
        "MINISTRO PISTARINI INTERNATIONAL AIRPORT": "EZE",
        "JORGE NEWBERY AIRPORT": "AEP",
        "AEROPARQUE JORGE NEWBERY": "AEP",
        "MIAMI INTERNATIONAL AIRPORT": "MIA",
    }
    return aliases.get(raw, raw)


def _airline_iata(v: Any, flight_number: str = "", airline_name: str = "") -> str:
    """Normaliza carrier de Fli 0.9.x sin confundir IDs internos con IATA."""
    name = _enum_name(v)
    if 2 <= len(name) <= 3 and name.isalnum() and not name.isdigit():
        return name
    raw = _enum_value(v).strip().upper()
    if 2 <= len(raw) <= 3 and raw.isalnum() and not raw.isdigit():
        return raw
    # Algunas respuestas traen el prefijo IATA dentro del número de vuelo.
    token = str(flight_number or "").strip().upper().split()[0] if str(flight_number or "").strip() else ""
    prefix = "".join(ch for ch in token if ch.isalpha())
    if 2 <= len(prefix) <= 3:
        return prefix[:3]
    # Respaldo mínimo para carriers de esta ruta cuando Fli sólo devuelve nombre.
    by_name = {
        "AEROLINEAS ARGENTINAS": "AR", "AEROLÍNEAS ARGENTINAS": "AR",
        "AMERICAN AIRLINES": "AA", "LATAM AIRLINES": "LA", "LATAM": "LA",
        "COPA AIRLINES": "CM", "COPA": "CM", "AVIANCA": "AV",
        "DELTA AIR LINES": "DL", "DELTA": "DL",
    }
    return by_name.get(str(airline_name or "").strip().upper(), "")


def _iso_dt(value: Any) -> str:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(timespec="seconds")
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
        return dt.replace(tzinfo=None).isoformat(timespec="seconds")
    except Exception:
        pass
    # Formatos habituales: YYYY-MM-DD HH:MM
    try:
        dt = datetime.strptime(raw[:16], "%Y-%m-%d %H:%M")
        return dt.isoformat(timespec="seconds")
    except Exception:
        return raw.replace(" ", "T")


def _segment(leg, cabin_class: str) -> Segment:
    fn_raw = str(getattr(leg, "flight_number", "") or "").strip()
    airline_name = str(getattr(leg, "airline_name", "") or "")
    carrier = _airline_iata(getattr(leg, "airline", ""), fn_raw, airline_name)
    if fn_raw and carrier and not fn_raw.upper().startswith(carrier):
        flight_number = f"{carrier} {fn_raw}"
    else:
        flight_number = fn_raw or carrier
    dep = _airport_iata(getattr(leg, "departure_airport", ""))
    arr = _airport_iata(getattr(leg, "arrival_airport", ""))
    return Segment(
        carrier=carrier or "??", flight_number=flight_number,
        departure_iata=dep, arrival_iata=arr,
        departure_time=_iso_dt(getattr(leg, "departure_datetime", "")),
        arrival_time=_iso_dt(getattr(leg, "arrival_datetime", "")),
        cabin_class=cabin_class,
        airline_name=airline_name,
    )


def _seat_type(models: dict, cabin: str):
    seat = models["SeatType"]
    name = str(cabin or "ECONOMY").upper()
    aliases = {"PREMIUM_ECONOMY": "PREMIUM_ECONOMY"}
    return getattr(seat, aliases.get(name, name), getattr(seat, "ECONOMY"))


def _airport(Airport, code: str):
    try:
        return getattr(Airport, code.upper())
    except AttributeError as exc:
        raise FliUnavailable(f"Fli no reconoce el aeropuerto {code}") from exc


def search_fli_offers(cfg) -> List[Offer]:
    """Busca ida/vuelta por EZE y AEP sin consumir SerpApi.

    La política de 1 escala y <=120 minutos se aplica tanto en Fli como otra vez
    sobre nuestro modelo interno antes de aceptar la oferta.
    """
    m = _imports()
    all_offers: List[Offer] = []
    children = len(getattr(cfg, "CHILDREN_AGES", []) or [])
    max_layover = int(round(float(getattr(cfg, "MAX_LAYOVER_HOURS", 2.0) or 2.0) * 60))

    for origin in list(getattr(cfg, "ORIGINS", ["EZE", "AEP"])):
        outbound = m["FlightSegment"](
            departure_airport=[[_airport(m["Airport"], origin), 0]],
            arrival_airport=[[_airport(m["Airport"], cfg.DESTINATION), 0]],
            travel_date=cfg.DATE_OUTBOUND,
        )
        inbound = m["FlightSegment"](
            departure_airport=[[_airport(m["Airport"], cfg.DESTINATION), 0]],
            arrival_airport=[[_airport(m["Airport"], origin), 0]],
            travel_date=cfg.DATE_RETURN,
        )
        filters = m["FlightSearchFilters"](
            trip_type=m["TripType"].ROUND_TRIP,
            passenger_info=m["PassengerInfo"](adults=int(cfg.ADULTS), children=children),
            flight_segments=[outbound, inbound],
            seat_type=_seat_type(m, cfg.CABIN_CLASS),
            stops=m["MaxStops"].ONE_STOP_OR_FEWER,
            layover_restrictions=m["LayoverRestrictions"](max_duration=max_layover),
            sort_by=m["SortBy"].CHEAPEST,
        )
        results = m["SearchFlights"]().search(
            filters, currency=cfg.CURRENCY,
            language="es-AR", country="AR",
        ) or []
        logger.info("WATCH Fli: %s→%s recibió %d combinación(es)", origin, cfg.DESTINATION, len(results))
        for pair in results:
            if not isinstance(pair, (tuple, list)) or len(pair) < 2:
                continue
            out_result, in_result = pair[0], pair[1]
            out_segments = [_segment(x, cfg.CABIN_CLASS) for x in (getattr(out_result, "legs", []) or [])]
            in_segments = [_segment(x, cfg.CABIN_CLASS) for x in (getattr(in_result, "legs", []) or [])]
            if not out_segments or not in_segments:
                continue
            try:
                price = float(getattr(out_result, "price"))
            except Exception:
                continue
            offer = Offer(
                origin=origin, destination=cfg.DESTINATION,
                date_out=cfg.DATE_OUTBOUND, date_ret=cfg.DATE_RETURN,
                total_price=price, currency=cfg.CURRENCY, cabin_class=cfg.CABIN_CLASS,
                outbound=_leg_from_segments(out_segments), inbound=_leg_from_segments(in_segments),
                adults=int(cfg.ADULTS), children=children,
                raw={"watch_source": "Fli", "watch_unverified": True},
            )
            ok, reason = _passes_filters(offer, cfg.MAX_STOPS, cfg.MAX_LAYOVER_HOURS)
            if ok:
                all_offers.append(offer)
            else:
                logger.debug("WATCH Fli descarta %s: %s", offer.itinerary_str, reason)
    # Dedupe por itinerario + precio.
    unique = {}
    for offer in all_offers:
        unique[(offer.itinerary_hash, round(offer.total_price, 2))] = offer
    return rank_offers(list(unique.values()))



def search_fli_flexible_dates(cfg, flex_days: int) -> list[dict]:
    """Explora una grilla de fechas alrededor de la pareja base sin usar SerpApi.

    Fli 0.9.0 expone ``SearchDates`` sobre el calendario de Google Flights. Para
    cubrir cambios independientes de ida y vuelta (no sólo desplazar ambas fechas
    juntas), se consulta cada duración posible entre ``base_duration-2N`` y
    ``base_duration+2N`` y luego se conserva únicamente la ventana ±N de ambas
    fechas. El resultado es un sensor de precio: no implica precio reservable
    hasta que una combinación sea verificada por SerpApi.
    """
    n = max(0, min(3, int(flex_days or 0)))
    if n <= 0:
        return []

    m = _imports()
    base_out = date.fromisoformat(str(cfg.DATE_OUTBOUND))
    base_ret = date.fromisoformat(str(cfg.DATE_RETURN))
    min_out, max_out = base_out - timedelta(days=n), base_out + timedelta(days=n)
    min_ret, max_ret = base_ret - timedelta(days=n), base_ret + timedelta(days=n)
    base_duration = (base_ret - base_out).days
    min_duration = max(1, base_duration - 2 * n)
    max_duration = base_duration + 2 * n
    children = len(getattr(cfg, "CHILDREN_AGES", []) or [])
    max_layover = int(round(float(getattr(cfg, "MAX_LAYOVER_HOURS", 2.0) or 2.0) * 60))
    searcher = m["SearchDates"]()
    rows: dict[tuple[str, str], dict] = {}

    origins = list(getattr(cfg, "ORIGINS", ["EZE", "AEP"]))
    origin_enums = [_airport(m["Airport"], origin) for origin in origins]
    destination_enum = _airport(m["Airport"], cfg.DESTINATION)
    origin_label = ",".join(origins)
    # SearchDates acepta múltiples aeropuertos en un mismo segmento. Consultamos
    # EZE+AEP juntos para no duplicar las llamadas gratuitas al calendario.
    for duration in range(min_duration, max_duration + 1):
        first_return = base_out + timedelta(days=duration)
        outbound = m["FlightSegment"](
            departure_airport=[[origin_enum, 0] for origin_enum in origin_enums],
            arrival_airport=[[destination_enum, 0]],
            travel_date=base_out.isoformat(),
        )
        inbound = m["FlightSegment"](
            departure_airport=[[destination_enum, 0]],
            arrival_airport=[[origin_enum, 0] for origin_enum in origin_enums],
            travel_date=first_return.isoformat(),
        )
        filters = m["DateSearchFilters"](
            trip_type=m["TripType"].ROUND_TRIP,
            passenger_info=m["PassengerInfo"](adults=int(cfg.ADULTS), children=children),
            flight_segments=[outbound, inbound],
            seat_type=_seat_type(m, cfg.CABIN_CLASS),
            stops=m["MaxStops"].ONE_STOP_OR_FEWER,
            layover_restrictions=m["LayoverRestrictions"](max_duration=max_layover),
            from_date=min_out.isoformat(),
            to_date=max_out.isoformat(),
            duration=duration,
        )
        try:
            results = searcher.search(
                filters,
                currency=cfg.CURRENCY,
                language="es-AR",
                country="AR",
            ) or []
        except Exception as exc:
            logger.warning(
                "FLEX Fli: %s→%s duración %dd falló: %s",
                origin_label, cfg.DESTINATION, duration, exc,
            )
            continue
        for item in results:
            pair = tuple(getattr(item, "date", ()) or ())
            if len(pair) != 2:
                continue
            out_date = pair[0].date() if hasattr(pair[0], "date") else date.fromisoformat(str(pair[0])[:10])
            ret_date = pair[1].date() if hasattr(pair[1], "date") else date.fromisoformat(str(pair[1])[:10])
            if not (min_out <= out_date <= max_out and min_ret <= ret_date <= max_ret):
                continue
            if ret_date <= out_date:
                continue
            try:
                price = float(getattr(item, "price"))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            currency = str(getattr(item, "currency", None) or cfg.CURRENCY).upper()
            key = (out_date.isoformat(), ret_date.isoformat())
            candidate = {
                "date_out": key[0],
                "date_ret": key[1],
                "price": price,
                "currency": currency,
                "origin": origin_label,
                "trip_days": (ret_date - out_date).days,
                "out_shift": (out_date - base_out).days,
                "ret_shift": (ret_date - base_ret).days,
                "source": "Fli · Google Flights calendar",
                "verified": False,
            }
            previous = rows.get(key)
            if previous is None or price < float(previous.get("price") or 10**18):
                rows[key] = candidate
    return sorted(
        rows.values(),
        key=lambda r: (float(r.get("price") or 10**18), abs(int(r.get("out_shift") or 0)) + abs(int(r.get("ret_shift") or 0)), r["date_out"], r["date_ret"]),
    )
