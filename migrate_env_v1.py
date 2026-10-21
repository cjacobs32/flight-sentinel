"""Migra un .env del monitor viejo a Flight Sentinel V2 sin mostrar secretos."""
from __future__ import annotations
import argparse
from pathlib import Path

FORCED = {
    "ORIGINS": "EZE,AEP",
    "DESTINATION": "MIA",
    "MAX_STOPS": "1",
    "MAX_LAYOVER_HOURS": "2",
    "EMAIL_SUBJECT_PREFIX": "[Vuelos Miami]",
    "SERPAPI_COMBINE_ORIGINS": "true",
    "MONITOR_ID": "miami-default",
    "MONITOR_NAME": "Buenos Aires a Miami",
}


def migrate(source: Path, target: Path) -> None:
    if not source.exists():
        raise SystemExit(f"No existe: {source}")
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    seen = set(); out=[]
    for line in lines:
        stripped=line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key=line.split("=",1)[0].strip()
            seen.add(key)
            if key in FORCED:
                out.append(f"{key}={FORCED[key]}")
                continue
        out.append(line)
    out.append("")
    out.append("# Flight Sentinel V2 — política Miami")
    for k,v in FORCED.items():
        if k not in seen:
            out.append(f"{k}={v}")
    target.write_text("\n".join(out).rstrip()+"\n",encoding="utf-8")
    print(f"OK: configuración migrada a {target}")
    print("Se preservaron tus credenciales sin mostrarlas. Revisá fechas en monitors.yaml.")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("old_env", help="Ruta al .env del monitor Google Flights anterior")
    ap.add_argument("--output", default=".env", help="Destino (default: .env)")
    a=ap.parse_args(); migrate(Path(a.old_env),Path(a.output))

if __name__=="__main__": main()
