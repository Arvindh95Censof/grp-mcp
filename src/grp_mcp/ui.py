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

from .acumatica import AcumaticaClient
from .config import Instance, load_config, save_config

HOST = "127.0.0.1"
PORT = 8765

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
  input[type=text],input[type=password]{width:100%;font:inherit;font-size:14px;padding:7px 9px;
         background:var(--bg);color:var(--tx);border:1px solid var(--bd2);border-radius:7px}
  .full{grid-column:1/-1}
  .checks{display:flex;gap:18px;margin:12px 0;flex-wrap:wrap}
  .checks label{display:flex;align-items:center;gap:6px;color:var(--tx);font-size:14px;margin:0}
  .msg{font-size:13px;padding:8px 12px;border-radius:8px;margin-top:10px;display:none}
  .msg.ok{display:block;background:var(--okbg);color:var(--ok)}
  .msg.bad{display:block;background:var(--errbg);color:var(--err)}
</style></head><body><div class="wrap">
<h1>grp-mcp profiles <span style="font-size:11px;font-weight:400;color:var(--mut)">build 2</span></h1>
<p class="sub" id="src">loading…</p>
<div class="banner">Editing here writes <code>connections.json</code>. Restart the grp-mcp server to apply add / active changes to the live connector.</div>
<div id="list"></div>

<form id="form" autocomplete="off">
  <div style="font-weight:600;margin-bottom:12px">Add / edit a profile</div>
  <div class="grid">
    <div><label>Name *</label><input type="text" id="name" placeholder="financenew"></div>
    <div><label>Tenant</label><input type="text" id="tenant" placeholder="CSM"></div>
    <div class="full"><label>Base URL *</label><input type="text" id="base_url" placeholder="https://financenew.censof.com/censof"></div>
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
  <button class="pri" type="submit">Save profile</button>
  <button type="button" id="clear">Clear</button>
  <div class="msg" id="formmsg"></div>
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
      +'<div class="meta">tenant '+esc(p.tenant||'—')+' · '+esc(p.endpoint_name)+'/'+esc(p.endpoint_version)+(p.has_secret?'':' · <span style="color:var(--err)">no secret</span>')+'</div>'
      +'<div class="gates">'+g(p.allow_write,'write')+' '+g(p.allow_delete,'delete')+' '+g(p.allow_publish,'publish')+'</div></div>'
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
      $('allow_write').checked=!!p.allow_write;$('allow_delete').checked=!!p.allow_delete;$('allow_publish').checked=!!p.allow_publish;
      showTop('Editing '+n+' — re-enter secret + password to change them (leave blank to keep).');$('name').scrollIntoView({behavior:'smooth'})}
  }catch(err){showTop(err.message,true)}
});
$('clear').onclick=()=>{FIELDS.forEach(f=>$(f).value='');['allow_write','allow_delete','allow_publish','set_active'].forEach(c=>$(c).checked=false);$('formmsg').className='msg'};
$('form').addEventListener('submit',async e=>{e.preventDefault();
  const body={};FIELDS.forEach(f=>body[f]=$(f).value.trim());
  ['allow_write','allow_delete','allow_publish','set_active'].forEach(c=>body[c]=$(c).checked);
  const m=$('formmsg');
  try{const r=await api('/api/profile',{method:'POST',body:JSON.stringify(body)});m.className='msg ok';m.textContent='Saved "'+r.name+'". Restart grp-mcp to apply.';$('clear').click();await load()}
  catch(err){m.className='msg bad';m.textContent=err.message}
});
load();
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
                "has_secret": bool(i.client_secret and i.password),
            }
            for n, i in cfg.instances.items()
        ],
    }


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

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path == "/" or self.path.startswith("/index"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/profiles":
                self._json(_profiles_payload(load_config()))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001 - surface config errors as JSON
            self._json({"error": str(e)[:400]}, 500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._read_json()
            if self.path == "/api/profile":
                self._save_profile(body)
            elif self.path == "/api/active":
                self._set_active(body)
            elif self.path == "/api/remove":
                self._remove(body)
            elif self.path == "/api/test":
                self._do_test(body)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)[:400]}, 400)

    def _save_profile(self, b: dict) -> None:
        cfg = load_config()
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
        inst = Instance(
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
            read_roots=existing.read_roots if existing else [],
            write_roots=existing.write_roots if existing else [],
        )
        cfg.instances[name] = inst
        if b.get("set_active") or len(cfg.instances) == 1:
            cfg.default = name
        save_config(cfg)
        self._json({"name": name, "active": cfg.default})

    def _set_active(self, b: dict) -> None:
        cfg = load_config()
        name = b.get("name")
        if name not in cfg.instances:
            raise ValueError(f"unknown profile '{name}'")
        cfg.default = name
        save_config(cfg)
        self._json({"active": name})

    def _remove(self, b: dict) -> None:
        cfg = load_config()
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
        cfg = load_config()
        name = b.get("name")
        if name not in cfg.instances:
            raise ValueError(f"unknown profile '{name}'")
        self._json(asyncio.run(_test(cfg.instances[name])))


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
