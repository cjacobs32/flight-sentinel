"""
Persistencia: SQLite con historial enriquecido, análisis y CSV.

Tablas
──────
  snapshots   — cada corrida (con o sin resultados)
  offers      — todas las ofertas filtradas de cada corrida (1 fila por oferta)
  alerts      — alertas disparadas

Deduplicación
─────────────
  Un registro en `offers` es único por (run_id, content_hash).
  `snapshots` son únicos por timestamp (cada corrida es distinta).
"""
import csv
import io
import json
import logging
import sqlite3
from datetime import datetime, UTC
from pathlib import Path
from typing import List, Optional

from src.normalizer import fmt_price_csv, normalize_airlines

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conexión
# ---------------------------------------------------------------------------

def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

def initialize_db(db_path: str) -> None:
    with get_connection(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at          TEXT NOT NULL,
                monitor_id      TEXT NOT NULL DEFAULT 'legacy',
                monitor_name    TEXT,
                origin          TEXT,
                destination     TEXT NOT NULL,
                date_out        TEXT NOT NULL,
                date_ret        TEXT NOT NULL,
                currency        TEXT NOT NULL,
                best_price      REAL,
                adults          INTEGER NOT NULL,
                children        INTEGER NOT NULL,
                offers_found    INTEGER NOT NULL DEFAULT 0,
                error           TEXT,
                provider        TEXT NOT NULL DEFAULT 'unknown',
                provider_mode   TEXT NOT NULL DEFAULT 'unknown',
                outbound_detected INTEGER NOT NULL DEFAULT 0,
                outbound_explored INTEGER NOT NULL DEFAULT 0,
                queries_used      INTEGER NOT NULL DEFAULT 0,
                coverage_status   TEXT NOT NULL DEFAULT 'UNKNOWN',
                market_coverage_status TEXT NOT NULL DEFAULT 'NOT_RUN',
                market_audit_json TEXT
            );

            CREATE TABLE IF NOT EXISTS offers (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id              INTEGER NOT NULL REFERENCES snapshots(id),
                content_hash        TEXT NOT NULL,
                rank                INTEGER NOT NULL,
                origin              TEXT NOT NULL,
                destination         TEXT NOT NULL,
                date_out            TEXT NOT NULL,
                date_ret            TEXT NOT NULL,
                price               REAL NOT NULL,
                currency            TEXT NOT NULL,
                cabin_class         TEXT NOT NULL,
                airlines            TEXT NOT NULL,
                total_duration_min  INTEGER NOT NULL,
                stops_outbound      INTEGER NOT NULL,
                stops_inbound       INTEGER NOT NULL,
                total_stops         INTEGER NOT NULL,
                total_layover_hours REAL NOT NULL,
                outbound_dep        TEXT,
                inbound_dep         TEXT,
                itinerary           TEXT,
                itinerary_hash      TEXT,
                offer_json          TEXT NOT NULL,
                UNIQUE(run_id, content_hash)
            );

            CREATE INDEX IF NOT EXISTS idx_offers_run  ON offers(run_id);
            CREATE INDEX IF NOT EXISTS idx_offers_hash ON offers(content_hash);
            CREATE INDEX IF NOT EXISTS idx_snap_scope
                ON snapshots(destination, date_out, date_ret, currency);
            CREATE INDEX IF NOT EXISTS idx_snap_monitor
                ON snapshots(monitor_id, run_at);

            CREATE TABLE IF NOT EXISTS alerts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                alerted_at      TEXT NOT NULL,
                snapshot_id     INTEGER NOT NULL REFERENCES snapshots(id),
                price_alerted   REAL NOT NULL,
                baseline_price  REAL NOT NULL,
                drop_absolute   REAL NOT NULL,
                drop_percent    REAL NOT NULL,
                email_sent      INTEGER NOT NULL DEFAULT 0,
                email_error     TEXT
            );
        """)
    # Migración: agregar columnas nuevas a DBs existentes (idempotente)
    # SQLite no soporta "ADD COLUMN IF NOT EXISTS" — ignoramos el error si ya existe
    with get_connection(db_path) as conn:
        for col_sql in [
            "ALTER TABLE snapshots ADD COLUMN monitor_id TEXT NOT NULL DEFAULT 'legacy'",
            "ALTER TABLE snapshots ADD COLUMN monitor_name TEXT",
            "ALTER TABLE offers ADD COLUMN itinerary TEXT",
            "ALTER TABLE offers ADD COLUMN itinerary_hash TEXT",
            "ALTER TABLE snapshots ADD COLUMN provider TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE snapshots ADD COLUMN provider_mode TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE snapshots ADD COLUMN outbound_detected INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE snapshots ADD COLUMN outbound_explored INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE snapshots ADD COLUMN queries_used INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE snapshots ADD COLUMN coverage_status TEXT NOT NULL DEFAULT 'UNKNOWN'",
            "ALTER TABLE snapshots ADD COLUMN market_coverage_status TEXT NOT NULL DEFAULT 'NOT_RUN'",
            "ALTER TABLE snapshots ADD COLUMN market_audit_json TEXT",
        ]:
            try:
                conn.execute(col_sql)
            except Exception:
                pass   # columna ya existe, ignorar
    logger.debug("DB inicializada en %s", db_path)


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def save_snapshot(
    db_path: str,
    *,
    origin: Optional[str],
    destination: str,
    date_out: str,
    date_ret: str,
    currency: str,
    best_price: Optional[float],
    adults: int,
    children: int,
    offers_found: int = 0,
    error: Optional[str] = None,
    monitor_id: str = "legacy",
    monitor_name: Optional[str] = None,
    provider: str = "unknown",
    provider_mode: str = "unknown",
    outbound_detected: int = 0,
    outbound_explored: int = 0,
    queries_used: int = 0,
    coverage_status: str = "UNKNOWN",
    market_coverage_status: str = "NOT_RUN",
    market_audit: Optional[dict] = None,
) -> int:
    run_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO snapshots
               (run_at, monitor_id, monitor_name, origin, destination, date_out, date_ret, currency,
                best_price, adults, children, offers_found, error, provider, provider_mode,
                outbound_detected, outbound_explored, queries_used, coverage_status,
                market_coverage_status, market_audit_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_at, monitor_id, monitor_name, origin, destination, date_out, date_ret, currency,
             best_price, adults, children, offers_found, error, provider, provider_mode,
             outbound_detected, outbound_explored, queries_used, coverage_status,
             market_coverage_status, json.dumps(market_audit, ensure_ascii=False) if market_audit else None),
        )
        return cur.lastrowid


def save_offers(db_path: str, run_id: int, offers: list) -> int:
    """
    Guarda las ofertas rankeadas de una corrida.
    Ignora duplicados (mismo run_id + content_hash) silenciosamente.
    Devuelve el número de filas insertadas.
    """
    inserted = 0
    with get_connection(db_path) as conn:
        for rank, offer in enumerate(offers, start=1):
            d = offer.to_dict()
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO offers
                       (run_id, content_hash, rank, origin, destination,
                        date_out, date_ret, price, currency, cabin_class,
                        airlines, total_duration_min, stops_outbound,
                        stops_inbound, total_stops, total_layover_hours,
                        outbound_dep, inbound_dep,
                        itinerary, itinerary_hash,
                        offer_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        d["content_hash"],
                        rank,
                        d["origin"], d["destination"],
                        d["date_out"], d["date_ret"],
                        d["total_price"], d["currency"], d["cabin_class"],
                        json.dumps(d["airlines"]),
                        d["total_duration_minutes"],
                        d["stops_outbound"], d["stops_inbound"],
                        d["total_stops"],
                        d["total_layover_hours"],
                        d["outbound"]["departure"],
                        d["inbound"]["departure"],
                        d.get("itinerary_str"),
                        d.get("itinerary_hash"),
                        json.dumps(d, ensure_ascii=False),
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    inserted += 1
            except sqlite3.IntegrityError:
                pass   # duplicado, ignorar
    return inserted



def update_snapshot_market_audit(db_path: str, snapshot_id: int, *, market_coverage_status: str, market_audit: dict) -> None:
    """Actualiza la conciliación de mercado de una corrida ya existente."""
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE snapshots SET market_coverage_status=?, market_audit_json=? WHERE id=?",
            (market_coverage_status, json.dumps(market_audit, ensure_ascii=False), snapshot_id),
        )


def get_market_audit(snapshot) -> dict:
    """Deserializa market_audit_json de una sqlite.Row/dict de forma tolerante."""
    if not snapshot:
        return {}
    try:
        keys = snapshot.keys()
        raw = snapshot["market_audit_json"] if "market_audit_json" in keys else None
    except Exception:
        raw = snapshot.get("market_audit_json") if isinstance(snapshot, dict) else None
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return {}

# ---------------------------------------------------------------------------
# Precios scoped por itinerario
# ---------------------------------------------------------------------------

def _scoped_prices(db_path: str, destination: str, date_out: str,
                   date_ret: str, currency: str, monitor_id: Optional[str] = None) -> List[float]:
    where = "best_price IS NOT NULL AND destination=? AND date_out=? AND date_ret=? AND currency=?"
    params = [destination, date_out, date_ret, currency]
    if monitor_id is not None:
        where += " AND monitor_id=?"
        params.append(monitor_id)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT best_price FROM snapshots WHERE {where} ORDER BY run_at",
            params,
        ).fetchall()
    return [r["best_price"] for r in rows]


def get_first_price(db_path, *, destination, date_out, date_ret, currency, monitor_id=None):
    p = _scoped_prices(db_path, destination, date_out, date_ret, currency, monitor_id)
    return p[0] if p else None


def get_min_price(db_path, *, destination, date_out, date_ret, currency, monitor_id=None):
    p = _scoped_prices(db_path, destination, date_out, date_ret, currency, monitor_id)
    return min(p) if p else None


# ---------------------------------------------------------------------------
# Análisis histórico
# ---------------------------------------------------------------------------

def get_price_stats(db_path: str, *, destination: str, date_out: str,
                    date_ret: str, currency: str, monitor_id: Optional[str] = None) -> dict:
    """
    Devuelve estadísticas de precios para el itinerario:
    min, max, avg, stddev, count, tendencia (última vs primer precio).
    """
    prices = _scoped_prices(db_path, destination, date_out, date_ret, currency, monitor_id)
    if not prices:
        return {"count": 0}
    avg = sum(prices) / len(prices)
    variance = sum((p - avg) ** 2 for p in prices) / len(prices)
    import math
    return {
        "count":       len(prices),
        "min":         min(prices),
        "max":         max(prices),
        "avg":         round(avg, 2),
        "stddev":      round(math.sqrt(variance), 2),
        "first":       prices[0],
        "latest":      prices[-1],
        "trend_pct":   round((prices[-1] - prices[0]) / prices[0] * 100, 2) if prices[0] else 0,
        "vs_avg_pct":  round((prices[-1] - avg) / avg * 100, 2) if avg else 0,
    }


# ---------------------------------------------------------------------------
# Alertas (scoped)
# ---------------------------------------------------------------------------

def get_last_alert(db_path, *, destination, date_out, date_ret, currency, monitor_id=None):
    where = "s.destination=? AND s.date_out=? AND s.date_ret=? AND s.currency=?"
    params = [destination, date_out, date_ret, currency]
    if monitor_id is not None:
        where += " AND s.monitor_id=?"
        params.append(monitor_id)
    with get_connection(db_path) as conn:
        return conn.execute(
            f"""SELECT a.* FROM alerts a
               JOIN snapshots s ON s.id = a.snapshot_id
               WHERE {where}
               ORDER BY a.alerted_at DESC LIMIT 1""",
            params,
        ).fetchone()


def save_alert(db_path, *, snapshot_id, price_alerted, baseline_price,
               drop_absolute, drop_percent, email_sent=False, email_error=None):
    alerted_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO alerts
               (alerted_at, snapshot_id, price_alerted, baseline_price,
                drop_absolute, drop_percent, email_sent, email_error)
               VALUES (?,?,?,?,?,?,?,?)""",
            (alerted_at, snapshot_id, price_alerted, baseline_price,
             drop_absolute, drop_percent, int(email_sent), email_error),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# CSV histórico
# ---------------------------------------------------------------------------

def export_csv(db_path: str, csv_path: str, *, destination: str,
               date_out: str, date_ret: str, currency: str, monitor_id: Optional[str] = None) -> int:
    """
    Exporta todas las ofertas del itinerario a CSV.
    Devuelve el número de filas escritas.
    Append-only: si el archivo ya existe agrega filas nuevas.
    """
    where = "o.destination=? AND o.date_out=? AND o.date_ret=? AND o.currency=?"
    params = [destination, date_out, date_ret, currency]
    if monitor_id is not None:
        where += " AND s.monitor_id=?"
        params.append(monitor_id)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"""SELECT s.run_at, s.monitor_id, o.rank, o.origin, o.destination,
                      o.date_out, o.date_ret, o.price, o.currency,
                      o.cabin_class, o.airlines, o.total_duration_min,
                      o.stops_outbound, o.stops_inbound, o.total_stops,
                      o.total_layover_hours, o.outbound_dep, o.inbound_dep,
                      o.itinerary, o.itinerary_hash,
                      o.content_hash
               FROM offers o
               JOIN snapshots s ON s.id = o.run_id
               WHERE {where}
               ORDER BY s.run_at, o.rank""",
            params,
        ).fetchall()

    if not rows:
        return 0

    fieldnames = [
        "timestamp", "monitor_id", "rank", "origin", "destination", "date_out", "date_ret",
        "price", "currency", "cabin_class", "airlines",
        "total_duration_min", "stops_outbound", "stops_inbound",
        "total_stops", "total_layover_hours",
        "outbound_dep", "inbound_dep",
        "itinerary", "itinerary_hash",
        "content_hash",
    ]

    p = Path(csv_path)
    write_header = not p.exists() or p.stat().st_size == 0

    # Leer hashes ya existentes para no duplicar filas
    existing_keys: set = set()
    if p.exists() and not write_header:
        with p.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing_keys.add((row.get("timestamp", ""), row.get("content_hash", "")))

    new_rows = 0
    with p.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for r in rows:
            key = (r["run_at"], r["content_hash"])
            if key in existing_keys:
                continue
            # Normalizar para presentación: precio decimal, aerolíneas legibles
            raw_airlines = json.loads(r["airlines"]) if isinstance(r["airlines"], str) else r["airlines"]
            writer.writerow({
                "timestamp":          r["run_at"],
                "monitor_id":         r["monitor_id"],
                "rank":               r["rank"],
                "origin":             r["origin"],
                "destination":        r["destination"],
                "date_out":           r["date_out"],
                "date_ret":           r["date_ret"],
                "price":              fmt_price_csv(r["price"]),
                "currency":           r["currency"],
                "cabin_class":        r["cabin_class"],
                "airlines":           json.dumps(normalize_airlines(raw_airlines), ensure_ascii=False),
                "total_duration_min": r["total_duration_min"],
                "stops_outbound":     r["stops_outbound"],
                "stops_inbound":      r["stops_inbound"],
                "total_stops":        r["total_stops"],
                "total_layover_hours":r["total_layover_hours"],
                "outbound_dep":       r["outbound_dep"],
                "inbound_dep":        r["inbound_dep"],
                "itinerary":          r["itinerary"] or "",
                "itinerary_hash":     r["itinerary_hash"] or "",
                "content_hash":       r["content_hash"],
            })
            new_rows += 1

    logger.info("CSV exportado: %s (%d filas nuevas)", csv_path, new_rows)
    return new_rows


# ---------------------------------------------------------------------------
# Consultas para dashboard V2
# ---------------------------------------------------------------------------

def get_monitor_history(db_path: str, monitor_id: str, limit: int = 100):
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        return conn.execute(
            """SELECT * FROM snapshots WHERE monitor_id=?
               ORDER BY run_at DESC LIMIT ?""", (monitor_id, limit)
        ).fetchall()


def get_latest_snapshot(db_path: str, monitor_id: str):
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        return conn.execute(
            """SELECT * FROM snapshots WHERE monitor_id=?
               ORDER BY run_at DESC LIMIT 1""", (monitor_id,)
        ).fetchone()


def get_monitor_chart(db_path: str, monitor_id: str, limit: int = 60):
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT run_at, best_price, currency FROM snapshots
               WHERE monitor_id=? AND best_price IS NOT NULL
               ORDER BY run_at DESC LIMIT ?""", (monitor_id, limit)
        ).fetchall()
    return list(reversed(rows))


def get_snapshot_offers(db_path: str, snapshot_id: int, limit: int = 20):
    """Devuelve las ofertas rankeadas de una corrida, incluyendo offer_json parseado."""
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM offers WHERE run_id=? ORDER BY rank ASC LIMIT ?""",
            (snapshot_id, limit),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["offer"] = json.loads(item.get("offer_json") or "{}")
        except Exception:
            item["offer"] = {}
        result.append(item)
    return result


def get_offer_by_id(db_path: str, offer_id: int) -> Optional[dict]:
    """Obtiene una oferta persistida por id, con offer_json parseado."""
    initialize_db(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM offers WHERE id=?", (int(offer_id),)).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["offer"] = json.loads(item.get("offer_json") or "{}")
    except Exception:
        item["offer"] = {}
    return item


def update_offer_google_details(
    db_path: str, offer_id: int, *, exact_url: Optional[str] = None,
    booking: Optional[dict] = None, baggage: Optional[dict] = None,
) -> None:
    """Persiste enriquecimiento Google sin modificar el precio observado del snapshot."""
    row = get_offer_by_id(db_path, offer_id)
    if not row:
        raise ValueError(f"Oferta {offer_id} inexistente")
    data = dict(row.get("offer") or {})
    if exact_url:
        links = dict(data.get("links") or {})
        links["google_flights_exact"] = exact_url
        links["google_flights"] = exact_url
        links["google_flights_scope"] = "EXACT"
        data["links"] = links
    if booking is not None:
        data["booking"] = dict(booking)
    if baggage is not None:
        data["baggage"] = dict(baggage)
    with get_connection(db_path) as conn:
        conn.execute("UPDATE offers SET offer_json=? WHERE id=?", (json.dumps(data, ensure_ascii=False), int(offer_id)))


def update_offer_exact_google_url(db_path: str, offer_id: int, exact_url: str) -> None:
    """Compatibilidad: persiste sólo el deep-link exacto."""
    update_offer_google_details(db_path, offer_id, exact_url=exact_url)


def get_latest_offers(db_path: str, monitor_id: str, limit: int = 20):
    snap = get_latest_snapshot(db_path, monitor_id)
    if not snap:
        return []
    return get_snapshot_offers(db_path, snap["id"], limit)

# ---------------------------------------------------------------------------
# Caché de enriquecimiento Google (equipaje + reserva + deep-link exacto)
# ---------------------------------------------------------------------------

def get_cached_verified_enrichment(db_path: str, content_hash: str) -> Optional[dict]:
    """Recupera enriquecimiento completo de la misma oferta/precio.

    El content_hash incluye precio y horarios, por lo que no reutilizamos precios
    reservables de una tarifa observada diferente.
    """
    if not content_hash:
        return None
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT offer_json FROM offers WHERE content_hash=? ORDER BY id DESC LIMIT 20",
                (content_hash,),
            ).fetchall()
        for row in rows:
            try:
                data = json.loads(row["offer_json"] or "{}")
            except Exception:
                continue
            bag = data.get("baggage") or {}
            booking = data.get("booking") or {}
            links = data.get("links") or {}
            bag_ok = str(bag.get("status") or "").upper() == "VERIFIED"
            booking_ok = str(booking.get("status") or "").upper() == "VERIFIED"
            if bag_ok or booking_ok:
                cached_bag = dict(bag) if bag else {}
                if cached_bag:
                    cached_bag["cache_hit"] = True
                    cached_bag["note"] = (
                        str(cached_bag.get("note") or "").rstrip(". " )
                        + ". Reutilizado de una corrida previa de la misma oferta/precio para ahorrar cuota SerpApi."
                    ).lstrip(". " )
                cached_booking = dict(booking) if booking else {}
                if cached_booking:
                    cached_booking["cache_hit"] = True
                return {
                    "baggage": cached_bag,
                    "booking": cached_booking,
                    "exact_url": links.get("google_flights_exact") or (bag.get("google_flights_exact_url") if isinstance(bag, dict) else None),
                }
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Caché de equipaje verificado
# ---------------------------------------------------------------------------

def get_cached_verified_baggage(db_path: str, content_hash: str) -> Optional[dict]:
    """Compatibilidad: recupera sólo el bloque de equipaje verificado."""
    cached = get_cached_verified_enrichment(db_path, content_hash)
    if not cached:
        return None
    bag = cached.get("baggage") or {}
    return bag if str(bag.get("status") or "").upper() == "VERIFIED" else None

