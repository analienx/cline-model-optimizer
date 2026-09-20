"use strict";
const $ = id => document.getElementById(id);
const state = {snap:null, catalog:[], selected:[], saved:[], dirty:false, loading:false, connected:false, readiness:{},setupHelp:null};
const el = (tag,cls,text) => {const x=document.createElement(tag);if(cls)x.className=cls;if(text!==undefined)x.textContent=String(text);return x;};
const clear = n => {n.replaceChildren();return n;};
const api = async (url, opts={}) => {const response=await fetch(url,{cache:'no-store',...opts});const data=await response.json();if(!response.ok)throw Error(data.error||`HTTP ${response.status}`);return data;};
const send = (url,data) => api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
const names = id => {const known={'cline-free/muse-spark-1.3-contributor':'Muse Spark 1.3','z-ai/glm-5.3-flash':'GLM 5.3 Flash','cline-free/deepseek-v4.1-flash':'DeepSeek V4.1 Flash'};return known[id]||id.split('/').pop().replace(/-/g,' ');};
const ago = ms => {if(!Number.isFinite(ms)||ms<=0)return 'No verified activity';const m=Math.max(0,Math.floor((Date.now()-ms)/60000));return m<1?'Just now':m<60?`${m} min ago`:m<1440?`${Math.floor(m/60)} h ago`:`${Math.floor(m/1440)} d ago`;};
function tell(message,error=false){const node=$('message');node.textContent=message;node.className=error?'message error':'message';node.hidden=false;}
function makeButton(text,callback,disabled=false){const b=el('button','small',text);b.type='button';b.disabled=disabled;b.addEventListener('click',callback);return b;}
function goalName(goal){if(goal.goal_id==='1452826a-661e-4a0d-b88a-6054c2de5a25')return 'Shiftio';if(goal.goal_id==='1c2ccdda-855a-4d2c-b75d-4897b31ae8d1')return 'CMO dashboard';if(goal.goal_id==='goal-cmo-trustworthy-20260919')return 'CMO migration · legacy';return goal.repo?.split('/').pop()||'Unidentified saved Goal';}
async function resumeGoal(goalId){
 try{const result=await send('/api/simple/resume',{goal_id:goalId});
 tell(result.message||'Resume requested. Checking the Goal handshake; execution is not yet confirmed.');
 await load(true);
 }catch(error){tell('Resume not started: '+error.message,true);}
}
async function explainGoal(goalId){
 try{const snap=await api('/api/snapshot?goal_id='+encodeURIComponent(goalId));
 const d=snap.decision||{};const r=d.route||{};
 tell((d.action==='BLOCKED'?'Blocked: ':'Next routing decision: ')+(d.reason||'No verified route evidence')+
 (r.model?' · '+names(r.model)+' / '+r.account_alias:''),d.action==='BLOCKED');
 }catch(error){tell('Goal details unavailable: '+error.message,true);}
}
function renderWork(snap){const box=clear($('work'));const goals=snap.goals||[];
 if(!goals.length){box.append(el('p','muted','No saved projects yet.'));return;}
 for(const goal of goals){const row=el('div','work-item'),main=el('div','item-main'),right=el('div','work-right'),actions=el('div','item-actions');
 const last=Number(goal.last_activity_at||0),age=Date.now()-last;
 let status='Activity unverified';if(['blocked','failed'].includes(goal.status))status='Blocked';else if(goal.status==='completed')status='Completed';
 else if(goal.status==='paused')status='Paused';else if(goal.liveness==='disconnected')status='Disconnected';
 else if(goal.liveness==='live'&&last>0&&age<90000)status='Recent telemetry';
 const name=goalName(goal),flag=el('span','work-status '+(['Blocked','Disconnected'].includes(status)?'bad':status==='Completed'?'good':''),status);
 main.append(el('strong','',name),el('small','',`Last recorded ${ago(last)} · ${status==='Recent telemetry'?'executor not independently verified':goal.status||'saved'}`));
 if(name==='CMO dashboard'&&status==='Disconnected')main.append(el('div','work-hint','Coding session stopped during takeover; saved history is intact.'));
 actions.append(makeButton('Details',()=>explainGoal(goal.goal_id)));
 if(name==='Shiftio'&&['Blocked','Paused','Disconnected','Activity unverified'].includes(status)){
 const b=makeButton('Check & resume',()=>resumeGoal(goal.goal_id));b.classList.add('primary');actions.append(b);}
 right.append(flag,actions);row.append(main,right);box.append(row);}
}
async function recheckRoute(cell){try{await send('/api/route/recheck',{route_key:cell.route_key,account_alias:cell.account_alias,provider:cell.provider,model:cell.model,tier:cell.tier,reason:'User requested one bounded free-route recheck'});tell('One bounded recheck queued. A refreshed state will appear after provider evidence arrives.');await load(true);}catch(error){tell('Recheck refused: '+error.message,true);}}
function renderRouter(snap){const box=clear($('route-status'));
 const matrix=new Map((snap.free_matrix||[]).map(r=>[r.model,r]));
 const models=(snap.policy.routes||[]).filter(r=>r.tier==='free'&&r.enabled!==false).slice(0,4);
 const accounts=[...(snap.policy.accounts||[])].filter(a=>a.enabled!==false).sort((a,b)=>(a.priority??0)-(b.priority??0));
 if(!models.length){box.append(el('p','muted','No free model selected. Open Change models & priority.'));return;}
 let allVerified=0,allRoutes=0,allExpired=0,allCooling=0;
 for(const [index,route] of models.entries()){
 const row=el('div','route-item'),title=el('div','route-title'),position=el('span','route-position',String(index+1)),text=el('div','route-overview');
 text.append(el('strong','',names(route.model)));title.append(position,text);
 const evidence=matrix.get(route.model);const enabled=accounts.filter(a=>route.accounts.includes(a.id));
 let verified=0,cooling=0,needsCheck=0,auth=0;const cells=[];
 for(const account of enabled){const cell=evidence?.accounts?.[account.id],status=cell?.state||'UNKNOWN';let label='Not yet verified',kind='unknown';
 if(status==='AVAILABLE'&&cell?.freshness==='fresh'){verified++;label='Verified recently';kind='good';}
 else if(status==='QUOTA'&&(!cell.reset_at||Number(cell.reset_at)>Date.now())){cooling++;label=cell.reset_at?'Quota until '+new Date(cell.reset_at).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'Quota exhausted · reset unknown';kind='bad';}
 else if(status==='AUTH_BLOCKED'){auth++;label='Sign-in required';kind='bad';}
 else if(status==='QUOTA_EXPIRED'||status==='QUOTA'&&cell?.reset_at&&Number(cell.reset_at)<=Date.now()){needsCheck++;label='Cooldown passed · needs recheck';}
 else if(status==='AVAILABLE'){needsCheck++;label='Success too old · needs recheck';}
 else if(status==='TRANSIENT'){needsCheck++;label='Temporary provider failure';}
 else if(status==='CAPABILITY_UNAVAILABLE'){label='Model unavailable';kind='bad';}
 const chip=el('span','route-cell '+kind,`${account.id.replace('account-','Account ')} · ${label}`);
 if(cell?.observed_at)chip.title='Observed '+ago(Number(cell.observed_at));
 if(cell&&(status==='QUOTA_EXPIRED'||status==='QUOTA'&&cell.reset_at&&Number(cell.reset_at)<=Date.now()))chip.append(makeButton('Recheck',()=>recheckRoute(cell)));
 cells.push(chip);}
 allVerified+=verified;allRoutes+=enabled.length;allExpired+=needsCheck;allCooling+=cooling;
 const blockedCount=cooling,unavailable=auth+cooling;
 const severity=verified?'model-ready':cooling===enabled.length&&enabled.length?'model-exhausted':
   unavailable&&unavailable<enabled.length?'model-partial':needsCheck?'model-recheck':'model-unknown';
 row.classList.add(severity);
 let summary=verified?`${verified} of ${enabled.length} accounts recently verified`:
   cooling===enabled.length&&enabled.length?`All ${enabled.length} accounts quota-blocked`:
   unavailable?`${unavailable} of ${enabled.length} accounts blocked | others need checking`:
   needsCheck?`${needsCheck} of ${enabled.length} need recheck`:'No account availability verified';
 text.append(el('em','',summary));const markers=el('div','route-markers');
 for(const account of enabled){const cell=evidence?.accounts?.[account.id],current=cell?.state;
 const readiness=state.readiness[account.id];const cls=readiness&&!readiness.cline_saved?'unverified':
   current==='AVAILABLE'&&cell?.freshness==='fresh'?'good':
   current==='QUOTA'&&(!cell.reset_at||Number(cell.reset_at)>Date.now())?'bad':
   current==='AUTH_BLOCKED'?'bad':current==='QUOTA_EXPIRED'?'pending':'unverified';
 const dot=el('span','route-marker '+cls,account.id.replace('account-','A'));
 dot.title=`${account.id}: ${readiness&&!readiness.cline_saved?'Pi Cline sign-in missing':
   cls==='good'?'Recently verified':cls==='bad'?'Blocked':cls==='pending'?'Needs recheck':'Unknown'}`;
 dot.setAttribute('aria-label',dot.title);markers.append(dot);}
 text.append(markers);const detail=el('details','route-details'),toggle=el('summary','',`Accounts ${enabled.length}  |  Details`),content=el('div','route-expanded');
 if(cells.length)content.append(...cells);else content.append(el('span','muted','No enabled account assigned to this model.'));
 detail.append(toggle,content);row.append(title,detail);box.append(row);}
 $('routing-summary').textContent=allVerified?`${allVerified} of ${allRoutes} account-model routes verified recently`:`No free route verified now · ${allCooling} cooling down · ${allExpired} ready to recheck`;
}
function renderModels(){const selected=clear($('selected')),choices=clear($('choices'));$('model-count').textContent=`${state.selected.length} / 4`;
if(!state.selected.length)selected.append(el('p','muted','Choose your first free model from the list.'));
state.selected.forEach((model,index)=>{const row=el('div','model-item'),title=el('div','item-main'),buttons=el('div','item-actions');title.append(el('strong','',`${index+1}. ${names(model)}`),el('small','',model));
buttons.append(makeButton('↑',()=>reorder(index,-1),index===0),makeButton('↓',()=>reorder(index,1),index===state.selected.length-1),makeButton('Remove',()=>{state.selected.splice(index,1);changed();}));row.append(title,buttons);selected.append(row);});
for(const model of state.catalog){const option=el('label','choice'),check=el('input');check.type='checkbox';check.checked=state.selected.includes(model);check.disabled=!check.checked&&state.selected.length>=4;
check.addEventListener('change',()=>{if(check.checked){if(state.selected.length>=4){check.checked=false;return;}state.selected.push(model);}else state.selected=state.selected.filter(m=>m!==model);changed();});
option.append(check,el('span','',names(model)));choices.append(option);}
if(!state.catalog.length)choices.append(el('p','muted','Free-model catalog unavailable. Saved selections remain visible.'));
$('save-models').disabled=!state.dirty||!state.selected.length||!state.connected;
$('draft-status').textContent=state.dirty?'Unsaved changes':'Saved preferences';}
function reorder(index,delta){const next=index+delta;if(next<0||next>=state.selected.length)return;[state.selected[index],state.selected[next]]=[state.selected[next],state.selected[index]];changed();}
function changed(){state.dirty=JSON.stringify(state.selected)!==JSON.stringify(state.saved);renderModels();}
async function saveModels(){if(state.selected.length<1||state.selected.length>4)return tell('Choose one to four free models.',true);
$('save-models').disabled=true;try{await send('/api/simple/models',{models:state.selected,expected_digest:state.snap.policy.digest});state.saved=[...state.selected];state.dirty=false;tell('Model priorities saved. Existing Pi attempts are not interrupted.');await load(true);}catch(e){tell('Could not save: '+e.message,true);renderModels();}}
async function loadCatalog(){try{const result=await api('/api/simple/catalog');state.catalog=[...new Set([...result.selected,...result.free])];$('catalog-note').textContent=result.ok?'Only verified free models can be added. Availability is checked separately.':result.message;}
catch(e){state.catalog=[...state.saved];$('catalog-note').textContent='Catalog not reachable. Existing choices are preserved.';}renderModels();}
function accountSetupCommand(id,stage){
 if(!/^account-[1-5]$/.test(id))throw Error('Invalid account slot');
 const helper=stage==='provision'?'Provision-PiClineAccount.ps1':'Initialize-PiClineAccount.ps1';
 return 'powershell -NoProfile -File "' + String.raw`C:\Workspace\repos\config\tools\pi` + '\\' + helper + '" -Account ' + id;
}
async function copySetupCommand(id,stage,button,guide,feedback){
 guide.open=true;
 const field=guide.querySelector('.account-command');
 let outcome='manual';
 try{
  const response=await send('/api/simple/accounts/copy-command',{id,stage});
  if(response.ok&&response.confirmed)outcome='verified';
 }catch(error){
  feedback.title='The Windows clipboard could not be verified: '+error.message;
 }
 state.setupHelp={id,stage,outcome};
 showSetupFeedback(stage,button,feedback,outcome);
 if(outcome!=='verified'){
  guide.querySelector('summary').textContent='Command & manual copy';
  field.focus();field.select();
 }
}
async function startAccountSignin(id,button,feedback,guide){
 button.disabled=true;
 feedback.hidden=false;feedback.textContent='Opening a Windows sign-in terminal…';
 try{
  const result=await send('/api/simple/accounts/start-signin',{id});
  feedback.textContent=result.already_open?'Your sign-in terminal is already open on Windows. Finish login there, then select Refresh setup.':
   'Sign-in terminal opened on Windows. Complete the browser login there, then select Refresh setup. Account connection is not verified yet.';
  button.textContent=result.already_open?'Terminal already open':'Sign-in terminal opened';
  state.signinHelp={id,message:feedback.textContent};
 }catch(error){
  feedback.textContent='Could not open the Windows sign-in terminal: '+error.message+'. Use Copy command instead.';
  button.textContent='Try opening sign-in again';button.disabled=false;
  guide.open=true;
 }
}
function showSetupFeedback(stage,button,feedback,outcome){
 button.textContent=outcome==='verified'?'Copied to Windows':'Select command';
 feedback.textContent=outcome==='verified'?'Windows text clipboard checked. Open PowerShell, paste the command and complete sign-in. Then refresh setup.':
  'Windows clipboard copy failed. The full command is selected below. Press Ctrl+C or use Copy from the selection menu, then paste into PowerShell.';
 feedback.hidden=false;
}
function renderAccounts(snap){const box=clear($('accounts'));
 const accounts=[...(snap.policy.accounts||[])].sort((a,b)=>(a.priority??0)-(b.priority??0));
 $('account-count').textContent=`${accounts.length} / 5`;const add=accounts.length<5;
 $('new-email').disabled=!add;$('add-account').querySelector('button').disabled=!add;
 for(const [index,account] of accounts.entries()){
 const row=el('div','account-item'),avatar=el('span','account-avatar',String(index+1)),main=el('div','item-main');
 const hasEmail=account.label&&account.label.includes('@');const ready=state.readiness[account.id];
 const profileState=!ready?'Profile not checked':!ready.profile_exists?'Pi profile missing':
   !ready.cline_saved?'Pi sign-in missing':'Pi sign-in saved | live login and email unverified';
 const routeState=account.enabled===false?'Not in routing':ready&&!ready.cline_saved?'Enabled in preferences | NOT route-ready':'Enabled in preferences | live route unverified';
 main.append(el('strong','',hasEmail?account.label:'Email not linked'),el('small','',`${account.id} | ${routeState} | ${profileState}`));
 const pass=el('div','pass-line');const passMessage=!ready?'ClinePass status not checked':!ready.profile_exists?'ClinePass: profile not created':
   ready.pass_saved?'ClinePass sign-in saved | plan and usage not synced here':'ClinePass not signed in to this Pi profile';
 pass.append(el('span','',passMessage));
 if(ready?.pass_saved){const link=el('a','pass-link','View 5h / weekly / monthly usage');link.href='https://app.cline.bot/dashboard';link.target='_blank';link.rel='noopener noreferrer';link.title='Open Cline dashboard; select this same account to view its live limits';pass.append(link);}
 main.append(pass);
 if(ready&&!ready.cline_saved){
  const stage=ready.profile_exists?'signin':'provision';
  const connect=el('div','account-connect'),label=el('div','connect-label',stage==='signin'?'Next step: sign in to Pi':'Next step: create your Pi profile');
  const guide=el('details','account-guide'),sum=el('summary','',stage==='signin'?'Copy command instead':'Show command & instructions'),body=el('div','account-guide-body');
  const feedback=el('p','account-copy-feedback');feedback.setAttribute('role','status');feedback.setAttribute('aria-live','polite');feedback.hidden=true;
  const command=el('textarea','account-command');command.value=accountSetupCommand(account.id,stage);command.readOnly=true;command.rows=3;command.spellcheck=false;command.setAttribute('aria-label','Setup command for '+account.id);
  body.append(el('p','',stage==='signin'?'If the sign-in window does not open, use this manual fallback on the Windows computer running CMO.':'On your Windows computer, run this setup command in PowerShell. This creates only the isolated profile for this account.'),command);
  body.append(makeButton('Refresh setup',()=>load(true)));
  body.append(el('p','account-device-note','Copy command writes to the Windows computer running this local dashboard, not to your phone clipboard.'));
  body.append(el('p','account-setup-hint',stage==='signin'?'A saved sign-in does not prove the email identity or free-model quota. Check availability separately.':'Once the profile is created, Refresh setup will show the separate sign-in step.'));
  guide.append(sum,body);
  const button=makeButton(stage==='signin'?'Start sign-in on Windows':'Copy setup command',
   ()=>stage==='signin'?startAccountSignin(account.id,button,feedback,guide):copySetupCommand(account.id,stage,button,guide,feedback));
  if(stage==='signin')body.insertBefore(makeButton('Copy command',()=>copySetupCommand(account.id,stage,copyButton,guide,feedback)),command);
  const copyButton=stage==='signin'?body.querySelector('button'):null;
  button.classList.add('primary','account-connect-button');button.setAttribute('aria-label',`${button.textContent} for ${account.id}`);
  if(state.signinHelp?.id===account.id&&stage==='signin'){
   feedback.hidden=false;feedback.textContent=state.signinHelp.message;button.textContent='Sign-in terminal requested';
  }else if(state.setupHelp?.id===account.id&&state.setupHelp.stage===stage){
   guide.open=true;showSetupFeedback(stage,stage==='signin'?copyButton:button,feedback,state.setupHelp.outcome);
  }

  connect.append(label,button,feedback,guide);main.append(connect);
 }
 const manage=el('details','account-actions'),summary=el('summary','','Manage'),menu=el('div','account-menu');summary.setAttribute('aria-label','Manage '+account.id);
 menu.append(makeButton('↑ Priority',()=>moveAccount(accounts,index,-1),index===0),makeButton('↓ Priority',()=>moveAccount(accounts,index,1),index===accounts.length-1));
 menu.append(makeButton('Set email',()=>editEmail(account,main)),makeButton('Check profile',()=>verifyAccount(account.id)));
 menu.append(makeButton(account.enabled===false?'Enable':'Disable',()=>toggleAccount(account)));
 manage.append(summary,menu);row.append(avatar,main,manage);box.append(row);
 }
}
async function mutateAccount(body){const result=await send('/api/accounts',body);tell(`Account ${body.id} updated. Existing Pi attempts are not interrupted.`);await load(true);return result;}
function editEmail(account,main){if(main.querySelector('form'))return;const form=el('form','email-editor'),input=el('input','email-input');input.type='email';input.required=true;input.maxLength=64;input.autocomplete='off';input.placeholder='Email label for this Pi profile';input.value=account.label?.includes('@')?account.label:'';
const button=el('button','secondary','Save email');button.type='submit';form.append(input,button);form.addEventListener('submit',async ev=>{ev.preventDefault();try{await mutateAccount({op:'update',id:account.id,label:input.value.trim(),note:'user supplied account email label'});}catch(e){tell('Email could not be saved: '+e.message,true);}});main.append(form);input.focus();}
async function verifyAccount(id){try{const result=await send('/api/accounts',{op:'verify',id});const status=result.check?.status||'unknown';tell(`${id}: ${status==='tracked'?'Pi profile missing. Create a separate Pi profile and sign in there first.':status==='verified'?'Pi auth files found. Live login and email identity are not yet verified.':'Pi profile exists, but sign-in is not complete.'}`);await load(true);}catch(e){tell('Profile check failed: '+e.message,true);}}
async function toggleAccount(account){try{await mutateAccount({op:'update',id:account.id,enabled:account.enabled===false,note:'simple dashboard account toggle'});}catch(e){tell('Cannot update account: '+e.message,true);}}
async function moveAccount(accounts,index,delta){const other=accounts[index+delta];if(!other)return;
try{const doc=JSON.parse(JSON.stringify(state.snap.policy));const mine=doc.accounts.find(a=>a.id===accounts[index].id),theirs=doc.accounts.find(a=>a.id===other.id);[mine.priority,theirs.priority]=[theirs.priority,mine.priority];
await send('/api/policy',{doc,expected_digest:state.snap.policy.digest,note:'simple dashboard account priority'});tell('Account priority saved for later route decisions.');await load(true);}catch(e){tell('Account priority could not be saved: '+e.message,true);}}
async function addAccount(event){event.preventDefault();const email=$('new-email').value.trim();const accounts=state.snap.policy.accounts||[];if(accounts.length>=5)return tell('Maximum five accounts.',true);
const used=new Set(accounts.map(a=>a.id));let id='';for(let i=1;i<=5;i++){if(!used.has(`account-${i}`)){id=`account-${i}`;break;}}if(!id)return tell('No account slot available.',true);
try{await mutateAccount({op:'add',id,label:email,profile:id,enabled:false,attach_routes:state.saved,note:'simple dashboard add tracked account'});$('new-email').value='';tell(`${id} added as a disabled slot. Use Copy setup command on the new account card. After creating its profile, select Refresh setup to get the sign-in step.`);}catch(e){tell('Account could not be added: '+e.message,true);}}
async function load(preserve=false){if(state.loading)return;state.loading=true;try{const [snap,readiness]=await Promise.all([api('/api/snapshot'),api('/api/simple/accounts/readiness')]);state.snap=snap;state.readiness=Object.fromEntries((readiness.accounts||[]).map(a=>[a.id,a]));state.connected=true;const status=$('connection');status.textContent='Connected';status.className='status good';renderRouter(snap);renderWork(snap);renderAccounts(snap);
const saved=(snap.policy.routes||[]).filter(r=>r.tier==='free'&&r.enabled!==false).slice(0,4).map(r=>r.model);if(!state.dirty||!preserve){state.selected=[...saved];state.saved=[...saved];state.dirty=false;}else{state.saved=[...saved];state.dirty=JSON.stringify(state.selected)!==JSON.stringify(saved);}state.catalog=[...new Set([...state.catalog,...saved])];renderModels();}
catch(e){state.connected=false;const status=$('connection');status.textContent='Disconnected';status.className='status bad';tell('Connection lost. Saved information may be stale. Changes are disabled until the service responds.',true);$('save-models').disabled=true;}finally{state.loading=false;}}
$('refresh').addEventListener('click',()=>load(true));$('save-models').addEventListener('click',saveModels);$('add-account').addEventListener('submit',addAccount);
load().then(loadCatalog);setInterval(()=>load(true),15000);
