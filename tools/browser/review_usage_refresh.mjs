import {spawn} from 'node:child_process';
import fs from 'node:fs';import os from 'node:os';import path from 'node:path';
const base=process.argv[2]||'http://127.0.0.1:4396',port=9469,wait=ms=>new Promise(r=>setTimeout(r,ms));
const chrome=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',[
 '--headless=new','--no-first-run','--disable-extensions',`--remote-debugging-port=${port}`,
 `--user-data-dir=${fs.mkdtempSync(path.join(os.tmpdir(),'cmo-refresh-review-'))}`,'about:blank'],{stdio:'ignore'});
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
 for(let i=0;i<80;i++){if(await evaluate("document.querySelectorAll('.account-usage button').length===4"))break;await wait(150);}
 const before=await(await fetch(base+'/api/snapshot')).json();
 const result=await evaluate(`(async()=>{
  const original=window.fetch;
  window.fetch=async(url,opts)=>{
   if(url==='/api/simple/accounts/refresh-usage'){
    window.__renewalRequest={method:opts.method,body:JSON.parse(opts.body)};
    return new Response(JSON.stringify({account_alias:'account-4',plan_status:'active',
     usage_status:'available',windows:{'5h':{percent_used:12},weekly:{percent_used:34},monthly:{percent_used:56}}}),
     {status:200,headers:{'Content-Type':'application/json'}});
   }
   return original(url,opts);
  };
  const fourth=document.querySelectorAll('.account-item')[3];
  fourth.querySelector('.account-usage button').click();
  for(let i=0;i<50;i++){
   if(window.__renewalRequest&&state.usage['account-4']?.plan_status==='active')break;
   await new Promise(ok=>setTimeout(ok,100));
  }
  return {request:window.__renewalRequest||null,
    displayed:fourth.textContent.includes('12% used')||document.querySelectorAll('.account-item')[3].textContent.includes('12% used'),
    account4:state.usage['account-4']?.plan_status,
    overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth};
 })()`);
 const after=await(await fetch(base+'/api/snapshot')).json();
 const pass=result.request?.method==='POST'&&result.request.body?.id==='account-4'&&
  result.account4==='active'&&result.displayed&&!result.overflow&&
  after.policy.digest===before.policy.digest&&after.policy.accounts.length===4;
 console.log(JSON.stringify({pass,...result,accountCount:after.policy.accounts.length},null,2));
 process.exitCode=pass?0:1;
}catch(e){console.error('REFRESH_BROWSER_ERROR',String(e));process.exitCode=2;}
finally{ws?.close();chrome.kill();}
