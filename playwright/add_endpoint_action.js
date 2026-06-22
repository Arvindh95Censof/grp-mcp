/**
 * add_endpoint_action.js   (classic .aspx — csmdev / 2025R1)
 * ----------------------------------------------------------------------------
 * Add ACTION(s) to an existing entity on a web service endpoint (SM207060),
 * classic UI. Maps screen command(s) -> API action(s), invokable via
 * grp-mcp invoke_action / POST {Endpoint}/{Entity}/{Action}.
 *
 * Classic flow (mirrors add_endpoint_entity.js Create-Entity, but for actions):
 *   select entity node -> expand -> select its "Actions" child -> Insert
 *   -> Create Action dialog: pick Mapped Action (selector filter+Enter),
 *      type Action Name -> OK -> Save.
 *
 * USAGE (PowerShell):
 *   $env:GRP_BASE="https://csmdev.censof.com/2025R1Setup"
 *   $env:GRP_USER="<user>"; $env:GRP_PASS="********"; $env:NODE_PATH=(npm root -g)
 *   # one action:
 *   node add_endpoint_action.js --endpoint GRPSetup --version 24.200.001 \
 *        --entity ImportByScenario --action "Import" --action-name ImportIBS --debug
 *   # many at once (parallel arrays, ; -separated):
 *   node add_endpoint_action.js --endpoint GRPSetup --version 24.200.001 \
 *        --entity ImportByScenario --actions "Import;Prepare;Save" --suffix IBS
 *   (--suffix builds ActionName = <mappedNoSpaces>+<suffix>; or use --action/--action-name for one)
 * ----------------------------------------------------------------------------
 */
const { chromium } = require('playwright');
function arg(n,d){ const i=process.argv.indexOf('--'+n); if(i===-1) return d; const v=process.argv[i+1]; return (!v||v.startsWith('--'))?true:v; }
const BASE=(process.env.GRP_BASE||'').replace(/\/+$/,'')+'/', USER=process.env.GRP_USER, PASS=process.env.GRP_PASS;
const EP=arg('endpoint'), VER=arg('version'), ENTITY=arg('entity');
const ONE=arg('action',null), ONENAME=arg('action-name',null);
const MANY=arg('actions',null), SUFFIX=arg('suffix',''), DEBUG=!!arg('debug',false);
if(!BASE||!USER||!PASS){ console.error('Set GRP_BASE/GRP_USER/GRP_PASS'); process.exit(2); }
if(!EP||!VER||!ENTITY){ console.error('Required: --endpoint --version --entity'); process.exit(2); }
// build work list: [{mapped, name}]
let WORK=[];
if(MANY){ WORK = String(MANY).split(';').map(s=>s.trim()).filter(Boolean).map(m=>({mapped:m, name:m.replace(/[^A-Za-z0-9]/g,'')+SUFFIX})); }
else if(ONE){ WORK=[{mapped:ONE, name:(ONENAME||ONE.replace(/[^A-Za-z0-9]/g,'')+SUFFIX)}]; }
if(!WORK.length){ console.error('Provide --action [--action-name] OR --actions "A;B;C" [--suffix X]'); process.exit(2); }

const P='#ctl00_phG_pnlCreateAction';
const ID={
  epName:'#ctl00_phF_form_edInterfaceName_text', epVer:'#ctl00_phF_form_edGateVersion_text',
  caMapped:P+'_formCreateAction_edMappedAction',
  caMappedFb:P+'_formCreateAction_edMappedAction_pnl_tlb_fb_text',
  caMappedText:P+'_formCreateAction_edMappedAction_text',
  caName:P+'_formCreateAction_edActionName',
  caOK:P+'_btnOK',
};
const log=(...a)=>console.log('[add-action]',...a);
const sleep=(pg,ms)=>pg.waitForTimeout(ms);
const frм=(pg)=>pg.frames().find(f=>f.name()==='main')||pg.frames().find(f=>/SM207060\.aspx/.test(f.url()));
const fire=(frame,sel)=>frame.evaluate(s=>{const x=document.querySelector(s); if(x)['mousedown','mouseup','click'].forEach(e=>x.dispatchEvent(new MouseEvent(e,{bubbles:true}))); return !!x;},sel);
const fireText=(frame,txt)=>frame.evaluate(t=>{const x=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&(e.textContent||'').trim()===t&&e.offsetParent!==null); if(x)['mousedown','mouseup','click'].forEach(e=>x.dispatchEvent(new MouseEvent(e,{bubbles:true}))); return !!x;},txt);

(async()=>{
  const b=await chromium.launch({headless:true});
  const pg=await (await b.newContext({viewport:{width:1600,height:900}})).newPage();
  pg.on('dialog',d=>d.accept().catch(()=>{}));   // auto-accept stray alerts
  const shot=(n)=>DEBUG&&pg.screenshot({path:`shots/${n}.png`}).catch(()=>{});
  await pg.goto(BASE,{waitUntil:'domcontentloaded',timeout:60000});
  await pg.locator("input[type='text']:visible, input:not([type]):visible").first().fill(USER);
  await pg.locator("input[type='password']:visible").first().fill(PASS);
  await pg.keyboard.press('Enter'); await sleep(pg,6000);
  if(/Login\.aspx/i.test(pg.url())) throw new Error('login failed');
  log('logged in');
  await pg.goto(BASE+'Main?ScreenId=SM207060&InterfaceName='+encodeURIComponent(EP)+'&GateVersion='+VER,{waitUntil:'domcontentloaded',timeout:60000});
  await sleep(pg,8000);
  let frame=frм(pg); if(!frame) throw new Error('form frame not found');
  const epNow=await frame.locator(ID.epName).inputValue().catch(()=>'');
  if(epNow!==EP){ await frame.locator(ID.epName).fill(EP); await frame.locator(ID.epName).press('Enter'); await sleep(pg,5000); frame=frм(pg); }
  log('endpoint', await frame.locator(ID.epName).inputValue().catch(()=>'?'));

  // expand the entity node + select its Actions child (once)
  await frame.evaluate(n=>{const x=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&(e.textContent||'').trim()===n); if(x){x.scrollIntoView({block:'center'});['mousedown','mouseup','click','dblclick'].forEach(ev=>x.dispatchEvent(new MouseEvent(ev,{bubbles:true})));}},ENTITY);
  await sleep(pg,2500);
  shot('0_expanded');

  const done=[];
  for(const {mapped,name} of WORK){
    frame=frм(pg);
    // select Actions child of THIS entity
    if(!await fireText(frame,'Actions')){ log('Actions node not found - re-expanding'); await frame.evaluate(n=>{const x=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&(e.textContent||'').trim()===n); if(x)['mousedown','mouseup','click','dblclick'].forEach(ev=>x.dispatchEvent(new MouseEvent(ev,{bubbles:true})));},ENTITY); await sleep(pg,2000); await fireText(frame,'Actions'); }
    await sleep(pg,1200);
    // Insert (tree toolbar)
    await frame.evaluate(()=>{const t=document.querySelector('#ctl00_phG_splitFields_entityTree_tlb'); const ins=t&&[...t.querySelectorAll('*')].find(e=>(e.textContent||'').trim()==='Insert'); if(ins)['mousedown','mouseup','click'].forEach(e=>ins.dispatchEvent(new MouseEvent(e,{bubbles:true})));});
    await sleep(pg,3500);
    shot('1_dlg_'+name);
    // pick Mapped Action via selector popup
    await fire(frame, ID.caMapped+' .control-SelectorN'); await sleep(pg,2500);
    await frame.locator(ID.caMappedFb).click().catch(()=>{});
    await frame.locator(ID.caMappedFb).fill('').catch(()=>{});
    await frame.locator(ID.caMappedFb).pressSequentially(mapped,{delay:60}).catch(()=>{});
    await sleep(pg,2500);
    await frame.locator(ID.caMappedFb).press('Enter').catch(()=>{});
    await sleep(pg,2000);
    const got=await frame.locator(ID.caMappedText).inputValue().catch(()=>'?');
    if(got.trim().toLowerCase()!==mapped.trim().toLowerCase()){ log('mapped mismatch wanted',mapped,'got',got,'- skipping'); await pg.keyboard.press('Escape'); await sleep(pg,1200); continue; }
    await frame.locator(ID.caName).fill(name).catch(()=>{});
    log('mapped:',got,'name:',name);
    // OK
    await fire(frame, ID.caOK); await sleep(pg,4000);
    done.push(name); shot('2_added_'+name);
  }
  // Save
  frame=frм(pg);
  await frame.evaluate(()=>{const x=document.querySelector('[data-cmd="Save"]'); if(x)['mousedown','mouseup','click'].forEach(e=>x.dispatchEvent(new MouseEvent(e,{bubbles:true})));});
  await sleep(pg,8000); shot('3_saved');
  log('SAVED. actions added:', JSON.stringify(done));
  await b.close();
})().catch(e=>{ console.error('[add-action] ERR:',e.message); process.exit(1); });
