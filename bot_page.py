"""HTML for the /bot screen. Served by web.py; polls /api/bot/status plus the
signal/offsets/history endpoints so the buy/sell range the bot gates on is
visible live, alongside the calibration state learned from banner history."""

BOT_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>kalshi · swing bot</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root {
  --bg:#0b0f17; --bg2:#121826; --bg3:#1b2334; --fg:#e8edf4; --mute:#8b96a8;
  --border:#263044; --hair:#1a2232;
  --green:#3fd68c; --red:#ff5c64; --yellow:#ffc53d;
  --blue:#5ca8ff; --orange:#ff9f45; --purple:#c29bff;
  --green-bg:rgba(63,214,140,.08);  --green-bd:rgba(63,214,140,.38);
  --red-bg:rgba(255,92,100,.08);    --red-bd:rgba(255,92,100,.38);
  --yellow-bg:rgba(255,197,61,.08); --yellow-bd:rgba(255,197,61,.38);
  --blue-bg:rgba(92,168,255,.09);   --blue-bd:rgba(92,168,255,.35);
  --orange-bg:rgba(255,159,69,.10); --purple-bg:rgba(194,155,255,.10);
  --mono:ui-monospace,"SF Mono","Fira Code",monospace;
  --sans:ui-sans-serif,system-ui,"Segoe UI",sans-serif;
  --radius:10px;
  --card-shadow:inset 0 1px 0 rgba(255,255,255,.035), 0 6px 20px rgba(0,0,0,.30);
}
* { box-sizing:border-box; margin:0; padding:0; }
::-webkit-scrollbar { width:9px; height:9px; }
::-webkit-scrollbar-thumb { background:var(--bg3); border-radius:5px; border:2px solid var(--bg); }
::-webkit-scrollbar-thumb:hover { background:var(--border); }
body {
  font-family:var(--mono);
  background:
    radial-gradient(1100px 460px at 75% -12%, rgba(92,168,255,.055), transparent 65%),
    radial-gradient(900px 420px at -10% 110%, rgba(63,214,140,.035), transparent 60%),
    var(--bg);
  background-attachment:fixed;
  color:var(--fg);
  min-height:100vh; font-size:13px;
}
button:focus-visible, a:focus-visible { outline:2px solid var(--blue); outline-offset:2px; }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation:none !important; transition:none !important; }
}

/* ── header ── */
header {
  padding:11px 20px; border-bottom:1px solid var(--border);
  background:rgba(18,24,38,.92); backdrop-filter:blur(6px);
  display:flex; align-items:center; gap:16px; flex-wrap:wrap;
  position:sticky; top:0; z-index:20;
}
.logo { font-size:14px; font-weight:900; letter-spacing:-0.5px; }
.logo::before { content:""; display:inline-block; width:8px; height:8px; border-radius:50%;
                background:var(--green); margin-right:8px; vertical-align:baseline;
                box-shadow:0 0 8px var(--green); animation:livedot 2.4s ease-in-out infinite; }
.logo.off::before { background:var(--red); box-shadow:0 0 8px var(--red); animation:none; }
@keyframes livedot { 0%,100%{opacity:1} 50%{opacity:.35} }
.badge { font-size:10px; font-weight:900; padding:3px 11px; border-radius:99px;
         letter-spacing:1px; border:1px solid var(--border); background:var(--bg3); }
.badge.paper   { color:var(--yellow); border-color:var(--yellow-bd); background:var(--yellow-bg); }
.badge.live    { color:var(--green);  border-color:var(--green-bd);  background:var(--green-bg); }
.badge.running { color:var(--green);  border-color:var(--green-bd);  background:var(--green-bg); }
.badge.paused  { color:var(--yellow); border-color:var(--yellow-bd); background:var(--yellow-bg); }
.badge.halted, .badge.offline { color:var(--red); border-color:var(--red-bd); background:var(--red-bg); }
.spot-btc { color:var(--orange); font-weight:800; font-size:15px; font-variant-numeric:tabular-nums; }
.clock { color:var(--mute); font-size:12px; margin-left:auto; font-variant-numeric:tabular-nums; }
.nav-link { color:var(--mute); font-size:11px; text-decoration:none;
            padding:4px 10px; border:1px solid transparent; border-radius:6px; transition:all .18s; }
.nav-link:hover { color:var(--blue); border-color:var(--blue-bd); background:var(--blue-bg); }

/* ── range banner ── */
.range-banner {
  margin:16px 20px 0; border:1px solid var(--border); border-radius:var(--radius);
  background:var(--bg2); box-shadow:var(--card-shadow); overflow:hidden;
}
.range-head { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap;
              padding:14px 18px 6px; }
.range-side { font-size:12px; font-weight:900; letter-spacing:1px; padding:3px 11px;
              border-radius:99px; border:1px solid var(--border); }
.range-side.yes { color:var(--green); border-color:var(--green-bd); background:var(--green-bg); }
.range-side.no  { color:var(--red);   border-color:var(--red-bd);   background:var(--red-bg); }
.range-buy  { font-size:26px; font-weight:900; color:var(--blue);   font-variant-numeric:tabular-nums; letter-spacing:-.5px; }
.range-arr  { font-size:16px; color:var(--mute); }
.range-sell { font-size:26px; font-weight:900; color:var(--yellow); font-variant-numeric:tabular-nums; letter-spacing:-.5px; }
.gate-chip  { font-size:11px; font-weight:800; padding:3px 11px; border-radius:99px;
              border:1px solid var(--border); margin-left:auto; }
.gate-chip.in   { color:var(--green);  border-color:var(--green-bd);  background:var(--green-bg); }
.gate-chip.out  { color:var(--yellow); border-color:var(--yellow-bd); background:var(--yellow-bg); }
.gate-chip.wait { color:var(--mute); }
.range-sub { padding:0 18px 4px; font-size:11px; color:var(--mute); font-family:var(--sans); }

/* range meter: buy band (blue) + sell band (yellow) + price/ask ticks */
.meter-wrap { padding:12px 18px 30px; }
.meter { position:relative; height:34px; background:var(--hair); border-radius:7px; }
.meter .grid { position:absolute; top:0; bottom:0; width:1px; background:var(--border);
               opacity:.55; }
.meter .grid-label { position:absolute; top:38px; transform:translateX(-50%);
                     font-size:9px; color:var(--mute); opacity:.8;
                     font-variant-numeric:tabular-nums; }
.meter .band { position:absolute; top:0; bottom:0; display:flex; align-items:center;
               justify-content:center; font-size:10px; font-weight:800; letter-spacing:1px; }
.meter .band.buy  { background:var(--blue-bg);   border:1px solid var(--blue-bd);
                    border-radius:7px 0 0 7px; color:var(--blue); }
.meter .band.sell { background:var(--yellow-bg); border:1px solid var(--yellow-bd);
                    border-radius:0 7px 7px 0; color:var(--yellow); }
.meter .tick { position:absolute; top:-6px; bottom:-6px; width:2px; border-radius:2px; }
.meter .tick.px  { background:var(--fg); }
.meter .tick.ask { background:var(--orange); }
.meter .tick-label { position:absolute; top:-22px; transform:translateX(-50%);
                     font-size:10px; font-weight:800; white-space:nowrap;
                     font-variant-numeric:tabular-nums; }
.meter .tick-label.px  { color:var(--fg); }
.meter .tick-label.ask { color:var(--orange); }
.range-banner.waiting { opacity:.5; filter:grayscale(60%); }

/* ── ledger strip: one row, one ledger ── */
.tiles { display:flex; flex-wrap:wrap; margin:14px 20px;
         background:var(--bg2); border:1px solid var(--border);
         border-radius:var(--radius); box-shadow:var(--card-shadow); }
.tile { flex:1 1 128px; padding:12px 16px; border-left:1px solid var(--hair); min-width:0; }
.tile:first-child { border-left:none; }
.tile.hero { flex:1.3 1 150px; }
.tile.hero .v { font-size:27px; letter-spacing:-1px; }
.tile .k { font-size:10px; color:var(--mute); text-transform:uppercase;
           letter-spacing:.7px; font-family:var(--sans); font-weight:600; }
.tile .v { font-size:18px; font-weight:800; margin-top:3px; font-variant-numeric:tabular-nums;
           white-space:nowrap; }
.tile .s { font-size:10px; color:var(--mute); margin-top:2px; font-variant-numeric:tabular-nums;
           white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.pos { color:var(--green); } .neg { color:var(--red); }

/* ── panels ── */
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(330px,1fr));
        gap:14px; margin:0 20px 20px; }
.panel { background:var(--bg2); border:1px solid var(--border); border-radius:var(--radius);
         padding:14px 16px; box-shadow:var(--card-shadow); min-width:0; }
.panel.wide { grid-column:1 / -1; }
.panel h3 { font-size:11px; color:var(--mute); text-transform:uppercase;
            letter-spacing:.9px; font-family:var(--sans); margin-bottom:10px; }
.tbl-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th,td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--hair);
        white-space:nowrap; font-variant-numeric:tabular-nums; }
th { color:var(--mute); font-weight:600; font-family:var(--sans); font-size:10px;
     text-transform:uppercase; letter-spacing:.6px; }
tr:last-child td { border-bottom:none; }
.dim { color:var(--mute); }
.side-chip { font-weight:800; }
.side-chip.yes { color:var(--green); } .side-chip.no { color:var(--red); }
.empty { color:var(--mute); font-size:12px; padding:8px 0; font-family:var(--sans); }

/* ── controls ── */
.controls { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
button { background:var(--bg3); color:var(--fg); border:1px solid var(--border);
         border-radius:7px; padding:7px 16px; cursor:pointer; font:inherit;
         font-weight:700; transition:all .18s; }
button:hover { border-color:var(--mute); }
button.go:hover     { color:var(--green); border-color:var(--green-bd); background:var(--green-bg); }
button.warn:hover   { color:var(--yellow); border-color:var(--yellow-bd); background:var(--yellow-bg); }
button.danger       { color:var(--red); }
button.danger:hover { border-color:var(--red-bd); background:var(--red-bg); }
#liveToggle[disabled] { opacity:.4; cursor:not-allowed; }
.unlock { color:var(--mute); font-size:11px; font-family:var(--sans); margin-top:8px; }

#log { max-height:280px; overflow-y:auto; font-size:11.5px; line-height:1.75; }
#log .t { color:var(--mute); margin-right:8px; }
#log .a { font-weight:800; margin-right:6px; }
#log .a.enter { color:var(--green); } #log .a.exit { color:var(--blue); }
#log .a.skip { color:var(--mute); } #log .a.halt, #log .a.feed_down, #log .a.error { color:var(--red); }
#log .a.pause { color:var(--yellow); } #log .a.resume { color:var(--green); }

/* ── settlement-grade verdict chips ── */
.vchip { font-size:10px; font-weight:800; padding:2px 8px; border-radius:99px;
         border:1px solid var(--border); background:var(--bg3); white-space:nowrap; }
.vchip.clean_win    { color:var(--green);  border-color:var(--green-bd);  background:var(--green-bg); }
.vchip.good_exit    { color:var(--green);  border-color:var(--green-bd);  background:var(--green-bg); }
.vchip.good_stop    { color:var(--blue);   border-color:var(--blue-bd);   background:var(--blue-bg); }
.vchip.lucky_exit   { color:var(--yellow); border-color:var(--yellow-bd); background:var(--yellow-bg); }
.vchip.left_money   { color:var(--orange); background:var(--orange-bg); }
.vchip.whipsaw_stop { color:var(--red);    border-color:var(--red-bd);    background:var(--red-bg); }
.vchip.ungraded     { color:var(--mute); }
.grade-strip { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; }
.grade-edge { font-family:var(--sans); font-size:12px; line-height:1.8; }
.grade-edge b { font-variant-numeric:tabular-nums; }
.notional { color:var(--mute); font-size:11px; }
/* gate progress bar inside its tile */
.gatebar { height:4px; background:var(--hair); border-radius:3px; margin-top:6px; overflow:hidden; }
.gatebar i { display:block; height:100%; background:var(--blue); border-radius:3px; }
</style></head><body>

<header>
  <span class="logo" id="logo">SWING BOT</span>
  <span class="badge paper" id="modeBadge">PAPER</span>
  <span class="badge" id="runBadge">…</span>
  <span class="spot-btc" id="spot">—</span>
  <a class="nav-link" href="/trade">/trade</a>
  <a class="nav-link" href="/crypto">/crypto</a>
  <a class="nav-link" href="/whales">/whales</a>
  <span class="clock" id="clock"></span>
</header>

<div class="range-banner" id="rangeBanner">
  <div class="range-head">
    <span class="range-side" id="rangeSide">—</span>
    <span class="range-buy" id="rangeBuy">BUY —</span>
    <span class="range-arr">→</span>
    <span class="range-sell" id="rangeSell">SELL —</span>
    <span class="gate-chip wait" id="gateChip">…</span>
  </div>
  <div class="range-sub" id="rangeSub">calibrated from graded banner-target history</div>
  <div class="meter-wrap">
    <div class="meter" id="meter"></div>
  </div>
</div>

<div class="tiles">
  <div class="tile hero"><div class="k">day p&amp;l</div><div class="v" id="dayPnl">—</div>
    <div class="s" id="dayStop"></div></div>
  <div class="tile"><div class="k">bankroll</div><div class="v" id="bankroll">—</div>
    <div class="s" id="bankrollSub"></div></div>
  <div class="tile"><div class="k">loss budget</div><div class="v" id="lossBudget">—</div>
    <div class="s" id="lossBudgetSub"></div></div>
  <div class="tile"><div class="k">win rate</div><div class="v" id="winRate">—</div>
    <div class="s" id="winRateSub"></div></div>
  <div class="tile"><div class="k">net avg / trade</div><div class="v" id="netAvg">—</div>
    <div class="s" id="nTrades"></div></div>
  <div class="tile"><div class="k">open plays</div><div class="v" id="nOpen">—</div>
    <div class="s" id="riskSub"></div></div>
  <div class="tile"><div class="k">range win line</div><div class="v" id="histWin">—</div>
    <div class="s" id="histWinSub"></div></div>
  <div class="tile"><div class="k">gate progress</div><div class="v" id="gateProg">—</div>
    <div class="s" id="gateProgSub"></div><div class="gatebar"><i id="gateBar" style="width:0%"></i></div></div>
</div>

<div class="grid">
  <div class="panel">
    <h3>Controls</h3>
    <div class="controls">
      <button class="warn" onclick="ctl('pause')">Pause</button>
      <button class="go" onclick="ctl('resume')">Resume</button>
      <button class="danger" onclick="ctl('flatten')">Flatten</button>
      <button id="liveToggle" disabled>LIVE 🔒</button>
    </div>
    <div class="unlock" id="unlock"></div>
  </div>

  <div class="panel">
    <h3>Open plays</h3>
    <div class="tbl-wrap"><table id="openTable"><thead><tr>
      <th>ticker</th><th>side</th><th>qty</th><th>cost</th><th>entry</th><th>live</th>
      <th>uP&amp;L</th><th>target</th><th>stretch</th>
    </tr></thead><tbody></tbody></table></div>
    <div class="empty" id="openEmpty" hidden>flat — waiting for a flow flip inside the buy range</div>
  </div>

  <div class="panel">
    <h3>Range calibration <span class="dim" style="text-transform:none">(learned from graded history)</span></h3>
    <div class="tbl-wrap"><table id="calTable"><thead><tr>
      <th>side</th><th>sell-low off</th><th>sell-high off</th><th>win hit</th>
      <th>stretch hit</th><th>touch</th><th>n</th>
    </tr></thead><tbody></tbody></table></div>
    <div class="empty" id="calNote"></div>
  </div>

  <div class="panel">
    <h3>Settlement grades <span class="dim" style="text-transform:none">(every exit vs holding to expiry)</span></h3>
    <div class="grade-strip" id="gradeStrip"></div>
    <div class="grade-edge" id="gradeEdge"></div>
    <div class="empty" id="gradeEmpty" hidden>no settled trades graded yet</div>
  </div>

  <div class="panel">
    <h3>Exit reasons</h3>
    <div class="tbl-wrap"><table id="reasonTable"><thead><tr>
      <th>reason</th><th>n</th><th>win%</th><th>net avg</th>
    </tr></thead><tbody></tbody></table></div>
  </div>

  <div class="panel">
    <h3>EV gate <span class="dim" style="text-transform:none">(learned skip buckets)</span></h3>
    <div class="tbl-wrap"><table id="evTable"><thead><tr>
      <th>bucket</th><th>n</th><th>win%</th><th>net avg</th><th>gate</th>
    </tr></thead><tbody></tbody></table></div>
    <div class="empty" id="evEmpty" hidden>no closed trades bucketed yet</div>
    <div class="empty" id="evNote"></div>
  </div>

  <div class="panel">
    <h3>Replay tuner <span class="dim" style="text-transform:none">(nightly, suggestion only)</span></h3>
    <div id="tunerBody" class="empty">no tuner report yet</div>
  </div>

  <div class="panel wide">
    <h3>Trades</h3>
    <div class="tbl-wrap"><table id="tradeTable"><thead><tr>
      <th>time</th><th>ticker</th><th>side</th><th>qty</th><th>cost</th><th>in</th><th>out</th>
      <th>reason</th><th>net</th><th>vs settle</th>
    </tr></thead><tbody></tbody></table></div>
    <div class="empty" id="tradeEmpty" hidden>no closed trades yet</div>
  </div>

  <div class="panel wide">
    <h3>Decision log</h3>
    <div id="log"></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const money = v => (v < 0 ? '-' : '+') + '$' + Math.abs(v).toFixed(2);
const pct = v => v == null ? '—' : (v * 100).toFixed(0) + '%';
const esc = s => String(s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function fj(url, fallback) {
  try { const r = await fetch(url); if (!r.ok) throw 0; return await r.json(); }
  catch (e) { return fallback; }
}

async function ctl(cmd) {
  await fetch('/api/bot/control', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({cmd})});
  pollBot();
}

setInterval(() =>
  $('clock').textContent = new Date().toISOString().slice(11,19) + 'Z', 1000);

/* — mirror of bot_core.compute_side_ranges, for display — */
function sideRanges(price, yesPct, side, off) {
  const up = side === 'YES';
  const buyC = (up ? price : 1 - price) * 100;
  const fairC = up ? yesPct : 100 - yesPct;
  const bl = Math.max(1, buyC - 3), bh = Math.min(95, buyC + 2);
  const sl = Math.max(bh + 2, Math.min(95, bh + 10 - (off.sell_low_offset_c || 0)));
  const sh = Math.max(sl + 2, Math.min(95, fairC - (off.sell_high_offset_c || 0)));
  return {bl, bh, sl, sh, buyC, fairC};
}

function drawMeter(r, askC) {
  const lo = Math.max(0, Math.floor(r.bl - 4)), hi = Math.min(100, Math.ceil(r.sh + 4));
  const X = v => ((Math.min(Math.max(v, lo), hi) - lo) / (hi - lo) * 100).toFixed(2) + '%';
  const W = (a, b) => ((Math.min(b, hi) - Math.max(a, lo)) / (hi - lo) * 100).toFixed(2) + '%';
  let h = '';
  for (let g = Math.ceil(lo / 5) * 5; g <= hi; g += 5)
    h += `<div class="grid" style="left:${X(g)}"></div>`
       + `<div class="grid-label" style="left:${X(g)}">${g}</div>`;
  h += `<div class="band buy" style="left:${X(r.bl)};width:${W(r.bl, r.bh)}">BUY</div>`
     + `<div class="band sell" style="left:${X(r.sl)};width:${W(r.sl, r.sh)}">SELL</div>`;
  h += `<div class="tick px" style="left:${X(r.buyC)}"></div>`
     + `<div class="tick-label px" style="left:${X(r.buyC)}">${r.buyC.toFixed(1)}¢</div>`;
  if (askC != null && Math.abs(askC - r.buyC) > 0.6)
    h += `<div class="tick ask" style="left:${X(askC)}"></div>`
       + `<div class="tick-label ask" style="left:${X(askC)}">ask ${askC.toFixed(1)}¢</div>`;
  $('meter').innerHTML = h;
}

let offsets = {yes:{}, no:{}};
let lastSig = null;
// mirrors backtest_gate.fee: per-contract Kalshi fee, ceil to the cent
const kfee = p => Math.ceil(7 * p * (1 - p)) / 100;
// mirrors PaperBroker.sell: achievable sell = ask - spread, floor 1c
function liveMark(play, sig) {
  if (!sig || sig.status !== 'ok') return null;
  const ask = play.side === 'YES' ? sig.yes_ask : sig.no_ask;
  if (ask == null) return null;
  const sell = Math.max(0.01, ask - Math.max(0, sig.spread || 0));
  const upnl = (sell - play.entry.price) * play.qty
             - play.entry.fee_total - kfee(sell) * play.qty;
  return {sell, upnl};
}

async function pollSignal() {
  const [s, spot] = await Promise.all([
    fj('/api/crypto/signal', null), fj('/api/crypto/spot', null)]);
  lastSig = s;
  const btc = (spot && spot.btc) || (s && s.spot);
  $('spot').textContent = btc ? '₿ $' + btc.toLocaleString(undefined,
      {maximumFractionDigits:0}) : '—';
  const banner = $('rangeBanner');
  if (!s || s.status !== 'ok' || s.price == null) {
    banner.classList.add('waiting');
    $('gateChip').textContent = 'NO MARKET'; $('gateChip').className = 'gate-chip wait';
    return;
  }
  const side = s.direction === 'YES' ? 'YES' : 'NO';
  const off = offsets[side.toLowerCase()] || {};
  const r = sideRanges(s.price, s.yes_pct, side, off);
  const ask = side === 'YES' ? s.yes_ask : s.no_ask;
  const askC = ask != null ? ask * 100 : null;
  const decided = s.price <= 0.05 || s.price >= 0.95 || (s.mins_left || 0) < 2;
  banner.classList.toggle('waiting', decided);
  $('rangeSide').textContent = side;
  $('rangeSide').className = 'range-side ' + side.toLowerCase();
  $('rangeBuy').textContent = `BUY ${r.bl.toFixed(1)}–${r.bh.toFixed(1)}¢`;
  $('rangeSell').textContent = `SELL ${r.sl.toFixed(1)}–${r.sh.toFixed(1)}¢`;
  const chip = $('gateChip');
  if (decided) { chip.textContent = 'MARKET DECIDED — WAITING'; chip.className = 'gate-chip wait'; }
  else if (askC == null) { chip.textContent = 'NO QUOTE'; chip.className = 'gate-chip wait'; }
  else if (askC >= r.bl && askC <= r.bh) {
    chip.textContent = 'ASK IN BUY ZONE ✓'; chip.className = 'gate-chip in';
  } else {
    chip.textContent = askC > r.bh ? 'ASK ABOVE BUY ZONE — BOT SKIPS'
                                   : 'ASK BELOW BUY ZONE — BOT SKIPS';
    chip.className = 'gate-chip out';
  }
  $('rangeSub').textContent =
    `${s.ticker || ''} · flow fair ${r.fairC.toFixed(0)}¢ · win line ${r.sl.toFixed(1)}¢ `
    + `(edge +${Math.round(r.sl - r.bh)}¢) · ${(s.mins_left || 0).toFixed(1)}m left `
    + `· offsets from graded range history`;
  drawMeter(r, askC);
}

async function pollCalibration() {
  const [o, h] = await Promise.all([
    fj('/api/crypto/banner_offsets', null),
    fj('/api/crypto/banner_history?limit=1', {stats:{}})]);
  if (o && o.yes) offsets = o;
  const rows = ['yes', 'no'].map(k => {
    const v = (o && o[k]) || {};
    return `<tr><td><span class="side-chip ${k}">${k.toUpperCase()}</span></td>
      <td>${(v.sell_low_offset_c || 0).toFixed(1)}¢</td>
      <td>${(v.sell_high_offset_c || 0).toFixed(1)}¢</td>
      <td>${pct(v.low_hit_rate)}</td><td>${pct(v.high_hit_rate)}</td>
      <td>${pct(v.buy_touch_rate)}</td><td>${v.n || 0}</td></tr>`;
  }).join('');
  $('calTable').tBodies[0].innerHTML = rows;
  const st = (h && h.stats) || {};
  if (st.entered) {
    $('histWin').textContent = st.win_pct.toFixed(0) + '%';
    $('histWinSub').textContent =
      `${st.wins}/${st.entered} graded · stretch ${st.str_pct.toFixed(0)}%`;
    $('calNote').textContent =
      `${st.total} range snapshots graded all-time; targets tuned to 90% win / 60% stretch`;
  }
}

async function pollBot() {
  const d = await fj('/api/bot/status', null);
  if (!d) { $('runBadge').textContent = 'API ERR'; $('runBadge').className = 'badge offline'; return; }
  const s = d.state || {}, cfg = d.config || {};
  $('modeBadge').textContent = (s.mode || 'paper').toUpperCase();
  $('modeBadge').className = 'badge ' + (s.mode === 'live' ? 'live' : 'paper');
  const fresh = s.heartbeat && (Date.now() / 1000 - s.heartbeat) < 30;
  const run = !fresh ? ['BOT OFFLINE', 'offline'] : s.halted ? ['HALTED', 'halted']
            : s.paused ? ['PAUSED', 'paused'] : ['RUNNING', 'running'];
  $('runBadge').textContent = run[0];
  $('runBadge').className = 'badge ' + run[1];
  $('logo').classList.toggle('off', !fresh);

  $('bankroll').textContent = s.bankroll ? '$' + s.bankroll.toFixed(2) : '—';
  $('bankrollSub').textContent = cfg.paper_bankroll
    ? `fixed paper stake · ${((cfg.risk_pct || 0) * 100).toFixed(0)}% per trade` : '';
  const cap = cfg.max_loss_usd || 0, tot = s.total_pnl || 0;
  $('dayPnl').textContent = money(s.day_pnl || 0);
  $('dayPnl').className = 'v ' + ((s.day_pnl || 0) >= 0 ? 'pos' : 'neg');
  $('dayStop').innerHTML = (s.bankroll
    ? `total <span class="${tot >= 0 ? 'pos' : 'neg'}" style="font-weight:800">${money(tot)}</span>`
      + ` · halt at -$${((cfg.day_stop_pct || .1) * s.bankroll).toFixed(0)}` : '');
  if (cap > 0) {
    const head = Math.max(0, cap + Math.min(0, tot));
    $('lossBudget').textContent = '$' + head.toFixed(0) + ' / $' + cap.toFixed(0);
    $('lossBudget').className = 'v ' + (head <= 0 ? 'neg' : head < cap * .3 ? '' : 'pos');
    $('lossBudgetSub').textContent = head <= 0
      ? 'MAX LOSS HIT — trading blocked'
      : `total ${money(tot)} · sizes ${((cfg.trade_risk_frac || .1) * 100).toFixed(0)}% of headroom`;
  } else { $('lossBudget').textContent = 'off'; $('lossBudgetSub').textContent = ''; }

  const a = (d.stats && d.stats.all_time) || {n: 0};
  const td = (d.stats && d.stats.today) || {n: 0};
  $('winRate').textContent = a.n ? a.win_pct.toFixed(0) + '%' : '—';
  $('winRateSub').textContent = td.n ? `today ${td.win_pct.toFixed(0)}% of ${td.n}` : 'no trades today';
  $('netAvg').textContent = a.n ? money(a.net_avg) : '—';
  $('netAvg').className = 'v ' + (a.n && a.net_avg >= 0 ? 'pos' : a.n ? 'neg' : '');
  $('nTrades').textContent = a.n + ' closed';
  const open = Object.entries(s.open_plays || {});
  $('nOpen').textContent = open.length;
  $('riskSub').textContent = `max ${cfg.max_open_plays || 3}`
    + (cfg.use_ranges === false ? ' · RANGE GATE OFF' : ' · range-gated');
  $('unlock').textContent = 'LIVE unlock: ' + ((d.unlock && d.unlock.reason) || '—');

  $('openTable').tBodies[0].innerHTML = open.map(([t, p]) => {
    const r = p.ranges || {};
    const m = (lastSig && lastSig.ticker === t) ? liveMark(p, lastSig) : null;
    const liveTd = m ? `${(m.sell * 100).toFixed(1)}¢` : '<span class="dim">—</span>';
    const upnlTd = m ? `<span class="${m.upnl >= 0 ? 'pos' : 'neg'}">${money(m.upnl)}</span>`
                     : '<span class="dim">—</span>';
    return `<tr><td>${esc(t)}</td>
      <td><span class="side-chip ${p.side.toLowerCase()}">${p.side}</span></td>
      <td>${p.qty}</td>
      <td class="notional">$${(p.entry.price * p.qty).toFixed(2)}</td>
      <td>${(p.entry.price * 100).toFixed(1)}¢</td>
      <td>${liveTd}</td><td>${upnlTd}</td>
      <td>${r.sell_low != null ? r.sell_low.toFixed(1) + '¢' : '<span class="dim">—</span>'}</td>
      <td>${r.sell_high != null ? r.sell_high.toFixed(1) + '¢' : '<span class="dim">—</span>'}</td></tr>`;
  }).join('');
  $('openEmpty').hidden = open.length > 0;

  const floor = cfg.ev_gate_min_samples || 12;
  const buckets = Object.entries(d.ev_buckets || {})
    .sort((x, y) => y[1].n - x[1].n);
  $('evTable').tBodies[0].innerHTML = buckets.map(([b, v]) => {
    const gated = v.n >= floor && v.net_avg < 0;
    return `<tr><td>${esc(b)}</td><td>${v.n}</td><td>${v.win_pct.toFixed(0)}%</td>
      <td class="${v.net_avg >= 0 ? 'pos' : 'neg'}">${money(v.net_avg)}</td>
      <td>${gated ? '<span class="neg" style="font-weight:800">SKIP</span>'
                  : v.n < floor ? `<span class="dim">${v.n}/${floor}</span>`
                  : '<span class="pos">open</span>'}</td></tr>`;
  }).join('');
  $('evEmpty').hidden = buckets.length > 0;
  $('evNote').textContent = cfg.ev_gate === false
    ? 'EV gate disabled in config'
    : `buckets skip only at ≥${floor} samples with negative net avg`;

  const tn = d.tuner;
  if (tn) {
    const fmt = p => Object.entries(p || {}).map(([k, v]) => `${k}=${v}`).join(' · ');
    const res = r => r ? `${r.trades} trades, ${r.win_pct.toFixed(0)}% win, `
      + `<span class="${r.net_total >= 0 ? 'pos' : 'neg'}">${money(r.net_total)}</span>` : '—';
    $('tunerBody').innerHTML =
      `<div style="font-family:var(--sans);font-size:12px;line-height:1.9">
       <div><span class="dim">ran ${esc(tn.day)} · ${tn.window_days}d window · `
      + `${tn.train_rows}+${tn.validate_rows} rows (train+validate)</span></div>
       <div>current: <b>${esc(fmt(tn.current && tn.current.params))}</b></div>
       <div class="dim">→ train ${res(tn.current && tn.current.train)} · `
      + `validate ${res(tn.current && tn.current.validate)}</div>
       <div style="margin-top:6px">${tn.suggested
         ? 'suggested: <b class="pos">' + esc(fmt(tn.suggested)) + '</b>'
         : '<b>keep current config</b>'}</div>
       <div class="dim">${esc(tn.note || '')}</div></div>`;
  }

  $('reasonTable').tBodies[0].innerHTML =
    Object.entries((d.stats && d.stats.by_exit_reason) || {}).map(([k, v]) =>
      `<tr><td>${esc(k)}</td><td>${v.n}</td><td>${v.win_pct.toFixed(0)}%</td>
       <td class="${v.net_avg >= 0 ? 'pos' : 'neg'}">${money(v.net_avg)}</td></tr>`).join('');

  // settlement grades: join to trades by ticker|entry_ts, fill summary panel
  const VLABEL = {clean_win:'clean win', lucky_exit:'lucky exit', good_stop:'good stop',
                  whipsaw_stop:'whipsaw', good_exit:'good exit', left_money:'left $',
                  ungraded:'no data'};
  const gmap = {};
  (d.grades || []).forEach(g => gmap[g.ticker + '|' + g.entry_ts] = g);
  const gs = d.grade_summary || {n: 0};
  $('gradeEmpty').hidden = gs.n > 0;
  $('gradeStrip').innerHTML = Object.entries(gs.verdicts || {})
    .sort((x, y) => y[1] - x[1])
    .map(([v, n]) => `<span class="vchip ${esc(v)}">${VLABEL[v] || esc(v)} × ${n}</span>`)
    .join('');
  $('gradeEdge').innerHTML = gs.n ? (
    `exits vs holding to expiry: <b class="${gs.exit_edge_usd >= 0 ? 'pos' : 'neg'}">`
    + `${money(gs.exit_edge_usd)}</b> across ${gs.n} settled`
    + ` · stops alone <b class="${gs.stops_saved_usd >= 0 ? 'pos' : 'neg'}">`
    + `${money(gs.stops_saved_usd)}</b>`
    + (gs.gaps ? ` · <span class="dim">${gs.gaps} with feed gaps</span>` : '')) : '';

  const GATE_N = 100;
  $('gateProg').textContent = `${a.n}/${GATE_N}`;
  $('gateProgSub').textContent = 'settled trades before sizing review';
  $('gateBar').style.width = Math.min(100, a.n / GATE_N * 100).toFixed(0) + '%';

  const trades = (d.trades || []).slice().reverse();
  $('tradeTable').tBodies[0].innerHTML = trades.map(t => {
    const g = gmap[t.ticker + '|' + t.entry_ts];
    const vTd = g && g.verdict
      ? `<span class="vchip ${esc(g.verdict)}"` +
        (g.delta_vs_held != null
          ? ` title="exit ${g.delta_vs_held >= 0 ? 'beat' : 'trailed'} holding by $${Math.abs(g.delta_vs_held).toFixed(2)}"` : '')
        + `>${VLABEL[g.verdict] || esc(g.verdict)}</span>`
      : '<span class="dim">settling…</span>';
    return `<tr><td class="dim">${new Date(t.exit_ts * 1000).toISOString().slice(5,16).replace('T',' ')}</td>
     <td>${esc(t.ticker)}</td>
     <td><span class="side-chip ${t.side.toLowerCase()}">${t.side}</span></td>
     <td>${t.qty}</td>
     <td class="notional">$${(t.entry_price * t.qty).toFixed(2)}</td>
     <td>${(t.entry_price * 100).toFixed(1)}¢</td>
     <td>${(t.exit_price * 100).toFixed(1)}¢</td><td>${esc(t.exit_reason)}</td>
     <td class="${t.net_pnl >= 0 ? 'pos' : 'neg'}">${money(t.net_pnl)}</td>
     <td>${vTd}</td></tr>`;
  }).join('');
  $('tradeEmpty').hidden = trades.length > 0;

  $('log').innerHTML = (d.events || []).slice().reverse().map(e =>
    `<div><span class="t">${new Date(e.ts * 1000).toISOString().slice(11,19)}</span>`
    + `<span class="a ${esc(e.action)}">${esc(e.action)}</span>`
    + `${esc(e.ticker || '')} <span class="dim">${esc(e.reason || '')}</span></div>`).join('');
}

pollCalibration(); pollBot(); pollSignal();
setInterval(pollBot, 3000);
setInterval(pollSignal, 3000);
setInterval(pollCalibration, 30000);
</script></body></html>"""
