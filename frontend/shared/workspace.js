import {WorkspaceAPI, STATUS_LABELS, PAGE_KIND} from './api.js';

const $ = selector => document.querySelector(selector);
const el = (tag, className='', text='') => {const n=document.createElement(tag);n.className=className;n.textContent=String(text);return n;};
function workingIndicator(text){const row=el('span','working-indicator');const spinner=el('span','working-spinner');spinner.setAttribute('aria-hidden','true');row.append(spinner,el('span','',text));return row;}
const TITLES = {step:'STEP 三维数模',symbol:'Symbol 原理图符号',pcb:'PCB 焊盘封装'};
const ACTIONS = {submit:'确认参数并继续',confirm:'确认并继续',provide:'提交补充',choose:'使用所选项',resolve:'提交核对结果',change:'使用所选模板',approve:'采纳结果',reject:'不采纳',cancel:'取消任务',auto:'自动识别'};
const DIMENSIONS = {
  negative_side_terminal_length:'下侧引脚脚底长度',positive_side_terminal_length:'上侧引脚脚底长度',negative_side_terminal_thickness:'下侧引脚金属厚度',positive_side_terminal_thickness:'上侧引脚金属厚度',negative_side_pin_count:'下侧引脚数量',positive_side_pin_count:'上侧引脚数量',negative_side_pin_span:'下侧首末引脚中心距',positive_side_pin_span:'上侧首末引脚中心距',negative_side_terminal_width:'下侧单只引脚宽度',positive_side_terminal_width:'上侧单只引脚宽度',
  housing_length:'本体长度',housing_width:'本体宽度',housing_height:'本体高度',body_length:'本体长度',body_width:'本体宽度',body_height:'本体高度',pin_pitch:'相邻引脚中心间距',terminal_pitch:'相邻引脚中心间距',lead_spacing:'引脚间距',nominal_pin_count:'引脚总数',circuit_count:'接点总数',pins_per_side:'每侧引脚数',pitch:'引脚间距',
  pin_span:'同排首末引脚中心距',terminal_span:'同边首末引脚中心距',terminal_width:'单只引脚宽度',terminal_thickness:'引脚金属厚度',terminal_length:'引脚脚底长度',terminal_height:'引脚高度',terminal_diameter:'引脚直径',total_height:'器件总高度',overall_height:'器件总高度',overall_length:'含引脚的整体长度',overall_width:'含引脚的整体宽度',standoff:'本体离板高度',body_standoff:'本体离板高度',lead_projection:'引脚外伸距离',lead_angle_deg:'引脚角度',mold_draft_angle_top_deg:'本体上部脱模角',mold_draft_angle_bottom_deg:'本体下部脱模角',exposed_pad_length:'底部散热盘长度',exposed_pad_width:'底部散热盘宽度',
  front_plate_height:'前面板高度',plate_thickness:'面板厚度',front_shell_top_width:'前部金属壳上边宽度',front_shell_bottom_width:'前部金属壳下边宽度',front_shell_height:'前部金属壳高度',front_projection_depth:'前部金属壳伸出深度',shell_wall_thickness:'金属壳壁厚',mounting_center_span:'安装孔中心距',mounting_hole_diameter:'安装孔直径',mounting_outer_diameter:'安装柱外径',rear_housing_width:'后部本体宽度',rear_body_depth:'后部本体深度',rear_housing_height:'后部本体高度',contact_pitch:'相邻接点中心间距',contact_column_span:'接点首末列中心距',contact_row_spacing:'接点排间距',contact_outer_diameter:'接点外径',contact_inner_diameter:'接点内径',signal_pin_width:'信号引脚宽度',pin_tail_length:'引脚尾部长度'
};
const DIMENSION_HELP = {
  negative_side_terminal_length:'填写下侧引脚脚底长度，和上侧分别引用图纸标注。单独设置脚长时，两侧一起填写。',positive_side_terminal_length:'填写上侧引脚脚底长度；不能直接套下侧普通引脚的脚长。单独设置脚长时，两侧一起填写。',negative_side_terminal_thickness:'填写下侧金属片厚度，不是离板高度。单独设置厚度时，两侧一起填写。',positive_side_terminal_thickness:'填写上侧金属片厚度；宽端子可能有自己的厚度标注。单独设置厚度时，两侧一起填写。',negative_side_pin_count:'按建模顶视图核对下方这一排的实际数量。',positive_side_pin_count:'请按同一顶视方向核对上方这一排的实际数量，两排数量之和应等于总数。',negative_side_pin_span:'填写该排首末引脚中心距，不是相邻间距；单脚排填 0。',positive_side_pin_span:'填写该排首末引脚中心距；两脚排可能跨两个基本间距（如 2 × 0.95 mm）；单脚排填 0。',negative_side_terminal_width:'该侧每个金属引脚沿 X 方向的宽度，按尺寸表毫米列核对；不能把总体高度 A 当成引脚宽度。',positive_side_terminal_width:'独立填写该侧引脚宽度。宽端子应采用其专有宽度标注（例如 D1），不能直接套用另一侧的普通引脚 b。',
  pin_span:'量同一排第一只与最后一只引脚的中心距离，不是两排之间的距离。也可用（该排引脚数 − 1）× 相邻引脚中心间距计算。',
  terminal_span:'量同一边第一只与最后一只引脚的中心距离，不是相邻引脚之间的距离。',
  total_height:'从引脚落在电路板上的平面量到器件最高点，包含本体高度和离板高度。请核对侧视图的整体高度标注。',
  overall_height:'从器件安装平面量到最高点，核对侧视图的整体高度标注。',
  body_standoff:'量电路板安装面到本体底面的垂直距离。不要填引脚金属厚度。',
  standoff:'量电路板安装面到本体底面的垂直距离。',
  housing_height:'只量本体底面到顶面，不包含本体与电路板之间的间隙。',body_height:'只量本体底面到顶面，不包含离板高度。',
  body_width:'只量本体两侧之间的宽度，不包含伸出的引脚。',body_length:'只量本体两端之间的长度，不包含伸出的引脚。',
  overall_width:'量包含引脚的最外侧到最外侧的宽度。',overall_length:'量包含引脚的最外侧到最外侧的长度。',
  terminal_pitch:'量相邻两只引脚的中心距离。',pin_pitch:'量相邻两只引脚的中心距离。',
  nominal_pin_count:'填写器件所有引脚的总数，例如 16；填写整数。',circuit_count:'填写所有接点的总数；填写整数。',
  terminal_width:'俯视图中单只引脚横向的宽度。',terminal_thickness:'引脚金属片本身的厚度，不是共面度或离板高度。',
  terminal_length:'量引脚脚底从弯折处到脚尖的长度，不要直接用整体尺寸减去本体尺寸。',lead_projection:'量引脚从本体边缘向外伸出的距离，与引脚脚底长度不同。'
};
function dimensionText(value){return String(value||'').replace(/\b[a-z]+(?:_[a-z]+)*\b/g,name=>DIMENSIONS[name]||PCB_FIELDS[name]||({pin_numbering:'焊盘脚号',pads:'焊盘参数'}[name])||name);}
function dimensionLabel(field,spec){return DIMENSIONS[field]||[spec.label_zh,spec.title].find(value=>/[\u4e00-\u9fff]/.test(value||''))||'待确认尺寸';}
const STEP_PHASES = {stopped_insufficient_extraction:'尺寸信息不足或存在冲突，请核对右侧尺寸后继续',stop_insufficient_extraction:'尺寸信息不足，请补充尺寸',dimensions_need_confirmation:'等待确认尺寸',stopped_unsupported_template:'当前封装模板暂不支持',needs_human_follow_up:'需要补充信息',completed:'生成完成',matched:'已匹配参考模型',failed:'执行失败',awaiting_input:'等待核对',pending_review:'等待模型复核'};
const TEMPLATES={'resistor/two_terminal_chip':'电阻 · 两端贴片','capacitor/two_terminal_chip':'电容 · 两端贴片',inductor:'电感',diode:'二极管',transistor:'晶体管','connector/cn/dsub_connector':'连接器 · 梯形接口','connector/cn/pin_header':'连接器 · 排针','connector/mt':'连接器 · 结构物料','ic/gullwing_ic':'集成芯片 · 两侧鸥翼（含奇数引脚）','ic/quad_gullwing_ic':'集成芯片 · 四侧鸥翼引脚','ic/qfn_ufqfpn':'集成芯片 · 无引脚方形封装',misc:'其他封装'};
const STEP_VIEWS={preview_isometric:'等轴测图',preview_front:'正视图',preview_top:'俯视图',preview_right:'右视图'};
const PCB_FIELDS = {name:'封装名称',family:'封装族',body_x:'本体 X 尺寸（mm）',body_y:'本体 Y 尺寸（mm）',body_height_max:'最大高度（mm）',pin_count:'焊盘数量',mask_expansion:'阻焊扩展（mm）',min_clearance:'最小铜箔间距（mm）',ep_x:'散热盘 X 尺寸（mm）',ep_y:'散热盘 Y 尺寸（mm）',ep_pin_number:'散热盘编号'};
let token=sessionStorage.getItem('step-access-token')||'', taskKind=PAGE_KIND||'step', filter=PAGE_KIND||'', selected=null, activeJob=null;
let epoch=0, timer, historyVersion=0, offset=0, storageKey='', deviceInfo=null, previewKey='', previewUrl=null, questionKey='', submitting=false, connectionFailed=false;
const api=new WorkspaceAPI({getToken:()=>token,onUnauthorized:()=>logout('登录已过期，请重新登录。')});
const registrationLink=el('a','muted','没有账号？使用邀请码注册');registrationLink.href='/api/v1/auth/ui';$('#login-form').append(registrationLink);
const invitationLink=el('a','quiet','邀请码管理');invitationLink.href='/api/v1/auth/ui/admin';invitationLink.hidden=true;$('.sidebar footer').before(invitationLink);
let sourceKey='',sourceRevision=0;const sourceUrls=new Set();
const sourcePanel=el('section','panel source-panel'),sourceHeading=el('div','panel-heading'),sourceBody=el('div','source-body');
sourcePanel.id='source-panel';sourcePanel.hidden=true;sourceHeading.append(el('h2','','原始附件'),el('span','muted','核对时可对照原图'));sourcePanel.append(sourceHeading,sourceBody);$('.job-grid').before(sourcePanel);
const sourceDialog=el('dialog','source-dialog'),sourceClose=el('button','secondary','关闭'),sourceLarge=el('img');
sourceClose.type='button';sourceClose.onclick=()=>sourceDialog.close();sourceDialog.append(sourceClose,sourceLarge);document.body.append(sourceDialog);

function clearAttachments(){sourceKey='';sourceRevision++;sourceDialog.close();sourceLarge.removeAttribute('src');for(const url of sourceUrls)URL.revokeObjectURL(url);sourceUrls.clear();sourceBody.replaceChildren();sourcePanel.hidden=true;}
async function renderAttachments(job){
  const key=`${job.kind}:${job.drawing_id}:${job.detail?.source_revision_id||''}`;if(sourceKey===key)return;clearAttachments();sourceKey=key;
  const revision=sourceRevision,requestToken=token,valid=()=>revision===sourceRevision&&requestToken===token&&Boolean(token)&&selected?.kind===job.kind&&selected?.id===job.drawing_id;
  sourcePanel.hidden=false;sourceBody.append(workingIndicator('正在读取原始附件…'));
  try{
    const data=await api.attachments(job.kind,job.drawing_id);if(!valid())return;sourceBody.replaceChildren();
    if(!data.items.length){sourceBody.append(el('p','compact','这条任务没有可读取的原始附件。'));return;}
    for(const item of data.items){
      const card=el('article','source-card'),info=el('div','source-info'),actions=el('div','source-actions'),download=el('button','secondary','下载附件');
      download.type='button';info.append(el('strong','',item.label),el('span','muted',item.filename),el('span','muted',`${(item.size/1024).toFixed(1)} KB`));actions.append(download);card.append(info,actions);sourceBody.append(card);
      let cached=null,pending=null;
      const load=async()=>{
        if(cached)return cached;if(pending)return pending;
        pending=(async()=>{const response=await api.attachment(job.kind,job.drawing_id,item.name),blob=await response.blob();if(!valid())return null;cached=URL.createObjectURL(blob);sourceUrls.add(cached);return cached;})();
        try{return await pending;}finally{pending=null;}
      };
      download.onclick=async()=>{download.disabled=true;try{const url=await load();if(!url||!valid())return;const link=el('a');link.href=url;link.download=item.filename;document.body.append(link);link.click();link.remove();}catch(error){if(valid())flash('附件下载失败：'+error.message,true);}finally{download.disabled=false;}};
      if(item.media_type.startsWith('image/')){
        const display=el('div','source-image'),zoom=el('button','secondary','放大查看');zoom.type='button';zoom.disabled=true;actions.prepend(zoom);card.append(display);
        zoom.onclick=()=>{if(cached&&valid()){sourceLarge.alt=`${item.label}：${item.filename}`;sourceLarge.src=cached;sourceDialog.showModal();}};
        const show=async()=>{display.replaceChildren(workingIndicator('正在加载原图…'));try{const url=await load();if(!url||!valid())return;const image=el('img');image.alt=`${item.label}：${item.filename}`;image.onload=()=>{if(valid())zoom.disabled=false;};image.onerror=()=>{if(valid())failed();};image.src=url;display.replaceChildren(image);}catch(error){if(valid())failed();}};
        const failed=()=>{display.replaceChildren(el('p','compact','原图暂时无法显示，请重试。'));const retry=el('button','secondary','重新读取');retry.type='button';retry.onclick=()=>{if(cached){URL.revokeObjectURL(cached);sourceUrls.delete(cached);cached=null;}show();};display.append(retry);};
        show();
      }else card.append(el('p','compact','PDF 手册请下载后查看。'));
    }
  }catch(error){if(valid()){sourceKey='';sourceBody.replaceChildren(el('p','compact','原始附件读取失败，请刷新任务重试。'));}}
}

function flash(message,error=false){$('#flash').textContent=message;$('#flash').classList.toggle('error',error);$('#flash').hidden=false;}
function stop(){clearTimeout(timer);epoch++;}
function clearPreview(){previewKey='';if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=null;$('#preview').replaceChildren(el('p','muted','生成预览后会在这里显示。'));}
function logout(message=''){
  invitationLink.hidden=true;
  stop();historyVersion++;token='';selected=null;activeJob=null;storageKey='';deviceInfo=null;clearPreview();clearAttachments();
  sessionStorage.removeItem('step-access-token');$('#workspace').hidden=true;$('#login').hidden=false;
  $('#history').replaceChildren();$('#question').replaceChildren();$('#login-form').elements.password.value='';
  $('#login-error').textContent=message;$('#login-error').hidden=!message;
}
async function enter(){
  const entering=token;
  try{
    const user=await api.me();if(token!==entering||!token)return;
    invitationLink.hidden=user.role!=='admin';
    storageKey=`${PAGE_KIND||'foundry'}-task:${user.tenant_id}:${user.user_id||user.id}`;
    $('#identity').textContent=user.username||user.email||'你的工作区';$('#login').hidden=true;$('#workspace').hidden=false;
    newTask();await Promise.all([loadDevices(),loadHistory(true)]);
    if(token!==entering)return;
    try{const remembered=JSON.parse(sessionStorage.getItem(storageKey)||'null');if(remembered)selectJob(remembered.kind,remembered.id);}catch{sessionStorage.removeItem(storageKey);}
  }catch(error){if(error.status!==401){$('#login-error').textContent=error.message;$('#login-error').hidden=false;}}
}
$('#login-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;$('#login-error').hidden=true;
  try{const form=event.currentTarget;const result=await api.login(form.elements.username.value.trim(),form.elements.password.value);token=result.access_token;sessionStorage.setItem('step-access-token',token);form.elements.password.value='';await enter();}
  catch(error){$('#login-error').textContent=error.message;$('#login-error').hidden=false;}finally{button.disabled=false;}
});
$('#logout').onclick=()=>logout();
function newTask(clearSelection=false){stop();connectionFailed=false;selected=null;activeJob=null;questionKey='';clearPreview();clearAttachments();$('#create-view').hidden=false;$('#job-view').hidden=true;$('#page-title').textContent=PAGE_KIND?TITLES[PAGE_KIND]:'工程工作台';$('#flash').hidden=true;if(clearSelection&&storageKey)sessionStorage.removeItem(storageKey);$('#history').querySelectorAll('.active').forEach(n=>n.classList.remove('active'));}
$('#new-task').onclick=()=>newTask(true);

async function loadDevices(){
  const requestToken=token;
  try{const data=await api.devices();if(!token||token!==requestToken)return;deviceInfo=data;
    const box=$('#device-state');box.replaceChildren();
    for(const device of data.items){const entry=el('span','device'+(device.configured?' configured':''));entry.append(el('i'));const queue=data.queues.filter(q=>q.mode===device.id);entry.append(el('span','',device.label+' · '+(queue.some(q=>q.state==='running')?'执行中':device.id==='ssh'?(device.configured?'已配置':'未配置'):'按本机安装检测')));if(queue.length)entry.append(el('small','busy',`${queue.filter(q=>q.state==='queued').length} 个排队`));box.append(entry);}
    $('#execution-mode').querySelector('[value=ssh]').disabled=!data.items.find(d=>d.id==='ssh')?.configured;
    updateDeviceNote();
  }catch(error){if(error.status!==401)flash('设备配置读取失败：'+error.message,true);}
}
$('#refresh-devices').onclick=loadDevices;
function updateDeviceNote(){
  const mode=$('#execution-mode').value||deviceInfo?.defaults[taskKind];
  $('#workflow-note').textContent=taskKind==='step'?'STEP 在工作台服务端生成，无需选择 Cadence 设备。':taskKind==='pcb'&&$('#pcb-mode').value==='emit_only'?'仅生成脚本与几何预览，不连接 Cadence 设备。':`${mode==='ssh'?'远程':'本机'} Cadence · 同一设备上的生成任务按顺序执行。`;
}
function chooseKind(kind){
  if(PAGE_KIND&&kind!==PAGE_KIND)return;
  taskKind=kind;for(const button of document.querySelectorAll('[data-task]'))button.setAttribute('aria-pressed',String(button.dataset.task===kind));
  $('#symbol-fields').hidden=kind!=='symbol';$('#contexts').hidden=kind!=='pcb';$('#device-field').hidden=kind==='step';$('#mode-field').hidden=kind!=='pcb';
  $('#file').accept=kind==='symbol'?'.pdf,.png,.jpg,.jpeg,.webp,.bmp':'.png,.jpg,.jpeg,.webp,.bmp';
  updateFile();updateDeviceNote();
}
document.querySelectorAll('[data-task]').forEach(button=>button.onclick=()=>chooseKind(button.dataset.task));
$('#execution-mode').onchange=updateDeviceNote;$('#pcb-mode').onchange=updateDeviceNote;
function updateFile(){const file=$('#file').files[0];$('#file-title').textContent=file?file.name:taskKind==='symbol'?'选择或拖入手册 / 引脚图':taskKind==='pcb'?'选择或拖入推荐焊盘图':'选择或拖入工程图';$('#file-hint').textContent=file?`${(file.size/1024/1024).toFixed(2)} MB · 点击更换`:'PNG / JPG / WEBP / BMP'+(taskKind==='symbol'?' / PDF':'');$('#pdf-options').hidden=!(taskKind==='symbol'&&/\.pdf$/i.test(file?.name||''));}
$('#file').onchange=updateFile;
for(const name of ['dragenter','dragover'])$('#dropzone').addEventListener(name,event=>{event.preventDefault();$('#dropzone').classList.add('over');});
for(const name of ['dragleave','drop'])$('#dropzone').addEventListener(name,event=>{event.preventDefault();$('#dropzone').classList.remove('over');if(name==='drop'&&event.dataTransfer.files.length){$('#file').files=event.dataTransfer.files;updateFile();}});
$('#task-form').addEventListener('submit',async event=>{
  event.preventDefault();if(submitting)return;
  const kind=taskKind,file=$('#file').files[0];if(!file)return;
  const isPDF=/\.pdf$/i.test(file.name)||file.type==='application/pdf';if(isPDF&&kind!=='symbol'){flash(`${kind==='pcb'?'PCB':'STEP'} 主图不能上传 PDF，请将对应的尺寸图截图为 PNG / JPG 后上传。`,true);return;}
  if(file.size>(kind==='symbol'&&isPDF?64:kind==='step'?25:32)*1024*1024){flash('文件超过当前任务的上传大小限制。',true);return;}
  if($('#context-images').files.length>4&&kind==='pcb'){flash('辅助图最多四张。',true);return;}
  if(kind==='pcb'){
    for(const [index,context]of Array.from($('#context-images').files).entries()){
      if(!/\.(png|jpe?g|webp|bmp|pdf)$/i.test(context.name)){flash(`第${index+1}份辅助资料“${context.name}”格式不支持，请使用图片或 PDF 手册。`,true);return;}
    }
  }
  const data=new FormData();data.set('kind',kind);data.set('file',file);data.set('message',$('#message').value);
  if(kind!=='step'&&$('#execution-mode').value)data.set('execution_mode',$('#execution-mode').value);
  if(kind==='symbol'){data.set('device',event.currentTarget.elements.device.value);data.set('package',event.currentTarget.elements.package.value);data.set('strict_pages',String(isPDF&&event.currentTarget.elements.strict_pages.checked));}
  if(kind==='pcb'){data.set('mode',$('#pcb-mode').value);for(const context of $('#context-images').files)data.append('context_images',context);}
  submitting=true;$('#submit-task').disabled=true;$('#task-form').setAttribute('aria-busy','true');const requestToken=token;
  try{const result=await api.generate(data);if(token!==requestToken||!token)return;flash('任务已提交，状态将自动更新。');await selectJob(result.kind,result.drawing_id);loadHistory(true);}
  catch(error){if(token===requestToken)flash(error.message,true);}finally{submitting=false;$('#submit-task').disabled=false;$('#task-form').setAttribute('aria-busy','false');}
});

for(const button of document.querySelectorAll('[data-kind]'))button.onclick=()=>{filter=button.dataset.kind;for(const b of document.querySelectorAll('[data-kind]'))b.setAttribute('aria-pressed',String(b===button));loadHistory(true);};
async function loadHistory(reset=false){
  const version=++historyVersion,requestToken=token;if(reset)offset=0;
  try{const result=await api.jobs(filter,offset);if(version!==historyVersion||token!==requestToken||!token)return;
    const box=$('#history');if(reset)box.replaceChildren();
    for(const job of result.items){const button=el('button',selected?.id===job.drawing_id&&selected?.kind===job.kind?'active':'');button.type='button';button.append(el('span','title',job.title));const meta=el('span','meta');meta.append(el('span','',job.kind.toUpperCase()),el('span','',STATUS_LABELS[job.status]||job.status));button.append(meta);button.onclick=()=>selectJob(job.kind,job.drawing_id);box.append(button);}
    offset+=result.items.length;$('#load-more').hidden=!result.has_more;if(!box.children.length)box.append(el('p','empty','还没有任务。上传一份资料，开始你的第一个工程任务。'));
  }catch(error){if(version===historyVersion&&error.status!==401)flash('任务列表读取失败：'+error.message,true);}
}
$('#load-more').onclick=()=>loadHistory();$('#refresh-history').onclick=()=>loadHistory(true);
async function selectJob(kind,id){if(PAGE_KIND&&kind!==PAGE_KIND)return;stop();connectionFailed=false;selected={kind,id};activeJob=null;questionKey='';clearPreview();clearAttachments();$('#question').replaceChildren();$('#create-view').hidden=true;$('#job-view').hidden=false;$('#page-title').textContent=TITLES[kind];$('#job-title').textContent='读取任务…';$('#job-error').hidden=true;$('#artifacts').replaceChildren();$('#job-record').textContent='';$('#summary').textContent='';$('#findings').replaceChildren();if(storageKey)sessionStorage.setItem(storageKey,JSON.stringify({kind,id}));await poll(epoch);}
async function poll(version){
  const selection=selected;if(!selection||version!==epoch||!token)return;
  try{const job=await api.job(selection.kind,selection.id);if(version!==epoch||!token)return;if(connectionFailed){$('#flash').hidden=true;connectionFailed=false;}const changed=activeJob&&activeJob.status!==job.status;activeJob=job;renderJob(job,version);loadDevices();if(changed)loadHistory(true);if(job.status==='ai_processing')timer=setTimeout(()=>poll(version),2500);}
  catch(error){if(version===epoch&&error.status!==401){
    const retrying=![403,404,422].includes(error.status);connectionFailed=true;
    $('#job-status').replaceChildren(el('span','',retrying?'连接中断 · 正在重连':'任务读取失败'));$('#job-status').classList.add('attention');
    const findings=$('#findings');findings.querySelector('.processing-message')?.remove();findings.querySelector('.connection-message')?.remove();
    const notice=el('p','compact connection-message',retrying?'暂时无法获取最新任务状态，正在重新连接。':'无法获取该任务，请检查登录权限或重新选择任务。');notice.setAttribute('role','status');
    if(activeJob)notice.append(el('span','',` 上次状态：${STATUS_LABELS[activeJob.status]||'待更新'}。`));findings.prepend(notice);
    flash(error.message,true);if(retrying)timer=setTimeout(()=>poll(version),5000);
  }}
}
$('#refresh-job').onclick=()=>{if(selected){stop();poll(epoch);}};
function taskErrorText(error){
  if(/invalid_api_key|invalid api[- ]?key/i.test(error||''))return '模型服务认证失败（401）：请管理员检查模型 API Key 与接口地址，修复配置后重试此任务。';
  if(/validation errors? for PackageSpec|errors\.pydantic\.dev/i.test(error||''))return '封装参数尚未完整。请核对封装名称、本体尺寸、高度以及每行焊盘的脚号，再重新提交。';
  return error||'';
}
function renderJob(job,version){
  const detail=job.detail;$('#job-kind').textContent=TITLES[job.kind];$('#job-title').textContent=job.title;$('#job-meta').textContent=job.drawing_id+(job.created_at?' · '+new Date(job.created_at).toLocaleString('zh-CN'):'');
  const working=job.status==='ai_processing'&&!job.pending_input;
  $('#job-status').replaceChildren(working?workingIndicator(job.queue?.state==='queued'?'工作中 · 等待设备':'工作中'):el('span','',STATUS_LABELS[job.status]||job.status));$('#job-status').classList.toggle('attention',Boolean(job.pending_input)||job.status==='failed');
  $('#job-error').textContent=taskErrorText(job.error_msg);$('#job-error').hidden=!job.error_msg;
  if(job.kind==='pcb'&&detail.extraction_retryable&&api.retryExtraction){
    const retry=el('button','secondary','重新识别原图');retry.type='button';
    retry.onclick=async()=>{retry.disabled=true;try{await api.retryExtraction(job.kind,job.drawing_id);if(version===epoch){flash('已重新开始识别原图。');await poll(version);}}catch(error){flash(error.message,true);}finally{retry.disabled=false;}};
    $('#job-status').append(retry);
  }
  $('#job-progress').replaceChildren();const phases=job.kind==='pcb'?['读取尺寸','参数核对','Allegro 三阶段','结果复核']:job.kind==='symbol'?['读取资料','还原引脚','Capture 生成']:['读取工程图','核对尺寸','生成 STEP','结果复核'];
  phases.forEach((phase,i)=>{const span=el('span');span.append(el('b','',String(i+1).padStart(2,'0')),el('span','',phase));$('#job-progress').append(span);});
  $('#summary').textContent=detail.summary||detail.layout_summary||(detail.result?.graph_status?`当前步骤：${STEP_PHASES[detail.result.graph_status]||'正在处理任务，状态会自动更新。'}`:'');
  const execution=job.execution;$('#execution-summary').textContent=job.kind==='step'?'后端 OpenCascade':execution.mode?(execution.mode==='ssh'?'远程 Cadence':'本机 Cadence')+(job.queue?' · '+(job.queue.state==='queued'?'等待执行':'正在执行'):''):'设备执行前显示预览与脚本';
  const verdict=detail.verdict;$('#check-label').textContent=verdict?.assertions?`${verdict.assertions.filter(a=>a.passed).length} / ${verdict.assertions.length} 项通过`:detail.check_report?.score!==undefined?`自检 ${detail.check_report.score}`:'';
  const findings=$('#findings');findings.replaceChildren();for(const warning of detail.warnings||[])findings.append(el('p','finding',warning));
  if(verdict?.assertions)for(const check of verdict.assertions)findings.append(el('p','finding '+(check.passed?'pass':'fail'),`${check.passed?'✓':'×'} ${check.name}${check.passed?'':': '+String(check.actual)}`));
  if(working){const message=el('p','compact processing-message');message.setAttribute('role','status');message.append(workingIndicator(job.queue?.state==='queued'?'工作中 · 正在等待设备，状态会自动更新。':'工作中 · 工作流正在处理资料，状态会自动更新。'));findings.prepend(message);}
  else if(!findings.children.length)findings.append(el('p','compact',job.pending_input?'请根据原始资料完成当前核对。':'核对记录和执行结果会保存在任务中。'));
  renderQuestion(job);renderArtifacts(job);renderPreview(job,version);renderAttachments(job);
  $('#job-record').textContent=JSON.stringify({spec:detail.spec,pins:detail.pins,execution:job.execution,runs:detail.runs,human_history:detail.human_history,execution_logs:detail.execution_logs},null,2);
}
function renderArtifacts(job){const box=$('#artifacts');box.replaceChildren();for(const name of Object.keys(job.artifacts)){if(job.kind==='step'&&(name==='preview'||name.startsWith('preview_')))continue;const button=el('button','artifact',job.kind==='step'&&name==='step'?'下载 STEP 文件 ↧':name.toUpperCase()+' ↧');button.onclick=async()=>{button.disabled=true;try{const response=await api.artifact(job.kind,job.drawing_id,name);const url=URL.createObjectURL(await response.blob());const a=el('a');a.href=url;const disposition=response.headers.get('content-disposition')||'';const match=disposition.match(/filename\*=utf-8''([^;]+)/i);a.download=match?decodeURIComponent(match[1]):disposition.match(/filename="([^"\r\n]+)"/i)?.[1]||name;a.download=a.download.split(/[\\/]/).pop();document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(error){flash(error.message,true);}finally{button.disabled=false;}};box.append(button);}if(!box.children.length)box.append(el('p','muted','生成文件后可在这里下载。'));}

const NS='http://www.w3.org/2000/svg';
function svgNode(tag,attrs,text){const n=document.createElementNS(NS,tag);for(const [key,value]of Object.entries(attrs))n.setAttribute(key,String(value));if(text!==undefined)n.textContent=String(text);return n;}
function pcbPadPreview(pads,bodyX,bodyY){
  if(!pads.length||pads.some(p=>['x','y','size_x','size_y'].some(k=>!Number.isFinite(p[k])||(k.startsWith('size_')&&p[k]<=0))))return el('p','compact','填写完整坐标和尺寸后显示顶视预览。表格行号不是电气脚号。');
  const bx=Number.isFinite(bodyX)&&bodyX>0?bodyX:0,by=Number.isFinite(bodyY)&&bodyY>0?bodyY:0;
  const xs=pads.flatMap(p=>[p.x-p.size_x/2,p.x+p.size_x/2]),ys=pads.flatMap(p=>[p.y-p.size_y/2,p.y+p.size_y/2]);
  const span=Math.max(Math.max(...xs)-Math.min(...xs),Math.max(...ys)-Math.min(...ys),bx,by,1),margin=span*.23,font=span*.065;
  const left=Math.min(-bx/2,...xs)-margin,right=Math.max(bx/2,...xs)+margin,bottom=Math.min(-by/2,...ys)-margin,top=Math.max(by/2,...ys)+margin;
  const svg=svgNode('svg',{viewBox:`${left} ${-top} ${right-left} ${top-bottom}`,role:'img','aria-label':'焊盘顶视即时预览'});svg.style.width='100%';svg.style.maxHeight='300px';
  if(bx&&by)svg.append(svgNode('rect',{x:-bx/2,y:-by/2,width:bx,height:by,fill:'none',stroke:'#697566','stroke-width':span*.007}));
  pads.forEach((p,index)=>{const attrs={fill:'#d5ad51',stroke:'#284a38','stroke-width':span*.006};svg.append(p.shape==='CIRCLE'?svgNode('circle',{cx:p.x,cy:-p.y,r:p.size_x/2,...attrs}):svgNode('rect',{x:p.x-p.size_x/2,y:-p.y-p.size_y/2,width:p.size_x,height:p.size_y,...attrs}));svg.append(svgNode('text',{x:p.x,y:-p.y-font*.6,'text-anchor':'middle','font-size':font,fill:'#222c28'},p.number||`第${index+1}行`));});
  return svg;
}
function symbolPreview(layout){
  const w=layout.body_width,h=layout.body_height;if(!w||!h)return null;
  const pins=layout.pins||[], xs=pins.flatMap(p=>[p.x,p.hx]),ys=pins.flatMap(p=>[p.y,p.hy]);
  const left=Math.min(-w/2,...xs)-55,top=Math.min(-h/2,...ys)-35,right=Math.max(w/2,...xs)+55,bottom=Math.max(h/2,...ys)+35;
  const svg=svgNode('svg',{viewBox:`${left} ${top} ${right-left} ${bottom-top}`,role:'img','aria-label':layout.part_name+' 引脚布局'});
  svg.append(svgNode('rect',{x:-w/2,y:-h/2,width:w,height:h,fill:'#fffefa',stroke:'#284a38','stroke-width':1}));
  for(const pin of pins){svg.append(svgNode('line',{x1:pin.x,y1:pin.y,x2:pin.hx,y2:pin.hy,stroke:'#284a38','stroke-width':1}));svg.append(svgNode('text',{x:pin.hx+(pin.side==='left'?-3:3),y:pin.hy-3,'text-anchor':pin.side==='left'?'end':'start','font-size':6,fill:'#697566'},pin.number));svg.append(svgNode('text',{x:pin.x+(pin.side==='left'?5:pin.side==='right'?-5:0),y:pin.y+2,'text-anchor':pin.side==='right'?'end':pin.side==='left'?'start':'middle','font-size':6,fill:'#222c28'},pin.name));}
  return svg;
}
function renderStepPreview(job,version){
  const entries=Object.keys(STEP_VIEWS).filter(name=>job.artifacts[name]).map(name=>[name,STEP_VIEWS[name]]);
  if(!entries.length&&job.artifacts.preview)entries.push(['preview','模型预览']);
  $('#preview-label').textContent='模型视图';if(!entries.length)return;
  const key=JSON.stringify([job.drawing_id,entries.map(([name])=>[name,job.artifacts[name]])]);if(previewKey===key)return;
  clearPreview();previewKey=key;
  const panel=el('div','step-preview'),controls=el('div','step-preview-actions'),display=el('div','step-preview-display');
  controls.setAttribute('role','group');controls.setAttribute('aria-label','STEP 预览视图');
  display.append(el('p','muted','选择视图查看模型。'));panel.append(controls,display);$('#preview').replaceChildren(panel);
  let request=0;
  for(const [name,label]of entries){
    const button=el('button','secondary','查看'+label);button.type='button';button.setAttribute('aria-pressed','false');
    button.onclick=async()=>{
      const current=++request;for(const item of controls.children)item.setAttribute('aria-pressed',String(item===button));
      button.disabled=true;display.setAttribute('aria-busy','true');display.replaceChildren(workingIndicator('正在读取'+label+'…'));
      const valid=()=>current===request&&panel.isConnected&&selected?.kind===job.kind&&selected?.id===job.drawing_id&&previewKey===key&&Boolean(token);
      try{
        const response=await api.artifact(job.kind,job.drawing_id,name);const url=URL.createObjectURL(await response.blob());
        if(!valid()){URL.revokeObjectURL(url);return;}
        if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=url;
        const img=el('img');img.alt=label;img.onload=()=>{if(valid())display.setAttribute('aria-busy','false');};
        img.onerror=()=>{if(valid()){display.setAttribute('aria-busy','false');display.replaceChildren(el('p','muted','预览图片无法显示，请重新选择视图。'));}};
        img.src=url;display.replaceChildren(img);
      }catch(error){if(valid()){display.setAttribute('aria-busy','false');display.replaceChildren(el('p','muted','预览读取失败，请重新选择视图。'));}}
      finally{button.disabled=false;}
    };controls.append(button);
  }
  controls.firstElementChild.click();
}
async function renderPreview(job,version){
  if(job.kind==='step'){renderStepPreview(job,version);return;}
  if(job.kind==='symbol'&&job.detail.layout?.pins?.length){const key=JSON.stringify(job.detail.layout);if(key!==previewKey){clearPreview();previewKey=key;const svg=symbolPreview(job.detail.layout);if(svg)$('#preview').replaceChildren(svg);}$('#preview-label').textContent=`${job.detail.layout.pins.length} 个引脚`;return;}
  const name=job.kind==='pcb'?(job.artifacts.preview_png?'preview_png':null):job.artifacts.preview?'preview':Object.keys(job.artifacts).find(k=>k.startsWith('preview_'));
  $('#preview-label').textContent=job.kind==='pcb'?'按参数渲染':'模型视图';if(!name)return;
  const key=job.kind+job.drawing_id+name;if(previewKey===key)return;
  previewKey=key;
  try{const response=await api.artifact(job.kind,job.drawing_id,name);const url=URL.createObjectURL(await response.blob());if(version!==epoch||!token){URL.revokeObjectURL(url);return;}if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=url;const img=el('img');img.src=url;img.alt=job.kind==='pcb'?'生成的 PCB 焊盘几何预览':'生成的三维模型预览';$('#preview').replaceChildren(img);}catch(error){if(version===epoch){previewKey='';$('#preview').replaceChildren(el('p','muted','预览暂时不可用，请刷新任务。'));}}
}

function inputField(parent,label,value='',type='text'){const wrap=el('label','',label);const input=el('input');input.type=type;if(type==='number')input.step='any';input.value=value??'';wrap.append(input);parent.append(wrap);return input;}
function selectField(parent,label,options,value=''){const wrap=el('label','',label),select=el('select');select.setAttribute('aria-label',label);for(const [key,text]of options){const option=el('option','',text);option.value=key;select.append(option);}select.value=value;wrap.append(select);parent.append(wrap);return select;}
async function continuePcb(job,q,button,action){
  const requestToken=token,version=epoch;button.disabled=true;button.setAttribute('aria-busy','true');
  const previous=button.textContent;button.replaceChildren(workingIndicator('正在保存并继续…'));
  try{await action();if(version!==epoch||token!==requestToken)return;questionKey='';clearAttachments();flash('补充已保存，正在检查剩余问题。');stop();poll(epoch);loadHistory(true);}
  catch(error){if(version===epoch&&token===requestToken){flash(taskErrorText(error.message),true);if(error.status===409){stop();poll(epoch);}}}
  finally{button.disabled=false;button.setAttribute('aria-busy','false');button.textContent=previous;}
}
function renderPcbFollowups(job,q,box){
  const history=(q.human_history||[]).filter(item=>item.stage==='image_clarification');
  if(history.length){const records=el('details','clarification-history');records.append(el('summary','',`已回答 ${history.length} 轮 · 查看追问记录`));
    for(const entry of q.human_history||[]){if(entry.stage==='source_supplement')records.append(el('p','compact',`资料已更新：${entry.filename}。之前回答保留为历史，新资料需要重新核对。`));if(entry.stage==='image_clarification')for(const question of entry.questions||[]){const value=entry.values?.[question.id];const label=question.options?.find(o=>o.value===value)?.label|| (typeof value==='object'&&!Array.isArray(value)?value.mode==='source'?'按图纸原值':`${value.from} → ${value.to} mm`:Array.isArray(value)?value.join('、'):String(value));records.append(el('p','compact',question.title+'：'+label));}}box.append(records);}
  if(q.questions?.length){const panel=el('section','clarification-panel');panel.setAttribute('aria-label','PCB 人工追问');panel.append(el('h3','','还需要你确认这些信息'),el('p','compact','可以先回答已知的部分；保存后会更新下一轮问题。不清楚的尺寸或脚号，请在下方补充资料。'));
    const readers=[];
    for(const [i,question] of q.questions.entries()){
      const card=el('div','clarification-card');card.append(el('span','clarification-index',String(i+1).padStart(2,'0')));
      if(question.type==='choice'){const input=selectField(card,question.title,[['','请选择'],...question.options.map(o=>[o.value,o.label])]);readers.push(()=>input.value?[question.id,input.value]:null);}
      else if(question.type==='manufacturing'){const mode=selectField(card,question.title,[['','请选择'],['source','按手册推荐的图纸原值'],['process','按本次工艺要求调整']]);const fields=el('div','field-row');card.append(fields);fields.hidden=true;const from=selectField(fields,'要调整的原宽度（mm）',question.widths.map(w=>[String(w),String(w)]),String(question.widths[0])),to=inputField(fields,'本次工艺宽度／直径（mm）','','number');mode.onchange=()=>{fields.hidden=mode.value!=='process';};readers.push(()=>mode.value?[question.id,mode.value==='source'?{mode:'source'}:{mode:'process',from:Number(from.value),to:Number(to.value)}]:null);}
      else{const input=inputField(card,question.title,'',question.type==='number'?'number':'text');if(question.type==='numbers')input.placeholder=question.example||'例如 A1, A2, B1';readers.push(()=>input.value.trim()?[question.id,question.type==='number'?Number(input.value):input.value.trim()]:null);}
      card.append(el('p','compact',question.help));panel.append(card);
    }
    const answer=el('button','primary','保存回答并继续追问');answer.type='button';answer.onclick=()=>{const values=Object.fromEntries(readers.map(read=>read()).filter(Boolean));if(!Object.keys(values).length){flash('请先回答一个问题，或补充下面的资料。',true);return;}continuePcb(job,q,answer,()=>api.answer(job.kind,job.drawing_id,q.interrupt_id,{action:'clarify',values}));};panel.append(answer);box.append(panel);
  }
  const sources=el('section','clarification-sources');sources.append(el('h3','','不知道怎么填？补充资料让系统重新识别'),el('p','compact','可补充清晰的推荐焊盘图、封装尺寸图、顶视引脚图或 PDF 手册。参考成品截图只作对照，请勿作为尺寸来源。'));
  const role=selectField(sources,'补充资料用途',[['context','辅助尺寸、引脚图或 PDF 手册'],['primary','替换为正式推荐焊盘主图']],'context');
  const label=el('label','','选择补充图片或 PDF'),file=el('input');file.type='file';file.accept='.png,.jpg,.jpeg,.webp,.bmp,.pdf';label.append(file);sources.append(label);
  const upload=el('button','secondary','上传补充资料并重新识别');upload.type='button';upload.onclick=()=>{const selectedFile=file.files[0];if(!selectedFile){flash('请先选择补充资料。',true);return;}if(selectedFile.size>32*1024*1024){flash('补充资料不能超过32 MB。',true);return;}const data=new FormData();data.set('file',selectedFile);data.set('role',role.value);data.set('interrupt_id',q.interrupt_id);continuePcb(job,q,upload,()=>api.supplement(job.kind,job.drawing_id,data));};sources.append(upload);box.append(sources);
}
function renderQuestion(job){
  const q=job.pending_input,box=$('#question');if(!q){questionKey='';box.replaceChildren();return;}
  const key=`${job.kind}:${job.drawing_id}:${q.interrupt_id}`;if(questionKey===key)return;questionKey=key;
  box.replaceChildren();box.append(el('p','question-title',dimensionText(q.question||'请核对当前参数。')));
  if(q.validation_error)box.append(el('p','error',dimensionText(taskErrorText(q.validation_error))));
  if(q.issues?.length)box.append(el('p','evidence',dimensionText(q.issues.map(taskErrorText).join('\n'))));
  if(job.kind==='pcb'&&q.stage==='image_parameters')renderPcbFollowups(job,q,box);
  const form=el('form','question-form');let read=()=>({});
  if(job.kind==='pcb'&&q.stage==='image_parameters'){
    const spec=q.spec||{},inputs={},row=el('div','field-row');form.append(row);
    for(const [field,label]of Object.entries(PCB_FIELDS)){if(field.startsWith('ep_'))continue;inputs[field]=inputField(row,label,spec[field],['name','family'].includes(field)?'text':'number');}
    const ep=selectField(form,'独立散热盘',[['','请按资料确认'],['false','没有'],['true','有']],spec.ep_present===null||spec.ep_present===undefined?'':String(spec.ep_present));
    for(const field of ['ep_x','ep_y','ep_pin_number']){inputs[field]=inputField(form,PCB_FIELDS[field],spec[field],field==='ep_pin_number'?'text':'number');inputs[field].parentElement.hidden=ep.value!=='true';}
    ep.onchange=()=>{for(const field of ['ep_x','ep_y','ep_pin_number'])inputs[field].parentElement.hidden=ep.value!=='true';};
    let policy;if(q.grid?.bridged_columns?.length)policy=selectField(form,'连通地焊盘的生成方式',[['','请明确选择'],['independent','按普通盘长度拆分为独立焊盘'],['source','保留原图连通铜箔']], '');
    const batch=el('div','field-row');const fromWidth=inputField(batch,'批量调整：原焊盘宽度（mm）',q.grid?.standard_size_x??'','number'),toWidth=inputField(batch,'工艺要求：生成宽度（mm）','','number');toWidth.placeholder='例如 0.20';
    const batchButton=el('button','secondary','批量修改相同宽度焊盘');batchButton.type='button';const batchStatus=el('p','compact');batchStatus.setAttribute('role','status');form.append(batch,batchButton,batchStatus);
    const table=el('table','pad-table'),head=el('thead'),tr=el('tr');for(const label of ['脚号','形状','X / mm','Y / mm','宽／直径 mm','长 / mm','操作'])tr.append(el('th','',label));head.append(tr);table.append(head);
    const body=el('tbody'),pads=[],rowControls=new Map(),emptyLine=el('tr','pad-empty'),emptyCell=el('td','','尚无焊盘。点击“新增焊盘”，按图纸填写脚号、中心坐标和铜盘尺寸。');emptyCell.colSpan=7;emptyLine.append(emptyCell);
    const toolbar=el('div','pad-toolbar'),addPad=el('button','secondary','＋ 新增焊盘'),padCount=el('span','compact');addPad.type='button';padCount.setAttribute('aria-live','polite');toolbar.append(addPad,padCount);
    let updatePadPreview=()=>{};
    const rowEditingLocked=Boolean(policy&&spec.pads?.length);addPad.disabled=rowEditingLocked;
    const labels={number:'脚号',shape:'形状',x:'X 坐标',y:'Y 坐标',size_x:'宽度',size_y:'长度'};
    const updateRows=(edited=false)=>{
      emptyLine.remove();if(!pads.length)body.append(emptyLine);
      padCount.textContent=`共 ${pads.length} 个焊盘`;
      if(edited)inputs.pin_count.value=String(pads.length);
      pads.forEach((fields,index)=>{for(const [field,input]of Object.entries(fields))input.setAttribute('aria-label',`第 ${index+1} 行焊盘${labels[field]}`);rowControls.get(fields).remove.setAttribute('aria-label',`删除第 ${index+1} 行焊盘`);});
      updatePadPreview();
    };
    const appendPad=(pad={},edited=false)=>{
      const line=el('tr'),fields={};
      for(const field of Object.keys(labels)){
        const cell=el('td'),input=el(field==='shape'?'select':'input');
        if(field==='shape'){
          for(const [key,label]of [['RECTANGLE','矩形'],['CIRCLE','圆形']]){const option=el('option','',label);option.value=key;input.append(option);}input.value=pad.shape||'RECTANGLE';
        }else{input.type=field==='number'?'text':'number';input.step='any';input.value=pad[field]??'';}
        cell.append(input);line.append(cell);fields[field]=input;
      }
      const syncCircle=()=>{const circle=fields.shape.value==='CIRCLE';fields.size_y.disabled=circle;if(circle)fields.size_y.value=fields.size_x.value;};fields.shape.onchange=syncCircle;fields.size_x.oninput=()=>{if(fields.shape.value==='CIRCLE')fields.size_y.value=fields.size_x.value;};syncCircle();
      const cell=el('td'),remove=el('button','pad-remove','删除');remove.type='button';remove.disabled=rowEditingLocked;cell.append(remove);line.append(cell);
      remove.onclick=()=>{const index=pads.indexOf(fields);pads.splice(index,1);rowControls.delete(fields);line.remove();updateRows(true);(pads[Math.min(index,pads.length-1)]?.number||addPad).focus();};
      body.append(line);pads.push(fields);rowControls.set(fields,{line,remove});updateRows(edited);
      if(edited){fields.number.focus();line.scrollIntoView({block:'nearest'});}
    };
    for(const pad of spec.pads||[])appendPad(pad);updateRows();addPad.onclick=()=>appendPad({},true);
    table.append(body);const wrap=el('div','table-wrap');wrap.tabIndex=0;wrap.setAttribute('aria-label','焊盘表，可左右滚动查看全部列');wrap.append(table);form.append(el('p','compact','焊盘尺寸均为毫米，X 向右、Y 向上。圆形焊盘在宽／直径列填写铜盘直径，长度自动同步。可新增或删除焊盘；新增行请按图纸填写，系统不会自动编脚号。表格可左右滚动查看全部列。图纸原值保留在来源记录中；修改后的尺寸作为人工工艺调整用于生成。'),toolbar,wrap);
    if(rowEditingLocked)form.append(el('p','compact','此图已识别完整连通焊盘网格，需要保留全部焊盘行；可修改现有行，或上传更正后的主图重新识别。'));
    batchButton.onclick=()=>{const from=Number(fromWidth.value),to=Number(toWidth.value);if(!Number.isFinite(from)||!Number.isFinite(to)||from<=0||to<=0){batchStatus.textContent='请填写大于零的原宽度和生成宽度。';return;}let count=0;for(const fields of pads){if(fields.size_x.value!==''&&Math.abs(Number(fields.size_x.value)-from)<1e-6){fields.size_x.value=String(to);if(fields.shape.value==='CIRCLE')fields.size_y.value=String(to);count++;}}batchStatus.textContent=count?`已将 ${count} 个焊盘宽度从 ${from} mm 调整为 ${to} mm，其他宽度保持原值。提交时将记录为工艺调整。`:'没有找到匹配原宽度的焊盘，请检查表中数值。';};
    const livePreview=el('section','pad-live-preview');livePreview.setAttribute('aria-label','焊盘位置核对');livePreview.append(el('h3','','提交前核对脚号与位置'),el('p','compact','顶视图：X 向右，Y 向上。请对照原图核对每个脚号的位置，表格行号不是电气脚号。'));
    const drawing=el('div');livePreview.append(drawing);form.append(livePreview);
    if(q.pin_reference?.pads?.length)livePreview.append(el('p','compact','来源引脚图已按位置绑定脚号：'+q.pin_reference.pads.map(p=>`${p.number} 脚 (${p.x}, ${p.y}) mm`).join('；')));
    updatePadPreview=()=>drawing.replaceChildren(pcbPadPreview(pads.map(fields=>Object.fromEntries(Object.entries(fields).map(([k,input])=>[k,['number','shape'].includes(k)?input.value.trim():input.value===''?NaN:Number(input.value)]))),Number(inputs.body_x.value),Number(inputs.body_y.value)));
    form.addEventListener('input',updatePadPreview);form.addEventListener('change',updatePadPreview);batchButton.addEventListener('click',updatePadPreview);updatePadPreview();
    read=()=>{const patch={};for(const [field,input]of Object.entries(inputs)){if(input.value!==''&&!input.parentElement.hidden)patch[field]=input.type==='number'?Number(input.value):input.value;}
      if(ep.value!=='')patch.ep_present=ep.value==='true';
      if(!pads.length){addPad.focus();throw new Error('请先新增焊盘，按图纸填写脚号、中心坐标和铜盘尺寸。');}
      if(pads.length){const missing=pads.flatMap((fields,index)=>fields.number.value.trim()?[]:[index+1]);if(missing.length){pads[missing[0]-1].number.focus();throw new Error(`请填写第 ${missing.join('、')} 行焊盘的脚号。请按顶视图或引脚定义确认编号；系统不会自动补造。`);}
        patch.pads=pads.map(fields=>Object.fromEntries(Object.entries(fields).map(([field,input])=>[field,['number','shape'].includes(field)?input.value.trim():input.value===''?null:Number(input.value)])));patch.pin_numbering=patch.pads.map(p=>p.number);
        if(new Set(patch.pin_numbering).size!==pads.length)throw new Error('焊盘脚号重复，请逐行核对后重新提交。');
        for(const [index,pad]of patch.pads.entries())for(const field of ['x','y','size_x','size_y'])if(pad[field]===null||!Number.isFinite(pad[field])||(field.startsWith('size_')&&pad[field]<=0)){pads[index][field].focus();throw new Error(`请填写第 ${index+1} 行焊盘的有效${labels[field]}${field.startsWith('size_')?'（大于零）':''}。`);}
        patch.pin_count=pads.length;patch.pin_layout='EXPLICIT';patch.pin1_marker='NONE';}
      if(policy&&!policy.value)throw new Error('请选择连通地焊盘的生成方式，再继续。');
      return {spec:patch,...(policy?.value?{pad_layout_policy:policy.value}:{})};};
  }else if(job.kind==='symbol'&&['device','package'].includes(q.stage)){
    const input=inputField(form,q.stage==='device'?'器件型号':'封装名称',q.candidates?.[0]||'');
    if(q.candidates?.length)form.append(el('p','compact','候选：'+q.candidates.join(' / ')));
    read=()=>({value:input.value});
  }else if(job.kind==='symbol'&&q.stage==='facts'){
    const inputs={};for(const item of q.items||[])inputs[item.key]=inputField(form,item.question||item.prompt||item.key,item.default||'');read=()=>({values:Object.fromEntries(Object.entries(inputs).map(([key,input])=>[key,input.value]))});
  }else if(job.kind==='symbol'&&['conflicts','review_diffs'].includes(q.stage)){
    const values=[];for(const item of q.items||[]){const labels={side:'所在侧别',name:'引脚名称',type:'电气类型',description:'说明'};const source=item.values?Object.entries(item.values).map(([source,value])=>`${source}：${value}`).join(' / '):`原识别：${item.observed||''} / 复核：${item.expected||''}`;form.append(el('p','compact',source));values.push({item,input:inputField(form,`引脚 ${item.pin_number} · ${labels[item.field]||item.field}`,'')});}read=()=>({resolutions:values.map(({item,input})=>({pin_number:item.pin_number,field:item.field,value:input.value}))});
  }else if(job.kind==='step'&&q.stage==='dimensions'){
    form.append(el('p','dimension-intro compact','只填写你能从图纸确认的尺寸；不确定的可以留空。长度单位为毫米（mm），数量填整数。图纸候选值是识别结果，请核对后再填写。'));
    for(const detail of q.conflict_details||[])form.append(el('p','dimension-conflict compact',dimensionText(detail)));
    const unitLabel=unit=>unit==='count'?'个':unit==='deg'?'°':unit||'mm';
    const inputs={},entries=Object.entries(q.fields||{}),grouped=q.side_groups||entries.some(([field])=>/^(positive|negative)_side_/.test(field));
    if(grouped)form.append(el('p','dimension-intro compact',q.side_orientation||'按建模顶视图核对：上侧在图上方，下侧在图下方。两侧数量、尺寸分别填写。'));
    const groups=grouped?[['upper','上侧引脚'],['lower','下侧引脚'],['body','本体与默认尺寸']]:[['all','']];
    for(const [group,title]of groups){
      const items=entries.filter(([field,spec])=>group==='all'||(spec.group||(field.startsWith('positive_side_')?'upper':field.startsWith('negative_side_')?'lower':'body'))===group);
      if(!items.length)continue;
      const target=grouped?el('fieldset','dimension-group'):form;
      if(grouped){target.append(el('legend','',title));form.append(target);if(group!=='body')target.append(el('p','compact','脚长、厚度若单独设置，请上下侧一起填；留空时使用下方已确认的默认尺寸，作为包络模型。'));}
      for(const [field,spec]of items){
      const label=dimensionLabel(field,spec)+`（${unitLabel(spec.unit)}）`;const previous=(spec.observed_candidates||[]).find(c=>c.evidence_kind==='human_input');inputs[field]=inputField(target,label,!spec.source_review_required&&previous?String(previous.value):'','number');if(spec.unit==='count')inputs[field].step='1';
      const help=el('p','dimension-help compact',DIMENSION_HELP[field]||(/[\u4e00-\u9fff]/.test(spec.description||'')?dimensionText(spec.description):'请按图纸中对应尺寸的标注填写。'));help.id=`dimension-help-${field}`;inputs[field].setAttribute('aria-describedby',help.id);target.append(help);
      if(spec.source_review_warning)target.append(el('p','dimension-conflict compact',spec.source_review_warning));
      if(spec.observed_candidates?.length)target.append(el('p','dimension-candidates compact',(spec.source_review_required?'识别候选（符号和单位未确认）：':'图纸候选：')+spec.observed_candidates.map(c=>spec.source_review_required?String(c.value):`${c.value} ${unitLabel(c.unit)}`).join(' / ')));
    }
    }
    read=()=>({values:Object.fromEntries(Object.entries(inputs).filter(([,input])=>input.value!=='').map(([key,input])=>[key,{value:Number(input.value),unit:q.fields[key].unit||'mm'}]))});
  }else if(job.kind==='step'&&q.stage==='package'){
    const input=inputField(form,'封装名称','','text');read=()=>({package_type:input.value});
  }else if(job.kind==='step'&&q.stage==='routing'){
    const options=(q.candidates||[]).filter(c=>c.implemented).map(c=>[c.family_id,TEMPLATES[c.family_id]||'其他封装模板']);const select=selectField(form,'封装模板',options,q.suggested?.family_id||options[0]?.[0]||'');read=()=>({family_id:select.value});
  }
  const evidence=el('details','details');evidence.append(el('summary','','查看当前问题的来源与参数'),el('pre','',JSON.stringify({summary:q.summary,evidence:q.evidence,grid:q.grid,fields:q.fields,suggested:q.suggested,items:q.items},null,2)));form.append(evidence);
  const comment=el('label','','核对说明（可选）'),text=el('textarea');text.rows=2;text.maxLength=2000;comment.append(text);form.append(comment);
  const actions=el('div','actions');for(const action of (q.options||[]).filter(action=>action!=='clarify')){const button=el('button',['cancel','reject'].includes(action)?'secondary':'primary',ACTIONS[action]||action);button.type='button';if(action==='approve'&&q.can_approve===false)button.disabled=true;
    button.onclick=async()=>{const requestToken=token;const jobVersion=epoch;try{const answer={action,comment:text.value,...(['cancel','reject','approve','auto','confirm'].includes(action)?{}:read())};for(const b of actions.querySelectorAll('button'))b.disabled=true;await api.answer(job.kind,job.drawing_id,q.interrupt_id,answer);if(jobVersion!==epoch||token!==requestToken)return;questionKey='';flash('回答已保存，任务继续处理。');stop();poll(epoch);loadHistory(true);}catch(error){if(jobVersion===epoch&&token===requestToken){flash(taskErrorText(error.message),true);if(error.status===409){stop();poll(epoch);}}}finally{for(const b of actions.querySelectorAll('button'))b.disabled=b.textContent===ACTIONS.approve&&q.can_approve===false;}};actions.append(button);}
  form.append(actions);box.append(form);
}
chooseKind(PAGE_KIND||'step');if(token)enter();
