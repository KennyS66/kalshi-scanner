"""HTML for the /bot screen. Served by web.py; polls /api/bot/status."""

BOT_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Swing Bot</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root { --bg:#0b0f17; --bg2:#121826; --bg3:#1b2334; --fg:#e8edf4; --mute:#8b96a8;
        --green:#3fd68c; --red:#ff5c64; --amber:#ffc24b; --border:#232d42; }
* { box-sizing:border-box; margin:0; }
body { background:var(--bg); color:var(--fg);
       font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; padding:18px; }
h1 { font-size:18px; margin-bottom:4px; }
a { color:var(--mute); text-decoration:none; margin-right:10px; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
        gap:14px; margin-top:14px; }
.panel { background:var(--bg2); border:1px solid var(--border);
         border-radius:10px; padding:14px; }
.badge { display:inline-block; padding:2px 10px; border-radius:6px;
         border:1px solid var(--border); background:var(--bg3); margin-right:8px; }
.badge.paper { color:var(--amber); }
.badge.live  { color:var(--green); }
.badge.halted{ color:var(--red); }
.badge.stale { color:var(--red); }
.tiles { display:flex; gap:12px; flex-wrap:wrap; }
.tile { background:var(--bg3); border-radius:8px; padding:10px 14px; min-width:110px; }
.tile b { display:block; font-size:20px; }
.pos { color:var(--green); } .neg { color:var(--red); }
table { width:100%; border-collapse:collapse; font-size:12px; }
th,td { text-align:left; padding:4px 6px; border-bottom:1px solid var(--border); }
th { color:var(--mute); font-weight:normal; }
#log { max-height:260px; overflow-y:auto; font-size:12px; color:var(--mute); }
button { background:var(--bg3); color:var(--fg); border:1px solid var(--border);
         border-radius:6px; padding:6px 14px; cursor:pointer; margin-right:8px; }
button:hover { border-color:var(--mute); }
button.danger { color:var(--red); }
#liveToggle[disabled] { opacity:.45; cursor:not-allowed; }
.unlock { color:var(--mute); font-size:12px; margin-top:6px; }
</style></head><body>
<h1>Swing Bot <span id="modeBadge" class="badge paper">PAPER</span>
    <span id="runBadge" class="badge">…</span></h1>
<div><a href="/trade">/trade</a><a href="/crypto">/crypto</a><a href="/whales">/whales</a></div>

<div class="grid">
  <div class="panel">
    <div class="tiles">
      <div class="tile">bankroll<b id="bankroll">—</b></div>
      <div class="tile">day P&amp;L<b id="dayPnl">—</b></div>
      <div class="tile">day stop<b id="dayStop">—</b></div>
      <div class="tile">win rate<b id="winRate">—</b></div>
      <div class="tile">net avg<b id="netAvg">—</b></div>
      <div class="tile">trades<b id="nTrades">—</b></div>
    </div>
    <p style="margin-top:12px">
      <button onclick="ctl('pause')">Pause</button>
      <button onclick="ctl('resume')">Resume</button>
      <button class="danger" onclick="ctl('flatten')">Flatten</button>
      <button id="liveToggle" disabled>LIVE 🔒</button>
    </p>
    <div class="unlock" id="unlock"></div>
  </div>
  <div class="panel"><h3>Open plays</h3>
    <table id="openTable"><thead><tr><th>ticker</th><th>side</th><th>qty</th>
    <th>entry</th></tr></thead><tbody></tbody></table></div>
  <div class="panel"><h3>Exit-reason stats</h3>
    <table id="reasonTable"><thead><tr><th>reason</th><th>n</th><th>win%</th>
    <th>net avg</th></tr></thead><tbody></tbody></table></div>
</div>
<div class="panel" style="margin-top:14px"><h3>Trades</h3>
  <table id="tradeTable"><thead><tr><th>ticker</th><th>side</th><th>qty</th>
  <th>in</th><th>out</th><th>reason</th><th>net</th></tr></thead><tbody></tbody></table></div>
<div class="panel" style="margin-top:14px"><h3>Decision log</h3><div id="log"></div></div>

<script>
const $ = id => document.getElementById(id);
const money = v => (v<0?'-':'+') + '$' + Math.abs(v).toFixed(2);
async function ctl(cmd){ await fetch('/api/bot/control',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd})}); poll(); }
async function poll(){
  try {
    const r = await fetch('/api/bot/status'); const d = await r.json();
    const s = d.state || {};
    $('modeBadge').textContent = (s.mode||'paper').toUpperCase();
    const fresh = s.heartbeat && (Date.now()/1000 - s.heartbeat) < 30;
    $('runBadge').textContent = !fresh ? 'BOT OFFLINE'
        : s.halted ? 'HALTED' : s.paused ? 'PAUSED' : 'RUNNING';
    $('runBadge').className = 'badge ' + (!fresh||s.halted ? 'halted' : 'paper');
    $('bankroll').textContent = s.bankroll ? '$'+s.bankroll.toFixed(2) : '—';
    $('dayPnl').textContent = money(s.day_pnl||0);
    $('dayPnl').className = (s.day_pnl||0) >= 0 ? 'pos':'neg';
    $('dayStop').textContent = s.bankroll ? '-$'+(0.10*s.bankroll).toFixed(2) : '—';
    const a = d.stats.all_time;
    const td = d.stats.today || {n: 0};
    const allStr = a.n ? a.win_pct.toFixed(0)+'%' : '—';
    $('winRate').textContent = td.n > 0
      ? `today: ${td.win_pct.toFixed(0)}% · all: ${allStr}` : allStr;
    $('netAvg').textContent = a.n ? money(a.net_avg) : '—';
    $('nTrades').textContent = a.n;
    $('unlock').textContent = 'LIVE unlock: ' + d.unlock.reason;
    $('openTable').tBodies[0].innerHTML = Object.entries(s.open_plays||{}).map(
      ([t,p]) => `<tr><td>${t}</td><td>${p.side}</td><td>${p.qty}</td>
                  <td>${p.entry.price}</td></tr>`).join('');
    $('reasonTable').tBodies[0].innerHTML = Object.entries(d.stats.by_exit_reason||{})
      .map(([k,v]) => `<tr><td>${k}</td><td>${v.n}</td><td>${v.win_pct}%</td>
                       <td>${money(v.net_avg)}</td></tr>`).join('');
    $('tradeTable').tBodies[0].innerHTML = (d.trades||[]).slice().reverse().map(t =>
      `<tr><td>${t.ticker}</td><td>${t.side}</td><td>${t.qty}</td>
       <td>${t.entry_price}</td><td>${t.exit_price}</td><td>${t.exit_reason}</td>
       <td class="${t.net_pnl>=0?'pos':'neg'}">${money(t.net_pnl)}</td></tr>`).join('');
    $('log').innerHTML = (d.events||[]).slice().reverse().map(e =>
      `<div>${new Date(e.ts*1000).toISOString().slice(11,19)} [${e.action}] `
      + `${e.ticker||''} ${e.reason||''}</div>`).join('');
  } catch(e) { $('runBadge').textContent = 'API ERR'; }
}
poll(); setInterval(poll, 3000);
</script></body></html>"""
