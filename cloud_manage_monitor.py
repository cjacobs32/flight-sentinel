#!/usr/bin/env python3
"""Gestiona ``monitors.yaml`` desde GitHub Actions.

La página estática no guarda secretos ni escribe directamente el repositorio.
El dashboard crea un comando prellenado mediante GitHub Issues; el workflow
valida sus campos y este script aplica la operación dentro de Actions.
"""
from __future__ import annotations

import argparse
import os

from src.date_utils import parse_date_ui
from src.monitor_registry import MonitorProfile, delete_monitor, load_monitors, upsert_monitor


class CloudMonitorActionError(ValueError):
    pass


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def _truthy(raw: str) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _date_to_iso(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise CloudMonitorActionError("Las fechas son obligatorias")
    if "/" in raw:
        return parse_date_ui(raw)
    return raw


def profile_from_values(values: dict[str, str], *, existing_id: str | None = None) -> MonitorProfile:
    raw_id = (existing_id or values.get("MONITOR_ID") or "").strip().lower()
    ages_raw = str(values.get("MONITOR_CHILDREN_AGES") or "").strip()
    try:
        ages = tuple(int(x.strip()) for x in ages_raw.split(",") if x.strip())
        adults = int(values.get("MONITOR_ADULTS") or 2)
        flex_days = int(values.get("MONITOR_FLEX_DAYS") or 0)
    except ValueError as exc:
        raise CloudMonitorActionError("Adultos, edades de menores y flexibilidad deben ser números enteros") from exc
    profile = MonitorProfile(
        id=raw_id,
        name=str(values.get("MONITOR_NAME") or "").strip(),
        date_outbound=_date_to_iso(str(values.get("MONITOR_DATE_OUTBOUND") or "")),
        date_return=_date_to_iso(str(values.get("MONITOR_DATE_RETURN") or "")),
        enabled=_truthy(str(values.get("MONITOR_ENABLED") or "true")),
        adults=adults,
        children_ages=ages,
        cabin_class=str(values.get("MONITOR_CABIN_CLASS") or "ECONOMY").strip().upper(),
        currency=str(values.get("MONITOR_CURRENCY") or "USD").strip().upper(),
        destination=str(values.get("MONITOR_DESTINATION") or "MIA").strip().upper(),
        flex_days=flex_days,
    )
    profile.validate()
    return profile


def values_from_env() -> dict[str, str]:
    keys = [
        "MONITOR_ID", "MONITOR_NAME", "MONITOR_DESTINATION",
        "MONITOR_DATE_OUTBOUND", "MONITOR_DATE_RETURN", "MONITOR_FLEX_DAYS",
        "MONITOR_ADULTS", "MONITOR_CHILDREN_AGES", "MONITOR_CABIN_CLASS",
        "MONITOR_CURRENCY", "MONITOR_ENABLED",
    ]
    return {k: _env(k) for k in keys}


def save_from_env(path: str = "monitors.yaml") -> MonitorProfile:
    values = values_from_env()
    profile = profile_from_values(values)
    upsert_monitor(profile, path)
    return profile


def delete_from_env(path: str = "monitors.yaml") -> str:
    monitor_id = _env("MONITOR_ID").lower()
    if not monitor_id:
        raise CloudMonitorActionError("MONITOR_ID es obligatorio para eliminar")
    if not delete_monitor(monitor_id, path):
        raise CloudMonitorActionError(f"Monitor no encontrado: {monitor_id}")
    return monitor_id


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["save", "delete"])
    p.add_argument("--file", default="monitors.yaml")
    args = p.parse_args()
    try:
        if args.action == "save":
            profile = save_from_env(args.file)
            print(f"[config] Monitor guardado: {profile.id} · {profile.name} · {profile.route_label} · flex ±{profile.flex_days}")
        else:
            monitor_id = delete_from_env(args.file)
            print(f"[config] Monitor eliminado: {monitor_id}")
        print(f"[config] {len(load_monitors(args.file))} monitor(es) configurados.")
        return 0
    except Exception as exc:
        print(f"[config] ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
