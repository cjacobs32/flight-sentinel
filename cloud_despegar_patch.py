#!/usr/bin/env python3
"""Inyecta la paridad Despegar en el dashboard estático de GitHub Pages.

Se aplica después de ``cloud_dashboard.py`` para no duplicar la UI base V2.9.
El navegador sólo genera URLs públicas y comandos por GitHub Issues; nunca
recibe tokens ni secretos.
"""
from __future__ import annotations

import argparse
from pathlib import Path

JS_DESPEGAR = r'''
(function(){
  const _baseBindCommon = bindCommon;
  const _baseRenderHome = renderHome;
  const _baseRenderMonitor = renderMonitor;

  function despegarUrl(n){
    const dest=destinationCode(n), adults=Number(n?.adults||1), children=(n?.children_ages||[]).length;
    const currency=String(n?.currency||'USD').toUpperCase();
    return `https://www.despegar.com.ar/shop/flights/results/roundtrip/BUE/${encodeURIComponent(dest)}/${encodeURIComponent(n.date_outbound||'')}/${encodeURIComponent(n.date_return||'')}/${adults}/${children}/0?currency=${encodeURIComponent(currency)}`;
  }
  function hasGoogleSnapshot(n){
    return ((n.last_audit||{}).offers||[]).length>0 || ((n.last_watch||{}).offers||[]).length>0;
  }
  function auditClass(status){
    status=String(status||'').toUpperCase();
    if(status==='CONSISTENT') return 'ok';
    if(status==='DISCREPANCY') return 'bad';
    if(['AUDIT_FAILED','BLOCKED_BY_SITE','INCONCLUSIVE'].includes(status)) return 'warn';
    return 'info';
  }
  function addHomeLinks(){
    document.querySelectorAll('.monitor-card').forEach(card=>{
      if(card.querySelector('[data-despegar-link]')) return;
      const monitorLink=card.querySelector('a[data-nav][href*="monitor="]');
      if(!monitorLink) return;
      const params=new URL(monitorLink.href,location.href).searchParams;
      const id=params.get('monitor'), n=(D.monitors||{})[id];
      if(!n) return;
      const actions=card.querySelector('.monitor-actions');
      if(!actions) return;
      const audit=hasGoogleSnapshot(n)?`<button class="btn ghost" data-despegar-audit="${esc(n.id)}">Auditar Despegar</button>`:'';
      actions.insertAdjacentHTML('beforeend',`<a class="btn small" data-despegar-link target="_blank" rel="noopener" href="${esc(despegarUrl(n))}">↗ Despegar</a>${audit}`);
    });
  }
  function addDetailsPanel(n){
    const host=$('#tabBody');
    if(!host || host.querySelector('[data-despegar-panel]')) return;
    const audit=n.last_market_audit||{}, rec=audit.reconciliation||{}, d=audit.despegar||{};
    const status=rec.status||d.status||'NOT_RUN', url=d.url||despegarUrl(n);
    const ga=rec.google_airlines||[], da=rec.despegar_airlines||[];
    const missing=rec.missing_in_google||[], missingDirect=rec.missing_direct_in_google||[];
    const note=rec.note||d.warning||'Todavía no se ejecutó la auditoría independiente de Despegar para este monitor.';
    const auditButton=hasGoogleSnapshot(n)?`<button class="btn primary" data-despegar-audit="${esc(n.id)}">Auditar Despegar gratis</button>`:'<button class="btn" disabled>Primero actualizá precios</button>';
    host.insertAdjacentHTML('beforeend',`<section class="section" data-despegar-panel><div class="section-head"><div><h2>Validación independiente · Despegar</h2><p>No consume SerpApi. Compara Despegar contra la última corrida Google ya guardada; un bloqueo de Despegar no invalida Google.</p></div><div class="section-actions"><a class="btn" data-despegar-link target="_blank" rel="noopener" href="${esc(url)}">↗ Ver en Despegar</a>${auditButton}</div></div><section class="card card-pad"><div class="home-summary"><div class="summary-cell"><div class="k">Estado</div><div class="v"><span class="badge ${auditClass(status)}">${esc(status)}</span></div></div><div class="summary-cell"><div class="k">Google</div><div class="v">${ga.length} aerolínea(s)</div></div><div class="summary-cell"><div class="k">Despegar</div><div class="v">${da.length} aerolínea(s)</div></div></div><div class="cloud-note" style="margin-top:14px"><b>${esc(note)}</b>${missing.length?`<br>Sólo en Despegar: ${esc(missing.join(', '))}`:''}${missingDirect.length?`<br>Directas sólo en Despegar: ${esc(missingDirect.join(', '))}`:''}${audit.at?`<br>Auditado: ${dateTimeUI(audit.at)}`:''}${d.cards_seen!=null?`<br>Tarjetas auditadas: ${esc(d.cards_seen)} · directas: ${esc(d.direct_cards||0)} · 1 escala: ${esc(d.one_stop_cards||0)}`:''}</div></section></section>`);
  }
  function addMonitorLinks(n,tab){
    const actions=document.querySelector('.hero-actions');
    if(actions && !actions.querySelector('[data-despegar-link]')){
      const audit=hasGoogleSnapshot(n)?`<button class="btn ghost" data-despegar-audit="${esc(n.id)}">Auditar Despegar</button>`:'';
      actions.insertAdjacentHTML('beforeend',`<a class="btn ghost" data-despegar-link target="_blank" rel="noopener" href="${esc(despegarUrl(n))}">↗ Ver en Despegar</a>${audit}`);
    }
    if(tab==='details') addDetailsPanel(n);
  }
  bindCommon=function(){
    _baseBindCommon();
    document.querySelectorAll('[data-despegar-audit]').forEach(b=>b.onclick=()=>goPipeline({FLIGHT_ACTION:'despegar_audit',FLIGHT_MONITOR_ID:b.dataset.despegarAudit}));
  };
  renderHome=function(){
    _baseRenderHome(); addHomeLinks(); bindCommon();
    const sub=document.querySelector('.brand-sub'); if(sub) sub.textContent='Cloud Full V2.9 · destinos + fechas flexibles + Despegar · GitHub Actions · $0';
  };
  renderMonitor=function(n,tab){
    _baseRenderMonitor(n,tab); addMonitorLinks(n,tab); bindCommon();
    const sub=document.querySelector('.brand-sub'); if(sub) sub.textContent='Cloud Full V2.9 · destinos + fechas flexibles + Despegar · GitHub Actions · $0';
  };
  render();
})();
'''.strip()


def patch_html(path: str) -> None:
    p = Path(path)
    html = p.read_text(encoding="utf-8")
    marker = "data-flight-despegar-v291"
    if marker in html:
        return
    script = f'<script {marker}>\n{JS_DESPEGAR}\n</script>'
    if "</body>" not in html:
        raise ValueError(f"HTML inválido, falta </body>: {p}")
    p.write_text(html.replace("</body>", script + "</body>", 1), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", default="public/index.html")
    args = parser.parse_args()
    patch_html(args.html)
    print(f"Paridad Despegar V2.9 inyectada: {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
