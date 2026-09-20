#!/usr/bin/env node
import {spawn} from 'node:child_process';import fs from 'node:fs';import os from 'node:os';import path from 'node:path';
const args=Object.fromEntries(process.argv.slice(2).reduce((a,v,i,x)=>{if(v.startsWith('--'))a.push([v.slice(2),x[i+1]]);return a;},[]));
const base=args.base||'http://127.0.0.1:4311',width=Number(args.width||390),port=Number(args.port||9504),out=args.out||path.join(os.tmpdir(),'cmo-advanced-review');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));fs.mkdirSync(out,{recursive:true});
const chrome=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',['--headless=new','--disable-gpu','--no-first-run','--disable-extensions',`--remote-debugging-port=${port}`,`--user-data-dir=${fs.mkdtempSync(path.join(os.tmpdir(),'cmo-adv-'))}`,'about:blank'],{stdio:'ignore'});
let socket;try{
for(let n=0;n<80;n++){try{if((await fetch(`http://127.0.0.1:${port}/json/version`)).ok)break;}catch{}await sleep(150);}
const target=await(await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(base+'/advanced')}`,{method:'PUT'})).json();socket=new WebSocket(target.webSocketDebuggerUrl);await new Promise((res,rej)=>{socket.addEventListener('open',res,{once:true});socket.addEventListener('error',rej,{once:true});});
let id=0;const pending=new Map();socket.addEventListener('message',e=>{const m=JSON.parse(e.data);if(pending.has(m.id)){const [resolve,reject]=pending.get(m.id);pending.delete(m.id);m.error?reject(Error(JSON.stringify(m.error))):resolve(m.result);}});
const send=(method,params={})=>new Promise((resolve,reject)=>{const i=++id;pending.set(i,[resolve,reject]);socket.send(JSON.stringify({id:i,method,params}));});
const evaluate=async expression=>(await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true})).result.value;
await send('Page.enable');await send('Runtime.enable');await send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:width<700});await send('Page.navigate',{url:base+'/advanced'});
let verdict;for(let i=0;i<45;i++){verdict=await evaluate(`({title:document.title,connection:document.querySelector('#connection')?.textContent,advanced:document.querySelectorAll('.advanced-panel').length,matrix:document.querySelectorAll('.advanced-model').length,legacy:document.querySelectorAll('link[href="/style.css"], script[src="/app.js"]').length,overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth})`);if(verdict.connection==='Connected'&&verdict.matrix>0)break;await sleep(200);}
const pass=verdict.connection==='Connected'&&verdict.advanced===3&&verdict.matrix>0&&!verdict.legacy&&!verdict.overflow;
const shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});fs.writeFileSync(path.join(out,'advanced.png'),Buffer.from(shot.data,'base64'));fs.writeFileSync(path.join(out,'verdict.json'),JSON.stringify({pass,width,...verdict},null,2));console.log(JSON.stringify({pass,width,...verdict}));process.exitCode=pass?0:1;
}catch(e){console.log('ADVANCED_BROWSER_ERROR='+String(e));process.exitCode=2;}finally{socket?.close();chrome.kill();}
