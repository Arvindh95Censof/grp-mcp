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


def _grid_column_slots(body: str) -> list[str | None]:
    """The grid's columns as POSITIONAL slots, `None` for a framework cell.

    A grid callback's Props JSON carries `levels[0].columns` as an array in
    which the leading entries are bare `{}` — the row's file and note indicator
    cells, which have no dataField but DO occupy a `<Cell>` position. Captured
    live (CS205000/AttributeDetails): 7 columns = 2 empty + ValueID,
    Description, SortOrder, Disabled, AttributeID, and each `<Row>` echoes
    exactly 7 `<Cell>`s in that same order. Preserving the empties is what makes
    cell->field alignment exact rather than an end-offset guess.

    Note a column can be `"visible":0` and still be a real, present cell
    (AttributeID above) — invisible on screen, still addressable here.

    Props is entity-escaped inside the CDATA (`&quot;`) while the `<Rows>` XML
    beside it is literal, so this unescapes before parsing while _grid_rows
    must NOT. Bracket-matched rather than regexed: the array nests objects.
    """
    plain = unescape(body)
    i = plain.find('"columns":[')
    if i < 0:
        return []
    start = i + len('"columns":')
    depth = 0
    for j in range(start, len(plain)):
        if plain[j] == "[":
            depth += 1
        elif plain[j] == "]":
            depth -= 1
            if depth == 0:
                try:
                    cols = json.loads(plain[start:j + 1])
                except Exception:  # noqa: BLE001 — malformed Props; caller degrades
                    return []
                return [(c.get("dataField") if isinstance(c, dict) else None) or None
                        for c in cols]
    return []


def _grid_rows(body: str, grid_ctl: str) -> list[dict[str, str]]:
    """Every row of the grid as {dataField: value}, from a Refresh/Save echo.

    This is the read-back the ASPX plane previously had NO way to do: it lets a
    write be verified against the grid's own post-save state without knowing the
    DAC name, which matters because the grids that need this plane are exactly
    the ones the other planes cannot see (PY309000's key is absent from both the
    SOAP container and /structure).

    Values are unescaped INDIVIDUALLY — the `<Cell>` XML is literal in the raw
    body, so unescaping the whole payload first would corrupt a value containing
    a quote into an attribute delimiter. Framework cells are dropped.
    Returns [] if the block/rows are absent (a grid with no rows, or a failed
    Refresh) — an EMPTY LIST AND "COULD NOT PARSE" ARE NOT DISTINGUISHED here,
    so callers must not read absence as proof of deletion on its own.
    """
    slots = _grid_column_slots(body)
    if not slots:
        return []
    m = re.search(rf'<{re.escape(grid_ctl)}[^>]*>(.*?)</{re.escape(grid_ctl)}>',
                  body, re.S)
    region = m.group(1) if m else body
    out: list[dict[str, str]] = []
    for rm in re.finditer(r'<Row i="\d+"[^>]*>(.*?)</Row>', region, re.S):
        cells = re.findall(r'<Cell ([^/>]*)/>', rm.group(1))
        if len(cells) != len(slots):
            # Alignment is positional; a length mismatch means the assumption
            # broke (different level, or a shape not seen live). Skip the row
            # rather than emit fields mapped to the wrong values.
            continue
        row: dict[str, str] = {}
        for field, attrs in zip(slots, cells):
            if not field:
                continue
            vm = re.search(r'Value="([^"]*)"', attrs)
            if vm:
                row[field] = unescape(vm.group(1))
        if row:
            out.append(row)
    return out


def _row_matches(row: dict[str, str], key: dict[str, Any]) -> bool:
    """Does `row` carry every cell of `key`? Compared as STRINGS — the grid
    echoes everything as text, so an int key from run_dac_odata (14551) must
    match the cell "14551"."""
    return all(str(row.get(k, "\0")) == str(v) for k, v in key.items())


# RowChanges section per operation. `Modified`/`Inserted`/`Deleted` are the
# classic grid's own section names; a Save envelope may carry several siblings.
_SECTION = {"insert": "Inserted", "update": "Modified", "delete": "Deleted"}

# The "nothing was sent" tail a refusal carries so its shape matches a real
# (empty) Save result — callers branch on possibly_saved either way.
_EMPTY_SAVE_FIELDS = {"alert": None, "rows_error_text": [], "row_errors": [],
                      "cell_errors": [], "graph_dirty": False,
                      "possibly_saved": False}


def _cells_xml(cells: dict[str, Any] | None, row_key: dict[str, Any] | None,
               old_values: dict[str, Any] | None) -> str:
    """The `<Cell .../>` list for ONE row: the changed cells (each with an
    optional OldValue attr) followed by the row_key cells that address the row.
    Rows are located by these key cells, never by the row's `i` index (proven:
    i is a batch ordinal, not a locator)."""
    parts = []
    for f, v in (cells or {}).items():
        old = (old_values or {}).get(f)
        oattr = f' OldValue="{_xml_attr_escape(old)}"' if old is not None else ""
        parts.append(f'<Cell Value="{_xml_attr_escape(v)}"{oattr} Key="{f}"/>')
    for f, v in (row_key or {}).items():
        parts.append(f'<Cell Value="{_xml_attr_escape(v)}" Key="{f}"/>')
    return "".join(parts)


def _preflight_op(operation: str, cells: dict[str, Any], row_key: dict[str, Any] | None,
                  columns: list[str], rows_before: list[dict[str, str]]) -> dict | None:
    """Validate ONE row change against the grid's real columns and current rows
    before it is sent. Returns a refusal dict (reason + supporting data) or None
    if the op is safe to send. Shared by the single-op and batch paths so both
    refuse identically.

    NOT a complete guard for delete/update: a partial key that is UNIQUE within
    the grid passes here and still no-ops server-side (the server matches the
    FULL key; the grid payload carries no is-key flag). Only the post-Save
    read-back in _verify_one_op catches that — the two checks are layered."""
    if columns:
        colset = set(columns)
        unknown = [f for f in list(cells or {}) + list(row_key or {})
                   if f not in colset]
        if unknown:
            suggestions = {f: f"Cury{f}" for f in unknown if f"Cury{f}" in colset}
            return {
                "refused": (
                    f"field(s) {unknown} are not columns of this grid on the "
                    f"classic page — sending them would crash or silently no-op "
                    f"the callback. Use the exact column names from grid_columns"
                    + (f"; likely mapping: {suggestions}" if suggestions else "")
                    + "."),
                "unknown_fields": unknown, "suggestions": suggestions,
                "grid_columns": columns}
    if row_key and rows_before:
        hits = [r for r in rows_before if _row_matches(r, row_key)]
        if not hits:
            return {
                "refused": (
                    f"row_key {row_key} matches NO row in this grid — the server "
                    f"would match nothing, change nothing, and still return a "
                    f"clean result. Check the value, or supply the row's FULL key "
                    f"(every key column, not just one): see grid_rows for what is "
                    f"actually there."),
                "row_key": row_key, "grid_rows": rows_before,
                "grid_columns": columns}
        if len(hits) > 1 and operation != "insert":
            return {
                "refused": (
                    f"row_key {row_key} matches {len(hits)} rows — it is a PARTIAL "
                    f"key, so the row actually hit would be the server's choice, "
                    f"not yours. Add the remaining key column(s) until exactly one "
                    f"row matches."),
                "row_key": row_key, "matched_rows": hits,
                "grid_rows": rows_before, "grid_columns": columns}
    return None


def _read_save_response(body: str) -> dict[str, Any]:
    """Base result from a Save callback response, shared by the single-op and
    batch paths. Surfaces the server-crash case (no `0|` prefix and no parseable
    control blocks — a raw, unwrapped codebehind exception) instead of guessing
    `possibly_saved: true` on a request that never reached validation."""
    blocks = _parse_control_blocks(body)
    if not body.startswith("0|") and not blocks:
        return {"alert": None, "rows_error_text": [], "row_errors": [],
                "cell_errors": [], "server_error": body.strip()[:500],
                "graph_dirty": False, "possibly_saved": False,
                "response_len": len(body)}
    ds = blocks.get(_DS_BLOCK) or {}
    errs = _grid_errors(body)
    alert = ds.get("alert")
    saved = not alert and not any(errs.values()) and not ds.get("isDirty")
    return {"alert": alert, **errs, "graph_dirty": bool(ds.get("isDirty")),
            "possibly_saved": saved, "response_len": len(body)}


def _verify_one_op(operation: str, cells: dict[str, Any], row_key: dict[str, Any] | None,
                   rows_before: list[dict[str, str]],
                   rows_after: list[dict[str, str]]) -> dict[str, Any]:
    """Per-op verdict from the grid's before/after row snapshots. Only meaningful
    when the Save came back clean (`possibly_saved`) — that is exactly the
    ambiguous case where "no error" cannot tell a silent success from a silent
    no-op.

    `save_verified`: True (the change is visibly there), False (unchanged — the
    silent no-op this plane produces), or "unverified" with a reason when the
    read-back itself cannot decide. Deletes also get `delete_verified` for
    symmetry with the classic SOAP plane's field of the same name. Reads the
    SCREEN's rows, so it proves the grid changed, not that the txn committed —
    run_dac_odata remains the authority."""
    if not rows_after and not rows_before:
        return {"save_verified": "unverified",
                "verify_note": ("could not read any rows back from this grid "
                                "(no rows parsed before OR after), so the change "
                                "cannot be confirmed either way here — verify "
                                "with run_dac_odata.")}
    if operation == "delete":
        gone = not any(_row_matches(r, row_key or {}) for r in rows_after)
        return {
            "save_verified": gone, "delete_verified": gone,
            "verify_note": (
                f"row {row_key} is GONE from the grid on re-read — the delete "
                f"engaged and hit the intended row."
                if gone else
                f"SILENT NO-OP: the Save reported no error, but row {row_key} "
                f"is STILL PRESENT on re-read. Nothing was deleted. A row "
                f"REFERENCED elsewhere is refused exactly like this, with no "
                f"error text — check the screen's rules in kb-mcp-dual.")}
    if operation == "insert":
        grew = len(rows_after) > len(rows_before)
        return {
            "save_verified": grew,
            "verify_note": (
                f"row count went {len(rows_before)} -> {len(rows_after)}: a row "
                f"was added."
                if grew else
                f"SILENT NO-OP: no error was returned but the row count did not "
                f"change ({len(rows_before)}). Note insert targets Row i=\"0\", "
                f"so on a non-empty grid it may have OVERWRITTEN an existing row "
                f"rather than added one — compare the rows.")}
    # update: the keyed row must now carry the values that were sent.
    target = [r for r in rows_after if _row_matches(r, row_key or {})]
    if not target:
        return {"save_verified": "unverified",
                "verify_note": (f"could not find row {row_key} on re-read, so the "
                                f"update cannot be confirmed — verify with "
                                f"run_dac_odata.")}
    mismatched = {f: {"sent": str(v), "now": target[0].get(f)}
                  for f, v in cells.items()
                  if f in target[0] and str(target[0][f]) != str(v)}
    applied = [f for f in cells if f in target[0]]
    if not applied:
        return {"save_verified": "unverified",
                "verify_note": ("none of the changed fields are readable as grid "
                                "cells, so the update cannot be confirmed — verify "
                                "with run_dac_odata.")}
    return {
        "save_verified": not mismatched,
        "verify_note": (
            f"re-read confirms {applied} now hold the values sent."
            if not mismatched else
            f"SILENT NO-OP (at least partly): the Save reported no error but "
            f"{list(mismatched)} did NOT change on re-read: {mismatched}.")}


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

    async def _grid_refresh(self, grid_ctl: str) -> str:
        """Raw body of a targeted grid Refresh callback ("" if it fails).

        Split out from _grid_columns because the SAME response also carries every
        row (see _grid_rows) — the callers that need a read-back would otherwise
        pay for a second identical round trip to get data they already fetched.
        """
        cb_target = grid_ctl.replace("_", "$")
        try:
            return await self._callback(
                cb_target,
                f'Refresh|<{grid_ctl} LoadedLevel="-1"><![CDATA[]]></{grid_ctl}>')
        except ScreenError:
            return ""

    async def _grid_snapshot(self, grid_ctl: str) -> tuple[list[str], list[dict[str, str]]]:
        """(columns, rows) from ONE Refresh — the grid's current state."""
        body = await self._grid_refresh(grid_ctl)
        if not body:
            return [], []
        return ([c for c in _grid_column_slots(body) if c],
                _grid_rows(body, grid_ctl))

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
        body = await self._grid_refresh(grid_ctl)
        if not body:
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

        columns, rows_before = await self._grid_snapshot(grid_ctl)
        # Validate this one change against the grid's real columns + rows before
        # sending. The refusal carries the empty-Save tail so its shape matches a
        # real (nothing-sent) result. NOTE this guard is layered, not complete —
        # a partial key unique within the grid slips through and is caught only by
        # the post-Save read-back below (see _preflight_op / _verify_one_op).
        refusal = _preflight_op(operation, cells, row_key, columns, rows_before)
        if refusal:
            return {**refusal, **_EMPTY_SAVE_FIELDS}

        # One row, one section. i="0" is the row's ordinal WITHIN THIS BATCH, not
        # a position in the live grid (proven: an insert at i="99" into a two-row
        # grid appended cleanly — the index addresses nothing; rows are located
        # by the row_key CELLS). "Deleted" removes the keyed row, which the
        # classic SOAP plane cannot do on a grid whose key it does not expose.
        section = _SECTION[operation]
        changes = (f'<RowChanges><{section}><Row i="0"><Cells>'
                   f'{_cells_xml(cells, row_key, old_values)}'
                   f'</Cells></Row></{section}></RowChanges>')
        inner = (f'<{grid_ctl}><![CDATA[{changes}]]></{grid_ctl}>')
        body = await self._callback(
            _DS_CTL, _DS_ENVELOPE.format(cmd="Save", inner=inner))
        out = _read_save_response(body)
        if "server_error" in out:
            return out  # codebehind crashed before validation — never guess saved
        alert = out["alert"]
        any_err = any([out["rows_error_text"], out["row_errors"], out["cell_errors"]])
        saved = out["possibly_saved"]
        # If a selector rejected a value it couldn't resolve, point the caller at
        # the SubstituteKey gotcha (send the name/description, not the code/id).
        _hint_src = " ".join(filter(None, [alert, *out["rows_error_text"],
                                           *out["row_errors"], *out["cell_errors"]]))
        _sel_hint = _selector_value_hint(_hint_src)
        if _sel_hint:
            out["selector_hint"] = _sel_hint
        if operation == "insert" and (alert or any_err):
            # THE "Row i=0 COLLIDES WITH AN EXISTING ROW" THEORY IS REFUTED
            # (tested live 2026-07-20, csmdev CS205000/AttributeDetails).
            # `i` is a BATCH ORDINAL, not a row locator: the server assigns the
            # new row's position itself. Decisive test — inserting at i="99"
            # into a TWO-row grid appended a third row cleanly, leaving both
            # existing rows intact; an insert at i="0" into a one-row grid
            # likewise appended rather than overwriting row 0. That is coherent
            # with how the other operations behave: delete and update target
            # rows through the row_key CELLS, never through `i`, so nothing on
            # this plane uses the index to address a row.
            #
            # What is NOT explained: PY309000/EmployeeBankDetails returned
            # DIFFERENT error text across IDENTICAL repeat inserts ("cannot be
            # found", then "cannot be empty") — real validation of unchanged
            # input would not do that. The external bug report blamed the
            # hardcoded index; that mechanism is now ruled out, so the cause of
            # the nondeterminism is genuinely unknown and PY309000 has not been
            # retested since. Keep warning about the SYMPTOM, drop the refuted
            # explanation.
            out["note"] = (
                "an insert that reports an error is not always reporting real "
                "business validation: on PY309000/EmployeeBankDetails the "
                "IDENTICAL insert returned DIFFERENT error text across repeat "
                "calls with no change in input. The cause is unknown — the "
                "previously-suspected row-index collision has been RULED OUT "
                "(the row index is a batch ordinal, not a row locator; proven "
                "live on CS205000). Verify what actually happened with "
                "run_dac_odata rather than trusting this message.")
        if not alert and not any_err and out["graph_dirty"]:
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
        if saved:
            out.update(await self._verify_save(
                grid_ctl, cells, row_key, operation, rows_before))
        return out

    async def _verify_save(self, grid_ctl: str, cells: dict[str, Any],
                           row_key: dict[str, Any] | None, operation: str,
                           rows_before: list[dict[str, str]]) -> dict[str, Any]:
        """Re-read the grid after an apparently-clean single-op Save and return
        the per-op verdict (see _verify_one_op) plus the row counts."""
        rows_after = _grid_rows(await self._grid_refresh(grid_ctl), grid_ctl)
        v = _verify_one_op(operation, cells, row_key or {}, rows_before, rows_after)
        v.setdefault("rows_before", len(rows_before))
        v.setdefault("rows_after", len(rows_after))
        return v

    async def replay_grid_batch(self, grid_view: str,
                                operations: list[dict[str, Any]]) -> dict[str, Any]:
        """Apply MULTIPLE row changes to ONE grid in a SINGLE atomic Save.

        The point is cross-row invariants. A grid like PY309000 EmployeeBankDetails
        enforces "percent must sum to 100 across all rows", so a STANDALONE delete
        of one row is rejected — the survivors no longer sum to 100. The browser
        deletes-and-rebalances in one Save; this does the same by emitting several
        RowChanges sections (e.g. <Deleted> + <Modified>) in one envelope.

        operations: a list of {operation, cells, row_key, old_values} dicts, each
        shaped exactly like replay_grid_save's args:
            {"operation": "delete", "row_key": {"EmployeeBankDetailID": 14551}}
            {"operation": "update", "row_key": {"EmployeeBankDetailID": 14550},
             "cells": {"Percent": 100}}
        Order is preserved within each section. Every op is pre-flighted against
        ONE grid snapshot (unknown columns, no-match / partial keys) and the WHOLE
        batch is refused if any op fails — nothing partial is sent. After a clean
        Save the grid is re-read ONCE and each op gets its own verdict.

        CAVEAT — verifying an INSERT inside a batch that also deletes is
        unreliable (insert is verified by row-count growth, which a concurrent
        delete masks); such inserts come back save_verified "unverified". As
        always this proves the GRID changed, not that the txn committed — confirm
        with run_dac_odata. DESTRUCTIVE if any op is a delete.
        """
        if not operations:
            raise ScreenError("replay_grid_batch: operations is empty")
        for i, op in enumerate(operations):
            if op.get("operation") not in _SECTION:
                raise ScreenError(
                    f"operations[{i}]: unknown operation {op.get('operation')!r} "
                    f"— use one of insert/update/delete.")
            if op["operation"] == "delete" and not op.get("row_key"):
                raise ScreenError(
                    f"operations[{i}]: delete requires row_key (the row's FULL "
                    f"key); without it it would fall back to row 0.")

        grid_ctl, tab_ctl, tab_idx = self.find_grid_control(grid_view)
        if tab_ctl is not None:
            self._activate_tab(tab_ctl, tab_idx)
        columns, rows_before = await self._grid_snapshot(grid_ctl)

        # Pre-flight every op against the SAME snapshot; refuse the whole batch if
        # any fails (an atomic Save that would half-apply is worse than not sent).
        refusals = []
        for i, op in enumerate(operations):
            r = _preflight_op(op["operation"], op.get("cells") or {},
                              op.get("row_key"), columns, rows_before)
            if r:
                refusals.append({"op_index": i, **r})
        if refusals:
            return {"grid_view": grid_view, "operations": len(operations),
                    "refused_ops": refusals, "grid_columns": columns,
                    "possibly_saved": False,
                    "note": ("no Save was sent — every operation is validated "
                             "against the grid first and one or more failed.")}

        # Build one RowChanges with sibling sections, grouped by section; i is a
        # per-section batch ordinal (not a locator — rows are keyed by cells).
        by_section: dict[str, list[str]] = {}
        for op in operations:
            sec = _SECTION[op["operation"]]
            rows = by_section.setdefault(sec, [])
            rows.append(f'<Row i="{len(rows)}"><Cells>'
                        f'{_cells_xml(op.get("cells") or {}, op.get("row_key"), op.get("old_values"))}'
                        f'</Cells></Row>')
        changes = ("<RowChanges>"
                   + "".join(f"<{sec}>{''.join(rows)}</{sec}>"
                             for sec, rows in by_section.items())
                   + "</RowChanges>")
        inner = f'<{grid_ctl}><![CDATA[{changes}]]></{grid_ctl}>'
        body = await self._callback(_DS_CTL, _DS_ENVELOPE.format(cmd="Save", inner=inner))

        out = _read_save_response(body)
        result: dict[str, Any] = {"grid_view": grid_view,
                                  "operations": len(operations), **out}
        if "server_error" in out:
            return result
        _hint = _selector_value_hint(" ".join(filter(None, [
            out["alert"], *out["rows_error_text"], *out["row_errors"],
            *out["cell_errors"]])))
        if _hint:
            result["selector_hint"] = _hint
        if out["possibly_saved"]:
            rows_after = _grid_rows(await self._grid_refresh(grid_ctl), grid_ctl)
            has_delete = any(o["operation"] == "delete" for o in operations)
            verifs = []
            for i, op in enumerate(operations):
                v = _verify_one_op(op["operation"], op.get("cells") or {},
                                   op.get("row_key"), rows_before, rows_after)
                if op["operation"] == "insert" and has_delete and v["save_verified"] is not True:
                    v = {"save_verified": "unverified",
                         "verify_note": ("insert verification is by row-count "
                                         "growth, which a concurrent delete in "
                                         "this batch masks — confirm with "
                                         "run_dac_odata.")}
                verifs.append({"op_index": i, "operation": op["operation"], **v})
            result["verifications"] = verifs
            result["all_verified"] = all(v["save_verified"] is True for v in verifs)
            result["rows_before"] = len(rows_before)
            result["rows_after"] = len(rows_after)
        return result
