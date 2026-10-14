import { SymbolAPI } from './api.js';

const $ = selector => document.querySelector(selector);
const node = (tag, className = '', text = '') => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== '') element.textContent = String(text);
  return element;
};
const statuses = { ai_processing: '工作中', awaiting_input: '等待你的确认', completed: '已完成', failed: '生成失败', stopped: '已停止', rejected: '已取消' };
const stages = { device: '确认型号', package: '确认封装', conflicts: '引脚冲突', review_diffs: '复核差异', facts: '补充信息', output: '生成确认' };
const sides = { left: '左侧', right: '右侧', top: '上侧', bottom: '下侧', unknown: '未确定' };
const fieldNames = { name: '名称', side: '侧别', type: '类型', description: '说明' };
const artifactInfo = { olb: ['OLB', 'Cadence 符号库', '.OLB'], dsn: ['DSN', '符号实例原理图', '.DSN'], tcl: ['TCL', '符号生成脚本', '_gen.tcl'], capture_log: ['LOG', 'Capture 执行日志', '.log'] };
let token = sessionStorage.getItem('step-access-token') || '';
let activeId = null, activeJob = null, epoch = 0, pollTimer, questionKey = '', previewKey = '', pinKey = '';
let storageKey = '', identity = '', historySequence = 0, historyOffset = 0, hasMore = false, historyLoading = false;
let uploading = false, refreshSequence = 0, zoom = 1;
let showDeleted = false;

function flash(message, error = false) {
  $('#flash').textContent = message;
  $('#flash').classList.toggle('error', error);
  $('#flash').hidden = false;
}

function clearSession(message = '') {
  token = ''; identity = ''; storageKey = '';
  sessionStorage.removeItem('step-access-token');
  stopPolling(); historySequence++;
  activeId = null; activeJob = null;
  $('#workspace').hidden = true; $('#login-view').hidden = false;
  $('#history').replaceChildren(); $('#question-area').replaceChildren();
  $('#password').value = '';
  $('#login-error').textContent = message; $('#login-error').hidden = !message;
  $('#upload-form').reset(); updateFile();
}

const api = new SymbolAPI({ getToken: () => token, onUnauthorized: () => clearSession('登录已过期，请重新登录。') });
function stopPolling() { clearTimeout(pollTimer); epoch++; }

async function enterWorkspace() {
  const enteringToken = token;
  try {
    const user = await api.me();
    if (!token || token !== enteringToken) return;
    identity = `${user.tenant_id || ''}:${user.user_id || user.id || ''}`;
    storageKey = `symbol-last-drawing:${identity}`;
    showDeleted = false;
    $('#login-view').hidden = true; $('#workspace').hidden = false;
    $('#flash').hidden = true;
    newJob();
    loadHistory(true);
    const lastId = sessionStorage.getItem(storageKey);
    if (lastId) selectJob(lastId);
  } catch (error) {
    if (error.status !== 401) {
      $('#login-error').textContent = error.message; $('#login-error').hidden = false;
      $('#login-view').hidden = false; $('#workspace').hidden = true;
    }
  }
}

$('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('#login-button').disabled = true; $('#login-error').hidden = true;
  try {
    const data = await api.login($('#username').value.trim(), $('#password').value);
    token = data.access_token; sessionStorage.setItem('step-access-token', token);
    $('#password').value = '';
    await enterWorkspace();
  } catch (error) { $('#login-error').textContent = error.message; $('#login-error').hidden = false; }
  finally { $('#login-button').disabled = false; }
});
$('#logout').addEventListener('click', () => clearSession());

function newJob(removeSelection = false) {
  stopPolling(); activeId = null; activeJob = null;
  questionKey = ''; previewKey = ''; pinKey = '';
  $('#job-view').hidden = true; $('#upload-view').hidden = false; $('#flash').hidden = true;
  if (removeSelection && storageKey) sessionStorage.removeItem(storageKey);
  $('#history').querySelectorAll('.active').forEach(item => item.classList.remove('active'));
}
$('#new-job').addEventListener('click', () => newJob(true));

function updateFile() {
  const file = $('#datasheet').files[0];
  const kind = sourceKind(file);
  $('#file-title').textContent = file ? file.name : '选择或拖入 PDF 手册 / 封装图片';
  $('#file-detail').textContent = file ? `${kind === 'pdf' ? 'PDF 手册' : kind === 'image' ? '封装图片' : '文件'} · ${(file.size / 1024 / 1024).toFixed(2)} MB · 点击更换文件` : 'PDF 最大 64 MB；PNG / JPG / WEBP / BMP 最大 32 MB';
  $('#pdf-options').hidden = kind !== 'pdf';
  $('#upload-hint').textContent = kind === 'pdf' ? '从手册解析引脚，核对后生成符号。' : kind === 'image' ? '请包含全部引脚号、名称与主体轮廓。' : 'PDF 解析手册，图片直接还原引脚布局。';
  if (!uploading) $('#generate').replaceChildren(document.createTextNode(kind === 'pdf' ? '解析手册 ' : kind === 'image' ? '照图生成 ' : '生成符号 '), node('span', '', '→'));
}
function sourceKind(file) {
  return /\.pdf$/i.test(file?.name || '') ? 'pdf' : /\.(png|jpe?g|webp|bmp)$/i.test(file?.name || '') ? 'image' : null;
}
$('#datasheet').addEventListener('change', () => { $('#flash').hidden = true; updateFile(); });
for (const eventName of ['dragenter', 'dragover']) $('#dropzone').addEventListener(eventName, event => { event.preventDefault(); $('#dropzone').classList.add('over'); });
for (const eventName of ['dragleave', 'drop']) $('#dropzone').addEventListener(eventName, event => { event.preventDefault(); $('#dropzone').classList.remove('over'); });
$('#dropzone').addEventListener('drop', event => {
  if (uploading || !event.dataTransfer.files.length) return;
  const transfer = new DataTransfer(); transfer.items.add(event.dataTransfer.files[0]);
  $('#datasheet').files = transfer.files; $('#flash').hidden = true; updateFile();
});

$('#upload-form').addEventListener('submit', async event => {
  event.preventDefault(); if (uploading) return;
  const file = $('#datasheet').files[0];
  const kind = sourceKind(file);
  if (!kind) { flash('请选择 PDF 手册或 PNG、JPG、WEBP、BMP 格式的封装图。', true); return; }
  if (file.size > (kind === 'pdf' ? 64 : 32) * 1024 * 1024) { flash(kind === 'pdf' ? 'PDF 超过 64 MB 限制，请选择较小的文件。' : '图片超过 32 MB 限制，请选择较小的文件。', true); return; }
  uploading = true;
  const submittedEpoch = epoch;
  $('#generate').disabled = true; $('#generate').textContent = '正在上传…';
  $('#datasheet').disabled = true;
  const data = new FormData(); data.append('file', file); data.append('device', $('#device').value.trim());
  data.append('package', $('#package').value.trim());
  if (kind === 'pdf') data.append('strict_pages', String($('#strict-pages').checked));
  try {
    const response = await api.generate(data);
    if (token && epoch === submittedEpoch) await selectJob(response.drawing_id);
    if (token) loadHistory(true);
  } catch (error) { if (token && epoch === submittedEpoch) flash(error.message, true); }
  finally {
    uploading = false; $('#generate').disabled = false; $('#datasheet').disabled = false;
    updateFile();
  }
});

function dateLabel(value, full = false) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('zh-CN', full ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' } : { month: '2-digit', day: '2-digit' }).format(date);
}

async function loadHistory(reset = false) {
  if (!token || (historyLoading && !reset)) return;
  const sequence = ++historySequence, account = identity;
  historyLoading = true; $('#load-more').disabled = true;
  if (reset) { historyOffset = 0; $('#history').replaceChildren(node('p', 'history-empty', '正在读取历史任务…')); }
  try {
    $('#history-title').textContent = showDeleted ? '已删除任务' : '历史任务';
    $('#deleted-history').textContent = showDeleted ? '返回历史' : '已删除';
    $('#deleted-history').setAttribute('aria-pressed', String(showDeleted));
    const data = await api.list(20, historyOffset, showDeleted);
    if (sequence !== historySequence || account !== identity) return;
    if (reset) $('#history').replaceChildren();
    for (const item of data.items) {
      const entry = node('div', 'history-entry');
      const button = node('button', 'history-item'); button.type = 'button'; button.dataset.id = item.drawing_id; button.dataset.status = item.status;
      const title = item.device || item.original_filename || '未命名符号';
      button.title = title;
      button.classList.toggle('active', activeId === item.drawing_id);
      button.append(node('strong', '', item.device || item.original_filename || '未命名符号'));
      const detail = node('small'); detail.append(node('span', 'mini-status', statuses[item.status] || item.status), node('time', '', dateLabel(item.created_at)));
      button.append(detail); button.addEventListener('click', () => selectJob(item.drawing_id));
      const action = node('button', 'history-action', item.deleted_at ? '↶' : '×'); action.type = 'button';
      action.setAttribute('aria-label', `${item.deleted_at ? '恢复' : '删除'}任务 ${title}`);
      action.title = item.deleted_at ? '恢复任务' : '移出历史任务，保留回溯记录';
      action.disabled = !item.deleted_at && ['pending', 'ai_processing'].includes(item.status);
      action.addEventListener('click', () => changeVisibility(item.drawing_id, !item.deleted_at, action));
      entry.append(button, action); $('#history').append(entry);
    }
    historyOffset += data.items.length; hasMore = data.has_more;
    if (!historyOffset) $('#history').append(node('p', 'history-empty', showDeleted ? '没有已删除的任务。' : '还没有符号任务。上传 PDF 手册或图片，开始生成第一份符号。'));
    $('#load-more').hidden = !hasMore;
  } catch (error) {
    if (token && sequence === historySequence && account === identity) {
      if (reset) $('#history').replaceChildren(node('p', 'history-empty', error.message));
      else flash(error.message, true);
    }
  } finally { if (sequence === historySequence) { historyLoading = false; $('#load-more').disabled = false; } }
}
$('#refresh-history').addEventListener('click', () => loadHistory(true));
$('#deleted-history').addEventListener('click', () => { showDeleted = !showDeleted; loadHistory(true); });
$('#load-more').addEventListener('click', () => { if (hasMore) loadHistory(); });

async function changeVisibility(id, deleted, button) {
  const account = identity, requestToken = token;
  button.disabled = true;
  try {
    if (deleted) await api.remove(id); else await api.restore(id);
    if (!token || identity !== account || token !== requestToken) return;
    if (deleted && storageKey && sessionStorage.getItem(storageKey) === id) sessionStorage.removeItem(storageKey);
    if (activeId === id) { if (deleted) newJob(true); else await refreshJob(); }
    await loadHistory(true);
    if (token && identity === account && token === requestToken) flash(deleted
      ? '任务已移出历史。回溯记录与文件保留，可在“已删除”中恢复。' : '任务已恢复。');
  } catch (error) { if (token && identity === account && token === requestToken) flash(error.message, true); }
  finally { button.disabled = false; }
}

async function selectJob(id) {
  stopPolling(); activeId = id; activeJob = null; questionKey = ''; previewKey = ''; pinKey = '';
  $('#upload-view').hidden = true; $('#job-view').hidden = false; $('#job-view').setAttribute('aria-busy', 'true');
  $('#flash').hidden = true; $('#job-title').textContent = '读取任务…'; $('#job-meta').textContent = '';
  $('#job-status').textContent = '加载中'; $('#job-status').dataset.status = '';
  $('#restore-job').hidden = true;
  $('#question-area').replaceChildren(node('p', 'muted', '正在读取任务状态…'));
  $('#preview').replaceChildren(node('p', 'muted', '正在读取引脚布局…'));
  $('#layout-summary').textContent = ''; $('#pin-count').textContent = '等待解析'; $('#check-score').hidden = true;
  $('#findings').replaceChildren();
  for (const selector of ['#artifact-section', '#pin-section', '#log-section', '#human-history-section']) $(selector).hidden = true;
  $('#history').querySelectorAll('.history-item').forEach(item => item.classList.toggle('active', item.dataset.id === id));
  if (storageKey) sessionStorage.setItem(storageKey, id);
  await refreshJob(epoch);
}

async function refreshJob(generation = epoch) {
    const id = activeId; if (!id || !token) return;
    const sequence = ++refreshSequence;
  clearTimeout(pollTimer);
  try {
    const job = await api.get(id);
    if (generation !== epoch || sequence !== refreshSequence || activeId !== id || !token) return;
    activeJob = job; renderJob(job);
    $('#job-view').setAttribute('aria-busy', 'false');
    if (!job.deleted_at && ['ai_processing', 'awaiting_input'].includes(job.status)) pollTimer = setTimeout(() => refreshJob(generation), job.status === 'awaiting_input' ? 5000 : 2500);
  } catch (error) {
    if (generation !== epoch || sequence !== refreshSequence || !token) return;
    $('#job-view').setAttribute('aria-busy', 'false');
    if (error.status === 404 || error.status === 422) { newJob(true); flash('这个任务已不可用，请从历史任务中重新选择。', true); }
    else { flash(error.message, true); pollTimer = setTimeout(() => refreshJob(generation), 5000); }
  }
}
$('#refresh-job').addEventListener('click', () => refreshJob());

function renderJob(job) {
  $('#job-reference').textContent = `SYMBOL / ${job.drawing_id.slice(-8).toUpperCase()}`;
  $('#job-title').textContent = job.device || job.original_filename || '符号任务';
  $('#job-meta').textContent = [job.original_filename, job.package, dateLabel(job.created_at, true)].filter(Boolean).join(' · ');
  $('#job-status').textContent = job.deleted_at ? '已删除 · 记录保留' : statuses[job.status] || job.status; $('#job-status').dataset.status = job.deleted_at ? 'deleted' : job.status;
  $('#restore-job').hidden = !job.deleted_at;
  $('#source-download').textContent = job.input_kind === 'image' ? '下载原图 ↧' : '下载手册 ↧';
  $('#progress li:first-child').textContent = job.input_kind === 'image' ? '读取图片' : '解析手册';
  $('#progress li:nth-child(2)').textContent = job.input_kind === 'image' ? '还原布局' : '核对引脚';
  const report = job.check_report || {};
  $('#check-score').hidden = report.score == null;
  $('#check-score').textContent = `自检 ${report.score ?? ''} / 100`;
  const progressIndex = job.status === 'completed' ? 3 : job.layout?.pins?.length ? (job.status === 'awaiting_input' && job.pending_input?.stage === 'output' ? 1 : 2) : ['conflicts', 'review_diffs', 'facts'].includes(job.pending_input?.stage) || job.graph_status?.startsWith('stopped_check') ? 1 : 0;
  $('#progress').querySelectorAll('li').forEach((item, index) => { item.classList.toggle('done', index < progressIndex); item.classList.toggle('current', index === progressIndex); });
  renderPreview(job.layout || {}); renderPins(job.pins || []);
  renderQuestion(job);
  const findings = [...(report.findings || []).map(item => ({ text: item.title, severity: item.severity })), ...[...new Set(job.warnings || [])].map(text => ({ text, severity: 'warn' }))];
  $('#findings').replaceChildren(...findings.map(item => node('p', `finding ${item.severity || ''}`, item.text || '')));
  $('#artifact-section').hidden = !(job.artifacts || []).length;
  const exec = job.capture_execution || {};
  $('#execution-summary').textContent = exec.mode ? `${exec.mode === 'ssh' ? '远端 Cadence' : '本机 Cadence'}${exec.exit_code != null ? ' · 退出码 ' + exec.exit_code : ''}` : '';
  $('#artifacts').replaceChildren(...['olb', 'dsn', 'tcl', 'capture_log'].filter(name => job.artifacts?.includes(name)).map(name => {
    const [title, help, extension] = artifactInfo[name]; const button = node('button', 'artifact-button'); button.type = 'button'; button.dataset.artifact = name;
    button.append(node('strong', '', title), node('span', '', '↧'), node('small', '', help));
    button.addEventListener('click', () => download(job.drawing_id, name, name === 'capture_log' ? 'capture.log' : `${job.device || 'symbol'}${extension}`, button));
    return button;
  }));
  $('#log-section').hidden = !job.capture_log_tail;
  $('#capture-log').textContent = job.capture_log_tail || '';
  const history = job.human_history || [];
  $('#human-history-section').hidden = !history.length;
  $('#human-history').replaceChildren(...history.map(item => {
    const answer = item.answer || item; let text = stages[item.stage] || item.stage;
    if (answer.action === 'auto') text += ' · 自动识别';
    else if (answer.action === 'cancel') text += ' · 已取消';
    else if (answer.action === 'confirm') text += ' · 已确认';
    if (answer.value) text += ` · ${answer.value}`;
    if (answer.values) text += ` · ${Object.entries(answer.values).map(([key, value]) => `${key}：${value}`).join('，')}`;
    if (answer.resolutions) text += ` · ${answer.resolutions.length} 项已裁决`;
    return node('li', '', text);
  }));
  const historyButton = [...$('#history').querySelectorAll('.history-item')].find(item => item.dataset.id === job.drawing_id);
  if (historyButton) { historyButton.dataset.status = job.status; historyButton.querySelector('.mini-status').textContent = statuses[job.status] || job.status; }
}

function svgElement(tag, attrs = {}, text = '') {
  const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, String(value));
  if (text) element.textContent = text;
  return element;
}

function renderPreview(layout) {
  const key = JSON.stringify(layout); if (key === previewKey) return; previewKey = key;
  $('#layout-summary').textContent = layout.summary || '';
  for (const id of ['#zoom-in', '#zoom-out', '#zoom-fit']) $(id).disabled = !layout.pins?.length;
  if (!layout.pins?.length) { $('#preview').replaceChildren(node('p', 'muted', '确认引脚后，这里将显示符号布局。')); return; }
  const w = Number(layout.body_width), h = Number(layout.body_height), hh = Number(layout.half_height);
  if (![w, h, hh].every(Number.isFinite) || w <= 0 || h <= 0) return;
  $('#pin-count').textContent = `${layout.pins.length} 个引脚`;
  const svg = svgElement('svg', { viewBox: `${-w / 2 - 35} ${-hh - 35} ${w + 70} ${h + 70}`, role: 'img', 'aria-label': `${layout.part_name} 引脚布局预览` });
  svg.append(svgElement('title', {}, `${layout.part_name} 引脚布局`), svgElement('rect', { x: -w / 2, y: -hh, width: w, height: h, fill: '#fff', stroke: '#697bba', 'stroke-width': .8 }));
  const maxName = Math.max(...layout.pins.map(pin => String(pin.name || '').length), 1);
  const imageLayout = activeJob?.input_kind === 'image';
  const nameSize = Math.min(imageLayout ? 11 : 5.5, Math.max(2.5, (w / 2 - 12) / (maxName * .6)));
  const numberSize = imageLayout ? 8 : 5;
  for (const pin of layout.pins) {
    const [x, y, hx, hy] = [pin.x, pin.y, pin.hx, pin.hy].map(Number);
    if (![x, y, hx, hy].every(Number.isFinite)) continue;
    svg.append(svgElement('line', { x1: x, y1: y, x2: hx, y2: hy, stroke: '#697bba', 'stroke-width': .7 }), svgElement('circle', { cx: hx, cy: hy, r: .9, fill: '#697bba' }));
    const horizontal = pin.side === 'left' || pin.side === 'right', left = pin.side === 'left', top = pin.side === 'top';
    const name = horizontal ? { x: x + (left ? 4 : -4), y: y + nameSize * .32, 'text-anchor': left ? 'start' : 'end' } : { x: x + 1.7, y: y + (top ? 6 : -6), transform: `rotate(-90 ${x + 1.7} ${y + (top ? 6 : -6)})`, 'text-anchor': top ? 'end' : 'start' };
    const number = horizontal ? { x: hx + (left ? -3 : 3), y: y + numberSize * .32, 'text-anchor': left ? 'end' : 'start' } : { x, y: hy + (top ? -3 : 7), 'text-anchor': 'middle' };
    svg.append(svgElement('text', { ...name, fill: '#354161', 'font-size': nameSize, 'font-family': 'Consolas, Microsoft YaHei, monospace' }, String(pin.name)), svgElement('text', { ...number, fill: '#7181ae', 'font-size': numberSize, 'font-family': 'Consolas, monospace' }, String(pin.number)));
  }
  $('#preview').replaceChildren(svg);
  zoom = 1; applyZoom();
}

function applyZoom() {
  const svg = $('#preview svg'); if (!svg) return;
  const [, , width, height] = svg.getAttribute('viewBox').split(' ').map(Number);
  const fit = Math.min(Math.max(100, $('#preview').clientWidth - 44), Math.max(120, $('#preview').clientHeight - 44) * width / height);
  svg.style.width = `${fit * zoom}px`; svg.style.height = `${fit * zoom * height / width}px`;
  svg.style.maxHeight = 'none'; svg.style.minHeight = '0';
  $('#zoom-out').disabled = zoom <= .5; $('#zoom-in').disabled = zoom >= 4;
}
$('#zoom-in').addEventListener('click', () => { zoom = Math.min(4, zoom * 1.5); applyZoom(); });
$('#zoom-out').addEventListener('click', () => { zoom = Math.max(.5, zoom / 1.5); applyZoom(); });
$('#zoom-fit').addEventListener('click', () => { zoom = 1; applyZoom(); $('#preview').scrollTo(0, 0); });
window.addEventListener('resize', applyZoom);

function renderPins(pins) {
  const key = JSON.stringify(pins); if (key === pinKey) return; pinKey = key;
  $('#pin-section').hidden = !pins.length; $('#pin-table-count').textContent = `${pins.length} 个引脚`;
  $('#pin-table').replaceChildren(...pins.map(pin => {
    const row = node('tr');
    for (const text of [pin.pin_number, pin.name, pin.type, sides[pin.side] || pin.side, pin.description]) row.append(node('td', '', text || '—'));
    return row;
  }));
}

function labelledInput(label, options = [], value = '', required = true) {
  const wrapper = node('label', '', label);
  const input = node(options.length ? 'select' : 'input'); input.required = required;
  if (options.length) {
    input.append(new Option('请选择', ''));
    for (const option of options) input.append(new Option(sides[option] || option, option));
  } else { input.type = 'text'; input.maxLength = 200; }
  input.value = value; wrapper.append(input); return { wrapper, input };
}

function renderQuestion(job) {
  const question = !job.deleted_at && job.status === 'awaiting_input' ? job.pending_input : null;
  const key = question ? `${job.drawing_id}:${question.interrupt_id}` : `${job.drawing_id}:${job.status}:${job.deleted_at || ''}:${job.reason || job.error_msg || ''}`;
  if (questionKey === key) return; questionKey = key;
  const area = $('#question-area'); area.replaceChildren();
  if (job.deleted_at) {
    area.append(node('p', 'terminal-message', '任务已从历史移除，解析与回溯记录仍保留。恢复任务后可继续处理。'));
    return;
  }
  if (!question) {
    const messages = { ai_processing: job.input_kind === 'image' ? '正在读取封装图片并生成符号。只有看不清的引脚需要你补充。' : '正在分析手册和引脚信息。需要你确认时，这里会显示问题。', completed: '符号已生成。你可以下载 OLB，并在 Cadence 中打开核对。', rejected: '你已取消这次生成。可新建任务重新上传 PDF 手册或图片。', awaiting_input: '正在同步待确认的问题…' };
    const message = node('p', `terminal-message ${['failed', 'stopped'].includes(job.status) ? 'error' : ''}`);
    if (job.status === 'ai_processing') {
      message.classList.add('processing');
      message.setAttribute('role', 'status');
      const spinner = node('span', 'processing-spinner');
      spinner.setAttribute('aria-hidden', 'true');
      message.append(spinner);
    }
    message.append(node('span', '', (job.status === 'ai_processing' ? '工作中 · ' : '') + (job.reason || job.error_msg || messages[job.status] || '任务已停止，请查看自检结果或生成日志。')));
    area.append(message);
    return;
  }
  area.append(node('span', 'question-stage', stages[question.stage] || '需要确认'), node('p', 'question-title', question.stage === 'output' ? '请核对引脚布局。确认后，将生成 OLB 符号库和 DSN 原理图。' : question.question));
  if (question.warnings?.length) {
    const warnings = node('ul', 'question-warnings'); warnings.append(...question.warnings.map(text => node('li', '', text))); area.append(warnings);
  }
  const form = node('form', 'question-form'); const options = question.options || []; let readAnswer;
  const actions = node('div', 'answer-actions'), errorBox = node('p', 'form-error'); errorBox.hidden = true; errorBox.setAttribute('role', 'alert');
  function addAction(text, action, answerReader = () => ({ action }), validate = true, primary = false) {
    if (!options.includes(action)) return;
    const button = node('button', primary ? 'primary' : 'secondary', text); button.type = 'button'; button.dataset.action = action;
    button.addEventListener('click', async () => {
      if (validate && !form.reportValidity()) return;
      const answer = answerReader(); if (!answer) return;
      answer.comment = comment.value.trim();
      await sendAnswer(job.drawing_id, question.interrupt_id, answer, form, errorBox);
    });
    actions.append(button);
  }
  if (['device', 'package'].includes(question.stage)) {
    let candidate;
    if (options.includes('choose')) {
      candidate = labelledInput('候选项', question.candidates || [], '', false); form.append(candidate.wrapper);
    }
    let manual;
    if (options.includes('provide')) {
      manual = labelledInput(candidate ? '或手工填写' : stages[question.stage], [], '', false);
      manual.input.placeholder = question.stage === 'device' ? '例如 ADS1230' : '例如 TSSOP-16'; form.append(manual.wrapper);
      readAnswer = () => {
        manual.input.setCustomValidity(question.stage === 'device' || question.candidates?.length ? (manual.input.value.trim() ? '' : '请填写此项。') : '');
        if (!manual.input.reportValidity()) return null;
        return { action: 'provide', value: manual.input.value.trim() };
      };
      manual.input.addEventListener('input', () => manual.input.setCustomValidity(''));
      addAction('提交填写内容', 'provide', readAnswer, false, true);
    }
    if (candidate) addAction('采用候选项', 'choose', () => {
      candidate.input.setCustomValidity(candidate.input.value ? '' : '请选择候选项。');
      if (!candidate.input.reportValidity()) return null;
      return { action: 'choose', value: candidate.input.value };
    }, false, !manual);
    if (candidate) candidate.input.addEventListener('change', () => candidate.input.setCustomValidity(''));
  } else if (['conflicts', 'review_diffs'].includes(question.stage)) {
    const inputs = (question.items || []).map((item, index) => {
      const group = node('fieldset'); group.append(node('legend', '', `引脚 ${item.pin_number} · ${fieldNames[item.field] || item.field}`));
      const hint = node('div', 'value-hint');
      const values = question.stage === 'conflicts' ? Object.entries(item.values || {}) : [['提取值', item.expected], ['复核值', item.observed]];
      for (const [source, value] of values) hint.append(node('span', '', `${source}：${sides[value] || value}`));
      if (item.pages?.length) hint.append(node('span', '', `手册页码：${item.pages.join('、')}`)); group.append(hint);
      const choice = labelledInput('采信值', item.field === 'side' ? Object.keys(sides) : [], question.stage === 'review_diffs' ? item.suggested || '' : '');
      if (item.field !== 'side') {
        const id = `choices-${index}`; choice.input.setAttribute('list', id); const list = node('datalist'); list.id = id;
        for (const value of new Set(values.map(entry => String(entry[1] ?? '')).filter(Boolean))) list.append(new Option(value, value)); group.append(list);
      }
      group.append(choice.wrapper); form.append(group); return { item, input: choice.input };
    });
    readAnswer = () => ({ action: 'resolve', resolutions: inputs.map(({ item, input }) => ({ pin_number: String(item.pin_number), field: item.field, value: input.value.trim() })) });
    addAction('提交全部裁决', 'resolve', readAnswer, true, true);
  } else if (question.stage === 'facts') {
    const inputs = (question.items || []).map(item => {
      const choice = labelledInput(item.prompt || item.key, [], item.default || '', false); form.append(choice.wrapper);
      if (item.options?.length) { const help = node('p', 'muted', `可选值：${item.options.join('、')}`); form.append(help); }
      return { item, input: choice.input };
    });
    readAnswer = () => ({ action: 'provide', values: Object.fromEntries(inputs.map(({ item, input }) => [item.key, input.value.trim()])) });
    addAction('提交补充信息', 'provide', readAnswer, true, true);
  } else if (question.stage === 'output') {
    readAnswer = () => ({ action: 'confirm' }); addAction('确认并生成 OLB / DSN', 'confirm', readAnswer, true, true);
  }
  const commentLabel = node('label', '', '备注（可选）'), comment = node('textarea'); comment.rows = 2; comment.maxLength = 2000; commentLabel.append(comment); form.append(commentLabel);
  addAction('取消任务', 'cancel', () => ({ action: 'cancel' }), false);
  form.addEventListener('submit', event => { event.preventDefault(); actions.querySelector('.primary')?.click(); });
  form.append(errorBox, actions); area.append(form);
}

async function sendAnswer(id, interruptId, answer, form, errorBox) {
  const generation = epoch;
  const controls = [...form.querySelectorAll('button,input,select,textarea')]; controls.forEach(control => control.disabled = true);
  errorBox.hidden = true;
  try {
    await api.answer(id, interruptId, answer);
    if (generation !== epoch || !token) return;
    flash(answer.action === 'cancel' ? '取消请求已提交。' : '确认已提交，任务正在继续。');
    questionKey = ''; await refreshJob(generation); loadHistory(true);
  } catch (error) {
    if (generation !== epoch || !token) return;
    errorBox.textContent = error.message; errorBox.hidden = false;
    if (error.status === 409) { flash('当前问题已变更，正在同步最新状态。', true); await refreshJob(generation); }
  } finally { controls.forEach(control => control.disabled = false); }
}

async function download(id, name, filename, button) {
  const account = identity, requestToken = token;
  button.disabled = true;
  try {
    const data = await api.download(id, name, filename);
    if (!token || identity !== account || token !== requestToken) return;
    const url = URL.createObjectURL(data.blob), link = node('a'); link.href = url; link.download = data.filename;
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { if (token && identity === account && token === requestToken) flash(error.message, true); }
  finally { button.disabled = false; }
}
$('#source-download').addEventListener('click', event => { if (activeId) download(activeId, 'source', activeJob?.original_filename || 'datasheet.pdf', event.currentTarget); });
$('#restore-job').addEventListener('click', event => { if (activeId) changeVisibility(activeId, false, event.currentTarget); });
$('#trace-download').addEventListener('click', async event => {
  const id = activeId, account = identity, requestToken = token, button = event.currentTarget;
  if (!id) return;
  button.disabled = true;
  try {
    const trace = await api.trace(id);
    if (!token || identity !== account || token !== requestToken || activeId !== id) return;
    const blob = new Blob([JSON.stringify(trace, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob), link = node('a'); link.href = url; link.download = `symbol-${id}-trace.json`;
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { if (token && identity === account && token === requestToken) flash(error.message, true); }
  finally { button.disabled = false; }
});
window.addEventListener('pagehide', () => stopPolling());
if (token) enterWorkspace();
