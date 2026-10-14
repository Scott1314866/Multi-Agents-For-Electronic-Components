import {SymbolAPI, ApiError} from '/api/v1/symbol/ui/assets/api.js';

export const PAGE_KIND = 'pcb';
export const STATUS_LABELS = {ai_processing:'处理中',awaiting_input:'等待核对',pending_review:'等待复核',completed:'已完成',failed:'执行失败',stopped:'已停止',rejected:'未采纳',cancelled:'已取消'};

function packagePath(kind,id){
  if(kind!=='pcb'||!/^[a-f0-9-]{36}$/i.test(id))throw new ApiError('PCB 任务编号无效。');
  return `/api/v1/pcb/packages/${encodeURIComponent(id)}`;
}

export class PCBAPI extends SymbolAPI {
  devices(){return this.request('/api/v1/pcb/devices');}
  async jobs(_kind='pcb',offset=0){
    const result=await this.request(`/api/v1/pcb/packages?limit=30&offset=${offset}`);
    return {...result,items:result.items.map(item=>({...item,kind:'pcb',title:item.spec_name||`PCB · ${item.drawing_id.slice(0,8)}`}))};
  }
  async job(kind,id){
    const detail=await this.request(packagePath(kind,id));
    return {drawing_id:detail.drawing_id,kind:'pcb',title:detail.spec_name||`PCB · ${id.slice(0,8)}`,
      status:detail.status,pending_input:detail.pending_input,execution:detail.execution||{},
      artifacts:detail.artifact_urls||{},queue:detail.queue,error_msg:detail.error_msg,
      created_at:detail.created_at,detail};
  }
  generate(data){
    // The dedicated page talks to PCB's image endpoint, not a second job store.
    data.delete('kind');
    return this.request('/api/v1/pcb/generate-image',{method:'POST',body:data,timeout:120000})
      .then(result=>({...result,kind:'pcb'}));
  }
  attachments(kind,id){return this.request(`${packagePath(kind,id)}/attachments`);}
  attachment(kind,id,name){
    packagePath(kind,id);if(!/^(source|context_[1-4]|document_[1-8])$/.test(name))throw new ApiError('附件类型无效。');
    return this.request(`${packagePath(kind,id)}/attachments/${name}`,{raw:true,timeout:120000});
  }
  answer(kind,id,interruptId,answer){
    return this.request(`${packagePath(kind,id)}/human-input`,{method:'POST',body:{interrupt_id:interruptId,answer}});
  }
  supplement(kind,id,data){return this.request(`${packagePath(kind,id)}/sources`,{method:'POST',body:data,timeout:120000});}
  retryExtraction(kind,id){
    return this.request(`${packagePath(kind,id)}/retry`,{method:'POST'});
  }
  artifact(kind,id,name){
    if(!/^[a-z0-9_]+$/i.test(name))throw new ApiError('文件类型无效。');
    return this.request(`${packagePath(kind,id)}/artifacts/${name}`,{raw:true,timeout:120000});
  }
}

export {PCBAPI as WorkspaceAPI};
