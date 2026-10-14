import {SymbolAPI} from '/api/v1/symbol/ui/assets/api.js';
const $=selector=>document.querySelector(selector);
const api=new SymbolAPI({getToken:()=>sessionStorage.getItem('step-access-token'),onUnauthorized:()=>{sessionStorage.removeItem('step-access-token');show('登录已过期，请返回工作台重新登录。');}});
function show(message,success=false){$('#message').textContent=message;$('#message').hidden=false;$('#message').classList.toggle('success',success);}
function errorText(error){return error.message;}
async function registration(){
  const form=$('#register-form'),button=$('#register-submit');
  try{const config=await api.request('/api/v1/auth/registration',{authenticated:false});
    if(config.mode==='closed'){$('#registration-note').textContent='当前未开放注册，请联系管理员。';return;}
    $('#registration-note').textContent=config.mode==='invite'?'使用管理员提供的邀请码，加入公司工作区。':'注册账号，开始你的工程任务。';
    $('#invite-field').hidden=config.mode!=='invite';form.elements.invite_code.required=config.mode==='invite';button.disabled=false;
  }catch(error){show(errorText(error));return;}
  form.addEventListener('submit',async event=>{event.preventDefault();$('#message').hidden=true;
    if(form.elements.password.value!==form.elements.confirm_password.value){show('两次输入的密码不一致。');return;}
    if(new TextEncoder().encode(form.elements.password.value).length>72){show('密码的 UTF-8 长度不能超过 72 字节。');return;}
    button.disabled=true;
    try{const result=await api.request('/api/v1/auth/register',{method:'POST',authenticated:false,body:{username:form.elements.username.value.trim(),email:form.elements.email.value.trim(),password:form.elements.password.value,invite_code:form.elements.invite_code.value.trim()}});
      sessionStorage.setItem('step-access-token',result.access_token);form.reset();window.location.replace('/api/v1/workbench/ui');
    }catch(error){show(errorText(error));}finally{button.disabled=false;}
  });
}
function node(tag,text,className=''){const element=document.createElement(tag);element.textContent=text;element.className=className;return element;}
async function admin(){
  const form=$('#invite-form');form.querySelector('button').disabled=true;
  try{const me=await api.me();if(me.role!=='admin'){show('邀请码管理需要管理员权限，请使用管理员账号登录。');return;}}
  catch(error){show(errorText(error));return;}
  form.querySelector('button').disabled=false;
  async function refresh(){
    try{const result=await api.request('/api/v1/auth/invites');const list=$('#invite-list');list.replaceChildren();
      for(const invite of result.items){const row=node('article','','invite-row'),info=node('div','');const state=invite.revoked_at?'已撤销':new Date(invite.expires_at)<=new Date()?'已过期':invite.used_count>=invite.max_uses?'名额已用完':'可使用';
        info.append(node('strong',invite.label||'未备注'),node('p',`尾号 ${invite.token_hint} · ${state} · 已注册 ${invite.used_count}/${invite.max_uses} 人 · 到期 ${new Date(invite.expires_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})}`));row.append(info);
        if(state==='可使用'){const revoke=node('button','撤销','secondary');revoke.type='button';revoke.onclick=async()=>{revoke.disabled=true;try{await api.request(`/api/v1/auth/invites/${encodeURIComponent(invite.id)}`,{method:'DELETE'});await refresh();}catch(error){show(errorText(error));revoke.disabled=false;}};row.append(revoke);}
        list.append(row);
      }if(!result.items.length)list.append(node('p','还没有邀请码，先生成一个。','muted'));
    }catch(error){show(errorText(error));}
  }
  $('#refresh-invites').onclick=refresh;
  form.addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$('#message').hidden=true;
    try{const result=await api.request('/api/v1/auth/invites',{method:'POST',body:{label:form.elements.label.value.trim(),max_uses:Number(form.elements.max_uses.value),expires_days:Number(form.elements.expires_days.value)}});
      $('#invite-code').value=result.code;$('#register-url').value=new URL('/api/v1/auth/ui',window.location.origin).href;$('#new-invite').hidden=false;await refresh();
    }catch(error){show(errorText(error));}finally{button.disabled=false;}
  });
  $('#copy-invite').onclick=async()=>{const text=`Component Foundry 注册：${$('#register-url').value}\n邀请码：${$('#invite-code').value}`;
    try{await navigator.clipboard.writeText(text);show('邀请信息已复制。',true);}catch{$('#invite-code').select();show('浏览器暂不允许自动复制，请手动复制邀请码和注册地址。');}
  };
  await refresh();
}
if($('#register-form'))registration();else if($('#invite-admin'))admin();
