"""Local config UI for grp-mcp connection profiles.

A tiny single-file web app (stdlib only) for managing the profiles in
connections.json — list, add/edit, set active, remove, and test credentials —
without hand-editing JSON. Reuses load_config/save_config and AcumaticaClient.

Run:  grp-mcp-ui            (or: python -m grp_mcp.ui)
Then open http://127.0.0.1:8765 .

SECURITY: binds to 127.0.0.1 only (never a public interface) because the page
edits credentials. Secrets are never sent to the browser — the profile list
returns only whether a secret/password is set, not its value. Changes are written
to connections.json; the running MCP server loads config at startup, so restart it
to apply add/active changes to the live connector.
"""

from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import kb_client
from .acumatica import AcumaticaClient
from .config import Config, ConfigNotFoundError, Instance, load_config, save_config

HOST = "127.0.0.1"
PORT = 8765


def _load() -> Config:
    """Load the config, or return an empty one so a brand-new user can bootstrap.

    On a fresh machine there is no connections.json and no env vars, so
    load_config() raises ConfigNotFoundError. The UI treats ONLY that specific
    condition as "no profiles yet" and lets the user add the first profile in the
    browser (save_config writes a new file). A malformed EXISTING connections.json
    (bad JSON / a field that fails validation) raises a DIFFERENT exception and is
    let through (audit finding 2026-07-15 #5: catching bare Exception here used to
    treat a corrupted file identically to "no config yet", silently presenting an
    empty profile list — and a later save from that empty state would overwrite the
    damaged-but-possibly-recoverable file). The caller (do_GET/do_POST) surfaces
    whatever propagates as a JSON error instead of a silent empty config.
    """
    try:
        return load_config()
    except ConfigNotFoundError:
        return Config(default="", instances={}, source_path=None)

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>grp-mcp profiles</title>
<style>
  :root{--bg:#f7f7f5;--card:#fff;--bd:#e3e3df;--bd2:#cfcfca;--tx:#23231f;--mut:#6a6a64;
        --ac:#185fa5;--acbg:#e6f1fb;--ok:#0f6e56;--okbg:#e1f5ee;--err:#a32d2d;--errbg:#fceaea;}
  @media(prefers-color-scheme:dark){:root{--bg:#1c1c1a;--card:#262624;--bd:#3a3a37;--bd2:#4a4a46;
        --tx:#ededed;--mut:#a0a09a;--ac:#85b7eb;--acbg:#0c447c;--ok:#5dcaa5;--okbg:#085041;--err:#f09595;--errbg:#791f1f;}}
  *{box-sizing:border-box}
  body{font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--tx);margin:0;padding:24px}
  .wrap{max-width:760px;margin:0 auto}
  h1{font-size:20px;font-weight:600;margin:0 0 4px;display:flex;align-items:center;gap:8px}
  .sub{color:var(--mut);font-size:13px;margin:0 0 18px}
  .banner{background:var(--acbg);color:var(--ac);font-size:13px;padding:8px 12px;border-radius:8px;margin-bottom:16px}
  .card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px 16px;margin-bottom:10px}
  .card.active{border:2px solid var(--ac)}
  .row{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap}
  .name{font-weight:600;font-size:15px}
  .pill{font-size:11px;background:var(--acbg);color:var(--ac);padding:2px 9px;border-radius:6px;margin-left:6px}
  .meta{color:var(--mut);font-size:12px;margin-top:3px;word-break:break-all}
  .gates{margin-top:6px;font-size:11px;color:var(--mut)}
  .gates b{color:var(--ok);font-weight:500}
  button{font:inherit;font-size:13px;background:transparent;color:var(--tx);border:1px solid var(--bd2);
         border-radius:8px;padding:5px 11px;cursor:pointer}
  button:hover{background:var(--bg)}
  button.pri{background:var(--ac);color:#fff;border-color:var(--ac)}
  button.danger{color:var(--err);border-color:var(--err)}
  .acts{display:flex;gap:6px;flex-shrink:0}
  form{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;margin-top:18px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  label{font-size:12px;color:var(--mut);display:block;margin-bottom:3px}
  input[type=text],input[type=password],select,textarea{width:100%;font:inherit;font-size:14px;padding:7px 9px;
         background:var(--bg);color:var(--tx);border:1px solid var(--bd2);border-radius:7px}
  textarea{resize:vertical;font-family:ui-monospace,Consolas,monospace;font-size:13px}
  .full{grid-column:1/-1}
  .gates b.enf{color:var(--ac)}
  .hint{font-size:11px;color:var(--mut);margin-top:2px}
  .checks{display:flex;gap:18px;margin:12px 0;flex-wrap:wrap}
  .checks label{display:flex;align-items:center;gap:6px;color:var(--tx);font-size:14px;margin:0}
  .msg{font-size:13px;padding:8px 12px;border-radius:8px;margin-top:10px;display:none}
  .msg.ok{display:block;background:var(--okbg);color:var(--ok)}
  .msg.bad{display:block;background:var(--errbg);color:var(--err)}
</style></head><body><div class="wrap">
<h1>grp-mcp profiles <span style="font-size:11px;font-weight:400;color:var(--mut)">build 7</span></h1>
<p class="sub" id="src">loading…</p>
<div class="banner">Editing here writes <code>connections.json</code>. To apply changes to the live connector, run <code>reload_config</code> in Claude (no restart needed) — or restart the grp-mcp server.</div>
<div id="list"></div>

<form id="form" autocomplete="off">
  <div style="font-weight:600;margin-bottom:12px">Add / edit a profile</div>
  <div class="grid">
    <div><label>Name *</label><input type="text" id="name" placeholder="financenew"></div>
    <div><label>Tenant</label><input type="text" id="tenant" placeholder="CSM"></div>
    <div class="full"><label>Base URL *</label><input type="text" id="base_url" placeholder="https://erp.example.com/Site"></div>
    <div><label>Endpoint name</label><input type="text" id="endpoint_name" placeholder="Default"></div>
    <div><label>Endpoint version</label><input type="text" id="endpoint_version" placeholder="24.200.001"></div>
    <div><label>Client ID *</label><input type="text" id="client_id" placeholder="GUID@Tenant"></div>
    <div><label>Client secret *</label><input type="password" id="client_secret"></div>
    <div><label>Username *</label><input type="text" id="username" placeholder="arvindh"></div>
    <div><label>Password *</label><input type="password" id="password"></div>
  </div>
  <div class="checks">
    <label><input type="checkbox" id="allow_write"> allow write</label>
    <label><input type="checkbox" id="allow_delete"> allow delete</label>
    <label><input type="checkbox" id="allow_publish"> allow publish</label>
    <label><input type="checkbox" id="set_active"> set active</label>
  </div>
  <div style="font-weight:600;margin:14px 0 8px;font-size:13px">Enforcement (KB-consult preflight before writes)</div>
  <div class="grid">
    <div><label>Risk</label><select id="risk"><option value="dev">dev</option><option value="production">production</option></select>
      <div class="hint">production defaults enforcement to “warn”; dev to “off”.</div></div>
    <div><label>Enforcement</label><select id="enforcement">
        <option value="">auto (from risk)</option>
        <option value="off">off — no preflight (legacy)</option>
        <option value="warn">warn — consult KB, attach evidence, proceed</option>
        <option value="enforce">enforce — block the write if the KB can’t be consulted</option>
      </select>
      <div class="hint">warn/enforce need a valid <code>kb_server.json</code> (kb-mcp-dual).</div></div>
  </div>
  <div class="checks">
    <label><input type="checkbox" id="allow_unrestricted_fs"> allow unrestricted filesystem (default: confine to working dir)</label>
  </div>
  <button class="pri" type="submit">Save profile</button>
  <button type="button" id="clear">Clear</button>
  <div class="msg" id="formmsg"></div>
</form>

<form id="kbform" autocomplete="off">
  <div style="font-weight:600;margin-bottom:4px">KB server (kb-mcp-dual)</div>
  <p class="sub" style="margin:0 0 12px">Used by <code>warn</code>/<code>enforce</code> enforcement: grp-mcp launches this and calls its <code>search_kb</code> before every write. Written to <span id="kbpath" style="word-break:break-all">…</span>.</p>
  <div class="grid">
    <div class="full"><label>Command (python.exe of the kb-mcp-dual venv)</label><input type="text" id="kb_command" placeholder="C:\\MCPs\\kb-mcp-venv\\Scripts\\python.exe"></div>
    <div class="full"><label>Args — one per line (usually the kb-mcp-dual server.py path)</label><textarea id="kb_args" rows="2" placeholder="C:\\...\\grp-kb\\server.py"></textarea></div>
    <div class="full"><label>Env — KEY=VALUE per line (vault + index dirs, offline flags)</label><textarea id="kb_env" rows="5" placeholder="KB_VAULT_DIR=C:\\...\\Acumatica-KB&#10;KB_INDEX_DIR=C:\\...\\index_minilm_full&#10;HF_HUB_OFFLINE=1&#10;TRANSFORMERS_OFFLINE=1"></textarea></div>
  </div>
  <div style="display:flex;gap:6px;margin-top:12px">
    <button class="pri" type="submit">Save KB server</button>
    <button type="button" id="kbtest">Test KB server</button>
    <button type="button" id="kbclear">Clear</button>
  </div>
  <div class="msg" id="kbmsg"></div>
</form>

<div class="msg" id="topmsg" style="margin-top:14px"></div>
</div>
<script>
const $=id=>document.getElementById(id);
const FIELDS=['name','tenant','base_url','endpoint_name','endpoint_version','client_id','client_secret','username','password'];
async function api(path,opts){const r=await fetch(path,opts);const t=await r.text();let j;try{j=JSON.parse(t)}catch{ j={error:t} } if(!r.ok)throw new Error(j.error||r.statusText);return j}
function showTop(m,bad){const e=$('topmsg');e.textContent=m;e.className='msg '+(bad?'bad':'ok')}
async function load(){
  let d;
  try{ d=await api('/api/profiles'); }
  catch(err){ $('src').textContent='failed to load profiles: '+err.message; $('list').innerHTML='<div class="card">Could not read connections.json. '+esc(err.message)+'</div>'; return; }
  $('src').textContent='config: '+(d.source_path||'(none)')+'  ·  active: '+d.active;
  $('list').innerHTML=d.instances.map(p=>{
    const g=(on,l)=>on?'<b>'+l+'</b>':'<span style="opacity:.5">'+l+'</span>';
    return '<div class="card'+(p.name===d.active?' active':'')+'">'
      +'<div class="row"><div><div class="name">'+esc(p.name)+(p.name===d.active?'<span class="pill">active</span>':'')+'</div>'
      +'<div class="meta">'+esc(p.base_url)+'</div>'
      +'<div class="meta">tenant '+esc(p.tenant||'—')+' · '+esc(p.endpoint_name)+'/'+esc(p.endpoint_version)+((p.has_client_secret&&p.has_password)?'':' · <span style="color:var(--err)">no secret</span>')+'</div>'
      +'<div class="gates">'+g(p.allow_write,'write')+' '+g(p.allow_delete,'delete')+' '+g(p.allow_publish,'publish')
      +' · <b class="enf">enforcement: '+esc(p.effective_enforcement)+'</b>'+(p.enforcement?'':' <span style="opacity:.6">(auto from '+esc(p.risk)+')</span>')+'</div></div>'
      +'<div class="acts">'
      +(p.name===d.active?'':'<button data-a="active" data-n="'+esc(p.name)+'">Set active</button>')
      +'<button data-a="test" data-n="'+esc(p.name)+'">Test</button>'
      +'<button data-a="edit" data-n="'+esc(p.name)+'">Edit</button>'
      +(p.name===d.active?'':'<button class="danger" data-a="remove" data-n="'+esc(p.name)+'">Remove</button>')
      +'</div></div></div>';
  }).join('');
  window._profiles=d.instances;
}
function esc(s){return String(s==null?'':s).replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}
$('list').addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;const n=b.dataset.n,a=b.dataset.a;
  try{
    if(a==='active'){await api('/api/active',{method:'POST',body:JSON.stringify({name:n})});showTop('Active → '+n+'. Restart grp-mcp to apply.');await load()}
    if(a==='remove'){if(!confirm('Remove profile "'+n+'"?'))return;await api('/api/remove',{method:'POST',body:JSON.stringify({name:n})});showTop('Removed '+n);await load()}
    if(a==='test'){b.textContent='…';const r=await api('/api/test',{method:'POST',body:JSON.stringify({name:n})});b.textContent='Test';showTop(r.ok?('✓ '+n+': '+r.entity_count+' entities'):('✗ '+n+': '+r.error),!r.ok)}
    if(a==='edit'){const p=(window._profiles||[]).find(x=>x.name===n)||{};FIELDS.forEach(f=>$(f).value=p[f]!=null?p[f]:'');$('client_secret').value='';$('password').value='';
      $('client_secret').placeholder=p.has_client_secret?'•••••••• (set — leave blank to keep)':'';
      $('password').placeholder=p.has_password?'•••••••• (set — leave blank to keep)':'';
      $('allow_write').checked=!!p.allow_write;$('allow_delete').checked=!!p.allow_delete;$('allow_publish').checked=!!p.allow_publish;
      $('risk').value=p.risk||'dev';$('enforcement').value=p.enforcement||'';$('allow_unrestricted_fs').checked=!!p.allow_unrestricted_fs;
      showTop('Editing '+n+' — re-enter secret + password to change them (leave blank to keep).');$('name').scrollIntoView({behavior:'smooth'})}
  }catch(err){showTop(err.message,true)}
});
$('clear').onclick=()=>{FIELDS.forEach(f=>$(f).value='');$('client_secret').placeholder='';$('password').placeholder='';['allow_write','allow_delete','allow_publish','set_active','allow_unrestricted_fs'].forEach(c=>$(c).checked=false);$('risk').value='dev';$('enforcement').value='';$('formmsg').className='msg'};
$('form').addEventListener('submit',async e=>{e.preventDefault();
  const body={};FIELDS.forEach(f=>body[f]=$(f).value.trim());
  ['allow_write','allow_delete','allow_publish','set_active','allow_unrestricted_fs'].forEach(c=>body[c]=$(c).checked);
  body.risk=$('risk').value;body.enforcement=$('enforcement').value;
  const m=$('formmsg');
  try{const r=await api('/api/profile',{method:'POST',body:JSON.stringify(body)});m.className='msg ok';m.textContent='Saved "'+r.name+'". Restart grp-mcp to apply.';$('clear').click();await load()}
  catch(err){m.className='msg bad';m.textContent=err.message}
});

async function loadKb(){
  try{const d=await api('/api/kb_server');
    $('kbpath').textContent=d.path||'(unknown)';
    $('kb_command').value=d.command||'';
    $('kb_args').value=(d.args||[]).join('\n');
    $('kb_env').value=Object.entries(d.env||{}).map(([k,v])=>k+'='+v).join('\n');
  }catch(err){$('kbpath').textContent='(could not read: '+err.message+')'}
}
function kbBody(){
  const args=$('kb_args').value.split('\n').map(s=>s.trim()).filter(Boolean);
  const env={};$('kb_env').value.split('\n').forEach(l=>{const i=l.indexOf('=');if(i>0){env[l.slice(0,i).trim()]=l.slice(i+1).trim()}});
  return {command:$('kb_command').value.trim(),args,env};
}
$('kbform').addEventListener('submit',async e=>{e.preventDefault();const m=$('kbmsg');
  try{const r=await api('/api/kb_server',{method:'POST',body:JSON.stringify(kbBody())});
    m.className='msg ok';m.textContent='Saved → '+r.path+'. Run reload_config or restart grp-mcp to apply.';await loadKb()}
  catch(err){m.className='msg bad';m.textContent=err.message}
});
$('kbtest').onclick=async()=>{const m=$('kbmsg'),b=$('kbtest');b.textContent='testing… (~20s cold)';b.disabled=true;
  try{const r=await api('/api/kb_server_test',{method:'POST',body:JSON.stringify(kbBody())});
    m.className='msg '+(r.available?'ok':'bad');
    m.textContent=r.available?('✓ KB reachable — '+r.match_count+' matches for a sample query'+(r.sample?' (e.g. '+r.sample+')'):''):('✗ '+(r.reason||'unavailable'));
  }catch(err){m.className='msg bad';m.textContent=err.message}
  finally{b.textContent='Test KB server';b.disabled=false}
};
$('kbclear').onclick=()=>{$('kb_command').value='';$('kb_args').value='';$('kb_env').value='';$('kbmsg').className='msg'};

load();loadKb();
</script></body></html>"""


def _profiles_payload(cfg) -> dict:
    return {
        "active": cfg.default,
        "source_path": cfg.source_path,
        "instances": [
            {
                "name": n,
                "base_url": i.base_url,
                "endpoint_name": i.endpoint_name,
                "endpoint_version": i.endpoint_version,
                "tenant": i.tenant,
                "allow_write": i.allow_write,
                "allow_delete": i.allow_delete,
                "allow_publish": i.allow_publish,
                "risk": i.risk,
                "enforcement": i.enforcement,
                "effective_enforcement": i.effective_enforcement(),
                "allow_unrestricted_fs": i.allow_unrestricted_fs,
                "has_client_secret": bool(i.client_secret),
                "has_password": bool(i.password),
            }
            for n, i in cfg.instances.items()
        ],
    }


def _is_same_origin(headers, expected: str) -> bool:
    """CSRF guard: binding to 127.0.0.1 stops remote attackers, but a page open in
    the SAME browser (any other tab/site) can still fire a blind cross-origin POST
    here while this UI happens to be running — CORS blocks it from reading the
    response, not from sending the request. A real load of this page's own JS
    always carries an Origin (or, failing that, a Referer) matching `expected`
    exactly; a POST forged from another site won't. Pure function (headers/expected
    passed in) so it's unit-testable without a live HTTP handler."""
    origin = headers.get("Origin")
    if origin is not None:
        return origin == expected
    referer = headers.get("Referer") or ""
    return referer == expected or referer.startswith(expected + "/")


def _kb_spec_from_body(b: dict) -> dict:
    """Normalise the KB-server form body into a {command, args, env} spec."""
    args = [str(a).strip() for a in (b.get("args") or []) if str(a).strip()]
    env = {str(k): str(v) for k, v in (b.get("env") or {}).items() if str(k).strip()}
    return {"command": (b.get("command") or "").strip(), "args": args, "env": env}


def _kb_server_payload() -> dict:
    """Current kb_server.json (or blanks) plus the path the UI writes to."""
    spec = kb_client.load_spec() or {}
    return {
        "path": str(kb_client.default_spec_path()),
        "command": spec.get("command", ""),
        "args": spec.get("args", []),
        "env": spec.get("env", {}),
    }


async def _kb_server_test(b: dict) -> dict:
    """Live-test the (edited, unsaved) KB-server spec: launch kb-mcp-dual and run a
    sample search. ~20s cold. Bypasses the cache so it always exercises the spawn."""
    spec = _kb_spec_from_body(b)
    if not spec["command"]:
        return {"available": False, "reason": "command is required"}
    r = await kb_client.consult("financial period setup", spec=spec, use_cache=False)
    sample = (r.get("matched") or [{}])[0].get("path") if r.get("available") else None
    return {"available": bool(r.get("available")),
            "match_count": r.get("match_count", 0),
            "reason": r.get("reason"), "sample": sample}


async def _test(inst: Instance) -> dict:
    client = AcumaticaClient(inst)
    try:
        ents = await client.list_entities()
        return {"ok": True, "entity_count": len(ents)}
    except Exception as e:  # noqa: BLE001 - surface any failure to the UI
        return {"ok": False, "error": str(e)[:400]}
    finally:
        await client.aclose()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n) or b"{}")

    def _same_origin(self) -> bool:
        return _is_same_origin(self.headers, f"http://{HOST}:{PORT}")

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path == "/" or self.path.startswith("/index"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/profiles":
                self._json(_profiles_payload(_load()))
            elif self.path == "/api/kb_server":
                self._json(_kb_server_payload())
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001 - surface config errors as JSON
            self._json({"error": str(e)[:400]}, 500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if not self._same_origin():
                self._json({"error": "cross-origin request rejected"}, 403)
                return
            body = self._read_json()
            if self.path == "/api/profile":
                self._save_profile(body)
            elif self.path == "/api/active":
                self._set_active(body)
            elif self.path == "/api/remove":
                self._remove(body)
            elif self.path == "/api/test":
                self._do_test(body)
            elif self.path == "/api/kb_server":
                self._save_kb_server(body)
            elif self.path == "/api/kb_server_test":
                self._json(asyncio.run(_kb_server_test(body)))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)[:400]}, 400)

    def _save_profile(self, b: dict) -> None:
        cfg = _load()
        name = (b.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        existing = cfg.instances.get(name)
        # secret/password may be blank on edit -> keep the existing value
        secret = b.get("client_secret") or (existing.client_secret if existing else "")
        password = b.get("password") or (existing.password if existing else "")
        for fld, val in {"base_url": b.get("base_url"), "client_id": b.get("client_id"),
                         "username": b.get("username")}.items():
            if not (val or (existing and getattr(existing, fld))):
                raise ValueError(f"{fld} is required")
        # Only the fields this form actually exposes. Anything NOT listed here (branch,
        # max_file_bytes, read_roots, write_roots, and any future field) is preserved
        # from `existing` via model_copy rather than reconstructed from scratch — audit
        # finding 2026-07-15 #6: rebuilding a bare Instance(...) here silently reset
        # branch and max_file_bytes to their class defaults on every edit, since they
        # were never in this dict and Instance() has no way to know the OLD value.
        updates = dict(
            base_url=b.get("base_url") or (existing.base_url if existing else ""),
            client_id=b.get("client_id") or (existing.client_id if existing else ""),
            client_secret=secret,
            username=b.get("username") or (existing.username if existing else ""),
            password=password,
            endpoint_name=(b.get("endpoint_name") or "").strip() or "Default",
            endpoint_version=(b.get("endpoint_version") or "").strip() or "24.200.001",
            tenant=(b.get("tenant") or "").strip(),
            allow_write=bool(b.get("allow_write")),
            allow_delete=bool(b.get("allow_delete")),
            allow_publish=bool(b.get("allow_publish")),
            # enforcement: "" (auto) -> None so it derives from risk
            risk=(b.get("risk") or "dev").strip(),
            enforcement=((b.get("enforcement") or "").strip() or None),
            allow_unrestricted_fs=bool(b.get("allow_unrestricted_fs")),
        )
        inst = existing.model_copy(update=updates) if existing else Instance(**updates)
        cfg.instances[name] = inst
        if b.get("set_active") or len(cfg.instances) == 1:
            cfg.default = name
        save_config(cfg)
        self._json({"name": name, "active": cfg.default})

    def _set_active(self, b: dict) -> None:
        cfg = _load()
        name = b.get("name")
        if name not in cfg.instances:
            raise ValueError(f"unknown profile '{name}'")
        cfg.default = name
        save_config(cfg)
        self._json({"active": name})

    def _remove(self, b: dict) -> None:
        cfg = _load()
        name = b.get("name")
        if name not in cfg.instances:
            raise ValueError(f"unknown profile '{name}'")
        if len(cfg.instances) == 1:
            raise ValueError("cannot remove the only profile")
        del cfg.instances[name]
        if cfg.default == name:
            cfg.default = next(iter(cfg.instances))
        save_config(cfg)
        self._json({"removed": name, "active": cfg.default})

    def _do_test(self, b: dict) -> None:
        cfg = _load()
        name = b.get("name")
        if name not in cfg.instances:
            raise ValueError(f"unknown profile '{name}'")
        self._json(asyncio.run(_test(cfg.instances[name])))

    def _save_kb_server(self, b: dict) -> None:
        spec = _kb_spec_from_body(b)
        if not spec["command"]:
            raise ValueError("command is required (path to the kb-mcp-dual venv python)")
        path = kb_client.default_spec_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
        self._json({"path": str(path)})


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), _Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"grp-mcp config UI -> {url}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
