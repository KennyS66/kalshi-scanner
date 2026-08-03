"""Kill-switch component shared by /bot and /trade.

Three strings injected into both pages at marker comments (see `inject`).
The backend it drives already exists: POST /api/bot/control {"cmd":"pause"}
blocks new entries while open plays keep managing to their normal exits.
This module adds the parts that were missing -- a control you can find in
an emergency, on every screen, that tells you what is still open.

Design spec: docs/superpowers/specs/2026-08-02-kill-switch-design.md
"""

CSS_MARKER = "/*STOP_CSS*/"
HTML_MARKER = "<!--STOP_BAR-->"        # inside <header>
BANNER_MARKER = "<!--STOP_BANNER-->"   # directly after </header>
JS_MARKER = "//STOP_JS"

STOP_CSS = r"""
/* ── kill-switch ── */
/* No margin-left:auto here -- the header's .clock already carries one, and
   a second would split the free space instead of pinning STOP to the edge.
   Sitting after the clock puts it at the far right on both pages. */
.stop-wrap { display:flex; align-items:center; gap:9px; padding-left:4px; }
#stopBtn {
  font-family:var(--mono); font-size:11px; font-weight:800; letter-spacing:.6px;
  padding:6px 14px; border-radius:7px; cursor:pointer;
  background:var(--red-bg); color:var(--red); border:1px solid var(--red-bd);
  transition:background .15s, color .15s, border-color .15s;
}
#stopBtn:hover:not(:disabled) { background:var(--red); color:#1a0508; border-color:var(--red); }
#stopBtn.resume { background:var(--yellow-bg); color:var(--yellow); border-color:var(--yellow-bd); }
#stopBtn.resume:hover:not(:disabled) { background:var(--yellow); color:#1a1405; border-color:var(--yellow); }
#stopBtn.pending { background:var(--bg3); color:var(--mute); border-color:var(--border); cursor:progress; }
#stopBtn:disabled { background:var(--bg3); color:var(--mute); border-color:var(--border);
                    cursor:not-allowed; opacity:.65; }
#stopState { font-size:10px; font-weight:700; letter-spacing:.5px; color:var(--mute); }
#stopState.stopped { color:var(--yellow); }
#stopState.offline { color:var(--red); }

/* Ambient signal: the whole header carries a hairline while halted, so a
   stopped bot is legible from across the room, not just at the button. */
header.is-stopped { border-bottom-color:var(--yellow-bd); box-shadow:inset 0 -2px 0 var(--yellow-bg); }
header.is-offline { border-bottom-color:var(--red-bd);    box-shadow:inset 0 -2px 0 var(--red-bg); }

/* Deliberately not sticky: a sticky banner needs a per-page header-height
   offset, and the two pages differ. The always-visible header label
   carries the same fact ("STOPPED · 2 open"), so the banner is the
   detailed callout rather than the load-bearing signal. */
#stopBanner {
  padding:9px 20px; font-size:12px; font-weight:700;
  background:var(--yellow-bg); border-bottom:1px solid var(--yellow-bd); color:var(--yellow);
  display:flex; align-items:center; gap:10px;
}
#stopBanner[hidden] { display:none; }
#stopBanner .tick { font-family:var(--mono); font-weight:600; color:var(--fg); opacity:.85; }
"""

STOP_HTML = """<span class="stop-wrap">
  <span id="stopState">…</span>
  <button id="stopBtn" onclick="stopToggle()">■ STOP</button>
</span>"""

BANNER_HTML = """<div id="stopBanner" hidden>
  <span>⚠ STOPPED</span><span class="tick" id="stopBannerDetail"></span>
</div>"""

STOP_JS = r"""
/* ── kill-switch ───────────────────────────────────────────────────────
   Self-polls /api/bot/status so the component is identical on every page
   it is embedded in, with no coupling to that page's own poll loop. One
   extra localhost request per 5s is a fair price for that.            */
let _stopPending = null;   // 'pause' | 'resume' while awaiting confirmation

/* Pure: bot status -> how the control should look. Kept separate from the
   DOM so it stays the one piece of this component that is easy to reason
   about (and to test, if a JS harness ever lands). */
function stopViewState(s, nowMs) {
  const fresh = s && s.heartbeat && (nowMs / 1000 - s.heartbeat) < 30;
  const open = Object.keys((s && s.open_plays) || {}).length;
  if (!fresh) return {label: '■ STOP', cls: '', disabled: true,
                      state: 'BOT OFFLINE', stateCls: 'offline', open, banner: false};
  if (s.paused) {
    let why = s.paused_by === 'deadman' ? 'STOPPED — deadman' : 'STOPPED';
    if (open) why += ` · ${open} open`;   // the header carries this too, so
    return {label: '▶ RESUME', cls: 'resume', disabled: false,   // it survives scroll
            state: why, stateCls: 'stopped', open, banner: open > 0};
  }
  return {label: '■ STOP', cls: '', disabled: false,
          state: 'RUNNING', stateCls: '', open, banner: false};
}

function renderStop(s) {
  const v = stopViewState(s, Date.now());
  const btn = document.getElementById('stopBtn');
  if (!btn) return;
  // A click in flight owns the button until a poll confirms the new state.
  if (_stopPending) {
    const settled = (_stopPending === 'pause') === !!(s && s.paused);
    if (settled) _stopPending = null;
    else {
      btn.textContent = _stopPending === 'pause' ? 'stopping…' : 'resuming…';
      btn.className = 'pending'; btn.disabled = true;
      return;
    }
  }
  btn.textContent = v.label; btn.className = v.cls; btn.disabled = v.disabled;
  const st = document.getElementById('stopState');
  st.textContent = v.state; st.className = v.stateCls;
  const hdr = document.querySelector('header');
  if (hdr) {
    hdr.classList.toggle('is-stopped', v.stateCls === 'stopped');
    hdr.classList.toggle('is-offline', v.stateCls === 'offline');
  }
  const ban = document.getElementById('stopBanner');
  if (ban) {
    ban.hidden = !v.banner;
    if (v.banner) document.getElementById('stopBannerDetail').textContent =
      `${v.open} position${v.open === 1 ? '' : 's'} still open, managing to exit`;
  }
}

function _openPlayLines(s) {
  return Object.entries((s && s.open_plays) || {}).map(([t, p]) => {
    const cost = (p.entry && p.entry.price != null) ? p.entry.price * p.qty : 0;
    return `  ${t}  ${p.side} x${p.qty}   $${cost.toFixed(2)} at risk`;
  });
}

function stopConfirmText(s, cfg) {
  const lines = _openPlayLines(s);
  if (!lines.length) return 'Stop the bot? No positions are open.\n\n'
    + 'New entries stop immediately.';
  return `Stop the bot?\n\n${lines.length} position`
    + `${lines.length === 1 ? '' : 's'} will stay OPEN:\n${lines.join('\n')}\n\n`
    + 'These keep managing to their normal exits (target / stop / time).\n'
    + 'STOP only blocks NEW entries.\n\n'
    + 'To close them instead, cancel and use Flatten on /bot.';
}

function resumeConfirmText(s, cfg) {
  const live = (cfg && cfg.live_sessions_requested) || [];
  const parts = [];
  if (s && s.mode === 'live' && live.length)
    parts.push(`${live.join(', ')} ${live.length === 1 ? 'is' : 'are'} LIVE`
               + ' — it will trade real money.');
  // Observed 2026-08-02: a resume with a stale marketloop heartbeat is
  // undone by the deadman on the very same tick. Say so, rather than
  // letting the button look broken.
  if (s && s.paused_by === 'deadman')
    parts.push('The marketloop heartbeat is stale, so the deadman will'
               + ' re-pause it immediately. Start /marketloop first.');
  return parts.length ? `Resume the bot?\n\n${parts.join('\n\n')}` : null;
}

async function stopToggle() {
  const d = window._stopLast;
  if (!d) return;
  const s = d.state || {}, cfg = d.config || {};
  const resuming = !!s.paused;
  if (resuming) {
    const warn = resumeConfirmText(s, cfg);
    if (warn && !confirm(warn)) return;
  } else if (!confirm(stopConfirmText(s, cfg))) return;

  _stopPending = resuming ? 'resume' : 'pause';
  renderStop(s);
  try {
    await fetch('/api/bot/control', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({cmd: resuming ? 'resume' : 'pause'})});
  } catch (e) {
    _stopPending = null;
    alert('Could not reach the bot API. Run ./stop.sh from a terminal instead.');
  }
  pollStop();
}

async function pollStop() {
  let d = null;
  try {
    const r = await fetch('/api/bot/status');
    if (r.ok) d = await r.json();
  } catch (e) { /* fall through: renders the offline state */ }
  window._stopLast = d;
  renderStop(d && d.state);
}
pollStop();
setInterval(pollStop, 5000);
"""


def inject(html: str) -> str:
    """Replace the four markers, asserting each was actually present.

    A silently-missing kill-switch is the failure mode worth engineering
    against: better to refuse to serve the page at import time than to
    show a dashboard whose STOP button quietly isn't there.
    """
    for marker in (CSS_MARKER, HTML_MARKER, BANNER_MARKER, JS_MARKER):
        if marker not in html:
            raise RuntimeError(
                f"kill-switch marker {marker!r} missing from page -- refusing "
                f"to serve a dashboard without a STOP control")
    return (html.replace(CSS_MARKER, STOP_CSS)
                .replace(HTML_MARKER, STOP_HTML)
                .replace(BANNER_MARKER, BANNER_HTML)
                .replace(JS_MARKER, STOP_JS))
