/* AllDebrid-Client — extracted from index.html */

const API = '/api';
let currentFilter = '';
let currentTorrentSearch = '';
let torrentPage = 1;
let torrentPageSize = 25;
let torrentTotal = 0;
let settingsData = {};
let flexgetAvailableTasks = [];
let flexgetTaskSchedules = [];
let aria2DownloadsTimer = null;

function renderTopbarActions() {
  const el = document.getElementById('topbar-actions');
  if (!el) return;
  const paused = !!settingsData.paused;
  el.innerHTML = `
    <button class="btn ${paused ? 'btn-primary' : 'btn-ghost'}" onclick="${paused ? 'resumeProcessing()' : 'pauseProcessing()'}">
      ${paused ? 'Resume' : 'Pause'}
    </button>
  `;
}

// ── Nav ────────────────────────────────────────────────────────────────────
function nav(el) {
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  el.classList.add('active');
  const v = el.dataset.view;
  document.querySelectorAll('.view').forEach(x => x.classList.remove('active'));
  const activeView = document.getElementById('view-' + v);
  if (!activeView) { console.error('nav: view not found:', v); return; }
  activeView.classList.add('active');

  const titles = {
    dashboard:'Dashboard',
    torrents:'Torrents',
    events:'Event Log',
    stats:'Statistics',
    changelog:'Changelog',
    github:'GitHub',
    coffee:'Coffee',
    settings:'Settings',
    help:'Help'
  };
  document.getElementById('page-title').textContent = titles[v] || v;
  if (v === 'dashboard') { loadStats(); loadRecent(); }
  if (v === 'torrents')  loadTorrents();
  if (v === 'events')    loadEvents();
  if (v === 'stats')     loadDetailedStats();
  if (v === 'changelog') loadChangelog();
  if (v === 'settings')  loadSettings();
  if (v === 'search')    initSearchView();
  if (v === 'aria2queue') loadAria2QueueView();
  if (v === 'help') switchHelpTab(document.querySelector('#help-tabs .stab'));
  closeSidebar();
}

// ── API ────────────────────────────────────────────────────────────────────
async function api(method, path, body, timeoutMs) {
  const opts = {method, headers: {'Content-Type':'application/json'}};
  if (body) opts.body = JSON.stringify(body);
  const ms = timeoutMs || 8000; // default 8s; callers can pass longer for slow operations
  const controller = new AbortController();
  const tid = setTimeout(() => controller.abort(), ms);
  opts.signal = controller.signal;
  try {
    const r = await fetch(API + path, opts);
    clearTimeout(tid);
    const data = await r.json().catch(() => ({detail: r.statusText}));
    if (!r.ok) throw new Error(data.detail || r.statusText);
    return data;
  } catch(e) {
    clearTimeout(tid);
    if (e.name === 'AbortError') throw new Error('Request timed out after ' + Math.round(ms/1000) + 's');
    throw e;
  }
}

// ── Toast ──────────────────────────────────────────────────────────────────
function esc(s) {
  // Escape HTML special chars to prevent XSS when inserting user-controlled
  // content (torrent names, filenames, labels) into innerHTML.
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function toast(msg, type = 'info') {
  const icons = {success:'✅',error:'❌',warn:'⚠️',info:'ℹ️'};
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<span>${icons[type]||'·'}</span><span>${msg}</span>`;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => el.style.opacity = '0', 3000);
  setTimeout(() => el.remove(), 3400);
}


function syncMaxDlFields(val) {
  // Keep both max_concurrent_downloads and aria2_max_active_downloads in sync.
  // They represent the same setting — how many files download simultaneously.
  var n = parseInt(val) || 3;
  var a = document.getElementById('s-max_concurrent_downloads');
  var b = document.getElementById('s-aria2_max_active_downloads');
  if (a && a !== document.activeElement) a.value = n;
  if (b && b !== document.activeElement) b.value = n;
}

function toggleSymlinkSettings(val) {
  var el = document.getElementById('symlink-settings');
  if (el) el.style.display = (val === 'symlink') ? 'block' : 'none';
}

// ── Format ─────────────────────────────────────────────────────────────────
function fmtSize(b) {
  if (!b) return '—';
  const u = ['B','KB','MB','GB','TB']; let i = 0;
  while (b >= 1024 && i < u.length-1) {b/=1024; i++;}
  return b.toFixed(1)+' '+u[i];
}
function fmtSpeed(bps) {
  return bps > 0 ? fmtSize(bps) + '/s' : '—';
}
function fmtDate(d) {
  if (!d) return '—';
  const x = new Date(d);
  return x.toLocaleDateString('de-DE',{day:'2-digit',month:'2-digit'})+' '+
         x.toLocaleTimeString('de-DE',{hour:'2-digit',minute:'2-digit'});
}
function pct(part, total) {
  if (!total) return 0;
  return Math.round((part / total) * 100);
}
function renderKvMap(arr, formatter) {
  // arr is an array of {status/level, count} objects from the API
  if (!arr || !arr.length) return '<div class="empty">No data available.</div>';
  const entries = Array.isArray(arr)
    ? arr.map(item => {
        const key = item.status ?? item.level ?? item.source ?? Object.keys(item).find(k => k !== 'count') ?? '?';
        return [key, item];
      })
    : Object.entries(arr);
  return `<div class="kv-list">${entries.map(([key, value]) => `
    <div class="kv-row">
      <span>${key}</span>
      <strong>${formatter ? formatter(value, key) : (value && typeof value === 'object' ? value.count ?? '—' : value)}</strong>
    </div>
  `).join('')}</div>`;
}
function badge(s) {
  const m = {pending:'⏳ Pending',uploading:'⬆ Uploading',processing:'⚙ Processing',
    queued:'🕓 Queued',paused:'⏸ Paused',downloading:'⬇ Downloading',ready:'✓ Ready',completed:'✅ Done',
    error:'❌ Error',deleted:'🗑 Deleted',imported:'📋 Imported',partial:'⚠ Partial'};
  return `<span class="badge badge-${s}">${m[s]||s}</span>`;
}
function progress(pct, status) {
  const done   = status === 'completed';
  const active = status === 'downloading';
  let pctVal = done ? 100 : Math.min(Math.max(pct || 0, 0), 100);
  // Show a thin "in progress" stripe when downloading but no percentage yet
  const showStripe = active && pctVal === 0;
  const fillStyle = showStripe
    ? 'width:100%;opacity:.35;background:repeating-linear-gradient(90deg,var(--accent) 0,var(--accent) 8px,transparent 8px,transparent 16px)'
    : `width:${pctVal}%`;
  const cls = done ? 'done' : '';
  const label = done ? '100%' : showStripe ? '…' : `${pctVal.toFixed(0)}%`;
  return `<div class="prog"><div class="prog-fill ${cls}" style="${fillStyle}"></div></div>
          <span class="prog-pct">${label}</span>`;
}

// ── Status Bar ─────────────────────────────────────────────────────────────

function getAria2ngUrl(aria2Url) {
  // Derive aria2ng URL from aria2 JSON-RPC URL.
  // Example: http://192.168.1.100:6800/jsonrpc → http://192.168.1.100:6880/
  if (!aria2Url) return '';
  try {
    const u = new URL(aria2Url);
    u.port = '6880';
    u.pathname = '/';
    u.search = '';
    return u.toString();
  } catch(e) {
    return '';
  }
}

function updateAria2ngLink() {
  const aria2Url = (settingsData || {}).aria2_url || '';
  const row  = document.getElementById('aria2ng-row');
  const link = document.getElementById('aria2ng-link');
  if (!row || !link) return;
  if (aria2Url) {
    link.href = getAria2ngUrl(aria2Url) || '#';
    row.style.display = 'flex';
  } else {
    row.style.display = 'none';
  }
}

async function checkConnections() {
  // AllDebrid dot is already set by loadStats() — skip duplicate /stats call
  const cfg = settingsData || {};

  // aria2 check — retry once if first attempt fails
  if (cfg.aria2_url || cfg.aria2_mode === 'builtin') {
    let aria2Ok = false;
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        const result = await api('POST', '/settings/test-aria2');
        setDot('aria2', 'ok', `aria2: ${result.version||'online'}`);
        aria2Ok = true;
        break;
      } catch {
        if (attempt < 3) {
          await new Promise(r => setTimeout(r, attempt * 800));
        } else {
          setDot('aria2', 'error', 'aria2: offline');
        }
      }
    }
  } else {
    setDot('aria2', 'warn', 'aria2: not configured');
  }
  updateAria2ngLink();

  if (cfg.jackett_enabled && cfg.jackett_url && cfg.jackett_api_key) {
    try {
      const result = await api('POST', '/settings/test-jackett');
      setDot('jackett', 'ok', `Jackett: ${result.version || 'online'}`);
    } catch(e) {
      setDot('jackett', 'error', `Jackett: ${e.message || 'error'}`);
    }
  } else if (cfg.jackett_enabled) {
    setDot('jackett', 'warn', 'Jackett: incomplete');
  } else {
    setDot('jackett', 'warn', 'Jackett: disabled');
  }
}


async function checkPremiumStatus() {
  try {
    const cfg = settingsData;
    if (!cfg || !cfg.alldebrid_api_key) return;
    const r = await api('POST', '/settings/test-alldebrid');
    _updatePremiumLabel(r);
    setDot('api', 'ok', `AllDebrid: ${r.username||'online'}`);
  } catch { /* silent — dot already set by checkConnections */ }
}

function setDot(id, state, label) {
  const d = document.getElementById('dot-'+id);
  const l = document.getElementById('lbl-'+id);
  if (!d || !l) return;  // element not in DOM yet
  d.className = 'dot' + (state ? ' '+state : '');
  l.textContent = label;
}

function getActiveSettingsTab() {
  return document.querySelector('.stab.active')?.dataset.tab || 'tab-general';
}

async function pauseProcessing() {
  try {
    await api('POST', '/processing/pause');
    settingsData.paused = true;
    renderTopbarActions();
    toast('Processing paused','warn');
    loadStats();
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
}

async function resumeProcessing() {
  try {
    await api('POST', '/processing/resume');
    settingsData.paused = false;
    renderTopbarActions();
    toast('Processing resumed','success');
    loadStats();
    loadRecent();
    if (document.getElementById('view-torrents').classList.contains('active')) loadTorrents();
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
}

// ── Dashboard ──────────────────────────────────────────────────────────────
function fmtDuration(secs) {
  if (!secs || secs <= 0) return '—';
  if (secs < 60) return secs + 's';
  if (secs < 3600) return Math.round(secs/60) + 'm';
  return (secs/3600).toFixed(1) + 'h';
}

async function loadStats() {
  // Retry up to 5 times — server may be slow on first request after container start
  for (let attempt = 1; attempt <= 5; attempt++) {
    try {
      const s = await api('GET', '/stats');
      // ── populate sidebar version ────────────────────────────────────────
      const versionEl = document.getElementById('sidebar-version');
      if (versionEl && s.version) versionEl.textContent = `v${String(s.version).replace(/^v/i, '')}`;
      if (settingsData) settingsData.paused = !!s.paused;
      renderTopbarActions();
      setDot('api', 'ok', 'AllDebrid: online');
      // ── stat cards ─────────────────────────────────────────────────────
      const bs = s.by_status || {};
      const total = Object.values(bs).reduce((a,b)=>a+b,0);
      const completed = s.completed_count ?? bs.completed ?? 0;
      const queuePct = pct(completed, total || 0);
      document.getElementById('s-total').textContent = total;
      document.getElementById('s-completed').textContent = completed;
      document.getElementById('s-active').textContent = s.active_downloads||0;
      document.getElementById('s-processing').textContent = s.paused ? 'Paused' : (bs.processing||0)+(bs.uploading||0);
      const errCount = s.error_count ?? bs.error ?? 0;
      document.getElementById('s-error').textContent = errCount;
      const errCard = document.getElementById('dash-error-card');
      if (errCard) errCard.style.opacity = errCount > 0 ? '1' : '.6';
      document.getElementById('s-size').textContent = fmtSize(s.total_completed_bytes);
      document.getElementById('s-blocked').textContent = `${s.total_blocked_files||0} blocked files`;
      const healthEl = document.getElementById('i-queue-health');
      if (healthEl) {
        healthEl.textContent = `${queuePct}%`;
        healthEl.style.color = queuePct >= 90 ? 'var(--green)' : queuePct >= 70 ? 'var(--accent)' : 'var(--red)';
      }
      document.getElementById('i-queue-copy').textContent = `${s.active_downloads||0} active / ${s.queued_downloads||0} queued`;
      document.getElementById('i-last-day').textContent = s.completed_last_24h||0;
      document.getElementById('i-last-week').textContent = s.completed_last_7d||0;
      document.getElementById('i-success-rate').textContent = s.success_rate_pct != null ? s.success_rate_pct+'%' : '—';
      document.getElementById('i-avg-duration').textContent = fmtDuration(s.avg_download_duration_seconds);
      document.getElementById('i-avg-size').textContent = s.avg_torrent_size_bytes ? fmtSize(s.avg_torrent_size_bytes) : '—';
      const active = s.active_downloads || 0;
      const nb = document.getElementById('nb-active');
      if (nb) { nb.textContent = active; nb.style.display = active > 0 ? '' : 'none'; }
      // Topbar aria2 badge: active download count (if aria2 badge visible)
      updateAria2TopbarBadge({active: s.active_downloads||0});
      // ── DB info + dot ──────────────────────────────────────────────────
      const dbEl = document.getElementById('i-db-type');
      if (dbEl && s.db_type) {
        const dbLabels = {sqlite:'SQLite', postgres:'PostgreSQL', sqlite_fallback:'⚠️ SQLite (fallback)'};
        dbEl.textContent = dbLabels[s.db_type] || s.db_type;
        dbEl.style.color = s.db_type === 'sqlite_fallback' ? 'var(--accent)' : '';
        dbEl.title       = s.db_type === 'sqlite_fallback' ? 'PostgreSQL unreachable — running on SQLite fallback.' : '';
      }
      setDot('db',
        s.db_type === 'sqlite_fallback' ? 'error' : 'ok',
        s.db_type === 'sqlite_fallback' ? 'DB: SQLite (fallback)'
          : s.db_type === 'postgres'    ? 'DB: PostgreSQL'
          : 'DB: SQLite'
      );
      return true; // signal success to caller
    } catch(e) {
      console.warn('loadStats attempt', attempt, 'failed:', e.message);
      if (attempt < 5) {
        await new Promise(r => setTimeout(r, 500 * attempt));
        continue;
      }
      return false;
    }
  }
  return false;
}


function renderTorrentPagination(total, limit, offset) {
  var totalPages = Math.max(1, Math.ceil(total / limit));
  var cur = Math.floor(offset / limit) + 1;
  torrentPage = cur;
  var info = document.getElementById('torrent-page-info');
  var btns = document.getElementById('torrent-page-btns');
  if (!info || !btns) return;
  var from = total === 0 ? 0 : offset + 1;
  var to   = Math.min(offset + limit, total);
  info.textContent = total > 0 ? from + '–' + to + ' of ' + total : 'No results';
  var pages = [];
  if (totalPages <= 7) { for (var i=1;i<=totalPages;i++) pages.push(i); }
  else {
    pages = [1];
    var s = Math.max(2, cur-2), e = Math.min(totalPages-1, cur+2);
    if (s > 2) pages.push('...');
    for (var i=s;i<=e;i++) pages.push(i);
    if (e < totalPages-1) pages.push('...');
    pages.push(totalPages);
  }
  btns.innerHTML =
    '<button class="btn btn-ghost btn-sm"'+(cur<=1?' disabled':'')+' onclick="goToTorrentPage('+(cur-1)+')">&#8249;</button>' +
    pages.map(function(p){ return p==='...'
      ? '<span style="padding:0 4px;color:var(--text3)">…</span>'
      : '<button class="btn '+(p===cur?'btn-primary':'btn-ghost')+' btn-sm" onclick="goToTorrentPage('+p+')">'+p+'</button>';
    }).join('') +
    '<button class="btn btn-ghost btn-sm"'+(cur>=totalPages?' disabled':'')+' onclick="goToTorrentPage('+(cur+1)+')">&#8250;</button>';
}
function goToTorrentPage(p) { torrentPage = Math.max(1,p); loadTorrents(); }
function onPageSizeChange(v) { torrentPageSize=Math.min(Math.max(parseInt(v)||25,15),100); torrentPage=1; loadTorrents(); }

async function checkForUpdate() {
  try {
    const data = await api('GET', '/version/check');
    const badge = document.getElementById('update-badge');
    const badgeV = document.getElementById('update-badge-version');
    if (!badge) return;
    if (data.update_available && data.latest) {
      if (badgeV) badgeV.textContent = 'v' + data.latest;
      badge.style.display = 'flex';
    } else {
      badge.style.display = 'none';
    }
  } catch (_) {}
}


async function loadDetailedStats(period) {
  period = period || (document.querySelector('#stats-period-tabs .ftab.active')||{}).dataset?.period || '24h';

  // Chart-Titel Mapping
  var chartTitles = {
    '1h':  'Completions — last hour',
    '24h': 'Completions — last 24 hours',
    '7d':  'Completions — last 7 days',
    '30d': 'Completions — last 30 days',
    '1y':  'Completions — last year',
    'all': 'All-time completions'
  };
  var chartTitleEl = document.getElementById('chart-title');
  if (chartTitleEl) chartTitleEl.textContent = chartTitles[period] || 'Completions';

  // Period label for subtext
  var periodLabels = {
    '1h':'last hour','24h':'last 24h','7d':'last 7 days',
    '30d':'last 30 days','1y':'last year','all':'all time'
  };
  var pLabel = periodLabels[period] || period;

  try {
    var stats = await api('GET', '/stats/detail?period=' + encodeURIComponent(period));
    var t = stats.totals || {};
    document.getElementById('detail-stat-cards').innerHTML =
      '<div class="metric-card"><div class="metric-label">Torrents</div><div class="metric-value">'+(t.torrent_total||0)+'</div><div class="metric-sub">Added in '+pLabel+'.</div></div>' +
      '<div class="metric-card"><div class="metric-label">Completed Size</div><div class="metric-value">'+fmtSize(t.completed_size||0)+'</div><div class="metric-sub">Completed in '+pLabel+'.</div></div>' +
      '<div class="metric-card"><div class="metric-label">Completed</div><div class="metric-value">'+(t.completed_count||0)+'</div><div class="metric-sub">Finished in '+pLabel+'.</div></div>' +
      '<div class="metric-card"><div class="metric-label">In Progress</div><div class="metric-value">'+(t.partial_total||0)+'</div><div class="metric-sub">Currently downloading or processing.</div></div>' +
      (t.success_rate_pct!=null ? '<div class="metric-card"><div class="metric-label">Success Rate</div><div class="metric-value">'+t.success_rate_pct+'%</div><div class="metric-sub">Completed vs. completed+error.</div></div>' : '');

    document.getElementById('detail-torrent-status').innerHTML = renderKvMap(stats.torrent_status);
    document.getElementById('detail-file-status').innerHTML   = renderKvMap(stats.file_status, function(v){return v.count??v;});
    document.getElementById('detail-event-levels').innerHTML  = renderKvMap(stats.event_levels);
    var srcEl = document.getElementById('detail-sources');
    if (srcEl) {
      var srcs = stats.sources||[];
      srcEl.innerHTML = srcs.length
        ? srcs.map(function(s){ return '<div class="kv-row"><span class="kv-key">'+esc(s.source||'(none)')+'</span><span class="kv-val">'+s.count+'</span></div>'; }).join('')
        : '<div class="empty">No data.</div>';
    }

    // Chart — data already period-filtered from backend
    var daily = stats.daily_completions || [];
    var ctx = document.getElementById('daily-chart');
    if (ctx && typeof Chart !== 'undefined') {
      if (ctx._ci) ctx._ci.destroy();
      var isDark = !document.body.classList.contains('light');
      var gridColor = isDark ? 'rgba(255,255,255,.04)' : 'rgba(0,0,0,.07)';
      var tickColor = isDark ? '#64748b' : '#6b7280';
      ctx._ci = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: daily.map(function(d){ return d.date||''; }),
          datasets: [{
            label: 'Completions', data: daily.map(function(d){ return d.count||0; }),
            backgroundColor: 'rgba(249,115,22,.55)', borderColor: '#f97316',
            borderWidth: 1, borderRadius: 4
          }]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { grid:{color:gridColor}, ticks:{color:tickColor,font:{size:10},maxRotation:45} },
            y: { grid:{color:gridColor}, ticks:{color:tickColor,font:{size:10}}, beginAtZero:true, precision:0 }
          }
        }
      });
    }
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
}

function setStatsPeriod(el) {
  document.querySelectorAll('#stats-period-tabs .ftab').forEach(function(t){t.classList.remove('active');});
  el.classList.add('active');
  loadDetailedStats(el.dataset.period);
}
function fmtSpeed(bps) {
  if (!bps||bps<0) return '0 B/s';
  if (bps<1024) return bps+' B/s';
  if (bps<1048576) return (bps/1024).toFixed(1)+' KB/s';
  if (bps<1073741824) return (bps/1048576).toFixed(1)+' MB/s';
  return (bps/1073741824).toFixed(2)+' GB/s';
}


async function loadRecent() {
  try {
    const {items} = await api('GET', '/torrents?limit=10');
    const tb = document.getElementById('dash-tbody');
    if (!items.length) {
      tb.innerHTML = '<tr><td colspan="5"><div class="empty"><div class="empty-icon">🧲</div>No torrents yet. Add a magnet link to start.</div></td></tr>';
      return;
    }
    // Update activity count
    const countEl = document.getElementById('dash-activity-count');
    if (countEl) countEl.textContent = items.length + ' most recent';
    tb.innerHTML = items.map(t => {
      const pct_val = t.progress != null ? Math.round(t.progress) : 0;
      const is_active = ['downloading','queued'].includes(t.status);
      return `<tr onclick="showDetail(${t.id})" style="cursor:pointer">
        <td>
          <div class="t-name" title="${esc(t.name)||''}">${esc(t.name)||'(unnamed)'}</div>
          ${is_active ? `<div class="dash-row-bar"><div class="dash-row-bar-fill" style="width:${pct_val}%;background:var(--blue)"></div></div>` : ''}
        </td>
        <td>${badge(t.status)}</td>
        <td>${progress(t.progress,t.status)}</td>
        <td class="sz">${fmtSize(t.size_bytes)}</td>
        <td class="sz">${fmtDate(t.created_at)}</td>
      </tr>`;
    }).join('');
  } catch(e) { console.error(e); }
}

async function quickAdd() {
  const v = document.getElementById('q-magnet').value.trim();
  if (!v) return;
  try {
    await api('POST', '/torrents/add-magnet', {magnet: v});
    toast('Magnet added!', 'success');
    document.getElementById('q-magnet').value = '';
    loadStats(); loadRecent();
  } catch(e) { toast(e.message, 'error'); }
}

// ── Torrents ───────────────────────────────────────────────────────────────
function setFilter(el, status) {
  document.querySelectorAll('.ftab').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  currentFilter = status; torrentPage = 1;
  loadTorrents();
}

function onTorrentSearchInput() {
  currentTorrentSearch = (document.getElementById('torrent-search')?.value || '').trim();
  torrentPage = 1; loadTorrents();
}

async function loadTorrents() {
  try {
    const params = new URLSearchParams();
    const _limit = Math.min(Math.max(parseInt(torrentPageSize)||25,15),100);
    const _offset = (torrentPage - 1) * _limit;
    params.set('limit', String(_limit));
    params.set('offset', String(_offset));
    if (currentFilter) params.set('status', currentFilter);
    if (currentTorrentSearch) params.set('search', currentTorrentSearch);
    const {items, total} = await api('GET', '/torrents?'+params.toString());
    torrentTotal = total ?? items.length;
    const tb = document.getElementById('t-tbody');
    const title = document.getElementById('torrent-card-title');
    if (title) title.textContent = `All Torrents (${torrentTotal})`;
    renderTorrentPagination(torrentTotal, _limit, _offset);
    if (!items.length) {
      tb.innerHTML = `<tr><td colspan="8"><div class="empty"><div class="empty-icon">&#129522;</div>${currentTorrentSearch || currentFilter ? 'No torrents match the current filter or search.' : 'No torrents found.'}</div></td></tr>`;
      return;
    }
    tb.innerHTML = items.map(t => `<tr>
      <td onclick="event.stopPropagation()"><input type="checkbox" class="t-chk" data-id="${t.id}" onchange="onCheckboxChange()"/></td>
      <td onclick="showDetail(${t.id})" style="cursor:pointer">
        <div class="t-name">${esc(t.name)||'(unnamed)'}</div>
        <div class="t-hash">${(t.hash||'').substring(0,16)}${t.hash?'…':''}</div>
      </td>
      <td class="sz">
        <div>${t.source||'—'}</div>
        ${t.label?`<span class="lbl-badge">🏷 ${esc(t.label)}</span>`:''}
      </td>
      <td>${badge(t.status)}</td>
      <td>${progress(t.progress,t.status)}</td>
      <td class="sz">${fmtSize(t.size_bytes)}</td>
      <td class="sz">${fmtDate(t.created_at)}</td>
      <td>
        <div class="actions">
          <button class="btn btn-ghost btn-sm" onclick="showDetail(${t.id})">Details</button>
          ${t.status==='downloading' || t.status==='queued' ? `<button class="btn btn-blue btn-sm" onclick="pauseT(${t.id})">⏸</button>` : ''}
          ${t.status==='paused' ? `<button class="btn btn-blue btn-sm" onclick="resumeT(${t.id})">▶</button>` : ''}
          ${t.status==='error'?`<button class="btn btn-blue btn-sm" onclick="retryT(${t.id})">↻</button>`:''}
          <button class="btn btn-danger btn-sm" onclick="deleteT(${t.id},event)">✕</button>
        </div>
      </td>
    </tr>`).join('');
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
}

async function addMagnet() {
  const v = document.getElementById('t-magnet').value.trim();
  if (!v) return;
  try {
    await api('POST','/torrents/add-magnet',{magnet:v});
    toast('Added!','success');
    document.getElementById('t-magnet').value='';
    loadTorrents();
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
}

async function importExisting() {
  try {
    const r = await api('POST','/torrents/import-existing');
    toast(`Imported ${r.imported} magnets from AllDebrid`,'success');
    loadStats(); loadRecent();
    if (document.getElementById('view-torrents').classList.contains('active')) loadTorrents();
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
}

async function recoverAll() {
  try {
    toast('Checking AllDebrid for ready torrents…','info');
    const r = await api('POST','/torrents/recover-all');
    const msg = `Recovery: reset ${r.reset} stuck, checked ${r.checked}, started ${r.started}`;
    toast(msg, r.started > 0 || r.reset > 0 ? 'success' : 'warn');
    loadStats(); loadRecent();
    if (document.getElementById('view-torrents').classList.contains('active')) loadTorrents();
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
}

async function deleteT(id, e) {
  e.stopPropagation();
  if (!confirm('Delete from AllDebrid and remove from list?')) return;
  try {
    await api('DELETE',`/torrents/${id}?from_alldebrid=true`);
    toast('Deleted','success');
    loadTorrents(); loadStats();
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
}

async function retryT(id) {
  try {
    await api('POST',`/torrents/${id}/retry`);
    toast('Queued for retry','success');
    loadTorrents();
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
}

async function pauseT(id) {
  try {
    await api('POST',`/torrents/${id}/pause`);
    toast('aria2 queue paused','warn');
    loadTorrents(); loadStats();
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
}

async function resumeT(id) {
  try {
    await api('POST',`/torrents/${id}/resume`);
    toast('aria2 queue resumed','success');
    loadTorrents(); loadStats();
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
}

// ── Detail Modal ───────────────────────────────────────────────────────────
async function showDetail(id) {
  try {
    const t = await api('GET',`/torrents/${id}`);
    document.getElementById('modal-title').textContent = t.name||'Torrent Details';
    document.getElementById('modal-body').innerHTML = `
      <div class="detail-grid">
        <div><div class="dk">Status</div><div class="dv">${badge(t.status)}</div></div>
        <div><div class="dk">Provider</div><div class="dv">${t.provider_status ? badge(t.provider_status) : '—'}</div></div>
        <div><div class="dk">Progress</div><div class="dv">${(t.progress||0).toFixed(1)}%</div></div>
        <div><div class="dk">Size</div><div class="dv">${fmtSize(t.size_bytes)}</div></div>
        <div><div class="dk">Source</div><div class="dv">${t.source||'—'}</div></div>
        <div><div class="dk">Downloader</div><div class="dv">${t.download_client||'aria2'}</div></div>
        <div><div class="dk">Added</div><div class="dv">${fmtDate(t.created_at)}</div></div>
        <div><div class="dk">Completed</div><div class="dv">${fmtDate(t.completed_at)}</div></div>
        <div style="grid-column:1/-1"><div class="dk">AllDebrid ID</div><div class="dv">${t.alldebrid_id||'—'}</div></div>
        <div style="grid-column:1/-1"><div class="dk">Hash</div><div class="dv" style="font-size:11px">${t.hash||'—'}</div></div>
        ${t.local_path?`<div style="grid-column:1/-1"><div class="dk">Local Path</div><div class="dv" style="font-size:11px">${t.local_path}</div></div>`:''}
        ${t.error_message?`<div style="grid-column:1/-1"><div class="dk">Error</div><div class="dv" style="color:var(--red)">${esc(t.error_message)}</div></div>`:''}
      </div>
      ${t.files&&t.files.length?`
        <div class="sec-label">Files (${t.files.length})</div>
        <div class="card">
          <table>
            <thead><tr><th>Filename</th><th>Size</th><th>Status</th></tr></thead>
            <tbody>${t.files.map(f=>`<tr>
              <td style="font-family:var(--mono);font-size:11px">${esc(f.filename)}
                ${f.blocked?`<span class="badge badge-error" style="font-size:9px;margin-left:6px">BLOCKED: ${esc(f.block_reason)}</span>`:''}
              </td>
              <td class="sz">${fmtSize(f.size_bytes)}</td>
              <td>${badge(f.status)}</td>
            </tr>`).join('')}</tbody>
          </table>
        </div>
      `:''}
      ${t.events&&t.events.length?`
        <div class="sec-label">Events</div>
        ${t.events.map(ev=>`
          <div class="event-item">
            <div class="elevel ${ev.level}"></div>
            <div class="emsg">${esc(ev.message)}</div>
            <div class="etime">${fmtDate(ev.created_at)}</div>
          </div>`).join('')}
      `:''}
    `;
    document.getElementById('overlay').classList.add('open');
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
}

function closeModal(e) {
  if (!e || e.target === document.getElementById('overlay'))
    document.getElementById('overlay').classList.remove('open');
}

function parseFlexgetTaskSchedules(raw) {
  try {
    const parsed = JSON.parse(raw || '[]');
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(item => item && typeof item === 'object')
      .map(item => ({
        task: String(item.task || '').trim(),
        interval_minutes: Math.max(0, parseInt(item.interval_minutes || 0, 10) || 0),
        jitter_seconds: Math.max(0, parseInt(item.jitter_seconds || 0, 10) || 0),
        enabled: item.enabled !== false,
      }))
      .filter(item => item.task);
  } catch (_) {
    return [];
  }
}

function serializeFlexgetTaskSchedules() {
  return JSON.stringify(
    flexgetTaskSchedules
      .map(item => ({
        task: String(item.task || '').trim(),
        interval_minutes: Math.max(0, parseInt(item.interval_minutes || 0, 10) || 0),
        jitter_seconds: Math.max(0, parseInt(item.jitter_seconds || 0, 10) || 0),
        enabled: item.enabled !== false,
      }))
      .filter(item => item.task)
  );
}

function renderFlexgetTaskSchedules() {
  const host = document.getElementById('flexget-schedule-editor');
  if (!host) return;
  if (!flexgetTaskSchedules.length) {
    host.innerHTML = '<div class="form-hint">No scheduled tasks configured yet.</div>';
    return;
  }
  host.innerHTML = flexgetTaskSchedules.map((item, index) => `
    <div style="display:grid;grid-template-columns:2fr 90px 90px 80px auto;gap:8px;align-items:end;margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--border)">
      <div class="form-group" style="margin:0">
        <label class="form-label" style="display:flex;align-items:center;justify-content:space-between">
          <span>Task</span>
          <span style="font-size:10px;font-weight:400;color:var(--text2)">enabled</span>
        </label>
        <div style="display:flex;align-items:center;gap:6px">
          <input class="input" list="flexget-task-options" value="${item.task}" onchange="updateFlexgetTaskSchedule(${index}, 'task', this.value)" style="flex:1" />
          <label style="position:relative;display:inline-block;width:36px;height:20px;flex-shrink:0">
            <input type="checkbox" style="opacity:0;width:0;height:0;position:absolute" ${item.enabled ? 'checked' : ''} onchange="updateFlexgetTaskSchedule(${index}, 'enabled', this.checked)">
            <span style="position:absolute;inset:0;background:${item.enabled?'var(--accent)':'var(--border2)'};border-radius:10px;cursor:pointer;transition:background .2s"></span>
            <span style="position:absolute;left:${item.enabled?'18':'3'}px;top:3px;width:14px;height:14px;background:#fff;border-radius:50%;transition:left .2s;pointer-events:none"></span>
          </label>
        </div>
      </div>
      <div class="form-group" style="margin:0">
        <label class="form-label">Interval (min)</label>
        <input class="input" type="number" min="0" max="720" value="${item.interval_minutes}" onchange="updateFlexgetTaskSchedule(${index}, 'interval_minutes', this.value)" />
      </div>
      <div class="form-group" style="margin:0">
        <label class="form-label">Jitter (sec)</label>
        <input class="input" type="number" min="0" max="3600" value="${item.jitter_seconds}" onchange="updateFlexgetTaskSchedule(${index}, 'jitter_seconds', this.value)" />
      </div>
      <div class="form-group" style="margin:0">
        <label class="form-label">Run</label>
        <button onclick="flexgetRunSingleTask('${item.task.replace(/'/g,"\\'")}', this)" style="width:100%;background:var(--surface2);border:1px solid var(--border);color:var(--accent);padding:5px 0;border-radius:var(--radius-sm);cursor:pointer;font-size:12px;transition:all .15s" onmouseover="this.style.borderColor='var(--accent)'" onmouseout="this.style.borderColor='var(--border)'">▶ Run</button>
      </div>
      <div class="form-group" style="margin:0">
        <label class="form-label" style="visibility:hidden">x</label>
        <button onclick="removeFlexgetTaskSchedule(${index})" style="width:100%;background:transparent;border:1px solid var(--red);color:var(--red);padding:5px 0;border-radius:var(--radius-sm);cursor:pointer;font-size:12px;transition:all .15s" onmouseover="this.style.background='rgba(239,68,68,.1)'" onmouseout="this.style.background='transparent'">✕ Del</button>
      </div>
    </div>
  `).join('');
}

function updateFlexgetTaskSchedule(index, field, value) {
  const current = flexgetTaskSchedules[index];
  if (!current) return;
  if (field === 'enabled') current.enabled = !!value;
  else if (field === 'interval_minutes' || field === 'jitter_seconds') current[field] = Math.max(0, parseInt(value || 0, 10) || 0);
  else current[field] = String(value || '').trim();
}

function addFlexgetTaskSchedule(task = '') {
  flexgetTaskSchedules.push({ task, interval_minutes: 60, jitter_seconds: 0, enabled: true });
  renderFlexgetTaskSchedules();
}

function removeFlexgetTaskSchedule(index) {
  flexgetTaskSchedules.splice(index, 1);
  renderFlexgetTaskSchedules();
}

// ── Theme toggle ─────────────────────────────────────────────────────────────
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('mobile-overlay').classList.toggle('open');
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('mobile-overlay').classList.remove('open');
}

function toggleTheme() {
  const isLight = document.body.classList.toggle('light');
  localStorage.setItem('theme', isLight ? 'light' : 'dark');
  document.getElementById('theme-toggle').textContent = isLight ? '🌙' : '☀️';
}
document.addEventListener('DOMContentLoaded', () => {
  document.addEventListener('click', function(e) {
    var p = document.getElementById('idx-picker');
    if (p && !p.contains(e.target)) idxClose();
  });
  setInterval(function() {
    if (settingsData && (settingsData.aria2_mode||'builtin')==='builtin') {
      loadAria2Runtime().catch(()=>{});
    }
  }, 5000);
  if (localStorage.getItem('theme') === 'light') {
    document.body.classList.add('light');
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = '☀️';
  }
});

// ── Bulk selection ────────────────────────────────────────────────────────────
let _selectedIds = new Set();

function onCheckboxChange() {
  _selectedIds = new Set(
    [...document.querySelectorAll('.t-chk:checked')].map(el => parseInt(el.dataset.id))
  );
  const bar = document.getElementById('bulk-bar');
  const cnt = document.getElementById('bulk-count');
  if (_selectedIds.size > 0) {
    bar.classList.add('visible');
    cnt.textContent = _selectedIds.size + ' selected';
  } else {
    bar.classList.remove('visible');
  }
  const all = document.getElementById('chk-all');
  const total = document.querySelectorAll('.t-chk').length;
  if (all) all.indeterminate = _selectedIds.size > 0 && _selectedIds.size < total;
}

function toggleAllCheckboxes(el) {
  document.querySelectorAll('.t-chk').forEach(c => {
    c.checked = el.checked;
  });
  onCheckboxChange();
}

function clearSelection() {
  _selectedIds.clear();
  document.querySelectorAll('.t-chk').forEach(c => c.checked = false);
  const all = document.getElementById('chk-all');
  if (all) { all.checked = false; all.indeterminate = false; }
  document.getElementById('bulk-bar').classList.remove('visible');
}

async function bulkAction(action) {
  if (!_selectedIds.size) return;
  const ids = [..._selectedIds];
  if (action === 'delete' && !confirm(`Delete ${ids.length} torrents?`)) return;
  try {
    const r = await api('POST', '/torrents/bulk', {ids, action});
    toast(`Done: ${r.ok} ok, ${r.failed} failed`, r.failed ? 'warn' : 'success');
    clearSelection();
    loadTorrents(); loadStats();
  } catch(e) { toast(e.message, 'error'); }
}

// ── Label management ─────────────────────────────────────────────────────────
async function setLabel(id) {
  const label = prompt('Label (leave empty to clear):') ?? null;
  if (label === null) return;
  try {
    await api('PUT', `/torrents/${id}/label`, {label: label.trim(), priority: 0});
    toast('Label updated', 'success');
    loadTorrents();
  } catch(e) { toast(e.message, 'error'); }
}

// ── Events ─────────────────────────────────────────────────────────────────
let _allEvents = [];

async function loadEvents() {
  try {
    _allEvents = await api('GET','/events?limit=500');
    filterEvents();
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
}

function filterEvents() {
  const el = document.getElementById('event-list');
  const q   = (document.getElementById('ev-search')?.value || '').toLowerCase();
  const lvl = document.getElementById('ev-level')?.value || '';
  const evs = _allEvents.filter(ev => {
    if (lvl && ev.level !== lvl) return false;
    if (!q) return true;
    return (ev.message||'').toLowerCase().includes(q) ||
           (ev.torrent_name||'').toLowerCase().includes(q);
  });
  if (!evs.length) { el.innerHTML='<div class="empty">No events match the filter.</div>'; return; }
  el.innerHTML = evs.map(ev=>`
    <div class="event-item">
      <div class="elevel ${ev.level}"></div>
      <div><div class="emsg">${esc(ev.message)}</div>${ev.torrent_name?`<div class="ename">${ev.torrent_name}</div>`:''}</div>
      <div class="etime">${fmtDate(ev.created_at)}</div>
    </div>`).join('');
}

// ── Settings ───────────────────────────────────────────────────────────────

async function loadChangelog() {
  const el = document.getElementById('changelog-content');
  el.innerHTML = '<p style="color:var(--text3)">Loading…</p>';
  try {
    const data = await api('GET', '/changelog');
    const md = data.content || 'No changelog content found.';
    el.innerHTML = renderMarkdown(md);
  } catch(e) {
    el.innerHTML = `<p style="color:#f87171">Failed to load changelog: ${e.message}</p>`;
    toast(e.message, 'error');
  }
}

function renderMarkdown(md) {
  const escape = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  // Inline formatting: **bold**, `code`, [text](url)
  const inline = s => escape(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const lines = md.split('\n');
  let html = '';
  let inList = false;
  let inCode = false;
  for (let raw of lines) {
    const line = raw.trimEnd();
    if (line.startsWith('```')) {
      if (inList) { html += '</ul>'; inList = false; }
      if (inCode) { html += '</code></pre>'; inCode = false; }
      else { html += '<pre><code>'; inCode = true; }
      continue;
    }
    if (inCode) { html += escape(line) + '\n'; continue; }
    if (/^#{3,}\s/.test(line)) {
      if (inList) { html += '</ul>'; inList = false; }
      html += `<h3>${inline(line.replace(/^#+\s*/,''))}</h3>`;
    } else if (/^#{2}\s/.test(line)) {
      if (inList) { html += '</ul>'; inList = false; }
      html += `<h2>${inline(line.replace(/^#+\s*/,''))}</h2>`;
    } else if (/^#\s/.test(line)) {
      if (inList) { html += '</ul>'; inList = false; }
      html += `<h1>${inline(line.replace(/^#+\s*/,''))}</h1>`;
    } else if (/^[-*]\s/.test(line)) {
      if (!inList) { html += '<ul>'; inList = true; }
      html += `<li>${inline(line.replace(/^[-*]\s+/,''))}</li>`;
    } else if (line === '---') {
      if (inList) { html += '</ul>'; inList = false; }
      html += '<hr>';
    } else if (line === '') {
      if (inList) { html += '</ul>'; inList = false; }
    } else {
      if (inList) { html += '</ul>'; inList = false; }
      html += `<p>${inline(line)}</p>`;
    }
  }
  if (inCode) html += '</code></pre>';
  if (inList) html += '</ul>';
  return html;
}

async function loadSettings() {
  try {
    settingsData = await api('GET','/settings');
    renderSettings();
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
  // Activate first tab by default
  switchSettingsTab('tab-general');
  // Avatar preview: show if a custom avatar URL is set
  const avatarUrl = settingsData.discord_avatar_url || '';
  if (avatarUrl && !avatarUrl.includes('github') && !avatarUrl.includes('_DEFAULT')) {
    showAvatarPreview(avatarUrl, 'Custom avatar', 0);
  }
  // Initialer Zustand: Test-PostgreSQL Button und pg-settings Sichtbarkeit
  const dbTypeEl = document.getElementById('s-db_type');
  if (dbTypeEl) {
    const isPg = dbTypeEl.value === 'postgres';
    const pgBtn = document.getElementById('btn-test-postgres');
    if (pgBtn) pgBtn.style.display = isPg ? '' : 'none';
  }
}

function renderSettings() {
  const s = settingsData;
  const aria2BuiltIn = (s.aria2_mode || 'builtin') === 'builtin';
  flexgetTaskSchedules = parseFlexgetTaskSchedules(s.flexget_task_schedules_json);

  // Define tabs
  const tabs = [
    { id:'tab-general',       label:'⚡ General' },
    { id:'tab-download',      label:'⬇️ Download' },
    { id:'tab-extract',       label:'📦 Extract' },
    { id:'tab-notifications', label:'🔔 Notifications' },
    { id:'tab-services',      label:'🔌 Services' },
    { id:'tab-advanced',      label:'🛠️ Advanced' },
  ];
  document.getElementById('settings-tabs').innerHTML = tabs.map((t,i)=>
    `<div class="stab${i===0?' active':''}" data-tab="${t.id}" onclick="switchSettingsTab('${t.id}')">${t.label}</div>`
  ).join('');
  const _sf = document.getElementById('settings-form');
  _sf.innerHTML = '';
  _sf.insertAdjacentHTML('beforeend', `<div class="stab-panel  active" id="tab-general">
      <div class="scard">
        <div class="scard-header">🔑 AllDebrid</div>
      <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Required. Your AllDebrid API key — get it at <a href="https://alldebrid.com/apikeys/" target="_blank" style="color:var(--accent)">alldebrid.com/apikeys</a>.</p>
        <div class="scard-body">
          <div class="form-group">
            <label class="form-label">API Key</label>
            <div class="test-row">
              <input class="input" type="password" id="s-alldebrid_api_key" value="${s.alldebrid_api_key||''}" placeholder="Your AllDebrid API key"/>
              <button class="btn btn-blue btn-sm" onclick="testAD()">Test</button>
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">Agent Name</label>
            <input class="input" id="s-alldebrid_agent" value="${s.alldebrid_agent||'AllDebrid-Client'}"/>
          </div>
        </div>
      </div>

      <div class="scard">
      <div class="scard-header">🔐 Access Control</div>
      <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Optional HTTP Basic Auth. Set both fields to enable; leave either empty to disable. The browser will prompt for credentials on next load.</p>
      <div class="scard-body">
        <div class="form-group">
          <label class="form-label">Username</label>
          <input class="input" id="s-auth_username" value="${s.auth_username||''}" placeholder="Leave empty to disable auth"/>
        </div>
        <div class="form-group">
          <label class="form-label">Password</label>
          <input class="input" type="password" id="s-auth_password" value="${s.auth_password||''}" placeholder="Leave empty to disable auth"/>
          <span class="form-hint">⚠️ Save settings and reload the page to activate. Keep both fields empty to disable.</span>
        </div>
      </div>
    </div>

        
      <div class="scard">
      <div class="scard-header">📁 Folders</div>
      <div class="scard-body">
        <div class="form-group">
          <label class="form-label">Watch Folder</label>
          <input class="input" id="s-watch_folder" value="${s.watch_folder||''}"/>
          <span class="form-hint">Drop .torrent or .magnet files here</span>
        </div>
        <div class="form-group">
          <label class="form-label">Processed Folder</label>
          <input class="input" id="s-processed_folder" value="${s.processed_folder||''}"/>
        </div>
        <div class="form-group">
          <label class="form-label">Download Folder</label>
          <input class="input" id="s-download_folder" value="${s.download_folder||''}"/>
        </div>
        <div class="form-group">
          <label class="form-label">Max Concurrent Downloads</label>
          <input class="input" type="number" id="s-max_concurrent_downloads" value="${s.max_concurrent_downloads??3}" min="1" max="20" onchange="syncMaxDlFields(this.value)"/>
          <span class="form-hint">How many torrents are processed in parallel (unlocked + dispatched to aria2). Default: 3. Higher values increase throughput but also RAM and bandwidth usage. Separate from the aria2 <em>max active downloads</em> setting below.</span>
        </div>
      </div>
    </div>

      <div class="scard">
        <div class="scard-header">💾 Disk Space Guard</div>
        <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Abort a download before it starts if the download folder has less than this amount of free space. 0 = disabled.</p>
        <div class="scard-body">
          <div class="form-group">
            <label class="form-label">Minimum Free Disk Space (GB, 0 = disabled)</label>
            <input class="input" type="number" id="s-min_free_disk_gb" value="${s.min_free_disk_gb??0}" min="0" step="0.5"/>
            <span class="form-hint">When free space on the download folder falls below this value, the torrent is marked error instead of downloading. The torrent row is kept — no duplicate download will occur after space is freed and you retry.</span>
          </div>
        </div>
      </div>

      <div class="scard">
        <div class="scard-header">⚙️ Post-Processing Script</div>
        <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Optional shell command executed after a torrent is fully downloaded and imported into Sonarr/Radarr. Leave empty to disable.</p>
        <div class="scard-body">
          <div class="form-group">
            <label class="form-label">On Torrent Complete</label>
            <input class="input" id="s-on_torrent_complete" value="${s.on_torrent_complete||''}" placeholder="/scripts/notify.sh &quot;{name}&quot; &quot;{path}&quot;"/>
            <span class="form-hint">Placeholders: <code>{name}</code> · <code>{path}</code> · <code>{torrent_id}</code> · <code>{status}</code>. Timeout: 300 s.</span>
          </div>
        </div>
      </div>

      <div class="scard">
        <div class="scard-header">⚠️ Auto-Restart Stuck Downloads</div>
      <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Torrents stuck in a non-terminal state longer than this will be automatically retried. 0 = disabled.</p>
        <div class="scard-body">
          <div class="form-group">
            <label class="form-label">Stuck timeout (hours)</label>
            <input class="input" type="number" id="s-stuck_download_timeout_hours" value="${s.stuck_download_timeout_hours??6}" min="0" max="168"/>
            <span class="form-hint">Torrents stuck in queued/downloading for longer than this are automatically reset. Set to 0 to disable.</span>
          </div>
        </div>
      </div>
      
      <div class="scard">
        <div class="scard-header">🚦 AllDebrid Rate Limit</div>
      <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Controls AllDebrid API call rate, background sync interval, and automatic retry settings.</p>
        <div class="scard-body">
          <div class="form-group">
            <label class="form-label">API calls per minute</label>
            <input class="input" type="number" id="s-alldebrid_rate_limit_per_minute" value="${s.alldebrid_rate_limit_per_minute??60}" min="0" max="300"/>
            <span class="form-hint">Default: 60 req/min. Set to 0 for unlimited.</span>
          </div>
          <div class="form-group">
            <label class="form-label">Full AllDebrid Sync Interval (minutes)</label>
            <input class="input" type="number" id="s-full_sync_interval_minutes" value="${s.full_sync_interval_minutes??5}" min="0" max="1440"/>
            <span class="form-hint">Reconciles all known AllDebrid magnets. 0 = disabled.</span>
          </div>
          <div class="form-group">
            <label class="form-label">aria2 Retry Count</label>
            <input class="input" type="number" id="s-aria2_error_retry_count" value="${s.aria2_error_retry_count??3}" min="0" max="20"/>
            <span class="form-hint">How often a failed aria2 file is retried. 0 = disabled.</span>
          </div>
          <div class="form-group">
            <label class="form-label">aria2 Retry Delay (seconds)</label>
            <input class="input" type="number" id="s-aria2_error_retry_delay_seconds" value="${s.aria2_error_retry_delay_seconds??60}" min="0" max="3600"/>
            <span class="form-hint">Delay before retrying a failed aria2 file.</span>
          </div>
        </div>
      </div>
    </div>`);
  _sf.insertAdjacentHTML('beforeend', `<div class="stab-panel" id="tab-download">
      <div class="scard">
      <div class="scard-header">⬇️ Download Client</div>
      <div class="scard-body">
        <div class="form-group">
          <label class="form-label">Delivery Mode</label>
          <select class="input" id="s-download_client" onchange="toggleSymlinkSettings(this.value)">
            <option value="aria2" ${(s.download_client||'aria2')==='aria2'?'selected':''}>aria2 (via JSON-RPC)</option>
            <option value="symlink" ${s.download_client==='symlink'?'selected':''}>Symlink / .url files (rclone mount)</option>
          </select>
          <span class="form-hint">
            <b>aria2</b> — unlocks AllDebrid links and hands them to aria2 for actual download.<br>
            <b>Symlink</b> — creates .url files containing the unlocked CDN link. Ideal for rclone AllDebrid mounts.
          </span>
          <div id="symlink-settings" style="display:${s.download_client==='symlink'?'block':'none'};margin-top:10px;border-left:3px solid var(--accent);padding-left:12px">
            <div class="form-group">
              <label class="form-label">Symlink / .url Output Path</label>
              <input class="input" id="s-symlink_path" value="${s.symlink_path||''}" placeholder="Leave empty to use Download Folder"/>
              <span class="form-hint">Directory where .url files are written. Defaults to Download Folder if empty.</span>
            </div>
          </div>
          <details class="info-details">
            <summary>How do the delivery modes work?</summary>
            <div class="info-details-body">
              <div class="info-mode">
                <div class="info-mode-title">⚡ aria2</div>
                <div class="info-mode-desc">
                  The app unlocks each AllDebrid link and hands the resulting URL to aria2 via JSON-RPC.
                  aria2 then handles the actual download entirely on its own — it decides how many connections to open,
                  where to write the file, whether to segment the transfer, and when it is complete.
                  The app only monitors aria2's reported status (<code>active / waiting / complete / error</code>)
                  and updates its internal state accordingly. When aria2 reports a download as complete,
                  the app marks the file done, removes the entry from aria2, and — once all files of a torrent
                  are finished — deletes the torrent from AllDebrid.
                </div>
                <div class="info-mode-pros">✔ Faster multi-connection downloads · resumable · aria2 manages bandwidth &amp; concurrency · works across Docker volumes</div>
                <div class="info-mode-cons">✖ Requires a running aria2 instance with RPC enabled · needs correct RPC URL and optional secret configured below</div>
              </div>
              <div class="info-mode">
                <div class="info-mode-title">📁 aria2 Download Root</div>
                <div class="info-mode-desc">
                  Only relevant in aria2 mode. When the app and aria2 run in separate Docker containers,
                  their views of the filesystem differ. Set this to the path that aria2 uses as its download
                  root (e.g. <code>/downloads</code>) so the app constructs the correct <code>dir</code> and
                  <code>out</code> options when submitting jobs. Leave empty if both containers share the same mount path.
                </div>
              </div>
            </div>
          </details>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Mode</label>
          <select class="input" id="s-aria2_mode" onchange="settingsData.aria2_mode=this.value; renderSettings(); loadAria2Runtime().catch(()=>{});">
            <option value="external" ${(s.aria2_mode||'external')==='external'?'selected':''}>External aria2</option>
            <option value="builtin" ${(s.aria2_mode||'external')==='builtin'?'selected':''}>Built-in aria2</option>
          </select>
          <span class="form-hint">Built-in aria2 is managed by this container and only receives AllDebrid HTTP(S) links.</span>
        </div>
        <div class="scard" style="margin-bottom:0">
          <div class="scard-header">aria2 Runtime</div>
          <div class="scard-body">
            <div id="aria2-runtime-status" class="form-hint" style="line-height:1.6">Runtime status not loaded yet.</div>
            <div class="input-row">
              <button class="btn btn-blue btn-sm" onclick="loadAria2Runtime()">Refresh</button>
              <button class="btn btn-ghost btn-sm" onclick="aria2RuntimeAction('start')" ${aria2BuiltIn?'':'disabled'}>Start</button>
              <button class="btn btn-ghost btn-sm" onclick="aria2RuntimeAction('restart')" ${aria2BuiltIn?'':'disabled'}>Restart</button>
              <button class="btn btn-danger btn-sm" onclick="aria2RuntimeAction('stop')" ${aria2BuiltIn?'':'disabled'}>Stop</button>
              <button class="btn btn-ghost btn-sm" onclick="aria2RuntimeAction('apply')" ${aria2BuiltIn?'':'disabled'}>Apply</button>
            </div>
          </div>
        </div>
        <div class="scard" style="margin-bottom:0">
          <div class="scard-header">aria2 Live Downloads</div>
          <div class="scard-body">
            <div class="aria2-queue-head">
              <div class="form-hint">Live aria2 queue with progress, speed, status, and basic controls.</div>
              <div class="input-row">
                <button class="btn btn-blue btn-sm" onclick="loadAria2Downloads()">Refresh Queue</button>
                <button class="btn btn-ghost btn-sm" onclick="runAria2Housekeeping()">Purge Results</button>
              </div>
            </div>
            <div id="aria2-downloads" class="aria2-queue">
              <div class="empty">Queue not loaded yet.</div>
            </div>
          </div>
        </div>
        <div class="toggle-row">
          <div class="toggle-info">
            <div class="tl">Auto-start Built-in aria2</div>
            <div class="td">Starts the internal aria2 daemon when the app starts in built-in mode.</div>
          </div>
          <label class="toggle"><input type="checkbox" id="s-aria2_builtin_auto_start" ${s.aria2_builtin_auto_start!==false?'checked':''}><div class="ttrack"></div></label>
        </div>
        <div class="form-group">
          <label class="form-label">Built-in aria2 Port</label>
          <input class="input" type="number" id="s-aria2_builtin_port" value="${s.aria2_builtin_port??6800}" min="1" max="65535"/>
          <span class="form-hint">The internal RPC secret is managed by the app and cannot be changed from the UI.</span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 RPC URL</label>
          <div class="test-row">
            <input class="input" id="s-aria2_url" value="${aria2BuiltIn ? 'http://127.0.0.1:'+(s.aria2_builtin_port||6800)+'/jsonrpc' : (s.aria2_url||'http://127.0.0.1:6800/jsonrpc')}" placeholder="http://127.0.0.1:6800/jsonrpc" ${aria2BuiltIn?'readonly':''}/>
            <button class="btn btn-blue btn-sm" onclick="testAria2()">Test</button>
          </div>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Secret</label>
          <input class="input" type="password" id="s-aria2_secret" value="${aria2BuiltIn ? 'managed-internally' : (s.aria2_secret||'')}" placeholder="Optional RPC secret" ${aria2BuiltIn?'readonly':''}/>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Download Root</label>
          <input class="input" id="s-aria2_download_path" value="${s.aria2_download_path||''}" placeholder="Optional remote root path"/>
          <span class="form-hint">External aria2 only. Built-in aria2 always uses the Download Folder mount directly.</span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Timeout (seconds)</label>
          <input class="input" type="number" id="s-aria2_operation_timeout_seconds" value="${s.aria2_operation_timeout_seconds??15}" min="5" max="120"/>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Simultaneous Downloads</label>
          <input class="input" type="number" id="s-aria2_max_active_downloads" value="${s.aria2_max_active_downloads??s.max_concurrent_downloads??3}" min="1" max="50" onchange="syncMaxDlFields(this.value)"/>
          <span class="form-hint">Only this many files are handed to aria2 at once. Remaining files stay pending until a slot becomes free.</span>
        </div>
        <div class="form-group">
          <label class="form-label">Deep Filesystem Sync Interval (minutes)</label>
          <input class="input" type="number" id="s-aria2_deep_sync_interval_minutes" value="${s.aria2_deep_sync_interval_minutes??10}" min="0" max="1440"/>
          <span class="form-hint">
            Periodically checks if downloaded files exist on disk — independent of aria2 GID or status.
            Resolves stuck downloads where aria2 lost track of the entry or the same filename appears in different folders.
            <b>0 = disabled.</b> Default: 10 minutes.
          </span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Poll Interval (seconds)</label>
          <input class="input" type="number" id="s-aria2_poll_interval_seconds" value="${s.aria2_poll_interval_seconds??5}" min="2" max="300"/>
          <span class="form-hint">How often the client refreshes aria2 download state.</span>
        </div>
        <div class="form-group">
          <button class="btn btn-ghost" onclick="triggerFullSync()">🔄 Full AllDebrid Sync Now</button>
          <button class="btn btn-ghost" onclick="runDeepSync()">🔍 Run Deep Sync Now</button>
          <span class="form-hint" style="margin-top:6px;display:block">Immediately checks all pending aria2 files on disk and marks completed ones.</span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Result Purge Interval (minutes)</label>
          <input class="input" type="number" id="s-aria2_purge_interval_minutes" value="${s.aria2_purge_interval_minutes??15}" min="0" max="1440"/>
          <span class="form-hint">Automatically purges stopped result entries from aria2. 0 = disabled.</span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 max-download-result</label>
          <input class="input" type="number" id="s-aria2_max_download_result" value="${s.aria2_max_download_result??50}" min="10" max="5000"/>
          <span class="form-hint">Lower values reduce how many stopped results aria2 keeps in memory.</span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Split Connections</label>
          <input class="input" type="number" id="s-aria2_split" value="${s.aria2_split??8}" min="1" max="64"/>
          <span class="form-hint">Parallel connections per file. Default: 8. Higher = faster single-file downloads. Capped by <em>Max connections per server</em>.</span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Min Split Size</label>
          <input class="input" id="s-aria2_min_split_size" value="${s.aria2_min_split_size||'10M'}" placeholder="10M"/>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Max Connections per Server</label>
          <input class="input" type="number" id="s-aria2_max_connection_per_server" value="${s.aria2_max_connection_per_server??8}" min="1" max="32"/>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Disk Cache</label>
          <input class="input" id="s-aria2_disk_cache" value="${s.aria2_disk_cache||'64M'}" placeholder="64M"/>
          <span class="form-hint">Write buffer size. Format: <code>0</code>, <code>64M</code>, <code>128M</code>. Default: 64M. A small cache reduces disk I/O on HDD or FUSE mounts.</span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 File Allocation</label>
          <select class="input" id="s-aria2_file_allocation">
            ${['none','prealloc','trunc','falloc'].map(v=>'<option value="'+v+'" '+((s.aria2_file_allocation||'falloc')===v?'selected':'')+'>'+v+'</option>').join('')}
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Lowest Speed Limit</label>
          <input class="input" id="s-aria2_lowest_speed_limit" value="${s.aria2_lowest_speed_limit||'0'}" placeholder="0"/>
        </div>
        <div class="toggle-row">
          <div class="toggle-info">
            <div class="tl">Continue Partial Downloads</div>
            <div class="td">Allows aria2 to resume partial HTTP downloads when possible.</div>
          </div>
          <label class="toggle"><input type="checkbox" id="s-aria2_continue_downloads" ${s.aria2_continue_downloads!==false?'checked':''}><div class="ttrack"></div></label>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Waiting Query Window</label>
          <input class="input" type="number" id="s-aria2_waiting_window" value="${s.aria2_waiting_window??100}" min="10" max="1000"/>
          <span class="form-hint">How many waiting jobs the client asks aria2 for per sync cycle. Lower values reduce RPC payload size and state pressure.</span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Stopped Query Window</label>
          <input class="input" type="number" id="s-aria2_stopped_window" value="${s.aria2_stopped_window??100}" min="10" max="1000"/>
          <span class="form-hint">How many stopped jobs the client inspects per sync cycle and diagnostics call.</span>
        </div>
        <div class="toggle-row">
          <div class="toggle-info">
            <div class="tl">Keep Unfinished Download Results</div>
            <div class="td">Usually best disabled to avoid large unfinished result history in aria2 memory.</div>
          </div>
          <label class="toggle"><input type="checkbox" id="s-aria2_keep_unfinished_download_result" ${s.aria2_keep_unfinished_download_result?'checked':''}><div class="ttrack"></div></label>
        </div>
        <div class="form-group">
          <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
            <button class="btn btn-ghost" onclick="runAria2Housekeeping()">Run aria2 Cleanup Now</button>
            <button class="btn btn-ghost" onclick="showMemoryInfo()" title="Shows real RAM vs kernel page cache">&#128202; Memory Info</button>
            <button class="btn btn-ghost" onclick="dropPageCache()" title="Release kernel page cache for all downloaded files">&#129522; Drop Page Cache</button>
          </div>
        </div>
        <div id="aria2-memory-diagnostics" class="form-hint" style="line-height:1.6"></div>
        <div id="aria2-memory-info" class="form-hint" style="line-height:1.6;margin-top:6px;display:none"></div>
        <div class="toggle-row">
          <div class="toggle-info">
            <div class="tl">Start aria2 Jobs Paused</div>
            <div class="td">Queue the job in aria2 first and resume it manually from the API/UI workflow.</div>
          </div>
          <label class="toggle"><input type="checkbox" id="s-aria2_start_paused" ${s.aria2_start_paused?'checked':''}><div class="ttrack"></div></label>
        </div>
      </div>
    </div>
    <div class="scard">
      <div class="scard-header">&#9889; Upload Retry</div>
      <div class="scard-body">
        <div class="form-group">
          <label class="form-label">AllDebrid Upload Retry Count</label>
          <input class="input" type="number" id="s-upload_fail_retry_count" value="${s.upload_fail_retry_count??3}" min="0" max="10"/>
          <span class="form-hint">How often to re-queue when AllDebrid reports "Upload failed" (statusCode 5). 0 = disabled.</span>
        </div>
        <div class="form-group">
          <label class="form-label">Upload Retry Delay (minutes)</label>
          <input class="input" type="number" id="s-upload_fail_retry_delay_minutes" value="${s.upload_fail_retry_delay_minutes??5}" min="1" max="60"/>
          <span class="form-hint">Minutes to wait before re-uploading. Default: 5.</span>
        </div>
      </div>
    </div>
    </div>`);
  _sf.insertAdjacentHTML('beforeend', `<div class="stab-panel" id="tab-extract">
      <div class="scard">
        <div class="scard-header">&#128230; Auto-Extraction</div>
        <div class="scard-body">
          <div class="form-group">
            <label class="form-label toggle-label"><span>Enable Auto-Extraction</span>
              <label class="tswitch"><input type="checkbox" id="s-extract_enabled" ${s.extract_enabled?'checked':''}/><span class="tslider"></span></label>
            </label>
            <span class="form-hint">Automatically extract archives (.zip .rar .7z .tar.gz .tar.bz2 .tar.xz and more) after download completes. p7zip-full and unrar-free are included in the Docker image.</span>
          </div>
          <div class="form-group">
            <label class="form-label toggle-label"><span>Delete Archive After Extraction</span>
              <label class="tswitch"><input type="checkbox" id="s-extract_delete_archive" ${s.extract_delete_archive!==false?'checked':''}/><span class="tslider"></span></label>
            </label>
            <span class="form-hint">Remove the source archive after successful extraction. Enabled by default.</span>
          </div>
          <div class="form-group">
            <label class="form-label">Max Concurrent Extractions</label>
            <input class="input" type="number" id="s-extract_max_concurrent" value="${s.extract_max_concurrent??2}" min="1" max="10"/>
            <span class="form-hint">Maximum number of archives extracted in parallel. Default: 2.</span>
          </div>
          <div class="form-group">
            <label class="form-label toggle-label"><span>Discord Notification on Extraction</span>
              <label class="tswitch"><input type="checkbox" id="s-discord_notify_extract" ${s.discord_notify_extract!==false?'checked':''}/><span class="tslider"></span></label>
            </label>
            <span class="form-hint">Send a Discord webhook on extraction completion or failure.</span>
          </div>
        </div>
      </div>
    </div>`);
  _sf.insertAdjacentHTML('beforeend', `<div class="stab-panel" id="tab-notifications">
      <div class="scard">
        <div class="scard-header">🔔 Discord Notifications</div>
        <div class="scard-body">
          <p class="form-hint" style="margin:0 0 10px">Receive Discord notifications when torrents are added, complete, or fail.</p>
          <div class="form-group">
            <label class="form-label">Bot Name <span style="font-weight:400;color:var(--muted)">(shown as sender in Discord)</span></label>
            <input class="input" id="s-discord_username" value="${s.discord_username||'AllDebrid-Client'}" placeholder="AllDebrid-Client"/>
          </div>
          <div class="form-group">
            <label class="form-label">Bot Avatar <span style="font-weight:400;color:var(--muted)">(PNG/JPG/WEBP only — no SVG)</span></label>
            <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
              <input class="input" id="s-discord_avatar_url" value="${s.discord_avatar_url||''}" placeholder="https://…/avatar.png" style="flex:1"/>
              <label class="btn btn-ghost btn-sm" style="cursor:pointer;white-space:nowrap">
                📎 Upload
                <input type="file" accept="image/png,image/jpeg,image/gif,image/webp"
                  style="display:none" onchange="uploadDiscordAvatar(this)"/>
              </label>
            </div>
            <div id="avatar-preview" style="display:none;align-items:center;gap:8px;font-size:12px;color:var(--text2)">
              <img id="avatar-preview-img" src="" style="width:40px;height:40px;border-radius:50%;object-fit:cover;border:2px solid var(--border)"/>
              <span id="avatar-preview-label"></span>
              <button class="btn btn-ghost btn-sm" onclick="clearDiscordAvatar()" style="font-size:11px">✕ Remove</button>
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">Main Webhook URL</label>
            <div class="test-row">
              <input class="input" id="s-discord_webhook_url" value="${s.discord_webhook_url||''}" placeholder="https://discord.com/api/webhooks/…"/>
              <button class="btn btn-blue btn-sm" onclick="testDiscord()">Test</button>
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">Webhook URL — Torrent Added <span style="font-weight:400;color:var(--muted)">(optional)</span></label>
            <input class="input" id="s-discord_webhook_added" value="${s.discord_webhook_added||''}" placeholder="https://discord.com/api/webhooks/…"/>
          </div>
          <div class="toggle-row">
            <div class="toggle-info"><div class="tl">Notify on Added</div></div>
            <label class="toggle"><input type="checkbox" id="s-discord_notify_added" ${s.discord_notify_added?'checked':''}><div class="ttrack"></div></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-info"><div class="tl">Notify on Finished</div></div>
            <label class="toggle"><input type="checkbox" id="s-discord_notify_finished" ${s.discord_notify_finished?'checked':''}><div class="ttrack"></div></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-info"><div class="tl">Notify on Error</div></div>
            <label class="toggle"><input type="checkbox" id="s-discord_notify_error" ${s.discord_notify_error?'checked':''}><div class="ttrack"></div></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-info"><div class="tl">Notify on new version</div><div class="ts">Send a webhook when a newer release is available on GitHub.</div></div>
            <label class="toggle"><input type="checkbox" id="s-discord_notify_update" ${s.discord_notify_update!==false?'checked':''}><div class="ttrack"></div></label>
          </div>
          <div class="form-group">
            <label class="form-label">Version check interval (hours) <span style="font-size:9px;color:var(--text3);font-weight:400">0 = disabled</span></label>
            <input class="input" id="s-update_check_interval_hours" type="number" min="0" max="168" style="width:90px" value="${s.update_check_interval_hours??12}"/>
            <span class="form-hint">How often GitHub is polled for a new release. Default: 12 h.</span>
          </div>
        </div>
      </div>
    </div>`);
  _sf.insertAdjacentHTML('beforeend', `<div class="stab-panel" id="tab-services">
      <div class="scard">
        <div class="scard-header">📺 Sonarr</div>
        <div class="scard-body">
          <div class="toggle-row">
            <div class="toggle-info"><div class="tl">Enable Sonarr</div><div class="ts">Trigger RescanSeries after download completes</div></div>
            <label class="toggle"><input type="checkbox" id="s-sonarr_enabled" ${s.sonarr_enabled?'checked':''}><div class="ttrack"></div></label>
          </div>
          <div class="form-group">
            <label class="form-label">Sonarr URL</label>
            <div class="test-row">
              <input class="input" id="s-sonarr_url" value="${s.sonarr_url||''}" placeholder="http://sonarr:8989"/>
              <button class="btn btn-blue btn-sm" onclick="testSonarr()">Test</button>
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">API Key</label>
            <input class="input" type="password" id="s-sonarr_api_key" value="${s.sonarr_api_key||''}" placeholder="Sonarr API key"/>
          </div>
        </div>
      </div>
      
      <div class="scard">
        <div class="scard-header">🎬 Radarr</div>
        <div class="scard-body">
          <div class="toggle-row">
            <div class="toggle-info"><div class="tl">Enable Radarr</div><div class="ts">Trigger RescanMovie after download completes</div></div>
            <label class="toggle"><input type="checkbox" id="s-radarr_enabled" ${s.radarr_enabled?'checked':''}><div class="ttrack"></div></label>
          </div>
          <div class="form-group">
            <label class="form-label">Radarr URL</label>
            <div class="test-row">
              <input class="input" id="s-radarr_url" value="${s.radarr_url||''}" placeholder="http://radarr:7878"/>
              <button class="btn btn-blue btn-sm" onclick="testRadarr()">Test</button>
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">API Key</label>
            <input class="input" type="password" id="s-radarr_api_key" value="${s.radarr_api_key||''}" placeholder="Radarr API key"/>
          </div>
        </div>
      </div>
      
      <div class="scard" style="border-color:rgba(59,130,246,.3)">
        <div class="scard-header">&#8505;&#65039; Jackett Setup</div>
        <div class="scard-body">
          <div style="font-size:12px;line-height:1.6;color:var(--text2)">
            <b>Jackett</b> is a torrent-indexer proxy that lets you search dozens of trackers from one place.<br><br>
            Set the <b>Jackett URL</b> (e.g. <code style="background:var(--surface2);padding:1px 5px;border-radius:3px">http://jackett:9117</code>)
            and paste your <b>API Key</b> from the Jackett dashboard (<em>Dashboard &rarr; API Key</em> button).
          </div>
        </div>
      </div>

      <div class="scard">
        <div class="scard-header">&#128269; Jackett Integration</div>
        <div class="scard-body">
          <div class="toggle-row">
            <div class="toggle-info"><div class="tl">Enable Jackett</div><div class="td">Show the Search tab and allow torrent searches</div></div>
            <label class="toggle"><input type="checkbox" id="s-jackett_enabled" ${s.jackett_enabled?'checked':''}><span class="ttrack"></span></label>
          </div>
          <div class="form-group">
            <label class="form-label">Jackett URL</label>
            <input class="input" id="s-jackett_url" value="${s.jackett_url||'http://localhost:9117'}" placeholder="http://jackett:9117"/>
            <span class="form-hint">Base URL of your Jackett instance (no trailing slash).</span>
          </div>
          <div class="form-group">
            <label class="form-label">API Key</label>
            <input class="input" id="s-jackett_api_key" value="${s.jackett_api_key||''}" placeholder="Jackett API key" type="password"/>
            <span class="form-hint">Found in the Jackett dashboard. Proxied through the backend &mdash; never exposed to the browser.</span>
          </div>
          <div style="margin-top:8px">
            <button class="btn btn-ghost btn-sm" onclick="testJackett()">&#128268; Test Connection</button>
            <span id="jackett-test-result" style="margin-left:10px;font-size:12px;color:var(--text2)"></span>
          </div>
        </div>
      </div>


      <div class="scard">
        <div class="scard-header">&#128270; Prowlarr</div>
        <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Modern indexer manager. Alternative to Jackett with native *arr integration.</p>
        <div class="scard-body">
          <div class="form-group">
            <label class="form-label">Enable Prowlarr</label>
            <label class="toggle"><input type="checkbox" id="s-prowlarr_enabled" ${s.prowlarr_enabled?'checked':''}/><span class="slider"></span></label>
          </div>
          <div class="form-group">
            <label class="form-label">Prowlarr URL</label>
            <input class="input" id="s-prowlarr_url" value="${s.prowlarr_url||'http://localhost:9696'}" placeholder="http://localhost:9696"/>
          </div>
          <div class="form-group">
            <label class="form-label">API Key</label>
            <input class="input" id="s-prowlarr_api_key" value="${s.prowlarr_api_key||''}" placeholder="Prowlarr Settings → General → API Key"/>
            <span class="form-hint">Find it in Prowlarr → Settings → General. When enabled, Prowlarr results appear in the Search view alongside Jackett results.</span>
          </div>
          <div style="margin-top:8px">
            <button class="btn btn-ghost btn-sm" onclick="testProwlarr()">&#128268; Test Connection</button>
            <span id="prowlarr-test-result" style="margin-left:10px;font-size:12px;color:var(--text2)"></span>
          </div>
        </div>
      </div>
      <div class="scard">
        <div class="scard-header">&#128276; Jackett Webhook</div>
        <div class="scard-body">
          <div class="form-group">
            <label class="form-label">Webhook URL <span style="font-weight:400;color:var(--text2)">(optional)</span></label>
            <input class="input" id="s-jackett_webhook_url" value="${s.jackett_webhook_url||''}" placeholder="https://discord.com/api/webhooks/&hellip;"/>
            <span class="form-hint">Dedicated webhook for torrents added via Jackett. Falls back to the main Discord webhook if empty.</span>
          </div>
        </div>
      </div>
      <div class="scard" style="border-color:rgba(59,130,246,.3)">
      <div class="scard-header">ℹ️ FlexGet Setup</div>
      <div class="scard-body">
        <div style="font-size:12px;line-height:1.6;color:var(--text2)">
          FlexGet must be running with its <b>web server enabled</b>.
          Add this to your <code style="background:var(--surface2);padding:1px 5px;border-radius:3px">config.yml</code>:
          <pre style="background:var(--surface2);border-radius:6px;padding:10px 12px;margin:8px 0;font-size:11px;overflow-x:auto">web_server:
  bind: 0.0.0.0
  port: 5050</pre>
          <b>Creating an API Key:</b><br>
          Run <code style="background:var(--surface2);padding:1px 5px;border-radius:3px">flexget web passwd &lt;username&gt;</code> to set a password,
          then use <code style="background:var(--surface2);padding:1px 5px;border-radius:3px">flexget web gentoken</code> to generate a token.
          Paste that token as API Key below.<br><br>
          <b>Docker note:</b> Make sure the FlexGet port (5050) is reachable from this container.
        </div>
      </div>
    </div>

    <div class="scard">
      <div class="scard-header">🤖 FlexGet Integration</div>
      <div class="scard-body">
        <div class="toggle-row">
          <div class="toggle-info"><div class="tl">Enable FlexGet</div><div class="td">Connect to a running FlexGet instance</div></div>
          <label class="toggle"><input type="checkbox" id="s-flexget_enabled" ${s.flexget_enabled?'checked':''}><span class="ttrack"></span></label>
        </div>
        <div class="form-group">
          <label class="form-label">FlexGet URL</label>
          <input class="input" id="s-flexget_url" value="${s.flexget_url||'http://localhost:5050'}" placeholder="http://localhost:5050"/>
          <span class="form-hint">Base URL of the FlexGet web server.</span>
        </div>
        <div class="form-group">
          <label class="form-label">API Key</label>
          <input class="input" type="password" id="s-flexget_api_key" value="${s.flexget_api_key||''}" placeholder="Optional API token"/>
        </div>
        <div class="form-group">
          <label class="form-label">Tasks to run (comma-separated)</label>
          <input class="input" id="s-flexget_tasks_raw" value="${s.flexget_tasks_raw||''}" placeholder="task1, task2 — leave empty for all"/>
          <span class="form-hint">Used for manual runs. Leave empty to run all tasks on demand.</span>
        </div>
        <div class="form-group">
          <label class="form-label">Scheduled Task Profiles</label>
          <span class="form-hint">Each task can have its own interval and jitter. Disabled rows are ignored.</span>
          <datalist id="flexget-task-options">
            ${flexgetAvailableTasks.map(function(task){return '<option value="'+task+'"></option>';}).join('')}
          </datalist>
          <div id="flexget-schedule-editor" style="margin-top:8px"></div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
            <button class="btn btn-ghost btn-sm" onclick="addFlexgetTaskSchedule()">+ Add Task Schedule</button>
            <button class="btn btn-ghost btn-sm" onclick="flexgetListTasks(true)">↻ Sync Tasks Into Editor</button>
          </div>
        </div>
        <div class="form-group">
          <label class="form-label">Task timeout (seconds, 0 = 3600)</label>
          <input class="input" type="number" id="s-flexget_task_timeout_seconds" value="${s.flexget_task_timeout_seconds??0}" min="0"/>
          <span class="form-hint">Max time to wait for a single task to complete. Default: 3600s (1 hour). Increase for very long-running tasks.</span>
        </div>
        <div class="form-group">
          <label class="form-label">Retry delay when unreachable (minutes)</label>
          <input class="input" type="number" id="s-flexget_retry_delay_minutes" value="${s.flexget_retry_delay_minutes??5}" min="0" max="60"/>
          <span class="form-hint">If FlexGet does not respond, wait this many minutes and try once more. 0 = no retry. A webhook is sent if still unreachable, and again when it recovers.</span>
        </div>

        <div class="form-group">
          <label class="form-label">FlexGet Webhook URL <span style="font-weight:400;color:var(--text2)">(optional — uses main Discord webhook if empty)</span></label>
          <input class="input" id="s-flexget_webhook_url" value="${s.flexget_webhook_url||''}" placeholder="Leave empty to use Discord webhook from Settings → Discord"/>
          <span class="form-hint">Optional. Receives all FlexGet events (run_started, task_started, task_ok, task_error, run_finished, server_unreachable, server_recovered) as Discord embeds or JSON. Falls back to Settings → Discord webhook when empty.</span>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:4px">
          <button class="btn btn-blue btn-sm" onclick="flexgetListTasks()">🔍 List Tasks</button>
          <button class="btn btn-primary btn-sm" onclick="flexgetRun()">▶ Run Now</button>
        </div>
        <div id="flexget-task-list" style="margin-top:10px;font-size:12px;color:var(--text2)"></div>
        <div id="flexget-run-result" style="margin-top:8px;font-size:12px;display:none"></div>
      </div>
    </div>

    <div class="scard">
      <div class="scard-header">📋 Recent FlexGet Runs</div>
      <div class="scard-body">
        <button class="btn btn-ghost btn-sm" onclick="loadFlexgetHistory()" style="margin-bottom:8px">↻ Refresh</button>
        <div id="flexget-history" style="font-size:12px;color:var(--text2)">Click refresh to load history.</div>
      </div>
    </div>
      <div class="scard">
        <div class="scard-header">🏷 Labels</div>
        <div class="scard-body">
          <div class="form-group">
            <label class="form-label">Predefined Labels <span style="font-weight:400;color:var(--text2)">(comma-separated)</span></label>
            <input class="input" id="s-torrent_labels_raw" value="${(s.torrent_labels||[]).join(', ')}" placeholder="Movies, Series, 4K, Anime"/>
            <span class="form-hint">Leave empty — labels are optional per torrent.</span>
          </div>
        </div>
      </div>
      
    </div>`);
  _sf.insertAdjacentHTML('beforeend', `</div>

    <div class="stab-panel" id="tab-advanced">
      <div class="scard">
      <div class="scard-header">🚫 File Filters</div>
      <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Skip unwanted files by extension, keyword, or minimum file size.</p>
      <div class="scard-body">
        <div class="toggle-row">
          <div class="toggle-info">
            <div class="tl">Enable File Filters</div>
            <div class="td">When off, all files are downloaded regardless of extension, keyword or size rules.</div>
          </div>
          <label class="toggle"><input type="checkbox" id="s-filters_enabled" ${s.filters_enabled!==false?'checked':''} onchange="toggleFilterFields()"><div class="ttrack"></div></label>
        </div>
        <div id="filter-fields" style="${s.filters_enabled===false?'opacity:.4;pointer-events:none':''}">
          <div class="form-group">
            <label class="form-label">Blocked Extensions (one per line)</label>
            <textarea class="input" id="s-blocked_extensions" rows="6">${(s.blocked_extensions||[]).join('\n')}</textarea>
            <span class="form-hint">e.g. .jpg · .png · .nfo — images are blocked by default</span>
          </div>
          <div class="form-group">
            <label class="form-label">Blocked Keywords (one per line)</label>
            <textarea class="input" id="s-blocked_keywords" rows="3">${(s.blocked_keywords||[]).join('\n')}</textarea>
          </div>
          <div class="form-group">
            <label class="form-label">Minimum File Size (MB, 0 = no limit)</label>
            <input class="input" type="number" id="s-min_file_size_mb" value="${s.min_file_size_mb??0}" min="0"/>
          </div>
        </div>
      </div>
    </div>
      <div class="scard">
      <div class="scard-header">⏱ Polling Intervals</div>
      <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">How often AllDebrid and your watch folder are checked for new activity.</p>
      <div class="scard-body">
        <div class="form-group">
          <label class="form-label">AllDebrid Poll Interval (seconds)</label>
            <input class="input" type="number" id="s-poll_interval_seconds" value="${s.poll_interval_seconds??30}" min="10"/>
          <span class="form-hint">How often to ask AllDebrid for torrent status. Default: 30 s. Minimum: 10 s. Lower = faster detection but more API calls.</span>
        </div>
        <div class="form-group">
          <label class="form-label">Watch Folder Scan (seconds)</label>
            <input class="input" type="number" id="s-watch_interval_seconds" value="${s.watch_interval_seconds??10}" min="5"/>
        </div>
      </div>
    </div>
      <div class="scard">
        <div class="scard-header">💾 Automatic Backups</div>
      <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Automatically create periodic database backups to prevent data loss.</p>
        <div class="scard-body">
          <div class="toggle-row">
            <div class="toggle-info"><div class="tl">Enable Backups</div><div class="ts">Automatically back up config and database</div></div>
            <label class="toggle"><input type="checkbox" id="s-backup_enabled" ${s.backup_enabled!==false?'checked':''}><div class="ttrack"></div></label>
          </div>
          <div class="form-group">
            <label class="form-label">Backup Folder</label>
            <input class="input" id="s-backup_folder" value="${s.backup_folder||'/app/data/backups'}"/>
          </div>
          <div class="form-group">
            <label class="form-label">Interval (hours)</label>
            <input class="input" type="number" id="s-backup_interval_hours" value="${s.backup_interval_hours??24}" min="1" max="168"/>
            <span class="form-hint">Default: 24h. Backup runs once per interval.</span>
          </div>
          <div class="form-group">
            <label class="form-label">Keep backups for (days)</label>
            <input class="input" type="number" id="s-backup_keep_days" value="${s.backup_keep_days??7}" min="1" max="90"/>
            <span class="form-hint">Default: 7 days. Older backups are deleted automatically.</span>
          </div>
          <div style="display:flex;gap:8px;margin-top:4px">
            <button class="btn btn-ghost" onclick="triggerBackup()">💾 Run Backup Now</button>
            <button class="btn btn-ghost" onclick="loadBackupList()">📋 List Backups</button>
          </div>
          <div id="backup-list" style="margin-top:10px;font-size:12px;color:var(--text2)"></div>
        </div>
      </div>
      <div class="scard" style="border-color:rgba(59,130,246,.3)">
      <div class="scard-header">ℹ️ About Reporting</div>
      <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Periodic statistics snapshots and optional Discord-based statistics reports.</p>
      <div class="scard-body">
        <div style="font-size:12px;line-height:1.6;color:var(--text2)">
          The reporting module captures <b>comprehensive metrics</b> across all client activity automatically.<br><br>
          <b>Snapshots</b> are periodic point-in-time captures stored in the database for trend analysis.
          Set an interval to enable automatic snapshots (recommended: 60 min).<br><br>
          <b>Export</b> downloads a full JSON report for the selected time window — useful for external analysis or archiving.<br><br>
          <b>Time windows</b>: select the period in the dropdown below, then click <i>Load Report</i>.
        </div>
      </div>
    </div>

    <div class="scard">
      <div class="scard-header">📊 Statistics Reporting</div>
      <div class="scard-body">
        <div class="form-group">
          <label class="form-label">Snapshot Interval (minutes, 0 = disabled)</label>
          <input class="input" type="number" id="s-stats_snapshot_interval_minutes" value="${s.stats_snapshot_interval_minutes??60}" min="0"/>
          <span class="form-hint">How often to capture a statistics snapshot. Default: 60.</span>
        </div>
        <div class="form-group">
          <label class="form-label">Keep Snapshots (days)</label>
          <input class="input" type="number" id="s-stats_snapshot_keep_days" value="${s.stats_snapshot_keep_days??30}" min="1"/>
        </div>
        <div class="form-group">
          <label class="form-label">Event Log Retention (days, 0 = keep forever)</label>
          <input class="input" type="number" id="s-events_keep_days" value="${s.events_keep_days??30}" min="0"/>
          <span class="form-hint">Events older than this are deleted daily. Torrent rows are never deleted — duplicate download prevention is not affected.</span>
        </div>
        <div class="form-group">
          <label class="form-label">Reporting Webhook URL <span style="font-weight:400;color:var(--text2)">(optional — uses main Discord webhook if empty)</span></label>
          <input class="input" id="s-stats_report_webhook_url" value="${s.stats_report_webhook_url||''}" placeholder="Leave empty to use main Discord webhook"/>
          <span class="form-hint">Receives structured reporting payloads as Discord embeds. Falls back to Settings → Discord → Webhook URL when empty.</span>
        </div>
        <div class="form-group">
          <label class="form-label">Automatic Report Interval (hours, 0 = disabled)</label>
          <input class="input" type="number" id="s-stats_report_interval_hours" value="${s.stats_report_interval_hours??0}" min="0" max="168"/>
          <span class="form-hint">How often the report is sent automatically. 0 = disabled.</span>
        </div>
        <div class="form-group">
          <label class="form-label">Report Window (hours)</label>
          <input class="input" type="number" id="s-stats_report_window_hours" value="${s.stats_report_window_hours??24}" min="1" max="8760"/>
          <span class="form-hint">Time window covered by each automatic report (default: 24h).</span>
        </div>
        <div class="form-group">
          <label class="form-label">Time Window</label>
          <select class="input" id="stats-report-hours" onchange="loadComprehensiveStats()">
            <option value="24"${Number(s.stats_report_window_hours ?? 24)===24?' selected':''}>Last 24 hours</option>
            <option value="168"${Number(s.stats_report_window_hours ?? 24)===168?' selected':''}>Last 7 days</option>
            <option value="720"${Number(s.stats_report_window_hours ?? 24)===720?' selected':''}>Last 30 days</option>
            <option value="8760"${Number(s.stats_report_window_hours ?? 24)===8760?' selected':''}>All time (~1 year)</option>
          </select>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:4px">
          <button class="btn btn-blue btn-sm" onclick="loadComprehensiveStats()">📊 Load Report</button>
          <button class="btn btn-ghost btn-sm" onclick="exportStats()">⬇ Export JSON</button>
          <button class="btn btn-ghost btn-sm" onclick="triggerStatsSnapshot()">📸 Snapshot Now</button>
          <button class="btn btn-ghost btn-sm" onclick="sendStatsReport()">📨 Send Webhook Now</button>
        </div>
        <div id="comprehensive-stats" style="margin-top:14px"></div>
      </div>
    </div>
      <div class="scard">
      <div class="scard-header">🗄️ Database</div>
      <div class="scard-body">
        <div style="background:rgba(249,115,22,.08);border:1px solid rgba(249,115,22,.2);border-radius:6px;padding:8px 12px;margin-bottom:12px;font-size:12px;color:var(--text2)">
          💾 <b>Save settings first</b>, then use <b>Test DB</b> to verify the connection.
        </div>
        <div class="form-group">
          <label class="form-label">Database Type</label>
          <select class="input" id="s-db_type"
            onchange="document.getElementById('pg-settings').style.display=(this.value==='postgres'||this.value==='postgres_internal')?'block':'none'"
            ${s._db_type_locked?'disabled':''}>
            <option value="sqlite" ${(s.db_type||'sqlite')==='sqlite'?'selected':''}>SQLite (default)</option>
            <option value="postgres" ${s.db_type==='postgres'?'selected':''}>PostgreSQL (external)</option>
          </select>
          ${s._db_type_locked ? '<span class="form-hint" style="color:var(--accent)">⚙️ DB_TYPE is set via docker-compose and cannot be changed here.</span>' : '<span class="form-hint">Use PostgreSQL to connect to an external database server. See docs/postgresql.md.</span>'}
        </div>
        <div id="pg-settings" style="display:${s.db_type==='postgres'?'block':'none'}">
          <div class="form-group">
            <label class="form-label">Host</label>
            <input class="input" id="s-postgres_host" value="${s.postgres_host||'localhost'}" ${s.db_type==='postgres_internal'?'readonly style="opacity:.5"':''}/>
          </div>
          <div class="form-group">
            <label class="form-label">Port</label>
            <input class="input" type="number" id="s-postgres_port" value="${s.postgres_port||5432}"/>
          </div>
          <div class="form-group">
            <label class="form-label">Database</label>
            <input class="input" id="s-postgres_db" value="${s.postgres_db||'alldebrid'}"/>
          </div>
          <div class="form-group">
            <label class="form-label">User</label>
            <input class="input" id="s-postgres_user" value="${s.postgres_user||'alldebrid'}"/>
          </div>
          <div class="form-group">
            <label class="form-label">Password</label>
            <input class="input" type="password" id="s-postgres_password" value="${s.postgres_password||''}"/>
          </div>
          <div class="form-group">
            <label class="form-label">Schema</label>
            <input class="input" id="s-postgres_schema" value="${s.postgres_schema||'public'}"/>
          </div>
          <div class="toggle-row">
            <div class="toggle-info"><div class="tl">SSL</div><div class="ts">Use SSL for PostgreSQL connection</div></div>
            <label class="toggle"><input type="checkbox" id="s-postgres_ssl" ${s.postgres_ssl?'checked':''}><span class="slider"></span></label>
          </div>
        </div>
      </div>
    </div>

    <div class="scard" style="border-color:rgba(249,115,22,.3)">
      <div class="scard-header">🔄 Data Migration</div>
      <div class="scard-body">
        <div style="font-size:12px;color:var(--text2);margin-bottom:12px">
          Migrate all data between SQLite and PostgreSQL. <b>Save settings first</b> before running migration.<br>
          <span style="color:var(--accent)">⚠️ This will overwrite all data in the target database.</span>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-ghost btn-sm" onclick="runMigration('sqlite_to_postgres', false)">
            📤 SQLite → PostgreSQL
          </button>
          <button class="btn btn-ghost btn-sm" onclick="runMigration('postgres_to_sqlite', false)">
            📥 PostgreSQL → SQLite
          </button>
          <button class="btn btn-ghost btn-sm" onclick="runMigration('sqlite_to_postgres', true)" style="opacity:.6">
            🔍 Dry Run (SQLite→PG)
          </button>
        </div>
        <div id="migration-result" style="margin-top:10px;font-size:12px;display:none"></div>
      </div>
    </div>

    <div class="scard">
      <div class="scard-header">🛠️ Database Maintenance</div>
      <div class="scard-body">
        <div class="form-group">
          <label class="form-label">Database Backup Folder</label>
          <input class="input" id="s-db_backup_folder" value="${s.db_backup_folder||'/app/data/db-backups'}"/>
        </div>
        <div class="toggle-row">
          <div class="toggle-info"><div class="tl">Enable Database Backups</div><div class="ts">Create JSON snapshots of the database only</div></div>
          <label class="toggle"><input type="checkbox" id="s-db_backup_enabled" ${s.db_backup_enabled!==false?'checked':''}><div class="ttrack"></div></label>
        </div>
        <div class="form-group">
          <label class="form-label">Keep database backups for (days)</label>
          <input class="input" type="number" id="s-db_backup_keep_days" value="${s.db_backup_keep_days??7}" min="1" max="365"/>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-ghost btn-sm" onclick="triggerDatabaseBackup()">💽 Run DB Backup Now</button>
          <button class="btn btn-ghost btn-sm" onclick="loadDatabaseBackupList()">📋 List DB Backups</button>
        </div>
        <div id="db-backup-list" style="margin-top:10px;font-size:12px;color:var(--text2)"></div>
        <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
          <div class="toggle-row">
            <div class="toggle-info"><div class="tl">Allow Database Wipe</div><div class="ts">Required before the wipe action can run</div></div>
            <label class="toggle"><input type="checkbox" id="s-db_wipe_enabled" ${s.db_wipe_enabled?'checked':''}><div class="ttrack"></div></label>
          </div>
          <div class="toggle-row" style="margin-top:10px">
            <div class="toggle-info"><div class="tl">Backup Before Wipe</div><div class="ts">Run a DB backup automatically before deleting rows</div></div>
            <label class="toggle"><input type="checkbox" id="s-db_backup_before_wipe" ${s.db_backup_before_wipe!==false?'checked':''}><div class="ttrack"></div></label>
          </div>
          <div class="form-hint" style="margin-top:10px">Pause processing first. Wipe clears torrents, files, events, FlexGet runs, and stats snapshots.</div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
            <button class="btn btn-danger btn-sm" onclick="wipeDatabase()">🗑️ Wipe Database</button>
          </div>
        </div>
      </div>
    </div>`);
  renderFlexgetTaskSchedules();
}

function getFormSettings() {
  const g = id => document.getElementById('s-'+id);
  const t = id => g(id)?.value?.trim() || '';
  const n = (id, fallback = 0) => {
    const raw = g(id)?.value;
    if (raw == null || raw === '') return fallback;
    const parsed = parseInt(raw, 10);
    return Number.isNaN(parsed) ? fallback : parsed;
  };
  const c = id => g(id)?.checked||false;
  const l = id => g(id)?.value?.split('\n').map(x=>x.trim()).filter(Boolean)||[];
  const reportHoursRaw = document.getElementById('stats-report-hours')?.value;
  const reportWindowHours = (() => {
    if (reportHoursRaw == null || reportHoursRaw === '') return Number(settingsData.stats_report_window_hours ?? 24);
    const parsed = parseInt(reportHoursRaw, 10);
    return Number.isNaN(parsed) ? Number(settingsData.stats_report_window_hours ?? 24) : parsed;
  })();
  return {
    ...settingsData,
    alldebrid_api_key: t('alldebrid_api_key'),
    alldebrid_agent:   t('alldebrid_agent')||'AllDebrid-Client',
    watch_folder: t('watch_folder'), processed_folder: t('processed_folder'),
    download_folder: t('download_folder'), max_concurrent_downloads: n('max_concurrent_downloads', 3),
    max_speed_mbps: (settingsData && settingsData.max_speed_mbps != null)
                   ? settingsData.max_speed_mbps : 0,
    download_client: t('download_client') || (settingsData && settingsData.download_client) || 'aria2',
    aria2_mode: t('aria2_mode') || 'builtin',
    aria2_url: (t('aria2_mode') || 'external') === 'builtin' ? (settingsData.aria2_url || 'http://127.0.0.1:6800/jsonrpc') : t('aria2_url'),
    aria2_secret: (t('aria2_mode') || 'external') === 'builtin' ? (settingsData.aria2_secret || '') : t('aria2_secret'),
    aria2_download_path: t('aria2_download_path'),
    aria2_builtin_auto_start: c('aria2_builtin_auto_start'),
    aria2_builtin_port: n('aria2_builtin_port', 6800),
    aria2_operation_timeout_seconds: n('aria2_operation_timeout_seconds', 15),
    aria2_max_active_downloads: n('aria2_max_active_downloads', n('max_concurrent_downloads', 3)),
    aria2_start_paused: c('aria2_start_paused'),
    aria2_poll_interval_seconds: n('aria2_poll_interval_seconds', 5),
    aria2_purge_interval_minutes: n('aria2_purge_interval_minutes', 15),
    aria2_max_download_result: n('aria2_max_download_result', 50),
    aria2_waiting_window: n('aria2_waiting_window', 100),
    aria2_stopped_window: n('aria2_stopped_window', 100),
    aria2_keep_unfinished_download_result: c('aria2_keep_unfinished_download_result'),
    aria2_split: n('aria2_split', 8),
    aria2_min_split_size: t('aria2_min_split_size') || '10M',
    aria2_max_connection_per_server: n('aria2_max_connection_per_server', 8),
    aria2_disk_cache: t('aria2_disk_cache') || '64M',
    aria2_file_allocation: t('aria2_file_allocation') || 'falloc',
    aria2_continue_downloads: c('aria2_continue_downloads'),
    aria2_lowest_speed_limit: t('aria2_lowest_speed_limit') || '0',
    db_type: t('db_type'),
    postgres_host: t('postgres_host'), postgres_port: n('postgres_port'),
    postgres_db: t('postgres_db'), postgres_user: t('postgres_user'),
    postgres_password: t('postgres_password'), postgres_schema: t('postgres_schema'),
    postgres_ssl: c('postgres_ssl'),
    postgres_application_name: t('postgres_application_name'),
    discord_username: t('discord_username') || 'AllDebrid-Client',
    discord_avatar_url: t('discord_avatar_url'),
    discord_webhook_url: t('discord_webhook_url'),
    discord_webhook_added: t('discord_webhook_added'),
    discord_notify_added: c('discord_notify_added'), discord_notify_finished: c('discord_notify_finished'),
    discord_notify_error: c('discord_notify_error'),
    discord_notify_update: c('discord_notify_update'),
    update_check_interval_hours: n('update_check_interval_hours', 12),
    sonarr_enabled: c('sonarr_enabled'), sonarr_url: t('sonarr_url'), sonarr_api_key: t('sonarr_api_key'),
    radarr_enabled: c('radarr_enabled'), radarr_url: t('radarr_url'), radarr_api_key: t('radarr_api_key'),
    torrent_labels: (t('torrent_labels_raw')||'').split(',').map(s=>s.trim()).filter(Boolean),
    stuck_download_timeout_hours: n('stuck_download_timeout_hours'),
    alldebrid_rate_limit_per_minute: n('alldebrid_rate_limit_per_minute'),
    full_sync_interval_minutes: n('full_sync_interval_minutes'),
    backup_enabled: c('backup_enabled'), backup_folder: t('backup_folder'),
    backup_interval_hours: n('backup_interval_hours'), backup_keep_days: n('backup_keep_days'),
    db_backup_enabled: c('db_backup_enabled'), db_backup_folder: t('db_backup_folder'),
    db_backup_keep_days: n('db_backup_keep_days'),
    db_wipe_enabled: c('db_wipe_enabled'), db_backup_before_wipe: c('db_backup_before_wipe'),
    blocked_extensions: l('blocked_extensions'), blocked_keywords: l('blocked_keywords'),
    min_file_size_mb: n('min_file_size_mb'), poll_interval_seconds: n('poll_interval_seconds', 30),
    watch_interval_seconds: n('watch_interval_seconds', 10),
    filters_enabled: c('filters_enabled'),
    aria2_deep_sync_interval_minutes: n('aria2_deep_sync_interval_minutes'),
    aria2_error_retry_count:           n('aria2_error_retry_count'),
      upload_fail_retry_count:         n('upload_fail_retry_count', 3),
      upload_fail_retry_delay_minutes: n('upload_fail_retry_delay_minutes', 5),
      extract_enabled:          c('extract_enabled'),
      extract_delete_archive:   c('extract_delete_archive', true),
      extract_max_concurrent:   n('extract_max_concurrent', 2),
      discord_notify_extract:   c('discord_notify_extract', true),
    aria2_error_retry_delay_seconds: n('aria2_error_retry_delay_seconds'),
    flexget_enabled: c('flexget_enabled'),
    flexget_url: t('flexget_url'),
    flexget_api_key: t('flexget_api_key'),
    flexget_tasks_raw: t('flexget_tasks_raw'),
    flexget_webhook_url: t('flexget_webhook_url'),
    flexget_task_schedules_json: serializeFlexgetTaskSchedules(),
    flexget_task_timeout_seconds: n('flexget_task_timeout_seconds'),
    flexget_retry_delay_minutes: n('flexget_retry_delay_minutes'),

    stats_snapshot_interval_minutes: n('stats_snapshot_interval_minutes'),
    stats_snapshot_keep_days: n('stats_snapshot_keep_days'),
    stats_report_interval_hours: n('stats_report_interval_hours'),
    // Jackett
    jackett_enabled:     c('jackett_enabled'),
    jackett_url:         t('jackett_url'),
    jackett_api_key:     t('jackett_api_key'),
    jackett_webhook_url: t('jackett_webhook_url'),
    stats_report_window_hours: reportWindowHours,
    stats_report_webhook_url: t('stats_report_webhook_url'),
  };
}

function toggleFilterFields() {
  const enabled = document.getElementById('s-filters_enabled')?.checked;
  const fields = document.getElementById('filter-fields');
  if (fields) {
    fields.style.opacity = enabled ? '' : '0.4';
    fields.style.pointerEvents = enabled ? '' : 'none';
  }
}

async function triggerFullSync() {
  try {
    const r = await api('POST', '/admin/full-sync');
    toast('Full sync: ' + r.updated + ' torrent(s) updated', r.updated > 0 ? 'success' : 'info');
    setTimeout(() => { loadStats(); loadRecent(); }, 1500);
  } catch(e) { toast(e.message, 'error'); }
}

async function saveSettings() {
  try {
    const activeTab = getActiveSettingsTab();
    const d = getFormSettings();
    await api('PUT','/settings',d);
    settingsData = await api('GET','/settings');
    // Re-render so defaults set by backend are immediately visible
    renderSettings();
    switchSettingsTab(activeTab);
    updateAria2ngLink();
    updateJackettNav();
    toast('Settings saved!','success');
    checkConnections();
    checkFlexgetRunning();
    // Sync Downloads panel — PUT /settings may have updated aria2 limits,
    // so reload them from aria2 to keep both views consistent.
    loadAria2SpeedLimit();
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
}

async function testDiscord() {
  try {
    const activeTab = getActiveSettingsTab();
    const current = getFormSettings();
    await api('PUT','/settings', current);
    settingsData = await api('GET','/settings');
    renderSettings();
    switchSettingsTab(activeTab);
    await api('POST','/settings/test-discord');
    toast('Discord notification sent ✓','success');
  } catch(e) { toast('Discord: '+e.message,'error'); }
}

async function testAD() {
  try {
    const r = await api('POST','/settings/test-alldebrid');
    toast(`AllDebrid: connected as ${r.username} ${r.isPremium?'(Premium)':'(Free)'}✓`,'success');
    setDot('api','ok',`AllDebrid: ${r.username}`);
    _updatePremiumLabel(r);
  } catch(e) { toast('AllDebrid: '+e.message,'error'); setDot('api','error','AllDebrid: error'); }
}

function _updatePremiumLabel(r) {
  const row = document.getElementById('premium-row');
  const lbl = document.getElementById('lbl-premium');
  if (!row || !lbl) return;
  if (!r || !r.isPremium) { row.style.display = 'none'; return; }
  // AllDebrid user object has premiumUntil as unix timestamp
  const until = r.premiumUntil || r.premium_until || 0;
  if (!until) { row.style.display = 'none'; return; }
  const d = new Date(until * 1000);
  const dd = String(d.getDate()).padStart(2,'0');
  const mm = String(d.getMonth()+1).padStart(2,'0');
  const yyyy = d.getFullYear();
  const days = Math.ceil((d - Date.now()) / 86400000);
  const daysLabel = days > 0 ? `${days} days` : 'expired';
  lbl.innerHTML = `Premium until ${dd}.${mm}.${yyyy} (${daysLabel})`;
  row.style.display = '';
}

function switchSettingsTab(id) {
  document.querySelectorAll('.stab').forEach(t => t.classList.toggle('active', t.dataset.tab === id));
  document.querySelectorAll('.stab-panel').forEach(p => p.classList.toggle('active', p.id === id));
  if (aria2DownloadsTimer) {
    clearInterval(aria2DownloadsTimer);
    aria2DownloadsTimer = null;
  }
  if (id === 'tab-advanced') {
    loadDatabaseBackupList();
  }
  if (id === 'tab-services') {
    flexgetListTasks();
    loadFlexgetHistory();
  }
  if (id === 'tab-download') {
    loadAria2Runtime().catch(()=>{});
    aria2DownloadsTimer = setInterval(() => {
      const panel = document.getElementById('tab-download');
      if (panel && panel.classList.contains('active')) loadAria2Downloads().catch(()=>{});
    }, 5000);
  }
}

async function testAria2() {
  try {
    const activeTab = getActiveSettingsTab();
    const current = getFormSettings();
    await api('PUT','/settings', current);
    settingsData = await api('GET','/settings');
    renderSettings();
    switchSettingsTab(activeTab);
    const r = await api('POST','/settings/test-aria2');
    renderAria2Diagnostics(r.diagnostics || null);
    toast(`aria2: ${r.version||'online'} ✓`,'success');
    setDot('aria2','ok',`aria2: ${r.version||'online'}`);
  } catch(e) {
    toast('aria2: '+e.message,'error');
    setDot('aria2','error','aria2: error');
  }
}

function renderAria2Diagnostics(diag) {
  const el = document.getElementById('aria2-memory-diagnostics');
  if (!el) return;
  if (!diag) {
    el.textContent = '';
    return;
  }
  const opts = diag.global_options || {};
  const limits = diag.query_limits || {};
  el.innerHTML =
    `<b>aria2 memory diagnostics</b><br>` +
    `Active: ${diag.active_count ?? 0} · Waiting: ${diag.waiting_count ?? 0} · Stopped: ${diag.stopped_count ?? 0}<br>` +
    `max-download-result: ${opts['max-download-result'] || 'n/a'} · keep-unfinished-download-result: ${opts['keep-unfinished-download-result'] || 'n/a'}<br>` +
    `query window — waiting: ${limits.waiting ?? 'n/a'} · stopped: ${limits.stopped ?? 'n/a'}`;
}

function renderAria2Runtime(data) {
  const el = document.getElementById('aria2-runtime-status');
  if (!el) return;
  if (!data) {
    el.textContent = 'Runtime status not loaded yet.';
    return;
  }
  const mode = data.mode || 'external';
  const state = data.running ? 'Running' : (mode === 'builtin' ? 'Stopped' : 'External');
  const rpc = data.rpc_ok ? 'RPC online' : (mode === 'builtin' ? 'RPC offline' : 'External RPC');
  const version = data.version ? ` · v${data.version}` : '';
  const uptime = data.uptime_seconds ? ` · uptime ${Math.floor(data.uptime_seconds / 60)}m` : '';
  const secret = data.secret_managed ? ' · internal secret managed' : '';
  const dir = data.download_dir ? `<br>Download folder: ${esc(data.download_dir)}` : '';
  const diag = data.diagnostics || {};
  const counts = diag && !diag.error
    ? `<br>Active: ${diag.active_count ?? 0} · Waiting: ${diag.waiting_count ?? 0} · Stopped: ${diag.stopped_count ?? 0}`
    : '';
  const err = data.last_error ? `<br><span style="color:var(--red)">${esc(data.last_error)}</span>` : '';
  el.innerHTML = `<b>${esc(state)}</b> · ${esc(mode)} · ${esc(rpc)}${esc(version)}${esc(uptime)}${secret}<br>${esc(data.rpc_url || '')}${counts}${err}`;
  el.innerHTML += dir;
  if (data.last_output) el.innerHTML += `<br><small>${esc(data.last_output)}</small>`;
  renderAria2Diagnostics(diag && !diag.error ? diag : null);
}

function aria2StatusLabel(status) {
  const map = {active:'Downloading', waiting:'Waiting', paused:'Paused', complete:'Complete', error:'Error', removed:'Removed'};
  const cls = status === 'active' ? 'downloading' : status === 'complete' ? 'completed' : status === 'error' ? 'error' : status === 'paused' ? 'paused' : 'queued';
  return `<span class="badge badge-${cls}">${map[status] || status || 'Unknown'}</span>`;
}

function renderAria2Downloads(data) {
  const el = document.getElementById('aria2-downloads');
  if (!el) return;
  if (!data || !Array.isArray(data.items)) {
    el.innerHTML = '<div class="empty">Queue not loaded yet.</div>';
    return;
  }
  const summary = data.summary || {};
  const items = data.items || [];
  const ordered = items.slice().sort((a,b) => {
    const weight = {active:0, waiting:1, paused:2, error:3, complete:4};
    return (weight[a.status] ?? 9) - (weight[b.status] ?? 9);
  });
  const header = `
    <div class="aria2-summary">
      <span class="aria2-chip">Active: ${summary.active ?? 0}</span>
      <span class="aria2-chip">Waiting: ${summary.waiting ?? 0}</span>
      <span class="aria2-chip">Stopped: ${summary.stopped ?? 0}</span>
      <span class="aria2-chip">Speed: ${fmtSpeed(summary.download_speed || 0)}</span>
      <span class="aria2-chip">Remaining: ${fmtSize(summary.remaining_length || 0)}</span>
    </div>`;
  if (!ordered.length) {
    el.innerHTML = header + '<div class="empty">No aria2 jobs currently visible.</div>';
    return;
  }
  el.innerHTML = header + ordered.map(job => {
    const canPause = job.status === 'active' || job.status === 'waiting';
    const canResume = job.status === 'paused';
    const files = (job.files || []).slice(0, 4).map(file => `
      <div title="${esc(file.path || '')}">
        ${esc(file.name || file.path || 'file')} · ${Math.max(0, file.progress || 0).toFixed(1)}% · ${fmtSize(file.completed_length || 0)} / ${fmtSize(file.length || 0)}
      </div>`).join('');
    const more = (job.files || []).length > 4 ? `<div>+ ${(job.files || []).length - 4} more file(s)</div>` : '';
    const error = job.error_message ? `<div class="aria2-error">${esc(job.error_code || '')} ${esc(job.error_message)}</div>` : '';
    return `
      <div class="aria2-job">
        <div class="aria2-job-top">
          <div class="aria2-job-title">
            <div class="aria2-job-name" title="${esc(job.name || '')}">${esc(job.name || job.gid || 'aria2 job')}</div>
            <div class="aria2-job-meta" title="${esc(job.path || '')}">${esc(job.gid || '')}${job.path ? ' · ' + esc(job.path) : ''}</div>
          </div>
          <div class="aria2-actions">
            ${canPause ? `<button class="btn btn-ghost btn-sm" onclick="aria2DownloadAction('${esc(job.gid)}','pause')">Pause</button>` : ''}
            ${canResume ? `<button class="btn btn-blue btn-sm" onclick="aria2DownloadAction('${esc(job.gid)}','resume')">Resume</button>` : ''}
            <button class="btn btn-danger btn-sm" onclick="aria2DownloadAction('${esc(job.gid)}','remove')">Remove</button>
          </div>
        </div>
        <div>${progress(job.progress || 0, job.status === 'complete' ? 'completed' : 'downloading')}</div>
        <div class="aria2-job-grid">
          <div><div class="aria2-k">Status</div><div class="aria2-v">${aria2StatusLabel(job.status)}</div></div>
          <div><div class="aria2-k">Speed</div><div class="aria2-v">${fmtSpeed(job.download_speed || 0)}</div></div>
          <div><div class="aria2-k">Done</div><div class="aria2-v">${fmtSize(job.completed_length || 0)} / ${fmtSize(job.total_length || 0)}</div></div>
          <div><div class="aria2-k">Remaining</div><div class="aria2-v">${fmtSize(job.remaining_length || 0)}</div></div>
        </div>
        ${error}
        ${(files || more) ? `<div class="aria2-file-list">${files}${more}</div>` : ''}
      </div>`;
  }).join('');
}

async function loadAria2Downloads() {
  try {
    const data = await api('GET', '/aria2/downloads');
    renderAria2Downloads(data);
    return data;
  } catch(e) {
    const el = document.getElementById('aria2-downloads');
    if (el) el.innerHTML = `<div class="aria2-error">Queue error: ${esc(e.message)}</div>`;
    throw e;
  }
}

async function aria2DownloadAction(gid, action) {
  try {
    await api('POST', `/aria2/downloads/${encodeURIComponent(gid)}/${action}`);
    toast(`aria2 ${action} sent`, 'success');
    await loadAria2Downloads();
    await loadAria2Runtime();
  } catch(e) {
    toast(`aria2 ${action}: ${e.message}`, 'error');
  }
}

async function loadAria2Runtime() {
  try {
    const data = await api('GET', '/aria2/runtime');
    renderAria2Runtime(data);
    loadAria2Downloads().catch(()=>{});
    const badge = document.getElementById('aria2-speed-badge');
    const dlEl  = document.getElementById('aria2-speed-dl');
    if (badge) {
      const isBuiltin = (data.mode||'')==='builtin' && data.running;
      if (!isBuiltin) {
        badge.style.display = 'none';
      } else {
        // Pre-seed from settingsData for instant first render,
        // then fetch live values from RPC
        if (settingsData) {
          _aria2BadgeState.limitBps = parseInt(settingsData.aria2_max_download_limit)||0;
          _aria2BadgeState.maxDl    = parseInt(settingsData.aria2_max_active_downloads)||3;
        }
        badge.style.display = 'flex';
        loadAria2SpeedLimit().catch(function(){});
      }
    }
    return data;
  } catch(e) {
    const el = document.getElementById('aria2-runtime-status');
    if (el) el.innerHTML = `<span style="color:var(--red)">Runtime error: ${esc(e.message)}</span>`;
    throw e;
  }
}

async function aria2RuntimeAction(action) {
  try {
    const current = getFormSettings();
    await api('PUT','/settings', current);
    settingsData = await api('GET','/settings');
    const data = await api('POST', `/aria2/runtime/${action}`);
    renderAria2Runtime(data);
    loadAria2Downloads().catch(()=>{});
    toast(`aria2 ${action} complete`, 'success');
  } catch(e) {
    toast(`aria2 ${action}: ${e.message}`, 'error');
    loadAria2Runtime().catch(()=>{});
  }
}

async function runAria2Housekeeping() {
  try {
    const current = getFormSettings();
    await api('PUT','/settings', current);
    settingsData = await api('GET','/settings');
    const r = await api('POST', '/settings/aria2-housekeeping');
    renderAria2Diagnostics(r.diagnostics || null);
    toast('aria2 cleanup finished', 'success');
  } catch(e) {
    toast(e.message, 'error');
  }
}

async function uploadDiscordAvatar(input) {
  const file = input.files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append('file', file);
  try {
    const resp = await fetch('/api/settings/upload-avatar', {method:'POST', body: formData});
    const data = await resp.json();
    if (!resp.ok) { toast(data.detail || 'Upload failed', 'error'); return; }
    // Discord requires a real HTTP URL, not a data URI
    // The server saves the file and returns the public URL
    document.getElementById('s-discord_avatar_url').value = data.url;
    showAvatarPreview(data.url, file.name, data.size_bytes);
    toast('Avatar uploaded — URL: ' + data.url, 'success');
    if (data.warning) toast(data.warning, 'warn');
  } catch(e) { toast(e.message, 'error'); }
  input.value = '';
}

function showAvatarPreview(src, name, bytes) {
  const preview = document.getElementById('avatar-preview');
  const img = document.getElementById('avatar-preview-img');
  const lbl = document.getElementById('avatar-preview-label');
  if (!preview) return;
  img.src = src;
  lbl.textContent = (name || 'Custom avatar') + (bytes > 0 ? ' (' + Math.round(bytes/1024) + ' KB)' : '');
  preview.style.display = 'flex';
}

function clearDiscordAvatar() {
  document.getElementById('s-discord_avatar_url').value = '';
  const preview = document.getElementById('avatar-preview');
  if (preview) preview.style.display = 'none';
}

async function testSonarr() {
  try {
    const r = await api('POST', '/settings/test-sonarr');
    toast(`Sonarr ${r.app_name} v${r.version} ✓`, 'success');
  } catch(e) { toast(e.message, 'error'); }
}

async function testRadarr() {
  try {
    const r = await api('POST', '/settings/test-radarr');
    toast(`Radarr ${r.app_name} v${r.version} ✓`, 'success');
  } catch(e) { toast(e.message, 'error'); }
}

async function runDeepSync() {
  try {
    toast('Running deep sync…', 'info');
    const r = await api('POST', '/admin/deep-sync');
    toast(`Deep sync done in ${r.elapsed_seconds}s ✓`, 'success');
    loadTorrents(); loadStats();
  } catch(e) { toast(e.message, 'error'); }
}

async function triggerBackup() {
  try {
    toast('Running backup…', 'info');
    const r = await api('POST', '/admin/backup');
    if (r.skipped) { toast('Backup disabled in settings', 'warn'); return; }
    toast(`Backup done: ${r.backed_up.join(', ')} (${r.rotated} old removed)`, 'success');
    loadBackupList();
  } catch(e) { toast(e.message, 'error'); }
}

async function loadBackupList() {
  try {
    const r = await api('GET', '/admin/backups');
    const el = document.getElementById('backup-list');
    if (!el) return;
    if (!r.backups.length) { el.textContent = 'No backups found.'; return; }
    el.innerHTML = r.backups.map(b =>
      `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)">
        <span>${b.name}</span>
        <span style="color:var(--text3)">${b.files.join(', ')} — ${Math.round(b.size_bytes/1024)} KB</span>
      </div>`
    ).join('');
  } catch(e) { toast(e.message, 'error'); }
}

async function triggerDatabaseBackup() {
  try {
    toast('Running database backup…', 'info');
    const r = await api('POST', '/admin/database/backup');
    if (r.skipped) { toast('Database backup disabled in settings', 'warn'); return; }
    toast(`Database backup done (${Object.values(r.tables || {}).reduce((a, b) => a + b, 0)} rows exported)`, 'success');
    loadDatabaseBackupList();
  } catch(e) { toast(e.message, 'error'); }
}

async function loadDatabaseBackupList() {
  try {
    const r = await api('GET', '/admin/database/backups');
    const el = document.getElementById('db-backup-list');
    if (!el) return;
    if (!r.backups.length) { el.textContent = 'No database backups found.'; return; }
    el.innerHTML = r.backups.map(b =>
      `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)">
        <span>${b.name}</span>
        <span style="color:var(--text3)">${b.files.join(', ')} — ${Math.round(b.size_bytes/1024)} KB</span>
      </div>`
    ).join('');
  } catch(e) { toast(e.message, 'error'); }
}

async function wipeDatabase() {
  const enabled = document.getElementById('s-db_wipe_enabled')?.checked;
  if (!enabled) { toast('Enable database wipe in settings first', 'warn'); return; }
  if (!confirm('This will remove all database rows. Continue?')) return;
  const confirmText = prompt('Type WIPE to confirm database wipe');
  if (confirmText !== 'WIPE') return;
  try {
    toast('Wiping database…', 'warn');
    const r = await api('POST', '/admin/database/wipe', {confirm: true});
    if (r.backup && !r.backup.skipped) {
      toast('Database wiped. Pre-wipe backup created.', 'success');
    } else {
      toast('Database wiped.', 'success');
    }
    loadDatabaseBackupList();
    loadStats().catch(()=>{});
    loadRecent().catch(()=>{});
    if (document.getElementById('view-torrents')?.classList.contains('active')) loadTorrents().catch(()=>{});
  } catch(e) { toast(e.message, 'error'); }
}

async function flexgetListTasks(syncToEditor = false) {
  const el = document.getElementById('flexget-task-list');
  if (el) el.textContent = '⏳ Loading tasks…';
  try {
    const r = await api('GET', '/flexget/tasks');
    flexgetAvailableTasks = Array.isArray(r.tasks) ? r.tasks : [];
    const datalist = document.getElementById('flexget-task-options');
    if (datalist) {
      datalist.innerHTML = flexgetAvailableTasks.map(task => `<option value="${task}"></option>`).join('');
    }
    if (syncToEditor) {
      const known = new Set(flexgetTaskSchedules.map(item => item.task));
      for (const task of flexgetAvailableTasks) {
        if (!known.has(task)) addFlexgetTaskSchedule(task);
      }
    }
    if (!el) return;
    if (!r.enabled) { el.textContent = 'FlexGet is not enabled.'; return; }
    el.innerHTML = flexgetAvailableTasks.length
      ? '<b>Available tasks (' + flexgetAvailableTasks.length + '):</b><br><div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px">'
        + flexgetAvailableTasks.map(t =>
            `<span style="display:inline-flex;align-items:center;gap:4px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:2px 6px;font-size:11px">
              <span>${t}</span>
              <button onclick="flexgetRunSingleTask('${t.replace(/'/g,"\\'")}')" title="Run ${t}" style="background:none;border:none;color:var(--accent);cursor:pointer;font-size:12px;padding:0 2px;line-height:1" onmouseover="this.style.color='var(--accent2)'" onmouseout="this.style.color='var(--accent)'">▶</button>
            </span>`
          ).join('')
        + '</div>'
      : 'No tasks found.';
  } catch(e) { if (el) el.textContent = '✗ ' + e.message; }
}

async function flexgetRun() {
  const resultEl = document.getElementById('flexget-run-result');
  resultEl.style.display = 'block';
  resultEl.style.color = 'var(--text2)';
  resultEl.textContent = '⏳ Running FlexGet tasks…';
  const fgRow = document.getElementById('flexget-running-row');
  if (fgRow) fgRow.style.display = 'flex';
  const raw = document.getElementById('s-flexget_tasks_raw')?.value || '';
  const tasks = raw.split(',').map(s=>s.trim()).filter(Boolean);
  try {
    const r = await api('POST', '/flexget/run', tasks.length ? {tasks} : {});
    const hasErr = r.tasks_error > 0;
    resultEl.style.color = hasErr ? 'var(--yellow)' : 'var(--green)';
    let msg = `✓ ${r.tasks_ok}/${r.tasks_total} tasks OK, ${r.tasks_error} error(s)`;
    if (hasErr && r.first_error) msg += ` — ${r.first_error}`;
    resultEl.textContent = msg;
    if (fgRow) { setTimeout(() => { if (fgRow) fgRow.style.display = 'none'; }, 2000); }
    if (btnEl) { btnEl.disabled = false; btnEl.textContent = '▶ Run'; }
    loadFlexgetHistory();
  } catch(e) {
    resultEl.style.color = 'var(--red)';
    resultEl.textContent = '✗ ' + e.message;
  }
}

async function loadFlexgetHistory() {
  const el = document.getElementById('flexget-history');
  if (!el) return;
  try {
    const r = await api('GET', '/flexget/history?limit=20');
    if (!r.runs.length) { el.textContent = 'No FlexGet runs yet.'; return; }
    el.innerHTML = r.runs.map(run => {
      const color = run.status === 'ok' ? 'var(--green)' : run.status === 'timeout' ? 'var(--yellow)' : 'var(--red)';
      return `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border);font-size:11px">
        <span style="color:${color}">●</span>
        <span style="flex:1;margin:0 8px">${run.task_name}</span>
        <span style="color:var(--text3)">${run.triggered_by}</span>
        <span style="color:var(--text3);margin-left:8px">${run.elapsed_seconds?.toFixed(1)}s</span>
        <span style="color:var(--text3);margin-left:8px">${(run.ran_at||'').slice(0,16)}</span>
      </div>`;
    }).join('');
  } catch(e) { el.textContent = '✗ ' + e.message; }
}



async function checkFlexgetRunning() {
  if (!settingsData.flexget_enabled) {
    const row = document.getElementById('flexget-running-row');
    if (row) row.style.display = 'none';
    return;
  }
  try {
    const r = await api('GET', '/flexget/running');
    const running = Array.isArray(r.running) && r.running.length > 0;
    const row = document.getElementById('flexget-running-row');
    const lbl = document.getElementById('lbl-flexget');
    if (row) row.style.display = running ? 'flex' : 'none';
    if (lbl && running) lbl.textContent = 'FlexGet: ' + r.running.join(', ') + '…';
  } catch { /* silent */ }
}

async function flexgetRunSingleTask(task, btnEl) {
  const resultEl = document.getElementById('flexget-run-result');
  resultEl.style.display = 'block';
  resultEl.style.color = 'var(--text2)';
  resultEl.textContent = `⏳ Running task: ${task}…`;
  if (btnEl) { btnEl.disabled = true; btnEl.textContent = '⏳'; }
  const fgRow = document.getElementById('flexget-running-row');
  if (fgRow) fgRow.style.display = 'flex';
  try {
    const r = await api('POST', `/flexget/run/${encodeURIComponent(task)}`);
    const ok = r.ok || r.status === 'ok';
    resultEl.style.color = ok ? 'var(--green)' : 'var(--yellow)';
    let msg = `${task}: ${ok ? '✓ ok' : '✗ error'}`;
    if (!ok && r.first_error) msg += ` — ${r.first_error}`;
    if (r.elapsed) msg += ` (${Number(r.elapsed).toFixed(1)}s)`;
    resultEl.textContent = msg;
    setTimeout(() => checkFlexgetRunning(), 1000);
    if (btnEl) { btnEl.disabled = false; btnEl.textContent = '▶ Run'; }
    loadFlexgetHistory();
  } catch(e) {
    const conflict = e.message && e.message.includes('409');
    resultEl.style.color = conflict ? 'var(--yellow)' : 'var(--red)';
    resultEl.textContent = conflict ? `⏳ ${task}: already running` : `✗ ${task}: ${e.message}`;
    setTimeout(() => checkFlexgetRunning(), 500);
    if (btnEl) { btnEl.disabled = false; btnEl.textContent = '▶ Run'; }
  }
}

async function sendStatsReport() {
  const hours = parseInt(document.getElementById('stats-report-hours')?.value || '24', 10);
  try {
    const r = await api('POST', `/stats/report/send?hours=${hours}`);
    toast(`Report sent via webhook (${r.hours}h) ✓`, 'success');
  } catch(e) {
    toast(e.message, 'error');
  }
}

async function loadComprehensiveStats() {
  const el = document.getElementById('comprehensive-stats');
  if (!el) return;
  const hours = parseInt(document.getElementById('stats-report-hours')?.value || '24');
  el.innerHTML = '<div style="color:var(--text2);font-size:12px">⏳ Loading…</div>';
  try {
    const r = await api('GET', `/stats/comprehensive?hours=${hours}`);
    const t = r.torrents || {};
    const d = r.downloads || {};
    const f = r.files || {};
    const ev = r.events || {};
    const fmtBytes = b => b > 1e9 ? (b/1e9).toFixed(2)+' GB' : b > 1e6 ? (b/1e6).toFixed(1)+' MB' : (b/1024).toFixed(0)+' KB';
    const fmtDur = s => s > 3600 ? `${(s/3600).toFixed(1)}h` : s > 60 ? `${Math.floor(s/60)}m` : s+'s';
    el.innerHTML = `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px">
        ${[
          ['Total Torrents', t.total||0, ''],
          ['Completed', t.completed||0, 'var(--green)'],
          ['Errors', t.errors||0, 'var(--red)'],
          ['Success Rate', t.success_rate_pct != null ? t.success_rate_pct+'%' : '—', 'var(--accent)'],
          ['Downloaded', fmtBytes(d.total_bytes||0), 'var(--blue)'],
          ['Avg Size', fmtBytes(d.avg_bytes||0), ''],
          ['Avg Duration', fmtDur(d.avg_duration_sec||0), ''],
          ['Total Files', f.total||0, ''],
          ['Blocked Files', f.blocked||0, 'var(--yellow)'],
          ['Total Retries', f.retry_total||0, ''],
          ['Error Events', ev.error||0, 'var(--red)'],
          ['Warn Events', ev.warn||0, 'var(--yellow)'],
        ].map(([k,v,c]) => `<div style="background:var(--surface2);padding:8px 10px;border-radius:6px">
          <div style="font-size:9px;text-transform:uppercase;color:var(--text2);font-weight:700">${k}</div>
          <div style="font-size:20px;font-weight:800;color:${c||'var(--text)'}">${v}</div>
        </div>`).join('')}
      </div>
      ${r.daily_trend?.length ? `<div style="font-size:11px;color:var(--text2);margin-top:8px"><b>Daily completions (last ${Math.min(14, hours/24|0)} days):</b><br>${r.daily_trend.map(d=>`${d.date}: ${d.cnt}`).join(' · ')}</div>` : ''}
      ${Object.keys(t.sources||{}).length ? `<div style="font-size:11px;color:var(--text2);margin-top:6px"><b>Sources:</b> ${Object.entries(t.sources).map(([k,v])=>`${k}: ${v}`).join(', ')}</div>` : ''}
    `;
  } catch(e) {
    el.innerHTML = `<span style="color:var(--red)">✗ ${e.message}</span>`;
  }
}

async function exportStats() {
  const hours = parseInt(document.getElementById('stats-report-hours')?.value || '24');
  window.open(`/api/stats/export?hours=${hours}`, '_blank');
}

async function triggerStatsSnapshot() {
  try {
    await api('POST', '/stats/snapshot');
    toast('Stats snapshot taken', 'success');
  } catch(e) { toast(e.message, 'error'); }
}

async function runMigration(direction, dryRun) {
  const resultEl = document.getElementById('migration-result');
  resultEl.style.display = 'block';
  resultEl.style.color = 'var(--text2)';
  resultEl.textContent = '⏳ Running migration…';
  try {
    const r = await api('POST', '/admin/migrate', { direction, dry_run: dryRun, force: true });
    resultEl.style.color = 'var(--green)';
    resultEl.textContent = '✓ ' + (r.summary || 'Migration complete');
    if (!dryRun) {
      setTimeout(() => { loadStats(); loadRecent(); }, 1000);
    }
  } catch(e) {
    resultEl.style.color = 'var(--red)';
    resultEl.textContent = '✗ ' + e.message;
  }
}

async function testPostgres() {
  try {
    const r = await api('POST', '/settings/test-postgres');
    toast(`PostgreSQL ${r.version} — ${r.host}:${r.port}/${r.database} ✓`, 'success');
  } catch(e) { toast(e.message, 'error'); }
}

// ── Init ───────────────────────────────────────────────────────────────────
(async()=>{
  // ── Debug helper — shows status in UI (removed in production) ──────────────
  function dbg(msg) {
    const el = document.getElementById('debug-status');
    if (!el) return;
    el.style.display = 'block';
    el.innerHTML += '<div>' + new Date().toLocaleTimeString() + ' — ' + msg + '</div>';
  }

  dbg('Script gestartet');
  setDot('api',   'check', 'AllDebrid: checking…');
  setDot('aria2', 'check', 'aria2: checking…');
  setDot('db',    'check', 'DB: checking…');

  // Load settings
  dbg('Lade Settings…');
  try {
    settingsData = await api('GET', '/settings');
    dbg('Settings OK');
  } catch(e) {
    dbg('Settings ERROR: ' + e.message);
  }
  renderTopbarActions();
  updateAria2ngLink();
  updateJackettNav();

  // Load stats with visible retry
  dbg('Starte loadStats…');
  let statsLoaded = false;
  let statsAttempt = 0;
  while (!statsLoaded) {
    statsAttempt++;
    dbg('loadStats Versuch ' + statsAttempt);
    statsLoaded = await loadStats();
    if (!statsLoaded) {
      const delay = Math.min(400 + statsAttempt * 400, 3000);
      dbg('Error — retrying in ' + delay + 'ms…');
      await new Promise(r => setTimeout(r, delay));
      if (statsAttempt >= 10) { dbg('Aufgegeben nach 10 Versuchen'); break; }
    }
  }
  // Hintergrund-Tasks sofort starten — nicht auf Stats warten
  loadRecent().catch(() => {});
  checkConnections().catch(() => {});   // setzt aria2-Dot
  checkPremiumStatus().catch(() => {});

  if (statsLoaded) {
    dbg('Stats loaded ✓');
    setTimeout(() => { const el = document.getElementById('debug-status'); if (el) el.style.display = 'none'; }, 5000);
  } else {
    dbg('Stats failed to load. Please reload the page.');
    setDot('api', 'error', 'AllDebrid: Error');
  }

  setInterval(checkPremiumStatus, 12 * 60 * 60 * 1000);
  setInterval(checkFlexgetRunning, 10000);
  checkFlexgetRunning();

  // ── Server-Sent Events — live updates without 15 s polling ──────────────
  // Falls back to polling if SSE is unavailable (proxy, browser quirk, etc.)
  (function initSSE() {
    if (typeof EventSource === 'undefined') return startPolling();
    var es;
    var sseOk = false;
    var fallbackTimer = null;

    function connect() {
      try {
        es = new EventSource('/api/events/stream');
        es.addEventListener('connected', function() {
          sseOk = true;
          if (fallbackTimer) { clearInterval(fallbackTimer); fallbackTimer = null; }
        });
        es.addEventListener('stats_changed', function() {
          loadStats().catch(()=>{});
          if (document.getElementById('view-dashboard')?.classList.contains('active')) loadRecent().catch(()=>{});
        });
        es.addEventListener('torrent_updated', function(e) {
          if (document.getElementById('view-torrents')?.classList.contains('active')) loadTorrents().catch(()=>{});
          if (document.getElementById('view-dashboard')?.classList.contains('active')) loadRecent().catch(()=>{});
          loadStats().catch(()=>{});
        });
        es.addEventListener('ping', function() {});
        es.onerror = function() {
          if (!sseOk) startPolling();
          es.close();
          setTimeout(connect, 10000); // reconnect after 10 s
        };
      } catch(err) {
        startPolling();
      }
    }

    function startPolling() {
      if (fallbackTimer) return;
      fallbackTimer = setInterval(()=>{
        loadStats().catch(()=>{});
        if (document.getElementById('view-dashboard')?.classList.contains('active')) loadRecent().catch(()=>{});
        if (document.getElementById('view-torrents')?.classList.contains('active')) loadTorrents().catch(()=>{});
      }, 15000);
    }

    connect();
    // Still refresh stats every 60 s as a safety net even with SSE
    setInterval(()=>{ loadStats().catch(()=>{}); }, 60000);
  })();
  setInterval(()=>checkConnections().catch(()=>{}), 60000);
})();


// ── Jackett Search ────────────────────────────────────────────────────────────

window._jackettResults = [];
window._jackettSort = { key: null, direction: null };
window._jackettAddInFlight = {};

function jackettSelectedTrackers() {
  var sel = document.getElementById('jackett-tracker');
  if (!sel) return [];
  return Array.from(sel.selectedOptions).map(function(o){return o.value;}).filter(Boolean);
}

var _idxSelected = new Set();

function idxToggle(e) {
  e.stopPropagation();
  var dd = document.getElementById('idx-dropdown');
  if (dd) dd.classList.toggle('open');
}
function idxClose() {
  var dd = document.getElementById('idx-dropdown');
  if (dd) dd.classList.remove('open');
}
function idxAllChanged(cb) {
  if (cb.checked) {
    _idxSelected.clear();
    document.querySelectorAll('.idx-item-cb').forEach(function(c){c.checked=false;});
  }
  cb.checked = true;
  idxPickerCommit();
}
function idxItemChanged(cb, id) {
  var allCb = document.getElementById('idx-all-cb');
  if (cb.checked) {
    _idxSelected.add(id);
    if (allCb) allCb.checked = false;
  } else {
    _idxSelected.delete(id);
    if (_idxSelected.size === 0 && allCb) allCb.checked = true;
  }
  idxPickerCommit();
}
function idxPickerCommit() {
  var sel = document.getElementById('jackett-tracker');
  var trigger = document.getElementById('idx-trigger');
  var placeholder = document.getElementById('idx-placeholder');
  if (!sel || !trigger) return;
  while (sel.options.length) sel.remove(0);
  trigger.querySelectorAll('.idx-chip').forEach(function(c){c.remove();});
  var names = {};
  document.querySelectorAll('.idx-item-cb').forEach(function(c){
    names[c.dataset.id] = c.dataset.name || c.dataset.id;
  });
  if (_idxSelected.size === 0) {
    if (placeholder) { placeholder.textContent = 'All Indexers'; placeholder.style.display = ''; }
  } else {
    if (placeholder) placeholder.style.display = 'none';
    var arrowEl = trigger.querySelector('.idx-arrow');
    _idxSelected.forEach(function(id) {
      var opt = document.createElement('option');
      opt.value = id; opt.selected = true;
      sel.appendChild(opt);
      var chip = document.createElement('span');
      chip.className = 'idx-chip';
      var nm = document.createElement('span');
      nm.textContent = names[id] || id;
      var cl = document.createElement('span');
      cl.className = 'idx-chip-close';
      cl.innerHTML = '&#215;';
      cl.onclick = (function(xid){ return function(ev){ ev.stopPropagation(); idxRemoveChip(xid); }; })(id);
      chip.appendChild(nm); chip.appendChild(cl);
      trigger.insertBefore(chip, arrowEl || null);
    });
  }
}
function idxRemoveChip(id) {
  _idxSelected.delete(id);
  var cb = document.querySelector('.idx-item-cb[data-id="'+id+'"]');
  if (cb) cb.checked = false;
  var allCb = document.getElementById('idx-all-cb');
  if (_idxSelected.size === 0 && allCb) allCb.checked = true;
  idxPickerCommit();
}

function jackettSortResults(items) {
  var sorted = (items || []).slice();
  var byText = function(value) { return String(value || '').toLowerCase(); };
  var byDate = function(value) { return String(value || ''); };
  var mode = window._jackettSort || { key: null, direction: null };
  var composite = mode.key && mode.direction ? (mode.key + '_' + mode.direction) : '';
  switch (composite) {
    case 'title_asc':
      sorted.sort(function(a, b) { return byText(a.title).localeCompare(byText(b.title)); });
      break;
    case 'title_desc':
      sorted.sort(function(a, b) { return byText(b.title).localeCompare(byText(a.title)); });
      break;
    case 'indexer_asc':
      sorted.sort(function(a, b) { return byText(a.indexer).localeCompare(byText(b.indexer)); });
      break;
    case 'indexer_desc':
      sorted.sort(function(a, b) { return byText(b.indexer).localeCompare(byText(a.indexer)); });
      break;
    case 'size_desc':
      sorted.sort(function(a, b) { return (b.size_bytes || 0) - (a.size_bytes || 0); });
      break;
    case 'size_asc':
      sorted.sort(function(a, b) { return (a.size_bytes || 0) - (b.size_bytes || 0); });
      break;
    case 'seeders_asc':
      sorted.sort(function(a, b) { return (a.seeders || 0) - (b.seeders || 0); });
      break;
    case 'date_asc':
      sorted.sort(function(a, b) { return byDate(a.pub_date).localeCompare(byDate(b.pub_date)); });
      break;
    case 'date_desc':
      sorted.sort(function(a, b) { return byDate(b.pub_date).localeCompare(byDate(a.pub_date)); });
      break;
    case 'peers_asc':
      sorted.sort(function(a, b) { return (a.leechers || 0) - (b.leechers || 0); });
      break;
    case 'peers_desc':
      sorted.sort(function(a, b) { return (b.leechers || 0) - (a.leechers || 0); });
      break;
    case 'seeders_desc':
      sorted.sort(function(a, b) {
        var seederDiff = (b.seeders || 0) - (a.seeders || 0);
        return seederDiff !== 0 ? seederDiff : byText(a.title).localeCompare(byText(b.title));
      });
      break;
    default:
      break;
  }
  return sorted;
}

function jackettUpdateSortIndicators() {
  ['title', 'indexer', 'size', 'seeders', 'peers', 'date'].forEach(function(key) {
    var el = document.getElementById('jackett-sort-' + key);
    if (!el) return;
    if (window._jackettSort.key === key && window._jackettSort.direction === 'asc') {
      el.textContent = '↑';
    } else if (window._jackettSort.key === key && window._jackettSort.direction === 'desc') {
      el.textContent = '↓';
    } else {
      el.textContent = '';
    }
  });
}

function jackettCycleSort(key) {
  var defaults = {
    title: 'asc',
    indexer: 'asc',
    size: 'desc',
    seeders: 'desc',
    peers: 'desc',
    date: 'desc'
  };
  if (window._jackettSort.key !== key) {
    window._jackettSort = { key: key, direction: defaults[key] || 'asc' };
  } else if (window._jackettSort.direction === (defaults[key] || 'asc')) {
    window._jackettSort = { key: key, direction: (defaults[key] === 'asc' ? 'desc' : 'asc') };
  } else {
    window._jackettSort = { key: null, direction: null };
  }
  renderJackettResults();
}

function renderJackettResults() {
  var wrap = document.getElementById('jackett-results-wrap');
  var tbody = document.getElementById('jackett-tbody');
  var count = document.getElementById('jackett-result-count');
  var state = document.getElementById('jackett-state');
  if (!tbody || !wrap) return;

  var results = jackettSortResults(window._jackettResults || []);
  jackettUpdateSortIndicators();

  // Reset selection on re-render
  window._jackettSelected = new Set();
  jackettUpdateBulkBar();

  tbody.innerHTML = '';
  if (!results.length) {
    wrap.style.display = 'none';
    if (count) count.textContent = '';
    if (state) { state.style.display = 'block'; state.textContent = 'No results found.'; }
    return;
  }

  results.forEach(function(r, i) {
    var tr = document.createElement('tr');
    tr.style.cursor = 'pointer';
    var sc = r.seeders > 10 ? 'var(--green)' : r.seeders > 0 ? 'var(--accent)' : 'var(--text3)';
    var key = String(r.hash || r.torrent_url || (r.title + '|' + r.indexer));
    var inFlight = !!(window._jackettAddInFlight && window._jackettAddInFlight[key]);
    var statusBadge = r.already_added
        ? '<span class="badge badge-'+esc((r.existing_status || 'queued'))+'">'+esc(r.existing_status || 'added')+'</span>'
        : '<span style="font-size:11px;color:var(--text3)">New</span>';
    var addBtn = '';
    if (!(r.magnet || r.torrent_url)) {
        addBtn = '<span style="font-size:11px;color:var(--text3)">No link</span>';
    } else if (r.already_added) {
        addBtn = '<button class="btn btn-ghost btn-sm" disabled id="jbtn-'+i+'">Added</button>';
    } else if (inFlight) {
        addBtn = '<button class="btn btn-ghost btn-sm" disabled id="jbtn-'+i+'">Adding...</button>';
    } else {
        addBtn = '<button class="btn btn-primary btn-sm" onclick="jackettAdd('+i+');event.stopPropagation()" id="jbtn-'+i+'">Add</button>';
    }
    var canSelect = !!(r.magnet || r.torrent_url) && !r.already_added;
    var chk = canSelect
      ? '<input type="checkbox" class="jck" data-idx="'+i+'" style="cursor:pointer;accent-color:var(--accent)" onclick="event.stopPropagation()" onchange="jackettRowCheck('+i+',this.checked)">'
      : '';
    tr.innerHTML =
      '<td style="padding:0 6px;width:34px">'+chk+'</td>'+
      '<td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(r.title)+'">'+esc(r.title)+'</td>'+
      '<td style="color:var(--text2);font-size:12px">'+esc(r.indexer)+'</td>'+
      '<td style="white-space:nowrap;font-size:12px">'+esc(r.size_human)+'</td>'+
      '<td style="text-align:center;color:'+sc+';font-weight:600">'+(r.seeders || 0)+'</td>'+
      '<td style="text-align:center;color:var(--text2)">'+(r.leechers || 0)+'</td>'+
      '<td style="font-size:11px;color:var(--text2)">'+esc(r.pub_date)+'</td>'+
      '<td>'+statusBadge+'</td>'+
      '<td>'+addBtn+'</td>';
    // Row click → toggle selection
    tr.addEventListener('click', function(e) {
      if (!canSelect) return;
      if (e.target.tagName === 'BUTTON' || e.target.tagName === 'INPUT') return;
      jackettRowCheck(i, null, true); // null = toggle
    });
    tbody.appendChild(tr);
  });
  wrap.style.display = '';
  if (state) state.style.display = 'none';
  if (count) count.textContent = results.length + ' result(s)';
}

// ── Bulk selection ───────────────────────────────────────────────────────────
window._jackettSelected = new Set();

function jackettRowCheck(idx, checked, toggle) {
  if (toggle) checked = !window._jackettSelected.has(idx);
  if (checked) window._jackettSelected.add(idx);
  else window._jackettSelected.delete(idx);
  // Sync checkbox element
  var chk = document.querySelector('.jck[data-idx="'+idx+'"]');
  if (chk) chk.checked = checked;
  jackettUpdateBulkBar();
}

function jackettUpdateBulkBar() {
  var bar = document.getElementById('jackett-bulk-bar');
  var lbl = document.getElementById('jackett-sel-count');
  var selCount = window._jackettSelected ? window._jackettSelected.size : 0;
  if (bar) bar.style.display = selCount > 0 ? 'flex' : 'none';
  if (lbl) lbl.textContent = selCount + ' selected';
  // Sync header checkbox indeterminate state
  var allChks = document.querySelectorAll('.jck');
  var masterChk = document.getElementById('jackett-check-all');
  if (masterChk && allChks.length) {
    masterChk.checked = selCount === allChks.length;
    masterChk.indeterminate = selCount > 0 && selCount < allChks.length;
  }
}

function jackettToggleAllCheckboxes(checked) {
  document.querySelectorAll('.jck').forEach(function(chk) {
    var idx = parseInt(chk.dataset.idx, 10);
    chk.checked = checked;
    if (checked) window._jackettSelected.add(idx);
    else window._jackettSelected.delete(idx);
  });
  jackettUpdateBulkBar();
}

function jackettSelectAll() {
  jackettToggleAllCheckboxes(true);
  var masterChk = document.getElementById('jackett-check-all');
  if (masterChk) masterChk.checked = true;
}

function jackettClearSelection() {
  jackettToggleAllCheckboxes(false);
  var masterChk = document.getElementById('jackett-check-all');
  if (masterChk) { masterChk.checked = false; masterChk.indeterminate = false; }
}

async function jackettAddSelected() {
  var results = jackettSortResults(window._jackettResults || []);
  var indices = Array.from(window._jackettSelected || []).sort(function(a,b){return a-b;});
  if (!indices.length) return;
  for (var i = 0; i < indices.length; i++) {
    await jackettAdd(indices[i]);
  }
  jackettClearSelection();
}

async function jackettAddAll() {
  var results = jackettSortResults(window._jackettResults || []);
  var addable = [];
  results.forEach(function(r, i) {
    if ((r.magnet || r.torrent_url) && !r.already_added) addable.push(i);
  });
  if (!addable.length) { toast('Nothing new to add', 'warn'); return; }
  toast('Adding ' + addable.length + ' result(s)…', 'info');
  for (var i = 0; i < addable.length; i++) {
    await jackettAdd(addable[i]);
  }
  toast('Added ' + addable.length + ' result(s)', 'success');
}

// ── Genre chip state ─────────────────────────────────────────────────────────
var _genreChips = new Set();

function toggleSearchAdv(btn) {
  // No longer used — filters always visible
  onSearchTypeChange();
}

function onSearchTypeChange() {
  var t = (document.getElementById('jackett-search-type')||{value:'search'}).value;
  var showImdb   = t === 'movie' || t === 'tvsearch';
  var showYear   = t === 'movie' || t === 'tvsearch';
  var showSeason = t === 'tvsearch';
  var showGenre  = t !== 'search';
  var setDisplay = function(id, show) {
    var el = document.getElementById(id);
    if (el) el.style.display = show ? 'flex' : 'none';
  };
  setDisplay('adv-genre-wrap',  showGenre);
  setDisplay('adv-imdbid-wrap', showImdb);
  setDisplay('adv-year-wrap',   showYear);
  setDisplay('adv-season-wrap', showSeason);
  setDisplay('adv-ep-wrap',     showSeason);
}

function addGenreChip() {
  var inp = document.getElementById('jackett-genre');
  if (!inp) return;
  var val = inp.value.trim().toLowerCase();
  if (!val) return;
  _genreChips.add(val);
  inp.value = '';
  renderGenreChips();
}

function updateGenreChips() {
  var inp = document.getElementById('jackett-genre');
  if (!inp || !inp.value.includes(',')) return;
  inp.value.split(',').forEach(function(v) {
    v = v.trim().toLowerCase();
    if (v) _genreChips.add(v);
  });
  inp.value = '';
  renderGenreChips();
}

function renderGenreChips() {
  var container = document.getElementById('genre-chips');
  if (!container) return;
  container.innerHTML = '';
  _genreChips.forEach(function(tag) {
    var chip = document.createElement('span');
    chip.className = 'search-tag-chip active';
    chip.title = 'Click to remove';
    var nm = document.createElement('span');
    nm.textContent = tag;
    var cl = document.createElement('span');
    cl.className = 'chip-x';
    cl.innerHTML = '&#215;';
    cl.onclick = (function(t){ return function(){ removeGenreChip(t); }; })(tag);
    chip.appendChild(nm); chip.appendChild(cl);
    chip.onclick = function(e){ if(e.target !== cl) removeGenreChip(tag); };
    container.appendChild(chip);
  });
}

function removeGenreChip(tag) {
  _genreChips.delete(tag);
  renderGenreChips();
}

function clearSearchAdv() {
  _genreChips.clear();
  renderGenreChips();
  ['jackett-imdbid','jackett-year','jackett-season','jackett-ep','jackett-genre',
   'jackett-query','jackett-cat','jackett-availability'].forEach(function(id) {
    var el = document.getElementById(id);
    if (!el) return;
    if (el.tagName === 'SELECT') el.selectedIndex = 0;
    else el.value = '';
  });
  var t = document.getElementById('jackett-search-type');
  if (t) { t.value = 'search'; onSearchTypeChange(); }
  _idxSelected.clear();
  var allCb = document.getElementById('idx-all-cb');
  if (allCb) allCb.checked = true;
  idxPickerCommit();
}

async function jackettSearch() {
  var query = (document.getElementById('jackett-query')||{value:''}).value.trim();
  if (!query) { toast('Please enter a search query', 'warn'); return; }
  var cat          = ((document.getElementById('jackett-cat')||{}).value)||'0';
  var trackers     = jackettSelectedTrackers();
  var availability = ((document.getElementById('jackett-availability')||{}).value)||'all';
  var hideDead     = availability === 'alive';
  var searchType   = (document.getElementById('jackett-search-type')||{value:'search'}).value || 'search';
  var genreVal     = _genreChips.size > 0
    ? Array.from(_genreChips).join(',')
    : ((document.getElementById('jackett-genre')||{value:''}).value.trim());
  var imdbid  = ((document.getElementById('jackett-imdbid')||{value:''}).value.trim());
  var year    = ((document.getElementById('jackett-year')||{value:''}).value.trim());
  var season  = ((document.getElementById('jackett-season')||{value:''}).value.trim());
  var epNum   = ((document.getElementById('jackett-ep')||{value:''}).value.trim());

  var btn   = document.getElementById('jackett-search-btn');
  var state = document.getElementById('jackett-state');
  var wrap  = document.getElementById('jackett-results-wrap');
  var tbody = document.getElementById('jackett-tbody');
  var count = document.getElementById('jackett-result-count');

  if (btn)   { btn.disabled=true; btn.textContent='Searching…'; }
  if (state) { state.style.display='block'; state.textContent='Searching…'; }
  if (wrap)  wrap.style.display='none';
  if (tbody) tbody.innerHTML='';
  // Show a "still searching" hint after 8s (Jackett with many indexers can take 30s+)
  var _slowHint = setTimeout(function() {
    if (state && state.style.display !== 'none' && (state.textContent||'').startsWith('Search')) {
      state.innerHTML = 'Searching… <span style="color:var(--text3);font-size:11px">(Jackett is querying all indexers — this can take up to 60s)</span>';
    }
  }, 8000);

  try {
    var payload = {
      query:query, category:parseInt(cat,10), trackers:trackers,
      hide_dead:hideDead, search_type:searchType,
    };
    if (genreVal) payload.genre  = genreVal;
    if (imdbid)   payload.imdbid = imdbid;
    if (year)     payload.year   = year;
    if (season)   payload.season = season;
    if (epNum)    payload.ep     = epNum;

    var data = await api('POST', '/jackett/search', payload, 150000); // 150s — Jackett with many indexers can take 60–90s
    if (data.error) {
      if (state) { state.style.display='block'; state.textContent='Error: '+data.error; }
      return;
    }
    if (!data.results || !data.results.length) {
      var hint = searchType !== 'search' ? ' (indexer may not support '+searchType+' mode)' : '';
      if (state) { state.style.display='block'; state.textContent='No results for: '+query+hint; }
      return;
    }
    var parts = ['"'+query+'"'];
    if (searchType !== 'search') parts.push('mode: '+searchType);
    if (genreVal)  parts.push('genre: '+genreVal);
    if (imdbid)    parts.push('IMDb: '+imdbid);
    if (year)      parts.push(year);
    if (hideDead)  parts.push('seeded only');
    if (count) count.textContent = data.total+' result(s) for '+parts.join(' · ');
    window._jackettResults = data.results;
    renderJackettResults();
  } catch(e) {
    if (state) { state.style.display='block'; state.textContent='Error: '+e.message; }
  } finally {
    clearTimeout(_slowHint);
    if (btn) { btn.disabled=false; btn.textContent='Search'; }
  }
}

async function jackettAdd(idx) {
  var r = jackettSortResults(window._jackettResults || [])[idx];
  if (!r) return;
  var key = String(r.hash || r.torrent_url || (r.title + '|' + r.indexer));
  if (window._jackettAddInFlight && window._jackettAddInFlight[key]) return;
  window._jackettAddInFlight[key] = true;
  var btn=document.getElementById('jbtn-'+idx);
  if (btn) { btn.disabled=true; btn.textContent='Adding...'; }
  try {
      var row=await api('POST', '/jackett/add', {
        hash:r.hash, magnet:r.magnet, torrent_url:r.torrent_url,
        title:r.title, indexer:r.indexer, size_bytes:r.size_bytes
      }, 60000);
    (window._jackettResults || []).forEach(function(item) {
      if ((item.hash && item.hash === r.hash) || (item.title === r.title && item.indexer === r.indexer)) {
        item.already_added = true;
        item.existing_torrent_id = row && row.id ? row.id : null;
        item.existing_status = row && row.status ? row.status : 'uploading';
      }
    });
    if (btn) {
      btn.textContent='\u2705 Added';
      btn.style.background='var(--green)';
      btn.style.color='#fff';
      }
      delete window._jackettAddInFlight[key];
      renderJackettResults();
    var label = row && row.added_via ? row.added_via.replace(/_/g, ' ') : (r.torrent_url ? 'torrent file' : 'magnet');
    var adId = (row && row.alldebrid_id) ? ' (ID: '+row.alldebrid_id+')' : '';
    toast('\u2714 Queued via '+label+adId+'\n'+r.title.slice(0,50), 'success');
    // Badge aktualisieren
    if (typeof loadStats === 'function') loadStats().catch(function(){});
    } catch(e) {
      delete window._jackettAddInFlight[key];
      if (btn) { btn.disabled=false; btn.textContent='Add'; }
      renderJackettResults();
      var _errMsg = e.message || 'Unknown error';
      toast('\u274c Failed to add: ' + sanitizeErrorMsg(_errMsg), 'error');
    }
  }

async function testProwlarr() {
  var el = document.getElementById('prowlarr-test-result');
  if (el) el.textContent = 'Testing…';
  try {
    await api('POST','/prowlarr/test');
    if (el) { el.textContent = '✓ Connected'; el.style.color = 'var(--green)'; }
  } catch(e) {
    if (el) { el.textContent = '✗ ' + sanitizeErrorMsg(e.message); el.style.color = 'var(--red)'; }
  }
}

async function testJackett() {
  var el=document.getElementById('jackett-test-result');
  if (el) { el.style.color='var(--text2)'; el.textContent='Testing...'; }
  try {
    const activeTab = getActiveSettingsTab();
    const current = getFormSettings();
    await api('PUT','/settings', current);
    settingsData = await api('GET','/settings');
    renderSettings();
    switchSettingsTab(activeTab);
    updateJackettNav();
    el=document.getElementById('jackett-test-result');
    if (el) { el.style.color='var(--text2)'; el.textContent='Testing...'; }
    var r=await api('POST', '/settings/test-jackett');
    if (el) { el.style.color='var(--green)'; el.textContent='Connected - Jackett '+(r.version||''); }
  } catch(e) {
    if (el) { el.style.color='var(--red)'; el.textContent='Error: '+e.message; }
  }
}

async function loadJackettIndexers() {
  try {
    var items = await api('GET', '/jackett/indexers', null, 15000);
    var dd = document.getElementById('idx-dropdown');
    if (!dd) return;
    dd.querySelectorAll('.idx-item').forEach(function(el){el.remove();});
    (items||[]).forEach(function(item) {
      var id = String(item.id||item.name||'');
      var name = String(item.name||item.id||'');
      var lbl = document.createElement('label');
      lbl.className = 'idx-option idx-item';
      var cb = document.createElement('input');
      cb.type='checkbox'; cb.className='idx-item-cb';
      cb.dataset.id=id; cb.dataset.name=name;
      cb.checked = _idxSelected.has(id);
      cb.onchange = (function(xcb,xid){return function(){idxItemChanged(xcb,xid);};})(cb,id);
      var sp = document.createElement('span');
      sp.textContent = name;
      lbl.appendChild(cb); lbl.appendChild(sp);
      dd.appendChild(lbl);
    });
    idxPickerCommit();
  } catch(e) { /* indexers optional */ }
}

function initSearchView() {
  var cfg=settingsData||{};
  var notConf=document.getElementById('jackett-not-configured');
  var searchBar=document.getElementById('jackett-search-bar');
  if (!cfg.jackett_enabled || !cfg.jackett_url || !cfg.jackett_api_key) {
    if (notConf) notConf.style.display='';
    if (searchBar) searchBar.style.display='none';
    return;
  }
  if (notConf) notConf.style.display='none';
  if (searchBar) searchBar.style.display='flex';
  onSearchTypeChange();
  loadJackettIndexers();
  setTimeout(function(){ var q=document.getElementById('jackett-query'); if(q)q.focus(); }, 100);
}

function updateJackettNav() {
  var cfg=settingsData||{};
  var el=document.querySelector('[data-view="search"]');
  if (el) el.style.display=cfg.jackett_enabled?'':'none';
}


// ── Downloads View (aria2 Queue) ─────────────────────────────────────────────

var _aria2qTimer = null;

// Consecutive error counter for the aria2 download panel — used to
// back off the polling interval after repeated failures so we don't
// flood logs when aria2 is restarting or temporarily unreachable.
var _aria2qErrCount = 0;

async function loadAria2QueueView() {
  clearTimeout(_aria2qTimer);
  var isActive = !!(document.getElementById('view-aria2queue')
                    ?.classList.contains('active'));

  // Event-delegation for action buttons (register once per tbody lifetime)
  var tb2 = document.getElementById('aria2q-tbody');
  if (tb2 && !tb2._delegated) {
    tb2._delegated = true;
    tb2.addEventListener('click', function(e) {
      var btn = e.target.closest('[data-gid]');
      if (!btn) return;
      var gid = decodeURIComponent(btn.getAttribute('data-gid') || '');
      var act = btn.getAttribute('data-act') || '';
      if (gid && act) aria2QueueAction(gid, act);
    });
  }

  try {
    // Use a 20 s timeout — aria2 can be slow when it has many completed items.
    // The default 8 s is too short and causes spurious "Request timed out" errors
    // that blank the panel and stop the poll loop.
    var data = await api('GET', '/aria2/downloads', null, 20000);
    _aria2qErrCount = 0;                 // reset error streak on success

    // Hide error banner (if shown from a previous failure)
    var errBanner = document.getElementById('aria2q-err-banner');
    if (errBanner) errBanner.style.display = 'none';

    renderAria2QueueView(data);

    // Refresh sidebar badge
    var badge = document.getElementById('nb-aria2-active');
    var cnt   = (data.summary || {}).active || 0;
    if (badge) { badge.textContent = cnt; badge.style.display = cnt > 0 ? '' : 'none'; }

    // Update topbar badge: active count + live speed
    updateAria2TopbarBadge({
      active:  cnt,
      liveBps: (data.summary || {}).download_speed || 0,
    });

    // Sync speed / max-concurrent controls
    loadAria2SpeedLimit();

  } catch(e) {
    _aria2qErrCount++;

    // Show a non-destructive error banner above the existing table rows
    // so previously fetched rows stay visible during a temporary outage.
    var errBanner = document.getElementById('aria2q-err-banner');
    if (errBanner) {
      errBanner.style.display = '';
      errBanner.innerHTML =
        '<span style="color:var(--red)">&#9888; aria2 unreachable</span>'
        + ' <span style="color:var(--text2);font-size:11px">('
        + esc(e.message || 'unknown error') + ')'
        + ' — retrying in ' + (_aria2qErrCount > 3 ? '10' : '3') + ' s</span>';
    } else {
      // Fallback: only replace tbody when it contains no real rows yet
      var tb = document.getElementById('aria2q-tbody');
      var hasRows = tb && tb.querySelector('tr[data-gid]');
      if (tb && !hasRows) {
        tb.innerHTML =
          '<tr><td colspan="7" style="text-align:center;padding:32px">'
          + '<div style="color:var(--red);margin-bottom:6px">&#9888; aria2 unreachable</div>'
          + '<div style="color:var(--text2);font-size:12px">' + esc(e.message || '') + '</div>'
          + '<div style="color:var(--text3);font-size:11px;margin-top:6px">Retrying automatically…</div>'
          + '</td></tr>';
      }
    }
  }

  // Always reschedule while the view is active — even after errors.
  // Use an exponential back-off: 2 s on success, 3 s after 1–3 errors,
  // 10 s after 4+ consecutive errors (aria2 likely restarting).
  if (isActive) {
    var delay = _aria2qErrCount === 0 ? 2000
               : _aria2qErrCount < 4  ? 3000
               :                        10000;
    _aria2qTimer = setTimeout(loadAria2QueueView, delay);
  }
}

function renderAria2QueueView(data) {
  var summary = data.summary || {};
  var items   = data.items   || [];

  // Summary bar
  var sb = document.getElementById('aria2q-summary');
  if (sb) {
    sb.innerHTML =
      '<span class="aria2-chip" style="font-size:12px"><b>' + (summary.active||0) + '</b>&nbsp;active</span>' +
      '<span class="aria2-chip" style="font-size:12px"><b>' + (summary.waiting||0) + '</b>&nbsp;waiting</span>' +
      '<span class="aria2-chip" style="font-size:12px"><b>' + (summary.stopped||0) + '</b>&nbsp;stopped</span>' +
      '<span class="aria2-chip" style="font-size:12px">&#9660;&nbsp;' + fmtSpeed(summary.download_speed||0) + '</span>' +
      '<span class="aria2-chip" style="font-size:12px">Remaining:&nbsp;' + fmtSize(summary.remaining_length||0) + '</span>';
  }

  var tb = document.getElementById('aria2q-tbody');
  if (!tb) return;

  if (!items.length) {
    tb.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:32px;color:var(--text2)">No downloads in aria2 queue.</td></tr>';
    return;
  }

  // Sort: active first, then waiting/paused, then stopped
  items = items.slice().sort(function(a,b) {
    var w = {active:0, waiting:1, paused:2, error:3, complete:4, removed:5};
    return ((w[a.status]||9) - (w[b.status]||9));
  });

  tb.innerHTML = items.map(function(job) {
    var pct = Math.min(100, Math.max(0, job.progress||0));
    var isActive  = job.status === 'active';
    var isPaused  = job.status === 'paused';
    var isWaiting = job.status === 'waiting';
    var canPause  = isActive || isWaiting;
    var canResume = isPaused;
    var canRemove = job.status !== 'complete';
    var barColor  = job.status === 'error' ? 'var(--red)' : isActive ? 'var(--accent)' : isPaused ? 'var(--text3)' : 'var(--green)';

    var firstFile = (job.files||[])[0] || {};
    var name = job.name || firstFile.name || job.gid || '—';
    var fileCount = (job.files||[]).length;
    var nameLabel = fileCount > 1 ? esc(name) + ' <span style="color:var(--text2);font-size:11px">(+' + (fileCount-1) + ' more)</span>' : esc(name);

    var statusDot = isActive ? '<span style="color:var(--accent)">&#9679;</span>' :
                   isPaused  ? '<span style="color:var(--text3)">&#9646;</span>' :
                   job.status === 'error' ? '<span style="color:var(--red)">&#10007;</span>' :
                   job.status === 'complete' ? '<span style="color:var(--green)">&#10003;</span>' :
                   '<span style="color:var(--text3)">&#9675;</span>';

    var progressBar =
      '<div style="width:100%;background:var(--surface2);border-radius:3px;height:5px;overflow:hidden">' +
        '<div style="width:'+pct+'%;background:'+barColor+';height:100%;border-radius:3px;transition:width .4s"></div>' +
      '</div>' +
      '<div style="font-size:10px;color:var(--text2);margin-top:2px">' + pct.toFixed(1) + '%</div>';

    var gEsc = encodeURIComponent(job.gid);
    var actions =
      (canPause  ? '<button class="btn btn-ghost btn-sm" style="padding:2px 7px;font-size:11px" data-gid="'+gEsc+'" data-act="pause"   title="Pause"  >&#9646;&#9646;</button>' : '') +
      (canResume ? '<button class="btn btn-ghost btn-sm" style="padding:2px 7px;font-size:11px" data-gid="'+gEsc+'" data-act="resume"  title="Resume" >&#9654;</button>' : '') +
      (canRemove ? '<button class="btn btn-ghost btn-sm" style="padding:2px 7px;font-size:11px;color:var(--red)" data-gid="'+gEsc+'" data-act="remove"  title="Remove" >&#128465;</button>' : '');

    return '<tr>' +
      '<td style="text-align:center">' + statusDot + '</td>' +
      '<td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(job.name||'') + '">' + nameLabel + '</td>' +
      '<td>' + progressBar + '</td>' +
      '<td style="font-size:12px;white-space:nowrap">' + fmtSize(job.total_length||0) + '</td>' +
      '<td style="font-size:12px;white-space:nowrap;color:var(--accent)">' + (isActive ? fmtSpeed(job.download_speed||0) : '—') + '</td>' +
      '<td style="font-size:12px;white-space:nowrap">' + aria2StatusLabel(job.status) + '</td>' +
      '<td style="white-space:nowrap">' + actions + '</td>' +
    '</tr>';
  }).join('');
}

async function aria2QueueAction(gid, action) {
  try {
    await api('POST', '/aria2/downloads/' + encodeURIComponent(gid) + '/' + action);
    toast('aria2: ' + action + ' sent', 'success');
    await loadAria2QueueView();
  } catch(e) {
    toast('aria2 ' + action + ': ' + e.message, 'error');
  }
}

// ── Speed Limit ───────────────────────────────────────────────────────────────

async function loadAria2SpeedLimit() {
  try {
    var data = await api('GET', '/aria2/global-options', null, 10000);
    var bps   = parseInt(data.max_download_speed || 0);
    var maxDl = parseInt(data.max_concurrent_downloads || 0)
                || (settingsData && settingsData.aria2_max_active_downloads)
                || 3;

    // ── Sync settingsData so PUT /settings uses the live value ───────────
    if (settingsData) {
      settingsData.aria2_max_active_downloads = maxDl;
      settingsData.max_concurrent_downloads   = maxDl;
      settingsData.aria2_max_download_limit   = bps;
    }
    // ── Sync Settings-page inputs (Downloads → Settings, bidirectional) ──
    var inMcd = document.getElementById('s-max_concurrent_downloads');
    var inMad = document.getElementById('s-aria2_max_active_downloads');
    if (inMcd) inMcd.value = maxDl;
    if (inMad) inMad.value = maxDl;

    // ── Sync speed preset in Downloads panel ─────────────────────────────
    var sel = document.getElementById('aria2-speed-preset');
    var st  = document.getElementById('aria2-speed-status');
    if (sel) {
      var found = false;
      for (var i = 0; i < sel.options.length; i++) {
        if (sel.options[i].value !== 'custom' && parseInt(sel.options[i].value || 0) === bps) {
          sel.value = sel.options[i].value;
          found = true; break;
        }
      }
      if (!found) {
        sel.value = 'custom';
        var ci = document.getElementById('aria2-speed-custom');
        var cb = document.getElementById('aria2-speed-apply');
        if (ci) { ci.style.display = ''; ci.value = Math.round(bps / 1024); }
        if (cb)   cb.style.display = '';
      }
      if (st) st.textContent = bps > 0 ? '(' + fmtSpeed(bps) + ')' : '(unlimited)';
    }

    // ── Sync Max DL preset in Downloads panel ─────────────────────────────
    var msel = document.getElementById('aria2-maxdl-preset');
    if (msel) {
      var mfound = false;
      for (var j = 0; j < msel.options.length; j++) {
        if (parseInt(msel.options[j].value) === maxDl) {
          msel.value = msel.options[j].value;
          mfound = true; break;
        }
      }
      if (!mfound) msel.value = '3';
    }

    // ── Update topbar badge ───────────────────────────────────────────────
    updateAria2TopbarBadge({ limitBps: bps, maxDl: maxDl });

  } catch (e) { /* aria2 not connected — silently ignore */ }
}

async function applyAria2SpeedPreset(val) {
  var ci = document.getElementById('aria2-speed-custom');
  var cb = document.getElementById('aria2-speed-apply');
  if (val === 'custom') {
    if (ci) ci.style.display=''; if (cb) cb.style.display=''; return;
  }
  if (ci) ci.style.display='none'; if (cb) cb.style.display='none';
  await _setAria2Speed(parseInt(val||0));
}

async function applyAria2SpeedCustom() {
  var ci = document.getElementById('aria2-speed-custom');
  var kbps = parseInt((ci&&ci.value)||0);
  await _setAria2Speed(kbps * 1024);
}

async function _setAria2Speed(bps) {
  var st = document.getElementById('aria2-speed-status');
  if (st) { st.style.color='var(--text2)'; st.textContent='Applying…'; }
  try {
    await api('POST', '/aria2/global-options', {max_download_speed: bps});
    // Keep settingsData in sync so subsequent PUT /settings calls don't
    // overwrite this value with the stale cached number.
    if (settingsData) settingsData.aria2_max_download_limit = bps;
    if (st) { st.style.color='var(--green)'; st.textContent = bps > 0 ? 'Set: ' + fmtSpeed(bps) : 'Unlimited'; }
    setTimeout(function(){ if(st) st.style.color='var(--text2)'; }, 3000);
    updateAria2TopbarBadge({limitBps: bps});
  } catch(e) {
    if (st) { st.style.color='var(--red)'; st.textContent='Error: '+e.message; }
    toast('Speed limit error: '+e.message, 'error');
  }
}

// Update Downloads badge from loadStats
function updateAria2Badge(activeCount) {
  var badge = document.getElementById('nb-aria2-active');
  if (!badge) return;
  badge.textContent = activeCount;
  badge.style.display = activeCount > 0 ? '' : 'none';
}

// Topbar badge: live active count, speed limit, max concurrent
var _aria2BadgeState = {active: 0, limitBps: 0, maxDl: 3, liveBps: 0};
function updateAria2TopbarBadge(patch) {
  Object.assign(_aria2BadgeState, patch);
  var s = _aria2BadgeState;
  var topBadge = document.getElementById('aria2-speed-badge');
  var elActive = document.getElementById('aria2-badge-active');
  var elMax    = document.getElementById('aria2-badge-max');
  var elSpeed  = document.getElementById('aria2-badge-speed');
  var elLimit  = document.getElementById('aria2-badge-limit');
  if (!topBadge) return;
  if (elActive) elActive.textContent = s.active;
  if (elMax)    elMax.textContent    = s.maxDl || '—';
  if (elSpeed)  elSpeed.textContent  = fmtSpeed(s.liveBps || 0);
  if (elLimit)  elLimit.textContent  = s.limitBps > 0 ? fmtSpeed(s.limitBps) : '\u221e';
}

async function applyAria2MaxDlPreset(val) {
  var n = parseInt(val) || 3;
  var st = document.getElementById('aria2-maxdl-status');
  if (st) { st.style.color='var(--text2)'; st.textContent='Applying…'; }
  try {
    // Apply live via RPC — POST /aria2/global-options also persists to settings.json
    await api('POST', '/aria2/global-options', {max_concurrent_downloads: n});
    // Keep settingsData in sync so subsequent PUT /settings calls don't
    // overwrite this value with the stale cached number.
    if (settingsData) {
      // Keep BOTH config fields in sync so a subsequent PUT /settings and a
      // Manager Semaphore reset both use the updated value.
      settingsData.aria2_max_active_downloads = n;
      settingsData.max_concurrent_downloads   = n;
    }
    // Sync Settings-page inputs so a subsequent Save Settings does not clobber.
    var maxDlInput = document.getElementById('s-max_concurrent_downloads');
    if (maxDlInput) maxDlInput.value = n;
    var maxDlInput2 = document.getElementById('s-aria2_max_active_downloads');
    if (maxDlInput2) maxDlInput2.value = n;
    if (st) { st.style.color='var(--green)'; st.textContent=n+' active'; }
    setTimeout(function(){ if(st) st.style.color='var(--text2)'; st.textContent=''; }, 3000);
    updateAria2TopbarBadge({maxDl: n});
  } catch(e) {
    if (st) { st.style.color='var(--red)'; st.textContent='Error'; }
    toast('Max downloads error: '+e.message, 'error');
  }
}


function switchHelpTab(el) {
  if (!el) return;
  const tabId = el.dataset.htab;
  document.querySelectorAll('#help-tabs .stab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.help-panel').forEach(p => p.classList.remove('active'));
  el.classList.add('active');
  const panel = document.getElementById('htab-' + tabId);
  if (panel) panel.classList.add('active');
}


async function showMemoryInfo() {
  var el = document.getElementById('aria2-memory-info');
  if (!el) return;
  el.style.display = '';
  el.innerHTML = '<span style="color:var(--text2)">Loading&#8230;</span>';
  try {
    var d = await api('GET', '/admin/memory-info');
    el.innerHTML =
      '<b>&#128202; System Memory</b><br>' +
      'Total: <b>' + d.total + '</b> &nbsp; ' +
      'Really used: <b>' + d.really_used + '</b> &nbsp; ' +
      'Page cache: <b style="color:var(--accent)">' + d.page_cache + '</b> &nbsp; ' +
      'Available: <b style="color:var(--green)">' + d.available + '</b><br>' +
      '<span style="font-size:11px;color:var(--text2)">' +
      'Page cache = kernel file cache shown as \"used\" in Unraid dashboard, ' +
      'but reclaimed automatically when needed. ' +
      'If large, click \"Drop Page Cache\" to release it immediately.' +
      '</span>';
  } catch(e) {
    el.innerHTML = '<span style="color:var(--red)">Error: ' + esc(e.message) + '</span>';
  }
}

async function dropPageCache() {
  var el = document.getElementById('aria2-memory-info');
  if (el) { el.style.display = ''; el.innerHTML = '<span style="color:var(--text2)">Releasing page cache&#8230;</span>'; }
  try {
    var d = await api('POST', '/admin/drop-page-cache');
    toast('Page cache released for ' + d.cache_released + '/' + d.files_processed + ' files', 'success');
    if (el) el.innerHTML =
      '<b style="color:var(--green)">&#10003; ' + d.message + '</b><br>' +
      '<span style="font-size:11px;color:var(--text2)">Run Memory Info again to see updated RAM usage.</span>';
    // refresh memory info after 1s
    setTimeout(showMemoryInfo, 1200);
  } catch(e) {
    toast('Drop page cache failed: ' + e.message, 'error');
    if (el) el.innerHTML = '<span style="color:var(--red)">Error: ' + esc(e.message) + '</span>';
  }
}
