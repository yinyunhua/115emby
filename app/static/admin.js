'use strict';

const $ = selector => document.querySelector(selector);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
const number = value => Number(value || 0).toLocaleString('zh-CN');
const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const pages = {
  scan: ['扫描管理', '让媒体井然有序，从同步你的目录开始。'],
  ov: ['媒体概览', '了解缩略图库的状态，安排空闲时段的自动任务。'],
  set: ['系统设置', '连接你的 115 账号，按需调整媒体库与扫描参数。'],
  pw: ['访问安全', '管理站点访问口令，守护你的私人媒体库。'],
  acc: ['账户管理', '给访客发独立口令，并限定他能看到的目录。'],
};
const settingFields = {
  APP_ID_115: ['f_appid', ''], HOME_CID: ['f_home', ''], HOME_NAME: ['f_homename', ''],
  THUMB_IMG_MIN_INTERVAL: ['f_imgint', '0.6'], THUMB_IMG_CONCURRENCY: ['f_imgconc', '2'],
  FAST_SCAN_INTERVAL: ['f_scanint', '0.5'], FAST_SCAN_CONCURRENCY: ['f_scanconc', '3'],
  FAST_SCAN_LOOKAHEAD: ['f_look', '12'], SCAN_INCREMENTAL: ['f_incr', '0'], SCAN_SKIP_WITHIN_S: ['f_skip', '0'],
};
let selected = [];
let trail = [{cid: '-1', name: '根目录'}];
let activeTab = 'scan';
let scanState = null;
let quickState = {};
let previousBusy = null;
let lastTaskType = null;
let lastJob = {};
let actionPending = false;
let stopRequested = false;
let refreshPromise = null;
let rootSavePromise = Promise.resolve();
let rootsReady = false;
let rootsSaving = 0;
let directoryRequest = 0;
let settingsLoaded = false;
let settingsDirty = false;
let settingsLoading = false;
let prewarmLoaded = false;
let toastTimer;

async function request(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST', cache: 'no-store', signal: controller.signal,
      ...(body === undefined ? {} : {headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}),
    });
    if (response.status === 401) {
      location.href = '/login?next=' + encodeURIComponent('/admin');
      throw new Error('登录已过期，请重新登录');
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败（HTTP ' + response.status + '）');
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('请求超时，请稍后重试');
    throw error;
  } finally { clearTimeout(timeout); }
}

function toast(message, kind = 'ok') {
  $('#toast').textContent = message;
  $('#toast').className = 'on ' + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('#toast').className = ''; }, 4500);
}

function disableFields(formId, disabled) {
  $('#' + formId).querySelectorAll('input, select, textarea').forEach(field => { field.disabled = disabled; });
}

function setSync(error) {
  $('#sync-status').textContent = error ? '连接中断，可重试' : '已更新 ' + new Date().toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false});
  $('#sync-status').classList.toggle('bad', !!error);
}

function fullBusy() {
  return !!(scanState && (scanState.running || scanState.current || scanState.queue?.length));
}

function updateControls() {
  const full = fullBusy();
  const quick = !!quickState.running;
  const busy = full || quick;
  const locked = actionPending || stopRequested || !scanState;
  $('#start').hidden = busy;
  $('#btnQuick').hidden = busy;
  $('#start').disabled = locked || !selected.length || !rootsReady || !!rootsSaving;
  $('#btnQuick').disabled = $('#start').disabled;
  $('#btnPause').hidden = !full || !!scanState?.paused;
  $('#btnResume').hidden = !full || !scanState?.paused;
  $('#btnCancel').hidden = !busy;
  $('#btnPause').disabled = $('#btnResume').disabled = $('#btnCancel').disabled = locked;
  $('#btnCancel').textContent = stopRequested ? '正在停止…' : '停止任务';
  $('#addsel').disabled = busy || actionPending || !rootsReady || !$('#rootsel').value;
  $('#clearroots').disabled = busy || actionPending;
  $('#pickcur').disabled = busy || actionPending;
  document.querySelectorAll('.remove-root, .folder .add').forEach(button => { button.disabled = busy || actionPending; });
}

function kpi(title, value, unit, symbol, tone = '') {
  return `<div class="card stat-card"><span class="stat-icon">${icon(symbol)}</span><h3>${escapeHtml(title)}</h3><div class="kpi ${tone}">${escapeHtml(value)}<small>${escapeHtml(unit)}</small></div></div>`;
}

function renderChips() {
  $('#root-count').textContent = selected.length + ' 个';
  $('#clearroots').hidden = !selected.length;
  $('#chips').innerHTML = selected.length ? selected.map((root, index) =>
    `<div class="chip">${icon('folder')}<div class="chip-name"><b title="${escapeHtml(root.name || root.cid)}">${escapeHtml(root.name || root.cid)}</b><small>CID ${escapeHtml(root.cid)}</small></div><button type="button" class="remove-root" data-index="${index}" aria-label="移除目录：${escapeHtml(root.name || root.cid)}">×</button></div>`
  ).join('') : `<div class="empty-roots">${icon('folder')}还没有选择目录<br>从上方添加一个目录，开始同步媒体库。</div>`;
  $('#chips').querySelectorAll('.remove-root').forEach(button => {
    button.onclick = () => {
      if (fullBusy() || quickState.running || actionPending) return;
      selected.splice(Number(button.dataset.index), 1);
      persistRoots();
    };
  });
  updateControls();
}

function persistRoots() {
  const roots = selected.map(root => ({...root}));
  rootsSaving++;
  rootSavePromise = rootSavePromise.catch(() => {}).then(() => request('/admin/scan/roots', {roots})).then(() => {
    try { localStorage.setItem('emby_roots', JSON.stringify(roots)); } catch {}
  }).catch(error => { toast('目录保存失败，请重新添加或移除后重试：' + error.message, 'bad'); throw error; }).finally(() => {
    rootsSaving--;
    updateControls();
  });
  rootSavePromise.catch(() => {});
  renderChips();
}

function addRoot(cid, name) {
  if (!cid || fullBusy() || quickState.running || actionPending) return;
  if (selected.some(root => root.cid === String(cid))) return toast('这个目录已经在扫描列表中');
  selected.push({cid: String(cid), name: name || String(cid)});
  persistRoots();
}

async function loadSavedRoots() {
  try {
    const data = await request('/admin/scan/roots');
    selected = Array.isArray(data.roots) ? data.roots.filter(root => root?.cid).map(root => ({cid: String(root.cid), name: String(root.name || root.cid)})) : [];
    try { localStorage.setItem('emby_roots', JSON.stringify(selected)); } catch {}
    rootsReady = true;
    renderChips();
  } catch (error) { toast('扫描目录读取失败，请刷新状态重试：' + error.message, 'bad'); }
}

async function loadRootOptions() {
  try {
    const data = await request('/api/dirs?parent=-1');
    $('#rootsel').innerHTML = '<option value="">选择 115 根目录…</option>' + (data.folders || []).map(folder => `<option value="${escapeHtml(folder.id)}">${escapeHtml(folder.name || folder.id)}</option>`).join('');
  } catch { $('#rootsel').innerHTML = '<option value="">目录读取失败，刷新状态重试</option>'; }
  updateControls();
}

async function openDir(cid, name, breadcrumbIndex) {
  const requestId = ++directoryRequest;
  const nextTrail = breadcrumbIndex === undefined ? (cid === '-1' ? [{cid, name}] : [...trail, {cid, name}]) : trail.slice(0, breadcrumbIndex + 1);
  $('#folders').textContent = '正在读取目录…';
  $('#pickcur').disabled = true;
  try {
    const data = await request('/api/dirs?parent=' + encodeURIComponent(cid));
    if (requestId !== directoryRequest) return;
    trail = nextTrail;
    $('#crumbs').innerHTML = trail.map((entry, index) => `<button type="button" data-index="${index}"${index === trail.length - 1 ? ' aria-current="location"' : ''}>${escapeHtml(entry.name)}</button>`).join('<span>/</span>');
    $('#crumbs').querySelectorAll('button').forEach(button => { button.onclick = () => {
      const index = Number(button.dataset.index);
      openDir(trail[index].cid, trail[index].name, index);
    }; });
    $('#pickcur').onclick = () => addRoot(cid, name);
    $('#folders').innerHTML = (data.folders || []).map(folder => `<div class="folder"><button type="button" class="folder-name" data-cid="${escapeHtml(folder.id)}" data-name="${escapeHtml(folder.name || folder.id)}">${icon('folder')}<span>${escapeHtml(folder.name || folder.id)}</span></button><button type="button" class="add" data-cid="${escapeHtml(folder.id)}" data-name="${escapeHtml(folder.name || folder.id)}" aria-label="添加目录：${escapeHtml(folder.name || folder.id)}">+</button></div>`).join('') || '<div class="hint">没有子目录，可以添加当前目录。</div>';
    $('#folders').querySelectorAll('.folder-name').forEach(button => { button.onclick = () => openDir(button.dataset.cid, button.dataset.name); });
    $('#folders').querySelectorAll('.add').forEach(button => { button.onclick = () => addRoot(button.dataset.cid, button.dataset.name); });
    updateControls();
  } catch (error) {
    if (requestId !== directoryRequest) return;
    $('#folders').textContent = '目录读取失败：' + error.message;
  }
}

/* ★ req-scanreport：检查新增内容完成后要说清"入库了多少"。
   以前只提示「任务已结束，媒体库统计已更新」，看不到新增数量。 */
function quickReport() {
  const q = quickState || {};
  const added = Number(q.new_items || 0);
  const folders = Number(q.folders || 0);
  const queued = Number(q.queued_thumbs || 0);
  const head = added > 0 ? `新增入库 ${number(added)} 项` : '没有发现新内容';
  const extra = [];
  if (folders > 0) extra.push(`新目录 ${number(folders)} 个`);
  if (queued > 0) extra.push(`已排队缩略图 ${number(queued)} 张`);
  return '检查完毕：' + head + (extra.length ? '（' + extra.join(' · ') + '）' : '') +
    ` · 检查 ${number(q.dirs)} 个目录，跳过 ${number(q.skipped)} 个未变化分支`;
}

function showDone(message, good = true) {
  $('#pdone').className = 'pdone on ' + (good ? 'good' : 'bad');
  $('#pdone').textContent = message;
}

function renderProgress(progress) {
  const full = fullBusy();
  const quick = !!quickState.running;
  const busy = full || quick;
  const job = scanState.job || {};
  if (scanState.job) lastJob = {...job};
  const pending = (progress.pending_dirs_by_depth || []).reduce((total, depth) => total + Number(depth.n || 0), 0);
  const done = Number(job.dirs_done || 0);
  const total = done + pending;
  const percent = total ? Math.min(100, Math.round(done * 100 / total)) : 0;
  const error = job.status === 'error' ? job.last_error || '未知错误' : '';
  let label = '等待扫描';
  let tone = '';
  let status = selected.length ? '目录已就绪。可完整扫描，或只检查最近新增的内容。' : '先在「扫描目录」中添加目录，再开始你的第一次同步。';
  if (quick) {
    label = '检查新增中';
    status = `已检查 ${number(quickState.dirs)} 个目录 · 跳过 ${number(quickState.skipped)} 个未变化分支 · 已新增入库 ${number(quickState.new_items)} 项`;
  } else if (error) {
    label = '扫描异常'; tone = 'bad'; status = error;
  } else if (full && scanState.paused) {
    label = scanState.current ? '等待暂停' : '已暂停'; tone = 'warn';
    status = scanState.current ? '已请求暂停：当前根目录扫描结束后暂停队列，已入库内容保留。' : '任务队列已暂停，点击「继续扫描」恢复。';
  } else if (full) {
    label = '正在扫描';
    status = `${scanState.current?.name || '正在准备任务'} · 已扫 ${number(done)} 个目录，待扫 ${number(pending)} 个 · 本次已读 ${number(job.items_found)} 项`;
  }
  if (stopRequested && busy) { label = '正在停止'; tone = 'warn'; status = '已发送停止请求，等待当前服务端操作结束；已入库内容保留。'; }
  $('#pstate').textContent = label;
  $('#pstate').className = 'status-pill ' + tone;
  $('#pstatusline').textContent = status;
  $('#progress-caption').textContent = quick ? '正在检查变化的目录分支' : full ? '当前根目录 · 动态进度' : '准备好，开始下一次同步';
  $('#ppct').innerHTML = quick ? '—' : percent + '<span>%</span>';
  $('#pbar').style.width = percent + '%';
  $('#pbar').style.background = tone === 'bad' ? 'var(--bad)' : tone === 'warn' ? 'var(--warn)' : 'var(--accent)';
  $('#progressbar').classList.toggle('indeterminate', quick || (full && !total && !scanState.paused));
  if (quick) $('#progressbar').removeAttribute('aria-valuenow');
  else $('#progressbar').setAttribute('aria-valuenow', String(percent));
  const kinds = Object.fromEntries((progress.by_kind || []).map(kind => [kind.kind, kind.n]));
  $('#pkpi').innerHTML =
    kpi('库内图片', number(kinds.image), '张', 'image') + kpi('库内视频', number(kinds.video), '个', 'video') +
    kpi('媒体文件总数', number(progress.files_total ?? Object.values(kinds).reduce((sum, count) => sum + Number(count), 0)), '项', 'grid') +
    kpi('已扫目录', full ? number(done) : quick ? number(quickState.dirs) : '—', '个', 'folder') +
    kpi('待扫目录', full ? number(pending) : '—', '个', 'clock', pending ? 'warn' : '') +
    kpi('本次已读', full ? number(job.items_found) : quick ? number(quickState.new_items) : '—', '项', 'check');
  const scanned = new Map((progress.scanned_dirs_by_depth || []).map(depth => [Number(depth.depth), Number(depth.n)]));
  const waiting = new Map((progress.pending_dirs_by_depth || []).map(depth => [Number(depth.depth), Number(depth.n)]));
  const depths = [...new Set([...scanned.keys(), ...waiting.keys()])].sort((left, right) => left - right);
  $('#pdepth').innerHTML = '<thead><tr><th scope="col">目录层级</th><th scope="col">库内已扫</th><th scope="col">本次待扫</th></tr></thead><tbody>' + (depths.length ? depths.map(depth => `<tr><td>${depth} 级目录</td><td>${number(scanned.get(depth))}</td><td>${number(waiting.get(depth))}</td></tr>`).join('') : '<tr><td colspan="3" class="dim">暂无目录扫描记录</td></tr>') + '</tbody>';
  $('#pfailed').textContent = progress.failed_dirs_total ? `有 ${number(progress.failed_dirs_total)} 个失败目录，下次扫描会重试。` : '暂无失败目录';
  if (previousBusy === true && !busy) {
    const cancelled = stopRequested || (lastTaskType === 'quick' ? quickState.stop : lastJob.status === 'canceled' || lastJob.last_error === 'cancelled');
    const failure = lastTaskType === 'quick' ? quickState.error : error || (lastJob.status === 'error' ? lastJob.last_error : '');
    const okMessage = lastTaskType === 'quick' ? quickReport() : '任务已结束，媒体库统计已更新。';
    showDone(cancelled ? '任务已停止，已入库的媒体内容保留。' : failure ? '任务结束，发生错误：' + failure : okMessage, !failure);
    stopRequested = false;
  }
  if (busy) lastTaskType = quick ? 'quick' : 'full';
  previousBusy = busy;
  updateControls();
}

async function refresh() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    try {
      const [multi, progress, quick] = await Promise.all([request('/admin/scan/multi'), request('/admin/scan/progress'), request('/admin/scan/quick')]);
      scanState = multi;
      quickState = quick;
      renderProgress(progress);
      if (activeTab === 'scan') setSync();
      return true;
    } catch (error) {
      scanState = null;
      $('#pstate').textContent = '连接中断';
      $('#pstate').className = 'status-pill bad';
      $('#pstatusline').textContent = '状态读取失败：' + error.message + '。请点击右上角刷新状态重试。';
      if (activeTab === 'scan') setSync(error);
      updateControls();
      return false;
    } finally { refreshPromise = null; }
  })();
  return refreshPromise;
}

async function scanAction(action) {
  if (actionPending || stopRequested || !scanState) return;
  const quick = !!quickState.running;
  const busy = fullBusy() || quick;
  if (['start', 'quick'].includes(action) && (busy || !selected.length || !rootsReady || rootsSaving)) return;
  const messages = {
    start: '开始全量扫描？\n\n目录：' + selected.map(root => root.name || root.cid).join('、') + '\n将递归扫描所有层级的图片和视频，任务在服务端执行。',
    quick: '检查新增内容？\n\n只检查已选目录中发生变化的分支。首次建库请使用全量扫描。',
    cancel: '停止当前任务？\n\n已入库内容会保留，未完成部分不会继续。',
  };
  if (messages[action] && !window.confirm(messages[action])) return;
  actionPending = true;
  updateControls();
  try {
    await rootSavePromise;
    if (refreshPromise) await refreshPromise;
    if (action === 'start') await request('/admin/scan/multi', {roots: selected, depth: 0, kinds: ['image', 'video']});
    else if (action === 'quick') await request('/admin/scan/quick', {});
    else await request(quick ? '/admin/scan/quick/stop' : '/admin/scan/multi/' + action, {});
    if (action === 'start' || action === 'quick') {
      $('#pdone').className = 'pdone';
      lastJob = {};
    }
    if (action === 'cancel') stopRequested = true;
    toast({start: '全量扫描已提交', quick: '新增检查已提交', pause: '暂停请求已发送', resume: '任务已继续', cancel: '停止请求已发送'}[action]);
    await refresh();
  } catch (error) { toast('操作失败：' + error.message, 'bad'); }
  finally { actionPending = false; updateControls(); }
}

/* ★ req-thumbnow：媒体概览「立即生成缩略图」的状态与文案。
   状态来自 /admin/thumb/stats 的 prewarm_now（后端记在 app.state.prewarm_holder）。 */
const THUMB_STOP_TEXT = {
  'queue-empty': '队列已抓完', 'window-closed': '到达时间段结束', 'budget': '达到本次上限',
  'paused': '已暂停', 'stop': '已停止',
};
let thumbNowTimer = 0;
function renderThumbNow(stats, pendingReal) {
  const now = stats.prewarm_now || {};
  const pw = stats.prewarm || {};
  const button = $('#btnThumbNow');
  const badge = $('#thumb-now-badge');
  const dot = $('#thumb-now-dot');
  const hint = $('#thumb_now_hint');
  if (!button || !hint) return;
  const pending = Number(pendingReal || 0);
  button.disabled = !!now.running;
  if (badge) {
    badge.textContent = now.running ? '生成中…' : pending > 0 ? `待生成 ${number(pending)} 项` : '队列已空';
    badge.classList.toggle('busy', !!now.running);
  }
  if (dot) dot.classList.toggle('on', !!now.running);
  hint.className = 'hint';
  if (now.running) {
    hint.classList.add('busy');
    hint.textContent = `正在生成…剩余 ${number(pending)} 项`;
  } else if (now.error) {
    hint.classList.add('bad');
    hint.textContent = '上次生成出错：' + now.error;
  } else if (now.finished) {
    const bits = [`抓取 ${number(now.fetched)} 张`];
    if (Number(now.hit || 0)) bits.push(`命中缓存 ${number(now.hit)} 张`);
    if (Number(now.failed || 0)) bits.push(`失败 ${number(now.failed)} 张`);
    if (now.stopped) bits.push(THUMB_STOP_TEXT[now.stopped] || now.stopped);
    hint.textContent = '上次生成：' + bits.join(' · ');
  } else if (!pending) {
    hint.textContent = '当前没有等待生成的缩略图';
  } else {
    hint.textContent = `有 ${number(pending)} 项等待生成`;
  }
  /* ★ 正在生成时每 3 秒自动刷新，否则进度不会动（本页只为扫描页起了 4 秒轮询） */
  clearTimeout(thumbNowTimer);
  if (now.running && activeTab === 'ov') {
    thumbNowTimer = setTimeout(() => {
      if (activeTab === 'ov' && !document.hidden) loadStats();
    }, 3000);
  }
}

async function thumbNow() {
  const button = $('#btnThumbNow');
  if (!button || button.disabled) return;
  const pending = Number(button.dataset.pending || 0);
  const ask = pending > 0
    ? `立即生成 ${number(pending)} 项缩略图？\n\n仍按相同的安全间隔限速，随时可以停止。`
    : '当前没有等待生成的缩略图，仍要执行一次检查吗？';
  if (!window.confirm(ask)) return;
  button.disabled = true;                        /* 先禁用防连点 */
  try {
    await request('/admin/thumb/prewarm', {});
    toast('已开始生成缩略图');
  } catch (error) {
    toast('操作失败：' + error.message, 'bad');
  } finally {
    /* ★ 成功路径原来没有恢复按钮 → 点一次就一直灰着（实测 btnDisabled 一直 true）。
       这里统一交给 loadStats → renderThumbNow 按"服务端是否在跑"决定可用性。 */
    await loadStats();
  }
}

async function loadStats() {
  try {
    const stats = await request('/admin/thumb/stats');
    const queue = stats.queue || {};
    const bad = stats.queue_bad_real ?? (Number(queue.failed || 0) + Number(queue.skipped || 0));
    /* req-scanqueue：等待生成要看**真实积压**。
       queue.pending 只统计 thumb_src 里已有记录的行；从未入队的媒体
       在表里根本没有行，GROUP BY 数不到 → 以前会永远显示 0。
       queue_pending_real = pending + 从未入队。 */
    const pendingReal = Number(
      stats.queue_pending_real ?? (Number(queue.pending || 0) + Number(stats.queue_never_queued || 0))
    );
    if ($('#btnThumbNow')) $('#btnThumbNow').dataset.pending = String(pendingReal);
    $('#kpis').innerHTML = kpi('已存缩略图', number(stats.thumbs), '张', 'image') +
      kpi('缓存占用', number(stats.disk_mb), 'MB', 'grid') + kpi('等待生成', number(pendingReal), '项', 'clock') +
      kpi('失败或跳过', number(bad), '项', 'folder', bad ? 'warn' : '') +
      /* req-numalign：已自动排除也放进 KPI，这样「已存 + 待生成 + 已排除」能直接加出总数 */
      kpi('已自动排除', number(stats.queue_excluded), '项', 'folder') +
      kpi('请求间隔', stats.throttle?.min_interval_s ?? '—', '秒', 'bolt');
    const pwInfo = stats.prewarm || {};
    /* ★ req-align：去掉前 3 行（库内缩略图 / 等待生成 / 已自动排除）——
       它们与上方 6 张 KPI 卡片完全重复，删掉后「运行信息」才与
       「定时生成缩略图」高度对齐。 */
    $('#state').innerHTML = [
      ['库内媒体总数', number(stats.items_media) + ' 项（= 已存 + 待生成 + 已排除）'],
      ['请求间隔', (stats.throttle?.min_interval_s ?? '—') + ' 秒 / 张'],
      /* req-thumbnow：让人一眼看出定时窗口与立即生成各自的状态。
         ⚠️ 这里必须用本函数内的 pwInfo —— 曾经误用 renderThumbNow 里的 pw，
            导致 loadStats 整体抛 ReferenceError（页面上显示"暂时无法读取"）。 */
      ['定时生成', !pwInfo.enabled ? '已关闭'
        : pwInfo.inside ? ('时间段内' + (pwInfo.running ? ' · 正在生成' : ' · 待命中'))
        : ('等待 ' + (pwInfo.window || '时间段'))],
      ['立即生成', !stats.prewarm_now?.finished ? '尚未执行'
        : (stats.prewarm_now.running ? '正在生成' : (THUMB_STOP_TEXT[stats.prewarm_now.stopped] || '已结束'))],
      ['加密方式', stats.enc || '—'],
    ].map(([label, value]) => `<tr><th scope="row">${escapeHtml(label)}</th><td>${escapeHtml(value)}</td></tr>`).join('');
    $('#alg').textContent = '密钥指纹：' + (stats.key_fp || '—') + ' · 小图 / GIF / 非媒体的排除不计入失败。';
    renderThumbNow(stats, pendingReal);
    if (!prewarmLoaded) await loadPrewarm();
    setSync();
  } catch (error) {
    $('#kpis').innerHTML = `<div class="card"><h2>暂时无法读取</h2><p class="hint bad">${escapeHtml(error.message)}，请刷新重试。</p></div>`;
    setSync(error);
  }
}

async function loadPrewarm() {
  $('#save-prewarm').disabled = true;
  disableFields('prewarm-form', true);
  try {
    const settings = await request('/admin/settings');
    $('#pw_enabled').value = settings.THUMB_PREWARM_ENABLED?.value === '1' ? '1' : '0';
    const [start = '02:00', end = '06:00'] = (settings.THUMB_PREWARM_WINDOW?.value || '02:00-06:00').split('-');
    $('#pw_start').value = start.trim();
    $('#pw_end').value = end.trim();
    $('#pw_hint').textContent = '时间以服务端时区为准';
    prewarmLoaded = true;
  } catch (error) { $('#pw_hint').textContent = '计划读取失败：' + error.message; }
  finally { $('#save-prewarm').disabled = !prewarmLoaded; disableFields('prewarm-form', !prewarmLoaded); }
}

async function savePrewarm(event) {
  event.preventDefault();
  if (!prewarmLoaded || $('#save-prewarm').disabled) return;
  if ($('#pw_start').value === $('#pw_end').value && $('#pw_enabled').value === '1' && !window.confirm('开始和结束时间相同，表示全天生成缩略图。确定保存？')) return;
  $('#save-prewarm').disabled = true;
  disableFields('prewarm-form', true);
  try {
    await request('/admin/settings', {THUMB_PREWARM_ENABLED: $('#pw_enabled').value, THUMB_PREWARM_WINDOW: $('#pw_start').value + '-' + $('#pw_end').value});
    $('#pw_hint').textContent = '计划已保存 · 以服务端时区为准';
    toast('缩略图生成计划已保存');
  } catch (error) { $('#pw_hint').textContent = '保存失败：' + error.message; toast('保存失败：' + error.message, 'bad'); }
  finally { $('#save-prewarm').disabled = false; disableFields('prewarm-form', false); }
}

async function loadSettings() {
  if (settingsLoading) return;
  settingsLoading = true;
  disableFields('settings-form', true);
  $('#save-settings').disabled = true;
  $('#reload-settings').disabled = true;
  try {
    const settings = await request('/admin/settings');
    Object.entries(settingFields).forEach(([name, [id, fallback]]) => { $('#' + id).value = settings[name]?.value ?? fallback; });
    $('#f_cookie').value = '';
    $('#f_token').value = '';
    $('#f_cookie').placeholder = settings.COOKIE_115?.set ? 'Cookie 已设置，留空保留原值；输入新值可替换' : 'UID=...; CID=...; SEID=...; KID=...';
    $('#f_token').placeholder = settings.ACCESS_TOKEN_115?.set ? 'Token 已设置，留空保留原值' : '可选，留空不修改';
    $('#cred').textContent = 'Cookie ' + (settings.COOKIE_115?.set ? '已配置' : '未配置') + ' · Token ' + (settings.ACCESS_TOKEN_115?.set ? '已配置' : '未配置') + ' · 115 接口' + (settings._ready?.upstream ? '已就绪' : '未就绪');
    $('#sethint').textContent = '当前设置已读取 · 修改后记得保存';
    settingsDirty = false;
    settingsLoaded = true;
    setSync();
  } catch (error) {
    $('#sethint').textContent = '读取失败：' + error.message + '，请重新读取后再保存。';
    settingsLoaded = false;
    setSync(error);
  } finally {
    settingsLoading = false;
    $('#save-settings').disabled = !settingsLoaded;
    $('#reload-settings').disabled = false;
    disableFields('settings-form', !settingsLoaded);
  }
}

async function saveSettings(event) {
  event.preventDefault();
  if (!settingsLoaded || $('#save-settings').disabled) return;
  const body = Object.fromEntries(Object.entries(settingFields).map(([name, [id]]) => [name, $('#' + id).value.trim()]));
  const cookie = $('#f_cookie').value.trim();
  const token = $('#f_token').value.trim();
  if (cookie) body.COOKIE_115 = cookie;
  if (token) body.ACCESS_TOKEN_115 = token;
  $('#save-settings').disabled = true;
  $('#reload-settings').disabled = true;
  disableFields('settings-form', true);
  try {
    await request('/admin/settings', body);
    $('#f_cookie').value = '';
    $('#f_token').value = '';
    settingsDirty = false;
    $('#sethint').textContent = '所有设置已保存';
    setSync();
    toast('系统设置已保存');
  } catch (error) { $('#sethint').textContent = '保存失败：' + error.message; toast('保存失败：' + error.message, 'bad'); }
  finally { $('#save-settings').disabled = false; $('#reload-settings').disabled = false; disableFields('settings-form', false); }
}

async function loadSitePw() {
  try {
    const state = await request('/api/site/state');
    $('#pwstate').innerHTML = [
      ['站点访问口令', state.has_password ? '已设置' : '未设置'],
      ['当前浏览器', state.logged_in ? '已登录' : '未登录'],
      ['上次修改', state.changed_at ? new Date(state.changed_at * 1000).toLocaleString('zh-CN') : '尚未修改'],
    ].map(([label, value]) => `<tr><th scope="row">${label}</th><td>${escapeHtml(value)}</td></tr>`).join('');
    $('#pw_old').required = !!state.has_password;
    setSync();
  } catch (error) { $('#pwstate').innerHTML = `<tr><td class="bad">读取失败：${escapeHtml(error.message)}</td></tr>`; setSync(error); }
}

async function changeSitePw(event) {
  event.preventDefault();
  if ($('#save-password').disabled) return;
  const password = $('#pw_new').value;
  if (password !== $('#pw_new2').value) {
    $('#pwhint').textContent = '两次新口令不一致，请重新确认';
    $('#pw_new2').focus();
    return;
  }
  if (password.length < 4) { $('#pwhint').textContent = '新口令至少 4 位'; return; }
  if (!window.confirm('确认更新站点访问口令？其他设备将需要重新登录。')) return;
  $('#save-password').disabled = true;
  try {
    const result = await request('/api/site/password', {old: $('#pw_old').value, new: password});
    if (!result.ok) throw new Error(result.detail || '修改未成功');
    $('#pw_old').value = $('#pw_new').value = $('#pw_new2').value = '';
    $('#pwhint').textContent = '口令已更新，其他设备需重新登录';
    toast('站点访问口令已更新');
    await loadSitePw();
  } catch (error) { $('#pwhint').textContent = '更新失败：' + error.message; toast('更新失败：' + error.message, 'bad'); }
  finally { $('#save-password').disabled = false; }
}


/* ================= req-accounts：账户管理 =================
   规则（务必与后端保持一致）：
   · 授权目录只能选「扫描目录」里已添加的根目录之下的目录
   · 保存时后端会**再严格校验一次**；前端校验只为即时反馈，不能当作安全边界
   · 账户权限的权威判定在服务端（accounts.py），前端只负责勾选与展示 */
let ACC = { admin: '', accounts: [], roots: [], cur: '', dirs: new Set(), dirty: false };

async function accLoad() {
  const box = $('#accList');
  if (box) box.innerHTML = '<div class="hint">正在读取…</div>';
  try {
    const r = await fetch('/admin/accounts', { credentials: 'same-origin' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    ACC.admin = d.admin || '';
    ACC.accounts = d.accounts || [];
    ACC.roots = d.roots || [];
  } catch (e) {
    if (box) box.innerHTML = '<div class="hint bad">读取失败：' + escapeHtml(String(e.message || e)) + '</div>';
    return;
  }
  const cnt = $('#accCount');
  if (cnt) cnt.textContent = ACC.accounts.length + ' 个普通账户';
  renderAccList();
  if (!$('#accDirTree').dataset.loaded) renderDirTree();
}

function renderAccList() {
  const box = $('#accList');
  if (!box) return;
  const rows = ACC.accounts.map(a => {
    const dirs = a.dirs || [];
    const names = a.dir_names || dirs;
    const on = a.enabled !== false;
    /* ★ req-accdirs：右侧显示**可访问目录的名字**（原来只有"授权目录 N 个"，
       管理员看不出到底授权了哪几个）。目录多时折叠成前 3 个 + "+N"。 */
    const shown = names.slice(0, 3);
    const more = names.length - shown.length;
    const chips = shown.map((nm, i) =>
      '<span class="acc-chip" title="' + escapeHtml(dirs[i] || nm) + '">' +
      escapeHtml(nm) + '</span>').join('') +
      (more > 0 ? '<span class="acc-chip more" title="' + escapeHtml(names.slice(3).join('、')) +
                  '">+' + more + '</span>' : '');
    const dirBox = dirs.length
      ? '<span class="acc-dirnames">' + chips + '</span>'
      : '<span class="acc-dirnames none">未授权任何目录（看不到内容）</span>';
    return '<div class="acc-row' + (a.user === ACC.cur ? ' on' : '') + '" data-user="' + escapeHtml(a.user) + '">' +
      '<div class="acc-main"><b>' + escapeHtml(a.user) + '</b>' +
      '<span class="acc-tag ' + (on ? 'ok' : 'off') + '">' + (on ? '启用' : '停用') + '</span>' +
      '<span class="acc-dirs">' + dirs.length + ' 个目录</span>' +
      (a.note ? '<span class="acc-note">' + escapeHtml(a.note) + '</span>' : '') +
      '</div>' +
      '<div class="acc-dirbox">' + dirBox + '</div>' +
      '<div class="acc-ops">' +
      '<button type="button" class="linkbtn" data-act="edit">编辑</button>' +
      '<button type="button" class="linkbtn danger" data-act="del">删除</button>' +
      '</div></div>';
  }).join('');
  box.innerHTML = rows || '<div class="hint">还没有普通账户。点右上角「新增账户」。</div>';
  box.querySelectorAll('.acc-row').forEach(el => {
    el.addEventListener('click', ev => {
      const act = ev.target && ev.target.dataset ? ev.target.dataset.act : '';
      const u = el.dataset.user;
      if (act === 'del') return accDelete(u);
      accPick(u);
    });
  });
}

function accPick(u) {
  ACC.cur = u;
  const a = ACC.accounts.find(x => x.user === u) || {};
  ACC.dirs = new Set(a.dirs || []);
  if ($('#accUser')) { $('#accUser').value = a.user || ''; $('#accUser').disabled = true; }
  if ($('#accPw')) $('#accPw').value = '';
  if ($('#accNote')) $('#accNote').value = a.note || '';
  if ($('#accEnabled')) $('#accEnabled').value = (a.enabled === false ? '0' : '1');
  if ($('#accSaveHint')) $('#accSaveHint').textContent = '编辑中：' + u + '（留空口令表示不改）';
  if ($('#accSave')) $('#accSave').disabled = false;
  if ($('#accDirHint')) $('#accDirHint').textContent = '勾选 ' + u + ' 可见的目录（保存后立即生效，旧会话会失效）。';
  renderAccList();
  renderDirTree().then(() => accReveal());
}

/* 逐级展开到已授权的目录，让管理员一眼看到"选了哪几个" */
async function accReveal() {
  for (const d of Array.from(ACC.dirs)) {
    try {
      const r = await fetch('/admin/accounts/path?id=' + encodeURIComponent(d),
                            { credentials: 'same-origin' });
      if (!r.ok) continue;
      const { path } = await r.json();
      for (const fid of (path || [])) {
        const node = document.querySelector('#accDirTree [data-fid="' + CSS.escape(fid) + '"]');
        if (!node) continue;
        const kids = node.querySelector('.acc-kids');
        const exp = node.querySelector('.acc-exp');
        if (kids && kids.dataset.open !== '1') { exp.click(); await new Promise(x => setTimeout(x, 220)); }
      }
    } catch (e) { /* 展开失败不影响授权本身 */ }
  }
  syncDirChecks();
}

function accNew() {
  ACC.cur = '';
  ACC.dirs = new Set();
  if ($('#accUser')) { $('#accUser').value = ''; $('#accUser').disabled = false; $('#accUser').focus(); }
  if ($('#accPw')) $('#accPw').value = '';
  if ($('#accNote')) $('#accNote').value = '';
  if ($('#accEnabled')) $('#accEnabled').value = '1';
  if ($('#accSave')) $('#accSave').disabled = false;
  if ($('#accSaveHint')) $('#accSaveHint').textContent = '新账户：填账户名与口令，再勾目录。';
  if ($('#accDirHint')) $('#accDirHint').textContent = '勾选新账户可见的目录。';
  renderAccList(); renderDirTree();
}

async function accDelete(u) {
  if (!confirm('删除账户 ' + u + '？该账户的所有会话会立即失效。')) return;
  try {
    const r = await fetch('/admin/accounts/' + encodeURIComponent(u), {
      method: 'DELETE', credentials: 'same-origin' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
    if (ACC.cur === u) accNew();
    toast('已删除账户 ' + u);
  } catch (e) {
    toast('删除失败：' + String(e.message || e));
  }
  accLoad();
}

/* ---------- 目录树（只列根目录之下的） ---------- */
async function renderDirTree(parent) {
  const box = $('#accDirTree');
  if (!box) return;
  if (!parent) { ACC._cache = {}; box.innerHTML = '<div class="hint">正在读取目录…</div>'; }
  try {
    const q = parent ? ('?parent=' + encodeURIComponent(parent)) : '';
    const r = await fetch('/admin/accounts/dirs' + q, { credentials: 'same-origin' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
    if (!parent) {
      box.innerHTML = '';
      box.dataset.loaded = '1';
      const roots = d.folders || [];
      if (!roots.length) {
        box.innerHTML = '<div class="hint bad">还没有添加任何根目录。请先到「扫描管理 → 扫描目录」添加根目录，再回来授权。</div>';
        return;
      }
      roots.forEach(f => box.appendChild(accDirNode(f, 0)));
    } else {
      const wrap = box.querySelector('[data-fid="' + CSS.escape(parent) + '"] .acc-kids');
      if (wrap) {
        wrap.innerHTML = '';
        (d.folders || []).forEach(f => wrap.appendChild(accDirNode(f, Number(wrap.dataset.level || 0) + 1)));
      }
    }
    syncDirChecks();
  } catch (e) {
    box.innerHTML = '<div class="hint bad">目录读取失败：' + escapeHtml(String(e.message || e)) + '</div>';
  }
}

function accDirNode(f, level) {
  const el = document.createElement('div');
  el.className = 'acc-node';
  el.dataset.fid = f.id;
  el.style.paddingLeft = (level * 16) + 'px';
  const checked = ACC.dirs.has(f.id) ? ' checked' : '';
  el.innerHTML =
    '<div class="acc-node-row">' +
      '<button type="button" class="acc-exp" title="展开/收起">' + (f.root ? '▸' : '▸') + '</button>' +
      '<label><input type="checkbox" value="' + escapeHtml(f.id) + '"' + checked + '>' +
      '<span class="acc-nm">' + escapeHtml(f.name || f.id) + '</span>' +
      (f.root ? '<span class="acc-tag root">根目录</span>' : '') + '</label>' +
    '</div><div class="acc-kids" data-level="' + level + '"></div>';
  const exp = el.querySelector('.acc-exp');
  const kids = el.querySelector('.acc-kids');
  exp.addEventListener('click', async ev => {
    ev.preventDefault();
    if (kids.dataset.open === '1') {
      kids.innerHTML = ''; kids.dataset.open = '0'; exp.textContent = '▸';
      return;
    }
    kids.dataset.open = '1'; exp.textContent = '▾';
    kids.innerHTML = '<div class="hint">读取中…</div>';
    try {
      const r = await fetch('/admin/accounts/dirs?parent=' + encodeURIComponent(f.id),
                            { credentials: 'same-origin' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
      kids.innerHTML = '';
      const fs = d.folders || [];
      if (!fs.length) { kids.innerHTML = '<div class="hint">（没有子目录）</div>'; return; }
      fs.forEach(x => kids.appendChild(accDirNode(x, level + 1)));
      syncDirChecks();
    } catch (e) {
      kids.innerHTML = '<div class="hint bad">' + escapeHtml(String(e.message || e)) + '</div>';
    }
  });
  const cb = el.querySelector('input[type=checkbox]');
  cb.addEventListener('change', () => {
    if (cb.checked) ACC.dirs.add(cb.value); else ACC.dirs.delete(cb.value);
    updateDirCount();
  });
  return el;
}

function syncDirChecks() {
  document.querySelectorAll('#accDirTree input[type=checkbox]').forEach(cb => {
    cb.checked = ACC.dirs.has(cb.value);
  });
  /* ★ 父目录半选：子目录里有选中的，父级显示 indeterminate，
     否则折叠状态下管理员看不出"里面选了东西"。倒序处理（先子后父）。 */
  [...document.querySelectorAll('#accDirTree .acc-node')].reverse().forEach(n => {
    const cb = n.querySelector(':scope > .acc-node-row input[type=checkbox]');
    if (!cb) return;
    if (!cb.checked) {
      const has = n.querySelector('.acc-kids input[type=checkbox]:checked, .acc-kids input:indeterminate');
      cb.indeterminate = !!has;
    } else {
      cb.indeterminate = false;
    }
  });
  updateDirCount();
}

function updateDirCount() {
  const h = $('#accDirHint');
  if (h) {
    const who = ACC.cur || ($('#accUser') ? $('#accUser').value.trim() : '') || '新账户';
    h.textContent = who + '：已选 ' + ACC.dirs.size + ' 个目录' +
      (ACC.dirs.size ? '' : '（不选任何目录 = 他什么都看不到）');
  }
}

async function accSave() {
  const btn = $('#accSave');
  const user = ($('#accUser') ? $('#accUser').value.trim() : '');
  const pw = ($('#accPw') ? $('#accPw').value : '');
  if (!user) { toast('请填账户名'); return; }
  if (!ACC.cur && pw.trim().length < 4) { toast('新账户的口令至少 4 位'); return; }
  const body = { user: user, dirs: Array.from(ACC.dirs) };
  if (pw) body.pw = pw;
  if ($('#accNote')) body.note = $('#accNote').value.trim();
  if ($('#accEnabled')) body.enabled = ($('#accEnabled').value === '1');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/admin/accounts', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
    toast('已保存：' + user + '（授权 ' + (d.account.dirs || []).length + ' 个目录）');
    ACC.cur = user;
    await accLoad();
    accPick(user);
  } catch (e) {
    // 后端会给出明确原因（例如"这些目录不在已添加的根目录下"），原样展示
    toast('保存失败：' + String(e.message || e));
  } finally {
    if (btn) btn.disabled = false;
  }
}

function switchTab(tab) {
  if (!pages[tab]) return;
  activeTab = tab;
  document.querySelectorAll('.navigation button').forEach(button => {
    const current = button.dataset.tab === tab;
    button.classList.toggle('on', current);
    if (current) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  document.querySelectorAll('.tabs').forEach(section => {
    const current = section.id === 'tab-' + tab;
    section.classList.toggle('on', current);
    section.hidden = !current;
  });
  $('#page-title').textContent = $('#breadcrumb').textContent = pages[tab][0];
  $('#page-description').textContent = pages[tab][1];
  document.title = pages[tab][0] + ' · 115emby';
  history.replaceState(null, '', '#' + tab);
  $('#sync-status').textContent = '正在读取…';
  if (tab === 'scan') refresh();
  if (tab === 'ov') loadStats();
  if (tab === 'set') {
    if (!settingsLoaded) loadSettings();
    else $('#sync-status').textContent = settingsDirty ? '有未保存的修改' : '已读取设置';
  }
  if (tab === 'acc') accLoad();
  if (tab === 'pw') loadSitePw();
}

document.querySelectorAll('.navigation button').forEach(button => { button.onclick = () => switchTab(button.dataset.tab); });
$('#start').onclick = () => scanAction('start');
$('#btnQuick').onclick = () => scanAction('quick');
$('#btnPause').onclick = () => scanAction('pause');
$('#btnResume').onclick = () => scanAction('resume');
$('#btnCancel').onclick = () => scanAction('cancel');
$('#rootsel').onchange = updateControls;
$('#addsel').onclick = () => {
  const option = $('#rootsel').selectedOptions[0];
  if (option?.value) addRoot(option.value, option.textContent);
};
$('#clearroots').onclick = () => {
  if (fullBusy() || quickState.running || actionPending) return;
  if (!window.confirm('清空所有已选扫描目录？已入库内容不会删除。')) return;
  selected = [];
  persistRoots();
};
$('#browse').onclick = () => {
  const picker = $('#picker');
  picker.hidden = !picker.hidden;
  $('#browse').setAttribute('aria-expanded', String(!picker.hidden));
  if (!picker.hidden) openDir(trail[trail.length - 1].cid, trail[trail.length - 1].name, trail.length - 1);
};
$('#btnRefresh').onclick = async () => {
  $('#btnRefresh').disabled = true;
  try {
    if (activeTab === 'scan') await Promise.all([refresh(), ...(!rootsReady ? [loadSavedRoots()] : []), ...(!$('#rootsel').options.length || !$('#rootsel').options[1] ? [loadRootOptions()] : [])]);
    else if (activeTab === 'ov') await loadStats();
    else if (activeTab === 'acc') await accLoad();
    else if (activeTab === 'pw') await loadSitePw();
    else if (settingsDirty) toast('设置有未保存的修改。要放弃修改，请点击底部「重新读取」。');
    else await loadSettings();
  } finally { $('#btnRefresh').disabled = false; }
};
$('#prewarm-form').onsubmit = savePrewarm;
$('#btnThumbNow').onclick = thumbNow;
$('#settings-form').onsubmit = saveSettings;
$('#password-form').onsubmit = changeSitePw;
$('#settings-form').addEventListener('input', () => {
  settingsDirty = true;
  $('#sethint').textContent = '有未保存的修改';
  $('#sync-status').textContent = '有未保存的修改';
});
$('#reload-settings').onclick = () => {
  if (settingsDirty && !window.confirm('放弃未保存的修改，重新读取服务端设置？')) return;
  loadSettings();
};
window.addEventListener('beforeunload', event => {
  if (!settingsDirty) return;
  event.preventDefault();
  event.returnValue = '';
});
$('#accNew')?.addEventListener('click', accNew);
$('#accReload')?.addEventListener('click', accLoad);
$('#accSave')?.addEventListener('click', accSave);
$('#accClearDirs')?.addEventListener('click', () => { ACC.dirs = new Set(); syncDirChecks(); });

document.addEventListener('visibilitychange', () => { if (!document.hidden && activeTab === 'scan') refresh(); });

$('#save-settings').disabled = $('#save-prewarm').disabled = true;
$('#pkpi').innerHTML = ['库内图片', '库内视频', '媒体文件总数', '已扫目录', '待扫目录', '本次已读'].map((title, index) => kpi(title, '—', '', ['image', 'video', 'grid', 'folder', 'clock', 'check'][index])).join('');
renderChips();
loadSavedRoots();
loadRootOptions();
refresh();
switchTab(location.hash.slice(1) || 'scan');
setInterval(() => { if (!document.hidden && activeTab === 'scan') refresh(); }, 4000);
