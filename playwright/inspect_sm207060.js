/**
 * inspect_sm207060.js - open SM207060 for an endpoint, dump the entity-tree leaf
 * texts and all [data-cmd] toolbar commands so we can drive node deletion correctly.
 *   node inspect_sm207060.js --endpoint GRPSetup --version 24.200.001
 */
const { chromium } = require('playwright');
function arg(n,d){ const i=process.argv.indexOf('--'+n); if(i===-1) return d; const v=process.argv[i+1]; return (!v||v.startsWith('--'))?true:v; }
const BASE=(process.env.GRP_BASE||'').replace(/\/+$/,'')+'/', USER=process.env.GRP_USER, PASS=process.env.GRP_PASS;
const EP=arg('endpoint','GRPSetup'), VER=arg('version','24.200.001');
const sleep=(pg,ms)=>pg.waitForTimeout(ms);
(async()=>{
  const b=await chromium.launch({headless:true});
  const pg=await (await b.newContext({viewport:{width:1600,height:900}})).newPage();
  pg.on('dialog',d=>{ d.accept().catch(()=>{}); });
  await pg.goto(BASE,{waitUntil:'domcontentloaded',timeout:60000});
  await pg.locator("input[type='text']:visible, input:not([type]):visible").first().fill(USER);
  await pg.locator("input[type='password']:visible").first().fill(PASS);
  await pg.keyboard.press('Enter'); await sleep(pg,6000);
  await pg.goto(`${BASE}Main?ScreenId=SM207060&InterfaceName=${encodeURIComponent(EP)}&GateVersion=${VER}`,{waitUntil:'domcontentloaded',timeout:60000});
  await sleep(pg,9000);
  const frame=pg.frames().find(f=>/SM207060\.html/.test(f.url())); if(!frame) throw new Error('frame');
  const info=await frame.evaluate(()=>{
    const cmds=[...document.querySelectorAll('[data-cmd]')].map(e=>e.getAttribute('data-cmd'));
    // tree nodes: look for tree/treeview leaf texts
    const treeEls=[...document.querySelectorAll('[class*="tree"] *, qp-tree *, li, span, div')]
      .filter(e=>e.children.length===0 && e.getBoundingClientRect().width>0)
      .map(e=>(e.textContent||'').trim())
      .filter(t=>t && t.length<40);
    const uniq=[...new Set(treeEls)];
    const finYear=uniq.filter(t=>/Financial|FinYear|Numbering|Segmented|Company|Branch/i.test(t));
    return {cmds:[...new Set(cmds)], finYearMatches:finYear};
  });
  console.log('CMDS', JSON.stringify(info.cmds));
  console.log('TREEHITS', JSON.stringify(info.finYearMatches));
  await b.close();
})().catch(e=>{ console.error('ERR:',e.message); process.exit(1); });
