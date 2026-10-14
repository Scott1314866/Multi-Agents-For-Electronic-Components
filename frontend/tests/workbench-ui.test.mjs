// Chromium + mock API: exercise the actual unified UI without model/native calls.
import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {createServer} from 'node:http';
import {readFile,mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
const require=createRequire(import.meta.url);
let chromium;try{({chromium}=require('playwright'));}catch{}
const ID='5b3dd468-95b7-4eb6-a4a5-415732f64501';
const OTHER='fa8401bd-bf32-4f83-8d21-c85228c77255';
const root=new URL('../workbench/',import.meta.url),images=new URL('../../output/workbench/ui/',import.meta.url);
const spec=JSON.parse(await readFile(new URL('../../tests/golden/pcb/emi8143/live/package_spec.json',import.meta.url),'utf8'));
const preview=Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" viewBox="-195 -105 380 210"><rect x="-195" y="-105" width="380" height="210" fill="#151813"/>'+spec.pads.map(p=>`<rect x="${(p.x-p.size_x/2)*100}" y="${(-p.y-p.size_y/2)*100}" width="${p.size_x*100}" height="${p.size_y*100}" fill="#f5293a" stroke="#790e78" stroke-width="4"/>`).join('')+'</svg>');
const png=await readFile(new URL('../../tests/golden/pcb/emi8143/footprint.png',import.meta.url));
test('STEP upper and lower rows retain confirmed counts and zero span while submitting distinct profiles',{skip:!chromium,timeout:60000},async()=>{
 const f=await fixture({stepQuestion:true,sideProfiles:true});try{
  await f.login();await f.page.locator('#history button').filter({hasText:'STEP model'}).click();await f.page.getByLabel('上侧引脚数量（个）',{exact:true}).waitFor();
  assert.deepEqual(await f.page.locator('.dimension-group legend').allTextContents(),['上侧引脚','下侧引脚','本体与默认尺寸']);
  assert.equal(await f.page.getByLabel('上侧引脚数量（个）',{exact:true}).inputValue(),'1');assert.equal(await f.page.getByLabel('下侧引脚数量（个）',{exact:true}).inputValue(),'3');
  assert.equal(await f.page.getByLabel('上侧首末引脚中心距（mm）',{exact:true}).inputValue(),'0');
  for(const [label,value]of [['上侧引脚脚底长度（mm）','.8'],['下侧引脚脚底长度（mm）','1.16'],['上侧引脚金属厚度（mm）','.25'],['下侧引脚金属厚度（mm）','.35']])await f.page.getByLabel(label,{exact:true}).fill(value);
  await f.page.setViewportSize({width:390,height:844});assert.ok(await f.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await f.page.getByRole('button',{name:'确认参数并继续',exact:true}).click();await f.page.waitForFunction(()=>document.querySelector('#flash').textContent.includes('回答已保存'));
  const values=f.state.answers[0].answer.values;assert.equal(values.positive_side_pin_span.value,0);assert.equal(values.positive_side_terminal_thickness.value,.25);assert.equal(values.negative_side_terminal_thickness.value,.35);assert.equal(values.positive_side_terminal_length.value,.8);assert.equal(values.negative_side_terminal_length.value,1.16);assert.deepEqual(f.errors,[]);
 }finally{await f.close();}
});
test('STEP unresolved inch-column candidates never display a confirmed millimeter value',{skip:!chromium,timeout:60000},async()=>{
  const f=await fixture({stepQuestion:true,sourceUnconfirmed:true});try{
    await f.login();await f.page.locator('#history button').filter({hasText:'STEP model'}).click();
    await f.page.getByLabel('本体长度（mm）',{exact:true}).waitFor();
    assert.match(await f.page.locator('#question').textContent(),/识别候选（符号和单位未确认）：0\.2598/);
    assert.doesNotMatch(await f.page.locator('#question').textContent(),/0\.2598 mm/);
    assert.equal(await f.page.getByLabel('本体长度（mm）',{exact:true}).inputValue(),'');
    assert.equal(f.state.answers.length,0);assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});
async function fixture({pcbPage=false,processingKind=null,stepQuestion=false,stepPreview=false,sourceImage=false,partialNumbers=false,circlePads=false,followups=false,emptyPads=false,manualRows=false,oddGullwing=false,fivePins=false,asymmetricWidth=false,sourceUnconfirmed=false,sideProfiles=false}={}){
  const pageRoot=pcbPage?new URL('../pcb/',import.meta.url):root;
  const prefix=pcbPage?'/api/v1/pcb':'/api/v1/workbench';
  const state={routes:[],approved:false,answers:[],uploads:[],downloads:[],completed:false,stale:false,unauthorized:false,delay:0,processing:Boolean(processingKind),artifactDelays:{}};
  const rows=[{kind:'pcb',drawing_id:ID,title:'EMI8143',status:'awaiting_input'},{kind:'symbol',drawing_id:OTHER,title:'MCP3021',status:'completed'}];
  if(processingKind==='step'||stepQuestion||stepPreview)rows.push({kind:'step',drawing_id:ID,title:'STEP model',status:'ai_processing'});
  const stepQ={interrupt_id:'step-dimensions',stage:'dimensions',question:'尺寸之间存在冲突，请核对相关项目并重新填写。',options:['submit','cancel'],conflict_details:['总体高度应等于 housing_height + body_standoff。'],fields:Object.fromEntries(['pin_span','body_width','total_height','body_standoff','overall_width','housing_height','terminal_pitch','nominal_pin_count'].map((name,i)=>[name,{unit:name==='nominal_pin_count'?'count':'mm',observed_candidates:[{value:i+1,unit:name==='nominal_pin_count'?'count':'mm'}]}]))};
  if(oddGullwing){stepQ.fields=Object.fromEntries(['negative_side_pin_count','positive_side_pin_count','negative_side_pin_span','positive_side_pin_span'].map(name=>[name,{unit:name.endsWith('_count')?'count':'mm'}]));stepQ.conflict_details=[];}
  if(asymmetricWidth){stepQ.fields.negative_side_terminal_width={unit:'mm'};stepQ.fields.positive_side_terminal_width={unit:'mm'};stepQ.conflict_details=[];}
  if(sourceUnconfirmed){stepQ.fields={body_length:{unit:'mm',source_review_required:true,source_review_warning:'候选数值的尺寸符号或单位尚未确认，请按原图对应符号及毫米列重新核对。',observed_candidates:[{value:.2598,unit:'mm'}]}};stepQ.conflict_details=[];}
  if(sideProfiles){stepQ.side_groups=true;for(const [name,value]of Object.entries({positive_side_pin_count:1,negative_side_pin_count:3,positive_side_pin_span:0,negative_side_pin_span:4.6,positive_side_terminal_width:3.1,negative_side_terminal_width:.82}))stepQ.fields[name]={unit:name.endsWith('_count')?'count':'mm',observed_candidates:[{value,unit:name.endsWith('_count')?'count':'mm',evidence_kind:'human_input'}]};for(const side of ['positive','negative'])for(const type of ['length','thickness'])stepQ.fields[`${side}_side_terminal_${type}`]={unit:'mm',optional:true};}
  const questionSpec=structuredClone(spec);if(partialNumbers)questionSpec.pads.forEach((p,i)=>{if(i)p.number=null;});
  if(circlePads)questionSpec.pads.forEach(p=>{p.shape='CIRCLE';p.size_x=.25;p.size_y=.25;});
  const q={interrupt_id:'pcb-parameters',stage:'image_parameters',question:'请核对图纸尺寸与焊盘布局',options:['submit','cancel'],spec:questionSpec,grid:{bridged_columns:[2,5],standard_size_x:.25}};
  if(emptyPads){questionSpec.pads=[];questionSpec.pin_numbering=[];delete questionSpec.pin_count;q.grid=null;}
  if(manualRows)q.grid=null;
  if(fivePins){q.grid=null;Object.assign(questionSpec,{body_x:1.72,body_y:3.02,pin_count:5,pads:[['5',1.2,.95],['4',1.2,-.95],['1',-1.2,.95],['2',-1.2,0],['3',-1.2,-.95]].map(([number,x,y])=>({number,x,y,size_x:.6,size_y:.5,shape:'RECTANGLE'}))});q.pin_reference={pads:structuredClone(questionSpec.pads)};}
  if(followups){q.options=['clarify','submit','cancel'];q.questions=[{id:'body_height_max',title:'请补充本体最大高度（mm）',type:'number',help:'请按侧视图读取最大高度。'},{id:'manufacturing',title:'本次铜焊盘尺寸采用图纸值还是工艺调整值？',type:'manufacturing',help:'只修改匹配原宽度的焊盘。',widths:[.25,.55]}];q.human_history=[];}
  const server=createServer(async(req,res)=>{
    const url=new URL(req.url,'http://localhost');state.routes.push(url.pathname);const send=(value,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(value));};
    try{
      const chunks=[];for await(const chunk of req)chunks.push(chunk);const body=Buffer.concat(chunks).toString();
      if(url.pathname===prefix+'/ui'){res.writeHead(200,{'Content-Type':'text/html'});return res.end(await readFile(new URL('index.html',pageRoot)));}
      if([prefix+'/ui/assets/workspace.js',prefix+'/ui/assets/workspace.css'].includes(url.pathname)){const name=url.pathname.split('/').pop();res.writeHead(200,{'Content-Type':name.endsWith('.js')?'text/javascript':'text/css'});return res.end(await readFile(new URL('../shared/'+name,root)));}
      if(url.pathname.startsWith(prefix+'/ui/assets/')){const name=url.pathname.split('/').pop();if(!['styles.css','app.js','api.js'].includes(name))return send({},404);res.writeHead(200,{'Content-Type':name.endsWith('.js')?'text/javascript':'text/css'});return res.end(await readFile(new URL(name,pageRoot)));}
      if(url.pathname==='/api/v1/symbol/ui/assets/api.js'){res.writeHead(200,{'Content-Type':'text/javascript'});return res.end(await readFile(new URL('../symbol/api.js',root)));}
      if(url.pathname==='/api/v1/auth/login')return send({access_token:'test-token'});
      if(state.unauthorized||req.headers.authorization!=='Bearer test-token')return send({detail:'expired'},401);
      if(url.pathname==='/api/v1/auth/me')return send({tenant_id:'test-tenant',user_id:'test-user'});
      if(url.pathname===prefix+'/devices')return send({items:[{id:'local',label:'本机 Cadence'},{id:'ssh',label:'远程 Cadence',configured:true}],defaults:{symbol:'ssh',pcb:'ssh'},queues:[]});
      if(pcbPage&&url.pathname==='/api/v1/pcb/packages')return send({items:[{drawing_id:ID,spec_name:'EMI8143',status:'awaiting_input'}],has_more:false});
      if(pcbPage&&url.pathname==='/api/v1/pcb/generate-image'){state.uploads.push(body);return send({drawing_id:ID,status:'ai_processing'},202);}
      if(pcbPage&&url.pathname===`/api/v1/pcb/packages/${ID}`){if(state.unavailable)return send({detail:'暂时无法连接服务'},503);return send({drawing_id:ID,spec_name:'EMI8143',status:state.processing?'ai_processing':state.approved?'completed':state.completed?'pending_review':'awaiting_input',pending_input:state.processing||state.approved?null:state.completed?{interrupt_id:'pcb-review',stage:'review',question:'确认采纳封装？',can_approve:true,options:['approve','reject']}:q,execution:{mode:'ssh'},artifact_urls:{preview_png:'/preview',psm:'/psm'},spec,summary:'16 个独立焊盘',verdict:{passed:true,assertions:[{name:'焊盘尺寸与位置',passed:true}]},human_history:[],warnings:[]});}
      if(url.pathname==='/api/v1/workbench/jobs'&&req.method==='GET')return send({items:rows.filter(r=>!url.searchParams.get('kind')||url.searchParams.get('kind')===r.kind),has_more:false});
      if(url.pathname==='/api/v1/workbench/jobs'&&req.method==='POST'){state.uploads.push(body);return send({kind:'pcb',drawing_id:ID,status:'ai_processing'},202);}
      if(url.pathname.endsWith('/human-input')){if(state.stale){state.stale=false;return send({detail:'当前问题已改变'},409);}const value=JSON.parse(body);assert.equal(value.interrupt_id,stepQuestion?'step-dimensions':state.completed&&pcbPage?'pcb-review':q.interrupt_id);state.answers.push(value);if(value.answer.action==='clarify'){q.human_history.push({stage:'image_clarification',values:value.answer.values,questions:q.questions.filter(item=>item.id in value.answer.values)});q.questions=q.questions.filter(item=>!(item.id in value.answer.values));q.interrupt_id+='-next';if(value.answer.values.body_height_max)q.spec.body_height_max=value.answer.values.body_height_max;return send({status:'ai_processing'},202);}if(value.answer.action==='approve')state.approved=true;state.completed=true;return send({status:'ai_processing'},202);}
      if(url.pathname.endsWith('/sources')){state.uploads.push(body);q.interrupt_id+='-source';q.questions=[];return send({status:'ai_processing'},202);}
      if(url.pathname.endsWith('/attachments'))return send({items:sourceImage?[{name:'source',label:'上传原图',filename:'封装尺寸图.png',media_type:'image/png',size:png.length}]:[]});
      if(url.pathname.endsWith('/attachments/source')){state.downloads.push(req.headers.authorization);res.writeHead(200,{'Content-Type':'image/png'});return res.end(png);}
      if(url.pathname.includes('/artifacts/')){state.downloads.push(req.headers.authorization);const artifact=url.pathname.split('/').pop();if(state.artifactDelays[artifact])await new Promise(r=>setTimeout(r,state.artifactDelays[artifact]));if(artifact==='preview'||artifact.startsWith('preview_')){res.writeHead(200,{'Content-Type':'image/svg+xml'});return res.end(preview);}res.writeHead(200,{'Content-Type':'application/octet-stream','Content-Disposition':'attachment; filename="'+(stepPreview?'model.step':'emi8143.psm')+'"'});return res.end('test artifact');}
      if(url.pathname.startsWith('/api/v1/workbench/jobs/')){if(state.unavailable)return send({detail:'暂时无法连接服务'},503);if(state.delay)await new Promise(r=>setTimeout(r,state.delay));const kind=url.pathname.split('/')[5];return send({kind,drawing_id:kind==='symbol'?OTHER:ID,title:kind==='symbol'?'<img src=x onerror=alert(1)>':'EMI8143',error_msg:state.failure||null,status:state.failure?'failed':state.processing?'ai_processing':kind==='symbol'?'completed':state.completed?'pending_review':'awaiting_input',pending_input:stepQuestion&&kind==='step'&&!state.completed?stepQ:!state.processing&&kind==='pcb'&&!state.completed?q:null,execution:{mode:'ssh'},artifacts:stepPreview&&kind==='step'?{step:'/step',preview:'/preview',preview_isometric:'/iso',preview_front:'/front',preview_top:'/top',preview_right:'/right'}:kind==='pcb'?{preview_png:'/preview',psm:'/psm'}:{olb:'/olb'},detail:{spec,summary:stepQuestion?'':'16 个独立焊盘，尺寸来源为图纸标注。',result:stepQuestion?{graph_status:'stopped_insufficient_extraction'}:null,verdict:state.completed?{assertions:[{name:'16个焊盘',passed:true}],passed:true}:null,human_history:[],warnings:[]}});}
      return send({detail:'missing'},404);
    }catch(error){send({detail:error.message},500);}
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1080},acceptDownloads:true});const errors=[];page.on('pageerror',error=>errors.push(error.message));
  const url=`http://127.0.0.1:${server.address().port}${prefix}/ui`;
  const login=async()=>{await page.goto(url);await page.locator('#login-form [name=username]').fill('tester');await page.locator('#login-form [name=password]').fill('secret');await page.locator('#login-form button').click();await page.locator('#workspace').waitFor({state:'visible'});await page.locator('#history button').first().waitFor();};
  return {page,state,errors,url,login,close:async()=>{await browser.close();await new Promise(r=>server.close(r));}};
}
test('workbench: unified PCB submit, parameter review, authenticated files and mobile layout',{skip:!chromium,timeout:60000},async()=>{
  const f=await fixture();try{
    await f.login();await mkdir(images,{recursive:true});await f.page.screenshot({path:fileURLToPath(new URL('desktop.png',images)),fullPage:true,animations:'disabled'});
    await f.page.locator('[data-task=pcb]').click();await f.page.locator('#execution-mode').selectOption('ssh');
    await f.page.locator('#file').setInputFiles({name:'footprint.png',mimeType:'image/png',buffer:png});
    await f.page.locator('#submit-task').click();await f.page.locator('.pad-table').waitFor();
    assert.match(f.state.uploads[0],/name="kind"\r\n\r\npcb/);assert.match(f.state.uploads[0],/name="execution_mode"\r\n\r\nssh/);
    assert.equal(await f.page.locator('.pad-table tbody tr').count(),16);
    await f.page.getByLabel('最大高度（mm）').fill('0.55');await f.page.getByLabel('连通地焊盘的生成方式').selectOption('independent');
    const refreshed=f.page.waitForResponse(r=>r.url().endsWith(ID));await f.page.locator('#refresh-job').click();await refreshed;
    assert.equal(await f.page.getByLabel('最大高度（mm）').inputValue(),'0.55');
    await f.page.screenshot({path:fileURLToPath(new URL('pcb-review-desktop.png',images)),fullPage:true,animations:'disabled'});
    await f.page.setViewportSize({width:390,height:844});await f.page.screenshot({path:fileURLToPath(new URL('pcb-review-mobile.png',images)),fullPage:true,animations:'disabled'});
    assert.ok(await f.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'mobile horizontal overflow');
    await f.page.getByRole('button',{name:'确认参数并继续',exact:true}).click();await f.page.locator('#job-status').getByText('等待复核').waitFor();
    assert.equal(await f.page.locator('#question button').count(),0);
    assert.equal(f.state.answers[0].answer.spec.body_height_max,.55);assert.equal(f.state.answers[0].answer.pad_layout_policy,'independent');
    const download=f.page.waitForEvent('download');await f.page.getByRole('button',{name:'PSM ↧',exact:true}).click();assert.equal((await download).suggestedFilename(),'emi8143.psm');assert.ok(f.state.downloads.every(value=>value==='Bearer test-token'));
    await f.page.reload();await f.page.locator('#job-status').getByText('等待复核').waitFor();assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});
test('workbench: type filters, stale confirmation, safe labels and session expiry',{skip:!chromium,timeout:60000},async()=>{
  const f=await fixture();try{
    await f.login();await f.page.locator('#filters [data-kind=symbol]').click();await f.page.waitForFunction(()=>document.querySelectorAll('#history button').length===1);await f.page.locator('#history button').getByText('MCP3021').waitFor();assert.equal(await f.page.locator('#history button').count(),1);
    await f.page.locator('#history button').click();await f.page.locator('#job-title').getByText('<img src=x onerror=alert(1)>',{exact:true}).waitFor();assert.equal(await f.page.locator('#job-title img').count(),0);
    await f.page.locator('#filters [data-kind=pcb]').click();await f.page.locator('#history button').getByText('EMI8143').waitFor();await f.page.locator('#history button').click();await f.page.locator('.pad-table').waitFor();
    await f.page.getByLabel('连通地焊盘的生成方式').selectOption('independent');
    f.state.stale=true;await f.page.getByRole('button',{name:'确认参数并继续',exact:true}).click();await f.page.getByText('当前问题已改变',{exact:true}).waitFor();assert.equal(f.state.answers.length,0);
    f.state.delay=200;const late=f.page.waitForResponse(r=>r.url().endsWith(ID));await f.page.locator('#refresh-job').click();await f.page.locator('#new-task').click();await late;assert.ok(await f.page.locator('#create-view').isVisible());
    f.state.delay=0;await f.page.locator('#history button').click();await f.page.locator('.pad-table').waitFor();f.state.unauthorized=true;await f.page.locator('#refresh-job').click();await f.page.locator('#login').waitFor({state:'visible'});assert.equal(await f.page.evaluate(()=>sessionStorage.getItem('step-access-token')),null);assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('PCB manufacturing widths and unknown pin IDs stay editable before submission',{skip:!chromium,timeout:60000},async()=>{
  const f=await fixture({pcbPage:true,partialNumbers:true});try{
    await f.login();await f.page.locator('#history button').filter({hasText:'EMI8143'}).click();await f.page.locator('.pad-table').waitFor();
    await f.page.getByLabel('批量调整：原焊盘宽度（mm）',{exact:true}).fill('0.25');
    await f.page.getByLabel('工艺要求：生成宽度（mm）',{exact:true}).fill('0.20');
    await f.page.getByRole('button',{name:'批量修改相同宽度焊盘',exact:true}).click();
    assert.match(await f.page.locator('#question [role=status]').textContent(),/15 个焊盘/);
    assert.equal(await f.page.getByLabel('第 1 行焊盘宽度',{exact:true}).inputValue(),'0.55');
    for(let i=2;i<=16;i++)assert.equal(await f.page.getByLabel(`第 ${i} 行焊盘宽度`,{exact:true}).inputValue(),'0.2');
    await f.page.getByRole('button',{name:'确认参数并继续',exact:true}).click();
    assert.match(await f.page.locator('#flash').textContent(),/请填写第.*行焊盘的脚号/);
    assert.equal(f.state.answers.length,0);
    for(let i=0;i<16;i++)await f.page.getByLabel(`第 ${i+1} 行焊盘脚号`,{exact:true}).fill(spec.pads[i].number);
    await f.page.getByLabel('连通地焊盘的生成方式').selectOption('independent');
    await f.page.getByRole('button',{name:'确认参数并继续',exact:true}).click();
    await f.page.waitForFunction(()=>document.querySelector('#flash').textContent.includes('回答已保存'));
    assert.equal(f.state.answers.length,1);
    assert.equal(f.state.answers[0].answer.spec.pads[0].size_x,.55);
    assert.ok(f.state.answers[0].answer.spec.pads.slice(1).every(p=>p.size_x===.2));
    assert.deepEqual(f.state.answers[0].answer.spec.pin_numbering,spec.pads.map(p=>p.number));
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('PCB round pads retain shape and keep both diameter axes synchronized',{skip:!chromium,timeout:60000},async()=>{
  const f=await fixture({pcbPage:true,circlePads:true});try{
    await f.login();await f.page.locator('#history button').filter({hasText:'EMI8143'}).click();await f.page.locator('.pad-table').waitFor();
    assert.equal(await f.page.getByLabel('第 1 行焊盘形状',{exact:true}).inputValue(),'CIRCLE');
    assert.equal(await f.page.getByLabel('第 1 行焊盘长度',{exact:true}).isDisabled(),true);
    await f.page.getByLabel('第 1 行焊盘宽度',{exact:true}).fill('0.23');
    assert.equal(await f.page.getByLabel('第 1 行焊盘长度',{exact:true}).inputValue(),'0.23');
    await f.page.getByLabel('工艺要求：生成宽度（mm）',{exact:true}).fill('0.20');
    await f.page.getByRole('button',{name:'批量修改相同宽度焊盘',exact:true}).click();
    assert.equal(await f.page.getByLabel('第 2 行焊盘长度',{exact:true}).inputValue(),'0.2');
    await f.page.getByLabel('连通地焊盘的生成方式').selectOption('independent');
    await f.page.getByRole('button',{name:'确认参数并继续',exact:true}).click();await f.page.getByRole('button',{name:'采纳结果',exact:true}).waitFor();
    assert.equal(f.state.answers[0].answer.spec.pads[0].shape,'CIRCLE');
    assert.equal(f.state.answers[0].answer.spec.pads[0].size_y,.23);
    assert.equal(f.state.answers[0].answer.spec.pads[1].size_x,.2);
    assert.equal(f.state.answers[0].answer.spec.pads[1].size_y,.2);
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('PCB page: dedicated frontend uses PCB API, supplementary images, review and downloads',{skip:!chromium,timeout:60000},async()=>{
  const f=await fixture({pcbPage:true});try{
    await f.login();assert.equal(await f.page.locator('#page-title').textContent(),'PCB 焊盘封装');
    assert.ok(await f.page.locator('#device-field').isVisible());assert.ok(await f.page.locator('#contexts').isVisible());
    await f.page.locator('#execution-mode').selectOption('ssh');
    await f.page.locator('#file').setInputFiles({name:'footprint.png',mimeType:'image/png',buffer:png});
    await f.page.locator('#context-images').setInputFiles({name:'dimensions.png',mimeType:'image/png',buffer:png});
    await f.page.locator('#submit-task').click();await f.page.locator('.pad-table').waitFor();
    assert.equal(await f.page.locator('.pad-table tbody tr').count(),16);
    assert.ok(f.state.routes.includes('/api/v1/pcb/generate-image'));
    assert.match(f.state.uploads[0],/name="context_images"/);assert.doesNotMatch(f.state.uploads[0],/name="kind"/);
    await f.page.getByLabel('连通地焊盘的生成方式').selectOption('independent');
    await f.page.getByRole('button',{name:'确认参数并继续',exact:true}).click();await f.page.getByRole('button',{name:'采纳结果',exact:true}).waitFor();
    await f.page.getByRole('button',{name:'采纳结果',exact:true}).click();await f.page.locator('#job-status').getByText('已完成').waitFor();
    assert.deepEqual(f.state.answers.map(a=>a.interrupt_id),['pcb-parameters','pcb-review']);
    const downloaded=f.page.waitForEvent('download');await f.page.getByRole('button',{name:'PSM ↧',exact:true}).click();assert.equal((await downloaded).suggestedFilename(),'emi8143.psm');
    assert.ok(!f.state.routes.some(path=>path.startsWith('/api/v1/workbench/jobs')||path==='/api/v1/workbench/devices'));
    await mkdir(images,{recursive:true});await f.page.screenshot({path:fileURLToPath(new URL('pcb-page-desktop.png',images)),fullPage:true,animations:'disabled'});
    await f.page.setViewportSize({width:390,height:844});assert.ok(await f.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await f.page.reload();await f.page.locator('#job-status').getByText('已完成').waitFor();assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('PCB upload: PDF main is rejected and auxiliary PDF is submitted as source material',{skip:!chromium,timeout:60000},async()=>{
  const f=await fixture();try{
    await f.login();await f.page.locator('[data-task=pcb]').click();
    await f.page.locator('#file').setInputFiles({name:'datasheet.PDF',mimeType:'application/pdf',buffer:Buffer.from('%PDF-1.7')});
    await f.page.locator('#submit-task').click();await f.page.getByText('PCB 主图不能上传 PDF，请将对应的尺寸图截图为 PNG / JPG 后上传。',{exact:true}).waitFor();
    assert.equal(f.state.uploads.length,0);
    await f.page.locator('#file').setInputFiles({name:'footprint.png',mimeType:'image/png',buffer:png});
    await f.page.locator('#context-images').setInputFiles({name:'dimensions.pdf',mimeType:'application/pdf',buffer:Buffer.from('%PDF-1.7')});
    await f.page.locator('#submit-task').click();await f.page.locator('.pad-table').waitFor();
    assert.equal(f.state.uploads.length,1);assert.match(f.state.uploads[0],/filename="dimensions.pdf"/);assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});


test('PCB followups: partial answers, next question, audit history and supplement upload',{skip:!chromium,timeout:60000},async()=>{
  const f=await fixture({pcbPage:true,followups:true});try{
    await f.login();await f.page.locator('#history button').filter({hasText:'EMI8143'}).click();await f.page.getByRole('region',{name:'PCB 人工追问'}).waitFor();
    await f.page.getByLabel('请补充本体最大高度（mm）',{exact:true}).fill('0.64');
    await f.page.getByRole('button',{name:'保存回答并继续追问',exact:true}).click();
    await f.page.getByText('已回答 1 轮 · 查看追问记录',{exact:true}).waitFor();
    assert.equal(f.state.answers[0].answer.values.body_height_max,.64);
    assert.equal(await f.page.getByLabel('请补充本体最大高度（mm）',{exact:true}).count(),0);
    await f.page.getByLabel('本次铜焊盘尺寸采用图纸值还是工艺调整值？',{exact:true}).selectOption('process');
    await f.page.getByLabel('本次工艺宽度／直径（mm）',{exact:true}).fill('0.20');
    await f.page.setViewportSize({width:390,height:844});assert.ok(await f.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await mkdir(images,{recursive:true});await f.page.screenshot({path:fileURLToPath(new URL('pcb-followup-mobile.png',images)),fullPage:true,animations:'disabled'});
    await f.page.getByRole('button',{name:'保存回答并继续追问',exact:true}).click();await f.page.getByText('已回答 2 轮 · 查看追问记录',{exact:true}).waitFor();
    assert.deepEqual(f.state.answers[1].answer.values.manufacturing,{mode:'process',from:.25,to:.2});
    await f.page.getByLabel('选择补充图片或 PDF',{exact:true}).setInputFiles({name:'datasheet.pdf',mimeType:'application/pdf',buffer:Buffer.from('%PDF-1.7')});
    const refreshed=f.page.waitForResponse(r=>r.url().endsWith(ID));await f.page.getByRole('button',{name:'上传补充资料并重新识别',exact:true}).click();await refreshed;
    assert.match(f.state.uploads[0],/name="interrupt_id"/);assert.match(f.state.uploads[0],/filename="datasheet.pdf"/);
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('working indicators: all task kinds and PCB page stop spinning at human review or completion',{skip:!chromium,timeout:60000},async()=>{
  for(const [kind,pcbPage] of [['step',false],['symbol',false],['pcb',false],['pcb',true]]){
    const f=await fixture({pcbPage,processingKind:kind});try{
      await f.login();if(!pcbPage)await f.page.locator(`#filters [data-kind=${kind}]`).click();
      await f.page.locator('#history button').first().click();
      await f.page.locator('#job-status').getByText('工作中',{exact:true}).waitFor();
      assert.equal(await f.page.locator('#job-status .working-spinner').count(),1);
      await f.page.locator('#findings [role=status]').waitFor();
      assert.equal(await f.page.locator('#findings .working-spinner').count(),1);
      assert.equal(await f.page.locator('#findings .working-spinner').evaluate(n=>getComputedStyle(n).animationName),'working-spin');
      if(kind==='pcb'&&!pcbPage){await mkdir(images,{recursive:true});await f.page.screenshot({path:fileURLToPath(new URL('pcb-working.png',images)),fullPage:true});}
      f.state.processing=false;await f.page.locator('#refresh-job').click();
      await f.page.waitForFunction(()=>!document.querySelector('#job-view .working-spinner'));
      assert.equal(await f.page.locator('#findings [role=status]').count(),0);
      assert.deepEqual(f.errors,[]);
    }finally{await f.close();}
  }
});


test('STEP dimension review: Chinese labels, measurement help and original API field names',{skip:!chromium,timeout:60000},async()=>{
  const f=await fixture({stepQuestion:true});try{
    await f.login();await f.page.locator('#filters [data-kind=step]').click();await f.page.locator('#history button').filter({hasText:'STEP model'}).click();
    await f.page.getByLabel('同排首末引脚中心距（mm）',{exact:true}).waitFor();
    await f.page.getByText('量电路板安装面到本体底面的垂直距离。不要填引脚金属厚度。',{exact:true}).waitFor();
    assert.match(await f.page.locator('#summary').textContent(),/尺寸信息不足或存在冲突/);
    assert.doesNotMatch(await f.page.locator('#question .question-form').innerText(),/pin_span|total_height|body_standoff|housing_height/);
    assert.match(await f.page.locator('.dimension-conflict').textContent(),/本体高度.*本体离板高度/);
    const input=f.page.getByLabel('器件总高度（mm）',{exact:true});assert.ok(await input.getAttribute('aria-describedby'));
    await input.fill('2');await f.page.getByLabel('同排首末引脚中心距（mm）',{exact:true}).fill('1.3');
    await f.page.getByLabel('本体离板高度（mm）',{exact:true}).fill('0.12');await f.page.getByLabel('引脚总数（个）',{exact:true}).fill('16');
    await mkdir(images,{recursive:true});await f.page.screenshot({path:fileURLToPath(new URL('step-dimensions-chinese.png',images)),fullPage:true});
    await f.page.setViewportSize({width:390,height:844});assert.ok(await f.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await f.page.getByRole('button',{name:'确认参数并继续',exact:true}).click();await f.page.locator('#job-status').getByText('等待复核').waitFor();
    assert.deepEqual(f.state.answers[0].answer.values,{pin_span:{value:1.3,unit:'mm'},total_height:{value:2,unit:'mm'},body_standoff:{value:.12,unit:'mm'},nominal_pin_count:{value:16,unit:'count'}});
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});


test('STEP preview: original four view controls, authenticated images, safe switching and STEP download',{skip:!chromium,timeout:60000},async()=>{
  const f=await fixture({stepPreview:true});try{
    await f.login();await f.page.locator('#filters [data-kind=step]').click();await f.page.locator('#history button').filter({hasText:'STEP model'}).click();
    await f.page.locator('#preview img[alt="等轴测图"]').waitFor();
    assert.equal(await f.page.locator('.step-preview-actions button').count(),4);
    assert.equal(await f.page.locator('#artifacts button').count(),1);
    assert.match(await f.page.locator('#artifacts').innerText(),/下载 STEP 文件/);
    assert.doesNotMatch(await f.page.locator('#artifacts').innerText(),/PREVIEW/);
    for(const label of ['正视图','俯视图','右视图']){
      await f.page.getByRole('button',{name:'查看'+label,exact:true}).click();await f.page.locator(`#preview img[alt="${label}"]`).waitFor();
    }
    await f.page.locator('#refresh-job').click();assert.equal(await f.page.locator('#preview img').getAttribute('alt'),'右视图');
    f.state.artifactDelays.preview_front=200;
    const delayed=f.page.waitForResponse(r=>r.url().endsWith('/artifacts/preview_front'));
    await f.page.getByRole('button',{name:'查看正视图',exact:true}).click();await f.page.getByRole('button',{name:'查看俯视图',exact:true}).click();
    await f.page.locator('#preview img[alt="俯视图"]').waitFor();await delayed;assert.equal(await f.page.locator('#preview img').getAttribute('alt'),'俯视图');
    const download=f.page.waitForEvent('download');await f.page.getByRole('button',{name:'下载 STEP 文件 ↧',exact:true}).click();assert.equal((await download).suggestedFilename(),'model.step');
    assert.ok(f.state.downloads.every(value=>value==='Bearer test-token'));
    await mkdir(images,{recursive:true});await f.page.screenshot({path:fileURLToPath(new URL('step-four-views.png',images)),fullPage:true});
    await f.page.setViewportSize({width:390,height:844});assert.ok(await f.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});


test('lost task connection: stop working spinner, show last known status and recover',{skip:!chromium,timeout:60000},async()=>{
  for(const pcbPage of [false,true]){
    const f=await fixture({pcbPage,processingKind:'pcb'});try{
      await f.login();await f.page.locator('#history button').first().click();await f.page.locator('#job-status').getByText('工作中',{exact:true}).waitFor();
      f.state.unavailable=true;await f.page.locator('#refresh-job').click();
      await f.page.locator('#job-status').getByText('连接中断 · 正在重连',{exact:true}).waitFor();
      assert.equal(await f.page.locator('#job-view .working-spinner').count(),0);
      assert.match(await f.page.locator('.connection-message').innerText(),/上次状态：处理中/);
      assert.equal(await f.page.locator('.connection-message').count(),1);
      f.state.unavailable=false;await f.page.locator('#refresh-job').click();
      await f.page.locator('#job-status').getByText('工作中',{exact:true}).waitFor();
      assert.equal(await f.page.locator('.connection-message').count(),0);
      assert.equal(await f.page.locator('#findings .working-spinner').count(),1);
      assert.ok(await f.page.locator('#flash').isHidden());assert.deepEqual(f.errors,[]);
    }finally{await f.close();}
  }
});


test('model authentication failure: Chinese explanation replaces working indicators',{skip:!chromium,timeout:60000},async()=>{
  const f=await fixture({processingKind:'step'});try{
    await f.login();await f.page.locator('#history button').filter({hasText:'STEP model'}).click();
    await f.page.locator('#job-status').getByText('工作中',{exact:true}).waitFor();
    f.state.failure="Error code: 401 - {'error': {'code': 'invalid_api_key'}}";
    await f.page.locator('#refresh-job').click();
    await f.page.locator('#job-error').getByText('模型服务认证失败（401）',{exact:false}).waitFor();
    assert.match(await f.page.locator('#job-error').innerText(),/修复配置后重试此任务/);
    assert.equal(await f.page.locator('#job-view .working-spinner').count(),0);
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('original image: visible before generation, authenticated zoom and download, restored on reload',{skip:!chromium,timeout:60000},async()=>{
  const f=await fixture({stepQuestion:true,sourceImage:true});try{
    await f.login();await f.page.locator('#history button').filter({hasText:'STEP model'}).click();
    const image=f.page.locator('#source-panel img');await image.waitFor();
    await f.page.waitForFunction(()=>document.querySelector('#source-panel img')?.naturalWidth>0);
    assert.equal(await f.page.locator('#preview img').count(),0,'original must not require generated model previews');
    await f.page.getByRole('button',{name:'放大查看',exact:true}).click();
    assert.ok(await f.page.locator('dialog[open] img').isVisible());await f.page.getByRole('button',{name:'关闭',exact:true}).click();
    const downloaded=f.page.waitForEvent('download');await f.page.getByRole('button',{name:'下载附件',exact:true}).click();
    assert.equal((await downloaded).suggestedFilename(),'封装尺寸图.png');
    assert.ok(f.state.downloads.every(value=>value==='Bearer test-token'));
    await mkdir(images,{recursive:true});await f.page.screenshot({path:fileURLToPath(new URL('step-source-attachment.png',images)),fullPage:true});
    await f.page.setViewportSize({width:390,height:844});assert.ok(await f.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),JSON.stringify(await f.page.evaluate(()=>[...document.querySelectorAll('body *')].filter(n=>n.getBoundingClientRect().right>innerWidth+1).filter(n=>n.closest('main')).map(n=>[n.tagName,n.id,n.className,n.getBoundingClientRect().width,n.getBoundingClientRect().right]).slice(0,25))));
    await f.page.reload();await f.page.locator('#source-panel img').waitFor();
    await f.page.waitForFunction(()=>document.querySelector('#source-panel img')?.naturalWidth>0);
    await f.page.locator('#new-task').click();assert.equal(await f.page.locator('#source-panel').isVisible(),false);
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('PCB empty pad table: add, validate, delete, preserve edits and submit on both entry pages',{skip:!chromium,timeout:60000},async()=>{
  for(const pcbPage of [true,false]){
    const f=await fixture({pcbPage,emptyPads:true});try{
      await f.login();await f.page.locator('#history button').first().click();await f.page.locator('.pad-table').waitFor();
      await f.page.getByText('尚无焊盘。',{exact:false}).waitFor();
      await f.page.getByRole('button',{name:'确认参数并继续',exact:true}).click();
      assert.equal(f.state.answers.length,0);await f.page.locator('#flash').getByText('请先新增焊盘，',{exact:false}).waitFor();
      await f.page.getByRole('button',{name:'＋ 新增焊盘',exact:true}).click();
      assert.equal(await f.page.getByLabel('焊盘数量',{exact:true}).inputValue(),'1');
      assert.equal(await f.page.getByLabel('第 1 行焊盘脚号',{exact:true}).inputValue(),'');
      assert.equal(await f.page.getByLabel('第 1 行焊盘X 坐标',{exact:true}).inputValue(),'');
      await f.page.getByRole('button',{name:'确认参数并继续',exact:true}).click();assert.equal(f.state.answers.length,0);
      await f.page.getByLabel('第 1 行焊盘脚号',{exact:true}).fill('1');
      await f.page.getByRole('button',{name:'确认参数并继续',exact:true}).click();assert.equal(f.state.answers.length,0);
      for(const [label,value]of [['X 坐标','0'],['Y 坐标','0'],['宽度','.82'],['长度','1.16']])await f.page.getByLabel('第 1 行焊盘'+label,{exact:true}).fill(value);
      await f.page.getByRole('button',{name:'＋ 新增焊盘',exact:true}).click();
      await f.page.getByRole('button',{name:'删除第 2 行焊盘',exact:true}).click();
      assert.equal(await f.page.getByLabel('第 1 行焊盘宽度',{exact:true}).inputValue(),'.82');
      const refreshed=f.page.waitForResponse(r=>r.url().endsWith(ID));await f.page.locator('#refresh-job').click();await refreshed;
      assert.equal(await f.page.locator('.pad-table tbody input').count(),5);
      await mkdir(images,{recursive:true});if(pcbPage)await f.page.locator('#question').screenshot({path:fileURLToPath(new URL('pcb-add-pad.png',images))});
      await f.page.setViewportSize({width:390,height:844});assert.ok(await f.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await f.page.getByRole('button',{name:'确认参数并继续',exact:true}).click();
      await f.page.locator('#job-status').getByText('等待复核').waitFor();
      const patch=f.state.answers[0].answer.spec;assert.equal(patch.pin_count,1);assert.equal(patch.pin_layout,'EXPLICIT');assert.equal(patch.pin1_marker,'NONE');assert.deepEqual(patch.pin_numbering,['1']);assert.deepEqual(patch.pads,[{number:'1',shape:'RECTANGLE',x:0,y:0,size_x:.82,size_y:1.16}]);
      assert.deepEqual(f.errors,[]);
    }finally{await f.close();}
  }
});

test('PCB remove existing row: keep remaining values, renumber labels and circle diameter edits',{skip:!chromium,timeout:60000},async()=>{
  const f=await fixture({pcbPage:true,circlePads:true,manualRows:true});try{
    await f.login();await f.page.locator('#history button').first().click();await f.page.locator('.pad-table').waitFor();
    await f.page.getByRole('button',{name:'删除第 2 行焊盘',exact:true}).click();
    assert.equal(await f.page.getByLabel('焊盘数量',{exact:true}).inputValue(),'15');
    assert.equal(await f.page.getByLabel('第 2 行焊盘脚号',{exact:true}).inputValue(),spec.pads[2].number);
    await f.page.getByLabel('第 2 行焊盘宽度',{exact:true}).fill('.4');
    assert.equal(await f.page.getByLabel('第 2 行焊盘长度',{exact:true}).inputValue(),'.4');
    assert.ok(await f.page.getByLabel('第 2 行焊盘长度',{exact:true}).isDisabled());
    await f.page.getByLabel('批量调整：原焊盘宽度（mm）').fill('.25');await f.page.getByLabel('工艺要求：生成宽度（mm）').fill('.3');await f.page.getByRole('button',{name:'批量修改相同宽度焊盘',exact:true}).click();
    await f.page.getByRole('button',{name:'确认参数并继续',exact:true}).click();await f.page.locator('#job-status').getByText('等待复核').waitFor();
    const patch=f.state.answers[0].answer.spec;assert.equal(patch.pin_count,15);assert.equal(patch.pads[1].size_y,.4);assert.equal(patch.pads[0].size_y,.3);assert.ok(!patch.pin_numbering.includes(spec.pads[1].number));assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('odd gullwing STEP: independent row counts and spans use Chinese labels and correct units',{skip:!chromium,timeout:60000},async()=>{
  const f=await fixture({stepQuestion:true,oddGullwing:true});try{
    await f.login();await f.page.locator('#filters [data-kind=step]').click();await f.page.locator('#history button').filter({hasText:'STEP model'}).click();
    for(const [label,value]of [['下侧引脚数量（个）','3'],['上侧引脚数量（个）','2'],['下侧首末引脚中心距（mm）','1.9'],['上侧首末引脚中心距（mm）','1.9']])await f.page.getByLabel(label,{exact:true}).fill(value);
    assert.doesNotMatch(await f.page.locator('#question .question-form').innerText(),/negative_side_pin_count|positive_side_pin_span/);
    await f.page.setViewportSize({width:390,height:844});assert.ok(await f.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await f.page.getByRole('button',{name:'确认参数并继续',exact:true}).click();await f.page.locator('#job-status').getByText('等待复核').waitFor();
    assert.deepEqual(f.state.answers[0].answer.values,{negative_side_pin_count:{value:3,unit:'count'},positive_side_pin_count:{value:2,unit:'count'},negative_side_pin_span:{value:1.9,unit:'mm'},positive_side_pin_span:{value:1.9,unit:'mm'}});
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('PCB five pins: source position labels and live preview expose incorrect row numbering',{skip:!chromium,timeout:60000},async()=>{
  for(const pcbPage of [false,true]){
    const f=await fixture({pcbPage,fivePins:true});try{
      await f.login();await f.page.locator('#history button').first().click();
      const svg=f.page.getByRole('img',{name:'焊盘顶视即时预览'});await svg.waitFor();
      const initial=await svg.locator('text').allTextContents();assert.deepEqual(initial,['5','4','1','2','3']);
      assert.match(await f.page.getByLabel('焊盘位置核对').textContent(),/5 脚 \(1.2, 0.95\)/);
      await f.page.getByLabel('第 1 行焊盘脚号',{exact:true}).fill('99');
      assert.equal(await svg.locator('text').first().textContent(),'99');
      await f.page.getByLabel('第 1 行焊盘脚号',{exact:true}).fill('5');
      await f.page.getByRole('button',{name:'删除第 2 行焊盘'}).click();assert.equal(await svg.locator('text').count(),4);
      assert.deepEqual(f.errors,[]);
    }finally{await f.close();}
  }
});

test('SOT223 STEP: independent side widths have separate Chinese inputs and API values',{skip:!chromium,timeout:60000},async()=>{
 const f=await fixture({stepQuestion:true,asymmetricWidth:true});try{
  await f.login();await f.page.locator('#filters [data-kind=step]').click();await f.page.locator('#history button').filter({hasText:'STEP model'}).click();
  await f.page.getByLabel('下侧单只引脚宽度（mm）',{exact:true}).fill('0.82');
  await f.page.getByLabel('上侧单只引脚宽度（mm）',{exact:true}).fill('3.1');
  assert.match(await f.page.locator('#question').innerText(),/D1/);
  await f.page.getByRole('button',{name:'确认参数并继续',exact:true}).click();await f.page.locator('#job-status').getByText('等待复核').waitFor();
  assert.deepEqual(f.state.answers[0].answer.values.negative_side_terminal_width,{value:.82,unit:'mm'});
  assert.deepEqual(f.state.answers[0].answer.values.positive_side_terminal_width,{value:3.1,unit:'mm'});
  assert.deepEqual(f.errors,[]);
 }finally{await f.close();}
});
