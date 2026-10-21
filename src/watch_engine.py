"""WATCH → VERIFY de Flight Sentinel V2.5.

WATCH: Fli, frecuente, sin API key ni cuota.
VERIFY: SerpApi sólo ante cambios materiales y siempre detrás del guardia de
presupuesto. La verificación fija el itinerario exacto mediante Booking Options.
"""
from __future__ import annotations

from datetime import datetime, UTC
import html
import logging
from typing import List

from src.cloud_state import append_event, ensure_monitor, offer_from_state, offer_to_state, offers_to_state, utc_now
from src.providers import Offer
from src.providers_serpapi import enrich_offer_baggage
from src.serpapi_budget import check_budget
from src.watch_provider_fli import search_fli_offers
from src.scheduled_notifier import send_scheduled_email
from src.date_utils import format_date_ui

logger = logging.getLogger(__name__)


def _valid_watch_snapshot(snapshot: dict | None) -> bool:
    """Descarta snapshots generados por adaptadores antiguos/mal normalizados.

    V2.5.1 con Fli 0.9 podía guardar IDs numéricos como aerolíneas y nombres
    largos de aeropuerto en vez de IATA. Compararlos produciría falsas alertas.
    """
    if not snapshot:
        return False
    airlines = snapshot.get("airlines") or []
    if any((not isinstance(a, str)) or not (2 <= len(a) <= 3) or a.isdigit() for a in airlines):
        return False
    best = snapshot.get("best") or {}
    for leg_name in ("outbound", "inbound"):
        for seg in best.get(leg_name) or []:
            dep = str(seg.get("departure_iata") or "")
            arr = str(seg.get("arrival_iata") or "")
            if not (len(dep) == 3 and dep.isalpha() and len(arr) == 3 and arr.isalpha()):
                return False
    return True


def watch_summary(offers: List[Offer]) -> dict:
    airlines = sorted({a for o in offers for a in o.airlines})
    direct_airlines = sorted({a for o in offers if o.total_stops == 0 for a in o.airlines})
    best = offers[0] if offers else None
    return {
        "status": "OK" if offers else "NO_RESULTS",
        "at": utc_now(),
        "offers_found": len(offers),
        "airlines": airlines,
        "direct_airlines": direct_airlines,
        "itinerary_hashes": [o.itinerary_hash for o in offers[:120]],
        "offers": offers_to_state(offers),
        "best": offer_to_state(best),
        "source": "Fli · Google Flights no oficial · WATCH gratuito",
    }


def detect_changes(previous: dict | None, current: dict, *, abs_threshold: float = 25.0,
                   pct_threshold: float = 1.0) -> list[dict]:
    if not previous:
        return [{"type": "BASELINE", "severity": "info", "message": "Primera observación WATCH."}]
    changes = []
    prev_best = previous.get("best")
    cur_best = current.get("best")
    if prev_best and not cur_best:
        changes.append({"type": "AVAILABILITY_LOST", "severity": "high", "message": "WATCH dejó de encontrar ofertas válidas."})
        return changes
    if cur_best and not prev_best:
        changes.append({"type": "AVAILABILITY_RESTORED", "severity": "high", "message": "WATCH volvió a encontrar ofertas válidas."})
    if prev_best and cur_best:
        old = float(prev_best.get("search_price") or 0)
        new = float(cur_best.get("search_price") or 0)
        diff = new - old
        pct = (abs(diff) / old * 100) if old else 0
        if abs(diff) >= float(abs_threshold) or pct >= float(pct_threshold):
            changes.append({
                "type": "PRICE_DROP" if diff < 0 else "PRICE_RISE",
                "severity": "high" if diff < 0 else "medium",
                "old": old, "new": new, "diff": diff, "pct": pct,
                "message": f"Precio WATCH cambió de {old:.2f} a {new:.2f} ({diff:+.2f}).",
            })
        if prev_best.get("itinerary_hash") != cur_best.get("itinerary_hash"):
            changes.append({"type": "BEST_ITINERARY_CHANGED", "severity": "medium", "message": "Cambió el mejor itinerario observado."})
        prev_hashes = set(previous.get("itinerary_hashes") or [])
        if prev_best.get("itinerary_hash") and prev_best.get("itinerary_hash") not in set(current.get("itinerary_hashes") or []):
            changes.append({"type": "PREVIOUS_BEST_MISSING", "severity": "high", "message": "El mejor itinerario anterior ya no aparece en WATCH."})

    prev_air = set(previous.get("airlines") or [])
    cur_air = set(current.get("airlines") or [])
    new_air = sorted(cur_air - prev_air)
    gone_air = sorted(prev_air - cur_air)
    if new_air:
        changes.append({"type": "NEW_AIRLINE", "severity": "medium", "airlines": new_air, "message": f"Nueva(s) aerolínea(s) en WATCH: {', '.join(new_air)}."})
    if gone_air:
        changes.append({"type": "AIRLINE_MISSING", "severity": "low", "airlines": gone_air, "message": f"Ya no aparecen en WATCH: {', '.join(gone_air)}."})

    # Disponibilidad útil: una aerolínea que antes no tenía vuelo directo y ahora sí.
    prev_direct = set(previous.get("direct_airlines") or [])
    cur_direct = set(current.get("direct_airlines") or [])
    new_direct = sorted(cur_direct - prev_direct)
    lost_direct = sorted(prev_direct - cur_direct)
    if new_direct:
        changes.append({"type": "NEW_DIRECT_AVAILABILITY", "severity": "high", "airlines": new_direct,
                        "message": f"Nueva disponibilidad directa en WATCH: {', '.join(new_direct)}."})
    if lost_direct:
        changes.append({"type": "DIRECT_AVAILABILITY_MISSING", "severity": "medium", "airlines": lost_direct,
                        "message": f"Dejó de aparecer disponibilidad directa en WATCH: {', '.join(lost_direct)}."})

    # Se registran altas/bajas de itinerarios para trazabilidad, pero por sí solas no
    # fuerzan gasto SerpApi: Fli puede reordenar/variar resultados entre consultas.
    prev_hashes = set(previous.get("itinerary_hashes") or [])
    cur_hashes = set(current.get("itinerary_hashes") or [])
    added = cur_hashes - prev_hashes
    removed = prev_hashes - cur_hashes
    if added:
        changes.append({"type": "NEW_ITINERARIES", "severity": "info", "count": len(added),
                        "message": f"WATCH detectó {len(added)} itinerario(s) que no estaban en la observación anterior."})
    if removed:
        changes.append({"type": "ITINERARIES_MISSING", "severity": "info", "count": len(removed),
                        "message": f"WATCH dejó de ver {len(removed)} itinerario(s) de la observación anterior."})
    return changes


def _candidate_targets(offers: List[Offer], previous: dict | None, changes: list[dict], max_items: int) -> list[tuple[str, Offer]]:
    targets: list[tuple[str, Offer]] = []
    seen = set()
    types = {c.get("type") for c in changes}
    if offers and (not previous or types & {"PRICE_DROP", "PRICE_RISE", "BEST_ITINERARY_CHANGED", "AVAILABILITY_RESTORED", "BASELINE"}):
        targets.append(("current_best", offers[0])); seen.add(offers[0].itinerary_hash)
    new_airlines = {a for c in changes if c.get("type") == "NEW_AIRLINE" for a in c.get("airlines", [])}
    for airline in sorted(new_airlines):
        for offer in offers:
            if airline in offer.airlines and offer.itinerary_hash not in seen:
                targets.append((f"new_airline:{airline}", offer)); seen.add(offer.itinerary_hash); break
            if len(targets) >= max_items:
                break
        if len(targets) >= max_items:
            break
    # Una nueva opción directa se considera disponibilidad relevante incluso si
    # no es el precio mínimo del WATCH.
    new_direct = {a for c in changes if c.get("type") == "NEW_DIRECT_AVAILABILITY" for a in c.get("airlines", [])}
    for airline in sorted(new_direct):
        if len(targets) >= max_items:
            break
        for offer in offers:
            if offer.total_stops == 0 and airline in offer.airlines and offer.itinerary_hash not in seen:
                targets.append((f"new_direct:{airline}", offer)); seen.add(offer.itinerary_hash); break
    if "PREVIOUS_BEST_MISSING" in types or "AVAILABILITY_LOST" in types:
        prev_offer = offer_from_state((previous or {}).get("best"))
        if prev_offer and prev_offer.itinerary_hash not in seen:
            targets.append(("previous_best", prev_offer)); seen.add(prev_offer.itinerary_hash)
    return targets[:max_items]


def _reconcile_missing_previous(changes: list[dict], verified_rows: list[dict]) -> tuple[list[dict], bool]:
    """Evita alertar falsa pérdida si Fli omite un vuelo que SerpApi aún confirma."""
    prev = next((x for x in verified_rows if x.get("role") == "previous_best"), None)
    if not prev:
        return changes, False
    booking_status = str(prev.get("booking_status") or "").upper()
    missing_types = {"PREVIOUS_BEST_MISSING", "AVAILABILITY_LOST"}
    had_missing = any(c.get("type") in missing_types for c in changes)
    if not had_missing:
        return changes, False
    if booking_status == "VERIFIED":
        kept = [c for c in changes if c.get("type") not in missing_types]
        kept.append({
            "type": "WATCH_SOURCE_DISCREPANCY", "severity": "info", "confirmed": True,
            "message": "Fli dejó de mostrar el mejor itinerario anterior, pero SerpApi confirmó que sigue reservable en Google Flights. No se considera pérdida de disponibilidad.",
        })
        return kept, False
    if booking_status == "UNAVAILABLE":
        out = []
        for c in changes:
            if c.get("type") in missing_types:
                c = dict(c); c["confirmed"] = True
                c["message"] = "El itinerario anterior dejó de aparecer en WATCH y Google Flights no devolvió opciones de reserva para ese itinerario exacto."
            out.append(c)
        return out, True
    return changes, False

def _event_email(profile, event: dict) -> tuple[str, str]:
    verified = event.get("verified") or []
    first = verified[0] if verified else None
    price_line = ""
    link_line = ""
    if first:
        p = first.get("bookable_price") or first.get("search_price")
        seller = first.get("seller") or "Google Flights"
        if p is not None:
            price_line = f"<p><b>Precio confirmado:</b> {html.escape(str(first.get('currency') or 'USD'))} {float(p):,.2f} · {html.escape(seller)}</p>"
        if first.get("exact_google_url"):
            link_line = f'<p><a href="{html.escape(first["exact_google_url"])}">Abrir itinerario exacto en Google Flights</a></p>'
    reasons = "".join(f"<li>{html.escape(str(x.get('message') or x.get('type')))}</li>" for x in event.get("changes", []))
    subject = f"Cambio confirmado · {profile.name}"
    body = f"""
    <html><body style="font-family:Arial,sans-serif">
    <h2>✈️ Flight Sentinel — {html.escape(profile.name)}</h2>
    <p>{format_date_ui(profile.date_outbound)} → {format_date_ui(profile.date_return)} · EZE/AEP → {html.escape(profile.destination) if hasattr(profile, 'destination') else 'MIA'}</p>
    <ul>{reasons}</ul>{price_line}{link_line}
    <p style="font-size:12px;color:#666">WATCH detecta cambios gratis con Fli; este aviso sólo se envía después de una verificación SerpApi cuando fue posible. No implica cobertura exhaustiva de todo el mercado.</p>
    </body></html>"""
    return subject, body


def run_watch_monitor(cfg, profile, state: dict) -> dict:
    node = ensure_monitor(state, profile)
    previous = node.get("last_watch")
    if previous and not _valid_watch_snapshot(previous):
        logger.warning("WATCH: snapshot previo incompatible/mal normalizado; se reinicia baseline sin generar alertas falsas.")
        previous = None
    try:
        offers = search_fli_offers(cfg)
        current = watch_summary(offers)
        changes = detect_changes(
            previous, current,
            abs_threshold=float(getattr(cfg, "WATCH_PRICE_ABS_CHANGE", 25.0)),
            pct_threshold=float(getattr(cfg, "WATCH_PRICE_PERCENT_CHANGE", 1.0)),
        )
        event = {"mode": "WATCH", "status": "OK", "changes": changes, "watch": current, "verified": [], "notify": False}

        targets = _candidate_targets(offers, previous, changes, int(getattr(cfg, "WATCH_VERIFY_MAX_OFFERS", 2)))
        material = any(c.get("type") != "BASELINE" for c in changes)
        # La primera observación se verifica para establecer una referencia confiable.
        should_verify = bool(targets) and (material or not previous)
        if should_verify:
            budget = check_budget(
                cfg.SERPAPI_API_KEY, requested=len(targets),
                auto_cap=int(getattr(cfg, "SERPAPI_AUTO_MONTHLY_CAP", 220)),
                reserve=int(getattr(cfg, "SERPAPI_MANUAL_RESERVE", 30)),
            )
            state["quota"] = budget.to_dict()
            event["budget"] = budget.to_dict()
            if budget.ok:
                verified_rows = []
                for role, offer in targets:
                    enrich_offer_baggage(offer, api_key=cfg.SERPAPI_API_KEY,
                                         gl=getattr(cfg, "SERPAPI_GL", "ar"), hl=getattr(cfg, "SERPAPI_HL", "es"))
                    row = offer_to_state(offer) or {}
                    row["role"] = role
                    verified_rows.append(row)
                changes, confirmed_loss = _reconcile_missing_previous(changes, verified_rows)
                event["changes"] = changes
                event["verified"] = verified_rows
                # Enriquecemos la colección visible del WATCH con la verificación
                # exacta, sin perder las demás ofertas gratuitas de Fli.
                verified_by_hash = {
                    x.get("itinerary_hash"): x for x in verified_rows
                    if x.get("itinerary_hash") and x.get("role") != "previous_best"
                }
                for idx, row in enumerate(current.get("offers") or []):
                    ver = verified_by_hash.get(row.get("itinerary_hash"))
                    if ver:
                        merged = dict(row); merged.update(ver); current["offers"][idx] = merged
                if current.get("best") and current["best"].get("itinerary_hash") in verified_by_hash:
                    merged = dict(current["best"]); merged.update(verified_by_hash[current["best"]["itinerary_hash"]]); current["best"] = merged
                confirmed = [x for x in verified_rows if x and x.get("booking_status") == "VERIFIED"]
                if confirmed:
                    node["last_verified"] = {"at": utc_now(), "offers": confirmed, "changes": changes}
                # Notifica cambios materiales sólo si hay confirmación reservable;
                # la pérdida se notifica cuando Booking Options confirma que no hay reserva.
                material_after = any(c.get("type") not in {"BASELINE", "WATCH_SOURCE_DISCREPANCY", "NEW_ITINERARIES", "ITINERARIES_MISSING"} for c in changes)
                event["notify"] = bool((confirmed and material_after) or confirmed_loss)
            else:
                event["verification_status"] = "BUDGET_GUARD"
        if previous:
            node["previous_watch"] = previous
        node["last_watch"] = current
        node["last_run"] = {"mode": "WATCH", "at": utc_now(), "status": event.get("verification_status") or "OK"}
        if event.get("notify"):
            subject, body = _event_email(profile, event)
            try:
                event["email_sent"] = send_scheduled_email(cfg, subject=subject, html=body)
            except Exception as exc:
                event["email_sent"] = False; event["email_error"] = str(exc)
                logger.warning("No se pudo enviar aviso WATCH: %s", exc)
        append_event(node, event)
        return event
    except Exception as exc:
        event = {"mode": "WATCH", "status": "ERROR", "at": utc_now(), "error": str(exc), "changes": [], "verified": []}
        node["last_run"] = {"mode": "WATCH", "at": utc_now(), "status": "ERROR", "error": str(exc)}
        try:
            body = (f"<h2>Flight Sentinel — WATCH ERROR · {html.escape(profile.name)}</h2>"
                    f"<p>{format_date_ui(profile.date_outbound)} → {format_date_ui(profile.date_return)} · EZE/AEP → {html.escape(profile.destination) if hasattr(profile, 'destination') else 'MIA'}</p>"
                    f"<p><b>Error:</b> {html.escape(str(exc))}</p>"
                    f"<p>La falla no consumió SerpApi si ocurrió antes de VERIFY. Revisá el próximo pipeline.</p>")
            event["email_sent"] = send_scheduled_email(cfg, subject=f"WATCH técnico · {profile.name}", html=body)
        except Exception as mail_exc:
            event["email_sent"] = False; event["email_error"] = str(mail_exc)
        append_event(node, event)
        logger.exception("WATCH falló para %s", profile.id)
        return event
