/**
 * add_endpoint_entity_modern.js
 * ----------------------------------------------------------------------------
 * MODERN-UI variant of add_endpoint_entity.js — for Acumatica 2025R2+/2026R1
 * instances that render SM207060 as the Aurelia `.html` UI (not the classic
 * `.aspx`). Adds a top-level entity mapped to a screen, populates its fields,
 * and Saves — fully headless.
 *
 * Verified on localhost/2026R1 (build 26.x), endpoint GRPSetup/25.200.001,
 * adding AccountClass -> screen "Account Classes" -> view AccountClassRecords.
 *
 * WHY A SEPARATE SCRIPT: the modern UI uses different control IDs and a
 * different selector-popup mechanism than classic (see EXTENDING_ENDPOINTS.md
 * "Modern UI" section). Classic Playwright `.click()` is overlay-blocked here, so
 * toolbar commands, OK buttons, grid rows and the green "Select" button are all
 * driven via dispatched MouseEvents in frame.evaluate.
 *
 * USAGE (PowerShell):
 *   $env:GRP_BASE="http://localhost/2026R1"
 *   $env:GRP_USER="admin"; $env:GRP_PASS="********"
 *   $env:NODE_PATH=(npm root -g)
 *   node add_endpoint_entity_modern.js --endpoint GRPSetup --version 25.200.001 `
 *        --entity AccountClass --screen "Account Classes" --view AccountClassRecords
 *
 * ARGS (same as the classic script)
 *   --endpoint --version --entity --screen   [required]
 *   --view     data view to populate fields from (omit -> entity created fieldless)
 *   --debug    write screenshots to ./shots
 *
 * ENV: GRP_BASE, GRP_USER, GRP_PASS (no secrets stored in this file).
 * The endpoint must already exist (use the Extend Endpoint flow first if needed).
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

const CE = 'edCreateEntityView-';
const log = (...a) => console.log('[add-entity-modern]', ...a);
const sleep = (pg, ms) => pg.waitForTimeout(ms);

// Fire an Acumatica toolbar/command button by its data-cmd (Playwright clicks are overlay-blocked).
const cmd = (frame, c) => frame.evaluate((c) => {
  const x = document.querySelector(`[data-cmd="${c}"]`);
  if (x) ['mousedown', 'mouseup', 'click'].forEach(e => x.dispatchEvent(new MouseEvent(e, { bubbles: true })));
}, c);
// Click the dialog OK (modern OK is a <button>OK</button>; multiple may exist — last visible wins).
const clickOK = (frame) => frame.evaluate(() => {
  const ok = [...document.querySelectorAll('button')].filter(x => (x.textContent || '').trim() === 'OK' && x.getBoundingClientRect().width > 0);
  if (ok.length) ok[ok.length - 1].click();
});
// Resolve a qp-selector: open its lookup button -> type in its search -> dblclick the row -> green Select.
async function pickSelector(frame, pg, containerId, search, optionText) {
  await frame.evaluate((c) => {
    const btn = document.querySelector(`#${c} button.qp-field-editor__button`);
    ['mousedown', 'mouseup', 'click'].forEach(e => btn.dispatchEvent(new MouseEvent(e, { bubbles: true })));
  }, containerId);
  await sleep(pg, 3000);
  await frame.evaluate(({ c, s }) => {
    const el = document.getElementById(`${c}_pnl_gr_fb_text`);
    el.focus(); el.value = s;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', keyCode: 13, which: 13, bubbles: true }));
  }, { c: containerId, s: search });
  await sleep(pg, 3500);
  await frame.evaluate((t) => {
    const el = [...document.querySelectorAll('*')].find(e => e.children.length === 0 && (e.textContent || '').trim() === t && e.getBoundingClientRect().width > 0);
    if (el) { el.scrollIntoView({ block: 'center' }); ['mousedown', 'mouseup', 'click', 'dblclick'].forEach(ev => el.dispatchEvent(new MouseEvent(ev, { bubbles: true }))); }
  }, optionText);
  await sleep(pg, 1200);
  await frame.evaluate(() => {
    const s = [...document.querySelectorAll('button,div,span,a')].find(e => (e.textContent || '').trim() === 'Select' && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().width < 160);
    if (s) ['mousedown', 'mouseup', 'click'].forEach(ev => s.dispatchEvent(new MouseEvent(ev, { bubbles: true })));
  });
  await sleep(pg, 2500);
}

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

  // 2) open SM207060 with the endpoint loaded via URL params (modern UI honours these)
  await pg.goto(`${BASE}Main?ScreenId=SM207060&InterfaceName=${encodeURIComponent(EP)}&GateVersion=${VER}`,
    { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(pg, 9000);
  const frame = pg.frames().find(f => /SM207060\.html/.test(f.url()));
  if (!frame) throw new Error('SM207060.html frame not found (is this a modern-UI instance?)');
  log('loaded endpoint', await frame.locator('#edEndpoint-InterfaceName_text').inputValue(), VER);

  const present = (n) => frame.evaluate((n) => !![...document.querySelectorAll('*')]
    .find(e => e.children.length === 0 && (e.textContent || '').trim() === n), n);
  if (await present(ENTITY)) { log('entity already exists — nothing to do'); await b.close(); return; }

  // 3) Insert -> Create Entity dialog
  await cmd(frame, 'InsertNew'); await sleep(pg, 4000);
  await frame.locator('#' + CE + 'ObjectName').fill(ENTITY);
  await pickSelector(frame, pg, CE + 'ScreenID', SCREEN, SCREEN);
  const sv = await frame.locator('#' + CE + 'ScreenIDValue').inputValue().catch(() => '');
  log('screen bound to:', sv);
  if (!sv) throw new Error(`screen "${SCREEN}" did not resolve`);
  shot('1_dialog');

  // 4) OK -> node added
  await clickOK(frame); await sleep(pg, 6000);
  if (!await present(ENTITY)) throw new Error('entity node not added');
  log('entity node added'); shot('2_added');

  // 5) populate fields (optional)
  if (VIEW) {
    await frame.evaluate((n) => { const x = [...document.querySelectorAll('*')].find(e => e.children.length === 0 && (e.textContent || '').trim() === n); if (x) ['mousedown', 'mouseup', 'click'].forEach(e => x.dispatchEvent(new MouseEvent(e, { bubbles: true }))); }, ENTITY);
    await sleep(pg, 2000);
    await cmd(frame, 'PopulateFields'); await sleep(pg, 4500);
    await pickSelector(frame, pg, 'edPopulateFilterView-Container', VIEW, VIEW);
    log('populate view:', await frame.locator('#edPopulateFilterView-Container_text').inputValue().catch(() => ''));
    await cmd(frame, 'SelectAll'); await sleep(pg, 2500);
    await clickOK(frame); await sleep(pg, 5000);
  }

  // 6) Save (live immediately)
  await cmd(frame, 'Save'); await sleep(pg, 8000);
  shot('3_saved');
  log(`SAVED. Verify: get_entity_schema("${ENTITY}", instance=..., refresh=true)`);
  await b.close();
})().catch(e => { console.error('[add-entity-modern] ERR:', e.message); process.exit(1); });
