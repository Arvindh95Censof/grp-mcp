"""Unit tests for the classic-ASPX diagnostic plane (aspx.py) — pure logic,
no live instance. Fixtures mirror payloads captured live on csmdev PY309000
(2026-07-17), including the exact discovery bug that made the first Save
silently no-op (RowChanges addressed to the wrong grid control), and on
GL301000 (2026-07-18), where the classic page's Save callback ITSELF crashes
server-side (a raw, unwrapped NullReferenceException, not a validation error)."""

from __future__ import annotations

import asyncio

import pytest

from grp_mcp.aspx import (AspxDiagnostic, _grid_column_slots, _grid_errors,
                          _grid_rows, _parse_control_blocks,
                          _parse_hidden_inputs, _row_matches,
                          _row0_readonly_fields, _xml_attr_escape)


def _diag_with_html(html: str) -> AspxDiagnostic:
    d = AspxDiagnostic.__new__(AspxDiagnostic)  # no ScreenClient needed for parsing
    d._html = html
    d._state = {}
    return d


# ---- hidden-input harvest ---------------------------------------------------

def test_parse_hidden_inputs_keeps_encoded_state_verbatim():
    html = (
        '<input type="hidden" name="__RequestVerificationToken" value="tok123" />'
        '<input type="hidden" name="ctl00_phG_tab_state" '
        'value="%3CPXBoundPanel%20SelectedIndex%3D%224%22%2F%3E" />'
        '<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="" />'
        '<input type="text" name="notHidden" value="x" />'
    )
    h = _parse_hidden_inputs(html)
    assert h["__RequestVerificationToken"] == "tok123"
    # single-URL-encoded XML must survive untouched (the POST encodes it again)
    assert h["ctl00_phG_tab_state"] == "%3CPXBoundPanel%20SelectedIndex%3D%224%22%2F%3E"
    assert h["__VIEWSTATE"] == ""
    assert "notHidden" not in h


def test_parse_hidden_inputs_html_unescapes_values():
    html = '<input type="hidden" name="f" value="a&amp;b&quot;c" />'
    assert _parse_hidden_inputs(html)["f"] == 'a&b"c'


# ---- callback-response block parsing ---------------------------------------

_SAVE_RESPONSE = (
    '0|<ctl00_phDS_ds><![CDATA[<ctl00_phDS_ds Props="{&quot;alert&quot;:'
    '&quot;Percent should be 100 for sum of all banks&quot;,'
    '&quot;isDirty&quot;:1}"/>]]></ctl00_phDS_ds>'
    '<ctl00_phF_form><![CDATA[<ctl00_phF_form Props="{&quot;dataKey&quot;:'
    '&quot;employees,/wEWAg&quot;,&quot;pageCount&quot;:-1}"/>]]></ctl00_phF_form>'
)


def test_parse_control_blocks_decodes_escaped_props():
    blocks = _parse_control_blocks(_SAVE_RESPONSE)
    assert blocks["ctl00_phDS_ds"]["alert"] == \
        "Percent should be 100 for sum of all banks"
    assert blocks["ctl00_phDS_ds"]["isDirty"] == 1
    assert blocks["ctl00_phF_form"]["dataKey"] == "employees,/wEWAg"


def test_grid_errors_extracts_rows_row_and_cell_detail():
    body = (
        '<Rows Level="0" ErrorLevel="3" '
        'ErrorText="Percent should be 100 for sum of all banks" HashCode="">'
        '<Row i="0" Error="Percent should be 100 for sum of all banks">'
        '<Cells><Cell Value="50" Error="Percent cannot be empty" /></Cells>'
        '</Row></Rows>'
    )
    e = _grid_errors(body)
    assert e["rows_error_text"] == ["Percent should be 100 for sum of all banks"]
    assert e["row_errors"] == ["Percent should be 100 for sum of all banks"]
    assert e["cell_errors"] == ["Percent cannot be empty"]


def test_grid_errors_handles_entity_escaped_attrs():
    body = '<Rows ErrorText=&quot;Boom&quot;><Row i=&quot;0&quot; Error=&quot;Boom&quot;>'
    e = _grid_errors(body)
    assert e["rows_error_text"] == ["Boom"]
    assert e["row_errors"] == ["Boom"]


def test_grid_errors_empty_on_clean_body():
    e = _grid_errors("0|<ctl00_phDS_ds/>")
    assert e == {"rows_error_text": [], "row_errors": [], "cell_errors": []}


# ---- grid-control discovery -------------------------------------------------

# Mirrors the LIVE PY309000 page structure that broke the first implementation:
# a t3 grid's id is mentioned (commandSourceID) just before the t4 grid's var
# declaration, and the id in the declaration has a leading underscore (so any
# \b-anchored "nearest preceding id" scan sees only the t3 mention and picks
# the WRONG control — the Save then no-ops with a clean 54-char ack).
_PAGE_JS = (
    'var _ctl00_phG_tab_t3_PXFormView2_PXGrid3_menu = {"commandSourceID":'
    '"ctl00_phG_tab_t3_PXFormView2_PXGrid3","items":{}};\n'
    'var _ctl00_phG_tab_t4_PXFormView4 = {"dataKey":"employments,/wEWAA==",'
    '"dataMember":"Employments"};\n'
    'var _ctl00_phG_tab_t4_PXGrid1 = {"layoutLoaded":1,"callbacks":[{},{}],'
    '"dataMember":"EmployeeBankDetails","levels":[{"columns":[]}]};\n'
)


def test_find_grid_control_resolves_var_declaration_owner():
    d = _diag_with_html(_PAGE_JS)
    ctl, tab, idx = d.find_grid_control("EmployeeBankDetails")
    assert ctl == "ctl00_phG_tab_t4_PXGrid1"      # NOT the t3 grid
    assert tab == "ctl00_phG_tab"
    assert idx == 4


def test_find_grid_control_formview_datamember_not_mistaken_for_grid():
    # "Employments" is bound to a FORM view (no "levels"): still resolvable,
    # but a same-named grid with levels elsewhere must win over the form.
    html = _PAGE_JS + (
        'var _ctl00_phG_tab_t5_PXGrid9 = {"dataMember":"Employments",'
        '"levels":[{"columns":[]}]};\n')
    d = _diag_with_html(html)
    ctl, tab, idx = d.find_grid_control("Employments")
    assert ctl == "ctl00_phG_tab_t5_PXGrid9"
    assert idx == 5


def test_find_grid_control_outside_tab_container():
    html = 'var _ctl00_phG_grid = {"dataMember":"Records","levels":[{}]};'
    d = _diag_with_html(html)
    ctl, tab, idx = d.find_grid_control("Records")
    assert ctl == "ctl00_phG_grid"
    assert tab is None and idx is None


def test_find_grid_control_missing_view_raises():
    d = _diag_with_html(_PAGE_JS)
    try:
        d.find_grid_control("NoSuchView")
    except Exception as e:
        assert "NoSuchView" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected ScreenError")


# ---- key-param discovery ----------------------------------------------------

def test_key_param_prefers_selector_text_subinput():
    d = _diag_with_html(
        '<input name="ctl00$phF$form$edEmployeeCD$text" id="x" />')
    assert d._key_param("EmployeeCD") == "ctl00$phF$form$edEmployeeCD$text"


def test_key_param_plain_input_and_fallback():
    d = _diag_with_html('<input name="ctl00$phF$form$edCode" id="x" />')
    assert d._key_param("Code") == "ctl00$phF$form$edCode"
    # nothing in HTML -> conventional page-template guess
    d2 = _diag_with_html("<html></html>")
    assert d2._key_param("Code") == "ctl00$phF$form$edCode$text"


# ---- misc -------------------------------------------------------------------

def test_xml_attr_escape():
    assert _xml_attr_escape('a<b>&"c') == "a&lt;b&gt;&amp;&quot;c"
    assert _xml_attr_escape(None) == ""
    assert _xml_attr_escape(50) == "50"


# ---- replay_grid_save: server-side crash + column validation (GL301000) ----

_GL301000_GRID_JS = (
    'var _ctl00_phG_tab_t0_grid = {"dataMember":"GLTranModuleBatNbr",'
    '"levels":[{"columns":[]}]};\n')

# a grid-Refresh response carrying the AUTHORITATIVE column dataFields (the
# classic grid speaks CuryCreditAmt, not the modern plane's CreditAmt)
_GL_REFRESH_BODY = (
    '0|<ctl00_phG_tab_t0_grid><![CDATA[<ctl00_phG_tab_t0_grid Props="'
    '{&quot;levels&quot;:[{&quot;columns&quot;:[{&quot;dataField&quot;:&quot;LineNbr&quot;},'
    '{&quot;dataField&quot;:&quot;CuryDebitAmt&quot;},'
    '{&quot;dataField&quot;:&quot;CuryCreditAmt&quot;},'
    '{&quot;dataField&quot;:&quot;Module&quot;},'
    '{&quot;dataField&quot;:&quot;BatchNbr&quot;}]}]}"/>]]></ctl00_phG_tab_t0_grid>')


def _diag_stub(html: str, save_body: str,
               refresh_body: str = _GL_REFRESH_BODY) -> AspxDiagnostic:
    """AspxDiagnostic working off `html`, with _callback stubbed: a Refresh
    command gets `refresh_body`, everything else gets `save_body` — no network."""
    d = _diag_with_html(html)

    async def fake_callback(cbid, cbparam, extra=None):
        return refresh_body if cbparam.startswith("Refresh|") else save_body

    d._callback = fake_callback  # type: ignore[method-assign]
    return d


def test_replay_grid_save_refuses_unknown_column_with_suggestion():
    # THE GL301000 root cause: the modern plane's field name (CreditAmt) is not
    # a column of the classic grid (CuryCreditAmt) — sending it crashed the
    # callback (NullReferenceException) or silently no-op'd. Must be REFUSED
    # up front with the real column list and the Cury-prefix suggestion.
    d = _diag_stub(_GL301000_GRID_JS, "should-never-be-sent")
    result = asyncio.run(d.replay_grid_save(
        "GLTranModuleBatNbr", {"CreditAmt": 50},
        row_key={"Module": "GL", "BatchNbr": "GL21000001", "LineNbr": 1}))
    assert result["unknown_fields"] == ["CreditAmt"]
    assert result["suggestions"] == {"CreditAmt": "CuryCreditAmt"}
    assert "CuryCreditAmt" in result["grid_columns"]
    assert result["possibly_saved"] is False


def test_replay_grid_save_surfaces_raw_server_crash_not_false_success():
    # Captured live: a callback that CRASHES server-side returns raw, unwrapped
    # exception text (no "0|" envelope, no Props blocks). Before the fix, the
    # "clean save" heuristic misread this as success (possibly_saved=True) —
    # exactly backwards, since the callback never even reached validation.
    crash_body = "eObject reference not set to an instance of an object."
    d = _diag_stub(_GL301000_GRID_JS, crash_body)
    result = asyncio.run(d.replay_grid_save(
        "GLTranModuleBatNbr", {"CuryCreditAmt": 50},
        row_key={"Module": "GL", "BatchNbr": "GL21000001", "LineNbr": 1}))
    assert result["possibly_saved"] is False
    assert result["server_error"] == crash_body
    assert result["alert"] is None


def test_replay_grid_save_normal_error_has_no_server_error_key():
    # A well-formed "0|"-prefixed response with a real business-rule alert
    # must NOT be misclassified as a server crash.
    normal_body = (
        '0|<ctl00_phDS_ds><![CDATA[<ctl00_phDS_ds Props="{&quot;alert&quot;:'
        '&quot;Percent should be 100 for sum of all banks&quot;,'
        '&quot;isDirty&quot;:1}"/>]]></ctl00_phDS_ds>'
    )
    d = _diag_stub(_GL301000_GRID_JS, normal_body)
    result = asyncio.run(d.replay_grid_save(
        "GLTranModuleBatNbr", {"CuryCreditAmt": 50},
        row_key={"Module": "GL", "BatchNbr": "GL21000001", "LineNbr": 1}))
    assert "server_error" not in result
    assert result["alert"] == "Percent should be 100 for sum of all banks"
    assert result["possibly_saved"] is False


def test_replay_grid_save_dirty_without_error_gets_honest_note():
    # Proven live on GL202500 (headerless list screen): RowChanges against the
    # PRIMARY grid never bind — the Save answers clean with isDirty=1 and ZERO
    # error text. An empty error list must not read as "no problem found"; the
    # result carries an explicit note that validation never fired.
    dirty_body = (
        '0|<ctl00_phDS_ds><![CDATA[<ctl00_phDS_ds Props="{&quot;popupMessage&quot;:'
        'null,&quot;isDirty&quot;:1}"/>]]></ctl00_phDS_ds>'
    )
    d = _diag_stub(_GL301000_GRID_JS, dirty_body)
    result = asyncio.run(d.replay_grid_save(
        "GLTranModuleBatNbr", {"CuryCreditAmt": 50},
        row_key={"LineNbr": 1}))
    assert result["possibly_saved"] is False
    assert "did not commit" in result["note"]
    assert result["alert"] is None


def test_replay_grid_save_no_note_when_error_present():
    body = (
        '0|<ctl00_phDS_ds><![CDATA[<ctl00_phDS_ds Props="{&quot;alert&quot;:'
        '&quot;Boom&quot;,&quot;isDirty&quot;:1}"/>]]></ctl00_phDS_ds>')
    d = _diag_stub(_GL301000_GRID_JS, body)
    result = asyncio.run(d.replay_grid_save(
        "GLTranModuleBatNbr", {"CuryCreditAmt": 50}))
    assert result["alert"] == "Boom"
    assert "note" not in result


def test_replay_grid_save_delete_emits_deleted_section_with_row_key():
    # operation="delete" must emit a <Deleted> RowChanges section carrying the
    # row_key cells — that key is what targets a SPECIFIC row (binding proven
    # live on PY309000: an update keyed to row 1 left row 0 untouched).
    sent = {}

    d = _diag_with_html(_GL301000_GRID_JS)

    async def fake_callback(cbid, cbparam, extra=None):
        if cbparam.startswith("Refresh|"):
            return _GL_REFRESH_BODY
        sent["body"] = cbparam
        return ('0|<ctl00_phDS_ds><![CDATA[<ctl00_phDS_ds Props="{}"/>'
                ']]></ctl00_phDS_ds>')

    d._callback = fake_callback  # type: ignore[method-assign]
    asyncio.run(d.replay_grid_save("GLTranModuleBatNbr", {},
                                   row_key={"LineNbr": 7}, operation="delete"))
    assert "<Deleted>" in sent["body"]
    assert "Inserted" not in sent["body"] and "Modified" not in sent["body"]
    assert 'Key="LineNbr"' in sent["body"] and 'Value="7"' in sent["body"]


def test_replay_grid_save_delete_emits_every_key_cell_for_composite_key():
    # A COMPOSITE key must emit ALL its cells — a partial key matches nothing and
    # the server silently no-ops (proven live on CS205000: ValueID alone did
    # nothing; AttributeID+ValueID deleted the row). The XML builder is what has
    # to carry every part; passing the full key is the caller's responsibility.
    sent = {}
    d = _diag_with_html(_GL301000_GRID_JS)

    async def fake_callback(cbid, cbparam, extra=None):
        if cbparam.startswith("Refresh|"):
            return _GL_REFRESH_BODY
        sent["body"] = cbparam
        return ('0|<ctl00_phDS_ds><![CDATA[<ctl00_phDS_ds Props="{}"/>'
                ']]></ctl00_phDS_ds>')

    d._callback = fake_callback  # type: ignore[method-assign]
    asyncio.run(d.replay_grid_save(
        "GLTranModuleBatNbr", {},
        row_key={"Module": "GL", "BatchNbr": "GL0001"}, operation="delete"))
    assert 'Key="Module"' in sent["body"] and 'Value="GL"' in sent["body"]
    assert 'Key="BatchNbr"' in sent["body"] and 'Value="GL0001"' in sent["body"]


def test_replay_grid_save_delete_without_row_key_is_refused():
    # No key -> the Deleted section would carry nothing identifying and the
    # server falls back to row 0, silently destroying the WRONG row. Must refuse
    # BEFORE any network call.
    from grp_mcp.screen import ScreenError

    d = _diag_with_html(_GL301000_GRID_JS)

    async def boom(cbid, cbparam, extra=None):  # must never be reached
        raise AssertionError("no callback should be made without a row_key")

    d._callback = boom  # type: ignore[method-assign]
    with pytest.raises(ScreenError) as e:
        asyncio.run(d.replay_grid_save("GLTranModuleBatNbr", {},
                                       operation="delete"))
    assert "requires row_key" in str(e.value)
    assert "row 0" in str(e.value)


def test_replay_grid_save_attaches_selector_hint_on_cannot_be_found():
    # A save whose alert is a selector "cannot be found" error gets a
    # selector_hint pointing at the SubstituteKey gotcha; an unrelated alert
    # (percent rule) does not.
    found_body = (
        '0|<ctl00_phDS_ds><![CDATA[<ctl00_phDS_ds Props="{&quot;alert&quot;:'
        '&quot;\'Employee Bank\' cannot be found in the system.&quot;,'
        '&quot;isDirty&quot;:1}"/>]]></ctl00_phDS_ds>')
    d = _diag_stub(_GL301000_GRID_JS, found_body)
    result = asyncio.run(d.replay_grid_save(
        "GLTranModuleBatNbr", {"CuryCreditAmt": 50}))
    assert "selector_hint" in result
    assert "SubstituteKey" in result["selector_hint"]

    percent_body = (
        '0|<ctl00_phDS_ds><![CDATA[<ctl00_phDS_ds Props="{&quot;alert&quot;:'
        '&quot;Percent should be 100 for sum of all banks&quot;,'
        '&quot;isDirty&quot;:1}"/>]]></ctl00_phDS_ds>')
    d2 = _diag_stub(_GL301000_GRID_JS, percent_body)
    result2 = asyncio.run(d2.replay_grid_save(
        "GLTranModuleBatNbr", {"CuryCreditAmt": 50}))
    assert "selector_hint" not in result2


def test_replay_grid_save_insert_with_error_gets_row_collision_note():
    # An insert that errors must still be flagged as potentially unreliable:
    # PY309000/EmployeeBankDetails returned DIFFERENT error text across
    # IDENTICAL repeat inserts (external report 2026-07-20, reproduced), which
    # real validation of unchanged input would not do. The SYMPTOM stands; the
    # originally-blamed mechanism (a hardcoded row index colliding with row 0)
    # was disproven live in 2026-07-20 testing and must not be reasserted here
    # — see test_insert_error_note_does_not_blame_the_row_index. Update
    # operations must NOT get this note (see
    # test_replay_grid_save_no_note_when_error_present above).
    body = (
        '0|<ctl00_phDS_ds><![CDATA[<ctl00_phDS_ds Props="{&quot;alert&quot;:'
        '&quot;Boom&quot;,&quot;isDirty&quot;:1}"/>]]></ctl00_phDS_ds>')
    d = _diag_stub(_GL301000_GRID_JS, body)
    result = asyncio.run(d.replay_grid_save(
        "GLTranModuleBatNbr", {"CuryCreditAmt": 50}, operation="insert"))
    assert result["alert"] == "Boom"
    assert "PY309000" in result["note"]
    assert "cause is unknown" in result["note"]


# ---- replay_grid_save: unconfirmed "clean" no-op (GL301000, live re-probe) --
#
# 2026-07-19: re-verifying with a fresh session found the earlier "GL301000
# Save persists cleanly" result did NOT reproduce — 5/5 repeat attempts of the
# IDENTICAL request shape left the graph un-dirtied with every target field
# echoed ReadOnly="False" (so read-only is A cause, proven on AP301000, but
# NOT the only one — the full mechanism isn't understood). The tool must not
# claim confirmed success on isDirty=false + no error; it's genuinely
# ambiguous and must say so, surfacing a readonly hit as a lead when present.

def test_row0_readonly_fields_detects_locked_cell():
    body = (
        '0|<ctl00_phG_tab_t0_grid><![CDATA[<ctl00_phG_tab_t0_grid Props="{}">'
        '<Rows Level="0"><Row i="0"><Cells>'
        '<Cell Value="0" Text="control@RowFileEmpty" />'
        '<Cell Value="1" Key="LineNbr" ReadOnly="False" />'
        '<Cell Value="999999999" Key="AccountID" ReadOnly="True" />'
        '<Cell Value="100.00" Key="CuryDebitAmt" ReadOnly="False" />'
        '</Cells></Row></Rows>]]></ctl00_phG_tab_t0_grid>')
    ro = _row0_readonly_fields(
        body, "ctl00_phG_tab_t0_grid", ["LineNbr", "AccountID", "CuryDebitAmt"])
    assert ro == ["AccountID"]


def test_replay_grid_save_ambiguous_noop_names_readonly_target():
    # AP301000 shape: AccountID IS read-only on this row -> confirmed cause.
    body = (
        '0|<ctl00_phDS_ds><![CDATA[<ctl00_phDS_ds Props="{}"/>]]></ctl00_phDS_ds>'
        '<ctl00_phG_tab_t0_grid><![CDATA[<ctl00_phG_tab_t0_grid Props="{}">'
        '<Rows Level="0"><Row i="0"><Cells>'
        '<Cell Value="0" Text="x" />'
        '<Cell Value="1" Key="LineNbr" ReadOnly="False" />'
        '<Cell Value="999999999" Key="AccountID" ReadOnly="True" />'
        '</Cells></Row></Rows>]]></ctl00_phG_tab_t0_grid>')
    d = _diag_stub(_GL301000_GRID_JS, body,
                   refresh_body=(
                       '0|<ctl00_phG_tab_t0_grid><![CDATA[<ctl00_phG_tab_t0_grid '
                       'Props="{&quot;levels&quot;:[{&quot;columns&quot;:['
                       '{&quot;dataField&quot;:&quot;LineNbr&quot;},'
                       '{&quot;dataField&quot;:&quot;AccountID&quot;}]}]}"/>'
                       ']]></ctl00_phG_tab_t0_grid>'))
    result = asyncio.run(d.replay_grid_save(
        "GLTranModuleBatNbr", {"AccountID": 999999999}, row_key={"LineNbr": 1}))
    assert result["possibly_saved"] is True  # the honest label, not a lie
    assert "UNCONFIRMED" in result["note"]
    assert "AccountID" in result["note"]
    assert "ARE read-only" in result["note"]


def test_replay_grid_save_ambiguous_noop_no_readonly_explanation():
    # No column metadata available (Refresh failed) -> can't check readonly;
    # note must still flag the ambiguity, without fabricating a cause.
    body = '0|<ctl00_phDS_ds><![CDATA[<ctl00_phDS_ds Props="{}"/>]]></ctl00_phDS_ds>'
    d = _diag_with_html(_GL301000_GRID_JS)

    from grp_mcp.screen import ScreenError

    async def fake_callback(cbid, cbparam, extra=None):
        if cbparam.startswith("Refresh|"):
            raise ScreenError("refresh broke")
        return body

    d._callback = fake_callback  # type: ignore[method-assign]
    result = asyncio.run(d.replay_grid_save(
        "GLTranModuleBatNbr", {"CuryCreditAmt": 50}, row_key={"LineNbr": 1}))
    assert "UNCONFIRMED" in result["note"]
    assert "No target field was read-only" in result["note"]


def test_replay_grid_save_skips_validation_when_refresh_fails():
    # If the Refresh itself errors, diagnosis proceeds without column
    # validation (best-effort) rather than blocking.
    from grp_mcp.aspx import AspxDiagnostic as _AD  # noqa: F401 (clarity)
    d = _diag_with_html(_GL301000_GRID_JS)
    normal_body = (
        '0|<ctl00_phDS_ds><![CDATA[<ctl00_phDS_ds Props="{&quot;alert&quot;:'
        '&quot;Boom&quot;,&quot;isDirty&quot;:1}"/>]]></ctl00_phDS_ds>')

    from grp_mcp.screen import ScreenError

    async def fake_callback(cbid, cbparam, extra=None):
        if cbparam.startswith("Refresh|"):
            raise ScreenError("refresh broke")
        return normal_body

    d._callback = fake_callback  # type: ignore[method-assign]
    result = asyncio.run(d.replay_grid_save(
        "GLTranModuleBatNbr", {"AnythingGoes": 1}))
    assert result["alert"] == "Boom"
    assert "refused" not in result


# ---- grid row read-back (v0.64.13) ----------------------------------------
# VERBATIM capture of a real Refresh callback (csmstg DBKK, CS205000, attribute
# COPYPO, 3 detail values). Note the two leading {} framework columns and the
# visible:0 AttributeID column — both are real <Cell> positions, which is why
# alignment is positional over ALL slots rather than an end-offset guess.
_CS205000_REFRESH_BODY = (
    '0|<ctl00_phG_grid><ctl00_phG_grid><![CDATA[<ctl00_phG_grid Props="{&quot'
    ';colsFilterActive&quot;:0,&quot;hidden&quot;:0,&quot;dataMember&quot;:&q'
    'uot;AttributeDetails&quot;,&quot;delDefaultsVisible&quot;:1,&quot;emptyM'
    'essageMode&quot;:0,&quot;levels&quot;:[{&quot;columns&quot;:[{},{},{&quo'
    't;dataField&quot;:&quot;ValueID&quot;},{&quot;dataField&quot;:&quot;Desc'
    'ription&quot;},{&quot;dataField&quot;:&quot;SortOrder&quot;},{&quot;data'
    'Field&quot;:&quot;Disabled&quot;},{&quot;dataField&quot;:&quot;Attribute'
    'ID&quot;,&quot;visible&quot;:0}]}],&quot;pageSize&quot;:200,&quot;totalR'
    'owCount&quot;:-1}"><Rows Level="0" HashCode=""><Row i="0"><Cells><Cell V'
    'alue="0" Text="control@RowFileEmpty" /><Cell Value="0" Text="control@Row'
    'NoteEmpty" /><Cell Value="1" /><Cell Value="ASAL" /><Cell Value="" /><Ce'
    'll Value="False" /><Cell Value="COPYPO" /></Cells></Row><Row i="1"><Cell'
    's><Cell Value="0" Text="control@RowFileEmpty" /><Cell Value="0" Text="co'
    'ntrol@RowNoteEmpty" /><Cell Value="2" /><Cell Value="PENDUA" /><Cell Val'
    'ue="" /><Cell Value="False" /><Cell Value="COPYPO" /></Cells></Row><Row '
    'i="2"><Cells><Cell Value="0" Text="control@RowFileEmpty" /><Cell Value="'
    '0" Text="control@RowNoteEmpty" /><Cell Value="3" /><Cell Value="PENIGA" '
    '/><Cell Value="" /><Cell Value="False" /><Cell Value="COPYPO" /></Cells>'
    '</Row></Rows></ctl00_phG_grid>]]></ctl00_phG_grid></ctl00_phG_grid>'
)


def test_grid_column_slots_preserves_framework_positions():
    slots = _grid_column_slots(_CS205000_REFRESH_BODY)
    # the two bare {} entries MUST survive as None — they occupy real <Cell>
    # positions, and collapsing them shifts every field one place left
    assert slots == [None, None, "ValueID", "Description", "SortOrder",
                     "Disabled", "AttributeID"]


def test_grid_rows_parses_every_row_aligned_to_fields():
    rows = _grid_rows(_CS205000_REFRESH_BODY, "ctl00_phG_grid")
    assert len(rows) == 3
    assert rows[0] == {"ValueID": "1", "Description": "ASAL", "SortOrder": "",
                       "Disabled": "False", "AttributeID": "COPYPO"}
    # a visible:0 column is still a real, readable cell
    assert [r["Description"] for r in rows] == ["ASAL", "PENDUA", "PENIGA"]


def test_grid_rows_empty_when_no_columns():
    assert _grid_rows("0|<ctl00_phG_grid/>", "ctl00_phG_grid") == []


def test_row_matches_compares_as_strings():
    row = {"EmployeeBankDetailID": "14551", "Percent": "50"}
    # run_dac_odata hands back ints; the grid echoes text
    assert _row_matches(row, {"EmployeeBankDetailID": 14551})
    assert not _row_matches(row, {"EmployeeBankDetailID": 14550})
    # a missing key must never match (sentinel, not None-equals-None)
    assert not _row_matches(row, {"Nope": ""})


def _cs_diag(save_body: str, refresh_body: str = _CS205000_REFRESH_BODY,
             after_body: str | None = None):
    """CS205000 stub: Refresh -> Save -> (verification Refresh)."""
    d = _diag_with_html(
        'var _ctl00_phG_grid = {"dataMember":"AttributeDetails",'
        '"levels":[{"columns":[]}]};')
    bodies = iter([refresh_body, save_body,
                   after_body if after_body is not None else refresh_body])

    async def fake_callback(cbid, cbparam, **kw):
        return next(bodies)

    d._callback = fake_callback  # type: ignore[method-assign]
    return d


_CLEAN_SAVE = ('0|<ctl00_phDS_ds><![CDATA[<ctl00_phDS_ds Props="'
               '{&quot;isDirty&quot;:0}"/>]]></ctl00_phDS_ds>')


# ---- #2 partial-key pre-flight ---------------------------------------------

def test_delete_refuses_key_matching_no_row():
    d = _cs_diag(_CLEAN_SAVE)
    r = asyncio.run(d.replay_grid_save(
        "AttributeDetails", {}, row_key={"AttributeID": "COPYPO",
                                         "ValueID": "NOPE"},
        operation="delete"))
    assert "matches NO row" in r["refused"]
    assert r["possibly_saved"] is False
    assert len(r["grid_rows"]) == 3


def test_delete_refuses_partial_key_matching_many_rows():
    # THE live footgun: AttributeID alone matches all 3 rows. Previously this
    # was sent and silently no-op'd with possibly_saved:true.
    d = _cs_diag(_CLEAN_SAVE)
    r = asyncio.run(d.replay_grid_save(
        "AttributeDetails", {}, row_key={"AttributeID": "COPYPO"},
        operation="delete"))
    assert "PARTIAL key" in r["refused"]
    assert len(r["matched_rows"]) == 3
    assert r["possibly_saved"] is False


def test_delete_with_full_key_is_not_refused():
    d = _cs_diag(_CLEAN_SAVE)
    r = asyncio.run(d.replay_grid_save(
        "AttributeDetails", {}, row_key={"AttributeID": "COPYPO",
                                         "ValueID": "2"},
        operation="delete"))
    assert "refused" not in r


# ---- #1 post-save read-back -------------------------------------------------

_AFTER_DELETE = _CS205000_REFRESH_BODY.replace(
    '<Row i="1"><Cells><Cell Value="0" Text="control@RowFileEmpty" />'
    '<Cell Value="0" Text="control@RowNoteEmpty" /><Cell Value="2" />'
    '<Cell Value="PENDUA" /><Cell Value="" /><Cell Value="False" />'
    '<Cell Value="COPYPO" /></Cells></Row>', '')


def test_delete_verified_true_when_row_gone_on_reread():
    d = _cs_diag(_CLEAN_SAVE, after_body=_AFTER_DELETE)
    r = asyncio.run(d.replay_grid_save(
        "AttributeDetails", {}, row_key={"AttributeID": "COPYPO",
                                         "ValueID": "2"},
        operation="delete"))
    assert r["possibly_saved"] is True
    assert r["delete_verified"] is True and r["save_verified"] is True
    assert r["rows_before"] == 3 and r["rows_after"] == 2


def test_delete_verified_false_catches_silent_noop():
    # clean Save, row STILL THERE on re-read: the exact silent no-op that
    # possibly_saved could never distinguish from success.
    d = _cs_diag(_CLEAN_SAVE)  # after == before
    r = asyncio.run(d.replay_grid_save(
        "AttributeDetails", {}, row_key={"AttributeID": "COPYPO",
                                         "ValueID": "2"},
        operation="delete"))
    assert r["possibly_saved"] is True          # the old, ambiguous signal
    assert r["delete_verified"] is False        # the new, decisive one
    assert "SILENT NO-OP" in r["verify_note"]


def test_update_verified_false_when_cell_unchanged():
    d = _cs_diag(_CLEAN_SAVE)
    r = asyncio.run(d.replay_grid_save(
        "AttributeDetails", {"Description": "CHANGED"},
        row_key={"AttributeID": "COPYPO", "ValueID": "2"}))
    assert r["save_verified"] is False
    assert "SILENT NO-OP" in r["verify_note"]


def test_update_verified_true_when_cell_changed():
    after = _CS205000_REFRESH_BODY.replace('Value="PENDUA"', 'Value="CHANGED"')
    d = _cs_diag(_CLEAN_SAVE, after_body=after)
    r = asyncio.run(d.replay_grid_save(
        "AttributeDetails", {"Description": "CHANGED"},
        row_key={"AttributeID": "COPYPO", "ValueID": "2"}))
    assert r["save_verified"] is True
    assert "now hold the values sent" in r["verify_note"]


def test_insert_verified_by_row_count_growth():
    after = _CS205000_REFRESH_BODY.replace(
        '</Rows>',
        '<Row i="3"><Cells><Cell Value="0" /><Cell Value="0" />'
        '<Cell Value="4" /><Cell Value="NEW" /><Cell Value="" />'
        '<Cell Value="False" /><Cell Value="COPYPO" /></Cells></Row></Rows>')
    d = _cs_diag(_CLEAN_SAVE, after_body=after)
    r = asyncio.run(d.replay_grid_save(
        "AttributeDetails", {"ValueID": "4", "Description": "NEW"},
        operation="insert"))
    assert r["save_verified"] is True
    assert r["rows_before"] == 3 and r["rows_after"] == 4


def test_insert_noop_flagged_when_count_static():
    d = _cs_diag(_CLEAN_SAVE)
    r = asyncio.run(d.replay_grid_save(
        "AttributeDetails", {"ValueID": "4", "Description": "NEW"},
        operation="insert"))
    assert r["save_verified"] is False
    assert "OVERWRITTEN" in r["verify_note"]


def test_verification_skipped_when_save_errored():
    # a Save that reported a real error is not ambiguous; no read-back needed
    err = ('0|<ctl00_phDS_ds><![CDATA[<ctl00_phDS_ds Props="'
           '{&quot;alert&quot;:&quot;Boom&quot;,&quot;isDirty&quot;:1}"/>]]>'
           '</ctl00_phDS_ds>')
    d = _cs_diag(err)
    r = asyncio.run(d.replay_grid_save(
        "AttributeDetails", {}, row_key={"AttributeID": "COPYPO",
                                         "ValueID": "2"},
        operation="delete"))
    assert r["alert"] == "Boom"
    assert "save_verified" not in r


# ---- #3 insert row index: REFUTED as a collision cause ----------------------

def test_insert_error_note_does_not_blame_the_row_index():
    """The row index is a BATCH ORDINAL, not a row locator — proven live on
    CS205000 (insert at i="99" into a 2-row grid appended cleanly, both rows
    intact). The note must warn about the unexplained PY309000 nondeterminism
    WITHOUT reasserting the refuted collision mechanism."""
    err = ('0|<ctl00_phDS_ds><![CDATA[<ctl00_phDS_ds Props="'
           '{&quot;alert&quot;:&quot;Boom&quot;,&quot;isDirty&quot;:1}"/>]]>'
           '</ctl00_phDS_ds>')
    d = _cs_diag(err)
    r = asyncio.run(d.replay_grid_save(
        "AttributeDetails", {"ValueID": "X"}, operation="insert"))
    note = r["note"]
    assert "RULED OUT" in note
    assert "collide" not in note.lower()
    assert "run_dac_odata" in note


def test_preflight_does_not_catch_a_grid_unique_partial_key():
    """MEASURED GAP, pinned deliberately (live CS205000, 2026-07-20).

    {"ValueID": "BBB"} matches exactly ONE grid row, so the pre-flight passes
    it — but the server matches on the FULL key and silently no-ops
    (possibly_saved:true, rows 3 -> 3, nothing deleted). The pre-flight cannot
    close this: the grid payload carries no "is key" flag. The post-Save
    read-back is the check that catches it, so assert BOTH halves: not refused,
    and caught afterwards.
    """
    d = _cs_diag(_CLEAN_SAVE)  # after == before: the row survives
    r = asyncio.run(d.replay_grid_save(
        "AttributeDetails", {}, row_key={"ValueID": "2"}, operation="delete"))
    assert "refused" not in r          # the gap: pre-flight lets it through
    assert r["possibly_saved"] is True  # and the plane reports it clean
    assert r["delete_verified"] is False        # only the read-back catches it
    assert "SILENT NO-OP" in r["verify_note"]
