"""Registro de monitores para Flight Sentinel V2.9.

Cada monitor representa una ruta Buenos Aires -> destino configurable y una
pareja de fechas base. La política de vuelo sigue protegida:
  EZE/AEP -> <destino IATA>
  directo o como máximo 1 escala por tramo
  cualquier escala debe durar <= 2 horas

``flex_days`` controla la exploración opcional de fechas alrededor de la pareja
base (0=exactas, 1=±1 día, 2=±2 días, 3=±3 días).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
import re
from typing import Iterable, List

import yaml

ALLOWED_ORIGINS = ("EZE", "AEP")
DEFAULT_DESTINATION = "MIA"
POLICY_MAX_STOPS = 1
POLICY_MAX_LAYOVER_HOURS = 2.0
MAX_FLEX_DAYS = 3


class MonitorError(ValueError):
    pass


@dataclass(frozen=True)
class MonitorProfile:
    id: str
    name: str
    date_outbound: str
    date_return: str
    enabled: bool = True
    adults: int = 2
    children_ages: tuple[int, ...] = ()
    cabin_class: str = "ECONOMY"
    currency: str = "USD"
    destination: str = DEFAULT_DESTINATION
    flex_days: int = 0

    @property
    def origins(self) -> List[str]:
        return list(ALLOWED_ORIGINS)

    @property
    def max_stops(self) -> int:
        return POLICY_MAX_STOPS

    @property
    def max_layover_hours(self) -> float:
        return POLICY_MAX_LAYOVER_HOURS

    @property
    def route_label(self) -> str:
        return f"EZE/AEP → {self.destination}"

    def validate(self) -> None:
        errors = []
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,49}", self.id):
            errors.append("ID inválido: usar 2-50 caracteres [a-z0-9_-]")
        if not self.name.strip():
            errors.append("El nombre es obligatorio")
        destination = str(self.destination or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", destination):
            errors.append("Destino inválido: usar un código IATA de 3 letras")
        if destination in ALLOWED_ORIGINS:
            errors.append("El destino no puede ser igual a uno de los aeropuertos de origen")
        try:
            dep = date.fromisoformat(self.date_outbound)
            ret = date.fromisoformat(self.date_return)
            if dep < date.today():
                errors.append(f"La ida {dep} está en el pasado")
            if ret <= dep:
                errors.append("La vuelta debe ser posterior a la ida")
        except ValueError:
            errors.append("Las fechas deben tener formato YYYY-MM-DD")
        if self.adults < 1:
            errors.append("Debe haber al menos 1 adulto")
        if any(a < 0 or a > 17 for a in self.children_ages):
            errors.append("Las edades de menores deben estar entre 0 y 17")
        if self.cabin_class not in {"ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"}:
            errors.append("Clase de cabina inválida")
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            errors.append("Moneda inválida")
        try:
            flex = int(self.flex_days)
            if flex < 0 or flex > MAX_FLEX_DAYS:
                errors.append(f"Flexibilidad inválida: usar 0-{MAX_FLEX_DAYS} días")
        except (TypeError, ValueError):
            errors.append("Flexibilidad inválida")
        if errors:
            raise MonitorError("; ".join(errors))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "destination": self.destination,
            "date_outbound": self.date_outbound,
            "date_return": self.date_return,
            "flex_days": int(self.flex_days),
            "adults": self.adults,
            "children_ages": list(self.children_ages),
            "cabin_class": self.cabin_class,
            "currency": self.currency,
        }


def _from_dict(raw: dict) -> MonitorProfile:
    p = MonitorProfile(
        id=str(raw.get("id", "")).strip().lower(),
        name=str(raw.get("name", "")).strip(),
        date_outbound=str(raw.get("date_outbound", raw.get("depart_date", ""))).strip(),
        date_return=str(raw.get("date_return", raw.get("return_date", ""))).strip(),
        enabled=bool(raw.get("enabled", True)),
        adults=int(raw.get("adults", 2)),
        children_ages=tuple(int(x) for x in (raw.get("children_ages") or [])),
        cabin_class=str(raw.get("cabin_class", "ECONOMY")).upper(),
        currency=str(raw.get("currency", "USD")).upper(),
        destination=str(raw.get("destination", DEFAULT_DESTINATION)).strip().upper(),
        flex_days=int(raw.get("flex_days", 0) or 0),
    )
    p.validate()
    return p


def load_monitors(path: str = "monitors.yaml", *, validate: bool = True) -> List[MonitorProfile]:
    p = Path(path)
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    raw_items = data.get("monitors", data if isinstance(data, list) else [])
    result = []
    ids = set()
    for raw in raw_items:
        profile = _from_dict(raw) if validate else MonitorProfile(**raw)
        if profile.id in ids:
            raise MonitorError(f"ID de monitor duplicado: {profile.id}")
        ids.add(profile.id)
        result.append(profile)
    return result


def save_monitors(monitors: Iterable[MonitorProfile], path: str = "monitors.yaml") -> None:
    items = list(monitors)
    for p in items:
        p.validate()
    Path(path).write_text(
        yaml.safe_dump({"monitors": [p.to_dict() for p in items]}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def upsert_monitor(profile: MonitorProfile, path: str = "monitors.yaml") -> None:
    profile.validate()
    monitors = load_monitors(path)
    replaced = False
    out = []
    for m in monitors:
        if m.id == profile.id:
            out.append(profile)
            replaced = True
        else:
            out.append(m)
    if not replaced:
        out.append(profile)
    save_monitors(out, path)


def delete_monitor(monitor_id: str, path: str = "monitors.yaml") -> bool:
    monitors = load_monitors(path)
    out = [m for m in monitors if m.id != monitor_id]
    if len(out) == len(monitors):
        return False
    save_monitors(out, path)
    return True


def get_monitor(monitor_id: str, path: str = "monitors.yaml") -> MonitorProfile:
    for m in load_monitors(path):
        if m.id == monitor_id:
            return m
    raise MonitorError(f"Monitor no encontrado: {monitor_id}")


def apply_monitor(base_cfg, profile: MonitorProfile):
    """Devuelve un Config independiente para el monitor seleccionado."""
    profile.validate()
    return replace(
        base_cfg,
        ORIGINS=profile.origins,
        DESTINATION=profile.destination,
        DATE_OUTBOUND=profile.date_outbound,
        DATE_RETURN=profile.date_return,
        ADULTS=profile.adults,
        CHILDREN_AGES=list(profile.children_ages),
        MAX_STOPS=profile.max_stops,
        MAX_LAYOVER_HOURS=profile.max_layover_hours,
        CABIN_CLASS=profile.cabin_class,
        CURRENCY=profile.currency,
        MONITOR_ID=profile.id,
        MONITOR_NAME=profile.name,
    )
