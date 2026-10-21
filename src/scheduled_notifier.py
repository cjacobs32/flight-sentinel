"""Notificaciones y diagnóstico SMTP del monitoreo automático."""
from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)


def _smtp_shape(cfg) -> dict:
    return {
        "host": str(getattr(cfg, "SMTP_HOST", "") or "").strip(),
        "port": int(getattr(cfg, "SMTP_PORT", 587) or 587),
        "tls": bool(getattr(cfg, "SMTP_USE_TLS", True)),
        "user_configured": bool(str(getattr(cfg, "SMTP_USER", "") or "").strip()),
        "recipient_count": len(list(getattr(cfg, "EMAIL_TO", []) or [])),
    }


def _ehlo_code(resp):
    return resp[0] if isinstance(resp, tuple) and resp else None


def _features(server) -> dict:
    return dict(getattr(server, "esmtp_features", {}) or {})


def _has_extn(server, name: str) -> bool:
    fn = getattr(server, "has_extn", None)
    if callable(fn):
        return bool(fn(name))
    # Compatibilidad con dobles de prueba antiguos. En SMTP real smtplib siempre
    # expone has_extn().
    return name.lower() in _features(server) if _features(server) else True


def _smtp_login(server, cfg):
    return server.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD)


def _open_smtp(cfg):
    shape = _smtp_shape(cfg)
    if not shape["host"]:
        raise ValueError("SMTP_HOST vacío")
    server = smtplib.SMTP(shape["host"], shape["port"], timeout=30)
    resp = server.ehlo()
    logger.info("SMTP diagnóstico: EHLO inicial code=%s features=%s", _ehlo_code(resp), sorted(_features(server).keys()))
    if shape["tls"]:
        if not _has_extn(server, "starttls"):
            try:
                server.quit()
            except Exception:
                pass
            raise RuntimeError(
                f"El servidor SMTP {shape['host']}:{shape['port']} no anuncia STARTTLS. "
                "Revisá SMTP_HOST/SMTP_PORT/SMTP_USE_TLS."
            )
        tls_resp = server.starttls()
        logger.info("SMTP diagnóstico: STARTTLS code=%s", _ehlo_code(tls_resp))
        resp = server.ehlo()
        features = _features(server)
        logger.info(
            "SMTP diagnóstico: EHLO post-TLS code=%s features=%s auth=%s",
            _ehlo_code(resp), sorted(features.keys()), features.get("auth", "<no anunciado>"),
        )
    return server


def smtp_test(cfg, *, send_message: bool = True) -> dict:
    """Prueba SMTP explícita. No usa SerpApi ni toca el estado de vuelos."""
    shape = _smtp_shape(cfg)
    recipients = list(getattr(cfg, "EMAIL_TO", []) or [])
    logger.info(
        "SMTP test: host=%s port=%s tls=%s user_configurado=%s destinatarios=%s",
        shape["host"], shape["port"], shape["tls"], shape["user_configured"], shape["recipient_count"],
    )
    if not recipients:
        raise ValueError("EMAIL_TO vacío")
    if not shape["user_configured"]:
        raise ValueError("SMTP_USER vacío")
    if not str(getattr(cfg, "SMTP_PASSWORD", "") or ""):
        raise ValueError("SMTP_PASSWORD vacío")

    server = _open_smtp(cfg)
    try:
        if not _has_extn(server, "auth"):
            raise RuntimeError(
                f"El servidor {shape['host']}:{shape['port']} no anuncia AUTH después de EHLO/STARTTLS. "
                "Esto ocurre antes de validar la contraseña; revisá host, puerto y TLS."
            )
        try:
            _smtp_login(server, cfg)
        except smtplib.SMTPAuthenticationError as exc:
            code = getattr(exc, "smtp_code", "?")
            raise RuntimeError(
                f"El servidor SMTP rechazó la autenticación (código {code}). "
                "Si usás Gmail, verificá SMTP_USER y la App Password."
            ) from exc
        logger.info("SMTP diagnóstico: AUTH aceptado")

        if send_message:
            msg = MIMEMultipart("alternative")
            prefix = getattr(cfg, "EMAIL_SUBJECT_PREFIX", "[Flight Sentinel]") or "[Flight Sentinel]"
            msg["Subject"] = f"{prefix} Prueba SMTP"
            msg["From"] = getattr(cfg, "EMAIL_FROM", "") or getattr(cfg, "SMTP_USER", "")
            msg["To"] = ", ".join(recipients)
            msg.attach(MIMEText(
                "<b>Flight Sentinel</b><br>SMTP configurado correctamente. Esta prueba no ejecutó búsquedas de vuelos ni consumió SerpApi.",
                "html", "utf-8"
            ))
            server.sendmail(msg["From"], recipients, msg.as_string())
            logger.info("SMTP diagnóstico: mensaje de prueba enviado a %s destinatario(s)", len(recipients))
        return {**shape, "auth": True, "sent": bool(send_message)}
    finally:
        try:
            server.quit()
        except Exception:
            pass


def send_scheduled_email(cfg, *, subject: str, html: str) -> bool:
    recipients = list(getattr(cfg, "EMAIL_TO", []) or [])
    if not recipients:
        logger.info("Notificación automática omitida: EMAIL_TO vacío")
        return False
    if not getattr(cfg, "SMTP_HOST", ""):
        logger.info("Notificación automática omitida: SMTP no configurado")
        return False
    msg = MIMEMultipart("alternative")
    prefix = getattr(cfg, "EMAIL_SUBJECT_PREFIX", "[Flight Sentinel]") or "[Flight Sentinel]"
    msg["Subject"] = f"{prefix} {subject}"
    msg["From"] = getattr(cfg, "EMAIL_FROM", "") or getattr(cfg, "SMTP_USER", "")
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html", "utf-8"))
    shape = _smtp_shape(cfg)
    logger.info(
        "SMTP: host=%s port=%s tls=%s user_configurado=%s",
        shape["host"], shape["port"], shape["tls"], shape["user_configured"],
    )
    server = _open_smtp(cfg)
    try:
        if getattr(cfg, "SMTP_USER", ""):
            _smtp_login(server, cfg)
        server.sendmail(msg["From"], recipients, msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass
    logger.info("Notificación automática enviada a %s", ", ".join(recipients))
    return True
