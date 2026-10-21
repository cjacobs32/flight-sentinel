"""
Normalización de datos para presentación (CSV, JSON, HTML).

Reglas
──────
• Las transformaciones se aplican en la capa de OUTPUT solamente.
  La DB interna sigue almacenando códigos IATA y precios tal como vienen.

Aerolíneas
  código IATA (2 chars) → nombre legible
  Si el código no está en el diccionario se devuelve el código original.

Precios
  Algunos proveedores (ej: Amadeus en ciertos endpoints) devuelven importes
  en minor units (centavos enteros). La función normalize_price() detecta
  si el valor parece estar en minor units y convierte automáticamente.
  Regla: si el valor es un entero >= 10_000, se asume minor units.
  De lo contrario se asume que ya está en unidades decimales.

  Ejemplos:
    213402  → 2134.02    (minor units detectado)
    2134.02 → 2134.02    (ya en decimal, sin cambio)
    999     → 999.00     (entero pequeño → decimal, sin división)
"""
from __future__ import annotations

from typing import Union

# ---------------------------------------------------------------------------
# Catálogo de aerolíneas (IATA 2-char → descripción)
# ---------------------------------------------------------------------------

import json
from pathlib import Path

# Los nombres que entrega el proveedor en cada segmento son la fuente primaria.
# Este catálogo amplio es un fallback local para códigos sin nombre. El archivo
# data/airlines_iata.json deriva de OpenFlights y contiene ~1.000 códigos activos.
_AIRLINE_CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "airlines_iata.json"

def _load_airline_catalog() -> dict[str, dict]:
    try:
        with _AIRLINE_CATALOG_PATH.open(encoding="utf-8") as fh:
            raw = json.load(fh)
        return {str(k).upper(): dict(v) for k, v in raw.items()}
    except Exception:
        return {}

AIRLINE_CATALOG: dict[str, dict] = _load_airline_catalog()

# Overrides mínimos para compañías relevantes/currentes. También sirven si el
# catálogo externo falta accidentalmente en una instalación.
AIRLINE_NAMES: dict[str, str] = {
    "AA": "American Airlines",
    "AR": "Aerolíneas Argentinas",
    "AV": "Avianca",
    "CM": "Copa Airlines",
    "DL": "Delta Air Lines",
    "LA": "LATAM Airlines",
    "G3": "GOL Linhas Aéreas",
    "UA": "United Airlines",
    "B6": "JetBlue Airways",
    "AM": "Aeroméxico",
    "UX": "Air Europa",
    "IB": "Iberia",
    "BA": "British Airways",
    "AF": "Air France",
    "KL": "KLM",
    "LH": "Lufthansa",
    "TP": "TAP Air Portugal",
    "EK": "Emirates",
    "QR": "Qatar Airways",
    "TK": "Turkish Airlines",
    "EY": "Etihad Airways",
    "W1": "World Ticket",
    "X1": "Hahn Air Systems",
    "HR": "Hahn Air",
    "GF": "Gulf Air",
}

# Códigos a excluir del monitoreo (GDS / ticketing carriers / no deseados)
EXCLUDED_AIRLINES: set[str] = {"W1", "X1", "HR", "GF"}

def has_excluded_airline(codes: list[str]) -> bool:
    """True si la lista contiene alguna aerolínea excluida."""
    if not codes:
        return False
    return any((c or "").strip().upper() in EXCLUDED_AIRLINES for c in codes)

def airline_info(code: str) -> dict:
    """Devuelve descripción local de un código IATA, si existe."""
    c = (code or "").strip().upper()
    rec = dict(AIRLINE_CATALOG.get(c) or {})
    if c in AIRLINE_NAMES:
        rec["name"] = AIRLINE_NAMES[c]
    rec.setdefault("name", c or "Aerolínea no informada")
    # Algunos registros históricos de OpenFlights tienen como nombre una mera
    # variación de mayúsculas del código (p.ej. ZZ -> Zz); no aporta descripción.
    if str(rec.get("name") or "").strip().upper() == c and c not in AIRLINE_NAMES:
        rec["name"] = c
    rec.setdefault("country", "")
    rec.setdefault("icao", "")
    rec["iata"] = c
    return rec

def normalize_airline(code: str) -> str:
    """Nombre legible para un código IATA; el código queda como fallback final."""
    return airline_info(code).get("name") or code

def normalize_airlines(codes: list[str]) -> list[str]:
    return [normalize_airline(c) for c in codes]

# ---------------------------------------------------------------------------
# Precios
# ---------------------------------------------------------------------------

def normalize_price(value: Union[int, float]) -> float:
    """
    Convierte un precio a unidades decimales con 2 decimales.

    Heurística de detección de minor units:
      - Si value es un entero (o float sin parte decimal) Y value >= 10_000
        → se divide por 100 (minor units).
      - En cualquier otro caso se redondea a 2 decimales sin dividir.

    Esto cubre:
      213402   → 2134.02
      213402.0 → 2134.02
      2134.02  → 2134.02
      999      → 999.00
      99.9     → 99.90

    >>> normalize_price(213402)
    2134.02
    >>> normalize_price(213402.0)
    2134.02
    >>> normalize_price(2134.02)
    2134.02
    >>> normalize_price(999)
    999.0
    """
    is_integer_value = isinstance(value, int) or (
        isinstance(value, float) and value == int(value)
    )
    if is_integer_value and value >= 10_000:
        return round(value / 100, 2)
    return round(float(value), 2)


def fmt_price_html(value: Union[int, float], currency: str) -> str:
    """
    Formatea un precio para HTML: "USD 2,134.02"
    Aplica normalize_price() internamente.

    >>> fmt_price_html(213402, "USD")
    'USD 2,134.02'
    >>> fmt_price_html(2134.02, "USD")
    'USD 2,134.02'
    """
    return f"{currency} {normalize_price(value):,.2f}"


def fmt_price_csv(value: Union[int, float]) -> str:
    """
    Formatea un precio para CSV: "2134.02"
    Sin símbolo de moneda, sin separador de miles.

    >>> fmt_price_csv(213402)
    '2134.02'
    >>> fmt_price_csv(2134.02)
    '2134.02'
    """
    return f"{normalize_price(value):.2f}"


def fmt_price_json(value: Union[int, float]) -> float:
    """
    Devuelve un float con 2 decimales para serialización JSON.

    >>> fmt_price_json(213402)
    2134.02
    >>> fmt_price_json(2134.02)
    2134.02
    """
    return normalize_price(value)