// Real Chromium UI tests with a local mock API; no database/model/Cadence calls.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require('playwright')); } catch { /* optional browser dependency */ }
const root = new URL('../symbol/', import.meta.url);
const screenshots = new URL('../../output/symbol_agent/ui/', import.meta.url);
const id = '5b3dd468-95b7-4eb6-a4a5-415732f64501';
const stages = [
  { stage: 'device', question: '请选择型号', options: ['choose', 'provide', 'cancel'], candidates: ['ADS1230', 'ADS1115'] },
  { stage: 'package', question: '请选择封装', options: ['choose', 'provide', 'cancel'], candidates: ['TSSOP-16', 'SOIC-16'] },
  { stage: 'conflicts', question: '请裁决引脚侧别', options: ['resolve', 'cancel'], items: [{ pin_number: '1', field: 'side', values: { 文本: 'left', 视觉: 'right' }, pages: [3] }] },
  { stage: 'review_diffs', question: '请裁决复核差异', options: ['resolve', 'cancel'], items: [{ pin_number: '2', field: 'name', expected: 'AIN1', observed: 'A1N1', suggested: 'AIN1' }] },
  { stage: 'facts', question: '请补充信息', options: ['provide', 'cancel'], items: [{ key: 'package', prompt: '请确认封装代码', default: 'TSSOP-16', options: ['TSSOP-16'] }] },
  { stage: 'output', question: '请确认输出', options: ['confirm', 'cancel'], warnings: ['请核对手册中的引脚编号与布局。'] },
];
const pins = [{ pin_number: '1', name: 'AIN0', type: 'IN', side: 'left', description: '模拟输入' }, { pin_number: '2', name: 'AIN1', type: 'IN', side: 'right', description: '模拟输入' }];
const layout = { part_name: 'ADS1230', body_width: 100, body_height: 40, half_height: 20, summary: 'ADS1230：2 脚（左 1 / 右 1）', pins: [
  { number: '1', name: 'AIN0', side: 'left', x: -50, y: 0, hx: -60, hy: 0 }, { number: '2', name: 'AIN1', side: 'right', x: 50, y: 0, hx: 60, hy: 0 },
] };

async function fixture({ imageJob = null } = {}) {
  const state = { created: false, stage: 0, answers: [], downloads: [], uploads: [], unauthorized: false, history: [], stale: false, requestDelay: 0, deletedAt: null, visibility: [], traces: [], forcedStatus: null, rejectDelete: false };
  const date = '2026-10-09T01:00:00Z';
  function job() {
    if (imageJob) return { drawing_id: id, deleted_at: state.deletedAt, created_at: date, ...imageJob };
    const completed = state.stage >= stages.length;
    return { drawing_id: id, status: state.forcedStatus || (completed ? 'completed' : 'awaiting_input'), deleted_at: state.deletedAt, device: state.stage ? 'ADS1230' : '', package: state.stage >= 2 ? 'TSSOP-16' : '', original_filename: 'ADS1230.pdf', created_at: date,
      graph_status: completed ? 'completed' : 'layout_built', pins: state.stage >= 2 ? pins : [], layout: state.stage >= 5 ? layout : {},
      pending_input: completed ? null : { ...stages[state.stage], interrupt_id: `question-${state.stage}` },
      check_report: { score: 100, verdict: 'ok', findings: [] }, warnings: [], human_history: state.history,
      artifacts: completed ? ['olb', 'dsn', 'tcl', 'capture_log'] : [], capture_execution: completed ? { mode: 'ssh', exit_code: 0 } : {},
      capture_log_tail: completed ? '===== CAPTURE RESULT: PASS =====' : '' };
  }
  const server = createServer(async (request, response) => {
    try {
      const url = new URL(request.url, 'http://localhost');
      const send = (value, status = 200) => { response.writeHead(status, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(value)); };
      const chunks = []; for await (const chunk of request) chunks.push(chunk); const body = Buffer.concat(chunks).toString();
      if (url.pathname === '/api/v1/auth/login') return send({ access_token: 'test-token' });
      const files = { '/api/v1/symbol/ui': ['index.html', 'text/html'], '/api/v1/symbol/ui/assets/styles.css': ['styles.css', 'text/css'], '/api/v1/symbol/ui/assets/app.js': ['app.js', 'text/javascript'], '/api/v1/symbol/ui/assets/api.js': ['api.js', 'text/javascript'] };
      if (files[url.pathname]) { const [file, type] = files[url.pathname]; response.writeHead(200, { 'Content-Type': type }); return response.end(await readFile(new URL(file, root))); }
      if (state.unauthorized || request.headers.authorization !== 'Bearer test-token') return send({ detail: 'expired' }, 401);
      if (url.pathname === '/api/v1/auth/me') return send({ tenant_id: 'test-tenant', user_id: 'test-user' });
      if (url.pathname === '/api/v1/symbol/drawings') return send({ items: state.created && Boolean(state.deletedAt) === (url.searchParams.get('deleted') === 'true') ? [{ drawing_id: id, device: job().device || 'ADS1230', status: job().status, deleted_at: state.deletedAt, original_filename: job().original_filename, created_at: date }] : [], has_more: false });
      if (url.pathname === `/api/v1/symbol/drawings/${id}` && request.method === 'DELETE') {
        if (state.rejectDelete) return send({ detail: '任务正在执行，请稍后重试' }, 409);
        state.deletedAt = date; state.visibility.push('delete'); return send({ deleted: true, records_preserved: true });
      }
      if (url.pathname === `/api/v1/symbol/drawings/${id}/restore`) {
        state.deletedAt = null; state.visibility.push('restore'); return send({ deleted: false, records_preserved: true });
      }
      if (url.pathname === `/api/v1/symbol/drawings/${id}/trace`) {
        state.traces.push(request.headers.authorization);
        return send({ drawing_id: id, thread_id: `symbol:${id}`, latest_checkpoint: { values: { pins, status: job().status } }, checkpoints: [{ checkpoint_id: 'saved-checkpoint', step: 3 }] });
      }
      if (url.pathname === '/api/v1/symbol/generate') { state.created = true; state.uploads.push(body); return send({ drawing_id: id, status: 'ai_processing' }, 202); }
      if (url.pathname.endsWith('/human-input')) {
        const answer = JSON.parse(body);
        if (state.stale) { state.stale = false; return send({ detail: '人工问题已过期' }, 409); }
        if (answer.interrupt_id !== `question-${state.stage}`) return send({ detail: 'stale id' }, 409);
        state.answers.push(answer); state.history.push({ stage: stages[state.stage].stage, ...answer.answer });
        state.stage = answer.answer.action === 'cancel' ? stages.length : state.stage + 1;
        return send({ status: 'ai_processing' });
      }
      if (url.pathname === `/api/v1/symbol/drawings/${id}`) {
        if (state.requestDelay) await new Promise(resolve => setTimeout(resolve, state.requestDelay));
        return send(job());
      }
      if (url.pathname.includes('/artifacts/') || url.pathname.endsWith('/source')) {
        state.downloads.push({ path: url.pathname, auth: request.headers.authorization });
        const isSource = url.pathname.endsWith('/source');
        const name = isSource ? job().original_filename : job().device + '.' + url.pathname.split('/').pop().toUpperCase();
        response.writeHead(200, { 'Content-Type': imageJob && isSource ? 'image/png' : 'application/octet-stream', 'Content-Disposition': `attachment; filename="${name}"` });
        return response.end(imageJob && isSource ? await readFile(new URL('../../tests/golden/symbol/mcp3021/pin-diagram.png', import.meta.url)) : 'test artifact');
      }
      send({ detail: 'not found' }, 404);
    } catch (error) { response.writeHead(500); response.end(error.message); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
  const page = await context.newPage();
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  const url = `http://127.0.0.1:${server.address().port}/api/v1/symbol/ui`;
  async function login() { await page.goto(url); await page.locator('#username').fill('test-user'); await page.locator('#password').fill('test-password'); await page.locator('#login-button').click(); await page.locator('#workspace').waitFor({ state: 'visible' }); }
  async function close() { await browser.close(); await new Promise(resolve => server.close(resolve)); }
  return { state, page, context, errors, url, login, close };
}

test('browser: PDF upload, all six human stages, authenticated downloads and reload', { skip: !chromium, timeout: 60000 }, async () => {
  const f = await fixture();
  try {
    await f.login(); await mkdir(screenshots, { recursive: true });
    await f.page.screenshot({ path: fileURLToPath(new URL('symbol-upload.png', screenshots)), fullPage: true });
    await f.page.locator('#datasheet').setInputFiles({ name: 'invalid.txt', mimeType: 'text/plain', buffer: Buffer.from('invalid') });
    await f.page.locator('#generate').click(); await f.page.getByText('请选择 PDF 手册或 PNG、JPG、WEBP、BMP 格式的封装图。', { exact: true }).waitFor(); assert.equal(f.state.uploads.length, 0);
    await f.page.locator('#datasheet').setInputFiles({ name: 'ADS1230.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.7') });
    assert.ok(await f.page.locator('#pdf-options').isVisible());
    assert.ok(await f.page.locator('#flash').isHidden());
    assert.match(await f.page.locator('#generate').textContent(), /解析手册/);
    await f.page.locator('#strict-pages').check();
    await f.page.screenshot({ path: fileURLToPath(new URL('symbol-pdf-upload.png', screenshots)), fullPage: true });
    await f.page.locator('#device').fill('ADS1230'); await f.page.locator('#generate').click();
    await f.page.locator('.question-stage').getByText('确认型号', { exact: true }).waitFor();
    assert.match(f.state.uploads[0], /name="device"\r\n\r\nADS1230/); assert.match(f.state.uploads[0], /name="strict_pages"\r\n\r\ntrue/);
    await f.page.getByLabel('候选项').selectOption('ADS1230'); await f.page.getByRole('button', { name: '采用候选项' }).click();
    await f.page.locator('.question-stage').getByText('确认封装', { exact: true }).waitFor();
    await f.page.getByLabel('或手工填写').fill('TSSOP-16'); await f.page.getByRole('button', { name: '提交填写内容' }).click();
    await f.page.locator('.question-stage').getByText('引脚冲突', { exact: true }).waitFor();
    await f.page.getByLabel('采信值').selectOption('left'); await f.page.getByRole('button', { name: '提交全部裁决' }).click();
    await f.page.locator('.question-stage').getByText('复核差异', { exact: true }).waitFor();
    assert.equal(await f.page.getByLabel('采信值').inputValue(), 'AIN1'); await f.page.getByRole('button', { name: '提交全部裁决' }).click();
    await f.page.locator('.question-stage').getByText('补充信息', { exact: true }).waitFor();
    await f.page.getByRole('button', { name: '提交补充信息' }).click();
    await f.page.getByRole('button', { name: '确认并生成 OLB / DSN' }).waitFor();
    await f.page.getByRole('img', { name: 'ADS1230 引脚布局预览' }).waitFor();
    const originalWidth = await f.page.locator('#preview svg').evaluate(svg => svg.getBoundingClientRect().width);
    await f.page.getByRole('button', { name: '放大引脚布局' }).click();
    assert.ok(await f.page.locator('#preview svg').evaluate(svg => svg.getBoundingClientRect().width) > originalWidth);
    await f.page.getByRole('button', { name: '适配引脚布局' }).click();
    await f.page.screenshot({ path: fileURLToPath(new URL('symbol-confirm-desktop.png', screenshots)), fullPage: true });
    await f.page.setViewportSize({ width: 390, height: 844 });
    await f.page.screenshot({ path: fileURLToPath(new URL('symbol-confirm-mobile.png', screenshots)), fullPage: true });
    assert.ok(await f.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile page should not overflow');
    await f.page.setViewportSize({ width: 1440, height: 1000 });
    await f.page.getByRole('button', { name: '确认并生成 OLB / DSN' }).click();
    await f.page.locator('#job-status').getByText('已完成', { exact: true }).waitFor();
    assert.equal(f.state.answers.length, 6); assert.equal(f.state.answers[2].answer.resolutions[0].value, 'left');
    assert.deepEqual(f.state.answers[4].answer.values, { package: 'TSSOP-16' });
    assert.equal(f.state.answers[5].answer.action, 'confirm');
    assert.match(await f.page.locator('#human-history').textContent(), /已确认/);
    for (const name of ['olb', 'dsn', 'tcl', 'capture_log']) {
      const downloaded = f.page.waitForEvent('download'); await f.page.locator(`[data-artifact="${name}"]`).click(); assert.ok((await downloaded).suggestedFilename());
    }
    const source = f.page.waitForEvent('download'); await f.page.locator('#source-download').click(); assert.equal((await source).suggestedFilename(), 'ADS1230.pdf');
    assert.equal(f.state.downloads.length, 5); assert.ok(f.state.downloads.every(item => item.auth === 'Bearer test-token'));
    await f.page.reload(); await f.page.locator('#job-status').getByText('已完成', { exact: true }).waitFor();
    assert.equal(await f.page.locator('#job-title').textContent(), 'ADS1230');
    await f.page.screenshot({ path: fileURLToPath(new URL('symbol-result.png', screenshots)), fullPage: true });
    assert.deepEqual(f.errors, []);
  } finally { await f.close(); }
});

test('browser: image generates directly with aligned pins and original image download', { skip: !chromium, timeout: 60000 }, async () => {
  const imagePins = [
    { number: '1', name: 'VDD', side: 'left', x: -50, y: -60, hx: -60, hy: -60 },
    { number: '2', name: 'VSS', side: 'left', x: -50, y: 0, hx: -60, hy: 0 },
    { number: '3', name: 'AIN', side: 'left', x: -50, y: 60, hx: -60, hy: 60 },
    { number: '5', name: 'SCL', side: 'right', x: 50, y: -60, hx: 60, hy: -60 },
    { number: '4', name: 'SDA', side: 'right', x: 50, y: 60, hx: 60, hy: 60 },
  ];
  const f = await fixture({ imageJob: {
    input_kind: 'image', device: 'MCP3021', package: 'SOT-23A', original_filename: 'MCP3021.png',
    status: 'completed', graph_status: 'completed', pending_input: null,
    pins: imagePins.map(p => ({ pin_number: p.number, name: p.name, side: p.side, type: 'PAS' })),
    layout: { part_name: 'MCP3021', body_width: 100, body_height: 220, half_height: 110, pins: imagePins },
    check_report: { score: 100, verdict: 'ok', findings: [] }, warnings: [], human_history: [],
    artifacts: ['olb', 'dsn', 'tcl', 'capture_log'], capture_execution: { mode: 'ssh', exit_code: 0 },
    capture_log_tail: '===== CAPTURE RESULT: PASS =====',
  } });
  try {
    await f.login();
    await f.page.locator('#datasheet').setInputFiles({ name: 'manual.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.7') });
    await f.page.locator('#strict-pages').check();
    await f.page.locator('#datasheet').setInputFiles({ name: 'MCP3021.png', mimeType: 'image/png',
      buffer: await readFile(new URL('../../tests/golden/symbol/mcp3021/pin-diagram.png', import.meta.url)) });
    assert.ok(await f.page.locator('#pdf-options').isHidden());
    assert.match(await f.page.locator('#generate').textContent(), /照图生成/);
    await f.page.locator('#generate').click();
    await f.page.locator('#job-status').getByText('已完成', { exact: true }).waitFor();
    assert.equal(await f.page.locator('.question-form').count(), 0);
    assert.equal(await f.page.locator('#job-title').textContent(), 'MCP3021');
    for (const pin of imagePins) await f.page.getByRole('cell', { name: pin.name, exact: true }).waitFor();
    const rows = await f.page.locator('#preview svg text').evaluateAll(texts => Object.fromEntries(texts
      .filter(t => /^[1-5]$/.test(t.textContent)).map(t => [t.textContent, t.getAttribute('y')])));
    assert.equal(rows['1'], rows['5']); assert.equal(rows['3'], rows['4']); assert.notEqual(rows['2'], rows['4']);
    assert.ok(await f.page.locator('#preview svg text').filter({ hasText: /^VDD$/ }).evaluate(t => t.getBoundingClientRect().height >= 10));
    assert.match(await f.page.locator('#source-download').textContent(), /下载原图/);
    const promise = f.page.waitForEvent('download'); await f.page.locator('#source-download').click();
    const download = await promise;
    assert.equal(download.suggestedFilename(), 'MCP3021.png');
    assert.deepEqual(await readFile(await download.path()), await readFile(new URL('../../tests/golden/symbol/mcp3021/pin-diagram.png', import.meta.url)));
    assert.deepEqual(f.state.answers, []); assert.equal(f.state.uploads.length, 1);
    assert.doesNotMatch(f.state.uploads[0], /name="strict_pages"/);
    await mkdir(screenshots, { recursive: true });
    await f.page.screenshot({ path: fileURLToPath(new URL('symbol-image-result.png', screenshots)), fullPage: true });
    assert.deepEqual(f.errors, []);
  } finally { await f.close(); }
});

test('browser: delete hides task, archived trace downloads and restore keeps same task', { skip: !chromium, timeout: 60000 }, async () => {
  const f = await fixture();
  f.state.created = true; f.state.stage = stages.length;
  try {
    await f.login(); await f.page.locator('.history-item').click();
    await f.page.locator('#job-status').getByText('已完成', { exact: true }).waitFor();
    await f.page.getByRole('button', { name: '删除任务 ADS1230', exact: true }).click();
    await f.page.getByText('任务已移出历史。回溯记录与文件保留，可在“已删除”中恢复。', { exact: true }).waitFor();
    assert.ok(await f.page.locator('#upload-view').isVisible());
    assert.equal(await f.page.locator('.history-item').count(), 0);
    assert.equal(await f.page.evaluate(() => sessionStorage.getItem('symbol-last-drawing:test-tenant:test-user')), null);
    await f.page.locator('#deleted-history').click(); await f.page.locator('.history-item').click();
    await f.page.getByText('已删除 · 记录保留', { exact: true }).waitFor();
    assert.equal(await f.page.locator('.question-form').count(), 0);
    const downloadPromise = f.page.waitForEvent('download');
    await f.page.locator('#trace-download').click();
    const download = await downloadPromise;
    const trace = JSON.parse(await readFile(await download.path(), 'utf8'));
    assert.equal(trace.thread_id, `symbol:${id}`);
    assert.equal(trace.latest_checkpoint.values.pins.length, 2);
    assert.deepEqual(f.state.traces, ['Bearer test-token']);
    await mkdir(screenshots, { recursive: true });
    await f.page.screenshot({ path: fileURLToPath(new URL('symbol-deleted-trace.png', screenshots)), fullPage: true });
    await f.page.locator('#restore-job').click(); await f.page.getByText('任务已恢复。', { exact: true }).waitFor();
    assert.ok(await f.page.locator('#restore-job').isHidden());
    await f.page.locator('#deleted-history').click(); await f.page.locator('.history-item').waitFor();
    assert.equal(await f.page.locator('.history-item').getAttribute('data-id'), id);
    assert.deepEqual(f.state.visibility, ['delete', 'restore']); assert.deepEqual(f.state.answers, []);
    assert.deepEqual(f.errors, []);
  } finally { await f.close(); }
});

test('browser: running task delete is disabled and rejected deletion preserves history', { skip: !chromium, timeout: 60000 }, async () => {
  const f = await fixture();
  f.state.created = true; f.state.stage = stages.length; f.state.forcedStatus = 'ai_processing';
  try {
    await f.login(); await f.page.locator('.history-item').waitFor();
    assert.ok(await f.page.getByRole('button', { name: '删除任务 ADS1230', exact: true }).isDisabled());
    await f.page.locator('.history-item').click();
    await f.page.locator('#job-status').getByText('工作中',{exact:true}).waitFor();
    await f.page.locator('#question-area .processing-spinner').waitFor();
    assert.equal(await f.page.locator('#job-status').evaluate(n=>getComputedStyle(n,'::before').animationName),'processing-spin');
    f.state.forcedStatus = 'completed';await f.page.locator('#refresh-job').click();
    await f.page.locator('#job-status').getByText('已完成',{exact:true}).waitFor();
    assert.equal(await f.page.locator('#question-area .processing-spinner').count(),0);
    f.state.rejectDelete = true;
    await f.page.locator('#refresh-history').click();
    const button = f.page.getByRole('button', { name: '删除任务 ADS1230', exact: true });
    await button.click(); await f.page.getByText('任务正在执行，请稍后重试', { exact: true }).waitFor();
    assert.equal(await f.page.locator('.history-item').count(), 1);
    assert.equal(f.state.deletedAt, null); assert.deepEqual(f.state.visibility, []);
    assert.deepEqual(f.errors, []);
  } finally { await f.close(); }
});

test('browser: stale answers, preserved edits during refresh, safe text and expired session', { skip: !chromium, timeout: 60000 }, async () => {
  const f = await fixture();
  try {
    f.state.created = true;
    await f.login(); await f.page.locator('.history-item').click(); await f.page.locator('.question-form').waitFor();
    await f.page.getByLabel('或手工填写').fill('<img src=x onerror=alert(1)>');
    await f.page.locator('#refresh-job').click(); await f.page.waitForResponse(response => response.url().endsWith(id));
    assert.equal(await f.page.getByLabel('或手工填写').inputValue(), '<img src=x onerror=alert(1)>');
    f.state.stale = true;
    await f.page.getByRole('button', { name: '提交填写内容' }).click();
    await f.page.getByText('当前问题已变更，正在同步最新状态。', { exact: true }).waitFor();
    assert.equal(f.state.answers.length, 0);
    await f.page.getByRole('button', { name: '取消任务' }).click(); await f.page.locator('#job-status').getByText('已完成', { exact: true }).waitFor();
    // Inject hostile pin labels as data; UI must retain literal text.
    const saved = pins[0].name; pins[0].name = '<img src=x onerror=alert(1)>';
    await f.page.locator('#refresh-job').click(); await f.page.getByRole('cell', { name: '<img src=x onerror=alert(1)>', exact: true }).waitFor();
    assert.equal(await f.page.locator('#pin-table img').count(), 0); pins[0].name = saved;
    f.state.requestDelay = 300;
    const delayed = f.page.waitForResponse(response => response.url().endsWith(id));
    await f.page.locator('#refresh-job').click(); await f.page.locator('#new-job').click(); await delayed;
    assert.ok(await f.page.locator('#upload-view').isVisible()); assert.ok(await f.page.locator('#job-view').isHidden());
    f.state.requestDelay = 0; await f.page.locator('.history-item').click(); await f.page.locator('#job-status').getByText('已完成', { exact: true }).waitFor();
    f.state.unauthorized = true; await f.page.locator('#refresh-job').click(); await f.page.locator('#login-view').waitFor({ state: 'visible' });
    assert.equal(await f.page.evaluate(() => sessionStorage.getItem('step-access-token')), null);
    assert.deepEqual(f.errors, []);
  } finally { await f.close(); }
});
