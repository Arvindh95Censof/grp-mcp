"""Smoke tests — pure logic, no live Acumatica instance required.

Covers the bits most worth guarding against regression: config/gating model,
the write/delete/publish gates, the value-wrapper, and the modern UI-screen
error parser. Run with:  python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import json

import pytest

from grp_mcp import server
from grp_mcp.acumatica import AcumaticaClient, AcumaticaError
from grp_mcp.config import Config, Instance
from grp_mcp.screen import ScreenClient, ScreenError


def _inst(**over) -> Instance:
    base = dict(
        base_url="https://host/Site",
        client_id="cid", client_secret="sek",
        username="u", password="p", tenant="Company",
    )
    base.update(over)
    return Instance(**base)


@pytest.fixture
def cfg(monkeypatch):
    """Inject a 2-instance config: 'ro' (all gates off) and 'rw' (all on)."""
    c = Config(
        default="ro",
        instances={
            "ro": _inst(),
            "rw": _inst(allow_write=True, allow_delete=True, allow_publish=True),
        },
    )
    monkeypatch.setattr(server, "_config", c)
    return c


# ---- Instance model / properties -------------------------------------------

def test_gate_defaults_are_read_only():
    i = _inst()
    assert i.allow_write is False
    assert i.allow_delete is False
    assert i.allow_publish is False


def test_origin_is_scheme_host_only():
    assert _inst(base_url="https://Host:8080/Site/x").origin == "https://host:8080"


def test_token_and_odata_urls():
    i = _inst(base_url="https://h/Site/")
    assert i.token_url == "https://h/Site/identity/connect/token"
    assert i.dac_odata_base == "https://h/Site/t/Company/api/odata/dac"


def test_config_get_unknown_raises():
    c = Config(default="a", instances={"a": _inst()})
    with pytest.raises(KeyError):
        c.get("nope")


# ---- write / delete / publish gates ----------------------------------------

def test_require_write_blocks_read_only(cfg):
    with pytest.raises(PermissionError):
        server._require_write("ro")
    server._require_write("rw")  # no raise


def test_require_delete_blocks_read_only(cfg):
    with pytest.raises(PermissionError):
        server._require_delete("ro")
    server._require_delete("rw")


def test_require_publish_blocks_read_only(cfg):
    with pytest.raises(PermissionError):
        server._require_publish("ro")
    server._require_publish("rw")


def test_delete_gate_is_independent_of_write(monkeypatch):
    # write ON, delete OFF -> deletes must still be blocked
    c = Config(default="d", instances={"d": _inst(allow_write=True, allow_delete=False)})
    monkeypatch.setattr(server, "_config", c)
    server._require_write("d")  # ok
    with pytest.raises(PermissionError):
        server._require_delete("d")


def test_destructive_actions_include_delete():
    assert "Delete" in server._DESTRUCTIVE_ACTIONS
    assert "DeleteRow" in server._DESTRUCTIVE_ACTIONS


# ---- value wrapper ----------------------------------------------------------

def test_wrap_scalar():
    assert server._wrap("x") == {"value": "x"}


def test_wrap_passthrough_wrapped_scalar():
    assert server._wrap({"value": 5}) == {"value": 5}


def test_wrap_nested_and_id_delete_passthrough():
    out = server._wrap({"id": "R1", "delete": True, "Name": "Acme",
                        "Addr": {"City": "KL"}})
    assert out["id"] == "R1"            # not wrapped
    assert out["delete"] is True        # bare bool, not {"value": True}
    assert out["Name"] == {"value": "Acme"}
    assert out["Addr"] == {"City": {"value": "KL"}}


def test_wrap_list_of_rows():
    assert server._wrap([{"A": 1}]) == [{"A": {"value": 1}}]


# ---- modern UI-screen error parser -----------------------------------------

class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if isinstance(body, (dict, list)) else str(body)

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("not json")


def test_ui_error_success_empty_messages():
    assert ScreenClient._ui_error(_Resp(200, {"messages": []})) is None


def test_ui_error_200_explicit_error_message():
    r = _Resp(200, {"messages": [{"message": "boom", "messageType": "error"}]})
    assert ScreenClient._ui_error(r) == "boom"


def test_ui_error_200_info_message_is_not_error():
    r = _Resp(200, {"messages": [{"message": "fyi", "messageType": "info"}]})
    assert ScreenClient._ui_error(r) is None


def test_ui_error_200_untyped_message_is_not_error():
    r = _Resp(200, {"messages": [{"message": "no type"}]})
    assert ScreenClient._ui_error(r) is None


def test_ui_error_409_surfaces_all_messages():
    r = _Resp(409, {"messages": [{"message": "bad year", "messageType": "error"}]})
    assert ScreenClient._ui_error(r) == "bad year"


def test_ui_error_setup_not_entered():
    msg = ScreenClient._ui_error(_Resp(409, {"type": "SetupNotEntered"}))
    assert "PREREQUISITE NOT MET" in msg


def test_ui_error_500_title_detail():
    assert ScreenClient._ui_error(_Resp(500, {"title": "x", "detail": "y"})) == "Error: y"


def test_ui_error_login_redirect_200_is_auth_error():
    # an unauthenticated/expired modern-plane session answers 200 with a Login
    # redirect body — must surface as a clear auth error, not silent None.
    r = _Resp(200, {"redirect": "/2026R1/Frames/Login.aspx?ReturnUrl=%2f..."})
    msg = ScreenClient._ui_error(r)
    assert msg is not None and "NOT AUTHENTICATED" in msg


def test_ui_error_login_redirect_302_is_auth_error():
    r = _Resp(302, {"redirect": "/2026R1/Frames/Login.aspx"})
    assert "NOT AUTHENTICATED" in ScreenClient._ui_error(r)


def test_ui_error_non_login_redirect_not_flagged_as_auth():
    # a goTo/other redirect (e.g. post-restore hand-off to SM203510) is NOT an
    # auth failure — the Login-specific guard must not swallow it as one.
    r = _Resp(200, {"redirect": "/Scripts/Screens/SalesDemo/SM203510.html"})
    assert ScreenClient._ui_error(r) is None


# ---- modern-plane GRID CRUD payload shapes ---------------------------------
#
# These lock the exact JSON the grid write path emits. The bugs they guard
# against are the silent no-persist / wrong-row traps that a clean HTTP 200
# hides: dropping the KEY from `values` (→ blank-row insert), dropping the
# `columns` echo (→ 200 no-op), a wrong dataKey/changeType, or (master-detail)
# failing to navigate the master or scope viewsParams to the parent view.

class _FakeHTTP:
    """Records outgoing payloads; serves `read_body` to reads, OK to Saves."""

    def __init__(self, read_body: dict):
        self.calls: list[dict] = []
        self._read = read_body

    async def post(self, url, json=None, headers=None):  # noqa: A002
        self.calls.append(json or {})
        if (json or {}).get("command"):
            return _Resp(200, {"messages": []})
        return _Resp(200, self._read)

    async def aclose(self):
        pass


def _client(screen: str = "GL202500") -> ScreenClient:
    s = ScreenClient(_inst(), screen)
    # these tests unit-test payload SHAPING with a fake _http; mark the session
    # authenticated so the self-heal login() guard (real network) stays a no-op.
    s._logged_in = True
    return s


def _read_body(grid: str, columns, rows, key_names, qff=None) -> dict:
    return {"controlsData": {grid: {"columns": columns, "rows": rows,
            "dataKeyNames": key_names, "quickFilterFields": qff or []}}}


def _save(fake: _FakeHTTP) -> dict:
    saves = [c for c in fake.calls if c.get("command")]
    assert saves, "no Save POST was emitted"
    return saves[-1]


# -- pure builders --

def test_kv_keeps_bool_strings_the_rest():
    d = {x["field"]: x["value"] for x in ScreenClient._kv({"a": True, "b": 5, "c": "x"})}
    assert d["a"] is True and d["b"] == "5" and d["c"] == "x"


def test_cell_key_unwraps_lookup_value():
    assert ScreenClient._cell_key({"cells": {"k": {"value": {"id": "X", "text": "t"}}}}, "k") == "X"
    assert ScreenClient._cell_key({"cells": {"k": {"value": "Y"}}}, "k") == "Y"


def test_full_key_builds_composite_incl_parent_id():
    s = _client()
    row = {"cells": {"CashAccountID": {"value": 994},
                     "EntryTypeID": {"value": {"id": "BANKCHG"}}}}
    assert s._full_key(row, ["CashAccountID", "EntryTypeID"]) == {
        "CashAccountID": 994, "EntryTypeID": "BANKCHG"}


def test_grid_ctrl_echoes_columns_and_gates_datakey():
    s = _client()
    cols = [{"field": "A"}]
    g = {"columns": cols, "rows": [{}], "quick_filter_fields": ["A"]}
    ins = s._grid_ctrl("G", g, {"inserted": []}, None)
    assert ins["columns"] is cols and ins["resultType"] == "GridData" and "pageSize" in ins
    assert "dataKey" not in ins                       # insert: no dataKey
    upd = s._grid_ctrl("G", g, {"modified": []}, {"A": "1"})
    assert upd["dataKey"] == {"A": "1"}               # update/delete: dataKey set


# -- top-level insert/update/delete payloads --

def test_insert_payload_has_key_columns_id_no_datakey():
    s = _client("GL202500")
    s._http = _FakeHTTP(_read_body(
        "AccountRecords", [{"field": "AccountCD"}, {"field": "Description"}],
        [{"id": "g1", "cells": {"AccountCD": {"value": "10100"}}}], ["AccountCD"]))
    asyncio.run(s.ui_insert_grid_row(
        "AccountRecords", {"AccountCD": "40100", "Type": "I", "Description": "X"}))
    ctrl = _save(s._http)["controlsParams"]["AccountRecords"]
    ins = ctrl["changes"]["inserted"][0]
    assert "AccountCD" in {v["field"] for v in ins["values"]}   # key present (else blank-row insert)
    assert ctrl["columns"] == [{"field": "AccountCD"}, {"field": "Description"}]  # echoed (else no-op)
    assert "dataKey" not in ctrl and ins.get("id") and ins["index"] == 1
    assert _save(s._http)["activeRowContexts"] == []


def test_update_payload_resends_key_in_values():
    s = _client("GL202500")
    s._http = _FakeHTTP(_read_body(
        "AccountRecords", [{"field": "AccountCD"}, {"field": "Description"}],
        [{"id": "g8", "cells": {"AccountCD": {"value": "40000"},
                                 "Description": {"value": "Sales"}}}], ["AccountCD"]))
    asyncio.run(s.ui_update_grid_row("AccountRecords", {"AccountCD": "40000"},
                                     {"Description": "New"}))
    ctrl = _save(s._http)["controlsParams"]["AccountRecords"]
    mod = ctrl["changes"]["modified"][0]
    fields = {v["field"]: v["value"] for v in mod["values"]}
    assert fields.get("AccountCD") == "40000" and fields.get("Description") == "New"
    assert ctrl["dataKey"] == {"AccountCD": "40000"} and mod["id"] == "g8"


def test_delete_payload_sends_key_in_values():
    s = _client("GL202500")
    s._http = _FakeHTTP(_read_body(
        "AccountRecords", [{"field": "AccountCD"}],
        [{"id": "g9", "cells": {"AccountCD": {"value": "40100"}}}], ["AccountCD"]))
    asyncio.run(s.ui_delete_grid_row("AccountRecords", {"AccountCD": "40100"}))
    ctrl = _save(s._http)["controlsParams"]["AccountRecords"]
    dl = ctrl["changes"]["deleted"][0]
    assert "AccountCD" in {v["field"] for v in dl["values"]}
    assert ctrl["dataKey"] == {"AccountCD": "40100"}


# -- master-detail payloads --

def test_md_insert_navigates_master_and_scopes_viewsparams():
    s = _client("CA202000")
    s._http = _FakeHTTP(_read_body(
        "ETDetails", [{"field": "EntryTypeID"}, {"field": "CashAccountID"}],
        [], ["CashAccountID", "EntryTypeID"]))
    parent = {"view": "CashAccount", "key": {"CashAccountCD": "10200"}}
    asyncio.run(s.ui_insert_grid_row("ETDetails", {"EntryTypeID": "BANKCHG"}, parent))
    nav = [c for c in s._http.calls
           if any(d.get("changeType") == 5 for d in (c.get("data") or []))]
    assert nav, "master was not navigated"
    d = nav[0]["data"][0]
    assert (d["viewName"], d["fieldName"], d["value"]) == ("CashAccount", "CashAccountCD", "10200")
    save = _save(s._http)
    assert "CashAccount" in save["viewsParams"] and "ETDetails" in save["viewsParams"]
    ins = save["controlsParams"]["ETDetails"]["changes"]["inserted"][0]
    assert {v["field"] for v in ins["values"]} == {"EntryTypeID"}  # parent id auto-filled server-side
    assert save["activeRowContexts"] == []


def test_md_delete_resolves_full_composite_key_from_lookup():
    s = _client("CA202000")
    s._http = _FakeHTTP(_read_body(
        "ETDetails", [{"field": "EntryTypeID"}, {"field": "CashAccountID"}],
        [{"id": "994;BANKCHG", "cells": {"CashAccountID": {"value": 994},
          "EntryTypeID": {"value": {"id": "BANKCHG", "text": "BANKCHG"}}}}],
        ["CashAccountID", "EntryTypeID"]))
    parent = {"view": "CashAccount", "key": {"CashAccountCD": "10200"}}
    asyncio.run(s.ui_delete_grid_row("ETDetails", {"EntryTypeID": "BANKCHG"}, parent))
    ctrl = _save(s._http)["controlsParams"]["ETDetails"]
    dl = ctrl["changes"]["deleted"][0]
    assert {v["field"]: v["value"] for v in dl["values"]} == {
        "CashAccountID": "994", "EntryTypeID": "BANKCHG"}   # full key, lookup unwrapped to id
    assert ctrl["dataKey"] == {"CashAccountID": 994, "EntryTypeID": "BANKCHG"}
    assert dl["id"] == "994;BANKCHG"


# -- the classic positional-row footgun stays disabled --

def test_positional_row_spec_hard_errors():
    from grp_mcp.screen import ScreenError
    s = _client("GL202500")
    with pytest.raises(ScreenError):
        s._spec_to_command({"row": "AccountRecords", "to": 8})


# ---- ui.py CSRF guard (_is_same_origin) -------------------------------------
# A page open in any OTHER tab/site can still fire a blind cross-origin POST at
# the config UI while it happens to be running (CORS blocks it from reading the
# response, not from sending the request) — do_POST must reject anything that
# doesn't prove same-origin via Origin (or, failing that, Referer).

def test_same_origin_exact_origin_match():
    from grp_mcp.ui import _is_same_origin
    assert _is_same_origin({"Origin": "http://127.0.0.1:8765"}, "http://127.0.0.1:8765")


def test_same_origin_rejects_cross_origin():
    from grp_mcp.ui import _is_same_origin
    assert not _is_same_origin({"Origin": "https://evil.com"}, "http://127.0.0.1:8765")


# ---- TREE node selection (ui_select_tree_node) ------------------------------
# A tree control (e.g. SM207060's EntityTree) isn't a normal data grid — it needs
# its own activeRowContexts/controlsParams/viewsParams shape. These three pure
# builders were derived by bisecting a live browser capture; lock the exact shape
# down since a silently-wrong field here produces a 200 with no error, not a
# visible failure (proven live, 2026-07-02).

def test_tree_active_row_context_root_node():
    from grp_mcp.screen import _tree_active_row_context
    ctx = _tree_active_row_context("EntityTree", {"Key": "ROOT#GRPMCP"}, None)
    assert ctx == {
        "dataView": "EntityTree", "syncPosition": True,
        "dataKey": {"Key": "ROOT#GRPMCP"},
        "selectedNodeParentId": None, "resultType": "TreeActiveDataRow",
    }


def test_tree_active_row_context_child_node():
    from grp_mcp.screen import _tree_active_row_context
    ctx = _tree_active_row_context(
        "EntityTree", {"Key": "ENT#Companies"}, [{"Key": "ROOT#GRPMCP"}])
    assert ctx["selectedNodeParentId"] == "ROOT#GRPMCP"


def test_tree_active_row_context_detail_node_uses_immediate_parent():
    from grp_mcp.screen import _tree_active_row_context
    # depth-2 detail node: ancestors root -> entity; parent is the entity
    ctx = _tree_active_row_context(
        "EntityTree", {"Key": "GRPMCP/25.200.001#E/2690/1/2691"},
        [{"Key": "ROOT#GRPMCP"}, {"Key": "GRPMCP/25.200.001#E/2690"}])
    assert ctx["selectedNodeParentId"] == "GRPMCP/25.200.001#E/2690"


def test_tree_control_block_root_parameters():
    from grp_mcp.screen import _tree_control_block
    block = _tree_control_block(
        "EntityTree", {"Key": "ROOT#GRPMCP"}, None,
        columns=["Key", "Title", "Icon", "IconColor"], key_fields=["Key"])
    assert block["columns"] == ["Key", "Title", "Icon", "IconColor"]
    assert block["treeKeys"] == ["Key"]
    assert block["parameters"] == ["ROOT#GRPMCP", None, "ROOT#GRPMCP"]
    assert block["selectedNodeParentId"] is None


def test_tree_control_block_entity_parameters_full_path():
    from grp_mcp.screen import _tree_control_block
    # depth-1 entity: [root, None, entity] (matches the live browser payload)
    block = _tree_control_block(
        "EntityTree", {"Key": "GRPMCP/25.200.001#E/2690"}, [{"Key": "ROOT#GRPMCP"}],
        columns=["Key"], key_fields=["Key"])
    assert block["parameters"] == ["ROOT#GRPMCP", None, "GRPMCP/25.200.001#E/2690"]
    assert block["selectedNodeParentId"] == "ROOT#GRPMCP"


def test_tree_control_block_detail_parameters_full_path():
    from grp_mcp.screen import _tree_control_block
    # depth-2 detail: [root, entity, None, detail] — the whole chain, not just parent
    block = _tree_control_block(
        "EntityTree", {"Key": "GRPMCP/25.200.001#E/2690/1/2691"},
        [{"Key": "ROOT#GRPMCP"}, {"Key": "GRPMCP/25.200.001#E/2690"}],
        columns=["Key"], key_fields=["Key"])
    assert block["parameters"] == [
        "ROOT#GRPMCP", "GRPMCP/25.200.001#E/2690", None,
        "GRPMCP/25.200.001#E/2690/1/2691"]
    assert block["selectedNodeParentId"] == "GRPMCP/25.200.001#E/2690"


def test_tree_control_block_falls_back_to_key_field_when_metadata_missing():
    from grp_mcp.screen import _tree_control_block
    block = _tree_control_block(
        "EntityTree", {"Key": "ROOT#GRPMCP"}, None, columns=None, key_fields=None)
    assert block["columns"] == []
    assert block["treeKeys"] == ["Key"]


def test_tree_context_views_picks_selected_star_only():
    from grp_mcp.screen import _tree_context_views
    views = ["Endpoint", "SelectedEndpoint", "SelectedEntity", "SelectedAction",
             "EntityTree", "CreateEntityView"]
    ctx_views = _tree_context_views(views, "EntityTree", {"Key": "ROOT#GRPMCP"})
    assert ctx_views == {
        "SelectedEndpoint": {"parameters": {"Key": "ROOT#GRPMCP"}},
        "SelectedEntity": {"parameters": {"Key": "ROOT#GRPMCP"}},
        "SelectedAction": {"parameters": {"Key": "ROOT#GRPMCP"}},
    }


def test_tree_context_views_excludes_the_tree_itself():
    from grp_mcp.screen import _tree_context_views
    # a pathological tree view name starting with "Selected" must not self-select
    ctx_views = _tree_context_views(
        ["SelectedTree"], "SelectedTree", {"Key": "X"})
    assert ctx_views == {}


def test_ui_post_auto_attaches_active_tree_row_and_controls():
    """_ui_post must merge the cached tree selection into every later call unless
    the caller already named that dataView/view — this is what lets InsertNew
    (fired via plain ui_command) see the node ui_select_tree_node selected."""
    s = _client("SM207060")
    s._ui_booted = True  # skip network bootstrap
    ctx = {"dataView": "EntityTree", "syncPosition": True,
           "dataKey": {"Key": "ROOT#GRPMCP"}, "selectedNodeParentId": None,
           "resultType": "TreeActiveDataRow"}
    block = {"view": "EntityTree", "dataKey": {"Key": "ROOT#GRPMCP"}}
    s._active_tree_row = ctx
    s._active_tree_controls = {"EntityTree": block}

    captured = {}

    async def fake_post(url, json, headers):
        captured.update(json)
        return _Resp(200, {})

    s._http.post = fake_post
    asyncio.run(s._ui_post({"command": [{"name": "InsertNew"}], "data": [],
                             "controlsParams": {}, "activeRowContexts": [],
                             "viewsParams": {}}))
    assert captured["activeRowContexts"] == [ctx]
    assert captured["controlsParams"]["EntityTree"] == block


def test_ui_post_does_not_override_caller_supplied_tree_context():
    """If the caller already names the SAME dataView explicitly, don't stomp it."""
    s = _client("SM207060")
    s._ui_booted = True
    s._active_tree_row = {"dataView": "EntityTree", "dataKey": {"Key": "ROOT#GRPMCP"}}
    s._active_tree_controls = {"EntityTree": {"view": "EntityTree"}}

    captured = {}

    async def fake_post(url, json, headers):
        captured.update(json)
        return _Resp(200, {})

    s._http.post = fake_post
    caller_ctx = [{"dataView": "EntityTree", "dataKey": {"Key": "ENT#Companies"}}]
    asyncio.run(s._ui_post({"activeRowContexts": caller_ctx, "controlsParams": {}}))
    assert captured["activeRowContexts"] == caller_ctx


def test_ui_post_auto_attaches_tree_context_views():
    """The Selected* context viewsParams must ride on every later command too — this
    is what lets a dialog commit STAGE the node (without them the trailing Save
    persists an empty graph). Proven load-bearing live (SM207060, 2026-07-02)."""
    s = _client("SM207060")
    s._ui_booted = True
    s._active_tree_context_views = {
        "SelectedEndpoint": {"parameters": {"Key": "ROOT#GRPMCP"}},
        "SelectedEntity": {"parameters": {"Key": "ROOT#GRPMCP"}},
    }
    captured = {}

    async def fake_post(url, json, headers):
        captured.update(json)
        return _Resp(200, {})

    s._http.post = fake_post
    asyncio.run(s._ui_post({"command": [{"name": "Save"}], "viewsParams": {}}))
    assert captured["viewsParams"]["SelectedEndpoint"] == {"parameters": {"Key": "ROOT#GRPMCP"}}
    assert captured["viewsParams"]["SelectedEntity"] == {"parameters": {"Key": "ROOT#GRPMCP"}}


# ---- selector (lookup) field resolution (ui_resolve_selector) ---------------
# A selector field's /structure fieldState carries everything to query its own
# grid sub-endpoint — no browser capture per field. Lock the extraction + payload.

def test_selector_meta_extracts_graph_from_field_dac_name():
    from grp_mcp.screen import _selector_meta
    # the real SM207060 CreateEntityView.ScreenID shape (trimmed)
    st = {"selectorMode": 33, "viewName": "_EntityDescriptionInsertModelScreenID_X",
          "fieldDacName": "PX.Api.ContractBased.UI.EntityConfigurationMaint+EntityDescriptionInsertModel",
          "valueField": "screenID", "descriptionName": "title",
          "fieldList": ["title", "screenID"], "headerList": ["Title", "Screen ID"]}
    sel = _selector_meta(st)
    assert sel["graph"] == "PX.Api.ContractBased.UI.EntityConfigurationMaint"
    assert sel["value_field"] == "screenID"
    assert sel["search_field"] == "title"
    assert sel["columns"] == ["title", "screenID"]


def test_selector_meta_none_for_non_selector():
    from grp_mcp.screen import _selector_meta
    assert _selector_meta({"typeName": "String"}) is None
    assert _selector_meta({"selectorMode": 0}) is None


def test_selector_grid_payload_shape():
    from grp_mcp.screen import _selector_grid_payload
    sel = {"view": "_V", "graph": "PX.Foo", "value_field": "screenID",
           "search_field": "title", "columns": ["title", "screenID"], "headers": []}
    p = _selector_grid_payload(sel, "ScreenID", "CreateEntityView", "Companies")
    assert p["view"] == "_V"
    assert p["graph"] == "PX.Foo"
    assert p["dataField"] == "ScreenID"
    assert p["dataView"] == "CreateEntityView"
    assert p["fastFilter"] == "Companies"
    assert p["searchField"] == "title"
    assert p["columns"] == [{"field": "title"}, {"field": "screenID"}]
    assert "activeRowContexts" not in p  # omitted when none passed


def test_selector_grid_payload_includes_active_row_contexts_when_given():
    from grp_mcp.screen import _selector_grid_payload
    sel = {"view": "_V", "graph": "g", "value_field": "v", "search_field": "s",
           "columns": ["s"], "headers": []}
    arc = [{"dataView": "EntityTree", "dataKey": {"Key": "ROOT#X"}}]
    p = _selector_grid_payload(sel, "F", "DV", "q", arc)
    assert p["activeRowContexts"] == arc


def test_selector_meta_falls_back_columns_when_field_list_absent():
    """Some selectors (SM207060 PopulateFilterView.Container) omit fieldList AND
    fieldDacName — columns must fall back to [valueField, descriptionName], else the
    grid query returns unfiltered/unreadable rows (proven live: 8 empty {} rows)."""
    from grp_mcp.screen import _selector_meta
    st = {"selectorMode": 16, "viewName": "_PopulateFilterContainer_X",
          "valueField": "mappedObject", "descriptionName": "displayName"}
    sel = _selector_meta(st)
    assert sel["graph"] is None                       # no fieldDacName -> caller fills it
    assert sel["columns"] == ["mappedObject", "displayName"]
    assert sel["value_field"] == "mappedObject"
    assert sel["search_field"] == "displayName"


def test_tree_row_by_title_matches_and_strips_inheritance_marker():
    s = _client("SM207060")
    resp = {"controlsData": {"EntityTree": {"rows": [
        {"cells": {"Title": {"value": "Endpoint"}, "Key": {"value": "ROOT#GRPMCP"}}},
        {"cells": {"Title": {"value": "DataProvider"}, "Key": {"value": "GRPMCP/25.200.001#E/2692"}}},
        {"cells": {"Title": {"value": "Account ↓"}, "Key": {"value": "Default/24.200.001#E/12887"}}},
    ]}}}
    assert s._tree_row_by_title(resp, "EntityTree", "DataProvider") == {"Key": "GRPMCP/25.200.001#E/2692"}
    # inherited node ('Account ↓') matched by its bare title
    assert s._tree_row_by_title(resp, "EntityTree", "Account") == {"Key": "Default/24.200.001#E/12887"}
    assert s._tree_row_by_title(resp, "EntityTree", "Nonexistent") is None


def test_same_origin_falls_back_to_referer_when_origin_absent():
    from grp_mcp.ui import _is_same_origin
    assert _is_same_origin(
        {"Referer": "http://127.0.0.1:8765/index.html"}, "http://127.0.0.1:8765"
    )


def test_same_origin_rejects_mismatched_referer():
    from grp_mcp.ui import _is_same_origin
    assert not _is_same_origin({"Referer": "https://evil.com/"}, "http://127.0.0.1:8765")


def test_same_origin_rejects_no_headers_at_all():
    # e.g. a bare curl/script with no browser-supplied Origin or Referer — secure
    # default is to reject, not to assume good faith just because both are absent.
    from grp_mcp.ui import _is_same_origin
    assert not _is_same_origin({}, "http://127.0.0.1:8765")


# ---- snapshot_entity write_roots enforcement (default path) -----------------
# The default (no `path` given) snapshot destination used to skip _check_write_path
# entirely — only a caller-SUPPLIED path was fenced. Verify the auto-computed
# default now goes through the same check, by configuring write_roots to a
# directory that can't contain it and confirming a PermissionError fires before
# any network call (proves the check now runs first, not after the fetch).

def test_snapshot_entity_default_path_respects_write_roots(monkeypatch):
    c = Config(
        default="restricted",
        instances={"restricted": _inst(write_roots=["C:/nowhere-real-should-not-match"])},
    )
    monkeypatch.setattr(server, "_config", c)
    monkeypatch.delenv("GRP_MCP_CONNECTIONS", raising=False)
    with pytest.raises(PermissionError):
        asyncio.run(server.snapshot_entity("SomeEntity", instance="restricted"))


# ---- ui_grid_row_action: select an existing grid row, then fire an action ----
# The capability the classic SOAP plane structurally lacks (it cannot address an
# existing grid row by key). Verify the row is made active via activeRowContexts
# (GridActiveDataRow with the row key), the action fires, and a 302 openDialog is
# auto-answered with a dialogCallback OK (dialogResult:1).

def test_ui_grid_row_action_selects_row_and_answers_dialog():
    s = _client("SM203520")
    s._ui_booted = True  # skip the bootstrap network round-trip
    calls = []

    async def fake_post(url, json, headers=None):  # noqa: A002
        calls.append(json)
        # first command call -> a confirmation dialog would open (302)
        if json.get("command") and "dialogCallback" not in json:
            return _Resp(302, {"redirects": [
                {"settings": {"type": "openDialog", "viewName": "SnapshotsHistory"}}]})
        return _Resp(200, {"graphIsDirty": False, "messages": []})

    s._http.post = fake_post
    res = asyncio.run(s.ui_grid_row_action(
        "Snapshots", {"SnapshotID": "abc"}, "importSnapshotCommand"))
    fires = [c for c in calls if c.get("command")]
    # the action fired with the row active as a GridActiveDataRow
    arc = fires[0]["activeRowContexts"]
    assert arc == [{"dataView": "Snapshots", "syncPosition": True,
                    "dataKey": {"SnapshotID": "abc"}, "resultType": "GridActiveDataRow"}]
    # the 302 openDialog was answered OK on the follow-up call, row still active
    assert fires[-1]["dialogCallback"] == {
        "dialogResult": 1, "validateInput": False, "viewName": "SnapshotsHistory"}
    assert fires[-1]["activeRowContexts"] == arc
    assert res["ok"] and res["status"] == "committed"


def test_ui_grid_row_action_confirm_false_leaves_dialog_open():
    """confirm=False must NOT answer the dialog — a safe arm-without-firing for a
    destructive action."""
    s = _client("SM203520")
    s._ui_booted = True
    calls = []

    async def fake_post(url, json, headers=None):  # noqa: A002
        calls.append(json)
        if json.get("command"):
            return _Resp(302, {"redirects": [
                {"settings": {"type": "openDialog", "viewName": "SnapshotsHistory"}}]})
        return _Resp(200, {})

    s._http.post = fake_post
    res = asyncio.run(s.ui_grid_row_action(
        "Snapshots", {"SnapshotID": "abc"}, "importSnapshotCommand", confirm=False))
    assert res["status"] == "dialog_open"
    assert not any("dialogCallback" in c for c in calls)  # never committed


# ---- bearer-token URL guard: origin AND base-path scoped --------------------
# The token must never ride to a same-host but different-app URL (poll_action /
# download take caller-supplied URLs). base_url "https://host/2026R1" -> only
# paths under /2026R1 are allowed; a bare-root base falls back to origin-only.

def _acu(base_url):
    return AcumaticaClient(_inst(base_url=base_url))


def test_token_guard_allows_url_under_base_path():
    c = _acu("https://host/2026R1")
    c._assert_allowed_url("https://host/2026R1/entity/Default/24.200.001/SalesOrder")
    c._assert_allowed_url("https://host/2026R1/t/Company/api/odata/dac/Account")
    c._assert_allowed_url("https://host/2026R1")  # the prefix itself


def test_token_guard_blocks_different_origin():
    c = _acu("https://host/2026R1")
    with pytest.raises(AcumaticaError):
        c._assert_allowed_url("https://evil.example.com/2026R1/entity/x")


def test_token_guard_blocks_same_host_different_app_path():
    c = _acu("https://host/2026R1")
    with pytest.raises(AcumaticaError):
        c._assert_allowed_url("https://host/OtherApp/entity/x")
    # a path that merely shares the prefix as a substring, not a segment, is blocked
    with pytest.raises(AcumaticaError):
        c._assert_allowed_url("https://host/2026R1Evil/x")


def test_token_guard_root_hosted_base_is_origin_only():
    c = _acu("https://host")  # site at domain root -> no path to scope to
    c._assert_allowed_url("https://host/entity/Default/24.200.001/SalesOrder")
    c._assert_allowed_url("https://host/anything/at/all")
    with pytest.raises(AcumaticaError):
        c._assert_allowed_url("https://elsewhere/x")


def test_extend_endpoint_is_not_a_registered_tool():
    # deregistered on purpose: a REST PUT to WebServiceEndpoints is a verified no-op,
    # so exposing it as a tool only invited a silent do-nothing "success".
    async def _names():
        return {t.name for t in await server.mcp.list_tools()}
    names = asyncio.run(_names())
    assert "extend_endpoint" not in names
    assert "ui_tree_dialog_insert" in names  # the working replacement


# ---- admin gate: persisting config mutations need GRP_MCP_ALLOW_ADMIN --------

def test_admin_gate_blocks_persist_without_env(cfg, monkeypatch):
    monkeypatch.delenv("GRP_MCP_ALLOW_ADMIN", raising=False)
    with pytest.raises(PermissionError):
        server.set_active_instance("rw", persist=True)


def test_admin_gate_allows_session_only_switch(cfg, monkeypatch):
    monkeypatch.delenv("GRP_MCP_ALLOW_ADMIN", raising=False)
    # persist=false touches no file -> ungated
    out = server.set_active_instance("rw", persist=False)
    assert out["active"] == "rw" and out["persisted"] is False


def test_admin_gate_opens_with_env(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("GRP_MCP_ALLOW_ADMIN", "1")
    monkeypatch.setenv("GRP_MCP_CONNECTIONS", str(tmp_path / "connections.json"))
    out = server.set_active_instance("rw", persist=True)
    assert out["active"] == "rw" and out["persisted"] is True


def test_admin_gate_remove_instance_persist_blocked(cfg, monkeypatch):
    monkeypatch.delenv("GRP_MCP_ALLOW_ADMIN", raising=False)
    with pytest.raises(PermissionError):
        server.remove_instance("rw", persist=True)


# ---- fs sandbox status is honest about "empty roots = unrestricted" ----------

def test_fs_sandbox_unrestricted_when_empty():
    s = _inst().fs_sandbox("write")
    assert "UNRESTRICTED" in s and "write_roots" in s


def test_fs_sandbox_restricted_when_set():
    s = _inst(read_roots=["C:/data"]).fs_sandbox("read")
    assert "restricted to" in s and "C:/data" in s


# ---- publish job view / status (non-blocking publish) -----------------------

def test_publish_job_view_in_progress_vs_completed_vs_error():
    base = {"job": "grp_mcp", "project_names": ["grp_mcp"], "completed": False,
            "failed": None, "result": None, "error": None}
    v = server._publish_job_view(dict(base))
    assert v["status"] == "in_progress" and v["completed"] is False and v["note"]
    done = server._publish_job_view({**base, "completed": True, "failed": False,
                                     "result": {"isCompleted": True}})
    assert done["status"] == "completed" and done["note"] is None
    err = server._publish_job_view({**base, "error": "boom"})
    assert err["status"] == "error" and err["note"] is None


def test_publish_status_reads_module_state(monkeypatch):
    monkeypatch.setattr(server, "_publish_jobs", {
        "a": {"job": "a", "project_names": ["a"], "completed": True, "failed": False,
              "result": {}, "error": None},
        "b": {"job": "b", "project_names": ["b"], "completed": False, "failed": None,
              "result": None, "error": None},
    })
    # explicit job
    assert asyncio.run(server.publish_status("a"))["status"] == "completed"
    # default = most recently inserted
    assert asyncio.run(server.publish_status())["job"] == "b"
    # unknown
    assert asyncio.run(server.publish_status("zzz"))["status"] == "unknown"


# ---- v0.35: endpoint override, publish phases, timeout wrapping -------------

def test_client_endpoint_override(cfg, monkeypatch):
    monkeypatch.setattr(server, "_clients", {})
    default = server._client("ro")
    assert default.instance.endpoint_name == "Default"
    over = server._client("ro", "grp_mcp/25.200.001")
    assert over.instance.endpoint_name == "grp_mcp"
    assert over.instance.endpoint_version == "25.200.001"
    assert over.instance.entity_base.endswith("/entity/grp_mcp/25.200.001")
    # distinct cache slots; base client untouched
    assert over is not default and server._client("ro") is default
    assert server._client("ro", "grp_mcp/25.200.001") is over


def test_client_endpoint_override_bad_format(cfg, monkeypatch):
    monkeypatch.setattr(server, "_clients", {})
    with pytest.raises(ValueError):
        server._client("ro", "grp_mcp")  # missing /Version
    with pytest.raises(ValueError):
        server._client("ro", "/25.200.001")  # missing name


def test_publish_job_view_phases():
    base = {"job": "j", "project_names": ["j"], "completed": False,
            "failed": None, "result": None, "error": None}
    begin = server._publish_job_view({**base, "phase": "begin"})
    assert begin["status"] == "in_progress" and begin["phase"] == "begin"
    assert "publishBegin" in begin["note"]
    pub = server._publish_job_view({**base, "phase": "publishing"})
    assert pub["phase"] == "publishing" and "recompile" in pub["note"]
    # begin failure surfaces as error via the job (the v0.35 fix)
    err = server._publish_job_view({**base, "phase": "begin", "error": "login failed (401)"})
    assert err["status"] == "error" and err["error"].startswith("login failed")


def test_request_raw_wraps_timeout_with_explicit_message():
    import httpx

    cli = AcumaticaClient(_inst())

    async def fake_auth_header():
        return {"Authorization": "Bearer x"}

    async def boom(*a, **k):
        raise httpx.ReadTimeout("")  # httpx timeouts often stringify to ""

    cli._auth_header = fake_auth_header
    cli._http.request = boom
    with pytest.raises(AcumaticaError) as ei:
        asyncio.run(cli._request_raw("GET", "https://host/Site/entity/x"))
    msg = str(ei.value)
    assert "TIMED OUT" in msg and "ReadTimeout" in msg and "cold IIS" in msg


def test_default_http_timeout_is_120():
    cli = AcumaticaClient(_inst())
    assert cli._http.timeout.read == 120.0
    assert cli._http.timeout.connect == 30.0


# ---- v0.36: submit-path validation, filter conditions, fault boundary ------

from grp_mcp.screen import _normalize_condition


def test_filter_condition_aliases():
    assert _normalize_condition({"field": "A", "value": "1", "op": ">="}) == "GreaterOrEqual"
    assert _normalize_condition({"field": "A", "value": "1", "op": ">"}) == "Greater"
    assert _normalize_condition({"field": "A", "value": "1", "op": "!="}) == "NotEqual"
    assert _normalize_condition({"field": "A", "value": "1", "condition": "Contains"}) == "Contains"
    # canonical names pass through; alias is case-insensitive
    assert _normalize_condition({"field": "A", "value": "1", "op": "STARTSWITH"}) == "StartsWith"
    # default when neither given
    assert _normalize_condition({"field": "A", "value": "1"}) == "Equals"


def test_filter_rejects_unknown_condition_and_keys():
    with pytest.raises(ValueError):
        _normalize_condition({"field": "A", "value": "1", "op": "~="})   # bad operator
    with pytest.raises(ValueError):
        _normalize_condition({"field": "A", "value": "1", "foo": "bar"})  # unknown key


def test_fault_boundary_keeps_full_message():
    # the #4 truncation: " at least one error" must NOT be treated as a stack frame
    import re
    msg = ("PX.Data.PXException: Error: Inserting 'Numbering Sequence' record raised "
           "at least one error. Please review the errors. at PX.Data.PXCache.Insert(Object)")
    m = re.search(r"PX\.\w[\w.]*Exception: (.+?)(?: at [A-Z][\w.]*[.(]|---|\Z)", msg)
    got = m.group(1).strip()
    assert "at least one error" in got and "Please review the errors" in got
    assert "PXCache.Insert" not in got  # stack frame stripped


def _screen_stub():
    from grp_mcp.screen import ScreenClient
    return ScreenClient(_inst(), "GL102000")


def test_validate_sets_flags_readonly_and_bad_enum(monkeypatch):
    import xml.etree.ElementTree as ET
    s = _screen_stub()
    # fake modern-plane metadata
    s._ui_meta = {
        ("GLSetupRecord", "TrialBalanceSign"): {
            "readonly": False, "enabled": True,
            "options": [{"value": "N", "text": "Normal"}, {"value": "R", "text": "Reversed"}]},
        ("GLSetupRecord", "AllocationNumberingID"): {
            "readonly": True, "enabled": False, "options": None},
    }

    def fake_find_field(name):
        fld = {"SignOfTheTrialBalance": ("TrialBalanceSign", "GLSetupRecord"),
               "AllocationNumberingSequence": ("AllocationNumberingID", "GLSetupRecord")}[name]
        el = ET.Element("x")
        ET.SubElement(el, "FieldName").text = fld[0]
        ET.SubElement(el, "ObjectName").text = fld[1]
        return el

    s._find_field = fake_find_field
    issues = asyncio.run(s._validate_sets([
        {"set": "AllocationNumberingSequence", "to": "ALLOCATION"},   # read-only
        {"set": "SignOfTheTrialBalance", "to": "Disbursement"},        # invalid enum
        {"set": "SignOfTheTrialBalance", "to": "R"},                   # valid -> no issue
    ]))
    flagged = {i["field"] for i in issues}
    assert flagged == {"AllocationNumberingSequence", "SignOfTheTrialBalance"}
    assert len(issues) == 2
    enum_issue = next(i for i in issues if i["field"] == "SignOfTheTrialBalance")
    assert "allowed" in enum_issue


def test_validate_sets_noop_without_metadata():
    s = _screen_stub()
    s._ui_meta = {}   # modern plane unreachable -> never block
    assert asyncio.run(s._validate_sets([{"set": "Whatever", "to": "x"}])) == []


# ---- v0.37: modern-plane write safety (coerce/validate, grid columns) -------

def _screen_ui(meta):
    from grp_mcp.screen import ScreenClient
    s = ScreenClient(_inst(), "GL102000")

    async def fake_meta():
        return meta
    s._ui_field_meta = fake_meta
    return s


def test_ui_coerce_validate_label_to_value_and_view_resolution():
    meta = {("GLSetupRecord", "TrialBalanceSign"): {
        "readonly": False, "enabled": True,
        "options": [{"value": "N", "text": "Normal"}, {"value": "R", "text": "Reversed"}]}}
    s = _screen_ui(meta)
    # label -> value coercion, and view omitted (resolved by unique field name)
    norm, issues, notes = asyncio.run(s.ui_coerce_validate(
        [{"field": "TrialBalanceSign", "value": "Reversed"}]))
    assert not issues
    assert norm[0] == {"view": "GLSetupRecord", "field": "TrialBalanceSign", "value": "R"}
    assert any("coerced" in n for n in notes)


def test_ui_coerce_validate_flags_readonly_and_bad_enum():
    meta = {
        ("V", "RO"): {"readonly": True, "enabled": False, "options": None},
        ("V", "E"): {"readonly": False, "enabled": True,
                     "options": [{"value": "A", "text": "Apple"}]},
    }
    s = _screen_ui(meta)
    _, issues, _ = asyncio.run(s.ui_coerce_validate([
        {"view": "V", "field": "RO", "value": "x"},
        {"view": "V", "field": "E", "value": "Zebra"},
    ]))
    probs = {i["field"]: i for i in issues}
    assert "read-only" in probs["V.RO"]["problem"]
    assert "allowed" in probs["V.E"]


def test_ui_coerce_validate_ambiguous_field():
    meta = {("V1", "Dup"): {"readonly": False, "enabled": True, "options": None},
            ("V2", "Dup"): {"readonly": False, "enabled": True, "options": None}}
    s = _screen_ui(meta)
    _, issues, _ = asyncio.run(s.ui_coerce_validate([{"field": "Dup", "value": "1"}]))
    assert issues and "ambiguous" in issues[0]["problem"]


def test_ui_coerce_validate_passthrough_when_no_meta():
    s = _screen_ui({})
    norm, issues, notes = asyncio.run(s.ui_coerce_validate(
        [{"view": "V", "field": "F", "value": "x"}]))
    assert issues == [] and norm[0]["value"] == "x"




# ---- v0.38: grid-cell write validation (realistic column fixtures) ----------

def _grid_client():
    from grp_mcp.screen import ScreenClient
    return ScreenClient(_inst(), "GL202500")


# real GL202500 AccountRecords column shape (from a live grid read)
_GL_COLS = [
    {"field": "AccountID", "allowUpdate": False, "dataType": 9},
    {"field": "AccountCD", "allowUpdate": True, "dataType": 18},
    {"field": "Description", "allowUpdate": True, "dataType": 18},
    {"field": "Type", "allowUpdate": True,
     "valueItems": {"items": [{"value": "A", "text": "Asset"}, {"value": "L", "text": "Liability"},
                              {"value": "I", "text": "Income"}, {"value": "E", "text": "Expense"}]}},
    {"field": "PostOption", "allowUpdate": True,
     "valueItems": {"items": [{"value": "S", "text": "Summary"}, {"value": "D", "text": "Detail"}]}},
    {"key": "Files", "allowUpdate": False},  # meta column, no `field`
]


def test_parse_grid_cols():
    s = _grid_client()
    meta = s._parse_grid_cols(_GL_COLS)
    assert set(meta) == {"AccountID", "AccountCD", "Description", "Type", "PostOption"}
    assert meta["AccountID"]["readonly"] is True
    assert meta["AccountCD"]["readonly"] is False
    assert [o["value"] for o in meta["Type"]["options"]] == ["A", "L", "I", "E"]
    assert meta["Description"]["options"] is None


def test_grid_validate_coerce_readonly_enum_and_label():
    s = _grid_client()
    meta = s._parse_grid_cols(_GL_COLS)
    # label -> value coercion (Expense -> E, Summary -> S), read-only + bad enum flagged
    coerced, issues = s._grid_validate_coerce(meta, {
        "AccountCD": "60000",            # editable, no enum -> untouched
        "Type": "Expense",               # label -> "E"
        "PostOption": "S",               # already a value -> kept
        "AccountID": "999",              # read-only -> issue
        "Description": "Ok",             # editable free text
    })
    assert coerced["Type"] == "E"
    assert coerced["AccountCD"] == "60000"
    probs = {i["field"] for i in issues}
    assert probs == {"AccountID"}


def test_grid_validate_coerce_bad_enum_lists_allowed():
    s = _grid_client()
    meta = s._parse_grid_cols(_GL_COLS)
    _, issues = s._grid_validate_coerce(meta, {"Type": "Widget"})
    assert len(issues) == 1 and issues[0]["field"] == "Type"
    assert [o["value"] for o in issues[0]["allowed"]] == ["A", "L", "I", "E"]


def test_grid_validate_coerce_unknown_column_passes_through():
    s = _grid_client()
    meta = s._parse_grid_cols(_GL_COLS)
    coerced, issues = s._grid_validate_coerce(meta, {"Nonexistent": "x"})
    assert issues == [] and coerced["Nonexistent"] == "x"   # never block on unknown col


def test_grid_write_guard_skips_without_columns():
    s = _grid_client()
    coerced, refusal = asyncio.run(
        s._grid_write_guard("AccountRecords", {"columns": []}, {"Type": "bad"}, "op", False))
    # _grid_col_meta would try the /structure fallback (no network in test) -> {} -> skip


# ---- v0.39: process-runner helpers -----------------------------------------

def test_is_processing_signals():
    S = ScreenClient
    assert S._is_processing({"redirects": [{"settings": {"type": "longRun"}}]}) is True
    assert S._is_processing({"controlsData": {"LongRunData": {"status": "InProcess"}}}) is True
    assert S._is_processing({"controlsData": {"LongRunData": {"running": True}}}) is True
    # the verified SYNC case: plain envelope -> not processing
    assert S._is_processing({"actionStates": {}, "graphIsDirty": False}) is False
    assert S._is_processing({"controlsData": {"LongRunData": {}}}) is False


def test_process_summary_extracts_result_and_messages():
    j = {"controlsData": {"ProcessingResultData": {"rows": [{"ok": 1}]}},
         "messages": [{"message": "2 records processed."}, {"message": None}]}
    out = ScreenClient._process_summary(j)
    assert out["processing_result"] == {"rows": [{"ok": 1}]}
    assert out["messages"] == ["2 records processed."]


# ---- v0.40: guide tool ------------------------------------------------------

def test_guide_full_overview():
    g = server.guide()
    assert set(g) >= {"start_here", "the_four_planes", "by_task", "plane_by_shape"}
    assert len(g["the_four_planes"]) == 4
    assert "write ONE record" in g["by_task"]


def test_guide_topic_filter_and_aliases():
    assert server.guide("write")["topic"] == "write ONE record"
    assert server.guide("gl")["topic"] == "financial-foundation / GL setup"
    p = server.guide("planes")
    assert "the_four_planes" in p and "plane_by_shape" in p


def test_guide_unknown_topic_lists_options():
    out = server.guide("nope")
    assert "error" in out and "planes" in out["topics"]


# ---- v0.41: CoA type mapping (#9) -------------------------------------------

def test_coa_type_map_letters_and_equity():
    n = server._normalize_coa_type
    assert n("A") == "Asset"
    assert n("L") == "Liability"
    assert n("E") == "Liability"  # Equity -> Liability (no Equity type)
    assert n("B") == "Expense"
    assert n("H") == "Income"


def test_coa_type_full_names_passthrough_case_insensitive():
    n = server._normalize_coa_type
    assert n("asset") == "Asset"
    assert n("LIABILITY") == "Liability"
    assert n("Income") == "Income"


def test_coa_type_override_and_invalid():
    n = server._normalize_coa_type
    assert n("B", {"B": "Asset"}) == "Asset"  # override built-in
    with pytest.raises(ValueError):
        n("Z")


# ---- v0.41: seat-limit detection (#10) --------------------------------------

def test_seat_limit_signatures():
    from grp_mcp.acumatica import looks_like_seat_limit as f
    assert f("API Login Limit reached")
    assert f("You have exceeded the maximum number of users")
    assert f("concurrent login not allowed")
    assert f(None, status=429)
    assert not f("some other fault")
    assert not f(None)


# ---- v0.41: run_dac_odata dedup (#12) ---------------------------------------

def test_dac_dedup_collapses_identical_rows(cfg, monkeypatch):
    dup = {"value": [{"FinPeriodID": "01-2026"}, {"FinPeriodID": "01-2026"},
                     {"FinPeriodID": "02-2026"}]}

    async def fake_run_dac(self, dac, params=None):
        return json.loads(json.dumps(dup))  # fresh copy

    monkeypatch.setattr(AcumaticaClient, "run_dac", fake_run_dac)
    out = asyncio.run(server.run_dac_odata("FinPeriod", instance="rw"))
    assert len(out["value"]) == 2
    assert out["@grp.deduped"] == 1
    # dedup=false leaves the raw payload untouched
    raw = asyncio.run(server.run_dac_odata("FinPeriod", dedup=False, instance="rw"))
    assert len(raw["value"]) == 3 and "@grp.deduped" not in raw


# ---- v0.41: session-only routing + collisions (#6) --------------------------

def test_list_instances_flags_tenant_collision(monkeypatch):
    c = Config(default="a", instances={
        "a": _inst(tenant="Company"), "b": _inst(tenant="Company"),
        "c": _inst(tenant="Other")})
    monkeypatch.setattr(server, "_config", c)
    out = server.list_instances()
    assert out["tenant_collisions"] == {"Company": ["a", "b"]}
    by_name = {i["name"]: i for i in out["instances"]}
    assert by_name["a"]["shares_tenant_with"] == ["b"]
    assert by_name["c"]["shares_tenant_with"] is None


def test_add_instance_session_only_marks_and_warns(monkeypatch):
    c = Config(default="a", instances={"a": _inst(tenant="Company")})
    monkeypatch.setattr(server, "_config", c)
    out = server.add_instance(
        "b", "https://h/S", "cid", "sek", "u", "p", tenant="Company", persist=False)
    assert out["session_only"] is True
    assert out["active"] == "a"  # not made active
    assert "a" in out["same_tenant_collision"]
    assert "b" in c.session_only


# ---- v0.41: bulk-load driver + resume (#5) ----------------------------------

def test_drive_load_success_and_next_offset(cfg, monkeypatch):
    puts = []

    async def fake_put(self, entity, body):
        puts.append(body)

    monkeypatch.setattr(AcumaticaClient, "put_entity", fake_put)
    state = {"job": "j", "entity": "E", "total": 3, "processed": 0, "succeeded": 0,
             "failed": 0, "next_offset": 0, "errors": [], "completed": False, "error": None}
    client = server._client("rw")
    mapped = [{"X": "1"}, {"X": "2"}, {"X": "3"}]
    asyncio.run(server._drive_load(state, client, "E", mapped, 0, False))
    assert state["succeeded"] == 3 and state["completed"] and state["next_offset"] == 3
    assert len(puts) == 3


def test_drive_load_stop_on_error_leaves_resume_offset(cfg, monkeypatch):
    async def fake_put(self, entity, body):
        if body["X"]["value"] == "2":
            raise AcumaticaError("boom")

    # _wrap_fields wraps scalars as {"value": ...}; assert resume points at the failed row
    monkeypatch.setattr(AcumaticaClient, "put_entity", fake_put)
    state = {"job": "j", "entity": "E", "total": 3, "processed": 0, "succeeded": 0,
             "failed": 0, "next_offset": 5, "errors": [], "completed": False, "error": None}
    client = server._client("rw")
    mapped = [{"X": "1"}, {"X": "2"}, {"X": "3"}]
    asyncio.run(server._drive_load(state, client, "E", mapped, 5, True))
    assert state["succeeded"] == 1 and state["failed"] == 1
    assert state["next_offset"] == 6  # base_offset 5 + failed index 1
    assert state["errors"][0]["row"] == 1 + 5 + 1 + 1


# ---- v0.42: calendar teardown + range validation (#7, #3) --------------------

def test_reset_calendar_rejects_inverted_range(cfg):
    with pytest.raises(ValueError):
        asyncio.run(server.reset_calendar("2027", "2025", instance="rw"))


def test_generate_master_calendar_rejects_inverted_range(cfg):
    with pytest.raises(ValueError):
        asyncio.run(server.generate_master_calendar("2027", "2025", instance="rw"))


def test_delete_financial_year_needs_delete_gate(cfg):
    # 'ro' has allow_delete=False -> refused before any network call
    with pytest.raises(PermissionError):
        asyncio.run(server.delete_financial_year("2027", instance="ro"))


def test_reset_calendar_needs_delete_gate(cfg):
    with pytest.raises(PermissionError):
        asyncio.run(server.reset_calendar("2026", instance="ro"))


# ---- v0.43: company-tree limitation is documented ---------------------------

def test_setup_map_documents_company_tree_limitation():
    ids = {r["id"] for r in server._setup_map()["cross_cutting_rules"]}
    assert "company-tree-select-not-api-driveable" in ids


# ---- v0.42: non-SOAP cookie login for the modern plane ----------------------

class _FakeResp:
    def __init__(self, status=204, text=""):
        self.status_code = status
        self.text = text


def test_cookie_login_sends_company_separately():
    # tenant with a space (like csmdev 'AI MPM') MUST go in `company`, not name@tenant
    inst = _inst(username="csmarvindh", tenant="AI MPM", password="pw")
    s = ScreenClient(inst, "PY101500")
    captured = {}

    async def fake_post(url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResp(204)

    s._http.post = fake_post
    asyncio.run(s._cookie_login())
    assert s._logged_in is True and s._cookie_session is True
    assert captured["json"] == {"name": "csmarvindh", "password": "pw", "company": "AI MPM"}
    assert "@" not in captured["json"]["name"]
    assert captured["url"].endswith("/entity/auth/login")


def test_cookie_login_raises_on_non_2xx():
    s = ScreenClient(_inst(), "PY101500")

    async def fake_post(url, json=None, headers=None):
        return _FakeResp(401, "denied")

    s._http.post = fake_post
    with pytest.raises(ScreenError):
        asyncio.run(s._cookie_login())
    assert s._logged_in is False


def test_ensure_login_falls_back_to_cookie_when_soap_fails():
    s = ScreenClient(_inst(), "PY101500")

    async def boom():
        raise ScreenError("SOAP login disabled")

    async def cookie():
        s._logged_in = True
        s._cookie_session = True

    s.login = boom
    s._cookie_login = cookie
    asyncio.run(s._ensure_login())
    assert s._logged_in is True and s._cookie_session is True


def test_ensure_login_prefers_soap_and_is_noop_when_logged_in():
    s = ScreenClient(_inst(), "PY101500")
    calls = {"soap": 0, "cookie": 0}

    async def soap():
        calls["soap"] += 1
        s._logged_in = True

    async def cookie():
        calls["cookie"] += 1

    s.login = soap
    s._cookie_login = cookie
    asyncio.run(s._ensure_login())          # SOAP works -> cookie not tried
    assert calls == {"soap": 1, "cookie": 0} and s._cookie_session is False
    asyncio.run(s._ensure_login())          # already logged in -> no-op
    assert calls == {"soap": 1, "cookie": 0}
