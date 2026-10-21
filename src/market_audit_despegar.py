"""Auditor independiente de mercado usando Despegar + Playwright.

Objetivo
--------
No usa una API paga. Abre la búsqueda pública de Despegar en un navegador
local, carga los resultados visibles y extrae inventario de aerolíneas / tipo
de escala como control independiente de Google Flights.

Importante
----------
* No intenta evadir CAPTCHA ni bloqueos del sitio.
* Si Despegar bloquea la automatización, el auditor devuelve AUDIT_FAILED.
* Los precios de Despegar se guardan sólo como evidencia orientativa; Flight
  Sentinel NO los mezcla con el ranking de precios de Google porque las tarifas
  y condiciones pueden no ser equivalentes.
* La conciliación es conservadora: una discrepancia detectada baja la confianza;
  una coincidencia NO se presenta como "mercado completo".
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, UTC
import json
import logging
import os
from pathlib import Path
import re
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


class DespegarBlockedError(RuntimeError):
    """El sitio bloqueó explícitamente la navegación automatizada."""


def _blocked_reason(body_text: str) -> str:
    """Detecta mensajes explícitos de protección anti-automatización."""
    text = re.sub(r"\s+", " ", body_text or "").strip()
    patterns = [
        r"verification required",
        r"access is temporarily restricted",
        r"slide right to secure your access",
        r"automated \(bot\) activity",
        r"unusual activity from your device or network",
        r"captcha",
        r"verifica que eres humano",
        r"verify you are human",
        r"access denied",
    ]
    if any(re.search(pat, text, re.I) for pat in patterns):
        return (
            "Despegar bloqueó el navegador automatizado con una verificación anti-bot. "
            "Flight Sentinel no intenta evadir CAPTCHA ni protecciones del sitio; "
            "la corrida Google se conserva y la cobertura de mercado queda NO VERIFICADA."
        )
    return ""

def _page_block_reason(page) -> str:
    """Revisa bloqueo usando cuerpo, título y URL; tolera páginas en transición."""
    parts = []
    try:
        parts.append(page.locator("body").inner_text(timeout=5000) or "")
    except Exception:
        pass
    try:
        parts.append(page.title() or "")
    except Exception:
        pass
    try:
        parts.append(page.url or "")
    except Exception:
        pass
    return _blocked_reason(" ".join(parts)[:24000])

# Nombres que hoy aparecen de forma habitual en BUE→destino y/o conexiones útiles.
# La detección también conserva nombres desconocidos cuando aparecen en alt/title.
_AIRLINES = {
    "American Airlines": "AA",
    "Aerolíneas Argentinas": "AR",
    "Aerolineas Argentinas": "AR",
    "LATAM": "LA",
    "LATAM Airlines": "LA",
    "LATAM Airlines Group": "LA",
    "Copa Airlines": "CM",
    "Copa": "CM",
    "Avianca": "AV",
    "Delta Air Lines": "DL",
    "Delta": "DL",
    "United Airlines": "UA",
    "United": "UA",
    "Arajet": "DM",
    "Gol": "G3",
    "GOL": "G3",
    "Air Canada": "AC",
    "Aeromexico": "AM",
    "Aeroméxico": "AM",
    "Sky Airline": "H2",
}


@dataclass
class DespegarCard:
    airline_name: str
    airline_code: str
    stops: Optional[int]
    stop_label: str
    currency: Optional[str]
    price_per_person: Optional[float]
    layover_minutes: Optional[int]
    policy_eligible: Optional[bool]
    raw_text: str

    def to_dict(self) -> dict:
        d = asdict(self)
        # Evitar inflar la DB/evidencia con textos enormes.
        d["raw_text"] = self.raw_text[:1200]
        return d


@dataclass
class DespegarAudit:
    status: str
    url: str
    cards_seen: int
    direct_cards: int
    one_stop_cards: int
    two_plus_cards: int
    airlines: list[str]
    direct_airlines: list[str]
    one_stop_airlines: list[str]
    eligible_airlines: list[str]
    unverified_one_stop_airlines: list[str]
    cheapest_per_person: dict[str, float]
    cards: list[dict]
    warning: str = ""
    evidence_json: str = ""
    evidence_screenshot: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def build_despegar_url(*, date_out: str, date_ret: str, adults: int, children: int,
                       currency: str = "USD", destination: str = "MIA") -> str:
    """URL pública de resultados de Despegar para Buenos Aires (BUE) → destino."""
    # Formato público observado por Despegar: .../<adultos>/<niños>/<infantes>
    # Para el auditor sólo necesitamos inventario; no se mezcla su precio con Google.
    return (
        "https://www.despegar.com.ar/shop/flights/results/roundtrip/"
        f"BUE/{destination.upper()}/{date_out}/{date_ret}/{int(adults)}/{int(children)}/0"
        f"?currency={currency.upper()}"
    )


def _money_number(raw: str) -> Optional[float]:
    """Convierte 1.034 / 1,034 / 1.034,50 / 1,034.50 a float de forma tolerante."""
    s = (raw or "").strip().replace(" ", "")
    if not s:
        return None
    if "." in s and "," in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        tail = s.rsplit(",", 1)[-1]
        s = s.replace(",", ".") if len(tail) <= 2 else s.replace(",", "")
    elif "." in s:
        # Despegar AR suele usar punto como separador de miles: US$ 1.034
        tail = s.rsplit(".", 1)[-1]
        if len(tail) == 3:
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_despegar_card(text: str, meta_text: str = "", destination: str = "MIA") -> Optional[DespegarCard]:
    """Parsea texto de una tarjeta visible de resultados.

    Se mantiene deliberadamente tolerante a cambios de clases CSS. El requisito
    mínimo es reconocer ruta BUE/EZE/AEP→destino, tipo de escala y aerolínea.
    """
    raw = re.sub(r"\s+", " ", (text or "")).strip()
    meta = re.sub(r"\s+", " ", (meta_text or "")).strip()
    haystack = f"{raw} {meta}".strip()
    up = haystack.upper()
    destination = (destination or "MIA").strip().upper()
    if destination not in up or not ("EZE" in up or "AEP" in up or "BUE" in up):
        return None

    stop_label = "NO IDENTIFICADO"
    stops: Optional[int] = None
    if re.search(r"\bDIRECTO\b|\bDIRECT\b|\bSIN ESCALA", up):
        stops, stop_label = 0, "DIRECTO"
    elif re.search(r"\b1\s*ESCALA\b|\bUNA\s+ESCALA\b", up):
        stops, stop_label = 1, "1 ESCALA"
    elif re.search(r"\b2\s*(?:O\s+M[AÁ]S\s+)?ESCALAS?\b|\bM[AÁ]S DE 1 ESCALA", up):
        stops, stop_label = 2, "2+ ESCALAS"

    # Si no conocemos el número de escalas, no sirve para reconciliar nuestra política.
    if stops is None:
        return None

    airline_name = ""
    airline_code = ""
    # Prioridad al texto de la tarjeta/alt/title para evitar inferencias por iconos.
    for name, code in sorted(_AIRLINES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if name.casefold() in haystack.casefold():
            airline_name, airline_code = name, code
            break

    # Fallback: buscar patrón textual "Aerolínea ..." de metadatos accesibles.
    if not airline_name:
        m = re.search(r"(?:airline|aerol[ií]nea)[:\s-]+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ .&-]{3,45})", haystack, re.I)
        if m:
            airline_name = m.group(1).strip(" -")
            airline_code = "??"

    if not airline_name:
        # Sin aerolínea no aporta a la conciliación de mercado.
        return None

    currency = None
    price = None
    patterns = [
        ("USD", r"(?:US\$|USD)\s*([0-9][0-9.,]*)"),
        ("ARS", r"(?:ARS|AR\$)\s*([0-9][0-9.,]*)"),
    ]
    for cur, pat in patterns:
        m = re.search(pat, haystack, re.I)
        if m:
            currency = cur
            price = _money_number(m.group(1))
            break

    layover_minutes: Optional[int] = None
    policy_eligible: Optional[bool]
    if stops == 0:
        policy_eligible = True
    elif stops == 1:
        # Sólo declaramos una conexión como elegible si su duración está explícita
        # en el texto de la tarjeta/detalle. Nunca inferimos que "1 escala" cumple 2h.
        m = re.search(
            r"(?:escala|conexi[oó]n|espera)[^0-9]{0,40}(?:(\d{1,2})\s*h(?:oras?)?)?\s*(\d{1,2})?\s*m(?:in)?",
            haystack, re.I,
        )
        if m and (m.group(1) or m.group(2)):
            layover_minutes = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
            policy_eligible = layover_minutes <= 120
        else:
            policy_eligible = None
    else:
        policy_eligible = False

    return DespegarCard(
        airline_name=airline_name,
        airline_code=airline_code,
        stops=stops,
        stop_label=stop_label,
        currency=currency,
        price_per_person=price,
        layover_minutes=layover_minutes,
        policy_eligible=policy_eligible,
        raw_text=raw,
    )


def _dedupe_cards(cards: Iterable[DespegarCard]) -> list[DespegarCard]:
    result: list[DespegarCard] = []
    seen = set()
    for c in cards:
        key = (c.airline_code, c.stops, c.currency, c.price_per_person, c.raw_text[:250])
        if key in seen:
            continue
        seen.add(key)
        result.append(c)
    return result


def _chrome_launch_kwargs() -> dict:
    """Usa Chrome instalado en Windows; evita descargar un navegador adicional."""
    if os.name == "nt":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        for p in candidates:
            if str(p) and p.exists():
                return {"executable_path": str(p)}
        return {"channel": "chrome"}
    # En otros SO primero probar Chromium del sistema si existe.
    for p in ("/usr/bin/chromium", "/usr/bin/google-chrome"):
        if Path(p).exists():
            return {"executable_path": p}
    return {}


def audit_despegar(*, monitor_id: str, date_out: str, date_ret: str, adults: int,
                   children: int, currency: str = "USD", destination: str = "MIA", headless: bool = True,
                   timeout_seconds: int = 50, max_scrolls: int = 14,
                   evidence_dir: str = "evidence") -> dict:
    """Ejecuta el auditor Despegar y devuelve un dict serializable.

    No lanza excepciones por errores operativos: retorna status=AUDIT_FAILED para
    que una falla del auditor nunca convierta una cotización Google real en falsa.
    """
    url = build_despegar_url(
        date_out=date_out, date_ret=date_ret, adults=adults,
        children=children, currency=currency, destination=destination,
    )
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except Exception as exc:
        return DespegarAudit(
            status="AUDITOR_NOT_INSTALLED", url=url, cards_seen=0, direct_cards=0,
            one_stop_cards=0, two_plus_cards=0, airlines=[], direct_airlines=[],
            one_stop_airlines=[], eligible_airlines=[], unverified_one_stop_airlines=[], cheapest_per_person={}, cards=[],
            warning=f"Playwright no está instalado: {exc}",
        ).to_dict()

    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    safe_monitor = re.sub(r"[^A-Za-z0-9_-]+", "_", monitor_id or "monitor")
    evidence_path = Path(evidence_dir)
    evidence_path.mkdir(parents=True, exist_ok=True)
    json_file = evidence_path / f"despegar_{safe_monitor}_{stamp}.json"
    screenshot_file = evidence_path / f"despegar_{safe_monitor}_{stamp}.png"

    cards: list[DespegarCard] = []
    warning = ""
    screenshot_saved = ""
    try:
        with sync_playwright() as p:
            launch_kwargs = _chrome_launch_kwargs()
            browser = p.chromium.launch(
                headless=headless,
                **launch_kwargs,
            )
            context = browser.new_context(
                locale="es-AR",
                timezone_id="America/Argentina/Buenos_Aires",
                viewport={"width": 1440, "height": 1100},
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)

            # No intentamos resolver CAPTCHA. Detectarlo y reportarlo es más confiable.
            page.wait_for_timeout(3500)
            blocked = _page_block_reason(page)
            if blocked:
                try:
                    page.screenshot(path=str(screenshot_file), full_page=False)
                    screenshot_saved = str(screenshot_file)
                except Exception:
                    pass
                raise DespegarBlockedError(blocked)

            # Esperar a que aparezca algún aeropuerto o resultado, sin depender de CSS.
            try:
                page.wait_for_function(
                    "dest => /(?:EZE|AEP|BUE)/.test(document.body.innerText) && document.body.innerText.toUpperCase().includes(dest)",
                    arg=destination.upper(),
                    timeout=min(timeout_seconds * 1000, 30000),
                )
            except PlaywrightTimeoutError:
                pass

            blocked = _page_block_reason(page)
            if blocked:
                try:
                    page.screenshot(path=str(screenshot_file), full_page=False)
                    screenshot_saved = str(screenshot_file)
                except Exception:
                    pass
                raise DespegarBlockedError(blocked)

            # Scroll incremental para disparar lazy-loading de tarjetas.
            last_height = 0
            stable = 0
            for _ in range(max(1, max_scrolls)):
                height = int(page.evaluate("document.body.scrollHeight") or 0)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(650)
                new_height = int(page.evaluate("document.body.scrollHeight") or 0)
                if new_height <= last_height and new_height == height:
                    stable += 1
                else:
                    stable = 0
                last_height = max(height, new_height)
                if stable >= 2:
                    break

            blocked = _page_block_reason(page)
            if blocked:
                try:
                    page.screenshot(path=str(screenshot_file), full_page=False)
                    screenshot_saved = str(screenshot_file)
                except Exception:
                    pass
                raise DespegarBlockedError(blocked)

            # Extraer contenedores mínimos que parezcan tarjetas. No dependemos de
            # nombres de clases, que suelen cambiar en Despegar.
            candidates = page.evaluate("""
                (dest) => {
                  const nodes = Array.from(document.querySelectorAll('article,li,div'));
                  const good = (el) => {
                    const t=(el.innerText||'').replace(/\\s+/g,' ').trim();
                    if (t.length < 45 || t.length > 1600) return false;
                    if (!/(EZE|AEP|BUE)/.test(t) || !t.toUpperCase().includes(dest)) return false;
                    if (!/(Directo|1\\s*escala|2\\s*(?:o más\\s*)?escalas?)/i.test(t)) return false;
                    return true;
                  };
                  const prelim=nodes.filter(good);
                  // Conservar el contenedor más pequeño: si un hijo también califica,
                  // el padre suele ser una lista/contenedor y genera duplicados.
                  return prelim.filter(el => !Array.from(el.children).some(ch => good(ch))).map(el => {
                    const meta=[];
                    el.querySelectorAll('img,[aria-label],[title]').forEach(n => {
                      if (n.alt) meta.push(n.alt);
                      if (n.getAttribute && n.getAttribute('aria-label')) meta.push(n.getAttribute('aria-label'));
                      if (n.title) meta.push(n.title);
                    });
                    return {text:(el.innerText||'').trim(), meta:meta.join(' | ')};
                  });
                }
            """, destination.upper()) or []

            for item in candidates:
                card = parse_despegar_card(item.get("text", ""), item.get("meta", ""), destination=destination)
                if card is not None:
                    cards.append(card)
            cards = _dedupe_cards(cards)

            # El challenge de Despegar puede aparecer varios segundos después de cargar.
            # Si no hubo tarjetas, hacemos un último control antes de clasificarlo como
            # parser inconcluso. Nunca interpretamos "0 parseadas" como "0 vuelos".
            if not cards:
                blocked = _page_block_reason(page)
                if blocked:
                    try:
                        page.screenshot(path=str(screenshot_file), full_page=False)
                        screenshot_saved = str(screenshot_file)
                    except Exception:
                        pass
                    raise DespegarBlockedError(blocked)

            # Guardar screenshot sólo si no encontramos nada o hay diversidad de fuente;
            # sirve de evidencia sin generar imágenes en cada corrida normal.
            if not cards:
                try:
                    page.screenshot(path=str(screenshot_file), full_page=False)
                    screenshot_saved = str(screenshot_file)
                except Exception:
                    pass
            browser.close()

    except Exception as exc:
        warning = str(exc)
        audit_status = "BLOCKED_BY_SITE" if isinstance(exc, DespegarBlockedError) else "AUDIT_FAILED"
        result = DespegarAudit(
            status=audit_status, url=url, cards_seen=0, direct_cards=0,
            one_stop_cards=0, two_plus_cards=0, airlines=[], direct_airlines=[],
            one_stop_airlines=[], eligible_airlines=[], unverified_one_stop_airlines=[],
            cheapest_per_person={}, cards=[], warning=warning,
            evidence_screenshot=screenshot_saved,
        )
        try:
            json_file.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            result.evidence_json = str(json_file)
        except Exception:
            pass
        logger.warning("Auditor Despegar no disponible: %s", warning)
        return result.to_dict()

    direct = [c for c in cards if c.stops == 0]
    one = [c for c in cards if c.stops == 1]
    two = [c for c in cards if c.stops is not None and c.stops >= 2]
    airlines = sorted({c.airline_code for c in cards if c.airline_code and c.airline_code != "??"})
    direct_airlines = sorted({c.airline_code for c in direct if c.airline_code and c.airline_code != "??"})
    one_airlines = sorted({c.airline_code for c in one if c.airline_code and c.airline_code != "??"})
    eligible_airlines = sorted({c.airline_code for c in cards if c.airline_code not in {"", "??"} and c.policy_eligible is True})
    unverified_one_stop_airlines = sorted({c.airline_code for c in one if c.airline_code not in {"", "??"} and c.policy_eligible is None})
    cheapest: dict[str, float] = {}
    for c in cards:
        if c.airline_code in {"", "??"} or c.price_per_person is None:
            continue
        old = cheapest.get(c.airline_code)
        if old is None or c.price_per_person < old:
            cheapest[c.airline_code] = c.price_per_person

    status = "OK" if cards else "NO_RESULTS_PARSED"
    result = DespegarAudit(
        status=status, url=url, cards_seen=len(cards), direct_cards=len(direct),
        one_stop_cards=len(one), two_plus_cards=len(two), airlines=airlines,
        direct_airlines=direct_airlines, one_stop_airlines=one_airlines,
        eligible_airlines=eligible_airlines,
        unverified_one_stop_airlines=unverified_one_stop_airlines,
        cheapest_per_person=cheapest,
        cards=[c.to_dict() for c in cards[:120]],
        warning=("Auditoría inconclusa: no se pudieron parsear tarjetas visibles de Despegar. Esto NO significa que no haya vuelos." if not cards else ""),
        evidence_screenshot=screenshot_saved,
    )
    try:
        json_file.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        result.evidence_json = str(json_file)
    except Exception as exc:
        logger.debug("No se pudo guardar evidencia Despegar: %s", exc)
    return result.to_dict()


def reconcile_market(google_offers: Iterable, despegar_audit: dict) -> dict:
    """Compara aerolíneas Google vs. Despegar de forma conservadora.

    `CONSISTENT` significa que no detectamos una discrepancia entre las dos
    fuentes observadas. Nunca significa cobertura total del mercado.
    """
    google_airlines: set[str] = set()
    for item in google_offers:
        if hasattr(item, "airlines"):
            google_airlines.update(getattr(item, "airlines") or [])
        elif isinstance(item, dict):
            off = item.get("offer") if "offer" in item else item
            google_airlines.update(off.get("airlines") or [])

    d_status = (despegar_audit or {}).get("status", "NOT_RUN")
    d_airlines = set((despegar_audit or {}).get("airlines") or [])
    d_direct = set((despegar_audit or {}).get("direct_airlines") or [])
    d_eligible = set((despegar_audit or {}).get("eligible_airlines") or d_direct)
    d_unverified_one = set((despegar_audit or {}).get("unverified_one_stop_airlines") or [])

    if d_status == "BLOCKED_BY_SITE":
        status = "AUDIT_BLOCKED"
    elif d_status in {"AUDITOR_NOT_INSTALLED", "AUDIT_FAILED"}:
        status = "AUDIT_FAILED"
    elif d_status == "NO_RESULTS_PARSED":
        status = "AUDIT_INCONCLUSIVE"
    elif d_status != "OK":
        status = "PARTIAL"
    else:
        # Sólo una oferta directa o una escala cuya duración fue verificada <=120m
        # puede constituir discrepancia fuerte contra la política del monitor.
        missing_google = d_eligible - google_airlines
        missing_google_direct = d_direct - google_airlines
        unresolved = d_unverified_one - google_airlines
        if missing_google:
            status = "DISCREPANCY"
        elif unresolved:
            status = "PARTIAL"
        else:
            status = "CONSISTENT"
        return {
            "status": status,
            "google_airlines": sorted(google_airlines),
            "despegar_airlines": sorted(d_airlines),
            "despegar_eligible_airlines": sorted(d_eligible),
            "unverified_one_stop_airlines": sorted(d_unverified_one),
            "missing_in_google": sorted(missing_google),
            "missing_direct_in_google": sorted(missing_google_direct),
            "unresolved_one_stop": sorted(unresolved),
            "only_google": sorted(google_airlines - d_airlines),
            "note": (
                "Despegar detectó una aerolínea con itinerario que cumple la política y no aparece en Google."
                if status == "DISCREPANCY" else
                ("Despegar muestra aerolíneas con 1 escala cuya duración de conexión no pudo verificarse automáticamente; no se las declara discrepancia todavía."
                 if status == "PARTIAL" else
                 "Las alternativas elegibles observadas en Despegar también aparecen en Google; esto no garantiza mercado exhaustivo.")
            ),
        }

    return {
        "status": status,
        "google_airlines": sorted(google_airlines),
        "despegar_airlines": sorted(d_airlines),
        "missing_in_google": [],
        "missing_direct_in_google": [],
        "unresolved_one_stop": [],
        "only_google": sorted(google_airlines - d_airlines),
        "note": (despegar_audit or {}).get("warning") or "Auditoría de mercado incompleta.",
    }
