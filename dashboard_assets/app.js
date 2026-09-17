'use strict';
const $=id=>document.getElementById(id);
const money=new Intl.NumberFormat('fr-CH',{style:'currency',currency:'USD',maximumFractionDigits:2});
const integer=new Intl.NumberFormat('fr-CH',{maximumFractionDigits:0});
let data=null, tab='paper', loading=false;
const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const usd=v=>v==null?'N/D':money.format(Number(v));
const num=v=>v==null?'N/D':integer.format(Number(v));
const date=v=>v?new Date(v*1000).toLocaleString('fr-CH'):'N/D';
const short=v=>v?String(v).slice(0,8)+'…'+String(v).slice(-6):'—';
const badge=v=>`<span class="badge">${esc(v)}</span>`;
const pnl=v=>`<span class="${v>0?'positive':v<0?'negative':''}">${usd(v)}</span>`;
function cell(v){return `<td>${v}</td>`;}
function address(v){return `<td class="address" title="${esc(v)}">${esc(short(v))}</td>`;}
function metric(label,value,sub,positive=false){return `<article class="metric"><div class="metric-label">${label}</div><div class="metric-value ${positive?'positive':''}">${value}</div><small>${sub}</small></article>`;}
function chart(points){
 if(!points?.length){$('chart').innerHTML='<div class="empty">Les premières exécutions paper feront apparaître cette courbe.<br>Aucun profit simulé n’est encore enregistré.</div>';return;}
 const width=700,height=185,left=65,top=12,bottom=155,values=[0,...points.map(p=>p.pnl)];
 let lo=Math.min(...values),hi=Math.max(...values);if(lo===hi){lo-=1;hi+=1;}
 const span=hi-lo;lo-=span*.12;hi+=span*.12;
 const y=v=>bottom-(v-lo)/(hi-lo)*(bottom-top);
 const series=[{at:points[0].at-86400,pnl:0},...points];
 const x=i=>left+i/(series.length-1)*(width-left-15);
 const line=series.map((p,i)=>`${i?'L':'M'}${x(i)},${y(p.pnl)}`).join(' ');
 const grid=[hi,(hi+lo)/2,lo].map(v=>`<line x1="${left}" x2="${width}" y1="${y(v)}" y2="${y(v)}" stroke="#253142"/><text x="${left-10}" y="${y(v)+4}" text-anchor="end">${num(v)}</text>`).join('');
 const utc=v=>new Date(v*1000).toLocaleDateString('fr-CH',{timeZone:'UTC'});
 $('chart').innerHTML=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="PnL réalisé cumulé : ${esc(usd(points.at(-1).pnl))}">${grid}<line x1="${left}" x2="${width}" y1="${y(0)}" y2="${y(0)}" stroke="#78869b" stroke-dasharray="3 5"/><path d="${line}" fill="none" stroke="#81e8c1" stroke-width="2.5"/><circle cx="${x(series.length-1)}" cy="${y(points.at(-1).pnl)}" r="4" fill="#81e8c1"/><text x="${left}" y="180">${utc(series[0].at)}</text><text x="${width-15}" y="180" text-anchor="end">${utc(points.at(-1).at)}</text></svg>`;
}
function render(){
 const p=data.paper,t=p?.totals,a=data.analysis,h=a?.history,tx=a?.transactions;
 $('metrics').innerHTML=metric('Wallets en simulation',num(t?.active),`${num(t?.wallets)} portefeuilles · capital indépendant`)+metric('PnL réalisé paper',usd(t?.realized_pnl),'Positions vendues · frais inclus',t?.realized_pnl>0)+metric('Capital disponible',usd(t?.cash),`${usd(t?.initial_cash)} alloués au total`)+metric('Coût des positions ouvertes',usd(t?.open_cost),`${num(t?.open_positions)} positions · valeur de marché inconnue`);
 if(p)chart(p.curve);else $('chart').innerHTML='<div class="empty">Données paper indisponibles. Aucun résultat ne peut être affiché.</div>';
 const row=(k,v)=>`<div class="stat-row"><span class="muted">${k}</span><strong>${v}</strong></div>`;
 $('analysis').innerHTML=a?row('Wallets découverts / profilés',num(a.wallets.length))+row('Analyses terminées',num(h?.COMPLETE??0))+row('En cours / en attente',`${num(h?.IN_PROGRESS??0)} / ${num(h?.WAITING??0)}`)+(tx?`<progress class="progress" max="${Math.max(tx.queued,1)}" value="${tx.decoded}" aria-label="Transactions reconstruites"></progress><p class="footnote">${num(tx.decoded)} / ${num(tx.queued)} transactions reconstruites<br>Dernière tentative : ${date(a.last_analysis_attempt)}</p>`:'<p class="footnote">Collecte non initialisée.</p>'):'<div class="empty">Analyse indisponible.</div>';
 const labels={launch_indexer:'Indexation tokens',wallet_profiler:'Profils wallets',auto_discovery:'Découverte',bot:'Scanner',historical_analysis:'Analyse historique',alchemy_webhook:'Webhook',paper_copy:'Copie paper',ngrok:'Tunnel ngrok',dashboard:'Tableau de bord'};
 $('components').innerHTML=data.processes?.components.length?data.processes.components.map(p=>`<span class="component"><span class="dot ${p.status==='RUNNING'?'on':p.status==='STOPPED'?'off':''}"></span>${esc(labels[p.name]||p.name)} · ${p.status==='RUNNING'?'actif':p.status==='STOPPED'?'arrêté':'inconnu'}</span>`).join(''):'<span class="muted">État du lanceur indisponible.</span>';
 $('checkpoints').innerHTML=`<span>Prochain bloc analyse : <b>${num(a?.scanner?.next_block)}</b></span><span>Prochain bloc paper : <b>${num(p?.progress?.next_block)}</b></span><span>Livraisons webhook acceptées : <b>${num(a?.deliveries)}</b></span><span>Lacunes paper : <b>${num(p?.gaps)}</b></span><span>Dernière exécution paper : <b>${date(p?.last_fill_at)}</b></span>`;
 renderTable();
}
function renderTable(){
 if(!data)return; const query=$('search').value.trim().toLowerCase(),sort=$('sort').value;
 let rows=[],headers=[],note='',format;
 if(tab==='paper'){
  rows=[...(data.paper?.wallets||[])];headers=['Wallet','Suivi','Cash','PnL réalisé','Coût ouvert','PnL si valeur ouverte = 0','Achats / ventes','Positions'];
  note='Résultats de copie uniquement. Le PnL prudent suppose une valeur nulle des tokens restants ; ce n’est pas un PnL de marché.';
  format=r=>address(r.wallet)+cell(badge(r.active?'ACTIF':'INACTIF'))+cell(usd(r.cash))+cell(pnl(r.realized_pnl))+cell(usd(r.open_cost))+cell(pnl(r.cash_only_pnl))+cell(`${r.buys} / ${r.sells}`)+cell(r.positions.length?`<details><summary>${r.positions.length} ouvertes</summary>${r.positions.map(p=>`<div class="position">${esc(p.token)}<br>Coût ${usd(p.cost)} · Qté ${esc(p.quantity)}</div>`).join('')}</details>`:'—');
  rows.sort((a,b)=>sort==='activity'?(b.buys+b.sells)-(a.buys+a.sells):b.realized_pnl-a.realized_pnl);
 }else if(tab==='history'){
  rows=[...(data.history?.wallets||[])];headers=['Wallet','Classe historique','État des données','Achats','Fermées / ouvertes','PnL prudent','Rendement prudent','Paper admis','Cluster'];
  note=`Dernier export : ${date(data.history?.exported_at)}. Photos historiques, distinctes des résultats paper. L’admission affichée est celle de cet export.`;
  format=r=>address(r.wallet)+cell(badge(r.historical_class))+cell(esc(r.metrics_status))+cell(num(r.buys))+cell(`${num(r.closed_positions)} / ${num(r.open_positions_count)}`)+cell(pnl(r.conservative_pnl_usdc))+cell(r.conservative_return==null?'N/D':`${(r.conservative_return*100).toFixed(1)} %`)+cell(badge(r.paper_review_eligible?'OUI, À L’EXPORT':'NON'))+cell(`${num(r.identity_cluster_size)} wallet(s)`);
  rows.sort((a,b)=>(sort==='activity'?(b.buys??-Infinity)-(a.buys??-Infinity):(b.conservative_pnl_usdc??-Infinity)-(a.conservative_pnl_usdc??-Infinity))||0);
 }else if(tab==='discovery'){
  rows=[...(data.analysis?.wallets||[])];headers=['Wallet','Statut moteur','Score','Classe profil','Sources winners','Première découverte'];
  note='Statuts et scores persistés du moteur. Le tableau de bord ne promeut aucun wallet.';
  format=r=>address(r.wallet)+cell(badge(r.status))+cell(num(r.profile_score))+cell(esc(r.profile_class))+cell(num(r.source_winners))+cell(date(r.first_discovered_at));
  rows.sort((a,b)=>(sort==='activity'?(b.source_winners??-1)-(a.source_winners??-1):(b.profile_score??-1)-(a.profile_score??-1)));
 }else{
  rows=[...(data.paper?.fills||[])];headers=['Wallet','Token','Sens','Date','Montant','Frais + gas','PnL réalisé'];
  note='Les 40 dernières exécutions simulées. Un transfert détecté n’est pas automatiquement un achat.';
  format=r=>address(r.wallet)+cell(`<span title="${esc(r.token)}">${esc(short(r.token))}</span>`)+cell(badge(r.side))+cell(date(r.filled_at))+cell(usd(r.notional))+cell(usd(Number(r.fee)+Number(r.gas)))+cell(pnl(Number(r.realized_pnl)));
 }
 if(sort==='wallet')rows.sort((a,b)=>a.wallet.localeCompare(b.wallet));
 const total=rows.length;rows=rows.filter(r=>!query||String(r.wallet).toLowerCase().includes(query)||String(r.token||'').toLowerCase().includes(query));
 $('table-note').textContent=note;
 $('table').innerHTML=rows.length?`<table><thead><tr>${headers.map(h=>`<th scope="col">${h}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${format(r)}</tr>`).join('')}</tbody></table>`:'<div class="empty">Aucune donnée à afficher pour cette vue.</div>';
 $('count').textContent=`${rows.length} / ${total} lignes · Survolez une adresse pour la voir en entier.`;
}
async function refresh(){
 if(loading)return;loading=true;$('refresh').disabled=true;
 try{
  const r=await fetch('/api/overview',{cache:'no-store',signal:AbortSignal.timeout(12000)});if(!r.ok)throw Error('unavailable');
  data=await r.json();render();$('freshness').textContent='Mis à jour '+new Date(data.at*1000).toLocaleTimeString('fr-CH');
  const errors=Object.keys(data.errors);$('error').hidden=!errors.length;
  $('error').textContent='Certaines données sont indisponibles : '+errors.join(', ')+'. Aucun résultat manquant n’est remplacé par zéro.';
 }catch(e){$('freshness').textContent='Connexion interrompue';$('error').hidden=false;$('error').textContent='Le tableau de bord ne répond plus. Les valeurs affichées sont les dernières reçues, pas un état actuel.';}
 finally{loading=false;$('refresh').disabled=false;}
}
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>{tab=b.dataset.tab;document.querySelectorAll('[data-tab]').forEach(t=>{const on=t===b;t.classList.toggle('selected',on);t.setAttribute('aria-pressed',on);});renderTable();}));
$('search').addEventListener('input',renderTable);$('sort').addEventListener('change',renderTable);$('refresh').addEventListener('click',refresh);
refresh();setInterval(refresh,10000);
