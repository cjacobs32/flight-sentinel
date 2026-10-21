"""
Proveedores de precios de vuelos.

Proveedores disponibles
───────────────────────
  mock     Datos sintéticos deterministas (sin API key, para pruebas)
  amadeus  Amadeus Flight Offers Search API v2  (pip install amadeus)
           Entorno test: cobertura limitada, sin costo
           Entorno prod: requiere plan pago
  duffel   Duffel API v2  (stdlib puro, sin dependencias extras)
           Sandbox gratuito: https://app.duffel.com/join
           Cobertura amplia: AR, LA, CM, AV, AA, BA y +300 más
           Token: duffel_test_XXX (sandbox) | duffel_live_XXX (producción)
  serpapi  SerpApi Google Flights (stdlib puro, sin dependencias extras)
           250 búsquedas/mes gratuitas, sin tarjeta de crédito
           Registro: https://serpapi.com/users/sign_up
           Cobertura: AR, LA, CM, AV y todo lo que Google Flights indexe

Semántica de filtros
────────────────────
  MAX_STOPS         → escalas por tramo (ida y vuelta por separado)
  MAX_LAYOVER_HOURS → tiempo máximo por escala individual

Exclusión de pseudo-aerolíneas
───────────────────────────────
  W1, X1, HR, GF (GDS/ticketing carriers) se descartan antes del ranking.

Ranking determinístico
──────────────────────
  1. precio total asc
  2. duración total (ida+vuelta) asc
  3. escalas totales asc
"""
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

logger = logging.getLogger(__name__)

from src.normalizer import has_excluded_airline
from src.date_utils import format_date_ui

# ---------------------------------------------------------------------------
# Modelo de datos
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    carrier: str
    flight_number: str
    departure_iata: str
    arrival_iata: str
    departure_time: str
    arrival_time: str
    stops: int = 0
    cabin_class: str = "ECONOMY"
    baggage_included: bool = False
    baggage_pieces: int = 0
    airline_name: str = ""

    @property
    def duration_minutes(self) -> int:
        dep = datetime.fromisoformat(self.departure_time)
        arr = datetime.fromisoformat(self.arrival_time)
        return int((arr - dep).total_seconds() / 60)

    @property
    def duration_str(self) -> str:
        m = self.duration_minutes
        return f"{m // 60}h {m % 60:02d}m"


@dataclass
class LayoverInfo:
    airport_iata: str
    duration_minutes: int

    @property
    def duration_str(self) -> str:
        m = self.duration_minutes
        return f"{m // 60}h {m % 60:02d}m"

    @property
    def duration_hours(self) -> float:
        return self.duration_minutes / 60


@dataclass
class Leg:
    segments: List[Segment] = field(default_factory=list)
    layovers: List[LayoverInfo] = field(default_factory=list)

    @property
    def stops(self) -> int:
        return max(0, len(self.segments) - 1)

    @property
    def origin(self) -> str:
        return self.segments[0].departure_iata if self.segments else ""

    @property
    def destination(self) -> str:
        return self.segments[-1].arrival_iata if self.segments else ""

    @property
    def departure_time(self) -> str:
        return self.segments[0].departure_time if self.segments else ""

    @property
    def arrival_time(self) -> str:
        return self.segments[-1].arrival_time if self.segments else ""

    @property
    def duration_minutes(self) -> int:
        if not self.segments:
            return 0
        dep = datetime.fromisoformat(self.segments[0].departure_time)
        arr = datetime.fromisoformat(self.segments[-1].arrival_time)
        return int((arr - dep).total_seconds() / 60)

    @property
    def duration_str(self) -> str:
        m = self.duration_minutes
        return f"{m // 60}h {m % 60:02d}m"

    @property
    def airlines(self) -> List[str]:
        return sorted({s.carrier for s in self.segments})

    @property
    def max_layover_hours(self) -> float:
        return max((lv.duration_hours for lv in self.layovers), default=0.0)

    @property
    def total_layover_minutes(self) -> int:
        return sum(lv.duration_minutes for lv in self.layovers)

    def to_dict(self) -> dict:
        return {
            "origin": self.origin,
            "destination": self.destination,
            "departure": self.departure_time,
            "arrival": self.arrival_time,
            "duration": self.duration_str,
            "duration_minutes": self.duration_minutes,
            "stops": self.stops,
            "airlines": self.airlines,
            "segments": [
                {
                    "flight": s.flight_number,
                    "carrier": s.carrier,
                    "from": s.departure_iata,
                    "to": s.arrival_iata,
                    "dep": s.departure_time,
                    "arr": s.arrival_time,
                    "duration": s.duration_str,
                    "duration_minutes": s.duration_minutes,
                    "cabin": s.cabin_class,
                    "baggage_included": s.baggage_included,
                    "baggage_pieces": s.baggage_pieces,
                    "airline_name": s.airline_name,
                }
                for s in self.segments
            ],
            "layovers": [
                {
                    "airport": lv.airport_iata,
                    "duration": lv.duration_str,
                    "duration_minutes": lv.duration_minutes,
                }
                for lv in self.layovers
            ],
        }


@dataclass
class Offer:
    origin: str
    destination: str
    date_out: str
    date_ret: str
    total_price: float
    currency: str
    cabin_class: str
    outbound: Leg = field(default_factory=Leg)
    inbound: Leg = field(default_factory=Leg)
    adults: int = 2
    children: int = 1
    raw: Optional[dict] = field(default=None, repr=False)
    baggage: dict = field(default_factory=dict)
    booking: dict = field(default_factory=dict)

    @property
    def exact_google_flights_url(self) -> Optional[str]:
        """Deep-link de Google Flights anclado al itinerario exacto, si fue resuelto."""
        from src.google_flights_link import is_structured_google_flights_url
        candidates = []
        if isinstance(self.raw, dict):
            candidates.extend([
                self.raw.get("exact_google_flights_url"),
                self.raw.get("google_flights_exact_url"),
            ])
        if isinstance(self.baggage, dict):
            candidates.append(self.baggage.get("google_flights_exact_url"))
        for url in candidates:
            if is_structured_google_flights_url(url):
                return str(url)
        return None

    @property
    def google_flights_url(self) -> str:
        from src.google_flights_link import normalize_google_flights_url
        if self.exact_google_flights_url:
            return self.exact_google_flights_url
        raw_url = None
        if isinstance(self.raw, dict):
            raw_url = self.raw.get("google_flights_url") or self.raw.get("_google_flights_url")
        return normalize_google_flights_url({
            "origin": self.origin,
            "destination": self.destination,
            "date_out": self.date_out,
            "date_ret": self.date_ret,
            "adults": self.adults,
            "children": self.children,
            "cabin_class": self.cabin_class,
            "currency": self.currency,
        }, raw_url)

    @property
    def amadeus_deeplink(self) -> str:
        return (
            f"https://test.api.amadeus.com/v2/shopping/flight-offers"
            f"?originLocationCode={self.origin}"
            f"&destinationLocationCode={self.destination}"
            f"&departureDate={self.date_out}"
            f"&returnDate={self.date_ret}"
            f"&adults={self.adults}"
            f"&children={self.children}"
            f"&travelClass={self.cabin_class}"
            f"&currencyCode={self.currency}"
        )

    @property
    def airline_url(self) -> Optional[str]:
        AIRLINE_URLS = {
            "AA": "https://www.aa.com",
            "LA": "https://www.latamairlines.com",
            "AR": "https://www.aerolineas.com.ar",
            "IB": "https://www.iberia.com",
            "CM": "https://www.copaair.com",
            "UA": "https://www.united.com",
            "DL": "https://www.delta.com",
            "AM": "https://www.aeromexico.com",
            "AV": "https://www.avianca.com",
            "G3": "https://www.voegol.com.br",
        }
        airlines = self.outbound.airlines
        return AIRLINE_URLS.get(airlines[0]) if airlines else None

    @property
    def stops_outbound(self) -> int:
        return self.outbound.stops

    @property
    def stops_inbound(self) -> int:
        return self.inbound.stops

    @property
    def total_stops(self) -> int:
        return self.stops_outbound + self.stops_inbound

    @property
    def total_duration_minutes(self) -> int:
        return self.outbound.duration_minutes + self.inbound.duration_minutes

    @property
    def total_duration_str(self) -> str:
        m = self.total_duration_minutes
        return f"{m // 60}h {m % 60:02d}m"

    @property
    def total_layover_hours(self) -> float:
        return (self.outbound.total_layover_minutes + self.inbound.total_layover_minutes) / 60

    @property
    def airlines(self) -> List[str]:
        return sorted({s.carrier for s in self.outbound.segments + self.inbound.segments})

    def max_layover_hours(self) -> float:
        return max(self.outbound.max_layover_hours, self.inbound.max_layover_hours)

    # ── Itinerario legible e identificable ────────────────────────────────

    def _leg_str(self, leg) -> str:
        """
        Representación compacta de una pierna de vuelo.
        Formato: "EZE→PTY(CM278) ✦90m PTY→MIA(CM419)"
        ✦ = escala con su duración en minutos.
        """
        parts = []
        for i, seg in enumerate(leg.segments):
            parts.append(f"{seg.departure_iata}→{seg.arrival_iata}({seg.flight_number})")
            if i < len(leg.layovers):
                lv = leg.layovers[i]
                parts.append(f"✦{lv.duration_minutes}m")
        return " ".join(parts)

    @property
    def itinerary_str(self) -> str:
        """
        Representación humana completa del itinerario, apta para logs y CSV.
        Ejemplo:
          IDA: EZE→PTY(CM278) ✦90m PTY→MIA(CM419)
          VTA: MIA→PTY(CM420) ✦105m PTY→EZE(CM279)
        Una sola línea para CSV: "EZE→PTY(CM278) ✦90m PTY→MIA(CM419) // MIA→PTY(CM420) ✦105m PTY→EZE(CM279)"
        """
        out = self._leg_str(self.outbound)
        ret = self._leg_str(self.inbound)
        return f"{out} // {ret}"

    @property
    def itinerary_hash(self) -> str:
        """
        Hash del itinerario SIN precio — identifica el mismo conjunto de vuelos
        aunque el precio cambie entre corridas.
        Permite rastrear: "¿este itinerario apareció en corridas anteriores?"
        """
        key = json.dumps({
            "origin": self.origin, "destination": self.destination,
            "date_out": self.date_out, "date_ret": self.date_ret,
            "outbound_flights": [
                {"fn": s.flight_number, "dep": s.departure_iata,
                 "arr": s.arrival_iata, "at": s.departure_time}
                for s in self.outbound.segments
            ],
            "inbound_flights": [
                {"fn": s.flight_number, "dep": s.departure_iata,
                 "arr": s.arrival_iata, "at": s.departure_time}
                for s in self.inbound.segments
            ],
        }, sort_keys=True)
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def content_hash(self) -> str:
        key = json.dumps({
            "origin": self.origin, "destination": self.destination,
            "date_out": self.date_out, "date_ret": self.date_ret,
            "price": round(self.total_price, 2),
            "outbound_dep": self.outbound.departure_time,
            "inbound_dep": self.inbound.departure_time,
        }, sort_keys=True)
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        """
        Serializa la oferta.

        Campos raw (usados por DB y content_hash):
          total_price  → float tal como viene del proveedor (puede ser minor units)
          airlines     → lista de códigos IATA

        Sub-dict "presentation" (para JSON exportado al usuario):
          price        → decimal normalizado (minor units → float con 2 dec)
          airlines     → nombres legibles
        """
        from src.normalizer import normalize_airlines, normalize_airline, fmt_price_json  # import local, evita ciclo
        provider_names = {}
        for seg in self.outbound.segments + self.inbound.segments:
            if getattr(seg, "airline_name", ""):
                provider_names.setdefault(seg.carrier, seg.airline_name)
        presentation_names = [provider_names.get(code) or normalize_airline(code) for code in self.airlines]
        return {
            "origin": self.origin, "destination": self.destination,
            "date_out": self.date_out, "date_ret": self.date_ret,
            "total_price": self.total_price,           # raw — usado internamente
            "currency": self.currency,
            "cabin_class": self.cabin_class,
            "adults": self.adults, "children": self.children,
            "stops_outbound": self.stops_outbound,
            "stops_inbound": self.stops_inbound,
            "total_stops": self.total_stops,
            "total_duration": self.total_duration_str,
            "total_duration_minutes": self.total_duration_minutes,
            "total_layover_hours": round(self.total_layover_hours, 2),
            "airlines": self.airlines,                 # raw IATA — usado por DB
            "outbound": self.outbound.to_dict(),
            "inbound": self.inbound.to_dict(),
            "baggage": self.baggage or {"status": "NOT_CHECKED"},
            "booking": self.booking or {"status": "NOT_CHECKED"},
            "links": {
                "google_flights": self.google_flights_url,
                "google_flights_exact": self.exact_google_flights_url,
                "google_flights_scope": "EXACT" if self.exact_google_flights_url else "SEARCH",
                "amadeus": self.amadeus_deeplink,
                "airline": self.airline_url,
            },
            "content_hash":    self.content_hash(),
            "itinerary_str":   self.itinerary_str,
            "itinerary_hash":  self.itinerary_hash,
            # ── Capa de presentación (para JSON exportado al usuario) ─────
            "presentation": {
                "price":    fmt_price_json(self.total_price),
                "airlines": presentation_names,
                "itinerary": self.itinerary_str,
            },
        }

    # Compat shims
    @property
    def outbound_segments(self) -> List[Segment]:
        return self.outbound.segments

    @property
    def inbound_segments(self) -> List[Segment]:
        return self.inbound.segments


# ---------------------------------------------------------------------------
# Filtrado y ranking
# ---------------------------------------------------------------------------

def _passes_filters(
    offer: Offer,
    max_stops: Optional[int],
    max_layover_hours: Optional[float],
) -> tuple[bool, str]:
    if max_stops is not None:
        if offer.stops_outbound > max_stops:
            return False, f"ida tiene {offer.stops_outbound} escala(s), máx: {max_stops}"
        if offer.stops_inbound > max_stops:
            return False, f"vuelta tiene {offer.stops_inbound} escala(s), máx: {max_stops}"
    if max_layover_hours is not None:
        for label, leg in [("ida", offer.outbound), ("vuelta", offer.inbound)]:
            for lv in leg.layovers:
                if lv.duration_hours > max_layover_hours:
                    return False, (
                        f"escala en {label} ({lv.airport_iata}) dura "
                        f"{lv.duration_str}, máx: {max_layover_hours}h"
                    )
    return True, ""


def rank_offers(offers: List[Offer]) -> List[Offer]:
    """Ranking determinístico: precio asc → duración asc → escalas asc."""
    return sorted(
        offers,
        key=lambda o: (o.total_price, o.total_duration_minutes, o.total_stops),
    )


def _leg_from_segments(segments: List[Segment]) -> Leg:
    layovers = []
    for i in range(len(segments) - 1):
        arr = datetime.fromisoformat(segments[i].arrival_time)
        dep = datetime.fromisoformat(segments[i + 1].departure_time)
        layovers.append(LayoverInfo(
            airport_iata=segments[i].arrival_iata,
            duration_minutes=int((dep - arr).total_seconds() / 60),
        ))
    return Leg(segments=segments, layovers=layovers)


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------

MOCK_AIRLINES = ["AA", "LA", "IB", "CM", "AR"]
MOCK_HUBS = ["PTY", "BOG", "LIM", "SCL", "GRU"]


def _make_segment(carrier, origin, destination, dep_dt, flight_hours, cabin_class="ECONOMY"):
    arr_dt = dep_dt + timedelta(hours=flight_hours)
    num = random.randint(100, 999)
    return Segment(
        carrier=carrier, flight_number=f"{carrier}{num}",
        departure_iata=origin, arrival_iata=destination,
        departure_time=dep_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        arrival_time=arr_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        cabin_class=cabin_class,
        baggage_included=random.choice([True, False]),
        baggage_pieces=random.choice([0, 1, 2]),
    )


def _build_mock_leg(origin, destination, date, n_stops, layover_hours, cabin_class):
    carrier = random.choice(MOCK_AIRLINES)
    dep_hour = random.randint(5, 20)
    dep_dt = datetime.fromisoformat(f"{date}T{dep_hour:02d}:00:00")
    if n_stops == 0:
        seg = _make_segment(carrier, origin, destination, dep_dt,
                            round(random.uniform(4, 7), 1), cabin_class)
        return _leg_from_segments([seg])
    candidate_hubs = [h for h in MOCK_HUBS if h not in {origin, destination}]
    hub = random.choice(candidate_hubs or ["PTY"])
    seg1 = _make_segment(carrier, origin, hub, dep_dt,
                          round(random.uniform(3, 5), 1), cabin_class)
    dep2 = datetime.fromisoformat(seg1.arrival_time) + timedelta(hours=layover_hours)
    seg2 = _make_segment(carrier, hub, destination, dep2,
                          round(random.uniform(2, 4), 1), cabin_class)
    return _leg_from_segments([seg1, seg2])


def _mock_search(origin, destination, date_out, date_ret, adults, children_ages,
                 cabin_class, currency, max_stops, max_layover_hours):
    raw = []
    for _ in range(random.randint(3, 6)):
        n_out = random.choice([0, 0, 1, 1, 2])
        n_in  = random.choice([0, 0, 1, 1, 2])
        lo    = round(random.uniform(0.75, 5), 2)
        li    = round(random.uniform(0.75, 5), 2)
        raw.append(Offer(
            origin=origin, destination=destination,
            date_out=date_out, date_ret=date_ret,
            total_price=round(random.uniform(600, 1800) * (adults + len(children_ages)), 2),
            currency=currency, cabin_class=cabin_class,
            outbound=_build_mock_leg(origin, destination, date_out, n_out, lo, cabin_class),
            inbound=_build_mock_leg(destination, origin, date_ret, n_in, li, cabin_class),
            adults=adults, children=len(children_ages),
        ))
    filtered = []
    excluded_airline_count = 0
    for offer in raw:
        if has_excluded_airline(offer.airlines):
            excluded_airline_count += 1
            logger.debug("Mock: descartada por airline excluida — %s", offer.airlines)
            continue
        ok, reason = _passes_filters(offer, max_stops, max_layover_hours)
        if ok:
            filtered.append(offer)
        else:
            logger.debug("Mock: descartada — %s", reason)
    if excluded_airline_count:
        logger.info("Mock: %d oferta(s) excluida(s) por airline filter", excluded_airline_count)
    return filtered


# ---------------------------------------------------------------------------
# Amadeus provider con retry + backoff exponencial
# ---------------------------------------------------------------------------

def _amadeus_search(origin, destination, date_out, date_ret, adults, children_ages,
                    cabin_class, currency, max_stops, max_layover_hours,
                    api_key, api_secret, env="test", max_retries=3, backoff_base=2.0):
    """
    Llama a Amadeus Flight Offers Search API v2.
    Dependencia opcional: pip install amadeus
    """
    try:
        from amadeus import Client, ResponseError  # type: ignore
    except ImportError:
        raise RuntimeError(
            "El SDK de Amadeus no está instalado.\n"
            "Instalalo con:  pip install amadeus\n"
            "O usá PRICE_PROVIDER=mock para pruebas sin API key."
        )

    client = Client(
        client_id=api_key, client_secret=api_secret,
        hostname="production" if env == "production" else "test",
    )
    params: dict = {
        "originLocationCode": origin, "destinationLocationCode": destination,
        "departureDate": date_out, "returnDate": date_ret,
        "adults": adults, "travelClass": cabin_class,
        "currencyCode": currency, "max": 250,
    }
    if children_ages:
        params["children"] = len(children_ages)

    last_exc: Exception = RuntimeError("no attempt")
    for attempt in range(1, max_retries + 1):
        try:
            response = client.shopping.flight_offers_search.get(**params)
            break
        except Exception as exc:
            last_exc = exc
            if attempt == max_retries:
                logger.error("Amadeus: %d intentos fallidos — %s", max_retries, exc,
                             extra={"amadeus_error": str(exc)})
                raise
            wait = backoff_base ** attempt
            logger.warning("Amadeus: intento %d/%d falló (%s). Reintentando en %.0fs…",
                           attempt, max_retries, exc, wait,
                           extra={"attempt": attempt, "wait_seconds": wait})
            time.sleep(wait)

    offers = []
    for raw in response.data:
        try:
            price = float(raw["price"]["grandTotal"])
            itins = raw.get("itineraries", [])
            tp    = raw.get("travelerPricings", [])

            def get_bag(seg_idx):
                try:
                    fa  = tp[0]["fareDetailsBySegment"][seg_idx]
                    inc = fa.get("includedCheckedBags", {})
                    qty = inc.get("quantity", inc.get("weight", 0))
                    return qty > 0, qty
                except (IndexError, KeyError):
                    return False, 0

            def parse_segs(itin):
                out = []
                for si, s in enumerate(itin.get("segments", [])):
                    bag_ok, bag_n = get_bag(si)
                    out.append(Segment(
                        carrier=s["carrierCode"],
                        flight_number=s["carrierCode"] + s["number"],
                        departure_iata=s["departure"]["iataCode"],
                        arrival_iata=s["arrival"]["iataCode"],
                        departure_time=s["departure"]["at"],
                        arrival_time=s["arrival"]["at"],
                        stops=s.get("numberOfStops", 0),
                        cabin_class=cabin_class,
                        baggage_included=bag_ok,
                        baggage_pieces=bag_n,
                    ))
                return out

            out_segs = parse_segs(itins[0]) if itins else []
            in_segs  = parse_segs(itins[1]) if len(itins) > 1 else []

            offer = Offer(
                origin=origin, destination=destination,
                date_out=date_out, date_ret=date_ret,
                total_price=price, currency=currency, cabin_class=cabin_class,
                outbound=_leg_from_segments(out_segs),
                inbound=_leg_from_segments(in_segs),
                adults=adults, children=len(children_ages), raw=raw,
            )
            if has_excluded_airline(offer.airlines):
                logger.debug("Amadeus: descartada por airline excluida — %s", offer.airlines)
                continue
            ok, reason = _passes_filters(offer, max_stops, max_layover_hours)
            if ok:
                offers.append(offer)
            else:
                logger.debug("Amadeus: descartada — %s", reason)
        except (KeyError, ValueError, IndexError) as e:
            logger.warning("No se pudo parsear oferta Amadeus: %s", e,
                           extra={"parse_error": str(e)})
    return offers


# ---------------------------------------------------------------------------
# Interfaz pública
# ---------------------------------------------------------------------------

def search_offers(origin, destination, date_out, date_ret, adults, children_ages,
                  cabin_class, currency, max_stops, max_layover_hours,
                  provider="mock", **provider_kwargs) -> List[Offer]:
    """
    Interfaz única de búsqueda de vuelos.

    Providers disponibles
    ─────────────────────
    mock    → datos sintéticos, sin API key (desarrollo / tests)
    amadeus → Amadeus Self-Service API v2 (requiere pip install amadeus)
    duffel  → Duffel API v2, stdlib puro, sandbox gratuito — RECOMENDADO
              Registro: https://app.duffel.com/join

    provider_kwargs por provider
    ─────────────────────────────
    duffel:
      api_token     str   Bearer token (duffel_test_XXX o duffel_live_XXX)
      max_results   int   máx ofertas a procesar (default 200)
      max_retries   int   reintentos con backoff (default 3)
      backoff_base  float base del backoff exponencial (default 2.0)

    amadeus:
      api_key       str   client_id de Amadeus
      api_secret    str   client_secret de Amadeus
      env           str   'test' | 'production'
      max_retries   int
      backoff_base  float

    serpapi:
      api_key                str   API key de SerpApi
      max_outbound_tokens    int   tokens de ida a usar (default 1 = 2 créditos/búsqueda)
      max_retries            int
      backoff_base           float
    """
    logger.info(
        "Buscando vuelos %s → %s (%s / %s) | escalas/tramo≤%s | escala≤%sh | proveedor=%s",
        origin, destination, format_date_ui(date_out), format_date_ui(date_ret),
        max_stops if max_stops is not None else "∞",
        max_layover_hours if max_layover_hours is not None else "∞",
        provider,
    )
    if provider == "mock":
        return _mock_search(origin, destination, date_out, date_ret,
                            adults, children_ages, cabin_class, currency,
                            max_stops, max_layover_hours)
    elif provider == "amadeus":
        return _amadeus_search(origin, destination, date_out, date_ret,
                               adults, children_ages, cabin_class, currency,
                               max_stops, max_layover_hours,
                               api_key=provider_kwargs.get("api_key", ""),
                               api_secret=provider_kwargs.get("api_secret", ""),
                               env=provider_kwargs.get("env", "test"),
                               max_retries=provider_kwargs.get("max_retries", 3),
                               backoff_base=provider_kwargs.get("backoff_base", 2.0))
    elif provider == "duffel":
        from src.providers_duffel import duffel_search
        return duffel_search(
            origin=origin, destination=destination,
            date_out=date_out, date_ret=date_ret,
            adults=adults, children_ages=children_ages,
            cabin_class=cabin_class, currency=currency,
            max_stops=max_stops, max_layover_hours=max_layover_hours,
            api_token=provider_kwargs.get("api_token", ""),
            max_results=provider_kwargs.get("max_results", 200),
            max_retries=provider_kwargs.get("max_retries", 3),
            backoff_base=provider_kwargs.get("backoff_base", 2.0),
        )
    elif provider == "serpapi":
        from src.providers_serpapi import serpapi_search
        return serpapi_search(
            origin=origin, destination=destination,
            date_out=date_out, date_ret=date_ret,
            adults=adults, children_ages=children_ages,
            cabin_class=cabin_class, currency=currency,
            max_stops=max_stops, max_layover_hours=max_layover_hours,
            api_key=provider_kwargs.get("api_key", ""),
            max_outbound_tokens=provider_kwargs.get("max_outbound_tokens", 1),
            show_hidden=provider_kwargs.get("show_hidden", True),
            gl=provider_kwargs.get("gl", "ar"),
            hl=provider_kwargs.get("hl", "es"),
            max_retries=provider_kwargs.get("max_retries", 3),
            backoff_base=provider_kwargs.get("backoff_base", 2.0),
            diagnostics=provider_kwargs.get("diagnostics"),
        )
    elif provider == "kiwi":
        from src.providers_kiwi import kiwi_search
        return kiwi_search(
            origin=origin, destination=destination,
            date_out=date_out, date_ret=date_ret,
            adults=adults, children_ages=children_ages,
            cabin_class=cabin_class, currency=currency,
            max_stops=max_stops, max_layover_hours=max_layover_hours,
            api_key=provider_kwargs.get("api_key", ""),
            max_results=provider_kwargs.get("max_results", 50),
        )
    else:
        raise ValueError(
            f"Proveedor desconocido: '{provider}'. "
            f"Opciones: 'mock', 'amadeus', 'duffel', 'serpapi', 'kiwi'"
        )