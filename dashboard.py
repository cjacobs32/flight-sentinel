"""Dashboard web local de Flight Sentinel V2.

V2.4.1: compactación de ofertas, CTA de decisión, badges consistentes y timeline resumido.
La UI prioriza comparación y decisión; los detalles técnicos quedan bajo demanda.
"""
from __future__ import annotations

from src.google_flights_link import normalize_google_flights_url

from datetime import date, datetime, timedelta
import csv
import html
import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from src.agent import run_check, audit_existing_snapshot
from src.database import (
    get_latest_snapshot, get_monitor_history, get_snapshot_offers,
    initialize_db, get_market_audit, get_offer_by_id, update_offer_google_details,
)
from src.monitor_registry import (
    MonitorProfile, apply_monitor, delete_monitor, get_monitor,
    load_monitors, upsert_monitor,
)
from src.normalizer import normalize_airline, normalize_airlines, airline_info
from src.providers_serpapi import resolve_exact_google_flights_details

_JOB_LOCKS_GUARD = threading.Lock()
_JOB_LOCKS: dict[str, threading.Lock] = {}


def _job_lock(monitor_id: str) -> threading.Lock:
    key = (monitor_id or "").strip().lower()
    with _JOB_LOCKS_GUARD:
        lock = _JOB_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _JOB_LOCKS[key] = lock
        return lock


def esc(v) -> str:
    return html.escape(str(v), quote=True)


# ---------------------------------------------------------------------------
# Design system
# ---------------------------------------------------------------------------

CSS = r"""
:root{
  --bg:#07111f;--surface:#0c1828;--surface-2:#101f32;--surface-3:#142740;
  --text:#f2f6fb;--muted:#91a5ba;--muted-2:#6f859b;--border:#213a54;
  --success:#27B067;--success-bg:rgba(39,176,103,.12);--info:#378ADD;--info-bg:rgba(55,138,221,.12);
  --warning:#BA7517;--warning-bg:rgba(186,117,23,.14);--critical:#E24B4A;--critical-bg:rgba(226,75,74,.12);
  --blue:var(--info);--blue-2:#2a68c4;--green:var(--success);--green-bg:var(--success-bg);
  --amber:var(--warning);--amber-bg:var(--warning-bg);--red:var(--critical);--red-bg:var(--critical-bg);
  --shadow:0 16px 44px rgba(0,0,0,.20);--radius:16px;--radius-sm:10px;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:radial-gradient(circle at 15% -10%,#102a45 0,transparent 30%),linear-gradient(165deg,#06101d,#091524 60%,#08192a);color:var(--text);font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;min-height:100vh;line-height:1.45}
a{color:inherit}.app{max-width:1180px;margin:0 auto;padding:22px 20px 64px}.appbar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:22px}.brand{display:flex;align-items:center;gap:11px;text-decoration:none}.brand-mark{width:38px;height:38px;border-radius:12px;background:linear-gradient(145deg,#53b7fa,#246da8);display:grid;place-items:center;color:#04121f;font-size:20px;box-shadow:0 8px 24px rgba(74,174,242,.24)}.brand-title{font-size:18px;font-weight:780;letter-spacing:-.02em}.brand-sub{font-size:11px;color:var(--muted);margin-top:1px}.nav-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.page-head{margin:10px 0 20px}.breadcrumb{font-size:12px;color:var(--muted);margin-bottom:7px}.breadcrumb a{text-decoration:none;color:#b7c9da}.page-title{font-size:29px;line-height:1.15;margin:0;letter-spacing:-.035em}.page-subtitle{font-size:13px;color:var(--muted);margin-top:6px}.section{margin-top:26px}.section-head{display:flex;align-items:end;justify-content:space-between;gap:14px;margin-bottom:12px}.section-head h2{font-size:18px;margin:0;letter-spacing:-.02em}.section-head p{font-size:12px;color:var(--muted);margin:3px 0 0}.section-actions{display:flex;gap:8px;flex-wrap:wrap}
.btn{appearance:none;border:1px solid var(--border);background:var(--surface-2);color:var(--text);padding:10px 14px;border-radius:10px;text-decoration:none;cursor:pointer;font:inherit;font-size:13px;font-weight:680;display:inline-flex;align-items:center;justify-content:center;gap:7px;transition:.18s ease;min-height:40px}.btn:hover{background:var(--surface-3);border-color:#355b7c;transform:translateY(-1px)}.btn.primary{background:var(--info);border-color:var(--info);color:white;box-shadow:0 7px 20px rgba(55,138,221,.18)}.btn.primary:hover{background:#2a68c4;border-color:#2a68c4}.btn.buy{background:var(--success);border-color:var(--success);color:white;font-weight:780;box-shadow:0 8px 22px rgba(39,176,103,.20)}.btn.buy:hover{background:#218f56;border-color:#218f56}.btn.ghost{background:transparent}.btn.danger{background:transparent;color:#ff9b9b;border-color:#613c46}.btn.danger:hover{background:var(--red-bg)}.btn.small{padding:8px 10px;min-height:34px;font-size:12px}.btn:disabled{opacity:.55;cursor:not-allowed;transform:none}.icon{font-size:15px;line-height:1}
.card{background:linear-gradient(180deg,rgba(16,31,50,.98),rgba(12,24,40,.98));border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow)}.card-pad{padding:20px}.muted{color:var(--muted)}.mini{font-size:11px}.small-text{font-size:12px}.divider{height:1px;background:var(--border);margin:18px 0}.empty{padding:42px 20px;text-align:center;color:var(--muted)}
.badge{display:inline-flex;align-items:center;gap:5px;padding:5px 8px;border-radius:999px;border:1px solid var(--border);font-size:10px;font-weight:800;letter-spacing:.03em;text-transform:uppercase;background:#142b43;color:#cce6fa;white-space:nowrap}.badge.ok,.badge.real,.badge.success,.badge.direct{background:var(--success-bg);color:#82e8b1;border-color:rgba(39,176,103,.36)}.badge.blue,.badge.info,.badge.stop{background:var(--info-bg);color:#9fd2ff;border-color:rgba(55,138,221,.38)}.badge.warn,.badge.warning,.badge.simulation,.badge.sandbox{background:var(--warning-bg);color:#ffd18d;border-color:rgba(186,117,23,.40)}.badge.bad,.badge.critical{background:var(--critical-bg);color:#ffb2b2;border-color:rgba(226,75,74,.40)}
.flash{margin:0 0 18px;padding:12px 14px;border:1px solid #2d5678;border-radius:11px;background:rgba(74,174,242,.09);font-size:13px}.flash.error{background:var(--red-bg);border-color:#6b3a46;color:#ffc4c4}
.source-strip{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:10px 13px;border:1px solid var(--border);border-radius:12px;background:rgba(12,24,40,.72);margin-bottom:18px}.source-copy{font-size:12px;color:var(--muted)}.source-copy b{color:var(--text)}
/* Home */
.monitor-list{display:grid;gap:16px}.monitor-card{padding:20px}.monitor-card-top{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}.monitor-name{font-size:19px;font-weight:780;margin:0}.monitor-route{font-size:13px;color:var(--muted);margin-top:4px}.status-stack{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}.monitor-main{display:grid;grid-template-columns:minmax(220px,.9fr) minmax(0,1.6fr);gap:22px;margin-top:18px;align-items:end}.home-price-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}.home-price{font-size:34px;font-weight:800;letter-spacing:-.04em;margin-top:3px}.home-price-sub{font-size:12px;color:#bfd0e0}.home-summary{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.summary-cell{padding:11px 12px;background:#091727;border:1px solid #1c3650;border-radius:11px}.summary-cell .k{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}.summary-cell .v{font-size:13px;font-weight:700;margin-top:4px}.monitor-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px}.updated{margin-top:12px;font-size:11px;color:var(--muted)}
/* Hero */
.hero{padding:22px}.hero-top{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(250px,.6fr);gap:20px;align-items:start}.hero-label{font-size:11px;color:var(--muted);font-weight:650;text-transform:uppercase;letter-spacing:.06em}.hero-price{font-size:43px;font-weight:820;letter-spacing:-.05em;line-height:1.05;margin:5px 0 2px}.hero-per{font-size:13px;color:#bfd0df}.delta-pill{display:inline-flex;align-items:center;gap:5px;margin-top:9px;font-size:12px;font-weight:700;padding:5px 8px;border-radius:999px}.delta-pill.down{background:var(--green-bg);color:#86e7af}.delta-pill.up{background:var(--red-bg);color:#ffabab}.delta-pill.flat{background:#12283e;color:#a8bdd1}.hero-side{border-left:1px solid var(--border);padding-left:20px}.hero-side-row{display:flex;justify-content:space-between;gap:10px;padding:5px 0;font-size:12px}.hero-side-row span:first-child{color:var(--muted)}.chip-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px}.chip{display:inline-flex;align-items:center;gap:7px;padding:8px 11px;border:1px solid var(--border);background:#0a1828;border-radius:999px;font-size:12px;color:#cedae6}.hero-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px}.trust-box{margin-top:18px;padding:12px 14px;border-radius:11px;border:1px solid var(--border);display:flex;justify-content:space-between;gap:14px;align-items:center}.trust-box.good{background:var(--green-bg);border-color:rgba(82,209,135,.28)}.trust-box.warn{background:var(--amber-bg);border-color:rgba(246,184,77,.28)}.trust-title{font-size:12px;font-weight:800}.trust-text{font-size:11px;color:var(--muted);margin-top:2px}
/* Tabs */
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--border);margin:24px 0 18px;overflow-x:auto;scrollbar-width:thin}.tab{padding:11px 14px;color:var(--muted);text-decoration:none;font-size:13px;font-weight:700;border-bottom:2px solid transparent;white-space:nowrap}.tab:hover{color:var(--text)}.tab.active{color:var(--text);border-bottom-color:var(--blue)}
/* Offer cards */
.offer-toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}.control{background:#0a1828;color:var(--text);border:1px solid var(--border);border-radius:9px;padding:9px 11px;font:inherit;font-size:12px;min-height:38px}.control:focus{outline:none;border-color:var(--info);box-shadow:0 0 0 2px rgba(55,138,221,.12)}.filter-count{font-size:11px;color:var(--muted);margin-left:auto}.offers-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:12px}.offer-card{background:linear-gradient(180deg,#0f2034,#0b192a);border:1px solid var(--border);border-radius:14px;padding:13px;display:flex;flex-direction:column;min-height:0;transition:.18s ease}.offer-card:hover{border-color:#355c7d;transform:translateY(-1px);box-shadow:0 10px 26px rgba(0,0,0,.17)}.offer-card.best{border:2px solid rgba(39,176,103,.72);box-shadow:0 0 0 1px rgba(39,176,103,.08) inset}.offer-card-head{display:flex;justify-content:space-between;gap:9px;align-items:flex-start}.airline-name{font-size:14px;font-weight:790}.airline-code{font-size:10px;color:var(--muted);margin-top:1px}.offer-price-row{display:flex;justify-content:space-between;gap:10px;align-items:end;margin-top:8px}.offer-price{font-size:25px;font-weight:830;letter-spacing:-.035em;line-height:1}.offer-per{font-size:10px;color:var(--muted);margin-top:3px}.price-change{font-size:10px;font-weight:800;padding:4px 7px;border-radius:999px;white-space:nowrap}.price-change.down{background:var(--success-bg);color:#8ae8b6}.price-change.up{background:var(--critical-bg);color:#ffb0b0}.price-change.flat{background:var(--info-bg);color:#a7d7ff}.offer-facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin:10px 0}.fact{display:block;text-align:center;background:#091727;border:1px solid #1d3852;border-radius:8px;padding:7px 6px;font-size:10px;min-width:0}.fact-icon{display:block;width:auto;text-align:center;font-size:15px;line-height:1.1;margin-bottom:3px}.fact-label{display:none}.fact-value{display:block;color:var(--text);font-weight:700;line-height:1.25;overflow-wrap:anywhere}.offer-bottom{padding-top:8px;border-top:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;gap:8px}.variant{font-size:10px;color:var(--muted)}.offer-diff{font-size:10px;font-weight:760;margin-top:2px}.offer-diff.good{color:#8ae8b6}.offer-diff.warn{color:#ffd18d}.offer-buttons{display:flex;gap:7px;flex-wrap:wrap;margin-top:9px}.offer-buttons .btn.buy{flex:1 1 150px;min-height:42px}.offer-buttons .btn.secondary-detail{flex:0 0 auto}.offer-details{margin-top:9px;border-top:1px solid var(--border);padding-top:8px}.offer-details>summary{list-style:none;cursor:pointer;font-size:11px;font-weight:700;color:#b9d7ec;display:flex;justify-content:space-between;align-items:center}.offer-details>summary::-webkit-details-marker{display:none}.offer-details>summary::after{content:'›';font-size:17px;color:var(--info);transition:transform .15s}.offer-details[open]>summary::after{transform:rotate(90deg)}.offer-detail-body{padding-top:11px}.legs{display:grid;grid-template-columns:1fr 1fr;gap:10px}.leg{border:1px solid #1d3954;background:#071524;border-radius:11px;padding:11px}.leg-head{display:flex;justify-content:space-between;gap:8px;align-items:center}.leg-title{font-size:10px;text-transform:uppercase;color:var(--muted);font-weight:800}.route-line{font-size:14px;font-weight:780;margin-top:8px}.timing{font-size:11px;color:#bfd0df;margin:3px 0 6px}.leg-meta,.segment{font-size:11px;color:var(--muted)}.segments{margin-top:8px;padding-top:7px;border-top:1px solid #18314a}.segment{padding:4px 0}.segment strong{color:#e8f2fb}.layover{font-size:11px;color:#ffd48e;background:var(--warning-bg);border-left:2px solid var(--warning);padding:5px 7px;margin:3px 0}.baggage-box{margin-top:10px;border:1px solid #1d3954;background:#071524;border-radius:11px;padding:10px}.baggage-head{display:flex;justify-content:space-between;gap:8px;align-items:center}.baggage-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:8px}.baggage-leg{border:1px solid #19344f;border-radius:9px;padding:9px}.bag-row{display:flex;justify-content:space-between;gap:8px;font-size:11px;padding:3px 0}.bag-row span:first-child{color:var(--muted)}.bag-ok{color:#82e8b1}.bag-warn{color:#ffd18d}.bag-bad{color:#ffb2b2}.bag-unknown{color:var(--muted)}
.booking-compact{margin:8px 0 2px;padding:8px 9px;border:1px solid rgba(39,176,103,.26);background:rgba(39,176,103,.07);border-radius:9px;display:grid;gap:3px}.booking-compact .primary-line{font-size:11px;color:#9be6bd;font-weight:760}.booking-compact .secondary-line{font-size:10px;color:var(--muted)}.booking-box{margin-top:10px;border:1px solid #1d3954;background:#071524;border-radius:11px;padding:10px}.booking-head{display:flex;justify-content:space-between;gap:8px;align-items:center}.booking-main{margin-top:8px;display:grid;grid-template-columns:1.15fr .85fr;gap:8px}.booking-main-card{border:1px solid #19344f;border-radius:9px;padding:9px}.booking-main-card .k{font-size:9px;text-transform:uppercase;color:var(--muted);font-weight:800}.booking-main-card .p{font-size:18px;font-weight:820;margin-top:2px}.booking-main-card .seller{font-size:11px;color:#bcd1e2}.booking-options{margin-top:8px;display:grid;gap:5px}.booking-option{display:grid;grid-template-columns:1fr auto;gap:8px;padding:6px 7px;border:1px solid #17334d;border-radius:7px;font-size:10px}.booking-option .seller{font-weight:700}.booking-option .fare{color:var(--muted);font-size:9px}.booking-option .price{font-weight:800}.price-context{font-size:10px;color:var(--muted);margin-top:4px}.hero-bookable{margin-top:10px;display:inline-flex;gap:7px;align-items:center;padding:7px 9px;border-radius:9px;background:var(--success-bg);border:1px solid rgba(39,176,103,.3);font-size:11px;color:#a2ebc3}.hero-bookable b{color:#fff}
/* Details */
.metrics-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.metric{background:#091727;border:1px solid #1d3852;border-radius:12px;padding:13px}.metric .k{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}.metric .v{font-size:18px;font-weight:780;margin-top:4px}.metric .hint{font-size:10px;color:var(--muted);margin-top:2px}.coverage-complete{color:#82e6ad}.coverage-partial{color:#ffd28b}.coverage-bad{color:#ffaaaa}.airline-catalog{display:flex;gap:7px;flex-wrap:wrap}.airline-chip{border:1px solid #284963;background:#0a1828;border-radius:999px;padding:7px 10px;font-size:11px}.policy-card{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.policy-item{padding:11px;border-radius:10px;background:#091727;border:1px solid #1d3852}.policy-item .k{font-size:10px;color:var(--muted)}.policy-item .v{font-size:13px;font-weight:720;margin-top:4px}.audit-card{padding:17px}.audit-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.audit-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin-top:13px}.audit-note{margin-top:12px;padding:11px;border-radius:9px;background:#091727;border:1px solid #1d3852;font-size:12px;color:#bfd0df}
/* History */
.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.stat-card{padding:14px;background:#091727;border:1px solid #1d3852;border-radius:12px}.stat-card .label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}.stat-card .value{font-size:21px;font-weight:800;margin-top:4px}.stat-card .sub{font-size:10px;color:var(--muted);margin-top:3px}.periods{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0}.period{padding:7px 11px;border:1px solid var(--border);border-radius:8px;text-decoration:none;font-size:11px;font-weight:700;color:var(--muted)}.period.active{background:var(--blue);border-color:var(--blue);color:#061522}.chart-card{padding:16px}.chart{width:100%;height:280px;background:#071524;border:1px solid #1d3852;border-radius:11px;display:block}.insight{margin-top:12px;padding:12px;border-radius:10px;background:rgba(74,174,242,.09);border:1px solid rgba(74,174,242,.26);font-size:12px;color:#cce8fb}.timeline{display:grid;gap:9px}.timeline-item{display:grid;grid-template-columns:105px minmax(0,1fr) auto;gap:12px;align-items:center;padding:12px 13px;border-radius:10px;background:#091727;border-left:3px solid #496176}.timeline-item.down{border-left-color:var(--success)}.timeline-item.up{border-left-color:var(--critical)}.timeline-item.neutral{border-left-color:#607b93}.timeline-date{font-size:10px;color:var(--muted)}.timeline-title{font-size:12px;font-weight:750}.timeline-desc{font-size:11px;color:var(--muted);margin-top:2px}.timeline-change{font-size:12px;font-weight:800}.timeline-change.down{color:#82e8b1}.timeline-change.up{color:#ffb2b2}.timeline-more{margin-top:3px;border:1px solid var(--border);border-radius:10px;background:#081625}.timeline-more>summary{list-style:none;cursor:pointer;padding:10px 12px;font-size:11px;font-weight:750;color:#b9d7ec}.timeline-more>summary::-webkit-details-marker{display:none}.timeline-more .timeline{padding:0 8px 8px}.raw-details{margin-top:16px;border:1px solid var(--border);border-radius:11px;overflow:hidden}.raw-details>summary{list-style:none;cursor:pointer;padding:12px 14px;font-size:12px;font-weight:750}.raw-details>summary::-webkit-details-marker{display:none}.table-wrap{overflow:auto}.table{width:100%;border-collapse:collapse;min-width:840px}.table th,.table td{padding:9px;border-top:1px solid var(--border);font-size:11px;text-align:left;vertical-align:top}.table th{color:var(--muted);text-transform:uppercase;font-size:9px;letter-spacing:.04em}
/* Forms */
.form-shell{max-width:760px;margin:0 auto}.form-card{padding:22px}.form-section{padding:4px 0 20px;border-bottom:1px solid var(--border);margin-bottom:18px}.form-section:last-of-type{border-bottom:0}.form-section h2{font-size:15px;margin:0 0 3px}.form-section p{font-size:11px;color:var(--muted);margin:0 0 13px}.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.form-group{margin-top:12px}.form-label{display:block;font-size:12px;font-weight:700;margin-bottom:6px}.form-help{font-size:10px;color:var(--muted);margin-top:5px}.input{width:100%;background:#081625;border:1px solid var(--border);color:var(--text);padding:11px 12px;border-radius:9px;font:inherit;font-size:13px}.input:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 2px rgba(74,174,242,.12)}.route-lock{display:grid;grid-template-columns:1fr auto 1fr;gap:10px;align-items:center}.airport-box{padding:12px;border:1px solid #27465f;background:#091727;border-radius:10px}.airport-code{font-size:17px;font-weight:800}.airport-name{font-size:10px;color:var(--muted)}.route-arrow{color:var(--blue);font-size:20px}.choice-group{display:flex;gap:7px;flex-wrap:wrap}.choice{border:1px solid var(--border);background:#091727;color:var(--text);padding:8px 11px;border-radius:8px;font-size:11px;font-weight:700;cursor:pointer}.choice.active{background:var(--blue);border-color:var(--blue);color:#061522}.readonly-choices .choice{cursor:default}.toggle-row{display:flex;justify-content:space-between;gap:14px;align-items:center;padding:12px;border:1px solid #1d3852;background:#091727;border-radius:10px}.toggle-copy b{font-size:12px}.toggle-copy div{font-size:10px;color:var(--muted);margin-top:2px}.switch{position:relative;width:42px;height:24px}.switch input{opacity:0;width:0;height:0}.slider{position:absolute;inset:0;border-radius:999px;background:#324b63;cursor:pointer;transition:.18s}.slider:before{content:'';position:absolute;width:18px;height:18px;left:3px;top:3px;border-radius:50%;background:#fff;transition:.18s}.switch input:checked+.slider{background:var(--blue)}.switch input:checked+.slider:before{transform:translateX(18px)}.danger-zone{margin-top:18px;padding:14px;border:1px solid #633b46;background:var(--red-bg);border-radius:11px}.danger-zone h3{margin:0;font-size:13px;color:#ffb1b1}.danger-zone p{font-size:10px;color:#d69fa8}.form-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:20px}
/* Loading */
.loading-overlay{display:none;position:fixed;inset:0;background:rgba(3,10,18,.84);backdrop-filter:blur(4px);z-index:9999;align-items:center;justify-content:center;padding:20px}.loading-overlay.show{display:flex}.loading-card{width:min(480px,94vw);background:#0d1d30;border:1px solid #2e5676;border-radius:18px;padding:27px;text-align:center;box-shadow:0 22px 70px rgba(0,0,0,.45)}.spinner{width:44px;height:44px;border:4px solid #29435f;border-top-color:var(--blue);border-radius:50%;margin:0 auto 15px;animation:spin .8s linear infinite}.loading-title{font-size:18px;font-weight:800}.loading-sub{font-size:12px;color:var(--muted);margin-top:5px}.loading-time{font-size:12px;color:#d6ecfb;margin-top:11px;font-weight:700}@keyframes spin{to{transform:rotate(360deg)}}
.toast{position:fixed;right:18px;bottom:18px;background:#122840;border:1px solid #315676;padding:10px 13px;border-radius:9px;font-size:12px;opacity:0;pointer-events:none;transform:translateY(8px);transition:.2s;z-index:10000}.toast.show{opacity:1;transform:none}
.footer{margin-top:34px;padding-top:18px;border-top:1px solid rgba(33,58,84,.55);font-size:10px;color:#627c93}
@media(max-width:900px){.hero-top{grid-template-columns:1fr}.hero-side{border-left:0;border-top:1px solid var(--border);padding:13px 0 0}.metrics-grid,.stats-grid,.audit-grid,.policy-card{grid-template-columns:repeat(2,1fr)}.legs,.baggage-grid{grid-template-columns:1fr}.monitor-main{grid-template-columns:1fr}.home-summary{grid-template-columns:repeat(3,1fr)}}
@media(max-width:650px){.app{padding:14px 12px 46px}.appbar{align-items:flex-start}.brand-sub{display:none}.nav-actions .btn span.label{display:none}.page-title{font-size:25px}.hero{padding:17px}.hero-price{font-size:36px}.hero-actions>.btn,.hero-actions>form{flex:1 1 100%}.hero-actions form .btn{width:100%}.chip-row{gap:6px}.chip{font-size:11px;padding:7px 9px}.offers-grid{grid-template-columns:1fr}.metrics-grid,.stats-grid,.policy-card,.audit-grid{grid-template-columns:1fr 1fr}.form-row{grid-template-columns:1fr}.route-lock{grid-template-columns:1fr}.route-arrow{transform:rotate(90deg);text-align:center}.timeline-item{grid-template-columns:1fr auto}.timeline-date{grid-column:1/-1}.monitor-card-top{flex-direction:column}.status-stack{justify-content:flex-start}.home-summary{grid-template-columns:1fr 1fr}.offer-toolbar .control{flex:1 1 45%}.filter-count{width:100%;margin:0}.source-strip{align-items:flex-start}.appbar .btn.primary{padding:9px 10px}.form-actions .btn{flex:1 1 100%}}
@media(max-width:420px){.metrics-grid,.stats-grid,.policy-card,.audit-grid,.home-summary{grid-template-columns:1fr}.offer-toolbar .control{flex:1 1 100%}.hero-price{font-size:33px}.appbar{gap:8px}.brand-mark{width:34px;height:34px}}
"""


# ---------------------------------------------------------------------------
# Generic formatting / provider state
# ---------------------------------------------------------------------------

def _fmt_date(raw: str) -> str:
    """All visible dates use DD/MM/YYYY; internal/API values remain ISO."""
    if not raw:
        return "—"
    try:
        return date.fromisoformat(raw).strftime("%d/%m/%Y")
    except Exception:
        return raw


def _parse_ui_date(raw: str) -> str:
    raw = (raw or "").strip()
    try:
        return datetime.strptime(raw, "%d/%m/%Y").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"Fecha inválida '{raw}'. Usar DD/MM/AAAA (ej.: 10/12/2026).") from exc


def _fmt_dt(raw: str) -> str:
    if not raw:
        return "—"
    try:
        return datetime.fromisoformat(raw).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(raw).replace("T", " ")[:16]


def _money(currency: str, value) -> str:
    try:
        return f"{currency} {float(value):,.2f}"
    except Exception:
        return f"{currency} {value}"


def _provider_info_from_cfg(cfg) -> tuple[str, str, str]:
    provider = (getattr(cfg, "PRICE_PROVIDER", "unknown") or "unknown").lower()
    if provider == "serpapi":
        if getattr(cfg, "SERPAPI_API_KEY", ""):
            return "Google Flights · SerpApi", "REAL", "real"
        return "Google Flights · SerpApi", "FALTA API KEY", "sandbox"
    if provider == "mock":
        return "Datos sintéticos", "SIMULACIÓN — NO ES PRECIO REAL", "simulation"
    if provider == "duffel":
        token = getattr(cfg, "DUFFEL_API_TOKEN", "") or ""
        if token.startswith("duffel_live_"):
            return "Duffel", "REAL", "real"
        return "Duffel Test Mode", "SANDBOX — NO ES PRECIO REAL", "sandbox"
    if provider == "amadeus":
        if (getattr(cfg, "AMADEUS_ENV", "test") or "test").lower() == "production":
            return "Amadeus", "REAL", "real"
        return "Amadeus Test", "SANDBOX — NO ES PRECIO REAL", "sandbox"
    if provider == "kiwi":
        return "Kiwi", "REAL", "real"
    return provider or "Desconocido", "FUENTE DESCONOCIDA", "sandbox"


def _snapshot_provider(s) -> tuple[str, str, str]:
    if not s:
        return "—", "SIN CORRIDAS", "sandbox"
    provider = (s["provider"] if "provider" in s.keys() else "unknown") or "unknown"
    mode = (s["provider_mode"] if "provider_mode" in s.keys() else "unknown") or "unknown"
    names = {"serpapi":"Google Flights · SerpApi","mock":"Mock","duffel":"Duffel","amadeus":"Amadeus","kiwi":"Kiwi"}
    mode_labels = {"real":"REAL","simulation":"SIMULACIÓN","sandbox":"SANDBOX","unknown":"FUENTE NO REGISTRADA"}
    css = "real" if mode == "real" else ("simulation" if mode == "simulation" else "sandbox")
    return names.get(provider, provider), mode_labels.get(mode, mode.upper()), css


def _runtime_html(cfg) -> str:
    if cfg is None:
        return ""
    name, mode, css = _provider_info_from_cfg(cfg)
    return f'''<div class="source-strip"><div class="source-copy"><b>Fuente:</b> {esc(name)} · EZE/AEP → MIA · directo o 1 escala ≤ 2 h</div><span class="badge {css}">{esc(mode)}</span></div>'''


def page(title: str, body: str, *, cfg=None, notice: str = "", error: bool = False, show_new: bool = True) -> str:
    notice_html = f'<div class="flash {"error" if error else ""}">{esc(notice)}</div>' if notice else ""
    new_btn = '<a class="btn primary" href="/monitors/new"><span class="icon">＋</span><span class="label">Nuevo monitor</span></a>' if show_new else ""
    runtime = _runtime_html(cfg)
    return f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>{esc(title)}</title><style>{CSS}</style></head><body><main class="app">
    <header class="appbar"><a class="brand" href="/"><span class="brand-mark">✈</span><span><span class="brand-title">Flight Sentinel</span><span class="brand-sub">Buenos Aires → Miami · precios de vuelos</span></span></a><nav class="nav-actions"><a class="btn ghost" href="/"><span class="icon">⌂</span><span class="label">Monitores</span></a>{new_btn}</nav></header>
    {runtime}{notice_html}{body}
    <footer class="footer">Flight Sentinel V2 · Los precios y condiciones de equipaje deben confirmarse en el proveedor antes de comprar. Cobertura Google completa no equivale a mercado exhaustivo.</footer>
    </main>
    <div id="loadingOverlay" class="loading-overlay" aria-live="polite"><div class="loading-card"><div class="spinner"></div><div id="loadingTitle" class="loading-title">Actualizando precios…</div><div class="loading-sub">Procesando búsqueda en segundo plano. La pantalla se actualizará cuando finalice.</div><div id="loadingTime" class="loading-time">0 s</div></div></div>
    <div id="toast" class="toast">Enlace copiado</div>
    <script>{JS}</script></body></html>'''


JS = r"""
(function(){
  const overlay=document.getElementById('loadingOverlay'), loadingTitle=document.getElementById('loadingTitle'), loadingTime=document.getElementById('loadingTime');
  document.querySelectorAll('form.js-long-action').forEach(form=>form.addEventListener('submit',(ev)=>{
    if(!form.checkValidity()) return;
    if(form.dataset.submitting==='1'){ev.preventDefault();return;}
    form.dataset.submitting='1';
    document.querySelectorAll('form.js-long-action button').forEach(b=>b.disabled=true);
    const btn=form.querySelector('button[type="submit"],button:not([type])'); if(btn) btn.textContent='Procesando…';
    loadingTitle.textContent=form.dataset.loadingText||'Actualizando precios…'; overlay.classList.add('show'); document.body.setAttribute('aria-busy','true');
    let sec=0; loadingTime.textContent='0 s'; window.setInterval(()=>{sec++;loadingTime.textContent=sec+' s transcurridos';},1000);
  }));
  document.querySelectorAll('input.date-input').forEach(inp=>inp.addEventListener('input',()=>{
    const d=inp.value.replace(/\D/g,'').slice(0,8); let out=d.slice(0,2); if(d.length>2)out+='/'+d.slice(2,4); if(d.length>4)out+='/'+d.slice(4); inp.value=out;
  }));
  document.querySelectorAll('[data-choice-group]').forEach(group=>{
    const target=document.getElementById(group.dataset.choiceGroup); if(!target)return;
    group.querySelectorAll('[data-value]').forEach(btn=>btn.addEventListener('click',()=>{
      target.value=btn.dataset.value; group.querySelectorAll('[data-value]').forEach(x=>x.classList.toggle('active',x===btn));
    }));
  });
  const grid=document.getElementById('offersGrid');
  if(grid){
    const airline=document.getElementById('airlineFilter'), stops=document.getElementById('stopsFilter'), sort=document.getElementById('sortOffers'), count=document.getElementById('filterCount');
    const apply=()=>{
      let cards=[...grid.querySelectorAll('.offer-card[data-filterable="1"]')];
      const ac=(airline&&airline.value)||'all', sc=(stops&&stops.value)||'all';
      cards.forEach(c=>{const okA=ac==='all'||c.dataset.airlines.split('|').includes(ac); const okS=sc==='all'||c.dataset.stops===sc; c.style.display=(okA&&okS)?'flex':'none';});
      if(sort){cards.sort((a,b)=>{if(sort.value==='duration')return Number(a.dataset.duration)-Number(b.dataset.duration);if(sort.value==='difference')return Number(a.dataset.price)-Number(b.dataset.price);return Number(a.dataset.price)-Number(b.dataset.price);});cards.forEach(c=>grid.appendChild(c));}
      const visible=cards.filter(c=>c.style.display!=='none').length; if(count)count.textContent=visible+' oferta(s) visibles';
    };
    [airline,stops,sort].forEach(el=>el&&el.addEventListener('change',apply)); apply();
  }
  document.querySelectorAll('[data-offer-details]').forEach(btn=>btn.addEventListener('click',()=>{
    const card=btn.closest('.offer-card'), details=card&&card.querySelector('details.offer-details'); if(!details)return; details.open=!details.open; btn.textContent=details.open?'Ocultar detalles':'Detalles'; if(details.open) details.scrollIntoView({behavior:'smooth',block:'nearest'});
  }));
  document.querySelectorAll('[data-share]').forEach(btn=>btn.addEventListener('click',async()=>{
    const url=window.location.href; const title=document.title; try{if(navigator.share){await navigator.share({title,url});return;} await navigator.clipboard.writeText(url); const t=document.getElementById('toast');t.textContent='Enlace copiado';t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1800);}catch(e){}
  }));
})();
"""


# ---------------------------------------------------------------------------
# Offer helpers
# ---------------------------------------------------------------------------

def _all_airline_codes(rows: list[dict]) -> set[str]:
    codes: set[str] = set()
    for row in rows:
        offer = row.get("offer") or {}
        codes.update(str(x) for x in (offer.get("airlines") or []) if x)
    return codes


def _airline_name_map(rows: list[dict]) -> dict[str, str]:
    names: dict[str, str] = {}
    for row in rows:
        offer = row.get("offer") or {}
        codes = [str(x) for x in (offer.get("airlines") or [])]
        display = (offer.get("presentation") or {}).get("airlines") or []
        for code, name in zip(codes, display):
            if code and name:
                names.setdefault(code, str(name))
        for leg_name in ("outbound", "inbound"):
            for seg in (offer.get(leg_name) or {}).get("segments") or []:
                code = str(seg.get("carrier") or "").upper()
                name = str(seg.get("airline_name") or "").strip()
                if code and name:
                    names[code] = name
    for code in _all_airline_codes(rows):
        names.setdefault(code, airline_info(code).get("name") or code)
    return names


def _pax_count(offer: dict, snapshot=None) -> int:
    adults = int(offer.get("adults") or (snapshot["adults"] if snapshot else 0) or 0)
    children = int(offer.get("children") or (snapshot["children"] if snapshot else 0) or 0)
    return max(1, adults + children)


def _airlines(offer: dict) -> tuple[list[str], list[str]]:
    codes = [str(x) for x in (offer.get("airlines") or [])]
    names = (offer.get("presentation") or {}).get("airlines") or normalize_airlines(codes)
    return codes, names


def _stop_label(leg: dict) -> tuple[str, str]:
    stops = int((leg or {}).get("stops") or 0)
    if stops == 0:
        return "DIRECTO", "direct"
    lays = (leg or {}).get("layovers") or []
    if lays:
        lv = lays[0]
        return f'1 ESCALA · {lv.get("airport","?")} {lv.get("duration","")}', "stop"
    return "1 ESCALA", "stop"


def _leg_summary(leg: dict) -> str:
    label, _ = _stop_label(leg or {})
    return label.title() if label == "DIRECTO" else label.replace("ESCALA", "escala")


def _render_leg(leg: dict, title: str) -> str:
    label, badge_class = _stop_label(leg)
    origin = leg.get("origin") or "—"; dest = leg.get("destination") or "—"
    segments = leg.get("segments") or []; layovers = leg.get("layovers") or []
    seg_html = []
    for i, seg in enumerate(segments):
        carrier = seg.get("carrier", "")
        airline = (seg.get("airline_name") or normalize_airline(carrier)) if carrier else (seg.get("airline_name") or "")
        flight = seg.get("flight") or carrier or "Vuelo"
        seg_html.append(f'<div class="segment"><strong>{esc(flight)}</strong> · {esc(airline)} · {esc(seg.get("from","—"))} {_fmt_dt(seg.get("dep",""))} → {esc(seg.get("to","—"))} {_fmt_dt(seg.get("arr",""))} · {esc(seg.get("duration",""))}</div>')
        if i < len(layovers):
            lv = layovers[i]
            seg_html.append(f'<div class="layover">Escala en <b>{esc(lv.get("airport","—"))}</b> · {esc(lv.get("duration","—"))}</div>')
    return f'''<div class="leg"><div class="leg-head"><div class="leg-title">{esc(title)}</div><span class="badge {badge_class}">{esc(label)}</span></div><div class="route-line">{esc(origin)} → {esc(dest)}</div><div class="timing">{_fmt_dt(leg.get("departure",""))} → {_fmt_dt(leg.get("arrival",""))}</div><div class="leg-meta">Duración total: {esc(leg.get("duration","—"))}</div><div class="segments">{''.join(seg_html) if seg_html else '<div class="segment">Sin detalle de segmentos.</div>'}</div></div>'''


def _bag_value(info: dict, key: str, currency: str) -> tuple[str, str]:
    value = str((info or {}).get(key) or "UNKNOWN").upper()
    labels = {"INCLUDED": ("Incluido", "bag-ok"),"PAID": ("Con cargo", "bag-warn"),"NOT_INCLUDED": ("No incluido", "bag-bad"),"UNKNOWN": ("No informado", "bag-unknown")}
    text, css = labels.get(value, (value, "bag-unknown"))
    if key == "checked_bag" and value == "PAID":
        if (info or {}).get("checked_bag_price") is not None:
            text = f'Con cargo · {_money(currency, (info or {}).get("checked_bag_price"))}'
        else:
            text = 'Con cargo · importe no informado'
    return text, css


def _render_baggage_leg(info: dict, title: str, currency: str) -> str:
    p, pc = _bag_value(info, "personal_item", currency); c, cc = _bag_value(info, "carry_on", currency); b, bc = _bag_value(info, "checked_bag", currency)
    raw = [str(x) for x in ((info or {}).get("raw") or []) if str(x).strip()]
    raw_html = f'<div class="mini muted" style="margin-top:5px">Google: {esc(" · ".join(raw))}</div>' if raw else ''
    return f'''<div class="baggage-leg"><div class="leg-title">{esc(title)}</div><div class="bag-row"><span>Artículo personal</span><b class="{pc}">{esc(p)}</b></div><div class="bag-row"><span>Carry-on / cabina</span><b class="{cc}">{esc(c)}</b></div><div class="bag-row"><span>Equipaje en bodega</span><b class="{bc}">{esc(b)}</b></div>{raw_html}</div>'''


def _render_baggage(offer: dict, currency: str) -> str:
    bag = offer.get("baggage") or {"status": "NOT_CHECKED"}
    status = str(bag.get("status") or "NOT_CHECKED").upper()
    labels = {"VERIFIED": ("VERIFICADO", "ok"),"PARTIAL": ("PARCIAL", "warn"),"UNAVAILABLE": ("NO INFORMADO", "warn"),"ERROR": ("ERROR", "bad"),"NOT_CHECKED": ("NO CONSULTADO", "warn")}
    label, badge = labels.get(status, (status, "warn"))
    note = bag.get("note") or ""
    return f'''<div class="baggage-box"><div class="baggage-head"><div><b>Equipaje de esta tarifa</b><div class="mini muted">{esc(bag.get("source") or "Google Flights")}</div></div><span class="badge {badge}">{esc(label)}</span></div><div class="baggage-grid">{_render_baggage_leg(bag.get("outbound") or {}, "IDA", currency)}{_render_baggage_leg(bag.get("inbound") or {}, "VUELTA", currency)}</div>{f'<div class="mini muted" style="margin-top:7px">{esc(note)}</div>' if note else ''}</div>'''


def _booking_info(offer: dict) -> dict:
    booking = (offer or {}).get("booking") or {}
    status = str(booking.get("status") or "NOT_CHECKED").upper()
    lowest = booking.get("lowest") if isinstance(booking.get("lowest"), dict) else None
    direct = booking.get("airline_direct") if isinstance(booking.get("airline_direct"), dict) else None
    options = [x for x in (booking.get("options") or []) if isinstance(x, dict)]
    return {"raw": booking, "status": status, "lowest": lowest, "direct": direct, "options": options}


def _render_booking(offer: dict, currency: str) -> str:
    info = _booking_info(offer)
    if info["status"] != "VERIFIED" or not info["lowest"]:
        note = (info["raw"].get("note") or "Precio reservable todavía no verificado para esta oferta.")
        return f'<div class="booking-box"><div class="booking-head"><b>Opciones de reserva</b><span class="badge warn">NO VERIFICADAS</span></div><div class="mini muted" style="margin-top:7px">{esc(note)}</div></div>'
    low = info["lowest"]; direct = info["direct"]; opts = info["options"]
    direct_html = '<div class="k">Directo con aerolínea</div><div class="p">No informado</div><div class="seller">Google no devolvió una opción directa estructurada.</div>'
    if direct:
        direct_html = f'<div class="k">Directo con aerolínea</div><div class="p">{_money(currency,direct.get("price"))}</div><div class="seller">{esc(direct.get("seller") or "Aerolínea")}{(" · "+esc(direct.get("fare"))) if direct.get("fare") else ""}</div>'
    rows=[]
    for o in opts[:6]:
        fare=f'<div class="fare">{esc(o.get("fare"))}</div>' if o.get("fare") else ''
        kind=' · aerolínea' if o.get('is_airline') else ''
        rows.append(f'<div class="booking-option"><div><div class="seller">{esc(o.get("seller") or "Vendedor")}{kind}</div>{fare}</div><div class="price">{_money(currency,o.get("price"))}</div></div>')
    search_price = info["raw"].get("search_price")
    context = f'Precio observado originalmente en la búsqueda: {_money(currency,search_price)}.' if search_price is not None else ''
    return f'''<div class="booking-box"><div class="booking-head"><div><b>Opciones de reserva del itinerario exacto</b><div class="mini muted">Google Flights · Booking Options</div></div><span class="badge ok">VERIFICADO</span></div><div class="booking-main"><div class="booking-main-card"><div class="k">Precio reservable más bajo</div><div class="p">{_money(currency,low.get("price"))}</div><div class="seller">{esc(low.get("seller") or "Vendedor")}{(" · "+esc(low.get("fare"))) if low.get("fare") else ""}</div></div><div class="booking-main-card">{direct_html}</div></div><div class="price-context">{esc(context)}</div><div class="booking-options">{''.join(rows)}</div></div>'''


def _compact_baggage_summary(offer: dict, currency: str) -> tuple[str, str, str]:
    bag = offer.get("baggage") or {}; status = str(bag.get("status") or "NOT_CHECKED").upper(); out = bag.get("outbound") or {}; inn = bag.get("inbound") or {}
    def combine(key: str) -> str:
        a = _bag_value(out, key, currency)[0]; b = _bag_value(inn, key, currency)[0]
        return a if a == b else f"Ida: {a} · Vuelta: {b}"
    status_label = {"VERIFIED":"Verificado","PARTIAL":"Parcial","UNAVAILABLE":"No informado","ERROR":"Error","NOT_CHECKED":"No consultado"}.get(status,status.title())
    return combine("carry_on"), combine("checked_bag"), status_label


def _offer_summary_values(row: dict, snapshot, best_price: float | None = None) -> dict:
    offer = row.get("offer") or {}; currency = offer.get("currency") or row.get("currency") or snapshot["currency"]
    observed_total = offer.get("total_price", row.get("price")); pax = _pax_count(offer, snapshot)
    booking = _booking_info(offer); lowest = booking.get("lowest")
    bookable_total = float(lowest.get("price")) if booking.get("status") == "VERIFIED" and lowest and lowest.get("price") is not None else None
    display_total = bookable_total if bookable_total is not None else observed_total
    per = float(display_total) / pax if display_total is not None else 0
    codes, names = _airlines(offer); airline_text = " + ".join(names) if names else "Aerolínea no informada"
    outbound = offer.get("outbound") or {}; inbound = offer.get("inbound") or {}; carry, checked, bag_status = _compact_baggage_summary(offer, currency)
    delta = pct = None
    if best_price is not None and observed_total is not None and float(best_price) > 0:
        delta = float(observed_total)-float(best_price); pct=delta/float(best_price)*100
    duration_min = int(row.get("total_duration_min") or 0)
    return {"offer":offer,"currency":currency,"total":observed_total,"display_total":display_total,"bookable_total":bookable_total,"pax":pax,"per":per,"codes":codes,"airline":airline_text,"outbound":outbound,"inbound":inbound,"carry":carry,"checked":checked,"bag_status":bag_status,"booking":booking,"delta":delta,"pct":pct,"duration_min":duration_min}


def _offer_group_key(row: dict) -> tuple[str, ...]:
    codes, _ = _airlines(row.get("offer") or {})
    return tuple(codes) or ("UNKNOWN",)


def _variant_counts(all_offers: list[dict]) -> dict[tuple[str, ...], int]:
    counts: dict[tuple[str, ...], int] = {}
    for r in all_offers:
        key = _offer_group_key(r); counts[key] = counts.get(key,0)+1
    return counts


def _previous_best_by_group(rows: list[dict]) -> dict[tuple[str, ...], float]:
    out: dict[tuple[str, ...], float] = {}
    for r in rows:
        try:
            price = float((r.get("offer") or {}).get("total_price", r.get("price")))
        except (TypeError, ValueError):
            continue
        key = _offer_group_key(r)
        if key not in out or price < out[key]:
            out[key] = price
    return out


def _select_highlights(all_offers: list[dict], max_items: int = 8) -> list[tuple[dict, str]]:
    """Best overall plus rows needed to represent every carrier detected."""
    if not all_offers:
        return []
    selected: list[tuple[dict, str]] = [(all_offers[0], "MEJOR PRECIO")]
    first_codes, _ = _airlines(all_offers[0].get("offer") or {}); covered=set(first_codes)
    for row in all_offers[1:]:
        codes, _ = _airlines(row.get("offer") or {}); new=[c for c in codes if c not in covered]
        if not new: continue
        selected.append((row, "MEJOR ITINERARIO DE " + " + ".join(airline_info(c).get("name") or c for c in new))); covered.update(codes)
        if len(selected)>=max_items: break
    return selected


def _render_offer_card(row: dict, snapshot, best_price: float, label: str, variants: int, previous_price: float | None = None) -> str:
    v=_offer_summary_values(row,snapshot,best_price); offer=v["offer"]; codes=v["codes"]; links=offer.get("links") or {}; best=(row.get("rank")==1)
    ob_text=_leg_summary(v["outbound"]); ib_text=_leg_summary(v["inbound"]); stop_kind="direct" if int(v["outbound"].get("stops") or 0)==0 and int(v["inbound"].get("stops") or 0)==0 else "stop"
    diff = "Mejor precio observado" if best else f'Observado +{_money(v["currency"],v["delta"] or 0)} · +{(v["pct"] or 0):.1f}% vs. mejor observado'
    diff_css="good" if best else "warn"
    offer_id = int(row.get("id") or 0)
    booking_known = str(((offer.get("booking") or {}).get("status") or "")).upper() == "VERIFIED"
    google_label = "↗ Ver oferta en Google" if booking_known else "↗ Ver oferta en Google · verificar precio"
    google = f'<a class="btn buy small" target="_blank" rel="noopener" href="/offers/{offer_id}/google">{google_label}</a>' if offer_id else ''
    airline_link = f'<a class="btn small ghost" target="_blank" rel="noopener" href="{esc(links.get("airline"))}">Web aerolínea</a>' if links.get("airline") else ''
    if previous_price is None or v["total"] is None:
        price_change='<span class="price-change flat" title="No hay una oferta comparable en la corrida anterior">Nueva / sin comparación</span>'
    else:
        pd=float(v["total"])-float(previous_price)
        if pd < -.01: price_change=f'<span class="price-change down" title="Vs. mejor oferta comparable de la corrida anterior · precio observado">↓ {_money(v["currency"],abs(pd))}</span>'
        elif pd > .01: price_change=f'<span class="price-change up" title="Vs. mejor oferta comparable de la corrida anterior · precio observado">↑ {_money(v["currency"],pd)}</span>'
        else: price_change='<span class="price-change flat" title="Vs. mejor oferta comparable de la corrida anterior · precio observado">Sin cambios</span>'
    duration = offer.get("total_duration") or (f'{v["duration_min"]//60}h {v["duration_min"]%60:02d}m' if v["duration_min"] else '—')
    bag_css='ok' if v['bag_status']=='Verificado' else 'warn'; data_airlines='|'.join(codes)
    booking=v['booking']; lowest=booking.get('lowest'); direct=booking.get('direct')
    if v['bookable_total'] is not None and lowest:
        price_label='Precio reservable verificado'
        compact=f'<div class="booking-compact"><div class="primary-line">Reservable con {esc(lowest.get("seller") or "vendedor")}</div><div class="secondary-line">Precio observado en búsqueda: {_money(v["currency"],v["total"])}{(" · Directo aerolínea: "+_money(v["currency"],direct.get("price"))) if direct and direct.get("price") is not None else ""}</div></div>'
    else:
        price_label='Precio observado en búsqueda'
        compact='<div class="price-context">Precio reservable todavía no verificado para esta oferta.</div>'
    badge_label='MEJOR PRECIO OBSERVADO' if best else label
    return f'''<article class="offer-card {'best' if best else ''}" data-filterable="1" data-airlines="{esc(data_airlines)}" data-stops="{stop_kind}" data-price="{float(v['display_total'] or 0):.2f}" data-duration="{v['duration_min']}">
      <div class="offer-card-head"><div><div class="airline-name">{esc(v['airline'])}</div><div class="airline-code">{esc(' · '.join(codes) if codes else 'Código no informado')}</div></div><span class="badge {'success' if best else 'info'}">{esc(badge_label)}</span></div>
      <div class="offer-price-row"><div><div class="mini muted" style="text-transform:uppercase;font-weight:800">{esc(price_label)}</div><div class="offer-price">{_money(v['currency'],v['display_total'])}</div><div class="offer-per">{_money(v['currency'],v['per'])} promedio por pasajero · {v['pax']} pasajero(s)</div></div>{price_change}</div>{compact}
      <div class="offer-facts"><div class="fact"><span class="fact-icon">✈</span><span class="fact-label">Vuelo:</span><span class="fact-value">{esc(ob_text)} / {esc(ib_text)}</span></div><div class="fact"><span class="fact-icon">💼</span><span class="fact-label">Carry-on:</span><span class="fact-value">{esc(v['carry'])}</span></div><div class="fact"><span class="fact-icon">🧳</span><span class="fact-label">Bodega:</span><span class="fact-value">{esc(v['checked'])}</span></div></div>
      <div class="offer-bottom"><div><div class="variant">{variants} variante(s) · {esc(duration)}</div><div class="offer-diff {diff_css}">{esc(diff)}</div></div><span class="badge {bag_css}">Equipaje {esc(v['bag_status'])}</span></div>
      <div class="offer-buttons">{google}<button type="button" class="btn small ghost secondary-detail" data-offer-details>Detalles</button>{airline_link}</div>
      <details class="offer-details {'best' if best else ''}"><summary>Ver itinerario y equipaje completo</summary><div class="offer-detail-body"><div class="legs">{_render_leg(v['outbound'],'IDA')}{_render_leg(v['inbound'],'VUELTA')}</div>{_render_booking(offer,v['currency'])}{_render_baggage(offer,v['currency'])}<div class="mini muted" style="margin-top:8px">El precio observado se usa para histórico y alertas comparables. El precio reservable proviene de Booking Options del itinerario exacto y puede variar por vendedor.</div></div></details>
    </article>'''


# ---------------------------------------------------------------------------
# Coverage / audit
# ---------------------------------------------------------------------------

def _coverage_values(snapshot) -> tuple[str,int,int,int]:
    if not snapshot: return "UNKNOWN",0,0,0
    keys=snapshot.keys(); return ((snapshot["coverage_status"] if "coverage_status" in keys else "UNKNOWN") or "UNKNOWN",int(snapshot["outbound_detected"] if "outbound_detected" in keys else 0),int(snapshot["outbound_explored"] if "outbound_explored" in keys else 0),int(snapshot["queries_used"] if "queries_used" in keys else 0))


def _market_audit_values(snapshot):
    if not snapshot: return "NOT_RUN",{},{}
    keys=snapshot.keys(); status=(snapshot["market_coverage_status"] if "market_coverage_status" in keys else "NOT_RUN") or "NOT_RUN"; audit=get_market_audit(snapshot); return status,audit.get("reconciliation") or {},audit.get("despegar") or {}


def _market_label(status: str) -> tuple[str,str]:
    labels={"CONSISTENT":("CONSISTENTE","coverage-complete"),"DISCREPANCY":("DISCREPANCIA","coverage-bad"),"PARTIAL":("PARCIAL","coverage-partial"),"AUDIT_BLOCKED":("NO VERIFICADA · BLOQUEADA","coverage-partial"),"AUDIT_FAILED":("NO VERIFICADA","coverage-partial"),"AUDIT_INCONCLUSIVE":("NO VERIFICADA","coverage-partial"),"AUDITOR_NOT_INSTALLED":("NO VERIFICADA","coverage-partial"),"NOT_RUN":("NO VERIFICADA","")}
    return labels.get(status,(status or "NO VERIFICADA",""))


def _render_market_audit(snapshot) -> str:
    status,rec,d=_market_audit_values(snapshot); label,css=_market_label(status); ga=rec.get("google_airlines") or []; da=rec.get("despegar_airlines") or []; missing=rec.get("missing_in_google") or []; missing_direct=rec.get("missing_direct_in_google") or []; unresolved=rec.get("unresolved_one_stop") or []; cards=int(d.get("cards_seen") or 0); direct=int(d.get("direct_cards") or 0); one=int(d.get("one_stop_cards") or 0); dstatus=d.get("status") or ("NOT_RUN" if status=="NOT_RUN" else "—"); note=rec.get("note") or d.get("warning") or ""
    if status=="NOT_RUN" and not d:
        note="Auditoría independiente no ejecutada. No es posible determinar todavía si existen itinerarios válidos fuera de Google Flights."
    elif not da and status in {"AUDIT_BLOCKED","AUDIT_FAILED","AUDIT_INCONCLUSIVE","PARTIAL"}:
        note=(note+" " if note else "")+"Cero tarjetas parseadas no significa cero vuelos."
    discrepancy=' · '.join(missing) if missing else 'Ninguna confirmada'
    extras=[]
    if missing_direct: extras.append("Directas sólo en Despegar: "+", ".join(missing_direct))
    if unresolved: extras.append("1 escala por verificar: "+", ".join(unresolved))
    return f'''<section class="card audit-card"><div class="audit-head"><div><div class="mini muted">Validación independiente de mercado</div><div class="metric v {css}" style="background:none;border:0;padding:0;font-size:21px">{esc(label)}</div></div><span class="badge {'ok' if status=='CONSISTENT' else ('bad' if status=='DISCREPANCY' else 'warn')}">{esc(dstatus)}</span></div><div class="audit-grid"><div class="metric"><div class="k">Google</div><div class="v">{len(ga)}</div><div class="hint">aerolíneas</div></div><div class="metric"><div class="k">Despegar</div><div class="v">{len(da)}</div><div class="hint">aerolíneas</div></div><div class="metric"><div class="k">Tarjetas auditadas</div><div class="v">{cards}</div><div class="hint">{direct} directas · {one} 1 escala</div></div><div class="metric"><div class="k">Sólo Despegar</div><div class="v">{len(missing)}</div><div class="hint">{esc(discrepancy)}</div></div></div><div class="audit-note">{esc(note or 'Sin observaciones adicionales.')}{'<br>'+esc(' · '.join(extras)) if extras else ''}</div></section>'''


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------

def _best_offer_google_link(offer_row: dict | None) -> str | None:
    if not offer_row:
        return None
    offer_id = int(offer_row.get("id") or 0)
    if offer_id:
        return f"/offers/{offer_id}/google"
    offer = offer_row.get("offer") or {}
    existing = (offer.get("links") or {}).get("google_flights")
    return normalize_google_flights_url(offer, existing)


def render_home(base_cfg, monitors_file: str, notice: str = "", error: bool = False) -> str:
    try: monitors=load_monitors(monitors_file)
    except Exception as exc: monitors=[]; notice,error=f"No se pudo leer {monitors_file}: {exc}",True
    cards=[]
    for m in monitors:
        s=get_latest_snapshot(base_cfg.DB_PATH,m.id); bestrow=None
        if s:
            ofs=get_snapshot_offers(base_cfg.DB_PATH,s["id"],1); bestrow=ofs[0] if ofs else None
        source,mode,mode_css=_snapshot_provider(s); pax=m.adults+len(m.children_ages); price='—'; per='Sin precio válido'; airline='—'; route='Sin oferta'; link=None
        if s and s["best_price"] is not None:
            price=_money(s["currency"],s["best_price"]); per=f'{_money(s["currency"],float(s["best_price"])/max(1,pax))} promedio por pasajero'
            if bestrow:
                off=bestrow.get("offer") or {}; _,names=_airlines(off); airline=' + '.join(names) if names else '—'; route=f'{_leg_summary(off.get("outbound") or {})} / {_leg_summary(off.get("inbound") or {})}'; link=_best_offer_google_link(bestrow)
        last=f'{_fmt_dt(s["run_at"])} UTC · {source}' if s else 'Todavía sin corridas'
        view_best=f'<a class="btn small" target="_blank" rel="noopener" href="{esc(link)}">↗ Mejor oferta</a>' if link else ''
        cards.append(f'''<article class="card monitor-card"><div class="monitor-card-top"><div><h2 class="monitor-name">{esc(m.name)}</h2><div class="monitor-route">EZE/AEP → MIA · {_fmt_date(m.date_outbound)} → {_fmt_date(m.date_return)}</div></div><div class="status-stack"><span class="badge {'ok' if m.enabled else 'warn'}">{'ACTIVO' if m.enabled else 'PAUSADO'}</span>{f'<span class="badge {mode_css}">{esc(mode)}</span>' if s else ''}</div></div><div class="monitor-main"><div><div class="home-price-label">Mejor tarifa observada</div><div class="home-price">{price}</div><div class="home-price-sub">{esc(per)}</div></div><div class="home-summary"><div class="summary-cell"><div class="k">Aerolínea</div><div class="v">{esc(airline)}</div></div><div class="summary-cell"><div class="k">Vuelo</div><div class="v">{esc(route)}</div></div><div class="summary-cell"><div class="k">Pasajeros</div><div class="v">{pax} · {esc(m.cabin_class.replace('_',' ').title())}</div></div></div></div><div class="updated">Última actualización: {esc(last)}</div><div class="monitor-actions"><a class="btn primary" href="/monitors/{esc(m.id)}/history">Ver monitor</a><form class="js-long-action" data-loading-text="Actualizando Google Flights y equipaje…" method="post" action="/monitors/{esc(m.id)}/run"><button class="btn" type="submit">↻ Actualizar precios</button></form>{view_best}<a class="btn ghost" href="/monitors/{esc(m.id)}/edit">✎ Editar</a></div></article>''')
    body=f'''<div class="page-head"><div class="breadcrumb">Flight Sentinel</div><h1 class="page-title">Tus monitores de vuelos</h1><div class="page-subtitle">Seguimiento de precios para Buenos Aires → Miami, con reglas de vuelo estrictas y evidencia de cobertura.</div></div>{'<div class="monitor-list">'+''.join(cards)+'</div>' if cards else '<div class="card empty">No hay monitores todavía. Creá el primero para comenzar.</div>'}'''
    return page("Flight Sentinel",body,cfg=base_cfg,notice=notice,error=error)


# ---------------------------------------------------------------------------
# Form
# ---------------------------------------------------------------------------

def _choice_buttons(current: str) -> str:
    opts=[("ECONOMY","Economía"),("PREMIUM_ECONOMY","Premium Economy"),("BUSINESS","Business"),("FIRST","Primera")]
    return ''.join(f'<button type="button" class="choice {"active" if current==v else ""}" data-value="{v}">{esc(label)}</button>' for v,label in opts)


def render_form(base_cfg, m: MonitorProfile | None = None, *, notice: str = "", error: bool = False) -> str:
    edit=m is not None; d=date.today()+timedelta(days=120); out=_fmt_date(m.date_outbound) if m else d.strftime("%d/%m/%Y"); ret=_fmt_date(m.date_return) if m else (d+timedelta(days=7)).strftime("%d/%m/%Y"); children=','.join(str(x) for x in m.children_ages) if m else ''; checked='checked' if (m is None or m.enabled) else ''; current_cabin=m.cabin_class if m else 'ECONOMY'; action=f'/monitors/{esc(m.id)}/edit' if edit else '/monitors/new'
    ident='' if edit else '<div class="form-group"><label class="form-label">ID corto</label><input class="input" name="id" required pattern="[a-z0-9_-]{2,50}" placeholder="miami-noviembre"><div class="form-help">Sólo letras minúsculas, números, guion o guion bajo.</div></div>'
    danger=f'''<div class="danger-zone"><h3>Zona de peligro</h3><p>Eliminar el monitor quita su configuración. El histórico de base no se borra automáticamente.</p><form method="post" action="/monitors/{esc(m.id)}/delete" onsubmit="return confirm('¿Eliminar este monitor?')"><button class="btn danger" type="submit">🗑 Eliminar monitor</button></form></div>''' if edit else ''
    body=f'''<div class="form-shell"><div class="page-head"><div class="breadcrumb"><a href="/">← Monitores</a> / {'Editar' if edit else 'Nuevo'}</div><h1 class="page-title">{'Editar monitor' if edit else 'Nuevo monitor'}</h1><div class="page-subtitle">Configurá las fechas y pasajeros. La ruta y la política de escalas permanecen protegidas.</div></div><section class="card form-card"><form method="post" action="{action}">{ident}<div class="form-section"><h2>Identificación</h2><p>Un nombre claro ayuda cuando seguís varias ventanas de fechas.</p><div class="form-group"><label class="form-label">Nombre</label><input class="input" name="name" value="{esc(m.name if m else '')}" required placeholder="Miami · vacaciones diciembre"></div></div><div class="form-section"><h2>Ruta y fechas</h2><p>La ruta está fijada en Buenos Aires → Miami.</p><div class="route-lock"><div class="airport-box"><div class="airport-code">EZE + AEP</div><div class="airport-name">Buenos Aires · orígenes incluidos</div></div><div class="route-arrow">→</div><div class="airport-box"><div class="airport-code">MIA</div><div class="airport-name">Miami International Airport</div></div></div><div class="form-row"><div class="form-group"><label class="form-label">Fecha de ida</label><input class="input date-input" type="text" inputmode="numeric" maxlength="10" autocomplete="off" name="date_outbound" value="{out}" required pattern="[0-9]{{2}}/[0-9]{{2}}/[0-9]{{4}}" placeholder="DD/MM/AAAA" title="Formato DD/MM/AAAA"><div class="form-help">Formato obligatorio: DD/MM/AAAA</div></div><div class="form-group"><label class="form-label">Fecha de vuelta</label><input class="input date-input" type="text" inputmode="numeric" maxlength="10" autocomplete="off" name="date_return" value="{ret}" required pattern="[0-9]{{2}}/[0-9]{{2}}/[0-9]{{4}}" placeholder="DD/MM/AAAA" title="Formato DD/MM/AAAA"><div class="form-help">Formato obligatorio: DD/MM/AAAA</div></div></div></div><div class="form-section"><h2>Pasajeros</h2><p>El precio por pasajero del dashboard es un promedio; el proveedor puede tarifar distinto a los menores.</p><div class="form-row"><div class="form-group"><label class="form-label">Adultos</label><input class="input" type="number" min="1" max="9" name="adults" value="{m.adults if m else 2}"></div><div class="form-group"><label class="form-label">Edades de menores</label><input class="input" name="children_ages" value="{esc(children)}" placeholder="8,10"><div class="form-help">Separadas por coma. Dejá vacío si no viajan menores.</div></div></div></div><div class="form-section"><h2>Preferencias de vuelo</h2><p>La cabina es editable. Las restricciones de escalas son parte de la política fija del monitor.</p><input type="hidden" id="cabinClass" name="cabin_class" value="{esc(current_cabin)}"><div class="form-group"><label class="form-label">Clase de cabina</label><div class="choice-group" data-choice-group="cabinClass">{_choice_buttons(current_cabin)}</div></div><div class="form-group"><label class="form-label">Escalas permitidas</label><div class="choice-group readonly-choices"><span class="choice active">Directo o 1 escala</span><span class="choice active">Escala máxima 2 h</span><span class="choice">2+ escalas descartadas</span></div></div></div><div class="form-section"><h2>Estado</h2><p>Un monitor pausado conserva su configuración e histórico, pero no debería incluirse en automatizaciones futuras.</p><div class="toggle-row"><div class="toggle-copy"><b>Monitor activo</b><div>Disponible para nuevas consultas.</div></div><label class="switch"><input type="checkbox" name="enabled" {checked}><span class="slider"></span></label></div></div><div class="form-actions"><button class="btn primary" type="submit">Guardar cambios</button><a class="btn" href="{'/monitors/'+esc(m.id)+'/history' if edit else '/'}">Cancelar</a></div></form>{danger}</section></div>'''
    return page("Editar monitor" if edit else "Nuevo monitor",body,cfg=base_cfg,notice=notice,error=error,show_new=False)


# ---------------------------------------------------------------------------
# Detail page: hero + tabs
# ---------------------------------------------------------------------------

def _price_delta(rows) -> tuple[float | None,float | None]:
    vals=[float(r["best_price"]) for r in rows if r["best_price"] is not None]
    if len(vals)<2: return None,None
    delta=vals[0]-vals[1]; pct=(delta/vals[1]*100) if vals[1] else None; return delta,pct


def _hero(m, latest, all_offers, rows) -> str:
    source,mode,mode_css=_snapshot_provider(latest); pax=m.adults+len(m.children_ages); bestrow=all_offers[0] if all_offers else None; best=float(latest["best_price"]) if latest and latest["best_price"] is not None else None; currency=latest["currency"] if latest else 'USD'; delta,pct=_price_delta(rows)
    if best is None:
        price='—'; per='Sin tarifa válida en la última corrida'; delta_html=''
    else:
        price=_money(currency,best); per=f'{_money(currency,best/max(1,pax))} promedio por pasajero'
        if delta is None: delta_html='<span class="delta-pill flat">Primera referencia</span>'
        elif abs(delta)<.01: delta_html='<span class="delta-pill flat">Sin cambio vs. corrida anterior</span>'
        elif delta<0: delta_html=f'<span class="delta-pill down">↓ {_money(currency,abs(delta))} ({abs(pct or 0):.1f}%) vs. anterior</span>'
        else: delta_html=f'<span class="delta-pill up">↑ {_money(currency,delta)} ({abs(pct or 0):.1f}%) vs. anterior</span>'
    google_link=_best_offer_google_link(bestrow); google_btn=f'<a class="btn buy" target="_blank" rel="noopener" href="{esc(google_link)}">↗ Ver mejor oferta en Google Flights</a>' if google_link else ''
    cov_status,detected,explored,queries=_coverage_values(latest); market_status,market_rec,_=_market_audit_values(latest); market_label,_=_market_label(market_status); trust_good=cov_status=='COMPLETE' and market_status=='CONSISTENT'; trust_class='good' if trust_good else 'warn'; trust_title='Cobertura de mercado consistente' if trust_good else ('Google explorado por completo; mercado no verificado' if cov_status=='COMPLETE' else 'Cobertura parcial de Google')
    trust_text=f'Cobertura Google: {"COMPLETA" if cov_status=="COMPLETE" else ("PARCIAL" if cov_status=="PARTIAL" else "NO REGISTRADA")} · {explored}/{detected} salidas exploradas · Cobertura mercado: {market_label} · Consultas SerpApi: {queries}.' if latest else 'Todavía no hay una corrida para evaluar cobertura.'
    if latest and (market_rec.get('missing_in_google') or []): trust_text += ' Sólo en Despegar: ' + ', '.join(market_rec.get('missing_in_google') or []) + '.'
    verified_bookables=[]
    for r in all_offers:
        off=r.get('offer') or {}; bi=_booking_info(off); low=bi.get('lowest')
        if bi.get('status')=='VERIFIED' and low and low.get('price') is not None:
            verified_bookables.append((float(low.get('price')),str(low.get('seller') or 'vendedor')))
    if verified_bookables:
        bp,bs=min(verified_bookables,key=lambda x:x[0]); bookable_html=f'<div class="hero-bookable"><span>Precio reservable verificado más bajo entre ofertas enriquecidas:</span><b>{_money(currency,bp)}</b><span>· {esc(bs)}</span></div>'
    else:
        bookable_html=''
    return f'''<section class="card hero"><div class="hero-top"><div><div class="hero-label">Mejor precio observado en búsqueda</div><div class="hero-price">{price}</div><div class="hero-per">{esc(per)}</div>{delta_html}{bookable_html}<div class="chip-row"><span class="chip">📅 {_fmt_date(m.date_outbound)} → {_fmt_date(m.date_return)}</span><span class="chip">👥 {m.adults} adulto(s) · {len(m.children_ages)} menor(es)</span><span class="chip">💺 {esc(m.cabin_class.replace('_',' ').title())}</span><span class="chip">✈ Directo o 1 escala ≤ 2 h</span></div><div class="hero-actions">{google_btn}<form class="js-long-action" data-loading-text="Actualizando Google Flights y equipaje…" method="post" action="/monitors/{esc(m.id)}/run"><button class="btn" type="submit">↻ Actualizar precios</button></form><a class="btn ghost" href="/monitors/{esc(m.id)}/edit">✎ Editar monitor</a><button class="btn ghost" type="button" data-share>⌁ Compartir</button></div></div><aside class="hero-side"><div class="hero-side-row"><span>Estado</span><span class="badge {'ok' if m.enabled else 'warn'}">{'ACTIVO' if m.enabled else 'PAUSADO'}</span></div><div class="hero-side-row"><span>Fuente</span><b>{esc(source)}</b></div><div class="hero-side-row"><span>Modo</span><span class="badge {mode_css}">{esc(mode)}</span></div><div class="hero-side-row"><span>Ofertas válidas</span><b>{latest['offers_found'] if latest else 0}</b></div><div class="hero-side-row"><span>Actualizado</span><b>{_fmt_dt(latest['run_at'])+' UTC' if latest else '—'}</b></div></aside></div><div class="trust-box {trust_class}"><div><div class="trust-title">{esc(trust_title)}</div><div class="trust-text">{esc(trust_text)}</div></div><span class="badge {'ok' if trust_good else 'warn'}">{esc('VALIDADO' if trust_good else 'TRANSPARENTE')}</span></div></section>'''


def _tabs(monitor_id: str, active: str) -> str:
    items=[('comparison','Comparación'),('details','Detalles y cobertura'),('history','Histórico')]
    return '<nav class="tabs" aria-label="Vistas del monitor">'+''.join(f'<a class="tab {"active" if active==key else ""}" href="/monitors/{esc(monitor_id)}/history?tab={key}">{label}</a>' for key,label in items)+'</nav>'


def _comparison_view(m, latest, all_offers, show_all: bool, previous_offers: list[dict] | None = None) -> str:
    if not latest:
        return '<div class="card empty">Este monitor todavía no tiene corridas.</div>'
    if latest["best_price"] is None or not all_offers:
        return f'<div class="card empty"><b>Sin ofertas válidas en la última corrida.</b><br>{esc(latest["error"] or "No hubo resultados que cumplieran la política.")}</div>'
    best=float(latest["best_price"]); display=[(o,f'OPCIÓN #{o.get("rank","—")}') for o in all_offers] if show_all else _select_highlights(all_offers); variants=_variant_counts(all_offers); airline_codes=sorted(_all_airline_codes(all_offers)); names=_airline_name_map(all_offers); prev_map=_previous_best_by_group(previous_offers or [])
    cards=''.join(_render_offer_card(row,latest,best,label,variants.get(_offer_group_key(row),1),prev_map.get(_offer_group_key(row))) for row,label in display)
    opts=''.join(f'<option value="{esc(code)}">{esc(names.get(code) or code)}</option>' for code in airline_codes)
    view_link=(f'<a class="btn" href="/monitors/{esc(m.id)}/history?tab=comparison">Ver resumen</a>' if show_all else (f'<a class="btn" href="/monitors/{esc(m.id)}/history?tab=comparison&all=1">Ver todas las ofertas ({len(all_offers)})</a>' if len(all_offers)>len(display) else ''))
    title='Todas las ofertas' if show_all else 'Comparación de ofertas'; subtitle='Filtrá y ordená las alternativas válidas de la última corrida.' if show_all else 'Una alternativa representativa por aerolínea, con precio, escalas y equipaje a simple vista.'
    return f'''<section class="section"><div class="section-head"><div><h2>{title}</h2><p>{subtitle}</p></div><div class="section-actions">{view_link}</div></div><div class="offer-toolbar"><select class="control" id="airlineFilter" aria-label="Filtrar aerolínea"><option value="all">Todas las aerolíneas</option>{opts}</select><select class="control" id="stopsFilter" aria-label="Filtrar escalas"><option value="all">Directos y con escala</option><option value="direct">Sólo directos ida/vuelta</option><option value="stop">Con alguna escala</option></select><select class="control" id="sortOffers" aria-label="Ordenar ofertas"><option value="price">Ordenar por precio</option><option value="duration">Ordenar por duración</option></select><span class="filter-count" id="filterCount"></span></div><div class="offers-grid" id="offersGrid">{cards}</div></section>'''


def _details_view(base_cfg,m,latest,all_offers) -> str:
    cov_status,detected,explored,queries=_coverage_values(latest); cov_label={'COMPLETE':'COMPLETA','PARTIAL':'PARCIAL'}.get(cov_status,'NO REGISTRADA'); market_status,_,_=_market_audit_values(latest); market_label,market_css=_market_label(market_status); source,mode,_=_snapshot_provider(latest); codes=sorted(_all_airline_codes(all_offers)); names=_airline_name_map(all_offers); bag_verified=sum(1 for r in all_offers if str(((r.get('offer') or {}).get('baggage') or {}).get('status') or '').upper()=='VERIFIED'); booking_verified=sum(1 for r in all_offers if str(((r.get('offer') or {}).get('booking') or {}).get('status') or '').upper()=='VERIFIED'); catalog=''.join(f'<span class="airline-chip"><b>{esc(c)}</b> · {esc(names.get(c) or c)}</span>' for c in codes)
    metrics=f'''<div class="metrics-grid"><div class="metric"><div class="k">Cobertura Google</div><div class="v {'coverage-complete' if cov_status=='COMPLETE' else 'coverage-partial'}">{esc(cov_label)}</div><div class="hint">{explored}/{detected} salidas</div></div><div class="metric"><div class="k">Cobertura mercado</div><div class="v {market_css}">{esc(market_label)}</div><div class="hint">auditor independiente</div></div><div class="metric"><div class="k">Aerolíneas</div><div class="v">{len(codes)}</div><div class="hint">en ofertas válidas</div></div><div class="metric"><div class="k">Equipaje verificado</div><div class="v">{bag_verified}</div><div class="hint">tarifas enriquecidas</div></div><div class="metric"><div class="k">Precio reservable</div><div class="v">{booking_verified}</div><div class="hint">ofertas verificadas</div></div><div class="metric"><div class="k">Consultas SerpApi</div><div class="v">{queries}</div><div class="hint">vuelos + reserva/equipaje</div></div><div class="metric"><div class="k">Fuente</div><div class="v" style="font-size:14px">{esc(source)}</div><div class="hint">{esc(mode)}</div></div></div>'''
    policy='''<div class="policy-card"><div class="policy-item"><div class="k">Origen</div><div class="v">EZE + AEP</div></div><div class="policy-item"><div class="k">Destino</div><div class="v">MIA</div></div><div class="policy-item"><div class="k">Escalas</div><div class="v">0 o 1 por tramo</div></div><div class="policy-item"><div class="k">Conexión máxima</div><div class="v">2 horas</div></div></div>'''
    audit_action=f'''<form class="js-long-action" data-loading-text="Auditando Despegar sin consumir SerpApi…" method="post" action="/monitors/{esc(m.id)}/audit"><button class="btn" type="submit">Auditar Despegar gratis</button></form>'''
    return f'''<section class="section"><div class="section-head"><div><h2>Estado técnico y cobertura</h2><p>Información de confianza, consumo y reglas aplicadas a la última corrida.</p></div></div><section class="card card-pad">{metrics}</section></section><section class="section"><div class="section-head"><div><h2>Política de búsqueda</h2><p>Estas reglas son obligatorias y no se relajan en el ranking.</p></div></div><section class="card card-pad">{policy}</section></section><section class="section"><div class="section-head"><div><h2>Aerolíneas encontradas por Google</h2><p>Código IATA + descripción normalizada.</p></div></div><section class="card card-pad"><div class="airline-catalog">{catalog or '<span class="muted">Sin aerolíneas registradas.</span>'}</div></section></section><section class="section"><div class="section-head"><div><h2>Validación independiente</h2><p>Despegar es opcional y puede bloquear automatización; un fallo no invalida Google.</p></div><div>{audit_action}</div></div>{_render_market_audit(latest)}</section>'''


# ---------------------------------------------------------------------------
# History view
# ---------------------------------------------------------------------------

def _parse_run_dt(raw: str) -> datetime | None:
    try: return datetime.fromisoformat(raw)
    except Exception: return None


def _filter_history(rows, period: str):
    if period=='all' or not rows: return list(rows)
    try: days=int(period)
    except Exception: return list(rows)
    anchor=_parse_run_dt(rows[0]['run_at']) or datetime.now(); cutoff=anchor-timedelta(days=days)
    return [r for r in rows if (_parse_run_dt(r['run_at']) or anchor)>=cutoff]


def _history_stats(rows) -> dict:
    priced=[r for r in rows if r['best_price'] is not None]
    if not priced: return {'current':None,'best':None,'worst':None,'change':None,'change_pct':None,'range_pct':None}
    current=float(priced[0]['best_price']); vals=[float(r['best_price']) for r in priced]; prev=float(priced[1]['best_price']) if len(priced)>1 else None; change=current-prev if prev is not None else None; change_pct=change/prev*100 if prev else None; oldest=float(priced[-1]['best_price']); range_pct=(current-oldest)/oldest*100 if oldest else None
    return {'current':current,'best':min(vals),'worst':max(vals),'change':change,'change_pct':change_pct,'range_pct':range_pct}


def _history_chart(rows, currency='USD') -> str:
    points=[(r['run_at'],float(r['best_price'])) for r in reversed(rows) if r['best_price'] is not None]
    if len(points)<2: return '<div class="empty" style="padding:90px 10px">El gráfico aparecerá después de 2 corridas con precio.</div>'
    vals=[v for _,v in points]; lo=min(vals); hi=max(vals); span=(hi-lo) or 1; w,h=760,280; l,r,t,b=58,22,25,36; coords=[]
    for i,(dt,v) in enumerate(points):
        x=l+(w-l-r)*(i/(len(points)-1)); y=t+(h-t-b)*(1-(v-lo)/span); coords.append((x,y,dt,v))
    poly=' '.join(f'{x:.1f},{y:.1f}' for x,y,_,_ in coords); area=f'M {coords[0][0]:.1f} {h-b} L '+ ' L '.join(f'{x:.1f} {y:.1f}' for x,y,_,_ in coords)+f' L {coords[-1][0]:.1f} {h-b} Z'
    grid=[]
    for i in range(5):
        y=t+(h-t-b)*(i/4); val=hi-(span*(i/4)); grid.append(f'<line x1="{l}" y1="{y:.1f}" x2="{w-r}" y2="{y:.1f}" stroke="#1b3550" stroke-width="1"/><text x="{l-8}" y="{y+4:.1f}" fill="#7890a7" font-size="10" text-anchor="end">{esc(_money(currency,val).replace(currency+" ","$"))}</text>')
    circles=''.join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#52d187"><title>{esc(_fmt_dt(dt))} · {esc(_money(currency,v))}</title></circle>' for x,y,dt,v in coords)
    labels=[]
    idxs=sorted(set([0,len(coords)//2,len(coords)-1]))
    for idx in idxs:
        x,_,dt,_=coords[idx]; labels.append(f'<text x="{x:.1f}" y="{h-10}" fill="#7890a7" font-size="10" text-anchor="middle">{esc(_fmt_dt(dt).split(" ")[0])}</text>')
    return f'''<svg class="chart" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" aria-label="Evolución del precio mínimo">{''.join(grid)}<path d="{area}" fill="#52d187" opacity=".08"/><polyline points="{poly}" fill="none" stroke="#52d187" stroke-width="3" vector-effect="non-scaling-stroke"/>{circles}{''.join(labels)}</svg>'''


def _timeline(rows, currency='USD') -> str:
    items=[]
    for i,r in enumerate(rows[:20]):
        current=float(r['best_price']) if r['best_price'] is not None else None; prev=None
        for nxt in rows[i+1:]:
            if nxt['best_price'] is not None: prev=float(nxt['best_price']); break
        if current is None:
            css='neutral'; title='Sin ofertas válidas'; desc=f'{r["offers_found"]} ofertas guardadas · {r["error"] or "sin precio válido"}'; change='—'; change_css=''
        elif prev is None:
            css='neutral'; title='Primera referencia del período'; desc=f'Mejor precio registrado: {_money(r["currency"],current)}'; change=_money(r['currency'],current); change_css=''
        else:
            d=current-prev
            if d<-.01: css='down'; title='Precio bajó'; change='−'+_money(r['currency'],abs(d)); change_css='down'
            elif d>.01: css='up'; title='Precio subió'; change='+'+_money(r['currency'],d); change_css='up'
            else: css='neutral'; title='Precio sin cambios'; change='0'; change_css=''
            desc=f'Mejor oferta de la corrida: {_money(r["currency"],current)} · {r["offers_found"]} oferta(s) válidas'
        items.append(f'<div class="timeline-item {css}"><div class="timeline-date">{_fmt_dt(r["run_at"])} UTC</div><div><div class="timeline-title">{esc(title)}</div><div class="timeline-desc">{esc(desc)}</div></div><div class="timeline-change {change_css}">{esc(change)}</div></div>')
    if not items:
        return '<div class="empty">Sin eventos en este período.</div>'
    visible=''.join(items[:5]); rest=items[5:]
    more='' if not rest else f'<details class="timeline-more"><summary>Ver {len(rest)} cambio(s) más</summary><div class="timeline">{"".join(rest)}</div></details>'
    return visible+more


def _raw_history_table(base_cfg, rows) -> str:
    trs=[]
    for r in rows:
        price=_money(r['currency'],r['best_price']) if r['best_price'] is not None else '—'; source,mode,css=_snapshot_provider(r); cov,det,exp,q=_coverage_values(r); market,_=_market_label(_market_audit_values(r)[0]); trs.append(f'<tr><td>{_fmt_dt(r["run_at"])} UTC</td><td><span class="badge {css}">{esc(mode)}</span><div class="mini muted">{esc(source)}</div></td><td><b>{price}</b></td><td>{r["offers_found"]}</td><td>{esc(cov)} · {exp}/{det}</td><td>{q}</td><td>{esc(market)}</td><td>{esc(r["error"] or "OK")}</td></tr>')
    return f'''<details class="raw-details"><summary>Ver datos técnicos de las corridas ({len(rows)})</summary><div class="table-wrap"><table class="table"><thead><tr><th>Corrida</th><th>Fuente</th><th>Precio</th><th>Ofertas</th><th>Google</th><th>Consultas</th><th>Mercado</th><th>Estado</th></tr></thead><tbody>{''.join(trs) if trs else '<tr><td colspan="8">Sin datos</td></tr>'}</tbody></table></div></details>'''


def _history_view(base_cfg,m,rows,period: str) -> str:
    filtered=_filter_history(rows,period); stats=_history_stats(filtered); currency=next((r['currency'] for r in filtered if r['best_price'] is not None),'USD'); ch=stats['change']; chpct=stats['change_pct']; rng=stats['range_pct']
    def mv(v): return _money(currency,v) if v is not None else '—'
    change_text=('—' if ch is None else (('−'+_money(currency,abs(ch))) if ch<0 else ('+'+_money(currency,ch) if ch>0 else '0'))); change_cls='coverage-complete' if ch is not None and ch<0 else ('coverage-bad' if ch is not None and ch>0 else '')
    if stats['current'] is None: insight='No hay precios válidos en el período seleccionado.'
    elif abs(stats['current']-stats['best'])<.01: insight=f'El precio actual ({mv(stats["current"])}) coincide con el mínimo registrado en este período.'
    else: insight=f'El precio actual está {_money(currency,stats["current"]-stats["best"])} por encima del mínimo registrado ({mv(stats["best"])}).'
    periods=''.join(f'<a class="period {"active" if period==p else ""}" href="/monitors/{esc(m.id)}/history?tab=history&period={p}">{label}</a>' for p,label in [('7','7 días'),('14','14 días'),('30','30 días'),('all','Todo')])
    return f'''<section class="section"><div class="section-head"><div><h2>Histórico de precios</h2><p>{_fmt_date(m.date_outbound)} → {_fmt_date(m.date_return)} · evolución del mejor precio válido por corrida.</p></div><div class="section-actions"><a class="btn" href="/monitors/{esc(m.id)}/export.csv">↓ Descargar CSV</a><button class="btn ghost" type="button" data-share>⌁ Compartir</button></div></div><section class="card card-pad"><div class="stats-grid"><div class="stat-card"><div class="label">Precio actual</div><div class="value">{mv(stats['current'])}</div><div class="sub">última corrida con precio</div></div><div class="stat-card"><div class="label">Mejor precio</div><div class="value coverage-complete">{mv(stats['best'])}</div><div class="sub">mínimo del período</div></div><div class="stat-card"><div class="label">Peor precio</div><div class="value">{mv(stats['worst'])}</div><div class="sub">máximo del período</div></div><div class="stat-card"><div class="label">Cambio vs. anterior</div><div class="value {change_cls}">{esc(change_text)}</div><div class="sub">{f'{chpct:+.1f}%' if chpct is not None else 'sin comparación'}</div></div></div><div class="periods">{periods}</div><div class="chart-card" style="padding:0">{_history_chart(filtered,currency)}</div><div class="insight"><b>Lectura del histórico:</b> {esc(insight)}{f' Variación desde la primera corrida del período: {rng:+.1f}%.' if rng is not None else ''}</div><div class="section-head" style="margin-top:20px"><div><h2>Cambios registrados</h2><p>Sólo hechos observados; no se infieren causas ni recomendaciones de compra.</p></div></div><div class="timeline">{_timeline(filtered,currency)}</div>{_raw_history_table(base_cfg,filtered)}</section></section>'''


def render_history(base_cfg, monitors_file: str, monitor_id: str, notice: str = "", show_all: bool = False, tab: str = "comparison", period: str = "14") -> str:
    m=get_monitor(monitor_id,monitors_file); rows=get_monitor_history(base_cfg.DB_PATH,monitor_id,100); latest=rows[0] if rows else None; all_offers=get_snapshot_offers(base_cfg.DB_PATH,latest['id'],500) if latest else []
    previous_snapshot=next((r for r in rows[1:] if r['best_price'] is not None),None); previous_offers=get_snapshot_offers(base_cfg.DB_PATH,previous_snapshot['id'],500) if previous_snapshot else []
    tab=tab if tab in {'comparison','details','history'} else 'comparison'
    body=f'''<div class="page-head"><div class="breadcrumb"><a href="/">← Monitores</a> / {esc(m.name)}</div><h1 class="page-title">Buenos Aires → Miami</h1><div class="page-subtitle">{esc(m.name)} · {_fmt_date(m.date_outbound)} → {_fmt_date(m.date_return)}</div></div>{_hero(m,latest,all_offers,rows)}{_tabs(m.id,tab)}'''
    if tab=='details': body+=_details_view(base_cfg,m,latest,all_offers)
    elif tab=='history': body+=_history_view(base_cfg,m,rows,period)
    else: body+=_comparison_view(m,latest,all_offers,show_all,previous_offers)
    return page("Monitor · Buenos Aires → Miami",body,cfg=base_cfg,notice=notice,show_new=False)


# ---------------------------------------------------------------------------
# CSV export / form parsing
# ---------------------------------------------------------------------------

def _history_csv(base_cfg, monitor_id: str) -> bytes:
    rows=get_monitor_history(base_cfg.DB_PATH,monitor_id,1000); sio=io.StringIO(newline=''); w=csv.writer(sio); w.writerow(['corrida_utc','precio','moneda','ofertas_validas','proveedor','modo','cobertura_google','salidas_detectadas','salidas_exploradas','consultas_serpapi','cobertura_mercado','estado'])
    for r in rows:
        source,mode,_=_snapshot_provider(r); cov,det,exp,q=_coverage_values(r); market,_=_market_label(_market_audit_values(r)[0]); w.writerow([_fmt_dt(r['run_at']),r['best_price'] if r['best_price'] is not None else '',r['currency'],r['offers_found'],source,mode,cov,det,exp,q,market,r['error'] or 'OK'])
    return ('\ufeff'+sio.getvalue()).encode('utf-8')


def _profile_from_form(form: dict[str,list[str]], existing_id: str | None = None) -> MonitorProfile:
    def one(k,default=''): return form.get(k,[default])[0]
    children=tuple(int(x.strip()) for x in one('children_ages').split(',') if x.strip())
    return MonitorProfile(id=(existing_id or one('id')).strip().lower(),name=one('name').strip(),date_outbound=_parse_ui_date(one('date_outbound')),date_return=_parse_ui_date(one('date_return')),enabled='enabled' in form,adults=int(one('adults','2')),children_ages=children,cabin_class=one('cabin_class','ECONOMY').upper(),currency='USD')


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

def make_handler(base_cfg, monitors_file: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,fmt,*args): print('[dashboard] '+(fmt%args))
        def _send(self,content: str,status: int=200,content_type='text/html; charset=utf-8',extra_headers: dict|None=None):
            data=content.encode('utf-8') if isinstance(content,str) else content
            try:
                self.send_response(status); self.send_header('Content-Type',content_type); self.send_header('Content-Length',str(len(data)))
                for k,v in (extra_headers or {}).items(): self.send_header(k,v)
                self.end_headers(); self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError) as exc: print(f'[dashboard] Cliente cerró la conexión: {exc}')
            except OSError as exc:
                if getattr(exc,'winerror',None) in {10053,10054}: print(f'[dashboard] Conexión del navegador cerrada: {exc}')
                else: raise
        def _redirect(self,location: str):
            try: self.send_response(303); self.send_header('Location',location); self.end_headers()
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError): pass
        def _read_form(self):
            n=int(self.headers.get('Content-Length','0') or 0); return parse_qs(self.rfile.read(n).decode('utf-8'),keep_blank_values=True)
        def do_GET(self):
            parsed=urlparse(self.path); path=parsed.path; query=parse_qs(parsed.query)
            try:
                if path=='/': return self._send(render_home(base_cfg,monitors_file))
                if path=='/favicon.ico': return self._send(b'',204)
                if path=='/monitors/new': return self._send(render_form(base_cfg))
                parts=[p for p in path.split('/') if p]
                if len(parts)==3 and parts[0]=='offers' and parts[2]=='google':
                    row=get_offer_by_id(base_cfg.DB_PATH,int(parts[1]))
                    if not row:
                        return self._send(page('Oferta no encontrada','<div class="card empty">La oferta ya no está disponible en el histórico local.</div>',cfg=base_cfg,show_new=False),404)
                    offer=row.get('offer') or {}; links=offer.get('links') or {}; booking=offer.get('booking') or {}
                    exact=links.get('google_flights_exact') if str(links.get('google_flights_scope') or '').upper()=='EXACT' else None
                    booking_ok=str(booking.get('status') or '').upper()=='VERIFIED'
                    if exact and booking_ok:
                        return self._redirect(str(exact))
                    try:
                        details=resolve_exact_google_flights_details(offer,api_key=base_cfg.SERPAPI_API_KEY,gl=getattr(base_cfg,'SERPAPI_GL','ar'),hl=getattr(base_cfg,'SERPAPI_HL','es'))
                        exact=details['url']
                        update_offer_google_details(base_cfg.DB_PATH,int(parts[1]),exact_url=exact,booking=details.get('booking'),baggage=details.get('baggage'))
                        return self._redirect(exact)
                    except Exception as exc:
                        generic=normalize_google_flights_url(offer,links.get('google_flights'))
                        body=(f'<div class="page-head"><div class="breadcrumb">Oferta #{esc(parts[1])}</div>'
                              f'<h1 class="page-title">No pude fijar el itinerario exacto en Google Flights</h1>'
                              f'<div class="page-subtitle">No voy a abrir una búsqueda genérica haciéndola pasar por esta oferta.</div></div>'
                              f'<div class="card card-pad"><p class="muted">{esc(exc)}</p>'
                              f'<p class="small-text">Podés abrir igualmente la búsqueda general con ruta, fechas y filtros, pero Google mostrará varias alternativas.</p>'
                              f'<a class="btn" target="_blank" rel="noopener" href="{esc(generic)}">Abrir búsqueda general en Google Flights</a></div>')
                        return self._send(page('Google Flights',body,cfg=base_cfg,show_new=False),502)
                if len(parts)==3 and parts[0]=='monitors' and parts[2]=='edit': return self._send(render_form(base_cfg,get_monitor(parts[1],monitors_file)))
                if len(parts)==3 and parts[0]=='monitors' and parts[2]=='history':
                    return self._send(render_history(base_cfg,monitors_file,parts[1],notice=query.get('msg',[''])[0],show_all=query.get('all',['0'])[0]=='1',tab=query.get('tab',['comparison'])[0],period=query.get('period',['14'])[0]))
                if len(parts)==3 and parts[0]=='monitors' and parts[2]=='export.csv':
                    data=_history_csv(base_cfg,parts[1]); return self._send(data,200,'text/csv; charset=utf-8',{'Content-Disposition':f'attachment; filename="flight_sentinel_{parts[1]}_history.csv"'})
                return self._send(page('No encontrado','<div class="card empty">Página no encontrada.</div>',cfg=base_cfg,show_new=False),404)
            except Exception as exc: return self._send(render_home(base_cfg,monitors_file,notice=str(exc),error=True),500)
        def do_POST(self):
            path=urlparse(self.path).path; form=self._read_form(); parts=[p for p in path.split('/') if p]
            try:
                if path=='/monitors/new':
                    p=_profile_from_form(form); p.validate(); upsert_monitor(p,monitors_file); return self._redirect('/')
                if len(parts)==3 and parts[0]=='monitors' and parts[2]=='edit':
                    p=_profile_from_form(form,parts[1]); p.validate(); upsert_monitor(p,monitors_file); return self._redirect(f'/monitors/{parts[1]}/history')
                if len(parts)==3 and parts[0]=='monitors' and parts[2]=='delete': delete_monitor(parts[1],monitors_file); return self._redirect('/')
                if len(parts)==3 and parts[0]=='monitors' and parts[2] in {'audit','run'}:
                    monitor_id=parts[1]; lock=_job_lock(monitor_id)
                    if not lock.acquire(blocking=False): return self._send(render_history(base_cfg,monitors_file,monitor_id,notice='Ya hay una ejecución en curso para este monitor. No se inició una segunda consulta.'),409)
                    try:
                        m=get_monitor(monitor_id,monitors_file); cfg=apply_monitor(base_cfg,m)
                        if parts[2]=='audit':
                            latest=get_latest_snapshot(base_cfg.DB_PATH,monitor_id)
                            if not latest: return self._send(render_history(base_cfg,monitors_file,monitor_id,notice='Primero debe existir una corrida Google para poder conciliar el mercado.'),400)
                            offers=get_snapshot_offers(base_cfg.DB_PATH,latest['id'],500); audit=audit_existing_snapshot(cfg,latest['id'],offers); st=(audit.get('reconciliation') or {}).get('status','NOT_RUN'); msg=f'Auditoría Despegar finalizada: {st}. No consumió consultas SerpApi.'; return self._redirect(f'/monitors/{monitor_id}/history?tab=details&msg={quote(msg)}')
                        result=run_check(cfg) or {}; status=result.get('status','OK'); market_label=_market_label(result.get('market_coverage_status','NOT_RUN'))[0]
                        if result.get('best_price') is not None: msg=f"Corrida {status}: {result['currency']} {result['best_price']:.2f} · {result.get('offers',0)} oferta(s) válidas · mercado: {market_label}."
                        else: msg=f'Corrida {status}: no hubo ofertas que cumplan la política · mercado: {market_label}.'
                        return self._redirect(f'/monitors/{monitor_id}/history?msg={quote(msg)}')
                    finally: lock.release()
                return self._send(page('No encontrado','<div class="card empty">Acción no encontrada.</div>',cfg=base_cfg,show_new=False),404)
            except Exception as exc:
                if len(parts)>=2 and parts[0]=='monitors' and parts[-1]=='edit':
                    try: return self._send(render_form(base_cfg,get_monitor(parts[1],monitors_file),notice=str(exc),error=True),400)
                    except Exception: pass
                return self._send(render_home(base_cfg,monitors_file,notice=str(exc),error=True),400)
    return Handler


def run_dashboard(base_cfg, monitors_file='monitors.yaml', host='127.0.0.1', port=8766):
    initialize_db(base_cfg.DB_PATH); server=ThreadingHTTPServer((host,port),make_handler(base_cfg,monitors_file)); print(f'Flight Sentinel V2: http://{host}:{port}'); name,mode,_=_provider_info_from_cfg(base_cfg); print(f'Proveedor dashboard: {name} | {mode}'); print('Ctrl+C para detener.')
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
