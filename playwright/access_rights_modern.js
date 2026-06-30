/**
 * access_rights_modern.js — read (and optionally grant) screen access rights on
 * SM201020 (Access Rights by Screen), modern Aurelia UI. Headless, self-auth via env.
 *
 *   node access_rights_modern.js --screen-title "Segment Values"            # read grid
 *   node access_rights_modern.js --screen-title "Segment Values" --grant \
 *        --role "Administrator" --right Delete                              # grant + save
 *
 * ENV: GRP_BASE, GRP_USER, GRP_PASS.
 */
const { chromium } = require('playwright');
function arg(n, d){ const i=process.argv.indexOf('--'+n); if(i===-1) return d; const v=process.argv[i+1]; return (!v||v.startsWith('--'))?true:v; }
const BASE=(process.env.GRP_BASE||'').replace(/\/+$/,'')+'/', USER=process.env.GRP_USER, PASS=process.env.GRP_PASS;
const SCREEN_TITLE=arg('screen-title','Segment Values');
const ROLE=arg('role',null), RIGHT=arg('right','Delete'), DO_GRANT=!!arg('grant',false);
const sleep=(pg,ms)=>pg.waitForTimeout(ms);
const cmd=(frame,c)=>frame.evaluate((c)=>{const x=document.querySelector(`[data-cmd="${c}"]`); if(x)['mousedown','mouseup','click'].forEach(e=>x.dispatchEvent(new MouseEvent(e,{bubbles:true})));},c);

(async()=>{
  const b=await chromium.launch({headless:true});
  const pg=await (await b.newContext({viewport:{width:1700,height:950}})).newPage();
  pg.on('dialog',d=>d.accept().catch(()=>{}));
  await pg.goto(BASE,{waitUntil:'domcontentloaded',timeout:60000});
  await pg.locator("input[type='text']:visible, input:not([type]):visible").first().fill(USER);
  await pg.locator("input[type='password']:visible").first().fill(PASS);
  await pg.keyboard.press('Enter'); await sleep(pg,6000);
  await pg.goto(`${BASE}Main?ScreenId=SM201020`,{waitUntil:'domcontentloaded',timeout:60000});
  await sleep(pg,11000);
  const frame=pg.frames().find(f=>/SM201020\.html/.test(f.url()))||pg.mainFrame();

  const hasNode=(t)=>frame.evaluate((t)=>!![...document.querySelectorAll('*')].find(e=>e.children.length===0&&(e.textContent||'').trim()===t&&e.getBoundingClientRect().width>0),t);
  let found=false;
  for(let pass=0; pass<10 && !found; pass++){
    found=await hasNode(SCREEN_TITLE);
    if(found) break;
    await frame.evaluate(()=>{ // click every collapsed tree arrow
      [...document.querySelectorAll('i[ref="arrow"],[class*="tree"] [class*="arrow"],[class*="collaps"],[class*="expand"]')]
        .forEach(a=>{try{['mousedown','mouseup','click'].forEach(e=>a.dispatchEvent(new MouseEvent(e,{bubbles:true})));}catch(e){}});
    });
    await sleep(pg,2200);
  }
  if(!found){ console.log('NODE_NOT_FOUND:'+SCREEN_TITLE); await pg.screenshot({path:'shots/sm201020_notfound.png'}).catch(()=>{}); await b.close(); return; }

  await frame.evaluate((t)=>{ const n=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&(e.textContent||'').trim()===t&&e.getBoundingClientRect().width>0); if(n){n.scrollIntoView({block:'center'});['mousedown','mouseup','click'].forEach(e=>n.dispatchEvent(new MouseEvent(e,{bubbles:true})));} }, SCREEN_TITLE);
  await sleep(pg,4500);
  console.log('SELECTED_NODE:'+SCREEN_TITLE);

  const rows=await frame.evaluate(()=>{
    const out=[];
    document.querySelectorAll('tr,[role="row"]').forEach(r=>{ const t=(r.textContent||'').replace(/\s+/g,' ').trim(); if(t && t.length<160 && /[A-Za-z]/.test(t)) out.push(t); });
    return [...new Set(out)].slice(0,50);
  });
  console.log('GRID_ROWS', JSON.stringify(rows,null,1));
  await pg.screenshot({path:'shots/sm201020_cs203000.png'}).catch(()=>{});
  await b.close();
})().catch(e=>{ console.error('ERR',e.message); process.exit(1); });
