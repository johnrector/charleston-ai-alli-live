'use strict';
let workData=null,workLoading=false;
Object.assign(labels,{running:'Working',waiting:'Waiting for a reply',needs_connection:'Connect account',awaiting_authorization:'Awaiting authorization'});
async function loadAlliWork(){
 if(workLoading)return;workLoading=true;
 try{workData=await api('work');renderAlliWork();}catch(e){$('status').textContent=e.message;}finally{workLoading=false;}
}
function renderAlliWork(){
 if(!workData)return;
 const target=$('work-list');target.replaceChildren();
 for(const w of workData.work){
  const box=node('article','','promise-card'),top=node('div','','card-top');top.append(node('span',w.contact_name,'eyebrow'),badge(w.state));
  box.append(top,node('h3',w.title),node('p',w.instruction,'work-brief'),node('p',date(w.created_at)+' · '+w.id,'small'));
  if(w.error)box.append(node('p',w.error,'work-error'));
  if(w.delivery_status)box.append(node('p','Result text: '+w.delivery_status,'small'));
  const actions=node('div','','item-actions');
  if(w.result_url){const a=node('a','Open finished work ↗','quiet');a.href=w.result_url;a.target='_blank';a.rel='noreferrer noopener';actions.append(a);}
  if(w.approval&&['needs_john','waiting'].includes(w.state)){
   box.append(node('p',w.approval.question));
   if(w.approval.kind==='tool'){
    box.append(node('pre',JSON.stringify(w.approval.arguments,null,2),'tool-review'));
    actions.append(button('Approve this exact action',async()=>{await api('work/'+w.id,{action:'approve'});await loadAlliWork();}),button('Decline action',async()=>{await api('work/'+w.id,{action:'deny'});await loadAlliWork();}));
   }else actions.append(button('Supply answer & continue',()=>editor('Continue '+w.title,[field('work-answer',w.approval.question,'textarea','',true)],'Continue work',async()=>{const r=await api('work/'+w.id,{action:'answer',answer:$('work-answer').value});await loadAlliWork();return r;})));
  }
  if(!['completed','cancelled'].includes(w.state))actions.append(button('Cancel work',async()=>{await api('work/'+w.id,{action:'cancel'});await loadAlliWork();}));
  box.append(actions);target.append(box);
 }
 if(!workData.work.length)empty(target,'No delegated work yet. Friends can call or text Alli with something to research, write or create.');
 const r=workData.runtime;
 $('runtime-status').textContent=r.connected?'Connected · '+r.tool_count+' account tools available in the cloud.':r.error||r.status;
 $('connect-cloud').hidden=r.connected;$('refresh-cloud').disabled=['starting','waiting_for_previous_instance'].includes(r.status);
 const apps=$('cloud-apps');apps.replaceChildren();
 for(const a of r.apps){const row=node('div','','cloud-app');row.append(node('strong',a.runtimeName||a.id),badge(a.enabled&&a.callable?'Connected':'Unavailable'));apps.append(row);}
 $('cloud-login').hidden=!r.login;
 if(r.login){
  const box=$('cloud-login');box.replaceChildren(node('p','Authorize this separate cloud runtime using the one-time device code. This does not use your Mac as a relay.'));
  box.append(node('strong',r.login.userCode,'device-code'));
  const a=node('a','Open account authorization ↗','quiet');
  if(/^https:\/\/(auth\.openai\.com|chatgpt\.com)\//.test(r.login.verificationUrl)){a.href=r.login.verificationUrl;a.target='_blank';a.rel='noreferrer noopener';box.append(a);}
 }
 const skills=workData.skills.skills;$('skills-status').textContent=skills.length+' skill workflows in the private cloud snapshot. Their tools and runtime requirements still apply.';
 const list=$('cloud-skills');list.replaceChildren();
 for(const s of skills){const d=node('details'),summary=node('summary',s.name);d.append(summary,node('p',s.description,'small'));list.append(d);}
}
$('delegate-work').onclick=()=>{
 const intent=crypto.randomUUID();const choices=[['','For John'],...state.people.filter(p=>p.source!=='self_introduced').map(p=>[p.phone,(p.name||p.phone)+' · '+p.phone])];
 const notify=node('input');notify.id='work-notify';notify.type='checkbox';notify.checked=true;const label=node('label','Text the result to this person');label.htmlFor=notify.id;
 editor('Delegate work',[field('work-title','Title','text','',true),selectField('work-person','Who is this for?',choices),field('work-instruction','What should Alli accomplish?','textarea','',true),label,notify,node('p','Work runs in the cloud. A friend’s work uses their own context; account access remains private to John.','small')],'Start cloud work',async()=>{
  const result=await api('work',{title:$('work-title').value,instruction:$('work-instruction').value,phone:$('work-person').value,intent_id:intent,notify:$('work-notify').checked});await loadAlliWork();return result;
 });
};
$('connect-cloud').onclick=async()=>{try{await api('cloud/login',{});await loadAlliWork();}catch(e){$('status').textContent=e.message;}};
$('refresh-cloud').onclick=async()=>{try{await api('cloud/refresh',{});await loadAlliWork();}catch(e){$('status').textContent=e.message;}};
$('skill-import').onchange=async()=>{
 const file=$('skill-import').files[0];if(!file)return;
 if(file.size>20*1024*1024){$('status').textContent='Use a skill archive under 20 MB.';return;}
 $('skill-import').disabled=true;
 try{
  const response=await fetch('/dashboard/api/skills/import',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/zip','X-CSRF-Token':csrf},body:file});
  const result=await response.json();if(!response.ok)throw Error(result.detail||'Skill import failed');
  await loadAlliWork();$('status').textContent='Private skill snapshot imported.';
 }catch(e){$('status').textContent=e.message;}finally{$('skill-import').disabled=false;}
};
setInterval(()=>{if(state&&(view==='work'||view==='tools')&&!$('editor').open)loadAlliWork();},10000);
