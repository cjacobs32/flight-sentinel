from pathlib import Path
import cloud_dashboard


def test_best_verified_uses_lowest_bookable_price():
    js = cloud_dashboard.JS
    assert "const verifiedOffers=" in js
    assert ".sort((a,b)=>Number(a.bookable_price)-Number(b.bookable_price))[0]" in js


def test_verified_when_bookable_equals_observed():
    js = cloud_dashboard.JS
    assert "booked=String(bookingInfo(off).status||off.booking_status||'').toUpperCase()==='VERIFIED'&&off.bookable_price!=null" in js
    assert "booked=shown!==observed" not in js


def test_history_prefers_lowest_verified_price_per_run():
    js = cloud_dashboard.JS
    assert "verified=(e.verified||[]).filter" in js
    assert "best=verified[0]||src.best" in js


def test_multi_airline_badge_is_not_misattributed():
    js = cloud_dashboard.JS
    assert "'MEJOR OPCIÓN CON '" in js
