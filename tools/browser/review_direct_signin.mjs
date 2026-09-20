import {spawn} from 'node:child_process';
import fs from 'node:fs';import os from 'node:os';import path from 'node:path';
const base=process.argv[2]||'http://127.0.0.1:4394';
const port=9615,sleep=ms=>new Promise(r=>setTimeout(r,ms));
const chrome=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',['--headless=new','--no-first-run','--disable-extensions',`--remote-debugging-port=${port}`,`--user-data-dir=${fs.mkdtempSync(path.join(os.tmpdir(),'cmo-direct-signin-'))}`,'about:blank'],{stdio:'ignore'});
let ws;try{
 for(let i=0;i<80;i++){try{if((await fetch(`http://127.0.0.1:${port}/json/version`)).ok)break;}catch{}await sleep(150);}
 const target=await(await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(base+'/')}`,{method:'PUT'})).json();
 ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((ok,bad)=>{ws.addEventListener('open',ok,{once:true});ws.addEventListener('error',bad,{once:true});});
 let id=0;const pending=new Map();ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(pending.has(m.id)){const [ok,bad]=pending.get(m.id);pending.delete(m.id);m.error?bad(Error(JSON.stringify(m.error))):ok(m.result);}});
 const send=(method,params={})=>new Promise((ok,bad)=>{const k=++id;pending.set(k,[ok,bad]);ws.send(JSON.stringify({id:k,method,params}));});
 const evaluate=async x=>{const r=await send('Runtime.evaluate',{expression:x,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.text);return r.result.value;};
 await send('Page.enable');await send('Runtime.enable');await send('Page.navigate',{url:base+'/'});
 for(let i=0;i<80;i++){if(await evaluate("document.querySelector('.account-connect-button')?.textContent==='Start sign-in on Windows'"))break;await sleep(150);}
 const before=await evaluate("({primary:document.querySelector('.account-connect-button')?.textContent,fallback:document.querySelector('.account-guide')?.open===false,alias:document.querySelector('.account-command')?.value?.endsWith('-Account account-4')})");
 const after=await evaluate(`(async()=>{
  const original=window.fetch;
  window.fetch=async(url,opts)=>{
   if(url==='/api/simple/accounts/start-signin'){
    window.__signinRequest={url,body:JSON.parse(opts.body),method:opts.method};
    return new Response(JSON.stringify({ok:true,started:true,already_open:false}),{status:202,headers:{'Content-Type':'application/json'}});
   }
   return original(url,opts);
  };
  document.querySelector('.account-connect-button').click();
  await new Promise(resolve=>setTimeout(resolve,180));
  return {request:window.__signinRequest,label:document.querySelector('.account-connect-button')?.textContent,
   feedback:document.querySelector('.account-copy-feedback')?.textContent,
   commandHidden:!document.querySelector('.account-guide')?.open};
 })()`);
 const pass=before.primary==='Start sign-in on Windows'&&before.fallback&&before.alias&&
  after.request?.url==='/api/simple/accounts/start-signin'&&after.request?.body?.id==='account-4'&&
  after.request?.method==='POST'&&after.label==='Sign-in terminal opened'&&
  after.feedback.includes('not verified yet')&&after.commandHidden;
 console.log(JSON.stringify({pass,before,after},null,2));process.exitCode=pass?0:1;
}catch(e){console.error('SIGNIN_BROWSER_TEST_ERROR',String(e));process.exitCode=2;}finally{ws?.close();chrome.kill();}
