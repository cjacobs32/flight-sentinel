"""
Lógica de evaluación de alertas: cuándo corresponde notificar.
"""
import logging
from datetime import datetime, timedelta, UTC
from typing import Optional

logger = logging.getLogger(__name__)


def should_alert(
    *,
    current_price: float,
    baseline_price: float,
    last_alerted_price: Optional[float],
    last_alert_time: Optional[datetime],
    absolute_drop: float,
    percent_drop: float,
    mode: str,                      # "either" | "absolute" | "percent" | "both"
    min_hours_between: float,
    currency: str = "USD",
) -> tuple[bool, str]:
    """
    Devuelve (True, motivo) si se debe enviar alerta, (False, motivo) si no.
    """
    now = datetime.now(UTC).replace(tzinfo=None)

    # 1. No alertar si el precio actual >= último alertado
    if last_alerted_price is not None and current_price >= last_alerted_price:
        return False, (
            f"precio actual ({current_price}) >= último alertado ({last_alerted_price})"
        )

    # 2. Ventana mínima entre alertas
    if last_alert_time is not None:
        elapsed = (now - last_alert_time).total_seconds() / 3600
        if elapsed < min_hours_between:
            return False, (
                f"solo pasaron {elapsed:.1f}h desde la última alerta "
                f"(mínimo: {min_hours_between}h)"
            )

    # 3. Calcular caída vs baseline
    drop_abs = baseline_price - current_price
    drop_pct = (drop_abs / baseline_price * 100) if baseline_price > 0 else 0

    meets_abs = drop_abs >= absolute_drop
    meets_pct = drop_pct >= percent_drop

    if mode == "either":
        triggered = meets_abs or meets_pct
    elif mode == "absolute":
        triggered = meets_abs
    elif mode == "percent":
        triggered = meets_pct
    elif mode == "both":
        triggered = meets_abs and meets_pct
    else:
        raise ValueError(f"Modo de alerta desconocido: {mode}")

    if triggered:
        reason = (
            f"bajó {currency} {drop_abs:.2f} ({drop_pct:.1f}%) vs baseline {currency} {baseline_price:.2f}"
        )
        return True, reason

    return False, (
        f"caída de {currency} {drop_abs:.2f} ({drop_pct:.1f}%) no supera umbral "
        f"(abs≥{currency} {absolute_drop} / pct≥{percent_drop}%)"
    )
