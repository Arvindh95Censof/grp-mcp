/**
 * delete_endpoint_entity_modern.js  (modern Aurelia SM207060)
 * Remove a top-level entity node from an endpoint, headless. Selects the node,
 * clicks the tree Delete, accepts any confirm, and Saves.
 *
 *   $env:GRP_BASE=...; $env:GRP_USER=...; $env:GRP_PASS=...; $env:NODE_PATH=(npm root -g)
 *   node delete_endpoint_entity_modern.js --endpoint GRPSetup --version 24.200.001 --entity Branch --debug
 */
const { chromium } = require('playwright');
function arg(n, d){ const i=process.argv.indexOf('--'+n); if(i===-1) return d; const v=process.argv[i+1]; return (!v||v.startsWith('--'))?true:v; }
const BASE=(process.env.GRP_BASE||'').replace(/\/+$/,'')+'/', USER=process.env.GRP_USER, PASS=process.env.GRP_PASS;
const EP=arg('endpoint'), VER=arg('version'), ENTITY=arg('entity'), DEBUG=!!arg('debug',false);
if(!BASE||!USER||!PASS||!EP||!VER||!ENTITY){ console.error('need GRP_BASE/USER/PASS + --endpoint --version --entity'); process.exit(2); }
const log=(...a)=>console.log('[del-entity]',...a);
const sleep=(pg,ms)=>pg.waitForTimeout(ms);
const cmd=(frame,c)=>frame.evaluate(c=>{const x=document.querySelector(`[data-cmd="${c}"]`); if(x)['mousedown','mouseup','click'].forEach(e=>x.dispatchEvent(new MouseEvent(e,{bubbles:true}))); return !!x;},c);

(async()=>{
  const b=await chromium.launch({headless:true});
  const pg=await (await b.newContext({viewport:{width:1600,height:900}})).newPage();
  pg.on('dialog',d=>{ log('dialog:',d.type(),JSON.stringify(d.message())); d.accept().catch(()=>{}); });
  const shot=(n)=>DEBUG&&pg.screenshot({path:`shots/${n}.png`}).catch(()=>{});
  await pg.goto(BASE,{waitUntil:'domcontentloaded',timeout:60000});
  await pg.locator("input[type='text']:visible, input:not([type]):visible").first().fill(USER);
  await pg.locator("input[type='password']:visible").first().fill(PASS);
  await pg.keyboard.press('Enter'); await sleep(pg,6000);
  log('logged in');
  await pg.goto(`${BASE}Main?ScreenId=SM207060&InterfaceName=${encodeURIComponent(EP)}&GateVersion=${VER}`,{waitUntil:'domcontentloaded',timeout:60000});
  await sleep(pg,9000);
  const frame=pg.frames().find(f=>/SM207060\.html/.test(f.url())); if(!frame) throw new Error('SM207060.html frame not found');
  const present=()=>frame.evaluate(n=>!![...document.querySelectorAll('*')].find(e=>e.children.length===0&&(e.textContent||'').trim()===n),ENTITY);
  if(!await present()){ log('entity not present - nothing to delete'); await b.close(); return; }

  // select the entity node — click the leaf text element (same as the add script)
  const sel=await frame.evaluate(n=>{const x=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&(e.textContent||'').trim()===n&&e.getBoundingClientRect().width>0); if(!x) return false; x.scrollIntoView({block:'center'}); ['mousedown','mouseup','click'].forEach(e=>x.dispatchEvent(new MouseEvent(e,{bubbles:true}))); return true;},ENTITY);
  if(!sel) throw new Error('could not select node '+ENTITY);
  await sleep(pg,2000); shot('1_selected');
  // try the tree Delete command (a few likely data-cmd names)
  let deleted=false;
  for(const c of ['DeleteNode','delete','Delete']){ if(await cmd(frame,c)){ log('clicked cmd',c); deleted=true; break; } }
  if(!deleted) log('no Delete data-cmd found - check screenshot');
  await sleep(pg,2000);
  // confirm any in-page "are you sure / save changes?" modal (Yes/OK)
  const clickBtn=(t)=>frame.evaluate(t=>{const x=[...document.querySelectorAll('button')].find(b=>(b.textContent||'').trim()===t&&b.getBoundingClientRect().width>0);if(x){x.click();return true;}return false;},t);
  await clickBtn('Yes'); await clickBtn('OK'); await sleep(pg,2000); shot('2_afterdelete');
  await cmd(frame,'Save'); await sleep(pg,4000);
  await clickBtn('Yes'); await clickBtn('OK'); await sleep(pg,6000); shot('3_saved');
  log('done. still present?', await present());
  await b.close();
})().catch(e=>{ console.error('[del-entity] ERR:',e.message); process.exit(1); });
