"""AUDIT profundo automático con SerpApi, limitado por presupuesto."""
from __future__ import annotations

from dataclasses import replace
import logging

from src.agent import fetch_all_offers
from src.cloud_state import append_event, ensure_monitor, offer_to_state, offers_to_state, utc_now
from src.providers_serpapi import enrich_offer_baggage
from src.serpapi_budget import check_budget
from src.scheduled_notifier import send_scheduled_email
from src.date_utils import format_date_ui

logger = logging.getLogger(__name__)


def _verification_targets(offers, max_items: int):
    """Selecciona pocas ofertas representativas para recuperar la riqueza de V2.4 sin disparar cuota.

    Prioriza el mejor precio y luego itinerarios que incorporen aerolíneas todavía no
    representadas. Es el mismo criterio visual que usaba el dashboard local.
    """
    if not offers or max_items <= 0:
        return []
    selected = [offers[0]]
    covered = set(offers[0].airlines or [])
    seen = {offers[0].itinerary_hash}
    for offer in offers[1:]:
        if len(selected) >= max_items:
            break
        if offer.itinerary_hash in seen:
            continue
        if set(offer.airlines or []) - covered:
            selected.append(offer)
            covered.update(offer.airlines or [])
            seen.add(offer.itinerary_hash)
    return selected


def run_audit_monitor(cfg, profile, state: dict) -> dict:
    node = ensure_monitor(state, profile)
    verify_max = max(1, int(getattr(cfg, "AUDIT_VERIFY_MAX_OFFERS", 4)))
    # Peor caso: 1 discovery + N departure_tokens + hasta K verificaciones exactas.
    requested = int(getattr(cfg, "SERPAPI_MAX_OUTBOUND_TOKENS", 15)) + 1 + verify_max
    budget = check_budget(
        cfg.SERPAPI_API_KEY, requested=requested,
        auto_cap=int(getattr(cfg, "SERPAPI_AUTO_MONTHLY_CAP", 220)),
        reserve=int(getattr(cfg, "SERPAPI_MANUAL_RESERVE", 30)),
    )
    state["quota"] = budget.to_dict()
    if not budget.ok:
        event = {"mode": "AUDIT", "status": "BUDGET_GUARD", "at": utc_now(), "budget": budget.to_dict()}
        node["last_run"] = {"mode": "AUDIT", "at": utc_now(), "status": "BUDGET_GUARD"}
        append_event(node, event)
        return event

    audit_cfg = replace(cfg, PRICE_PROVIDER="serpapi", SERPAPI_BAGGAGE_ENRICH=False, MARKET_AUDIT_AUTO=False)
    try:
        offers, diag = fetch_all_offers(audit_cfg)
        verified_rows = []
        verify_targets = _verification_targets(offers, verify_max)
        for offer in verify_targets:
            # La consulta completa conserva muchas ofertas y verifica unas pocas
            # representativas (mejor + cobertura de aerolíneas), como hacía V2.4.
            enrich_offer_baggage(
                offer, api_key=cfg.SERPAPI_API_KEY,
                gl=getattr(cfg, "SERPAPI_GL", "ar"), hl=getattr(cfg, "SERPAPI_HL", "es"),
            )
            row = offer_to_state(offer)
            if row:
                verified_rows.append(row)
        current = {
            "at": utc_now(), "offers_found": len(offers),
            "airlines": sorted({a for o in offers for a in o.airlines}),
            "direct_airlines": sorted({a for o in offers if o.total_stops == 0 for a in o.airlines}),
            "offers": offers_to_state(offers),
            "best": offer_to_state(offers[0]) if offers else None,
            "coverage_status": diag.get("coverage_status", "UNKNOWN"),
            "outbound_detected": int(diag.get("outbound_detected", 0)),
            "outbound_explored": int(diag.get("outbound_explored", 0)),
            "queries_used": int(diag.get("queries_used", 0)) + len(verify_targets),
            "source": "Google Flights · SerpApi · AUDIT profundo",
        }
        previous_audit = node.get("last_audit") or {}
        changes = []
        if previous_audit.get("best") and current.get("best"):
            old = float(previous_audit["best"].get("search_price") or 0)
            new = float(current["best"].get("search_price") or 0)
            if old != new:
                changes.append({"type": "AUDIT_PRICE_CHANGE", "old": old, "new": new,
                                "message": f"AUDIT cambió de {old:.2f} a {new:.2f}."})
        old_air = set(previous_audit.get("airlines") or [])
        new_air = set(current.get("airlines") or [])
        if old_air and old_air != new_air:
            changes.append({"type": "AUDIT_AIRLINES_CHANGE", "message": "Cambió el conjunto de aerolíneas del AUDIT.",
                            "added": sorted(new_air-old_air), "removed": sorted(old_air-new_air)})
        event = {"mode": "AUDIT", "status": "OK", "at": utc_now(), "audit": current,
                 "verified": verified_rows, "changes": changes, "budget": budget.to_dict()}
        if previous_audit:
            node["previous_audit"] = previous_audit
        node["last_audit"] = current
        confirmed = [x for x in verified_rows if x.get("booking_status") == "VERIFIED"]
        if confirmed:
            node["last_verified"] = {"at": utc_now(), "offers": confirmed, "changes": changes}
        node["last_run"] = {"mode": "AUDIT", "at": utc_now(), "status": "OK"}
        best_verified = confirmed[0] if confirmed else None
        if changes and best_verified:
            p = best_verified.get("bookable_price") or best_verified.get("search_price")
            seller = best_verified.get("seller") or "Google Flights"
            body = (f"<h2>Flight Sentinel — AUDIT {profile.name}</h2>"
                    f"<p>{format_date_ui(profile.date_outbound)} → {format_date_ui(profile.date_return)} · EZE/AEP → {profile.destination}</p>"
                    f"<p>{'<br>'.join(str(x.get('message')) for x in changes)}</p>"
                    f"<p><b>Precio reservable verificado:</b> {best_verified.get('currency','USD')} {float(p):,.2f} · {seller}</p>")
            try:
                event["email_sent"] = send_scheduled_email(cfg, subject=f"AUDIT detectó cambios · {profile.name}", html=body)
            except Exception as exc:
                event["email_sent"] = False; event["email_error"] = str(exc)
        append_event(node, event)
        return event
    except Exception as exc:
        event = {"mode": "AUDIT", "status": "ERROR", "at": utc_now(), "error": str(exc), "budget": budget.to_dict()}
        node["last_run"] = {"mode": "AUDIT", "at": utc_now(), "status": "ERROR", "error": str(exc)}
        try:
            body = (f"<h2>Flight Sentinel — AUDIT ERROR · {profile.name}</h2>"
                    f"<p>{format_date_ui(profile.date_outbound)} → {format_date_ui(profile.date_return)} · EZE/AEP → {profile.destination}</p>"
                    f"<p><b>Error:</b> {str(exc)}</p>")
            event["email_sent"] = send_scheduled_email(cfg, subject=f"AUDIT técnico · {profile.name}", html=body)
        except Exception as mail_exc:
            event["email_sent"] = False; event["email_error"] = str(mail_exc)
        append_event(node, event)
        logger.exception("AUDIT falló para %s", profile.id)
        return event
