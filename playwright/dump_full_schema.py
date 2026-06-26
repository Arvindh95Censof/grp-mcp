"""Dump the FULL contract field tree (header + every detail tab + nested) for
foundation entities, straight from the endpoint swagger.json. Self-auths from
connections.json. Output: full_schema.json (tree) + full_schema_rows.json (flat).
"""
import json, sys, urllib.request, urllib.parse, urllib.error
from pathlib import Path

CONN = r"C:\Users\CSM-Arvindh\OneDrive - Censof Holdings\Desktop\MCPs\grp-mcp\connections.json"
INST = sys.argv[1] if len(sys.argv) > 1 else None
ENTITIES = ["Company", "Branch", "Ledger", "SegmentedKey", "NumberingSequence",
            "FinancialYear", "Account", "Subaccount", "AccountClass"]
META = {"id", "rowNumber", "note", "_links", "custom", "files", "rowState",
        "LastModifiedDateTime", "CreatedDateTime", "_workflowActions"}

cfg = json.loads(Path(CONN).read_text(encoding="utf-8"))
name = INST or cfg["default"]
inst = cfg["instances"][name]
base = inst["base_url"].rstrip("/")
ep = f'{inst["endpoint_name"]}/{inst["endpoint_version"]}'
print(f"instance={name} base={base} endpoint={ep}", file=sys.stderr)

# ---- OAuth password grant ----
tok_body = urllib.parse.urlencode({
    "grant_type": "password",
    "client_id": inst["client_id"], "client_secret": inst["client_secret"],
    "username": inst["username"], "password": inst["password"],
    "scope": "api offline_access",
}).encode()
req = urllib.request.Request(f"{base}/identity/connect/token", data=tok_body,
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
tok = json.loads(urllib.request.urlopen(req, timeout=60).read())["access_token"]
print("token OK", file=sys.stderr)

# ---- swagger.json ----
req = urllib.request.Request(f"{base}/entity/{ep}/swagger.json",
                            headers={"Authorization": f"Bearer {tok}"})
doc = json.loads(urllib.request.urlopen(req, timeout=120).read())
schemas = (doc.get("components") or {}).get("schemas") or {}
print(f"schemas in swagger: {len(schemas)}", file=sys.stderr)

def merged_props(schema_name):
    node = schemas.get(schema_name)
    if not node:
        return {}
    props = dict(node.get("properties") or {})
    for part in node.get("allOf") or []:
        if "$ref" not in part:
            props.update(part.get("properties") or {})
        else:
            ref = part["$ref"].split("/")[-1]
            if ref != "Entity":
                props.update(merged_props(ref))
    return props

def ref_of(spec):
    # array detail -> items.$ref ; single nested -> $ref
    if not isinstance(spec, dict):
        return None
    if spec.get("type") == "array":
        items = spec.get("items") or {}
        if "$ref" in items:
            return items["$ref"].split("/")[-1]
    if "$ref" in spec:
        return spec["$ref"].split("/")[-1]
    return None

def field_meta(spec):
    # unwrap Acumatica value-wrapper {allOf:[{$ref:StringValue}]} etc.
    t = spec.get("type") if isinstance(spec, dict) else None
    ref = spec.get("$ref", "").split("/")[-1] if isinstance(spec, dict) and "$ref" in spec else ""
    if not t and ref:
        t = ref.replace("Value", "").lower() or "value"
    fmt = spec.get("format", "") if isinstance(spec, dict) else ""
    return t or "value", fmt

def walk(schema_name, path, depth, visited, out):
    props = merged_props(schema_name)
    for fname in sorted(props):
        if fname in META:
            continue
        spec = props[fname]
        is_arr = isinstance(spec, dict) and spec.get("type") == "array"
        child_ref = ref_of(spec) if is_arr else None
        t, fmt = field_meta(spec)
        row = {"entity": path.split(".")[0], "path": f"{path}.{fname}",
               "field": fname, "kind": "detail" if is_arr else "scalar",
               "type": ("array<%s>" % child_ref) if is_arr else t, "format": fmt,
               "depth": depth}
        out.append(row)
        if is_arr and child_ref and child_ref not in visited and depth < 3:
            walk(child_ref, f"{path}.{fname}", depth + 1, visited | {child_ref}, out)

rows = []
tree = {}
for ent in ENTITIES:
    if ent not in schemas:
        close = [s for s in schemas if ent.lower() in s.lower()][:6]
        print(f"  SKIP {ent}: not in schemas. close={close}", file=sys.stderr)
        continue
    sub = []
    walk(ent, ent, 0, {ent}, sub)
    rows.extend(sub)
    hdr = [r["field"] for r in sub if r["depth"] == 0 and r["kind"] == "scalar"]
    det = sorted({r["path"].split(".")[1] for r in sub if r["depth"] == 0 and r["kind"] == "detail"})
    tree[ent] = {"header_fields": hdr, "detail_collections": det,
                 "total_rows": len(sub)}
    print(f"  {ent}: header={len(hdr)} details={det}", file=sys.stderr)

out_dir = Path(__file__).parent
(out_dir / "full_schema_rows.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
(out_dir / "full_schema.json").write_text(json.dumps(tree, indent=1), encoding="utf-8")
print(f"\nTOTAL field rows: {len(rows)} across {len(tree)} entities", file=sys.stderr)
print("wrote full_schema_rows.json + full_schema.json", file=sys.stderr)
