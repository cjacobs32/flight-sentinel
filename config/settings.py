"""
Configuración central del agente de monitoreo de vuelos.

Precedencia de configuración (mayor a menor):
  1. Argumentos CLI      (pasados como overrides al llamar a load_config)
  2. config.yaml         (archivo en el directorio de trabajo)
  3. Variables de entorno / .env
  4. Defaults hardcodeados aquí

Uso típico:
    from config.settings import load_config, cfg
    load_config(cli_overrides={"date_outbound": "2026-08-01"})
    print(cfg.DATE_OUTBOUND)
"""
from __future__ import annotations

import os
import sys
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from src.date_utils import format_date_ui

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Carga de .env (opcional, no falla si no existe)
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)   # no sobreescribe vars ya seteadas en el entorno
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Dataclass de configuración validada
# ---------------------------------------------------------------------------

@dataclass
class Config:
    # ── Itinerario ────────────────────────────────────────────────────────
    ORIGINS: List[str]
    DESTINATION: str
    DATE_OUTBOUND: str           # YYYY-MM-DD
    DATE_RETURN: str             # YYYY-MM-DD

    # ── Pasajeros ─────────────────────────────────────────────────────────
    ADULTS: int
    CHILDREN_AGES: List[int]

    # ── Filtros de búsqueda ───────────────────────────────────────────────
    MAX_STOPS: Optional[int]
    MAX_LAYOVER_HOURS: Optional[float]
    CABIN_CLASS: str
    CURRENCY: str

    # ── Umbrales de alerta ────────────────────────────────────────────────
    ALERT_ABSOLUTE_DROP: float
    ALERT_PERCENT_DROP: float
    ALERT_MODE: str
    BASELINE_MODE: str
    ALERT_MIN_HOURS_BETWEEN: float

    # ── Email / SMTP ──────────────────────────────────────────────────────
    SMTP_HOST: str
    SMTP_PORT: int
    SMTP_USE_TLS: bool
    SMTP_USER: str
    SMTP_PASSWORD: str
    EMAIL_FROM: str
    EMAIL_TO: List[str]
    EMAIL_SUBJECT_PREFIX: str

    # ── Persistencia ──────────────────────────────────────────────────────
    DB_PATH: str

    # ── Scheduler ─────────────────────────────────────────────────────────
    CHECK_INTERVAL_MINUTES: int

    # ── Proveedor ─────────────────────────────────────────────────────────
    PRICE_PROVIDER: str
    AMADEUS_API_KEY: str
    AMADEUS_API_SECRET: str
    AMADEUS_ENV: str
    # Duffel
    DUFFEL_API_TOKEN: str        # duffel_test_XXX (sandbox) o duffel_live_XXX (producción)
    # Kiwi Tequila API (gratuito — registro en https://tequila.kiwi.com/)
    KIWI_API_KEY: str
    KIWI_MAX_RESULTS: int
    # SerpApi Google Flights (gratuito: 250 búsquedas/mes, sin CC)
    SERPAPI_API_KEY: str
    SERPAPI_MAX_OUTBOUND_TOKENS: int  # techo de departure_tokens a explorar
    SERPAPI_COMBINE_ORIGINS: bool
    SERPAPI_SHOW_HIDDEN: bool
    SERPAPI_GL: str
    SERPAPI_HL: str
    SERPAPI_BAGGAGE_ENRICH: bool
    SERPAPI_BAGGAGE_MAX_OFFERS: int

    # ── Automatización V2.5 WATCH → VERIFY → AUDIT ────────────────────────
    WATCH_PRICE_ABS_CHANGE: float
    WATCH_PRICE_PERCENT_CHANGE: float
    WATCH_VERIFY_MAX_OFFERS: int
    AUDIT_VERIFY_MAX_OFFERS: int
    SERPAPI_AUTO_MONTHLY_CAP: int
    SERPAPI_MANUAL_RESERVE: int

    # ── Auditoría de mercado gratuita ─────────────────────────────────────
    MARKET_AUDIT_ENABLED: bool
    MARKET_AUDIT_AUTO: bool
    DESPEGAR_HEADLESS: bool
    DESPEGAR_TIMEOUT_SECONDS: int
    DESPEGAR_MAX_SCROLLS: int
    MARKET_ALERT_REQUIRE_VALIDATION: bool

    # ── Identidad del monitor (V2) ─────────────────────────────────────────
    MONITOR_ID: str
    MONITOR_NAME: str


# ---------------------------------------------------------------------------
# Valores por defecto
# ---------------------------------------------------------------------------

_DEFAULTS: dict = {
    "ORIGINS":                  ["EZE", "AEP"],
    "DESTINATION":              "MIA",
    "DATE_OUTBOUND":            "2027-07-04",
    "DATE_RETURN":              "2027-07-11",
    "ADULTS":                   2,
    "CHILDREN_AGES":            [7],
    "MAX_STOPS":                1,
    "MAX_LAYOVER_HOURS":        2.0,
    "CABIN_CLASS":              "ECONOMY",
    "CURRENCY":                 "USD",
    "ALERT_ABSOLUTE_DROP":      50.0,
    "ALERT_PERCENT_DROP":       5.0,
    "ALERT_MODE":               "either",
    "BASELINE_MODE":            "min",
    "ALERT_MIN_HOURS_BETWEEN":  6.0,
    "SMTP_HOST":                "smtp.gmail.com",
    "SMTP_PORT":                587,
    "SMTP_USE_TLS":             True,
    "SMTP_USER":                "",
    "SMTP_PASSWORD":            "",
    "EMAIL_FROM":               "",
    "EMAIL_TO":                 [],
    "EMAIL_SUBJECT_PREFIX":     "[Flight Sentinel]",
    "DB_PATH":                  "flight_monitor.db",
    "CHECK_INTERVAL_MINUTES":   60,
    "PRICE_PROVIDER":           "serpapi",
    "AMADEUS_API_KEY":          "",
    "AMADEUS_API_SECRET":       "",
    "AMADEUS_ENV":              "test",
    "DUFFEL_API_TOKEN":         "",
    "KIWI_API_KEY":             "",
    "KIWI_MAX_RESULTS":         50,
    "SERPAPI_API_KEY":           "",
    "SERPAPI_MAX_OUTBOUND_TOKENS": 15,
    "SERPAPI_COMBINE_ORIGINS":    True,
    "SERPAPI_SHOW_HIDDEN":        True,
    "SERPAPI_GL":                 "ar",
    "SERPAPI_HL":                 "es",
    "SERPAPI_BAGGAGE_ENRICH":     True,
    "SERPAPI_BAGGAGE_MAX_OFFERS": 6,
    "WATCH_PRICE_ABS_CHANGE":     25.0,
    "WATCH_PRICE_PERCENT_CHANGE": 1.0,
    "WATCH_VERIFY_MAX_OFFERS":    2,
    "AUDIT_VERIFY_MAX_OFFERS":    4,
    "SERPAPI_AUTO_MONTHLY_CAP":   220,
    "SERPAPI_MANUAL_RESERVE":     30,
    "MARKET_AUDIT_ENABLED":       True,
    "MARKET_AUDIT_AUTO":          False,
    "DESPEGAR_HEADLESS":          True,
    "DESPEGAR_TIMEOUT_SECONDS":   50,
    "DESPEGAR_MAX_SCROLLS":       14,
    "MARKET_ALERT_REQUIRE_VALIDATION": True,
    "MONITOR_ID":               "default",
    "MONITOR_NAME":             "Vuelo monitoreado",
}


# ---------------------------------------------------------------------------
# Lectura de variables de entorno
# ---------------------------------------------------------------------------

def _from_env() -> dict:
    def _list(key, default):
        raw = os.getenv(key)
        if raw is None:
            return default
        return [x.strip() for x in raw.split(",") if x.strip()]

    def _int_list(key, default):
        raw = os.getenv(key)
        if raw is None:
            return default
        return [int(x.strip()) for x in raw.split(",") if x.strip()]

    smtp_user = os.getenv("SMTP_USER", _DEFAULTS["SMTP_USER"])
    return {
        "ORIGINS":                  _list("ORIGINS", None),
        "DESTINATION":              os.getenv("DESTINATION"),
        "DATE_OUTBOUND":            os.getenv("DATE_OUTBOUND") or os.getenv("DEPART_DATE"),
        "DATE_RETURN":              os.getenv("DATE_RETURN") or os.getenv("RETURN_DATE"),
        "ADULTS":                   int(os.getenv("ADULTS")) if os.getenv("ADULTS") else None,
        "CHILDREN_AGES":            _int_list("CHILDREN_AGES", None),
        "MAX_STOPS":                int(os.getenv("MAX_STOPS")) if os.getenv("MAX_STOPS") else None,
        "MAX_LAYOVER_HOURS":        float(os.getenv("MAX_LAYOVER_HOURS")) if os.getenv("MAX_LAYOVER_HOURS") else None,
        "CABIN_CLASS":              os.getenv("CABIN_CLASS"),
        "CURRENCY":                 os.getenv("CURRENCY"),
        "ALERT_ABSOLUTE_DROP":      float(os.getenv("ALERT_ABSOLUTE_DROP")) if os.getenv("ALERT_ABSOLUTE_DROP") else None,
        "ALERT_PERCENT_DROP":       float(os.getenv("ALERT_PERCENT_DROP")) if os.getenv("ALERT_PERCENT_DROP") else None,
        "ALERT_MODE":               os.getenv("ALERT_MODE"),
        "BASELINE_MODE":            os.getenv("BASELINE_MODE"),
        "ALERT_MIN_HOURS_BETWEEN":  float(os.getenv("ALERT_MIN_HOURS_BETWEEN")) if os.getenv("ALERT_MIN_HOURS_BETWEEN") else None,
        "SMTP_HOST":                os.getenv("SMTP_HOST"),
        "SMTP_PORT":                int(os.getenv("SMTP_PORT")) if os.getenv("SMTP_PORT") else None,
        "SMTP_USE_TLS":             (os.getenv("SMTP_USE_TLS", "").lower() == "true") if os.getenv("SMTP_USE_TLS") else None,
        "SMTP_USER":                smtp_user or None,
        "SMTP_PASSWORD":            os.getenv("SMTP_PASSWORD"),
        "EMAIL_FROM":               os.getenv("EMAIL_FROM") or smtp_user or None,
        "EMAIL_TO":                 _list("EMAIL_TO", None),
        "EMAIL_SUBJECT_PREFIX":     os.getenv("EMAIL_SUBJECT_PREFIX"),
        "DB_PATH":                  os.getenv("DB_PATH"),
        "CHECK_INTERVAL_MINUTES":   int(os.getenv("CHECK_INTERVAL_MINUTES")) if os.getenv("CHECK_INTERVAL_MINUTES") else None,
        "PRICE_PROVIDER":           os.getenv("PRICE_PROVIDER"),
        "AMADEUS_API_KEY":          os.getenv("AMADEUS_API_KEY"),
        "AMADEUS_API_SECRET":       os.getenv("AMADEUS_API_SECRET"),
        "AMADEUS_ENV":              os.getenv("AMADEUS_ENV"),
        "DUFFEL_API_TOKEN":         os.getenv("DUFFEL_API_TOKEN"),
        "KIWI_API_KEY":             os.getenv("KIWI_API_KEY"),
        "KIWI_MAX_RESULTS":         int(os.getenv("KIWI_MAX_RESULTS")) if os.getenv("KIWI_MAX_RESULTS") else None,
        "SERPAPI_API_KEY":           os.getenv("SERPAPI_API_KEY"),
        "SERPAPI_MAX_OUTBOUND_TOKENS": int(os.getenv("SERPAPI_MAX_OUTBOUND_TOKENS")) if os.getenv("SERPAPI_MAX_OUTBOUND_TOKENS") else None,
        "SERPAPI_COMBINE_ORIGINS":    (os.getenv("SERPAPI_COMBINE_ORIGINS", "").lower() == "true") if os.getenv("SERPAPI_COMBINE_ORIGINS") else None,
        "SERPAPI_SHOW_HIDDEN":        (os.getenv("SERPAPI_SHOW_HIDDEN", "").lower() == "true") if os.getenv("SERPAPI_SHOW_HIDDEN") else None,
        "SERPAPI_GL":                 os.getenv("SERPAPI_GL"),
        "SERPAPI_HL":                 os.getenv("SERPAPI_HL"),
        "SERPAPI_BAGGAGE_ENRICH":     (os.getenv("SERPAPI_BAGGAGE_ENRICH", "").lower() == "true") if os.getenv("SERPAPI_BAGGAGE_ENRICH") else None,
        "SERPAPI_BAGGAGE_MAX_OFFERS": int(os.getenv("SERPAPI_BAGGAGE_MAX_OFFERS")) if os.getenv("SERPAPI_BAGGAGE_MAX_OFFERS") else None,
        "WATCH_PRICE_ABS_CHANGE": float(os.getenv("WATCH_PRICE_ABS_CHANGE")) if os.getenv("WATCH_PRICE_ABS_CHANGE") else None,
        "WATCH_PRICE_PERCENT_CHANGE": float(os.getenv("WATCH_PRICE_PERCENT_CHANGE")) if os.getenv("WATCH_PRICE_PERCENT_CHANGE") else None,
        "WATCH_VERIFY_MAX_OFFERS": int(os.getenv("WATCH_VERIFY_MAX_OFFERS")) if os.getenv("WATCH_VERIFY_MAX_OFFERS") else None,
        "AUDIT_VERIFY_MAX_OFFERS": int(os.getenv("AUDIT_VERIFY_MAX_OFFERS")) if os.getenv("AUDIT_VERIFY_MAX_OFFERS") else None,
        "SERPAPI_AUTO_MONTHLY_CAP": int(os.getenv("SERPAPI_AUTO_MONTHLY_CAP")) if os.getenv("SERPAPI_AUTO_MONTHLY_CAP") else None,
        "SERPAPI_MANUAL_RESERVE": int(os.getenv("SERPAPI_MANUAL_RESERVE")) if os.getenv("SERPAPI_MANUAL_RESERVE") else None,
        "MARKET_AUDIT_ENABLED":       (os.getenv("MARKET_AUDIT_ENABLED", "").lower() == "true") if os.getenv("MARKET_AUDIT_ENABLED") else None,
        "MARKET_AUDIT_AUTO":          (os.getenv("MARKET_AUDIT_AUTO", "").lower() == "true") if os.getenv("MARKET_AUDIT_AUTO") else None,
        "DESPEGAR_HEADLESS":          (os.getenv("DESPEGAR_HEADLESS", "").lower() == "true") if os.getenv("DESPEGAR_HEADLESS") else None,
        "DESPEGAR_TIMEOUT_SECONDS":   int(os.getenv("DESPEGAR_TIMEOUT_SECONDS")) if os.getenv("DESPEGAR_TIMEOUT_SECONDS") else None,
        "DESPEGAR_MAX_SCROLLS":       int(os.getenv("DESPEGAR_MAX_SCROLLS")) if os.getenv("DESPEGAR_MAX_SCROLLS") else None,
        "MARKET_ALERT_REQUIRE_VALIDATION": (os.getenv("MARKET_ALERT_REQUIRE_VALIDATION", "").lower() == "true") if os.getenv("MARKET_ALERT_REQUIRE_VALIDATION") else None,
        "MONITOR_ID":                 os.getenv("MONITOR_ID"),
        "MONITOR_NAME":               os.getenv("MONITOR_NAME"),
    }


# ---------------------------------------------------------------------------
# Lectura de config.yaml
# ---------------------------------------------------------------------------

def _from_yaml(yaml_path: str = "config.yaml") -> dict:
    p = Path(yaml_path)
    if not p.exists():
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:
        logger.warning("PyYAML no instalado — config.yaml ignorado. Instalar: pip install pyyaml")
        return {}
    with p.open() as f:
        data = yaml.safe_load(f) or {}

    # Normalizar aliases que el usuario podría usar en YAML
    aliases = {
        "depart_date": "DATE_OUTBOUND",
        "return_date": "DATE_RETURN",
        "max_stops": "MAX_STOPS",
        "max_layover_hours": "MAX_LAYOVER_HOURS",
        "origins": "ORIGINS",
        "destination": "DESTINATION",
        "adults": "ADULTS",
        "children_ages": "CHILDREN_AGES",
        "cabin_class": "CABIN_CLASS",
        "currency": "CURRENCY",
        "price_provider": "PRICE_PROVIDER",
        "email_to": "EMAIL_TO",
        "smtp_user": "SMTP_USER",
        "smtp_password": "SMTP_PASSWORD",
        "smtp_host": "SMTP_HOST",
        "smtp_port": "SMTP_PORT",
        "db_path": "DB_PATH",
        "check_interval_minutes": "CHECK_INTERVAL_MINUTES",
        "alert_absolute_drop": "ALERT_ABSOLUTE_DROP",
        "alert_percent_drop": "ALERT_PERCENT_DROP",
        "alert_mode": "ALERT_MODE",
        "baseline_mode": "BASELINE_MODE",
        "alert_min_hours_between": "ALERT_MIN_HOURS_BETWEEN",
        "amadeus_api_key": "AMADEUS_API_KEY",
        "amadeus_api_secret": "AMADEUS_API_SECRET",
        "amadeus_env": "AMADEUS_ENV",
        "duffel_api_token": "DUFFEL_API_TOKEN",
        "kiwi_api_key": "KIWI_API_KEY",
        "kiwi_max_results": "KIWI_MAX_RESULTS",
        "serpapi_api_key": "SERPAPI_API_KEY",
        "serpapi_max_outbound_tokens": "SERPAPI_MAX_OUTBOUND_TOKENS",
        "serpapi_combine_origins": "SERPAPI_COMBINE_ORIGINS",
        "serpapi_show_hidden": "SERPAPI_SHOW_HIDDEN",
        "serpapi_gl": "SERPAPI_GL",
        "serpapi_hl": "SERPAPI_HL",
        "serpapi_baggage_enrich": "SERPAPI_BAGGAGE_ENRICH",
        "serpapi_baggage_max_offers": "SERPAPI_BAGGAGE_MAX_OFFERS",
        "watch_price_abs_change": "WATCH_PRICE_ABS_CHANGE",
        "watch_price_percent_change": "WATCH_PRICE_PERCENT_CHANGE",
        "watch_verify_max_offers": "WATCH_VERIFY_MAX_OFFERS",
        "serpapi_auto_monthly_cap": "SERPAPI_AUTO_MONTHLY_CAP",
        "serpapi_manual_reserve": "SERPAPI_MANUAL_RESERVE",
        "market_audit_enabled": "MARKET_AUDIT_ENABLED",
        "market_audit_auto": "MARKET_AUDIT_AUTO",
        "despegar_headless": "DESPEGAR_HEADLESS",
        "despegar_timeout_seconds": "DESPEGAR_TIMEOUT_SECONDS",
        "despegar_max_scrolls": "DESPEGAR_MAX_SCROLLS",
        "market_alert_require_validation": "MARKET_ALERT_REQUIRE_VALIDATION",
        "monitor_id": "MONITOR_ID",
        "monitor_name": "MONITOR_NAME",
    }
    normalized = {}
    for k, v in data.items():
        key = aliases.get(k.lower(), k.upper())
        normalized[key] = v
    return normalized


# ---------------------------------------------------------------------------
# Merge de capas (CLI > YAML > ENV > defaults)
# ---------------------------------------------------------------------------

def _merge(*layers: dict) -> dict:
    """Aplica capas de mayor a menor precedencia. None se ignora."""
    result = {}
    keys = set().union(*[d.keys() for d in layers])
    for key in keys:
        for layer in layers:
            val = layer.get(key)
            if val is not None:
                result[key] = val
                break
        if key not in result:
            result[key] = _DEFAULTS.get(key)
    return result


# ---------------------------------------------------------------------------
# Validación
# ---------------------------------------------------------------------------

class ConfigError(ValueError):
    pass


def _validate(d: dict) -> None:
    errors = []

    # Fechas
    try:
        dep = date.fromisoformat(d["DATE_OUTBOUND"])
    except (ValueError, TypeError):
        errors.append(f"DEPART_DATE inválida: '{d.get('DATE_OUTBOUND')}' — usar formato YYYY-MM-DD")
        dep = None

    try:
        ret = date.fromisoformat(d["DATE_RETURN"])
    except (ValueError, TypeError):
        errors.append(f"RETURN_DATE inválida: '{d.get('DATE_RETURN')}' — usar formato YYYY-MM-DD")
        ret = None

    today = date.today()
    if dep is not None and dep < today:
        errors.append(f"DEPART_DATE {dep} es una fecha pasada (hoy: {today})")
    if ret is not None and ret < today:
        errors.append(f"RETURN_DATE {ret} es una fecha pasada (hoy: {today})")
    if dep is not None and ret is not None and dep >= ret:
        errors.append(f"DEPART_DATE ({dep}) debe ser anterior a RETURN_DATE ({ret})")

    # MAX_STOPS
    ms = d.get("MAX_STOPS")
    if ms is not None:
        try:
            ms = int(ms)
            if ms < 0:
                errors.append(f"MAX_STOPS debe ser ≥ 0, recibido: {ms}")
        except (ValueError, TypeError):
            errors.append(f"MAX_STOPS debe ser un entero, recibido: '{ms}'")

    # MAX_LAYOVER_HOURS
    mlh = d.get("MAX_LAYOVER_HOURS")
    if mlh is not None:
        try:
            mlh = float(mlh)
            if mlh <= 0:
                errors.append(f"MAX_LAYOVER_HOURS debe ser > 0, recibido: {mlh}")
        except (ValueError, TypeError):
            errors.append(f"MAX_LAYOVER_HOURS debe ser un número, recibido: '{mlh}'")

    # V2.9: origen protegido en Buenos Aires, destino configurable por IATA.
    destination = str(d.get("DESTINATION", "")).strip().upper()
    if not (len(destination) == 3 and destination.isalpha()):
        errors.append(f"DESTINATION debe ser un código IATA de 3 letras, recibido: {destination}")

    origins = d.get("ORIGINS") or []
    invalid_origins = [o for o in origins if str(o).upper() not in {"EZE", "AEP"}]
    if invalid_origins:
        errors.append(f"ORIGINS sólo admite EZE/AEP en Flight Sentinel V2: {invalid_origins}")
    if destination in {str(o).upper() for o in origins}:
        errors.append("DESTINATION no puede coincidir con un aeropuerto de origen")

    if ms is not None and int(ms) > 1:
        errors.append("MAX_STOPS no puede superar 1: sólo directo o una escala por tramo")
    if mlh is not None and float(mlh) > 2.0:
        errors.append("MAX_LAYOVER_HOURS no puede superar 2.0 horas")

    # ADULTS
    adults = d.get("ADULTS", 1)
    try:
        if int(adults) < 1:
            errors.append("ADULTS debe ser ≥ 1")
    except (ValueError, TypeError):
        errors.append(f"ADULTS inválido: '{adults}'")

    if errors:
        msg = "\n".join(f"  • {e}" for e in errors)
        raise ConfigError(f"Errores de configuración:\n{msg}")


# ---------------------------------------------------------------------------
# Config global y función de carga
# ---------------------------------------------------------------------------

# Singleton mutable que el resto del código importa
cfg: Config = None  # type: ignore  — se inicializa con load_config()


def load_config(
    cli_overrides: Optional[dict] = None,
    yaml_path: str = "config.yaml",
) -> Config:
    """
    Carga la configuración con la precedencia definida y la valida.
    Almacena el resultado en el singleton `cfg` y lo devuelve.

    cli_overrides: dict con claves en UPPER_CASE o lowercase (e.g. {"MAX_STOPS": 0})
    """
    global cfg

    # Normalizar CLI overrides a UPPER_CASE
    cli = {}
    if cli_overrides:
        for k, v in cli_overrides.items():
            cli[k.upper()] = v

    merged = _merge(cli, _from_yaml(yaml_path), _from_env(), _DEFAULTS)

    # Coerciones de tipo post-merge
    if merged.get("CHILDREN_AGES") and isinstance(merged["CHILDREN_AGES"], str):
        merged["CHILDREN_AGES"] = [int(x) for x in merged["CHILDREN_AGES"].split(",") if x.strip()]
    if merged.get("ORIGINS") and isinstance(merged["ORIGINS"], str):
        merged["ORIGINS"] = [x.strip() for x in merged["ORIGINS"].split(",") if x.strip()]
    if merged.get("EMAIL_TO") and isinstance(merged["EMAIL_TO"], str):
        merged["EMAIL_TO"] = [x.strip() for x in merged["EMAIL_TO"].split(",") if x.strip()]
    if isinstance(merged.get("MAX_STOPS"), str):
        merged["MAX_STOPS"] = int(merged["MAX_STOPS"])
    if isinstance(merged.get("MAX_LAYOVER_HOURS"), str):
        merged["MAX_LAYOVER_HOURS"] = float(merged["MAX_LAYOVER_HOURS"])

    _validate(merged)

    smtp_user = merged.get("SMTP_USER", "")
    cfg = Config(
        ORIGINS=merged["ORIGINS"],
        DESTINATION=merged["DESTINATION"],
        DATE_OUTBOUND=merged["DATE_OUTBOUND"],
        DATE_RETURN=merged["DATE_RETURN"],
        ADULTS=int(merged["ADULTS"]),
        CHILDREN_AGES=merged["CHILDREN_AGES"],
        MAX_STOPS=int(merged["MAX_STOPS"]) if merged.get("MAX_STOPS") is not None else None,
        MAX_LAYOVER_HOURS=float(merged["MAX_LAYOVER_HOURS"]) if merged.get("MAX_LAYOVER_HOURS") is not None else None,
        CABIN_CLASS=merged["CABIN_CLASS"],
        CURRENCY=merged["CURRENCY"],
        ALERT_ABSOLUTE_DROP=float(merged["ALERT_ABSOLUTE_DROP"]),
        ALERT_PERCENT_DROP=float(merged["ALERT_PERCENT_DROP"]),
        ALERT_MODE=merged["ALERT_MODE"],
        BASELINE_MODE=merged["BASELINE_MODE"],
        ALERT_MIN_HOURS_BETWEEN=float(merged["ALERT_MIN_HOURS_BETWEEN"]),
        SMTP_HOST=merged["SMTP_HOST"],
        SMTP_PORT=int(merged["SMTP_PORT"]),
        SMTP_USE_TLS=bool(merged["SMTP_USE_TLS"]),
        SMTP_USER=smtp_user,
        SMTP_PASSWORD=merged.get("SMTP_PASSWORD", ""),
        EMAIL_FROM=merged.get("EMAIL_FROM") or smtp_user,
        EMAIL_TO=merged.get("EMAIL_TO") or [],
        EMAIL_SUBJECT_PREFIX=merged["EMAIL_SUBJECT_PREFIX"],
        DB_PATH=merged["DB_PATH"],
        CHECK_INTERVAL_MINUTES=int(merged["CHECK_INTERVAL_MINUTES"]),
        PRICE_PROVIDER=merged["PRICE_PROVIDER"],
        AMADEUS_API_KEY=merged.get("AMADEUS_API_KEY", ""),
        AMADEUS_API_SECRET=merged.get("AMADEUS_API_SECRET", ""),
        AMADEUS_ENV=merged.get("AMADEUS_ENV", "test"),
        DUFFEL_API_TOKEN=merged.get("DUFFEL_API_TOKEN", ""),
        KIWI_API_KEY=merged.get("KIWI_API_KEY", ""),
        KIWI_MAX_RESULTS=int(merged.get("KIWI_MAX_RESULTS", 50)),
        SERPAPI_API_KEY=merged.get("SERPAPI_API_KEY", ""),
        SERPAPI_MAX_OUTBOUND_TOKENS=int(merged.get("SERPAPI_MAX_OUTBOUND_TOKENS", 15)),
        SERPAPI_COMBINE_ORIGINS=bool(merged.get("SERPAPI_COMBINE_ORIGINS", True)),
        SERPAPI_SHOW_HIDDEN=bool(merged.get("SERPAPI_SHOW_HIDDEN", True)),
        SERPAPI_GL=str(merged.get("SERPAPI_GL") or "ar"),
        SERPAPI_HL=str(merged.get("SERPAPI_HL") or "es"),
        SERPAPI_BAGGAGE_ENRICH=bool(merged.get("SERPAPI_BAGGAGE_ENRICH", True)),
        SERPAPI_BAGGAGE_MAX_OFFERS=int(merged.get("SERPAPI_BAGGAGE_MAX_OFFERS", 6)),
        WATCH_PRICE_ABS_CHANGE=float(merged.get("WATCH_PRICE_ABS_CHANGE", 25.0)),
        WATCH_PRICE_PERCENT_CHANGE=float(merged.get("WATCH_PRICE_PERCENT_CHANGE", 1.0)),
        WATCH_VERIFY_MAX_OFFERS=int(merged.get("WATCH_VERIFY_MAX_OFFERS", 2)),
        AUDIT_VERIFY_MAX_OFFERS=int(merged.get("AUDIT_VERIFY_MAX_OFFERS", 4)),
        SERPAPI_AUTO_MONTHLY_CAP=int(merged.get("SERPAPI_AUTO_MONTHLY_CAP", 220)),
        SERPAPI_MANUAL_RESERVE=int(merged.get("SERPAPI_MANUAL_RESERVE", 30)),
        MARKET_AUDIT_ENABLED=bool(merged.get("MARKET_AUDIT_ENABLED", True)),
        MARKET_AUDIT_AUTO=bool(merged.get("MARKET_AUDIT_AUTO", False)),
        DESPEGAR_HEADLESS=bool(merged.get("DESPEGAR_HEADLESS", True)),
        DESPEGAR_TIMEOUT_SECONDS=int(merged.get("DESPEGAR_TIMEOUT_SECONDS", 50)),
        DESPEGAR_MAX_SCROLLS=int(merged.get("DESPEGAR_MAX_SCROLLS", 14)),
        MARKET_ALERT_REQUIRE_VALIDATION=bool(merged.get("MARKET_ALERT_REQUIRE_VALIDATION", True)),
        MONITOR_ID=str(merged.get("MONITOR_ID") or "miami-default"),
        MONITOR_NAME=str(merged.get("MONITOR_NAME") or "Vuelo monitoreado"),
    )
    logger.info(
        "Config cargada: %s→%s | %s → %s | stops≤%s | layover≤%sh | proveedor=%s",
        cfg.ORIGINS, cfg.DESTINATION,
        format_date_ui(cfg.DATE_OUTBOUND), format_date_ui(cfg.DATE_RETURN),
        cfg.MAX_STOPS, cfg.MAX_LAYOVER_HOURS, cfg.PRICE_PROVIDER,
    )
    return cfg
