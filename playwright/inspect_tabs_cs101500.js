/**
 * inspect_tabs_cs101500.js - per-TAB field/view inventory for the Company screen
 * (CS101500). Clicks each tab, dumps visible form-editor view.field ids, and for
 * grid tabs lists the grid + its column field names. Used to plan endpoint mapping.
 *   node inspect_tabs_cs101500.js
 */
const { chromium } = require('playwright');
const BASE=(process.env.GRP_BASE||'').replace(/\/+$/,'')+'/', USER=process.env.GRP_USER, PASS=process.env.GRP_PASS;
function arg(n,d){ const i=process.argv.indexOf('--'+n); if(i===-1) return d; const v=process.argv[i+1]; return (!v||v.startsWith('--'))?true:v; }
const SCREEN=arg('screen','CS101500');
const sleep=(pg,ms)=>pg.waitForTimeout(ms);
const cmd=(frame,c)=>frame.evaluate(c=>{const x=document.querySelector(`[data-cmd="${c}"]`); if(x){['mousedown','mouseup','click'].forEach(e=>x.dispatchEvent(new MouseEvent(e,{bubbles:true})));return true;} return false;},c);

(async()=>{
  const b=await chromium.launch({headless:true});
  const pg=await (await b.newContext({viewport:{width:1600,height:1000}})).newPage();
  pg.on('dialog',d=>{ d.accept().catch(()=>{}); });
  await pg.goto(BASE,{waitUntil:'domcontentloaded',timeout:60000});
  await pg.locator("input[type='text']:visible, input:not([type]):visible").first().fill(USER);
  await pg.locator("input[type='password']:visible").first().fill(PASS);
  await pg.keyboard.press('Enter'); await sleep(pg,6000);
  await pg.goto(`${BASE}Main?ScreenId=${SCREEN}`,{waitUntil:'domcontentloaded',timeout:60000});
  await sleep(pg,11000);
  const frame=pg.frames().find(f=>new RegExp(SCREEN+'\\.html').test(f.url())); if(!frame) throw new Error('frame');
  await cmd(frame,'Insert'); await sleep(pg,3500);

  // tabs are <div ...> inside [id="tabs_tab<Name>_tabbar"]
  const tabs=await frame.evaluate(()=>[...document.querySelectorAll('[id^="tabs_tab"][id$="_tabbar"]')]
    .map(e=>({id:e.id, text:(e.textContent||'').replace(/\s+/g,' ').trim()})));
  console.log('TABS FOUND:', JSON.stringify(tabs));

  const dumpVisible=()=>frame.evaluate(()=>{
    const out={}, seen=new Set();
    document.querySelectorAll('[id]').forEach(el=>{
      const m=(el.id||'').match(/^ed([A-Za-z0-9]+)-([A-Za-z0-9_]+)$/);
      if(!m) return; const f=m[2].replace(/_(text|btn|label|link)$/,''); const key=m[1]+'.'+f;
      if(seen.has(key)) return;
      if(el.getBoundingClientRect().width===0) return; // only visible (current tab)
      seen.add(key); (out[m[1]]=out[m[1]]||new Set()).add(f);
    });
    // grids on the current tab: capture grid view + column field names
    const grids=[];
    document.querySelectorAll('qp-grid, [class*="grid"]').forEach(g=>{
      if(g.getBoundingClientRect().width===0) return;
      const cols=new Set();
      g.querySelectorAll('[data-field],[col-id],[data-id]').forEach(c=>{
        const v=c.getAttribute('data-field')||c.getAttribute('col-id')||c.getAttribute('data-id');
        if(v && /^[A-Za-z][A-Za-z0-9_]*$/.test(v)) cols.add(v);
      });
      const view=g.getAttribute('id')||g.getAttribute('data-view')||g.getAttribute('name')||'';
      if(cols.size) grids.push({view, columns:[...cols].slice(0,40)});
    });
    const res={}; for(const k in out) res[k]=[...out[k]];
    return {editors:res, grids};
  });

  for(const tab of tabs){
    const clicked=await frame.evaluate(id=>{
      const el=document.getElementById(id); if(!el) return false;
      ['mousedown','mouseup','click'].forEach(ev=>el.dispatchEvent(new MouseEvent(ev,{bubbles:true})));
      return true;
    }, tab.id);
    await sleep(pg,2800);
    // for grid tabs, try to add a row so column editors materialize
    await cmd(frame,'gridInsertNew'); await sleep(pg,800);
    const data=await dumpVisible();
    console.log(`\n===== TAB: ${tab.text}  (clicked=${clicked}) =====`);
    console.log('  editors(view->fields):', JSON.stringify(data.editors));
    if(data.grids.length) console.log('  grids:', JSON.stringify(data.grids));
  }
  await b.close();
})().catch(e=>{ console.error('ERR:',e.message); process.exit(1); });
