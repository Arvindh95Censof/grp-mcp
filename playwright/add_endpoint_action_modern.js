/**
 * add_endpoint_action_modern.js
 * ----------------------------------------------------------------------------
 * Add an ACTION to an existing entity in a web service endpoint contract, on a
 * MODERN-UI Acumatica instance (2025R2+/2026R1, SM207060.html). Maps an Acumatica
 * screen command (Release, Save, ChangeID, Import…) to an API action name so it
 * becomes invokable via grp-mcp invoke_action / POST {Endpoint}/{Entity}/{Action}.
 *
 * Verified on localhost/2026R1, GRPSetup/25.200.001, adding the "Save" action to
 * the custom AccountClass entity -> grp-mcp list_actions("AccountClass") => "Save".
 *
 * Companion to add_endpoint_entity_modern.js (fields) — same modern-UI conventions
 * (URL-loaded endpoint, qp-selector lookup, JS-clicked toolbar/OK). The entity must
 * already exist in the endpoint. Inherited entities already carry their standard
 * actions; this is for adding actions to custom entities (or extra ones).
 *
 * USAGE (PowerShell):
 *   $env:GRP_BASE="http://localhost/2026R1"
 *   $env:GRP_USER="admin"; $env:GRP_PASS="********"
 *   $env:NODE_PATH=(npm root -g)
 *   node add_endpoint_action_modern.js --endpoint GRPSetup --version 25.200.001 `
 *        --entity AccountClass --action "Save" --action-name Save
 *
 * ARGS
 *   --endpoint --version --entity   [required]
 *   --action       the Mapped Action text as listed in the Create Action lookup
 *                  (e.g. "Save", "AccountClassRecords$ImportAction")   [required]
 *   --action-name  the API name to invoke it by (e.g. "Save")          [required]
 *   --populate-params   click Populate Parameters after OK (for actions w/ params)
 *   --debug        write screenshots to ./shots
 *
 * ENV: GRP_BASE, GRP_USER, GRP_PASS (no secrets stored here).
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
const MAPPED = arg('action'), ANAME = arg('action-name');
const POP = !!arg('populate-params', false), DEBUG = !!arg('debug', false);
if (!BASE || !USER || !PASS) { console.error('Set GRP_BASE, GRP_USER, GRP_PASS env vars.'); process.exit(2); }
if (!EP || !VER || !ENTITY || !MAPPED || !ANAME) { console.error('Required: --endpoint --version --entity --action --action-name'); process.exit(2); }

const A = 'edCreateActionView-';
const log = (...a) => console.log('[add-action]', ...a);
const sleep = (pg, ms) => pg.waitForTimeout(ms);
const cmd = (frame, c) => frame.evaluate((c) => { const x = document.querySelector(`[data-cmd="${c}"]`); if (x) ['mousedown', 'mouseup', 'click'].forEach(e => x.dispatchEvent(new MouseEvent(e, { bubbles: true }))); }, c);
const clickOK = (frame) => frame.evaluate(() => { const ok = [...document.querySelectorAll('button')].filter(x => (x.textContent || '').trim() === 'OK' && x.getBoundingClientRect().width > 0); if (ok.length) ok[ok.length - 1].click(); });

async function pickSelector(frame, pg, containerId, search, optionText) {
  await frame.evaluate((c) => { const btn = document.querySelector(`#${c} button.qp-field-editor__button`); ['mousedown', 'mouseup', 'click'].forEach(e => btn.dispatchEvent(new MouseEvent(e, { bubbles: true }))); }, containerId);
  await sleep(pg, 3000);
  await frame.evaluate(({ c, s }) => { const el = document.getElementById(`${c}_pnl_gr_fb_text`); el.focus(); el.value = s; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', keyCode: 13, which: 13, bubbles: true })); }, { c: containerId, s: search });
  await sleep(pg, 3000);
  await frame.evaluate((t) => { const el = [...document.querySelectorAll('*')].find(e => e.children.length === 0 && (e.textContent || '').trim() === t && e.getBoundingClientRect().width > 0); if (el) { el.scrollIntoView({ block: 'center' }); ['mousedown', 'mouseup', 'click', 'dblclick'].forEach(ev => el.dispatchEvent(new MouseEvent(ev, { bubbles: true }))); } }, optionText);
  await sleep(pg, 1200);
  await frame.evaluate(() => { const s = [...document.querySelectorAll('button,div,span,a')].find(e => (e.textContent || '').trim() === 'Select' && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().width < 160); if (s) ['mousedown', 'mouseup', 'click'].forEach(ev => s.dispatchEvent(new MouseEvent(ev, { bubbles: true }))); });
  await sleep(pg, 2000);
}

(async () => {
  const b = await chromium.launch({ headless: true });
  const ctx = await b.newContext({ viewport: { width: 1600, height: 900 } });
  const pg = await ctx.newPage();
  const shot = (n) => DEBUG && pg.screenshot({ path: `shots/${n}.png` }).catch(() => {});

  await pg.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await pg.locator("input[type='text']:visible, input:not([type]):visible").first().fill(USER);
  await pg.locator("input[type='password']:visible").first().fill(PASS);
  await pg.keyboard.press('Enter'); await sleep(pg, 6000);
  if (/Login\.aspx/i.test(pg.url())) throw new Error('login failed');

  await pg.goto(`${BASE}Main?ScreenId=SM207060&InterfaceName=${encodeURIComponent(EP)}&GateVersion=${VER}`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(pg, 9000);
  const frame = pg.frames().find(f => /SM207060\.html/.test(f.url()));
  if (!frame) throw new Error('SM207060.html frame not found');

  // expand the entity node (click its row arrow), then select its "Actions" child
  const found = await frame.evaluate((n) => {
    const lbl = [...document.querySelectorAll('*')].find(e => e.children.length === 0 && (e.textContent || '').trim() === n);
    if (!lbl) return false;
    const arrow = lbl.closest('tr').querySelector('i[ref="arrow"]');
    if (arrow) ['mousedown', 'mouseup', 'click'].forEach(ev => arrow.dispatchEvent(new MouseEvent(ev, { bubbles: true })));
    return true;
  }, ENTITY);
  if (!found) throw new Error(`entity "${ENTITY}" not found in tree`);
  await sleep(pg, 2000);
  // Select the "Actions" row that belongs to ENTITY: locate the entity's row, then
  // the first visible "Actions" tree-row AFTER it (its child) — not the first global
  // "Actions" row, which could belong to a different entity.
  const picked = await frame.evaluate((n) => {
    const rows = [...document.querySelectorAll('tr.tree-row')].filter(r => r.getBoundingClientRect().width > 0);
    const ei = rows.findIndex(r => (r.textContent || '').trim() === n || (r.textContent || '').trim().startsWith(n));
    if (ei === -1) return false;
    const a = rows.slice(ei + 1).find(r => (r.textContent || '').trim() === 'Actions');
    if (!a) return false;
    ['mousedown', 'mouseup', 'click'].forEach(ev => a.querySelector('td').dispatchEvent(new MouseEvent(ev, { bubbles: true })));
    return true;
  }, ENTITY);
  if (!picked) throw new Error(`Actions node not found under "${ENTITY}"`);
  await sleep(pg, 1500);

  // Insert -> Create Action dialog
  await cmd(frame, 'InsertNew'); await sleep(pg, 4000);
  await pickSelector(frame, pg, A + 'MappedAction', MAPPED, MAPPED);
  await frame.locator('#' + A + 'ActionName').fill(ANAME);
  log('mapped:', await frame.locator('#' + A + 'MappedAction_text').inputValue(), '| name:', await frame.locator('#' + A + 'ActionName').inputValue());
  shot('1_action_dialog');
  await clickOK(frame); await sleep(pg, 5000);

  if (POP) { await cmd(frame, 'PopulateParameters'); await sleep(pg, 3000); await clickOK(frame); await sleep(pg, 3000); }

  await cmd(frame, 'Save'); await sleep(pg, 8000);
  shot('2_saved');
  log(`SAVED. Verify: list_actions("${ENTITY}", instance=..., refresh=true) should include "${ANAME}".`);
  await b.close();
})().catch(e => { console.error('[add-action] ERR:', e.message); process.exit(1); });
