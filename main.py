#!/usr/bin/env python3
"""
Punto de entrada del agente de monitoreo de vuelos.

Precedencia de configuración: CLI > config.yaml > .env > defaults

Uso:
    python main.py                              # una corrida con config actual
    python main.py --daemon                     # corridas periódicas
    python main.py --depart-date 01/08/2026 --return-date 08/08/2026
    python main.py --max-stops 0                # solo vuelos directos
    python main.py --max-layover 2.5            # escalas ≤ 2.5h
    python main.py --history                    # tabla de historial
    python main.py --alerts                     # tabla de alertas
    python main.py --stats                      # análisis de precios
    python main.py --test-email                 # email de prueba
    python main.py --export-csv offers.csv      # exportar CSV
"""
import argparse
import logging
import sys
import time

from src.date_utils import format_date_ui, parse_date_ui

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("flight_monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Parsing de argumentos CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Agente de monitoreo de precios de vuelos",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Parámetros de itinerario
    p.add_argument("--depart-date",  metavar="DD/MM/AAAA",
                   help="Fecha de ida en DD/MM/AAAA (sobreescribe config.yaml / .env)")
    p.add_argument("--return-date",  metavar="DD/MM/AAAA",
                   help="Fecha de vuelta en DD/MM/AAAA (sobreescribe config.yaml / .env)")
    p.add_argument("--max-stops",    type=int, metavar="N",
                   help="Máximo de escalas por tramo (0 = directo)")
    p.add_argument("--max-layover",  type=float, metavar="H",
                   help="Máximo de horas de espera en escala")
    p.add_argument("--provider",  choices=["mock", "amadeus", "duffel", "kiwi", "serpapi"],
                   help="Provider de precios (sobreescribe PRICE_PROVIDER)")

    # Flight Sentinel V2: monitores configurables
    p.add_argument("--monitor", metavar="ID",
                   help="Ejecuta un monitor de monitors.yaml")
    p.add_argument("--all-monitors", action="store_true",
                   help="Ejecuta todos los monitores habilitados")
    p.add_argument("--monitors-file", default="monitors.yaml",
                   help="Archivo de monitores (default: monitors.yaml)")
    p.add_argument("--dashboard", action="store_true",
                   help="Abre el dashboard web local de Flight Sentinel V2")
    p.add_argument("--host", default="127.0.0.1", help="Host del dashboard")
    p.add_argument("--port", type=int, default=8766, help="Puerto del dashboard")

    # Modos de operación
    g = p.add_mutually_exclusive_group()
    g.add_argument("--daemon",      action="store_true",
                   help="Modo daemon: corridas automáticas")
    g.add_argument("--history",     action="store_true",
                   help="Muestra historial de corridas")
    g.add_argument("--alerts",      action="store_true",
                   help="Muestra historial de alertas")
    g.add_argument("--stats",       action="store_true",
                   help="Análisis de tendencia de precios")
    g.add_argument("--test-email",  action="store_true",
                   help="Envía un email de prueba")
    g.add_argument("--export-csv",  metavar="ARCHIVO.csv",
                   help="Exporta historial a CSV")

    p.add_argument("--config",  default="config.yaml",
                   help="Ruta al archivo de configuración YAML (default: config.yaml)")
    p.add_argument("--limit",   type=int, default=20,
                   help="Filas a mostrar en --history / --alerts")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Comandos de consulta
# ---------------------------------------------------------------------------

def show_history(cfg, limit: int) -> None:
    from src.database import get_connection, initialize_db
    initialize_db(cfg.DB_PATH)
    with get_connection(cfg.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT * FROM snapshots ORDER BY run_at DESC LIMIT ?", (limit,)
        ).fetchall()
    if not rows:
        print("No hay historial registrado.")
        return
    print(f"\n{'ID':>4}  {'Fecha UTC':>20}  {'Org':>4}  {'Ofertas':>7}  {'Mejor precio':>13}")
    print("─" * 58)
    for r in rows:
        price = f"{cfg.CURRENCY} {r['best_price']:>9,.2f}" if r["best_price"] else "—"
        print(f"{r['id']:>4}  {r['run_at'][:19]:>20}  {r['origin'] or '—':>4}  "
              f"{r['offers_found']:>7}  {price:>13}")


def show_alerts(cfg, limit: int) -> None:
    from src.database import get_connection, initialize_db
    initialize_db(cfg.DB_PATH)
    with get_connection(cfg.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT * FROM alerts ORDER BY alerted_at DESC LIMIT ?", (limit,)
        ).fetchall()
    if not rows:
        print("No hay alertas registradas.")
        return
    print(f"\n{'ID':>4}  {'Fecha UTC':>20}  {'Precio':>12}  {'Baseline':>12}  {'Baja%':>6}  {'Email'}")
    print("─" * 75)
    for r in rows:
        email = "✓" if r["email_sent"] else f"✗ {(r['email_error'] or '')[:20]}"
        print(f"{r['id']:>4}  {r['alerted_at'][:19]:>20}  "
              f"{cfg.CURRENCY} {r['price_alerted']:>8,.2f}  "
              f"{cfg.CURRENCY} {r['baseline_price']:>8,.2f}  "
              f"{r['drop_percent']:>5.1f}%  {email}")


def show_stats(cfg) -> None:
    from src.database import get_price_stats, initialize_db
    initialize_db(cfg.DB_PATH)
    scope = dict(destination=cfg.DESTINATION, date_out=cfg.DATE_OUTBOUND,
                 date_ret=cfg.DATE_RETURN, currency=cfg.CURRENCY)
    s = get_price_stats(cfg.DB_PATH, **scope)
    if not s.get("count"):
        print("Sin datos históricos aún.")
        return
    print(f"\n── Análisis de precios: {cfg.ORIGINS}→{cfg.DESTINATION} "
          f"({format_date_ui(cfg.DATE_OUTBOUND)} / {format_date_ui(cfg.DATE_RETURN)}) ──")
    print(f"  Corridas registradas : {s['count']}")
    print(f"  Mínimo histórico     : {cfg.CURRENCY} {s['min']:,.2f}")
    print(f"  Máximo histórico     : {cfg.CURRENCY} {s['max']:,.2f}")
    print(f"  Promedio             : {cfg.CURRENCY} {s['avg']:,.2f}")
    print(f"  Desviación estándar  : {cfg.CURRENCY} {s['stddev']:,.2f}")
    print(f"  Primer precio        : {cfg.CURRENCY} {s['first']:,.2f}")
    print(f"  Último precio        : {cfg.CURRENCY} {s['latest']:,.2f}")
    trend = s['trend_pct']
    sign = "▼" if trend < 0 else ("▲" if trend > 0 else "─")
    print(f"  Tendencia            : {sign} {trend:+.1f}% (vs primer precio)")
    vs_avg = s['vs_avg_pct']
    print(f"  Actual vs promedio   : {'⬇' if vs_avg < 0 else '⬆'} {vs_avg:+.1f}%")


def do_export_csv(cfg, csv_path: str) -> None:
    from src.database import export_csv, initialize_db
    initialize_db(cfg.DB_PATH)
    n = export_csv(cfg.DB_PATH, csv_path,
                   destination=cfg.DESTINATION, date_out=cfg.DATE_OUTBOUND,
                   date_ret=cfg.DATE_RETURN, currency=cfg.CURRENCY)
    print(f"✅ CSV exportado: {csv_path} ({n} filas nuevas)")


def do_test_email(cfg) -> None:
    from src.providers import Offer, Segment, Leg, LayoverInfo, _leg_from_segments
    from src.notifier import send_alert_email
    from datetime import datetime as dt, timedelta as td

    def make_leg(date: str, origin: str, hub: str, dest: str):
        dep1 = dt.fromisoformat(f"{date}T08:30:00")
        arr1 = dep1 + td(hours=9, minutes=30)
        dep2 = arr1 + td(hours=1, minutes=45)
        arr2 = dep2 + td(hours=3, minutes=15)
        fmt = "%Y-%m-%dT%H:%M:%S"
        segs = [
            Segment("AA", "AA900", origin, hub,
                    dep1.strftime(fmt), arr1.strftime(fmt),
                    cabin_class=cfg.CABIN_CLASS, baggage_included=True, baggage_pieces=1),
            Segment("AA", "AA910", hub, dest,
                    dep2.strftime(fmt), arr2.strftime(fmt),
                    cabin_class=cfg.CABIN_CLASS, baggage_included=True, baggage_pieces=1),
        ]
        return _leg_from_segments(segs)

    fake = Offer(
        origin=cfg.ORIGINS[0], destination=cfg.DESTINATION,
        date_out=cfg.DATE_OUTBOUND, date_ret=cfg.DATE_RETURN,
        total_price=1500.00, currency=cfg.CURRENCY, cabin_class=cfg.CABIN_CLASS,
        outbound=make_leg(cfg.DATE_OUTBOUND, cfg.ORIGINS[0], "PTY", cfg.DESTINATION),
        inbound=make_leg(cfg.DATE_RETURN, cfg.DESTINATION, "PTY", cfg.ORIGINS[0]),
        adults=cfg.ADULTS, children=len(cfg.CHILDREN_AGES),
    )
    send_alert_email(
        ranked_offers=[fake],
        origins_considered=cfg.ORIGINS,
        baseline_price=1700.00, drop_absolute=200.00, drop_percent=11.76,
        last_alerted_price=1650.00, currency=cfg.CURRENCY,
        stats={"count": 5, "min": 1450.0, "max": 1800.0, "avg": 1620.0,
               "stddev": 120.0, "first": 1700.0, "latest": 1500.0,
               "trend_pct": -11.76, "vs_avg_pct": -7.4},
        smtp_host=cfg.SMTP_HOST, smtp_port=cfg.SMTP_PORT,
        smtp_use_tls=cfg.SMTP_USE_TLS,
        smtp_user=cfg.SMTP_USER, smtp_password=cfg.SMTP_PASSWORD,
        email_from=cfg.EMAIL_FROM, email_to=cfg.EMAIL_TO,
        subject_prefix=cfg.EMAIL_SUBJECT_PREFIX,
    )
    print("✅ Email de prueba enviado.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    # Construir overrides desde CLI
    cli_overrides = {}
    if args.depart_date:
        cli_overrides["DATE_OUTBOUND"] = parse_date_ui(args.depart_date)
    if args.return_date:
        cli_overrides["DATE_RETURN"] = parse_date_ui(args.return_date)
    if args.max_stops is not None:
        cli_overrides["MAX_STOPS"] = args.max_stops
    if args.max_layover is not None:
        cli_overrides["MAX_LAYOVER_HOURS"] = args.max_layover
    if getattr(args, "provider", None):
        cli_overrides["PRICE_PROVIDER"] = args.provider

    # Cargar y validar config
    from config.settings import load_config, ConfigError
    try:
        cfg = load_config(cli_overrides=cli_overrides, yaml_path=args.config)
    except ConfigError as e:
        print(f"\n❌ {e}", file=sys.stderr)
        sys.exit(1)

    # Flight Sentinel V2: dashboard y registro de monitores
    if args.dashboard:
        from dashboard import run_dashboard
        run_dashboard(base_cfg=cfg, monitors_file=args.monitors_file, host=args.host, port=args.port)
        return

    if args.monitor or args.all_monitors:
        from src.monitor_registry import load_monitors, get_monitor, apply_monitor, MonitorError
        from src.agent import run_check
        try:
            if args.monitor:
                profiles = [get_monitor(args.monitor, args.monitors_file)]
            else:
                profiles = [m for m in load_monitors(args.monitors_file) if m.enabled]
        except MonitorError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            sys.exit(2)
        if not profiles:
            print("No hay monitores habilitados en monitors.yaml.")
            return
        failures = 0
        for profile in profiles:
            print(f"\n▶ {profile.name} [{profile.id}] — {format_date_ui(profile.date_outbound)} → {format_date_ui(profile.date_return)}")
            try:
                result = run_check(apply_monitor(cfg, profile))
                print(f"  Resultado: {result.get('status', 'OK')}")
            except Exception as exc:
                failures += 1
                logger.exception("Falló monitor %s", profile.id)
                print(f"  ❌ ERROR: {exc}")
        if failures:
            sys.exit(1)
        return

    # Despachar comandos legacy / monitor único desde config.yaml
    if args.history:
        show_history(cfg, args.limit)
    elif args.alerts:
        show_alerts(cfg, args.limit)
    elif args.stats:
        show_stats(cfg)
    elif args.test_email:
        do_test_email(cfg)
    elif args.export_csv:
        do_export_csv(cfg, args.export_csv)
    elif args.daemon:
        from src.agent import run_check
        interval = cfg.CHECK_INTERVAL_MINUTES * 60
        logger.info("Modo daemon: intervalo %d min. Ctrl+C para detener.", cfg.CHECK_INTERVAL_MINUTES)
        while True:
            try:
                run_check(cfg)
            except Exception as e:
                logger.error("Error en corrida daemon: %s", e)
            time.sleep(interval)
    else:
        from src.agent import run_check
        run_check(cfg)


if __name__ == "__main__":
    main()
