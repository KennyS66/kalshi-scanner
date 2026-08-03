"""Analytics charts for the /bot screen: shared SVG helpers, a filter bar
that drives every chart at once, and the session strip.

Injected into bot_page.py at marker comments, the same pattern as
stop_control.py -- keeping bot_page.py from growing another 600 lines.

Data comes from GET /api/bot/series (all 680 closed trades as four
columns) rather than /api/bot/status, whose trades[-50:] cap is why the
equity chart could only ever show 50 of them.

Design spec: docs/superpowers/specs/2026-08-02-dashboard-charts-design.md
Palette: computed at OKLCH L 0.665 and validated with the dataviz
validator -- worst adjacent CVD separation deltaE 19.8, every check PASS.
Session hues carry identity; profit/loss green+red are reserved status
colors and never used for a series.
"""

CSS_MARKER = "/*CHART_CSS*/"
HTML_MARKER = "<!--CHART_PANELS-->"
STRIP_MARKER = "<!--SESSION_STRIP-->"
JS_MARKER = "//CHART_JS"

CHART_CSS = r"""
/* ── analytics charts ── */
:root {
  /* Categorical: session identity. Fixed order, never cycled -- a session
     keeps its hue no matter which filter is active. */
  --s-wd-day:#00a5c0; --s-wd-night:#c58500;
  --s-we-day:#9578ff; --s-we-night:#36b100;
  --grid-line:rgba(255,255,255,.055);
}
.ctrlbar { display:flex; gap:18px; align-items:center; flex-wrap:wrap;
           margin:0 20px 12px; padding:9px 14px; background:var(--bg2);
           border:1px solid var(--border); border-radius:var(--radius); }
.ctrlgrp { display:flex; gap:7px; align-items:center; }
.ctrlgrp > .lbl { font-size:9px; font-weight:800; letter-spacing:1px;
                  text-transform:uppercase; color:var(--mute); margin-right:2px; }
.chip { font-family:var(--mono); font-size:10px; font-weight:700; letter-spacing:.4px;
        padding:4px 10px; border-radius:99px; cursor:pointer; color:var(--mute);
        background:transparent; border:1px solid var(--border); transition:all .15s; }
.chip:hover { color:var(--fg); border-color:var(--mute); }
.chip.on { color:var(--fg); background:var(--bg3); border-color:var(--blue-bd); }
.chip .dot { display:inline-block; width:7px; height:7px; border-radius:50%;
             margin-right:6px; vertical-align:middle; }
.ctrlcount { margin-left:auto; font-size:11px; color:var(--mute);
             font-variant-numeric:tabular-nums; }

.cwrap { position:relative; }
.cwrap svg { display:block; width:100%; overflow:visible; }
.ctip { position:absolute; pointer-events:none; z-index:6; padding:6px 9px;
        background:var(--bg3); border:1px solid var(--border); border-radius:7px;
        font-size:11px; line-height:1.5; white-space:nowrap;
        box-shadow:0 6px 20px rgba(0,0,0,.45); font-variant-numeric:tabular-nums; }
.ctip[hidden] { display:none; }
.ctip .k { color:var(--mute); }
.clegend { display:flex; gap:14px; flex-wrap:wrap; margin-top:9px;
           font-size:10px; color:var(--mute); }
.clegend i { display:inline-block; width:9px; height:9px; border-radius:2px;
             margin-right:5px; vertical-align:middle; font-style:normal; }
.cnote { font-size:10px; color:var(--mute); font-family:var(--sans); margin-top:7px; }

/* ── session strip (signature) ── */
.sstrip { display:grid; grid-template-columns:repeat(4,1fr); gap:1px;
          margin:14px 20px 16px; background:var(--border);
          border:1px solid var(--border); border-radius:var(--radius);
          overflow:hidden; }
.sseg { background:var(--bg2); padding:10px 13px 8px; cursor:pointer;
        position:relative; transition:background .18s; min-width:0; }
.sseg:hover { background:var(--bg3); }
.sseg.on { background:var(--bg3); }
.sseg::before { content:""; position:absolute; inset:0 0 auto 0; height:2px;
                background:var(--seg); opacity:.5; }
.sseg.on::before { opacity:1; box-shadow:0 0 12px var(--seg); }
.sseg.live-now { background:linear-gradient(180deg, var(--bg3), var(--bg2)); }
.sseg-hd { display:flex; align-items:baseline; gap:7px; margin-bottom:2px; }
.sseg-name { font-size:10px; font-weight:800; letter-spacing:.7px;
             text-transform:uppercase; color:var(--seg); }
.sseg-now { font-size:8px; font-weight:800; letter-spacing:.6px; color:var(--bg);
            background:var(--seg); padding:1px 5px; border-radius:99px; }
.sseg-avg { margin-left:auto; font-size:12px; font-weight:800;
            font-variant-numeric:tabular-nums; }
.sseg-sub { font-size:9px; color:var(--mute); font-variant-numeric:tabular-nums; }
.sseg svg { display:block; width:100%; height:26px; margin-top:3px; }
@media (max-width:720px) { .sstrip { grid-template-columns:repeat(2,1fr); } }
"""

SESSION_STRIP_HTML = """<div class="sstrip" id="sessionStrip"></div>"""

CHART_PANELS_HTML = """
  <div class="section-label">Analytics
    <span class="dim" style="text-transform:none">(every settled trade — filters below drive all charts)</span></div>

  <div class="ctrlbar" id="chartCtrls">
    <span class="ctrlgrp"><span class="lbl">Range</span>
      <button class="chip" data-range="7">7d</button>
      <button class="chip" data-range="30">30d</button>
      <button class="chip on" data-range="all">all</button>
    </span>
    <span class="ctrlgrp" id="sessChips"><span class="lbl">Session</span></span>
    <span class="ctrlcount" id="chartCount">—</span>
  </div>

  <div class="grid">
    <div class="panel wide">
      <h3>Drawdown <span class="dim" style="text-transform:none">(equity below its running peak — depth and duration)</span></h3>
      <div class="cwrap" id="ddWrap"><svg id="ddSvg" height="190"></svg><div class="ctip" id="ddTip" hidden></div></div>
      <div class="cnote" id="ddNote"></div>
    </div>

    <div class="panel wide">
      <h3>Rolling edge <span class="dim" style="text-transform:none">(30-trade window — is the edge holding or decaying?)</span></h3>
      <div class="cwrap" id="rollWrap"><svg id="rollSvg" height="230"></svg><div class="ctip" id="rollTip" hidden></div></div>
      <div class="cnote">Two panels, one x-axis — win rate and net avg are different units and never share a y-scale.</div>
    </div>

    <div class="panel wide">
      <h3>Equity by session <span class="dim" style="text-transform:none">(shared y-scale — the four are directly comparable)</span></h3>
      <div class="cwrap" id="smWrap"><svg id="smSvg" height="200"></svg><div class="ctip" id="smTip" hidden></div></div>
    </div>

    <div class="panel">
      <h3>P&amp;L distribution <span class="dim" style="text-transform:none">(per trade)</span></h3>
      <div class="cwrap" id="histWrap"><svg id="histSvg" height="200"></svg><div class="ctip" id="histTip" hidden></div></div>
      <div class="cnote" id="histNote"></div>
    </div>

    <div class="panel">
      <h3>Exit reason <span class="dim" style="text-transform:none">(net avg per trade)</span></h3>
      <div class="cwrap" id="exWrap"><svg id="exSvg" height="200"></svg><div class="ctip" id="exTip" hidden></div></div>
    </div>
  </div>
"""

CHART_JS = r"""
/* ── analytics charts ─────────────────────────────────────────────────
   One filter state drives every chart; charts never own their own
   controls. Data is /api/bot/series (all closed trades, 4 columns) --
   polled slowly because it only changes when a trade closes, unlike the
   5s status poll it deliberately does not ride on.                    */
const CK_SESSIONS = ['weekday_day', 'weekday_night', 'weekend_day', 'weekend_night'];
const CK_HUE = {weekday_day: 'var(--s-wd-day)', weekday_night: 'var(--s-wd-night)',
                  weekend_day: 'var(--s-we-day)', weekend_night: 'var(--s-we-night)'};
/* Labels come from the page's own sessLabel ("WD·day") so the dashboard
   speaks one session vocabulary rather than two. Falls back to the raw
   name if that ever goes away -- degraded, not broken. */
const ckLabel = s => (typeof sessLabel === 'function' ? sessLabel(s) : s);
let CK_ALL = [];                                   // every closed trade
let CK_F = {range: 'all', session: 'all'};         // the one filter state

const ckMoney = v => (v >= 0 ? '+' : '−') + '$' + Math.abs(v).toFixed(2);
const ckPct = v => v.toFixed(0) + '%';
const ckDate = ts => new Date(ts * 1000).toISOString().slice(5, 10);
const ckEsc = s => String(s).replace(/[&<>"]/g,
  c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

/* Linear scale. Degenerate domains centre rather than divide by zero. */
function ckScale(d0, d1, r0, r1) {
  if (d1 === d0) return () => (r0 + r1) / 2;
  return v => r0 + (v - d0) * (r1 - r0) / (d1 - d0);
}
/* ~4 round gridline values spanning [lo,hi] and always including 0. */
function ckTicks(lo, hi, n) {
  n = n || 4;
  if (hi === lo) { hi = lo + 1; }
  const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || mag * 10;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step)
    out.push(Math.abs(v) < 1e-9 ? 0 : v);
  return out;
}
/* Recessive gridlines + right-hand value labels, shared by every panel. */
function ckGrid(ticks, y, x0, x1, fmt) {
  return ticks.map(t => `<line x1="${x0}" x2="${x1}" y1="${y(t).toFixed(1)}"
      y2="${y(t).toFixed(1)}" stroke="${Math.abs(t) < 1e-9 ? 'var(--border)' : 'var(--grid-line)'}"
      stroke-width="1"/><text x="${x1 + 5}" y="${(y(t) + 3.5).toFixed(1)}"
      fill="var(--mute)" font-size="9">${fmt(t)}</text>`).join('');
}
function ckHover(wrap, svg, tip, n, xAt, html) {
  if (!n) { svg.onmousemove = null; return; }
  svg.onmousemove = ev => {
    const r = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(n - 1, xAt((ev.clientX - r.left) / r.width * svg.viewBox.baseVal.width)));
    const h = html(i);
    if (!h) { tip.hidden = true; return; }
    tip.innerHTML = h; tip.hidden = false;
    const w = wrap.getBoundingClientRect();
    tip.style.left = Math.min(w.width - tip.offsetWidth - 4,
                              Math.max(0, ev.clientX - w.left + 12)) + 'px';
    tip.style.top = Math.max(0, ev.clientY - w.top - tip.offsetHeight - 10) + 'px';
  };
  svg.onmouseleave = () => { tip.hidden = true; };
}

function ckFiltered() {
  let rows = CK_ALL;
  if (CK_F.session !== 'all') rows = rows.filter(r => r.s === CK_F.session);
  if (CK_F.range !== 'all') {
    const cut = Date.now() / 1000 - Number(CK_F.range) * 86400;
    rows = rows.filter(r => r.t >= cut);
  }
  return rows;
}

/* ── 1. drawdown ── */
function ckDrawdown(rows) {
  const svg = document.getElementById('ddSvg'), wrap = document.getElementById('ddWrap');
  const W = 1000, H = 190, padL = 8, padR = 46, padT = 12, padB = 20;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('preserveAspectRatio', 'none');
  if (!rows.length) { svg.innerHTML = ''; document.getElementById('ddNote').textContent = ''; return; }
  let cum = 0, peak = 0; const dd = rows.map(r => { cum += r.p; peak = Math.max(peak, cum); return cum - peak; });
  const worst = Math.min(...dd, 0);
  const x = ckScale(0, Math.max(1, rows.length - 1), padL, W - padR);
  const y = ckScale(worst, 0, H - padB, padT);
  const pts = dd.map((d, i) => `${x(i).toFixed(1)},${y(d).toFixed(1)}`).join(' ');
  const area = `${padL},${y(0).toFixed(1)} ${pts} ${x(dd.length - 1).toFixed(1)},${y(0).toFixed(1)}`;
  const wi = dd.indexOf(worst);
  svg.innerHTML = `<defs><linearGradient id="ddG" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="var(--red)" stop-opacity=".30"/>
      <stop offset="100%" stop-color="var(--red)" stop-opacity=".02"/></linearGradient></defs>
    ${ckGrid(ckTicks(worst, 0), y, padL, W - padR, v => '$' + v.toFixed(0))}
    <polygon points="${area}" fill="url(#ddG)"/>
    <polyline points="${pts}" fill="none" stroke="var(--red)" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${x(wi).toFixed(1)}" cy="${y(worst).toFixed(1)}" r="3.5"
      fill="var(--red)" stroke="var(--bg2)" stroke-width="2"/>
    <line id="ddHair" y1="${padT}" y2="${H - padB}" stroke="var(--mute)"
      stroke-width="1" stroke-dasharray="3 3" opacity="0"/>`;
  document.getElementById('ddNote').textContent =
    `deepest ${ckMoney(worst)} on ${ckDate(rows[wi].t)} · currently ${ckMoney(dd[dd.length - 1])} below peak`;
  const hair = document.getElementById('ddHair');
  ckHover(wrap, svg, document.getElementById('ddTip'), rows.length,
    px => Math.round((px - padL) / ((W - padR - padL) / Math.max(1, rows.length - 1))),
    i => { hair.setAttribute('x1', x(i)); hair.setAttribute('x2', x(i)); hair.setAttribute('opacity', '.7');
           return `<span class="k">${ckDate(rows[i].t)}</span> ${ckLabel(rows[i].s)}<br>`
                + `<span class="k">under peak</span> ${ckMoney(dd[i])}`; });
  svg.addEventListener('mouseleave', () => hair.setAttribute('opacity', '0'));
}

/* ── 2. rolling edge: win rate + net avg, two panels, one x-axis ── */
function ckRolling(rows) {
  const svg = document.getElementById('rollSvg'), wrap = document.getElementById('rollWrap');
  const W = 1000, H = 230, padL = 8, padR = 46, gap = 16;
  const win = 30, top = 10, panelH = (H - top - 22 - gap) / 2;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('preserveAspectRatio', 'none');
  if (rows.length < win) {
    svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" fill="var(--mute)" font-size="12"
      text-anchor="middle">needs ${win} trades in range — ${rows.length} here</text>`;
    return;
  }
  const wr = [], na = [];
  for (let i = win - 1; i < rows.length; i++) {
    const w = rows.slice(i - win + 1, i + 1);
    wr.push(w.filter(r => r.p > 0).length / win * 100);
    na.push(w.reduce((a, r) => a + r.p, 0) / win);
  }
  const x = ckScale(0, Math.max(1, wr.length - 1), padL, W - padR);
  const y1 = ckScale(0, 100, top + panelH, top);
  const naLo = Math.min(0, ...na), naHi = Math.max(0, ...na);
  const y2t = top + panelH + gap, y2 = ckScale(naLo, naHi, y2t + panelH, y2t);
  const line = (arr, sc, col) => `<polyline points="${arr.map((v, i) =>
    `${x(i).toFixed(1)},${sc(v).toFixed(1)}`).join(' ')}" fill="none" stroke="${col}"
    stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  svg.innerHTML = `
    ${ckGrid([0, 50, 100], y1, padL, W - padR, v => v + '%')}
    <line x1="${padL}" x2="${W - padR}" y1="${y1(50).toFixed(1)}" y2="${y1(50).toFixed(1)}"
      stroke="var(--mute)" stroke-width="1" stroke-dasharray="4 4" opacity=".55"/>
    ${line(wr, y1, 'var(--blue)')}
    <text x="${padL}" y="${top + 9}" fill="var(--mute)" font-size="9"
      letter-spacing=".8">WIN RATE · 30-TRADE</text>
    ${ckGrid(ckTicks(naLo, naHi, 3), y2, padL, W - padR, v => '$' + v.toFixed(2))}
    ${line(na, y2, 'var(--purple)')}
    <text x="${padL}" y="${y2t + 9}" fill="var(--mute)" font-size="9"
      letter-spacing=".8">NET AVG · 30-TRADE</text>
    <line id="rollHair" y1="${top}" y2="${y2t + panelH}" stroke="var(--mute)"
      stroke-width="1" stroke-dasharray="3 3" opacity="0"/>`;
  const hair = document.getElementById('rollHair');
  ckHover(wrap, svg, document.getElementById('rollTip'), wr.length,
    px => Math.round((px - padL) / ((W - padR - padL) / Math.max(1, wr.length - 1))),
    i => { hair.setAttribute('x1', x(i)); hair.setAttribute('x2', x(i)); hair.setAttribute('opacity', '.7');
           return `<span class="k">${ckDate(rows[i + win - 1].t)}</span><br>`
                + `<span class="k">win</span> ${ckPct(wr[i])} · <span class="k">net avg</span> ${ckMoney(na[i])}`; });
  svg.addEventListener('mouseleave', () => hair.setAttribute('opacity', '0'));
}

/* ── 3. equity by session, small multiples on a shared y-scale ── */
function ckSmallMultiples(rows) {
  const svg = document.getElementById('smSvg'), wrap = document.getElementById('smWrap');
  const W = 1000, H = 200, gap = 12, cellW = (W - gap * 3) / 4, padT = 18, padB = 16;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('preserveAspectRatio', 'none');
  const per = CK_SESSIONS.map(s => {
    let c = 0;
    return {s, pts: CK_ALL.filter(r => r.s === s && rows.some(f => f.t === r.t))
      .map(r => (c += r.p))};
  });
  const flat = per.flatMap(p => p.pts);
  if (!flat.length) { svg.innerHTML = ''; return; }
  const lo = Math.min(0, ...flat), hi = Math.max(0, ...flat);
  svg.innerHTML = per.map((p, k) => {
    const x0 = k * (cellW + gap), y = ckScale(lo, hi, H - padB, padT);
    const x = ckScale(0, Math.max(1, p.pts.length - 1), x0 + 2, x0 + cellW - 2);
    const hue = CK_HUE[p.s];
    const last = p.pts.length ? p.pts[p.pts.length - 1] : 0;
    const poly = p.pts.length > 1 ? `<polyline points="${p.pts.map((v, i) =>
      `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')}" fill="none" stroke="${hue}"
      stroke-width="2" stroke-linejoin="round"/>` : '';
    return `<line x1="${x0}" x2="${x0 + cellW}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}"
        stroke="var(--border)" stroke-width="1"/>${poly}
      <text x="${x0}" y="11" fill="${hue}" font-size="9" font-weight="800"
        letter-spacing=".7">${ckLabel(p.s)}</text>
      <text x="${x0 + cellW}" y="11" text-anchor="end" font-size="10" font-weight="800"
        fill="${last >= 0 ? 'var(--green)' : 'var(--red)'}">${ckMoney(last)}</text>
      <text x="${x0}" y="${H - 3}" fill="var(--mute)" font-size="9">${p.pts.length} trades</text>`;
  }).join('');
}

/* ── 4. P&L distribution, diverging around zero ── */
function ckHistogram(rows) {
  const svg = document.getElementById('histSvg'), wrap = document.getElementById('histWrap');
  const W = 520, H = 200, padL = 8, padR = 34, padT = 12, padB = 26;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('preserveAspectRatio', 'none');
  if (!rows.length) { svg.innerHTML = ''; document.getElementById('histNote').textContent = ''; return; }
  const vals = rows.map(r => r.p);
  const lo = Math.min(...vals), hi = Math.max(...vals), nb = 21;
  const step = (hi - lo) / nb || 1;
  const bins = new Array(nb).fill(0);
  vals.forEach(v => { bins[Math.min(nb - 1, Math.floor((v - lo) / step))]++; });
  const maxN = Math.max(...bins);
  const x = ckScale(0, nb, padL, W - padR), y = ckScale(0, maxN, H - padB, padT);
  const bw = Math.max(2, (W - padR - padL) / nb - 2);       // 2px surface gap
  const zeroX = x((0 - lo) / step);
  svg.innerHTML = ckGrid(ckTicks(0, maxN, 3), y, padL, W - padR, v => v.toFixed(0))
    + bins.map((n, i) => {
      const mid = lo + (i + 0.5) * step, h = (H - padB) - y(n);
      if (!n) return '';
      return `<rect x="${x(i).toFixed(1)}" y="${y(n).toFixed(1)}" width="${bw.toFixed(1)}"
        height="${h.toFixed(1)}" rx="3" fill="${mid >= 0 ? 'var(--green)' : 'var(--red)'}"
        opacity=".82"/>`;
    }).join('')
    + `<line x1="${zeroX.toFixed(1)}" x2="${zeroX.toFixed(1)}" y1="${padT}" y2="${H - padB}"
        stroke="var(--mute)" stroke-width="1" stroke-dasharray="3 3" opacity=".8"/>
       <text x="${zeroX.toFixed(1)}" y="${H - 12}" fill="var(--mute)" font-size="9"
        text-anchor="middle">$0</text>
       <text x="${padL}" y="${H - 12}" fill="var(--mute)" font-size="9">${ckMoney(lo)}</text>
       <text x="${W - padR}" y="${H - 12}" fill="var(--mute)" font-size="9"
        text-anchor="end">${ckMoney(hi)}</text>`;
  const wins = vals.filter(v => v > 0).length;
  const sorted = [...vals].sort((a, b) => a - b);
  const med = sorted[Math.floor(sorted.length / 2)];
  document.getElementById('histNote').textContent =
    `${wins}/${vals.length} winners · median ${ckMoney(med)} · worst ${ckMoney(lo)} · best ${ckMoney(hi)}`;
  ckHover(wrap, svg, document.getElementById('histTip'), nb,
    px => Math.floor((px - padL) / ((W - padR - padL) / nb)),
    i => bins[i] ? `<span class="k">${ckMoney(lo + i * step)} … ${ckMoney(lo + (i + 1) * step)}</span>`
                 + `<br>${bins[i]} trade${bins[i] === 1 ? '' : 's'}` : null);
}

/* ── 5. exit reason, diverging horizontal bars ── */
function ckExitReasons(rows) {
  const svg = document.getElementById('exSvg'), wrap = document.getElementById('exWrap');
  const W = 520, H = 200, padL = 74, padR = 46, padT = 8;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('preserveAspectRatio', 'none');
  const by = {};
  rows.forEach(r => { (by[r.r] = by[r.r] || []).push(r.p); });
  const items = Object.entries(by)
    .map(([k, v]) => ({k, n: v.length, avg: v.reduce((a, b) => a + b, 0) / v.length}))
    .sort((a, b) => a.avg - b.avg);
  if (!items.length) { svg.innerHTML = ''; return; }
  const lo = Math.min(0, ...items.map(i => i.avg)), hi = Math.max(0, ...items.map(i => i.avg));
  const x = ckScale(lo, hi, padL, W - padR);
  const rowH = Math.min(24, (H - padT - 6) / items.length), bh = Math.max(6, rowH - 7);
  svg.innerHTML = items.map((it, i) => {
    const yy = padT + i * rowH, x0 = x(0), x1 = x(it.avg);
    const pos = it.avg >= 0;
    return `<text x="${padL - 8}" y="${(yy + bh / 2 + 3.5).toFixed(1)}" text-anchor="end"
        fill="var(--mute)" font-size="10">${ckEsc(it.k)}</text>
      <rect x="${Math.min(x0, x1).toFixed(1)}" y="${yy.toFixed(1)}"
        width="${Math.max(1.5, Math.abs(x1 - x0)).toFixed(1)}" height="${bh.toFixed(1)}"
        rx="3" fill="${pos ? 'var(--green)' : 'var(--red)'}" opacity=".82"/>
      <text x="${(pos ? x1 + 5 : x1 - 5).toFixed(1)}" y="${(yy + bh / 2 + 3.5).toFixed(1)}"
        text-anchor="${pos ? 'start' : 'end'}" fill="var(--fg)" font-size="10"
        font-weight="700">${ckMoney(it.avg)}</text>`;
  }).join('') + `<line x1="${x(0).toFixed(1)}" x2="${x(0).toFixed(1)}" y1="${padT}"
      y2="${(padT + items.length * rowH).toFixed(1)}" stroke="var(--border)" stroke-width="1"/>`;
  ckHover(wrap, svg, document.getElementById('exTip'), items.length,
    () => -1, () => null);
  svg.onmousemove = ev => {
    const r = svg.getBoundingClientRect(), tip = document.getElementById('exTip');
    const i = Math.floor(((ev.clientY - r.top) / r.height * H - padT) / rowH);
    if (i < 0 || i >= items.length) { tip.hidden = true; return; }
    tip.innerHTML = `<span class="k">${ckEsc(items[i].k)}</span><br>${items[i].n} trades`
      + ` · net avg ${ckMoney(items[i].avg)}`;
    tip.hidden = false;
    const w = wrap.getBoundingClientRect();
    tip.style.left = Math.min(w.width - tip.offsetWidth - 4, ev.clientX - w.left + 12) + 'px';
    tip.style.top = Math.max(0, ev.clientY - w.top - tip.offsetHeight - 8) + 'px';
  };
}

/* ── session strip: the page's structural spine ── */
function ckSessionStrip() {
  const el = document.getElementById('sessionStrip');
  if (!el) return;
  const g = new Date(), nowSess =
    (g.getUTCDay() >= 5 ? 'weekend' : 'weekday') + '_' + (g.getUTCHours() >= 13 ? 'day' : 'night');
  el.innerHTML = CK_SESSIONS.map(s => {
    const rows = CK_ALL.filter(r => r.s === s);
    const n = rows.length, tot = rows.reduce((a, r) => a + r.p, 0);
    const avg = n ? tot / n : 0;
    let c = 0; const pts = rows.map(r => (c += r.p));
    const lo = Math.min(0, ...pts), hi = Math.max(0, ...pts);
    const y = ckScale(lo, hi, 24, 2), x = ckScale(0, Math.max(1, pts.length - 1), 0, 100);
    const spark = pts.length > 1 ? `<polyline points="${pts.map((v, i) =>
      `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')}" fill="none"
      stroke="var(--seg)" stroke-width="1.6" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>` : '';
    const on = CK_F.session === s;
    return `<div class="sseg${on ? ' on' : ''}${s === nowSess ? ' live-now' : ''}"
        style="--seg:${CK_HUE[s]}" onclick="ckPick('session','${s}')"
        title="click to filter every chart to ${ckLabel(s)}">
      <div class="sseg-hd"><span class="sseg-name">${ckLabel(s)}</span>
        ${s === nowSess ? '<span class="sseg-now">NOW</span>' : ''}
        <span class="sseg-avg ${avg >= 0 ? 'pos' : 'neg'}">${n ? ckMoney(avg) : '—'}</span></div>
      <div class="sseg-sub">${n} trades · total ${ckMoney(tot)}</div>
      <svg viewBox="0 0 100 26" preserveAspectRatio="none">
        <line x1="0" x2="100" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}"
          stroke="var(--border)" stroke-width="1" vector-effect="non-scaling-stroke"/>${spark}</svg>
    </div>`;
  }).join('');
}

/* Clicking the active session chip clears the filter -- a filter you can
   set but not unset is the classic dashboard trap. */
function ckPick(kind, val) {
  CK_F[kind] = (kind === 'session' && CK_F.session === val) ? 'all' : val;
  ckRenderAll();
}

function ckRenderAll() {
  document.querySelectorAll('#chartCtrls .chip[data-range]').forEach(b =>
    b.classList.toggle('on', b.dataset.range === CK_F.range));
  document.querySelectorAll('#chartCtrls .chip[data-sess]').forEach(b =>
    b.classList.toggle('on', b.dataset.sess === CK_F.session));
  const rows = ckFiltered();
  document.getElementById('chartCount').textContent =
    `${rows.length} of ${CK_ALL.length} settled trades`;
  ckDrawdown(rows); ckRolling(rows); ckSmallMultiples(rows);
  ckHistogram(rows); ckExitReasons(rows); ckSessionStrip();
}

function ckInitControls() {
  const holder = document.getElementById('sessChips');
  if (holder && !holder.querySelector('.chip')) {
    holder.insertAdjacentHTML('beforeend',
      `<button class="chip on" data-sess="all">all</button>` + CK_SESSIONS.map(s =>
        `<button class="chip" data-sess="${s}"><i class="dot"
          style="background:${CK_HUE[s]}"></i>${ckLabel(s)}</button>`).join(''));
  }
  document.querySelectorAll('#chartCtrls .chip').forEach(b => {
    b.onclick = () => ckPick(b.dataset.range ? 'range' : 'session',
                            b.dataset.range || b.dataset.sess);
  });
}

async function pollSeries() {
  try {
    const r = await fetch('/api/bot/series');
    if (!r.ok) return;
    CK_ALL = (await r.json()).trades || [];
  } catch (e) { return; }
  ckRenderAll();
}
ckInitControls();
pollSeries();
setInterval(pollSeries, 30000);   // only changes when a trade closes
window.addEventListener('resize', () => { if (CK_ALL.length) ckRenderAll(); });
"""


def inject(html: str) -> str:
    """Replace the four markers, asserting each was present -- same
    fail-loud contract as stop_control.inject."""
    for marker in (CSS_MARKER, STRIP_MARKER, HTML_MARKER, JS_MARKER):
        if marker not in html:
            raise RuntimeError(f"chart marker {marker!r} missing from /bot page")
    return (html.replace(CSS_MARKER, CHART_CSS)
                .replace(STRIP_MARKER, SESSION_STRIP_HTML)
                .replace(HTML_MARKER, CHART_PANELS_HTML)
                .replace(JS_MARKER, CHART_JS))
