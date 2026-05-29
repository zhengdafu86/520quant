/* 520量化 Web 前端逻辑 */

const REFRESH_INTERVAL = 30;   // 秒
let countdown = REFRESH_INTERVAL;
let activeTab = 'positions';

/* ── Tab 切换 ──────────────────────────────── */
document.querySelectorAll('[data-tab]').forEach(el => {
  el.addEventListener('click', e => {
    e.preventDefault();
    const tab = el.dataset.tab;
    activeTab = tab;

    document.querySelectorAll('[data-tab]').forEach(t => t.classList.remove('active'));
    el.classList.add('active');

    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    document.getElementById('tab-' + tab).classList.add('active');

    loadTab(tab);
  });
});

/* ── 颜色工具 ──────────────────────────────── */
function pnlClass(v) {
  if (v > 0) return 'up';
  if (v < 0) return 'down';
  return 'flat';
}
function pnlSign(v) { return v > 0 ? '+' : ''; }

/* ── 止损进度条 ─────────────────────────────── */
function stopBarHtml(price, cost, stop) {
  if (!stop || !cost || stop >= price) return '';
  // 区间: stop ~ (cost * 1.2)，现价在其中的位置
  const hi  = cost * 1.20;
  const pct = Math.min(100, Math.max(0, ((price - stop) / (hi - stop)) * 100));
  let color;
  if (pct < 20)      color = '#e53935';
  else if (pct < 50) color = '#ff9800';
  else               color = '#43a047';

  return `
    <div class="stop-bar-wrap">
      <div class="bar-label">
        <span>止损 ${stop.toFixed(2)}</span>
        <span>距止损 ${((price - stop) / price * 100).toFixed(1)}%</span>
      </div>
      <div class="progress">
        <div class="stop-bar-fill" style="width:${pct}%;background:${color}"></div>
      </div>
    </div>`;
}

/* ── 持仓 ──────────────────────────────────── */
async function loadPositions() {
  const res  = await fetch('/api/positions').then(r => r.json());
  const cont = document.getElementById('positions-list');

  if (!res.length) {
    cont.innerHTML = '<div class="empty-state">暂无持仓</div>';
    return;
  }

  cont.innerHTML = res.map(p => {
    const cls  = pnlClass(p.pnl_pct);
    const sign = pnlSign(p.pnl_pct);
    return `
    <div class="stock-card">
      <div class="d-flex justify-content-between align-items-start">
        <div>
          <span class="code-name">${p.name}</span>
          <span class="code-tag">${p.code}</span>
        </div>
        <div class="text-end">
          <div class="price ${cls}">${p.price.toFixed(2)}</div>
          <div class="small ${cls}">${sign}${p.pnl_pct.toFixed(2)}%
            (${sign}${Math.round(p.pnl).toLocaleString()}元)</div>
        </div>
      </div>
      <div class="meta mt-2">
        成本 <b>${p.cost.toFixed(2)}</b> ·
        ${p.shares}股 ·
        市值 <b>${p.mkt_value.toLocaleString()}</b> 元
      </div>
      ${stopBarHtml(p.price, p.cost, p.stop_price)}
    </div>`;
  }).join('');
}

/* ── 自选股 ─────────────────────────────────── */
async function loadWatchlist() {
  const res  = await fetch('/api/watchlist').then(r => r.json());
  const cont = document.getElementById('watchlist-list');

  if (!res.length) {
    cont.innerHTML = '<div class="empty-state">自选股为空<br>在上方输入代码添加</div>';
    return;
  }

  cont.innerHTML = res.map(w => {
    const signalCls = w.signal.includes('金叉') ? 'signal-金叉'
                    : w.signal.includes('回踩') ? 'signal-回踩'
                    : w.signal.includes('压缩') ? 'signal-压缩'
                    : 'signal-候选';
    return `
    <div class="stock-card">
      <div class="d-flex justify-content-between align-items-center">
        <div>
          <span class="code-name">${w.name}</span>
          <span class="code-tag">${w.code}</span>
        </div>
        <div class="d-flex align-items-center gap-2">
          <span class="price">${w.price > 0 ? w.price.toFixed(2) : '--'}</span>
          <button class="btn btn-sm btn-outline-danger py-0 px-2"
                  onclick="removeWatch('${w.code}','${w.name}')">移除</button>
        </div>
      </div>
      <div class="meta mt-1">
        <span class="signal-badge ${signalCls}">${w.signal}</span>
        <span class="ms-2">加入 ${(w.added_time||'').slice(0,10)}</span>
      </div>
    </div>`;
  }).join('');
}

async function addToWatchlist() {
  const code = document.getElementById('add-code').value.trim();
  if (!code) return;

  const res = await fetch('/api/watchlist/add', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({code, signal: '手动添加'})
  }).then(r => r.json());

  if (res.ok) {
    showToast(`已添加 ${res.name}(${res.code})`);
    document.getElementById('add-code').value = '';
    loadWatchlist();
  } else {
    showToast(res.msg || '添加失败', 'danger');
  }
}

async function removeWatch(code, name) {
  if (!confirm(`确认移除 ${name}(${code})?`)) return;
  await fetch(`/api/watchlist/${code}`, {method: 'DELETE'});
  showToast(`已移除 ${name}`);
  loadWatchlist();
}

/* ── 扫描结果 ────────────────────────────────── */
async function loadScan() {
  const data = await fetch('/api/scan').then(r => r.json());
  document.getElementById('scan-date').textContent =
    '最新扫描：' + (data.date || '暂无');

  const cont = document.getElementById('scan-list');
  const res  = data.results || [];

  if (!res.length) {
    cont.innerHTML = '<div class="empty-state">暂无扫描结果<br>点击右上角「手动扫描」</div>';
    return;
  }

  const signalIcon = {'金叉':'✅','回踩':'🔄','压缩':'🔀'};

  cont.innerHTML = res.map((r, i) => {
    const icon = signalIcon[r.signal] || '⭕';
    const sigCls = `signal-${r.signal}`;
    return `
    <div class="stock-card">
      <div class="d-flex justify-content-between align-items-start">
        <div>
          <span class="me-1 text-muted small">#${i+1}</span>
          <span class="code-name">${r.name}</span>
          <span class="code-tag">${r.code}</span>
        </div>
        <div class="d-flex align-items-center gap-2">
          <span class="price">${r.price.toFixed(2)}</span>
          <button class="btn btn-sm btn-outline-primary py-0 px-2"
                  onclick="addScanToWatch('${r.code}','${r.name}','${r.signal}')">+自选</button>
        </div>
      </div>
      <div class="meta mt-1">
        <span class="signal-badge ${sigCls}">${icon} ${r.signal}</span>
        <span class="ms-2 text-muted">止损 ${r.stop_price.toFixed(2)}</span>
      </div>
      <div class="meta mt-1 text-muted">${r.reason}</div>
    </div>`;
  }).join('');
}

async function addScanToWatch(code, name, signal) {
  const res = await fetch('/api/watchlist/add', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({code, name, signal})
  }).then(r => r.json());
  if (res.ok) showToast(`已加入自选: ${name}`);
}

async function triggerScan() {
  const res = await fetch('/api/scan/run').then(r => r.json());
  showToast(res.msg, 'primary');
}

/* ── 账户详情 ────────────────────────────────── */
async function loadAccount() {
  const d   = await fetch('/api/account').then(r => r.json());
  const ret = d.total_return || 0;
  const cls = pnlClass(ret);

  const rows = [
    ['初始资金', `${(d.init_capital||0).toLocaleString()} 元`],
    ['当前现金', `${(d.cash||0).toLocaleString()} 元`],
    ['持仓市值', `${(d.pos_value||0).toLocaleString()} 元`],
    ['总资产',   `${(d.total_assets||0).toLocaleString()} 元`],
    ['累计收益', `<span class="${cls}">${pnlSign(ret)}${ret.toFixed(2)}%</span>`],
  ];

  document.getElementById('account-detail').innerHTML = `
    <div class="stock-card">
      ${rows.map(([k,v]) => `
        <div class="account-row">
          <span class="key">${k}</span>
          <span class="val">${v}</span>
        </div>`).join('')}
    </div>`;
}

/* ── 账户概览条 ──────────────────────────────── */
async function loadAccountBar() {
  try {
    const d = await fetch('/api/account').then(r => r.json());
    const ret = d.total_return || 0;
    document.getElementById('total-assets').textContent =
      (d.total_assets || 0).toLocaleString() + ' 元';
    document.getElementById('total-return').textContent =
      (ret >= 0 ? '+' : '') + ret.toFixed(2) + '%';
    document.getElementById('total-return').className =
      'value ' + pnlClass(ret);
    document.getElementById('cash').textContent =
      (d.cash || 0).toLocaleString() + ' 元';
  } catch(e) {}
}

/* ── Toast ───────────────────────────────────── */
function showToast(msg, type = 'success') {
  const toast = document.getElementById('toast');
  const body  = document.getElementById('toast-body');
  toast.className = `toast align-items-center text-white border-0 bg-${type}`;
  body.textContent = msg;
  bootstrap.Toast.getOrCreateInstance(toast, {delay: 3000}).show();
}

/* ── 加载当前 Tab ────────────────────────────── */
function loadTab(tab) {
  if (tab === 'positions') loadPositions();
  else if (tab === 'watchlist') loadWatchlist();
  else if (tab === 'scan')     loadScan();
  else if (tab === 'account')  loadAccount();
}

/* ── 自动刷新 ────────────────────────────────── */
function tick() {
  countdown--;
  document.getElementById('refresh-time').textContent =
    `${countdown}s 后刷新`;
  if (countdown <= 0) {
    countdown = REFRESH_INTERVAL;
    loadAccountBar();
    loadTab(activeTab);
  }
}

// 初始化
loadAccountBar();
loadTab('positions');
setInterval(tick, 1000);
