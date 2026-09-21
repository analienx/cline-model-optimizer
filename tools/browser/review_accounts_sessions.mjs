import {spawn} from 'node:child_process';
import fs from 'node:fs';import os from 'node:os';import path from 'node:path';
const base=process.argv[2]||'http://127.0.0.1:4396',port=9461;
const wait=ms=>new Promise(r=>setTimeout(r,ms));
const chrome=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',[
 '--headless=new','--no-first-run','--disable-extensions',`--remote-debugging-port=${port}`,
 `--user-data-dir=${fs.mkdtempSync(path.join(os.tmpdir(),'cmo-account-session-'))}`,'about:blank'],{stdio:'ignore'});
let ws;
try{
 for(let i=0;i<80;i++){try{if((await fetch(`http://127.0.0.1:${port}/json/version`)).ok)break;}catch{}await wait(150);}
 const target=await(await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(base+'/')}`,{method:'PUT'})).json();
 ws=new WebSocket(target.webSocketDebuggerUrl);
 await new Promise((ok,bad)=>{ws.addEventListener('open',ok,{once:true});ws.addEventListener('error',bad,{once:true});});
 let id=0;const pending=new Map();ws.addEventListener('message',e=>{
  const m=JSON.parse(e.data);if(pending.has(m.id)){const [ok,bad]=pending.get(m.id);pending.delete(m.id);
   m.error?bad(Error(JSON.stringify(m.error))):ok(m.result);}});
 const send=(method,params={})=>new Promise((ok,bad)=>{const k=++id;pending.set(k,[ok,bad]);ws.send(JSON.stringify({id:k,method,params}));});
 const evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
  if(r.exceptionDetails)throw Error(r.exceptionDetails.text);return r.result.value;};
 await send('Page.enable');await send('Runtime.enable');
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 await send('Page.navigate',{url:base+'/'});
 for(let i=0;i<80;i++){if(await evaluate("document.querySelectorAll('.account-item').length===4 && document.querySelectorAll('.work-item').length===3"))break;await wait(150);}
 for(let i=0;i<70;i++){if(await evaluate("state.usage['account-1']?.plan_status==='active'"))break;await wait(160);}
 const accountUsage=await evaluate(`({
  plan:state.usage['account-1']?.plan_status,
  week:state.usage['account-1']?.windows?.weekly?.percent_used,
  account4:state.usage['account-4']?.plan_status,
  genericLink:!!document.querySelector('a[href="https://app.cline.bot/dashboard"]'),
  perAccountButtons:document.querySelectorAll('.account-usage button').length
 })`);
 await evaluate("document.querySelector('.work-item button')?.click()");
 for(let i=0;i<70;i++){if(await evaluate("document.querySelector('.work-item .goal-detail')?.textContent.includes('Resume preflight')"))break;await wait(160);}
 const details=await evaluate(`({
  visible:!!document.querySelector('.work-item .goal-detail'),
  attempts:document.querySelector('.work-item .goal-detail')?.textContent.includes('Last attempt:'),
  preflight:document.querySelector('.work-item .goal-detail')?.textContent.includes('Resume preflight'),
  nextRoute:document.querySelector('.work-item .goal-detail')?.textContent.includes('Next free route:')
 })`);
 await evaluate("document.querySelectorAll('.account-item')[3].querySelector('.account-remove-trigger').click()");
 for(let i=0;i<35;i++){if(await evaluate("!!document.querySelectorAll('.account-item')[3].querySelector('.account-remove-review button')"))break;await wait(100);}
 await wait(5500);
 const removal=await evaluate(`({preview:!!document.querySelectorAll('.account-item')[3].querySelector('.account-remove-review'),
  confirmation:document.querySelectorAll('.account-item')[3].querySelector('.account-remove-review')?.textContent.includes('Confirm removal'),
  accountCount:state.snap.policy.accounts.length})`);
 await evaluate("[...document.querySelectorAll('.account-item')[3].querySelectorAll('.account-remove-review button')].find(b=>b.textContent.trim()==='Cancel')?.click()");
 await evaluate(`(()=>{const previous=window.fetch;window.fetch=async(url,opts)=>{
  if(url==='/api/simple/resume'){
   window.__resumeRequest=JSON.parse(opts.body);
   return new Response(JSON.stringify({ok:true,status:'requested',message:'Launcher request accepted; awaiting Goal handshake.'}),
    {status:202,headers:{'Content-Type':'application/json'}});
  }return previous(url,opts);};return true;})()`);
 await evaluate("document.querySelector('.work-item .work-right button.primary')?.click()");
 for(let i=0;i<70;i++){if(await evaluate("document.querySelector('.work-item .goal-ready,.work-item .goal-warning')?.textContent.includes('resume')"))break;await wait(160);}
 const resume=await evaluate(`({
  request:window.__resumeRequest||null,
  feedback:document.querySelector('.work-item .goal-ready,.work-item .goal-warning')?.textContent||'',
  detail:!!document.querySelector('.work-item .goal-detail')
 })`);
 const overflowNodes=await evaluate("[...document.querySelectorAll('*')].filter(e=>e.getBoundingClientRect().right>document.documentElement.clientWidth+4).slice(0,18).map(e=>({tag:e.tagName,cls:e.className,id:e.id,right:Math.round(e.getBoundingClientRect().right),width:Math.round(e.getBoundingClientRect().width)}))");
 console.log('OVERFLOW_NODES',JSON.stringify(overflowNodes));
 const layout=await evaluate("({width:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})");
 const saved=await(await fetch(base+'/api/snapshot')).json();
 const pass=!!accountUsage.plan&&!!accountUsage.account4&&!accountUsage.genericLink&&
  accountUsage.perAccountButtons===4&&details.visible&&details.attempts&&details.preflight&&
  details.nextRoute&&removal.preview&&removal.confirmation&&removal.accountCount===4&&
  saved.policy.accounts.length===4&&layout.width<=layout.client&&resume.feedback.length>15&&resume.detail;
 console.log(JSON.stringify({pass,accountUsage,details,removal,resume,layout,liveAccountCount:saved.policy.accounts.length},null,2));
 process.exitCode=pass?0:1;
}catch(e){console.error('ACCOUNTS_SESSIONS_BROWSER_ERROR',String(e));process.exitCode=2;}
finally{ws?.close();chrome.kill();}
