"""
Notificaciones por email: cuerpo HTML completo + JSON adjunto.

Normalización en este módulo:
  - fmt_money() usa normalizer.fmt_price_html() → convierte minor units si aplica
  - offer.airlines se convierte a nombres legibles con normalize_airlines()
  - El JSON adjunto incluye la sección "presentation" de to_dict() con datos ya normalizados
"""
import json
import logging
import smtplib
from datetime import datetime, UTC
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from typing import List, Optional
from src.date_utils import format_date_ui

from src.providers import Offer
from src.normalizer import fmt_price_html, normalize_airlines

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tabla HTML de una oferta
# ---------------------------------------------------------------------------

def _offer_table(offer: Offer, rank: int, currency: str) -> str:
    """Genera una sección HTML con el detalle completo de una oferta."""

    # fmt_money normaliza minor units y formatea como "USD 2,134.02"
    def fmt_money(v):
        return fmt_price_html(v, currency)

    # Nombres legibles de aerolíneas para el encabezado de la oferta
    airline_names = ", ".join(normalize_airlines(offer.airlines))

    def seg_rows(leg, label):
        rows = ""
        for i, seg in enumerate(leg.segments):
            bag = f"{seg.baggage_pieces} pc" if seg.baggage_included else "no incluido"
            rows += f"""
            <tr style="background:{'#f9f9f9' if i % 2 == 0 else '#fff'}">
              <td style="padding:6px 10px">{seg.flight_number}</td>
              <td style="padding:6px 10px">{seg.departure_iata} → {seg.arrival_iata}</td>
              <td style="padding:6px 10px">{seg.departure_time[11:16]}</td>
              <td style="padding:6px 10px">{seg.arrival_time[11:16]}</td>
              <td style="padding:6px 10px">{seg.duration_str}</td>
              <td style="padding:6px 10px">{seg.cabin_class}</td>
              <td style="padding:6px 10px">{"✅" if seg.baggage_included else "❌"} {bag}</td>
            </tr>"""
            if i < len(leg.layovers):
                lv = leg.layovers[i]
                rows += f"""
            <tr style="background:#fff3cd">
              <td colspan="7" style="padding:4px 10px;font-size:12px;color:#856404">
                ⏱ Escala en <b>{lv.airport_iata}</b> — {lv.duration_str}
              </td>
            </tr>"""
        return rows

    th = "style=\"background:#2c3e50;color:#fff;padding:8px 10px;text-align:left\""
    lv_out = f"{offer.outbound.total_layover_minutes // 60}h {offer.outbound.total_layover_minutes % 60:02d}m" if offer.outbound.layovers else "—"
    lv_in  = f"{offer.inbound.total_layover_minutes // 60}h {offer.inbound.total_layover_minutes % 60:02d}m"  if offer.inbound.layovers  else "—"

    links = ""
    if offer.google_flights_url:
        links += f' <a href="{offer.google_flights_url}" style="color:#1a6fa8">Google Flights</a>'
    if offer.airline_url:
        links += f' · <a href="{offer.airline_url}" style="color:#1a6fa8">Aerolínea</a>'
    if offer.amadeus_deeplink:
        links += f' · <a href="{offer.amadeus_deeplink}" style="color:#1a6fa8">Amadeus API</a>'

    return f"""
<div style="border:1px solid #ddd;border-radius:6px;margin-bottom:20px;overflow:hidden">
  <div style="background:#1a6fa8;color:#fff;padding:10px 14px">
    <strong>#{rank} — {fmt_money(offer.total_price)}</strong>
    &nbsp;·&nbsp; {offer.origin} → {offer.destination}
    &nbsp;·&nbsp; {airline_names}
    &nbsp;·&nbsp; {offer.total_duration_str} total
  </div>
  <div style="padding:10px 14px;font-size:13px;display:flex;gap:20px;flex-wrap:wrap">
    <span>✈️ Ida: {offer.outbound.stops} escala(s) · {offer.outbound.duration_str} · espera: {lv_out}</span>
    <span>🔁 Vuelta: {offer.inbound.stops} escala(s) · {offer.inbound.duration_str} · espera: {lv_in}</span>
  </div>

  <div style="padding:0 14px 10px">
    <p style="font-size:12px;font-weight:bold;margin:8px 0 4px;color:#555">IDA — {offer.outbound.departure_time[:10]}</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <tr><th {th}>Vuelo</th><th {th}>Ruta</th><th {th}>Salida</th>
          <th {th}>Llegada</th><th {th}>Duración</th><th {th}>Cabina</th><th {th}>Equipaje</th></tr>
      {seg_rows(offer.outbound, "ida")}
    </table>

    <p style="font-size:12px;font-weight:bold;margin:12px 0 4px;color:#555">VUELTA — {offer.inbound.departure_time[:10]}</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <tr><th {th}>Vuelo</th><th {th}>Ruta</th><th {th}>Salida</th>
          <th {th}>Llegada</th><th {th}>Duración</th><th {th}>Cabina</th><th {th}>Equipaje</th></tr>
      {seg_rows(offer.inbound, "vuelta")}
    </table>

    <p style="font-size:12px;margin-top:8px">{links}</p>
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Cuerpo HTML completo
# ---------------------------------------------------------------------------

def _build_email_body(
    *,
    ranked_offers: List[Offer],
    origins_considered: List[str],
    baseline_price: float,
    drop_absolute: float,
    drop_percent: float,
    last_alerted_price: Optional[float],
    currency: str,
    stats: Optional[dict] = None,
) -> str:
    """Construye y devuelve el cuerpo HTML del email de alerta."""

    # Todos los importes pasan por fmt_price_html → normaliza minor units
    def fmt_money(v):
        return fmt_price_html(v, currency)

    best = ranked_offers[0]
    last_alerted_html = (
        f"<tr><td><b>Último precio alertado</b></td><td>{fmt_money(last_alerted_price)}</td></tr>"
        if last_alerted_price is not None else ""
    )

    stats_html = ""
    if stats and stats.get("count", 0) > 1:
        stats_html = f"""
<h3 style="color:#555;margin-top:24px">📊 Histórico de precios ({stats['count']} corridas)</h3>
<table border="0" cellpadding="6" style="font-size:13px">
  <tr><td>Mínimo histórico</td><td><b>{fmt_money(stats['min'])}</b></td></tr>
  <tr><td>Máximo histórico</td><td>{fmt_money(stats['max'])}</td></tr>
  <tr><td>Promedio</td><td>{fmt_money(stats['avg'])}</td></tr>
  <tr><td>Desviación estándar</td><td>{fmt_money(stats['stddev'])}</td></tr>
  <tr><td>Precio actual vs promedio</td>
      <td style="color:{'green' if stats['vs_avg_pct'] < 0 else 'red'}">
        {stats['vs_avg_pct']:+.1f}%</td></tr>
  <tr><td>Tendencia (último vs primero)</td>
      <td style="color:{'green' if stats['trend_pct'] < 0 else 'red'}">
        {stats['trend_pct']:+.1f}%</td></tr>
</table>"""

    offers_html = "".join(
        _offer_table(o, i + 1, currency)
        for i, o in enumerate(ranked_offers[:5])
    )

    return f"""
<html><body style="font-family:Arial,sans-serif;color:#222;max-width:800px">
<h2 style="color:#1a6fa8">✈️ Alerta de precio bajo — {best.origin} → {best.destination}</h2>

<table border="0" cellpadding="6" style="font-size:14px;margin-bottom:16px">
  <tr><td><b>Orígenes evaluados</b></td><td>{" / ".join(origins_considered)}</td></tr>
  <tr><td><b>Mejor origen encontrado</b></td><td><b>{best.origin}</b></td></tr>
  <tr><td><b>Destino</b></td><td>{best.destination}</td></tr>
  <tr><td><b>Fecha ida / vuelta</b></td><td>{format_date_ui(best.date_out)} → {format_date_ui(best.date_ret)}</td></tr>
  <tr><td><b>Pasajeros</b></td><td>{best.adults} adultos + {best.children} niño(s)</td></tr>
</table>

<h3 style="color:#c0392b">💰 Mejor precio: {fmt_money(best.total_price)}</h3>
<table border="0" cellpadding="6" style="font-size:14px">
  <tr><td><b>Precio baseline</b></td><td>{fmt_money(baseline_price)}</td></tr>
  <tr><td><b>Caída absoluta</b></td>
      <td style="color:green"><b>−{fmt_money(drop_absolute)}</b></td></tr>
  <tr><td><b>Caída porcentual</b></td>
      <td style="color:green"><b>−{drop_percent:.1f}%</b></td></tr>
  {last_alerted_html}
</table>

{stats_html}

<h3 style="margin-top:24px">🏆 Top ofertas encontradas (ver JSON adjunto para lista completa)</h3>
{offers_html}

<hr>
<p style="font-size:11px;color:#999">
  Generado: {datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")}<br>
  Verificá siempre el precio final en el sitio oficial antes de comprar.
</p>
</body></html>"""


# ---------------------------------------------------------------------------
# Envío
# ---------------------------------------------------------------------------

def send_alert_email(
    *,
    ranked_offers: List[Offer],
    origins_considered: List[str],
    baseline_price: float,
    drop_absolute: float,
    drop_percent: float,
    last_alerted_price: Optional[float],
    currency: str,
    stats: Optional[dict] = None,
    smtp_host: str,
    smtp_port: int,
    smtp_use_tls: bool,
    smtp_user: str,
    smtp_password: str,
    email_from: str,
    email_to: List[str],
    subject_prefix: str,
) -> None:
    if not email_to:
        raise ValueError("No hay destinatarios configurados (EMAIL_TO vacío).")

    best = ranked_offers[0]
    html_body = _build_email_body(
        ranked_offers=ranked_offers,
        origins_considered=origins_considered,
        baseline_price=baseline_price,
        drop_absolute=drop_absolute,
        drop_percent=drop_percent,
        last_alerted_price=last_alerted_price,
        currency=currency,
        stats=stats,
    )

    # Asunto: precio normalizado
    from src.normalizer import normalize_price
    subject = (
        f"{subject_prefix} ¡Precio bajo! "
        f"{best.origin}→{best.destination} "
        f"| {currency} {normalize_price(best.total_price):,.2f} "
        f"(−{drop_percent:.1f}%)"
    )

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = ", ".join(email_to)

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # JSON adjunto: to_dict() incluye sección "presentation" normalizada
    offers_json = json.dumps(
        [o.to_dict() for o in ranked_offers],
        ensure_ascii=False, indent=2,
    ).encode("utf-8")
    attachment = MIMEBase("application", "json")
    attachment.set_payload(offers_json)
    encoders.encode_base64(attachment)
    attachment.add_header(
        "Content-Disposition",
        "attachment",
        filename=f"ofertas_{best.origin}_{best.destination}_{best.date_out}.json",
    )
    msg.attach(attachment)

    logger.info("Enviando email a %s | asunto: %s", email_to, subject)

    if smtp_use_tls:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo(); server.starttls(); server.ehlo()
            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)
            server.sendmail(email_from, email_to, msg.as_string())
    else:
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)
            server.sendmail(email_from, email_to, msg.as_string())

    logger.info("Email enviado correctamente.")
