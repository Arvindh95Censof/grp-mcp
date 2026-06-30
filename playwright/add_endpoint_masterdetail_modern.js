/**
 * add_endpoint_masterdetail_modern.js  (modern Aurelia SM207060)
 * Add a TOP-LEVEL entity plus a DETAIL collection under it, headless. For
 * context/master-detail screens (e.g. CS203000 Segment Values) whose value grid
 * only accepts inserts once the parent row is loaded — a flat entity can't do that.
 *
 *   node add_endpoint_masterdetail_modern.js --endpoint GRPSetup --version 24.200.001 \
 *     --entity SegmentValueSet --screen "Segment Values" --master-view "Segment Summary" \
 *     --detail-field Values --detail-entity SegmentValueLine --detail-view "Possible Values"
 *
 * ENV: GRP_BASE, GRP_USER, GRP_PASS.  Live on Save (no customization publish).
 */
const { chromium } = require('playwright');
function arg(n,d){ const i=process.argv.indexOf('--'+n); if(i===-1) return d; const v=process.argv[i+1]; return (!v||v.startsWith('--'))?true:v; }
const BASE=(process.env.GRP_BASE||'').replace(/\/+$/,'')+'/', USER=process.env.GRP_USER, PASS=process.env.GRP_PASS;
const EP=arg('endpoint'), VER=arg('version'), ENTITY=arg('entity'), SCREEN=arg('screen');
const MVIEW=arg('master-view'), DFIELD=arg('detail-field'), DENT=arg('detail-entity'), DVIEW=arg('detail-view');
const DEBUG=!!arg('debug',false);
const CE='edCreateEntityView-';
const log=(...a)=>console.log('[md]',...a);
const sleep=(pg,ms)=>pg.waitForTimeout(ms);
const cmd=(f,c)=>f.evaluate(c=>{const x=document.querySelector(`[data-cmd="${c}"]`);if(x)['mousedown','mouseup','click'].forEach(e=>x.dispatchEvent(new MouseEvent(e,{bubbles:true})));},c);
const clickOK=(f)=>f.evaluate(()=>{const ok=[...document.querySelectorAll('button')].filter(x=>(x.textContent||'').trim()==='OK'&&x.getBoundingClientRect().width>0);if(ok.length)ok[ok.length-1].click();});
// dismiss the "SaveChanges — save and continue?" modal that InsertNew triggers on a dirty form
const clickYes=(f)=>f.evaluate(()=>{const y=[...document.querySelectorAll('button')].find(b=>(b.textContent||'').trim()==='Yes'&&b.getBoundingClientRect().width>0);if(y){y.click();return true;}return false;});
async function pickDropdown(frame,pg,id,optionText){
  await frame.evaluate((id)=>{const el=document.getElementById(id+'_text')||document.getElementById(id);if(el){el.focus();['mousedown','mouseup','click'].forEach(e=>el.dispatchEvent(new MouseEvent(e,{bubbles:true})));}},id);
  await sleep(pg,1500);
  await frame.evaluate((t)=>{const o=[...document.querySelectorAll('li,div,span,[role="option"],.qp-dropdown-item')].find(e=>e.children.length===0&&(e.textContent||'').trim()===t&&e.getBoundingClientRect().width>0);if(o)['mousedown','mouseup','click'].forEach(e=>o.dispatchEvent(new MouseEvent(e,{bubbles:true})));},optionText);
  await sleep(pg,1200);
}
const selectNode=(f,t,exact=true)=>f.evaluate(({t,exact})=>{const x=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&(exact?(e.textContent||'').trim()===t:(e.textContent||'').trim().startsWith(t))&&e.getBoundingClientRect().width>0);if(x){x.scrollIntoView({block:'center'});['mousedown','mouseup','click'].forEach(e=>x.dispatchEvent(new MouseEvent(e,{bubbles:true})));return true;}return false;},{t,exact});
const present=(f,t)=>f.evaluate(t=>!![...document.querySelectorAll('*')].find(e=>e.children.length===0&&(e.textContent||'').trim()===t),t);
async function pickSelector(frame,pg,containerId,search,optionText){
  await frame.evaluate((c)=>{const btn=document.querySelector(`#${c} button.qp-field-editor__button`);if(btn)['mousedown','mouseup','click'].forEach(e=>btn.dispatchEvent(new MouseEvent(e,{bubbles:true})));},containerId);
  await sleep(pg,3000);
  await frame.evaluate(({c,s})=>{const el=document.getElementById(`${c}_pnl_gr_fb_text`);if(el){el.focus();el.value=s;el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',keyCode:13,which:13,bubbles:true}));}},{c:containerId,s:search});
  await sleep(pg,3500);
  await frame.evaluate((t)=>{
    const els=[...document.querySelectorAll('*')].filter(e=>e.children.length===0&&e.getBoundingClientRect().width>0);
    let el=els.find(e=>(e.textContent||'').trim()===t) || els.find(e=>(e.textContent||'').trim().includes(t));
    if(el){el.scrollIntoView({block:'center'});['mousedown','mouseup','click','dblclick'].forEach(ev=>el.dispatchEvent(new MouseEvent(ev,{bubbles:true})));}
  },optionText);
  await sleep(pg,1200);
  await frame.evaluate(()=>{const s=[...document.querySelectorAll('button,div,span,a')].find(e=>(e.textContent||'').trim()==='Select'&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().width<160);if(s)['mousedown','mouseup','click'].forEach(ev=>s.dispatchEvent(new MouseEvent(ev,{bubbles:true})));});
  await sleep(pg,2500);
}
async function setDropdown(frame,pg,id,val){
  await frame.evaluate(({id,val})=>{
    const inp=document.getElementById(id+'_text')||document.getElementById(id);
    if(inp){inp.focus();inp.value=val;inp.dispatchEvent(new Event('input',{bubbles:true}));inp.dispatchEvent(new Event('change',{bubbles:true}));inp.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',keyCode:13,which:13,bubbles:true}));if(inp.blur)inp.blur();}
  },{id,val});
  await sleep(pg,1500);
}
async function populate(frame,pg,view,shotName,shot){
  await cmd(frame,'PopulateFields'); await sleep(pg,4500);
  await pickSelector(frame,pg,'edPopulateFilterView-Container',view,view);
  const bound=await frame.locator('#edPopulateFilterView-Container_text').inputValue().catch(()=>'');
  log('populate view bound:', bound);
  if(shot) shot(shotName+'_dialog');
  const fieldCount=await frame.evaluate(()=>document.querySelectorAll('[role="row"],tr').length);
  log('  dialog rows ~', fieldCount);
  await cmd(frame,'SelectAll'); await sleep(pg,2500);
  await clickOK(frame); await sleep(pg,5000);
}
(async()=>{
  const b=await chromium.launch({headless:true});
  const pg=await (await b.newContext({viewport:{width:1700,height:950}})).newPage();
  const shot=(n)=>DEBUG&&pg.screenshot({path:`shots/${n}.png`}).catch(()=>{});
  pg.on('dialog',d=>d.accept().catch(()=>{}));
  await pg.goto(BASE,{waitUntil:'domcontentloaded',timeout:60000});
  await pg.locator("input[type='text']:visible, input:not([type]):visible").first().fill(USER);
  await pg.locator("input[type='password']:visible").first().fill(PASS);
  await pg.keyboard.press('Enter'); await sleep(pg,6000);
  if(/Login\.aspx/i.test(pg.url())) throw new Error('login failed');
  await pg.goto(`${BASE}Main?ScreenId=SM207060&InterfaceName=${encodeURIComponent(EP)}&GateVersion=${VER}`,{waitUntil:'domcontentloaded',timeout:60000});
  await sleep(pg,9000);
  const frame=pg.frames().find(f=>/SM207060\.html/.test(f.url())); if(!frame) throw new Error('SM207060.html frame not found');
  log('endpoint loaded', VER);
  if(await present(frame,ENTITY)){ log('top-level entity already exists — aborting (delete it first)'); await b.close(); return; }

  // 1) top-level entity
  await cmd(frame,'InsertNew'); await sleep(pg,4000);
  await frame.locator('#'+CE+'ObjectName').fill(ENTITY);
  await pickSelector(frame,pg,CE+'ScreenID',SCREEN,SCREEN);
  const sv=await frame.locator('#'+CE+'ScreenIDValue').inputValue().catch(()=>'');
  log('screen bound:', sv); if(!sv) throw new Error('screen not resolved');
  await clickOK(frame); await sleep(pg,6000);
  if(!await present(frame,ENTITY)) throw new Error('top-level node not added'); shot('1_toplevel');

  // 2) populate master fields, then SAVE (so adding the detail won't trigger the
  //    "SaveChanges — save and continue?" modal that blocks the Create dialog)
  await selectNode(frame,ENTITY); await sleep(pg,1500);
  await populate(frame,pg,MVIEW,'2_master',shot); shot('2_master_fields');
  await cmd(frame,'Save'); await sleep(pg,7000); await clickYes(frame); await sleep(pg,4000);
  log('top-level saved'); shot('2b_saved');

  // 3) add detail under the entity (Object Type MUST be Detail)
  await selectNode(frame,ENTITY); await sleep(pg,1500);
  await cmd(frame,'InsertNew'); await sleep(pg,3000);
  await clickYes(frame); await sleep(pg,3000);                 // dismiss SaveChanges if it still appears
  shot('3a_detail_dialog');
  await frame.locator('#'+CE+'FieldName').fill(DFIELD).catch(()=>{});
  await frame.locator('#'+CE+'ObjectName').fill(DENT).catch(()=>{});
  await pickDropdown(frame,pg,CE+'ObjectType','Detail');
  log('object type set:', await frame.locator('#'+CE+'ObjectType_text').inputValue().catch(()=>'?'));
  shot('3b_detail_filled');
  await clickOK(frame); await sleep(pg,6000); shot('3c_detail_created');
  if(!await present(frame,DFIELD) && !await present(frame,DENT)) log('WARN: detail node not obviously present — continuing');

  // 4) populate detail fields (select the detail node by its field name prefix)
  await selectNode(frame,DFIELD,false); await sleep(pg,1500);
  await populate(frame,pg,DVIEW,'4_detail',shot); shot('4_detail_fields');

  // 5) Save (live)
  await cmd(frame,'Save'); await sleep(pg,9000); shot('5_saved');
  log(`SAVED. Verify: get_entity_schema("${ENTITY}", deep=true, refresh=true)`);
  await b.close();
})().catch(e=>{ console.error('[md] ERR:',e.message); process.exit(1); });
