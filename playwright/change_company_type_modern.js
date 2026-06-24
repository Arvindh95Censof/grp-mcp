/**
 * change_company_type_modern.js (modern CS101500)
 * Set a company's Organization Type combobox (e.g. "Without Branches" ->
 * "With Branches, Not Requiring Balancing") and Save. Headless.
 *
 *   $env:GRP_BASE/USER/PASS; $env:NODE_PATH=(npm root -g)
 *   node change_company_type_modern.js --match "Not Requiring" --debug
 */
const { chromium } = require('playwright');
function arg(n,d){ const i=process.argv.indexOf('--'+n); if(i===-1) return d; const v=process.argv[i+1]; return (!v||v.startsWith('--'))?true:v; }
const BASE=(process.env.GRP_BASE||'').replace(/\/+$/,'')+'/', USER=process.env.GRP_USER, PASS=process.env.GRP_PASS;
const MATCH=arg('match','Not Requiring'), DEBUG=!!arg('debug',false);
const FIELD='edOrganizationView-OrganizationType';
const log=(...a)=>console.log('[company-type]',...a);
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
  await pg.keyboard.press('Enter'); await sleep(pg,6000); log('logged in');
  await pg.goto(`${BASE}Main?ScreenId=CS101500`,{waitUntil:'domcontentloaded',timeout:60000});
  await sleep(pg,11000);
  const frame=pg.frames().find(f=>/CS101500\.html/.test(f.url())); if(!frame) throw new Error('CS101500.html frame not found');
  const before=await frame.locator('#'+FIELD+'_text').inputValue().catch(()=>'');
  log('current type:', JSON.stringify(before));

  // open the combobox via mousedown on the editorWrap (mousedown.trigger=toggleSelector)
  await frame.evaluate(id=>{const w=document.getElementById(id); ['mousedown','mouseup'].forEach(e=>w.dispatchEvent(new MouseEvent(e,{bubbles:true})));},FIELD);
  await sleep(pg,2500); shot('1_dropdown');
  // pick the option matching MATCH from the rendered list (leaf elements)
  const picked=await frame.evaluate(m=>{
    const re=new RegExp(m,'i');
    const all=[...document.querySelectorAll('div,li,span,a')].filter(e=>e.children.length===0&&e.getBoundingClientRect().width>0&&/Branch/i.test(e.textContent||'')&&(e.textContent||'').trim().length<60).map(e=>(e.textContent||'').trim());
    const t=[...document.querySelectorAll('div,li,span,a')].find(e=>e.children.length===0&&e.getBoundingClientRect().width>0&&re.test((e.textContent||'').trim())&&(e.textContent||'').trim().length<60);
    if(t){ t.scrollIntoView({block:'center'}); ['mousedown','mouseup','click'].forEach(e=>t.dispatchEvent(new MouseEvent(e,{bubbles:true}))); }
    return {options:[...new Set(all)].slice(0,8), clicked:t?(t.textContent||'').trim():null};
  },MATCH);
  log('options seen:', JSON.stringify(picked.options));
  log('clicked option:', JSON.stringify(picked.clicked));
  await sleep(pg,2000); shot('2_picked');
  const after=await frame.locator('#'+FIELD+'_text').inputValue().catch(()=>'');
  log('type after pick:', JSON.stringify(after));
  if(!/Branch/i.test(after) || /Without/i.test(after)){ log('WARN: type not changed to a with-branches value'); }
  await cmd(frame,'Save'); await sleep(pg,6000); shot('3_saved');
  const saved=await frame.locator('#'+FIELD+'_text').inputValue().catch(()=>'');
  log('type after save:', JSON.stringify(saved));
  await b.close();
})().catch(e=>{ console.error('[company-type] ERR:',e.message); process.exit(1); });
