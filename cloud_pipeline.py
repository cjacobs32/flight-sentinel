#!/usr/bin/env python3
"""Orquestador cloud V2.9 para GitHub Actions.

Acciones (FLIGHT_ACTION):
- watch / audit / flex
- save_monitor / delete_monitor / smtp_test

Los schedules usan FLIGHT_MODE=watch|audit. El workflow genera GitHub Pages y
persiste configuración/estado en el propio repositorio sin exponer tokens.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("cloud-pipeline")


def _truthy(raw: str) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _run_mode(base_cfg, state: dict, profiles: list, mode: str, monitor_id: str = "") -> list[dict]:
    from src.monitor_registry import apply_monitor
    from src.watch_engine import run_watch_monitor
    from src.audit_engine import run_audit_monitor
    from src.flex_engine import run_flex_monitor
    from src.date_utils import format_date_ui

    active = [m for m in profiles if m.enabled]
    if monitor_id:
        active = [m for m in active if m.id == monitor_id]
    if not active:
        if monitor_id:
            raise ValueError(f"No existe un monitor activo con ID '{monitor_id}'")
        raise ValueError("No hay monitores habilitados")

    results = []
    for profile in active:
        cfg = apply_monitor(base_cfg, profile)
        logger.info(
            "%s [%s] %s | %s → %s", mode.upper(), profile.id, profile.name,
            format_date_ui(profile.date_outbound), format_date_ui(profile.date_return),
        )
        if mode == "watch":
            event = run_watch_monitor(cfg, profile, state)
        elif mode == "flex":
            event = run_flex_monitor(cfg, profile, state)
        else:
            event = run_audit_monitor(cfg, profile, state)
        results.append({"monitor_id": profile.id, "event": event})
    return results


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state", default=os.getenv("FLIGHT_STATE_PATH", "cloud_state/state.json"))
    p.add_argument("--monitors-file", default="monitors.yaml")
    p.add_argument("--config", default="config.yaml")
    args = p.parse_args()

    from config.settings import load_config, ConfigError
    from src.cloud_state import load_state, save_state, sync_monitors
    from src.monitor_registry import load_monitors
    from cloud_manage_monitor import save_from_env, delete_from_env
    from cloud_git_sync import push_config

    try:
        base_cfg = load_config(yaml_path=args.config)
    except ConfigError as exc:
        logger.error("Configuración inválida: %s", exc)
        return 2

    state = load_state(args.state)
    action = str(os.getenv("FLIGHT_ACTION", "") or "").strip().lower()
    default_mode = str(os.getenv("FLIGHT_MODE", "watch") or "watch").strip().lower()
    monitor_id = str(os.getenv("FLIGHT_MONITOR_ID", "") or "").strip().lower()
    pipeline_source = str(os.getenv("GITHUB_EVENT_NAME", "") or "").strip().lower()
    logger.info(
        "Entrada pipeline: source=%s | FLIGHT_ACTION=%s | FLIGHT_MODE=%s | monitor=%s",
        pipeline_source or "<local>", action or "<vacío>", default_mode or "<vacío>", monitor_id or "<todos>",
    )
    results: list[dict] = []
    summary: dict = {"action": action or default_mode, "results": results}

    try:
        # Un workflow_dispatch manual sin acción no debe degradar silenciosamente a WATCH.
        if pipeline_source == "workflow_dispatch" and not action:
            raise ValueError(
                "Ejecución manual sin FLIGHT_ACTION. Elegí watch, audit, flex, smtp_test o render desde GitHub Actions."
            )

        if action == "render":
            profiles = load_monitors(args.monitors_file)
            sync_monitors(state, profiles, prune=True)
            summary["render_only"] = True

        elif action == "smtp_test":
            from src.scheduled_notifier import smtp_test
            summary["smtp"] = smtp_test(base_cfg, send_message=True)

        elif action == "save_monitor":
            profile = save_from_env(args.monitors_file)
            # La configuración se guarda en el repo ANTES de ejecutar opcionalmente
            # el monitor. Un fallo de búsqueda no debe perder cambios de usuario.
            push_config(args.monitors_file)
            profiles = load_monitors(args.monitors_file)
            sync_monitors(state, profiles, prune=True)
            run_after = str(os.getenv("FLIGHT_RUN_AFTER_SAVE", "") or "").strip().lower()
            if run_after in {"watch", "audit", "flex"} and profile.enabled:
                results.extend(_run_mode(base_cfg, state, profiles, run_after, profile.id))
            summary["monitor_saved"] = profile.to_dict()

        elif action == "delete_monitor":
            deleted_id = delete_from_env(args.monitors_file)
            push_config(args.monitors_file)
            profiles = load_monitors(args.monitors_file)
            sync_monitors(state, profiles, prune=True)
            summary["monitor_deleted"] = deleted_id

        else:
            mode = action if action in {"watch", "audit", "flex"} else default_mode
            if mode not in {"watch", "audit", "flex"}:
                raise ValueError(f"FLIGHT_ACTION/FLIGHT_MODE inválido: {mode}")
            profiles = load_monitors(args.monitors_file)
            sync_monitors(state, profiles, prune=True)
            results.extend(_run_mode(base_cfg, state, profiles, mode, monitor_id))

        save_state(args.state, state)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # Se conserva el estado previo. En acciones de consulta los motores ya
        # registran errores por monitor; aquí quedan fallas de orquestación/config.
        logger.exception("Pipeline cloud falló: %s", exc)
        try:
            save_state(args.state, state)
        except Exception:
            pass
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
