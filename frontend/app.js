/* ============================================================
   Bitwise · Ethereum Monitor — vanilla JS
   Fetches the local FastAPI and renders the dashboard.
   ============================================================ */

// API base. Defaults to same-origin (frontend served by FastAPI). To edit the
// frontend locally against a remote backend, pass ?api=http://host:8000 in the
// URL (or set window.API_BASE before this script loads).
const API = new URLSearchParams(location.search).get("api")
          || window.API_BASE
          || ""; // same-origin (mounted by FastAPI)
const TABS = ["home", "validators", "consolidations", "methodology", "history"];
const FAR_FUTURE = "18446744073709551615";
const EPOCH_SECONDS = 384; // 32 slots × 12s
const GWEI = 1_000_000_000;
const THEME_KEY = "bw_explorer_theme";

const state = {
  exitQueue: null,
  churn: null,
  pendingWithdrawals: null,
  lastUpdated: null,
  error: null,
  predictMode: "full",
  batch: null,
  batchFilter: "all",
  consolidations: null,
  consolidationsOpen: new Set(),
  pwSort: { key: "epoch", dir: "asc" },
  history: null,
  historyRange: "all",
};

/* ---------- Helpers ---------- */
const $  = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));

function fmtInt(n) {
  return (n ?? 0).toLocaleString();
}
function fmtEth(eth, digits = 2) {
  if (eth === null || eth === undefined) return "—";
  return Number(eth).toLocaleString(undefined, {
    minimumFractionDigits: digits === 0 ? 0 : 2,
    maximumFractionDigits: digits,
  });
}
// Canonical wait-time formatter — always uses real units (h+m or d+h),
// never decimal hours like "1.9 h".
function fmtHours(h) {
  if (h == null) return "—";
  if (h <= 0) return "0 m";
  const totalMin = Math.round(h * 60);
  if (totalMin < 1) return "< 1 m";
  const days = Math.floor(totalMin / 1440);
  const hh   = Math.floor((totalMin % 1440) / 60);
  const mm   = totalMin % 60;
  if (days >= 1) return hh ? `${days} d ${hh} h` : `${days} d`;
  if (hh >= 1)   return mm ? `${hh} h ${mm} m` : `${hh} h`;
  return `${mm} m`;
}

// Canonical "Xd Yh Zm" formatter for any seconds-delta. Used by every methodology headline.
function fmtDaysHours(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return "<1m";
  const totalMinutes = Math.round(seconds / 60);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  const parts = [];
  if (days) parts.push(`${days}d`);
  if (hours || (days && minutes)) parts.push(`${hours}h`);
  if (minutes) parts.push(`${minutes}m`);
  return parts.join(" ") || "0m";
}

function fmtAgo(d) {
  if (!d) return "—";
  const sec = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
  if (sec < 5)   return "just now";
  if (sec < 60)  return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  return `${Math.floor(sec / 3600)}h ago`;
}
function fmtTime(d) {
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function timeFromNow(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  const diff = d.getTime() - Date.now();
  if (diff <= 0) return "now";
  const hours = diff / 3_600_000;
  if (hours < 1)  return `${Math.round(hours * 60)}m`;
  if (hours < 24) return `${hours.toFixed(1)}h`;
  return `${Math.floor(hours / 24)}d ${Math.round(hours % 24)}h`;
}
function epochToDate(epoch, currentEpoch) {
  const epochsAway = epoch - currentEpoch;
  const d = new Date(Date.now() + epochsAway * EPOCH_SECONDS * 1000);
  return fmtDate(d.toISOString());
}
function statusChip(status) {
  if (!status) return { cls: "chip", label: "UNKNOWN" };
  if (status.includes("active"))     return { cls: "chip active",   label: "ACTIVE" };
  if (status.includes("exit"))       return { cls: "chip exit",     label: "EXITING" };
  if (status.includes("withdrawal")) return { cls: "chip withdraw", label: "WITHDRAWABLE" };
  if (status.includes("slashed"))    return { cls: "chip slashed",  label: "SLASHED" };
  return { cls: "chip", label: status.replace(/_/g, " ").toUpperCase() };
}
function credentialChip(t) {
  if (t === "compounding") return { cls: "chip compounding", label: "COMPOUNDING" };
  if (t === "bls")         return { cls: "chip bls",         label: "BLS" };
  return                          { cls: "chip execution",   label: "NON-COMPOUNDING" };
}
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function formatEpochsAway(epochsAway) {
  if (epochsAway <= 0) return "now";
  const hours = (epochsAway * EPOCH_SECONDS) / 3600;
  return fmtHours(hours);
}
async function api(path, opts) {
  const res = await fetch(API + path, opts);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body.detail) detail = body.detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

/* ---------- Theme ---------- */
function applyTheme(t) {
  document.body.classList.toggle("theme-dark",  t === "dark");
  document.body.classList.toggle("theme-light", t === "light");
}
function initTheme() {
  const saved = localStorage.getItem(THEME_KEY) || "light";
  applyTheme(saved);
}
function wireTheme() {
  $(".theme-toggle")?.addEventListener("click", () => {
    const next = document.body.classList.contains("theme-dark") ? "light" : "dark";
    applyTheme(next);
    localStorage.setItem(THEME_KEY, next);
  });
}

/* ---------- Hash-routed tabs ---------- */
function tabFromHash() {
  const h = (location.hash || "").replace(/^#/, "");
  return TABS.includes(h) ? h : TABS[0];
}
function applyTab() {
  const t = tabFromHash();
  $$("[data-tab-content]").forEach(el => { el.hidden = el.dataset.tabContent !== t; });
  $$("[data-tab-link]").forEach(a => { a.classList.toggle("active", a.dataset.tabLink === t); });
  window.scrollTo(0, 0);
  // Lazy load per-tab data
  if (t === "consolidations" && !state.consolidations) {
    loadConsolidations();
  }
  if (t === "methodology") {
    methodology.onTabActivate();
  }
  if (t === "history" && !state.history) {
    loadHistory();
  }
}
function wireTabs() {
  window.addEventListener("hashchange", applyTab);
  applyTab();
}

/* ---------- Boot overlay ---------- */
function hideBootOverlay() {
  const o = $("#boot-overlay");
  if (o) o.classList.add("gone");
}
function showBootError(msg) {
  const o = $("#boot-overlay");
  if (o) { o.textContent = `Error · ${msg}`; o.style.color = "var(--warn)"; }
}

/* ---------- Context bar ---------- */
function renderContext() {
  $("#cx-epoch")    .textContent = state.exitQueue ? fmtInt(state.exitQueue.current_epoch) : "—";
  $("#cx-churn")    .textContent = state.churn ? `${fmtEth(state.churn.churn_limit_eth, 0)} ETH/epoch` : "—";
  $("#cx-stake")    .textContent = state.churn ? `${(state.churn.total_active_balance_eth / 1_000_000).toFixed(2)}M ETH` : "—";
  $("#cx-refreshed").textContent = state.lastUpdated ? fmtTime(state.lastUpdated) : "—";
  $("#foot-refreshed").textContent = state.lastUpdated ? fmtTime(state.lastUpdated) : "—";
}

/* ---------- Home KPI strip (6 cells: 2 exit · 2 entry · 2 sweep) ---------- */
function renderHeroKpis() {
  const root = $("#home-kpis");
  if (!root) return;
  const eq = state.exitQueue, ch = state.churn, pw = state.pendingWithdrawals, en = state.entryQueue;

  if (!eq || !ch) {
    root.innerHTML = Array(6).fill(0).map(() => `
      <div class="cell" aria-hidden="true">
        <span class="k">&nbsp;</span>
        <span class="v" style="opacity: 0.15;">—</span>
        <span class="sub">loading…</span>
      </div>
    `).join("");
    return;
  }

  const exitSev = eq.queue_depth_epochs === 0 ? "clear"
                : eq.queue_depth_epochs <= 5 ? "short"
                : eq.queue_depth_epochs <= 20 ? "moderate"
                : "congested";
  const entrySev = en?.severity || "clear";

  const ppwCount = pw?.count ?? 0;
  const ppwEth   = pw?.total_amount_eth ?? 0;

  // Visual indicators — clamped 0–100 mini bars
  const validatorsPct = Math.min(100, Math.round((eq.total_exiting_validators / 200_000) * 100));
  const exitingEthPct = Math.min(100, Math.round((eq.total_exiting_balance_eth / 5_000_000) * 100));
  const depositCntPct = Math.min(100, Math.round(((en?.finalized_count ?? 0) / 100_000) * 100));
  const depositEthPct = Math.min(100, Math.round(((en?.finalized_eth ?? 0) / 10_000_000) * 100));
  const ppwCountPct   = Math.min(100, Math.round((ppwCount / 1_000) * 100));
  const ppwEthPct     = Math.min(100, Math.round((ppwEth / 50_000) * 100));

  const tiles = [
    {
      k: "Validators exiting",
      v: fmtInt(eq.total_exiting_validators),
      sub: `${fmtInt(eq.queue_depth_epochs)} epochs deep · ${exitSev}`,
      subCls: exitSev === "clear" ? "pos" : exitSev === "congested" ? "neg" : "",
      pct: validatorsPct,
      barCls: exitSev === "congested" ? "warn" : "",
    },
    {
      k: "Stake exiting",
      v: `${fmtEth(eq.total_exiting_balance_eth, 0)}`,
      sub: "ETH awaiting exit",
      pct: exitingEthPct,
      barCls: "",
    },
    {
      k: "Pending deposits",
      v: fmtInt(en?.finalized_count ?? 0),
      sub: en ? `${entrySev}` : "loading…",
      subCls: entrySev === "clear" ? "pos" : entrySev === "congested" ? "neg" : "",
      pct: depositCntPct,
      barCls: entrySev === "congested" ? "warn" : "",
    },
    {
      k: "Stake entering",
      v: en ? `${fmtEth(en.finalized_eth, 0)}` : "—",
      sub: "ETH awaiting activation",
      pct: depositEthPct,
      barCls: "",
    },
    {
      k: "Pending partials",
      v: fmtInt(ppwCount),
      sub: ppwCount > 0 ? "in sweep queue" : "queue empty",
      subCls: ppwCount === 0 ? "pos" : "",
      pct: ppwCountPct,
      barCls: "peer",
    },
    {
      k: "Partial ETH waiting",
      v: `${fmtEth(ppwEth, 0)}`,
      sub: "ETH in pending partials",
      pct: ppwEthPct,
      barCls: "peer",
    },
  ];

  root.innerHTML = tiles.map(t => `
    <div class="cell">
      <span class="k">${escapeHtml(t.k)}</span>
      <span class="v">${escapeHtml(String(t.v))}</span>
      <span class="sub"><span class="${t.subCls ?? ""}">${escapeHtml(t.sub)}</span></span>
      <span class="bar"><i class="${t.barCls ?? ""}" style="width: ${t.pct}%;"></i></span>
    </div>
  `).join("");
}

/* ---------- Exit queue dash-kpis ---------- */
/* ---------- Severity scale (shared across entry + exit) ----------
   Thresholds are defined in wait-time hours:
     clear      · < 2 hours
     short      · 2 hours – 2 days     (48 hours)
     moderate   · 2 days  – 10 days    (240 hours)
     congested  · 10 days +
   Gauge arc fills piecewise — each tier occupies 25% of the visible arc,
   so a "moderate" reading always lands in the third quadrant.
*/
const QUEUE_TIERS = {
  clear:     { maxHours: 2,    label: "clear" },
  short:     { maxHours: 48,   label: "short" },
  moderate:  { maxHours: 240,  label: "moderate" },
  congested: { maxHours: Infinity, label: "congested" },
};
function queueTier(waitHours) {
  if (waitHours == null || waitHours < 2)    return "clear";
  if (waitHours < 48)   return "short";
  if (waitHours < 240)  return "moderate";
  return "congested";
}
function queueFillPct(waitHours) {
  if (waitHours == null || waitHours <= 0) return 0;
  if (waitHours < 2)   return (waitHours / 2) * 0.25;
  if (waitHours < 48)  return 0.25 + ((waitHours - 2) / 46) * 0.25;
  if (waitHours < 240) return 0.50 + ((waitHours - 48) / 192) * 0.25;
  // Congested — cap at 30 days (~720 h) for full arc.
  return Math.min(1, 0.75 + ((waitHours - 240) / 480) * 0.25);
}
function tierBarCls(sev) {
  return sev === "congested" ? "warn" : sev === "moderate" ? "peer" : "";
}

/* ---------- Queue gauge renderer ---------- */
function gaugeHTML({ title, value, severity, footLeft, footRight, pct }) {
  const RADIUS = 100;
  const CIRC = Math.PI * RADIUS;
  const fillLen = CIRC * Math.max(0, Math.min(1, pct));
  const arcColor = severity === "clear" ? "var(--brand-5)"
                 : severity === "short"   ? "var(--brand-6)"
                 : severity === "moderate" ? "var(--peer)"
                 : "var(--warn)";
  return `
    <span class="gauge-title">${escapeHtml(title)}</span>
    <div class="arc-wrap">
      <svg viewBox="0 0 260 160" aria-label="${escapeHtml(title)} severity">
        <path d="M 30 130 A ${RADIUS} ${RADIUS} 0 0 1 230 130"
              fill="none" stroke="var(--slate-4)" stroke-width="20" stroke-linecap="round" />
        <path d="M 30 130 A ${RADIUS} ${RADIUS} 0 0 1 230 130"
              fill="none" stroke="${arcColor}" stroke-width="20" stroke-linecap="round"
              stroke-dasharray="${fillLen.toFixed(1)} ${CIRC.toFixed(1)}" />
        <text x="14"  y="152" font-family="var(--mono)" font-size="10" fill="var(--text-faint)" text-anchor="middle" letter-spacing="0.12em">${escapeHtml(footLeft)}</text>
        <text x="246" y="152" font-family="var(--mono)" font-size="10" fill="var(--text-faint)" text-anchor="middle" letter-spacing="0.12em">${escapeHtml(footRight)}</text>
      </svg>
    </div>
    <span class="arc-value">${escapeHtml(value)}</span>
    <span class="sev sev-${severity}">${escapeHtml(severity)}</span>
  `;
}

function renderExitQueueStats() {
  const root = $("#exit-gauge");
  if (!root) return;
  const eq = state.exitQueue;
  if (!eq) { root.innerHTML = `<div style="color: var(--text-faint); font-family: var(--mono); font-size: 11px;">Loading…</div>`; return; }

  const waitH = eq.estimated_wait_hours || 0;
  const severity = queueTier(waitH);
  const pct = queueFillPct(waitH);
  const depth = eq.queue_depth_epochs || 0;

  root.innerHTML = gaugeHTML({
    title: "Exit queue · wait",
    value: fmtHours(waitH),
    severity,
    footLeft: "CLEAR",
    footRight: "HIGH",
    pct,
  }) + `
    <span class="gauge-meta">${fmtInt(eq.total_exiting_validators)} validator${eq.total_exiting_validators === 1 ? "" : "s"} · ${fmtInt(depth)} epoch${depth === 1 ? "" : "s"} deep</span>
  `;

  renderExitKpis(severity, pct);
}

function renderEntryQueueStats() {
  const root = $("#entry-gauge");
  if (!root) return;
  const enq = state.entryQueue;
  if (!enq) { root.innerHTML = `<div style="color: var(--text-faint); font-family: var(--mono); font-size: 11px;">Loading…</div>`; return; }

  const days = enq.drain_days || 0;
  const waitH = days * 24;
  const severity = queueTier(waitH);
  const pct = queueFillPct(waitH);

  root.innerHTML = gaugeHTML({
    title: "Entry queue · drain",
    value: fmtHours(waitH),
    severity,
    footLeft: "CLEAR",
    footRight: "HIGH",
    pct,
  }) + `
    <span class="gauge-meta">${fmtInt(enq.finalized_count)} deposit${enq.finalized_count === 1 ? "" : "s"} · ${fmtEth(enq.finalized_eth, 0)} ETH</span>
  `;

  renderEntryKpis(severity, pct);
}

/* ---------- Stats cards under each gauge ---------- */
function _loadingCells(n) {
  return Array(n).fill(0).map(() => `
    <div class="cell" aria-hidden="true">
      <span class="k">&nbsp;</span>
      <span class="v" style="opacity: 0.15;">—</span>
      <span class="sub">loading…</span>
    </div>
  `).join("");
}

function renderEntryKpis(severity, fillPct) {
  const root = $("#entry-stats");
  if (!root) return;
  const en = state.entryQueue;
  if (!en) { root.innerHTML = _loadingCells(2); return; }
  const bar = tierBarCls(severity);
  const pct = Math.round((fillPct ?? 0) * 100);
  root.innerHTML = `
    <div class="cell">
      <span class="k">Pending deposits</span>
      <span class="v">${fmtInt(en.finalized_count)}</span>
      <span class="sub"><span class="${severity === "clear" ? "pos" : severity === "congested" ? "neg" : ""}">${escapeHtml(severity)}</span></span>
      <span class="bar"><i class="${bar}" style="width: ${pct}%;"></i></span>
    </div>
    <div class="cell">
      <span class="k">Stake entering</span>
      <span class="v">${fmtEth(en.finalized_eth, 0)}</span>
      <span class="sub">ETH awaiting activation</span>
      <span class="bar"><i class="${bar}" style="width: ${pct}%;"></i></span>
    </div>
  `;
}

function renderExitKpis(severity, fillPct) {
  const root = $("#exit-stats");
  if (!root) return;
  const eq = state.exitQueue;
  if (!eq) { root.innerHTML = _loadingCells(2); return; }
  const bar = tierBarCls(severity);
  const pct = Math.round((fillPct ?? 0) * 100);
  root.innerHTML = `
    <div class="cell">
      <span class="k">Validators exiting</span>
      <span class="v">${fmtInt(eq.total_exiting_validators)}</span>
      <span class="sub"><span class="${severity === "clear" ? "pos" : severity === "congested" ? "neg" : ""}">${escapeHtml(severity)}</span></span>
      <span class="bar"><i class="${bar}" style="width: ${pct}%;"></i></span>
    </div>
    <div class="cell">
      <span class="k">Stake exiting</span>
      <span class="v">${fmtEth(eq.total_exiting_balance_eth, 0)}</span>
      <span class="sub">ETH awaiting exit</span>
      <span class="bar"><i class="${bar}" style="width: ${pct}%;"></i></span>
    </div>
  `;
}

/* ---------- Pending withdrawals table ---------- */
function sortedWithdrawals() {
  if (!state.pendingWithdrawals) return [];
  const ws = [...state.pendingWithdrawals.withdrawals];
  const { key, dir } = state.pwSort;
  const sign = dir === "asc" ? 1 : -1;
  const get = (w) => {
    if (key === "index")  return Number(w.validator_index);
    if (key === "amount") return w.amount_eth;
    if (key === "epoch")  return w.withdrawable_epoch;
    if (key === "eta")    return new Date(w.withdrawable_time || 0).getTime();
    return 0;
  };
  ws.sort((a, b) => (get(a) - get(b)) * sign);
  return ws;
}
function renderPendingWithdrawals() {
  const tbody = $("#pw-tbody");
  const deck  = $("#pw-deck");
  const pill  = $("#pw-pill");
  const pillT = $("#pw-pill-total");
  const data  = state.pendingWithdrawals;
  if (!tbody) return;

  if (!data) {
    tbody.innerHTML = `<tr><td colspan="4" style="text-align:left; color: var(--text-faint); font-family: var(--mono);">Loading…</td></tr>`;
    if (deck) deck.textContent = "loading…";
    return;
  }
  if (data.count === 0) {
    tbody.innerHTML = `<tr><td colspan="4" style="text-align:left; color: var(--text-muted); font-family: var(--mono);">None pending.</td></tr>`;
    if (deck) deck.textContent = "queue empty";
    return;
  }
  if (deck) deck.textContent = `${fmtInt(data.count)} entries · ${fmtEth(data.total_amount_eth, 0)} ETH`;

  const sorted = sortedWithdrawals();
  tbody.innerHTML = sorted.map(w => {
    const etaIso = w.withdrawable_time;
    const ageHours = etaIso ? (new Date(etaIso).getTime() - Date.now()) / 3600000 : null;
    const tone = ageHours === null ? "" : ageHours <= 0 ? "tone-ok" : ageHours < 1 ? "tone-blue" : "";
    return `
      <tr>
        <td><a href="https://beaconcha.in/validator/${w.validator_index}" target="_blank" rel="noopener noreferrer">#${escapeHtml(w.validator_index)}</a></td>
        <td>${fmtEth(w.amount_eth, 4)} ETH</td>
        <td style="color: var(--text-muted);">${fmtInt(w.withdrawable_epoch)}</td>
        <td>
          <span class="${tone}">${timeFromNow(etaIso)}</span>
          <span style="margin-left: 10px; color: var(--text-faint);">${fmtDate(etaIso)}</span>
        </td>
      </tr>
    `;
  }).join("");
}

/* ---------- Predictor ---------- */
function wirePredictor() {
  state.predictMode = state.predictMode || "full";

  const modeLinks = $$("#predict-modes a[data-mode]");
  modeLinks.forEach(a => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      modeLinks.forEach(o => o.classList.toggle("active", o === a));
      state.predictMode = a.dataset.mode;
      $("#predict-amount").placeholder = state.predictMode === "full" ? "32" : "1";
      $("#predict-result").hidden = true;
      $("#predict-error").hidden = true;
    });
  });

  $("#predict-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const amount = parseFloat($("#predict-amount").value);
    if (!amount || amount <= 0) return;
    const errEl = $("#predict-error");
    const resEl = $("#predict-result");
    errEl.hidden = true;
    resEl.hidden = true;

    const gwei = Math.round(amount * GWEI);
    const path = state.predictMode === "full"
      ? `/predict/exit?balance=${gwei}`
      : `/predict/partial-withdrawal?amount=${gwei}`;

    try {
      const r = await api(path);
      renderPredictResult(r, amount);
      resEl.hidden = false;
    } catch (err) {
      errEl.textContent = err.message;
      errEl.hidden = false;
    }
  });
}

function renderPredictResult(r, amount) {
  const isFull = state.predictMode === "full";
  const exitEpoch = isFull ? r.predicted_exit_epoch : r.predicted_epoch;
  const queueEpochs = r.estimated_wait_epochs;
  const totalHours = r.estimated_withdrawable_hours;
  const queueHours = r.estimated_wait_hours;
  const heroLabel = isFull ? "Total time to withdrawable" : "Time until sweep-eligible";
  const sub = isFull
    ? "includes 256-epoch withdrawability delay · sweep position not included"
    : "256-epoch withdrawability delay applies to fresh requests";
  const modeLabel = isFull ? "Full exit" : "Partial";

  $("#predict-result").innerHTML = `
    <header class="result-head">
      <span class="kicker">— Prediction · <em>${fmtEth(amount, 4)} ETH</em> · ${escapeHtml(modeLabel)}</span>
      <button type="button" class="result-dismiss" data-dismiss="predict" aria-label="Dismiss result">Clear</button>
    </header>
    <div class="predict-hero">
      <span class="k">${escapeHtml(heroLabel)}</span>
      <span class="v">~${escapeHtml(fmtHours(totalHours))}</span>
      <span class="sub">${escapeHtml(sub)}</span>
    </div>
    <div class="predict-meta">
      <div class="cell">
        <span class="k">Queue wait</span>
        <span class="v">${queueEpochs === 0 ? "0 m" : "~" + escapeHtml(fmtHours(queueHours))}</span>
        <span class="sub">${fmtInt(queueEpochs)} epoch${queueEpochs === 1 ? "" : "s"} ahead${queueEpochs === 0 ? " · no wait" : ""}</span>
      </div>
      <div class="cell">
        <span class="k">${isFull ? "Exit epoch" : "Eligible epoch"}</span>
        <span class="v">${fmtInt(exitEpoch)}</span>
        <span class="sub">${escapeHtml(epochToDate(exitEpoch, r.current_epoch))}</span>
      </div>
      <div class="cell">
        <span class="k">Withdrawable epoch</span>
        <span class="v">${fmtInt(r.withdrawable_epoch)}</span>
        <span class="sub">${escapeHtml(epochToDate(r.withdrawable_epoch, r.current_epoch))}</span>
      </div>
    </div>
  `;
  $("#predict-result").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* ---------- Home: single-validator lookup using validatorDetail ---------- */
function wireHomeLookup() {
  const form  = $("#home-lookup-form");
  const input = $("#home-lookup-input");
  const panel = $("#home-lookup-detail");
  if (!form || !input || !panel) return;

  async function run(rawQuery) {
    const q = (rawQuery || "").trim();
    if (!q) return;
    panel.hidden = false;
    panel.innerHTML = `<div class="v-loading">Resolving validator…</div>`;
    try {
      const d = await validatorDetail.fetch(q);
      const subjectLabel = d.is_pending_deposit
        ? `Pending deposit · <em>${escapeHtml(d.pubkey.slice(0, 10))}…${escapeHtml(d.pubkey.slice(-6))}</em>`
        : `Validator <em>#${fmtInt(d.index)}</em>`;
      panel.innerHTML = `
        <header class="result-head">
          <span class="kicker">— Lookup · ${subjectLabel}</span>
          <button type="button" class="result-dismiss" data-dismiss="lookup" aria-label="Dismiss result">Clear</button>
        </header>
        ${validatorDetail.renderHeader(d)}
        ${validatorDetail.render(d)}
      `;
      panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (err) {
      panel.innerHTML = `<div class="v-err">Couldn’t resolve <em>${escapeHtml(q)}</em>. <span class="mono" style="opacity: 0.7;">(${escapeHtml(String(err.message || err))})</span></div>`;
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    run(input.value);
  });

  // Delegate the dismiss buttons inside the result stack to clear panels.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-dismiss]");
    if (!btn) return;
    if (btn.dataset.dismiss === "lookup") {
      const p = $("#home-lookup-detail");
      if (p) { p.hidden = true; p.innerHTML = ""; }
    } else if (btn.dataset.dismiss === "predict") {
      const p = $("#predict-result");
      if (p) { p.hidden = true; p.innerHTML = ""; }
    }
  });

  document.querySelectorAll("#home-lookup-examples a[data-q]").forEach(a => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      const q = a.dataset.q;
      input.value = q;
      document.querySelectorAll("#home-lookup-examples a").forEach(x => x.classList.remove("active"));
      a.classList.add("active");
      run(q);
    });
  });
}

/* ---------- Validators batch ---------- */
function parseValidatorInput(raw) {
  return raw.split(/[\n,\s]+/).map(s => s.trim()).filter(Boolean);
}
function matchesValFilter(v, f) {
  if (f === "all") return true;
  if (f === "compounding") return v.credential_type === "compounding";
  if (f === "execution")   return v.credential_type === "execution";
  if (f === "bls")         return v.credential_type === "bls";
  if (f === "active")      return v.status.includes("active");
  if (f === "exiting")     return v.status.includes("exit");
  if (f === "withdrawable")return v.status.includes("withdrawal");
  if (f === "slashed")     return v.status.includes("slashed");
  return true;
}
function renderValSummary() {
  const validators = state.batch?.validators ?? [];
  if (!validators.length) return;

  const statusCounts = {}, credCounts = {};
  let totBal = 0, totEff = 0;
  for (const v of validators) {
    const sk = statusChip(v.status).label;
    statusCounts[sk] = (statusCounts[sk] ?? 0) + 1;
    const ck = credentialChip(v.credential_type).label;
    credCounts[ck] = (credCounts[ck] ?? 0) + 1;
    totBal += v.balance_eth;
    totEff += v.effective_balance_gwei / GWEI;
  }

  const listRows = (obj) => Object.entries(obj).map(([k, n]) => `
    <span class="row"><span class="k">${escapeHtml(k.toLowerCase())}</span><span class="n">${fmtInt(n)}</span></span>
  `).join("");

  $("#val-summary").innerHTML = `
    <div class="dash-kpi">
      <span class="label">Loaded</span>
      <span class="value">${fmtInt(validators.length)}</span>
      <span class="foot neutral">validators</span>
    </div>
    <div class="dash-kpi list-value">
      <span class="label">By status</span>
      <span class="value">${listRows(statusCounts)}</span>
    </div>
    <div class="dash-kpi list-value">
      <span class="label">Credentials</span>
      <span class="value">${listRows(credCounts)}</span>
    </div>
    <div class="dash-kpi">
      <span class="label">Total balance</span>
      <span class="value">${fmtEth(totBal, 0)}</span>
      <span class="foot neutral">ETH</span>
    </div>
    <div class="dash-kpi">
      <span class="label">Total effective</span>
      <span class="value">${fmtEth(totEff, 0)}</span>
      <span class="foot neutral">ETH</span>
    </div>
  `;
}
function renderValFilters() {
  const validators = state.batch?.validators ?? [];
  const root = $("#val-filters");
  if (!validators.length) { root.innerHTML = ""; return; }

  const all = [
    { v: "all",         label: "All" },
    { v: "compounding", label: "Compounding" },
    { v: "execution",   label: "Non-compounding" },
    { v: "active",      label: "Active" },
    { v: "exiting",     label: "Exiting" },
    { v: "withdrawable",label: "Withdrawable" },
    { v: "slashed",     label: "Slashed" },
  ];
  const available = all.filter(f => f.v === "all" || validators.some(v => matchesValFilter(v, f.v)));
  if (available.length <= 2) { root.innerHTML = ""; return; }

  root.innerHTML = available.map(f => `
    <button type="button" class="filter-pill ${state.batchFilter === f.v ? "active" : ""}" data-filter="${f.v}">${f.label}</button>
  `).join("");
  $$(".filter-pill", root).forEach(b => {
    b.addEventListener("click", () => {
      state.batchFilter = b.dataset.filter;
      renderValFilters();
      renderValList();
    });
  });
}
function renderValList() {
  const validators = state.batch?.validators ?? [];
  const root = $("#val-list");
  const cnt  = $("#val-result-count");
  if (!validators.length) {
    root.innerHTML = "";
    cnt.textContent = "";
    return;
  }
  const filtered = validators.filter(v => matchesValFilter(v, state.batchFilter));
  cnt.textContent = state.batchFilter === "all"
    ? `Validators · ${fmtInt(validators.length)}`
    : `Validators · ${fmtInt(filtered.length)} of ${fmtInt(validators.length)}`;

  if (!filtered.length) {
    root.innerHTML = `<p style="font-family: var(--mono); font-size: 12px; color: var(--text-muted);">No validators match this filter.</p>`;
    return;
  }

  root.innerHTML = filtered.map(v => {
    const status = statusChip(v.status);
    const cred = credentialChip(v.credential_type);
    const exitEpoch = (v.exit_epoch && v.exit_epoch !== FAR_FUTURE) ? parseInt(v.exit_epoch) : null;
    const withEpoch = (v.withdrawable_epoch && v.withdrawable_epoch !== FAR_FUTURE) ? parseInt(v.withdrawable_epoch) : null;
    const isExiting = v.status.includes("exit") || v.status.includes("withdrawal");
    const epochSec = EPOCH_SECONDS;
    const currentEpoch = state.exitQueue?.current_epoch ?? 0;
    // Lookup key — pubkey for pending entries (no index), index otherwise
    const lookupKey = v.is_pending_deposit ? v.pubkey : String(v.index);
    const isExpanded = state.expandedValidators?.has?.(lookupKey);

    // Compact ETA formatter for the row (hours+minutes / days+hours)
    const fmtWait = (sec) => {
      if (sec == null || sec <= 0) return "0 m";
      const total = Math.round(sec / 60);
      const d = Math.floor(total / 1440);
      const h = Math.floor((total % 1440) / 60);
      const m = total % 60;
      if (d >= 1) return h ? `${d} d ${h} h` : `${d} d`;
      if (h >= 1) return m ? `${h} h ${m} m` : `${h} h`;
      return `${m} m`;
    };

    if (v.is_pending_deposit) {
      // Pre-validator row — confident pending-deposit presentation.
      const eta = v.pending_deposit_eta_seconds;
      return `
        <div class="val-card expandable ${isExpanded ? "expanded" : ""}" data-val-index="${escapeHtml(lookupKey)}">
          <div class="row1">
            <div class="meta">
              <span class="vidx-pending">Pending deposit</span>
              <span class="chip pending"><span class="dot"></span>pending_deposit</span>
              <span class="${cred.cls}">${v.credential_type === "compounding" ? `<span class="dot"></span>` : ""}${cred.label}</span>
            </div>
            <div class="balances">
              <div>
                <span class="label">Amount</span>
                <span class="value">${fmtEth(v.pending_deposit_amount_eth ?? v.balance_eth, 0)} ETH</span>
              </div>
              <div>
                <span class="label">ETA to active</span>
                <span class="value tone-peer">~${escapeHtml(fmtWait(eta))}</span>
              </div>
              <button type="button" class="val-toggle" data-val-toggle="${escapeHtml(lookupKey)}">${isExpanded ? "Collapse" : "Expand"}</button>
            </div>
          </div>
          <div class="pending-row">
            <span><span class="k">Position</span> <span class="v">#${fmtInt(v.pending_deposit_position)} of ${fmtInt(v.pending_deposit_queue_total)}</span></span>
            <span><span class="k">Ahead in queue</span> <span class="v">${fmtEth(v.pending_deposit_ahead_eth ?? 0, 0)} ETH</span></span>
            ${v.pending_deposit_slot != null ? `<span><span class="k">Queued at slot</span> <span class="v">${fmtInt(v.pending_deposit_slot)}</span></span>` : ""}
          </div>
          <p class="pubkey">${escapeHtml(v.pubkey)}</p>
          <div class="val-detail" data-val-detail="${escapeHtml(lookupKey)}" ${isExpanded ? "" : "hidden"}>
            ${isExpanded ? validatorDetail.renderCached(lookupKey) : ""}
          </div>
        </div>
      `;
    }

    // Existing fully-resolved validator row
    return `
      <div class="val-card expandable ${isExpanded ? "expanded" : ""}" data-val-index="${escapeHtml(lookupKey)}">
        <div class="row1">
          <div class="meta">
            <a href="https://beaconcha.in/validator/${v.index}" target="_blank" rel="noopener noreferrer">#${escapeHtml(v.index)}</a>
            <span class="${status.cls}"><span class="dot"></span>${status.label}</span>
            <span class="${cred.cls}">${v.credential_type === "compounding" ? `<span class="dot"></span>` : ""}${cred.label}</span>
          </div>
          <div class="balances">
            <div>
              <span class="label">Balance</span>
              <span class="value">${fmtEth(v.balance_eth, 4)} ETH</span>
            </div>
            <div>
              <span class="label">Effective</span>
              <span class="value muted">${(v.effective_balance_gwei / GWEI).toFixed(0)} ETH</span>
            </div>
            <button type="button" class="val-toggle" data-val-toggle="${escapeHtml(lookupKey)}">${isExpanded ? "Collapse" : "Expand"}</button>
          </div>
        </div>
        ${isExiting && exitEpoch !== null && currentEpoch > 0 ? `
          <div class="info-row">
            <span><span class="k">Exit epoch</span> <span class="v">${fmtInt(exitEpoch)}</span></span>
            <span><span class="k">${exitEpoch > currentEpoch ? "ETA to exit" : "Exited"}</span> <span class="v tone-peer">${exitEpoch > currentEpoch ? "~" + fmtWait((exitEpoch - currentEpoch) * epochSec) : fmtWait((currentEpoch - exitEpoch) * epochSec) + " ago"}</span></span>
            ${withEpoch !== null ? `<span><span class="k">Withdrawable</span> <span class="v">epoch ${fmtInt(withEpoch)} · ${withEpoch > currentEpoch ? "~" + fmtWait((withEpoch - currentEpoch) * epochSec) : "ready"}</span></span>` : ""}
          </div>
        ` : ""}
        <p class="pubkey">${escapeHtml(v.pubkey)}</p>
        <div class="val-detail" data-val-detail="${escapeHtml(lookupKey)}" ${isExpanded ? "" : "hidden"}>
          ${isExpanded ? validatorDetail.renderCached(lookupKey) : ""}
        </div>
      </div>
    `;
  }).join("");

  // The whole card is the click target. Skip external links so they navigate
  // normally, and skip clicks inside the expanded detail panel so users can
  // interact with tables / select text without collapsing.
  root.querySelectorAll(".val-card.expandable").forEach(card => {
    card.addEventListener("click", (e) => {
      if (e.target.closest("a[href^='http']")) return;
      if (e.target.closest(".val-detail")) return;
      const idx = card.dataset.valIndex;
      if (idx) validatorDetail.toggle(idx);
    });
  });
}

/* ---------- Validator detail (expand-in-place) ---------- */
const validatorDetail = (() => {
  const SECS_PER_EPOCH = EPOCH_SECONDS;
  const STATES = [
    { id: "deposited",           label: "Deposited" },
    { id: "pending_deposit",     label: "Queued deposits" },
    { id: "pending_initialized", label: "Initialised" },
    { id: "pending_queued",      label: "Queued for activation" },
    { id: "active_ongoing",      label: "Active" },
    { id: "active_exiting",      label: "Exiting" },
    { id: "exited_unslashed",    label: "Exited" },
    { id: "withdrawal_possible", label: "Withdrawable" },
    { id: "withdrawal_done",     label: "Swept" },
  ];
  const STATUS_TO_STATE = {
    pending_deposit:      "pending_deposit",   // pre-validator — only in pending_deposits
    pending_initialized:  "pending_initialized",
    pending_queued:       "pending_queued",
    active_ongoing:       "active_ongoing",
    active_exiting:       "active_exiting",
    active_slashed:       "active_exiting",
    exited_unslashed:     "exited_unslashed",
    exited_slashed:       "exited_unslashed",
    withdrawal_possible:  "withdrawal_possible",
    withdrawal_done:      "withdrawal_done",
  };
  const cache = new Map();

  function fmtPub(p) { return p || "—"; }
  function fmtAddr(a) { if (!a) return "no execution address (BLS credentials)"; return a; }
  function fmtDur(seconds) {
    if (seconds == null) return "—";
    if (seconds < 60) return "< 1 m";
    const totalMin = Math.round(seconds / 60);
    const d = Math.floor(totalMin / 1440);
    const h = Math.floor((totalMin % 1440) / 60);
    const m = totalMin % 60;
    const parts = [];
    if (d) parts.push(`${d} d`);
    if (h || (d && m)) parts.push(`${h} h`);
    if (m) parts.push(`${m} m`);
    return parts.join(" ") || "0 m";
  }
  function fmtAge(seconds) {
    if (seconds == null || seconds < 0) return "—";
    if (seconds < 60) return `${seconds | 0} s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
    const days = seconds / 86400;
    if (days < 60) return `${Math.round(days)} d ago`;
    const months = days / 30.4;
    if (months < 24) return `${Math.round(months)} mo ago`;
    return `${(days / 365).toFixed(1)} y ago`;
  }
  function fmtRelEpoch(epoch, currentEpoch) {
    if (epoch == null) return "not scheduled";
    const secsAgo = (currentEpoch - epoch) * SECS_PER_EPOCH;
    if (secsAgo > 0) return fmtAge(secsAgo);
    if (secsAgo < 0) return `~${fmtDur(-secsAgo)} from now`;
    return "this epoch";
  }
  function chipForCreds(t) {
    if (t === "compounding") return `<span class="v-chip compounding">Compounding · 0x02</span>`;
    if (t === "execution")   return `<span class="v-chip execution">Execution · 0x01</span>`;
    return `<span class="v-chip bls">BLS · 0x00</span>`;
  }

  function chipForStatusInternal(status, slashed) {
    if (slashed) return `<span class="v-chip warn">Slashed</span>`;
    if (status === "pending_deposit")     return `<span class="v-chip pending">pending_deposit</span>`;
    if (status === "active_ongoing")      return `<span class="v-chip active">active_ongoing</span>`;
    if (status === "active_exiting")      return `<span class="v-chip exiting">active_exiting</span>`;
    if (status === "exited_unslashed")    return `<span class="v-chip exited">exited_unslashed</span>`;
    if (status === "withdrawal_possible") return `<span class="v-chip exited">withdrawal_possible</span>`;
    if (status === "withdrawal_done")     return `<span class="v-chip exited">withdrawal_done</span>`;
    return `<span class="v-chip bls">${escapeHtml(status)}</span>`;
  }

  function renderHeaderBlock(d) {
    const idLabel = d.is_pending_deposit
      ? `<span class="vid is-pending">Pending deposit</span>`
      : `<span class="vid">#${fmtInt(d.index)}</span>`;
    return `
      <div class="v-header">
        <div class="v-header-top">
          ${idLabel}
          ${chipForStatusInternal(d.status, d.slashed)}
          ${chipForCreds(d.credential_type)}
        </div>
        <div class="v-pubkey-row">
          <span class="k">Pubkey</span>
          <span class="v">${escapeHtml(d.pubkey || "—")}</span>
        </div>
      </div>
    `;
  }

  function renderKpis(d) {
    if (d.is_pending_deposit) return renderPendingHero(d);
    return `
      <div class="v-kpis">
        <div class="cell">
          <span class="k">Balance</span>
          <span class="v">${fmtEth(d.balance_eth, 4)}</span>
          <span class="sub">effective ${fmtEth(d.effective_balance_eth, 4)} ETH</span>
        </div>
        <div class="cell">
          <span class="k">Activation</span>
          <span class="v">${d.activation_epoch != null ? fmtInt(d.activation_epoch) : "—"}</span>
          <span class="sub">${escapeHtml(fmtRelEpoch(d.activation_epoch, d.current_epoch))}</span>
        </div>
        <div class="cell">
          <span class="k">Exit epoch</span>
          <span class="v">${d.exit_epoch != null ? fmtInt(d.exit_epoch) : "—"}</span>
          <span class="sub">${escapeHtml(fmtRelEpoch(d.exit_epoch, d.current_epoch))}</span>
        </div>
        <div class="cell">
          <span class="k">Withdrawable</span>
          <span class="v">${d.withdrawable_epoch != null ? fmtInt(d.withdrawable_epoch) : "—"}</span>
          <span class="sub">${escapeHtml(fmtRelEpoch(d.withdrawable_epoch, d.current_epoch))}</span>
        </div>
      </div>
    `;
  }

  // Confident pending-deposit hero — ETA is the focal number.
  function renderPendingHero(d) {
    const dep = (d.pending_deposits || [])[0];
    if (!dep) return "";
    const aheadFrac = dep.queue_total ? (dep.position - 1) / dep.queue_total : 0;
    const aheadPct  = (aheadFrac * 100).toFixed(1);
    return `
      <div class="v-pending-hero">
        <div class="v-pending-eta">
          <span class="k">ETA to activation</span>
          <span class="v">~${escapeHtml(fmtDur(dep.eta_seconds))}</span>
          <span class="sub">${fmtEth(dep.ahead_eth ?? 0, 0)} ETH must drain before this deposit is processed</span>
        </div>
        <div class="v-pending-meta">
          <div class="cell">
            <span class="k">Position</span>
            <span class="v">#${fmtInt(dep.position)}</span>
            <span class="sub">of ${fmtInt(dep.queue_total)} · ${aheadPct}% ahead</span>
          </div>
          <div class="cell">
            <span class="k">Deposit amount</span>
            <span class="v">${fmtEth(dep.amount_eth, 0)}</span>
            <span class="sub">ETH</span>
          </div>
          <div class="cell">
            <span class="k">Queued at slot</span>
            <span class="v">${dep.slot != null ? fmtInt(dep.slot) : "—"}</span>
            <span class="sub">${dep.slot != null ? "in CL block" : "unknown"}</span>
          </div>
        </div>
      </div>
    `;
  }
  function throttleWait(from, to, d) {
    if (from === "active_exiting" && to === "exited_unslashed" && d.exit_epoch != null) {
      const secs = (d.exit_epoch - d.current_epoch) * SECS_PER_EPOCH;
      return secs > 0 ? `~${fmtDur(secs)}` : "now";
    }
    if (from === "exited_unslashed" && to === "withdrawal_possible" && d.withdrawable_epoch != null) {
      const w = (d.withdrawable_epoch - d.current_epoch) * SECS_PER_EPOCH;
      const e = d.exit_epoch != null ? (d.exit_epoch - d.current_epoch) * SECS_PER_EPOCH : 0;
      return `~${fmtDur(Math.max(0, w - e))}`;
    }
    if (from === "withdrawal_possible" && to === "withdrawal_done") return "~hours";
    return null;
  }
  function renderRail(d) {
    const currentStateId = STATUS_TO_STATE[d.status] || "active_ongoing";
    const currentIdx = STATES.findIndex(s => s.id === currentStateId);
    let html = `
      <aside class="v-sidebar">
        <span class="kicker">— Lifecycle</span>
        <ol class="rail-list">
    `;
    for (let i = 0; i < STATES.length; i++) {
      const s = STATES[i];
      const when = i < currentIdx ? "past" : i === currentIdx ? "now" : "future";
      const ts = when === "now"
        ? `<span class="ts">now</span>`
        : (s.id === "active_ongoing" && d.activation_epoch != null)
          ? `<span class="ts">epoch ${fmtInt(d.activation_epoch)}<br/>${escapeHtml(fmtAge((d.current_epoch - d.activation_epoch) * SECS_PER_EPOCH))}</span>`
          : (s.id === "exited_unslashed" && d.exit_epoch != null)
            ? `<span class="ts">epoch ${fmtInt(d.exit_epoch)}<br/>${escapeHtml(fmtRelEpoch(d.exit_epoch, d.current_epoch))}</span>`
            : (s.id === "withdrawal_possible" && d.withdrawable_epoch != null)
              ? `<span class="ts">epoch ${fmtInt(d.withdrawable_epoch)}<br/>${escapeHtml(fmtRelEpoch(d.withdrawable_epoch, d.current_epoch))}</span>`
              : `<span class="ts">—</span>`;
      html += `
        <li class="rl-node" data-when="${when}">
          <span class="rl-dot"></span>
          <span class="lab"><span class="name">${escapeHtml(s.label)}</span></span>
          ${ts}
        </li>
      `;
      if (i < STATES.length - 1) {
        const fromId = STATES[i].id, toId = STATES[i + 1].id;
        const onFlag = i < currentIdx ? "true" : i === currentIdx ? "next" : "false";
        const wait = i === currentIdx ? throttleWait(fromId, toId, d) : null;
        html += `
          <li class="rl-throttle" data-on="${onFlag}">
            <span class="spine"></span>
            <span class="chip-cell">${wait ? `<span class="wait">${escapeHtml(wait)}</span>` : ""}</span>
          </li>
        `;
      }
    }
    return html + `</ol></aside>`;
  }

  function renderMainSections(d) {
    const exit = d.exit_queue_position;
    const pp = d.pending_partial_withdrawals || [];
    const pc = d.pending_consolidations || [];
    let html = "";

    if (exit) {
      html += `
        <section class="v-section">
          <span class="kicker">— Exit queue position</span>
          <div class="v-hero-row">
            <span class="big">#${fmtInt(exit.position)} <span style="font-size: 0.55em; font-style: normal; font-weight: 540; font-family: var(--mono); font-stretch: normal; color: var(--text-muted); letter-spacing: 0.04em; margin-left: 6px;">of ${fmtInt(exit.total)}</span></span>
            <span class="deck">
              ETA <span class="v">~${escapeHtml(fmtDur(exit.eta_seconds))}</span> to exit_epoch <span class="v">${fmtInt(exit.exit_epoch)}</span> · churn-gated by <span class="v">ae_churn</span>
            </span>
          </div>
        </section>
      `;
    }

    if (pp.length > 0) {
      const rows = pp.map(p => `
        <tr>
          <td>${fmtEth(p.amount_eth, 4)} ETH</td>
          <td>${fmtInt(p.withdrawable_epoch)}</td>
          <td>#${fmtInt(p.position)} <span style="color: var(--text-faint);">of ${fmtInt(p.queue_total)}</span></td>
          <td>~${escapeHtml(fmtDur(p.eta_seconds))}</td>
        </tr>
      `).join("");
      html += `
        <section class="v-section">
          <span class="kicker">— Pending partial withdrawals · ${pp.length} ${pp.length === 1 ? "entry" : "entries"}</span>
          <table class="v-table">
            <thead><tr><th>Amount</th><th>Withdrawable epoch</th><th>Sweep position</th><th>ETA</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </section>
      `;
    }

    if (pc.length > 0) {
      const rows = pc.map(c => `
        <tr>
          <td>${escapeHtml(c.role)}</td>
          <td>#${fmtInt(c.source_index)}</td>
          <td>#${fmtInt(c.target_index)}</td>
          <td>#${fmtInt(c.position)} <span style="color: var(--text-faint);">of ${fmtInt(c.queue_total)}</span></td>
        </tr>
      `).join("");
      html += `
        <section class="v-section">
          <span class="kicker">— Pending consolidations · ${pc.length} ${pc.length === 1 ? "entry" : "entries"}</span>
          <table class="v-table">
            <thead><tr><th>Role</th><th>Source</th><th>Target</th><th>Queue position</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </section>
      `;
    }

    // Pending-deposit position info is rendered as a hero above (renderPendingHero),
    // so we skip the duplicate section here for the is_pending_deposit case.
    const pd = d.pending_deposits || [];
    if (pd.length > 0 && !d.is_pending_deposit) {
      const dep = pd[0];
      const aheadStr = dep.ahead_eth != null
        ? `<span class="v">${fmtEth(dep.ahead_eth, 0)}</span> ETH ahead in queue · `
        : "";
      html += `
        <section class="v-section">
          <span class="kicker">— Pending deposit · position in entry queue</span>
          <div class="v-hero-row">
            <span class="big">#${fmtInt(dep.position)} <span style="font-size: 0.55em; font-style: normal; font-weight: 540; font-family: var(--mono); font-stretch: normal; color: var(--text-muted); letter-spacing: 0.04em; margin-left: 6px;">of ${fmtInt(dep.queue_total)}</span></span>
            <span class="deck">
              ${fmtEth(dep.amount_eth, 4)} ETH deposit · ${aheadStr}ETA <span class="v">~${escapeHtml(fmtDur(dep.eta_seconds))}</span> to validator creation${dep.slot != null ? ` · queued at slot <span class="v">${fmtInt(dep.slot)}</span>` : ""}
            </span>
          </div>
        </section>
      `;
    }

    const hasAddress = !!d.credential_address;
    const credsLabel = d.credential_type === "compounding" ? "Compounding · 0x02"
                    : d.credential_type === "execution"   ? "Execution · 0x01"
                    : "BLS · 0x00";
    const credsChipClass = d.credential_type === "compounding" ? "compounding"
                        : d.credential_type === "execution"   ? "execution" : "bls";
    html += `
      <section class="v-section">
        <span class="kicker">— Withdrawal credentials</span>
        <div class="v-creds-hero">
          <span class="addr">${escapeHtml(hasAddress ? d.credential_address : "no execution address (BLS credentials)")}</span>
          <span class="v-chip ${credsChipClass}">${escapeHtml(credsLabel)}</span>
        </div>
        <div class="v-creds-foot">
          <span class="pair"><span class="k">Max effective</span><span class="v">${d.credential_type === "compounding" ? "2,048 ETH" : "32 ETH"}</span></span>
          <span class="pair"><span class="k">Current balance</span><span class="v">${fmtEth(d.balance_eth, 4)} ETH</span></span>
          <span class="pair"><span class="k">Effective balance</span><span class="v">${fmtEth(d.effective_balance_eth, 4)} ETH</span></span>
          <span class="pair"><span class="k">Slashed</span><span class="v">${d.slashed ? "yes" : "no"}</span></span>
        </div>
      </section>
    `;

    return html;
  }

  function renderFull(d) {
    return `
      <div class="v-body">
        <div class="v-main">
          ${renderKpis(d)}
          ${renderMainSections(d)}
        </div>
        ${renderRail(d)}
      </div>
    `;
  }

  function renderCached(idx) {
    const d = cache.get(idx);
    if (!d) return `<div class="v-loading">Loading detail…</div>`;
    return renderFull(d);
  }

  async function fetchDetail(idx) {
    if (cache.has(idx)) return cache.get(idx);
    const r = await fetch(`/validator/${encodeURIComponent(idx)}/lookup`);
    if (!r.ok) {
      let detail = `${r.status}`;
      try { detail = (await r.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    const data = await r.json();
    cache.set(idx, data);
    return data;
  }

  async function toggle(idx) {
    if (!state.expandedValidators) state.expandedValidators = new Set();
    const card  = document.querySelector(`.val-card[data-val-index="${CSS.escape(idx)}"]`);
    const panel = document.querySelector(`.val-detail[data-val-detail="${CSS.escape(idx)}"]`);
    const btn   = document.querySelector(`[data-val-toggle="${CSS.escape(idx)}"]`);
    if (!card || !panel) return;
    const expanded = state.expandedValidators.has(idx);
    if (expanded) {
      state.expandedValidators.delete(idx);
      card.classList.remove("expanded");
      panel.hidden = true;
      panel.innerHTML = "";
      if (btn) btn.textContent = "Expand";
      return;
    }
    state.expandedValidators.add(idx);
    card.classList.add("expanded");
    panel.hidden = false;
    if (btn) btn.textContent = "Collapse";
    panel.innerHTML = `<div class="v-loading">Resolving validator…</div>`;
    try {
      const d = await fetchDetail(idx);
      panel.innerHTML = renderFull(d);
    } catch (err) {
      panel.innerHTML = `<div class="v-err">${escapeHtml(String(err.message || err))}</div>`;
    }
  }

  function reset() {
    if (state.expandedValidators) state.expandedValidators.clear();
    cache.clear();
  }

  return { toggle, renderCached, reset, render: renderFull, fetch: fetchDetail, renderHeader: renderHeaderBlock };
})();
async function loadValidators(ids) {
  if (!ids.length) return;
  $("#val-loading").hidden = false;
  $("#val-error").hidden = true;
  state.batchFilter = "all";
  validatorDetail.reset();
  try {
    const r = await fetch("/validators/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ validators: ids }),
    });
    if (!r.ok) {
      const text = await r.text();
      throw new Error(`${r.status}: ${text}`);
    }
    const data = await r.json();
    state.batch = data;
    $("#val-results").hidden = false;

    const errBox = $("#val-batch-errors");
    errBox.innerHTML = data.errors.length
      ? data.errors.map(e => `<div class="batch-error">${escapeHtml(e)}</div>`).join("")
      : "";

    renderValSummary();
    renderValFilters();
    renderValList();

    // Auto-expand when there's exactly one result — single-validator UX.
    if (data.validators.length === 1) {
      const only = data.validators[0];
      const key = only.is_pending_deposit ? only.pubkey : String(only.index);
      validatorDetail.toggle(key);
    }
  } catch (err) {
    $("#val-error").textContent = err.message;
    $("#val-error").hidden = false;
  } finally {
    $("#val-loading").hidden = true;
  }
}
function wireValidators() {
  const input = $("#val-input");
  const countEl = $("#val-count");

  function updateCount() {
    if (!countEl || !input) return;
    const n = parseValidatorInput(input.value).length;
    countEl.querySelector("strong").textContent = String(n);
    countEl.classList.toggle("over", n > 100);
  }

  $("#val-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const ids = parseValidatorInput(input.value);
    loadValidators(ids);
  });
  input?.addEventListener("input", updateCount);
  updateCount();

  $("#val-upload-btn").addEventListener("click", () => $("#val-file").click());
  $("#val-file").addEventListener("change", (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const text = reader.result;
      input.value = text;
      updateCount();
      loadValidators(parseValidatorInput(text));
    };
    reader.onerror = () => {
      $("#val-error").textContent = "Failed to read file";
      $("#val-error").hidden = false;
    };
    reader.readAsText(file);
    e.target.value = "";
  });

  // Try-chip → populate textarea and submit.
  document.querySelectorAll("#val-examples a[data-q]").forEach(a => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      const q = a.dataset.q;
      input.value = q;
      updateCount();
      loadValidators(parseValidatorInput(q));
    });
  });
}

/* ---------- Consolidations ---------- */
async function loadConsolidations() {
  try {
    const data = await api("/consolidations");
    state.consolidations = data;
    renderConsolidations();
  } catch (err) {
    state.consolidations = { error: err.message };
    $("#con-error").textContent = err.message;
    $("#con-error").hidden = false;
  }
}
function renderConsolidations() {
  const data = state.consolidations;
  const kpiRoot = $("#con-kpis");
  const listRoot = $("#con-list");
  if (!data || data.error) {
    if (kpiRoot) kpiRoot.innerHTML = "";
    if (listRoot) listRoot.innerHTML = "";
    return;
  }

  kpiRoot.innerHTML = `
    <div class="dash-kpi">
      <span class="label">Pending consolidations</span>
      <span class="value">${fmtInt(data.count)}</span>
      <span class="foot neutral">source validators</span>
    </div>
    <div class="dash-kpi">
      <span class="label">Target validators</span>
      <span class="value">${fmtInt(data.target_count)}</span>
      <span class="foot neutral">absorbing stake</span>
    </div>
    <div class="dash-kpi">
      <span class="label">Total ETH consolidating</span>
      <span class="value">${fmtEth(data.total_eth, 0)}</span>
      <span class="foot neutral">ETH</span>
    </div>
  `;

  if (data.targets.length === 0) {
    listRoot.innerHTML = `<p style="font-family: var(--mono); font-size: 12px; color: var(--text-muted);">No pending consolidations.</p>`;
    return;
  }

  listRoot.innerHTML = data.targets.map(t => {
    const status = statusChip(t.target_status);
    const isOpen = state.consolidationsOpen.has(t.target_index);
    return `
      <div class="con-card ${isOpen ? "open" : ""}" data-target="${t.target_index}">
        <button type="button" class="row-button">
          <svg class="chev" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" />
          </svg>
          <div class="ident">
            <a href="https://beaconcha.in/validator/${t.target_index}" target="_blank" rel="noopener noreferrer">#${escapeHtml(t.target_index)}</a>
            <span class="${status.cls}"><span class="dot"></span>${status.label}</span>
          </div>
          <div class="stats">
            <div><span class="label">Sources</span><span class="value">${fmtInt(t.source_count)}</span></div>
            <div><span class="label">Incoming</span><span class="value">${fmtEth(t.total_incoming_eth, 0)} ETH</span></div>
            <div><span class="label">Current balance</span><span class="value muted">${fmtEth(t.target_balance_eth, 2)} ETH</span></div>
          </div>
        </button>
        ${isOpen ? `
          <div class="expanded">
            <table>
              <thead>
                <tr>
                  <th>Validator</th>
                  <th>Status</th>
                  <th>Balance</th>
                  <th>Effective</th>
                </tr>
              </thead>
              <tbody>
                ${t.sources.map(s => {
                  const ss = statusChip(s.status);
                  return `
                    <tr>
                      <td><a href="https://beaconcha.in/validator/${s.index}" target="_blank" rel="noopener noreferrer" style="color: var(--text-strong); text-decoration: none;">#${escapeHtml(s.index)}</a></td>
                      <td style="text-align:left;"><span class="${ss.cls}"><span class="dot"></span>${ss.label}</span></td>
                      <td>${fmtEth(s.balance_eth, 4)} ETH</td>
                      <td style="color: var(--text-muted);">${fmtEth(s.effective_balance_eth, 0)} ETH</td>
                    </tr>
                  `;
                }).join("")}
              </tbody>
            </table>
            <p class="pubkey">${escapeHtml(t.target_pubkey)}</p>
          </div>
        ` : ""}
      </div>
    `;
  }).join("");

  $$(".con-card .row-button", listRoot).forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      const card = btn.closest(".con-card");
      const id = card.dataset.target;
      if (state.consolidationsOpen.has(id)) state.consolidationsOpen.delete(id);
      else state.consolidationsOpen.add(id);
      renderConsolidations();
    });
  });
}

/* ---------- Sortable PW table ---------- */
function wirePwTable() {
  $$("#pw-table thead th").forEach(th => {
    th.style.cursor = "pointer";
    th.addEventListener("click", () => {
      const k = th.dataset.sort;
      if (state.pwSort.key === k) {
        state.pwSort.dir = state.pwSort.dir === "asc" ? "desc" : "asc";
      } else {
        state.pwSort.key = k;
        state.pwSort.dir = "asc";
      }
      renderPendingWithdrawals();
    });
  });
}

/* ============================================================
   Methodology · validator lifecycle rail
   ------------------------------------------------------------
   9 spec-aligned states (Beacon REST API status enum + the two
   pre-validator stages) connected by 8 throttles. Each
   methodology highlights a contiguous slice and fills in the
   live durations from the API summary.
   ============================================================ */
const lifecycle = (() => {
  const STATES = [
    { id: "deposited",           label: "Deposited",             sub: "EL deposit included" },
    { id: "pending_deposit",     label: "Queued deposits",       sub: "state.pending_deposits" },
    { id: "pending_initialized", label: "Initialised",           sub: "pending_initialized" },
    { id: "pending_queued",      label: "Queued for activation", sub: "pending_queued" },
    { id: "active_ongoing",      label: "Active",                sub: "active_ongoing" },
    { id: "active_exiting",      label: "Exiting",               sub: "active_exiting" },
    { id: "exited_unslashed",    label: "Exited",                sub: "exited_unslashed" },
    { id: "withdrawal_possible", label: "Withdrawable",          sub: "withdrawal_possible" },
    { id: "withdrawal_done",     label: "Swept",                 sub: "withdrawal_done" },
  ];

  // 8 throttles indexed by the destination state. `name` is plain language;
  // `spec` is the consensus-spec function or constant the throttle is gated by.
  const THROTTLES = [
    { id: "a", from: "deposited",           to: "pending_deposit",     name: "Block inclusion",       spec: "process_deposit_request" },
    { id: "b", from: "pending_deposit",     to: "pending_initialized", name: "Deposit-queue drain",   spec: "ae_churn · 16 per epoch" },
    { id: "c", from: "pending_initialized", to: "pending_queued",      name: "Eligibility set",       spec: "effective balance ≥ 32 ETH" },
    { id: "d", from: "pending_queued",      to: "active_ongoing",      name: "Activation queue",      spec: "finality + 4-epoch seed lookahead" },
    { id: "e", from: "active_ongoing",      to: "active_exiting",      name: "Exit churn",            spec: "ae_churn" },
    { id: "f", from: "active_exiting",      to: "exited_unslashed",    name: "Wait to exit epoch",    spec: "linear, until exit_epoch" },
    { id: "g", from: "exited_unslashed",    to: "withdrawal_possible", name: "Withdrawability delay", spec: "+256 epochs" },
    { id: "h", from: "withdrawal_possible", to: "withdrawal_done",     name: "Withdrawal sweep",      spec: "FIFO · 16 per payload" },
  ];

  const SLICE = {
    "entry-queue":        { states: ["deposited","pending_deposit","pending_initialized","pending_queued","active_ongoing"], throttles: ["a","b","c","d"] },
    "exit-queue":         { states: ["active_ongoing","active_exiting","exited_unslashed","withdrawal_possible","withdrawal_done"], throttles: ["e","f","g","h"] },
    "consolidation":      { states: ["active_ongoing","active_exiting","exited_unslashed","withdrawal_possible","withdrawal_done"], throttles: ["e","f","g","h"], specOverride: { e: "cons_churn", h: "instant at epoch boundary" } },
    "partial-withdrawal": { states: ["active_ongoing"], throttles: ["h"], nameOverride: { h: "Partial sweep" }, specOverride: { h: "MAX_PENDING_PARTIALS_PER_WITHDRAWALS_SWEEP = 8" } },
  };

  let built = false;

  function build() {
    const rail = $("#lifecycle-rail");
    if (!rail || built) return;
    const list = document.createElement("ol");
    list.className = "lc-list";
    STATES.forEach((s, i) => {
      const node = document.createElement("li");
      node.className = "lc-node";
      node.dataset.state = s.id;
      node.dataset.active = "false";
      node.innerHTML = `
        <span class="lc-dot" aria-hidden="true"></span>
        <span class="lc-label">${escapeHtml(s.label)}<span class="lc-sub">${escapeHtml(s.sub)}</span></span>
      `;
      list.appendChild(node);
      const th = THROTTLES[i];
      if (th) {
        const t = document.createElement("li");
        t.className = "lc-throttle";
        t.dataset.throttle = th.id;
        t.dataset.to = th.to;
        t.dataset.active = "false";
        t.innerHTML = `
          <span class="lc-spine" aria-hidden="true"></span>
          <span class="lc-chip">
            <span class="lc-name" data-name>${escapeHtml(th.name)}</span>
            <span class="lc-wait" data-wait>—</span>
            <span class="lc-spec" data-spec>${escapeHtml(th.spec)}</span>
          </span>
        `;
        list.appendChild(t);
      }
    });
    rail.appendChild(list);
    built = true;
  }

  // Map the API summary into per-throttle wait strings.
  function waitsFor(kind, summary) {
    if (!summary) return {};
    const fmt = (s) => (s == null ? "—" : `~${fmtDaysHours(s)}`);
    if (kind === "entry-queue") {
      const drain = summary.drain_seconds ?? (summary.epochs_to_drain ?? 0) * 384;
      const tail = (summary.activation_tail_epochs ?? 0) * 384;
      return {
        a: "≤ 1 slot",
        b: fmt(drain),
        c: "~6m 24s",
        d: tail ? fmt(tail) : "—",
      };
    }
    if (kind === "exit-queue") {
      const toExit = summary.wait_to_exit_seconds;
      const toW = summary.wait_to_withdrawable_seconds;
      const delay = (toW != null && toExit != null) ? toW - toExit : null;
      return {
        e: "≤ 1 slot",
        f: fmt(toExit),
        g: fmt(delay),
        h: "~hours",
      };
    }
    if (kind === "consolidation") {
      if (summary.stalled) return { e: "stalled · zero churn", f: "—", g: "—", h: "—" };
      const toCons = summary.wait_to_consolidation_seconds;
      const toW = summary.wait_to_withdrawable_seconds;
      const delay = (toW != null && toCons != null) ? toW - toCons : null;
      return {
        e: fmt(toCons),
        f: "skipped",
        g: fmt(delay),
        h: "≤ 1 epoch",
      };
    }
    if (kind === "partial-withdrawal") {
      return { h: fmt(summary.wait_seconds) };
    }
    return {};
  }

  function update(kind, summary) {
    build();
    const slice = SLICE[kind];
    if (!slice) return;
    const activeStates = new Set(slice.states);
    const activeThrottles = new Set(slice.throttles);
    const waits = waitsFor(kind, summary);

    $$("#lifecycle-rail .lc-node").forEach(n => {
      n.dataset.active = activeStates.has(n.dataset.state) ? "true" : "false";
    });
    $$("#lifecycle-rail .lc-throttle").forEach(t => {
      const id = t.dataset.throttle;
      const isActive = activeThrottles.has(id);
      t.dataset.active = isActive ? "true" : "false";
      const def = THROTTLES.find(x => x.id === id);
      const nameEl = t.querySelector("[data-name]");
      const waitEl = t.querySelector("[data-wait]");
      const specEl = t.querySelector("[data-spec]");
      const nameOverride = isActive ? slice.nameOverride?.[id] : null;
      const specOverride = isActive ? slice.specOverride?.[id] : null;
      if (nameEl) nameEl.textContent = nameOverride ?? def?.name ?? "";
      if (waitEl) waitEl.textContent = isActive ? (waits[id] ?? "—") : "—";
      if (specEl) specEl.textContent = specOverride ?? def?.spec ?? "";
    });
  }

  return { update };
})();

/* ============================================================
   Methodology · live spec waterfall
   ============================================================ */
const methodology = (() => {
  const state = {
    kind: "entry-queue",
    lastValues: {},        // resultKey → prior value (for pulse detection)
    pollTimer: null,
    input: null,           // current numeric input (when applicable)
    initialised: false,
  };

  const INPUT_BY_KIND = {
    "entry-queue": null,
    "exit-queue": { default: 32, unit: "ETH", label: "Hypothetical exit" },
    "consolidation": { default: 32, unit: "ETH", label: "Hypothetical consolidation" },
    "partial-withdrawal": { default: 1, unit: "ETH", label: "Hypothetical partial" },
  };

  // Heuristic syntax tinting for the spec block.
  function tintSpec(s) {
    return escapeHtml(s)
      .replace(/\b(def|return|if|else|elif|for|in|break|continue|max|min|state|None|True|False)\b/g, '<span class="kw">$1</span>')
      .replace(/\b(MIN_PER_EPOCH_CHURN_LIMIT_ELECTRA|MAX_PER_EPOCH_ACTIVATION_EXIT_CHURN_LIMIT|CHURN_LIMIT_QUOTIENT|EFFECTIVE_BALANCE_INCREMENT|MIN_ACTIVATION_BALANCE|MAX_EFFECTIVE_BALANCE_ELECTRA|MAX_PENDING_DEPOSITS_PER_EPOCH|MAX_PENDING_PARTIALS_PER_WITHDRAWALS_SWEEP|MIN_VALIDATOR_WITHDRAWABILITY_DELAY|MAX_SEED_LOOKAHEAD|FAR_FUTURE_EPOCH|SLOTS_PER_EPOCH|GENESIS_SLOT)\b/g, '<span class="const">$1</span>');
  }

  // Render a single trace step into a DOM card.
  function renderStep(step, idx) {
    const card = document.createElement("article");
    card.className = "step-card";
    card.dataset.stepNum = String(idx + 1).padStart(2, "0");
    card.dataset.stepId = step.id;
    card.dataset.provenance = step.provenance;

    const headProvenance = step.provenance === "live"
      ? `<span class="live-flag"><span class="live-dot"></span>LIVE</span>`
      : step.provenance.startsWith("derived")
        ? `<span class="derived-flag">${step.provenance === "derived-approx" ? "DERIVED · APPROX" : "DERIVED"}</span>`
        : "";

    const citation = step.spec_lines && (step.spec_lines[0] || step.spec_lines[1])
      ? `<span class="cite" title="Click to copy citation">${escapeHtml(step.spec_file)}:${step.spec_lines[0]}–${step.spec_lines[1]}</span>`
      : `<span class="cite">${escapeHtml(step.spec_file || "")}</span>`;

    // Function name — keep the actual identifier but italicise the verb-noun when possible.
    const fnDisplay = escapeHtml(step.function);

    card.innerHTML = `
      <header class="step-head">
        <div class="step-fn">${fnDisplay}</div>
        <div class="step-fn-meta">
          ${headProvenance}
          ${citation}
        </div>
      </header>
      ${step.spec_excerpt ? `<pre class="spec-block">${tintSpec(step.spec_excerpt)}</pre>` : ""}
      ${step.substituted ? `<pre class="subst-block">${renderSubstituted(step.substituted, step)}</pre>` : ""}
      ${renderBranches(step.branches)}
      ${renderEpochStrip(step.intermediate)}
      ${renderNotes(filterNotes(step.notes, step.provenance))}
    `;

    // Wire citation copy
    const cite = card.querySelector(".cite");
    if (cite) {
      cite.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(`${step.spec_file}:${step.spec_lines[0]}-${step.spec_lines[1]}`);
          cite.textContent = "copied";
          setTimeout(() => {
            cite.textContent = `${step.spec_file}:${step.spec_lines[0]}–${step.spec_lines[1]}`;
          }, 900);
        } catch (_) { /* ignore */ }
      });
    }

    return card;
  }

  // Wrap numbers in the substitution text with em.live so they pulse on update.
  function renderSubstituted(s, step) {
    let html = escapeHtml(s);
    // Pulse-wrap numbers (with optional comma/decimal, optional ETH suffix).
    html = html.replace(/(-?\d[\d,]*(\.\d+)?)(?=\s*(ETH|h|min|d|days|epochs|epoch|months|s|sec|seconds)?)/g, (m) => {
      return `<em class="live">${m}</em>`;
    });
    return html;
  }

  function renderBranches(branches) {
    if (!branches || branches.length === 0) return "";
    return `<div class="branch-list">${branches.map(b => `<span class="branch-pill">${escapeHtml(b)}</span>`).join("")}</div>`;
  }

  function renderEpochStrip(intermediate) {
    if (!intermediate || !intermediate.first_n_epochs || !intermediate.first_n_epochs.length) return "";
    const tiles = intermediate.first_n_epochs.map(e => `
      <div class="epoch-tile">
        <div class="ep-label">epoch ${fmtInt(e.epoch)}</div>
        <div class="ep-value">${e.processed_count} deposits</div>
        <div class="ep-eth">${e.processed_eth} ETH</div>
        <div class="ep-foot">${e.stop_reason || "—"} · carry ${e.carry_dbc_eth} ETH</div>
      </div>
    `).join("");
    return `<div class="epoch-strip">${tiles}</div>`;
  }

  function renderResultRow(result, stepId) {
    if (!result || !Object.keys(result).length) return "";
    const items = Object.entries(result).map(([k, v]) => {
      const isHero = k === "epochs_to_drain" || k === "drain_days" || k === "exit_epoch" || k === "consolidation_epoch" || k === "ae_churn_eth" || k === "balance_churn_eth";
      const valClass = isHero ? "result-val" : "result-val dim";
      const display = formatResultValue(k, v);
      return `<span class="result-key">${escapeHtml(k)}</span><span class="${valClass}" data-result-key="${escapeHtml(stepId)}.${escapeHtml(k)}">${display}</span>`;
    }).join('<span class="arrow">·</span>');
    return `<div class="step-result-row"><span class="arrow">→</span>${items}</div>`;
  }

  function formatResultValue(key, value) {
    if (value === null || value === undefined) return "—";
    if (typeof value === "boolean") return value ? "true" : "false";
    if (typeof value === "number") {
      if (key.endsWith("_eth") || key.includes("balance") && key.includes("eth")) {
        return `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} ETH`;
      }
      if (key.endsWith("_days") || key === "drain_days") {
        return `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} d`;
      }
      if (key.endsWith("_seconds")) {
        return `${(value / 3600).toLocaleString(undefined, { maximumFractionDigits: 2 })} h`;
      }
      if (key === "epochs_to_drain" || key.endsWith("_epochs") || key === "exit_epoch" || key === "withdrawable_epoch" || key === "consolidation_epoch" || key === "earliest_exit_epoch" || key === "earliest_consolidation_epoch" || key === "floor_epoch" || key === "current_epoch" || key === "head_slot" || key === "finalized_epoch" || key === "process_epoch" || key === "last_deposit_lands_in_epoch" || key === "rate_per_epoch" || key === "queue_length" || key === "count" || key === "finalized_count" || key === "unfinalized_count" || key === "activation_tail_epochs") {
        return value.toLocaleString();
      }
      return Number.isInteger(value) ? value.toLocaleString() : value.toLocaleString(undefined, { maximumFractionDigits: 4 });
    }
    return String(value);
  }

  function renderNotes(notes) {
    if (!notes || !notes.length) return "";
    return `<div class="step-notes">${notes.map(n => `<p>${escapeHtml(n)}</p>`).join("")}</div>`;
  }

  // Drop notes that just restate what the LIVE/DERIVED chip already conveys.
  function filterNotes(notes, provenance) {
    if (!notes || !notes.length) return notes;
    const isProvenanceRestatement = (n) => {
      const lower = n.toLowerCase();
      // "x and y are derived (not exposed by ...)" / "not exposed by standard beacon rest"
      if (provenance === "derived" || provenance === "derived-approx") {
        if (lower.includes("not exposed") && lower.includes("rest")) return true;
        if (lower.includes("re-emulated") || lower.includes("re-derived")) return true;
        if (lower.startsWith("approximation.") && lower.includes("recovery requires")) return true;
      }
      return false;
    };
    return notes.filter(n => !isProvenanceRestatement(n));
  }

  function renderFinalHeadline(kind, summary, input) {
    const el = $("#method-final-headline");
    if (!el || !summary) { if (el) el.hidden = true; return; }
    el.hidden = false;
    let label = "", headline = "", deck = "";

    if (kind === "entry-queue") {
      const drainSeconds = summary.drain_seconds ?? (summary.epochs_to_drain * 384);
      const tailSeconds = (summary.activation_tail_epochs ?? 8) * 384;
      const totalSeconds = drainSeconds + tailSeconds;
      label = "— Entry queue · final answer";
      headline = `New ${input ?? 32} ETH deposit waits <em>~${fmtDaysHours(totalSeconds)}</em>.`;
      deck = `Lands in epoch ${fmtInt(summary.last_deposit_lands_in_epoch)} · ${fmtInt(summary.epochs_to_drain)} epochs to drain + ${summary.activation_tail_epochs}-epoch activation tail.`;
    } else if (kind === "exit-queue") {
      label = "— Exit queue · final answer";
      headline = `Exit lands in epoch <em>${fmtInt(summary.exit_epoch)}</em> · <em>~${fmtDaysHours(summary.wait_to_exit_seconds)}</em>.`;
      deck = `Withdrawable at epoch ${fmtInt(summary.withdrawable_epoch)} · ~${fmtDaysHours(summary.wait_to_withdrawable_seconds)} from now.`;
    } else if (kind === "consolidation") {
      if (summary.stalled) {
        label = "— Consolidation queue · stalled";
        headline = `Consolidation churn is <em>zero</em>.`;
        deck = "total_active_balance is at or below the activation/exit cap — no budget left for consolidations.";
      } else {
        label = "— Consolidation · final answer";
        headline = `Consolidation lands in epoch <em>${fmtInt(summary.consolidation_epoch)}</em> · <em>~${fmtDaysHours(summary.wait_to_consolidation_seconds)}</em>.`;
        deck = `Withdrawable at epoch ${fmtInt(summary.withdrawable_epoch)} · ~${fmtDaysHours(summary.wait_to_withdrawable_seconds)} from now.`;
      }
    } else if (kind === "partial-withdrawal") {
      label = "— Partial withdrawal · final answer";
      headline = `Processes in epoch <em>${fmtInt(summary.process_epoch)}</em> · <em>~${fmtDaysHours(summary.wait_seconds)}</em>.`;
      deck = `Position ${fmtInt(summary.wait_epochs * (summary.rate_per_epoch ?? 256))} in the sweep queue · rate-limited by the withdrawal sweep, not churn.`;
    }

    el.innerHTML = `
      <span class="label">${escapeHtml(label)}</span>
      <span class="headline">${headline}</span>
      ${deck ? `<span class="deck">${escapeHtml(deck)}</span>` : ""}
    `;
  }

  async function load() {
    const params = new URLSearchParams();
    const inputCfg = INPUT_BY_KIND[state.kind];
    if (inputCfg && state.input != null) {
      const key = state.kind === "partial-withdrawal" ? "amount_eth" : "balance_eth";
      params.set(key, String(state.input));
    }
    const url = `/methodology/${state.kind}` + (params.toString() ? `?${params}` : "");
    const wf = $("#waterfall");
    const stamp = $("#method-stamp");
    const refreshBtn = $("#method-refresh");
    if (!wf) return;
    // Show a low-key loading state without spinners.
    wf.style.opacity = "0.4";
    if (refreshBtn) refreshBtn.disabled = true;
    if (stamp) stamp.textContent = "Refreshing…";

    try {
      const data = await api(url);
      wf.innerHTML = "";
      data.trace.forEach((step, i) => wf.appendChild(renderStep(step, i)));
      pulseChangedValues(data);
      renderFinalHeadline(state.kind, data.summary, state.input);
      lifecycle.update(state.kind, data.summary);
      wf.style.opacity = "1";
      state.lastRefreshAt = new Date();
      updateStamp();
    } catch (err) {
      wf.innerHTML = `<div class="step-card" style="border-color: var(--warn);">
        <header class="step-head"><div class="step-fn" style="color: var(--warn);">Error</div></header>
        <pre class="spec-block">${escapeHtml(err.message)}</pre>
      </div>`;
      wf.style.opacity = "1";
      if (stamp) stamp.textContent = "Refresh failed";
    } finally {
      if (refreshBtn) refreshBtn.disabled = false;
    }
  }

  function updateStamp() {
    const stamp = $("#method-stamp");
    if (!stamp) return;
    stamp.textContent = state.lastRefreshAt
      ? `Refreshed ${fmtAgo(state.lastRefreshAt)}`
      : "—";
  }

  function pulseChangedValues(data) {
    // Collect every numeric result in this trace, keyed by stepId.resultKey.
    const next = {};
    for (const step of data.trace) {
      for (const [k, v] of Object.entries(step.result || {})) {
        if (typeof v === "number" || typeof v === "string") {
          next[`${step.id}.${k}`] = v;
        }
      }
    }
    // Compare to last; if changed, animate the corresponding element.
    for (const [k, v] of Object.entries(next)) {
      const prev = state.lastValues[k];
      if (prev !== undefined && prev !== v) {
        // Pulse every em.live inside the matching result element + the result-val itself.
        const sel = `[data-result-key="${k.replace(/"/g, '\\"')}"]`;
        $$(sel).forEach(el => {
          el.classList.remove("value-pulse");
          // restart by forcing reflow
          void el.offsetWidth;
          el.classList.add("value-pulse");
        });
      }
    }
    state.lastValues = next;
  }

  function setKind(kind) {
    state.kind = kind;
    const cfg = INPUT_BY_KIND[kind];
    const row = $("#method-input-row");
    const input = $("#method-input");
    if (cfg) {
      row.hidden = false;
      const suffix = row.querySelector(".suffix");
      suffix.textContent = cfg.unit;
      if (state.input == null) {
        state.input = cfg.default;
        input.value = String(cfg.default);
      }
    } else {
      row.hidden = true;
    }
    $$(".methodology-controls .seg-switch button").forEach(b => {
      b.classList.toggle("active", b.dataset.method === kind);
    });
    load();
  }

  function wire() {
    if (state.initialised) return;
    state.initialised = true;
    $$(".methodology-controls .seg-switch button").forEach(b => {
      b.addEventListener("click", () => setKind(b.dataset.method));
    });
    const input = $("#method-input");
    if (input) {
      let debounce;
      input.addEventListener("input", () => {
        clearTimeout(debounce);
        debounce = setTimeout(() => {
          const v = parseFloat(input.value);
          if (!isNaN(v) && v > 0) {
            state.input = v;
            load();
          }
        }, 240);
      });
    }
    const refreshBtn = $("#method-refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", () => load());
    // Tick the "refreshed Xm ago" stamp every 15s without re-fetching.
    if (!state.stampTimer) {
      state.stampTimer = setInterval(updateStamp, 15_000);
    }
  }

  function onTabActivate() {
    wire();
    // Load once on tab entry. From here on the user is in control via the Refresh button.
    load();
  }

  return { onTabActivate, setKind, load };
})();

/* ---------- Main data load + refresh ---------- */
async function loadCore() {
  const [eq, ch, pw, enq, net] = await Promise.all([
    api("/exit-queue"),
    api("/churn"),
    api("/pending-partial-withdrawals"),
    api("/entry-queue").catch(() => null),
    api("/network/stats").catch(() => null),
  ]);
  state.exitQueue = eq;
  state.churn = ch;
  state.pendingWithdrawals = pw;
  state.entryQueue = enq;
  state.network = net;
  state.lastUpdated = new Date();
  state.error = null;
}
function renderAll() {
  renderContext();
  renderNetworkStats();
  // renderExitQueueStats / renderEntryQueueStats also render their respective stats cards
  renderEntryQueueStats();
  renderExitQueueStats();
  renderPendingWithdrawals();
}

/* ---------- Network stats card (4 cells above the queues) ---------- */
function renderNetworkStats() {
  const root = $("#network-stats");
  if (!root) return;
  const n = state.network;
  if (!n) { root.innerHTML = _loadingCells(4); return; }

  // Helper denominators — rough magnitudes for the mini bars
  const valsPct  = Math.min(100, Math.round((n.active_validators / 1_500_000) * 100));
  const stakePct = Math.min(100, Math.round((n.total_stake_eth / 40_000_000) * 100));
  const compPct  = Math.min(100, Math.round((n.compounding_share || 0) * 100));
  const pcCount  = n.pending_consolidations ?? 0;
  const pcTargets = n.pending_consolidation_targets ?? 0;
  // Bar scales 0 → "lots happening" at 256 consolidations (cons_churn cap implication).
  const pcPct = Math.min(100, Math.round((pcCount / 256) * 100));
  const pcSub = pcCount === 0
    ? "queue empty"
    : `${fmtInt(pcTargets)} target validator${pcTargets === 1 ? "" : "s"}`;
  const pcSubCls = pcCount === 0 ? "pos" : "";

  const stakeM = n.total_stake_eth >= 1_000_000
    ? `${(n.total_stake_eth / 1_000_000).toFixed(2)}M`
    : fmtInt(Math.round(n.total_stake_eth));

  root.innerHTML = `
    <div class="cell">
      <span class="k">Active validators</span>
      <span class="v">${fmtInt(n.active_validators)}</span>
      <span class="sub">active_ongoing + exiting + slashed</span>
      <span class="bar"><i style="width: ${valsPct}%;"></i></span>
    </div>
    <div class="cell">
      <span class="k">Total stake</span>
      <span class="v">${stakeM}</span>
      <span class="sub">ETH staked network-wide</span>
      <span class="bar"><i style="width: ${stakePct}%;"></i></span>
    </div>
    <div class="cell">
      <span class="k">Pending consolidations</span>
      <span class="v">${fmtInt(pcCount)}</span>
      <span class="sub"><span class="${pcSubCls}">${escapeHtml(pcSub)}</span></span>
      <span class="bar"><i class="peer" style="width: ${pcPct}%;"></i></span>
    </div>
    <div class="cell">
      <span class="k">Compounding share</span>
      <span class="v">${compPct}%</span>
      <span class="sub">${fmtInt(n.compounding_count)} on 0x02 creds</span>
      <span class="bar"><i class="peer" style="width: ${compPct}%;"></i></span>
    </div>
  `;
}

/* ---------- History (daily time-series since Pectra) ---------- */

// One chart per scalar metric the dashboard shows live. `val` pulls a number
// from a snapshot row; `fmt` renders the latest-value hero. Accent follows the
// locked chip convention: peer-purple for compounding/consolidation, brand-green
// for everything else.
const HISTORY_CHARTS = [
  { title: "Active validators", accent: "brand",
    val: s => s.active_validators, fmt: v => fmtInt(Math.round(v)) },
  { title: "Total stake", accent: "brand",
    val: s => s.total_stake_gwei / GWEI, fmt: v => _fmtEthCompact(v) + " ETH" },
  { title: "Compounding share", accent: "peer",
    val: s => (s.compounding_share || 0) * 100, fmt: v => v.toFixed(2) + "%" },
  { title: "Compounding validators", accent: "peer",
    val: s => s.compounding_count, fmt: v => fmtInt(Math.round(v)) },
  { title: "Pending consolidations", accent: "peer",
    val: s => s.pending_consolidations, fmt: v => fmtInt(Math.round(v)) },
  { title: "Churn limit", accent: "brand",
    val: s => s.churn_limit_gwei / GWEI, fmt: v => fmtInt(Math.round(v)) + " ETH/epoch" },
  { title: "Exit queue wait", accent: "brand",
    val: s => s.exit_wait_hours, fmt: v => fmtHours(v) },
  { title: "Exiting validators", accent: "brand",
    val: s => s.exit_count, fmt: v => fmtInt(Math.round(v)) },
  { title: "Entry queue drain", accent: "brand",
    val: s => s.entry_drain_days, fmt: v => fmtDaysHours(v * 86400) },
  { title: "Pending deposits", accent: "brand",
    val: s => s.entry_pending_count, fmt: v => fmtInt(Math.round(v)) },
  { title: "Pending partials", accent: "brand",
    val: s => s.partial_count, fmt: v => fmtInt(Math.round(v)) },
];

function _fmtEthCompact(eth) {
  if (eth >= 1_000_000) return (eth / 1_000_000).toFixed(2) + "M";
  if (eth >= 1_000) return (eth / 1_000).toFixed(1) + "k";
  return fmtInt(Math.round(eth));
}
function _fmtShortDate(iso) {
  const d = new Date(iso + "T00:00:00Z");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

// Hand-rolled SVG line+area chart, matching the bespoke-SVG idiom used by the
// queue gauges. Colour comes from CSS tokens so dark mode just works.
function _lineChartSVG(points, accent) {
  const W = 320, H = 120, padL = 8, padR = 8, padT = 12, padB = 18;
  const color = accent === "peer" ? "var(--peer)" : "var(--primary)";
  const baseY = H - padB;
  const n = points.length;

  // X is positioned by actual date (not index) so any gap in the daily series
  // shows as a gap, not a misleading single step.
  const ts = points.map(p => new Date(p.date + "T00:00:00Z").getTime());
  let tmin = Math.min(...ts), tmax = Math.max(...ts);
  const x = i => tmax === tmin ? W / 2 : padL + ((ts[i] - tmin) / (tmax - tmin)) * (W - padL - padR);

  const vals = points.map(p => p.value);
  let min = Math.min(...vals), max = Math.max(...vals);
  if (min === max) { min -= 1; max += 1; } // flat series → give the line air
  const y = v => baseY - ((v - min) / (max - min)) * (baseY - padT);

  const dots = points.map((p, i) =>
    `<circle cx="${x(i).toFixed(1)}" cy="${y(p.value).toFixed(1)}" r="${i === n - 1 ? 3 : 1.8}"
       fill="${color}" ${i === n - 1 ? '' : 'fill-opacity="0.55"'} />`).join("");

  if (n === 1) {
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="hc-svg">${dots}</svg>`;
  }

  const line = points.map((p, i) => `${i ? "L" : "M"} ${x(i).toFixed(1)} ${y(p.value).toFixed(1)}`).join(" ");
  const area = `${line} L ${x(n - 1).toFixed(1)} ${baseY} L ${x(0).toFixed(1)} ${baseY} Z`;
  return `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="hc-svg">
      <path d="${area}" fill="${color}" fill-opacity="0.09" stroke="none" />
      <path d="${line}" fill="none" stroke="${color}" stroke-width="2"
            stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke" />
      ${dots}
    </svg>`;
}

function _historyChartHTML(def, rows) {
  const points = rows
    .map(r => ({ date: r.date, value: def.val(r) }))
    .filter(p => p.value != null && isFinite(p.value));
  if (!points.length) return "";
  const latest = points[points.length - 1].value;
  return `
    <div class="history-card accent-${def.accent}">
      <span class="kicker">— ${escapeHtml(def.title)}</span>
      <span class="hc-hero">${escapeHtml(def.fmt(latest))}</span>
      <div class="hc-chart">${_lineChartSVG(points, def.accent)}</div>
      <div class="hc-foot">
        <span>${escapeHtml(_fmtShortDate(points[0].date))}</span>
        <span>${escapeHtml(_fmtShortDate(points[points.length - 1].date))}</span>
      </div>
    </div>`;
}

async function loadHistory() {
  const root = $("#history-charts");
  const err = $("#history-error");
  if (root && !state.history) {
    root.innerHTML = `<div class="history-empty">Loading series…</div>`;
  }
  try {
    state.history = await api("/history/daily");
    if (err) err.hidden = true;
    renderHistory();
  } catch (e) {
    if (err) { err.hidden = false; err.textContent = `Could not load history · ${e.message || e}`; }
  }
}

function renderHistory() {
  const root = $("#history-charts");
  if (!root) return;
  const all = (state.history && state.history.snapshots) || [];
  if (!all.length) {
    root.innerHTML = `<div class="history-empty">No snapshots yet — run the backfill collector to populate the series.</div>`;
    return;
  }
  let rows = all;
  if (state.historyRange !== "all") {
    rows = all.slice(-parseInt(state.historyRange, 10));
  }
  root.innerHTML = HISTORY_CHARTS.map(c => _historyChartHTML(c, rows)).join("");
}

function wireHistory() {
  const bar = $("#history-range");
  if (!bar) return;
  bar.addEventListener("click", e => {
    const a = e.target.closest("a[data-range]");
    if (!a) return;
    e.preventDefault();
    state.historyRange = a.dataset.range;
    $$("#history-range a").forEach(x => x.classList.toggle("active", x === a));
    renderHistory();
  });
}

/* ---------- Boot ---------- */
async function boot() {
  initTheme();
  wireTheme();
  wireTabs();          // BEFORE await — so nav works even if data load fails
  wirePredictor();
  wireHomeLookup();
  wireValidators();
  wirePwTable();
  wireHistory();

  try {
    await loadCore();
    renderAll();
    hideBootOverlay();
  } catch (err) {
    console.error("boot failed", err);
    showBootError(err.message || String(err));
  }

  // Refresh every minute
  setInterval(async () => {
    try {
      await loadCore();
      renderAll();
    } catch (err) {
      state.error = err.message || String(err);
      renderContext();
    }
  }, 60_000);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
