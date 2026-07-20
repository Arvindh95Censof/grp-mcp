"""Classic ASPX WebForms callback plane — DIAGNOSTIC-ONLY client.

Some (mostly custom-module) screens still run on Acumatica's legacy ASP.NET
WebForms rendering path (/Pages/XX/.../SCREENID.aspx) driven by the classic
ICallbackEventHandler protocol (PXCallbackManager client-side). On a FAILED
grid save this plane returns the REAL validation message — e.g. PY309000's
"Percent should be 100 for sum of all banks" — while BOTH API planes grp-mcp
normally drives return only the generic "record raised at least one error":
the classic SOAP plane truncates it, and the modern JSON plane never
serializes fieldStates for a hidden tab's grid at all (proven raw, 2026-07-17).

This client exists to answer ONE question: "my save failed with a useless
generic error — what is the screen actually complaining about?" It replays the
failing change on the ASPX plane and extracts the detailed error. It is NOT a
general write path — the other planes stay the way records are written.

Protocol (reverse-engineered + headless-proven live on csmdev PY309000):

  request: form-urlencoded POST to the .aspx page itself, with
    - every hidden input from the page GET (anti-forgery token + per-control
      `ctl00_*_state` fields — PLAIN single-URL-encoded XML, no ViewState
      opacity; `__VIEWSTATE` itself rides empty)
    - `__DataSourceSessionID` EMPTY (no session bootstrap exists or is needed;
      the graph context rides entirely in the _state fields)
    - `__CALLBACKID` = the target control (`ctl00$phDS$ds` for record commands)
    - `__CALLBACKPARAM` = `Command|<envelope XML>` — the envelope is REQUIRED
      (a bare command string is accepted but no-ops against an empty graph)
  response: `0|<ctl00_X><![CDATA[<ctl00_X Props="{json}">…]]></ctl00_X>…`
    per-control blocks. Props carries `dataKey` (fold back into `{id}_state`
    before the next call — the server is stateless between callbacks) and, on
    the datasource block after a failed Save, `alert` = THE REAL ERROR TEXT.
    Grid blocks additionally carry `<Rows ErrorText=…><Row Error=…>` detail.

  navigation: `Cancel` + the key field's discrete edit param loads a record by
    key (same semantics as the classic SOAP plane, where Cancel commits key
    fields). Grid data does NOT need to load: a Save's RowChanges is validated
    server-side against the DB rows reconstructed from the header dataKey.
"""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any
from urllib.parse import quote

from .screen import ScreenClient, ScreenError, _selector_value_hint

_CB_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}

# Standard Acumatica page-template control IDs (constant across screens: the
# page template puts the PXDataSource in the phDS placeholder as "ds" and the
# header PXFormView in phF as "form").
_DS_CTL = "ctl00$phDS$ds"
_DS_BLOCK = "ctl00_phDS_ds"
_FORM_BLOCK = "ctl00_phF_form"

_DS_ENVELOPE = ('{cmd}|<ctl00_phDS_ds LoadedLevel="-1">'
                '{inner}'
                '<ctl00_phDS_ds OwnerData="1"><![CDATA[]]></ctl00_phDS_ds>'
                '</ctl00_phDS_ds>')


def _parse_hidden_inputs(html: str) -> dict[str, str]:
    """All <input type=hidden> name->value. Values kept VERBATIM (they are
    single-URL-encoded XML; the form POST encodes them once more, matching the
    double-encoding the browser puts on the wire)."""
    out: dict[str, str] = {}
    for m in re.finditer(r'<input[^>]*type="hidden"[^>]*>', html):
        tag = m.group(0)
        nm = re.search(r'\bname="([^"]*)"', tag)
        vm = re.search(r'\bvalue="([^"]*)"', tag)
        if nm:
            out[nm.group(1)] = unescape(vm.group(1)) if vm else ""
    return out


def _parse_control_blocks(body: str) -> dict[str, dict]:
    """Callback response -> {control_id: Props dict}. Props JSON arrives
    HTML-entity-escaped inside the CDATA blocks."""
    out: dict[str, dict] = {}
    for m in re.finditer(r'<(ctl00_[A-Za-z0-9_]+) Props="([^"]*)"', body):
        try:
            out[m.group(1)] = json.loads(unescape(m.group(2)))
        except Exception:  # noqa: BLE001 — non-JSON Props block; skip
            pass
    return out


def _row0_readonly_fields(body: str, grid_ctl: str, columns: list[str]) -> list[str]:
    """Which of `columns` are ReadOnly="True" on row 0's OWN echo in a Save
    response — a REAL, confirmed cause of a silent no-op (an existing line's
    field can be locked from direct grid edit; the server drops such an edit
    instead of erroring). Best-effort, NOT exhaustive: many no-ops have no
    readonly field at all (proven live, GL301000 — CuryDebitAmt/CuryCreditAmt/
    TranDesc all echoed ReadOnly="False" yet the edit still didn't apply), so
    an empty result here does NOT mean the change is safe to trust.

    Cell order has 1 leading framework cell (file/note indicator) before the
    dataField-mapped cells — align from the END, not the start, since the
    leading offset is fixed but its exact count isn't guaranteed stable."""
    m = re.search(rf'<{re.escape(grid_ctl)}><!\[CDATA\[(.*?)\]\]></{re.escape(grid_ctl)}>',
                  body, re.S)
    if not m:
        return []
    g = unescape(m.group(1))
    rm = re.search(r'<Row i="0"[^>]*>(.*?)</Row>', g, re.S)
    if not rm:
        return []
    cells = re.findall(r'<Cell ([^/>]*)/>', rm.group(1))
    if len(cells) < len(columns):
        return []
    aligned = cells[len(cells) - len(columns):]
    return [col for col, attrs in zip(columns, aligned)
            if 'ReadOnly="True"' in attrs]


def _grid_errors(body: str) -> dict[str, Any]:
    """Per-grid error detail from a callback response: <Rows ErrorText=…>,
    <Row Error=…>, <Cell … Error=…>. Handles both raw and entity-escaped
    attribute forms (the CDATA payloads escape quotes on some paths)."""
    plain = unescape(body)
    rows_text = re.findall(r'<Rows [^>]*?ErrorText="([^"]+)"', plain)
    row_errors = re.findall(r'<Row [^>]*?Error="([^"]+)"', plain)
    cell_errors = re.findall(r'<Cell [^>]*?Error="([^"]+)"', plain)
    return {
        "rows_error_text": list(dict.fromkeys(rows_text)),
        "row_errors": list(dict.fromkeys(row_errors)),
        "cell_errors": list(dict.fromkeys(cell_errors)),
    }


def _xml_attr_escape(v: Any) -> str:
    s = "" if v is None else str(v)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


class AspxDiagnostic:
    """One diagnostic session against a classic ASPX screen page.

    Wraps an already-logged-in ScreenClient (same cookie session — the classic
    page shares the ASP.NET forms auth). Usage:

        async with ScreenClient(inst, screen_id) as s:
            d = AspxDiagnostic(s, page_url)
            await d.open()
            await d.navigate(record_key)          # {"EmployeeCD": "EMP001"}
            result = await d.replay_grid_save(    # the failing change
                grid_view="EmployeeBankDetails",
                cells={"Percent": 50}, row_key={"EmployeeBankDetailID": 14542})
    """

    def __init__(self, screen: ScreenClient, page_url: str):
        self._s = screen
        self.page_url = page_url
        self._state: dict[str, str] = {}
        self._html = ""

    # -- plumbing ---------------------------------------------------------

    async def _callback(self, cbid: str, cbparam: str,
                        extra: dict[str, str] | None = None) -> str:
        form = dict(self._state)
        form["__CALLBACKID"] = cbid
        form["__CALLBACKPARAM"] = cbparam
        if extra:
            form.update(extra)
        headers = dict(_CB_HEADERS)
        headers["Referer"] = self.page_url
        resp = await self._s._http.post(self.page_url, data=form, headers=headers)
        if resp.status_code != 200:
            raise ScreenError(
                f"aspx callback {cbid} -> HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.text
        if "Login.aspx" in body[:500]:
            raise ScreenError("aspx callback bounced to Login.aspx — session lost")
        return body

    def _fold(self, body: str) -> dict[str, dict]:
        """Round-trip: write each response block's dataKey back into its _state
        hidden field. Without this every later callback sees an EMPTY graph —
        the server is stateless between callbacks (proven live)."""
        blocks = _parse_control_blocks(body)
        for cid, props in blocks.items():
            fld = cid + "_state"
            dk = props.get("dataKey")
            if fld in self._state and dk:
                pc = props.get("pageCount", -1)
                self._state[fld] = quote(
                    f'<PXBoundPanel PageCount="{pc}" PageIndex="0" DataKey="{dk}"/>',
                    safe="")
        return blocks

    # -- steps ------------------------------------------------------------

    async def open(self) -> None:
        """GET the page; harvest the anti-forgery token + all control states."""
        params = {}
        if self._s.instance.tenant:
            params["CompanyID"] = self._s.instance.tenant
        resp = await self._s._http.get(self.page_url, params=params)
        if resp.status_code != 200:
            raise ScreenError(
                f"aspx GET {self.page_url} -> HTTP {resp.status_code}")
        self._html = resp.text
        if "__RequestVerificationToken" not in self._html:
            raise ScreenError(
                "aspx GET returned no __RequestVerificationToken — not a classic "
                "WebForms page (modern-only screens have no ASPX plane) or not "
                "authenticated")
        self._state = _parse_hidden_inputs(self._html)
        self._state.update({"__DataSourceSessionID": "", "__DataSourceLoginID": "",
                            "__EVENTTARGET": "", "__EVENTARGUMENT": ""})

    def _key_param(self, field: str) -> str:
        """Discrete edit-param name for a header key field, discovered from the
        page HTML (selector widgets take a `$text` sub-input, plain ones don't)."""
        for cand in (rf'name="(ctl00\$[\w$]*?\$ed{re.escape(field)}\$text)"',
                     rf'name="(ctl00\$[\w$]*?\$ed{re.escape(field)})"'):
            m = re.search(cand, self._html)
            if m:
                return m.group(1)
        # conventional fallback (standard page template)
        return f"ctl00$phF$form$ed{field}$text"

    async def navigate(self, record_key: dict[str, Any]) -> str:
        """Load the record by key: ds `Cancel` commits the key fields (classic
        SOAP-plane semantics). Returns the loaded header dataKey; raises if the
        record didn't load."""
        extra = {self._key_param(f): str(v) for f, v in record_key.items()}
        body = await self._callback(_DS_CTL, _DS_ENVELOPE.format(cmd="Cancel", inner=""),
                                    extra=extra)
        blocks = self._fold(body)
        dk = (blocks.get(_FORM_BLOCK) or {}).get("dataKey") or ""
        if not dk:
            raise ScreenError(
                f"aspx navigate: record {record_key} did not load (no header "
                f"dataKey in Cancel response) — wrong key field name/value?")
        return dk

    def find_grid_control(self, grid_view: str) -> tuple[str, str | None, int | None]:
        """Locate the grid control bound to `grid_view`, returning (grid_ctl_id,
        tab_ctl_id, tab_index); tab_* are None outside a tab container.

        Each control's client config is emitted as `var _<control_id> = {json};`
        with the id in the VAR NAME (leading underscore — invisible to \\b-anchored
        matching, proven live: a naive nearest-preceding-id scan picked the WRONG
        grid and the Save silently no-op'd). So: collect the var declarations,
        map each `"dataMember":"<view>"` occurrence to the declaration that owns
        it (last var start before it), and prefer an owner whose body carries the
        grid-specific `"levels":` key (a form view bound to the same dataMember
        would swallow the RowChanges without validating)."""
        decls = [(m.start(), m.group(1)) for m in
                 re.finditer(r'var _(ctl00_[A-Za-z0-9_]+)\s*=\s*\{', self._html)]
        if not decls:
            raise ScreenError("aspx: page has no control config declarations — "
                              "not a classic WebForms page?")
        needle = f'"dataMember":"{grid_view}"'
        candidates: list[str] = []
        pos = self._html.find(needle)
        while pos >= 0:
            owner_idx = None
            for j, (start, _cid) in enumerate(decls):
                if start > pos:
                    break
                owner_idx = j
            if owner_idx is not None:
                start, cid = decls[owner_idx]
                end = decls[owner_idx + 1][0] if owner_idx + 1 < len(decls) \
                    else len(self._html)
                body = self._html[start:end]
                candidates.append(cid) if '"levels":' in body else \
                    candidates.append("~" + cid)  # de-prioritize non-grid owners
            pos = self._html.find(needle, pos + 1)
        best = next((c for c in candidates if not c.startswith("~")),
                    candidates[0].lstrip("~") if candidates else None)
        if not best:
            raise ScreenError(
                f"aspx: no control bound to view '{grid_view}' on this page "
                f"(dataMember not found in the page HTML)")
        tm = re.match(r"^(.*_tab)_t(\d+)_", best)
        if tm:
            return best, tm.group(1), int(tm.group(2))
        return best, None, None

    def _activate_tab(self, tab_ctl: str, index: int) -> None:
        """Make the grid's tab the ACTIVE one via its _state field (plain XML,
        SelectedIndex attribute). Required: validators and error rendering only
        cover the active tab's controls."""
        fld = tab_ctl + "_state"
        self._state[fld] = quote(
            f'<PXBoundPanel PageCount="0" PageIndex="0" SelectedIndex="{index}">'
            f'<Items/></PXBoundPanel>', safe="")

    async def _grid_columns(self, grid_ctl: str) -> list[str]:
        """The grid's authoritative column dataFields, from a targeted Refresh
        callback. This is load-bearing twice over (both proven live, GL301000):
        (1) the Refresh primes the server-side graph — a Save sent without one
        can silently no-op; (2) the response's `"dataField"` list is the ONLY
        reliable source of the classic grid's column names, which can differ
        from the modern plane's field names (GLTran exposes `CreditAmt` on the
        modern plane but the classic grid column is `CuryCreditAmt` — sending
        the former crashes the callback with a NullReferenceException or
        silently no-ops). Present even when the grid returns ZERO rows
        (PY309000). Empty list if the Refresh itself fails — caller then skips
        validation rather than blocking the diagnosis."""
        cb_target = grid_ctl.replace("_", "$")
        try:
            body = await self._callback(
                cb_target,
                f'Refresh|<{grid_ctl} LoadedLevel="-1"><![CDATA[]]></{grid_ctl}>')
        except ScreenError:
            return []
        return list(dict.fromkeys(
            re.findall(r'"dataField":"([A-Za-z0-9_]+)"', unescape(body))))

    async def replay_grid_save(self, grid_view: str, cells: dict[str, Any],
                               row_key: dict[str, Any] | None = None,
                               old_values: dict[str, Any] | None = None,
                               operation: str = "update") -> dict[str, Any]:
        """Replay a failing grid-row change and return the REAL error detail.

        operation "update": `cells` are the changed cells; `row_key` identifies
        the existing row (its cells ride along, e.g. {"EmployeeBankDetailID":
        14542}). "insert": `cells` are the new row's cells; row_key unused.
        "delete": `row_key` identifies the row to REMOVE (`cells` may be empty);
            it must carry the row's FULL key — ALL key cells, not just one. A
            single-column identity key needs one cell (PY309000
            EmployeeBankDetailID); a COMPOSITE key needs every part or the
            server matches nothing and SILENTLY no-ops (proven live on CS205000
            AttributeDetails: `ValueID` alone did nothing, `AttributeID`+`ValueID`
            deleted the exact row). Always read back — a partial-key delete
            returns a clean `possibly_saved:true` while changing nothing.
        old_values: optional {field: previousValue} — included as OldValue attrs
        (browser parity; not required for validation to fire).

        ROW TARGETING (proven live 2026-07-20, PY309000 EmployeeBankDetails):
        `row_key` genuinely binds to the named row on THIS plane — an update
        keyed to the 2nd row changed only that row, leaving row 0 untouched.
        That matters because the classic SOAP container schema and the modern
        /structure can BOTH omit a grid's key while the classic ASPX grid still
        exposes it (EmployeeBankDetailID is absent from the SOAP `BankDetails`
        container and from /structure, but IS a real ASPX dataField) — so this
        plane can address rows the other two cannot.

        Cell keys are validated against the grid's REAL column dataFields
        (harvested via a Refresh first — see _grid_columns): an unknown key is
        REFUSED with the column list and a best-guess suggestion instead of
        being sent (an unknown key crashes some screens' callbacks and silently
        no-ops others — both proven live on GL301000).

        WARNING: this POSTS a real Save. If the change is actually VALID the
        server PERSISTS it. Use only to diagnose a change that already failed.
        """
        if operation == "delete" and not row_key:
            # Without a key the Deleted section carries no identifying cells and
            # the server falls back to row 0 — silently destroying the WRONG row.
            # Refuse before any network call rather than guess.
            raise ScreenError(
                "operation='delete' requires row_key identifying the row to "
                "remove (e.g. {\"EmployeeBankDetailID\": 14551}); without it the "
                "delete would fall back to row 0 and remove the wrong row. Get "
                "the key from run_dac_odata.")

        grid_ctl, tab_ctl, tab_idx = self.find_grid_control(grid_view)
        if tab_ctl is not None:
            self._activate_tab(tab_ctl, tab_idx)

        columns = await self._grid_columns(grid_ctl)
        if columns:
            colset = set(columns)
            unknown = [f for f in list(cells) + list(row_key or {})
                       if f not in colset]
            if unknown:
                # best-guess mapping: modern-plane name -> Cury-prefixed column
                suggestions = {f: f"Cury{f}" for f in unknown
                               if f"Cury{f}" in colset}
                return {
                    "refused": (
                        f"field(s) {unknown} are not columns of this grid on the "
                        f"classic page — sending them would crash or silently "
                        f"no-op the callback. Use the exact column names from "
                        f"grid_columns" + (f"; likely mapping: {suggestions}"
                                            if suggestions else "") + "."),
                    "unknown_fields": unknown,
                    "suggestions": suggestions,
                    "grid_columns": columns,
                    "alert": None,
                    "rows_error_text": [], "row_errors": [], "cell_errors": [],
                    "graph_dirty": False,
                    "possibly_saved": False,
                }

        cell_xml = []
        for f, v in cells.items():
            old = (old_values or {}).get(f)
            oattr = f' OldValue="{_xml_attr_escape(old)}"' if old is not None else ""
            cell_xml.append(
                f'<Cell Value="{_xml_attr_escape(v)}"{oattr} Key="{f}"/>')
        for f, v in (row_key or {}).items():
            cell_xml.append(f'<Cell Value="{_xml_attr_escape(v)}" Key="{f}"/>')
        # "Deleted" removes the row identified by the row_key cells above. Unlike
        # the classic SOAP plane — whose delete_row can only ever take ROW 0, and
        # which on this grid has no key to address a row with at all — a keyed
        # Deleted section targets a specific row (row_key binding proven live).
        section = ("Inserted" if operation == "insert"
                   else "Deleted" if operation == "delete"
                   else "Modified")
        changes = (f'<RowChanges><{section}><Row i="0"><Cells>'
                   f'{"".join(cell_xml)}</Cells></Row></{section}></RowChanges>')
        inner = (f'<{grid_ctl}><![CDATA[{changes}]]></{grid_ctl}>')
        body = await self._callback(
            _DS_CTL, _DS_ENVELOPE.format(cmd="Save", inner=inner))
        blocks = _parse_control_blocks(body)
        # A well-formed callback response ALWAYS starts with "0|" (the ASP.NET
        # ICallbackEventHandler success/argument-count prefix) — observed on
        # every captured response, success or business-validation error alike.
        # A callback that CRASHES server-side (e.g. a NullReferenceException in
        # the screen's codebehind — proven live: GL301000's Transactions grid)
        # instead returns raw, unwrapped exception text with no "0|" prefix and
        # no parseable control blocks. Silently falling through to the "clean"
        # heuristic below would report `possibly_saved: true` on a request that
        # never even reached validation — exactly the false confidence this
        # tool exists to prevent. Surface the raw text and refuse to guess.
        if not body.startswith("0|") and not blocks:
            return {
                "alert": None,
                "rows_error_text": [], "row_errors": [], "cell_errors": [],
                "server_error": body.strip()[:500],
                "graph_dirty": False,
                "possibly_saved": False,
                "response_len": len(body),
            }
        ds = blocks.get(_DS_BLOCK) or {}
        errs = _grid_errors(body)
        alert = ds.get("alert")
        saved = not alert and not any(errs.values()) and not ds.get("isDirty")
        out = {
            "alert": alert,
            **errs,
            "graph_dirty": bool(ds.get("isDirty")),
            "possibly_saved": saved,
            "response_len": len(body),
        }
        # If a selector rejected a value it couldn't resolve, point the caller at
        # the SubstituteKey gotcha (send the name/description, not the code/id).
        # Check the alert + any per-row/per-cell error text this save surfaced.
        _hint_src = " ".join(filter(None, [alert, *errs.get("rows_error_text", []),
                                           *errs.get("row_errors", []),
                                           *errs.get("cell_errors", [])]))
        _sel_hint = _selector_value_hint(_hint_src)
        if _sel_hint:
            out["selector_hint"] = _sel_hint
        if operation == "insert" and (alert or any(errs.values())):
            # Row i="0" is hardcoded for EVERY insert regardless of how many
            # rows already exist in the live grid (see the RowChanges XML
            # built above) — on a grid that already has >=1 row, a genuine
            # insert can COLLIDE with an existing row instead of landing on
            # a new one. Confirmed live on PY309000/EmployeeBankDetails
            # (external bug report 2026-07-20, reproduced independently):
            # the IDENTICAL insert request (same screen/grid/values) returned
            # DIFFERENT error text across repeat calls in the same session —
            # "cannot be found" once, "cannot be empty" the next — which real
            # business validation of unchanged input would never do. The
            # report's own hypothesis (a fabricated uniqueness error from
            # colliding with the existing row) is plausible but NOT the only
            # symptom observed; treat ANY error text from an insert as
            # potentially describing a collision with an existing row rather
            # than the new data, not confirmed business validation.
            out["note"] = (
                "operation=\"insert\" always targets Row i=\"0\" — there is no "
                "row-count-aware indexing for inserts yet, so on a grid that "
                "already has at least one row this can collide with that "
                "existing row instead of a genuinely new one. Confirmed live: "
                "the identical insert request returned DIFFERENT error text "
                "across repeat calls with no change in input, which real "
                "validation of the same data would not do. Treat this error "
                "as UNRELIABLE for insert on a non-empty grid — verify "
                "independently via run_dac_odata, or retest against a "
                "genuinely empty grid if one is available.")
        if not alert and not any(errs.values()) and ds.get("isDirty"):
            # Dirty graph + zero error text = the RowChanges never BOUND, so
            # validation never fired (proven live on GL202500: both insert and
            # update against the PRIMARY grid of a headerless list screen land
            # here — child grids under a loaded header bind fine). Say so,
            # or the empty error list reads as "no problem found".
            out["note"] = (
                "no error text returned but the graph was left dirty — the "
                "change did not commit and validation never fired. Known "
                "cause: RowChanges against the PRIMARY grid of a headerless "
                "list screen (e.g. GL202500) do not bind on this plane (the "
                "browser uses a per-cell commit flow this tool does not "
                "emulate). The real error is not recoverable this way for "
                "this screen shape.")
        elif saved:
            # possibly_saved=True (no alert, no errors, graph not dirty) is
            # GENUINELY AMBIGUOUS, not a confirmed success — proven live on
            # GL301000: the IDENTICAL request shape that once persisted a
            # change did NOT persist on repeat attempts in a fresh session (5
            # for 5), with every target field echoed ReadOnly="False" — so a
            # readonly cell is not the only cause of a silent no-op here; the
            # mechanism is not fully understood. Flag readonly target fields
            # when present (a REAL, confirmed cause — AP301000: an existing
            # line's Account was locked from grid edit) as a lead, but never
            # claim confirmed success on this signal alone.
            ro = _row0_readonly_fields(body, grid_ctl, columns) if columns else []
            ro_targets = [f for f in list(cells) if f in ro]
            out["note"] = (
                "possibly_saved is UNCONFIRMED, not a guarantee — no error was "
                "returned but the graph was also never marked dirty, which is "
                "AT LEAST AS LIKELY to mean the change never applied at all as "
                "it is to mean a clean silent success (reproduced live: an "
                "identical request that once persisted failed to persist on "
                "repeat, with no read-only or other visible cause). "
                + (f"Target field(s) {ro_targets} ARE read-only on this row — "
                   f"a confirmed cause of a silent no-op." if ro_targets else
                   "No target field was read-only here, so that's not the "
                   "explanation this time.")
                + " ALWAYS verify the real database state via run_dac_odata "
                "before trusting this result either way.")
        return out
