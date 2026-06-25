/**
 * inspect_company_fields.js (modern CS101500)
 * Dump every field-editor id (ed<View>-<Field>) on the Companies form so we know
 * the exact DAC view-cache + field names to map in the endpoint XML. Headless.
 *
 *   $env:GRP_BASE/USER/PASS; $env:NODE_PATH=(npm root -g)
 *   node inspect_company_fields.js
 */
const { chromium } = require('playwright');
const BASE=(process.env.GRP_BASE||'').replace(/\/+$/,'')+'/', USER=process.env.GRP_USER, PASS=process.env.GRP_PASS;
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
  await pg.goto(`${BASE}Main?ScreenId=CS101500`,{waitUntil:'domcontentloaded',timeout:60000});
  await sleep(pg,11000);
  const frame=pg.frames().find(f=>/CS101500\.html/.test(f.url())); if(!frame) throw new Error('CS101500.html frame not found');
  // enter a NEW record so all input fields render + enable
  await cmd(frame,'Insert'); await sleep(pg,4000);
  // walk through tabs to force-render tab panels (click each tab header)
  await frame.evaluate(()=>{ document.querySelectorAll('li[role="tab"],a[role="tab"],.tab-header,[data-name]').forEach(t=>{ try{t.dispatchEvent(new MouseEvent('click',{bubbles:true}));}catch(e){} }); });
  await sleep(pg,3000);
  const fields=await frame.evaluate(()=>{
    const out=[];
    const seen=new Set();
    document.querySelectorAll('[id]').forEach(el=>{
      const id=el.id||'';
      const m=id.match(/^ed([A-Za-z0-9]+)-([A-Za-z0-9_]+)$/);
      if(!m) return;
      const key=m[1]+'.'+m[2];
      if(seen.has(key)) return; seen.add(key);
      const txt=document.getElementById(id+'_text');
      out.push({view:m[1], field:m[2], tag:el.tagName.toLowerCase(),
        value:txt?(txt.value||''):'',
        visible:el.getBoundingClientRect().width>0});
    });
    return out.sort((a,b)=> a.view===b.view? a.field.localeCompare(b.field) : a.view.localeCompare(b.view));
  });
  // group by view for readability
  const byView={};
  for(const f of fields){ (byView[f.view]=byView[f.view]||[]).push(f); }
  console.log('FIELDS_JSON_START');
  console.log(JSON.stringify(byView,null,1));
  console.log('FIELDS_JSON_END');
  await b.close();
})().catch(e=>{ console.error('ERR:',e.message); process.exit(1); });
