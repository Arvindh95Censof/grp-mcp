"""Unit tests for the classic-ASPX diagnostic plane (aspx.py) — pure logic,
no live instance. Fixtures mirror payloads captured live on csmdev PY309000
(2026-07-17), including the exact discovery bug that made the first Save
silently no-op (RowChanges addressed to the wrong grid control), and on
GL301000 (2026-07-18), where the classic page's Save callback ITSELF crashes
server-side (a raw, unwrapped NullReferenceException, not a validation error)."""

from __future__ import annotations

import asyncio

from grp_mcp.aspx import (AspxDiagnostic, _grid_errors, _parse_control_blocks,
                          _parse_hidden_inputs, _xml_attr_escape)


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
