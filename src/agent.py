"""
Orquestador principal del agente de monitoreo de vuelos.
"""
import logging
from datetime import datetime, UTC
from typing import List, Optional

from src.date_utils import format_date_ui
from src.database import (
    initialize_db, save_snapshot, save_offers, save_alert,
    get_first_price, get_min_price, get_last_alert, get_price_stats, export_csv,
    update_snapshot_market_audit, get_cached_verified_enrichment,
)
from src.providers import search_offers, rank_offers, Offer
from src.providers_serpapi import enrich_offer_baggage
from src.alert_logic import should_alert
from src.notifier import send_alert_email
from src.market_audit_despegar import audit_despegar, reconcile_market

logger = logging.getLogger(__name__)


def _provider_mode(cfg) -> str:
    """Clasifica la corrida para que el dashboard no confunda simulación con datos reales."""
    provider = (getattr(cfg, "PRICE_PROVIDER", "") or "").lower()
    if provider == "mock":
        return "simulation"
    if provider == "duffel":
        token = getattr(cfg, "DUFFEL_API_TOKEN", "") or ""
        return "real" if token.startswith("duffel_live_") else "sandbox"
    if provider == "amadeus":
        return "real" if (getattr(cfg, "AMADEUS_ENV", "test") or "test").lower() == "production" else "sandbox"
    if provider in {"serpapi", "kiwi"}:
        return "real"
    return "unknown"


def fetch_all_offers(cfg) -> tuple[List[Offer], dict]:
    """Busca en todos los orígenes, consolida/rankea y devuelve diagnóstico de cobertura."""
    all_offers: List[Offer] = []
    diagnostics = {
        "outbound_detected": 0,
        "outbound_explored": 0,
        "queries_used": 0,
        "coverage_status": "UNKNOWN",
    }
    provider_kwargs = {}
    if cfg.PRICE_PROVIDER == "amadeus":
        provider_kwargs = {
            "api_key": cfg.AMADEUS_API_KEY,
            "api_secret": cfg.AMADEUS_API_SECRET,
            "env": cfg.AMADEUS_ENV,
        }
    elif cfg.PRICE_PROVIDER == "duffel":
        provider_kwargs = {
            "api_token": cfg.DUFFEL_API_TOKEN,
        }
    elif cfg.PRICE_PROVIDER == "serpapi":
        provider_kwargs = {
            "api_key": cfg.SERPAPI_API_KEY,
            "max_outbound_tokens": cfg.SERPAPI_MAX_OUTBOUND_TOKENS,
            "show_hidden": getattr(cfg, "SERPAPI_SHOW_HIDDEN", True),
            "gl": getattr(cfg, "SERPAPI_GL", "ar"),
            "hl": getattr(cfg, "SERPAPI_HL", "es"),
        }
    elif cfg.PRICE_PROVIDER == "kiwi":
        provider_kwargs = {
            "api_key": cfg.KIWI_API_KEY,
            "max_results": cfg.KIWI_MAX_RESULTS,
        }

    # SerpApi soporta múltiples aeropuertos de origen en una sola búsqueda.
    # V2 lo usa para EZE+AEP y así evitar duplicar la llamada inicial.
    if cfg.PRICE_PROVIDER == "serpapi" and cfg.SERPAPI_COMBINE_ORIGINS and len(cfg.ORIGINS) > 1:
        search_origins = [",".join(cfg.ORIGINS)]
    else:
        search_origins = list(cfg.ORIGINS)

    coverage_parts = []
    for origin in search_origins:
        try:
            call_diag = {}
            call_kwargs = dict(provider_kwargs)
            if cfg.PRICE_PROVIDER == "serpapi":
                call_kwargs["diagnostics"] = call_diag
            offers = search_offers(
                origin=origin, destination=cfg.DESTINATION,
                date_out=cfg.DATE_OUTBOUND, date_ret=cfg.DATE_RETURN,
                adults=cfg.ADULTS, children_ages=cfg.CHILDREN_AGES,
                cabin_class=cfg.CABIN_CLASS, currency=cfg.CURRENCY,
                max_stops=cfg.MAX_STOPS, max_layover_hours=cfg.MAX_LAYOVER_HOURS,
                provider=cfg.PRICE_PROVIDER, **call_kwargs,
            )
            if call_diag:
                diagnostics["outbound_detected"] += int(call_diag.get("outbound_detected", 0))
                diagnostics["outbound_explored"] += int(call_diag.get("outbound_explored", 0))
                diagnostics["queries_used"] += int(call_diag.get("queries_used", 0))
                coverage_parts.append(call_diag.get("coverage_status", "UNKNOWN"))
            logger.info("  %s → %s: %d oferta(s)", origin, cfg.DESTINATION, len(offers))
            all_offers.extend(offers)
        except Exception as e:
            logger.error("Error buscando desde %s: %s", origin, e,
                         extra={"origin": origin, "error": str(e)})

    if coverage_parts:
        diagnostics["coverage_status"] = (
            "COMPLETE" if all(x == "COMPLETE" for x in coverage_parts) else "PARTIAL"
        )
    return rank_offers(all_offers), diagnostics



def _baggage_targets(ranked: List[Offer], max_items: int) -> List[Offer]:
    """Elige pocas ofertas que cubran la mayor cantidad de aerolíneas distintas."""
    if not ranked or max_items <= 0:
        return []
    selected = [ranked[0]]
    covered = set(ranked[0].airlines)
    seen_hashes = {ranked[0].itinerary_hash}
    for offer in ranked[1:]:
        if len(selected) >= max_items:
            break
        new_codes = set(offer.airlines) - covered
        if not new_codes or offer.itinerary_hash in seen_hashes:
            continue
        selected.append(offer)
        covered.update(offer.airlines)
        seen_hashes.add(offer.itinerary_hash)
    return selected


def enrich_featured_baggage(cfg, ranked: List[Offer], diagnostics: dict) -> None:
    """Verifica equipaje sólo en ofertas representativas, cuidando el free tier."""
    if not ranked or (getattr(cfg, "PRICE_PROVIDER", "") or "").lower() != "serpapi":
        return
    if not getattr(cfg, "SERPAPI_BAGGAGE_ENRICH", True):
        return
    targets = _baggage_targets(ranked, int(getattr(cfg, "SERPAPI_BAGGAGE_MAX_OFFERS", 6) or 0))
    used = 0
    verified = 0
    cache_hits = 0
    booking_verified = 0
    for offer in targets:
        cached = get_cached_verified_enrichment(getattr(cfg, "DB_PATH", "flight_monitor.db"), offer.content_hash())
        if cached:
            offer.baggage = cached.get("baggage") or {}
            offer.booking = cached.get("booking") or {}
            exact_url = cached.get("exact_url")
            if exact_url:
                if not isinstance(offer.raw, dict):
                    offer.raw = {}
                offer.raw["exact_google_flights_url"] = exact_url
            summary, q = offer.baggage, 0
            cache_hits += 1
        else:
            summary, q = enrich_offer_baggage(
                offer,
                api_key=cfg.SERPAPI_API_KEY,
                gl=getattr(cfg, "SERPAPI_GL", "ar"),
                hl=getattr(cfg, "SERPAPI_HL", "es"),
            )
        used += int(q or 0)
        if (summary or {}).get("status") == "VERIFIED":
            verified += 1
        if str((getattr(offer, "booking", {}) or {}).get("status") or "").upper() == "VERIFIED":
            booking_verified += 1
    diagnostics["queries_used"] = int(diagnostics.get("queries_used", 0)) + used
    diagnostics["baggage_queries_used"] = used
    diagnostics["baggage_cache_hits"] = cache_hits
    diagnostics["baggage_offers_checked"] = len(targets)
    diagnostics["baggage_offers_verified"] = verified
    diagnostics["booking_offers_verified"] = booking_verified
    if targets:
        logger.info(
            "Google enriquecido: equipaje %d/%d · precios reservables %d/%d | %d consulta(s) SerpApi adicional(es) | %d caché",
            verified, len(targets), booking_verified, len(targets), used, cache_hits,
        )


def run_market_audit(cfg, google_offers) -> dict:
    """Ejecuta control independiente gratuito contra Despegar."""
    # Sólo tiene sentido conciliar mercado cuando la fuente primaria es Google real.
    if (getattr(cfg, "PRICE_PROVIDER", "") or "").lower() != "serpapi":
        return {
            "reconciliation": {"status": "NOT_RUN", "google_airlines": [], "despegar_airlines": [],
                               "missing_in_google": [], "missing_direct_in_google": [], "only_google": [],
                               "note": "Auditoría de mercado sólo aplica a Google Flights real."},
            "despegar": {"status": "NOT_RUN"},
        }
    if not getattr(cfg, "MARKET_AUDIT_ENABLED", True):
        return {
            "reconciliation": {
                "status": "NOT_RUN", "google_airlines": sorted({a for o in google_offers for a in getattr(o, "airlines", [])}),
                "despegar_airlines": [], "missing_in_google": [], "missing_direct_in_google": [],
                "only_google": [], "note": "Auditoría independiente deshabilitada."
            },
            "despegar": {"status": "NOT_RUN"},
        }
    logger.info("Auditor de mercado gratuito: consultando Despegar con Playwright…")
    try:
        d = audit_despegar(
            monitor_id=getattr(cfg, "MONITOR_ID", "legacy"),
            date_out=cfg.DATE_OUTBOUND, date_ret=cfg.DATE_RETURN,
            adults=cfg.ADULTS, children=len(cfg.CHILDREN_AGES), currency=cfg.CURRENCY,
            destination=cfg.DESTINATION,
            headless=getattr(cfg, "DESPEGAR_HEADLESS", True),
            timeout_seconds=getattr(cfg, "DESPEGAR_TIMEOUT_SECONDS", 50),
            max_scrolls=getattr(cfg, "DESPEGAR_MAX_SCROLLS", 14),
        )
    except Exception as exc:
        # El auditor secundario jamás debe invalidar ni perder una corrida Google real.
        logger.exception("Falla inesperada del auditor Despegar; se conserva la corrida Google: %s", exc)
        d = {
            "status": "AUDIT_FAILED", "url": "", "cards_seen": 0,
            "direct_cards": 0, "one_stop_cards": 0, "two_plus_cards": 0,
            "airlines": [], "direct_airlines": [], "one_stop_airlines": [],
            "eligible_airlines": [], "unverified_one_stop_airlines": [],
            "cheapest_per_person": {}, "cards": [],
            "warning": f"Auditor Despegar falló de forma aislada: {exc}",
        }
    rec = reconcile_market(google_offers, d)
    logger.info(
        "Auditor mercado: %s | Google=%s | Despegar=%s | faltan en Google=%s",
        rec.get("status"), ",".join(rec.get("google_airlines", [])) or "—",
        ",".join(rec.get("despegar_airlines", [])) or "—",
        ",".join(rec.get("missing_in_google", [])) or "—",
    )
    return {"reconciliation": rec, "despegar": d}


def audit_existing_snapshot(cfg, snapshot_id: int, google_offer_rows: list[dict]) -> dict:
    """Re-audita Despegar sin gastar una nueva consulta SerpApi."""
    audit = run_market_audit(cfg, google_offer_rows)
    status = (audit.get("reconciliation") or {}).get("status", "NOT_RUN")
    update_snapshot_market_audit(
        cfg.DB_PATH, snapshot_id, market_coverage_status=status, market_audit=audit
    )
    return audit


def run_check(cfg) -> None:
    """Ejecuta una corrida completa del agente."""
    logger.info("=" * 60)
    logger.info(
        "Corrida [%s] — %s UTC | %s→%s | %s/%s | stops≤%s | layover≤%sh",
        getattr(cfg, "MONITOR_ID", "legacy"),
        datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        cfg.ORIGINS, cfg.DESTINATION,
        format_date_ui(cfg.DATE_OUTBOUND), format_date_ui(cfg.DATE_RETURN),
        cfg.MAX_STOPS if cfg.MAX_STOPS is not None else "∞",
        cfg.MAX_LAYOVER_HOURS if cfg.MAX_LAYOVER_HOURS is not None else "∞",
    )

    initialize_db(cfg.DB_PATH)
    provider = (getattr(cfg, "PRICE_PROVIDER", "unknown") or "unknown").lower()
    provider_mode = _provider_mode(cfg)

    # itinerary_scope se define más abajo, después de conocer best.currency.
    # (No se puede definir aquí porque la moneda real la devuelve el provider.)

    # --- Búsqueda ---
    ranked: List[Offer] = []
    diagnostics = {"outbound_detected": 0, "outbound_explored": 0, "queries_used": 0, "coverage_status": "UNKNOWN"}
    error_msg: Optional[str] = None
    try:
        ranked, diagnostics = fetch_all_offers(cfg)
    except Exception as e:
        error_msg = str(e)
        logger.error("Error general en búsqueda: %s", e, extra={"error": str(e)})

    # Equipaje es un enriquecimiento secundario: nunca debe invalidar una
    # búsqueda de vuelos que ya salió bien.
    if ranked:
        try:
            enrich_featured_baggage(cfg, ranked, diagnostics)
        except Exception as exc:
            logger.exception("Falla aislada consultando equipaje; se conservan las ofertas: %s", exc)

    # Auditoría independiente de mercado. Desde V2.3.2 NO se ejecuta
    # automáticamente por defecto: Despegar puede bloquear Playwright y afectar
    # la navegación normal del usuario. La auditoría manual sigue disponible y
    # no consume SerpApi. Puede reactivarse explícitamente con MARKET_AUDIT_AUTO=true.
    if getattr(cfg, "MARKET_AUDIT_AUTO", False) and getattr(cfg, "MARKET_AUDIT_ENABLED", True):
        market_audit = run_market_audit(cfg, ranked)
    else:
        google_airlines = sorted({a for o in ranked for a in (getattr(o, "airlines", []) or [])})
        market_audit = {
            "reconciliation": {
                "status": "NOT_RUN", "google_airlines": google_airlines,
                "despegar_airlines": [], "missing_in_google": [],
                "missing_direct_in_google": [], "only_google": google_airlines,
                "note": "Auditoría Despegar automática desactivada; usar el botón de auditoría manual si se desea."
            },
            "despegar": {"status": "NOT_RUN"},
        }
    market_status = (market_audit.get("reconciliation") or {}).get("status", "NOT_RUN")

    # Sin resultados
    if not ranked:
        save_snapshot(
            db_path=cfg.DB_PATH, origin=None, destination=cfg.DESTINATION,
            date_out=cfg.DATE_OUTBOUND, date_ret=cfg.DATE_RETURN,
            currency=cfg.CURRENCY, best_price=None,
            adults=cfg.ADULTS, children=len(cfg.CHILDREN_AGES),
            offers_found=0, error=error_msg or "Sin ofertas",
            monitor_id=getattr(cfg, "MONITOR_ID", "legacy"),
            monitor_name=getattr(cfg, "MONITOR_NAME", None),
            provider=provider, provider_mode=provider_mode,
            outbound_detected=diagnostics.get("outbound_detected", 0),
            outbound_explored=diagnostics.get("outbound_explored", 0),
            queries_used=diagnostics.get("queries_used", 0),
            coverage_status=diagnostics.get("coverage_status", "UNKNOWN"),
            market_coverage_status=market_status, market_audit=market_audit,
        )
        logger.warning("No se encontraron ofertas Google que cumplan la política de ruta. Mercado=%s", market_status)
        return {"status": "NO_RESULTS", "offers": 0, "error": error_msg, "market_coverage_status": market_status}

    best = ranked[0]
    if provider == "serpapi":
        logger.info(
            "Cobertura SerpApi: %s | %d/%d salidas | %d consultas lógicas",
            diagnostics.get("coverage_status", "UNKNOWN"),
            diagnostics.get("outbound_explored", 0),
            diagnostics.get("outbound_detected", 0),
            diagnostics.get("queries_used", 0),
        )

    # ── Moneda real de la oferta ──────────────────────────────────────────────
    # Fuente de verdad: best.currency (lo que devuelve el provider).
    # cfg.CURRENCY es lo que el usuario configuró; Duffel puede ignorarlo en
    # sandbox y devolver EUR aunque se pida USD.
    # Todo el pipeline post-búsqueda usa offer_currency para ser consistente.
    offer_currency = best.currency
    if offer_currency != cfg.CURRENCY:
        logger.warning(
            "⚠️  Moneda de oferta (%s) difiere de la configurada (%s). "
            "El pipeline usará %s para baseline, alertas y CSV.",
            offer_currency, cfg.CURRENCY, offer_currency,
        )

    logger.info(
        "Mejor oferta: %s→%s | %s %.2f | dur: %s | stops: %d+%d | %s",
        best.origin, best.destination, offer_currency, best.total_price,
        best.total_duration_str, best.stops_outbound, best.stops_inbound,
        ", ".join(best.airlines),
    )
    logger.info("  Itinerario: %s", best.itinerary_str)
    logger.info("  Itinerary hash: %s | Content hash: %s",
                best.itinerary_hash, best.content_hash())

    # itinerary_scope usa la moneda real — garantiza que baseline y alertas
    # comparen precios en la misma moneda que la oferta actual.
    itinerary_scope = dict(
        destination=cfg.DESTINATION, date_out=cfg.DATE_OUTBOUND,
        date_ret=cfg.DATE_RETURN, currency=offer_currency,
        monitor_id=getattr(cfg, "MONITOR_ID", None),
    )

    # --- Baseline ANTES de insertar ---
    if cfg.BASELINE_MODE == "first":
        baseline = get_first_price(cfg.DB_PATH, **itinerary_scope)
    else:
        baseline = get_min_price(cfg.DB_PATH, **itinerary_scope)

    # --- Guardar snapshot + ofertas ---
    run_id = save_snapshot(
        db_path=cfg.DB_PATH, origin=best.origin, destination=cfg.DESTINATION,
        date_out=cfg.DATE_OUTBOUND, date_ret=cfg.DATE_RETURN,
        currency=offer_currency, best_price=best.total_price,
        adults=cfg.ADULTS, children=len(cfg.CHILDREN_AGES),
        offers_found=len(ranked), error=error_msg,
        monitor_id=getattr(cfg, "MONITOR_ID", "legacy"),
        monitor_name=getattr(cfg, "MONITOR_NAME", None),
        provider=provider, provider_mode=provider_mode,
        outbound_detected=diagnostics.get("outbound_detected", 0),
        outbound_explored=diagnostics.get("outbound_explored", 0),
        queries_used=diagnostics.get("queries_used", 0),
        coverage_status=diagnostics.get("coverage_status", "UNKNOWN"),
        market_coverage_status=market_status, market_audit=market_audit,
    )
    inserted = save_offers(cfg.DB_PATH, run_id, ranked)
    logger.info("Snapshot #%d | %d oferta(s) guardadas (%d nuevas)", run_id, len(ranked), inserted)

    # Exportar CSV incremental
    monitor_id = getattr(cfg, "MONITOR_ID", "legacy")
    csv_path = cfg.DB_PATH.replace(".db", f"_{monitor_id}_history.csv")
    export_csv(cfg.DB_PATH, csv_path, **itinerary_scope)

    if baseline is None:
        logger.info(
            "Primera corrida con precio. Baseline: %s %.2f. Sin alerta.",
            offer_currency, best.total_price,
        )
        return {"status": "OK_BASELINE", "offers": len(ranked), "best_price": best.total_price, "currency": offer_currency, "provider": provider, "provider_mode": provider_mode, "run_id": run_id, "market_coverage_status": market_status}

    logger.info(
        "Baseline (%s): %s %.2f | Actual: %s %.2f",
        cfg.BASELINE_MODE, offer_currency, baseline, offer_currency, best.total_price,
    )

    # --- Última alerta ---
    last_alert_row = get_last_alert(cfg.DB_PATH, **itinerary_scope)
    last_alerted_price = last_alert_row["price_alerted"] if last_alert_row else None
    last_alert_time = (
        datetime.fromisoformat(last_alert_row["alerted_at"]) if last_alert_row else None
    )

    drop_abs = baseline - best.total_price
    drop_pct = (drop_abs / baseline * 100) if baseline > 0 else 0

    trigger, reason = should_alert(
        current_price=best.total_price, baseline_price=baseline,
        last_alerted_price=last_alerted_price, last_alert_time=last_alert_time,
        absolute_drop=cfg.ALERT_ABSOLUTE_DROP, percent_drop=cfg.ALERT_PERCENT_DROP,
        mode=cfg.ALERT_MODE, min_hours_between=cfg.ALERT_MIN_HOURS_BETWEEN,
        currency=offer_currency,
    )

    if trigger and getattr(cfg, "MARKET_ALERT_REQUIRE_VALIDATION", True) and market_status != "CONSISTENT":
        trigger = False
        reason = (
            f"Precio cumple umbral, pero la alerta se suprime porque la cobertura de mercado es {market_status}. "
            "El precio se conserva en histórico, sin presentarlo como mejor precio exhaustivo."
        )

    logger.info("Evaluación: %s — %s",
                "✅ ALERTA" if trigger else "🔕 Sin alerta", reason)

    if not trigger:
        return {"status": "OK_NO_ALERT", "offers": len(ranked), "best_price": best.total_price, "currency": offer_currency, "provider": provider, "provider_mode": provider_mode, "run_id": run_id, "market_coverage_status": market_status}

    # --- Email ---
    stats = get_price_stats(cfg.DB_PATH, **itinerary_scope)
    email_sent = False
    email_error: Optional[str] = None

    try:
        send_alert_email(
            ranked_offers=ranked,
            origins_considered=cfg.ORIGINS,
            baseline_price=baseline,
            drop_absolute=drop_abs,
            drop_percent=drop_pct,
            last_alerted_price=last_alerted_price,
            currency=offer_currency,
            stats=stats,
            smtp_host=cfg.SMTP_HOST, smtp_port=cfg.SMTP_PORT,
            smtp_use_tls=cfg.SMTP_USE_TLS,
            smtp_user=cfg.SMTP_USER, smtp_password=cfg.SMTP_PASSWORD,
            email_from=cfg.EMAIL_FROM, email_to=cfg.EMAIL_TO,
            subject_prefix=cfg.EMAIL_SUBJECT_PREFIX,
        )
        email_sent = True
    except Exception as e:
        email_error = str(e)
        logger.error("Error al enviar email: %s", e, extra={"smtp_error": str(e)})

    alert_id = save_alert(
        db_path=cfg.DB_PATH, snapshot_id=run_id,
        price_alerted=best.total_price, baseline_price=baseline,
        drop_absolute=drop_abs, drop_percent=drop_pct,
        email_sent=email_sent, email_error=email_error,
    )
    logger.info("Alerta #%d registrada. Email: %s", alert_id, "✓" if email_sent else "✗")
    logger.info("=" * 60)
    return {"status": "ALERT", "offers": len(ranked), "best_price": best.total_price,
            "currency": offer_currency, "email_sent": email_sent, "email_error": email_error,
            "provider": provider, "provider_mode": provider_mode, "run_id": run_id, "market_coverage_status": market_status}
