/**
 * inspect_screen_fields.js  - dump every field-editor id (ed<View>-<Field>) on a
 * modern Acumatica form, so we know exact DAC view-cache + field names to map in
 * the endpoint XML. Headless, self-auth via env.
 *
 *   $env:GRP_BASE/USER/PASS; $env:NODE_PATH=(npm root -g)
 *   node inspect_screen_fields.js --screen CS102000
 */
const { chromium } = require('playwright');
function arg(n,d){ const i=process.argv.indexOf('--'+n); if(i===-1) return d; const v=process.argv[i+1]; return (!v||v.startsWith('--'))?true:v; }
const BASE=(process.env.GRP_BASE||'').replace(/\/+$/,'')+'/', USER=process.env.GRP_USER, PASS=process.env.GRP_PASS;
const SCREEN=arg('screen','CS102000');
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
  await cmd(frame,'Insert'); await sleep(pg,4000);
  await frame.evaluate(()=>{ document.querySelectorAll('li[role="tab"],a[role="tab"],.tab-header,[data-name]').forEach(t=>{ try{t.dispatchEvent(new MouseEvent('click',{bubbles:true}));}catch(e){} }); });
  await sleep(pg,3000);
  const fields=await frame.evaluate(()=>{
    const out=[],seen=new Set();
    document.querySelectorAll('[id]').forEach(el=>{
      const m=(el.id||'').match(/^ed([A-Za-z0-9]+)-([A-Za-z0-9_]+)$/);
      if(!m) return; const key=m[1]+'.'+m[2]; if(seen.has(key)) return; seen.add(key);
      const txt=document.getElementById(el.id+'_text');
      out.push({view:m[1],field:m[2],tag:el.tagName.toLowerCase(),value:txt?(txt.value||''):'',visible:el.getBoundingClientRect().width>0});
    });
    return out.sort((a,b)=> a.view===b.view? a.field.localeCompare(b.field): a.view.localeCompare(b.view));
  });
  const byView={}; for(const f of fields){ (byView[f.view]=byView[f.view]||[]).push(f); }
  console.log('FIELDS_JSON_START'); console.log(JSON.stringify(byView,null,1)); console.log('FIELDS_JSON_END');
  await b.close();
})().catch(e=>{ console.error('ERR:',e.message); process.exit(1); });
