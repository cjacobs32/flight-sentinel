#!/usr/bin/env python3
"""Parsea comandos creados por el dashboard mediante GitHub Issues.

El dashboard no contiene tokens. Abre ``issues/new`` con un cuerpo prellenado;
el usuario confirma la creación y el workflow ``issues: opened`` ejecuta sólo
si el issue fue creado por el owner del repositorio.
"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

MAGIC = "FLIGHT_SENTINEL_COMMAND_V1"
ALLOWED = {
    "FLIGHT_ACTION",
    "FLIGHT_MONITOR_ID",
    "MONITOR_ID",
    "MONITOR_NAME",
    "MONITOR_DESTINATION",
    "MONITOR_DATE_OUTBOUND",
    "MONITOR_DATE_RETURN",
    "MONITOR_FLEX_DAYS",
    "MONITOR_ADULTS",
    "MONITOR_CHILDREN_AGES",
    "MONITOR_CABIN_CLASS",
    "MONITOR_CURRENCY",
    "MONITOR_ENABLED",
    "FLIGHT_RUN_AFTER_SAVE",
}
ACTIONS = {"watch", "audit", "flex", "despegar_audit", "save_monitor", "delete_monitor", "smtp_test", "render"}


class CommandError(ValueError):
    pass


def parse_body(body: str) -> dict[str, str]:
    text = str(body or "").replace("\r\n", "\n").strip()
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines or lines[0] != MAGIC:
        raise CommandError("Issue no reconocido como comando Flight Sentinel")

    values: dict[str, str] = {}
    for line in lines[1:]:
        if "=" not in line:
            raise CommandError(f"Línea inválida: {line[:80]}")
        key, value = line.split("=", 1)
        key = key.strip().upper()
        value = value.strip()
        if key not in ALLOWED:
            raise CommandError(f"Campo no permitido: {key}")
        if len(value) > 250:
            raise CommandError(f"Valor demasiado largo: {key}")
        values[key] = value

    action = values.get("FLIGHT_ACTION", "").lower()
    if action not in ACTIONS:
        raise CommandError(f"FLIGHT_ACTION inválido: {action or '<vacío>'}")
    values["FLIGHT_ACTION"] = action

    for key in ("FLIGHT_MONITOR_ID", "MONITOR_ID"):
        if values.get(key) and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,49}", values[key].lower()):
            raise CommandError(f"{key} inválido")
        if values.get(key):
            values[key] = values[key].lower()

    if action in {"watch", "audit", "flex", "despegar_audit", "delete_monitor"}:
        mid = values.get("FLIGHT_MONITOR_ID") or values.get("MONITOR_ID")
        if not mid:
            raise CommandError(f"{action} requiere monitor_id")
        if action == "delete_monitor":
            values["MONITOR_ID"] = mid
        else:
            values["FLIGHT_MONITOR_ID"] = mid

    if action == "save_monitor":
        required = [
            "MONITOR_ID", "MONITOR_NAME", "MONITOR_DATE_OUTBOUND", "MONITOR_DATE_RETURN",
            "MONITOR_ADULTS", "MONITOR_CABIN_CLASS", "MONITOR_ENABLED",
        ]
        missing = [k for k in required if not values.get(k)]
        if missing:
            raise CommandError("Faltan campos para save_monitor: " + ", ".join(missing))
        for key in ("MONITOR_DATE_OUTBOUND", "MONITOR_DATE_RETURN"):
            if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", values[key]):
                raise CommandError(f"{key} debe usar DD/MM/AAAA")
        if not values["MONITOR_ADULTS"].isdigit() or not 1 <= int(values["MONITOR_ADULTS"]) <= 9:
            raise CommandError("MONITOR_ADULTS inválido")
        ages = values.get("MONITOR_CHILDREN_AGES", "")
        if ages and not all(x.strip().isdigit() and 0 <= int(x.strip()) <= 17 for x in ages.split(",")):
            raise CommandError("MONITOR_CHILDREN_AGES inválido")
        destination = values.get("MONITOR_DESTINATION", "MIA").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", destination):
            raise CommandError("MONITOR_DESTINATION debe ser IATA de 3 letras")
        values["MONITOR_DESTINATION"] = destination
        flex_days = values.get("MONITOR_FLEX_DAYS", "0")
        if not flex_days.isdigit() or not 0 <= int(flex_days) <= 3:
            raise CommandError("MONITOR_FLEX_DAYS debe estar entre 0 y 3")
        values["MONITOR_FLEX_DAYS"] = flex_days
        if values["MONITOR_CABIN_CLASS"] not in {"ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"}:
            raise CommandError("MONITOR_CABIN_CLASS inválido")
        if values["MONITOR_ENABLED"].lower() not in {"true", "false"}:
            raise CommandError("MONITOR_ENABLED inválido")
        if values.get("FLIGHT_RUN_AFTER_SAVE", "") not in {"", "watch", "audit", "flex"}:
            raise CommandError("FLIGHT_RUN_AFTER_SAVE inválido")
        values.setdefault("MONITOR_CURRENCY", "USD")

    return values


def _append_github_env(path: str, values: dict[str, str]) -> None:
    out = Path(path)
    with out.open("a", encoding="utf-8") as fh:
        for key, value in values.items():
            # Los valores permitidos son de una sola línea por construcción.
            fh.write(f"{key}={value}\n")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--body-file", default="")
    args = p.parse_args()
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    else:
        body = os.getenv("FLIGHT_ISSUE_BODY", "")
    try:
        values = parse_body(body)
        env_path = os.getenv("GITHUB_ENV", "")
        if env_path:
            _append_github_env(env_path, values)
        print(f"[github-command] Acción={values['FLIGHT_ACTION']} monitor={values.get('FLIGHT_MONITOR_ID') or values.get('MONITOR_ID') or '<n/a>'}")
        return 0
    except Exception as exc:
        print(f"[github-command] ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
