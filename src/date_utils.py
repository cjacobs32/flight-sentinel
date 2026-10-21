"""Formato de fechas visible de Flight Sentinel.

Política de UI: DD/MM/AAAA.
Persistencia/APIs: ISO AAAA-MM-DD.
"""
from datetime import date, datetime


def format_date_ui(raw: str) -> str:
    if not raw:
        return "—"
    try:
        return date.fromisoformat(str(raw)[:10]).strftime("%d/%m/%Y")
    except Exception:
        return str(raw)


def format_datetime_ui(raw: str) -> str:
    if not raw:
        return "—"
    try:
        return datetime.fromisoformat(str(raw)).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(raw).replace("T", " ")


def parse_date_ui(raw: str) -> str:
    """Convierte DD/MM/AAAA a ISO AAAA-MM-DD."""
    raw = (raw or "").strip()
    try:
        return datetime.strptime(raw, "%d/%m/%Y").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"Fecha inválida '{raw}'. Usar DD/MM/AAAA (ej.: 10/12/2026).") from exc
