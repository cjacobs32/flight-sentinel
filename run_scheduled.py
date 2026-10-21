#!/usr/bin/env python3
"""Ejecución cloud de Flight Sentinel V2.5.

Modos:
  watch  — sensor Fli sin API key; verifica con SerpApi sólo cambios materiales.
  audit  — exploración SerpApi profunda + verificación exacta del mejor itinerario.

El estado se guarda en JSON para GitHub Pages; no requiere DB externa.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("scheduled")


def parse_args():
    p = argparse.ArgumentParser(description="Flight Sentinel V2.5 scheduled runner")
    p.add_argument("--mode", choices=["watch", "audit"], default="watch")
    p.add_argument("--state", default="cloud_state/state.json")
    p.add_argument("--monitors-file", default="monitors.yaml")
    p.add_argument("--monitor", help="Ejecutar sólo un monitor")
    p.add_argument("--config", default="config.yaml")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    from config.settings import load_config, ConfigError
    from src.monitor_registry import load_monitors, apply_monitor
    from src.cloud_state import load_state, save_state
    from src.watch_engine import run_watch_monitor
    from src.audit_engine import run_audit_monitor
    from src.date_utils import format_date_ui

    try:
        base_cfg = load_config(yaml_path=args.config)
    except ConfigError as exc:
        logger.error("Configuración inválida: %s", exc)
        return 2

    state = load_state(args.state)
    monitors = [m for m in load_monitors(args.monitors_file) if m.enabled]
    if args.monitor:
        monitors = [m for m in monitors if m.id == args.monitor]
    if not monitors:
        logger.error("No hay monitores habilitados en %s", args.monitors_file)
        return 3

    results = []
    for profile in monitors:
        cfg = apply_monitor(base_cfg, profile)
        logger.info("%s [%s] %s | %s → %s", args.mode.upper(), profile.id, profile.name,
                    format_date_ui(profile.date_outbound), format_date_ui(profile.date_return))
        if args.mode == "watch":
            event = run_watch_monitor(cfg, profile, state)
        else:
            event = run_audit_monitor(cfg, profile, state)
        results.append({"monitor_id": profile.id, "event": event})
        # Persistencia incremental para no perder el estado si falla otro monitor.
        save_state(args.state, state)

    print(json.dumps({"mode": args.mode, "results": results}, ensure_ascii=False, indent=2))
    # Un error aislado queda visible en estado/Pages pero no impide publicar el resto.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
