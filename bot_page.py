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
.badge.sess { font-size:9px; padding:2px 8px; cursor:pointer; }
.badge.sess.wd_day    { color:var(--blue);   border-color:var(--blue-bd);   background:var(--blue-bg); }
.badge.sess.wd_night  { color:var(--blue);   border-color:var(--blue-bd);   opacity:.55; }
.badge.sess.we_day    { color:var(--purple); border-color:var(--purple-bd, var(--border)); background:var(--purple-bg); }
.badge.sess.we_night  { color:var(--purple); border-color:var(--purple-bd, var(--border)); opacity:.55; }
.badge.sess.off { opacity:.25; filter:grayscale(1); }
.ev-summary { display:flex; flex-wrap:wrap; gap:8px; margin:0 0 12px; }
.ev-summary .tile { flex:1 1 130px; border:1px solid var(--hair); border-left:1px solid var(--hair);
                    border-radius:8px; padding:8px 12px; background:var(--bg2); }
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

/* ── tabs ── */
.tabs { display:flex; gap:2px; margin:14px 20px 0; }
.tab-btn { background:var(--bg2); border:1px solid var(--border); border-bottom:none;
           color:var(--mute); font:inherit; font-size:11px; font-weight:800; letter-spacing:.5px;
           text-transform:uppercase; padding:7px 16px; border-radius:8px 8px 0 0; cursor:pointer; }
.tab-btn.active { color:var(--fg); background:var(--bg3); }
.tab-btn:hover:not(.active) { color:var(--fg); }
#overviewTab, #deepdiveTab { border-top:1px solid var(--border); padding-top:1px; }
.section-label { font-size:10px; font-weight:800; letter-spacing:1px; text-transform:uppercase;
                 color:var(--mute); margin:14px 20px 2px; }
.section-label:first-child { margin-top:6px; }

/* ── flagged findings ── */
.findings { margin:14px 20px 0; display:flex; flex-direction:column; border:1px solid var(--border);
            border-radius:var(--radius); overflow:hidden; background:var(--bg2); box-shadow:var(--card-shadow); }
.finding-row { display:flex; align-items:baseline; gap:10px; padding:7px 14px; font-size:12px;
               border-top:1px solid var(--hair); }
.finding-row:first-child { border-top:none; }
.finding-row .fi-sev { font-size:9px; font-weight:900; padding:1px 7px; border-radius:99px; flex-shrink:0; }
.finding-row .fi-sev.warn { color:var(--red); border:1px solid var(--red-bd); background:var(--red-bg); }
.finding-row .fi-sev.info { color:var(--blue); border:1px solid var(--blue-bd); background:var(--blue-bg); }
.finding-row .fi-title { font-weight:700; flex-shrink:0; }
.finding-row .fi-detail { color:var(--mute); font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.findings.clear { padding:8px 14px; font-size:12px; color:var(--green); }

/* ── loop log (compact, embedded on Overview) ── */
.looplog-row { display:flex; gap:8px; padding:3px 14px; font-size:11px; border-top:1px solid var(--hair);
               white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.looplog-row:first-child { border-top:none; }
.looplog-row .lg-ts { color:var(--mute); flex-shrink:0; font-variant-numeric:tabular-nums; }
.looplog-row .lg-type { flex-shrink:0; font-weight:800; font-size:9px; color:var(--blue); align-self:center; }
.looplog-row .lg-msg { color:var(--fg); overflow:hidden; text-overflow:ellipsis; }

/* ── equity chart ── */
#eqWrap { position:relative; }
#eqWrap svg { display:block; width:100%; }
.eqtip { position:absolute; pointer-events:none; z-index:5;
         background:var(--bg3); border:1px solid var(--border); border-radius:7px;
         padding:7px 10px; font-size:11px; line-height:1.7; white-space:nowrap;
         box-shadow:0 6px 20px rgba(0,0,0,.45); font-variant-numeric:tabular-nums; }
.eqtip .dim { font-family:var(--sans); }
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

<div class="tabs">
  <button class="tab-btn active" id="tabBtnOverview" onclick="switchTab('overview')">Overview</button>
  <button class="tab-btn" id="tabBtnDeepdive" onclick="switchTab('deepdive')">Deep Dive</button>
</div>

<div id="overviewTab">
  <div class="findings" id="findingsBox"></div>

  <div class="ev-summary" id="poolTiles"></div>

  <div class="tiles">
    <div class="tile"><div class="k">win rate</div><div class="v" id="winRate">—</div>
      <div class="s" id="winRateSub"></div></div>
    <div class="tile"><div class="k">net avg / trade</div><div class="v" id="netAvg">—</div>
      <div class="s" id="nTrades"></div></div>
    <div class="tile"><div class="k">open plays</div><div class="v" id="nOpen">—</div>
      <div class="s" id="riskSub"></div></div>
    <div class="tile"><div class="k">range win line</div><div class="v" id="histWin">—</div>
      <div class="s" id="histWinSub"></div></div>
    <div class="tile"><div class="k">gate progress</div><div class="v" id="gateProg">—</div>
      <div class="s" id="gateWd">wd —</div><div class="gatebar"><i id="gateWdBar" style="width:0%"></i></div>
      <div class="s" id="gateWe">we —</div><div class="gatebar"><i id="gateWeBar" style="width:0%;background:var(--purple)"></i></div></div>
  </div>

  <div class="grid" style="grid-template-columns:1fr 1fr">
    <div class="panel">
      <h3>Equity <span class="dim" style="text-transform:none">(cumulative net P&amp;L, last 50 settled · per-trade net below)</span></h3>
      <div id="eqWrap"><svg id="eqSvg" height="240"></svg><div id="eqTip" class="eqtip" hidden></div></div>
      <div class="empty" id="eqEmpty" hidden>no closed trades yet — the curve starts with the first settle</div>
    </div>

    <div class="panel">
      <h3>Daily P&amp;L <span class="dim" style="text-transform:none">(net per UTC day)</span></h3>
      <svg id="daySvg" height="240" style="display:block;width:100%"></svg>
    </div>
  </div>

  <div class="panel wide" style="margin:14px 20px 0">
    <h3>Loop log <span class="dim" style="text-transform:none">(live marketloop commentary — full feed at /trade)</span></h3>
    <div id="looplogBody"></div>
  </div>
</div>

<div id="deepdiveTab" hidden>
  <div class="section-label">Performance</div>
  <div class="grid">
    <div class="panel wide">
      <h3>BTC <span class="dim" style="text-transform:none">(15m candles, last 8h · key level dashed)</span></h3>
      <div id="cdWrap" style="position:relative"><svg id="cdSvg" height="200" style="display:block;width:100%"></svg>
        <div id="cdTip" class="eqtip" hidden></div></div>
    </div>

    <div class="panel">
      <h3>Session map <span class="dim" style="text-transform:none">(capitalize / skip)</span></h3>
      <div class="tbl-wrap"><table id="sessTable"><thead><tr>
        <th>session</th><th>n</th><th>win%</th><th>net avg</th><th>zone</th>
      </tr></thead><tbody></tbody></table></div>
    </div>

    <div class="panel wide">
      <h3>Pool P&amp;L by day <span class="dim" style="text-transform:none">(net per pool, UTC date rows)</span></h3>
      <div class="tbl-wrap"><table id="poolDateTable"><thead><tr></tr></thead><tbody></tbody></table></div>
      <div class="empty" id="poolDateEmpty" hidden>no closed trades yet</div>
    </div>

    <div class="panel">
      <h3>Settlement grades <span class="dim" style="text-transform:none">(every exit vs holding to expiry)</span></h3>
      <div id="gradeMix" style="display:flex;gap:2px;height:10px;border-radius:5px;overflow:hidden;margin-bottom:10px"></div>
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
  </div>

  <div class="section-label">Risk &amp; Health</div>
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
  </div>

  <div class="section-label">Strategy Detail</div>
  <div class="grid">
    <div class="panel wide">
      <div class="range-banner" id="rangeBanner" style="margin:0">
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
    </div>

    <div class="panel wide">
      <h3>EV gate <span class="dim" style="text-transform:none">(learned skip buckets, by session)</span></h3>
      <div class="ev-summary" id="evSummary"></div>
      <div class="tbl-wrap"><table id="evTable"><thead><tr>
        <th>session</th><th>bucket</th><th>n</th><th>win%</th><th>net avg</th><th>gate</th>
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

function switchTab(name) {
  const overview = name === 'overview';
  $('overviewTab').hidden = !overview;
  $('deepdiveTab').hidden = overview;
  $('tabBtnOverview').classList.toggle('active', overview);
  $('tabBtnDeepdive').classList.toggle('active', !overview);
}

function renderFindings(findings) {
  const box = $('findingsBox');
  if (!findings || !findings.length) {
    box.className = 'findings clear';
    box.innerHTML = '✓ nothing flagged';
    return;
  }
  box.className = 'findings';
  box.innerHTML = findings.map(f => `<div class="finding-row">
      <span class="fi-sev ${f.severity}">${f.severity}</span>
      <span class="fi-title">${esc(f.title)}</span>
      <span class="fi-detail">${esc(f.detail || '')}</span>
    </div>`).join('');
}

async function pollLoopLog() {
  const j = await fj('/api/loop_log?limit=100', null);
  const rows = ((j && (j.entries || j.rows)) || []).filter(e => e.type !== 'HB').slice(0, 20);
  const el = $('looplogBody');
  if (!rows.length) { el.innerHTML = '<div class="empty">no recent commentary</div>'; return; }
  el.innerHTML = rows.map(e => {
    const t = new Date(e.ts * 1000).toISOString().slice(11, 19);
    return `<div class="looplog-row">
        <span class="lg-ts">${t}</span>
        <span class="lg-type">${esc(e.type || '')}</span>
        <span class="lg-msg">${esc(e.msg || '')}</span>
      </div>`;
  }).join('');
}
setInterval(pollLoopLog, 15000);

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

const SESSIONS = ['weekday_day', 'weekday_night', 'weekend_day', 'weekend_night'];
const sessCls = s => ({weekday_day:'wd_day', weekday_night:'wd_night',
                       weekend_day:'we_day', weekend_night:'we_night'}[s] || '');
const sessLabel = s => ({weekday_day:'WD·day', weekday_night:'WD·night',
                         weekend_day:'WE·day', weekend_night:'WE·night'}[s] || (s || '?'));
let offsets = {yes:{}, no:{}};
let lastSig = null;
let evFilter = null;      // null = all sessions, else 'weekday_day' etc.
let lastBotData = null, lastBotCfg = null;   // cached for the session-filter re-render
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

/* ── BTC candle chart ── */
let dayKey = null;   // day plan key level, drawn dashed when in range
async function pollCandles() {
  const d = await fj('/api/crypto/candles?mins=15&hours=8', null);
  const svg = $('cdSvg'), wrap = $('cdWrap');
  if (!d || !d.candles || d.candles.length < 2) { svg.innerHTML = ''; return; }
  const cs = d.candles, W = wrap.clientWidth || 800, H = 200, padL = 56, padR = 10, top = 8, bot = 18;
  let lo = Math.min(...cs.map(c => c.l)), hi = Math.max(...cs.map(c => c.h));
  if (dayKey && dayKey > lo - 200 && dayKey < hi + 200) { lo = Math.min(lo, dayKey); hi = Math.max(hi, dayKey); }
  const pad = (hi - lo) * 0.05 || 1; lo -= pad; hi += pad;
  const Y = v => top + (hi - v) / (hi - lo) * (H - top - bot);
  const X = i => padL + (i + 0.5) * (W - padL - padR) / cs.length;
  const cw = Math.max(3, Math.min(16, (W - padL - padR) / cs.length - 3));
  let h = '';
  const step = Math.max(50, Math.round((hi - lo) / 4 / 50) * 50);
  for (let g = Math.ceil(lo / step) * step; g <= hi; g += step)
    h += `<line x1="${padL}" x2="${W - padR}" y1="${Y(g)}" y2="${Y(g)}" stroke="var(--hair)"/>`
       + `<text x="${padL - 6}" y="${Y(g) + 3}" text-anchor="end" font-size="9" fill="var(--mute)">${g.toLocaleString()}</text>`;
  if (dayKey && dayKey >= lo && dayKey <= hi)
    h += `<line x1="${padL}" x2="${W - padR}" y1="${Y(dayKey)}" y2="${Y(dayKey)}" stroke="var(--yellow)"
           stroke-dasharray="5 4" opacity=".7"/>
          <text x="${W - padR}" y="${Y(dayKey) - 4}" text-anchor="end" font-size="9" fill="var(--yellow)">key ${dayKey.toLocaleString()}</text>`;
  cs.forEach((c, i) => {
    const up = c.c >= c.o, col = up ? 'var(--green)' : 'var(--red)';
    const yO = Y(c.o), yC = Y(c.c);
    h += `<line x1="${X(i)}" x2="${X(i)}" y1="${Y(c.h)}" y2="${Y(c.l)}" stroke="${col}" stroke-width="1.5"/>`
       + `<rect x="${(X(i) - cw / 2).toFixed(1)}" y="${Math.min(yO, yC).toFixed(1)}" width="${cw.toFixed(1)}"
           height="${Math.max(1.5, Math.abs(yO - yC)).toFixed(1)}" rx="1" fill="${col}"
           ${up ? 'fill-opacity=".35" stroke="' + col + '"' : ''}/>`;
  });
  const xt = t => new Date(t * 1000).toISOString().slice(11, 16) + 'Z';
  h += `<text x="${padL}" y="${H - 4}" font-size="9" fill="var(--mute)">${xt(cs[0].t)}</text>`
     + `<text x="${W - padR}" y="${H - 4}" text-anchor="end" font-size="9" fill="var(--mute)">${xt(cs[cs.length - 1].t)}</text>`;
  h += `<line id="cdHair" y1="${top}" y2="${H - bot}" stroke="var(--mute)" opacity="0" pointer-events="none"/>`;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('width', W);
  svg.innerHTML = h;
  svg.onmousemove = ev => {
    const r = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(cs.length - 1,
      Math.floor((ev.clientX - r.left - padL) / ((W - padL - padR) / cs.length))));
    const c = cs[i], hair = $('cdHair');
    hair.setAttribute('x1', X(i)); hair.setAttribute('x2', X(i)); hair.setAttribute('opacity', '.5');
    const tip = $('cdTip'); tip.hidden = false;
    const f = v => '$' + v.toLocaleString(undefined, {maximumFractionDigits: 0});
    tip.innerHTML = `<span class="dim">${xt(c.t)}</span> `
      + `O ${f(c.o)} H ${f(c.h)} L ${f(c.l)} C <b class="${c.c >= c.o ? 'pos' : 'neg'}">${f(c.c)}</b>`;
    tip.style.left = Math.min(Math.max(0, X(i) - 120), W - 300) + 'px'; tip.style.top = '4px';
  };
  svg.onmouseleave = () => { $('cdTip').hidden = true; $('cdHair').setAttribute('opacity', '0'); };
}

/* ── equity chart: step-line cumulative + diverging per-trade bars ── */
let _eq = null;   // {trades (chronological), gmap} for resize re-render
function renderEquity(trades, gmap) {
  _eq = {trades, gmap};
  const svg = $('eqSvg'), wrap = $('eqWrap');
  $('eqEmpty').hidden = trades.length > 0;
  wrap.hidden = trades.length === 0;
  if (!trades.length) return;
  const W = wrap.clientWidth || 600, H = 240;
  const padL = 46, padR = 12, curveT = 10, curveB = 130, barsT = 150, barsB = 232;
  const plotW = W - padL - padR, n = trades.length;
  const X = i => padL + (n === 1 ? plotW / 2 : i * plotW / (n - 1));
  // cumulative series, domain always includes 0
  let c = 0; const cum = trades.map(t => +(c += t.net_pnl).toFixed(2));
  const lo = Math.min(0, ...cum), hi = Math.max(0, ...cum);
  const Y = v => curveB - (v - lo) / ((hi - lo) || 1) * (curveB - curveT);
  // bars: symmetric domain so equal wins/losses read equal
  const bmax = Math.max(...trades.map(t => Math.abs(t.net_pnl)), 0.01);
  const bzero = (barsT + barsB) / 2;
  const BY = v => bzero - v / bmax * (barsB - barsT) / 2;
  const gv = 'var(--hair)', tick = v => '$' + v.toFixed(2).replace('.00', '');
  let h = '';
  // gridlines + y labels: min / zero / max of the cumulative scale
  const marks = [...new Set([lo, 0, hi])];
  for (const m of marks) {
    const em = m === 0 ? 'var(--border)' : gv;
    h += `<line x1="${padL}" x2="${W - padR}" y1="${Y(m)}" y2="${Y(m)}" stroke="${em}" stroke-width="1"/>`
       + `<text x="${padL - 6}" y="${Y(m) + 3}" text-anchor="end" font-size="9" fill="var(--mute)">${tick(m)}</text>`;
  }
  h += `<line x1="${padL}" x2="${W - padR}" y1="${bzero}" y2="${bzero}" stroke="var(--border)" stroke-width="1"/>`;
  // step-after equity path
  let p = `M ${X(0)} ${Y(cum[0])}`;
  for (let i = 1; i < n; i++) p += ` H ${X(i)} V ${Y(cum[i])}`;
  h += `<path d="${p}" fill="none" stroke="var(--blue)" stroke-width="2" stroke-linejoin="round"/>`;
  // direct label on the last point only
  const endV = cum[n - 1];
  h += `<circle cx="${X(n - 1)}" cy="${Y(endV)}" r="3" fill="var(--blue)"/>`
     + `<text x="${Math.min(X(n - 1) + 6, W - padR)}" y="${Y(endV) - 6}" text-anchor="end" font-size="10"
          font-weight="800" fill="var(--fg)">${money(endV)}</text>`;
  // per-trade diverging bars, 2px gap, floor width 2px
  const bw = Math.max(2, Math.min(14, plotW / n - 2));
  trades.forEach((t, i) => {
    const v = t.net_pnl, y = BY(v);
    h += `<rect x="${(X(i) - bw / 2).toFixed(1)}" y="${Math.min(y, bzero).toFixed(1)}"
           width="${bw.toFixed(1)}" height="${Math.max(1, Math.abs(y - bzero)).toFixed(1)}" rx="1.5"
           fill="var(--${v >= 0 ? 'green' : 'red'})"/>`;
  });
  // x labels: first and last settle times
  const xl = ts => new Date(ts * 1000).toISOString().slice(5, 16).replace('T', ' ');
  h += `<text x="${padL}" y="${H - 1}" font-size="9" fill="var(--mute)">${xl(trades[0].exit_ts)}</text>`
     + `<text x="${W - padR}" y="${H - 1}" text-anchor="end" font-size="9" fill="var(--mute)">${xl(trades[n - 1].exit_ts)}</text>`;
  h += `<line id="eqHair" y1="${curveT}" y2="${barsB}" stroke="var(--mute)" stroke-width="1" opacity="0" pointer-events="none"/>`;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('width', W); svg.setAttribute('height', H);
  svg.innerHTML = h;
  svg.onmousemove = ev => {
    const r = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(n - 1,
      Math.round((ev.clientX - r.left - padL) / (plotW / Math.max(1, n - 1)))));
    const hair = $('eqHair'); hair.setAttribute('x1', X(i)); hair.setAttribute('x2', X(i));
    hair.setAttribute('opacity', '.5');
    const t = trades[i], g = gmap[t.ticker + '|' + t.entry_ts];
    const tip = $('eqTip'); tip.hidden = false;
    tip.innerHTML = `<span class="dim">${xl(t.exit_ts)}</span> ${esc(t.ticker)}<br>`
      + `<span class="side-chip ${t.side.toLowerCase()}">${t.side}</span> ${t.qty} × ${(t.entry_price * 100).toFixed(0)}¢`
      + ` ($${(t.entry_price * t.qty).toFixed(2)}) · ${esc(t.exit_reason)}<br>`
      + `net <b class="${t.net_pnl >= 0 ? 'pos' : 'neg'}">${money(t.net_pnl)}</b>`
      + ` · total <b class="${cum[i] >= 0 ? 'pos' : 'neg'}">${money(cum[i])}</b>`
      + (g && g.verdict ? ` · <span class="vchip ${esc(g.verdict)}">${esc(g.verdict.replace('_', ' '))}</span>` : '');
    const tx = Math.min(Math.max(0, X(i) - 90), W - 220);
    tip.style.left = tx + 'px'; tip.style.top = '6px';
  };
  svg.onmouseleave = () => { $('eqTip').hidden = true; $('eqHair').setAttribute('opacity', '0'); };
}
window.addEventListener('resize', () => _eq && renderEquity(_eq.trades, _eq.gmap));

function renderPoolByDate(d) {
  const byDate = d.pool_by_date || {};
  const dates = Object.keys(byDate).sort().reverse().slice(0, 30);
  $('poolDateTable').tHead.rows[0].innerHTML =
    '<th>date</th>' + SESSIONS.map(p => `<th>${sessLabel(p)}</th>`).join('');
  $('poolDateTable').tBodies[0].innerHTML = dates.map(day => {
    const row = byDate[day] || {};
    return `<tr><td>${day}</td>` + SESSIONS.map(p => {
      const v = row[p];
      return v == null ? '<td class="dim">—</td>'
        : `<td class="${v >= 0 ? 'pos' : 'neg'}">${money(v)}</td>`;
    }).join('') + '</tr>';
  }).join('');
  $('poolDateEmpty').hidden = dates.length > 0;
}

function renderPoolTiles(s) {
  const pools = s.pools || {};
  $('poolTiles').innerHTML = SESSIONS.map(p => {
    const ps = pools[p] || {};
    const dp = ps.day_pnl || 0;
    const bankroll = ps.bankroll || 0;
    const status = ps.halted ? '<span class="neg" style="font-weight:800">HALTED</span>'
                 : ps.loss_capped ? '<span class="neg" style="font-weight:800">LOSS CAP</span>'
                 : '<span class="dim">running</span>';
    return `<div class="tile">
        <div class="k"><span class="badge sess ${sessCls(p)}">${sessLabel(p)}</span></div>
        <div class="v ${dp >= 0 ? 'pos' : 'neg'}">${money(dp)}</div>
        <div class="s">bankroll $${bankroll.toFixed(2)} · ${status}</div>
      </div>`;
  }).join('');
}

function renderEvGate(d, cfg) {
  const floor = cfg.ev_gate_min_samples || 12;
  // bucket key: side|price|mins|momentum|session — split off the trailing
  // session tag rather than assuming a fixed part count, so an older/
  // shorter bucket key (pre-session-tagging history) degrades gracefully.
  const splitBucket = b => {
    const i = b.lastIndexOf('|');
    const sess = SESSIONS.includes(b.slice(i + 1)) ? b.slice(i + 1) : null;
    return {label: sess ? b.slice(0, i) : b, sess};
  };
  const allBuckets = Object.entries(d.ev_buckets || {})
    .map(([b, v]) => ({...splitBucket(b), n: v.n, win_pct: v.win_pct, net_avg: v.net_avg}));

  // Per-session rollup: total trades, win%, net avg, and how many buckets
  // within that session have reached the gate floor vs are still gated
  // negative — this is the "gate progress by market" view at a glance,
  // no need to scan the raw bucket table to answer it.
  $('evSummary').innerHTML = SESSIONS.map(s => {
    const bs = allBuckets.filter(x => x.sess === s);
    const n = bs.reduce((a, x) => a + x.n, 0);
    const net = bs.reduce((a, x) => a + x.n * x.net_avg, 0);
    const wins = bs.reduce((a, x) => a + x.n * x.win_pct / 100, 0);
    const gated = bs.filter(x => x.n >= floor && x.net_avg < 0).length;
    const active = evFilter === s;
    return `<div class="tile" style="cursor:pointer;${active ? 'border-color:var(--blue-bd)' : ''}"
              onclick="evFilter = evFilter === '${s}' ? null : '${s}'; renderEvGate(lastBotData, lastBotCfg)">
        <div class="k"><span class="badge sess ${sessCls(s)}">${sessLabel(s)}</span></div>
        <div class="v">${n ? money(net) : '—'}</div>
        <div class="s">${n} trades${n ? `, ${(100 * wins / n).toFixed(0)}% win` : ''}${gated ? ` · ${gated} gated` : ''}</div>
      </div>`;
  }).join('');

  const shown = evFilter ? allBuckets.filter(x => x.sess === evFilter) : allBuckets;
  shown.sort((x, y) => y.n - x.n);
  const evMax = Math.max(...shown.map(x => Math.abs(x.net_avg)), 0.01);
  $('evTable').tBodies[0].innerHTML = shown.map(x => {
    const gated = x.n >= floor && x.net_avg < 0;
    return `<tr><td>${x.sess ? `<span class="badge sess ${sessCls(x.sess)}">${sessLabel(x.sess)}</span>` : '<span class="dim">—</span>'}</td>
      <td>${esc(x.label)}</td><td>${x.n}</td><td>${x.win_pct.toFixed(0)}%</td>
      <td class="${x.net_avg >= 0 ? 'pos' : 'neg'}">${money(x.net_avg)}
        <div class="gatebar" style="margin-top:2px;width:52px"><i style="width:${(Math.abs(x.net_avg) / evMax * 100).toFixed(0)}%;background:var(--${x.net_avg >= 0 ? 'green' : 'red'})"></i></div></td>
      <td>${gated ? '<span class="neg" style="font-weight:800">SKIP</span>'
                  : x.n < floor ? `<span class="dim">${x.n}/${floor}</span>`
                  : '<span class="pos">open</span>'}</td></tr>`;
  }).join('');
  $('evEmpty').hidden = shown.length > 0;
  $('evNote').textContent = cfg.ev_gate === false
    ? 'EV gate disabled in config'
    : `buckets skip only at ≥${floor} samples with negative net avg — click a session tile to filter`;
}

async function pollBot() {
  const d = await fj('/api/bot/status', null);
  if (!d) { $('runBadge').textContent = 'API ERR'; $('runBadge').className = 'badge offline'; return; }
  const s = d.state || {}, cfg = d.config || {};
  $('modeBadge').textContent = (s.mode || 'paper').toUpperCase();
  $('modeBadge').className = 'badge ' + (s.mode === 'live' ? 'live' : 'paper');
  const fresh = s.heartbeat && (Date.now() / 1000 - s.heartbeat) < 30;
  const anyHalted = s.pools && Object.values(s.pools).some(p => p.halted);
  const run = !fresh ? ['BOT OFFLINE', 'offline'] : anyHalted ? ['HALTED', 'halted']
            : s.paused ? ['PAUSED', 'paused'] : ['RUNNING', 'running'];
  $('runBadge').textContent = run[0];
  $('runBadge').className = 'badge ' + run[1];
  $('logo').classList.toggle('off', !fresh);

  renderPoolTiles(s);

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

  lastBotData = d; lastBotCfg = cfg;
  renderEvGate(d, cfg);
  renderFindings(d.findings);

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
  const VCOL = {clean_win: 'green', good_exit: 'green', good_stop: 'blue',
                lucky_exit: 'yellow', left_money: 'orange', whipsaw_stop: 'red',
                ungraded: 'mute'};
  $('gradeMix').innerHTML = Object.keys(VCOL)
    .filter(v => (gs.verdicts || {})[v])
    .map(v => `<div title="${VLABEL[v]} × ${gs.verdicts[v]}"
      style="flex:${gs.verdicts[v]};background:var(--${VCOL[v]});opacity:.85"></div>`)
    .join('');
  $('gradeEdge').innerHTML = gs.n ? (
    `exits vs holding to expiry: <b class="${gs.exit_edge_usd >= 0 ? 'pos' : 'neg'}">`
    + `${money(gs.exit_edge_usd)}</b> across ${gs.n} settled`
    + ` · stops alone <b class="${gs.stops_saved_usd >= 0 ? 'pos' : 'neg'}">`
    + `${money(gs.stops_saved_usd)}</b>`
    + (gs.gaps ? ` · <span class="dim">${gs.gaps} with feed gaps</span>` : '')) : '';

  // dual gate: 100 weekday + 100 weekend settled before live test
  const g8 = d.gate || {weekday: 0, weekend: 0};
  $('gateProg').textContent = `${a.n}/200`;
  $('gateWd').textContent = `weekday ${g8.weekday}/100`;
  $('gateWe').textContent = `weekend ${g8.weekend}/100`;
  $('gateWdBar').style.width = Math.min(100, g8.weekday) + '%';
  $('gateWeBar').style.width = Math.min(100, g8.weekend) + '%';

  // daily P&L columns
  (() => {
    const days = Object.entries((d.stats && d.stats.by_day) || {});
    const svg = $('daySvg'); if (!days.length) { svg.innerHTML = ''; return; }
    const DW = svg.clientWidth || 300, DH = 240, top = 16, bot = 18;
    const mx = Math.max(...days.map(([, v]) => Math.abs(v)), 0.01);
    const zero = top + (DH - top - bot) / 2, half = (DH - top - bot) / 2;
    const bw = Math.min(42, DW / days.length - 8);
    let h = `<line x1="0" x2="${DW}" y1="${zero}" y2="${zero}" stroke="var(--border)"/>`;
    days.forEach(([day, v], i) => {
      const x = (i + 0.5) * DW / days.length, y = zero - v / mx * half;
      h += `<rect x="${(x - bw / 2).toFixed(1)}" y="${Math.min(y, zero).toFixed(1)}" width="${bw.toFixed(1)}"
             height="${Math.max(1, Math.abs(y - zero)).toFixed(1)}" rx="2" fill="var(--${v >= 0 ? 'green' : 'red'})"/>
            <text x="${x}" y="${(v >= 0 ? y - 4 : y + 11)}" text-anchor="middle" font-size="9"
             fill="var(--fg)" font-weight="700">${money(v)}</text>
            <text x="${x}" y="${DH - 4}" text-anchor="middle" font-size="9" fill="var(--mute)">${day.slice(5)}</text>`;
    });
    svg.setAttribute('viewBox', `0 0 ${DW} ${DH}`); svg.innerHTML = h;
  })();

  renderEquity((d.trades || []).filter(t => t.status === 'closed'), gmap);

  renderPoolByDate(d);

  // session map: curfew zones (00-13Z) marked; others colored by measured EV
  $('sessTable').tBodies[0].innerHTML =
    Object.entries((d.stats && d.stats.by_session) || {}).map(([k, v]) => {
      const curfewed = (cfg.overnight_curfew !== false) && /asia|europe/.test(k);
      const zone = curfewed ? '<span class="neg" style="font-weight:800">CURFEW</span>'
        : v.n < 12 ? `<span class="dim">${v.n}/12</span>`
        : v.net_avg >= 0 ? '<span class="pos">play</span>'
        : '<span class="neg">bleeds</span>';
      return `<tr><td>${esc(k)}</td><td>${v.n}</td><td>${v.win_pct.toFixed(0)}%</td>
        <td class="${v.net_avg >= 0 ? 'pos' : 'neg'}">${money(v.net_avg)}</td><td>${zone}</td></tr>`;
    }).join('');

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

async function pollThesis() {
  const t = await fj('/api/crypto/daily_thesis', null);
  dayKey = t && t.level ? parseFloat(t.level) : null;
}

pollThesis().then(pollCandles); pollCalibration(); pollBot(); pollSignal(); pollLoopLog();
setInterval(pollBot, 3000);
setInterval(pollSignal, 3000);
setInterval(pollCalibration, 30000);
setInterval(pollCandles, 30000);
setInterval(pollThesis, 300000);
</script></body></html>"""
