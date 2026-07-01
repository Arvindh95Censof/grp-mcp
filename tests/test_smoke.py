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
from grp_mcp.config import Config, Instance
from grp_mcp.screen import ScreenClient


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
    return ScreenClient(_inst(), screen)


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
