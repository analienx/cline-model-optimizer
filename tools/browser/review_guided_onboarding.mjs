import {spawn} from 'node:child_process';
import fs from 'node:fs';import os from 'node:os';import path from 'node:path';
const base=process.argv[2]||'http://127.0.0.1:4395',port=9641,wait=ms=>new Promise(r=>setTimeout(r,ms));
const chrome=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',['--headless=new','--no-first-run','--disable-extensions',`--remote-debugging-port=${port}`,`--user-data-dir=${fs.mkdtempSync(path.join(os.tmpdir(),'cmo-guided-account-'))}`,'about:blank'],{stdio:'ignore'});
let ws;try{
 for(let i=0;i<80;i++){try{if((await fetch(`http://127.0.0.1:${port}/json/version`)).ok)break;}catch{}await wait(150);}
 const target=await(await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(base+'/')}`,{method:'PUT'})).json();
 ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((ok,bad)=>{ws.addEventListener('open',ok,{once:true});ws.addEventListener('error',bad,{once:true});});
 let id=0;const pending=new Map();ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(pending.has(m.id)){const [ok,bad]=pending.get(m.id);pending.delete(m.id);m.error?bad(Error(JSON.stringify(m.error))):ok(m.result);}});
 const send=(method,params={})=>new Promise((ok,bad)=>{const k=++id;pending.set(k,[ok,bad]);ws.send(JSON.stringify({id:k,method,params}));});
 const evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.text);return r.result.value;};
 await send('Page.enable');await send('Runtime.enable');await send('Page.navigate',{url:base+'/'});
 for(let i=0;i<80;i++){if(await evaluate("document.querySelector('#account-count')?.textContent==='4 / 5' && state.snap?.policy?.accounts?.length===4"))break;await wait(150);}
 const result=await evaluate(`(async()=>{
  const original=window.fetch;window.fetch=async(url,opts)=>{
   if(url==='/api/simple/accounts/start-onboarding'){
    window.__connectRequest={url,method:opts.method,body:JSON.parse(opts.body)};
    return new Response(JSON.stringify({ok:true,started:true,already_open:false}),{status:202,headers:{'Content-Type':'application/json'}});
   }
   return original(url,opts);
  };
  document.querySelector('.add-account-panel').open=true;
  document.querySelector('#new-email').value='new-fifth-account@example.invalid';
  document.querySelector('#add-account').requestSubmit();
  for(let i=0;i<70;i++){if(window.__connectRequest&&document.querySelector('#accounts')?.textContent.includes('new-fifth-account'))break;await new Promise(r=>setTimeout(r,120));}
  return {request:window.__connectRequest,emailPresent:document.querySelector('#accounts')?.textContent.includes('new-fifth-account'),feedback:document.querySelector('#accounts')?.textContent.includes('private profile'),five:document.querySelector('#account-count')?.textContent,message:document.querySelector('#message')?.textContent,valid:document.querySelector('#add-account')?.checkValidity()};
 })()`);
 const saved=await(await fetch(base+'/api/snapshot')).json();
 const account=saved.policy.accounts.find(a=>a.id==='account-5');
 const pass=result.request?.url==='/api/simple/accounts/start-onboarding'&&result.request?.body?.id==='account-5'&&result.request?.method==='POST'&&result.emailPresent&&account?.enabled===false;
 console.log(JSON.stringify({pass,result,registered:!!account,enabled:account?.enabled},null,2));process.exitCode=pass?0:1;
}catch(e){console.error('GUIDED_ACCOUNT_BROWSER_ERROR',String(e));process.exitCode=2;}finally{ws?.close();chrome.kill();}
