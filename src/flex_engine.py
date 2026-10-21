"""Exploración de fechas flexibles para Flight Sentinel V2.9.

La exploración primaria usa Fli/SearchDates (sin SerpApi) para construir una
matriz de precios alrededor de las fechas base. Luego, detrás del guardia de
presupuesto, se verifican sólo las mejores combinaciones con una búsqueda
SerpApi deliberadamente acotada.
"""
from __future__ import annotations

from dataclasses import replace
import logging

from src.agent import fetch_all_offers
from src.cloud_state import append_event, ensure_monitor, offer_to_state, utc_now
from src.providers_serpapi import enrich_offer_baggage
from src.serpapi_budget import check_budget
from src.watch_provider_fli import search_fli_flexible_dates

logger = logging.getLogger(__name__)


def _candidate_key(row: dict) -> tuple[str, str]:
    return str(row.get("date_out") or ""), str(row.get("date_ret") or "")


def _verify_candidate(cfg, row: dict, *, max_tokens: int = 1) -> tuple[dict | None, int, dict]:
    """Verifica una pareja de fechas con cobertura intencionalmente acotada."""
    audit_cfg = replace(
        cfg,
        DATE_OUTBOUND=str(row["date_out"]),
        DATE_RETURN=str(row["date_ret"]),
        PRICE_PROVIDER="serpapi",
        SERPAPI_MAX_OUTBOUND_TOKENS=max(1, int(max_tokens)),
        SERPAPI_BAGGAGE_ENRICH=False,
        MARKET_AUDIT_AUTO=False,
    )
    offers, diag = fetch_all_offers(audit_cfg)
    used = int(diag.get("queries_used", 0))
    if not offers:
        return None, used, diag
    best = offers[0]
    _, q = enrich_offer_baggage(
        best,
        api_key=cfg.SERPAPI_API_KEY,
        gl=getattr(cfg, "SERPAPI_GL", "ar"),
        hl=getattr(cfg, "SERPAPI_HL", "es"),
    )
    used += int(q or 0)
    return offer_to_state(best), used, diag


def run_flex_monitor(cfg, profile, state: dict) -> dict:
    node = ensure_monitor(state, profile)
    flex_days = max(0, min(3, int(getattr(profile, "flex_days", 0) or 0)))
    if flex_days <= 0:
        raise ValueError("El monitor usa fechas exactas. Elegí ±1, ±2 o ±3 días para explorar flexibilidad.")

    rows = search_fli_flexible_dates(cfg, flex_days)
    exact = next(
        (x for x in rows if x.get("date_out") == profile.date_outbound and x.get("date_ret") == profile.date_return),
        None,
    )
    sensor_best = rows[0] if rows else None
    verify_top = max(0, min(3, int(getattr(cfg, "FLEX_VERIFY_TOP", 2) or 2)))
    max_tokens = max(1, min(2, int(getattr(cfg, "FLEX_MAX_OUTBOUND_TOKENS", 1) or 1)))
    candidates = rows[:verify_top]

    # Peor caso por candidato: 1 discovery + N departure_token + 1 Booking Options.
    requested = len(candidates) * (2 + max_tokens)
    budget = None
    verified_rows: list[dict] = []
    queries_used = 0
    verification_status = "NOT_RUN"
    if candidates and requested > 0:
        budget = check_budget(
            cfg.SERPAPI_API_KEY,
            requested=requested,
            auto_cap=int(getattr(cfg, "SERPAPI_AUTO_MONTHLY_CAP", 220)),
            reserve=int(getattr(cfg, "SERPAPI_MANUAL_RESERVE", 30)),
        )
        state["quota"] = budget.to_dict()
        if budget.ok:
            verification_status = "OK"
            for candidate in candidates:
                try:
                    offer_row, used, diag = _verify_candidate(cfg, candidate, max_tokens=max_tokens)
                    queries_used += used
                    enriched = dict(candidate)
                    enriched["serpapi_coverage_status"] = diag.get("coverage_status", "UNKNOWN")
                    enriched["serpapi_outbound_explored"] = int(diag.get("outbound_explored", 0))
                    enriched["serpapi_outbound_detected"] = int(diag.get("outbound_detected", 0))
                    if offer_row:
                        enriched["verified_offer"] = offer_row
                        enriched["booking_status"] = offer_row.get("booking_status")
                        enriched["verified_price"] = offer_row.get("bookable_price") or offer_row.get("search_price")
                        enriched["verified"] = str(offer_row.get("booking_status") or "").upper() == "VERIFIED"
                    else:
                        enriched["booking_status"] = "NO_RESULTS"
                    verified_rows.append(enriched)
                except Exception as exc:
                    logger.warning(
                        "FLEX verificación %s→%s %s/%s falló: %s",
                        ",".join(cfg.ORIGINS), cfg.DESTINATION,
                        candidate.get("date_out"), candidate.get("date_ret"), exc,
                    )
                    enriched = dict(candidate)
                    enriched["booking_status"] = "ERROR"
                    enriched["verification_error"] = str(exc)
                    verified_rows.append(enriched)
        else:
            verification_status = "BUDGET_GUARD"

    verified_by_key = {_candidate_key(x): x for x in verified_rows}
    grid = []
    for row in rows:
        merged = dict(row)
        ver = verified_by_key.get(_candidate_key(row))
        if ver:
            merged.update(ver)
        grid.append(merged)

    # El "mejor verificado" prevalece sobre el sensor si existe.
    verified_ok = [x for x in verified_rows if x.get("verified_price") is not None and x.get("verified")]
    verified_ok.sort(key=lambda x: float(x.get("verified_price") or 10**18))
    best = verified_ok[0] if verified_ok else sensor_best

    base_sensor_price = float(exact.get("price")) if exact and exact.get("price") is not None else None
    best_sensor_price = float(sensor_best.get("price")) if sensor_best and sensor_best.get("price") is not None else None
    sensor_savings = None
    if base_sensor_price is not None and best_sensor_price is not None:
        sensor_savings = base_sensor_price - best_sensor_price

    current = {
        "at": utc_now(),
        "flex_days": flex_days,
        "base_date_out": profile.date_outbound,
        "base_date_ret": profile.date_return,
        "destination": profile.destination,
        "grid": grid,
        "combinations_found": len(grid),
        "best": best,
        "sensor_best": sensor_best,
        "base_sensor": exact,
        "sensor_savings": sensor_savings,
        "verified": verified_rows,
        "verification_status": verification_status,
        "queries_used": queries_used,
        "source": "Fli SearchDates + verificación selectiva SerpApi",
    }
    previous = node.get("last_flex")
    if previous:
        node["previous_flex"] = previous
    node["last_flex"] = current
    node["last_run"] = {
        "mode": "FLEX",
        "at": utc_now(),
        "status": verification_status if verification_status != "NOT_RUN" else "OK",
    }
    event = {
        "mode": "FLEX",
        "status": "OK" if rows else "NO_RESULTS",
        "at": utc_now(),
        "flex": current,
        "budget": budget.to_dict() if budget else None,
    }
    append_event(node, event)
    return event
