"""Admin panel HTML (VPS-only via /panel, password-gated data).

Self-contained: no external JS/CSS. Hand-rolled SVG bar charts — zero
dependencies, works offline. The page itself carries no secrets: without
a valid session every data endpoint answers 401, so the page only shows
the login form.
"""

PANEL_PAGE = """<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Panel — Za Ile Przejadę?</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
         background: #f4f6f8; color: #1a2a3a; padding: 24px; }
  h1 { font-size: 1.3rem; margin-bottom: 4px; }
  .sub { color: #667; font-size: .85rem; margin-bottom: 20px; }
  .wrap { max-width: 1100px; margin: 0 auto; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
           gap: 12px; margin-bottom: 20px; }
  .card { background: #fff; border: 1px solid #e3e8ee; border-radius: 10px;
          padding: 14px 16px; }
  .card .v { font-size: 1.7rem; font-weight: 700; }
  .card .l { color: #667; font-size: .78rem; text-transform: uppercase;
             letter-spacing: .04em; margin-top: 2px; }
  .card .v.red { color: #e74c3c; } .card .v.orange { color: #f39c12; }
  .box { background: #fff; border: 1px solid #e3e8ee; border-radius: 10px;
         padding: 16px 18px; margin-bottom: 20px; }
  .box h2 { font-size: .95rem; margin-bottom: 12px; color: #445; }
  table { width: 100%; border-collapse: collapse; font-size: .85rem; }
  td, th { padding: 6px 8px; border-bottom: 1px solid #eef1f4; text-align: left; }
  th { color: #667; font-weight: 600; font-size: .75rem; text-transform: uppercase; }
  #login { max-width: 360px; margin: 12vh auto 0; text-align: center; }
  #login input { width: 100%; padding: 12px; font-size: 1rem; margin: 14px 0;
                 border: 1px solid #ccd; border-radius: 8px; }
  #login button, #logout { padding: 10px 22px; font-size: 1rem; border: 0;
                 border-radius: 8px; background: #2A5BD5; color: #fff;
                 cursor: pointer; }
  #login .err { color: #e74c3c; font-size: .85rem; min-height: 1.2em; }
  .viewbar { display: flex; align-items: center; gap: 8px; margin-bottom: 16px;
             flex-wrap: wrap; }
  .view-btn { padding: 7px 14px; border: 1px solid #ccd; border-radius: 8px;
              background: #fff; cursor: pointer; font-size: .85rem; }
  .view-btn.active { background: #2A5BD5; color: #fff; border-color: #2A5BD5; }
  .date-inp { padding: 6px 8px; border: 1px solid #ccd; border-radius: 8px;
              font-size: .85rem; }
  .hidden { display: none !important; }
  svg text { font-family: system-ui, sans-serif; }
  .legend { display: flex; gap: 16px; font-size: .78rem; color: #667;
            margin-bottom: 8px; flex-wrap: wrap; }
  .legend i { display: inline-block; width: 10px; height: 10px;
              border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
  #dash { display: none; }
  .cols { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  @media (max-width: 800px) { .cols { grid-template-columns: 1fr; } }
  .mut { color: #99a; font-size: .8rem; }
  #logout { background: none; border: 1px solid #ccd; color: #567;
            border-radius: 8px; padding: 6px 12px; cursor: pointer;
            font-size: .8rem; }
</style>
</head>
<body>
<div id="login" class="wrap">
  <h1>🔐 Panel operatora</h1>
  <div class="sub">zaileprzeja.de</div>
  <input type="password" id="pw" placeholder="Hasło" autocomplete="current-password">
  <div class="err" id="err"></div>
  <button onclick="doLogin()">Wejdź</button>
</div>

<div id="dash" class="wrap" style="display:none">
  <h1>Statystyki <span id="ver" style="color:#2A5BD5"></span></h1>
  <div class="sub">Czas Europe/Warsaw · odświeżanie co 60 s
      <button id="logout" onclick="logout()">Wyloguj</button></div>

  <div class="viewbar">
    <button class="view-btn active" data-days="7">7 dni</button>
    <button class="view-btn" data-days="30">30 dni</button>
    <button class="view-btn" data-view="custom">Zakres</button>
    <input type="date" id="date-from" class="date-inp hidden">
    <input type="date" id="date-to" class="date-inp hidden">
    <button id="date-apply" class="view-btn hidden">Pokaż</button>
  </div>

  <div class="cards" id="kpis"></div>

  <div class="box">
    <h2>Wyszukiwania tras / dzień</h2>
    <div class="legend">
      <span><i style="background:#27ae60"></i>ok</span>
      <span><i style="background:#e74c3c"></i>timeout</span>
      <span><i style="background:#f39c12"></i>odrzucone (kolejka)</span>
    </div>
    <div id="ch-req"></div>
  </div>

  <div class="box">
    <h2>Użytkownicy (unikalne IP/dzień)</h2>
    <div id="ch-vis"></div>
  </div>

  <div class="cols">
    <div class="box"><h2>Restarty serwera</h2><div id="t-restart"></div></div>
    <div class="box"><h2>Wdrożenia (autoupdate)</h2><div id="t-updates"></div></div>
  </div>
</div>

<script nonce="__CSP_NONCE__">
'use strict';
function esc(s){const d=document.createElement('i');d.textContent=s==null?'':s;return d.innerHTML;}
function fmtTs(ts){const d=new Date(ts*1000);return d.toLocaleString('pl-PL',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});}

async function api(path, opts){
  const r = await fetch(path, Object.assign({credentials:'same-origin'}, opts||{}));
  if (r.status === 401) throw {auth:true};
  if (!r.ok) throw new Error('HTTP '+r.status);
  return r.json();
}

function login(){
  fetch('/api/admin/login', {method:'POST', credentials:'same-origin',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({password:document.getElementById('pw').value})})
  .then(r => { if(r.ok){showDash();} else {document.getElementById('err').textContent='Błędne hasło';} })
  .catch(()=>{document.getElementById('err').textContent='Błąd połączenia';});
  return false;
}
function logout(){ fetch('/api/admin/logout',{method:'POST',credentials:'same-origin'}); location.reload(); }

function barChart(el, series){
  // series: [{name, color, values:[..]}, ...] — stacked; labels z serii 0
  const W = el.clientWidth || 900, H = 220, padL = 34, padB = 22, padT = 8;
  const labels = series[0].labels;
  const n = labels.length;
  const bw = Math.max(2, Math.floor((W-padL-8)/n) - 2);
  const max = Math.max(1, ...series.map(s=>Math.max(...s.values)));
  let x = '<svg width="100%" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="xMidYMid meet">';
  for (let g = 0; g <= 2; g++){
    const y = padT + (H-padT-padB)*g/2;
    const v = Math.round(max*(2-g)/2*10)/10;
    x += '<line x1="'+padL+'" y1="'+y+'" x2="'+W+'" y2="'+y+'" stroke="#eef1f4"/>';
    x += '<text x="'+(padL-6)+'" y="'+(y+4)+'" font-size="10" fill="#99a" text-anchor="end">'+v+'</text>';
  }
  labels.forEach((_, i)=>{
    let acc = 0;
    series.forEach(s=>{
      const bh = (s.values[i]/max)*(H-padT-padB);
      if (bh > 0){
        x += '<rect x="'+(padL+i*((W-padL-8)/n))+'" y="'+(H-padB-acc-bh)+'" '
           + 'width="'+bw+'" height="'+Math.max(bh,1)+'" fill="'+s.color+'" rx="1">'
           + '<title>'+esc(s.name)+': '+s.values[i]+'</title></rect>';
      }
      acc += bh;
    });
  });
  const every = Math.max(1, Math.round(n/8));
  labels.forEach((lab, i)=>{
    if (i % every === 0)
      x += '<text x="'+(padL+i*((W-padL-8)/n)+bw/2)+'" y="'+(H-6)+'" font-size="10" fill="#99a" text-anchor="middle">'+esc(String(lab).slice(5))+'</text>';
  });
  x += '</svg>';
  el.innerHTML = x;
}

let refreshTimer = null;
const view = { from: null, to: null };   // 'YYYY-MM-DD' × 2
function shiftDays(days){
  const d = new Date();
  d.setDate(d.getDate() - (days - 1));
  return d.toISOString().slice(0, 10);
}
async function showDash(){
  document.getElementById('login').style.display='none';
  document.getElementById('dash').style.display='block';
  if (!view.from){ setView(30); } else { await loadStats(); }
  clearInterval(refreshTimer);
  refreshTimer = setInterval(loadStats, 60000);
}
function setView(days){
  view.from = shiftDays(days); view.to = shiftDays(1);
  syncInputs();
  setActiveBtn(String(days));
  loadStats();
}
function setCustom(){
  view.from = document.getElementById('date-from').value;
  view.to = document.getElementById('date-to').value;
  if (!view.from || !view.to) return;
  setActiveBtn('custom');
  loadStats();
}
function syncInputs(){
  document.getElementById('date-from').value = view.from;
  document.getElementById('date-to').value = view.to;
}
function setActiveBtn(v){
  document.querySelectorAll('.view-btn').forEach(b=>{
    b.classList.toggle('active',
      b.dataset.days === v || b.dataset.view === v);
  });
  const custom = v === 'custom';
  document.getElementById('date-from').classList.toggle('hidden', !custom);
  document.getElementById('date-to').classList.toggle('hidden', !custom);
  document.getElementById('date-apply').classList.toggle('hidden', !custom);
}
function rangeLabel(){
  const today = shiftDays(1);
  if (view.to === today && view.from === shiftDays(6)) return '7 dni';
  if (view.to === today && view.from === shiftDays(29)) return '30 dni';
  return view.from + ' → ' + view.to;
}
async function loadStats(){
  const q = '/api/admin/stats?from=' + encodeURIComponent(view.from)
          + '&to=' + encodeURIComponent(view.to);
  let r = await fetch(q, {credentials:'same-origin'});
  if (r.status === 401){ location.reload(); return; }
  render(await r.json());
}
document.querySelectorAll('.view-btn[data-days]').forEach(b=>{
  b.addEventListener('click', ()=>setView(Number(b.dataset.days)));
});
document.querySelector('.view-btn[data-view="custom"]')
  .addEventListener('click', ()=>{
    document.getElementById('date-from').classList.toggle('hidden');
    document.getElementById('date-to').classList.toggle('hidden');
    document.getElementById('date-apply').classList.toggle('hidden');
  });
document.getElementById('date-apply').addEventListener('click', setCustom);
function render(s){
  const days = Object.keys(s.daily);
  barChart(document.getElementById('ch-vis'), [
    {name:'użytkownicy', values:days.map(d=>s.daily[d].visitors||0), color:'#2A5BD5', labels:days}
  ]);
  barChart(document.getElementById('ch-req'), [
    {name:'ok', values:days.map(d=>s.daily[d].ok||0), color:'#27ae60', labels:days},
    {name:'timeout', values:days.map(d=>s.daily[d].timeout||0), color:'#e74c3c', labels:days},
    {name:'kolejka', values:days.map(d=>s.daily[d].busy||0), color:'#f39c12', labels:days}
  ]);

  const total = (k)=>days.reduce((x,d)=>x+(s.daily[d][k]||0),0);
  const rng = (s.range && s.range.from) ? s.range.from + ' → ' + s.range.to
                                        : 'ostatnie 30 dni';

  document.getElementById('kpis').innerHTML = [
    {l:'wyszukiwania · ' + rng, v:total('requests'), c:''},
    {l:'użytkownicy · ' + rng, v:(s.unique_total||0), c:''},
    {l:'timeouty', v:total('timeout'), c:total('timeout')?'red':''},
    {l:'odrzucone', v:total('busy'), c:total('busy')?'orange':''}
  ].map(k=>'<div class="card"><div class="v '+k.c+'">'+k.v+'</div><div class="l">'+esc(k.l)+'</div></div>').join('');

  document.getElementById('t-restart').innerHTML = s.restarts.length
    ? '<table><tr><th>kiedy</th><th>wersja</th></tr>' + s.restarts.map(r=>
        '<tr><td>'+fmtTs(r.ts)+'</td><td>'+(esc(r.version)||'—')+'</td></tr>').join('')+'</table>'
    : '<span class="mut">brak zdarzeń w tym zakresie</span>';
  document.getElementById('t-updates').innerHTML = s.updates.length
    ? '<table><tr><th>kiedy</th><th>co</th></tr>' + s.updates.map(u=>
        '<tr><td>'+esc(u.ts)+'</td><td>'+esc(u.what)+'</td></tr>').join('')+'</table>'
    : '<span class="mut">brak wdrożeń w tym zakresie</span>';
}

async function boot(){
  try{
    const r = await fetch('/api/admin/session', {credentials:'same-origin'});
    if (r.ok) showDash(); else document.getElementById('login').style.display='block';
  }catch(e){ document.getElementById('err').textContent='Błąd połączenia'; }
}
document.getElementById('pw').addEventListener('keydown', e=>{ if(e.key==='Enter') login(); });
boot();
</script>
</body>
</html>
"""
