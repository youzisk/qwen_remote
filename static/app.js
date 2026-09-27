'use strict';

const $ = (id) => document.getElementById(id);
const SIZES = [
  { label: '1:1', w: 1024, h: 1024 },
  { label: '3:4', w: 896, h: 1152 },
  { label: '4:3', w: 1152, h: 896 },
  { label: '9:16', w: 768, h: 1344 },
  { label: '16:9', w: 1344, h: 768 },
];
const STEPS = [8, 20, 30];
const MPS = [
  { label: '标准 1.5MP', v: 1.5 },
  { label: '清晰 2MP', v: 2.0 },
  { label: '快速 1MP', v: 1.0 },
];
const SEC_PER_STEP_PER_MP = 2.4;
const MAX_EDGE = 2048;              // 上传前把长边压到这个尺寸以内
const KEEP_AS_IS_UNDER = 1.2 * 1024 * 1024;

const NOTIFY_CHANNELS = [
  ['', '不接收通知'],
  ['serverchan', 'Server酱(微信)'],
  ['wecom', '企业微信群机器人'],
  ['feishu', '飞书群机器人'],
  ['dingtalk', '钉钉机器人'],
  ['pushplus', 'PushPlus(微信)'],
  ['bark', 'Bark(iOS)'],
  ['ntfy', 'ntfy(安卓)'],
  ['custom', '自定义 webhook'],
];
const NOTIFY_PLACEHOLDER = {
  serverchan: ['SendKey,如 sctp... 或 SCT...', '可留空'],
  wecom: ['不需要填', '群机器人的 webhook 地址'],
  feishu: ['不需要填', '群机器人的 webhook 地址'],
  dingtalk: ['不需要填', '机器人 webhook 地址'],
  pushplus: ['PushPlus 的 token', '可留空'],
  bark: ['Bark 的 key', '可留空(默认官方服务器)'],
  ntfy: ['topic 名称', '可留空(默认 ntfy.sh)'],
  custom: ['不需要填', '接收 POST 的地址'],
};

const state = {
  defaults: { steps: 20, width: 1024, height: 1024, megapixels: 1.5 },
  jobs: [],
  current: null,
  progress: null,
  comfyOnline: false,
  selectedSize: 0,
  selectedMp: 0,
  files: [],
  lastSeen: {},
  maxImages: 6,
};

let ws = null;
let pollTimer = null;
let enhanceTarget = 't2i';
let loginNotifyForm = null;
let appNotifyForm = null;

/* ------------------------------------------------------------------ 工具 */

function toast(msg, ms = 2200) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add('hidden'), ms);
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtDuration(job) {
  if (!job.started) return '';
  const end = job.finished || Date.now() / 1000;
  const s = Math.max(0, end - job.started);
  return s < 60 ? `${s.toFixed(0)} 秒` : `${(s / 60).toFixed(1)} 分钟`;
}

function estimate(kind) {
  const steps = Number(kind === 't2i' ? $('t2iSteps').value : $('editSteps').value);
  let mp = 1.05;
  if (kind === 't2i') {
    const s = SIZES[state.selectedSize];
    mp = (s.w * s.h) / 1048576;
  } else {
    mp = MPS[state.selectedMp].v;
  }
  const sec = steps * SEC_PER_STEP_PER_MP * mp;
  return sec < 60 ? `约 ${sec.toFixed(0)} 秒` : `约 ${(sec / 60).toFixed(1)} 分钟`;
}

async function api(path, options = {}) {
  const res = await fetch(path, { credentials: 'same-origin', ...options });
  if (res.status === 401) {
    showLogin();
    throw new Error('未登录');
  }
  if (!res.ok) {
    let text = `请求失败 (${res.status})`;
    try { text = (await res.text()) || text; } catch (e) { /* ignore */ }
    throw new Error(text);
  }
  const type = res.headers.get('content-type') || '';
  return type.includes('json') ? res.json() : res.text();
}

/* ------------------------------------------------------------------ 登录 */

function showLogin() {
  $('login').classList.remove('hidden');
  $('shell').classList.add('hidden');
  if (ws) { ws.close(); ws = null; }
}

function showApp() {
  $('login').classList.add('hidden');
  $('shell').classList.remove('hidden');
}

async function doLogin() {
  const token = $('tokenInput').value.trim();
  if (!token) return;
  $('loginErr').textContent = '';
  try {
    await api('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    });
    try {
      const payload = loginNotifyForm ? loginNotifyForm.read() : null;
      if (payload && payload.channel) {
        await api('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
      }
    } catch (err) {
      toast('通知设置没保存上:' + err.message, 4000);
    }
    await boot();
  } catch (err) {
    $('loginErr').textContent = '口令不对,再看一眼电脑上显示的';
  }
}

async function boot() {
  try {
    const data = await api('/api/state');
    showApp();
    applyState(data);
    connectWs();
    startPolling();
    return true;
  } catch (err) {
    showLogin();
    return false;
  }
}

/* ------------------------------------------------------------------ 实时状态 */

function applyState(data) {
  if (data.defaults) state.defaults = data.defaults;
  state.jobs = data.jobs || [];
  state.current = data.current;
  state.progress = data.progress;
  state.comfyOnline = !!data.comfy_online;
  if (data.max_images) state.maxImages = data.max_images;
  renderStatus(data);
  renderProgress();
  renderHistory();
}

function renderStatus(data) {
  const pill = $('statusPill');
  pill.className = 'pill';
  if (!state.comfyOnline) {
    pill.classList.add('off');
    pill.textContent = '引擎未运行';
  } else if (data.current) {
    pill.classList.add('busy');
    pill.textContent = '生成中';
  } else if (data.queued > 0) {
    pill.classList.add('busy');
    pill.textContent = `排队 ${data.queued}`;
  } else {
    pill.classList.add('on');
    const vram = data.vram_used_percent;
    pill.textContent = vram != null ? `就绪 · 显存 ${vram}%` : '就绪';
  }
}

function renderProgress() {
  const bar = $('jobBar');
  const job = state.jobs.find((j) => j.id === state.current);
  if (!job) { bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  const p = state.progress || {};
  const value = p.value || 0;
  const max = p.max || job.params.steps || 1;
  const pct = Math.min(100, Math.round((value / max) * 100));
  $('jobFill').style.width = `${pct}%`;
  $('jobLabel').textContent = `${job.kind === 'edit' ? '改图' : '出图'}中 ${pct}% · 已用 ${fmtDuration(job)}`;
}

/* ------------------------------------------------------------------ 连接 */

function connectWs() {
  if (ws) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/api/ws`);
  ws.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch (e) { return; }
    if (msg.type === 'state') {
      applyState(msg);
      checkFinished(msg.jobs || []);
    }
  };
  ws.onclose = () => { ws = null; setTimeout(connectWs, 4000); };
  ws.onerror = () => { if (ws) ws.close(); };
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    if (document.hidden) return;
    try {
      const data = await api('/api/state');
      applyState(data);
      checkFinished(data.jobs || []);
    } catch (err) { /* 断网时忽略 */ }
  }, 5000);
}

function checkFinished(jobs) {
  for (const job of jobs) {
    const last = state.lastSeen[job.id];
    const running = job.status === 'queued' || job.status === 'running';
    if (last && last !== job.status && !running) {
      if (job.status === 'done') notify('出好了', '点击查看生成的图片');
      else if (job.status === 'error') notify('生成失败', job.error || '');
      if (job.status === 'done') toast('生成完成');
    }
    state.lastSeen[job.id] = job.status;
  }
}

function notify(title, body) {
  // 手机震动,页面在后台时也能感觉到
  if (navigator.vibrate) {
    try { navigator.vibrate([180, 90, 180]); } catch (err) { /* 忽略 */ }
  }
  if (!('Notification' in window)) return;
  if (Notification.permission === 'granted') {
    try { new Notification(title, { body, icon: '/static/icon-192.png' }); } catch (e) { /* ignore */ }
  } else if (Notification.permission === 'default') {
    Notification.requestPermission();
  }
}

/* ------------------------------------------------------------------ 界面 */

function renderChips() {
  $('sizeChips').innerHTML = SIZES
    .map((s, i) => `<button data-i="${i}" class="${i === state.selectedSize ? 'sel' : ''}">${s.label}<br><span class="hint">${s.w}×${s.h}</span></button>`)
    .join('');
  $('sizeChips').querySelectorAll('button').forEach((btn) => {
    btn.onclick = () => {
      state.selectedSize = Number(btn.dataset.i);
      renderChips();
      updateEta();
    };
  });

  $('stepChips').innerHTML = STEPS
    .map((s) => `<button data-s="${s}">${s} 步</button>`)
    .join('');
  $('stepChips').querySelectorAll('button').forEach((btn) => {
    btn.onclick = () => {
      $('t2iSteps').value = btn.dataset.s;
      syncSteps('t2i');
    };
  });

  $('mpChips').innerHTML = MPS
    .map((m, i) => `<button data-i="${i}" class="${i === state.selectedMp ? 'sel' : ''}">${m.label}</button>`)
    .join('');
  $('mpChips').querySelectorAll('button').forEach((btn) => {
    btn.onclick = () => {
      state.selectedMp = Number(btn.dataset.i);
      renderChips();
      updateEta();
    };
  });
}

function syncSteps(kind) {
  const val = Number($(kind === 't2i' ? 't2iSteps' : 'editSteps').value);
  $(kind === 't2i' ? 't2iStepsVal' : 'editStepsVal').textContent = val;
  updateEta();
}

function updateEta() {
  $('t2iEta').textContent = estimate('t2i');
  $('editEta').textContent = estimate('edit');
}

function renderThumbs() {
  $('thumbs').innerHTML = state.files
    .map((f, i) => {
      const url = URL.createObjectURL(f);
      return `<div class="thumb">
        <img src="${url}" alt="">
        <span class="num">${i + 1}</span>
        <button class="del" data-i="${i}">✕</button>
      </div>`;
    })
    .join('');
  $('thumbs').querySelectorAll('.del').forEach((btn) => {
    btn.onclick = () => {
      state.files.splice(Number(btn.dataset.i), 1);
      renderThumbs();
    };
  });
}

function renderHistory() {
  const list = $('historyList');
  if (!state.jobs.length) {
    list.innerHTML = '<p class="hint">还没有记录</p>';
    return;
  }
  const tagText = { queued: '排队中', running: '生成中', done: '完成', error: '失败' };
  list.innerHTML = state.jobs
    .map((job) => {
      const imgs = (job.images || [])
        .map((_, i) => `<img src="/api/file/${job.id}/${i}?max=200" data-job="${job.id}" data-i="${i}" alt="">`)
        .join('');
      const dur = job.finished && job.started ? fmtDuration(job) : '';
      const count = job.kind === 'edit' ? `${job.params.image_count || 0} 张参考图 · ` : '';
      const err = job.status === 'error' && job.error ? `<p class="hint" style="color:#e05c5c">${escapeHtml(job.error)}</p>` : '';
      return `<div class="card">
        <p class="p">${escapeHtml(job.params.prompt || '')}</p>
        <div class="meta">
          <span class="tag ${job.status}">${tagText[job.status] || job.status}</span>
          <span>${job.kind === 'edit' ? '改图' : '文生图'}</span>
          <span>${count}${job.params.steps} 步</span>
          ${dur ? `<span>${dur}</span>` : ''}
          <span>${fmtTime(job.created)}</span>
        </div>
        ${err}
        <div class="imgs">${imgs}</div>
      </div>`;
    })
    .join('');
  list.querySelectorAll('img').forEach((img) => {
    img.onclick = () => openViewer(img.dataset.job, Number(img.dataset.i));
  });
}

async function compressImage(file) {
  if (!file.type || !file.type.startsWith('image/')) return file;
  try {
    const bitmap = await createImageBitmap(file);
    const edge = Math.max(bitmap.width, bitmap.height);
    const scale = Math.min(1, MAX_EDGE / edge);
    if (scale === 1 && file.size <= KEEP_AS_IS_UNDER) {
      bitmap.close();
      return file;
    }
    const width = Math.max(1, Math.round(bitmap.width * scale));
    const height = Math.max(1, Math.round(bitmap.height * scale));
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    canvas.getContext('2d').drawImage(bitmap, 0, 0, width, height);
    bitmap.close();
    // PNG 可能是带透明通道的抠图,瘦身前先保留原格式
    const keepAlpha = file.type === 'image/png' && file.size < 2 * 1024 * 1024;
    const blob = await new Promise((resolve) =>
      canvas.toBlob(resolve, keepAlpha ? 'image/png' : 'image/jpeg', keepAlpha ? undefined : 0.92));
    if (!blob) return file;
    const name = (file.name || 'image').replace(/\.[^.]+$/, '') + (keepAlpha ? '.png' : '.jpg');
    return new File([blob], name, { type: blob.type });
  } catch (err) {
    return file;
  }
}

function renderNotifyForm(root, data) {
  const info = data || {};
  const current = info.channel || '';
  const options = NOTIFY_CHANNELS
    .map(([value, label]) => `<option value="${value}"${value === current ? ' selected' : ''}>${label}</option>`)
    .join('');
  root.innerHTML = `
    <select class="nf-channel">${options}</select>
    <input class="nf-token" type="text" autocomplete="off" placeholder="密钥">
    <input class="nf-webhook" type="text" autocomplete="off" value="${escapeHtml(info.webhook || '')}" placeholder="推送地址">
    <label class="chk"><input type="checkbox" class="nf-success"${info.on_success === false ? '' : ' checked'}> 出图完成时通知</label>
    <label class="chk"><input type="checkbox" class="nf-failure"${info.on_failure === false ? '' : ' checked'}> 生成失败时通知</label>
    <p class="hint nf-state"></p>
  `;
  const select = root.querySelector('.nf-channel');
  const token = root.querySelector('.nf-token');
  const webhook = root.querySelector('.nf-webhook');
  const state = root.querySelector('.nf-state');

  const refresh = () => {
    const [tokenHint, webhookHint] = NOTIFY_PLACEHOLDER[select.value] || ['密钥', '推送地址'];
    token.placeholder = info.token_hint ? `已保存 ${info.token_hint}(留空表示不改)` : tokenHint;
    webhook.placeholder = webhookHint;
    if (!select.value) {
      state.textContent = '关闭后这台设备不再收到通知';
    } else if (info.using_global) {
      state.textContent = `还没单独设置,现在走电脑上的默认通道(${info.global_channel || '无'})`;
    } else {
      state.textContent = '这台设备使用自己的推送,只推这台设备出的图';
    }
  };
  select.onchange = refresh;
  refresh();

  return {
    read() {
      const payload = {
        channel: select.value,
        webhook: webhook.value.trim(),
        on_success: root.querySelector('.nf-success').checked,
        on_failure: root.querySelector('.nf-failure').checked,
      };
      const value = token.value.trim();
      if (value) payload.token = value;
      return payload;
    },
  };
}

function renderPeSelect(data) {
  const select = $('pePreset');
  const options = data.options || [];
  select.innerHTML = options
    .map(function (o) {
      const sel = o.id === data.preset ? ' selected' : '';
      return '<option value="' + escapeHtml(o.id) + '"' + sel + '>' + escapeHtml(o.label) + '</option>';
    })
    .join('');
  const describe = function (id) {
    const hit = options.find(function (o) { return o.id === id; });
    return hit ? '当前:' + hit.label : '还没选择扩写方案';
  };
  $('peState').textContent = describe(data.preset);
  select.onchange = async function () {
    try {
      const res = await api('/api/pe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preset: select.value }),
      });
      $('peState').textContent = describe(res.preset);
      toast('扩写方案已切换');
    } catch (err) {
      toast(err.message);
    }
  };
}

async function openSettings() {
  $('settingsBox').classList.remove('hidden');
  try {
    renderPeSelect(await api('/api/pe'));
    appNotifyForm = renderNotifyForm($('appNotify'), await api('/api/settings'));
  } catch (err) {
    toast(err.message);
  }
}

async function saveSettings() {
  if (!appNotifyForm) return;
  try {
    const res = await api('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(appNotifyForm.read()),
    });
    toast(res.channel ? '已保存,这台设备会用这个渠道' : '已关闭这台设备的通知');
    appNotifyForm = renderNotifyForm($('appNotify'), res);
  } catch (err) {
    toast(err.message, 4000);
  }
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function openViewer(jobId, index) {
  $('viewerImg').src = `/api/file/${jobId}/${index}`;
  $('viewerSave').href = `/api/file/${jobId}/${index}`;
  $('viewerSave').setAttribute('download', `qwen_${jobId}_${index}.png`);
  $('viewer').classList.remove('hidden');
}

/* ------------------------------------------------------------------ 提交 */

async function submitT2i() {
  const prompt = $('t2iPrompt').value.trim();
  if (!prompt) return toast('先写点提示词');
  const size = SIZES[state.selectedSize];
  const btn = $('t2iGo');
  btn.disabled = true;
  try {
    await api('/api/t2i', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt,
        negative: $('t2iNegative').value.trim(),
        steps: Number($('t2iSteps').value),
        width: size.w,
        height: size.h,
      }),
    });
    notify('已开始生成', prompt.slice(0, 40));
    toast('已加入队列');
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
  }
}

async function submitEdit() {
  const prompt = $('editPrompt').value.trim();
  if (!prompt) return toast('先写点要改什么');
  if (!state.files.length) return toast('先选参考图');
  const btn = $('editGo');
  btn.disabled = true;
  try {
    const form = new FormData();
    form.append('prompt', prompt);
    form.append('steps', $('editSteps').value);
    form.append('megapixels', MPS[state.selectedMp].v);
    state.files.forEach((file) => form.append('images', file, file.name || 'image.jpg'));
    await api('/api/edit', { method: 'POST', body: form });
    toast('已加入队列');
    state.files = [];
    renderThumbs();
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
  }
}

function openClear() {
  const count = state.jobs.length;
  if (!count) return toast('现在没有记录');
  $('clearHint').textContent = `当前 ${count} 条`;
  $('clearBox').classList.remove('hidden');
}

async function doClear(deleteFiles) {
  try {
    const res = await api('/api/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ delete_files: deleteFiles }),
    });
    $('clearBox').classList.add('hidden');
    toast(deleteFiles
      ? `已清 ${res.cleared} 条记录,删除 ${res.files} 张图`
      : `已清 ${res.cleared} 条记录`);
    applyState(await api('/api/state'));
  } catch (err) {
    toast(err.message);
  }
}

async function cancelCurrent() {
  if (!state.current) return;
  try {
    await api(`/api/job/${state.current}/cancel`, { method: 'POST' });
    toast('已请求取消');
  } catch (err) {
    toast(err.message);
  }
}

/* ------------------------------------------------------------------ 提示词扩写 */

async function enhancePrompt(kind) {
  const ta = kind === 't2i' ? $('t2iPrompt') : $('editPrompt');
  const prompt = ta.value.trim();
  if (!prompt) return toast('先写一句话,AI 再帮你扩写');

  const btn = kind === 't2i' ? $('t2iEnhance') : $('editEnhance');
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = '扩写中…';
  toast('正在扩写,大概 20 秒');
  try {
    let job;
    if (kind === 'edit' && state.files.length) {
      const form = new FormData();
      form.append('prompt', prompt);
      form.append('mode', 'edit');
      state.files.slice(0, 3).forEach((file) => form.append('images', file, file.name || 'image.jpg'));
      job = await api('/api/enhance', { method: 'POST', body: form });
    } else {
      job = await api('/api/enhance', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, mode: kind === 'edit' ? 'edit' : 't2i' }),
      });
    }
    const text = await waitForText(job.id);
    enhanceTarget = kind;
    $('enhanceText').value = text;
    $('enhanceHint').textContent = `${text.length} 字`;
    $('enhance').classList.remove('hidden');
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

async function waitForText(jobId) {
  for (let i = 0; i < 240; i++) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    const job = await api(`/api/job/${jobId}`);
    if (job.status === 'done') return job.text || '';
    if (job.status === 'error') throw new Error(job.error || '扩写失败');
  }
  throw new Error('扩写超时了');
}

function applyEnhance() {
  const text = $('enhanceText').value.trim();
  if (!text) return toast('内容为空');
  const ta = enhanceTarget === 't2i' ? $('t2iPrompt') : $('editPrompt');
  ta.value = text;
  $('enhance').classList.add('hidden');
  toast('已替换提示词');
}

/* ------------------------------------------------------------------ 启动 */

function init() {
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.onclick = () => {
      document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t === tab));
      ['t2i', 'edit', 'history'].forEach((name) => {
        $(`panel-${name}`).classList.toggle('hidden', name !== tab.dataset.tab);
      });
    };
  });

  $('loginBtn').onclick = doLogin;
  $('tokenInput').onkeydown = (e) => { if (e.key === 'Enter') doLogin(); };
  $('t2iSteps').oninput = () => syncSteps('t2i');
  $('editSteps').oninput = () => syncSteps('edit');
  $('t2iGo').onclick = submitT2i;
  $('editGo').onclick = submitEdit;
  $('cancelBtn').onclick = cancelCurrent;
  $('clearBtn').onclick = openClear;
  $('clearRecords').onclick = () => doClear(false);
  $('clearBoth').onclick = () => doClear(true);
  $('clearCancel').onclick = () => $('clearBox').classList.add('hidden');
  $('settingsBtn').onclick = openSettings;
  $('settingsSave').onclick = saveSettings;
  $('settingsClose').onclick = () => $('settingsBox').classList.add('hidden');
  loginNotifyForm = renderNotifyForm($('loginNotify'), {});
  $('notifyTest').onclick = async () => {
    try {
      const res = await api('/api/notify/test', { method: 'POST' });
      toast(`测试通知已发往 ${res.channel},看下手机`);
    } catch (err) {
      toast(err.message, 4000);
    }
  };
  $('t2iEnhance').onclick = () => enhancePrompt('t2i');
  $('editEnhance').onclick = () => enhancePrompt('edit');
  $('enhanceApply').onclick = applyEnhance;
  $('enhanceClose').onclick = () => $('enhance').classList.add('hidden');
  $('refreshBtn').onclick = async () => {
    const data = await api('/api/state');
    applyState(data);
    toast('已刷新');
  };

  $('editFiles').onchange = async (event) => {
    const picked = Array.from(event.target.files);
    event.target.value = '';
    if (!picked.length) return;
    toast('正在处理图片…', 1200);
    for (const file of picked) {
      if (state.files.length >= state.maxImages) {
        toast(`最多 ${state.maxImages} 张`);
        break;
      }
      state.files.push(await compressImage(file));
    }
    renderThumbs();
  };

  $('viewerClose').onclick = () => {
    $('viewer').classList.add('hidden');
    $('viewerImg').src = '';
  };

  renderChips();
  syncSteps('t2i');
  syncSteps('edit');
  boot();
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  // 回到前台时补一次:息屏期间完成的任务也能收到提醒
  api('/api/state')
    .then((data) => {
      applyState(data);
      checkFinished(data.jobs || []);
    })
    .catch(() => {});
  if (ws === null) connectWs();
});

init();


