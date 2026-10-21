"""
Tests del agente de monitoreo de vuelos.
Ejecutar: python -m pytest tests/ -v
"""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, UTC

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from src.alert_logic import should_alert
from src.database import (
    initialize_db, save_snapshot, save_offers, save_alert,
    get_first_price, get_min_price, get_last_alert, get_price_stats, export_csv,
)
from src.providers import (
    search_offers, rank_offers, Offer, Segment, Leg, LayoverInfo,
    _passes_filters, _leg_from_segments,
)
from config.settings import load_config, ConfigError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    db = str(tmp_path / "test.db")
    initialize_db(db)
    return db


@pytest.fixture
def scope():
    return dict(destination="PUJ", date_out="2027-07-04", date_ret="2027-07-11", currency="USD")


def _make_offer(price=1000.0, stops_out=0, stops_in=0, layover_h=1.5) -> Offer:
    def leg(origin, hub, dest, date, n_stops):
        dep = datetime(2026, 7, 4, 8, 0)
        if n_stops == 0:
            seg = Segment("AA", "AA1", origin, dest,
                          dep.strftime("%Y-%m-%dT%H:%M:%S"),
                          (dep + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S"),
                          cabin_class="ECONOMY", baggage_included=True)
            return _leg_from_segments([seg])
        s1 = Segment("AA", "AA1", origin, hub,
                     dep.strftime("%Y-%m-%dT%H:%M:%S"),
                     (dep + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S"),
                     cabin_class="ECONOMY", baggage_included=True)
        dep2 = dep + timedelta(hours=4) + timedelta(hours=layover_h)
        s2 = Segment("AA", "AA2", hub, dest,
                     dep2.strftime("%Y-%m-%dT%H:%M:%S"),
                     (dep2 + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S"),
                     cabin_class="ECONOMY")
        return _leg_from_segments([s1, s2])

    return Offer(
        origin="EZE", destination="PUJ",
        date_out="2027-07-04", date_ret="2027-07-11",
        total_price=price, currency="USD", cabin_class="ECONOMY",
        outbound=leg("EZE", "MIA", "PUJ", "2027-07-04", stops_out),
        inbound=leg("PUJ", "MIA", "EZE", "2027-07-11", stops_in),
        adults=2, children=1,
    )


# ---------------------------------------------------------------------------
# Tests: config/settings
# ---------------------------------------------------------------------------

class TestConfig:
    def test_defaults_load(self):
        cfg = load_config(cli_overrides={"DATE_OUTBOUND": "2027-07-04", "DATE_RETURN": "2027-07-11"})
        assert cfg.DESTINATION == "MIA"
        assert cfg.ADULTS == 2
        assert cfg.MAX_STOPS == 1
        assert cfg.MAX_LAYOVER_HOURS == 2.0

    def test_cli_overrides_env(self):
        cfg = load_config(cli_overrides={
            "DATE_OUTBOUND": "2027-08-01", "DATE_RETURN": "2027-08-08",
            "MAX_STOPS": 0, "MAX_LAYOVER_HOURS": 2.0,
        })
        assert cfg.DATE_OUTBOUND == "2027-08-01"
        assert cfg.MAX_STOPS == 0
        assert cfg.MAX_LAYOVER_HOURS == 2.0

    def test_invalid_date_format_raises(self):
        with pytest.raises(ConfigError, match="DEPART_DATE"):
            load_config(cli_overrides={"DATE_OUTBOUND": "04/07/2026", "DATE_RETURN": "2027-07-11"})

    def test_past_date_raises(self):
        with pytest.raises(ConfigError, match="pasada"):
            load_config(cli_overrides={"DATE_OUTBOUND": "2020-01-01", "DATE_RETURN": "2020-01-08"})

    def test_return_before_depart_raises(self):
        with pytest.raises(ConfigError, match="anterior"):
            load_config(cli_overrides={"DATE_OUTBOUND": "2027-07-11", "DATE_RETURN": "2027-07-04"})

    def test_negative_max_stops_raises(self):
        with pytest.raises(ConfigError, match="MAX_STOPS"):
            load_config(cli_overrides={
                "DATE_OUTBOUND": "2027-07-04", "DATE_RETURN": "2027-07-11",
                "MAX_STOPS": -1,
            })

    def test_zero_max_layover_raises(self):
        with pytest.raises(ConfigError, match="MAX_LAYOVER_HOURS"):
            load_config(cli_overrides={
                "DATE_OUTBOUND": "2027-07-04", "DATE_RETURN": "2027-07-11",
                "MAX_LAYOVER_HOURS": 0,
            })

    def test_yaml_loaded(self, tmp_path):
        yaml_file = tmp_path / "cfg.yaml"
        yaml_file.write_text("max_stops: 0\ndepart_date: '2027-07-04'\nreturn_date: '2027-07-11'\n")
        cfg = load_config(yaml_path=str(yaml_file))
        assert cfg.MAX_STOPS == 0

    def test_cli_overrides_yaml(self, tmp_path):
        yaml_file = tmp_path / "cfg.yaml"
        yaml_file.write_text("max_stops: 0\ndepart_date: '2027-07-04'\nreturn_date: '2027-07-11'\n")
        cfg = load_config(cli_overrides={"MAX_STOPS": 1}, yaml_path=str(yaml_file))
        assert cfg.MAX_STOPS == 1  # CLI wins over YAML dentro de la política V2


# ---------------------------------------------------------------------------
# Tests: providers — Offer model
# ---------------------------------------------------------------------------

class TestOfferModel:
    def test_stops_per_leg(self):
        offer = _make_offer(stops_out=1, stops_in=0)
        assert offer.stops_outbound == 1
        assert offer.stops_inbound == 0
        assert offer.total_stops == 1

    def test_duration_str(self):
        offer = _make_offer(stops_out=0, stops_in=0)
        assert "h" in offer.total_duration_str

    def test_layover_info(self):
        offer = _make_offer(stops_out=1, layover_h=2.0)
        assert len(offer.outbound.layovers) == 1
        assert abs(offer.outbound.layovers[0].duration_hours - 2.0) < 0.1

    def test_content_hash_deterministic(self):
        o1 = _make_offer(price=1000.0)
        o2 = _make_offer(price=1000.0)
        assert o1.content_hash() == o2.content_hash()

    def test_content_hash_differs_on_price(self):
        assert _make_offer(1000.0).content_hash() != _make_offer(1001.0).content_hash()

    def test_to_dict_has_links(self):
        d = _make_offer().to_dict()
        assert "links" in d
        assert "google_flights" in d["links"]
        assert "tfs=" in d["links"]["google_flights"]
        assert "q=Flights" not in d["links"]["google_flights"]

    def test_to_dict_has_segments(self):
        d = _make_offer(stops_out=1).to_dict()
        assert len(d["outbound"]["segments"]) == 2
        assert len(d["outbound"]["layovers"]) == 1

    def test_google_flights_url(self):
        offer = _make_offer()
        url = offer.google_flights_url
        assert "google.com" in url
        assert "tfs=" in url
        assert "q=Flights" not in url

    def test_airline_url_known_carrier(self):
        offer = _make_offer()
        assert offer.airline_url is not None  # AA is known


# ---------------------------------------------------------------------------
# Tests: filtering and ranking
# ---------------------------------------------------------------------------

class TestFiltersAndRanking:
    def test_passes_filter_direct(self):
        ok, _ = _passes_filters(_make_offer(stops_out=0, stops_in=0), 1, None)
        assert ok

    def test_fails_max_stops_outbound(self):
        offer = _make_offer(stops_out=1, stops_in=0)
        # Forzar el offer a tener 2 segmentos en outbound ya lo hace _make_offer con stops_out=1
        ok, reason = _passes_filters(offer, max_stops=0, max_layover_hours=None)
        assert not ok and "ida" in reason

    def test_fails_layover_too_long(self):
        offer = _make_offer(stops_out=1, layover_h=5.0)
        ok, reason = _passes_filters(offer, max_stops=None, max_layover_hours=3.0)
        assert not ok

    def test_rank_by_price_then_duration(self):
        cheap_slow  = _make_offer(price=900.0,  stops_out=1, stops_in=1)
        cheap_fast  = _make_offer(price=900.0,  stops_out=0, stops_in=0)
        expensive   = _make_offer(price=1200.0, stops_out=0, stops_in=0)
        ranked = rank_offers([expensive, cheap_slow, cheap_fast])
        assert ranked[0].total_price == 900.0
        assert ranked[0].total_duration_minutes <= ranked[1].total_duration_minutes


# ---------------------------------------------------------------------------
# Tests: database
# ---------------------------------------------------------------------------

class TestDatabase:
    def test_tables_created(self, tmp_db):
        with sqlite3.connect(tmp_db) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"snapshots", "offers", "alerts"} <= tables

    def test_save_snapshot_and_retrieve(self, tmp_db, scope):
        sid = save_snapshot(db_path=tmp_db, origin="EZE", **scope,
                            best_price=1200.0, adults=2, children=1)
        assert sid == 1
        assert get_first_price(tmp_db, **scope) == 1200.0

    def test_save_offers_no_duplicate(self, tmp_db, scope):
        sid = save_snapshot(db_path=tmp_db, origin="EZE", **scope,
                            best_price=1000.0, adults=2, children=1)
        offer = _make_offer(price=1000.0)
        n1 = save_offers(tmp_db, sid, [offer])
        n2 = save_offers(tmp_db, sid, [offer])   # mismo run_id + hash
        assert n1 == 1
        assert n2 == 0  # deduplicado

    def test_min_price_scoped(self, tmp_db, scope):
        save_snapshot(db_path=tmp_db, origin="EZE", **scope,
                      best_price=1500.0, adults=2, children=1)
        save_snapshot(db_path=tmp_db, origin="AEP", **scope,
                      best_price=1100.0, adults=2, children=1)
        assert get_min_price(tmp_db, **scope) == 1100.0
        assert get_first_price(tmp_db, **scope) == 1500.0

    def test_prices_not_contaminated_by_other_route(self, tmp_db, scope):
        save_snapshot(db_path=tmp_db, origin="EZE", **scope,
                      best_price=1000.0, adults=2, children=1)
        save_snapshot(db_path=tmp_db, origin="EZE",
                      destination="GRU", date_out="2027-07-04", date_ret="2027-07-11",
                      currency="USD", best_price=300.0, adults=2, children=1)
        assert get_min_price(tmp_db, **scope) == 1000.0

    def test_price_stats(self, tmp_db, scope):
        for p in [1800.0, 1700.0, 1500.0]:
            save_snapshot(db_path=tmp_db, origin="EZE", **scope,
                          best_price=p, adults=2, children=1)
        s = get_price_stats(tmp_db, **scope)
        assert s["count"] == 3
        assert s["min"] == 1500.0
        assert s["max"] == 1800.0
        assert s["trend_pct"] < 0   # bajó desde 1800 a 1500

    def test_last_alert_scoped(self, tmp_db, scope):
        sid_puj = save_snapshot(db_path=tmp_db, origin="EZE", **scope,
                                best_price=1000.0, adults=2, children=1)
        save_alert(db_path=tmp_db, snapshot_id=sid_puj, price_alerted=1000.0,
                   baseline_price=1200.0, drop_absolute=200.0, drop_percent=16.7, email_sent=True)
        sid_gru = save_snapshot(db_path=tmp_db, origin="EZE",
                                destination="GRU", date_out="2027-07-04", date_ret="2027-07-11",
                                currency="USD", best_price=400.0, adults=2, children=1)
        save_alert(db_path=tmp_db, snapshot_id=sid_gru, price_alerted=400.0,
                   baseline_price=600.0, drop_absolute=200.0, drop_percent=33.3, email_sent=True)
        la = get_last_alert(tmp_db, **scope)
        assert la["price_alerted"] == 1000.0

    def test_export_csv(self, tmp_db, tmp_path, scope):
        sid = save_snapshot(db_path=tmp_db, origin="EZE", **scope,
                            best_price=1000.0, adults=2, children=1)
        save_offers(tmp_db, sid, [_make_offer(price=1000.0)])
        csv_path = str(tmp_path / "history.csv")
        n = export_csv(tmp_db, csv_path, **scope)
        assert n == 1
        # Segunda exportación no duplica
        n2 = export_csv(tmp_db, csv_path, **scope)
        assert n2 == 0


# ---------------------------------------------------------------------------
# Tests: alert_logic
# ---------------------------------------------------------------------------

class TestShouldAlert:
    def test_triggers_absolute(self):
        ok, _ = should_alert(current_price=900, baseline_price=1000,
                             last_alerted_price=None, last_alert_time=None,
                             absolute_drop=50, percent_drop=99, mode="absolute",
                             min_hours_between=0, currency="USD")
        assert ok

    def test_no_trigger_price_above_last_alerted(self):
        ok, _ = should_alert(current_price=1000, baseline_price=1200,
                             last_alerted_price=950, last_alert_time=None,
                             absolute_drop=50, percent_drop=5, mode="either",
                             min_hours_between=0, currency="USD")
        assert not ok

    def test_no_trigger_within_window(self):
        recent = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        ok, _ = should_alert(current_price=800, baseline_price=1000,
                             last_alerted_price=1000, last_alert_time=recent,
                             absolute_drop=50, percent_drop=5, mode="either",
                             min_hours_between=6, currency="USD")
        assert not ok

    def test_reason_has_no_empty_placeholder(self):
        _, reason = should_alert(current_price=900, baseline_price=1000,
                                 last_alerted_price=None, last_alert_time=None,
                                 absolute_drop=50, percent_drop=5, mode="absolute",
                                 min_hours_between=0, currency="USD")
        assert "''" not in reason
        assert "USD" in reason


# ---------------------------------------------------------------------------
# Tests: mock provider
# ---------------------------------------------------------------------------

class TestMockProvider:
    def test_returns_list(self):
        offers = search_offers(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[7],
            cabin_class="ECONOMY", currency="USD",
            max_stops=1, max_layover_hours=3.0, provider="mock",
        )
        assert isinstance(offers, list)

    def test_max_stops_per_leg_respected(self):
        for _ in range(20):
            offers = search_offers(
                origin="EZE", destination="PUJ",
                date_out="2027-07-04", date_ret="2027-07-11",
                adults=2, children_ages=[7], cabin_class="ECONOMY",
                currency="USD", max_stops=1, max_layover_hours=None,
                provider="mock",
            )
            for o in offers:
                assert o.stops_outbound <= 1
                assert o.stops_inbound <= 1

    def test_layover_limit_respected(self):
        for _ in range(20):
            offers = search_offers(
                origin="EZE", destination="PUJ",
                date_out="2027-07-04", date_ret="2027-07-11",
                adults=2, children_ages=[7], cabin_class="ECONOMY",
                currency="USD", max_stops=1, max_layover_hours=3.0,
                provider="mock",
            )
            for o in offers:
                assert o.max_layover_hours() <= 3.01

    def test_ranked_offers_sorted(self):
        offers = search_offers(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[7], cabin_class="ECONOMY",
            currency="USD", max_stops=2, max_layover_hours=None,
            provider="mock",
        )
        ranked = rank_offers(offers)
        for i in range(len(ranked) - 1):
            assert ranked[i].total_price <= ranked[i + 1].total_price

    def test_offer_json_has_links(self):
        offers = search_offers(
            origin="EZE", destination="PUJ",
            date_out="2027-07-04", date_ret="2027-07-11",
            adults=2, children_ages=[7], cabin_class="ECONOMY",
            currency="USD", max_stops=2, max_layover_hours=None,
            provider="mock",
        )
        if offers:
            d = offers[0].to_dict()
            assert "links" in d
            assert d["links"]["google_flights"]
