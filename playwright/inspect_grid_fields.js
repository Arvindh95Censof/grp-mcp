/**
 * inspect_grid_fields.js - open a master-detail screen, insert a master record,
 * add a detail grid row, and dump ed<View>-<Field> ids (so we learn the DETAIL
 * view-cache name + fields). Headless, self-auth via env.
 *   node inspect_grid_fields.js --screen CS201010
 */
const { chromium } = require('playwright');
function arg(n,d){ const i=process.argv.indexOf('--'+n); if(i===-1) return d; const v=process.argv[i+1]; return (!v||v.startsWith('--'))?true:v; }
const BASE=(process.env.GRP_BASE||'').replace(/\/+$/,'')+'/', USER=process.env.GRP_USER, PASS=process.env.GRP_PASS;
const SCREEN=arg('screen','CS201010');
const sleep=(pg,ms)=>pg.waitForTimeout(ms);
const cmd=(frame,c)=>frame.evaluate(c=>{const x=document.querySelector(`[data-cmd="${c}"]`); if(x)['mousedown','mouseup','click'].forEach(e=>x.dispatchEvent(new MouseEvent(e,{bubbles:true}))); return !!x;},c);

(async()=>{
  const b=await chromium.launch({headless:true});
  const pg=await (await b.newContext({viewport:{width:1600,height:900}})).newPage();
  pg.on('dialog',d=>{ d.accept().catch(()=>{}); });
  await pg.goto(BASE,{waitUntil:'domcontentloaded',timeout:60000});
  await pg.locator("input[type='text']:visible, input:not([type]):visible").first().fill(USER);
  await pg.locator("input[type='password']:visible").first().fill(PASS);
  await pg.keyboard.press('Enter'); await sleep(pg,6000);
  await pg.goto(`${BASE}Main?ScreenId=${SCREEN}`,{waitUntil:'domcontentloaded',timeout:60000});
  await sleep(pg,11000);
  const frame=pg.frames().find(f=>new RegExp(SCREEN+'\\.html').test(f.url())); if(!frame) throw new Error(SCREEN+'.html frame not found');
  await cmd(frame,'Insert'); await sleep(pg,3000);
  // dump grid component metadata
  const grids=await frame.evaluate(()=>{
    return [...document.querySelectorAll('qp-grid,[class*="grid"]')].slice(0,12).map(g=>({
      tag:g.tagName.toLowerCase(), id:g.id||'',
      view:g.getAttribute('view')||g.getAttribute('name')||g.getAttribute('data-view')||''
    })).filter(x=>x.id||x.view);
  });
  // try to add a detail row: click first grid, press Insert
  await frame.evaluate(()=>{ const g=document.querySelector('qp-grid'); if(g){ g.scrollIntoView(); const cell=g.querySelector('div,td'); if(cell) cell.dispatchEvent(new MouseEvent('click',{bubbles:true})); } });
  await sleep(pg,1500);
  await pg.keyboard.press('Insert'); await sleep(pg,2500);
  const fields=await frame.evaluate(()=>{
    const out=[],seen=new Set();
    document.querySelectorAll('[id]').forEach(el=>{
      const m=(el.id||'').match(/^ed([A-Za-z0-9]+)-([A-Za-z0-9_]+)$/);
      if(!m) return; const k=m[1]+'.'+m[2]; if(seen.has(k))return; seen.add(k);
      out.push({view:m[1],field:m[2]});
    });
    return out.sort((a,b)=> a.view===b.view?a.field.localeCompare(b.field):a.view.localeCompare(b.view));
  });
  const byView={}; for(const f of fields){ (byView[f.view]=byView[f.view]||[]).push(f.field); }
  console.log('GRIDS',JSON.stringify(grids));
  console.log('FIELDS_JSON_START'); console.log(JSON.stringify(byView,null,1)); console.log('FIELDS_JSON_END');
  await b.close();
})().catch(e=>{ console.error('ERR:',e.message); process.exit(1); });
