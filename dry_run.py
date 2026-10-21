#!/usr/bin/env python3
"""
dry_run.py — Prueba end-to-end SIN enviar emails.

Simula tres corridas con precios controlados y muestra exactamente
qué haría el agente en cada caso. Acepta los mismos flags de itinerario
que main.py (--depart-date, --return-date, --max-stops, --max-layover).

Uso:
    python dry_run.py                           # 3 corridas mock, DB temporal
    python dry_run.py --max-stops 0             # fuerza solo vuelos directos
    python dry_run.py --depart-date 2027-08-10 --return-date 2027-08-17
    python dry_run.py --keep-db                 # guarda dry_run_test.db
    python dry_run.py --show-email              # imprime HTML en consola
"""
import argparse
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("dry_run")

from datetime import datetime, timedelta
from src.database import (
    initialize_db, save_snapshot, save_offers, save_alert,
    get_first_price, get_min_price, get_last_alert, get_price_stats,
    export_csv, get_connection,
)
from src.providers import Offer, Segment, _leg_from_segments, _passes_filters, rank_offers
from src.alert_logic import should_alert
from src.notifier import _build_email_body


def _fake_offer(cfg, price: float) -> Offer:
    def make_leg(date, origin, hub, dest):
        dep1 = datetime.fromisoformat(f"{date}T08:00:00")
        arr1 = dep1 + timedelta(hours=9)
        dep2 = arr1 + timedelta(hours=1, minutes=30)
        arr2 = dep2 + timedelta(hours=3)
        fmt = "%Y-%m-%dT%H:%M:%S"
        segs = [
            Segment("AA", "AA900", origin, hub,
                    dep1.strftime(fmt), arr1.strftime(fmt),
                    cabin_class=cfg.CABIN_CLASS, baggage_included=True, baggage_pieces=1),
            Segment("AA", "AA910", hub, dest,
                    dep2.strftime(fmt), arr2.strftime(fmt),
                    cabin_class=cfg.CABIN_CLASS, baggage_included=False, baggage_pieces=0),
        ]
        return _leg_from_segments(segs)

    return Offer(
        origin=cfg.ORIGINS[0], destination=cfg.DESTINATION,
        date_out=cfg.DATE_OUTBOUND, date_ret=cfg.DATE_RETURN,
        total_price=price, currency=cfg.CURRENCY, cabin_class=cfg.CABIN_CLASS,
        outbound=make_leg(cfg.DATE_OUTBOUND, cfg.ORIGINS[0], "PTY", cfg.DESTINATION),
        inbound=make_leg(cfg.DATE_RETURN, cfg.DESTINATION, "PTY", cfg.ORIGINS[0]),
        adults=cfg.ADULTS, children=len(cfg.CHILDREN_AGES),
    )


def run_simulated_check(cfg, db_path: str, price: float,
                         run_label: str, show_email: bool = False) -> None:
    sep = "─" * 60
    logger.info("%s", sep)
    logger.info("▶  %s  |  Precio simulado: %s %.2f", run_label, cfg.CURRENCY, price)

    itinerary_scope = dict(
        destination=cfg.DESTINATION, date_out=cfg.DATE_OUTBOUND,
        date_ret=cfg.DATE_RETURN, currency=cfg.CURRENCY,
    )

    offer = _fake_offer(cfg, price)
    passes, reason = _passes_filters(offer, cfg.MAX_STOPS, cfg.MAX_LAYOVER_HOURS)
    if not passes:
        logger.warning("Oferta descartada por filtros: %s", reason)
        return

    ranked = rank_offers([offer])

    # Baseline ANTES de insertar
    baseline = (get_first_price if cfg.BASELINE_MODE == "first" else get_min_price)(
        db_path, **itinerary_scope
    )

    run_id = save_snapshot(
        db_path=db_path, origin=offer.origin, destination=cfg.DESTINATION,
        date_out=cfg.DATE_OUTBOUND, date_ret=cfg.DATE_RETURN,
        currency=cfg.CURRENCY, best_price=price,
        adults=cfg.ADULTS, children=len(cfg.CHILDREN_AGES), offers_found=1,
    )
    save_offers(db_path, run_id, ranked)
    logger.info("Snapshot #%d guardado.", run_id)

    if baseline is None:
        logger.info("Primera corrida. Baseline: %s %.2f. Sin alerta.", cfg.CURRENCY, price)
        return

    logger.info("Baseline (%s): %s %.2f  |  Actual: %s %.2f",
                cfg.BASELINE_MODE, cfg.CURRENCY, baseline, cfg.CURRENCY, price)

    last_row = get_last_alert(db_path, **itinerary_scope)
    last_price = last_row["price_alerted"] if last_row else None
    last_time  = datetime.fromisoformat(last_row["alerted_at"]) if last_row else None

    drop_abs = baseline - price
    drop_pct = (drop_abs / baseline * 100) if baseline > 0 else 0

    trigger, reason = should_alert(
        current_price=price, baseline_price=baseline,
        last_alerted_price=last_price, last_alert_time=last_time,
        absolute_drop=cfg.ALERT_ABSOLUTE_DROP, percent_drop=cfg.ALERT_PERCENT_DROP,
        mode=cfg.ALERT_MODE, min_hours_between=cfg.ALERT_MIN_HOURS_BETWEEN,
        currency=cfg.CURRENCY,
    )
    logger.info("Evaluación: %s — %s", "✅ ALERTA" if trigger else "🔕 Sin alerta", reason)

    if not trigger:
        return

    stats = get_price_stats(db_path, **itinerary_scope)
    html = _build_email_body(
        ranked_offers=ranked, origins_considered=cfg.ORIGINS,
        baseline_price=baseline, drop_absolute=drop_abs, drop_percent=drop_pct,
        last_alerted_price=last_price, currency=cfg.CURRENCY, stats=stats,
    )

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  📧  EMAIL QUE SE ENVIARÍA  (dry-run, no enviado)       ║")
    print("╠══════════════════════════════════════════════════════════╣")
    to_str = ", ".join(cfg.EMAIL_TO) if cfg.EMAIL_TO else "(EMAIL_TO no configurado)"
    print(f"║  Para:   {to_str[:51]:<51}║")
    subj = f"{cfg.EMAIL_SUBJECT_PREFIX} ¡Precio bajo! {offer.origin}→{cfg.DESTINATION} | {cfg.CURRENCY} {price:,.0f} (-{drop_pct:.1f}%)"
    print(f"║  Asunto: {subj[:51]:<51}║")
    print("╚══════════════════════════════════════════════════════════╝")

    if show_email:
        print("\n── HTML del email ──────────────────────────────────────────")
        print(html)
        print("────────────────────────────────────────────────────────────")
    else:
        print("  (usá --show-email para ver el HTML completo)\n")

    save_alert(
        db_path=db_path, snapshot_id=run_id,
        price_alerted=price, baseline_price=baseline,
        drop_absolute=drop_abs, drop_percent=drop_pct,
        email_sent=False, email_error="dry-run",
    )
    logger.info("Alerta registrada en DB (email_sent=False).")


SCENARIOS = [
    ("Corrida 1/3", 1_800.00, "Precio inicial — establece baseline"),
    ("Corrida 2/3", 1_850.00, "Precio sube — no debe alertar"),
    ("Corrida 3/3", 1_500.00, "Precio baja −16.7% — debe alertar"),
]


def main():
    p = argparse.ArgumentParser(description="Dry-run del agente (sin emails)")
    p.add_argument("--depart-date",  metavar="YYYY-MM-DD")
    p.add_argument("--return-date",  metavar="YYYY-MM-DD")
    p.add_argument("--max-stops",    type=int, metavar="N")
    p.add_argument("--max-layover",  type=float, metavar="H")
    p.add_argument("--keep-db",    action="store_true", help="Guarda dry_run_test.db")
    p.add_argument("--show-email", action="store_true", help="Imprime HTML en consola")
    p.add_argument("--provider",   choices=["mock", "amadeus", "duffel", "kiwi", "serpapi"],
                   help="Provider de precios (sobreescribe PRICE_PROVIDER)")
    p.add_argument("--config", default="config.yaml")
    args = p.parse_args()

    cli = {}
    if args.depart_date:  cli["DATE_OUTBOUND"]    = args.depart_date
    if args.return_date:  cli["DATE_RETURN"]       = args.return_date
    if args.max_stops is not None: cli["MAX_STOPS"] = args.max_stops
    if args.max_layover is not None: cli["MAX_LAYOVER_HOURS"] = args.max_layover
    if getattr(args, "provider", None): cli["PRICE_PROVIDER"] = args.provider

    from config.settings import load_config, ConfigError
    try:
        cfg = load_config(cli_overrides=cli, yaml_path=args.config)
    except ConfigError as e:
        print(f"\n❌ {e}", file=sys.stderr)
        sys.exit(1)

    if args.keep_db:
        db_path = "dry_run_test.db"
        if os.path.exists(db_path):
            os.remove(db_path)
        logger.info("DB persistida en: %s", db_path)
    else:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = tmp.name
        tmp.close()

    try:
        initialize_db(db_path)
        logger.info(
            "Config: %s→%s | %s/%s | stops≤%s | layover≤%sh | "
            "alerta si baja ≥%s%s o ≥%s%%",
            cfg.ORIGINS, cfg.DESTINATION, cfg.DATE_OUTBOUND, cfg.DATE_RETURN,
            cfg.MAX_STOPS, cfg.MAX_LAYOVER_HOURS,
            cfg.ALERT_ABSOLUTE_DROP, cfg.CURRENCY, cfg.ALERT_PERCENT_DROP,
        )
        print()

        for label, price, desc in SCENARIOS:
            logger.info("Escenario: %s", desc)
            run_simulated_check(cfg, db_path, price, label, show_email=args.show_email)
            print()

        # Resumen
        print("═" * 60)
        print("  RESUMEN — Snapshots")
        print("═" * 60)
        with get_connection(db_path) as conn:
            for r in conn.execute("SELECT id,run_at,origin,best_price,offers_found FROM snapshots ORDER BY id").fetchall():
                p_str = f"{cfg.CURRENCY} {r['best_price']:,.2f}" if r["best_price"] else "—"
                print(f"  #{r['id']}  {r['run_at'][:19]}  {r['origin'] or '—':>4}  {p_str}  ({r['offers_found']} oferta(s))")

        print()
        print("  RESUMEN — Alertas")
        print("═" * 60)
        with get_connection(db_path) as conn:
            alerts = conn.execute("SELECT * FROM alerts ORDER BY id").fetchall()
        if alerts:
            for a in alerts:
                print(f"  #{a['id']}  {a['alerted_at'][:19]}  "
                      f"{cfg.CURRENCY} {a['price_alerted']:,.2f}  "
                      f"-{a['drop_percent']:.1f}%  email=no (dry-run)")
        else:
            print("  (ninguna)")

        # Stats
        print()
        print("  ANÁLISIS DE PRECIOS")
        print("═" * 60)
        from src.database import get_price_stats
        s = get_price_stats(db_path, destination=cfg.DESTINATION,
                            date_out=cfg.DATE_OUTBOUND, date_ret=cfg.DATE_RETURN,
                            currency=cfg.CURRENCY)
        if s.get("count"):
            print(f"  Corridas: {s['count']} | Min: {cfg.CURRENCY} {s['min']:,.2f} | "
                  f"Avg: {cfg.CURRENCY} {s['avg']:,.2f} | "
                  f"Tendencia: {s['trend_pct']:+.1f}%")

        # CSV
        csv_path = db_path.replace(".db", "_history.csv")
        export_csv(db_path, csv_path,
                   destination=cfg.DESTINATION, date_out=cfg.DATE_OUTBOUND,
                   date_ret=cfg.DATE_RETURN, currency=cfg.CURRENCY)
        if os.path.exists(csv_path):
            print(f"\n  CSV generado: {csv_path}")
            if args.keep_db:
                import shutil
                shutil.copy(csv_path, "dry_run_history.csv")
                print("  Copiado a: dry_run_history.csv")
        print()

    finally:
        if not args.keep_db:
            os.unlink(db_path)
            if os.path.exists(db_path.replace(".db", "_history.csv")):
                os.unlink(db_path.replace(".db", "_history.csv"))
            logger.info("Archivos temporales eliminados.")


if __name__ == "__main__":
    main()
