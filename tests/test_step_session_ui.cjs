// Run with: node tests/test_step_session_ui.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../backend/api/v1/step_ui.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script); // Check the entire page script, including event bindings.
const deletion = script.slice(script.indexOf('async function deleteSession('), script.indexOf('const dimensionCopy ='));
const polling = script.slice(script.indexOf('async function poll('), script.indexOf('function statusLabel('));
const conversation = script.slice(script.indexOf('function readConversation('), script.indexOf("$('#generate-form').addEventListener('submit'"));
const publicMessage = script.slice(script.indexOf('function publicMessage('), script.indexOf("$('#new-session').addEventListener"));
const sendState = script.slice(script.indexOf('function setSendState('), script.indexOf('function publicMessage('));

function checkMessageHistory() {
  const storage = new Map();
  const context = vm.createContext({
    localStorage: { getItem: key => storage.get(key), removeItem: key => storage.delete(key), setItem: (key, value) => storage.set(key, value) },
    dimensionNames: {},
  });
  vm.runInContext(publicMessage + '\n' + conversation, context);
  for(const family of ['resistor/two_terminal_chip','capacitor/two_terminal_chip','inductor','diode','transistor','connector/cn/dsub_connector','connector/cn/pin_header','connector/mt','ic/gullwing_ic','ic/quad_gullwing_ic','ic/qfn_ufqfpn','misc']){
    assert(/[\u4e00-\u9fff]/.test(context.templateLabel(family)));
    assert(!context.templateLabel(family).includes(family));
  }
  assert.equal(context.formatAnswer('routing',{family_id:'ic/gullwing_ic'}),'选择模板：集成芯片 · 双侧鸥翼引脚封装');
  assert.equal(context.templateLabel('unknown/internal_id'),'其他封装模板');
  context.saveConversation('legacy-selection',[{role:'user',key:'history-a-0',text:'选择模板：connector/cn/dsub_connector'}]);
  context.seedConversation({drawing_id:'legacy-selection',result:{}});
  assert.equal(context.readConversation('legacy-selection')[0].text,'选择模板：连接器 · 梯形接口连接器');
  const logRows=context.executionLogRows([
    {id:'one',timestamp:'2026-10-08T07:00:00+00:00',phase:'started',message:'开始：识别图纸'},
    {id:'two',timestamp:'invalid-date',phase:'completed',message:'完成：识别图纸',duration_seconds:3.25},
    {id:'three',timestamp:'2026-10-08T07:00:00+00:00',phase:'error',message:"cannot import name 'DeltaChannelHistory' from C:\\Users\\scott\\site-packages"},
    {id:'four',timestamp:'2026-10-08T07:00:00+00:00',phase:'completed',message:'完成',duration_seconds:-1},null,
  ]);
  assert.equal(logRows.length,4);assert.equal(logRows[0].phase,'started');assert.equal(logRows[1].time,'—');
  assert.equal(logRows[1].duration,'3.3s');assert(!logRows[2].text.includes('DeltaChannelHistory'));assert.equal(logRows[3].duration,'');
  assert.equal(context.executionLogRows(null).length,0);
  assert.equal(context.executionLogRows(Array.from({length:650},(_,i)=>({id:String(i),message:'完成'}))).length,600);
  assert.equal(context.publicMessage("cannot import name 'DeltaChannelHistory' from C:\\Users\\scott\\site-packages"), '服务暂时无法处理，请稍后重试。');
  assert.equal(context.publicMessage('请填写封装名称'), '请填写封装名称');
  assert.equal(context.publicMessage('Could not parse response content as the length limit was reached - CompletionUsage(completion_tokens=1800)'), '图纸分析输出不完整，可以在原任务中重试当前步骤。');
  const job = { drawing_id: 'chat', result: { human_request: '帮我生成step数模文件', original_filename: 'MCU_图纸8.png' } };
  context.seedConversation(job);
  let messages = context.readConversation('chat');
  assert.equal(messages.length, 1);
  assert.equal(messages[0].role, 'user');
  assert.equal(messages[0].text, '帮我生成step数模文件\n\n附件：MCU_图纸8.png');
  context.seedConversation(job);
  assert.equal(context.readConversation('chat').length, 1); // Polls do not duplicate the request.

  // Legacy caches that contain only an error gain the original user message.
  context.saveConversation('chat', [{ role: 'system', key: 'terminal:failed', text: 'old error' }]);
  context.seedConversation(job);
  messages = context.readConversation('chat');
  assert.equal(messages[0].key, 'initial-request');
  assert.equal(messages.length, 1); // Previous failures are not part of the current transcript.
  context.appendConversation('chat', 'system', 'legacy-error', "cannot import name 'DeltaChannelHistory' from langgraph.checkpoint.base");
  context.appendConversation('chat', 'assistant', 'question:legacy', '进入 Jev 模板路由前，只支持 implemented 模板。');
  context.seedConversation(job);
  assert(!context.readConversation('chat').some(item=>item.text.includes('DeltaChannelHistory')));
  assert(context.readConversation('chat').some(item=>item.text==='请确认使用的封装模板。'));

  // The initial user message must not count as an answer to a human question.
  job.result.human_history = [{ stage: 'package', action: 'provide', package_type: 'TSSOP-16' }];
  context.seedConversation(job);
  messages = context.readConversation('chat');
  assert.equal(messages[0].key, 'initial-request');
  assert(messages.some(item => item.role === 'user' && item.text === '封装：TSSOP-16'));
  assert(!messages.some(item => item.text === 'old error'));
  context.seedConversation(job);
  assert.equal(context.readConversation('chat').length, messages.length);

  // Rebuilding server history preserves the question currently awaiting input.
  context.saveConversation('chat', [{ role: 'assistant', key: 'question:current', text: '补充尺寸' }]);
  context.seedConversation(job);
  assert(context.readConversation('chat').some(item => item.key === 'question:current'));

  for(let i=0;i<120;i++)context.appendConversation('chat', 'system', `event-${i}`, `event ${i}`);
  messages = context.readConversation('chat');
  assert.equal(messages.length, 100);
  assert.equal(messages[0].key, 'initial-request');
  assert.equal(messages.at(-1).text, 'event 119');
  console.log('Message history checks passed: initial message, attachment name, polling, legacy cache, server history, pending question, bounded history.');
}

function harness({ confirmed = true, status = 200, filesDeleted = true, active = 'target' } = {}) {
  const storage = new Map([
    ['step-last-drawing-id', active],
    ['step-chat-target', 'target history'],
    ['step-chat-other', 'other history'],
  ]);
  if(active === null) storage.delete('step-last-drawing-id');
  const events = [];
  const message = { value: 'draft' }, submit = { disabled: true, dataset:{}, setAttribute(name,value){this[name]=value;} };
  const file = { value: 'drawing.png', dispatchEvent: () => events.push('file-reset') };
  const context = vm.createContext({
    token: 'test-token', activeSessionId: active, pollGeneration: 0, polling: 1,
    window: { confirm: () => confirmed },
    localStorage: { getItem: key => storage.get(key), removeItem: key => storage.delete(key), setItem: (key, value) => storage.set(key, value) },
    fetch: async (url, options) => {
      events.push({ url, options });
      return { ok: status === 200, status, json: async () => ({ files_deleted: filesDeleted, detail: '会话正在处理' }) };
    },
    clearTimeout: () => events.push('poll-cancelled'),
    Event: class {}, file,
    $: selector => selector === '#message' ? message : submit,
    renderEmptyWorkspace: () => events.push('empty-workspace'),
    markActiveSession: () => {},
    loadRecentJobs: async reset => events.push({ listReset: reset }),
    show: (text, error) => events.push({ text, error }),
  });
  vm.runInContext(sendState + '\n' + deletion + '\n' + polling, context);
  const button = { disabled: false };
  return { context, storage, events, file, message, submit, button, run: () => context.deleteSession({ drawing_id: 'target', human_request: 'Test session' }, button) };
}

async function main() {
  checkMessageHistory();
  const stateHarness=harness();
  stateHarness.context.setSendState('ready');assert.equal(stateHarness.submit.disabled,false);assert.equal(stateHarness.submit['aria-label'],'发送消息');
  stateHarness.context.setSendState('working');assert.equal(stateHarness.submit.dataset.state,'working');assert.equal(stateHarness.submit.disabled,true);assert.equal(stateHarness.submit.title,'正在处理');
  stateHarness.context.setSendState('waiting');assert.equal(stateHarness.submit.disabled,true);assert.equal(stateHarness.submit['aria-label'],'请先完成上方确认');
  stateHarness.context.setSendState('ready');assert.equal(stateHarness.submit.disabled,false);
  let h = harness();
  await h.run();
  const request = h.events.find(event => event.options);
  assert.equal(request.url, '/api/v1/step/drawings/target');
  assert.equal(request.options.method, 'DELETE');
  assert.equal(request.options.headers.Authorization, 'Bearer test-token');
  assert.equal(h.storage.has('step-chat-target'), false);
  assert.equal(h.storage.has('step-last-drawing-id'), false);
  assert.equal(h.storage.get('step-chat-other'), 'other history');
  assert.equal(h.context.activeSessionId, null);
  assert.equal(h.context.pollGeneration, 1);
  assert.equal(h.submit.disabled, false);
  assert.equal(h.file.value, '');
  assert.equal(h.message.value, '');
  assert.equal(h.button.disabled, false);
  assert(h.events.includes('empty-workspace'));

  h = harness({ confirmed: false });
  await h.run();
  assert.equal(h.events.length, 0);
  assert.equal(h.storage.get('step-chat-target'), 'target history');

  h = harness({ status: 409 });
  await h.run();
  assert.equal(h.storage.get('step-last-drawing-id'), 'target');
  assert.equal(h.context.activeSessionId, 'target');
  assert(!h.events.includes('empty-workspace'));
  assert(h.events.some(event => event.error && event.text === '会话正在处理'));
  assert.equal(h.button.disabled, false);

  h = harness({ active: 'other' });
  await h.run();
  assert.equal(h.context.activeSessionId, 'other');
  assert.equal(h.storage.get('step-last-drawing-id'), 'other');
  assert(!h.events.includes('empty-workspace'));

  h = harness({ filesDeleted: false });
  await h.run();
  assert(h.events.some(event => event.text === '会话已删除，部分文件暂未清理。'));

  h = harness();
  h.context.fetch = async () => { throw new Error('network unavailable'); };
  await h.run();
  assert.equal(h.storage.get('step-chat-target'), 'target history');
  assert.equal(h.button.disabled, false);
  assert(h.events.some(event => event.error && event.text === 'network unavailable'));

  // A slow status response cannot restore a just-deleted conversation.
  h = harness({ active: null });
  let resolveStatus;
  const deleteFetch = h.context.fetch;
  h.context.fetch = (url, options) => options.method === 'DELETE'
    ? deleteFetch(url, options)
    : new Promise(resolve => { resolveStatus = resolve; });
  const pendingPoll = h.context.poll('/api/v1/step/drawings/target');
  assert.equal(h.context.activeSessionId, 'target');
  await h.run();
  resolveStatus({ ok: true, json: async () => ({ drawing_id: 'target', status: 'completed' }) });
  await pendingPoll;
  assert.equal(h.context.activeSessionId, null);
  assert.equal(h.storage.has('step-last-drawing-id'), false);
  assert.equal(h.context.pollGeneration, 2);
  console.log('Session UI checks passed: confirmed deletion, cancellation, errors, inactive deletion, cleanup warning, network failure, stale polling.');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
