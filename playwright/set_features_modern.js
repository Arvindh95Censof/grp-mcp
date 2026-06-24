/**
 * set_features_modern.js  (modern Aurelia CS100000)
 * ----------------------------------------------------------------------------
 * Turn Acumatica features ON and/or OFF on Enable/Disable Features (CS100000),
 * any number in one run: Modify -> set each checkbox to the wanted state -> Enable.
 * WARNING: "Enable" recompiles/restarts the instance (~1-3 min). Acumatica BLOCKS
 * disabling a feature that has dependent data/config — those stay on and the run
 * reports the error.
 *
 *   $env:GRP_BASE=...; $env:GRP_USER=...; $env:GRP_PASS=...; $env:NODE_PATH=(npm root -g)
 *   node set_features_modern.js --on "Multibranch Support;Subaccounts" --off "GL Consolidation" --debug
 *   node set_features_modern.js --on "Multicurrency"
 *
 * Feature names are the exact labels on CS100000 (e.g. "Multibranch Support",
 * "Subaccounts", "Multicompany Support", "Inter-Branch Transactions", "Multicurrency",
 * "GL Consolidation"). Use --dry to only report current states (no change).
 * ----------------------------------------------------------------------------
 */
const { chromium } = require('playwright');
function arg(n,d){ const i=process.argv.indexOf('--'+n); if(i===-1) return d; const v=process.argv[i+1]; return (!v||v.startsWith('--'))?true:v; }
const list=(s)=>String(s||'').split(';').map(x=>x.trim()).filter(Boolean);
const BASE=(process.env.GRP_BASE||'').replace(/\/+$/,'')+'/', USER=process.env.GRP_USER, PASS=process.env.GRP_PASS;
const ON=list(arg('on','')), OFF=list(arg('off','')), DRY=!!arg('dry',false), LISTALL=!!arg('listall',false), DEBUG=!!arg('debug',false);
if(!BASE||!USER||!PASS){ console.error('Set GRP_BASE/GRP_USER/GRP_PASS'); process.exit(2); }
if(!ON.length && !OFF.length && !DRY && !LISTALL){ console.error('Provide --on "A;B" and/or --off "C" (or --listall / --dry)'); process.exit(2); }
const WANT = [...ON.map(f=>({f,want:true})), ...OFF.map(f=>({f,want:false}))];
const log=(...a)=>console.log('[set-features]',...a);
const sleep=(pg,ms)=>pg.waitForTimeout(ms);
const cmd=(frame,c)=>frame.evaluate(c=>{const x=document.querySelector(`[data-cmd="${c}"]`); if(x)['mousedown','mouseup','click'].forEach(e=>x.dispatchEvent(new MouseEvent(e,{bubbles:true}))); return !!x;},c);
const clickOK=(frame)=>frame.evaluate(()=>{const ok=[...document.querySelectorAll('button,div,span,a')].filter(x=>/^(OK|Yes|Enable)$/i.test((x.textContent||'').trim())&&x.getBoundingClientRect().width>0&&x.getBoundingClientRect().width<200); if(ok.length){ok[ok.length-1].click(); return (ok[ok.length-1].textContent||'').trim();} return null;});
// read a feature checkbox state by its label text
const stateOf=(frame,name)=>frame.evaluate(n=>{const lab=[...document.querySelectorAll('label.qp-checkbox__label')].find(l=>(l.textContent||'').trim()===n); if(!lab) return null; const cb=lab.closest('span.qp-checkbox')?.querySelector('input.qp-checkbox__input'); return cb?cb.checked:null;},name);
// set a feature checkbox to `want` (click only if different); returns resulting checked
const setOf=(frame,name,want)=>frame.evaluate(({n,w})=>{const lab=[...document.querySelectorAll('label.qp-checkbox__label')].find(l=>(l.textContent||'').trim()===n); if(!lab) return 'missing'; const cb=lab.closest('span.qp-checkbox').querySelector('input.qp-checkbox__input'); if(cb.checked!==w){ cb.scrollIntoView({block:'center'}); ['mousedown','mouseup','click'].forEach(e=>cb.dispatchEvent(new MouseEvent(e,{bubbles:true}))); } return cb.checked;},{n:name,w:want});

(async()=>{
  const b=await chromium.launch({headless:true});
  const pg=await (await b.newContext({viewport:{width:1600,height:900}})).newPage();
  pg.on('dialog',d=>{ log('dialog:',d.type(),JSON.stringify(d.message())); d.accept().catch(()=>{}); });
  const shot=(n)=>DEBUG&&pg.screenshot({path:`shots/${n}.png`}).catch(()=>{});
  await pg.goto(BASE,{waitUntil:'domcontentloaded',timeout:60000});
  await pg.locator("input[type='text']:visible, input:not([type]):visible").first().fill(USER);
  await pg.locator("input[type='password']:visible").first().fill(PASS);
  await pg.keyboard.press('Enter'); await sleep(pg,6000); log('logged in');
  await pg.goto(`${BASE}Main?ScreenId=CS100000`,{waitUntil:'domcontentloaded',timeout:60000});
  await sleep(pg,11000);
  const frame=pg.frames().find(f=>/CS100000\.html/.test(f.url())); if(!frame) throw new Error('CS100000.html frame not found');

  // --listall: dump every feature label + current state, then exit (label discovery)
  if(LISTALL){
    const all=await frame.evaluate(()=>[...document.querySelectorAll('label.qp-checkbox__label')].map(l=>{const cb=l.closest('span.qp-checkbox')?.querySelector('input.qp-checkbox__input'); return {feature:(l.textContent||'').trim(), on:cb?cb.checked:null};}).filter(x=>x.feature));
    console.log(JSON.stringify(all,null,0));
    await b.close(); return;
  }

  // report current states; bail if any requested feature label is missing
  const targets = DRY ? [] : WANT;
  for(const {f} of (DRY?[{f:'__dump__'}]:WANT)){ /* noop */ }
  const before={}; for(const {f} of WANT){ before[f]=await stateOf(frame,f); }
  log('current:', JSON.stringify(before));
  const missing=WANT.filter(x=>before[x.f]===null).map(x=>x.f);
  if(missing.length) throw new Error('feature label(s) not found on CS100000: '+JSON.stringify(missing));
  const todo=WANT.filter(x=>before[x.f]!==x.want);
  if(DRY){ log('dry run - no changes'); await b.close(); return; }
  if(!todo.length){ log('all requested features already in the wanted state - nothing to do'); await b.close(); return; }
  log('changing:', JSON.stringify(todo.map(x=>x.f+'->'+(x.want?'ON':'OFF'))));

  await cmd(frame,'insert'); await sleep(pg,3000); shot('1_modify');   // Modify
  for(const {f,want} of todo){ const r=await setOf(frame,f,want); log('set',f,'->',r); }
  shot('2_set');
  await cmd(frame,'requestValidation'); await sleep(pg,3000);          // Enable
  const ok=await clickOK(frame); log('confirm:', ok);
  log('Enable triggered - applying (recompile/restart). Disables with dependent data are rejected here.');
  await sleep(pg,8000); shot('3_enable');
  await b.close();
  log('DONE. Verify with: run_dac_odata("FeaturesSet") once the instance is back up.');
})().catch(e=>{ console.error('[set-features] ERR:',e.message); process.exit(1); });
