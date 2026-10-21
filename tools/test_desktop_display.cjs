// Optional real-browser integration test. Requires Playwright and an isolated
// network:none Docker fixture built from the runtime, with a disposable home.
// Set CCBOX_TEST_SANDBOX (ccbox-display-qa-*), CCBOX_TEST_CONFIG (fixture JSON with
// desktop_token/desktop_port), CCBOX_TEST_OUTPUT, and optionally CCBOX_TEST_BROWSER
// and CCBOX_TEST_DOCKER. Never use real accounts, workspaces or proxy credentials.
const {chromium}=require('playwright');
const {execFileSync}=require('node:child_process');
const fs=require('node:fs');const path=require('node:path');
const assert=require('node:assert/strict');
const docker=process.env.CCBOX_TEST_DOCKER||'docker';
const sandbox=process.env.CCBOX_TEST_SANDBOX;
assert(sandbox?.startsWith('ccbox-display-qa-'), 'Use an isolated fixture, never a user container');
const fixture=JSON.parse(fs.readFileSync(process.env.CCBOX_TEST_CONFIG));
const token=fixture.desktop_token; const port=fixture.desktop_port;
assert(Number.isInteger(port)&&port>=16000&&port<=16999, 'Fixture must use a test port');
const output=path.resolve(process.env.CCBOX_TEST_OUTPUT);
fs.mkdirSync(output,{recursive:true});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CCBOX_TEST_BROWSER||undefined,headless:true});
 const report={cases:[],requests:[],errors:[]};
 try {
 const ctx=await browser.newContext({viewport:{width:1920,height:1080}});const page=await ctx.newPage();
 page.on('pageerror',e=>report.errors.push(e.message));
 page.on('websocket',ws=>ws.on('framesent',({payload})=>{const b=Buffer.from(payload);if(b[0]===251&&b.length===24)report.requests.push([b.readUInt16BE(2),b.readUInt16BE(4)]);}));
 await page.goto(`http://127.0.0.1:${port}/desktop.html?access=`+encodeURIComponent(token));
 await page.waitForFunction(()=>document.querySelector('#status').textContent==='桌面已连接');
 async function match(name){
  await page.waitForFunction(()=>{const c=document.querySelector('#display canvas'),r=document.querySelector('#display').getBoundingClientRect();return c&&Math.abs(c.width-Math.floor(r.width))<=2&&Math.abs(c.height-Math.floor(r.height))<=2;},{},{timeout:10000});
  await sleep(250);
  const dims=await page.evaluate(()=>{const c=document.querySelector('#display canvas'),r=document.querySelector('#display').getBoundingClientRect();return{canvas:[c.width,c.height],area:[Math.floor(r.width),Math.floor(r.height)],overflow:document.documentElement.scrollWidth>innerWidth,info:document.querySelector('#display-info').textContent};});
  const query="import ctypes,json;from ctypes import c_void_p,c_char_p,c_int; x=ctypes.CDLL('libX11.so.6'); x.XOpenDisplay.argtypes=[c_char_p];x.XOpenDisplay.restype=c_void_p;d=x.XOpenDisplay(b':99');x.XDisplayWidth.argtypes=x.XDisplayHeight.argtypes=[c_void_p,c_int];print(json.dumps([x.XDisplayWidth(d,0),x.XDisplayHeight(d,0)]))";
  const x=JSON.parse(execFileSync(docker,['exec',sandbox,'python3','-c',query],{encoding:'utf8'}));
  assert.deepEqual(dims.canvas,x);assert(!dims.overflow);
  report.cases.push({name,...dims});
 }
 for(const [w,h] of [[1366,768],[1920,1080],[2560,1440],[3440,1440],[3840,2160],[5120,2880],[390,844]]){await page.setViewportSize({width:w,height:h});await match(`${w}x${h}`);}
 await page.setViewportSize({width:1920,height:1080});await match('restore desktop');
 const before=await page.locator('#display canvas').evaluate(c=>[c.width,c.height]);
 await page.selectOption('#scale','follow');const requestCount=report.requests.length;
 await page.setViewportSize({width:1366,height:768});await sleep(1800);
 assert.deepEqual(await page.locator('#display canvas').evaluate(c=>[c.width,c.height]),before);
 assert.equal(report.requests.length,requestCount);report.followDoesNotResize=true;
 await page.selectOption('#scale','native');await sleep(600);assert.equal(report.requests.length,requestCount);
 report.originalDoesNotResize=true;
 await page.selectOption('#scale','auto');await match('return to automatic');
 await page.selectOption('#quality','6');await sleep(300);assert((await page.locator('#display-info').innerText()).includes('流畅'));
 await page.selectOption('#quality','9');
 await page.locator('#fullscreen').click();await page.waitForFunction(()=>!!document.fullscreenElement);await match('fullscreen');
 await page.locator('#fullscreen').click();await page.waitForFunction(()=>!document.fullscreenElement);await match('leave fullscreen');
 await page.setViewportSize({width:1920,height:1080});await match('final');
 await page.screenshot({path:path.join(output,'responsive-1920.png')});
 // Existing clipboard bridge must still own selections with VNC sync disabled.
 await page.locator('#clipboard').click();await page.locator('#text').fill('中文剪贴板 test\nline 2 !@');await page.locator('#send').click();
 await page.waitForFunction(()=>!document.querySelector('#clip').open);
 const clip=execFileSync(docker,['exec',sandbox,'xclip','-selection','clipboard','-o'],{encoding:'utf8'});
 assert.equal(clip,'中文剪贴板 test\nline 2 !@');report.explicitClipboard=true;
 await page.reload();await page.waitForFunction(()=>document.querySelector('#status').textContent==='桌面已连接');await match('reconnect');
 assert.deepEqual(report.errors,[]);report.passed=true;
 } finally {await browser.close();fs.writeFileSync(path.join(output,'responsive-browser-results.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));}
})().catch(e=>{console.error(e);process.exitCode=1;});
