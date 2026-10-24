'use strict';
window.alliSecurity=null;
let activityCursor=null,activityQuery='';
async function loadAdminControls(){
 try{window.alliSecurity=await api('security');}catch(e){$('status').textContent=e.message;}
 const target=$('usage-list');target.replaceChildren();if(!window.alliSecurity)return;
 target.append(node('p','Saved-contact ceilings: 4 calendar changes per day, 8 per 7 days, 3 upcoming meetings; 5 cloud jobs per day, with shared model and calling budgets.','small'));
 for(const [kind,units] of Object.entries(window.alliSecurity.usage))target.append(item(kind.replaceAll('_',' '),units+' units in the past 24 hours'));
}
function closePerson(){$('person-dialog').close();}
function renderAdminContact(el,p){
 const status=personStatus(p),suspended=status==='Suspended'||status==='Deleted',actions=node('div','','item-actions');
 actions.append(button(status==='Deleted'?'Restore & reactivate':suspended?'Reactivate':'Suspend',async()=>{closePerson();await api('security/contacts',{phone:p.phone,action:suspended?'reactivate':'suspend'});await refresh();}));
 if(status!=='Deleted')actions.append(button('Delete',()=>{closePerson();editor('Delete '+(p.name||p.phone),[node('p','Move this person out of your active directory and suspend access. Their history and existing calendar meetings stay available to you. You can restore them from the Deleted filter.')],'Delete contact',()=>api('security/contacts',{phone:p.phone,action:'delete'}));}));
 if(status==='Approved')actions.append(button('Link existing meeting',()=>{closePerson();editor('Link a meeting for '+(p.name||p.phone),[field('bind-event','Google Calendar event ID','text','',true),node('p','Use an event you have checked belongs to this person. Matching a name alone does not grant access.','small')],'Link meeting',()=>api('security/meeting-link',{phone:p.phone,event_id:$('bind-event').value}));}));
 actions.append(button('View activity',()=>openPerson(p.phone)));el.append(actions);
}
let personCursor=null,personPhone='';
function conversationView(row){
 const box=node('div','','conversation-record');box.append(node('p',(row.channel||'Message')+' · '+(row.status||'')+' · '+date(row.created_at||row.send_at),'small'));
 if(row.body)box.append(item(row.channel==='sms'?'Incoming text':'Message',row.body));
 if(row.reply)box.append(item('Alli',row.reply));
 let previous=null;
 for(const part of row.transcript||[]){if(!part.text)continue;const role=part.role==='assistant'?'Alli':'Caller / group';if(previous?.role===role){previous.text+=part.text;previous.el.textContent=previous.text;}else{const el=node('p',part.text,'transcript-text'),entry=node('div','','transcript-entry');entry.append(node('strong',role),el);box.append(entry);previous={role,text:part.text,el};}}
 if(row.group_notes)box.append(node('p','Group notes: '+row.group_notes.status+'. Assignments use explicit introductions; the mixed phone line does not verify individual speakers.','small'));
 if(row.report){const report=node('details');report.append(node('summary','Call report'),node('pre',JSON.stringify(row.report,null,2),'tool-review'));box.append(report);}
 if(!row.body&&!row.reply&&!row.transcript?.length)box.append(node('pre',JSON.stringify(row,null,2),'tool-review'));
 return box;
}
function personActivity(row){
 const p=row.payload,box=node('details','','person-activity'),summary=node('summary');summary.append(node('span',p.summary||p.action||p.source||'Activity'),node('small',date(row.created_at)+(p.status?' · '+p.status:'')));box.append(summary);let loaded=false;
 box.ontoggle=async()=>{if(!box.open||loaded)return;loaded=true;const content=node('div','','activity-content');box.append(content);try{const sid=p.sid||p.request_id,wid=p.work_id||(p.request_id?.startsWith('work-result:')?p.request_id.slice(12):null);if(sid){const r=await api('activity/conversation/'+encodeURIComponent(sid));content.append(conversationView(r.conversation));}if(wid){const r=await api('activity/work/'+encodeURIComponent(wid));content.append(item(r.work.title,(typeof r.work.result==='string'?r.work.result.slice(0,2500):r.work.result?.summary)||r.work.error||r.work.state));const link=node('a','Open work & files ↗','quiet');link.href='/dashboard/work/'+encodeURIComponent(wid);link.target='_blank';link.rel='noopener';content.append(link);const receipts=node('details');receipts.append(node('summary','Execution receipts'),node('pre',JSON.stringify(r.events,null,2),'tool-review'));content.append(receipts);}const details=node('details');details.append(node('summary','Activity details'),node('pre',JSON.stringify(row,null,2),'tool-review'));content.append(details);}catch(e){content.append(node('p',e.message,'small'));}};return box;
}
async function openPerson(phone,more=false){
 const r=await api('people/'+encodeURIComponent(phone)+'?before='+(more?personCursor||0:0));personPhone=phone;personCursor=r.next_cursor;
 if(!more){const p={...state.people.find(x=>x.phone===phone),...r.person};$('person-title').textContent=p.name||p.phone;const target=$('person-body');target.replaceChildren();target.append(badge(personStatus(p)),node('p',p.phone+(p.email?' · '+p.email:''),'person-contact'),node('p',(p.last_activity?'Last conversation '+date(p.last_activity):'No recorded conversations yet')+' · '+(p.conversation_count||0)+' calls / texts · '+(p.work_count||0)+' work assignments','small'));const actions=node('div');actions.append(button('Edit contact',()=>{closePerson();editPerson(p);}));renderAdminContact(actions,p);target.append(actions,node('h3','Conversations & work'));const timeline=node('div');timeline.id='person-timeline';target.append(timeline);const moreButton=button('Load older activity',()=>openPerson(personPhone,true));moreButton.id='person-more';target.append(moreButton);}
 const timeline=$('person-timeline');for(const row of r.activities)timeline.append(personActivity(row));if(!timeline.children.length)empty(timeline,'No recorded activity yet. Their first call or text will appear here.');$('person-more').hidden=!personCursor;if(!$('person-dialog').open)$('person-dialog').showModal();
}
$('close-person').onclick=closePerson;
function inspectData(title,data){editor(title,[node('pre',JSON.stringify(data,null,2),'tool-review')],'Done',async()=>({ok:true}));}
async function loadActivity(reset=false){
 const target=$('full-activity');if(reset){activityCursor=null;target.replaceChildren();}
 const result=await api('activity?before='+(activityCursor||0)+'&q='+encodeURIComponent(activityQuery));
 for(const row of result.activities){
  const p=row.payload,box=item(p.summary||p.action||p.source||'Activity',(p.phone||p.contact_name||'Alli')+' · '+date(row.created_at),p.status),actions=node('div','','item-actions');
  actions.append(button('Details',()=>inspectData('Activity #'+row.id,row)));
  const sid=p.sid||p.request_id;if(sid)actions.append(button('Conversation / message',async()=>inspectData('Call / text',await api('activity/conversation/'+encodeURIComponent(sid)))));
  const wid=p.work_id||(p.request_id?.startsWith('work-result:')?p.request_id.slice(12):null);
  if(wid)actions.append(button('Work & receipts',async()=>inspectData('Work history',await api('activity/work/'+encodeURIComponent(wid)))));
  box.append(actions);target.append(box);
 }
 if(!target.children.length)empty(target,'No matching activity.');activityCursor=result.next_cursor;$('activity-more').hidden=!activityCursor;
}
$('activity-find').onclick=async()=>{try{activityQuery=$('activity-search').value;await loadActivity(true);}catch(e){$('status').textContent=e.message;}};
$('activity-search').onkeydown=e=>{if(e.key==='Enter')$('activity-find').click();};
$('activity-more').onclick=()=>loadActivity().catch(e=>$('status').textContent=e.message);
$('enable-owner-sms').onclick=async()=>{try{await api('security/sms-session',{enabled:true});$('status').textContent='Your owner texting session is verified for 12 hours.';}catch(e){$('status').textContent=e.message;}};
$('revoke-owner-sms').onclick=async()=>{try{await api('security/sms-session',{enabled:false});$('status').textContent='Private texting access ended.';}catch(e){$('status').textContent=e.message;}};

$('edit-public-profile').onclick=async()=>{try{const r=await api('profile');editor('What Alli may share about John',[field('public-profile-body','Shareable background','textarea',r.body,true),node('p','Alli may tell callers and texters this information. Keep private account, family and financial details out.','small')],'Save public profile',()=>api('profile',{body:$('public-profile-body').value}));}catch(e){$('status').textContent=e.message;}};
