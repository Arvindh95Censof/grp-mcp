/**
 * delete_finyear.js - remove the FinancialYear node from GRPSetup endpoint (SM207060),
 * handling the in-page confirm modal, then Save. Verifies before/after.
 *   node delete_finyear.js
 */
const { chromium } = require('playwright');
const BASE=(process.env.GRP_BASE||'').replace(/\/+$/,'')+'/', USER=process.env.GRP_USER, PASS=process.env.GRP_PASS;
const EP='GRPSetup', VER='24.200.001', ENTITY='FinancialYear';
const sleep=(pg,ms)=>pg.waitForTimeout(ms);
const cmd=(frame,c)=>frame.evaluate(c=>{const x=document.querySelector(`[data-cmd="${c}"]`); if(x){['mousedown','mouseup','click'].forEach(e=>x.dispatchEvent(new MouseEvent(e,{bubbles:true})));return true;} return false;},c);
const clickText=(frame,re)=>frame.evaluate(rs=>{const r=new RegExp(rs,'i');const el=[...document.querySelectorAll('button,div,span,a')].find(e=>e.children.length===0&&e.getBoundingClientRect().width>0&&r.test((e.textContent||'').trim())&&(e.textContent||'').trim().length<16);if(el){['mousedown','mouseup','click'].forEach(e=>el.dispatchEvent(new MouseEvent(e,{bubbles:true})));return (el.textContent||'').trim();}return null;},re.source);
(async()=>{
  const b=await chromium.launch({headless:true});
  const pg=await (await b.newContext({viewport:{width:1600,height:900}})).newPage();
  pg.on('dialog',d=>{ d.accept().catch(()=>{}); });
  await pg.goto(BASE,{waitUntil:'domcontentloaded',timeout:60000});
  await pg.locator("input[type='text']:visible, input:not([type]):visible").first().fill(USER);
  await pg.locator("input[type='password']:visible").first().fill(PASS);
  await pg.keyboard.press('Enter'); await sleep(pg,6000);
  await pg.goto(`${BASE}Main?ScreenId=SM207060&InterfaceName=${EP}&GateVersion=${VER}`,{waitUntil:'domcontentloaded',timeout:60000});
  await sleep(pg,9000);
  const frame=pg.frames().find(f=>/SM207060\.html/.test(f.url())); if(!frame) throw new Error('frame');
  const present=()=>frame.evaluate(n=>!![...document.querySelectorAll('*')].find(e=>e.children.length===0&&(e.textContent||'').trim()===n),ENTITY);
  console.log('present before:', await present());
  // select the node
  const sel=await frame.evaluate(n=>{const x=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&(e.textContent||'').trim()===n&&e.getBoundingClientRect().width>0); if(!x)return false; x.scrollIntoView({block:'center'}); ['mousedown','mouseup','click'].forEach(e=>x.dispatchEvent(new MouseEvent(e,{bubbles:true}))); return true;},ENTITY);
  console.log('selected:', sel);
  await sleep(pg,1500);
  console.log('DeleteNode cmd:', await cmd(frame,'DeleteNode'));
  await sleep(pg,2000);
  // handle in-page confirm modal
  const confirm=await clickText(frame, /^(OK|Yes|Delete|Confirm)$/);
  console.log('confirm modal click:', confirm);
  await sleep(pg,2500);
  console.log('Save cmd:', await cmd(frame,'Save'));
  await sleep(pg,9000);
  console.log('present after:', await present());
  await b.close();
})().catch(e=>{ console.error('ERR:',e.message); process.exit(1); });
