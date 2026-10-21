"""Guardia de presupuesto para automatizaciones SerpApi.

La Account API de SerpApi no consume cuota. Las corridas automáticas usan esta
fuente antes de gastar búsquedas y fallan de forma cerrada si no pueden conocer
el saldo: costo automático inesperado = 0.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import urllib.parse
import urllib.request
from typing import Callable, Optional

ACCOUNT_URL = "https://serpapi.com/account.json"


@dataclass
class BudgetStatus:
    ok: bool
    requested: int
    used: int = 0
    monthly_limit: int = 0
    searches_left: int = 0
    auto_cap: int = 0
    auto_left: int = 0
    reserve: int = 0
    renewal_date: str = ""
    reason: str = ""
    source: str = "SerpApi Account API"

    def to_dict(self) -> dict:
        return asdict(self)


def fetch_account(api_key: str, *, timeout: int = 15, opener: Optional[Callable] = None) -> dict:
    if not api_key:
        raise ValueError("SERPAPI_API_KEY vacío")
    url = f"{ACCOUNT_URL}?{urllib.parse.urlencode({'api_key': api_key})}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    open_fn = opener or urllib.request.urlopen
    with open_fn(req, timeout=timeout) as resp:
        raw = resp.read()
    data = json.loads(raw.decode("utf-8"))
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data


def evaluate_account(account: dict, *, requested: int, auto_cap: int = 220, reserve: int = 30) -> BudgetStatus:
    requested = max(0, int(requested or 0))
    limit = int(account.get("searches_per_month") or 0)
    left = int(account.get("plan_searches_left") if account.get("plan_searches_left") is not None else account.get("total_searches_left") or 0)
    used_raw = account.get("this_month_usage")
    used = int(used_raw) if used_raw is not None else max(0, limit - left)
    if limit <= 0:
        # Cuenta sin plan mensual: no usamos créditos/planes ambiguos en automático.
        return BudgetStatus(
            ok=False, requested=requested, used=used, monthly_limit=limit, searches_left=left,
            auto_cap=0, auto_left=0, reserve=reserve,
            renewal_date=str(account.get("plan_renewal_date") or ""),
            reason="La cuenta no informa un límite mensual gratuito utilizable; automatización bloqueada.",
        )

    safe_cap = min(int(auto_cap), max(0, limit - int(reserve)))
    auto_left = max(0, safe_cap - used)
    ok = requested <= auto_left and requested <= left
    reason = (
        f"Presupuesto OK: {used}/{limit} usadas; techo automático {safe_cap}; quedan {auto_left} automáticas."
        if ok else
        f"Guardia de costo: se requieren {requested} búsquedas y quedan {auto_left} dentro del techo automático {safe_cap} (uso {used}/{limit})."
    )
    return BudgetStatus(
        ok=ok, requested=requested, used=used, monthly_limit=limit, searches_left=left,
        auto_cap=safe_cap, auto_left=auto_left, reserve=int(reserve),
        renewal_date=str(account.get("plan_renewal_date") or ""), reason=reason,
    )


def check_budget(api_key: str, *, requested: int, auto_cap: int = 220, reserve: int = 30,
                 timeout: int = 15, fetcher: Optional[Callable] = None) -> BudgetStatus:
    """Consulta el saldo y decide si una corrida automática puede gastar cuota.

    Si la Account API no responde, bloquea el consumo automático. Las corridas
    manuales del dashboard no pasan por esta función.
    """
    if not api_key:
        return BudgetStatus(ok=False, requested=int(requested or 0), reason="SERPAPI_API_KEY no configurada.")
    try:
        account = (fetcher or fetch_account)(api_key, timeout=timeout)
        return evaluate_account(account, requested=requested, auto_cap=auto_cap, reserve=reserve)
    except Exception as exc:
        return BudgetStatus(
            ok=False, requested=int(requested or 0), reserve=int(reserve),
            reason=f"No se pudo comprobar la cuota gratuita ({exc}); consumo automático bloqueado por seguridad.",
        )
