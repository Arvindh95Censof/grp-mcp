/**
 * populate_all_views.js  (classic .aspx — csmdev / 2025R1)
 * ----------------------------------------------------------------------------
 * For an EXISTING top-level entity on an endpoint (SM207060), open Populate
 * Fields, enumerate every available Object (data view), and add all fields from
 * each one, then Save. Use after add_endpoint_entity.js to fully flesh out an
 * entity's contract (header + every detail view).
 *
 * USAGE (PowerShell):
 *   $env:GRP_BASE="https://erp.example.com/2025R1Setup"
 *   $env:GRP_USER="<user>"; $env:GRP_PASS="********"; $env:NODE_PATH=(npm root -g)
 *   node populate_all_views.js --endpoint GRPSetup --version 24.200.001 --entity DataProvider
 *     [--skip "Field Command Editor,Object Command Editor"]   # views to skip
 *     [--debug]
 * ----------------------------------------------------------------------------
 */
const { chromium } = require('playwright');
function arg(n, d){ const i=process.argv.indexOf('--'+n); if(i===-1) return d; const v=process.argv[i+1]; return (!v||v.startsWith('--'))?true:v; }
const BASE=(process.env.GRP_BASE||'').replace(/\/+$/,'')+'/', USER=process.env.GRP_USER, PASS=process.env.GRP_PASS;
const EP=arg('endpoint'), VER=arg('version'), ENTITY=arg('entity'), DEBUG=!!arg('debug',false);
const SKIP=(arg('skip','')||'').split(',').map(s=>s.trim()).filter(Boolean);
if(!BASE||!USER||!PASS){ console.error('Set GRP_BASE/GRP_USER/GRP_PASS'); process.exit(2); }
if(!EP||!VER||!ENTITY){ console.error('Required: --endpoint --version --entity'); process.exit(2); }

const ID={
  epName:'#ctl00_phF_form_edInterfaceName_text', epVer:'#ctl00_phF_form_edGateVersion_text',
  pfObjSel:'#ctl00_phG_pnlPopulateFields_formPopulateFields_PXTextEdit1 .control-SelectorN',
  pfObjFb:'#ctl00_phG_pnlPopulateFields_formPopulateFields_PXTextEdit1_pnl_tlb_fb_text',
  pfObjText:'#ctl00_phG_pnlPopulateFields_formPopulateFields_PXTextEdit1_text',
  pfOK:'#ctl00_phG_pnlPopulateFields_PXButton5',
};
const log=(...a)=>console.log('[populate]',...a);
const sleep=(pg,ms)=>pg.waitForTimeout(ms);
const fr=(pg)=>pg.frames().find(f=>f.name()==='main')||pg.frames().find(f=>/SM207060\.aspx/.test(f.url()));
const fire=(frame,sel)=>frame.evaluate(s=>{const x=document.querySelector(s); if(x)['mousedown','mouseup','click'].forEach(e=>x.dispatchEvent(new MouseEvent(e,{bubbles:true}))); return !!x;},sel);
const fireCmd=(frame,cmd)=>frame.evaluate(c=>{const x=document.querySelector('[data-cmd="'+c+'"]'); if(x)['mousedown','mouseup','click'].forEach(e=>x.dispatchEvent(new MouseEvent(e,{bubbles:true}))); return !!x;},cmd);

(async()=>{
  const b=await chromium.launch({headless:true});
  const pg=await (await b.newContext({viewport:{width:1600,height:900}})).newPage();
  const shot=(n)=>DEBUG&&pg.screenshot({path:`shots/${n}.png`}).catch(()=>{});
  // login
  await pg.goto(BASE,{waitUntil:'domcontentloaded',timeout:60000});
  await pg.locator("input[type='text']:visible, input:not([type]):visible").first().fill(USER);
  await pg.locator("input[type='password']:visible").first().fill(PASS);
  await pg.keyboard.press('Enter'); await sleep(pg,6000);
  if(/Login\.aspx/i.test(pg.url())) throw new Error('login failed');
  log('logged in');
  // open SM207060 + load endpoint
  await pg.goto(BASE+'Main?ScreenId=SM207060&InterfaceName='+EP+'&GateVersion='+VER,{waitUntil:'domcontentloaded',timeout:60000});
  await sleep(pg,8000);
  let frame=fr(pg); if(!frame) throw new Error('form frame not found');
  // ensure endpoint loaded
  const epNow=await frame.locator(ID.epName).inputValue().catch(()=>'');
  if(epNow!==EP){ await frame.locator(ID.epName).fill(EP); await frame.locator(ID.epName).press('Enter'); await sleep(pg,5000); frame=fr(pg); }
  log('endpoint', await frame.locator(ID.epName).inputValue().catch(()=>'?'));

  // helper: select the entity node
  const selectNode=async()=>{ await frame.evaluate(n=>{const x=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&(e.textContent||'').trim()===n); if(x)['mousedown','mouseup','click'].forEach(ev=>x.dispatchEvent(new MouseEvent(ev,{bubbles:true})));},ENTITY); await sleep(pg,1500); };

  // 1) enumerate views: open PopulateFields, open object selector, read display names
  await selectNode();
  if(!await fireCmd(frame,'PopulateFields')) throw new Error('PopulateFields btn missing (entity selected?)');
  await sleep(pg,3000);
  await fire(frame,ID.pfObjSel); await sleep(pg,2500);
  let views=await frame.evaluate(()=>{
    const rows=[...document.querySelectorAll('tr')].map(t=>{const td=[...t.querySelectorAll('td')].map(c=>(c.innerText||'').trim()).filter(Boolean); return td;}).filter(a=>a.length===2&&/^[A-Za-z]/.test(a[0])&&a[1].length<50);
    return [...new Set(rows.map(a=>a[1]))];
  });
  // close dialog (Escape) before looping
  await pg.keyboard.press('Escape'); await sleep(pg,1500);
  views=views.filter(v=>!SKIP.includes(v));
  log('views to populate ('+views.length+'):', JSON.stringify(views));

  // 2) per view: open PopulateFields -> set object (filter+Enter) -> SelectAll -> OK
  const done=[];
  for(const view of views){
    frame=fr(pg);
    await selectNode();
    if(!await fireCmd(frame,'PopulateFields')){ log('SKIP (no dialog):',view); continue; }
    await sleep(pg,2500);
    await fire(frame,ID.pfObjSel); await sleep(pg,2000);
    await frame.locator(ID.pfObjFb).click().catch(()=>{});
    await frame.locator(ID.pfObjFb).fill('').catch(()=>{});
    await frame.locator(ID.pfObjFb).pressSequentially(view,{delay:50}).catch(()=>{});
    await sleep(pg,2000);
    await frame.locator(ID.pfObjFb).press('Enter').catch(()=>{});
    await sleep(pg,2500);
    const got=await frame.locator(ID.pfObjText).inputValue().catch(()=>'');
    if(got.trim()!==view.trim()){ log('view mismatch, wanted',view,'got',got,'- skipping'); await pg.keyboard.press('Escape'); await sleep(pg,1200); continue; }
    await fireCmd(frame,'SelectAll'); await sleep(pg,1800);
    // OK: footer button (lowest "OK")
    await frame.evaluate(()=>{const panel=document.querySelector('#ctl00_phG_pnlPopulateFields')||document; const bs=[...panel.querySelectorAll('button,input[type=button],div')].filter(e=>e.offsetParent!==null&&/^OK$/i.test((e.textContent||e.value||'').trim())&&(e.textContent||e.value||'').trim().length<=3); bs.sort((a,b)=>b.getBoundingClientRect().top-a.getBoundingClientRect().top); if(bs[0])['mousedown','mouseup','click'].forEach(e=>bs[0].dispatchEvent(new MouseEvent(e,{bubbles:true})));});
    await sleep(pg,3500);
    done.push(view); log('populated:',view);
    shot('v_'+view.replace(/[^a-z0-9]+/gi,'_'));
  }

  // 3) Save once
  frame=fr(pg);
  await fireCmd(frame,'Save'); await sleep(pg,8000);
  log('SAVED. populated views:', JSON.stringify(done));
  await b.close();
})().catch(e=>{ console.error('[populate] ERR:',e.message); process.exit(1); });
