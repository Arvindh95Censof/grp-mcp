"""Screen-based SOAP client (the typed /Soap/<ScreenID>.asmx API).

The contract-based REST API addresses records by key and cannot write screens
that only enable an action once a parent context is loaded (popup / master-detail
/ context screens — e.g. Segment Values CS203000). The screen-based SOAP API
replays UI command sequences *as a user*, so it drives the screen WITH context.

This is a thin, dependency-free async client (pure httpx — no zeep): Login,
GetSchema, Submit, Logout. It reuses the instance's username/password/tenant.

IMPORTANT — seats: every Login holds one of the instance's "Max Web Services API
Users" seats (a trial allows only 2). Always Logout. Use `async with
ScreenClient(...) as s:` so logout runs even on error; leaking sessions yields
"API Login Limit" faults until they idle-time-out.
"""

from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from html import escape, unescape
from typing import Any

import httpx

from .config import Instance

_XSI = "http://www.w3.org/2001/XMLSchema-instance"
ET.register_namespace("xsi", _XSI)

_TNS = "http://www.acumatica.com/typed/"
_ENV_OPEN = (
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
    f'xmlns:tns="{_TNS}" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    "<soap:Body>"
)
_ENV_CLOSE = "</soap:Body></soap:Envelope>"


class ScreenError(RuntimeError):
    pass


class ScreenClient:
    """One screen-based SOAP session, bound to a single screen.

    screen_id: e.g. "CS203000". The service lives at
    {base_url}/Soap/{screen_id}.asmx and Login/Logout are session-wide.
    """

    def __init__(self, instance: Instance, screen_id: str) -> None:
        self.instance = instance
        self.screen_id = screen_id.upper()
        self._http = httpx.AsyncClient(timeout=120.0, follow_redirects=True)
        self._logged_in = False
        self._tree: ET.Element | None = None

    @property
    def url(self) -> str:
        return f"{self.instance.base_url.rstrip('/')}/Soap/{self.screen_id}.asmx"

    @property
    def login_name(self) -> str:
        """Screen-API login name. Multi-tenant sites need user@Tenant."""
        u = self.instance.username
        t = self.instance.tenant
        return f"{u}@{t}" if t and "@" not in u else u

    # ---- transport ------------------------------------------------------

    async def _call(self, op: str, inner_xml: str) -> str:
        resp = await self._http.post(
            self.url,
            content=(_ENV_OPEN + inner_xml + _ENV_CLOSE).encode("utf-8"),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": _TNS + op,
            },
        )
        text = resp.text
        if "<soap:Fault>" in text or "<faultstring>" in text:
            m = re.search(r"<faultstring>(.*?)</faultstring>", text, re.S)
            msg = re.sub(r"\s+", " ", m.group(1)).strip() if m else text[:400]
            # surface the real PX inner exception, not the SOAP wrapper boilerplate
            inner = re.search(r"PX\.\w[\w.]*Exception: ([^\n]+?)(?: at |---)", msg)
            raise ScreenError(
                f"{op} on {self.screen_id}: {inner.group(1).strip() if inner else msg}"
            )
        if resp.status_code >= 400:
            raise ScreenError(f"{op} on {self.screen_id} -> HTTP {resp.status_code}")
        return text

    # ---- session --------------------------------------------------------

    async def login(self) -> None:
        await self._call(
            "Login",
            f"<tns:Login><tns:name>{escape(self.login_name)}</tns:name>"
            f"<tns:password>{escape(self.instance.password)}</tns:password></tns:Login>",
        )
        self._logged_in = True

    async def logout(self) -> None:
        if not self._logged_in:
            return
        self._logged_in = False
        try:
            await self._call("Logout", "<tns:Logout/>")
        except Exception:
            pass

    async def aclose(self) -> None:
        await self.logout()
        try:
            await self._http.aclose()
        except Exception:
            pass

    async def __aenter__(self) -> "ScreenClient":
        await self.login()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ---- operations -----------------------------------------------------

    async def get_schema_xml(self) -> str:
        return await self._call("GetSchema", "<tns:GetSchema/>")

    async def get_schema(self) -> dict:
        """Parse GetSchema into {container: {friendly_field: {object, field}}}.

        The schema's field descriptors carry the exact ObjectName + FieldName the
        Submit engine expects (e.g. Segment.DimensionID, Values.Value) plus the
        per-container service commands (NewRow/Key/DeleteRow). This is what you
        feed back into submit().
        """
        xml = await self.get_schema_xml()
        containers: dict[str, dict] = {}
        # each top-level container is <Name>...<DisplayName>..</DisplayName>...</Name>
        for cm in re.finditer(r"<(\w+)><DisplayName>(.*?)</DisplayName>(.*?)</\1>", xml, re.S):
            cname, _disp, body = cm.group(1), cm.group(2), cm.group(3)
            fields: dict[str, dict] = {}
            for fm in re.finditer(
                r"<(\w+)><FieldName>([^<]*)</FieldName><ObjectName>([^<]*)</ObjectName>",
                body,
            ):
                friendly, field, obj = fm.group(1), fm.group(2), fm.group(3)
                if friendly in ("ServiceCommands",):
                    continue
                fields.setdefault(friendly, {"object": obj, "field": field})
            if fields:
                containers[cname] = fields
        return {"screen_id": self.screen_id, "containers": containers}

    # ---- schema tree (for descriptor-based commands) -------------------

    async def _ensure_tree(self) -> ET.Element:
        """Fetch + parse GetSchema into an element tree (cached per session).

        The tree's container elements hold each field's FULL descriptor —
        FieldName, ObjectName, Value, Commit, and crucially the LinkedCommand
        navigation chain. Building Submit commands by cloning these descriptors
        (and overwriting the value) replays the chain, which is what actually
        loads/navigates the record. Bare hand-built commands omit the chain and
        silently no-op (Submit returns ok but nothing persists).
        """
        if self._tree is None:
            xml = await self.get_schema_xml()
            m = re.search(r"<GetSchemaResult>(.*)</GetSchemaResult>", xml, re.S)
            inner = m.group(1) if m else xml
            self._tree = ET.fromstring(
                f'<root xmlns:xsi="{_XSI}">{inner}</root>'
            )
        return self._tree

    def _find_field(self, name: str) -> ET.Element:
        """Locate a field/action descriptor by friendly name.

        `name` is the schema's friendly element name (e.g. "CustomerID",
        "AccountName", "Save"); use "Container.Field" to disambiguate when the
        same friendly name appears in more than one container.
        """
        root = self._tree
        if "." in name:
            cont, fname = name.split(".", 1)
            c = root.find(cont)
            el = c.find(fname) if c is not None else None
            if el is None:
                raise ScreenError(f"field {name!r} not found in schema")
            return copy.deepcopy(el)
        matches = []
        for cont in list(root):
            for child in list(cont):
                if child.tag in ("ServiceCommands", "DisplayName"):
                    continue
                if child.tag == name:
                    matches.append((cont.tag, child))
        if not matches:
            raise ScreenError(f"field {name!r} not found in any container")
        if len(matches) > 1:
            where = ", ".join(f"{c}.{name}" for c, _ in matches)
            raise ScreenError(f"field {name!r} is ambiguous — qualify it: {where}")
        return copy.deepcopy(matches[0][1])

    def _service(self, container: str, which: str) -> ET.Element:
        """Find a service command (NewRow/DeleteRow/...) under a container."""
        root = self._tree
        c = root.find(container)
        sc = c.find("ServiceCommands") if c is not None else None
        el = sc.find(which) if sc is not None else None
        if el is None:
            raise ScreenError(
                f"service command {which!r} not found under {container!r}"
            )
        return copy.deepcopy(el)

    @staticmethod
    def _wrap(el: ET.Element, xsi_type: str, value: str | None) -> str:
        if value is not None:
            v = el.find("Value")
            if v is None:
                v = ET.SubElement(el, "Value")
            v.text = str(value)
        kids = "".join(ET.tostring(c, encoding="unicode") for c in el)
        return (
            f'<Command xmlns="{_TNS}" xmlns:xsi="{_XSI}" '
            f'xsi:type="{xsi_type}">{kids}</Command>'
        )

    def _spec_to_command(self, c: dict) -> str:
        """Turn one ergonomic command spec into descriptor-based command XML.

        Specs:
          {"set": "<FriendlyName>", "to": <value>}  set a field (navigates if key)
          {"action": "<FriendlyName>"}              click a button (e.g. "Save")
          {"new_row": "<Container>"}                add a detail row
          {"delete_row": "<Container>"}             delete the current detail row
          {"answer": "<Container>", "to": "Yes"}    answer a pop-up dialog
        """
        if "key" in c:
            # bare Key command (flat FieldName/ObjectName/Value) — selects an
            # existing parent record. Some screens (e.g. CS203000's segment
            # selector) navigate via Key, not via a descriptor-Value set.
            el = self._find_field(c["key"])
            fld = el.findtext("FieldName") or ""
            obj = el.findtext("ObjectName") or ""
            return (
                f'<Command xmlns="{_TNS}" xmlns:xsi="{_XSI}" xsi:type="Key">'
                f"<FieldName>{escape(fld)}</FieldName>"
                f"<ObjectName>{escape(obj)}</ObjectName>"
                f"<Value>{escape(str(c.get('to', '')))}</Value></Command>"
            )
        if "set" in c:
            return self._wrap(self._find_field(c["set"]), "Value", c.get("to"))
        if "action" in c:
            return self._wrap(self._find_field(c["action"]), "Action", None)
        if "new_row" in c:
            return self._wrap(self._service(c["new_row"], "NewRow"), "NewRow", None)
        if "delete_row" in c:
            return self._wrap(
                self._service(c["delete_row"], "DeleteRow"), "DeleteRow", None
            )
        if "answer" in c:
            return self._wrap(
                self._service(c["answer"], "DialogAnswer"), "Answer", c.get("to")
            )
        raise ScreenError(f"unrecognized command spec: {c!r}")

    async def submit(self, commands: list[dict], dry_run: bool = False) -> dict:
        """Submit an ergonomic command sequence; return parsed result.

        Commands reference the schema's friendly field/action names (from
        get_schema) — the client clones the matching descriptor (with its
        LinkedCommand navigation chain) so the record is actually loaded/edited.

        Spec shapes (see _spec_to_command): {"set","to"}, {"action"},
        {"new_row"}, {"delete_row"}, {"answer","to"}.

        dry_run=True drops the committing commands (button actions + row deletes)
        so the field SETs run but nothing persists — a safe preview that still
        surfaces field-level errors.

        Recipe — update a record: set the key field, set other fields, Save:
            [{"set":"CustomerID","to":"ABARTENDE"},
             {"set":"AccountName","to":"New Name"},
             {"action":"Save"}]
        Add a detail row: set the parent key(s), new_row the detail container,
        set the row's fields, Save.
        """
        await self._ensure_tree()
        if dry_run:
            # preview: drop the committing commands (button actions + row deletes)
            # so the field SETs run but nothing persists; surfaces field errors.
            commands = [c for c in commands if not ("action" in c or "delete_row" in c)]
        inner = "".join(self._spec_to_command(c) for c in commands)
        try:
            xml = await self._call(
                "Submit", f"<tns:Submit><tns:commands>{inner}</tns:commands></tns:Submit>"
            )
        except ScreenError as e:
            # A fatal action (Save/Delete/AutoFill) faulted — the SOAP fault only
            # carries a generic "record raised at least one error". Re-run just the
            # field SETs (no actions, so nothing commits) and read the per-field
            # IsError/Message from that Content to surface WHY.
            field_errors = []
            diag = [c for c in commands if ("set" in c or "key" in c)]
            if diag and len(diag) < len(commands):
                try:
                    di = "".join(self._spec_to_command(c) for c in diag)
                    dx = await self._call(
                        "Submit",
                        f"<tns:Submit><tns:commands>{di}</tns:commands></tns:Submit>",
                    )
                    field_errors = self._parse_field_errors(dx)
                except ScreenError:
                    pass
            return {
                "screen_id": self.screen_id,
                "ok": False,
                "error": str(e),
                "field_errors": field_errors,
                "messages": [f["message"] for f in field_errors],
            }
        errors = self._parse_field_errors(xml)
        return {
            "screen_id": self.screen_id,
            "ok": not errors,
            "dry_run": dry_run,
            "messages": [e["message"] for e in errors],
            "field_errors": errors,
            "raw_len": len(xml),
        }

    @staticmethod
    def _parse_field_errors(xml: str) -> list[dict]:
        """Extract per-field errors from a Submit Content response.

        An errored field comes back as a Value element carrying <Message> +
        <IsError>true</IsError> (the API reports field errors inside an HTTP 200,
        not as a SOAP fault). Returns [{field, object, message, level}].
        """
        out: list[dict] = []
        try:
            root = ET.fromstring(xml.encode("utf-8"))
        except ET.ParseError:
            # fall back to a loose scan
            for m in re.findall(r"<Message>([^<]+)</Message>", xml):
                out.append({"field": None, "object": None,
                            "message": re.sub(r"\s+", " ", m).strip(), "level": None})
            return out
        for el in root.iter():
            msg = el.find("Message")
            iserr = el.find("IsError")
            if msg is not None and msg.text and (iserr is None or iserr.text == "true"):
                out.append({
                    "field": (el.findtext("FieldName") or None),
                    "object": (el.findtext("ObjectName") or None),
                    "message": re.sub(r"\s+", " ", msg.text).strip(),
                    "level": (el.findtext("ErrorLevel") or None),
                })
        return out

    async def export(
        self, fields: list[str], top: int = 10, filters: list[dict] | None = None
    ) -> dict:
        """Read current values from a screen via the Export SOAP operation.

        fields: schema friendly field names (qualify Container.Field if ambiguous)
                — the columns to return. top: max rows.
        filters: optional row filters, each {"field": "<Friendly>", "value": ...,
                "condition": "Equals"|"Contain"|"StartsWith"|"Greater"|... (default
                Equals)} — e.g. read one record by its key field.
        Returns {fields, headers, rows} where rows is a list of {header: value}.
        This is the read counterpart to submit(): the screen-based API's Export
        returns the live grid/record data (Submit alone doesn't echo it).
        """
        await self._ensure_tree()
        cols = []
        for f in fields:
            el = self._find_field(f)
            # Export columns are SIMPLE field references — FieldName + ObjectName
            # only. The full descriptor (with its LinkedCommand navigation chain)
            # confuses Export and collapses the result to one column.
            fld = el.findtext("FieldName") or ""
            obj = el.findtext("ObjectName") or ""
            cols.append(
                f'<Command xmlns="{_TNS}" xmlns:xsi="{_XSI}" xsi:type="Field">'
                f"<FieldName>{escape(fld)}</FieldName>"
                f"<ObjectName>{escape(obj)}</ObjectName></Command>"
            )
        fxml = ""
        for flt in filters or []:
            el = self._find_field(flt["field"])
            fld = el.findtext("FieldName") or ""
            obj = el.findtext("ObjectName") or ""
            cond = flt.get("condition", "Equals")
            val = escape(str(flt.get("value", "")))
            # Filter.Value is anyType — it MUST carry an explicit xsi:type or the
            # server fails to cast it (XmlNode[] -> String). Strings cover the
            # common key/field-match case.
            fxml += (
                f'<Filter xmlns="{_TNS}" xmlns:xsi="{_XSI}" '
                f'xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
                f'<Field xsi:type="Field"><FieldName>{escape(fld)}</FieldName>'
                f"<ObjectName>{escape(obj)}</ObjectName></Field>"
                f'<Condition>{escape(cond)}</Condition>'
                f'<Value xsi:type="xsd:string">{val}</Value>'
                f"<OpenBrackets>0</OpenBrackets><CloseBrackets>0</CloseBrackets>"
                f"<Operator>And</Operator></Filter>"
            )
        inner = (
            f"<tns:Export><tns:commands>{''.join(cols)}</tns:commands>"
            f"<tns:filters>{fxml}</tns:filters><tns:topCount>{int(top)}</tns:topCount>"
            f"<tns:includeHeaders>true</tns:includeHeaders>"
            f"<tns:breakOnError>false</tns:breakOnError></tns:Export>"
        )
        xml = await self._call("Export", inner)
        rows: list[list[str]] = []
        try:
            root = ET.fromstring(xml.encode("utf-8"))
            for aos in root.iter():
                if aos.tag.split("}")[-1] == "ArrayOfString":
                    rows.append([(s.text or "") for s in list(aos)])
        except ET.ParseError:
            pass
        if not rows:
            return {"screen_id": self.screen_id, "fields": fields, "headers": [], "rows": []}
        headers = rows[0]
        return {
            "screen_id": self.screen_id,
            "fields": fields,
            "headers": headers,
            "rows": [dict(zip(headers, r)) for r in rows[1:]],
        }
