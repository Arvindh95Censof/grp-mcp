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
from grp_mcp.customization import CustomizationClient
from grp_mcp.screen import ScreenClient, ScreenError


def _inst(**over) -> Instance:
    base = dict(
        base_url="https://host/Site",
        client_id="cid", client_secret="sek",
        username="u", password="p", tenant="Company",
    )
    base.update(over)
    return Instance(**base)


@pytest.fixture(autouse=True)
def _clear_ui_session_cache():
    """The shared UI-plane cookie cache is module-global; isolate tests from each other."""
    from grp_mcp import screen as _screen
    _screen._SESSION_CACHE.clear()
    yield
    _screen._SESSION_CACHE.clear()


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

    async def fake_run_dac(self, dac, params=None, timeout=None):
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

def test_setup_map_documents_company_tree_recipe():
    ids = {r["id"] for r in server._setup_map()["cross_cutting_rules"]}
    assert "company-tree-build-via-ep204060" in ids


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


# ---- v0.43: publish merge-not-replace safety (#1/#2) ------------------------

def test_published_project_names_extractor():
    f = server._published_project_names
    assert f({"projects": [{"name": "A"}, {"name": "B"}], "items": []}) == ["A", "B"]
    assert f(["X", {"name": "Y"}]) == ["X", "Y"]
    assert f({"nope": 1}) == []


def _mock_published(monkeypatch, names):
    async def fake_get_published(self):
        return {"projects": [{"name": n} for n in names]}

    async def fake_aclose(self):
        return None

    monkeypatch.setattr(CustomizationClient, "get_published", fake_get_published)
    monkeypatch.setattr(CustomizationClient, "aclose", fake_aclose)


def test_publish_merge_dry_run_keeps_existing(cfg, monkeypatch):
    _mock_published(monkeypatch, ["grp_mcp"])
    out = asyncio.run(server.publish_customization(["NewThing"], mode="merge",
                                                   dry_run=True, instance="rw"))
    assert out["will_publish"] == ["grp_mcp", "NewThing"]
    assert out["will_unpublish"] == []


def test_publish_replace_dry_run_flags_unpublish(cfg, monkeypatch):
    _mock_published(monkeypatch, ["grp_mcp"])
    out = asyncio.run(server.publish_customization(["NewThing"], mode="replace",
                                                   dry_run=True, instance="rw"))
    assert out["will_publish"] == ["NewThing"]
    assert out["will_unpublish"] == ["grp_mcp"]


def test_publish_replace_refuses_without_confirm(cfg, monkeypatch):
    _mock_published(monkeypatch, ["grp_mcp", "other"])
    out = asyncio.run(server.publish_customization(["NewThing"], mode="replace",
                                                   instance="rw"))
    assert out["refused"] is True
    assert set(out["would_unpublish"]) == {"grp_mcp", "other"}


def test_publish_requires_allow_publish(cfg):
    # 'ro' profile has allow_publish=False
    with pytest.raises(PermissionError):
        asyncio.run(server.publish_customization(["X"], dry_run=True, instance="ro"))


def test_publish_invalid_mode_raises(cfg):
    with pytest.raises(ValueError):
        asyncio.run(server.publish_customization(["X"], mode="bogus", instance="rw"))


# ---- v0.44: parser & robustness fixes ---------------------------------------

def test_get_swagger_raises_clear_error_on_non_json(monkeypatch):
    from grp_mcp.acumatica import AcumaticaError

    inst = _inst(endpoint_name="GRP9", endpoint_version="1")
    client = AcumaticaClient(inst)

    async def fake_request(method, url, **kw):
        return "<html>error page</html>"  # non-JSON -> a str, not a dict

    client._request = fake_request
    with pytest.raises(AcumaticaError) as ei:
        asyncio.run(client.get_swagger())
    assert "OpenAPI" in str(ei.value) and "GRP9/1" in str(ei.value)


def test_get_schema_captures_unnamed_container(monkeypatch):
    import xml.etree.ElementTree as ET

    # a summary container with NO DisplayName (the old regex dropped it) + a named one
    root = ET.fromstring(
        "<root>"
        "<SummaryHeader>"
        "  <PayCodeCD><FieldName>PayCodeCD</FieldName><ObjectName>PayCode</ObjectName></PayCodeCD>"
        "  <Type><FieldName>PayType</FieldName><ObjectName>PayCode</ObjectName></Type>"
        "</SummaryHeader>"
        "<Actions><Save><FieldName>Save</FieldName><ObjectName>x</ObjectName></Save></Actions>"
        "<Details>"
        "  <DisplayName>Detail Lines</DisplayName>"
        "  <Amount><FieldName>Amount</FieldName><ObjectName>Line</ObjectName></Amount>"
        "</Details>"
        "</root>")
    s = ScreenClient(_inst(), "PY302000")

    async def fake_tree():
        return root

    s._ensure_tree = fake_tree
    out = asyncio.run(s.get_schema())
    conts = out["containers"]
    assert "SummaryHeader" in conts                       # unnamed (no DisplayName) captured
    assert conts["SummaryHeader"]["PayCodeCD"] == {"object": "PayCode", "field": "PayCodeCD"}
    assert "Details" in conts                              # named still works
    assert "Actions" not in conts                          # action container excluded


def test_run_dac_filter_in_runs_per_value(cfg, monkeypatch):
    calls = []

    async def fake_run_dac(self, dac, params=None, timeout=None):
        calls.append((params or {}).get("$filter"))
        v = (params or {}).get("$filter", "").split("'")[1]
        return {"value": [{"ScreenID": v}]}

    monkeypatch.setattr(AcumaticaClient, "run_dac", fake_run_dac)
    out = asyncio.run(server.run_dac_odata(
        "SiteMap", select="ScreenID",
        filter_in={"ScreenID": ["GL101000", "GL201000"]}, instance="rw"))
    assert len(calls) == 2 and "or" not in " ".join(calls)   # per-value, no giant OR
    assert {r["ScreenID"] for r in out["value"]} == {"GL101000", "GL201000"}


def test_config_unknown_instance_error_mentions_session_only():
    c = Config(default="a", instances={"a": _inst()})
    try:
        c.get("csmstg")
        assert False
    except KeyError as e:
        assert "session-only" in str(e) and "persist" in str(e)


# ---- v0.45: shared cookie session + endpoint-entity generator ---------------

def test_shared_session_reuse_across_clients(monkeypatch):
    from grp_mcp import screen as scr
    scr._SESSION_CACHE.clear()
    logins = {"soap": 0}

    async def fake_login(self):
        logins["soap"] += 1
        self._logged_in = True
        self._cookie_session = False

    monkeypatch.setattr(ScreenClient, "login", fake_login)
    inst = _inst()
    # first client logs in + caches; second reuses the cache (no new login)
    a = ScreenClient(inst, "GL101000")
    asyncio.run(a._ensure_login())
    b = ScreenClient(inst, "GL201000")
    asyncio.run(b._ensure_login())
    assert logins["soap"] == 1               # only ONE login for both clients
    assert a._shared and b._shared           # both defer logout to the cache
    key = a._session_key
    assert key in scr._SESSION_CACHE
    assert scr.clear_session_cache(key) == [key]
    assert key not in scr._SESSION_CACHE


def test_shared_session_not_logged_out(monkeypatch):
    from grp_mcp import screen as scr
    scr._SESSION_CACHE.clear()

    async def fake_login(self):
        self._logged_in = True

    called = {"soap_logout": 0}

    async def fake_call(self, op, inner, _seat_retried=False):
        if op == "Logout":
            called["soap_logout"] += 1
        return ""

    monkeypatch.setattr(ScreenClient, "login", fake_login)
    monkeypatch.setattr(ScreenClient, "_call", fake_call)
    s = ScreenClient(_inst(), "GL101000")
    asyncio.run(s._ensure_login())     # creates + caches -> _shared True
    asyncio.run(s.logout())            # must NOT log out a shared session
    assert called["soap_logout"] == 0
    assert s._session_key in scr._SESSION_CACHE


def _fake_async_client(sink):
    """A stand-in httpx.AsyncClient that records POST urls instead of hitting the net."""
    import httpx as _httpx

    class _Fake:
        def __init__(self, *a, **k):
            self._cookies = k.get("cookies")
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, *a, **k):
            sink.append(url)
            return _httpx.Response(204)
    return _Fake


def test_logout_session_cache_ends_session_serverside(monkeypatch):
    # The leak fix: dropping a shared cookie session must LOG IT OUT server-side
    # (POST /entity/auth/logout with its cookies), not just forget it locally.
    from grp_mcp import screen as scr
    import httpx as _httpx
    scr._SESSION_CACHE.clear()
    key = "http://localhost/AcumaticaERP|admin|Company"
    scr._SESSION_CACHE[key] = {"cookies": _httpx.Cookies(), "at": 0.0, "kind": "cookie"}
    posted: list[str] = []
    monkeypatch.setattr(scr.httpx, "AsyncClient", _fake_async_client(posted))

    done = asyncio.run(scr.logout_session_cache())

    assert done == [key]                                   # returned the identity logged out
    assert key not in scr._SESSION_CACHE                   # dropped locally
    # base_url parsed from the key, contract logout endpoint hit
    assert posted == ["http://localhost/AcumaticaERP/entity/auth/logout"]


def test_logout_session_cache_best_effort_on_failure(monkeypatch):
    # A failed server logout must still drop the entry (idle-timeout is the backstop) —
    # never leave a dead cookie wedged in the cache.
    from grp_mcp import screen as scr
    import httpx as _httpx
    scr._SESSION_CACHE.clear()
    key = "http://localhost/AcumaticaERP|admin|Company"
    scr._SESSION_CACHE[key] = {"cookies": _httpx.Cookies(), "at": 0.0, "kind": "cookie"}

    class _Boom:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            raise _httpx.ConnectError("network down")

    monkeypatch.setattr(scr.httpx, "AsyncClient", _Boom)
    done = asyncio.run(scr.logout_session_cache())
    assert done == [key]                     # still reported handled
    assert key not in scr._SESSION_CACHE      # and still dropped


# ---- v0.47: tree_triage ------------------------------------------------------

def test_indent_actions_detects_indent_not_reorder():
    from grp_mcp.server import _indent_actions
    # Left/Right and indent/outdent/promote/demote count; Up/Down (reorder) do NOT.
    acts = ["Save", "Up", "Down", "Left", "Right", "AddWorkGroup", "Indent", "Promote"]
    got = _indent_actions(acts)
    assert set(got) == {"Left", "Right", "Indent", "Promote"}
    assert "Up" not in got and "Down" not in got


def test_parent_fields_finds_settable_parent_link():
    from grp_mcp.server import _parent_fields
    struct = {
        "grids": {"Nodes": {"columns": ["NodeID", "ParentID", "Description"]}},
        "views": {
            "Header": [
                {"field": "ParentGroupID", "readonly": False},
                {"field": "ReadOnlyParent", "readonly": True},   # read-only -> ignored
                {"field": "Name", "readonly": False},
            ]
        },
    }
    got = _parent_fields(struct)
    assert "Nodes.ParentID" in got
    assert "Header.ParentGroupID" in got
    assert "Header.ReadOnlyParent" not in got   # read-only parent isn't settable
    assert "Nodes.NodeID" not in got


def test_tree_triage_is_registered_tool():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "tree_triage" in names


def test_reframe_ui_validation_is_actionable():
    # A screen business-rule rejection must read as "fixable — supply field + retry",
    # NOT as "this screen can't be set up".
    from grp_mcp.server import _reframe_ui_validation, _flagged_field_names, _UI_VALIDATION_PAT
    struct = {"views": {"CurrentCompany": [
        {"field": "CashAcctID", "required": True, "readonly": False},
        {"field": "BranchID", "required": True, "readonly": True},   # readonly -> not askable
        {"field": "RetirementAge", "required": False, "readonly": False},
    ]}}
    out = _reframe_ui_validation("PY301000", "Save",
                                 "ui_command Save on PY301000: PCB Pay Code can not be empty.",
                                 struct)
    assert out["ok"] is False and out["status"] == "validation_failed"
    assert out["reachable"] is True and out["writable"] is True
    assert "PCB Pay Code" in out["flagged_fields"]
    assert "CurrentCompany.CashAcctID" in out["required_fields"]
    assert "CurrentCompany.BranchID" not in out["required_fields"]   # readonly excluded
    assert "not a 'cannot set up'" in out["guidance"].lower()
    # the classifier recognizes the common phrasings, and ignores unrelated errors
    assert _UI_VALIDATION_PAT.search("Field X is required")
    assert _UI_VALIDATION_PAT.search("PREREQUISITE NOT MET — configure ARSetup")
    assert not _UI_VALIDATION_PAT.search("Object reference not set to an instance")


def test_flagged_field_names_parsing():
    from grp_mcp.server import _flagged_field_names
    assert _flagged_field_names("PCB Pay Code can not be empty") == ["PCB Pay Code"]
    assert "Cash Account" in _flagged_field_names("'Cash Account' is required")


def test_get_swagger_404_scopes_error_to_rest_plane(monkeypatch):
    # A contract-REST endpoint 404 must NOT read as a hard dead end — it should say the
    # screen SOAP / modern-UI planes are independent (the give-up trap the eval found).
    from grp_mcp.acumatica import AcumaticaError
    c = _acu("https://host/Site")

    async def boom(method, url, **kw):
        raise AcumaticaError(
            "GET https://host/Site/entity/GRPSetup/24.200.001/swagger.json -> "
            "404: Endpoint [GRPSetup/24.200.001] not found")

    monkeypatch.setattr(c, "_request", boom)
    try:
        asyncio.run(c.get_swagger())
        assert False, "should raise"
    except AcumaticaError as e:
        s = str(e)
        assert "CONTRACT-REST endpoint only" in s
        assert "screen_health" in s and "modern-UI" in s


def test_whoami_scopes_reachability_and_hints(monkeypatch):
    from grp_mcp.config import Config
    cfg = Config(default="t", instances={"t": _inst()})
    monkeypatch.setattr(server, "_cfg", lambda: cfg)
    monkeypatch.setattr(server, "_clients", {})

    class FakeClient:
        async def get_swagger(self, refresh=False):
            raise server.AcumaticaError(
                "GET .../swagger.json -> 404: Endpoint [X/Y] not found")

    monkeypatch.setattr(server, "_client", lambda instance=None: FakeClient())
    out = asyncio.run(server.whoami())
    assert out["reachable"] is False
    assert "contract-REST" in out["reachable_scope"]          # scope always present
    assert "screen_health" in out["hint"]                     # hint only when unreachable
    assert "CONTRACT-REST plane ONLY" in out["hint"]


def test_guide_teaches_validation_is_not_dead_end():
    g = server.guide()
    assert "can't be set up" in g["start_here"] or "can't be driven" in g["start_here"] \
        or "not a 'this screen" in g["start_here"].lower()
    assert "validation error" in g["start_here"].lower()


def test_guide_names_every_registered_tool():
    # guide() is the START-HERE router — every registered tool must be discoverable
    # through it, or an agent leaning on guide alone can't find the tool (e.g. a poll
    # companion like load_status/activate_features_status). Grow NOT_IN_GUIDE only for
    # a tool deliberately excluded, with a reason.
    import json as _json
    NOT_IN_GUIDE: dict[str, str] = {
        # (empty) — guide is expected to name all tools; add exclusions here with cause
    }
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    blob = _json.dumps(server.guide())
    missing = sorted(n for n in names if n not in blob and n not in NOT_IN_GUIDE)
    assert not missing, (
        f"tool(s) not referenced anywhere in guide() — add them to the right by_task "
        f"bucket (or to NOT_IN_GUIDE with a reason): {missing}")


def test_guide_and_setupmap_document_odata_role():
    # The OData v4 role prerequisite must be discoverable IN the MCP (guide +
    # get_setup_guidance), not only in the external Word doc.
    g = server.guide()
    assert "odata_v4_role" in g.get("env_prerequisites", {})
    assert "OData v4" in g["env_prerequisites"]["odata_v4_role"]
    assert "OData v4" in g["the_four_planes"]["DAC / GI OData (raw read)"]
    ids = {r["id"] for r in server._setup_map()["cross_cutting_rules"]}
    assert "odata-v4-role-required-for-probing" in ids


# ---- v0.48 audit fixes -------------------------------------------------------

def test_save_config_excludes_session_only(tmp_path):
    # persist=false is a promise: those credentials must NEVER land on disk —
    # even when a LATER persisting call writes the config.
    from grp_mcp.config import Config, Instance, save_config
    import json as _json
    kw = dict(client_id="x", client_secret="y", username="u", password="SECRET")
    cfg = Config(
        default="mem",
        instances={"disk": Instance(base_url="http://a/A", **kw),
                   "mem": Instance(base_url="http://b/B", **kw)},
        session_only={"mem"},
    )
    target = str(tmp_path / "connections.json")
    save_config(cfg, target)
    data = _json.loads((tmp_path / "connections.json").read_text(encoding="utf-8"))
    assert "mem" not in data["instances"]          # session-only stayed in memory
    assert "disk" in data["instances"]
    assert data["default"] == "disk"               # on-disk default must exist on disk
    assert "SECRET" not in (tmp_path / "connections.json").read_text(encoding="utf-8") \
        or data["instances"]["disk"]["password"] == "SECRET"  # only via the disk profile


def test_save_config_all_session_only_refuses(tmp_path):
    from grp_mcp.config import Config, Instance, save_config
    kw = dict(client_id="x", client_secret="y", username="u", password="p")
    cfg = Config(default="mem",
                 instances={"mem": Instance(base_url="http://b/B", **kw)},
                 session_only={"mem"})
    try:
        save_config(cfg, str(tmp_path / "c.json"))
        assert False, "should refuse to write an empty instance set"
    except RuntimeError as e:
        assert "session-only" in str(e)


def test_seat_limit_regex_not_fooled_by_business_errors():
    from grp_mcp.acumatica import looks_like_seat_limit
    # business errors must NOT trigger session logouts + LoginLimitError masking
    assert not looks_like_seat_limit("You have exceeded the credit limit for this customer")
    assert not looks_like_seat_limit("you have exceeded the maximum row count")
    # real seat-limit phrasings still match
    assert looks_like_seat_limit("API Login Limit")
    assert looks_like_seat_limit("You have exceeded the maximum number of concurrent sessions")
    assert looks_like_seat_limit("you have exceeded the allowed number of users")
    assert looks_like_seat_limit("", status=429)


def test_oq_escapes_odata_quotes():
    from grp_mcp.server import _oq
    assert _oq("O'Brien Import") == "O''Brien Import"
    assert _oq("plain") == "plain"
    assert _oq(123) == "123"


def test_customization_login_sanitizes_error_and_relieves_seats(monkeypatch):
    import httpx as _httpx
    from grp_mcp.customization import CustomizationClient, CustomizationError
    inst = _inst()
    calls = {"n": 0, "relieved": 0}

    # 1) seat-limit 500 -> reliever fires, retry succeeds
    async def fake_post_seat(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _httpx.Response(500, text='{"exceptionMessage":"API Login Limit"}')
        return _httpx.Response(204)

    async def fake_reliever(exclude=None):
        calls["relieved"] += 1

    c = CustomizationClient(inst)
    monkeypatch.setattr(c._http, "post", fake_post_seat)
    monkeypatch.setattr(c, "seat_reliever", fake_reliever)
    asyncio.run(c._login())
    assert calls == {"n": 2, "relieved": 1} and c._logged_in

    # 2) credential failure -> structured message, raw body NOT echoed
    async def fake_post_bad(url, **kw):
        return _httpx.Response(401, text='{"message":"Invalid credentials",'
                                         '"secretEcho":"do-not-leak"}')
    c2 = CustomizationClient(inst)
    monkeypatch.setattr(c2._http, "post", fake_post_bad)
    try:
        asyncio.run(c2._login())
        assert False, "should have raised"
    except CustomizationError as e:
        assert "Invalid credentials" in str(e)
        assert "do-not-leak" not in str(e)     # raw body never echoed


def test_cookie_login_relieves_seat_limit_and_retries(monkeypatch):
    # Seat-limit self-heal parity: /entity/auth/login 500 "API Login Limit" must
    # trigger the reliever and retry ONCE (previously only classic SOAP _call did;
    # a SOAP-disabled instance could never recover from a seat jam).
    import httpx as _httpx
    calls = {"n": 0, "relieved": 0}

    async def fake_post(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _httpx.Response(500, text='{"exceptionMessage":"API Login Limit"}')
        return _httpx.Response(204)

    async def fake_reliever(exclude=None):
        calls["relieved"] += 1

    s = ScreenClient(_inst(), "GL101000")
    monkeypatch.setattr(s._http, "post", fake_post)
    monkeypatch.setattr(s, "seat_reliever", fake_reliever)
    asyncio.run(s._cookie_login())
    assert calls == {"n": 2, "relieved": 1}   # relieved once, retried once, succeeded
    assert s._logged_in and s._cookie_session


def test_cookie_login_non_seat_error_no_retry(monkeypatch):
    # A non-seat 500 must NOT loop the reliever — raise immediately.
    import httpx as _httpx
    calls = {"n": 0}

    async def fake_post(url, **kw):
        calls["n"] += 1
        return _httpx.Response(500, text='{"exceptionMessage":"boom"}')

    s = ScreenClient(_inst(), "GL101000")
    monkeypatch.setattr(s._http, "post", fake_post)
    try:
        asyncio.run(s._cookie_login())
        assert False, "should have raised"
    except ScreenError as e:
        assert "cookie login" in str(e)
    assert calls["n"] == 1


def test_ui_command_dialog_answer_none_returns_dialog(monkeypatch):
    # answer="none" must NOT auto-confirm — it returns the unanswered dialog info.
    import httpx as _httpx
    posted = []

    async def fake_ui_post(payload, _auth_retried=False):
        posted.append(payload)
        return _httpx.Response(302, json={"redirects": [
            {"settings": {"type": "openDialog", "viewName": "ConfirmView"}}]})

    s = ScreenClient(_inst(), "GL101000")
    monkeypatch.setattr(s, "_ui_post", fake_ui_post)
    out = asyncio.run(s.ui_command("Generate", answer="none"))
    assert out["dialog_open"] is True and out["dialog_view"] == "ConfirmView"
    assert len(posted) == 1                     # never sent a dialogCallback
    assert "dialogCallback" not in posted[0]


def test_ui_command_rejects_unknown_answer():
    s = ScreenClient(_inst(), "GL101000")
    try:
        asyncio.run(s.ui_command("Save", answer="maybe"))
        assert False, "should have raised"
    except ScreenError as e:
        assert "unknown dialog answer" in str(e)


def test_indent_pref_prefers_indent_verb():
    from grp_mcp.server import _indent_pref
    assert _indent_pref(["Left", "Right"]) == "Right"      # Right = nest deeper
    assert _indent_pref(["Left"]) == "Left"                # fall back to what's there
    assert _indent_pref(["Outdent", "Indent"]) == "Indent"


def test_list_grid_guess_skips_tree_and_members():
    from grp_mcp.server import _list_grid_guess
    # EP204060's grids — the insert target is Items, not the tree/detail grids.
    assert _list_grid_guess(["Folders", "Items", "Members"]) == "Items"
    assert _list_grid_guess(["EntityTree"]) == "EntityTree"   # nothing else -> use it
    assert _list_grid_guess([]) == "<grid>"


def test_flatten_tree_preorder_depths():
    f = server._flatten_tree
    struct = [{"name": "R", "children": [
        {"name": "A", "children": ["A1", "A2"]},
        "B"]}]
    assert f(struct) == [("R", 0), ("A", 1), ("A1", 2), ("A2", 2), ("B", 1)]
    assert f(["X"]) == [("X", 0)]
    with pytest.raises(ValueError):
        f([{"children": []}])   # node without a name


def test_build_company_tree_is_registered_tool():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "build_company_tree" in names
    assert "_flatten_tree" not in names   # helper stays private


def test_edm_to_valuetype_mapping():
    f = server._edm_to_valuetype
    assert f("Edm.String") == "StringValue"
    assert f("Boolean") == "BooleanValue"
    assert f("Int16") == "ShortValue"
    assert f("Int32") == "IntValue"
    assert f("DateTime") == "DateTimeValue"
    assert f("WeirdType") == "StringValue"   # safe default


# ---- tool-registration integrity (guards the decorator-on-helper bug) --------

def test_tool_registration_integrity():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    # every real tool must be registered
    for must in ("run_dac_odata", "publish_customization", "screen_health",
                 "generate_endpoint_entity", "list_published", "get_endpoint_definition",
                 "activate_features"):
        assert must in names, f"{must} is not a registered MCP tool"
    # module-private helpers must NOT leak as tools (a @mcp.tool() landing on the wrong
    # def steals the tool's decorator — how run_dac_odata briefly de-registered)
    for helper in ("_dedup_rows", "_published_project_names", "_activation_status",
                   "_edm_to_valuetype"):
        assert helper not in names, f"internal helper {helper} wrongly exposed as a tool"
    # belt-and-suspenders: no underscore-prefixed tool names at all
    assert not [n for n in names if n.startswith("_")]


def test_every_public_function_is_a_registered_tool():
    # STRUCTURAL guard for the decorator-theft bug class: any PUBLIC module-level
    # function in server.py is either a registered tool or on the explicit non-tool
    # allowlist. This is what a hand-picked must-list can't do — activate_features
    # was silently unregistered for 4 releases (v0.43.1..v0.47.1) because the fix
    # for a stolen decorator DELETED it instead of moving it back, and no list
    # mentioned the tool. Grow the allowlist only for a function that is genuinely
    # not meant to be a tool.
    import inspect
    NOT_TOOLS = {
        "extend_endpoint",   # deliberately deregistered (REST PUT is a verified no-op)
        "main",              # console entry point
    }
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    missing = []
    for fname, fn in inspect.getmembers(server, inspect.isfunction):
        if fn.__module__ != server.__name__:   # imported, not defined here
            continue
        if fname.startswith("_") or fname in NOT_TOOLS:
            continue
        if fname not in names:
            missing.append(fname)
    assert not missing, (
        f"public function(s) in server.py not registered as MCP tools (lost/stolen "
        f"@mcp.tool() decorator?): {missing} — if intentional, add to NOT_TOOLS")
