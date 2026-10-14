import test from 'node:test';
import assert from 'node:assert/strict';
import { SymbolAPI, ApiError } from '../symbol/api.js';

test('task deletion, restore and trace requests retain authentication', async () => {
  const observed = [];
  const api = new SymbolAPI({ getToken: () => 'test-token', fetchImpl: async (url, options) => {
    observed.push({ url, method: options.method, auth: options.headers.Authorization });
    return Response.json({ records_preserved: true });
  } });
  await api.remove('job/id'); await api.restore('job/id'); await api.trace('job/id'); await api.list(20, 0, true);
  assert.deepEqual(observed.map(o => [o.url, o.method]), [
    ['/api/v1/symbol/drawings/job%2Fid', 'DELETE'],
    ['/api/v1/symbol/drawings/job%2Fid/restore', 'POST'],
    ['/api/v1/symbol/drawings/job%2Fid/trace', 'GET'],
    ['/api/v1/symbol/drawings?limit=20&offset=0&deleted=true', 'GET'],
  ]);
  assert.ok(observed.every(o => o.auth === 'Bearer test-token'));
});

test('upload preserves multipart data and sends authentication separately', async () => {
  const form = new FormData(); form.append('file', new Blob(['%PDF-1.7']), 'chip.pdf');
  let observed;
  const api = new SymbolAPI({ getToken: () => 'test-token', fetchImpl: async (url, options) => {
    observed = { url, options }; return Response.json({ drawing_id: 'id', status: 'ai_processing' });
  } });
  await api.generate(form);
  assert.equal(observed.url, '/api/v1/symbol/generate');
  assert.equal(observed.options.headers.Authorization, 'Bearer test-token');
  assert.equal(observed.options.body, form);
  assert.equal(observed.options.headers['Content-Type'], undefined);
});

test('answers carry the current interrupt id and JSON schema', async () => {
  let observed;
  const api = new SymbolAPI({ getToken: () => 'test-token', fetchImpl: async (url, options) => {
    observed = { url, options }; return Response.json({ status: 'ai_processing' });
  } });
  await api.answer('job/id', 'current-id', { action: 'resolve', resolutions: [{ pin_number: '1', field: 'side', value: 'left' }] });
  assert.equal(observed.url, '/api/v1/symbol/drawings/job%2Fid/human-input');
  const data = JSON.parse(observed.options.body);
  assert.equal(data.interrupt_id, 'current-id');
  assert.equal(data.answer.resolutions[0].value, 'left');
});

test('expired credentials clear the session; invalid login does not', async () => {
  let expired = 0;
  const api = new SymbolAPI({ getToken: () => 'expired', onUnauthorized: () => expired++, fetchImpl: async () => Response.json({ detail: 'Unauthorized' }, { status: 401 }) });
  await assert.rejects(api.get('job'), error => error instanceof ApiError && error.status === 401);
  assert.equal(expired, 1);
  await assert.rejects(api.login('user', 'bad-password'), error => error.status === 401);
  assert.equal(expired, 1);
});

test('structured validation errors become readable messages', async () => {
  const api = new SymbolAPI({ getToken: () => 'test-token', fetchImpl: async () => Response.json({ detail: [{ loc: ['body', 'device'], msg: 'String too long' }] }, { status: 422 }) });
  await assert.rejects(api.get('job'), error => error.message === 'String too long');
});

test('a late unauthorized response cannot log out a newly signed-in user', async () => {
  let token = 'old-token', expired = 0, respond;
  const api = new SymbolAPI({ getToken: () => token, onUnauthorized: () => expired++, fetchImpl: () => new Promise(resolve => { respond = resolve; }) });
  const oldRequest = api.get('old-job');
  token = 'new-token'; respond(Response.json({ detail: 'expired' }, { status: 401 }));
  await assert.rejects(oldRequest, error => error.status === 401);
  assert.equal(expired, 0);
});

test('downloads retain auth and accept UTF-8 filenames without paths', async () => {
  let observed;
  const api = new SymbolAPI({ getToken: () => 'test-token', fetchImpl: async (url, options) => {
    observed = { url, options }; return new Response('native document', { headers: { 'Content-Disposition': "attachment; filename*=utf-8''%E8%8A%AF%E7%89%87.OLB" } });
  } });
  const result = await api.download('job', 'olb', 'fallback.OLB');
  assert.equal(result.filename, '芯片.OLB');
  assert.equal(await result.blob.text(), 'native document');
  assert.equal(observed.options.headers.Authorization, 'Bearer test-token');
  assert.equal(observed.url, '/api/v1/symbol/drawings/job/artifacts/olb');
  await assert.rejects(api.download('job', '../secrets'), /未知文件类型/);
});
