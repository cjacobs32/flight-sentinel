#!/usr/bin/env python3
"""Persistencia Git de Flight Sentinel Cloud V2.8 para GitHub Actions.

- ``config-push`` guarda ``monitors.yaml`` en la rama principal con el
  ``GITHUB_TOKEN`` efímero del workflow.
- ``state-pull/state-push`` mantienen ``state.json`` en la rama separada
  ``flight-state``.

El workflow debe declarar ``permissions: contents: write``. No requiere PAT.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse

STATE_BRANCH = os.getenv("FLIGHT_STATE_BRANCH", "flight-state")


class GitSyncError(RuntimeError):
    pass


def _run(args, *, cwd=None, check=True, capture=True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def _github_context() -> tuple[str, str, str, str]:
    token = os.getenv("GITHUB_TOKEN", "")
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repository = os.getenv("GITHUB_REPOSITORY", "")
    branch = (
        os.getenv("GITHUB_DEFAULT_BRANCH", "")
        or os.getenv("GITHUB_REF_NAME", "")
        or "main"
    )
    return token, server, repository, branch


def _auth_url() -> str:
    token, server, repository, _ = _github_context()
    if not token or not repository:
        raise GitSyncError(
            "GITHUB_TOKEN/GITHUB_REPOSITORY no disponibles; esta operación sólo aplica dentro de GitHub Actions"
        )
    parsed = urlparse(server)
    if parsed.scheme != "https" or not parsed.netloc:
        raise GitSyncError(f"GITHUB_SERVER_URL inválido: {server}")
    return f"https://x-access-token:{token}@{parsed.netloc}/{repository}.git"


def _safe_error(text: str) -> str:
    token = os.getenv("GITHUB_TOKEN", "")
    return str(text or "").replace(token, "***") if token else str(text or "")


def _configure_identity(cwd: str | Path) -> None:
    _run(["git", "config", "user.name", "Flight Sentinel Bot"], cwd=cwd)
    _run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=cwd)


def push_config(path: str = "monitors.yaml") -> bool:
    """Commit y push de configuración a la rama principal."""
    p = Path(path)
    if not p.exists():
        raise GitSyncError(f"No existe {path}")
    _, _, _, branch = _github_context()
    _configure_identity(Path.cwd())
    _run(["git", "add", "--", path])
    diff = _run(["git", "diff", "--cached", "--quiet"], check=False)
    if diff.returncode == 0:
        print("[git] monitors.yaml sin cambios; no se crea commit.")
        return False
    _run(["git", "commit", "-m", "Update flight monitor configuration [skip ci]"])
    url = _auth_url()
    try:
        result = _run(["git", "push", url, f"HEAD:{branch}"], check=False)
        if result.returncode != 0:
            raise GitSyncError(
                "No se pudo guardar monitors.yaml en GitHub. Revisá que el workflow tenga "
                "permissions: contents: write y que las reglas de la rama permitan al GitHub Actions bot escribir. Detalle: "
                + _safe_error(result.stdout)
            )
    finally:
        url = ""
    print(f"[git] Configuración guardada en rama {branch}.")
    return True


def pull_state(output: str) -> bool:
    """Recupera state.json de ``flight-state``; si no existe conserva estado local."""
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not os.getenv("GITHUB_REPOSITORY"):
        print("[state-git] Fuera de GitHub Actions; se conserva estado local.")
        return False
    fetch = _run(["git", "fetch", "--quiet", "--depth=1", "origin", STATE_BRANCH], check=False)
    if fetch.returncode != 0:
        print(f"[state-git] Rama {STATE_BRANCH} aún no existe; se conserva estado local si está disponible.")
        return False
    show = _run(["git", "show", "FETCH_HEAD:state.json"], check=False)
    if show.returncode != 0 or not (show.stdout or "").strip().startswith("{"):
        print(f"[state-git] {STATE_BRANCH}/state.json inválido; se conserva estado local.")
        return False
    out.write_text(show.stdout, encoding="utf-8")
    print(f"[state-git] Estado recuperado desde rama {STATE_BRANCH}.")
    return True


def push_state(input_path: str) -> bool:
    src = Path(input_path)
    if not src.exists():
        raise GitSyncError(f"No existe {input_path}")
    if not os.getenv("GITHUB_REPOSITORY"):
        print("[state-git] Fuera de GitHub Actions; no se publica rama de estado.")
        return False

    url = _auth_url()
    with tempfile.TemporaryDirectory(prefix="flight-state-") as td:
        _run(["git", "init", "-q"], cwd=td)
        _configure_identity(td)
        _run(["git", "remote", "add", "origin", url], cwd=td)
        fetch = _run(["git", "fetch", "--quiet", "--depth=1", "origin", STATE_BRANCH], cwd=td, check=False)
        if fetch.returncode == 0:
            _run(["git", "checkout", "-q", "-B", STATE_BRANCH, "FETCH_HEAD"], cwd=td)
        else:
            _run(["git", "checkout", "-q", "--orphan", STATE_BRANCH], cwd=td)
        shutil.copy2(src, Path(td) / "state.json")
        _run(["git", "add", "state.json"], cwd=td)
        diff = _run(["git", "diff", "--cached", "--quiet"], cwd=td, check=False)
        if diff.returncode == 0:
            print("[state-git] Estado sin cambios; no se crea commit.")
            return False
        _run(["git", "commit", "-q", "-m", "Update Flight Sentinel cloud state [skip ci]"], cwd=td)
        result = _run(["git", "push", "--quiet", "origin", f"HEAD:{STATE_BRANCH}"], cwd=td, check=False)
        if result.returncode != 0:
            raise GitSyncError(
                "No se pudo persistir el estado en GitHub. Revisá permissions: contents: write. Detalle: "
                + _safe_error(result.stdout)
            )
    print(f"[state-git] Estado persistido en rama {STATE_BRANCH}.")
    return True


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["config-push", "state-pull", "state-push"])
    p.add_argument("--file", default="monitors.yaml")
    p.add_argument("--state", default="cloud_state/state.json")
    args = p.parse_args()
    try:
        if args.action == "config-push":
            push_config(args.file)
        elif args.action == "state-pull":
            pull_state(args.state)
        else:
            push_state(args.state)
        return 0
    except Exception as exc:
        print(f"[git] ERROR: {_safe_error(str(exc))}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
