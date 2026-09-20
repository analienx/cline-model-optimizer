import {spawn} from 'node:child_process';
import fs from 'node:fs';import os from 'node:os';import path from 'node:path';
const base=process.argv[2]||'http://127.0.0.1:4393',port=9596,wait=ms=>new Promise(r=>setTimeout(r,ms));
const chrome=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',['--headless=new','--no-first-run','--disable-extensions',`--remote-debugging-port=${port}`,`--user-data-dir=${fs.mkdtempSync(path.join(os.tmpdir(),'cmo-native-copy-'))}`,'about:blank'],{stdio:'ignore'});
let ws;try{
 for(let i=0;i<80;i++){try{if((await fetch(`http://127.0.0.1:${port}/json/version`)).ok)break;}catch{}await wait(160);}
 const target=await(await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(base+'/')}`,{method:'PUT'})).json();
 ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((ok,bad)=>{ws.addEventListener('open',ok,{once:true});ws.addEventListener('error',bad,{once:true});});
 let id=0;const pending=new Map();ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(pending.has(m.id)){const [ok,bad]=pending.get(m.id);pending.delete(m.id);m.error?bad(Error(JSON.stringify(m.error))):ok(m.result);}});
 const send=(method,params={})=>new Promise((ok,bad)=>{const k=++id;pending.set(k,[ok,bad]);ws.send(JSON.stringify({id:k,method,params}));});
 const evaluate=async x=>{const r=await send('Runtime.evaluate',{expression:x,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.text);return r.result.value;};
 await send('Page.enable');await send('Runtime.enable');await send('Page.navigate',{url:base+'/'});
 for(let i=0;i<70;i++){if(await evaluate("document.querySelectorAll('.account-connect-button').length===1"))break;await wait(200);}
 const first=await evaluate("document.querySelector('.account-connect-button')?.textContent");
 await evaluate("document.querySelector('.account-connect-button').click();true");
 for(let i=0;i<50;i++){if(await evaluate("!document.querySelector('.account-copy-feedback')?.hidden"))break;await wait(200);}
 const result=await evaluate("({label:document.querySelector('.account-connect-button')?.textContent,feedback:document.querySelector('.account-copy-feedback')?.textContent,visible:document.querySelector('.account-guide')?.open,field:document.querySelector('.account-command')?.value?.endsWith('-Account account-4')})");
 const pass=first==='Copy sign-in command'&&result.label==='Copied to Windows'&&result.feedback.includes('Windows text clipboard checked')&&result.field;
 console.log(JSON.stringify({pass,first,result}));process.exitCode=pass?0:1;
}catch(e){console.error('NATIVE_COPY_BROWSER_ERROR',String(e));process.exitCode=2;}finally{ws?.close();chrome.kill();}
