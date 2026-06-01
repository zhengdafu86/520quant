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
  const data = await fetch('/api/positions').then(r => r.json());
  const cont = document.getElementById('positions-list');
  const res  = data.positions || [];

  // 账户栏与持仓同步更新（共用同一次行情）
  _updateAccountBar(data);

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
        <div class="d-flex align-items-start gap-2">
          <div class="text-end">
            <div class="price ${cls}">${p.price.toFixed(2)}</div>
            <div class="small ${cls}">${sign}${p.pnl_pct.toFixed(2)}%
              (${sign}${Math.round(p.pnl).toLocaleString()}元)</div>
          </div>
          <button class="btn btn-sm btn-outline-danger py-0 px-2 mt-1"
                  onclick="sellPosition('${p.code}','${p.name}',${p.price})">卖出</button>
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

/* ── 手动卖出持仓 ────────────────────────────── */
async function sellPosition(code, name, currentPrice) {
  const input = prompt(
    `手动卖出 ${name}(${code})\n请输入卖出价格（当前价 ${currentPrice.toFixed(2)}）：`,
    currentPrice.toFixed(2)
  );
  if (input === null) return;   // 用户取消

  const price = parseFloat(input);
  if (isNaN(price) || price <= 0) {
    showToast('价格无效，请重新输入', 'danger');
    return;
  }

  if (!confirm(`确认以 ${price.toFixed(2)} 元卖出 ${name}(${code})？`)) return;

  const res = await fetch(`/api/positions/${code}/sell`, {
    method:  'POST',
    headers: {'Content-Type': 'application/json'},
    body:    JSON.stringify({price}),
  }).then(r => r.json());

  if (res.ok) {
    showToast(res.msg, 'success');
    loadPositions();

    // 防止监控引擎重新自动买入：询问是否同时移出自选股
    const removeAlso = confirm(
      `卖出成功！\n\n「${name}」仍在自选股中，监控系统可能在下一个 tick 重新自动买入。\n\n是否同时移出自选股？`
    );
    if (removeAlso) {
      await fetch(`/api/watchlist/${code}`, {method: 'DELETE'});
      showToast(`已同时移出自选股`, 'primary');
      if (activeTab === 'scan') {
        _scanWatchSet.delete(code);
        applyScanFilters();
      }
    }
  } else {
    showToast(res.msg || '卖出失败', 'danger');
  }
}

/* ── 自选股 ─────────────────────────────────── */
const _PORDER = {'P1': 1, 'P2': 2, 'P3': 3, '': 4};

function _prioritySelectHtml(code, current) {
  const opts = [
    ['',   '— 无 —'],
    ['P1', 'P1 优先'],
    ['P2', 'P2'],
    ['P3', 'P3'],
  ];
  const optHtml = opts.map(([v, label]) =>
    `<option value="${v}" ${current === v ? 'selected' : ''}>${label}</option>`
  ).join('');
  return `<select class="priority-select pri-${current||'none'}"
                  onchange="setPriority('${code}', this.value)">${optHtml}</select>`;
}

/* 相对强度徽章 */
function _rsBadgeHtml(rs) {
  if (rs == null) return '';
  const cls  = rs >= 0 ? 'rs-pos' : 'rs-neg';
  const sign = rs >= 0 ? '+' : '';
  return `<span class="rs-badge ${cls}">RS ${sign}${rs.toFixed(1)}%</span>`;
}

/* 板块方向徽章：只显示箭头方向，不显示行业名 */
function _sectorDirHtml(dir) {
  if (!dir || dir === 'unknown') return '';
  const map = {
    up:   { cls: 'sector-up',   label: '板块↑' },
    down: { cls: 'sector-down', label: '板块↓' },
    flat: { cls: 'sector-flat', label: '板块→' },
  };
  const m = map[dir];
  if (!m) return '';
  return `<span class="sector-badge ${m.cls}" title="行业ETF MA20方向">${m.label}</span>`;
}

/* 小指标标签（量比、换手率、止损价等） */
function _chip(text, extraCls = '') {
  return `<span class="metric-chip ${extraCls}">${text}</span>`;
}

async function loadWatchlist() {
  // 自选 API 已内嵌当日扫描信号 + 实时涨跌幅/量比，无需单独请求扫描接口
  const res  = await fetch('/api/watchlist').then(r => r.json());
  const cont = document.getElementById('watchlist-list');

  if (!res.length) {
    cont.innerHTML = '<div class="empty-state">自选股为空<br>在上方输入代码添加</div>';
    return;
  }

  // 按优先级排序（P1→P2→P3→无）
  res.sort((a, b) => (_PORDER[a.priority] || 4) - (_PORDER[b.priority] || 4));

  cont.innerHTML = res.map(w => {
    /* ── 今日涨跌 ── */
    const chg     = w.change_pct || 0;
    const chgCls  = chg > 0 ? 'up' : chg < 0 ? 'down' : 'flat';
    const chgSign = chg > 0 ? '+' : '';
    const priceHtml = w.price > 0
      ? `<div class="text-end">
           <div class="price">${w.price.toFixed(2)}</div>
           <div class="small ${chgCls}">${chgSign}${chg.toFixed(2)}%</div>
         </div>`
      : `<div class="price text-muted">--</div>`;

    /* ── 量比标签 ── */
    const vr    = w.vol_ratio || 0;
    const vrCls = vr >= 2.0 ? 'vol-high' : vr >= 1.5 ? 'vol-mid' : '';
    const vrHtml = vr > 0 ? _chip(`量比 ${vr.toFixed(1)}x`, vrCls) : '';

    /* ── 相对强度 RS（仅今日上榜扫描才显示）── */
    const rsHtml = _rsBadgeHtml(w.scan_rs);

    /* ── 板块方向（今日上榜时显示）── */
    const sectorWHtml = _sectorDirHtml(w.scan_sector_dir || '');

    /* ── 扫描信号标识 ── */
    const hasScan   = !!w.scan_signal;
    const scanBadge = hasScan
      ? (() => {
          const sc = w.scan_signal.includes('金叉') ? 'signal-金叉'
                   : w.scan_signal.includes('回踩') ? 'signal-回踩' : 'signal-压缩';
          const short = w.scan_signal.replace('买点','');
          const icon  = w.scan_signal.includes('金叉') ? '✅' : w.scan_signal.includes('回踩') ? '🔄' : '🔀';
          return `<span class="signal-badge ${sc} scan-live-badge">${icon} ${short}</span>`;
        })()
      : '';

    /* ── 手动加入时的信号标签（默认态）── */
    const manualCls = w.signal.includes('金叉') ? 'signal-金叉'
                    : w.signal.includes('回踩') ? 'signal-回踩'
                    : w.signal.includes('压缩') ? 'signal-压缩'
                    : 'signal-候选';
    const manualBadge = `<span class="signal-badge ${manualCls}">${w.signal}</span>`;

    /* ── 止损价（若当日上榜则有）── */
    const stopHtml = (hasScan && w.scan_stop > 0)
      ? _chip(`止损 ${w.scan_stop.toFixed(2)}`)
      : '';

    /* ── 金叉日期 ── */
    const crossDateHtml = (hasScan && w.scan_cross_date)
      ? _chip(`金叉 ${w.scan_cross_date}`)
      : '';

    /* ── 针形支撑标记 ── */
    const watchHammerHtml = (hasScan && (w.scan_reason || '').includes('针形'))
      ? `<span class="hammer-badge">📌 针形支撑</span>` : '';

    /* ── 信号消失提示（无实时买点时显示，提示可移除）── */
    const noSignalHtml = !hasScan
      ? `<span class="no-signal-badge">⚠️ 暂无买点</span>`
      : '';

    /* ── 扫描信号详情 ── */
    const descHtml = (hasScan && w.scan_reason)
      ? `<div class="scan-desc mt-2">
           <span class="text-muted" style="font-size:11px">今日扫描信号</span><br>
           ${w.scan_reason.replace(/\n/g,'<br>')}
         </div>`
      : '';

    return `
    <div class="stock-card ${hasScan ? 'card-has-signal' : ''}">
      <div class="d-flex justify-content-between align-items-start">
        <div>
          <span class="code-name">${w.name}</span>
          <span class="code-tag">${w.code}</span>
          ${scanBadge}
          ${noSignalHtml}
        </div>
        <div class="d-flex align-items-center gap-2">
          ${priceHtml}
          ${_prioritySelectHtml(w.code, w.priority || '')}
          <button class="btn btn-sm btn-outline-danger py-0 px-2"
                  onclick="removeWatch('${w.code}','${w.name}')">移除</button>
        </div>
      </div>
      <div class="meta mt-2 d-flex flex-wrap align-items-center gap-1">
        ${hasScan ? '' : manualBadge}
        ${watchHammerHtml}
        ${sectorWHtml}
        ${vrHtml}
        ${rsHtml}
        ${crossDateHtml}
        ${stopHtml}
        <span class="text-muted" style="margin-left:auto;font-size:11px">
          加入 ${(w.added_time||'').slice(0,10)}
        </span>
      </div>
      ${descHtml}
    </div>`;
  }).join('');
}

async function setPriority(code, priority) {
  const res = await fetch(`/api/watchlist/${code}/priority`, {
    method:  'PATCH',
    headers: {'Content-Type': 'application/json'},
    body:    JSON.stringify({priority}),
  }).then(r => r.json());
  if (res.ok) {
    showToast(priority ? `${code} 已设为 ${priority}` : `${code} 优先级已清除`);
    loadWatchlist();   // 重新排序列表
  } else {
    showToast(res.msg || '设置失败', 'danger');
  }
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
  if (activeTab === 'scan') {
    _scanWatchSet.delete(code);  // 立即更新本地状态
    applyScanFilters();           // 按钮即时变回「+ 自选」
  }
}

/* ── 扫描过滤器状态 ─────────────────────────── */
let _scanAllData  = [];          // 全量扫描结果（服务端排序）
let _scanWatchSet = new Set();   // 已加入自选的代码集合

const _scanFilters = {
  signals: new Set(['金叉', '回踩', '粘合']),   // 选中的信号类型关键词
  sector:  'all',      // 'all' | 'up' | 'down'
  rsSort:  'default',  // 'default' | 'rs_desc' | 'rs_asc'
};

/* 信号 chip 多选切换（至少保留一个选项）*/
function toggleScanFilter(btn) {
  const val = btn.dataset.value;
  if (_scanFilters.signals.has(val)) {
    if (_scanFilters.signals.size <= 1) return;  // 不允许全不选
    _scanFilters.signals.delete(val);
    btn.classList.remove('active');
  } else {
    _scanFilters.signals.add(val);
    btn.classList.add('active');
  }
  applyScanFilters();
}

/* 板块方向单选 */
function setSectorFilter(btn) {
  document.querySelectorAll('[data-filter="sector"]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  _scanFilters.sector = btn.dataset.value;
  applyScanFilters();
}

/* RS 排序单选 */
function setSortFilter(btn) {
  document.querySelectorAll('[data-filter="sort"]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  _scanFilters.rsSort = btn.dataset.value;
  applyScanFilters();
}

/* 过滤 + 排序 + 渲染 */
function applyScanFilters() {
  let data = [..._scanAllData];

  // 信号类型过滤
  data = data.filter(r => {
    const s = r.signal || '';
    if (s.includes('金叉') && _scanFilters.signals.has('金叉')) return true;
    if (s.includes('回踩') && _scanFilters.signals.has('回踩')) return true;
    if ((s.includes('粘合') || s.includes('发散') || s.includes('压缩'))
        && _scanFilters.signals.has('粘合')) return true;
    return false;
  });

  // 板块方向过滤
  if (_scanFilters.sector === 'up') {
    data = data.filter(r => r.sector_dir === 'up');
  } else if (_scanFilters.sector === 'down') {
    data = data.filter(r => r.sector_dir === 'down');
  }

  // RS 排序（default 保持服务端 score+rs 排序）
  if (_scanFilters.rsSort === 'rs_desc') {
    data.sort((a, b) => ((b.rs_score ?? -9999) - (a.rs_score ?? -9999)));
  } else if (_scanFilters.rsSort === 'rs_asc') {
    data.sort((a, b) => ((a.rs_score ?? 9999) - (b.rs_score ?? 9999)));
  }

  // 更新计数提示
  const countEl = document.getElementById('scan-count');
  if (countEl) {
    const withSector = _scanAllData.filter(r => r.sector_dir && r.sector_dir !== 'unknown').length;
    const sectorNote = withSector > 0
      ? `，其中 ${withSector} 只有板块方向数据`
      : '，板块方向数据暂无（名称不含行业词）';
    countEl.textContent = data.length < _scanAllData.length
      ? `显示 ${data.length} / ${_scanAllData.length} 只`
      : `共 ${data.length} 只${sectorNote}`;
  }

  _renderScanCards(data);
}

/* 渲染扫描卡片列表（传入已过滤/排序好的数组）*/
function _renderScanCards(list) {
  const cont = document.getElementById('scan-list');

  if (!list.length) {
    const sectorActive = _scanFilters.sector !== 'all';
    const hint = sectorActive
      ? '当前板块过滤无结果<br><span style="font-size:12px;color:#bbb">板块方向仅覆盖名称含行业词的股票<br>（如"XX银行"、"XX医药"、"XX半导体"）</span>'
      : '当前过滤条件下无结果';
    cont.innerHTML = `<div class="empty-state">${hint}</div>`;
    return;
  }

  const signalIcon  = s => s.includes('金叉') ? '✅' : s.includes('回踩') ? '🔄' : '🔀';
  const signalShort = s => s.replace('买点', '');

  cont.innerHTML = list.map((r, i) => {
    const icon   = signalIcon(r.signal);
    const sigCls = r.signal.includes('金叉') ? 'signal-金叉'
                 : r.signal.includes('回踩') ? 'signal-回踩' : 'signal-压缩';

    const descHtml   = (r.reason || '').replace(/\n/g, '<br>');
    const inWatch    = _scanWatchSet.has(r.code);
    const btnHtml    = inWatch
      ? `<button class="btn btn-sm btn-secondary py-0 px-2"
                 onclick="removeWatch('${r.code}','${r.name}')">✓ 已选</button>`
      : `<button class="btn btn-sm btn-outline-primary py-0 px-2"
                 onclick="addScanToWatch('${r.code}','${r.name}','${r.signal}')">+ 自选</button>`;
    const rsHtml     = _rsBadgeHtml(r.rs_score ?? null);
    const sectorHtml = _sectorDirHtml(r.sector_dir || '');
    const stopHtml   = r.stop_price > 0
      ? _chip(`止损 ${r.stop_price.toFixed(2)}`) : '';
    const scoreHtml  = r.score > 0
      ? _chip(`得分 ${r.score}`) : '';
    const crossHtml  = r.cross_date
      ? _chip(`金叉 ${r.cross_date}`) : '';
    const hammerHtml = (r.reason || '').includes('针形')
      ? `<span class="hammer-badge">📌 针形支撑</span>` : '';

    return `
    <div class="stock-card">
      <div class="d-flex justify-content-between align-items-start">
        <div>
          <span class="rank-num">#${i + 1}</span>
          <span class="code-name">${r.name}</span>
          <span class="code-tag">${r.code}</span>
        </div>
        <div class="d-flex align-items-center gap-2">
          <span class="price">${r.price.toFixed(2)}</span>
          ${btnHtml}
        </div>
      </div>
      <div class="meta mt-2 d-flex flex-wrap align-items-center gap-1">
        <span class="signal-badge ${sigCls}">${icon} ${signalShort(r.signal)}</span>
        ${hammerHtml}
        ${sectorHtml}
        ${rsHtml}
        ${crossHtml}
        ${stopHtml}
        ${scoreHtml}
      </div>
      <div class="scan-desc mt-2">${descHtml}</div>
    </div>`;
  }).join('');
}

/* ── 扫描结果 ────────────────────────────────── */
async function loadScan() {
  const [data, wlist] = await Promise.all([
    fetch('/api/scan').then(r => r.json()),
    fetch('/api/watchlist').then(r => r.json()),
  ]);
  document.getElementById('scan-date').textContent =
    '最新扫描：' + (data.date || '暂无');

  _scanAllData  = data.results || [];
  _scanWatchSet = new Set((wlist || []).map(w => w.code));

  if (!_scanAllData.length) {
    document.getElementById('scan-count').textContent = '';
    document.getElementById('scan-list').innerHTML =
      '<div class="empty-state">暂无扫描结果<br>点击右上角「手动扫描」</div>';
    return;
  }

  applyScanFilters();
}

async function addScanToWatch(code, name, signal) {
  const res = await fetch('/api/watchlist/add', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({code, name, signal})
  }).then(r => r.json());
  if (res.ok) {
    showToast(`已加入自选: ${name}`);
    _scanWatchSet.add(code);  // 立即更新本地状态，按钮即时变「已选」
    applyScanFilters();       // 用当前过滤/排序重渲染，不重新请求
  }
}

let _scanningDateSnapshot = null;   // 触发扫描时的日期快照

async function triggerScan() {
  const btn = document.getElementById('btn-scan');
  if (btn.disabled) return;

  // 记录当前扫描日期快照
  try {
    const cur = await fetch('/api/scan').then(r => r.json());
    _scanningDateSnapshot = cur.date || null;
  } catch(e) {}

  // 进入 processing 状态
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span>扫描中...';
  btn.classList.replace('btn-outline-primary', 'btn-secondary');

  try {
    const res = await fetch('/api/scan/run').then(r => r.json());
    showToast(res.msg, 'primary');
  } catch(e) {
    showToast('请求失败，请重试', 'danger');
    _resetScanBtn();
  }
}

function _resetScanBtn() {
  const btn = document.getElementById('btn-scan');
  if (!btn) return;
  btn.disabled = false;
  btn.innerHTML = '手动扫描';
  btn.classList.replace('btn-secondary', 'btn-outline-primary');
  _scanningDateSnapshot = null;
}

/* ── 交易记录 ────────────────────────────────── */
async function loadTrades() {
  const data = await fetch('/api/trades').then(r => r.json());
  const cont = document.getElementById('trades-list');

  if (!data.length) {
    cont.innerHTML = '<div class="empty-state">暂无交易记录</div>';
    return;
  }

  cont.innerHTML = data.map(s => {
    const totalCls  = pnlClass(s.total_pnl);
    const totalSign = pnlSign(s.total_pnl);
    const statusBadge = s.open
      ? `<span class="signal-badge signal-候选 ms-2">持仓中</span>`
      : `<span class="signal-badge ms-2" style="background:#6c757d;color:#fff">已平仓</span>`;

    // 已完成交易明细
    const tradeRows = s.trades.map((t, i) => {
      const cls  = pnlClass(t.pnl);
      const sign = pnlSign(t.pnl);
      return `
      <div class="trade-detail-row">
        <div class="trade-detail-header">
          <span class="text-muted small">第${i + 1}笔</span>
          <span class="${cls} fw-bold">${sign}${t.pnl.toLocaleString()} 元
            <span class="small">(${sign}${t.pnl_pct}%)</span>
          </span>
        </div>
        <div class="trade-detail-body">
          <div class="trade-row">
            <span class="trade-label up">买入</span>
            <span>${t.buy_price.toFixed(2)} × ${t.shares}股 = ${t.buy_amount.toLocaleString()}元</span>
            <span class="text-muted small">${(t.buy_time||'').slice(0,16)}</span>
          </div>
          <div class="trade-row">
            <span class="trade-label down">卖出</span>
            <span>${t.sell_price.toFixed(2)} × ${t.shares}股 = ${t.sell_amount.toLocaleString()}元</span>
            <span class="text-muted small">${(t.sell_time||'').slice(0,16)}</span>
          </div>
          <div class="text-muted small mt-1">触发：${t.sell_signal}</div>
        </div>
      </div>`;
    }).join('');

    // 当前持仓（未平仓买入）
    const openRow = s.open ? `
      <div class="trade-detail-row">
        <div class="trade-detail-header">
          <span class="text-muted small">当前持仓</span>
          <span class="signal-badge signal-候选">持有中</span>
        </div>
        <div class="trade-detail-body">
          <div class="trade-row">
            <span class="trade-label up">买入</span>
            <span>${s.open_price.toFixed(2)} × ${s.open_shares}股</span>
            <span class="text-muted small">${(s.open_time||'').slice(0,16)}</span>
          </div>
        </div>
      </div>` : '';

    const detailId = `trade-detail-${s.code}`;
    const winRate  = s.trades.length
      ? Math.round(s.win / s.trades.length * 100) + '%'
      : '--';

    return `
    <div class="stock-card">
      <div class="d-flex justify-content-between align-items-center"
           onclick="toggleTradeDetail('${detailId}')" style="cursor:pointer">
        <div>
          <span class="code-name">${s.name}</span>
          <span class="code-tag">${s.code}</span>
          ${statusBadge}
        </div>
        <div class="text-end">
          <div class="fw-bold ${totalCls}">${totalSign}${s.total_pnl.toLocaleString()} 元</div>
          <div class="text-muted small">
            ${s.trades.length}笔已平仓 · 胜率${winRate}
          </div>
        </div>
      </div>
      <div class="trade-detail-wrap" id="${detailId}" style="display:none">
        <div class="mt-3">${tradeRows}${openRow}</div>
      </div>
    </div>`;
  }).join('');
}

function toggleTradeDetail(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
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
function _updateAccountBar(d) {
  try {
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

async function loadAccountBar() {
  // 当不在持仓 Tab 时，单独请求账户数据更新顶栏
  try {
    const d = await fetch('/api/account').then(r => r.json());
    _updateAccountBar(d);
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
  else if (tab === 'trades')   loadTrades();
  else if (tab === 'account')  loadAccount();
}

/* ── 交易时段判断 ────────────────────────────── */
function isTradingTime() {
  const now  = new Date();
  const day  = now.getDay();                         // 0=周日 6=周六
  if (day === 0 || day === 6) return false;
  const h = now.getHours(), m = now.getMinutes();
  const t = h * 60 + m;
  return (t >= 9 * 60 + 30 && t < 11 * 60 + 30) ||
         (t >= 13 * 60      && t < 15 * 60);
}

/* ── 自动刷新 ────────────────────────────────── */
function tick() {
  const trading = isTradingTime();
  const label   = document.getElementById('refresh-time');

  if (!trading) {
    label.textContent = '非交易时段';
    countdown = REFRESH_INTERVAL;   // 重置，开盘时立即触发一次刷新
    return;
  }

  countdown--;
  label.textContent = `${countdown}s 后刷新`;
  if (countdown <= 0) {
    countdown = REFRESH_INTERVAL;
    // 持仓 Tab 时 loadPositions() 内部会同步更新账户栏，无需单独请求
    if (activeTab !== 'positions') loadAccountBar();
    loadTab(activeTab);

    // 检测扫描是否完成（日期变化即完成）
    if (_scanningDateSnapshot !== null) {
      fetch('/api/scan').then(r => r.json()).then(data => {
        if (data.date && data.date !== _scanningDateSnapshot) {
          _resetScanBtn();
          if (activeTab === 'scan') loadScan();
          showToast('扫描完成', 'success');
        }
      }).catch(() => {});
    }
  }
}

// 初始化
loadAccountBar();
loadTab('positions');
setInterval(tick, 1000);
