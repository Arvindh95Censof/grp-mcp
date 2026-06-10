/**
 * add_endpoint_entity.js
 * ----------------------------------------------------------------------------
 * Extend an Acumatica web service endpoint (SM207060) by adding a top-level
 * entity mapped to a screen, populate its fields, and Save — fully headless.
 *
 * WHY THIS EXISTS: the contract `WebServiceEndpoints` entity is a stateful
 * wizard projection — a plain REST PUT to it is a NO-OP. The only reliable way
 * to extend a contract programmatically is to drive the SM207060 UI. This script
 * encodes the working click-path (control IDs + waits) discovered on csmdev.
 *
 * After it runs, the new entity is live in the contract immediately (endpoint
 * edits do NOT need a customization publish) and is usable via the grp-mcp API.
 *
 * USAGE (PowerShell):
 *   $env:GRP_BASE="https://csmdev.censof.com/2025R1Setup"
 *   $env:GRP_USER="<your-user>"; $env:GRP_PASS="********"
 *   $env:NODE_PATH=(npm root -g)
 *   node add_endpoint_entity.js --endpoint GRPSetup --version 24.200.001 `
 *        --entity AccountClass --screen "Account Classes" --view AccountClassRecords
 *
 * ARGS
 *   --endpoint  endpoint name to extend (e.g. GRPSetup)        [required]
 *   --version   endpoint version (e.g. 24.200.001)             [required]
 *   --entity    new top-level entity name (e.g. AccountClass)  [required]
 *   --screen    screen TITLE as shown in SM207060 lookup       [required]
 *               (e.g. "Account Classes" — NOT the GLxxxxxx id)
 *   --view      data view to populate fields from              [optional]
 *               (e.g. AccountClassRecords; if omitted, fields skipped)
 *   --debug     write screenshots to ./shots                   [optional flag]
 *
 * ENV: GRP_BASE, GRP_USER, GRP_PASS (no secrets are stored in this file).
 * Requires the login user's role to permit SM207060 edits, and Playwright
 * (`npm i -g playwright` + `npx playwright install chromium`).
 * ----------------------------------------------------------------------------
 */
const { chromium } = require('playwright');

function arg(name, def) {
  const i = process.argv.indexOf('--' + name);
  if (i === -1) return def;
  const v = process.argv[i + 1];
  return (!v || v.startsWith('--')) ? true : v;
}

const BASE = (process.env.GRP_BASE || '').replace(/\/+$/, '') + '/';
const USER = process.env.GRP_USER, PASS = process.env.GRP_PASS;
const EP = arg('endpoint'), VER = arg('version'), ENTITY = arg('entity');
const SCREEN = arg('screen'), VIEW = arg('view', null), DEBUG = !!arg('debug', false);

if (!BASE || !USER || !PASS) { console.error('Set GRP_BASE, GRP_USER, GRP_PASS env vars.'); process.exit(2); }
if (!EP || !VER || !ENTITY || !SCREEN) { console.error('Required: --endpoint --version --entity --screen'); process.exit(2); }

// Stable SM207060 control IDs (Acumatica 2024R2/2025R1 contract v4).
const ID = {
  epName: '#ctl00_phF_form_edInterfaceName_text',
  epVer: '#ctl00_phF_form_edGateVersion_text',
  insertNew: 'div.toolsBtn[data-cmd="InsertNew"]',
  ceName: '#ctl00_phG_pnlCreateEntity_formCreateEntity_edObjectName',
  ceScreen: '#ctl00_phG_pnlCreateEntity_formCreateEntity_edScreenID',
  ceScreenFb: '#ctl00_phG_pnlCreateEntity_formCreateEntity_edScreenID_pnl_tlb_fb_text',
  ceOK: '#ctl00_phG_pnlCreateEntity_btnOK',
  pfObj: '#ctl00_phG_pnlPopulateFields_formPopulateFields_PXTextEdit1',
  pfObjFb: '#ctl00_phG_pnlPopulateFields_formPopulateFields_PXTextEdit1_pnl_tlb_fb_text',
  pfOK: '#ctl00_phG_pnlPopulateFields_PXButton5',
};
const log = (...a) => console.log('[add-entity]', ...a);
const sleep = (pg, ms) => pg.waitForTimeout(ms);

(async () => {
  const b = await chromium.launch({ headless: true });
  const ctx = await b.newContext({ viewport: { width: 1600, height: 900 } });
  const pg = await ctx.newPage();
  const shot = (n) => DEBUG && pg.screenshot({ path: `shots/${n}.png` }).catch(() => {});

  // 1) login
  await pg.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await pg.locator("input[type='text']:visible, input:not([type]):visible").first().fill(USER);
  await pg.locator("input[type='password']:visible").first().fill(PASS);
  await pg.keyboard.press('Enter');
  await sleep(pg, 6000);
  if (/Login\.aspx/i.test(pg.url())) throw new Error('login failed');
  log('logged in');

  // 2) open SM207060 + locate the form frame
  await pg.goto(BASE + 'Main?ScreenId=SM207060', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(pg, 8000);
  const frame = pg.frames().find(f => /SM207060\.aspx/.test(f.url()));
  if (!frame) throw new Error('SM207060 frame not found');

  // 3) load the target endpoint (key selectors)
  for (const [sel, val] of [[ID.epName, EP], [ID.epVer, VER]]) {
    await frame.locator(sel).click(); await frame.locator(sel).fill(val);
    await frame.locator(sel).press('Enter'); await sleep(pg, 5000);
  }
  log('loaded endpoint', await frame.locator(ID.epName).inputValue(), VER);

  // guard: skip if entity already present
  const already = await frame.evaluate((n) => !![...document.querySelectorAll('*')]
    .find(e => e.children.length === 0 && (e.textContent || '').trim() === n), ENTITY);
  if (already) { log('entity already exists — nothing to do'); await b.close(); return; }

  // 4) Insert -> Create Entity dialog
  await frame.evaluate((s) => { const x = document.querySelector(s);
    ['mousedown', 'mouseup', 'click'].forEach(e => x.dispatchEvent(new MouseEvent(e, { bubbles: true }))); }, ID.insertNew);
  await sleep(pg, 4000);
  await frame.locator(ID.ceName).fill(ENTITY);

  // 5) pick screen via selector popup: magnifier -> filter box -> Enter (commits active row)
  await frame.locator(ID.ceScreen + ' .control-SelectorN').click();
  await sleep(pg, 3500);
  await frame.locator(ID.ceScreenFb).click();
  await frame.locator(ID.ceScreenFb).pressSequentially(SCREEN, { delay: 80 });
  await sleep(pg, 3500);
  await frame.locator(ID.ceScreenFb).press('Enter');
  await sleep(pg, 2500);
  const gotScreen = await frame.locator(ID.ceScreen + '_text').inputValue();
  log('screen selected:', gotScreen);
  if (gotScreen.trim().toLowerCase() !== SCREEN.trim().toLowerCase())
    throw new Error(`screen mismatch: wanted "${SCREEN}", got "${gotScreen}"`);
  shot('1_dialog');

  // 6) OK -> entity node added
  await frame.locator(ID.ceOK).click();
  await sleep(pg, 6000);
  const added = await frame.evaluate((n) => !![...document.querySelectorAll('*')]
    .find(e => e.children.length === 0 && (e.textContent || '').trim() === n), ENTITY);
  log('entity node added:', added);
  if (!added) throw new Error('entity not added');
  shot('2_added');

  // 7) populate fields (optional)
  if (VIEW) {
    // select the new node, open Populate Fields
    await frame.evaluate((n) => { const x = [...document.querySelectorAll('*')]
      .find(e => e.children.length === 0 && (e.textContent || '').trim() === n);
      ['mousedown', 'mouseup', 'click'].forEach(e => x.dispatchEvent(new MouseEvent(e, { bubbles: true }))); }, ENTITY);
    await sleep(pg, 2500);
    await frame.evaluate(() => { const x = document.querySelector('[data-cmd="PopulateFields"]');
      ['mousedown', 'mouseup', 'click'].forEach(e => x.dispatchEvent(new MouseEvent(e, { bubbles: true }))); });
    await sleep(pg, 4000);
    // pick Object (data view) via its selector popup
    await frame.locator(ID.pfObj + ' .control-SelectorN').click();
    await sleep(pg, 3000);
    await frame.locator(ID.pfObjFb).click();
    await frame.locator(ID.pfObjFb).pressSequentially(VIEW, { delay: 60 });
    await sleep(pg, 2500);
    await frame.locator(ID.pfObjFb).press('Enter');
    await sleep(pg, 4000);
    log('view selected:', await frame.locator(ID.pfObj + '_text').inputValue());
    // SELECT ALL -> OK
    await frame.evaluate(() => { const x = document.querySelector('[data-cmd="SelectAll"]');
      ['mousedown', 'mouseup', 'click'].forEach(e => x.dispatchEvent(new MouseEvent(e, { bubbles: true }))); });
    await sleep(pg, 2500);
    shot('3_fields');
    await frame.locator(ID.pfOK).click();
    await sleep(pg, 5000);
  }

  // 8) Save (endpoint edits go live immediately — no publish)
  await frame.evaluate(() => { const x = document.querySelector('[data-cmd="Save"]');
    ['mousedown', 'mouseup', 'click'].forEach(e => x.dispatchEvent(new MouseEvent(e, { bubbles: true }))); });
  await sleep(pg, 8000);
  shot('4_saved');
  log('SAVED. Verify with grp-mcp: get_entity_schema("' + ENTITY + '", refresh=true)');
  await b.close();
})().catch(e => { console.error('[add-entity] ERR:', e.message); process.exit(1); });
