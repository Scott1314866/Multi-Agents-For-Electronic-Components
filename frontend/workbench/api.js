import { SymbolAPI, ApiError } from '/api/v1/symbol/ui/assets/api.js';

export const KINDS = new Set(['step', 'symbol', 'pcb']);
export const STATUS_LABELS = {ai_processing:'处理中',awaiting_input:'等待核对',pending_review:'等待复核',completed:'已完成',failed:'执行失败',stopped:'已停止',rejected:'未采纳',cancelled:'已取消'};
export function jobPath(kind, id) {
  if (!KINDS.has(kind) || !/^[a-f0-9-]{36}$/i.test(id)) throw new ApiError('任务编号无效。');
  return `/api/v1/workbench/jobs/${kind}/${encodeURIComponent(id)}`;
}
export function artifactPath(kind, id, name) {
  if (!/^[a-z0-9_]+$/i.test(name)) throw new ApiError('文件类型无效。');
  return `${jobPath(kind,id)}/artifacts/${name}`;
}
export class WorkbenchAPI extends SymbolAPI {
  devices() { return this.request('/api/v1/workbench/devices'); }
  jobs(kind='', offset=0) { return this.request(`/api/v1/workbench/jobs?limit=30&offset=${offset}${kind ? '&kind='+encodeURIComponent(kind) : ''}`); }
  job(kind,id) { return this.request(jobPath(kind,id)); }
  retryExtraction(kind,id) {
    jobPath(kind,id);
    if(kind!=='pcb')throw new ApiError('仅支持 PCB 图片重新识别。');
    return this.request(`/api/v1/pcb/packages/${encodeURIComponent(id)}/retry`,{method:'POST'});
  }
  generate(data) { return this.request('/api/v1/workbench/jobs',{method:'POST',body:data,timeout:120000}); }
  answer(kind,id,interruptId,answer) { return this.request(`${jobPath(kind,id)}/human-input`,{method:'POST',body:{interrupt_id:interruptId,answer}}); }
  artifact(kind,id,name) { return this.request(artifactPath(kind,id,name),{raw:true,timeout:120000}); }
  attachments(kind,id) { return this.request(`${jobPath(kind,id)}/attachments`); }
  attachment(kind,id,name) {
    if(!/^(source|context_[1-4]|document_[1-8])$/.test(name))throw new ApiError('附件类型无效。');
    return this.request(`${jobPath(kind,id)}/attachments/${name}`,{raw:true,timeout:120000});
  }
  supplement(kind,id,data){jobPath(kind,id);if(kind!=='pcb')throw new ApiError('仅支持PCB任务补充尺寸资料。');return this.request(`/api/v1/pcb/packages/${encodeURIComponent(id)}/sources`,{method:'POST',body:data,timeout:120000});}
}

export const PAGE_KIND = null;
export {WorkbenchAPI as WorkspaceAPI};
