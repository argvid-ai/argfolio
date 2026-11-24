'use strict';

const $ = id => document.getElementById(id);
const stages = [
  ['录制书页', '居中单页取景'],
  ['选取页面', '从视频挑出稳定画面'],
  ['检查质量', '核对翻页与选帧'],
  ['识别文字', '微调版 PP-OCRv5'],
  ['整理正文', 'MiniCPM3 分段'],
  ['导出 PDF', '文字版与可搜索原图版'],
];
const stageIndex = {pageflow: 1, audit: 2, models: 3, ocr_v5: 3, no_text: 3, minicpm3_body: 4, minicpm3_retry: 4, export: 5, complete: 5};
const labels = {queued: '等待处理', running: '处理中', cancelling: '正在中断', cancelled: '已中断', complete: '已完成', failed: '处理失败', interrupted: '处理中断'};
const active = job => ['queued', 'running', 'cancelling'].includes(job?.state);
const failed = job => ['failed', 'interrupted', 'cancelled'].includes(job?.state);
const escaped = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

let token = localStorage.getItem('pairing') || '';
let connected = false, localAccess = false, tab = 'scan';
let status = {}, jobs = [], currentId = sessionStorage.getItem('paperscan-job');
let currentJob, locked = false, refreshing = false;
let previewActive = false, toastTimer, viewer, page = 1, variant = 'search';
let lastResult = '', lastLibrary = '', videoData = [], lastVideoList = '';
let savedName = '';

const dateTime = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
});

function titleFor(item) {
  const recorded = item.name.match(/rec_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})(?:_(\d+))?\.mp4$/);
  if (recorded) {
    const [, year, month, day, hours, minutes, seconds, copy] = recorded;
    return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}${copy ? ' · ' + copy : ''}`;
  }
  const created = new Date(item.created_at);
  return Number.isNaN(created.getTime()) ? item.name : dateTime.format(created);
}

function confirmAction(title, detail, button) {
  const dialog = $('confirmAction');
  $('confirmTitle').textContent = title;
  $('confirmDetail').textContent = detail;
  $('confirmSubmit').textContent = button;
  dialog.returnValue = '';
  dialog.showModal();
  return new Promise(resolve => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), {once: true});
  });
}

if (new URLSearchParams(location.hash.slice(1)).has('pair')) {
  history.replaceState(null, '', location.pathname + location.search);
}

function toast(message) {
  $('toast').textContent = message;
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 6000);
}

function headers() {
  return {'Authorization': `Bearer ${token}`, 'X-PaperScan': 'workbench', 'X-PaperScan-Media': location.protocol.slice(0, -1)};
}

async function api(path, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), path === '/api/record/stop' ? 60000 : 12000);
  try {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: {...headers(), 'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    const value = await response.json();
    if (response.status === 401) {
      connected = false;
      localStorage.removeItem('pairing');
      $('pair').hidden = false;
      $('workspace').hidden = true;
    }
    if (!response.ok || value.ok === false) throw new Error(value.error || '操作暂不可用。');
    return value;
  } finally {
    clearTimeout(timer);
  }
}

function selectTab(name) {
  if (name === 'scan' && connected && !status.capture_enabled) name = 'library';
  tab = name;
  document.querySelectorAll('.nav-button').forEach(button => {
    const selected = button.dataset.tab === name;
    button.classList.toggle('selected', selected);
    if (selected) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  document.querySelectorAll('.tabpage').forEach(section => { section.hidden = section.id !== name; });
  $('crumb').textContent = name === 'scan' ? '扫描工作台' : '文档库';
  syncPreview();
  if (name === 'library') renderLibrary();
}

function setJob(id) {
  currentId = id;
  if (id) sessionStorage.setItem('paperscan-job', id);
  else sessionStorage.removeItem('paperscan-job');
}

function displayStatus(id, label, on, extra = '') {
  const element = $(id);
  element.innerHTML = `<i></i>${escaped(label)}`;
  element.classList.toggle('on', on);
  element.classList.toggle('recording', extra === 'recording');
}

function renderFlow() {
  const camera = status.camera;
  const recording = !!camera?.recording;
  const ready = !!camera?.width && !!camera?.height;
  const job = currentJob;
  const progress = job?.progress || {};
  const stage = recording ? 0 : job?.state === 'complete' ? stages.length
    : job ? (stageIndex[progress.stage] ?? 1) : -1;
  const canStart = connected && ready && status.pipeline_ready && status.model_ready && !status.active_job;
  $('steps').innerHTML = stages.map(([title, description], index) => {
    const state = stage > index ? 'done' : stage === index ? 'current' : '';
    let detail = description;
    if (state === 'current' && progress.stage === 'export') {
      if (active(job)) detail = '正在生成文字版和可搜索原图版';
    } else if (state === 'current' && progress.total) {
      detail += ` · ${progress.done || 0}/${progress.total} 页`;
    }
    return `<li class="${state}" ${state === 'current' ? 'aria-current="step"' : ''}><span class="step-number">${state === 'done' ? '✓' : index + 1}</span><div><span class="step-title">${title}</span><span class="step-detail">${escaped(detail)}</span></div></li>`;
  }).join('');
  $('flowState').textContent = recording ? '录制中' : labels[job?.state] || '待开始';
  $('flowState').classList.toggle('warn', failed(job));
  $('flowNote').textContent = locked ? '正在执行操作…'
    : recording ? '停止后，视频会保存并自动开始处理。'
    : job?.state === 'cancelling' ? '正在停止板端处理。'
    : job?.state === 'cancelled' ? '处理已中断。可从原录制重新生成。'
    : active(job) ? '板端正在处理，结果会显示在下方。'
    : failed(job) ? job.error
    : savedName ? '录制已保存，可在下方重新发起处理。'
    : !connected ? '正在尝试连接板子。'
    : !ready ? '摄像头未就绪，请检查板端连接。'
    : !status.model_ready ? 'MiniCPM3 服务未就绪。'
    : !status.pipeline_ready ? '板端处理程序未就绪。'
    : job?.state === 'complete' ? '结果已生成，可以开始下一次录制。'
    : '把书页放入取景框，翻页后稍作停留。';
  $('record').hidden = recording || active(job);
  $('record').disabled = locked || !canStart;
  $('stop').hidden = !recording;
  $('stop').disabled = locked || !connected;
  $('cancelJob').hidden = !active(job) || recording;
  $('cancelJob').disabled = locked || !connected || job?.state === 'cancelling';
  $('retry').hidden = !job?.can_retry;
  $('retry').disabled = locked || !connected || recording || !!status.active_job || !status.model_ready;
  displayStatus('liveStatus', recording ? '录制中' : ready && connected ? '实时' : '未连接',
    ready && connected, recording ? 'recording' : '');
  const seconds = Math.floor(camera?.elapsed || 0);
  $('elapsed').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  if (ready) {
    const width = camera.capture_mode === 'single_center' ? camera.record_width : Math.min(1536, camera.width);
    const height = camera.height;
    $('viewfinder').style.aspectRatio = `${width}/${height}`;
    $('resolution').textContent = `录制 ${width} × ${height} · ${camera.record_fps || 30} fps`;
  }
}

const downloadURL = (id, name) => `/download/${encodeURIComponent(id)}/${encodeURIComponent(name)}`;
const previewURL = (id, number, type = 'search') =>
  `/preview/${encodeURIComponent(id)}/${type === 'text' ? 'text/' : ''}${String(number).padStart(4, '0')}.png`;

function downloads(job) {
  return `<div class="downloads">${job.preview_pages ? `<button class="quiet" data-preview="${escaped(job.id)}">预览</button>` : ''}${
    job.downloads.map(file => `<a class="download" href="${escaped(downloadURL(job.id, file.name))}" download>${escaped(file.label)} ↓</a>`).join('')
  }</div>`;
}

function removeResult(job) {
  return `<button class="quiet remove" data-remove-job="${escaped(job.id)}">删除结果</button>`;
}

function reviewNote(job) {
  return job.review && job.review !== 'PASS'
    ? `<p class="notice">需复核 · ${escaped(job.note || '请对照原图确认文字完整性。')}</p>` : '';
}

function renderResult() {
  const recent = !currentId && !savedName && !status.camera?.recording && !currentJob;
  const job = currentJob || (recent ? jobs.find(item => item.state === 'complete') : undefined);
  const key = JSON.stringify([job?.id, job?.state, job?.updated_at, job?.preview_pages, status.camera?.recording, savedName]);
  if (key === lastResult) return;
  lastResult = key;
  $('resultTitle').textContent = recent && job ? '最近结果' : '本次结果';
  $('resultCount').textContent = job?.state === 'complete' ? `${job.pages} 页` : '';
  if (job?.state === 'complete') {
    const thumbs = Array.from({length: job.preview_pages || 0}, (_, index) =>
      `<button class="thumb" data-preview="${escaped(job.id)}" data-page="${index + 1}"><img loading="lazy" src="${escaped(previewURL(job.id, index + 1))}" alt="选帧 ${index + 1}"><span>第 ${index + 1} 页</span></button>`).join('');
    $('result').innerHTML = `<div class="result-body"><div><h3>${escaped(titleFor(job))}</h3>${reviewNote(job)}</div><div class="actions">${downloads(job)}${removeResult(job)}</div>${thumbs ? `<div class="thumbnails">${thumbs}</div>` : ''}</div>`;
  } else {
    const message = active(job) ? '处理完成后，选帧和两种 PDF 会显示在这里。'
      : failed(job) ? '处理未完成。'
      : status.camera?.recording ? '正在录制书页。' : '完成录制后，选帧和 PDF 会显示在这里。';
    $('result').innerHTML = `<p class="empty">${message}</p>`;
  }
}

function renderLibrary() {
  const query = $('documentSearch').value.trim().toLowerCase();
  const entries = jobs.filter(job => titleFor(job).includes(query) || job.name.toLowerCase().includes(query));
  const key = JSON.stringify([query, entries.map(job => [job.id, job.state, job.updated_at, job.progress])]);
  if (key === lastLibrary) return;
  lastLibrary = key;
  $('jobs').innerHTML = entries.length ? entries.map(job => `<article class="panel job">
    <h3>${escaped(titleFor(job))}</h3>
    <p class="job-meta">${escaped(labels[job.state] || job.state)}${job.pages ? ` · ${job.pages} 页` : ''}</p>
    <div class="actions">${job.state === 'complete' ? downloads(job)
      : `<button class="quiet" data-track="${escaped(job.id)}">查看进度</button>`}
      ${active(job) ? '' : removeResult(job)}</div>
    ${job.state === 'complete' ? reviewNote(job) : ''}
  </article>`).join('') : `<p class="empty">${query ? '没有找到匹配的文档。' : '暂无文档。'}</p>`;
}

function renderVideos() {
  const disabled = !connected || locked || !!status.active_job || !!status.camera?.recording
    || !status.model_ready || !status.pipeline_ready;
  const canDelete = connected && !locked && !status.camera?.recording;
  const key = JSON.stringify([disabled, canDelete, currentJob?.name, videoData, savedName]);
  if (key === lastVideoList) return;
  lastVideoList = key;
  $('videos').innerHTML = videoData.length ? videoData.map(file => `<div class="video">
    <div><strong>${escaped(titleFor(file))}</strong><small>${escaped(file.size_mb)} MB${file.name === savedName ? ' · 刚保存' : ''}</small></div>
    <div class="actions"><button class="quiet" data-video="${escaped(file.name)}" ${disabled ? 'disabled' : ''}>生成 PDF</button>
    <button class="quiet remove" data-remove-video="${escaped(file.name)}" ${!canDelete || (active(currentJob) && currentJob.name === file.name) ? 'disabled' : ''}>删除录制</button></div>
  </div>`).join('') : '<p class="empty">暂无录制。</p>';
}

async function loadVideos() {
  try {
    videoData = (await api('/api/videos')).files || [];
    renderVideos();
    syncPreview();
  } catch {
    $('videos').innerHTML = '<p class="empty">录制列表暂不可用，点击刷新重试。</p>';
    lastVideoList = '';
  }
}

async function refresh() {
  if (refreshing || (!localAccess && !token)) return;
  refreshing = true;
  try {
    const [nextStatus, list] = await Promise.all([api('/api/status'), api('/api/jobs')]);
    const reconnect = !connected;
    connected = true;
    status = nextStatus;
    jobs = list.jobs;
    if (currentId && !jobs.some(job => job.id === currentId)) setJob(null);
    currentJob = status.camera?.recording ? undefined
      : jobs.find(job => job.id === status.active_job)
        || jobs.find(job => job.id === currentId);
    if (status.active_job) setJob(status.active_job);
    $('documentCount').textContent = jobs.filter(job => job.state === 'complete').length;
    $('pair').hidden = true;
    $('workspace').hidden = false;
    $('deviceStatus').textContent = '板端已连接';
    $('deviceDot').classList.add('on');
    displayStatus('connection', '板子在线', true);
    document.querySelector('[data-tab="scan"]').hidden = !status.capture_enabled;
    if (!status.capture_enabled && tab === 'scan') selectTab('library');
    renderFlow();
    renderResult();
    renderLibrary();
    renderVideos();
    syncPreview();
    if (reconnect && status.capture_enabled) loadVideos();
  } catch {
    connected = false;
    $('deviceStatus').textContent = '等待重连';
    $('deviceDot').classList.remove('on');
    displayStatus('connection', '连接中断', false);
    syncPreview();
    renderFlow();
    renderVideos();
  } finally {
    refreshing = false;
  }
}

function syncPreview() {
  const visible = connected && status.camera?.width && tab === 'scan' && !document.hidden;
  const image = $('preview');
  if (!visible) {
    if (previewActive) image.removeAttribute('src');
    previewActive = false;
    image.hidden = true;
    $('previewEmpty').hidden = false;
    return;
  }
  if (previewActive) return;
  previewActive = true;
  const url = new URL('/api/preview.mjpeg', location.origin);
  url.searchParams.set('t', Date.now());
  image.src = url.href;
}

$('preview').onload = () => {
  if (!previewActive) return;
  $('preview').hidden = false;
  $('previewEmpty').hidden = true;
};
$('preview').onerror = () => {
  previewActive = false;
  $('preview').hidden = true;
  $('previewEmpty').hidden = false;
};
document.addEventListener('visibilitychange', syncPreview);

async function act(action) {
  if (locked) return;
  locked = true;
  renderFlow();
  renderVideos();
  try {
    await action();
  } catch (error) {
    toast(error.name === 'AbortError' ? '请求超时，请查看当前录制或处理状态。' : error.message);
  } finally {
    locked = false;
    await refresh();
    renderFlow();
    renderVideos();
  }
}

function showPage() {
  const count = variant === 'text' ? viewer.text_preview_pages : viewer.preview_pages;
  page = Math.min(page, count);
  $('viewerTitle').textContent = titleFor(viewer);
  $('viewText').hidden = !viewer.text_preview_pages;
  $('viewSearch').setAttribute('aria-pressed', variant === 'search');
  $('viewText').setAttribute('aria-pressed', variant === 'text');
  $('pageLabel').textContent = `${page} / ${count} 页`;
  $('previousPage').disabled = page <= 1;
  $('nextPage').disabled = page >= count;
  $('viewerError').hidden = true;
  $('viewerImage').hidden = false;
  $('viewerImage').src = previewURL(viewer.id, page, variant);
}

document.querySelectorAll('.nav-button').forEach(button => {
  button.onclick = () => selectTab(button.dataset.tab);
});
$('record').onclick = () => act(async () => {
  await api('/api/record/start', {});
  setJob(null);
  savedName = '';
});
$('stop').onclick = () => act(async () => {
  const result = await api('/api/record/stop', {process: true});
  savedName = result.file;
  if (result.job) setJob(result.job.id);
  toast(result.processing_error ? `录制已保存。处理未启动：${result.processing_error}` : '录制已保存，开始处理。');
  await loadVideos();
});
$('retry').onclick = () => act(async () => {
  const job = await api(`/api/jobs/${currentJob.id}/retry`, {});
  setJob(job.id);
});
$('cancelJob').onclick = async () => {
  const job = currentJob;
  if (active(job) && await confirmAction('中断处理？',
    `板子会停止处理 ${titleFor(job)}。录制视频可用于再次生成 PDF。`, '确认中断')) {
    await act(async () => {
      await api(`/api/jobs/${job.id}/cancel`, {});
      toast('已请求中断处理。');
    });
  }
};
$('refreshVideos').onclick = loadVideos;
$('refreshJobs').onclick = refresh;
$('documentSearch').oninput = renderLibrary;
$('videos').onclick = event => {
  const button = event.target.closest('[data-video]');
  if (button) act(async () => {
    const job = await api('/api/jobs', {name: button.dataset.video});
    setJob(job.id);
    $('flowTitle').scrollIntoView({block: 'center'});
  });
};
document.addEventListener('click', async event => {
  const removeVideo = event.target.closest('[data-remove-video]');
  if (removeVideo) {
    const name = removeVideo.dataset.removeVideo;
    if (await confirmAction('删除录制？',
      `${titleFor({name})} 将移到板端回收目录，已生成的文档仍可查看。`, '确认删除')) {
      await act(async () => {
        await api('/api/videos/delete', {name});
        if (savedName === name) savedName = '';
        await loadVideos();
        toast('录制已删除。');
      });
    }
    return;
  }
  const removeJob = event.target.closest('[data-remove-job]');
  if (removeJob) {
    const job = jobs.find(item => item.id === removeJob.dataset.removeJob);
    if (!job) return;
    const detail = job.imported
      ? '工作台副本将移到板端回收目录，原始导出保留在板端工作目录。'
      : '结果将移到板端回收目录，原录制可在录制列表中管理。';
    if (await confirmAction('删除结果？', `${titleFor(job)} · ${detail}`, '确认删除')) {
      await act(async () => {
        await api(`/api/jobs/${job.id}/delete`, {});
        if (currentId === job.id) setJob(null);
        toast('结果已删除。');
      });
    }
    return;
  }
  const track = event.target.closest('[data-track]');
  if (track) {
    setJob(track.dataset.track);
    selectTab('scan');
    refresh();
  }
  const button = event.target.closest('[data-preview]');
  if (!button) return;
  viewer = jobs.find(job => job.id === button.dataset.preview);
  if (!viewer?.preview_pages) return;
  variant = 'search';
  page = Number(button.dataset.page || 1);
  showPage();
  $('viewer').showModal();
});
$('viewText').onclick = () => { variant = 'text'; showPage(); };
$('viewSearch').onclick = () => { variant = 'search'; showPage(); };
$('previousPage').onclick = () => { page--; showPage(); };
$('nextPage').onclick = () => { page++; showPage(); };
$('closeViewer').onclick = () => $('viewer').close();
$('viewer').onclose = () => $('viewerImage').removeAttribute('src');
$('viewerImage').onerror = () => { $('viewerImage').hidden = true; $('viewerError').hidden = false; };
$('pairForm').onsubmit = async event => {
  event.preventDefault();
  token = $('token').value.trim();
  await refresh();
  if (connected) localStorage.setItem('pairing', token);
  else toast('配对失败，请检查配对码与网络。');
};

async function initialize() {
  $('steps').innerHTML = '';
  renderFlow();
  renderResult();
  try {
    localAccess = !!(await api('/health')).local_access;
  } catch {
    displayStatus('connection', '等待重连', false);
    setTimeout(initialize, 3000);
    return;
  }
  if (!localAccess && !token) {
    $('pair').hidden = false;
    $('workspace').hidden = true;
  } else {
    await refresh();
    if (connected && token && !localAccess) localStorage.setItem('pairing', token);
  }
  setInterval(() => { if (!document.hidden) refresh(); }, 2000);
}
initialize();
