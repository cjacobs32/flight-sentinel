"""Estado persistente y portable del monitoreo automático cloud.

V2.9 conserva y amplía el estado para que GitHub Pages pueda ofrecer una experiencia
completa (comparación, detalle, histórico y administración de monitores) sin
base de datos externa. Las credenciales nunca se serializan.
"""
from __future__ import annotations

from datetime import datetime, UTC
import copy
import json
from pathlib import Path
from typing import Iterable, Optional

from src.providers import Offer, Segment, _leg_from_segments

STATE_VERSION = 2
MAX_HISTORY_PER_MONITOR = 360
MAX_CURRENT_OFFERS = 120


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def empty_state() -> dict:
    return {"version": STATE_VERSION, "updated_at": None, "quota": {}, "monitors": {}, "archived_monitors": {}}


def _migrate_state(data: dict) -> dict:
    """Migra estados V1 en memoria sin perder el baseline existente."""
    if not isinstance(data, dict):
        return empty_state()
    data.setdefault("quota", {})
    data.setdefault("monitors", {})
    data.setdefault("archived_monitors", {})
    data.setdefault("updated_at", None)
    data["version"] = STATE_VERSION
    for node in (data.get("monitors") or {}).values():
        if not isinstance(node, dict):
            continue
        node.setdefault("history", [])
        node.setdefault("destination", "MIA")
        node.setdefault("flex_days", 0)
        node.setdefault("route", f"EZE/AEP → {node.get('destination') or 'MIA'}")
    return data


def load_state(path: str) -> dict:
    p = Path(path)
    if not p.exists() or not p.read_text(encoding="utf-8").strip():
        return empty_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return _migrate_state(data)
    except Exception:
        return empty_state()


def save_state(path: str, state: dict) -> None:
    state["version"] = STATE_VERSION
    state["updated_at"] = utc_now()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=False), encoding="utf-8")
    tmp.replace(p)


def _segment_dict(seg: Segment) -> dict:
    return {
        "carrier": seg.carrier,
        "flight_number": seg.flight_number,
        "departure_iata": seg.departure_iata,
        "arrival_iata": seg.arrival_iata,
        "departure_time": seg.departure_time,
        "arrival_time": seg.arrival_time,
        "airline_name": getattr(seg, "airline_name", "") or "",
    }


def offer_to_state(offer: Optional[Offer]) -> Optional[dict]:
    if offer is None:
        return None
    booking = getattr(offer, "booking", {}) or {}
    lowest = booking.get("lowest") if isinstance(booking, dict) else None
    return {
        "origin": offer.origin,
        "destination": offer.destination,
        "date_out": offer.date_out,
        "date_ret": offer.date_ret,
        "search_price": float(offer.total_price),
        "currency": offer.currency,
        "cabin_class": offer.cabin_class,
        "adults": offer.adults,
        "children": offer.children,
        "itinerary_hash": offer.itinerary_hash,
        "itinerary": offer.itinerary_str,
        "airlines": list(offer.airlines),
        "stops_outbound": offer.stops_outbound,
        "stops_inbound": offer.stops_inbound,
        "max_layover_hours": round(offer.max_layover_hours(), 3),
        "total_duration": offer.total_duration_str,
        "total_duration_minutes": offer.total_duration_minutes,
        "airline_url": offer.airline_url,
        "outbound": [_segment_dict(s) for s in offer.outbound.segments],
        "inbound": [_segment_dict(s) for s in offer.inbound.segments],
        "exact_google_url": offer.exact_google_flights_url,
        "booking_status": str(booking.get("status") or "NOT_CHECKED"),
        "bookable_price": float(lowest.get("price")) if isinstance(lowest, dict) and lowest.get("price") is not None else None,
        "seller": str(lowest.get("seller") or "") if isinstance(lowest, dict) else "",
        "booking": booking,
        "baggage": getattr(offer, "baggage", {}) or {},
    }


def offers_to_state(offers: Iterable[Offer], limit: int = MAX_CURRENT_OFFERS) -> list[dict]:
    rows = []
    for offer in list(offers)[: max(0, int(limit))]:
        row = offer_to_state(offer)
        if row:
            rows.append(row)
    return rows


def _seg_from_state(raw: dict, cabin: str) -> Segment:
    return Segment(
        carrier=str(raw.get("carrier") or ""),
        flight_number=str(raw.get("flight_number") or ""),
        departure_iata=str(raw.get("departure_iata") or ""),
        arrival_iata=str(raw.get("arrival_iata") or ""),
        departure_time=str(raw.get("departure_time") or ""),
        arrival_time=str(raw.get("arrival_time") or ""),
        cabin_class=cabin,
        airline_name=str(raw.get("airline_name") or ""),
    )


def offer_from_state(raw: Optional[dict]) -> Optional[Offer]:
    if not raw:
        return None
    cabin = str(raw.get("cabin_class") or "ECONOMY")
    out = [_seg_from_state(x, cabin) for x in raw.get("outbound", []) or []]
    inn = [_seg_from_state(x, cabin) for x in raw.get("inbound", []) or []]
    if not out or not inn:
        return None
    offer = Offer(
        origin=str(raw.get("origin") or out[0].departure_iata),
        destination=str(raw.get("destination") or out[-1].arrival_iata),
        date_out=str(raw.get("date_out") or out[0].departure_time[:10]),
        date_ret=str(raw.get("date_ret") or inn[0].departure_time[:10]),
        total_price=float(raw.get("search_price") or raw.get("bookable_price") or 0),
        currency=str(raw.get("currency") or "USD"),
        cabin_class=cabin,
        outbound=_leg_from_segments(out),
        inbound=_leg_from_segments(inn),
        adults=int(raw.get("adults") or 1),
        children=int(raw.get("children") or 0),
        raw={},
        baggage=dict(raw.get("baggage") or {}),
        booking=dict(raw.get("booking") or {}),
    )
    if raw.get("exact_google_url"):
        offer.raw["exact_google_flights_url"] = raw["exact_google_url"]
    return offer


def ensure_monitor(state: dict, profile) -> dict:
    monitors = state.setdefault("monitors", {})
    archived = state.setdefault("archived_monitors", {})
    node = monitors.setdefault(profile.id, archived.pop(profile.id, {}))
    node.update({
        "id": profile.id,
        "name": profile.name,
        "enabled": profile.enabled,
        "destination": profile.destination,
        "date_outbound": profile.date_outbound,
        "date_return": profile.date_return,
        "adults": profile.adults,
        "children_ages": list(profile.children_ages),
        "cabin_class": profile.cabin_class,
        "currency": profile.currency,
        "flex_days": int(getattr(profile, "flex_days", 0) or 0),
        "route": profile.route_label,
    })
    node.setdefault("history", [])
    return node


def sync_monitors(state: dict, profiles: Iterable, *, prune: bool = True) -> dict:
    """Sincroniza definición de monitores con ``monitors.yaml``.

    Los históricos de monitores existentes se conservan. Si ``prune`` es True,
    los monitores que ya no existen en el YAML dejan de publicarse en Pages.
    """
    profiles = list(profiles)
    valid_ids = {p.id for p in profiles}
    for profile in profiles:
        ensure_monitor(state, profile)
    if prune:
        monitors = state.setdefault("monitors", {})
        archived = state.setdefault("archived_monitors", {})
        for monitor_id in list(monitors):
            if monitor_id not in valid_ids:
                node = monitors.pop(monitor_id)
                node["deleted_at"] = utc_now()
                archived[monitor_id] = node
    return state


def _compact_offer(row: dict) -> dict:
    """Versión liviana de una oferta para eventos históricos."""
    if not isinstance(row, dict):
        return {}
    keep = {
        "search_price", "bookable_price", "currency", "seller", "itinerary_hash",
        "itinerary", "airlines", "stops_outbound", "stops_inbound",
        "max_layover_hours", "booking_status", "exact_google_url",
    }
    return {k: copy.deepcopy(v) for k, v in row.items() if k in keep}


def _compact_event(event: dict) -> dict:
    """Evita que 360 eventos repitan cientos de ofertas completas."""
    out = copy.deepcopy(event)
    watch = out.get("watch")
    if isinstance(watch, dict):
        watch.pop("offers", None)
        if isinstance(watch.get("best"), dict):
            watch["best"] = _compact_offer(watch["best"])
    audit = out.get("audit")
    if isinstance(audit, dict):
        audit.pop("offers", None)
        if isinstance(audit.get("best"), dict):
            audit["best"] = _compact_offer(audit["best"])
    verified = out.get("verified")
    if isinstance(verified, list):
        out["verified"] = [_compact_offer(x) for x in verified[:3] if isinstance(x, dict)]
    flex = out.get("flex")
    if isinstance(flex, dict):
        compact_flex = {k: copy.deepcopy(v) for k, v in flex.items() if k not in {"grid", "verified"}}
        if isinstance(compact_flex.get("best"), dict):
            compact_flex["best"] = {k: copy.deepcopy(v) for k, v in compact_flex["best"].items() if k != "verified_offer"}
        out["flex"] = compact_flex
    return out


def append_event(node: dict, event: dict) -> None:
    event = dict(event)
    event.setdefault("at", utc_now())
    history = node.setdefault("history", [])
    history.append(_compact_event(event))
    if len(history) > MAX_HISTORY_PER_MONITOR:
        del history[:-MAX_HISTORY_PER_MONITOR]
