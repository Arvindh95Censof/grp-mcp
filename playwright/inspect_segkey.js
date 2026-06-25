/**
 * inspect_segkey.js - load an existing segmented key on CS202000, open its segment
 * grid row in edit mode, and dump ed<View>-<Field> ids + grid attributes so we learn
 * the SEGMENT detail view-cache name + fields. Headless.
 *   node inspect_segkey.js --key ACCOUNT
 */
const { chromium } = require('playwright');
function arg(n,d){ const i=process.argv.indexOf('--'+n); if(i===-1) return d; const v=process.argv[i+1]; return (!v||v.startsWith('--'))?true:v; }
const BASE=(process.env.GRP_BASE||'').replace(/\/+$/,'')+'/', USER=process.env.GRP_USER, PASS=process.env.GRP_PASS;
const KEY=arg('key','ACCOUNT');
const sleep=(pg,ms)=>pg.waitForTimeout(ms);
(async()=>{
  const b=await chromium.launch({headless:true});
  const pg=await (await b.newContext({viewport:{width:1600,height:900}})).newPage();
  pg.on('dialog',d=>{ d.accept().catch(()=>{}); });
  await pg.goto(BASE,{waitUntil:'domcontentloaded',timeout:60000});
  await pg.locator("input[type='text']:visible, input:not([type]):visible").first().fill(USER);
  await pg.locator("input[type='password']:visible").first().fill(PASS);
  await pg.keyboard.press('Enter'); await sleep(pg,6000);
  await pg.goto(`${BASE}Main?ScreenId=CS202000`,{waitUntil:'domcontentloaded',timeout:60000});
  await sleep(pg,9000);
  const frame=pg.frames().find(f=>/CS202000\.html/.test(f.url())); if(!frame) throw new Error('frame');
  // load the key into the Header.DimensionID selector
  await frame.evaluate(k=>{ const t=document.getElementById('edHeader-DimensionID_text'); if(t){ t.value=k; t.dispatchEvent(new Event('input',{bubbles:true})); t.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true})); } }, KEY);
  await sleep(pg,5000);
  // grid attributes
  const grids=await frame.evaluate(()=>[...document.querySelectorAll('qp-grid')].map(g=>{
    const a={}; for(const at of g.attributes) a[at.name]=at.value; return {id:g.id, attrs:a};
  }));
  // double-click first data row in the segment grid to enter edit
  await frame.evaluate(()=>{ const g=document.querySelector('qp-grid'); if(g){ const row=g.querySelector('[class*="row"] [class*="cell"], tr td, [role="gridcell"]'); if(row){ row.scrollIntoView(); ['mousedown','mouseup','click','dblclick'].forEach(e=>row.dispatchEvent(new MouseEvent(e,{bubbles:true}))); } } });
  await sleep(pg,2500);
  const fields=await frame.evaluate(()=>{
    const out=[],seen=new Set();
    document.querySelectorAll('[id]').forEach(el=>{ const m=(el.id||'').match(/^ed([A-Za-z0-9]+)-([A-Za-z0-9_]+)$/); if(!m)return; const k=m[1]+'.'+m[2]; if(seen.has(k))return; seen.add(k); out.push(m[1]+'.'+m[2]); });
    return out.sort();
  });
  console.log('GRID_ATTRS', JSON.stringify(grids).slice(0,1500));
  console.log('ED_IDS_START'); console.log(JSON.stringify(fields,null,0)); console.log('ED_IDS_END');
  await b.close();
})().catch(e=>{ console.error('ERR:',e.message); process.exit(1); });
